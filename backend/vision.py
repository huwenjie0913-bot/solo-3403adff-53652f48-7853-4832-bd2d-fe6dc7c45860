"""本地图像处理（纯 OpenCV / NumPy，不访问网络）。

提供：
  * 基于边框颜色建模 + 分水岭思路的碎片去背景分割；
  * 等弧长重采样轮廓（逆时针、单位 mm）；
  * 沿轮廓内侧采样 Lab 边缘色带；
  * 透明底抠图与缩略图输出；
  * 用户手工修正多边形 / 蒙版后的重新生成。
"""
from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

IMG_DIR = Path(__file__).resolve().parent.parent / "data" / "images"
MAX_SIDE = 1800          # 工作图像最长边（px）
CONTOUR_POINTS = 400     # 入库轮廓采样点数


# ---------------------------------------------------------------- 基础工具

def load_image(path: str | Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取图像: {path}")
    return _downscale(img)


def _downscale(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    scale = min(1.0, MAX_SIDE / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA)
    return img


def save_upload(file_storage) -> Path:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    raw = np.frombuffer(file_storage.read(), dtype=np.uint8)
    img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("上传文件不是有效图像")
    img = _downscale(img)
    name = f"{uuid.uuid4().hex}.jpg"
    out = IMG_DIR / name
    cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tofile(str(out))
    return out


def crop_rect(img: np.ndarray, crop: Optional[list]) -> tuple[np.ndarray, int, int]:
    """crop = [x, y, w, h]（原图像素）。返回裁剪图与偏移。"""
    if not crop or len(crop) != 4:
        return img, 0, 0
    h, w = img.shape[:2]
    x, y, cw, ch = [int(round(v)) for v in crop]
    x, y = max(0, x), max(0, y)
    x2, y2 = min(w, x + cw), min(h, y + ch)
    if x2 - x < 20 or y2 - y < 20:
        return img, 0, 0
    return img[y:y2, x:x2].copy(), x, y


# ---------------------------------------------------------------- 去背景

def segment_shard(img: np.ndarray) -> tuple[np.ndarray, dict]:
    """返回与原图等大的 uint8 蒙版（碎片=255）与诊断信息。"""
    h, w = img.shape[:2]
    blur = cv2.GaussianBlur(img, (5, 5), 0)
    lab = cv2.cvtColor(blur, cv2.COLOR_BGR2LAB)
    bg_lab = _border_median(lab)
    dist = np.sqrt(((lab.astype(np.float32) - bg_lab) ** 2).sum(axis=2))
    sat = cv2.cvtColor(blur, cv2.COLOR_BGR2HSV)[:, :, 1]
    gray = cv2.cvtColor(blur, cv2.COLOR_BGR2GRAY)

    # 背景种子：与边框颜色接近且低饱和、且能从边界漫进来的区域
    t_d = max(16.0, _otsu(dist) * 0.7)
    t_s = max(60.0, _otsu(sat) * 1.2)
    bg_candidate = ((dist < t_d) & (sat < t_s)).astype(np.uint8)
    n, labels = cv2.connectedComponents(bg_candidate, connectivity=4)
    border_ids = set(np.unique(np.r_[labels[0, :], labels[-1, :],
                                     labels[:, 0], labels[:, -1]]))
    border_ids.discard(0)
    flooded = np.isin(labels, list(border_ids))

    fg = (~flooded).astype(np.uint8) * 255
    # 明显比背景暗（断裂面/陶胎）且颜色不同的区域强制保留
    dark = gray < (np.median(_border_pixels(gray)) - 25)
    fg[(dark) & (dist > t_d * 0.6)] = 255

    k = max(3, int(round(min(h, w) * 0.006)))
    if k % 2 == 0:
        k += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel, iterations=2)
    fg = _largest_component(fg)
    fg = _fill_holes(fg)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    area_ratio = float((fg > 0).sum()) / (h * w)
    return fg, {"area_ratio": round(area_ratio, 4),
                "bg_lab": [round(float(v), 1) for v in bg_lab]}


def _border_pixels(chan: np.ndarray, band: int = 12) -> np.ndarray:
    h, w = chan.shape[:2]
    b = min(band, h // 4, w // 4)
    return np.concatenate([chan[:b, :].ravel(), chan[-b:, :].ravel(),
                           chan[:, :b].ravel(), chan[:, -b:].ravel()])


def _border_median(lab: np.ndarray, band: int = 12) -> np.ndarray:
    h, w = lab.shape[:2]
    b = min(band, h // 4, w // 4)
    edges = np.concatenate([
        lab[:b, :].reshape(-1, 3), lab[-b:, :].reshape(-1, 3),
        lab[:, :b].reshape(-1, 3), lab[:, -b:].reshape(-1, 3),
    ])
    return np.median(edges, axis=0)


def _otsu(a: np.ndarray) -> float:
    t, _ = cv2.threshold(a.astype(np.uint8), 0, 255,
                         cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return float(t)


def _largest_component(mask: np.ndarray) -> np.ndarray:
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return mask
    keep = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return np.where(labels == keep, 255, 0).astype(np.uint8)


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros_like(mask)
    cv2.drawContours(out, cnts, -1, 255, thickness=cv2.FILLED)
    return out


# ---------------------------------------------------------------- 轮廓

def mask_to_contour(mask: np.ndarray, epsilon_ratio: float = 0.0025) -> np.ndarray:
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        raise ValueError("蒙版中未找到碎片轮廓")
    cnt = max(cnts, key=cv2.contourArea)
    peri = cv2.arcLength(cnt, True)
    approx = cv2.approxPolyDP(cnt, epsilon_ratio * peri, True)
    pts = approx.reshape(-1, 2).astype(np.float64)
    pts = resample_closed(pts, CONTOUR_POINTS)
    area = cv2.contourArea(pts.astype(np.float32))
    if area < 0:  # 统一为逆时针（图像坐标下面积>0 即屏幕方向顺时针，这里仅保证一致）
        pts = pts[::-1]
    return pts


def resample_closed(pts: np.ndarray, n: int) -> np.ndarray:
    """按弧长等距重采样闭合折线。"""
    seg = np.linalg.norm(np.diff(np.vstack([pts, pts[:1]]), axis=0), axis=1)
    cum = np.concatenate([[0], np.cumsum(seg)])
    total = cum[-1]
    targets = np.linspace(0, total, n, endpoint=False)
    out = np.empty((n, 2))
    for dim in range(2):
        out[:, dim] = np.interp(targets, cum, np.r_[pts[:, dim], pts[0, dim]])
    return out


def polygon_to_mask(shape: tuple, polygon_px: list[list[float]]) -> np.ndarray:
    mask = np.zeros(shape[:2], np.uint8)
    pts = np.array(polygon_px, dtype=np.int32).reshape(-1, 1, 2)
    cv2.fillPoly(mask, [pts], 255)
    return _fill_holes(mask)


def contour_features(pts_px: np.ndarray, mm_per_px: float) -> dict:
    area_px = abs(float(cv2.contourArea(pts_px.astype(np.float32))))
    peri_px = float(cv2.arcLength(pts_px.astype(np.float32), True))
    M = cv2.moments(pts_px.astype(np.float32))
    cx = float(M["m10"] / M["m00"]) if M["m00"] else float(pts_px[:, 0].mean())
    cy = float(M["m01"] / M["m00"]) if M["m00"] else float(pts_px[:, 1].mean())
    return {
        "area_mm2": round(area_px * mm_per_px ** 2, 2),
        "perimeter_mm": round(peri_px * mm_per_px, 2),
        "centroid_px": [round(cx, 2), round(cy, 2)],
        "bbox_px": [int(pts_px[:, 0].min()), int(pts_px[:, 1].min()),
                    int(pts_px[:, 0].max()), int(pts_px[:, 1].max())],
    }


# ---------------------------------------------------------------- 色带

def sample_edge_colors(img: np.ndarray, mask: np.ndarray,
                       pts_px: np.ndarray, inset: int = 5) -> list[list[float]]:
    """在每个轮廓点沿法线向内 inset px 处取 3x3 邻域 Lab 均值。"""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    n = len(pts_px)
    prev = pts_px[(np.arange(n) - 1) % n]
    nxt = pts_px[(np.arange(n) + 1) % n]
    tangent = nxt - prev
    norm = np.linalg.norm(tangent, axis=1, keepdims=True)
    norm[norm == 0] = 1
    tangent /= norm
    inward = np.stack([-tangent[:, 1], tangent[:, 0]], axis=1)
    h, w = mask.shape
    samples: list[list[float]] = []
    radius = max(2, inset - 2)
    for i in range(n):
        x, y = pts_px[i] + inward[i] * inset
        xi, yi = int(round(np.clip(x, radius, w - 1 - radius))), \
                 int(round(np.clip(y, radius, h - 1 - radius)))
        patch = lab[max(0, yi - 1):yi + 2, max(0, xi - 1):xi + 2]
        mean_lab = patch.reshape(-1, 3).mean(axis=0)
        # 转 OpenCV Lab 存储值到近似 CIELAB
        samples.append([round(float(mean_lab[0]) / 255 * 100, 2),
                        round(float(mean_lab[1]) - 128, 2),
                        round(float(mean_lab[2]) - 128, 2)])
    return samples


def summarize_bands(edge_lab: list[list[float]], k: int = 5) -> list[dict]:
    """将逐点 Lab 聚成色带，供界面展示“邻接色带”依据。"""
    if not edge_lab:
        return []
    z = np.array(edge_lab, dtype=np.float32)
    k = min(k, len(z))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
    _, labels, centers = cv2.kmeans(z, k, None, criteria, 3,
                                    cv2.KMEANS_PP_CENTERS)
    bands = []
    for i in range(k):
        idx = np.where(labels.flatten() == i)[0]
        bands.append({
            "lab": [round(float(v), 1) for v in centers[i]],
            "ratio": round(len(idx) / len(z), 3),
            "positions": [int(idx.min()), int(idx.max())],
        })
    bands.sort(key=lambda b: -b["ratio"])
    return bands


# ---------------------------------------------------------------- 输出图

def make_cutout(img: np.ndarray, mask: np.ndarray) -> bytes:
    rgba = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = mask
    ok, buf = cv2.imencode(".png", rgba)
    return buf.tobytes()


def make_thumbnail(img: np.ndarray, mask: np.ndarray, side: int = 220) -> bytes:
    rgba = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = mask
    ys, xs = np.where(mask > 0)
    pad = 6
    x0, x1 = max(0, xs.min() - pad), min(mask.shape[1], xs.max() + pad)
    y0, y1 = max(0, ys.min() - pad), min(mask.shape[0], ys.max() + pad)
    crop = rgba[y0:y1, x0:x1]
    scale = side / max(crop.shape[:2])
    thumb = cv2.resize(crop, (max(1, int(crop.shape[1] * scale)),
                              max(1, int(crop.shape[0] * scale))),
                       interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", thumb)
    return buf.tobytes()


def write_bytes(data: bytes, suffix: str = ".png") -> Path:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    p = IMG_DIR / f"{uuid.uuid4().hex}{suffix}"
    p.write_bytes(data)
    return p


def decode_mask_data_url(data_url: str) -> np.ndarray:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    buf = np.frombuffer(raw, np.uint8)
    im = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if im.ndim == 3:
        im = im[:, :, 3] if im.shape[2] == 4 else \
            cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    return np.where(im > 127, 255, 0).astype(np.uint8)
