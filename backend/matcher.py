"""候选拼接分析（全部在本地 NumPy 完成）。

打分依据（0~100）：
  * 曲率一致性  curvature：等弧长轮廓在滑动窗口内的归一化互相关；
  * 几何贴合    fit：Kabsch 刚性配准后接缝间距 RMSE（mm）；
  * 厚度相容    thickness：两件厚度记录之差（mm）；
  * 邻接色带    color：接缝对应点内侧 Lab 色差（ΔE）。
"""
from __future__ import annotations

import math

import cv2
import numpy as np

N = 400                 # 与 vision.CONTOUR_POINTS 保持一致
WIN = 46                # 接缝搜索窗口（弧长点数）
MAX_TOP = 12            # 进入刚性配准的候选数
MIN_SCORE = 45.0
MIN_RUN = 10            # 最短有效接缝（点数，约周长 2.5%）


def contact_stats(poly_a: np.ndarray, poly_b_mapped: np.ndarray,
                  gap_mm: float = 2.0) -> tuple[float, float, float]:
    """接触弧几何统计（用于反对向/重叠旁证）。

    返回 (contact_mm, opposition, wiggle_deg)。
    """
    n = len(poly_b_mapped)
    d = np.linalg.norm(poly_b_mapped[:, None, :] - poly_a[None, :, :], axis=2)
    near = d.min(axis=1) < gap_mm
    seg = np.linalg.norm(np.roll(poly_b_mapped, -1, axis=0) - poly_b_mapped, axis=1)
    best_run, cur = 0.0, 0.0
    best_idx: list[int] = []
    cur_idx: list[int] = []
    for k, i in enumerate(list(range(n)) * 2):
        if near[i]:
            cur += seg[i]
            cur_idx.append(i)
            if cur > best_run:
                best_run, best_idx = cur, list(cur_idx)
        else:
            cur, cur_idx = 0.0, []
            if best_run > 0 and k >= n:
                break
    tb = poly_b_mapped[(np.arange(n) + 2) % n] - \
        poly_b_mapped[(np.arange(n) - 2) % n]
    nb = np.linalg.norm(tb, axis=1) + 1e-9
    if len(best_idx) >= 4:
        ang = np.arctan2(tb[:, 1], tb[:, 0])
        da = np.diff(np.unwrap(ang[np.array(best_idx) % n]))
        wiggle = float(np.degrees(np.abs(da).sum()))
    else:
        wiggle = 0.0
    ta = poly_a[(np.arange(len(poly_a)) + 2) % len(poly_a)] - \
        poly_a[(np.arange(len(poly_a)) - 2) % len(poly_a)]
    na = np.linalg.norm(ta, axis=1) + 1e-9
    nearest = d.argmin(axis=1)
    if near.any():
        idx_b = np.where(near)[0]
        dot = (ta[nearest[idx_b]] / na[nearest[idx_b], None] *
               tb[idx_b] / nb[idx_b, None]).sum(axis=1)
        opposition = float(np.median(dot))
    else:
        opposition = 1.0
    return best_run, opposition, wiggle


def seam_verify(ca: np.ndarray, cb: np.ndarray, ia: np.ndarray,
                ib: np.ndarray, R: np.ndarray, seam_center: np.ndarray,
                max_extend: int = 150, gap_mm: float = 3.0) -> dict:
    """以候选 (ia,ib) 为种子，沿参数方向有序延伸接缝并验证。

    步骤：
      1. 种子变换下向两侧延伸（a +1 / b -1），收集间距 < gap_mm 的点对；
      2. 在全部接触点对上重新做一次刚性拟合，得到整段接缝 RMSE；
      3. 用重拟合后的映射再核对平均间距与反对向。
    返回接触长度、重拟合 RMSE、平均间隙、接触弧累计转角。
    """
    n = len(ca)
    center_b0 = cb[ib].mean(axis=0)

    def map_b(idx, Rr, cc):
        return (cb[idx] - cc) @ Rr.T + seam_center

    seed = ia[len(ia) // 2]
    seed_b = ib[len(ib) // 2]
    chains = []
    for da_, db_ in ((1, -1), (-1, 1)):
        chain = []
        for step in range(1, max_extend + 1):
            ai = (seed + da_ * step) % n
            bi = (seed_b + db_ * step) % n
            if np.linalg.norm(ca[ai] - map_b(bi, R, center_b0)) > gap_mm:
                break
            chain.append((ai, bi))
        chains.append(chain)
    ordered = chains[1][::-1] + chains[0]
    # 把种子窗口内的对应也纳入
    seed_pairs = list(zip(ia.tolist(), ib.tolist()))
    all_pairs = ordered + seed_pairs
    if len(ordered) < 6 or len(all_pairs) < 12:
        return {"contact_mm": 0.0, "seam_rmse": 99.0, "gap": 99.0,
                "wiggle": 0.0, "R2": R, "center_b": center_b0}
    Aidx = np.array([p[0] for p in all_pairs])
    Bidx = np.array([p[1] for p in all_pairs])
    # 整段重拟合
    refit = fit_transform(ca[Aidx], cb[Bidx])
    R2 = refit["R"]
    center_b2 = cb[Bidx].mean(axis=0)
    mapped_b = map_b(Bidx, R2, center_b2)
    gap = float(np.linalg.norm(ca[Aidx] - mapped_b, axis=1).mean())
    seg = np.linalg.norm(np.roll(mapped_b, -1, axis=0) - mapped_b, axis=1)
    contact = float(seg[:-1].sum())
    tang = np.arctan2(np.gradient(mapped_b[:, 1]), np.gradient(mapped_b[:, 0]))
    wiggle = float(np.degrees(np.abs(np.diff(np.unwrap(tang))).sum()))
    return {"contact_mm": contact, "seam_rmse": refit["rmse"],
            "gap": gap, "wiggle": wiggle, "R2": R2, "center_b": center_b2,
            "Aidx": Aidx, "Bidx": Bidx, "mapped_b": mapped_b}


def overlap_ratio(poly_a: np.ndarray, poly_b_mapped: np.ndarray) -> float:
    """poly_b 经变换后与 poly_a 内部重叠面积占较小件面积的比例（0~1）。

    用凸包交集快速估算（断裂碎片接近凸形，足以区分“贴合”与“穿插”）。
    """
    ha = cv2.convexHull(poly_a.astype(np.float32))
    hb = cv2.convexHull(poly_b_mapped.astype(np.float32))
    ra, rb = abs(float(cv2.contourArea(ha))), abs(float(cv2.contourArea(hb)))
    try:
        ri, _ = cv2.intersectConvexConvex(ha, hb)
    except cv2.error:
        return 1.0
    return float(ri) / max(min(ra, rb), 1e-9)
WIN = 46                # 接缝搜索窗口（弧长点数）
MAX_TOP = 12            # 进入刚性配准的候选数
MIN_SCORE = 45.0
MIN_RUN = 10            # 最短有效接缝（点数，约周长 2.5%）


# ---------------------------------------------------------------- 特征

def _arc_gaussian(x: np.ndarray, sigma_mm: float, step_mm: float) -> np.ndarray:
    """沿闭合弧长对逐点信号做高斯平滑（sigma 以毫米计）。"""
    radius = max(2, int(round(3 * sigma_mm / step_mm)))
    t = np.arange(-radius, radius + 1) * step_mm
    k = np.exp(-(t ** 2) / (2 * sigma_mm ** 2))
    k /= k.sum()
    mid = np.convolve(np.concatenate([x[-radius:], x, x[:radius]]),
                      k, mode="valid")
    return mid


def derive(curves_mm: np.ndarray, smooth_mm: float = 2.5,
           half: int = 4) -> dict:
    """curves_mm: (N,2) 毫米坐标轮廓（图像系 y 向下）。

    先沿弧长以 smooth_mm 高斯平滑坐标，再用 ±half 点的有符号转角
    sinθ = cross(d1,d2)/(|d1||d2|) 作为曲率描述（对像素噪声稳健）。
    """
    n = len(curves_mm)
    seg = np.linalg.norm(np.roll(curves_mm, -1, axis=0) - curves_mm, axis=1)
    step = float(np.median(seg)) + 1e-9
    sx = _arc_gaussian(curves_mm[:, 0], smooth_mm, step)
    sy = _arc_gaussian(curves_mm[:, 1], smooth_mm, step)
    c = np.stack([sx, sy], axis=1)
    i = np.arange(n)
    p1, p0 = c[(i - half) % n], c[(i - 2 * half) % n]
    n1, n2 = c[(i + half) % n], c[(i + 2 * half) % n]
    d1 = p1 - p0
    d2 = n2 - n1
    cross = d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]
    denom = np.linalg.norm(d1, axis=1) * np.linalg.norm(d2, axis=1) + 1e-9
    kappa = np.clip(cross / denom, -1, 1)
    return {"curve": curves_mm, "kappa": kappa}


def _ncc_peak(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """循环互相关（FFT）。返回每个偏移 s 的 NCC 及对应相关序列。"""
    a0 = a - a.mean()
    b0 = b - b.mean()
    denom = math.sqrt(float((a0 ** 2).sum() * (b0 ** 2).sum())) + 1e-12
    full = np.fft.ifft(np.fft.fft(a0) * np.conj(np.fft.fft(b0))).real / denom
    return full


def _window_color_de(color_a: np.ndarray, color_b: np.ndarray,
                     win: int) -> np.ndarray:
    """所有 (位移 s, 起点 sa) 窗口内平均 Lab 距离矩阵 DE[s, sa]。

    位移 s 下 a[i] 对应 b[(i+s)%n]，取色差矩阵循环对角线后做滑窗均值。
    """
    n = len(color_a)
    dc = np.linalg.norm(color_a[:, None, :] - color_b[None, :, :], axis=2)
    i = np.arange(n)
    G = dc[i[None, :], (i[None, :] + i[:, None]) % n]   # G[s, i]
    ext = np.concatenate([G, G[:, :win - 1]], axis=1)
    csum = np.concatenate([np.zeros((n, 1)), np.cumsum(ext, axis=1)], axis=1)
    starts = np.arange(n)
    return (csum[:, starts + win] - csum[:, starts]) / win


def _window_ncc_matrix(ka: np.ndarray, kb: np.ndarray, win: int,
                       energy_floor: float = 0.03) -> np.ndarray:
    """返回 M[s, sa]：b 相对 a 偏移 s、窗口起点 sa 时的窗口 NCC。

    全部用 FFT 循环互相关向量化：对每个位移 s，一次得到所有起点的
    窗口求和，无 Python 层逐行循环。
    """
    n = len(ka)

    def win_sum(x):                      # 循环滑窗和（含平方/乘积）
        e = np.concatenate([x, x[: win - 1]])
        c = np.concatenate([[0], np.cumsum(e)])
        starts = np.arange(n)
        return c[starts + win] - c[starts]

    ones = np.ones(n)
    wcnt = win_sum(ones)                  # 边界点权重（≈win）
    sa_sum = win_sum(ka)
    sa2 = win_sum(ka * ka)
    ea = win_sum(np.abs(ka)) / wcnt
    # G[l, s] = a[l] * b[(l+s) mod n]；窗口乘积和沿 l 轴滑窗即可
    l_idx = np.arange(n)
    G = ka[l_idx, None] * kb[(l_idx[:, None] + l_idx[None, :]) % n]
    ext = np.concatenate([G, G[: win - 1, :]], axis=0)
    cc = np.concatenate([np.zeros((1, n)), np.cumsum(ext, axis=0)], axis=0)
    starts = np.arange(n)
    sp = cc[starts + win, :] - cc[starts, :]          # sp[i, s]
    # 各起点/位移的窗口和与平方和
    SBa = sa_sum[:, None]
    SA2 = sa2[:, None]
    # b 窗口和（与 a 起点无关，只随 s 变）：b 起点 i+s 的窗和
    sb_full = win_sum(kb)
    sb2_full = win_sum(kb * kb)
    e_full = win_sum(np.abs(kb)) / wcnt
    i_idx, s_idx = np.meshgrid(starts, starts, indexing="ij")
    b_start = (i_idx + s_idx) % n
    sb_sum = sb_full[b_start]
    sb2v = sb2_full[b_start]
    eb = e_full[b_start]
    num = sp - SBa * sb_sum / win
    va = SA2 - SBa ** 2 / win
    vb = sb2v - sb_sum ** 2 / win
    den = np.sqrt(np.maximum(va, 1e-9) * np.maximum(vb, 1e-9))
    M = num / den
    M[(ea[:, None] < energy_floor) | (eb < energy_floor)] = -1
    return M.T   # M[s, sa]


def _energy_windows(kappa: np.ndarray, win: int,
                    floor: float = 0.05) -> np.ndarray:
    """曲率能量（窗口平均 |κ|）≥ floor 的起点掩码。"""
    n = len(kappa)
    e = np.concatenate([np.abs(kappa), np.abs(kappa)[:win - 1]])
    c = np.concatenate([[0], np.cumsum(e)])
    starts = np.arange(n)
    energy = (c[starts + win] - c[starts]) / win
    return energy >= floor


def _best_windows(kappa_a: np.ndarray, kappa_b: np.ndarray,
                  color_a: np.ndarray | None, color_b: np.ndarray | None,
                  wins: tuple = (30, 46, 70)) -> list[dict]:
    """在曲率能量非零的窗口上枚举对应，最终交给刚性配准 RMSE 裁决。

    全等直边会使曲率 NCC 处处为 1，因此这里不按 NCC 取 top，
    而是收集若干非平直窗口候选，调用方再用 RMSE 选出真正贴合的接缝。
    """
    n = len(kappa_a)
    raws: list[dict] = []
    seen: set = set()
    for win in wins:
        ea = _energy_windows(kappa_a, win)
        eb = _energy_windows(kappa_b, win)
        if not ea.any() or not eb.any():
            continue
        for sign in (1, -1):
            kb = kappa_b[::sign]
            M = _window_ncc_matrix(kappa_a, kb, win, energy_floor=0.0)
            # 只保留双方都有曲率内容的窗口
            valid = ea[:, None] & eb[None, :]
            M = np.where(valid, M, -1.0)
            order = np.argsort(M.ravel())[::-1]
            kept = 0
            for idx in order:
                s, start = divmod(int(idx), n)
                val = float(M[s, start])
                if val < 0.80 or kept >= 8:
                    break
                ia = (np.arange(start, start + win)) % n
                ib_shifted = (np.arange(start, start + win) + s) % n
                mid_a = (start + win // 2) % n
                mid_b = (start + s + win // 2) % n
                key = (mid_a // 12, mid_b // 12, sign)
                if key in seen:
                    continue
                seen.add(key)
                kept += 1
                if color_a is not None:
                    DE = _window_color_de(color_a, color_b[::sign], win)
                    de = float(DE[s, start])
                else:
                    de = 99.0
                ib_orig = ib_shifted if sign == 1 else (-ib_shifted) % n
                raws.append({"ncc": round(val, 3), "de": round(de, 2),
                             "ia": ia.tolist(), "ib": ib_orig.tolist(),
                             "run": win})
    # 粗排：颜色一致优先，但真正的取舍在 analyze_pair 的 RMSE
    raws.sort(key=lambda r: (-r["ncc"], r["de"]))
    return raws[:14]


# ---------------------------------------------------------------- 配准

def fit_transform(pa: np.ndarray, pb: np.ndarray,
                  allow_reflect: bool = False) -> dict:
    """求 Pb -> Pa 的刚体变换（旋转 + 平移），返回 RMSE（mm）。"""
    ca, cb = pa.mean(axis=0), pb.mean(axis=0)
    A, B = pa - ca, pb - cb
    H = B.T @ A
    U, S, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T
    reflected = False
    if np.linalg.det(R) < 0:
        if allow_reflect:
            Vt[-1] *= -1
            R = Vt.T @ U.T
            reflected = True
        else:
            Vt[-1] *= -1
            R = Vt.T @ U.T
    # RMSE 只衡量“去质心后”的形状误差；平移由接缝在世界坐标的位置决定
    mapped0 = B @ R.T
    rmse = float(np.sqrt(((mapped0 - A) ** 2).sum(axis=1).mean()))
    mapped = mapped0 + ca
    return {"R": R, "t": ca - cb @ R.T, "rmse": rmse,
            "reflected": reflected, "mapped": mapped}


# ---------------------------------------------------------------- 主入口

def analyze_pair(fa: dict, fb: dict) -> dict | None:
    """fa/fb：fragment 行解析后的特征字典。

    必含 mm 轮廓 curve、曲率 kappa、edge_lab(N,3)、厚度 thickness、尺寸信息。
    """
    ca = np.array(fa["curve_mm"], dtype=np.float64)
    cb = np.array(fb["curve_mm"], dtype=np.float64)
    ka, kb = derive(ca), derive(cb)
    el_a = fa.get("edge_lab")
    el_b = fb.get("edge_lab")
    has_color = bool(el_a is not None and el_b is not None and
                     len(el_a) == N and len(el_b) == N)
    color_a = np.array(el_a if has_color else np.full((N, 3), np.nan),
                       dtype=np.float64)
    color_b = np.array(el_b if has_color else np.full((N, 3), np.nan),
                       dtype=np.float64)

    windows = _best_windows(ka["kappa"], kb["kappa"],
                            color_a if has_color else None,
                            color_b if has_color else None)
    if not windows:
        return None

    best = None
    for w in windows:
        ia = np.array(w["ia"])
        ib_orig = np.array(w["ib"])
        fit = fit_transform(ca[ia], cb[ib_orig])
        if fit["rmse"] > 8.0:          # mm，明显对不上
            continue
        # 接缝窗口质心在全局毫米系中直接对齐，得到 B->A 的世界变换
        seam_center = ca[ia].mean(axis=0)
        t_world = seam_center - cb[ib_orig].mean(axis=0) @ fit["R"].T
        # 有序参数对应上延伸验证真实接缝（偶然贴合的短弧通不过）
        sv = seam_verify(ca, cb, ia, ib_orig, fit["R"], seam_center)
        # 整段接缝重拟合：接触长度、形状 RMSE、平均间隙
        sv = seam_verify(ca, cb, ia, ib_orig, fit["R"], seam_center)
        if (sv["contact_mm"] < 15.0 or sv["seam_rmse"] > 3.0
                or sv["gap"] > 2.0 or sv["wiggle"] < 25.0):
            continue
        R2 = sv["R2"]
        center_b2 = sv["center_b"]
        t_world = seam_center - center_b2 @ R2.T
        # 整件重叠只作旁证（seam_verify 已确认接触弧）
        cb_mapped = (cb - center_b2) @ R2.T + seam_center
        ov = overlap_ratio(ca, cb_mapped)
        if ov > 0.30:
            continue
        contact_mm, opposition, wiggle = contact_stats(ca, cb_mapped, gap_mm=2.5)
        de = w["de"]
        color_score = max(0.0, min(1.0, 1 - de / 25.0)) if has_color else 0.5
        thick_a = float(fa.get("thickness") or 0)
        thick_b = float(fb.get("thickness") or 0)
        if thick_a and thick_b:
            dt = abs(thick_a - thick_b)
            thick_score = max(0.0, 1 - dt / 3.0)
        else:
            thick_score, dt = 0.5, -1.0
        window_fit = max(0.0, 1 - fit["rmse"] / 4.0)
        seam_fit = max(0.0, 1 - sv["seam_rmse"] / 3.0)
        overlap_score = max(0.0, 1 - ov / 0.25)
        opposition_score = max(0.0, (-opposition + 1) / 2)
        contact_score = min(1.0, contact_mm / 50.0)
        score = (18 * w["ncc"] + 14 * window_fit +
                 24 * seam_fit + 12 * color_score + 6 * thick_score +
                 6 * overlap_score + 6 * opposition_score +
                 14 * contact_score)
        cand = {
            "score": round(float(score), 1),
            "ncc": w["ncc"], "rmse": round(fit["rmse"], 2),
            "seam_rmse": round(sv["seam_rmse"], 2),
            "de": de if has_color else None,
            "thick_diff": round(dt, 2) if dt >= 0 else None,
            "overlap": round(ov, 3),
            "contact_mm": round(sv["contact_mm"], 1),
            "opposition": round(opposition, 2),
            "wiggle_deg": round(wiggle, 1),
            "seam_gap_mm": round(sv["gap"], 2),
            "ia": sv["Aidx"].tolist(), "ib": sv["Bidx"].tolist(),
            "R": R2.tolist(),
            "t": t_world.tolist(),
            "seam_center_a": seam_center.tolist(),
            "seam_center_b": center_b2.tolist(),
            "reflected": fit["reflected"],
            "run_points": len(sv["Aidx"]),
        }
        if best is None or cand["score"] > best["score"]:
            best = cand
    if best is None or best["score"] < MIN_SCORE:
        return None
    return best


def build_reasons(c: dict, fa: dict, fb: dict) -> list[str]:
    reasons = [
        f"边缘曲率相关系数 {c['ncc']:.2f}（接缝弧长约 "
        f"{c['run_points'] / N * 100:.0f}% 周长）",
        f"接缝刚性配准偏差 RMSE {c.get('seam_rmse', c['rmse'])} mm，"
        f"接触长度约 {c.get('contact_mm', 0)} mm",
    ]
    if c["de"] is not None:
        reasons.append(f"邻接色带平均色差 ΔE {c['de']:.1f}（<15 较接近）")
    else:
        reasons.append("色带数据不足，未计入颜色依据")
    if c["thick_diff"] is not None:
        reasons.append(f"厚度差 {c['thick_diff']:.2f} mm"
                       f"（{fa.get('thickness')} / {fb.get('thickness')} mm）")
    else:
        reasons.append("厚度记录不完整，按中性分处理")
    if c["reflected"]:
        reasons.append("配准需要翻面（正反面），请人工核对")
    return reasons


# ================================================================
# 人工复核：按用户锚点与排除区段重新计算接缝指标
# ================================================================

REVIEW_U_STEPS = 160          # 复核条带上的对应采样数


def _signed_span(i0: int, i1: int, n: int) -> int:
    """闭合轮廓上从 i0 到 i1 的最短有向步长（±n/2 以内）。"""
    d = (i1 - i0) % n
    return d - n if d > n / 2 else d


def review_adjacency(params: dict, n_a: int, n_b: int) -> dict:
    """由候选 params 推出初始接缝对应关系。

    返回 {dir_b:±1, anchors:[{u, ia, ib}]}（u∈[0,1]，条带横坐标）。
    """
    ia0, ib0 = int(params["ia"][0]), int(params["ib"][0])
    ia1, ib1 = int(params["ia"][-1]), int(params["ib"][-1])
    da = _signed_span(ia0, ia1, n_a)
    db = _signed_span(ib0, ib1, n_b)
    dir_b = 1 if da * db >= 0 else -1
    span_a = abs(da)
    return {
        "dir_b": dir_b,
        "span_a": span_a,
        "anchors": [
            {"u": 0.0, "ia": ia0, "ib": ib0},
            {"u": 1.0, "ia": ia1 % n_a, "ib": ib1 % n_b},
        ],
    }


def _anchor_maps(anchors: list[dict], n_a: int, n_b: int,
                 dir_b: int, span_a: int):
    """按 u=0..1 返回每个采样点的 (ia 浮点, ib 浮点)。

    端点之间以“展开的轮廓参数”线性插值（跨过 0 点也连续）。
    """
    u = np.linspace(0.0, 1.0, REVIEW_U_STEPS)
    anchors = sorted(anchors, key=lambda a: a["u"])
    ia_unw = np.empty(REVIEW_U_STEPS)
    ib_unw = np.empty(REVIEW_U_STEPS)
    for k in range(len(anchors) - 1):
        a0, a1 = anchors[k], anchors[k + 1]
        m = (u >= a0["u"]) & (u <= a1["u"])
        t = np.clip((u[m] - a0["u"]) / max(a1["u"] - a0["u"], 1e-9), 0, 1)
        ia_unw[m] = a0["ia"] + _signed_span(a0["ia"], a1["ia"], n_a) * t
        ib_unw[m] = a0["ib"] + _signed_span(a0["ib"], a1["ib"], n_b) * t
    return u, ia_unw, ib_unw


def _point_at(ca: np.ndarray, idx_float: np.ndarray) -> np.ndarray:
    """闭合折线上按浮点下标线性取点。"""
    n = len(ca)
    i0 = np.floor(idx_float).astype(int)
    t = (idx_float - i0)[:, None]
    return ca[i0 % n] * (1 - t) + ca[(i0 + 1) % n] * t


def _excluded_mask(u: np.ndarray, zones: list[dict]) -> np.ndarray:
    out = np.zeros(len(u), dtype=bool)
    for z in zones or []:
        z0, z1 = float(z.get("u0", 0)), float(z.get("u1", 0))
        lo, hi = min(z0, z1), max(z0, z1)
        out |= (u >= lo) & (u <= hi)
    return out


def review_recompute(fa: dict, fb: dict, params: dict,
                     adjustments: dict) -> dict | None:
    """按人工调整重算接缝。

    adjustments: {anchors:[{u,ia,ib}], zones:[{u0,u1,kind,note}], dir_b?}
    返回有效接触长度、配准偏差、色差、曲率相关、综合分与修正后的 B→A 变换。
    """
    ca = np.asarray(fa["curve_mm"], dtype=np.float64)
    cb = np.asarray(fb["curve_mm"], dtype=np.float64)
    n_a, n_b = len(ca), len(cb)
    base = review_adjacency(params, n_a, n_b)
    anchors = adjustments.get("anchors") or base["anchors"]
    dir_b = int(adjustments.get("dir_b", base["dir_b"]))
    span_a = base["span_a"]

    u, iaf, ibf = _anchor_maps(anchors, n_a, n_b, dir_b, span_a)
    pa = _point_at(ca, iaf)
    pb = _point_at(cb, ibf)

    excluded = _excluded_mask(u, adjustments.get("zones"))
    valid = ~excluded
    # 排除区段过短时也保证至少可拟合
    if valid.sum() < 6:
        return {"ok": False, "message": "有效接触点过少（至少保留约 3% 接缝）",
                "valid_ratio": round(float(valid.mean()), 3)}

    fit = fit_transform(pa[valid], pb[valid])
    R, t = fit["R"], fit["t"]
    pb_map = pb @ R.T + t

    gaps = np.linalg.norm(pa - pb_map, axis=1)
    contact_gap = 2.0
    near = valid & (gaps < contact_gap)
    seg_a = np.linalg.norm(np.roll(pa, -1, axis=0) - pa, axis=1)
    contact_mm = float(seg_a[near].sum())
    valid_mm = float(seg_a[valid].sum())
    rmse_valid = float(np.sqrt((gaps[valid] ** 2).mean()))
    gap_valid = float(gaps[valid].mean())

    # Lab 色差（按最近轮廓点取带内采样）
    el_a, el_b = fa.get("edge_lab"), fb.get("edge_lab")
    if el_a is not None and el_b is not None and len(el_a) == n_a and len(el_b) == n_b:
        la = np.asarray(el_a)[np.rint(iaf).astype(int) % n_a]
        lb = np.asarray(el_b)[np.rint(ibf).astype(int) % n_b]
        de_all = np.linalg.norm(la - lb, axis=1)
        de = float(de_all[valid].mean())
    else:
        de = None

    # 曲率相关（有效点上的 NCC；ib 反向时取反向轮廓的曲率）
    ka, kb = derive(ca)["kappa"], derive(cb)["kappa"]
    ia_near = np.rint(iaf).astype(int) % n_a
    ib_near = np.rint(ibf).astype(int) % n_b
    va = ka[ia_near][valid]
    vb = (kb[ib_near] if dir_b == 1 else kb[(-ib_near) % n_b])[valid]
    a0, b0 = va - va.mean(), vb - vb.mean()
    denom = math.sqrt(float((a0 ** 2).sum() * (b0 ** 2).sum())) + 1e-9
    ncc = float(np.dot(a0, b0) / denom)

    # 整件重叠 / 反对向（沿用自动分析的旁证）
    # B→A 世界变换：q = p @ R.T + t
    cb_mapped = cb @ R.T + t
    ov = overlap_ratio(ca, cb_mapped)
    _, opposition, _ = contact_stats(ca, cb_mapped, gap_mm=2.5)

    thick_a, thick_b = float(fa.get("thickness") or 0), float(fb.get("thickness") or 0)
    if thick_a and thick_b:
        dt = abs(thick_a - thick_b)
        thick_score = max(0.0, 1 - dt / 3.0)
    else:
        thick_score, dt = 0.5, -1.0

    color_score = max(0.0, min(1.0, 1 - de / 25.0)) if de is not None else 0.5
    seam_fit = max(0.0, 1 - rmse_valid / 3.0)
    overlap_score = max(0.0, 1 - ov / 0.25)
    opposition_score = max(0.0, (-opposition + 1) / 2)
    contact_score = min(1.0, contact_mm / 50.0)
    ncc_score = max(0.0, (ncc + 1) / 2) if np.isfinite(ncc) else 0.5
    # 有效区段占接缝的比例（异常区段越多越扣分）
    valid_ratio = float(valid.mean())
    score = (24 * seam_fit + 14 * contact_score + 16 * ncc_score +
             16 * color_score + 8 * thick_score + 8 * overlap_score +
             6 * opposition_score + 8 * valid_ratio)

    ia_idx = ia_near
    ib_idx = ib_near
    if el_a is not None and el_b is not None:
        la_all = np.asarray(el_a)[ia_idx]
        lb_all = np.asarray(el_b)[ib_idx]
    else:
        la_all = lb_all = None
    zone_de = []
    for z in adjustments.get("zones") or []:
        m = _excluded_mask(u, [z])
        zone_de.append(round(float(np.linalg.norm(
            la_all[m] - lb_all[m], axis=1).mean()), 1)
            if la_all is not None and m.any() else None)

    return {
        "ok": True,
        "score": round(float(score), 1),
        "contact_mm": round(contact_mm, 1),
        "valid_mm": round(valid_mm, 1),
        "valid_ratio": round(valid_ratio, 3),
        "rmse": round(rmse_valid, 2),
        "gap_mm": round(gap_valid, 2),
        "de": round(de, 2) if de is not None else None,
        "ncc": round(ncc, 3),
        "overlap": round(ov, 3),
        "opposition": round(opposition, 2),
        "thick_diff": round(dt, 2) if dt >= 0 else None,
        "excluded_mm": round(float(seg_a[excluded].sum()), 1),
        "R": R.tolist(), "t": t.tolist(),
        "ia": ia_idx[valid].tolist(), "ib": ib_idx[valid].tolist(),
        "ia_all": ia_idx.tolist(), "ib_all": ib_idx.tolist(),
        "excluded": excluded.tolist(),
        "seam_center_a": pa[valid].mean(axis=0).round(2).tolist(),
        "seam_center_b": (pb[valid].mean(axis=0)).round(2).tolist(),
        "reflected": bool(fit["reflected"]),
        "zone_count": int(len(adjustments.get("zones") or [])),
        "zone_de": zone_de,
    }
