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
import planner
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
        "weight_g": r["weight_g"] if "weight_g" in r.keys() else 0,
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


def review_dict(r) -> dict:
    return {
        "id": r["id"], "plan_id": r["plan_id"],
        "candidate_id": r["candidate_id"],
        "status": r["status"], "note": r["note"],
        "adjustments": _jload(r["adjustments"], {}),
        "result": _jload(r["result"], {}),
        "auto_snapshot": _jload(r["auto_snapshot"], {}),
        "updated_at": r["updated_at"],
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


@app.post("/api/fragments/<int:fid>/weight")
def set_weight(fid):
    """补录碎片实测重量（克）；0 表示清除，回到按面积×厚度估算。"""
    data = request.get_json(force=True)
    db = get_db()
    db.execute("UPDATE fragment SET weight_g=? WHERE id=?",
               (max(0.0, float(data.get("weight_g") or 0)), fid))
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
    # 复核记录同样按方案复制（保留人工调整与重算结果）
    for rv in db.execute("SELECT * FROM review WHERE plan_id=?", (plan_id,)):
        db.execute(
            "INSERT INTO review (plan_id, candidate_id, status, note, "
            "adjustments, result, auto_snapshot) VALUES (?,?,?,?,?,?,?)",
            (new_id, rv["candidate_id"], rv["status"], rv["note"],
             rv["adjustments"], rv["result"], rv["auto_snapshot"]))
    # 装配规划随方案复制
    ap = db.execute("SELECT data FROM assembly_plan WHERE plan_id=?",
                    (plan_id,)).fetchone()
    if ap:
        db.execute("INSERT INTO assembly_plan (plan_id, data) VALUES (?,?)",
                   (new_id, ap["data"]))
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

def _accepted_review_params(db, plan_id) -> dict:
    """本方案中已接受复核的修正变换（不覆盖候选，仅在使用处生效）。"""
    if not plan_id:
        return {}
    out = {}
    for r in db.execute(
            "SELECT candidate_id, result FROM review WHERE plan_id=? "
            "AND status='accepted'", (plan_id,)):
        res = _jload(r["result"], {})
        if res.get("ok"):
            out[r["candidate_id"]] = res
    return out


def _effective_candidate(row, review_result: dict | None) -> dict:
    """把已接受复核的 ia/ib/R/t/接缝中心覆盖到候选参数上（副本）。"""
    d = dict(row)
    if review_result:
        p = _jload(row["params"], {})
        p.update({
            "ia": review_result["ia"], "ib": review_result["ib"],
            "R": review_result["R"], "t": review_result["t"],
            "seam_center_a": review_result["seam_center_a"],
            "seam_center_b": review_result["seam_center_b"],
            "review_corrected": True,
        })
        d["params"] = json.dumps(p, ensure_ascii=False)
    return d


@app.post("/api/projects/<int:pid>/metrics")
def plan_metrics(pid):
    """body: {layout:{fid:{x,y,rot,flip}}, accepted_ids:[...], plan_id?}"""
    body = request.get_json(force=True)
    db = get_db()
    frag_rows = db.execute("SELECT * FROM fragment WHERE project_id=?", (pid,)).fetchall()
    cands = {r["id"]: r for r in
             db.execute("SELECT * FROM candidate WHERE project_id=?", (pid,))}
    overrides = _accepted_review_params(db, body.get("plan_id"))
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
    accepted = [_effective_candidate(cands[i], overrides.get(i))
                for i in body.get("accepted_ids", []) if i in cands]
    metrics = compute_metrics(frags, body.get("layout") or {}, accepted)
    return jsonify(metrics)


@app.post("/api/projects/<int:pid>/conflicts")
def plan_conflicts(pid):
    """body: {accepted_ids:[...], plan_id?} —— 立即检测已接受候选间的矛盾。

    已接受的人工复核会以修正后的 ia/ib/R 参与检测。
    """
    body = request.get_json(force=True)
    db = get_db()
    frag_rows = db.execute("SELECT id FROM fragment WHERE project_id=?", (pid,)).fetchall()
    frags = {r["id"]: {"code": str(r["id"])} for r in frag_rows}
    cands = {r["id"]: r for r in
             db.execute("SELECT * FROM candidate WHERE project_id=?", (pid,))}
    overrides = _accepted_review_params(db, body.get("plan_id"))
    db.close()
    accepted = [_effective_candidate(cands[i], overrides.get(i))
                for i in body.get("accepted_ids", []) if i in cands]
    return jsonify({"conflicts": detect_conflicts(frags, accepted)})


# ---------------------------------------------------------------- 接缝人工复核

def _review_auto_snapshot(db, cand_row) -> dict:
    """用当前候选参数重算一遍，作为“自动结果”基线快照。"""
    fa = _load_feature(db, cand_row["frag_a"])
    fb = _load_feature(db, cand_row["frag_b"])
    params = _jload(cand_row["params"], {})
    if not fa or not fb or not params.get("ia"):
        return {"ok": False}
    base = matcher.review_adjacency(params, len(fa["curve_mm"]),
                                    len(fb["curve_mm"]))
    res = matcher.review_recompute(fa, fb, params, {"anchors": base["anchors"]})
    snap = res or {"ok": False}
    snap["candidate_score"] = cand_row["score"]
    snap["candidate_metrics"] = _jload(cand_row["metrics"], {})
    return snap


@app.get("/api/plans/<int:plan_id>/reviews")
def list_reviews(plan_id):
    db = get_db()
    rows = db.execute("SELECT * FROM review WHERE plan_id=?",
                      (plan_id,)).fetchall()
    out = [review_dict(r) for r in rows]
    db.close()
    return jsonify({"reviews": out})


@app.get("/api/plans/<int:plan_id>/reviews/<int:cid>")
def get_review(plan_id, cid):
    db = get_db()
    cand = db.execute("SELECT * FROM candidate WHERE id=?", (cid,)).fetchone()
    if not cand:
        db.close()
        return jsonify({"error": "候选不存在"}), 404
    r = db.execute("SELECT * FROM review WHERE plan_id=? AND candidate_id=?",
                   (plan_id, cid)).fetchone()
    if not r:
        # 首次打开：自动结果快照 + 默认锚点（不落库，保存时才写）
        snap = _review_auto_snapshot(db, cand)
        params = _jload(cand["params"], {})
        fa_row = _get_fragment(db, cand["frag_a"])
        n_a = len(_jload(fa_row["contour"], [])) if fa_row else matcher.N
        adj = (matcher.review_adjacency(params, n_a, matcher.N)
               if params.get("ia") else {"dir_b": 1, "anchors": []})
        db.close()
        return jsonify({"review": None, "auto_snapshot": snap,
                        "default_adj": adj})
    db.close()
    return jsonify({"review": review_dict(r)})


@app.post("/api/plans/<int:plan_id>/reviews/<int:cid>/recompute")
def recompute_review(plan_id, cid):
    """仅重算不落库：拖动锚点/区段时节流调用，返回实时指标。"""
    body = request.get_json(force=True)
    db = get_db()
    cand = db.execute("SELECT * FROM candidate WHERE id=?", (cid,)).fetchone()
    if not cand:
        db.close()
        return jsonify({"error": "候选不存在"}), 404
    fa = _load_feature(db, cand["frag_a"])
    fb = _load_feature(db, cand["frag_b"])
    db.close()
    params = _jload(cand["params"], {})
    if not fa or not fb or not params.get("ia"):
        return jsonify({"ok": False, "message": "缺少校准数据"}), 400
    result = matcher.review_recompute(fa, fb, params,
                                      body.get("adjustments") or {})
    return jsonify(result or {"ok": False, "message": "有效接触点过少"})


@app.put("/api/plans/<int:plan_id>/reviews/<int:cid>")
def save_review(plan_id, cid):
    """保存人工复核。body: {status, note, adjustments, recompute:true}

    recompute 为真时按锚点/排除区段重算指标与修正变换；
    调整结果以方案为单位存于 review 表，不修改原始候选。
    """
    body = request.get_json(force=True)
    status = body.get("status", "pending")
    if status not in ("accepted", "pending", "excluded"):
        return jsonify({"error": "状态无效"}), 400
    adjustments = body.get("adjustments") or {}
    db = get_db()
    cand = db.execute("SELECT * FROM candidate WHERE id=?", (cid,)).fetchone()
    if not cand:
        db.close()
        return jsonify({"error": "候选不存在"}), 404
    plan = db.execute("SELECT * FROM plan WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        db.close()
        return jsonify({"error": "方案不存在"}), 404

    result = {}
    if body.get("recompute", True):
        fa = _load_feature(db, cand["frag_a"])
        fb = _load_feature(db, cand["frag_b"])
        params = _jload(cand["params"], {})
        if not fa or not fb or not params.get("ia"):
            db.close()
            return jsonify({"error": "碎片缺少校准或轮廓，无法重算"}), 400
        result = matcher.review_recompute(fa, fb, params, adjustments)
        if not result or not result.get("ok"):
            db.close()
            return jsonify({"error": (result or {}).get("message",
                                                        "有效接触点过少")}), 400

    existing = db.execute("SELECT id FROM review WHERE plan_id=? AND candidate_id=?",
                          (plan_id, cid)).fetchone()
    note = str(body.get("note", ""))[:500]
    if existing:
        db.execute(
            "UPDATE review SET status=?, note=?, adjustments=?, result=?, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (status, note, json.dumps(adjustments, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), existing["id"]))
    else:
        snap = _review_auto_snapshot(db, cand)
        db.execute(
            "INSERT INTO review (plan_id, candidate_id, status, note, "
            "adjustments, result, auto_snapshot) VALUES (?,?,?,?,?,?,?)",
            (plan_id, cid, status, note,
             json.dumps(adjustments, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False),
             json.dumps(snap, ensure_ascii=False)))
    # 同步方案裁定，使接受的复核直接参与画布与冲突检查
    db.execute(
        "INSERT INTO decision (plan_id, candidate_id, status, note) "
        "VALUES (?,?,?,?) ON CONFLICT(plan_id, candidate_id) DO UPDATE SET "
        "status=excluded.status, note=excluded.note",
        (plan_id, cid, status, note))
    db.commit()
    row = db.execute("SELECT * FROM review WHERE plan_id=? AND candidate_id=?",
                     (plan_id, cid)).fetchone()
    db.close()
    return jsonify({"review": review_dict(row), "result": result})


@app.delete("/api/plans/<int:plan_id>/reviews/<int:cid>")
def reset_review(plan_id, cid):
    """放弃人工调整，恢复到自动候选状态（裁定回到待定）。"""
    db = get_db()
    db.execute("DELETE FROM review WHERE plan_id=? AND candidate_id=?",
               (plan_id, cid))
    db.execute(
        "INSERT INTO decision (plan_id, candidate_id, status, note) "
        "VALUES (?,?,'pending','') ON CONFLICT(plan_id, candidate_id) DO UPDATE SET "
        "status='pending', note=''", (plan_id, cid))
    db.commit()
    cand = db.execute("SELECT * FROM candidate WHERE id=?", (cid,)).fetchone()
    snap = _review_auto_snapshot(db, cand) if cand else {}
    db.close()
    return jsonify({"ok": True, "auto_snapshot": snap})


@app.get("/api/reviews/<int:rid>/sheet-data")
def review_sheet_data(rid):
    """打印核对单所需的两侧边缘条带渲染数据。"""
    db = get_db()
    r = db.execute("SELECT * FROM review WHERE id=?", (rid,)).fetchone()
    if not r:
        db.close()
        return jsonify({"error": "复核不存在"}), 404
    cand = db.execute("SELECT * FROM candidate WHERE id=?",
                      (r["candidate_id"],)).fetchone()
    fa = _get_fragment(db, cand["frag_a"])
    fb = _get_fragment(db, cand["frag_b"])
    db.close()
    result = _jload(r["result"], {})
    return jsonify({
        "review": review_dict(r),
        "candidate": candidate_dict(cand),
        "frag_a": fragment_dict(fa), "frag_b": fragment_dict(fb),
        "ia_all": result.get("ia_all", _jload(cand["params"], {}).get("ia", [])),
        "ib_all": result.get("ib_all", _jload(cand["params"], {}).get("ib", [])),
    })


# ---------------------------------------------------------------- 装配次序与临时支撑规划

def _assembly_frags(db, pid) -> dict:
    """规划所需的碎片几何与物理量（轮廓 px、质心、厚度、重量）。"""
    out = {}
    for r in db.execute("SELECT * FROM fragment WHERE project_id=?", (pid,)):
        contour = _jload(r["contour"], [])
        if len(contour) < 3:
            continue
        feats = _jload(r["features"], {})
        out[r["id"]] = {
            "code": r["code"],
            "contour": contour,
            "centroid_px": feats.get("centroid_px") or [0.0, 0.0],
            "mm_per_px": r["mm_per_px"] or 0.0,
            "thickness": r["thickness"] or 0.0,
            "weight_g": r["weight_g"] if "weight_g" in r.keys() else 0.0,
            "area_mm2": feats.get("area_mm2") or 0.0,
        }
    return out


def _accepted_seam_rows(db, plan) -> list:
    """本方案已接受接缝（含人工复核修正后的 ia/ib/R/t）。"""
    pid = plan["project_id"]
    cands = {r["id"]: r for r in
             db.execute("SELECT * FROM candidate WHERE project_id=?", (pid,))}
    accepted_ids = [r["candidate_id"] for r in db.execute(
        "SELECT candidate_id FROM decision WHERE plan_id=? AND status='accepted'",
        (plan["id"],))]
    overrides = _accepted_review_params(db, plan["id"])
    return [_effective_candidate(cands[i], overrides.get(i))
            for i in accepted_ids if i in cands]


@app.get("/api/plans/<int:plan_id>/assembly")
def get_assembly(plan_id):
    db = get_db()
    row = db.execute("SELECT * FROM assembly_plan WHERE plan_id=?",
                     (plan_id,)).fetchone()
    db.close()
    return jsonify({"data": _jload(row["data"], {}) if row else {},
                    "updated_at": row["updated_at"] if row else None})


@app.put("/api/plans/<int:plan_id>/assembly")
def save_assembly(plan_id):
    """保存装配规划（托点/禁入区/先后关系/接缝参数/步骤锁定与上次结果）。

    只写 assembly_plan，不改 plan_state 布局与 decision 裁定。
    """
    data = request.get_json(force=True)
    payload = json.dumps(data.get("data") or {}, ensure_ascii=False)
    db = get_db()
    db.execute(
        "INSERT INTO assembly_plan (plan_id, data) VALUES (?,?) "
        "ON CONFLICT(plan_id) DO UPDATE SET data=excluded.data, "
        "updated_at=datetime('now','localtime')", (plan_id, payload))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.post("/api/plans/<int:plan_id>/assembly/recompute")
def recompute_assembly(plan_id):
    """重算装配次序（只计算不落库，由前端随后整体保存）。

    body: {layout?, seams, supports, zones, relations, order, locked}
    """
    body = request.get_json(force=True)
    db = get_db()
    plan = db.execute("SELECT * FROM plan WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        db.close()
        return jsonify({"error": "方案不存在"}), 404
    frags = _assembly_frags(db, plan["project_id"])
    rows = _accepted_seam_rows(db, plan)
    st = db.execute("SELECT layout FROM plan_state WHERE plan_id=?",
                    (plan_id,)).fetchone()
    db.close()
    saved_layout = _jload(st["layout"], {}) if st else {}
    layout = body.get("layout") or saved_layout
    layout = {int(k): v for k, v in layout.items()}
    seams = []
    for row in rows:
        row = dict(row)
        row["params"] = _jload(row["params"], {})
        g = planner.seam_geometry(frags, layout, row)
        if g:
            seams.append(g)
    result = planner.compute_assembly(frags, layout, seams, body)
    return jsonify(result)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="127.0.0.1", port=port, debug=False)
