# -*- coding: utf-8 -*-
"""
预处理脚本：过滤掉距离过近的 POLYLINE，保留较长的线条
用法：python _filter_dense.py input.dxf -o output.dxf -t 0.5
"""
import argparse                # 命令行参数
import math                    # 几何计算
import sys                     # 退出
import os                      # 路径

import ezdxf                   # DXF 读写


def _polyline_vertices(pl):
    """从 POLYLINE 里提取顶点坐标列表"""
    verts = []
    if hasattr(pl, "vertices"):
        for v in pl.vertices:
            loc = v.dxfattribs().get("location")
            if loc is not None:
                verts.append((float(loc.x), float(loc.y)))
    elif hasattr(pl, "get_points"):
        try:
            for p in pl.get_points("xy"):
                verts.append((float(p[0]), float(p[1])))
        except Exception:
            pass
    return verts


def _polyline_length(verts):
    """计算 POLYLINE 的总长度"""
    if len(verts) < 2:
        return 0.0
    length = sum(math.hypot(verts[j][0] - verts[j-1][0], verts[j][1] - verts[j-1][1])
                 for j in range(1, len(verts)))
    return length


def _polyline_bbox(verts):
    """计算包围盒 (x_min, x_max, y_min, y_max)"""
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    return (min(xs), max(xs), min(ys), max(ys))


def _polyline_min_dist(a_verts, b_verts, sample_n=40):
    """
    两条 POLYLINE 的顶点间最近距离
    为了速度，每条线只采样 sample_n 个点（均匀间隔）
    """
    if len(a_verts) > sample_n:
        step = max(1, len(a_verts) // sample_n)
        a_verts = a_verts[::step]
    if len(b_verts) > sample_n:
        step = max(1, len(b_verts) // sample_n)
        b_verts = b_verts[::step]

    d_min = float("inf")
    for ax, ay in a_verts:
        for bx, by in b_verts:
            d = math.hypot(ax - bx, ay - by)
            if d < d_min:
                d_min = d
                if d_min < 0.01:
                    return d_min    # 已经非常近了，提前返回
    return d_min


def _filter_polylines(entities, threshold):
    """
    贪心策略过滤密集 POLYLINE：
      1. 按总长度从长到短排序（长的优先保留）
      2. 遍历每条，和已保留的 POLYLINE 算距离
      3. 如果和任何一条已保留的距离 < 阈值，丢弃；否则保留

    返回 (保留的实体列表, 丢弃的实体列表, 过滤统计信息)
    """
    # 收集 POLYLINE 并计算属性
    infos = []
    non_polylines = []       # 非 POLYLINE 实体（全部保留）

    for e in entities:
        t = e.dxftype()
        if t in ("POLYLINE", "LWPOLYLINE"):
            verts = _polyline_vertices(e)
            if len(verts) < 2:
                non_polylines.append(e)
                continue
            infos.append({
                "entity": e,
                "verts": verts,
                "length": _polyline_length(verts),
                "bbox": _polyline_bbox(verts),
            })
        else:
            # 其他实体（LINE、ARC、CIRCLE、样条、文字）全部保留
            non_polylines.append(e)

    # 按长度从长到短排序
    infos.sort(key=lambda x: x["length"], reverse=True)

    # 贪心过滤
    kept = []
    removed = []

    for info in infos:
        keep = True
        bx0, bx1, by0, by1 = info["bbox"]

        for k in kept:
            kx0, kx1, ky0, ky1 = k["bbox"]

            # 包围盒粗筛：如果两条线的包围盒距离已经 > 阈值的 2 倍，
            # 那精确距离一定远，跳过（省计算时间）
            hx = max(kx0 - bx1, 0, bx0 - kx1)
            hy = max(ky0 - by1, 0, by0 - ky1)
            bbox_d = math.hypot(hx, hy)
            if bbox_d > threshold * 2.0:
                continue

            # 精确计算
            d = _polyline_min_dist(info["verts"], k["verts"])
            if d < threshold:
                keep = False
                break

        if keep:
            kept.append(info)
        else:
            removed.append(info)

    # 组装结果
    kept_entities = non_polylines + [k["entity"] for k in kept]

    stats = {
        "total": len(entities),
        "polylines_total": len(infos),
        "polylines_kept": len(kept),
        "polylines_removed": len(removed),
        "non_polylines": len(non_polylines),
        "threshold": threshold,
        "kept_avg_length": sum(k["length"] for k in kept) / max(1, len(kept)),
        "removed_avg_length": sum(r["length"] for r in removed) / max(1, len(removed)),
    }

    return kept_entities, stats


def main():
    parser = argparse.ArgumentParser(description="过滤 DXF 中距离过近的 POLYLINE")
    parser.add_argument("-i", "--input", required=True, help="输入 DXF 文件路径")
    parser.add_argument("-o", "--output", required=True, help="输出过滤后 DXF 文件路径")
    parser.add_argument("-t", "--threshold", type=float, default=0.5,
                        help="最小间距阈值（mm）。两条 POLYLINE 距离小于此值就丢弃短的那条。"
                             "默认 0.5（刚好能让 0.3mm 刀头刻开）")

    args = parser.parse_args()

    # 读取
    print(f"读取: {args.input}")
    doc = ezdxf.readfile(args.input)
    msp = doc.modelspace()
    entities = list(msp)
    print(f"  总实体数: {len(entities)}")

    # 过滤
    kept, stats = _filter_polylines(entities, args.threshold)

    # 重建 DXF（输出用 R2010，兼容性最好，能同时装下 POLYLINE 和 LWPOLYLINE）
    doc_out = ezdxf.new(dxfversion="R2010")
    msp_out = doc_out.modelspace()

    # 搬实体：兼容 POLYLINE（旧格式）和 LWPOLYLINE（新格式）
    for e in kept:
        try:
            if e.dxftype() == "POLYLINE":
                # R12 旧格式 POLYLINE → 转成 LWPOLYLINE 输出
                verts = _polyline_vertices(e)
                pl_out = msp_out.add_lwpolyline(verts)
                if bool(getattr(e, "closed", False)) or (e.dxf.flags & 1):
                    pl_out.close()
            elif e.dxftype() == "LWPOLYLINE":
                verts = [(float(p[0]), float(p[1])) for p in e.get_points()]
                pl_out = msp_out.add_lwpolyline(verts)
                if bool(e.closed):
                    pl_out.close()
            else:
                # 其他类型（LINE/ARC/CIRCLE 等）直接复制
                msp_out.add_entity(e.copy_to(msp_out.doc))
        except Exception as e2:
            print(f"  [警告] 实体复制失败，已跳过: {e.dxftype()} — {e2}")

    # 输出
    doc_out.saveas(args.output)

    # 打印统计
    print(f"\n过滤完成（阈值 {args.threshold}mm）:")
    print(f"  POLYLINE 总数: {stats['polylines_total']}")
    print(f"  保留: {stats['polylines_kept']} 条 ({stats['polylines_kept']/max(1,stats['polylines_total'])*100:.1f}%)")
    print(f"  丢弃: {stats['polylines_removed']} 条 ({stats['polylines_removed']/max(1,stats['polylines_total'])*100:.1f}%)")
    print(f"  保留的平均长度: {stats['kept_avg_length']:.2f}mm")
    print(f"  丢弃的平均长度: {stats['removed_avg_length']:.2f}mm")
    if stats['non_polylines'] > 0:
        print(f"  其他实体（全部保留）: {stats['non_polylines']}")
    print(f"\n输出: {args.output}")


if __name__ == "__main__":
    main()
