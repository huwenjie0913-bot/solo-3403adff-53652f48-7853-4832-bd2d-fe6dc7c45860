"""复原方案指标：覆盖率、未归位边缘，以及接受候选间的矛盾检测。"""
from __future__ import annotations

import json
import math
from collections import defaultdict

import numpy as np


# ---------------------------------------------------------------- 几何

def transform_contour(contour_mm, layout: dict) -> np.ndarray:
    """把毫米轮廓按 layout {x,y,rot,flip} 映射到画布毫米坐标（质心->x,y）。"""
    x, y = float(layout.get("x", 0.0)), float(layout.get("y", 0.0))
    rot = float(layout.get("rot", 0.0))
    flip = -1.0 if layout.get("flip") else 1.0
    c = np.asarray(contour_mm, dtype=np.float64)
    c = c - c.mean(axis=0)
    a = math.radians(rot)
    R = np.array([[math.cos(a), -math.sin(a)],
                  [math.sin(a), math.cos(a)]])
    c[:, 0] *= flip
    return c @ R.T + np.array([x, y])


def _arc_lengths(pts: np.ndarray) -> np.ndarray:
    return np.linalg.norm(np.roll(pts, -1, axis=0) - pts, axis=1)


def _poly_area(pts: np.ndarray) -> float:
    x, y = pts[:, 0], pts[:, 1]
    return abs(float(0.5 * abs(np.dot(x, np.roll(y, -1)) -
                               np.dot(y, np.roll(x, -1)))))


def _norm_frags(frags: dict) -> dict:
    return {int(k): v for k, v in frags.items()}


# ---------------------------------------------------------------- 指标

def compute_metrics(raw_frags: dict, layout: dict, accepted: list) -> dict:
    """frags: {id: {code, contour_mm, mm_per_px}}；accepted: candidate 行。"""
    frags = _norm_frags(raw_frags)
    occupied = _occupied_arcs(frags, accepted)

    total_peri = 0.0
    placed_peri = 0.0
    unplaced_edge = 0.0
    placed_area = 0.0
    all_pts = []
    placed = 0

    for fid, f in frags.items():
        pts_mm = np.asarray(f["contour_mm"], dtype=np.float64)
        total_peri += _arc_lengths(pts_mm).sum()
        l = layout.get(str(fid)) or layout.get(fid)
        if not l:
            continue
        placed += 1
        mapped = transform_contour(pts_mm, l)
        seg = _arc_lengths(mapped)
        placed_peri += seg.sum()
        free = ~occupied.get(fid, np.zeros(len(pts_mm), dtype=bool))
        unplaced_edge += seg[free].sum()
        placed_area += _poly_area(mapped)
        all_pts.append(mapped)

    union_hull_area = 0.0
    coverage = 0.0
    if all_pts:
        stack = np.vstack(all_pts)
        try:
            import cv2
            hull = cv2.convexHull(stack.astype(np.float32))
            union_hull_area = abs(float(cv2.contourArea(hull)))
        except Exception:
            lo, hi = stack.min(0), stack.max(0)
            union_hull_area = float((hi[0] - lo[0]) * (hi[1] - lo[1]))
        coverage = round(placed_area / union_hull_area, 4) if union_hull_area else 0.0

    return {
        "placed_count": placed,
        "total_count": len(frags),
        "coverage_of_hull": coverage,
        "placed_area_mm2": round(placed_area, 1),
        "assembly_hull_mm2": round(union_hull_area, 1),
        "unplaced_edge_mm": round(unplaced_edge, 1),
        "placed_perimeter_mm": round(placed_peri, 1),
        "unplaced_edge_ratio": round(unplaced_edge / placed_peri, 3) if placed_peri else 0.0,
    }


def _occupied_arcs(frags: dict, accepted: list) -> dict:
    """根据候选 params 的 ia/ib 标记已被接缝占用的轮廓点。"""
    occ: dict[int, np.ndarray] = {}

    def mask_for(fid):
        if fid not in occ:
            occ[fid] = np.zeros(len(frags[fid]["contour_mm"]), dtype=bool)
        return occ[fid]

    for row in accepted:
        p = json.loads(row["params"] or "{}")
        ia, ib = p.get("ia"), p.get("ib")
        if not ia or not ib:
            continue
        a, b = row["frag_a"], row["frag_b"]
        if a not in frags or b not in frags:
            continue
        ma, mb = mask_for(a), mask_for(b)
        ma[np.array(ia) % len(ma)] = True
        mb[np.array(ib) % len(mb)] = True
    return occ


# ---------------------------------------------------------------- 矛盾

def detect_conflicts(raw_frags: dict, accepted: list) -> list[dict]:
    """检查已接受候选之间是否互相矛盾。

    1. 同一件碎片的同一段边缘被两条接缝占用（一对多）；
    2. 三件环中 A-B、B-C、C-A 的旋转角无法闭合。
    """
    frags = _norm_frags(raw_frags)
    conflicts: list[dict] = []
    arc_use = defaultdict(list)
    cand_map = {}
    for row in accepted:
        p = json.loads(row["params"] or "{}")
        ia, ib = p.get("ia"), p.get("ib")
        cid = row["id"]
        cand_map[cid] = (row, p)
        if ia and ib:
            arc_use[row["frag_a"]].append((cid, set(ia)))
            arc_use[row["frag_b"]].append((cid, set(ib)))

    # 1) 边缘重叠占用（超过 25% 弧段重合即冲突）
    for fid, uses in arc_use.items():
        for i in range(len(uses)):
            for j in range(i + 1, len(uses)):
                c1, s1 = uses[i]
                c2, s2 = uses[j]
                inter = len(s1 & s2)
                if inter > 0.25 * min(len(s1), len(s2)):
                    conflicts.append({
                        "type": "edge_overlap",
                        "candidates": [c1, c2],
                        "fragment": fid,
                        "message": f"碎片 #{fid} 同一段边缘同时被候选 "
                                   f"#{c1} 与 #{c2} 占用，拼接互相矛盾",
                    })

    # 2) 三角形环的旋转角闭合
    adj = defaultdict(dict)
    for cid, (row, p) in cand_map.items():
        if "R" not in p:
            continue
        a = math.degrees(math.atan2(p["R"][1][0], p["R"][0][0]))
        adj[row["frag_a"]][row["frag_b"]] = (cid, a)
        adj[row["frag_b"]][row["frag_a"]] = (cid, -a)
    seen_tri: set = set()
    for a in list(adj):
        for b in list(adj[a]):
            for c in list(adj[b]):
                if c == a or c not in adj[a]:
                    continue
                tri = tuple(sorted((a, b, c)))
                if tri in seen_tri:
                    continue
                seen_tri.add(tri)
                ang = adj[a][b][1] + adj[b][c][1] + adj[c][a][1]
                ang = (ang + 180) % 360 - 180
                if abs(ang) > 12:
                    conflicts.append({
                        "type": "closure_angle",
                        "candidates": [adj[a][b][0], adj[b][c][0], adj[a][c][0]],
                        "fragments": list(tri),
                        "message": f"候选 {adj[a][b][0]}、{adj[b][c][0]}、"
                                   f"{adj[a][c][0]} 构成环但旋转角不闭合"
                                   f"（残差 {ang:.0f}°），无法同时成立",
                    })
    return conflicts
