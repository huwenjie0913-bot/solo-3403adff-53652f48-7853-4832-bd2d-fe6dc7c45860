"""Flask 入口：文物陶瓷碎片整理工作台，所有接口仅监听本机。"""
from __future__ import annotations

import io  # noqa: F401  (保留给后续导出接口)
import json
import os
import sys
import threading
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, request, send_file, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import get_db, init_db
import vision
import matcher
from metrics import compute_metrics, detect_conflicts

app = Flask(__name__, static_folder="../static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

MATCH_LOCK = threading.Lock()


# ---------------------------------------------------------------- 序列化

def _jload(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


def fragment_dict(r) -> dict:
    return {
        "id": r["id"], "project_id": r["project_id"], "code": r["code"],
        "front_path": r["front_path"], "back_path": r["back_path"],
        "crop": _jload(r["crop"], None),
        "scale_mm": r["scale_mm"], "scale_px": r["scale_px"],
        "mm_per_px": r["mm_per_px"],
        "contour": _jload(r["contour"], []),
        "mask_url": f"/api/mask/{r['id']}" if r["mask_path"] else None,
        "thumb_url": f"/api/thumb/{r['id']}" if r["thumb_path"] else None,
        "cutout_url": f"/api/cutout/{r['id']}" if r["mask_path"] else None,
        "thickness": r["thickness"], "thickness_note": r["thickness_note"],
        "color_bands": _jload(r["color_bands"], []),
        "features": _jload(r["features"], {}),
        "created_at": r["created_at"],
    }


def candidate_dict(r, status: str | None = None) -> dict:
    return {
        "id": r["id"], "project_id": r["project_id"],
        "frag_a": r["frag_a"], "frag_b": r["frag_b"],
        "score": r["score"], "reasons": _jload(r["reasons"], []),
        "metrics": _jload(r["metrics"], {}), "params": _jload(r["params"], {}),
        "edge_a": r["edge_a"], "edge_b": r["edge_b"],
        "overlap": r["overlap"], "status": status or "pending",
        "created_at": r["created_at"],
    }


# ---------------------------------------------------------------- 页面

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- 项目

@app.get("/api/projects")
def list_projects():
    db = get_db()
    rows = db.execute("SELECT * FROM project ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        n = db.execute("SELECT COUNT(*) c FROM fragment WHERE project_id=?",
                       (r["id"],)).fetchone()["c"]
        p = db.execute("SELECT COUNT(*) c FROM plan WHERE project_id=?",
                       (r["id"],)).fetchone()["c"]
        out.append({"id": r["id"], "name": r["name"], "vessel": r["vessel"],
                    "note": r["note"], "mm_per_px": r["mm_per_px"],
                    "fragments": n, "plans": p,
                    "created_at": r["created_at"]})
    db.close()
    return jsonify(out)


@app.post("/api/projects")
def create_project():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip() or "未命名项目"
    db = get_db()
    cur = db.execute("INSERT INTO project (name, vessel, note) VALUES (?,?,?)",
                     (name, data.get("vessel", ""), data.get("note", "")))
    pid = cur.lastrowid
    db.execute("INSERT INTO plan (project_id, name, is_active) VALUES (?,?,1)",
               (pid, "方案 A"))
    db.commit()
    db.close()
    return jsonify({"id": pid})


@app.get("/api/projects/<int:pid>")
def get_project(pid):
    db = get_db()
    p = db.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
    if not p:
        db.close()
        return jsonify({"error": "项目不存在"}), 404
    frags = [fragment_dict(r) for r in
             db.execute("SELECT * FROM fragment WHERE project_id=? ORDER BY id",
                        (pid,))]
    plans = []
    for r in db.execute("SELECT * FROM plan WHERE project_id=? ORDER BY id",
                        (pid,)):
        st = db.execute("SELECT * FROM plan_state WHERE plan_id=?",
                        (r["id"],)).fetchone()
        plans.append({"id": r["id"], "name": r["name"],
                      "is_active": bool(r["is_active"]),
                      "layout": _jload(st["layout"], {}) if st else {}})
    cands = []
    for r in db.execute("SELECT * FROM candidate WHERE project_id=? ORDER BY score DESC",
                        (pid,)):
        cands.append(candidate_dict(r))
    db.close()
    return jsonify({
        "id": p["id"], "name": p["name"], "vessel": p["vessel"],
        "note": p["note"], "mm_per_px": p["mm_per_px"],
        "fragments": frags, "plans": plans, "candidates": cands,
    })


@app.put("/api/projects/<int:pid>")
def update_project(pid):
    data = request.get_json(force=True)
    db = get_db()
    db.execute("UPDATE project SET name=?, vessel=?, note=? WHERE id=?",
               (data.get("name", "未命名项目"), data.get("vessel", ""),
                data.get("note", ""), pid))
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 碎片

@app.post("/api/projects/<int:pid>/fragments")
def upload_fragments(pid):
    """批量上传。form 字段：files（多个）、side(front/back)、code_prefix。

    上传后立即执行去背景分割与轮廓/色带提取（尚未校准时 mm_per_px=0，
    轮廓仍以像素存储，校准后可重算）。
    """
    side = request.form.get("side", "front")
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "没有文件"}), 400
    db = get_db()
    project = db.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
    if not project:
        db.close()
        return jsonify({"error": "项目不存在"}), 404
    existing = db.execute(
        "SELECT COUNT(*) c FROM fragment WHERE project_id=?", (pid,)).fetchone()["c"]
    created = []
    errors = []
    for i, fs in enumerate(files):
        try:
            img_path = vision.save_upload(fs)
            code = f"{request.form.get('code_prefix', 'S')}{existing + i + 1:03d}"
            img = vision.load_image(img_path)
            mask, diag = vision.segment_shard(img)
            pts = vision.mask_to_contour(mask)
            mmpp = project["mm_per_px"] or 0.0
            feats = vision.contour_features(pts, mmpp or 1.0)
            feats["segment"] = diag
            edge_lab = vision.sample_edge_colors(img, mask, pts)
            bands = vision.summarize_bands(edge_lab)
            mask_p = vision.write_bytes(vision.make_cutout(img, mask), ".png")
            thumb_p = vision.write_bytes(vision.make_thumbnail(img, mask), ".png")
            cur = db.execute(
                "INSERT INTO fragment (project_id, code, front_path, back_path, "
                "mm_per_px, contour, mask_path, thumb_path, color_bands, features) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, code, str(img_path) if side == "front" else None,
                 str(img_path) if side == "back" else None,
                 mmpp, json.dumps(pts.round(2).tolist()),
                 str(mask_p), str(thumb_p),
                 json.dumps(bands), json.dumps({**feats, "edge_lab": edge_lab})))
            created.append({"id": cur.lastrowid, "code": code})
        except Exception as e:  # 单张失败不影响整批
            errors.append(f"{getattr(fs, 'filename', '?')}: {e}")
    db.commit()
    db.close()
    return jsonify({"created": created, "errors": errors})


def _get_fragment(db, fid):
    r = db.execute("SELECT * FROM fragment WHERE id=?", (fid,)).fetchone()
    return r


@app.post("/api/fragments/<int:fid>/calibrate")
def calibrate(fid):
    """标尺校准。body: {length_mm, p1:[x,y], p2:[x,y], side:'front'|'back'}"""
    data = request.get_json(force=True)
    db = get_db()
    r = _get_fragment(db, fid)
    if not r:
        db.close()
        return jsonify({"error": "碎片不存在"}), 404
    p1, p2 = data["p1"], data["p2"]
    px = float(np.hypot(p2[0] - p1[0], p2[1] - p1[1]))
    mm = float(data["length_mm"])
    if px <= 0 or mm <= 0:
        db.close()
        return jsonify({"error": "标尺参数无效"}), 400
    mmpp = mm / px
    db.execute("UPDATE fragment SET scale_mm=?, scale_px=?, mm_per_px=? WHERE id=?",
               (mm, px, mmpp, fid))
    # 用新比例刷新尺寸特征
    pts = np.array(_jload(r["contour"], []), dtype=np.float64)
    if len(pts) >= 3:
        feats = _jload(r["features"], {})
        feats.update(vision.contour_features(pts, mmpp))
        db.execute("UPDATE fragment SET features=? WHERE id=?",
                   (json.dumps(feats), fid))
    # 项目默认比例采用最近一次校准值
    db.execute("UPDATE project SET mm_per_px=? WHERE id=?", (mmpp, r["project_id"]))
    db.commit()
    db.close()
    return jsonify({"mm_per_px": round(mmpp, 6), "scale_px": round(px, 2)})


@app.post("/api/fragments/<int:fid>/thickness")
def set_thickness(fid):
    data = request.get_json(force=True)
    db = get_db()
    db.execute("UPDATE fragment SET thickness=?, thickness_note=? WHERE id=?",
               (float(data.get("thickness") or 0),
                str(data.get("note", ""))[:200], fid))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.put("/api/fragments/<int:fid>")
def rename_fragment(fid):
    data = request.get_json(force=True)
    db = get_db()
    db.execute("UPDATE fragment SET code=? WHERE id=?",
               (str(data.get("code") or f"S{fid}")[:20], fid))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.post("/api/fragments/<int:fid>/segment")
def resegment(fid):
    """指定裁剪框后重新分割。body: {crop:[x,y,w,h] | null}"""
    data = request.get_json(force=True)
    db = get_db()
    r = _get_fragment(db, fid)
    if not r:
        db.close()
        return jsonify({"error": "碎片不存在"}), 404
    img = vision.load_image(r["front_path"] or r["back_path"])
    crop = data.get("crop")
    cropped, ox, oy = vision.crop_rect(img, crop)
    mask_c, diag = vision.segment_shard(cropped)
    mask = np.zeros(img.shape[:2], np.uint8)
    mask[oy:oy + mask_c.shape[0], ox:ox + mask_c.shape[1]] = mask_c
    _store_mask_results(db, r, img, mask, crop)
    db.commit()
    db.close()
    return jsonify(_reload_payload(fid))


def _reload_payload(fid):
    db = get_db()
    r = _get_fragment(db, fid)
    out = fragment_dict(r)
    db.close()
    return out


@app.post("/api/fragments/<int:fid>/correct-contour")
def correct_contour(fid):
    """手工修正轮廓。body: {polygon_px:[[x,y],...]} 或 {mask_data_url:'...'}"""
    data = request.get_json(force=True)
    db = get_db()
    r = _get_fragment(db, fid)
    if not r:
        db.close()
        return jsonify({"error": "碎片不存在"}), 404
    img = vision.load_image(r["front_path"] or r["back_path"])
    if data.get("polygon_px"):
        mask = vision.polygon_to_mask(img.shape, data["polygon_px"])
    elif data.get("mask_data_url"):
        mask = vision.decode_mask_data_url(data["mask_data_url"])
        if mask.shape != img.shape[:2]:
            mask = cv2_resize_mask(mask, img.shape[:2])
    else:
        db.close()
        return jsonify({"error": "需要 polygon_px 或 mask_data_url"}), 400
    _store_mask_results(db, r, img, mask, _jload(r["crop"], None))
    db.commit()
    db.close()
    return jsonify(_reload_payload(fid))


def cv2_resize_mask(mask, shape_hw):
    import cv2
    return cv2.resize(mask, (shape_hw[1], shape_hw[0]),
                      interpolation=cv2.INTER_NEAREST)


def _store_mask_results(db, r, img, mask, crop):
    pts = vision.mask_to_contour(mask)
    mmpp = r["mm_per_px"] or 0.0
    feats = vision.contour_features(pts, mmpp or 1.0)
    old = _jload(r["features"], {})
    if "segment" in old:
        feats["segment"] = old["segment"]
    edge_lab = vision.sample_edge_colors(img, mask, pts)
    feats["edge_lab"] = edge_lab
    bands = vision.summarize_bands(edge_lab)
    mask_p = vision.write_bytes(vision.make_cutout(img, mask), ".png")
    thumb_p = vision.write_bytes(vision.make_thumbnail(img, mask), ".png")
    db.execute(
        "UPDATE fragment SET contour=?, mask_path=?, thumb_path=?, crop=?, "
        "color_bands=?, features=? WHERE id=?",
        (json.dumps(pts.round(2).tolist()), str(mask_p), str(thumb_p),
         json.dumps(crop) if crop else None,
         json.dumps(bands), json.dumps(feats), r["id"]))


@app.get("/api/img/<int:fid>")
def original_image(fid):
    db = get_db()
    r = _get_fragment(db, fid)
    db.close()
    if not r:
        return jsonify({"error": "碎片不存在"}), 404
    p = r["front_path"] or r["back_path"]
    return send_file(p, mimetype="image/jpeg")


@app.get("/api/mask/<int:fid>")
def mask_image(fid):
    return _serve_file(fid, "mask_path", "image/png")


@app.get("/api/thumb/<int:fid>")
def thumb_image(fid):
    return _serve_file(fid, "thumb_path", "image/png")


@app.get("/api/cutout/<int:fid>")
def cutout_image(fid):
    return _serve_file(fid, "mask_path", "image/png")


def _serve_file(fid, column, mime):
    db = get_db()
    r = _get_fragment(db, fid)
    db.close()
    if not r or not r[column]:
        return jsonify({"error": "文件不存在"}), 404
    return send_file(r[column], mimetype=mime)


# ---------------------------------------------------------------- 匹配

def _load_feature(db, fid) -> dict | None:
    r = _get_fragment(db, fid)
    if not r:
        return None
    feats = _jload(r["features"], {})
    pts = np.array(_jload(r["contour"], []), dtype=np.float64)
    if len(pts) < 10 or not r["mm_per_px"]:
        return None
    curve_mm = pts * r["mm_per_px"]
    return {
        "id": fid, "code": r["code"], "thickness": r["thickness"],
        "curve_mm": curve_mm,
        "edge_lab": feats.get("edge_lab"),
        "mm_per_px": r["mm_per_px"],
    }


@app.post("/api/projects/<int:pid>/match")
def run_match(pid):
    """对项目内全部碎片两两计算候选拼接。body: {frag_ids?:[...]}"""
    body = request.get_json(silent=True) or {}
    only = set(body.get("frag_ids") or [])
    with MATCH_LOCK:
        db = get_db()
        rows = db.execute("SELECT id FROM fragment WHERE project_id=?", (pid,)).fetchall()
        ids = [r["id"] for r in rows if not only or r["id"] in only]
        feats = {fid: f for fid in ids if (f := _load_feature(db, fid))}
        skipped = [fid for fid in ids if fid not in feats]
        new_rows = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                if a not in feats or b not in feats:
                    continue
                res = matcher.analyze_pair(feats[a], feats[b])
                if not res:
                    continue
                # 同一边对重复分析时刷新旧候选
                db.execute("DELETE FROM candidate WHERE project_id=? AND frag_a=? AND frag_b=?",
                           (pid, a, b))
                ia, ib = res["ia"], res["ib"]
                n = matcher.N
                cur = db.execute(
                    "INSERT INTO candidate (project_id, frag_a, frag_b, score, "
                    "reasons, metrics, params, edge_a, edge_b, overlap) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, a, b, res["score"],
                     json.dumps(matcher.build_reasons(res, feats[a], feats[b]),
                                ensure_ascii=False),
                     json.dumps({"ncc": res["ncc"], "rmse": res["rmse"],
                                 "de": res["de"],
                                 "thick_diff": res["thick_diff"]},
                                ensure_ascii=False),
                     json.dumps({"R": res["R"], "t": res["t"],
                                 "ia": ia, "ib": ib,
                                 "seam_center_a": res["seam_center_a"],
                                 "seam_center_b": res["seam_center_b"],
                                 "reflected": res["reflected"]}),
                     f"{min(ia)}-{max(ia)}", f"{min(ib)}-{max(ib)}",
                     round(res["run_points"] / n, 3)))
                new_rows.append(cur.lastrowid)
        db.commit()
        cands = [candidate_dict(r) for r in db.execute(
            "SELECT * FROM candidate WHERE project_id=? ORDER BY score DESC", (pid,))]
        db.close()
    return jsonify({"candidates": cands, "new": new_rows,
                    "skipped_uncalibrated": skipped})


# ---------------------------------------------------------------- 方案

def _active_plan(db, pid):
    r = db.execute("SELECT * FROM plan WHERE project_id=? AND is_active=1",
                   (pid,)).fetchone()
    if not r:
        r = db.execute("SELECT * FROM plan WHERE project_id=? ORDER BY id LIMIT 1",
                       (pid,)).fetchone()
    return r


@app.post("/api/projects/<int:pid>/plans")
def create_plan(pid):
    data = request.get_json(force=True)
    db = get_db()
    db.execute("UPDATE plan SET is_active=0 WHERE project_id=?", (pid,))
    cur = db.execute("INSERT INTO plan (project_id, name, is_active) VALUES (?,?,1)",
                     (pid, (data.get("name") or "新方案")[:40]))
    plan_id = cur.lastrowid
    db.execute("INSERT INTO plan_state (plan_id, layout) VALUES (?,?)",
               (plan_id, json.dumps(data.get("layout") or {})))
    db.commit()
    db.close()
    return jsonify({"id": plan_id})


@app.put("/api/plans/<int:plan_id>")
def rename_plan(plan_id):
    data = request.get_json(force=True)
    db = get_db()
    db.execute("UPDATE plan SET name=? WHERE id=?",
               ((data.get("name") or "新方案")[:40], plan_id))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.post("/api/plans/<int:plan_id>/duplicate")
def duplicate_plan(plan_id):
    db = get_db()
    r = db.execute("SELECT * FROM plan WHERE id=?", (plan_id,)).fetchone()
    if not r:
        db.close()
        return jsonify({"error": "方案不存在"}), 404
    st = db.execute("SELECT layout FROM plan_state WHERE plan_id=?",
                    (plan_id,)).fetchone()
    db.execute("UPDATE plan SET is_active=0 WHERE project_id=?", (r["project_id"],))
    cur = db.execute("INSERT INTO plan (project_id, name, is_active) VALUES (?,?,1)",
                     (r["project_id"], f"{r['name']} 副本"))
    new_id = cur.lastrowid
    db.execute("INSERT INTO plan_state (plan_id, layout) VALUES (?,?)",
               (new_id, st["layout"] if st else "{}"))
    for d in db.execute("SELECT * FROM decision WHERE plan_id=?", (plan_id,)):
        db.execute("INSERT INTO decision (plan_id, candidate_id, status, note) "
                   "VALUES (?,?,?,?)",
                   (new_id, d["candidate_id"], d["status"], d["note"]))
    db.commit()
    db.close()
    return jsonify({"id": new_id})


@app.post("/api/projects/<int:pid>/plans/activate")
def activate_plan(pid):
    data = request.get_json(force=True)
    plan_id = int(data["plan_id"])
    db = get_db()
    db.execute("UPDATE plan SET is_active=0 WHERE project_id=?", (pid,))
    db.execute("UPDATE plan SET is_active=1 WHERE id=? AND project_id=?",
               (plan_id, pid))
    db.commit()
    ok = db.total_changes > 0
    db.close()
    return jsonify({"ok": bool(ok)})


@app.put("/api/plans/<int:plan_id>/layout")
def save_layout(plan_id):
    data = request.get_json(force=True)
    db = get_db()
    db.execute("UPDATE plan_state SET layout=?, updated_at=datetime('now','localtime') "
               "WHERE plan_id=?", (json.dumps(data.get("layout") or {}), plan_id))
    if db.total_changes == 0:
        db.execute("INSERT INTO plan_state (plan_id, layout) VALUES (?,?)",
                   (plan_id, json.dumps(data.get("layout") or {})))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.put("/api/plans/<int:plan_id>/decisions/<int:cid>")
def set_decision(plan_id, cid):
    data = request.get_json(force=True)
    status = data.get("status", "pending")
    if status not in ("accepted", "excluded", "pending"):
        return jsonify({"error": "状态无效"}), 400
    db = get_db()
    note = str(data.get("note", ""))[:200]
    db.execute(
        "INSERT INTO decision (plan_id, candidate_id, status, note) "
        "VALUES (?,?,?,?) ON CONFLICT(plan_id, candidate_id) DO UPDATE SET "
        "status=excluded.status, note=excluded.note",
        (plan_id, cid, status, note))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.get("/api/plans/<int:plan_id>/decisions")
def get_decisions(plan_id):
    db = get_db()
    rows = db.execute("SELECT * FROM decision WHERE plan_id=?", (plan_id,)).fetchall()
    db.close()
    return jsonify({str(r["candidate_id"]):
                    {"status": r["status"], "note": r["note"]} for r in rows})


# ---------------------------------------------------------------- 指标

@app.post("/api/projects/<int:pid>/metrics")
def plan_metrics(pid):
    """body: {layout:{fid:{x,y,rot,flip}}, accepted_ids:[...]}"""
    body = request.get_json(force=True)
    db = get_db()
    frag_rows = db.execute("SELECT * FROM fragment WHERE project_id=?", (pid,)).fetchall()
    cands = {r["id"]: r for r in
             db.execute("SELECT * FROM candidate WHERE project_id=?", (pid,))}
    db.close()
    frags = {}
    for r in frag_rows:
        pts = _jload(r["contour"], [])
        if pts:
            frags[r["id"]] = {
                "code": r["code"],
                "contour_mm": (np.array(pts) * r["mm_per_px"]).tolist()
                if r["mm_per_px"] else pts,
                "mm_per_px": r["mm_per_px"],
            }
    accepted = [cands[i] for i in body.get("accepted_ids", []) if i in cands]
    metrics = compute_metrics(frags, body.get("layout") or {}, accepted)
    return jsonify(metrics)


@app.post("/api/projects/<int:pid>/conflicts")
def plan_conflicts(pid):
    """body: {accepted_ids:[...]} —— 立即检测已接受候选间的矛盾。"""
    body = request.get_json(force=True)
    db = get_db()
    frag_rows = db.execute("SELECT id FROM fragment WHERE project_id=?", (pid,)).fetchall()
    frags = {r["id"]: {"code": str(r["id"])} for r in frag_rows}
    cands = {r["id"]: r for r in
             db.execute("SELECT * FROM candidate WHERE project_id=?", (pid,))}
    db.close()
    accepted = [cands[i] for i in body.get("accepted_ids", []) if i in cands]
    return jsonify({"conflicts": detect_conflicts(frags, accepted)})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="127.0.0.1", port=port, debug=False)
