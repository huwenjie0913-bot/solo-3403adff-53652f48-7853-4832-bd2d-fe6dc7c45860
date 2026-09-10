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
                max_extend: int = 130, gap_mm: float = 2.5) -> dict:
    """以候选 (ia,ib) 为种子，沿参数方向有序延伸接缝并验证。

    B 轮廓按 (去质心) R 平移映射到世界系；延伸方向上保持
    a 索引 +1 / b 索引 -1（互补边走向相反），直到间距超过 gap_mm。
    返回接触长度、接触段上的逐点转角 NCC 与平均间距。
    """
    n = len(ca)
    center_b = cb[ib].mean(axis=0)

    def map_b(idx):
        return (cb[idx] - center_b) @ R.T + seam_center

    pair_a, pair_b = [], []
    seed_mid = len(ia) // 2
    for step in range(max_extend):
        for sgn in (1, -1):
            ai = (ia[seed_mid] + sgn * step) % n
            bi = (ib[seed_mid] - sgn * step) % n
            if np.linalg.norm(ca[ai] - map_b(bi)) <= gap_mm:
                pair_a.append((step, sgn, ai, bi))
    if len(pair_a) < 8:
        return {"contact_mm": 0.0, "tmatch": 0.0, "gap": 99.0}
    pair_a.sort(key=lambda z: z[0] * z[1])
    Aidx = np.array([z[2] for z in pair_a])
    Bidx = np.array([z[3] for z in pair_a])
    mapped_b = map_b(Bidx)
    gap = float(np.linalg.norm(ca[Aidx] - mapped_b, axis=1).mean())
    seg = np.linalg.norm(np.diff(np.vstack([mapped_b, mapped_b[:1]]), axis=0),
                         axis=1)
    contact = float(seg.sum())

    def turning(poly, idx, reverse=False):
        k = np.arange(len(idx))
        v0 = poly[idx[(k - 3) % len(idx)]] - poly[idx[k]]
        v1 = poly[idx[(k + 3) % len(idx)]] - poly[idx[k]]
        ang = np.arctan2(np.cross(v0, v1), (v0 * v1).sum(axis=1))
        return -ang if reverse else ang

    ua = turning(ca, Aidx)
    local = np.arange(len(mapped_b))
    ub = turning(mapped_b, local)
    ua0, ub0 = ua - ua.mean(), ub - ub.mean()
    den = math.sqrt(float((ua0 ** 2).sum() * (ub0 ** 2).sum())) + 1e-12
    tmatch = float((ua0 * ub0).sum() / den)
    return {"contact_mm": contact, "tmatch": tmatch, "gap": gap}


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
    sp = cc[starts[:, None] + win, :] - cc[starts[:, None], :]  # sp[i, s]
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
        if sv["contact_mm"] < 15.0 or sv["tmatch"] < 0.55 or sv["gap"] > 2.0:
            continue
        # 整件重叠与切线反向只作旁证（seam_verify 已确认接触弧）
        cb_mapped = (cb - cb[ib_orig].mean(axis=0)) @ fit["R"].T + seam_center
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
        fit_score = max(0.0, 1 - fit["rmse"] / 4.0)
        overlap_score = max(0.0, 1 - ov / 0.25)
        opposition_score = max(0.0, (-opposition + 1) / 2)
        contact_score = min(1.0, contact_mm / 40.0)
        tmatch_score = max(0.0, sv["tmatch"])
        score = (22 * w["ncc"] + 24 * fit_score +
                 12 * color_score + 6 * thick_score +
                 6 * overlap_score + 6 * opposition_score +
                 10 * contact_score + 14 * tmatch_score)
        cand = {
            "score": round(float(score), 1),
            "ncc": w["ncc"], "rmse": round(fit["rmse"], 2),
            "de": de if has_color else None,
            "thick_diff": round(dt, 2) if dt >= 0 else None,
            "overlap": round(ov, 3),
            "contact_mm": round(sv["contact_mm"], 1),
            "opposition": round(opposition, 2),
            "wiggle_deg": round(wiggle, 1),
            "tangent_match": round(sv["tmatch"], 3),
            "seam_gap_mm": round(sv["gap"], 2),
            "ia": ia.tolist(), "ib": ib_orig.tolist(),
            "R": fit["R"].tolist(),
            "t": t_world.tolist(),
            "seam_center_a": seam_center.tolist(),
            "seam_center_b": cb[ib_orig].mean(axis=0).tolist(),
            "reflected": fit["reflected"],
            "run_points": w["run"],
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
        f"刚性配准接缝偏差 RMSE {c['rmse']:.2f} mm",
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
