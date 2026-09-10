"""装配次序与临时支撑规划（全部本地计算，不访问网络）。

依据方案布局、已接受接缝（含人工复核修正）、校准轮廓与厚度/重量，
结合用户在画布上标注的托点、夹具禁入区、先后关系以及接缝胶粘参数，
生成分阶段装配候选：

  * 每步校验“已装整体”的投影重心是否落在支撑域（托点凸包）内；
  * 新装碎片自身重心须落在支撑域、接缝接触带或其下方临时支撑上，
    否则视为悬臂，需接缝按承载方向设置承担，并在固化后再加载；
  * 先后关系（必须先装/后装）与锁定步骤作为硬约束；
  * 无解时逐件定位阻塞原因，并给出建议托点位置。

物理约定（与修复台作业一致的简化模型）：
  * 投影重心 = 碎片多边形形心在画布（水平面）上的投影；碎片质心即
    画布锚点（features.centroid_px 为多边形形心），故碎片投影重心
    恰为布局坐标 (x, y)，整体重心为各件按重量的加权平均；
  * 支撑域 = 全部托点/临时支撑的凸包；未标托点时按底件外轮廓
    （碎片平放台面）作为支撑域；
  * 接缝涂胶后在固化前只能“抓住”接触带附近 SEAM_GRIP_MM 范围内的
    碎片重心，超出则需临时支撑或按悬臂处理。
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np

SUPPORT_TOL_MM = 2.0       # 重心允许超出支撑域边界的容差
DEGENERATE_TOL_MM = 8.0    # 支撑点不足 3 个（点/线支撑域）时的容差
SEAM_GRIP_MM = 8.0         # 接缝接触带能“抓住”碎片重心的距离
DENSITY_G_MM3 = 0.0024     # 陶瓷估算密度 2.4 g/cm³（重量未补录时）
DEFAULT_THICK_MM = 5.0     # 厚度未录时的估算厚度
DEFAULT_OPEN_MIN = 5.0     # 胶粘默认开放时间（分钟）
DEFAULT_CURE_MIN = 30.0    # 胶粘默认固化时间（分钟）
MIN_CONTACT_MM = 25.0      # 低于该接触长度给出风险提示
LOAD_DIR_DOT = 0.5         # 承载方向允许的最小夹角余弦

LOAD_DIR_LABEL = {"any": "任意方向", "along": "仅沿接缝",
                  "inward": "仅指向主体", "none": "固化前不可承载"}


# ---------------------------------------------------------------- 几何

def _hull(points) -> np.ndarray:
    """单调链凸包，返回 (m,2) 顶点；不足 3 点时原样返回。"""
    pts = sorted({(float(p[0]), float(p[1])) for p in points})
    if len(pts) <= 2:
        return np.array(pts, dtype=np.float64).reshape(-1, 2)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1], dtype=np.float64)


def _seg_dist(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    ab = b - a
    den = float(ab @ ab)
    t = 0.0 if den < 1e-12 else max(0.0, min(1.0, float((p - a) @ ab) / den))
    return float(np.linalg.norm(p - (a + t * ab)))


def _signed_dist_to_domain(p, domain: np.ndarray) -> float:
    """到支撑域的带符号距离：负值在域内，正值在域外。

    支撑域 1 点退化为点、2 点退化为线段（此时返回值恒 ≥0）。
    """
    p = np.asarray(p, dtype=np.float64)
    n = len(domain)
    if n == 0:
        return 1e9
    if n == 1:
        return float(np.linalg.norm(p - domain[0]))
    if n == 2:
        return _seg_dist(p, domain[0], domain[1])
    d_min = 1e18
    inside = True
    for i in range(n):
        a, b = domain[i], domain[(i + 1) % n]
        edge = b - a
        if edge[0] * (p[1] - a[1]) - edge[1] * (p[0] - a[0]) < 0:
            inside = False
        d_min = min(d_min, _seg_dist(p, a, b))
    return -d_min if inside else d_min


def _domain_tol(domain: np.ndarray) -> float:
    return SUPPORT_TOL_MM if len(domain) >= 3 else DEGENERATE_TOL_MM


def _point_in_poly(p, poly: np.ndarray) -> bool:
    x, y = float(p[0]), float(p[1])
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
                (x < (xj - xi) * (y - yi) / (yj - yi + 1e-300) + xi):
            inside = not inside
        j = i
    return inside


def _poly_centroid(pts: np.ndarray) -> np.ndarray:
    """多边形形心（面积加权）；退化时退化为顶点均值。"""
    x, y = pts[:, 0], pts[:, 1]
    cr = x * np.roll(y, -1) - np.roll(x, -1) * y
    a = cr.sum()
    if abs(a) < 1e-9:
        return pts.mean(axis=0)
    return np.array([((x + np.roll(x, -1)) * cr).sum() / (3 * a),
                     ((y + np.roll(y, -1)) * cr).sum() / (3 * a)])


def world_contour(frag: dict, layout: dict) -> np.ndarray:
    """与前端画布一致：轮廓 px -> 以质心为锚点的世界 mm 坐标。"""
    pts = np.asarray(frag["contour"], dtype=np.float64)
    anchor = np.asarray(frag.get("centroid_px") or [0.0, 0.0], dtype=np.float64)
    mm = float(frag.get("mm_per_px") or 1.0)
    local = (pts - anchor) * mm
    if layout.get("flip"):
        local = local * np.array([-1.0, 1.0])
    a = math.radians(float(layout.get("rot", 0.0)))
    R = np.array([[math.cos(a), -math.sin(a)],
                  [math.sin(a), math.cos(a)]])
    return local @ R.T + np.array([float(layout.get("x", 0.0)),
                                   float(layout.get("y", 0.0))])


def seam_geometry(frags: dict, layout: dict, row: dict) -> dict | None:
    """由候选（或复核修正后的）ia/ib 计算世界坐标接缝折线。

    row: candidate 行解析后的 dict（params 已含复核覆盖）。
    """
    p = row.get("params") or {}
    ia, ib = p.get("ia"), p.get("ib")
    if not ia or not ib:
        return None
    fa, fb = frags.get(row["frag_a"]), frags.get(row["frag_b"])
    la = layout.get(row["frag_a"])
    lb = layout.get(row["frag_b"])
    if fa is None or fb is None or la is None or lb is None:
        return None
    wa = world_contour(fa, la)
    wb = world_contour(fb, lb)
    pa = wa[np.asarray(ia, dtype=int) % len(wa)]
    pb = wb[np.asarray(ib, dtype=int) % len(wb)]
    mid = (pa + pb) / 2.0
    contact = float(np.linalg.norm(np.diff(pa, axis=0), axis=1).sum()) \
        if len(pa) > 1 else 0.0
    return {
        "id": row["id"],
        "frag_a": row["frag_a"], "frag_b": row["frag_b"],
        "contact_mm": round(contact, 1),
        "path": mid.round(2).tolist(),
        "center": mid.mean(axis=0).round(2).tolist(),
    }


# ---------------------------------------------------------------- 规划

def _norm_layout(layout: dict) -> dict:
    return {int(k): v for k, v in (layout or {}).items()}


def _weight_of(frag: dict) -> tuple[float, bool]:
    """返回 (重量 g, 是否估算)。未补录时按 面积×厚度×陶瓷密度 估算。"""
    w = float(frag.get("weight_g") or 0)
    if w > 0:
        return w, False
    area = float(frag.get("area_mm2") or 0)
    thick = float(frag.get("thickness") or 0) or DEFAULT_THICK_MM
    return max(area * thick * DENSITY_G_MM3, 1.0), True


def _relation_preds(relations: list) -> dict:
    preds: dict[int, set] = {}
    for r in relations or []:
        before, after = int(r.get("before", 0)), int(r.get("after", 0))
        if before and after and before != after:
            preds.setdefault(after, set()).add(before)
    return preds


def _find_cycle(relations: list) -> list[int] | None:
    """检测先后关系环，返回构成环的碎片 id 列表（无环返回 None）。"""
    nxt: dict[int, list] = {}
    for r in relations or []:
        nxt.setdefault(int(r.get("before", 0)), []).append(int(r.get("after", 0)))
    color: dict[int, int] = {}     # 0=未访问 1=在栈中 2=完成
    stack: list[int] = []

    def dfs(u: int) -> list[int] | None:
        color[u] = 1
        stack.append(u)
        for v in nxt.get(u, []):
            if color.get(v, 0) == 0:
                found = dfs(v)
                if found:
                    return found
            elif color.get(v) == 1:
                return stack[stack.index(v):] + [v]
        stack.pop()
        color[u] = 2
        return None

    for u in list(nxt):
        if color.get(u, 0) == 0:
            found = dfs(u)
            if found:
                return found
    return None


def compute_assembly(frags: dict, layout: dict, seams: list,
                     settings: dict) -> dict:
    """生成分阶段装配方案。

    frags: {fid: {code, contour, centroid_px, mm_per_px, thickness,
                  weight_g, area_mm2}}
    layout: {fid: {x,y,rot,flip,placed}}
    seams:  seam_geometry 列表（世界坐标，仅已接受接缝）
    settings: {seams:{cid:{open_min,cure_min,load_dir}},
               supports:[{id,x,y,kind,note}], zones:[{id,x,y,w,h,note}],
               relations:[{before,after,note}], order:[fid], locked:[int]}
    """
    layout = _norm_layout(layout)
    frags = {int(k): v for k, v in frags.items()}
    codes = {fid: f.get("code", str(fid)) for fid, f in frags.items()}

    # ---- 已上台碎片与重量
    placed_ids = [fid for fid in frags
                  if fid in layout and layout[fid].get("placed") is not False
                  and len(frags[fid].get("contour") or []) >= 3]
    weights, estimated = {}, {}
    for fid in placed_ids:
        w, est = _weight_of(frags[fid])
        weights[fid] = round(w, 1)
        estimated[fid] = est

    result = {
        "ok": False, "steps": [], "conflicts": [],
        "suggested_supports": [], "weights": weights, "estimated": estimated,
        "codes": codes, "seams": {s["id"]: s for s in seams},
        "total_wait_min": 0.0, "domain": [], "domain_source": "supports",
        "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not placed_ids:
        result["conflicts"].append({
            "type": "empty",
            "message": "画布上没有已摆放的碎片：请先在画布上布置复原方案。",
        })
        return result

    # ---- 世界轮廓与投影重心（碎片质心即布局锚点）
    world = {fid: world_contour(frags[fid], layout[fid]) for fid in placed_ids}
    cg = {fid: np.array([float(layout[fid].get("x", 0.0)),
                         float(layout[fid].get("y", 0.0))]) for fid in placed_ids}

    # ---- 先后关系（硬约束），先查环
    relations = settings.get("relations") or []
    cycle = _find_cycle(relations)
    if cycle:
        names = " → ".join(codes.get(u, str(u)) for u in cycle)
        result["conflicts"].append({
            "type": "relation_cycle", "fragments": cycle,
            "message": f"先后关系构成环：{names}，请删除其中一条关系。",
        })
        return result
    preds = _relation_preds(relations)

    # ---- 支撑域与禁入区
    supports = [{"id": int(s.get("id", i + 1)), "x": float(s["x"]),
                 "y": float(s["y"]), "kind": s.get("kind", "post"),
                 "note": str(s.get("note", ""))[:80]}
                for i, s in enumerate(settings.get("supports") or [])]
    zones = [{"id": int(z.get("id", i + 1)),
              "x0": min(float(z["x"]), float(z["x"]) + float(z.get("w", 0))),
              "y0": min(float(z["y"]), float(z["y"]) + float(z.get("h", 0))),
              "x1": max(float(z["x"]), float(z["x"]) + float(z.get("w", 0))),
              "y1": max(float(z["y"]), float(z["y"]) + float(z.get("h", 0))),
              "note": str(z.get("note", ""))[:80]}
             for i, z in enumerate(settings.get("zones") or [])]
    seam_cfg = {int(k): v for k, v in (settings.get("seams") or {}).items()}
    order = [int(v) for v in (settings.get("order") or [])]
    locked = {int(i) for i in (settings.get("locked") or [])}
    order_pos = {fid: i for i, fid in enumerate(order)}

    domain = _hull([[s["x"], s["y"]] for s in supports]) if supports \
        else np.zeros((0, 2))
    domain_source = "supports"

    # ---- 接缝邻接
    adj: dict[int, list] = {fid: [] for fid in placed_ids}
    for s in seams:
        a, b = s["frag_a"], s["frag_b"]
        if a in adj and b in adj:
            adj[a].append(s)
            adj[b].append(s)

    def supports_under(fid) -> list:
        poly = world[fid]
        return [s for s in supports if _point_in_poly((s["x"], s["y"]), poly)]

    def zone_of(pt) -> dict | None:
        for z in zones:
            if z["x0"] <= pt[0] <= z["x1"] and z["y0"] <= pt[1] <= z["y1"]:
                return z
        return None

    def combined_cg(ids) -> np.ndarray:
        wsum = sum(weights[i] for i in ids)
        return sum(cg[i] * weights[i] for i in ids) / max(wsum, 1e-9)

    def evaluate(fid, placed_set: set) -> tuple[bool, list, dict]:
        """评估 fid 能否在 placed_set 之后装入。返回 (可行, 阻塞原因, 步骤信息)。"""
        reasons: list[str] = []
        info: dict = {"frag": fid}
        if not placed_set:                       # 底件
            if len(domain):
                sd = _signed_dist_to_domain(cg[fid], domain)
                if sd > _domain_tol(domain) and not supports_under(fid):
                    reasons.append(
                        f"底件 {codes[fid]} 的投影重心在支撑域外 "
                        f"{sd:.0f} mm，请把托点移到其重心附近")
                    return False, reasons, info
            info.update(seams=[], contact_mm=0.0, cantilever=False,
                        supports=supports_under(fid), clamps=[],
                        cg=cg[fid], margin_mm=0.0)
            return True, reasons, info

        # 1) 先后关系
        missing = [p for p in preds.get(fid, set()) if p not in placed_set]
        if missing:
            names = "、".join(codes.get(p, str(p)) for p in missing)
            reasons.append(f"{codes[fid]} 须等 {names} 先装")
            return False, reasons, info

        # 2) 与已装部分的接缝
        used = [s for s in adj.get(fid, [])
                if (s["frag_a"] == fid and s["frag_b"] in placed_set)
                or (s["frag_b"] == fid and s["frag_a"] in placed_set)]
        if not used:
            reasons.append(f"{codes[fid]} 与已装部分之间没有已接受接缝")
            return False, reasons, info
        contact = sum(s["contact_mm"] for s in used)

        # 3) 夹具禁入区：夹具点（接缝中心）落入禁入区时须有临时支撑托住该件
        under = supports_under(fid)
        clamps = []
        for s in used:
            z = zone_of(s["center"])
            clamps.append({"seam": s["id"], "x": s["center"][0],
                           "y": s["center"][1], "forbidden": bool(z),
                           "zone": z["id"] if z else None})
        if any(c["forbidden"] for c in clamps) and not under:
            bad = "、".join(f"#{c['seam']}" for c in clamps if c["forbidden"])
            reasons.append(
                f"接缝 {bad} 的夹具位置落入夹具禁入区，且 {codes[fid]} "
                f"下方没有临时支撑，请在该件投影范围内加临时支撑")
            result["suggested_supports"].append(
                [round(float(cg[fid][0]), 1), round(float(cg[fid][1]), 1)])
            return False, reasons, info

        # 4) 整体重心须在支撑域内
        new_ids = list(placed_set) + [fid]
        new_cg = combined_cg(new_ids)
        sd_all = _signed_dist_to_domain(new_cg, domain) if len(domain) else 1e9
        if len(domain) and sd_all > _domain_tol(domain):
            reasons.append(
                f"装入 {codes[fid]} 后整体投影重心超出支撑域 "
                f"{sd_all:.0f} mm（重心在 ({new_cg[0]:.0f}, {new_cg[1]:.0f})），"
                f"请调整顺序或在该处附近加托点")
            result["suggested_supports"].append(
                [round(float(new_cg[0]), 1), round(float(new_cg[1]), 1)])
            return False, reasons, info

        # 5) 本件局部支撑：重心在支撑域内 / 接缝接触带可抓住 / 下方有托点
        sd_self = _signed_dist_to_domain(cg[fid], domain) if len(domain) else 1e9
        grip = min((_seg_dist(cg[fid], np.asarray(p0), np.asarray(p1))
                    for s in used for p0, p1 in
                    zip(s["path"][:-1], s["path"][1:])), default=1e9)
        cantilever = (sd_self > _domain_tol(domain)
                      and grip > SEAM_GRIP_MM and not under)
        if cantilever:
            # 悬臂：至少一条所用接缝的承载方向允许
            overhang_ok = False
            for s in used:
                cfg = seam_cfg.get(s["id"], {})
                ld = cfg.get("load_dir", "any")
                if ld == "any":
                    overhang_ok = True
                    break
                if ld == "none":
                    continue
                center = np.asarray(s["center"], dtype=np.float64)
                d = cg[fid] - center
                n = float(np.linalg.norm(d))
                if n < 1e-6:
                    overhang_ok = True
                    break
                d = d / n
                if ld == "along":
                    p0, p1 = np.asarray(s["path"][0]), np.asarray(s["path"][-1])
                    t = p1 - p0
                    tl = float(np.linalg.norm(t))
                    if tl > 1e-6 and abs(float(d @ (t / tl))) >= LOAD_DIR_DOT:
                        overhang_ok = True
                        break
                elif ld == "inward":
                    body = combined_cg(list(placed_set)) - center
                    bl = float(np.linalg.norm(body))
                    if bl > 1e-6 and float(d @ (body / bl)) >= LOAD_DIR_DOT:
                        overhang_ok = True
                        break
            if not overhang_ok:
                reasons.append(
                    f"{codes[fid]} 重心悬空（超出支撑域 {sd_self:.0f} mm），"
                    f"且所用接缝的承载方向均不允许（见接缝胶粘参数），"
                    f"请在该件下方加临时支撑或放宽承载方向")
                result["suggested_supports"].append(
                    [round(float(cg[fid][0]), 1), round(float(cg[fid][1]), 1)])
                return False, reasons, info

        info.update(seams=used, contact_mm=contact, cantilever=cantilever,
                    supports=under, clamps=clamps, cg=new_cg,
                    margin_mm=-sd_all if len(domain) else 0.0)
        return True, reasons, info

    # ---- 逐阶段贪心装配（锁定步骤优先，其次用户顺序，再次接触长度）
    placed_set: set = set()
    remaining = list(placed_ids)
    steps: list[dict] = []
    blocked: dict[int, list] = {}

    while remaining:
        idx = len(steps)
        chosen, info = None, None
        if idx in locked and idx < len(order) and order[idx] in remaining:
            ok, reasons, info = evaluate(order[idx], placed_set)
            if not ok:
                result["steps"] = steps
                result["conflicts"].append({
                    "type": "locked_infeasible", "fragment": order[idx],
                    "message": f"锁定的第 {idx + 1} 步（{codes.get(order[idx])}）"
                               f"不可行：{'；'.join(reasons)}",
                })
                return _finish(result, steps, seam_cfg, supports, domain,
                               domain_source, weights, estimated, codes)
            chosen = order[idx]
        else:
            feasible = []
            blocked = {}
            for fid in remaining:
                ok, reasons, inf = evaluate(fid, placed_set)
                if ok:
                    feasible.append((fid, inf))
                else:
                    blocked[fid] = reasons
            if not feasible:
                break
            feasible.sort(key=lambda fi: (
                order_pos.get(fi[0], len(order) + 1),
                -fi[1].get("contact_mm", 0.0)))
            chosen, info = feasible[0]
        # 底件选定后：未标托点时以底件外轮廓为支撑域
        if not placed_set and not len(domain):
            domain = _hull(world[chosen].tolist())
            domain_source = "base"
            ok, reasons, info = evaluate(chosen, placed_set)
            if not ok:      # 理论上不会发生（底件重心必在自身轮廓内）
                blocked = {chosen: reasons}
                break
        placed_set.add(chosen)
        remaining.remove(chosen)
        steps.append(_make_step(idx, chosen, info, seam_cfg, codes))

    if remaining:
        for fid in remaining:
            for msg in blocked.get(fid, [f"{codes[fid]} 暂不可装"]):
                result["conflicts"].append({
                    "type": "blocked", "fragment": fid, "message": msg})
        result["steps"] = steps
        return _finish(result, steps, seam_cfg, supports, domain,
                       domain_source, weights, estimated, codes)

    result["ok"] = True
    result["steps"] = steps
    return _finish(result, steps, seam_cfg, supports, domain,
                   domain_source, weights, estimated, codes)


def _make_step(idx: int, fid: int, info: dict, seam_cfg: dict,
               codes: dict) -> dict:
    seams = info.get("seams") or []
    return {
        "idx": idx, "frag": fid, "code": codes.get(fid, str(fid)),
        "seam_ids": [s["id"] for s in seams],
        "contact_mm": round(info.get("contact_mm", 0.0), 1),
        "supports": [s["id"] for s in info.get("supports") or []],
        "clamps": info.get("clamps") or [],
        "cantilever": bool(info.get("cantilever")),
        "cg": [round(float(info["cg"][0]), 1), round(float(info["cg"][1]), 1)]
              if info.get("cg") is not None else None,
        "margin_mm": round(float(info.get("margin_mm", 0.0)), 1),
        "open_min": max((float(seam_cfg.get(s["id"], {}).get(
            "open_min", DEFAULT_OPEN_MIN)) for s in seams), default=0.0),
        "cure_min": max((float(seam_cfg.get(s["id"], {}).get(
            "cure_min", DEFAULT_CURE_MIN)) for s in seams), default=0.0),
        "wait_min": 0.0,      # 由 _finish 按后续依赖回填
        "risks": [],
    }


def _finish(result: dict, steps: list, seam_cfg: dict, supports: list,
            domain: np.ndarray, domain_source: str, weights: dict,
            estimated: dict, codes: dict) -> dict:
    """回填等待时间、风险提示、建议托点去重与汇总信息。"""
    # 后续依赖：有更晚的碎片接到本件上时，本步接缝须等固化
    frag_step = {st["frag"]: st["idx"] for st in steps}
    seam_map = result["seams"]
    for st in steps:
        later_load = False
        for s2 in seam_map.values():
            if st["frag"] not in (s2["frag_a"], s2["frag_b"]):
                continue
            other = s2["frag_b"] if s2["frag_a"] == st["frag"] else s2["frag_a"]
            if frag_step.get(other, -1) > st["idx"]:
                later_load = True
                break
        if later_load or st["cantilever"]:
            st["wait_min"] = st["cure_min"]
        else:
            st["wait_min"] = 0.0
        # 风险提示
        if st["cantilever"]:
            st["risks"].append("悬臂装配：依赖接缝承载，固化前勿加载、勿撤支撑")
        if st["contact_mm"] and st["contact_mm"] < MIN_CONTACT_MM:
            st["risks"].append(
                f"有效接触长度仅 {st['contact_mm']:.0f} mm，定位时需反复核对")
        if estimated.get(st["frag"]):
            st["risks"].append("重量为按面积×厚度估算，建议补录实测重量")
        if st["clamps"] and any(c["forbidden"] for c in st["clamps"]):
            st["risks"].append("夹具位置落入禁入区，已改用临时支撑固定")
        if st["idx"] > 0 and 0 < st["margin_mm"] < 5:
            st["risks"].append(
                f"稳定裕度仅 {st['margin_mm']:.0f} mm，操作时避免碰撞已装部分")
        if st["open_min"]:
            st["risks"].append(
                f"涂胶后须在开放时间 {st['open_min']:.0f} 分钟内完成定位")

    total_wait = sum(st["wait_min"] for st in steps)
    result["total_wait_min"] = round(total_wait, 1)
    result["total_steps"] = len(steps)
    result["domain"] = np.round(domain, 1).tolist() if len(domain) else []
    result["domain_source"] = domain_source
    result["supports"] = supports
    # 建议托点去重（相距 10mm 以内合并）
    sugg = []
    for p in result["suggested_supports"]:
        if not any(math.hypot(p[0] - q[0], p[1] - q[1]) < 10 for q in sugg):
            sugg.append(p)
    result["suggested_supports"] = sugg
    est_n = sum(1 for f in estimated.values() if f)
    result["notes"] = ([f"{est_n} 件碎片重量为估算值（面积×厚度×2.4g/cm³），"
                        f"建议补录实测重量"] if est_n else [])
    if domain_source == "base":
        result["notes"].append("未标注托点：按底件外轮廓为支撑域（碎片平放台面）")
    return result
