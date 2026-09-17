# -*- coding: utf-8 -*-
"""
CAD 矢量图转 CNC G 代码工具
==========================
功能：把 AutoCAD 画好的 DXF / DWF / DWFx 名字线条
      （多段线/直线/圆弧/圆/样条曲线）转换成
      仅使用 G00、G01、G02、G03 四种指令的 G 代码。
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
from typing import List, Tuple, Dict, Any  # 类型标注，方便阅读

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


# ======================================================================
# 几何 / 数学辅助函数
# ======================================================================

# 边框识别容差（mm）——实体包围盒与图形整体包围盒完全重合则判定为边框
_FRAME_TOL = 0.01


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
            # POLYLINE 可能是 ezdxf 老式（有 vertices）或 ezdwf 风格（有 get_points）
            if hasattr(entity, "vertices"):
                # ezdxf 老式 POLYLINE
                for v in entity.vertices:
                    loc = v.dxfattribs().get("location")
                    if loc is not None:
                        pts.append((float(loc.x), float(loc.y)))
            elif hasattr(entity, "get_points"):
                # ezdwf 适配后的 POLYLINE
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
            # SPLINE 用控制点求包围盒（近似，但够用来算全局范围）
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
# ezdwf 实体属性命名和 ezdxf 不一样
# 这里写适配器类，把 ezdwf 实体包装成"长得像 ezdxf"的样子
# 这样已有的 handle_line / handle_arc 等函数一行都不用改
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
            # ezdwf POLYLINE 没有 bulge，全部 bulge=0
            pts = list(e.points)
            xy = [(float(p.x), float(p.y), 0.0) for p in pts]
            return _FakeDxfAttr(
                xy=xy,
                closed=bool(e.closed),
            )

        elif t == "ARC":
            center = e.center
            # x_axis 是半径向量，长度即半径
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
            # 其他类型返回空 dxf 属性，会被主循环跳过
            return _FakeDxfAttr()


class _FakeVertex:
    """模拟 ezdxf VERTEX 对象（有 dxfattribs() 方法返回 location）"""
    def __init__(self, x, y):
        self._loc = _FakeVec3(x, y, 0.0)
    def dxfattribs(self):
        return {"location": self._loc}


class _DwfPolylineAdapter(_DwfEntityAdapter):
    """
    POLYLINE 适配器：需要同时兼容 handle_lwpolyline 和 handle_polyline
    - handle_lwpolyline 调用 entity.get_points("xy")
    - handle_polyline 访问 entity.dxf.flags 和 entity.vertices
    """
    def __init__(self, dwf_entity):
        super().__init__(dwf_entity)
        # 补全 POLYLINE 需要的 ezdxf 属性
        closed = bool(dwf_entity.closed)
        self.dxf.flags = 1 if closed else 0
        # 模拟 vertices 列表
        pts = list(dwf_entity.points)
        self.vertices = [_FakeVertex(float(p.x), float(p.y)) for p in pts]

    def get_points(self, fmt: str = "xy"):
        pts = list(self._e.points)
        if fmt == "xy":
            return [(float(p.x), float(p.y)) for p in pts]
        elif fmt == "xyseb":
            # x, y, start_width, end_width, bulge
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
        # ---- DXF 文件，用 ezdxf ----
        try:
            doc = ezdxf.readfile(input_path)
        except Exception as e:
            raise RuntimeError(f"无法读取 DXF 文件：{e}")

        msp = doc.modelspace()
        # ezdxf 实体直接返回，不用适配器
        return list(msp), "DXF"

    elif ext in (".dwf", ".dwfx"):
        # ---- DWF / DWFx 文件，用 ezdwf ----
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

        # 把 ezdwf 实体全部包装成 ezdxf 风格适配器
        entities = [_dwf_adapt_entity(e) for e in sheet.entities]
        return entities, ("DWFx" if ext == ".dwfx" else "DWF")

    else:
        raise RuntimeError(
            f"不支持的文件扩展名 '{ext}'。\n"
            "支持：.dxf / .dwf / .dwfx"
        )


def _compute_global_bbox(msp) -> Tuple[float, float, float, float]:
    """
    遍历 modelspace 中所有受支持实体，求整体包围盒
    用于识别"外框"实体（包围盒恰好等于整体包围盒的就是外框）
    """
    xs: List[float] = []
    ys: List[float] = []
    supported = ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE")
    for entity in msp:
        if entity.dxftype() not in supported:
            continue
        b = _entity_bbox(entity)
        # 跳过计算失败的（全零）
        if b[0] == 0.0 and b[1] == 0.0 and b[2] == 0.0 and b[3] == 0.0:
            continue
        xs.extend([b[0], b[1]])
        ys.extend([b[2], b[3]])
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), max(xs), min(ys), max(ys))


def _is_frame(entity, global_bbox: Tuple[float, float, float, float]) -> bool:
    """
    判断一个实体是否属于"XY 最大范围外框"
    判定规则（命中任意一条就算外框）：
    1. 实体包围盒与全局包围盒**完全重合**（闭合外框多段线）
    2. 实体是 LINE，且 X 范围恰好等于全局 X 范围 **或者** Y 范围恰好等于全局 Y 范围
       （外框四条边的直线，每条边覆盖全宽或全高）
    """
    bbox = _entity_bbox(entity)
    gx_min, gx_max, gy_min, gy_max = global_bbox
    ex_min, ex_max, ey_min, ey_max = bbox
    dxftype = entity.dxftype()

    # 规则 1：完全重合
    if (abs(ex_min - gx_min) <= _FRAME_TOL and abs(ex_max - gx_max) <= _FRAME_TOL
            and abs(ey_min - gy_min) <= _FRAME_TOL and abs(ey_max - gy_max) <= _FRAME_TOL):
        return True

    # 规则 2：LINE 覆盖全宽 或 全高（外框边）
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
    对一段 G 代码的每一行，把所有 X/Y/I/J 坐标加上偏移量
    - X、Y 加偏移（绝对坐标平移）
    - I、J 是圆心相对起点的偏移量，**不需要**平移！因为 I/J = (cx-ix, cy-iy)，
      起点和圆心都平移了相同的量，差值不变
    """
    new_lines: List[str] = []
    for line in lines:
        # 只处理 G 命令行里的 X 和 Y
        # I/J 保持不变（因为它们是相对偏移）
        def _sub(m):
            letter = m.group(1)
            val = float(m.group(2))
            if letter == "X":
                return f"X{_fmt(val + dx)}"
            elif letter == "Y":
                return f"Y{_fmt(val + dy)}"
            else:
                return m.group(0)  # I/J 原样返回
        new_lines.append(_COORD_RE.sub(_sub, line))
    return new_lines


def _point_on_arc(center: Tuple[float, float], radius: float, angle_deg: float) -> Tuple[float, float]:
    """根据圆心、半径和角度（度）计算圆弧上的坐标"""
    rad = math.radians(angle_deg)
    return (center[0] + radius * math.cos(rad),
            center[1] + radius * math.sin(rad))


def _directional_cross(start: Tuple[float, float], end: Tuple[float, float],
                        center: Tuple[float, float]) -> float:
    """
    计算圆心相对起点到终点的"转向叉积"
    > 0：从 start→end 绕 center 是逆时针（CCW）
    < 0：顺时针（CW）
    """
    v1x = start[0] - center[0]
    v1y = start[1] - center[1]
    v2x = end[0] - center[0]
    v2y = end[1] - center[1]
    return v1x * v2y - v1y * v2x


def _bulge_to_arc(p1: Tuple[float, float], p2: Tuple[float, float],
                   bulge: float):
    """
    根据多段线两点 + bulge 值，返回该段的几何参数。
    bulge = tan(θ/4)，正值表示逆时针(G03)，负值表示顺时针(G02)。

    返回: (center_x, center_y, radius, is_ccw)
          若 bulge≈0（直线），返回 None
    实现：调用 ezdxf 内置的 bulge_to_arc 函数，避免自己手写几何公式出错。
    """
    if abs(bulge) < 1e-12:
        return None

    try:
        # ezdxf.math.bulge_to_arc 返回一个 Arc 对象
        arc_obj = ezdxf.math.bulge_to_arc(p1, p2, bulge)
        center = arc_obj.center  # (cx, cy)
        radius = arc_obj.radius
        # ezdxf 的 bulge_to_arc 返回的弧总是逆时针方向（因为 bulge 正=逆时针）
        # 但如果 bulge 为负，arc_obj 的方向是顺时针，需要看 arc_obj 的方向属性
        is_ccw = bulge > 0  # bulge 正=逆时针=G03，简洁可靠
        return (float(center[0]), float(center[1]), float(radius), bool(is_ccw))
    except Exception:
        # 如果 ezdxf 的 bulge_to_arc 调用失败，回退到叉积判断方向
        # 但 bulge 正负本身就直接决定了方向：正=逆时针(G03)，负=顺时针(G02)
        # 所以只需要用 bulge 正负
        pass

    # 回退方案：自己算
    x1, y1 = p1
    x2, y2 = p2
    dx = x2 - x1
    dy = y2 - y1
    chord_len = math.hypot(dx, dy)
    if chord_len < 1e-12:
        return None

    theta = 4.0 * math.atan(bulge)
    sin_half = math.sin(theta / 2.0)
    if abs(sin_half) < 1e-12:
        return None

    radius = chord_len / (2.0 * sin_half)
    mid_x = (x1 + x2) / 2.0
    mid_y = (y1 + y2) / 2.0
    # 弦的垂直单位向量（逆时针旋转 90°）
    perp_x = -dy / chord_len
    perp_y = dx / chord_len
    cos_half = math.cos(theta / 2.0)
    # sin_half > 0 时 bulge > 0，圆心沿 (+perp) 方向；反之沿 (-perp)
    sign = 1.0 if sin_half > 0 else -1.0
    center_x = mid_x + perp_x * (radius * cos_half) * sign
    center_y = mid_y + perp_y * (radius * cos_half) * sign

    return (float(center_x), float(center_y), float(abs(radius)), bulge > 0)


# ======================================================================
# 单个图元 → G 代码片段的生成器
# ======================================================================

def _segment_header(x0: float, y0: float, safe_z: float, cut_depth: float, feed: float) -> List[str]:
    """
    每个新轮廓开始前的标准"抬刀→定位→下刀"三行指令
    """
    lines = []
    # 先抬刀到安全高度
    lines.append(f"G00 Z{_fmt(safe_z)}")
    # 快速定位到轮廓起点上方
    lines.append(f"G00 X{_fmt(x0)} Y{_fmt(y0)}")
    # 下刀到切割深度
    lines.append(f"G01 Z{_fmt(cut_depth)} F{int(feed)}")
    return lines


def _emit_line_segment(x_end: float, y_end: float, feed: float) -> str:
    """输出一条直线插补 G01"""
    return f"G01 X{_fmt(x_end)} Y{_fmt(y_end)} F{int(feed)}"


def _emit_arc_full(x_start: float, y_start: float,
                   x_end: float, y_end: float,
                   center_x: float, center_y: float,
                   is_ccw: bool, feed: float) -> str:
    """
    完整圆弧输出：自动计算 I、J（圆心相对起点的偏移）
    """
    # G 代码中 I、J = 圆心 - 起点
    i_val = center_x - x_start
    j_val = center_y - y_start
    g_code = "G03" if is_ccw else "G02"
    return (f"{g_code} X{_fmt(x_end)} Y{_fmt(y_end)} "
            f"I{_fmt(i_val)} J{_fmt(j_val)} F{int(feed)}")


# ======================================================================
# 图元逐个处理
# ======================================================================

def handle_line(line, safe_z: float, cut_depth: float, feed: float) -> List[str]:
    """
    处理 DXF LINE 实体
    """
    try:
        start = (float(line.dxf.start.x), float(line.dxf.start.y))
        end = (float(line.dxf.end.x), float(line.dxf.end.y))
    except Exception as e:
        print(f"【警告】解析 LINE 失败：{e}")
        return []

    gcode = _segment_header(start[0], start[1], safe_z, cut_depth, feed)
    gcode.append(_emit_line_segment(end[0], end[1], feed))
    gcode.append(f"G00 Z{_fmt(safe_z)}")  # 轮廓结束抬刀
    return gcode


def handle_arc(arc, safe_z: float, cut_depth: float, feed: float) -> List[str]:
    """
    处理 DXF ARC 实体
    DXF 标准规定：ARC 从 start_angle 沿逆时针方向画到 end_angle
    所以直接用 G03（逆时针）即可
    """
    try:
        center = (float(arc.dxf.center.x), float(arc.dxf.center.y))
        radius = float(arc.dxf.radius)
        start_angle = float(arc.dxf.start_angle)
        end_angle = float(arc.dxf.end_angle)
    except Exception as e:
        print(f"【警告】解析 ARC 失败：{e}")
        return []

    # 圆弧起点与终点坐标
    p_start = _point_on_arc(center, radius, start_angle)
    p_end = _point_on_arc(center, radius, end_angle)

    # DXF 标准：ARC 始终沿逆时针方向绘制
    # delta = (end - start) % 360，正值表示逆时针跨越的角度
    # 用 G03 不会错
    is_ccw = True

    gcode = _segment_header(p_start[0], p_start[1], safe_z, cut_depth, feed)
    gcode.append(_emit_arc_full(p_start[0], p_start[1],
                                p_end[0], p_end[1],
                                center[0], center[1],
                                is_ccw, feed))
    gcode.append(f"G00 Z{_fmt(safe_z)}")
    return gcode


def handle_circle(circle, safe_z: float, cut_depth: float, feed: float) -> List[str]:
    """
    处理 DXF CIRCLE 实体（整圆）
    G 代码不支持单段整圆，必须拆成两个半圆
    这里统一拆成两个 G03（逆时针画圆）
    """
    try:
        center = (float(circle.dxf.center.x), float(circle.dxf.center.y))
        radius = float(circle.dxf.radius)
    except Exception as e:
        print(f"【警告】解析 CIRCLE 失败：{e}")
        return []

    if radius < 1e-9:
        print("【警告】跳过半径为 0 的圆")
        return []

    # 选择起点角度（0° = 圆最右侧）
    start_angle = 0.0
    p_start = _point_on_arc(center, radius, start_angle)

    gcode = _segment_header(p_start[0], p_start[1], safe_z, cut_depth, feed)

    # 第一个半圆：0° → 180°，逆时针
    p_mid = _point_on_arc(center, radius, 180.0)
    gcode.append(_emit_arc_full(p_start[0], p_start[1],
                                p_mid[0], p_mid[1],
                                center[0], center[1],
                                is_ccw=True, feed=feed))

    # 第二个半圆：180° → 360°(即 0°)，逆时针回到起点
    p_end = _point_on_arc(center, radius, 360.0)
    gcode.append(_emit_arc_full(p_mid[0], p_mid[1],
                                p_end[0], p_end[1],
                                center[0], center[1],
                                is_ccw=True, feed=feed))

    gcode.append(f"G00 Z{_fmt(safe_z)}")
    return gcode


def handle_lwpolyline(polyline, safe_z: float, cut_depth: float, feed: float) -> List[str]:
    """
    处理 DXF LWPOLYLINE 实体（轻量多段线）
    多段线由若干顶点（带或不带 bulge）顺序连接而成
    bulge ≈ 0 时为直线段，否则为圆弧段

    注意：ezdxf 1.4.4 的 get_points() 返回 5 元组：
          (x, y, start_width, end_width, bulge)
    """
    try:
        # 获取所有顶点数据
        pts = list(polyline.get_points())
        is_closed = bool(polyline.closed)
    except Exception as e:
        print(f"【警告】解析 LWPOLYLINE 失败：{e}")
        return []

    if len(pts) < 2:
        print("【警告】跳过顶点数小于 2 的多段线")
        return []

    gcode: List[str] = []
    seg_count = len(pts) - 1
    if is_closed:
        seg_count += 1  # 闭合多段线要多画一段回到起点

    # 先添加抬刀 + 定位 + 下刀（定位到第一个顶点）
    gcode.extend(_segment_header(pts[0][0], pts[0][1], safe_z, cut_depth, feed))

    for i in range(seg_count):
        p_curr = pts[i]                    # 当前顶点 5 元组
        p_next = pts[(i + 1) % len(pts)]   # 下一个顶点 5 元组
        x1 = p_curr[0]                     # 起点 X
        y1 = p_curr[1]                     # 起点 Y
        bulge = p_curr[4]                  # bulge 在第 5 位（索引 4）
        x2 = p_next[0]                     # 终点 X
        y2 = p_next[1]                     # 终点 Y

        arc_params = _bulge_to_arc((x1, y1), (x2, y2), bulge)
        if arc_params is None:
            # 直线段（bulge ≈ 0 或两点重合）
            gcode.append(_emit_line_segment(x2, y2, feed))
        else:
            cx, cy, r, is_ccw = arc_params
            gcode.append(_emit_arc_full(x1, y1, x2, y2, cx, cy, is_ccw, feed))

    gcode.append(f"G00 Z{_fmt(safe_z)}")
    return gcode


def handle_polyline(polyline, safe_z: float, cut_depth: float, feed: float) -> List[str]:
    """
    处理老式 DXF POLYLINE 实体（多段线，非 LWPOLYLINE 轻量版）
    顶点通过 polyline.vertices 遍历，坐标在 VERTEX 的 location 属性中
    老式 POLYLINE 通常没有 bulge，全为直线段
    """
    try:
        # 闭合状态：flags 最低位为 1 表示闭合
        is_closed = bool(polyline.dxf.flags & 1)
        # 提取所有顶点坐标
        verts = list(polyline.vertices)
        pts: List[Tuple[float, float]] = []
        for v in verts:
            loc = v.dxfattribs().get("location")
            if loc is not None:
                pts.append((float(loc.x), float(loc.y)))
    except Exception as e:
        print(f"【警告】解析 POLYLINE 失败：{e}")
        return []

    if len(pts) < 2:
        print("【警告】跳过顶点数小于 2 的多段线")
        return []

    gcode: List[str] = []
    seg_count = len(pts) - 1
    if is_closed:
        seg_count += 1  # 闭合多段线要多画一段回到起点

    gcode.extend(_segment_header(pts[0][0], pts[0][1], safe_z, cut_depth, feed))

    for i in range(seg_count):
        p_curr = pts[i]
        p_next = pts[(i + 1) % len(pts)]
        x1, y1 = p_curr
        x2, y2 = p_next
        # POLYLINE 无 bulge，全部按直线段处理
        gcode.append(_emit_line_segment(x2, y2, feed))

    gcode.append(f"G00 Z{_fmt(safe_z)}")
    return gcode


def handle_spline(spline, safe_z: float, cut_depth: float, feed: float) -> List[str]:
    """
    处理 SPLINE（样条曲线）实体
    ezdxf 里 SPLINE 没有 fit_points，只有 control_points + knots
    用 BSpline.approximate() 离散化成折线，全部输出 G01
    """
    try:
        from ezdxf.math import BSpline

        # 提取控制点和 knots
        cp = list(spline.control_points)
        knots = list(spline.knots)
        degree = int(spline.dxf.degree)

        if len(cp) < 2:
            return []

        # DXF 里 knots 可能多一个（闭样条），去掉末尾重复的
        # 正常数量 = len(cp) + degree + 1
        expected_len = len(cp) + degree + 1
        if len(knots) > expected_len:
            knots = knots[:expected_len]

        # 用 BSpline 重建并离散化
        bs = BSpline(cp, order=degree + 1, knots=knots)
        # 按曲线长度自适应：每 2mm 一个采样点，最少 10 点，最多 200 点
        approx = list(bs.approximate(segments=30))
        pts = [(float(p[0]), float(p[1])) for p in approx]

        if len(pts) < 2:
            return []

        # 检查闭合（flags & 1 = 闭合样条）
        is_closed = bool(spline.dxf.flags & 1)

    except ImportError:
        print("【警告】ezdxf.math 里找不到 BSpline，跳过 SPLINE")
        return []
    except Exception as e:
        print(f"【警告】解析 SPLINE 失败：{e}")
        return []

    gcode: List[str] = []
    seg_count = len(pts) - 1
    if is_closed:
        seg_count += 1

    gcode.extend(_segment_header(pts[0][0], pts[0][1], safe_z, cut_depth, feed))

    for i in range(seg_count):
        p_next = pts[(i + 1) % len(pts)]
        gcode.append(_emit_line_segment(p_next[0], p_next[1], feed))

    gcode.append(f"G00 Z{_fmt(safe_z)}")
    return gcode


def dxf_to_gcode(input_path: str, output_path: str,
                 safe_z: float, cut_depth: float, feed: float,
                 auto_offset: bool = True, origin: str = "br") -> None:
    """
    读 CAD 文件（DXF / DWF / DWFx）→ 转 G 代码 → 写入 .txt 文件

    参数 auto_offset：是否把图形平移到指定原点。
    参数 origin：加工原点位置，可选值：
        "bl" = 左下角（CNC 原点在图形左下）
        "br" = 右下角（CNC 原点在图形右下，本次需求）
        "tl" = 左上角
        "tr" = 右上角
    """
    # ---------- 1. 统一加载文件（根据扩展名自动选 ezdxf / ezdwf） ----------
    try:
        entities, fmt_name = _load_any_file(input_path)
    except RuntimeError as e:
        print(f"【错误】{e}")
        sys.exit(1)

    print(f"【文件格式】{fmt_name}")
    all_lines: List[str] = []

    # ---------- 2. 先算整体包围盒，用于识别外框 ----------
    global_bbox = _compute_global_bbox(entities)
    gx_min, gx_max, gy_min, gy_max = global_bbox
    width = gx_max - gx_min
    height = gy_max - gy_min
    print(f"【DXF 原始范围】X: {gx_min:.3f} ~ {gx_max:.3f}  ({width:.3f} mm)")
    print(f"               Y: {gy_min:.3f} ~ {gy_max:.3f}  ({height:.3f} mm)")

    # ---------- 3. 根据 origin 参数计算平移量 ----------
    # offset 的作用：让图形指定角点落到 CNC 坐标 (0, 0)
    # origin 参数指定的就是"哪个角点变成 CNC 的 (0, 0)"
    offset_x = 0.0
    offset_y = 0.0
    if auto_offset:
        if origin == "bl":            # origin 在 DXF 左下角 (gx_min, gy_min)
            offset_x = -gx_min
            offset_y = -gy_min
        elif origin == "br":          # origin 在 DXF 右下角 (gx_max, gy_min)
            offset_x = -gx_max
            offset_y = -gy_min
        elif origin == "tl":          # origin 在 DXF 左上角 (gx_min, gy_max)
            offset_x = -gx_min
            offset_y = -gy_max
        elif origin == "tr":          # origin 在 DXF 右上角 (gx_max, gy_max)
            offset_x = -gx_max
            offset_y = -gy_max
        else:
            print(f"【警告】未知 origin 参数 '{origin}'，使用默认 br")
            offset_x = -gx_max
            offset_y = -gy_min

        print(f"【加工原点位置】{origin}（CNC (0,0) = DXF ({-offset_x:.3f}, {-offset_y:.3f})）")
        print(f"【平移量】X = {offset_x:.3f}, Y = {offset_y:.3f}")
        print(f"【平移后 CNC 范围】X: {gx_min+offset_x:.3f} ~ {gx_max+offset_x:.3f}")
        print(f"                 Y: {gy_min+offset_y:.3f} ~ {gy_max+offset_y:.3f}")

    # ---------- 4. 程序头（固定格式） ----------
    all_lines.append("O1099;")          # 程序号
    all_lines.append("M03S3000;")        # 主轴正转 3000 转
    all_lines.append("G54G90;")          # 选择工件坐标系 + 绝对坐标
    all_lines.append("G00Z15;")          # 抬刀到安全高度 Z15
    all_lines.append("G00X0Y0;")         # 快速回原点
    all_lines.append("")                 # 空行，方便阅读

    # ---------- 4. 按实体顺序依次处理 ----------
    entity_count = 0
    skip_frame_count = 0
    supported_types = ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "SPLINE")
    for entity in entities:
        dxftype = entity.dxftype()

        # 跳过边框（包围盒恰好等于整个图形包围盒的实体 = 外框）
        if dxftype in supported_types and _is_frame(entity, global_bbox):
            skip_frame_count += 1
            continue

        if dxftype == "LINE":
            seg = handle_line(entity, safe_z, cut_depth, feed)
        elif dxftype == "ARC":
            seg = handle_arc(entity, safe_z, cut_depth, feed)
        elif dxftype == "CIRCLE":
            seg = handle_circle(entity, safe_z, cut_depth, feed)
        elif dxftype == "LWPOLYLINE":
            seg = handle_lwpolyline(entity, safe_z, cut_depth, feed)
        elif dxftype == "POLYLINE":
            seg = handle_polyline(entity, safe_z, cut_depth, feed)
        elif dxftype == "SPLINE":
            seg = handle_spline(entity, safe_z, cut_depth, feed)
        else:
            # 不认识的图元跳过，不要中断
            print(f"【提示】跳过不支持的图元类型：{dxftype}")
            continue

        if seg:
            # 如果有偏移，把每一行里的坐标都平移
            if offset_x != 0.0 or offset_y != 0.0:
                seg = _apply_offset(seg, offset_x, offset_y)
            entity_count += 1
            all_lines.extend(seg)
            all_lines.append("")  # 每个轮廓后加空行

    # ---------- 4. 程序尾 ----------
    all_lines.append(f"G00 Z{_fmt(safe_z)}")
    all_lines.append("M30")

    # ---------- 5. 写入输出文件 ----------
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines))

    print(f"【完成】共处理 {entity_count} 个图元，跳过边框 {skip_frame_count} 个")
    print(f"【输出】G 代码已保存至：{output_path}")


# ======================================================================
# 命令行入口
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="DXF → CNC G代码（仅 G00/G01/G02/G03）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
    python dxf2gcode.py -i name.dxf -o name.txt
    python dxf2gcode.py -i name.dxf -o name.txt -s 10 -d -1.0 -f 150
        """,
    )
    parser.add_argument("-i", "--input",  required=True, help="输入 DXF 文件路径")
    parser.add_argument("-o", "--output", required=True, help="输出 .txt G 代码文件路径")
    parser.add_argument("-s", "--safe-z",      type=float, default=15.0,  help="安全高度 Z（抬刀高度，默认 15.0，与程序头一致）")
    parser.add_argument("-d", "--cut-depth",   type=float, default=-0.2,  help="下刀深度（负值，默认 -0.2）")
    parser.add_argument("-f", "--feed",         type=float, default=200.0, help="进给速度 F（默认 200）")
    parser.add_argument("-p", "--origin",       default="br",
                        choices=["bl", "br", "tl", "tr"],
                        help="加工原点位置：bl=左下 br=右下 tl=左上 tr=右上（默认 br 右下角）")

    args = parser.parse_args()

    print(f"输入文件  : {args.input}")
    print(f"输出文件  : {args.output}")
    print(f"安全高度 Z: {args.safe_z}")
    print(f"下刀深度  : {args.cut_depth}")
    print(f"进给速度 F: {args.feed}")
    print(f"加工原点  : {args.origin}")
    print("-" * 40)

    dxf_to_gcode(
        input_path=args.input,
        output_path=args.output,
        safe_z=args.safe_z,
        cut_depth=args.cut_depth,
        feed=args.feed,
        origin=args.origin,
    )


if __name__ == "__main__":
    main()
