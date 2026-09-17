# -*- coding: utf-8 -*-
"""
CAD 矢量图转 CNC G 代码工具
==========================
功能：把 AutoCAD 画好的 DXF / DWF / DWFx 名字线条
      （多段线/直线/圆弧/圆/样条曲线）转换成
      仅使用 G00、G01、G02、G03 四种指令的 G 代码。
      相邻实体端点对齐时自动合并成连续切削，不抬刀。
依赖：ezdxf（DXF）、ezdwf（DWF/DWFx，可选）
运行环境：Windows / Python 3.10+
使用示例：
    python dxf2gcode.py -i name.dxf  -o name.txt
    python dxf2gcode.py -i name.dwf  -o name.txt
"""

import argparse                # 命令行参数解析
import math                    # 三角函数、反正切等几何计算
import os                      # 扩展名判断
import re                      # 正则表达式，用于坐标偏移
import sys                     # 退出码与错误输出
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any, Optional

try:
    import ezdxf               # DXF 读写库
except ImportError:
    print("【错误】缺少依赖库 ezdxf，请先执行：pip install ezdxf")
    sys.exit(1)

# ezdwf 是可选依赖，DWF/DWFx 文件才需要
try:
    import ezdwf               # DWF/DWFx 读写库
    HAS_EZDWF = True
except ImportError:
    HAS_EZDWF = False

# matplotlib 是可选依赖，只有图纸里带文字对象（TEXT / MTEXT）时才需要
# 它内部用 freetype 读字体文件，TextPath 可以直接把一串文字变成矢量轮廓
try:
    from matplotlib.textpath import TextPath          # 文字 → 矢量轮廓
    from matplotlib.font_manager import FontProperties  # 指定字体文件
    from matplotlib.path import Path as MplPath       # 轮廓的顶点与指令码
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# 文字轮廓化默认用的字体：黑体，中英文字母数字都能出
# 想换字体用命令行参数 --font 指定，例如 C:\Windows\Fonts\simkai.ttf（楷体）
_DEFAULT_FONT = r"C:\Windows\Fonts\simhei.ttf"

# 文字轮廓离散精度：一段贝塞尔曲线切成多少条直线
# 数值越大越圆滑、G 代码越长；12 段对刻字来说足够
_TEXT_CURVE_STEPS = 12


# ======================================================================
# 几何 / 数学辅助函数
# ======================================================================

# 边框识别容差（mm）——实体包围盒与图形整体包围盒完全重合则判定为边框
_FRAME_TOL = 0.01

# 端点匹配容差（mm）——浮点运算无法精确相等，1e-6 对 CNC 来说等于"完全重合"
_ENDPOINT_TOL = 1e-6


def _points_equal(p1: Tuple[float, float], p2: Tuple[float, float],
                  tol: Optional[float] = None) -> bool:
    """
    判断两个点是否重合
    tol 传 None 时使用严格容差 _ENDPOINT_TOL（对 CNC 来说等于"完全重合"）
    tol 传具体数值时使用该吸附容差（用于把"差一点点"的端点也接起来）
    """
    t = _ENDPOINT_TOL if tol is None else tol
    return abs(p1[0] - p2[0]) <= t and abs(p1[1] - p2[1]) <= t


def _dist(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """计算两点之间的直线距离（用于统计抬刀后的空行程长度）"""
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def _entity_bbox(entity) -> Tuple[float, float, float, float]:
    """
    计算任意 DXF 实体的包围盒 (x_min, x_max, y_min, y_max)
    不依赖 ezdxf 的 get_extents（版本差异大），手动遍历顶点求 min/max
    """
    pts: List[Tuple[float, float]] = []
    dxftype = entity.dxftype()

    try:
        if dxftype == "LINE":
            pts.append((float(entity.dxf.start.x), float(entity.dxf.start.y)))
            pts.append((float(entity.dxf.end.x),   float(entity.dxf.end.y)))

        elif dxftype == "LWPOLYLINE":
            for p in entity.get_points():
                pts.append((float(p[0]), float(p[1])))

        elif dxftype == "POLYLINE":
            if hasattr(entity, "vertices"):
                for v in entity.vertices:
                    loc = v.dxfattribs().get("location")
                    if loc is not None:
                        pts.append((float(loc.x), float(loc.y)))
            elif hasattr(entity, "get_points"):
                try:
                    for p in entity.get_points("xy"):
                        pts.append((float(p[0]), float(p[1])))
                except Exception:
                    pass

        elif dxftype == "ARC":
            cx = float(entity.dxf.center.x)
            cy = float(entity.dxf.center.y)
            r  = float(entity.dxf.radius)
            pts.append((cx - r, cy - r))
            pts.append((cx + r, cy + r))

        elif dxftype == "CIRCLE":
            cx = float(entity.dxf.center.x)
            cy = float(entity.dxf.center.y)
            r  = float(entity.dxf.radius)
            pts.append((cx - r, cy - r))
            pts.append((cx + r, cy + r))

        elif dxftype == "SPLINE":
            for cp in entity.control_points:
                pts.append((float(cp[0]), float(cp[1])))
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)

    if not pts:
        return (0.0, 0.0, 0.0, 0.0)

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), max(xs), min(ys), max(ys))


# ======================================================================
# DWF / DWFx 兼容层（适配器模式）
# ======================================================================

class _FakeVec3:
    """模拟 ezdxf 的 Vec3 对象（有 .x .y .z 属性）"""
    def __init__(self, x, y, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


class _FakeDxfAttr:
    """模拟 ezdxf 的 entity.dxf 属性容器"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _DwfEntityAdapter:
    """
    把 ezdwf 实体包装成"长得像 ezdxf 实体"的样子
    支持 dxftype() 和 .dxf 属性访问
    """
    def __init__(self, dwf_entity):
        self._e = dwf_entity
        self._type = dwf_entity.dxftype()
        self.dxf = self._build_dxf()

    def dxftype(self):
        return self._type

    def _build_dxf(self):
        """根据实体类型，构造模拟 ezdxf 的 .dxf 对象"""
        e = self._e
        t = self._type

        if t == "LINE":
            pts = list(e.points)
            return _FakeDxfAttr(
                start=_FakeVec3(pts[0].x, pts[0].y, 0.0),
                end=_FakeVec3(pts[1].x, pts[1].y, 0.0),
            )

        elif t == "POLYLINE":
            pts = list(e.points)
            xy = [(float(p.x), float(p.y), 0.0) for p in pts]
            return _FakeDxfAttr(xy=xy, closed=bool(e.closed))

        elif t == "ARC":
            center = e.center
            rx = float(e.x_axis.x)
            ry = float(e.x_axis.y)
            radius = math.sqrt(rx * rx + ry * ry)
            return _FakeDxfAttr(
                center=_FakeVec3(center.x, center.y, 0.0),
                radius=radius,
                start_angle=float(e.start_angle_degrees),
                end_angle=float(e.end_angle_degrees),
            )

        elif t == "CIRCLE":
            center = e.center
            rx = float(e.x_axis.x)
            ry = float(e.x_axis.y)
            radius = math.sqrt(rx * rx + ry * ry)
            return _FakeDxfAttr(
                center=_FakeVec3(center.x, center.y, 0.0),
                radius=radius,
            )

        else:
            return _FakeDxfAttr()


class _FakeVertex:
    """模拟 ezdxf VERTEX 对象"""
    def __init__(self, x, y):
        self._loc = _FakeVec3(x, y, 0.0)
    def dxfattribs(self):
        return {"location": self._loc}


class _DwfPolylineAdapter(_DwfEntityAdapter):
    """POLYLINE 适配器"""
    def __init__(self, dwf_entity):
        super().__init__(dwf_entity)
        closed = bool(dwf_entity.closed)
        self.dxf.flags = 1 if closed else 0
        pts = list(dwf_entity.points)
        self.vertices = [_FakeVertex(float(p.x), float(p.y)) for p in pts]

    def get_points(self, fmt: str = "xy"):
        pts = list(self._e.points)
        if fmt == "xy":
            return [(float(p.x), float(p.y)) for p in pts]
        elif fmt == "xyseb":
            return [(float(p.x), float(p.y), 0.0, 0.0, 0.0) for p in pts]
        return [(float(p.x), float(p.y)) for p in pts]

    @property
    def closed(self):
        return bool(self._e.closed)


def _dwf_adapt_entity(dwf_entity) -> _DwfEntityAdapter:
    """把一个 ezdwf 实体包装成 ezdxf 风格的适配器"""
    t = dwf_entity.dxftype()
    if t == "POLYLINE":
        return _DwfPolylineAdapter(dwf_entity)
    return _DwfEntityAdapter(dwf_entity)


def _load_any_file(input_path: str) -> Tuple[list, str]:
    """
    统一文件加载入口，根据扩展名自动选 ezdxf 或 ezdwf
    返回 (实体适配器列表, 格式名称)
    """
    ext = os.path.splitext(input_path)[1].lower()

    if ext == ".dxf":
        try:
            doc = ezdxf.readfile(input_path)
        except Exception as e:
            raise RuntimeError(f"无法读取 DXF 文件：{e}")
        msp = doc.modelspace()
        return list(msp), "DXF"

    elif ext in (".dwf", ".dwfx"):
        if not HAS_EZDWF:
            raise RuntimeError(
                "需要 ezdwf 库才能读取 DWF/DWFx 文件。\n"
                "请执行：pip install ezdwf"
            )
        try:
            drawing = ezdwf.readfile(input_path)
            sheet = drawing.modelspace()
        except Exception as e:
            raise RuntimeError(f"无法读取 DWF/DWFx 文件：{e}")

        entities = [_dwf_adapt_entity(e) for e in sheet.entities]
        return entities, ("DWFx" if ext == ".dwfx" else "DWF")

    else:
        raise RuntimeError(
            f"不支持的文件扩展名 '{ext}'。\n"
            "支持：.dxf / .dwf / .dwfx"
        )


# ======================================================================
# 文字实体（TEXT / MTEXT）→ 矢量轮廓
# 思路：TTF 字体里每个字本来就是"一堆闭合轮廓线"，只是用贝塞尔曲线描述。
#      所以先按指定字体取出字形轮廓，再把曲线细分成直线段，
#      就能当作普通多段线混进原有的链化和出刀流程里
# ======================================================================

class _TextContour:
    """
    一条文字轮廓折线（例如"口"字的外框，或者"口"字中间那个方孔）
    故意伪装成 LWPOLYLINE 实体，这样包围盒计算、端点链化、G 代码生成
    都能原样复用，不必为文字单独写一套流程
    """
    def __init__(self, points: List[Tuple[float, float]]):
        # 补齐成 ezdxf get_points() 的 5 元组格式：(x, y, 起始宽, 结束宽, bulge)
        self._points = [(float(p[0]), float(p[1]), 0.0, 0.0, 0.0) for p in points]
        self.closed = True          # 字形轮廓一定是闭合的

    def dxftype(self) -> str:
        return "LWPOLYLINE"

    def get_points(self):
        return self._points


def _split_bezier(points: List[Tuple[float, float]],
                  steps: int) -> List[Tuple[float, float]]:
    """
    用 de Casteljau 递推把一段贝塞尔曲线细分成若干直线
    points 是控制点（二次曲线 3 个点，三次曲线 4 个点）
    返回不含起点的中间点与终点
    """
    out: List[Tuple[float, float]] = []
    for k in range(1, steps + 1):
        t = k / float(steps)
        pts = [[float(p[0]), float(p[1])] for p in points]
        # 每迭代一轮，控制点就少一个，最后剩下唯一的曲线上的点
        while len(pts) > 1:
            pts = [[pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t,
                    pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t]
                   for i in range(len(pts) - 1)]
        out.append((pts[0][0], pts[0][1]))
    return out


def _path_to_polylines(path,
                       steps: int = _TEXT_CURVE_STEPS) -> List[List[Tuple[float, float]]]:
    """
    把 matplotlib 的 Path（直线 + 贝塞尔曲线 + 闭合标记）拆成一组折线
    每条折线对应一个独立的字形轮廓（外框或内孔）
    """
    verts = [(float(v[0]), float(v[1])) for v in path.vertices]
    codes = path.codes
    if codes is None:
        return [verts] if len(verts) >= 2 else []

    groups: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = []
    i = 0
    n = len(verts)

    while i < n:
        code = codes[i]
        if code == MplPath.MOVETO:
            # 一条新轮廓开始
            if len(cur) >= 2:
                groups.append(cur)
            cur = [verts[i]]
            i += 1
        elif code == MplPath.LINETO:
            cur.append(verts[i])
            i += 1
        elif code == MplPath.CURVE3:
            # 二次贝塞尔：当前点 + 1 个控制点 + 终点
            if cur and i < n - 1:
                cur.extend(_split_bezier([cur[-1], verts[i], verts[i + 1]], steps))
            i += 2
        elif code == MplPath.CURVE4:
            # 三次贝塞尔：当前点 + 2 个控制点 + 终点
            if cur and i < n - 2:
                cur.extend(_split_bezier([cur[-1], verts[i], verts[i + 1], verts[i + 2]], steps))
            i += 3
        elif code == MplPath.CLOSEPOLY:
            # 闭合标记，顶点是占位用的 (0,0)，直接接回起点即可
            if len(cur) >= 2:
                if cur[0] != cur[-1]:
                    cur.append(cur[0])
                groups.append(cur)
            cur = []
            i += 1
        else:
            i += 1

    if len(cur) >= 2:
        if cur[0] != cur[-1]:
            cur.append(cur[0])      # 有些字形末尾不写闭合标记，这里补上
        groups.append(cur)

    return groups


def _text_to_contours(entity, font_path: str) -> List[_TextContour]:
    """
    把一个文字实体（TEXT / MTEXT）转成一组矢量轮廓折线
    返回的坐标已经换算到图纸坐标系（含字高缩放、对齐、旋转、插入点平移）
    """
    dxftype = entity.dxftype()

    if dxftype == "MTEXT":
        # MTEXT 的换行在内部写作 \P，先还原成真正的换行
        content = entity.plain_text().replace("\\P", "\n")
        height = float(entity.dxf.char_height)
        # MTEXT 不用 halign/valign，而是一个 1~9 的 attachment_point 表示插入点落在哪：
        #   1左上 2中上 3右上 / 4左中 5正中 6右中 / 7左下 8中下 9右下
        # 默认值 1，也就是插入点在整块文字的左上角，文字往下、往右排
        attach = int(entity.dxf.get("attachment_point", 1) or 1)
        attach = min(max(attach, 1), 9)
        halign = (attach - 1) % 3                       # 0 左 / 1 中 / 2 右
        v_mode = ("top", "middle", "bottom")[(attach - 1) // 3]
    else:
        content = str(entity.dxf.text)
        height = float(entity.dxf.height)
        halign = int(entity.dxf.get("halign", 0) or 0)
        valign = int(entity.dxf.get("valign", 0) or 0)
        # TEXT 的 valign：0 基线 / 1 底 / 2 中 / 3 顶
        v_mode = ("base", "bottom", "middle", "top")[min(max(valign, 0), 3)]

    if height <= 0.0 or not content.strip():
        return []

    insert = entity.dxf.insert
    rotation = float(entity.dxf.get("rotation", 0.0) or 0.0)
    fp = FontProperties(fname=font_path)

    # ---- 标定字号：CAD 的"字高"到底指多高 ----
    # 同一个字体里，西文大写字母和汉字的高度占 em 的比例差得很远
    # （黑体实测：H 只有 em 的 67%，"国" 却占 88%）
    # 所以西文图纸要拿 H 当探针、中文图纸要拿"国"当探针，
    # 用错的话中文会刻成设定值的 1.3 倍大
    has_cjk = any("\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"
                  for ch in content)
    probe_char = "国" if has_cjk else "H"

    def _probe_height(ch: str) -> float:
        """量某个字在 em=100 时实际有多高（取不到返回 0）"""
        try:
            return float(TextPath((0.0, 0.0), ch, size=100.0,
                                  prop=fp).get_extents().height)
        except Exception:
            return 0.0

    cap_h = _probe_height(probe_char)
    if cap_h <= 1e-6 and probe_char != "H":
        # 字体里没有汉字（比如给中文配了纯西文字体），退回用 H 标定，
        # 至少保证有字能描出来，不会整段消失
        cap_h = _probe_height("H")
    if cap_h <= 1e-6:
        cap_h = 66.8                # 取不到就按黑体 H 的实测比例兜底
    em_size = 100.0 * height / cap_h

    # ---- 逐行取出字形轮廓（此时还没做对齐、旋转、平移）----
    line_gap = height * 1.5         # 多行文字的行距
    raw_lines: List[List[Tuple[float, float]]] = []
    for row, text in enumerate(content.split("\n")):
        if not text:
            continue
        try:
            tp = TextPath((0.0, -row * line_gap), text, size=em_size, prop=fp)
        except Exception:
            continue
        raw_lines.extend(_path_to_polylines(tp))

    if not raw_lines:
        return []

    # ---- 按对齐方式算偏移 ----
    xs = [p[0] for ln in raw_lines for p in ln]
    ys = [p[1] for ln in raw_lines for p in ln]

    dx = 0.0
    if halign == 1:                 # 水平居中：插入点是文字宽度中点
        dx = -(min(xs) + max(xs)) / 2.0
    elif halign == 2:               # 右对齐：插入点在文字右端
        dx = -max(xs)

    dy = 0.0
    if v_mode == "bottom":          # 底对齐：插入点是文字最低点
        dy = -min(ys)
    elif v_mode == "middle":        # 垂直居中：插入点是文字高度中点
        dy = -(min(ys) + max(ys)) / 2.0
    elif v_mode == "top":           # 顶对齐：插入点是文字最高点（MTEXT 默认就是这种）
        dy = -max(ys)
    # v_mode == "base" 时 dy = 0，插入点就是基线左端（TEXT 的默认对齐方式）

    # ---- 旋转 + 平移到插入点 ----
    rad = math.radians(rotation)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    ix, iy = float(insert.x), float(insert.y)

    contours: List[_TextContour] = []
    for ln in raw_lines:
        moved: List[Tuple[float, float]] = []
        for x, y in ln:
            px, py = x + dx, y + dy
            moved.append((ix + px * cos_a - py * sin_a,
                          iy + px * sin_a + py * cos_a))
        contours.append(_TextContour(moved))

    return contours


def _expand_text_entities(entities: list, font_path: str):
    """
    把实体列表里的 TEXT / MTEXT 就地替换成它们的轮廓折线，其它实体原样保留
    返回：(展开后的实体列表, 文字对象个数)
    """
    out: list = []
    text_count = 0

    for e in entities:
        t = e.dxftype()
        if t in ("TEXT", "MTEXT"):
            text_count += 1
            if not HAS_MPL:
                continue            # 没有 matplotlib 就先跳过，外面会给出提示
            out.extend(_text_to_contours(e, font_path))
        else:
            out.append(e)

    return out, text_count


def _compute_global_bbox(msp) -> Tuple[float, float, float, float]:
    """遍历 modelspace 中所有受支持实体，求整体包围盒（用于识别外框）"""
    xs: List[float] = []
    ys: List[float] = []
    supported = ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE")
    for entity in msp:
        if entity.dxftype() not in supported:
            continue
        b = _entity_bbox(entity)
        if b[0] == 0.0 and b[1] == 0.0 and b[2] == 0.0 and b[3] == 0.0:
            continue
        xs.extend([b[0], b[1]])
        ys.extend([b[2], b[3]])
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), max(xs), min(ys), max(ys))


def _is_frame(entity, global_bbox: Tuple[float, float, float, float]) -> bool:
    """判断一个实体是否属于 XY 最大范围外框"""
    # 文字轮廓永远不当作边框处理：
    # 如果整张图章只刻一个字（比如就刻一个"福"字），那个字的外轮廓包围盒
    # 恰好等于整体包围盒，会被下面的判断误判成边框整条丢掉，字就没了
    if isinstance(entity, _TextContour):
        return False

    bbox = _entity_bbox(entity)
    gx_min, gx_max, gy_min, gy_max = global_bbox
    ex_min, ex_max, ey_min, ey_max = bbox
    dxftype = entity.dxftype()

    if (abs(ex_min - gx_min) <= _FRAME_TOL and abs(ex_max - gx_max) <= _FRAME_TOL
            and abs(ey_min - gy_min) <= _FRAME_TOL and abs(ey_max - gy_max) <= _FRAME_TOL):
        return True

    if dxftype == "LINE":
        full_width = (abs(ex_min - gx_min) <= _FRAME_TOL and abs(ex_max - gx_max) <= _FRAME_TOL)
        full_height = (abs(ey_min - gy_min) <= _FRAME_TOL and abs(ey_max - gy_max) <= _FRAME_TOL)
        if full_width or full_height:
            return True

    return False


def _fmt(value: float) -> str:
    """把坐标或偏移量格式化为保留 3 位小数的字符串"""
    return f"{value:.3f}"


# 正则：匹配 G 代码里的坐标字段（X/Y/I/J 后面跟数字）
_COORD_RE = re.compile(r"\b([XYIJ])(-?\d+\.\d+)\b")


def _apply_offset(lines: List[str], dx: float, dy: float) -> List[str]:
    """
    对一段 G 代码的每一行，把所有 X/Y 坐标加上偏移量
    I/J 是圆心相对起点的偏移量，不需要平移
    """
    new_lines: List[str] = []
    for line in lines:
        def _sub(m):
            letter = m.group(1)
            val = float(m.group(2))
            if letter == "X":
                return f"X{_fmt(val + dx)}"
            elif letter == "Y":
                return f"Y{_fmt(val + dy)}"
            else:
                return m.group(0)
        new_lines.append(_COORD_RE.sub(_sub, line))
    return new_lines


def _point_on_arc(center: Tuple[float, float], radius: float, angle_deg: float) -> Tuple[float, float]:
    """根据圆心、半径和角度（度）计算圆弧上的坐标"""
    rad = math.radians(angle_deg)
    return (center[0] + radius * math.cos(rad),
            center[1] + radius * math.sin(rad))


def _bulge_to_arc(p1: Tuple[float, float], p2: Tuple[float, float], bulge: float):
    """
    根据多段线两点 + bulge 值返回几何参数
    bulge = tan(θ/4)，正值逆时针(G03)，负值顺时针(G02)
    返回: (cx, cy, radius, is_ccw)，若 bulge≈0 返回 None
    """
    if abs(bulge) < 1e-12:
        return None

    try:
        arc_obj = ezdxf.math.bulge_to_arc(p1, p2, bulge)
        center = arc_obj.center
        radius = arc_obj.radius
        is_ccw = bulge > 0
        return (float(center[0]), float(center[1]), float(radius), bool(is_ccw))
    except Exception:
        pass

    x1, y1 = p1
    x2, y2 = p2
    dx = x2 - x1
    dy = y2 - y1
    chord_len = math.hypot(dx, dy)
    if chord_len < 1e-12:
        return None

    # 圆心角（带上正负号，正 = 逆时针）
    theta = 4.0 * math.atan(bulge)
    half = theta / 2.0
    sin_half = math.sin(half)
    if abs(sin_half) < 1e-12:
        return None

    # 半径永远取正值；圆心到弦中点的有向距离 = r·cos(θ/2)
    # 逆时针(bulge>0)时圆心在前进方向的左侧，顺时针在右侧，所以再乘 bulge 的符号
    radius = chord_len / (2.0 * abs(sin_half))
    dir_sign = 1.0 if bulge > 0.0 else -1.0
    dist = radius * math.cos(half) * dir_sign

    # 弦的左法向（前进方向逆时针转 90°）
    perp_x = -dy / chord_len
    perp_y = dx / chord_len
    mid_x = (x1 + x2) / 2.0
    mid_y = (y1 + y2) / 2.0
    center_x = mid_x + perp_x * dist
    center_y = mid_y + perp_y * dist

    return (float(center_x), float(center_y), float(radius), bulge > 0)


# ======================================================================
# G 代码片段生成（只生成切削运动，不含抬刀/下刀）
# ======================================================================

def _emit_line_segment(x_end: float, y_end: float) -> str:
    """
    输出一条直线插补 G01
    不写 F：F 是模态指令，下刀那一行写过一次就一直有效，
    这里再写只是把同一句话重复几百遍，白白拉长文件、拖慢传程序
    """
    return f"G01 X{_fmt(x_end)} Y{_fmt(y_end)}"


def _emit_arc(x_start: float, y_start: float,
              x_end: float, y_end: float,
              center_x: float, center_y: float,
              is_ccw: bool) -> str:
    """输出一条圆弧插补 G02 或 G03（自动算 I/J，同样不重复写 F）"""
    i_val = center_x - x_start
    j_val = center_y - y_start
    g_code = "G03" if is_ccw else "G02"
    return f"{g_code} X{_fmt(x_end)} Y{_fmt(y_end)} I{_fmt(i_val)} J{_fmt(j_val)}"


# ======================================================================
# 轨迹元数据 + 端点链
# ======================================================================

@dataclass
class _EntityMeta:
    """
    一个实体的轨迹元数据，用于端点链接
    """
    entity: Any                         # 原始实体引用
    dxftype: str                        # 实体类型
    start: Tuple[float, float]          # 起点坐标
    end: Tuple[float, float]             # 终点坐标
    closed: bool                        # 是否闭合（起点=终点）
    needs_reverse: bool = False          # 链接时是否需要反向切削


def _extract_meta(entity) -> Optional[_EntityMeta]:
    """
    从任意实体提取 (起点, 终点, 是否闭合) 元数据
    无法识别的实体返回 None
    """
    dxftype = entity.dxftype()

    try:
        if dxftype == "LINE":
            s = (float(entity.dxf.start.x), float(entity.dxf.start.y))
            e = (float(entity.dxf.end.x), float(entity.dxf.end.y))
            return _EntityMeta(entity, dxftype, s, e, False)

        elif dxftype == "ARC":
            center = (float(entity.dxf.center.x), float(entity.dxf.center.y))
            radius = float(entity.dxf.radius)
            start_angle = float(entity.dxf.start_angle)
            end_angle = float(entity.dxf.end_angle)
            s = _point_on_arc(center, radius, start_angle)
            e = _point_on_arc(center, radius, end_angle)
            return _EntityMeta(entity, dxftype, s, e, False)

        elif dxftype == "CIRCLE":
            # 整圆：起点终点重合（取角度 0° 位置）
            center = (float(entity.dxf.center.x), float(entity.dxf.center.y))
            radius = float(entity.dxf.radius)
            s = _point_on_arc(center, radius, 0.0)
            return _EntityMeta(entity, dxftype, s, s, True)

        elif dxftype == "LWPOLYLINE":
            pts = list(entity.get_points())
            is_closed = bool(entity.closed)
            if len(pts) < 2:
                return None
            s = (float(pts[0][0]), float(pts[0][1]))
            if is_closed:
                e = s
            else:
                e = (float(pts[-1][0]), float(pts[-1][1]))
            return _EntityMeta(entity, dxftype, s, e, is_closed)

        elif dxftype == "POLYLINE":
            if hasattr(entity, "vertices"):
                is_closed = bool(entity.dxf.flags & 1)
                verts = list(entity.vertices)
                pts: List[Tuple[float, float]] = []
                for v in verts:
                    loc = v.dxfattribs().get("location")
                    if loc is not None:
                        pts.append((float(loc.x), float(loc.y)))
            elif hasattr(entity, "get_points"):
                is_closed = bool(getattr(entity, "closed", False))
                pts = [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
            else:
                return None

            if len(pts) < 2:
                return None
            s = pts[0]
            e = s if is_closed else pts[-1]
            return _EntityMeta(entity, dxftype, s, e, is_closed)

        elif dxftype == "SPLINE":
            from ezdxf.math import BSpline
            cp = list(entity.control_points)
            knots = list(entity.knots)
            degree = int(entity.dxf.degree)
            if len(cp) < 2:
                return None
            expected_len = len(cp) + degree + 1
            if len(knots) > expected_len:
                knots = knots[:expected_len]
            bs = BSpline(cp, order=degree + 1, knots=knots)
            approx = list(bs.approximate(segments=30))
            pts = [(float(p[0]), float(p[1])) for p in approx]
            is_closed = bool(entity.dxf.flags & 1)
            if len(pts) < 2:
                return None
            s = pts[0]
            e = s if is_closed else pts[-1]
            return _EntityMeta(entity, dxftype, s, e, is_closed)

    except Exception as e:
        print(f"【警告】提取 {dxftype} 端点失败：{e}")
        return None

    return None


def _meta_entry(meta: _EntityMeta) -> Tuple[float, float]:
    """
    取一个实体在链中的"入点"（刀具从哪一点切进去）
    反向切削时，入点就是它原本的终点
    """
    return meta.end if meta.needs_reverse else meta.start


def _meta_exit(meta: _EntityMeta) -> Tuple[float, float]:
    """
    取一个实体在链中的"出点"（刀具从哪一点切出来）
    反向切削时，出点就是它原本的起点
    """
    return meta.start if meta.needs_reverse else meta.end


def _cluster_index(cluster_pts: List[Tuple[float, float]],
                   p: Tuple[float, float],
                   snap_tol: float) -> int:
    """
    把点 p 归入已有的某个端点簇；找不到就新建一簇
    "簇"= 落在同一个位置上的所有端点，返回簇的下标
    """
    for idx, cp in enumerate(cluster_pts):
        if _points_equal(cp, p, snap_tol):
            return idx
    cluster_pts.append(p)
    return len(cluster_pts) - 1


def _chain_entities(meta_list: List[_EntityMeta],
                    snap_tol: float = 0.0) -> Tuple[List[List[_EntityMeta]], int]:
    """
    把实体按"端点首尾相接"的规则串成若干条连续切削链
    连接规则：上一段的出点 == 当前段的入点 → 直接连切，中间不抬刀
             上一段的出点 == 当前段的出点 → 当前段反向切（needs_reverse=True）

    这版算法的做法（参考欧拉路径/中国邮递员问题的经典思路）：
      1. 先把所有端点按容差归并成"端点簇"，线段是连接两个簇的"边"，
         整个图形就变成一张图；一次下刀连续切削 = 图里的一条路径
      2. 从"度数为奇数"的簇起链。因为度数奇数的位置必然是某条路径的头或尾，
         从这些位置起链，能让进刀/出刀配对得最整齐，路径条数最少
         （举例：一条能连通的折线，两头的端点度数是 1、中间点度数是 2，
           从度数为 1 的点起链，整条折线就只需要一次下刀）
      3. 走链时优先避开"死胡同"，防止一步走进尽头把连通的线拆散
      4. snap_tol > 0 时，相距该容差以内的端点也算接上（用于 CAD 里没精确对齐的线段）

    返回：(链列表, 靠吸附容差接上的处数)
    """
    n = len(meta_list)
    if n == 0:
        return [], 0

    # ---- 1. 把所有端点按容差归并成"端点簇"（同一个位置上的端点算同一簇） ----
    cluster_pts: List[Tuple[float, float]] = []     # 每个簇的代表坐标
    ends: List[Tuple[int, int]] = []                # 每个实体的 (起点簇, 终点簇)
    for em in meta_list:
        si = _cluster_index(cluster_pts, em.start, snap_tol)
        ei = _cluster_index(cluster_pts, em.end, snap_tol)
        ends.append((si, ei))

    # ---- 2. 每个簇上挂着哪些实体的哪一端 ----
    #    side=0 表示实体的起点端挂在这个簇上，side=1 表示终点端
    attach: List[List[Tuple[int, int]]] = [[] for _ in cluster_pts]
    for i, (si, ei) in enumerate(ends):
        attach[si].append((i, 0))
        attach[ei].append((i, 1))

    remain = [len(a) for a in attach]   # 每个簇上还剩几个没被用掉的端头
    used = [False] * n                  # 标记实体是否已经进链
    chains: List[List[_EntityMeta]] = []
    snap_count = 0                      # 统计有多少处连接是靠容差"凑"上的

    def _pick_edge(cur: int, cands: List[Tuple[int, int]]):
        """
        从当前簇的候选边里挑一条走
        优先挑"走过去之后还有别的出路"的那条，避免一步跨进死胡同，
        把本该连在一起的线段白白浪费掉（这是欧拉路径算法里的经典思路）
        """
        if len(cands) == 1:
            return cands[0]
        for i, side in cands:
            nxt = ends[i][1] if side == 0 else ends[i][0]
            if nxt != cur and remain[nxt] - 1 > 0:
                return i, side
        return cands[0]

    def _walk(start_cluster: int) -> List[_EntityMeta]:
        """
        从某个端点簇出发一路走下去，走出一条连续切削链
        走不动了就停，剩下的线段留给后面的链
        """
        nonlocal snap_count
        chain: List[_EntityMeta] = []
        cur = start_cluster

        while True:
            cands = [(i, s) for (i, s) in attach[cur] if not used[i]]
            if not cands:
                break

            idx, side = _pick_edge(cur, cands)
            em = meta_list[idx]
            used[idx] = True
            remain[ends[idx][0]] -= 1
            remain[ends[idx][1]] -= 1
            # side=1 表示刀具是从这个实体的终点端进去的 → 需要反向切削
            em.needs_reverse = (side == 1)

            # 这一处连接不是严格重合，而是靠吸附容差凑上的，计数以便提示用户
            if not _points_equal(cluster_pts[cur], _meta_entry(em)):
                snap_count += 1

            chain.append(em)
            # 刀具顺着这条边走到了它的另一端
            cur = ends[idx][1] if side == 0 else ends[idx][0]

        return chain

    # ---- 3. 起链顺序：奇度簇优先 ----
    #    欧拉路径理论：端点度数为奇数的位置，必然是某条链的两头，
    #    从这些位置起链能让"一头进一头出"配对得最整齐，链数最少。
    #    奇偶性相同时度数小的先处理，避免高密度的交叉点把边抢光
    cluster_order = sorted(range(len(cluster_pts)),
                           key=lambda c: (remain[c] % 2 == 0, remain[c]))

    for c in cluster_order:
        # 一个簇上可能挂着好几条互不相连的线，所以要循环起链直到用完
        while any(not used[i] for i, _s in attach[c]):
            chains.append(_walk(c))

    return chains, snap_count


# ======================================================================
# 单个实体 → 切削 G 代码（不含抬刀/下刀）
# ======================================================================

def _entity_to_cut_lines(meta: _EntityMeta) -> List[str]:
    """
    根据实体元数据生成纯切削 G 代码（无 header/footer）
    如果 meta.needs_reverse=True，则按反向切削（适用于端点反接）
    注意：这里不写 F，F 只在每条链的下刀那一行写一次（模态指令）
    """
    entity = meta.entity
    dxftype = meta.dxftype
    reversed_ = meta.needs_reverse
    lines: List[str] = []

    try:
        if dxftype == "LINE":
            # 正向：从 start 到 end；反向：从 end 到 start
            if not reversed_:
                lines.append(_emit_line_segment(meta.end[0], meta.end[1]))
            else:
                lines.append(_emit_line_segment(meta.start[0], meta.start[1]))

        elif dxftype == "ARC":
            center = (float(entity.dxf.center.x), float(entity.dxf.center.y))
            radius = float(entity.dxf.radius)
            start_angle = float(entity.dxf.start_angle)
            end_angle = float(entity.dxf.end_angle)
            if not reversed_:
                # 正向：start_angle → end_angle（逆时针 G03）
                p_start = _point_on_arc(center, radius, start_angle)
                p_end = _point_on_arc(center, radius, end_angle)
                lines.append(_emit_arc(p_start[0], p_start[1],
                                       p_end[0], p_end[1],
                                       center[0], center[1],
                                       is_ccw=True))
            else:
                # 反向：end_angle → start_angle（顺时针 G02）
                p_start = _point_on_arc(center, radius, end_angle)
                p_end = _point_on_arc(center, radius, start_angle)
                lines.append(_emit_arc(p_start[0], p_start[1],
                                       p_end[0], p_end[1],
                                       center[0], center[1],
                                       is_ccw=False))

        elif dxftype == "CIRCLE":
            center = (float(entity.dxf.center.x), float(entity.dxf.center.y))
            radius = float(entity.dxf.radius)
            if radius < 1e-9:
                return []
            # 整圆拆两段 G03
            p0 = _point_on_arc(center, radius, 0.0)
            p180 = _point_on_arc(center, radius, 180.0)
            p360 = _point_on_arc(center, radius, 360.0)
            if not reversed_:
                lines.append(_emit_arc(p0[0], p0[1],
                                       p180[0], p180[1],
                                       center[0], center[1], is_ccw=True))
                lines.append(_emit_arc(p180[0], p180[1],
                                       p360[0], p360[1],
                                       center[0], center[1], is_ccw=True))
            else:
                # 反向整圆：两段 G02
                lines.append(_emit_arc(p0[0], p0[1],
                                       p180[0], p180[1],
                                       center[0], center[1], is_ccw=False))
                lines.append(_emit_arc(p180[0], p180[1],
                                       p360[0], p360[1],
                                       center[0], center[1], is_ccw=False))

        elif dxftype == "LWPOLYLINE":
            pts = list(entity.get_points())
            is_closed = bool(entity.closed)
            n_pts = len(pts)
            seg_count = n_pts - 1 + (1 if is_closed else 0)

            for i in range(seg_count):
                if not reversed_:
                    # 正向：按 DXF 顶点原始顺序一段段切，bulge 记在起始顶点上
                    p_curr = pts[i]
                    p_next = pts[(i + 1) % n_pts]
                    bulge = p_curr[4]
                elif is_closed:
                    # 闭合环反向：还是从 pts[0] 出发，但倒着绕一圈，
                    # 必须保证链定位点（pts[0]）就是第一段的起点，否则会切错
                    p_curr = pts[(-i) % n_pts]
                    p_next = pts[(-i - 1) % n_pts]
                    bulge = -p_next[4]
                else:
                    # 开放折线反向：从最后一个顶点倒着走回第一个顶点
                    p_curr = pts[n_pts - 1 - i]
                    p_next = pts[n_pts - 2 - i]
                    bulge = -p_next[4]

                x1, y1 = p_curr[0], p_curr[1]
                x2, y2 = p_next[0], p_next[1]
                # bulge 取负号后，圆弧的旋转方向已经由 bulge 自身的正负表达出来了
                arc_params = _bulge_to_arc((x1, y1), (x2, y2), bulge)
                if arc_params is None:
                    lines.append(_emit_line_segment(x2, y2))
                else:
                    cx, cy, r, is_ccw = arc_params
                    lines.append(_emit_arc(x1, y1, x2, y2, cx, cy, is_ccw))

        elif dxftype == "POLYLINE":
            if hasattr(entity, "vertices"):
                is_closed = bool(entity.dxf.flags & 1)
                verts = list(entity.vertices)
                pts_list: List[Tuple[float, float]] = []
                for v in verts:
                    loc = v.dxfattribs().get("location")
                    if loc is not None:
                        pts_list.append((float(loc.x), float(loc.y)))
            elif hasattr(entity, "get_points"):
                is_closed = bool(getattr(entity, "closed", False))
                pts_list = [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
            else:
                return []

            n_pts = len(pts_list)
            seg_count = n_pts - 1 + (1 if is_closed else 0)

            for i in range(seg_count):
                if not reversed_:
                    p_next = pts_list[(i + 1) % n_pts]
                elif is_closed:
                    # 闭合环反向：从 pts[0] 出发倒着绕一圈
                    p_next = pts_list[(-i - 1) % n_pts]
                else:
                    # 开放折线反向：从最后一个顶点倒着走回第一个顶点
                    p_next = pts_list[n_pts - 2 - i]
                lines.append(_emit_line_segment(p_next[0], p_next[1]))

        elif dxftype == "SPLINE":
            from ezdxf.math import BSpline
            cp = list(entity.control_points)
            knots = list(entity.knots)
            degree = int(entity.dxf.degree)
            expected_len = len(cp) + degree + 1
            if len(knots) > expected_len:
                knots = knots[:expected_len]
            bs = BSpline(cp, order=degree + 1, knots=knots)
            approx = list(bs.approximate(segments=30))
            pts_list = [(float(p[0]), float(p[1])) for p in approx]
            is_closed = bool(entity.dxf.flags & 1)

            n_pts = len(pts_list)
            seg_count = n_pts - 1 + (1 if is_closed else 0)

            for i in range(seg_count):
                if not reversed_:
                    p_next = pts_list[(i + 1) % n_pts]
                elif is_closed:
                    # 闭合样条反向：从 pts[0] 出发倒着绕一圈
                    p_next = pts_list[(-i - 1) % n_pts]
                else:
                    # 开放样条反向：从最后一个点倒着走回第一个点
                    p_next = pts_list[n_pts - 2 - i]
                lines.append(_emit_line_segment(p_next[0], p_next[1]))

    except Exception as e:
        print(f"【警告】生成 {dxftype} 切削代码失败：{e}")
        return []

    return lines


# ======================================================================
# 链间顺序优化（减少抬刀空行程）
# 思路来自开源实现：最近邻（Nearest Neighbor）构造初始解，
# 再用 2-opt 局部搜索改进，是 TSP（旅行商问题）里最经典也最实用的两个算法
# ======================================================================

@dataclass
class _Chain:
    """
    一条连续切削链：内部实体首尾相接，一次下刀从头切到尾
    对外只暴露两个端点：entry（进刀点）和 exit（出刀点）
    整条链可以翻转（flip）：切削方向全部反向，两个端点互换
    """
    metas: List[_EntityMeta]

    def entry(self) -> Tuple[float, float]:
        """进刀点：这条链从哪里开始切"""
        return _meta_entry(self.metas[0])

    def exit(self) -> Tuple[float, float]:
        """出刀点：这条链切完停在哪里"""
        return _meta_exit(self.metas[-1])

    def flip(self) -> None:
        """
        整链反向
        实体顺序倒过来，同时每个实体的切削方向也翻转
        数学上等价于"从原来的出刀点进刀，一路切回原来的进刀点"
        """
        for m in self.metas:
            m.needs_reverse = not m.needs_reverse
        self.metas.reverse()


def _path_travel(chains: List[_Chain], start_pos: Tuple[float, float]) -> float:
    """
    计算按给定顺序依次加工这些链时，抬刀状态下的总空行程长度（mm）
    start_pos 是刀具开始时的位置（程序头 G00X0Y0，即原点）
    """
    total = 0.0
    cur = start_pos
    for c in chains:
        total += _dist(cur, c.entry())
        cur = c.exit()
    return total


def _nearest_neighbor_order(chains: List[_Chain],
                            start_pos: Tuple[float, float]) -> List[_Chain]:
    """
    最近邻启发式：从刀具当前位置出发，每一步都挑"离得最近"的那条链去切，
    并且比较链的两个端点，选更近的一端进刀（必要时把整条链翻转）

    优点：一次遍历就能得到相当不错的顺序，速度极快
    缺点：容易在最后剩几条远链时绕远路，所以后面还要接 2-opt 修正
    """
    remaining = list(chains)
    ordered: List[_Chain] = []
    cur = start_pos

    while remaining:
        best_i = 0
        best_rev = False
        best_d = float("inf")

        # 在剩下的链里找"离当前刀具位置最近的一端"
        for i, c in enumerate(remaining):
            d_entry = _dist(cur, c.entry())
            if d_entry < best_d:
                best_d, best_i, best_rev = d_entry, i, False
            d_exit = _dist(cur, c.exit())
            if d_exit < best_d:
                best_d, best_i, best_rev = d_exit, i, True

        chain = remaining.pop(best_i)
        if best_rev:
            chain.flip()          # 从更近的那一端进刀，整链反向
        ordered.append(chain)
        cur = chain.exit()

    return ordered


def _two_opt_order(ordered: List[_Chain],
                   start_pos: Tuple[float, float],
                   max_rounds: int = 50) -> List[_Chain]:
    """
    2-opt 局部搜索：反复尝试"把其中一段链整体倒过来加工"，
    如果倒过来之后空行程更短就接受，直到再也改不动为止

    原理：把路径看成很多段连线，2-opt 每次剪断两条线、交叉重连，
          重连后的增量可以用 新距离 - 旧距离 直接算出来（不用重算整条路径）
          参考 afourmy/pyTSP 里的写法：
              delta = new_cost - old_cost
              if delta < 0: 接受
    注意：倒转一段链的同时，这段里每条链的切削方向也要跟着翻转，
          否则端点接不上
    """
    n = len(ordered)
    if n < 3:
        return ordered          # 少于 3 条链没有可交换的余地

    # 链特别多的时候限制迭代轮数，避免耗时过长
    rounds = max_rounds if n <= 200 else 5

    for _ in range(rounds):
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                # 被剪断的两条线：prev →(i 的进刀点)、（j 的出刀点）→ nxt
                prev_point = start_pos if i == 0 else ordered[i - 1].exit()
                nxt_point = ordered[j + 1].entry() if j + 1 < n else None

                # 剪断前的空行程代价
                old_cost = _dist(prev_point, ordered[i].entry())
                if nxt_point is not None:
                    old_cost += _dist(ordered[j].exit(), nxt_point)

                # 把 i..j 整段倒过来之后：出刀点变进刀点，进刀点变出刀点
                new_cost = _dist(prev_point, ordered[j].exit())
                if nxt_point is not None:
                    new_cost += _dist(ordered[i].entry(), nxt_point)

                if new_cost < old_cost - 1e-9:
                    # 确实变短了 → 接受这次倒转
                    segment = ordered[i:j + 1]
                    for c in segment:
                        c.flip()
                    ordered[i:j + 1] = segment[::-1]
                    improved = True

        if not improved:
            break               # 一轮下来没有任何改进，说明已经收敛

    return ordered


# ======================================================================
# 主入口：dxf_to_gcode
# ======================================================================

def dxf_to_gcode(input_path: str, output_path: str,
                 safe_z: float, cut_depth: float, feed: float,
                 auto_offset: bool = True, origin: str = "br",
                 snap_tol: float = 0.0, optimize: bool = True,
                 font_path: str = _DEFAULT_FONT,
                 prog_num: int = 1099) -> None:
    """
    读 CAD 文件 → 转 G 代码 → 写入 .txt

    核心流程：
      1. 先提取所有实体的端点元数据
      2. 端点链化：上一段出点 == 当前段入点 → 直接连切不抬刀
      3. 链间顺序优化：最近邻 + 2-opt，把空行程压到最短
      4. 每条链只一次"抬刀→定位→下刀→切削→抬刀"，链内实体连续切削

    参数 snap_tol：端点吸附容差（mm）。0 表示严格要求端点重合；
                  填 0.05 表示相距 0.05mm 以内的端点也算接上，可以少抬刀
    参数 optimize：是否开启链间顺序优化（默认开启）
    参数 font_path：图纸里带文字时，用哪个 TTF 字体把文字描成轮廓
    """
    # ---------- 1. 加载文件 ----------
    try:
        entities, fmt_name = _load_any_file(input_path)
    except RuntimeError as e:
        print(f"【错误】{e}")
        sys.exit(1)

    print(f"【文件格式】{fmt_name}")
    all_lines: List[str] = []

    # ---------- 1.5 文字实体（TEXT / MTEXT）→ 矢量轮廓 ----------
    # 图纸里如果写了字，先把每个字按字体描成闭合轮廓，再混进普通线条里，
    # 这样文字就会跟线条一起参与包围盒计算、链化、出刀，直接刻出来。
    # 注意：必须在算整体包围盒之前做，否则文字会被算漏、原点位置就偏了。
    if HAS_MPL and not os.path.isfile(font_path):
        # 字体文件不存在就没法描字，直接退出，避免用户拿到一份缺字的 G 代码
        print(f"【错误】找不到字体文件：{font_path}")
        print("        请用 --font 指定一个存在的 TTF 字体，"
              r"例如 C:\Windows\Fonts\simkai.ttf（楷体）")
        sys.exit(1)

    entities, text_count = _expand_text_entities(entities, font_path)
    if text_count > 0:
        if not HAS_MPL:
            print(f"【警告】图纸里有 {text_count} 个文字对象，但当前环境缺少 matplotlib，"
                  "无法把文字描成轮廓，这些文字已被跳过。")
            print("        需要文字的话请先执行：pip install matplotlib")
        else:
            contour_count = sum(1 for e in entities if isinstance(e, _TextContour))
            print(f"【文字轮廓化】{text_count} 个文字对象（字体：{os.path.basename(font_path)}）"
                  f" → 描出 {contour_count} 条闭合轮廓线")

    # ---------- 2. 整体包围盒（识别外框 + 算平移量） ----------
    global_bbox = _compute_global_bbox(entities)
    gx_min, gx_max, gy_min, gy_max = global_bbox
    width = gx_max - gx_min
    height = gy_max - gy_min
    print(f"【DXF 原始范围】X: {gx_min:.3f} ~ {gx_max:.3f}  ({width:.3f} mm)")
    print(f"               Y: {gy_min:.3f} ~ {gy_max:.3f}  ({height:.3f} mm)")

    # ---------- 3. 计算平移量 ----------
    offset_x = 0.0
    offset_y = 0.0
    if auto_offset:
        if origin == "bl":
            offset_x = -gx_min; offset_y = -gy_min
        elif origin == "br":
            offset_x = -gx_max; offset_y = -gy_min
        elif origin == "tl":
            offset_x = -gx_min; offset_y = -gy_max
        elif origin == "tr":
            offset_x = -gx_max; offset_y = -gy_max
        else:
            offset_x = -gx_max; offset_y = -gy_min

        print(f"【加工原点位置】{origin}（CNC (0,0) = DXF ({-offset_x:.3f}, {-offset_y:.3f})）")
        print(f"【平移后 CNC 范围】X: {gx_min+offset_x:.3f} ~ {gx_max+offset_x:.3f}")
        print(f"                 Y: {gy_min+offset_y:.3f} ~ {gy_max+offset_y:.3f}")

    # ---------- 4. 提取端点元数据 ----------
    supported_types = ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "SPLINE")
    meta_list: List[_EntityMeta] = []
    skip_frame_count = 0

    for entity in entities:
        dxftype = entity.dxftype()
        if dxftype not in supported_types:
            continue
        # 跳过边框
        if _is_frame(entity, global_bbox):
            skip_frame_count += 1
            continue
        meta = _extract_meta(entity)
        if meta is not None:
            meta_list.append(meta)

    print(f"【实体端点提取】有效 {len(meta_list)} 个，跳过边框 {skip_frame_count} 个")

    # ---------- 5. 端点链化 ----------
    raw_chains, snap_count = _chain_entities(meta_list, snap_tol)
    print(f"【端点链接】{len(meta_list)} 个实体 → {len(raw_chains)} 条连续切削链")
    if snap_tol > 0.0:
        print(f"【端点吸附】容差 {snap_tol:.3f} mm，其中 {snap_count} 处靠吸附接上")
    for i, chain in enumerate(raw_chains):
        if len(chain) == 1:
            print(f"  链 {i+1}: {chain[0].dxftype} ×1（单独切削）")
        else:
            print(f"  链 {i+1}: {len(chain)} 个实体连续连切")

    # ---------- 5.5 链间顺序优化（减少空行程） ----------
    chains = [_Chain(m) for m in raw_chains]

    # 刀具起始位置：程序头里是 G00X0Y0，对应平移前的坐标 (-offset_x, -offset_y)
    start_pos = (-offset_x, -offset_y)

    if optimize and len(chains) > 1:
        travel_before = _path_travel(chains, start_pos)
        chains = _nearest_neighbor_order(chains, start_pos)
        chains = _two_opt_order(chains, start_pos)
        travel_after = _path_travel(chains, start_pos)

        if travel_before > 1e-9:
            saved_pct = (travel_before - travel_after) / travel_before * 100.0
        else:
            saved_pct = 0.0
        print(f"【路径优化】空行程 {travel_before:.1f} mm → {travel_after:.1f} mm"
              f"（减少 {saved_pct:.1f}%）")

    # ---------- 6. 程序头 ----------
    # 程序号 = O + 4 位数字（O0001 ~ O9999）
    # 单文件转换默认 O1099；批量转换由 convert_all.bat 传入 1、2、3… 得到 O0001、O0002…
    all_lines.append(f"O{prog_num:04d};")
    all_lines.append("M03S3000;")
    all_lines.append("G54G90;")
    all_lines.append("G00Z15;")
    all_lines.append("G00X0Y0;")
    all_lines.append("")

    # ---------- 7. 按链生成 G 代码 ----------
    # 状态标记：刀具现在是否已经停在安全高度上。
    # 程序头的 G00Z15 已经把刀抬起来了，所以一开始就是"在安全高"。
    # 有了这个标记，就不会出现"上一条链刚抬完刀、下一条链又抬一次"这种废话，
    # 也不会在程序头 G00Z15 后面再补一条一模一样的抬刀
    at_safe_z = True

    for chain in chains:
        metas = chain.metas
        # 链头：定位到链的进刀点 + 下刀（整条链只下刀一次）
        first_pos = chain.entry()

        # 只有刀当前不在安全高时才需要抬刀
        if not at_safe_z:
            all_lines.append(f"G00 Z{_fmt(safe_z)}")
            at_safe_z = True

        all_lines.append(f"G00 X{_fmt(first_pos[0])} Y{_fmt(first_pos[1])}")
        # 下刀这一行的 F 是整条链里唯一一次进给设定，
        # 后面所有切削行都不再重复写（G01/G02/G03 的 F 是模态指令）
        all_lines.append(f"G01 Z{_fmt(cut_depth)} F{int(feed)}")
        at_safe_z = False           # 已经下刀，刀不在安全高了

        # 链内每个实体生成切削代码（连续，不抬刀）
        for meta in metas:
            cut = _entity_to_cut_lines(meta)
            all_lines.extend(cut)

        # 链尾：抬刀。保证任何时候加工完刀都是离开工件的
        all_lines.append(f"G00 Z{_fmt(safe_z)}")
        at_safe_z = True
        all_lines.append("")

    # ---------- 8. 坐标平移 ----------
    if offset_x != 0.0 or offset_y != 0.0:
        all_lines = _apply_offset(all_lines, offset_x, offset_y)

    # ---------- 9. 程序尾 ----------
    # 每条链切完都已经抬过刀了，这里正常情况下不必再抬一次，
    # 只有万一把刀还留在工件里（比如图纸里一个可加工实体都没有）才补一刀
    if not at_safe_z:
        all_lines.append(f"G00 Z{_fmt(safe_z)}")
    all_lines.append("M30")

    # ---------- 10. 写入输出 ----------
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines))

    total_entities = sum(len(c.metas) for c in chains)
    print(f"【完成】共处理 {total_entities} 个图元 → {len(chains)} 条连续切削链"
          f"（抬刀/下刀 {len(chains)} 次）")
    print(f"【输出】G 代码已保存至：{output_path}")


# ======================================================================
# 命令行入口
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="DXF → CNC G代码（仅 G00/G01/G02/G03，自动合并连续切削）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-i", "--input",  required=True, help="输入 DXF/DWF/DWFx 文件路径")
    parser.add_argument("-o", "--output", required=True, help="输出 .txt G 代码文件路径")
    parser.add_argument("-s", "--safe-z",      type=float, default=15.0,  help="安全高度 Z（抬刀高度，默认 15.0）")
    parser.add_argument("-d", "--cut-depth",   type=float, default=-0.2,  help="下刀深度（负值，默认 -0.2）")
    parser.add_argument("-f", "--feed",         type=float, default=200.0, help="进给速度 F（默认 200）")
    parser.add_argument("-p", "--origin",       default="br",
                        choices=["bl", "br", "tl", "tr"],
                        help="加工原点位置：bl=左下 br=右下 tl=左上 tr=右上（默认 br 右下角）")
    parser.add_argument("--snap-tol", type=float, default=0.0,
                        help="端点吸附容差（mm），默认 0 表示要求端点严格重合；"
                             "填 0.05 表示相距 0.05mm 以内的端点也接成一条链，可减少抬刀次数")
    parser.add_argument("--no-optimize", action="store_true",
                        help="关闭链间路径优化（默认开启「最近邻 + 2-opt」排序，用于压缩空行程）")
    parser.add_argument("--font", default=_DEFAULT_FONT,
                        help="图纸里的文字用哪个 TTF 字体描成轮廓，默认黑体 "
                             r"(C:\Windows\Fonts\simhei.ttf)；换字体例如 "
                             r"--font C:\Windows\Fonts\simkai.ttf（楷体）")
    parser.add_argument("-n", "--prog-num", type=int, default=1099,
                        help="程序号 O 后面的数字，范围 1~9999（默认 1099，即 O1099）。"
                             "批量转换时由 convert_all.bat 自动传入 1、2、3…，"
                             "生成 O0001、O0002、O0003…")

    args = parser.parse_args()

    # 程序号必须是 4 位数字范围（O0001 ~ O9999），超出范围机床可能不识别
    if not (1 <= args.prog_num <= 9999):
        print("【错误】程序号只能是 1 ~ 9999 之间的整数")
        sys.exit(1)

    print(f"输入文件  : {args.input}")
    print(f"输出文件  : {args.output}")
    print(f"程序号    : O{args.prog_num:04d}")
    print(f"安全高度 Z: {args.safe_z}")
    print(f"下刀深度  : {args.cut_depth}")
    print(f"进给速度 F: {args.feed}")
    print(f"加工原点  : {args.origin}")
    print(f"吸附容差  : {args.snap_tol} mm")
    print(f"路径优化  : {'关闭' if args.no_optimize else '开启'}")
    print(f"文字字体  : {args.font}")
    print("-" * 40)

    dxf_to_gcode(
        input_path=args.input,
        output_path=args.output,
        safe_z=args.safe_z,
        cut_depth=args.cut_depth,
        feed=args.feed,
        origin=args.origin,
        snap_tol=args.snap_tol,
        optimize=not args.no_optimize,
        font_path=args.font,
        prog_num=args.prog_num,
    )


if __name__ == "__main__":
    main()
