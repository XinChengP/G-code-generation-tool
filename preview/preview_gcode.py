# -*- coding: utf-8 -*-
"""
G 代码二维预览工具
=================
功能：读取 CNC .txt / .tap / .gcode 文件，解析 G00/G01/G02/G03 轨迹，
      生成 PNG 图片展示 XY 平面的刀具路径。
依赖：Pillow（已装），matplotlib 可选（效果更好）
运行：
    python preview_gcode.py ../11111.txt
    python preview_gcode.py ../11111.txt -o preview.png
"""

import argparse                # 命令行参数
import math                    # 圆弧离散化
import re                      # 正则解析 G 代码
import sys                     # 退出
import os                      # 文件路径
import webbrowser              # 打开图片
from typing import List, Tuple # 类型标注

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("【错误】缺少 Pillow，请先执行：pip install Pillow")
    sys.exit(1)


# ======================================================================
# G 代码解析
# ======================================================================

# 匹配一行里的 G 指令、X/Y/Z/I/J/F 等字段
_TOKEN_RE = re.compile(r"([A-Za-z])\s*(-?\d+\.?\d*)")


def parse_gcode_file(filepath: str) -> List[dict]:
    """
    逐行解析 G 代码文件，返回事件列表
    """
    events: List[dict] = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"【错误】无法读取文件：{e}")
        sys.exit(1)

    for raw_line in lines:
        line = raw_line.strip()
        # 跳过注释、空行、程序号
        if not line or line.startswith("(") or line.startswith(";") or line.startswith("O"):
            continue

        # 提取所有 token
        tokens = _TOKEN_RE.findall(line)
        if not tokens:
            continue

        fields: dict = {}
        for letter, value in tokens:
            letter = letter.upper()
            try:
                fields[letter] = float(value)
            except ValueError:
                pass

        g_type = None
        g_val = fields.get("G")
        if g_val is not None:
            g_int = int(g_val)
            if g_int == 0:
                g_type = "rapid"
            elif g_int == 1:
                g_type = "line"
            elif g_int == 2:
                g_type = "cw"
            elif g_int == 3:
                g_type = "ccw"
            else:
                continue  # 其他 G 指令跳过

        # 没有任何坐标 → 跳过
        if "X" not in fields and "Y" not in fields and "Z" not in fields and g_type is None:
            continue

        event = {
            "g_type": g_type,
            "x": fields.get("X"),
            "y": fields.get("Y"),
            "z": fields.get("Z"),
            "i": fields.get("I"),
            "j": fields.get("J"),
        }
        events.append(event)

    return events


def build_toolpath(events: List[dict]) -> List[dict]:
    """
    重建完整刀具路径（每条线段/弧带完整坐标）
    """
    path: List[dict] = []
    cur_x, cur_y, cur_z = 0.0, 0.0, 0.0
    cur_mode = "rapid"

    for ev in events:
        if ev["g_type"] is not None:
            cur_mode = ev["g_type"]

        nx = ev["x"] if ev["x"] is not None else cur_x
        ny = ev["y"] if ev["y"] is not None else cur_y
        nz = ev["z"] if ev["z"] is not None else cur_z

        xy_changed = (nx != cur_x) or (ny != cur_y)

        if xy_changed:
            is_cutting = nz < -1e-9
            seg = {
                "kind": cur_mode,
                "x1": cur_x, "y1": cur_y,
                "x2": nx,    "y2": ny,
                "cx": None,  "cy": None,  "r": None,
                "is_cutting": is_cutting,
            }
            if cur_mode in ("cw", "ccw"):
                i_val = ev["i"] if ev["i"] is not None else 0.0
                j_val = ev["j"] if ev["j"] is not None else 0.0
                cx = cur_x + i_val
                cy = cur_y + j_val
                r = math.hypot(i_val, j_val)
                seg["cx"] = cx
                seg["cy"] = cy
                seg["r"]  = r
            path.append(seg)

        cur_x, cur_y, cur_z = nx, ny, nz

    return path


def _arc_points(x1, y1, x2, y2, cx, cy, r, is_ccw: bool, steps: int = 96):
    """把 G02/G03 圆弧离散成点序列"""
    a_start = math.atan2(y1 - cy, x1 - cx)
    a_end   = math.atan2(y2 - cy, x2 - cx)

    if is_ccw:
        if a_end < a_start:
            a_end += 2.0 * math.pi
    else:
        if a_end > a_start:
            a_end -= 2.0 * math.pi

    delta = a_end - a_start
    if abs(delta) < 1e-12:
        delta = 2.0 * math.pi if is_ccw else -2.0 * math.pi

    points = []
    for i in range(steps + 1):
        a = a_start + delta * (i / steps)
        points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return points


# ======================================================================
# Pillow 可视化
# ======================================================================

def render_pillow(path: List[dict], out_path: str, cutting_only: bool = False) -> None:
    """
    用 Pillow 画刀具路径并保存为 PNG

    cutting_only=True 时，只画切削轨迹（Z<0 的 G01/G02/G03），
    不画 G00 快速移动，也不画抬刀空跑。适合只想看雕刻形状的场景。
    """
    # ---- 1. 根据 cutting_only 过滤轨迹 ----
    if cutting_only:
        path_to_draw = [seg for seg in path if seg["is_cutting"]]
    else:
        path_to_draw = path

    if not path_to_draw:
        print(f"【警告】没有 {'切削' if cutting_only else ''}轨迹可画")
        return

    # ---- 2. 收集坐标，自适应范围 ----
    all_x: List[float] = []
    all_y: List[float] = []
    for seg in path_to_draw:
        all_x.extend([seg["x1"], seg["x2"]])
        all_y.extend([seg["y1"], seg["y2"]])
        if seg["cx"] is not None:
            r = seg["r"]
            all_x.extend([seg["cx"] - r, seg["cx"] + r])
            all_y.extend([seg["cy"] - r, seg["cy"] + r])

    if not all_x:
        print("【警告】没有轨迹可画")
        return

    x_min, x_max = min(all_x), max(all_x)
    y_min, y_max = min(all_y), max(all_y)

    # ---- 2. 画布参数 ----
    W, H = 1200, 960          # 画布像素
    padding = 70               # 四周留白（像素）
    plot_w = W - 2 * padding
    plot_h = H - 2 * padding

    # 边距（毫米），避免图形贴边
    if x_max == x_min:
        x_min -= 1.0; x_max += 1.0
    if y_max == y_min:
        y_min -= 1.0; y_max += 1.0
    x_span = x_max - x_min
    y_span = y_max - y_min
    x_min -= x_span * 0.05
    x_max += x_span * 0.05
    y_min -= y_span * 0.05
    y_max += y_span * 0.05

    # 等比例缩放：以较大的 span 为基准，保证不失真
    sx = plot_w / (x_max - x_min)
    sy = plot_h / (y_max - y_min)
    scale = min(sx, sy)

    # 居中偏移
    used_w = (x_max - x_min) * scale
    used_h = (y_max - y_min) * scale
    off_x = padding + (plot_w - used_w) / 2
    off_y = padding + (plot_h - used_h) / 2

    # ---- 3. 坐标变换函数（XY → 像素）----
    def to_pixel(x: float, y: float) -> Tuple[int, int]:
        px = int(off_x + (x - x_min) * scale)
        # Pillow Y 向下为正，CNC Y 向上为正 → 翻转
        py = int(H - off_y - (y - y_min) * scale)
        return (px, py)

    # ---- 4. 画 ----
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    # 网格（灰色细线）
    _draw_grid(draw, to_pixel, x_min, x_max, y_min, y_max, scale)

    # 原点十字线
    o_pix = to_pixel(0, 0)
    draw.line([(o_pix[0], padding), (o_pix[0], H - padding)], fill=(180, 180, 180), width=1)
    draw.line([(padding, o_pix[1]), (W - padding, o_pix[1])], fill=(180, 180, 180), width=1)

    # ---- 画每条轨迹 ----
    for seg in path_to_draw:
        kind = seg["kind"]
        cutting = seg["is_cutting"]
        p1 = to_pixel(seg["x1"], seg["y1"])
        p2 = to_pixel(seg["x2"], seg["y2"])

        if kind == "rapid":
            # G00：灰色（快速空跑）—— cutting_only 模式下不会进这里，因为已过滤
            draw.line([p1, p2], fill=(170, 170, 170), width=1)

        elif kind in ("cw", "ccw"):
            # 圆弧：离散化后画
            pts = _arc_points(seg["x1"], seg["y1"], seg["x2"], seg["y2"],
                              seg["cx"], seg["cy"], seg["r"],
                              is_ccw=(kind == "ccw"))
            pixels = [to_pixel(p[0], p[1]) for p in pts]
            if cutting:
                draw.line(pixels, fill=(26, 95, 180), width=2)
            else:
                draw.line(pixels, fill=(170, 170, 170), width=1)

        else:  # line
            if cutting:
                # 切削：蓝色实线，宽度 2
                draw.line([p1, p2], fill=(26, 95, 180), width=2)
            else:
                # 抬刀空跑：浅灰色，宽度 1
                draw.line([p1, p2], fill=(200, 200, 200), width=1)

    # ---- 5. 图例 + 标题 + 范围 ----
    try:
        font = ImageFont.truetype("msyh.ttc", 14)       # Windows 微软雅黑
        font_small = ImageFont.truetype("msyh.ttc", 11)
    except Exception:
        font = ImageFont.load_default()
        font_small = font

    # 标题（注明是完整轨迹还是仅切削）
    mode_label = "【仅切削轨迹】" if cutting_only else "【完整轨迹】"
    draw.text((padding, 15), f"G 代码预览：{mode_label}", fill="black", font=font)

    # 图例
    leg_x = W - padding - 180
    leg_y = 15

    if cutting_only:
        # 仅切削模式：只显示切削图例
        draw.line([(leg_x, leg_y + 8), (leg_x + 30, leg_y + 8)],
                  fill=(26, 95, 180), width=2)
        draw.text((leg_x + 36, leg_y + 2), "切削轨迹 G01/G02/G03", fill="black", font=font_small)
    else:
        # 完整模式：三条图例
        # 切削（蓝色粗实线）
        draw.line([(leg_x, leg_y + 8), (leg_x + 30, leg_y + 8)],
                  fill=(26, 95, 180), width=2)
        draw.text((leg_x + 36, leg_y + 2), "切削 G01/G02/G03", fill="black", font=font_small)
        # 快速（灰色实线）
        ry1 = leg_y + 28
        draw.line([(leg_x, ry1 + 8), (leg_x + 30, ry1 + 8)],
                  fill=(170, 170, 170), width=1)
        draw.text((leg_x + 36, ry1 + 2), "快速 G00", fill="black", font=font_small)
        # 抬刀空跑（浅灰色实线）
        ry2 = leg_y + 52
        draw.line([(leg_x, ry2 + 8), (leg_x + 30, ry2 + 8)],
                  fill=(200, 200, 200), width=1)
        draw.text((leg_x + 36, ry2 + 2), "抬刀空跑", fill="black", font=font_small)

    # 坐标范围（右下角）
    info = (f"X: {x_min:.2f} ~ {x_max:.2f} mm\n"
            f"Y: {y_min:.2f} ~ {y_max:.2f} mm\n"
            f"缩放: {scale:.2f} px/mm")
    # 画个半透明底
    box = (W - padding - 200, H - padding - 80,
           W - padding,        H - padding)
    draw.rectangle(box, fill=(255, 255, 255), outline=(200, 200, 200))
    draw.multiline_text((box[0] + 8, box[1] + 6), info,
                        fill=(80, 80, 80), font=font_small, spacing=3)

    # 坐标轴标签
    draw.text((W - padding - 80, H - 22), "X (mm)", fill="black", font=font_small)
    draw.text((12, padding + 3), "Y (mm)", fill="black", font=font_small)

    img.save(out_path, "PNG")
    print(f"【保存】预览图：{out_path}")


def _draw_grid(draw, to_pixel, x_min, x_max, y_min, y_max, scale):
    """画灰色网格"""
    # 根据图形尺寸自动选网格间距
    target_step_px = 50  # 网格线间距约 50 像素
    mm_step = target_step_px / scale
    # 把间距调整到好看的数字（1、2、5、10、20、50…）
    nice_steps = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]
    for ns in nice_steps:
        if ns >= mm_step:
            step = ns
            break
    else:
        step = 500

    # X 方向
    x_start = math.ceil(x_min / step) * step
    x = x_start
    while x <= x_max:
        p = to_pixel(x, 0)
        draw.line([(p[0], 70), (p[0], 70 + int((y_max - y_min) * scale + 20))],
                  fill=(225, 225, 225), width=1)
        x += step

    # Y 方向
    y_start = math.ceil(y_min / step) * step
    y = y_start
    while y <= y_max:
        p = to_pixel(0, y)
        draw.line([(70, p[1]), (70 + int((x_max - x_min) * scale + 20), p[1])],
                  fill=(225, 225, 225), width=1)
        y += step


def _draw_dashed(draw, points, fill, width=1, dash=6, gap=4):
    """Pillow 画虚线——手动分段"""
    if len(points) < 2:
        if len(points) == 2:
            draw.line(points, fill=fill, width=width)
        return
    # 把折线拆成每段 dash+gap 的小线段
    seg_len = dash + gap
    acc = 0
    drawing = True  # True=正在画，False=跳过
    out: List[Tuple[int, int]] = []
    out.append(points[0])

    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        d = math.hypot(x2 - x1, y2 - y1)
        if d < 0.01:
            out.append((x2, y2))
            continue
        dx = (x2 - x1) / d
        dy = (y2 - y1) / d
        remaining = d
        t = 0.0
        while remaining > 0.01:
            if drawing:
                this_seg = min(dash - (acc % seg_len), remaining)
            else:
                this_seg = min(gap - (acc % seg_len), remaining)
            t += this_seg / d
            acc += this_seg
            remaining -= this_seg
            if drawing:
                out.append((int(x1 + (x2 - x1) * t),
                            int(y1 + (y2 - y1) * t)))
            else:
                # 空段：断开，新起一个点
                out.append((None, None))
            # 切换
            if acc % seg_len < (dash if drawing else gap) - 0.001:
                pass
            drawing = not drawing
        out.append((x2, y2))

    # 过滤并绘制
    current: List[Tuple[int, int]] = []
    for p in out:
        if p[0] is None:
            if len(current) >= 2:
                draw.line(current, fill=fill, width=width)
            current = []
        else:
            current.append(p)
    if len(current) >= 2:
        draw.line(current, fill=fill, width=width)


# ======================================================================
# 主入口
# ======================================================================

def main():
    parser = argparse.ArgumentParser(description="G 代码二维预览工具")
    parser.add_argument("input", help="输入 G 代码文件（.txt / .nc / .tap / .gcode）")
    parser.add_argument("-o", "--output", help="输出图片路径（默认自动出两张：_full.png 和 _cutting_only.png）")
    parser.add_argument("--single", action="store_true", help="只出一张图（完整轨迹），默认出两张")
    parser.add_argument("--no-open", action="store_true", help="生成后不自动打开图片")

    args = parser.parse_args()

    print(f"【读取】{args.input}")
    events = parse_gcode_file(args.input)
    if not events:
        print("【错误】没有解析到任何有效 G 代码")
        return

    path = build_toolpath(events)
    print(f"【解析】{len(path)} 段轨迹")

    # 统计
    kind_count = {"rapid": 0, "line": 0, "cw": 0, "ccw": 0}
    for seg in path:
        kind_count[seg["kind"]] = kind_count.get(seg["kind"], 0) + 1
    cutting_count = sum(1 for s in path if s["is_cutting"])
    print(f"  G00 快速: {kind_count['rapid']} 段")
    print(f"  G01 直线: {kind_count['line']} 段")
    print(f"  G02 顺圆: {kind_count['cw']}   段")
    print(f"  G03 逆圆: {kind_count['ccw']}   段")
    print(f"  切削段数: {cutting_count} / {len(path)}")

    base = os.path.splitext(args.input)[0]
    generated: List[str] = []

    if args.single or args.output is not None:
        # 单图模式
        out_path = args.output if args.output else base + "_full.png"
        print(f"\n【生成】完整轨迹图：{out_path}")
        render_pillow(path, out_path, cutting_only=False)
        generated.append(out_path)
    else:
        # 双图模式（默认）
        full_path = base + "_full.png"
        cutting_path = base + "_cutting.png"

        print(f"\n【生成 1/2】完整轨迹（含抬刀空跑）：{full_path}")
        render_pillow(path, full_path, cutting_only=False)
        generated.append(full_path)

        print(f"【生成 2/2】仅切削轨迹：{cutting_path}")
        render_pillow(path, cutting_path, cutting_only=True)
        generated.append(cutting_path)

    # 自动打开
    if not args.no_open:
        for gp in generated:
            abs_path = os.path.abspath(gp)
            webbrowser.open("file:///" + abs_path.replace("\\", "/"))
        print(f"【打开】已用默认程序打开图片")


if __name__ == "__main__":
    main()
