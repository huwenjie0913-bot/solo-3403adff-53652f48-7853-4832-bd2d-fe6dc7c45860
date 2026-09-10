/* ===================================================================
   接缝人工复核与校正 — 双栏检查器
   - 两件碎片的接缝边缘被“展开”为上下两条同步条带：
     共享 u∈[0,1] 横轴，支持同步缩放/滑动；
   - 条带像素列 = 沿轮廓法线向内采样的原图/透明抠图，
     可叠加曲率曲线与 Lab 色带；
   - 可拖动接缝起止点与对应锚点、圈选磨损/补配/反光等异常区段；
   - JS 实时重算 + 服务端节流复核，显示自动结果与人工结果差异。
=================================================================== */

const Review = (() => {
  const modal = document.getElementById('review-modal');
  const stage = document.getElementById('rv-stage');
  const canvas = document.getElementById('rv-canvas');
  const ctx = canvas.getContext('2d');
  const GAP = 64;            // 两条带之间留给对应连线的空隙
  const STEPS = 160;         // 与后端 REVIEW_U_STEPS 对齐

  let st = null;             // 当前复核会话状态
  let imgCache = new Map();  // fid -> {orig:ImageData, cut:ImageData, imgW,imgH}
  let rafQueued = false;
  let serverTimer = null;
  let saveTimer = null;
  let drag = null;

  const $ = (id) => document.getElementById(id);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  const ZONE_META = {
    wear: { label: '磨损', color: '#d3b44a' },
    fill: { label: '补配', color: '#6f97c4' },
    glare: { label: '反光', color: '#e0a85c' },
    other: { label: '其他', color: '#9aa3b0' },
  };

  // ---------------------------------------------------------------- 打开/关闭
  async function open(cid) {
    const app = App.context();
    const cand = app.candidates.get(+cid);
    if (!cand) return;
    const fa = app.fragments.get(cand.frag_a);
    const fb = app.fragments.get(cand.frag_b);
    if (!fa?.mm_per_px || !fb?.mm_per_px) {
      toast('两件碎片都完成标尺校准后才能复核接缝');
      return;
    }
    if (!cand.params?.ia) { toast('该候选缺少接缝参数'); return; }

    modal.classList.remove('hidden');
    setHead('正在载入复核数据…');
    let data;
    try {
      data = await API.get(`/api/plans/${app.activePlan}/reviews/${cid}`);
    } catch (e) { toast('载入失败：' + e.message); modal.classList.add('hidden'); return; }

    let status = 'pending', note = '', savedResult = null;
    let adjustments;
    if (data.review) {
      adjustments = data.review.adjustments || {};
      status = data.review.status; note = data.review.note || '';
      savedResult = data.review.result || {};
    } else {
      // 未复核：用接口给出的默认调整（自动接缝起止锚点、无异常区段）初始化
      adjustments = {
        anchors: data.default_adj?.anchors || [],
        zones: [],
      };
    }
    const anchors = adjustments.anchors ? clone(adjustments.anchors) : [];
    const zones = adjustments.zones ? clone(adjustments.zones) : [];

    st = {
      cid, cand, fa, fb,
      anchors, zones,
      status, note,
      auto: data.auto_snapshot || data.review?.auto_snapshot || {},
      savedResult,
      layers: new Set(['orig']),
      mode: 'pan',
      depth: 70,
      flipA: false, flipB: false,
      view: { u0: -0.03, u1: 1.03 },
      live: { gaps: new Array(STEPS).fill(0), excluded: zones.map(() => false) },
      undoStack: [], redoStack: [],
      dirty: false,
    };
    setHead(`接缝 #${cand.id} 复核：${fa.code} ⌇ ${fb.code}`);
    $('rv-note').value = note;
    selectMode('pan');
    syncLayerButtons();
    resizeCanvas();
    requestPaint();
    liveRecompute();
    renderSide();
    loadImages(fa, fb);
  }

  function close() { modal.classList.add('hidden'); st = null; }

  function setHead(t) { $('rv-title').textContent = t; }

  // ---------------------------------------------------------------- 图像缓存
  async function loadImages(fa, fb) {
    await Promise.all([fa, fb].map(async (f) => {
      if (imgCache.has(f.id)) return;
      const [origImg, cutImg] = await Promise.all([
        loadHtmlImage(`/api/img/${f.id}`),
        loadHtmlImage(`/api/cutout/${f.id}`),
      ]);
      imgCache.set(f.id, {
        orig: toImageData(origImg), cut: toImageData(cutImg),
        w: origImg.naturalWidth, h: origImg.naturalHeight,
      });
    }));
    requestPaint();
  }

  function loadHtmlImage(url) {
    return new Promise((res, rej) => {
      const im = new Image();
      im.onload = () => res(im);
      im.onerror = rej;
      im.src = url;
    });
  }
  function toImageData(img) {
    const c = document.createElement('canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    const g = c.getContext('2d');
    g.drawImage(img, 0, 0);
    return g.getImageData(0, 0, c.width, c.height);
  }

  // ---------------------------------------------------------------- 视图几何
  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const W = stage.clientWidth, H = stage.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    requestPaint();
  }

  function geom() {
    const W = stage.clientWidth, H = stage.clientHeight;
    const padX = 8, padY = 14;
    const ribH = (H - padY * 2 - GAP) / 2;
    return {
      W, H, padX,
      ay0: padY, ay1: padY + ribH,
      by0: padY + ribH + GAP, by1: H - padY,
      ribH,
    };
  }
  function pxPerU() {
    const { W, padX } = geom();
    return (W - padX * 2) / (st.view.u1 - st.view.u0);
  }
  const uToX = (u) => geom().padX + (u - st.view.u0) * pxPerU();
  const xToU = (x) => st.view.u0 + (x - geom().padX) / pxPerU();

  function zoomAt(cxPx, factor) {
    const u = xToU(cxPx);
    const span = st.view.u1 - st.view.u0;
    const ns = clamp(span / factor, 0.02, 1.4);
    const r = (u - st.view.u0) / span;
    st.view.u0 = u - r * ns;
    st.view.u1 = st.view.u0 + ns;
    requestPaint();
  }

  // ---------------------------------------------------------------- 对应映射
  function mapU(u) {
    const a = st.anchors;
    if (a.length < 2) return { ia: 0, ib: 0 };
    if (u <= a[0].u) return { ia: a[0].ia, ib: a[0].ib };
    for (let k = 0; k < a.length - 1; k++) {
      if (u <= a[k + 1].u) {
        const t = clamp((u - a[k].u) / (a[k + 1].u - a[k].u), 0, 1);
        return {
          ia: a[k].ia + (a[k + 1].ia - a[k].ia) * t,
          ib: a[k].ib + (a[k + 1].ib - a[k].ib) * t,
        };
      }
    }
    const l = a[a.length - 1];
    return { ia: l.ia, ib: l.ib };
  }

  function sampledCorrespondence() {
    const iaf = new Array(STEPS), ibf = new Array(STEPS);
    for (let i = 0; i < STEPS; i++) {
      const m = mapU(i / (STEPS - 1));
      iaf[i] = m.ia; ibf[i] = m.ib;
    }
    return { iaf, ibf };
  }

  function excludedMask() {
    const mask = new Array(STEPS).fill(false);
    for (const z of st.zones) {
      const lo = Math.min(z.u0, z.u1), hi = Math.max(z.u0, z.u1);
      for (let i = 0; i < STEPS; i++) {
        const u = i / (STEPS - 1);
        if (u >= lo && u <= hi) mask[i] = true;
      }
    }
    return mask;
  }

  // ---------------------------------------------------------------- 实时重算（JS）
  function contourMm(f) {
    const k = f.mm_per_px || 1;
    return f.contour.map(p => [p[0] * k, p[1] * k]);
  }

  function arcKappa(cm) {
    // 与 matcher.derive 一致：弧长高斯平滑 + ±4 点有符号转角
    const n = cm.length;
    const seg = new Array(n);
    for (let i = 0; i < n; i++) {
      const j = (i + 1) % n;
      seg[i] = Math.hypot(cm[j][0] - cm[i][0], cm[j][1] - cm[i][1]);
    }
    const step = seg.slice().sort((a, b) => a - b)[n >> 1] || 1;
    const rad = Math.max(2, Math.round(3 * 2.5 / step));
    const kernel = [];
    for (let t = -rad; t <= rad; t++) {
      const tt = t * step;
      kernel.push(Math.exp(-(tt * tt) / (2 * 2.5 * 2.5)));
    }
    const ksum = kernel.reduce((a, b) => a + b, 0);
    const sm = (dim) => {
      const out = new Array(n);
      for (let i = 0; i < n; i++) {
        let v = 0;
        for (let t = -rad; t <= rad; t++) v += cm[((i + t) % n + n) % n][dim] * kernel[t + rad];
        out[i] = v / ksum;
      }
      return out;
    };
    const sx = sm(0), sy = sm(1), kappa = new Array(n);
    const half = 4;
    for (let i = 0; i < n; i++) {
      const d1x = sx[(i + half) % n] - sx[(i - half + n) % n];
      const d1y = sy[(i + half) % n] - sy[(i - half + n) % n];
      const d2x = sx[(i + 2 * half) % n] - sx[(i + half) % n];
      const d2y = sy[(i + 2 * half) % n] - sy[(i + half) % n];
      const den = Math.hypot(d1x, d1y) * Math.hypot(d2x, d2y) + 1e-9;
      kappa[i] = clamp((d1x * d2y - d1y * d2x) / den, -1, 1);
    }
    return kappa;
  }

  function pointAtPoly(poly, idxFloat, tmp) {
    // 结果写入 tmp=[x,y]；闭合折线线性插值
    const n = poly.length;
    const i0 = Math.floor(idxFloat);
    const t = idxFloat - i0;
    const p0 = poly[((i0 % n) + n) % n];
    const p1 = poly[((i0 + 1) % n + n) % n];
    tmp[0] = p0[0] + (p1[0] - p0[0]) * t;
    tmp[1] = p0[1] + (p1[1] - p0[1]) * t;
    return tmp;
  }

  function liveRecompute() {
    if (!st) return;
    const ca = contourMm(st.fa), cb = contourMm(st.fb);
    const nA = ca.length, nB = cb.length;
    const { iaf, ibf } = sampledCorrespondence();
    const excluded = excludedMask();
    const pa = iaf.map((v, i) => pointAtPoly(ca, v, [0, 0]).slice());
    const pb = ibf.map((v) => pointAtPoly(cb, v, [0, 0]).slice());
    const validIdx = [];
    for (let i = 0; i < STEPS; i++) if (!excluded[i]) validIdx.push(i);

    const r = { ok: validIdx.length >= 6, gaps: new Array(STEPS).fill(0), excluded };
    if (!r.ok) { st.live = r; renderSide(); return; }

    // 2D 刚体拟合 B→A（闭式角）
    let ma = [0, 0], mb = [0, 0];
    for (const i of validIdx) { ma[0] += pa[i][0]; ma[1] += pa[i][1]; mb[0] += pb[i][0]; mb[1] += pb[i][1]; }
    ma = ma.map(v => v / validIdx.length); mb = mb.map(v => v / validIdx.length);
    let num = 0, den = 0;
    for (const i of validIdx) {
      const x0 = pa[i][0] - ma[0], x1 = pa[i][1] - ma[1];
      const y0 = pb[i][0] - mb[0], y1 = pb[i][1] - mb[1];
      num += x0 * y1 - x1 * y0;
      den += x0 * y0 + x1 * y1;
    }
    const ang = Math.atan2(num, den), c = Math.cos(ang), s = Math.sin(ang);
    const R = [[c, -s], [s, c]];
    const t = [ma[0] - (mb[0] * c + mb[1] * s), ma[1] - (-mb[0] * s + mb[1] * c)];
    const mapB = (p) => [p[0] * c + p[1] * s + t[0], -p[0] * s + p[1] * c + t[1]];

    let se = 0, segSum = 0, contact = 0, validMm = 0, exclMm = 0;
    for (let i = 0; i < STEPS; i++) {
      const q = mapB(pb[i]);
      const g = Math.hypot(pa[i][0] - q[0], pa[i][1] - q[1]);
      r.gaps[i] = g;
      const i2 = (i + 1) % STEPS;
      const seg = Math.hypot(pa[i2][0] - pa[i][0], pa[i2][1] - pa[i][1]);
      if (!excluded[i]) {
        se += g * g;
        validMm += seg;
        if (g < 2.0) contact += seg;
      } else {
        exclMm += seg;
      }
      segSum += seg;
    }
    r.rmse = Math.sqrt(se / validIdx.length);
    r.contact_mm = contact; r.valid_mm = validMm; r.excluded_mm = exclMm;
    r.valid_ratio = validIdx.length / STEPS;
    r.R = R; r.t = t; r.angDeg = ang * 180 / Math.PI;

    // Lab 色差
    const labA = st.fa.features?.edge_lab, labB = st.fb.features?.edge_lab;
    if (labA && labB) {
      let ds = 0;
      for (const i of validIdx) {
        const la = labA[((Math.round(iaf[i]) % nA) + nA) % nA];
        const lb = labB[((Math.round(ibf[i]) % nB) + nB) % nB];
        ds += Math.hypot(la[0] - lb[0], la[1] - lb[1], la[2] - lb[2]);
      }
      r.de = ds / validIdx.length;
    }

    // 曲率 NCC（与后端同向/反向规则一致）
    const ka = arcKappa(ca), kb = arcKappa(cb);
    const va = [], vb = [];
    const dirB = inferDirB();
    for (const i of validIdx) {
      const ai = ((Math.round(iaf[i]) % nA) + nA) % nA;
      const bi0 = ((Math.round(ibf[i]) % nB) + nB) % nB;
      va.push(ka[ai]);
      vb.push(dirB === 1 ? kb[bi0] : kb[(-bi0 + nB * 2) % nB]);
    }
    const maK = va.reduce((a, b) => a + b, 0) / va.length;
    const mbK = vb.reduce((a, b) => a + b, 0) / vb.length;
    let dot = 0, sa2 = 0, sb2 = 0;
    for (let i = 0; i < va.length; i++) {
      const x = va[i] - maK, y = vb[i] - mbK;
      dot += x * y; sa2 += x * x; sb2 += y * y;
    }
    r.ncc = dot / (Math.sqrt(sa2 * sb2) + 1e-9);

    // 反对向（最近点法线点积中位数）
    const dots = [];
    for (const i of validIdx) {
      const q = mapB(pb[i]);
      let best = 1e9, bj = 0;
      for (let j = 0; j < nA; j++) {
        const d = (ca[j][0] - q[0]) ** 2 + (ca[j][1] - q[1]) ** 2;
        if (d < best) { best = d; bj = j; }
      }
      const ta = tangent(ca, bj), tbRaw = tangent(cb, ((Math.round(ibf[i]) % nB) + nB) % nB);
      // 映射后的 b 切线
      const tbm = [tbRaw[0] * c + tbRaw[1] * s, -tbRaw[0] * s + tbRaw[1] * c];
      dots.push(ta[0] * tbm[0] + ta[1] * tbm[1]);
    }
    dots.sort((a, b) => a - b);
    r.opposition = dots[dots.length >> 1];
    // 重叠率实时不重算，沿用服务端最近一次
    r.overlap = st.live?.overlap ?? st.auto?.overlap ?? st.savedResult?.overlap ?? 0;

    const faT = parseFloat(st.fa.thickness) || 0, fbT = parseFloat(st.fb.thickness) || 0;
    r.thick_diff = faT && fbT ? Math.abs(faT - fbT) : null;
    r.score = scoreOf(r);
    st.live = r;
    renderSide();
  }

  function tangent(poly, i) {
    const n = poly.length;
    const p0 = poly[(i - 2 + n) % n], p1 = poly[(i + 2) % n];
    const v = [p1[0] - p0[0], p1[1] - p0[1]];
    const l = Math.hypot(v[0], v[1]) || 1;
    return [v[0] / l, v[1] / l];
  }

  function inferDirB() {
    const a = st.anchors;
    if (a.length < 2) return 1;
    const da = signedSpan(a[0].ia, a[a.length - 1].ia, st.fa.contour.length);
    const db = signedSpan(a[0].ib, a[a.length - 1].ib, st.fb.contour.length);
    return da * db >= 0 ? 1 : -1;
  }
  function signedSpan(i0, i1, n) {
    const d = ((((i1 - i0) % n) + n) % n);
    return d > n / 2 ? d - n : d;
  }

  function scoreOf(r) {
    const seamFit = Math.max(0, 1 - r.rmse / 3);
    const contactScore = Math.min(1, r.contact_mm / 50);
    const nccScore = Number.isFinite(r.ncc) ? Math.max(0, (r.ncc + 1) / 2) : 0.5;
    const deScore = r.de == null ? 0.5 : clamp(1 - r.de / 25, 0, 1);
    const dt = r.thick_diff;
    const thickScore = dt == null ? 0.5 : Math.max(0, 1 - dt / 3);
    const overlapScore = Math.max(0, 1 - (r.overlap || 0) / 0.25);
    const oppScore = Math.max(0, (-r.opposition + 1) / 2);
    return Math.round((24 * seamFit + 14 * contactScore + 16 * nccScore +
      16 * deScore + 8 * thickScore + 8 * overlapScore +
      6 * oppScore + 8 * r.valid_ratio) * 10) / 10;
  }

  // ---------------------------------------------------------------- 条带绘制
  function paint() {
    if (!st) return;
    const g = geom();
    ctx.clearRect(0, 0, g.W, g.H);
    ctx.fillStyle = '#15181d';
    ctx.fillRect(0, 0, g.W, g.H);
    paintRibbon('a', g.ay0, g.ay1, g);
    paintRibbon('b', g.by0, g.by1, g);
    paintGap(g, ctx);
    paintZones(g, false, ctx);
    paintAnchorKnobs(g, ctx);
  }

  function paintRibbon(side, y0, y1, g, targetCtx, targetW) {
    const g2 = g || geom();
    const c2 = targetCtx || ctx;
    const screen = !targetCtx;
    const W = targetW || g2.W;
    const f = side === 'a' ? st.fa : st.fb;
    const H = y1 - y0;
    const cache = imgCache.get(f.id);
    const useCut = st.layers.has('cutout');
    const x0 = screen ? clamp(Math.floor(uToX(st.view.u0)), g2.padX, W - g2.padX) : 0;
    const x1 = screen ? clamp(Math.ceil(uToX(st.view.u1)), g2.padX, W - g2.padX) : W;
    const bw = Math.max(1, x1 - x0);

    const img = cache ? (useCut ? cache.cut : cache.orig) : null;
    const cut = cache?.cut;
    const imgW = cache?.w || 1, imgH = cache?.h || 1;
    const n = f.contour.length;
    const flip = side === 'a' ? st.flipA : st.flipB;
    const buf = c2.createImageData(bw, H);
    const data = buf.data;

    for (let x = 0; x < bw; x++) {
      const screenX = x0 + x;
      const u = screen ? xToU(screenX)
        : st.view.u0 + (x / W) * (st.view.u1 - st.view.u0);
      const m = mapU(u);
      const idxF = side === 'a' ? m.ia : m.ib;
      const i0 = Math.floor(idxF), tt = idxF - i0;
      const p0 = f.contour[((i0 % n) + n) % n];
      const p1 = f.contour[(((i0 + 1) % n) + n) % n];
      const px = p0[0] + (p1[0] - p0[0]) * tt;
      const py = p0[1] + (p1[1] - p0[1]) * tt;
      const im = ((i0 - 1) % n + n) % n, ip = (i0 + 1) % n;
      let tx = f.contour[ip][0] - f.contour[im][0];
      let ty = f.contour[ip][1] - f.contour[im][1];
      const tl = Math.hypot(tx, ty) || 1; tx /= tl; ty /= tl;
      let nx = -ty, ny = tx;                 // 与 vision.sample_edge_colors 一致
      if (flip) { nx = -nx; ny = -ny; }

      for (let y = 0; y < H; y++) {
        const d = (y / H) * (st.depth + 12) - 6;
        const sx = clamp(Math.round(px + nx * d), 0, imgW - 1);
        const sy = clamp(Math.round(py + ny * d), 0, imgH - 1);
        const o = (y * bw + x) * 4;
        if (!img) {
          data[o] = 40; data[o + 1] = 46; data[o + 2] = 54; data[o + 3] = 255;
          continue;
        }
        const si = (sy * imgW + sx) * 4;
        if (useCut) {
          data[o] = img.data[si]; data[o + 1] = img.data[si + 1];
          data[o + 2] = img.data[si + 2]; data[o + 3] = img.data[si + 3];
        } else {
          const outside = cut && cut.data[si + 3] < 60;
          const dim = outside ? 0.32 : 1;
          data[o] = img.data[si] * dim; data[o + 1] = img.data[si + 1] * dim;
          data[o + 2] = img.data[si + 2] * dim; data[o + 3] = 255;
        }
      }
    }
    c2.putImageData(buf, screen ? x0 : 0, y0);

    // 标签框
    c2.strokeStyle = '#5a6470'; c2.lineWidth = 1;
    c2.strokeRect(screen ? g2.padX : 0, y0,
      screen ? g2.W - g2.padX * 2 : W, H);
    c2.fillStyle = '#cfd6df'; c2.font = '12px sans-serif';
    c2.fillText((side === 'a' ? 'A · ' : 'B · ') + f.code +
      (flip ? '（法向翻转）' : ''), (screen ? g2.padX : 0) + 6, y0 + 15);

    if (st.layers.has('curve')) paintCurveOverlay(side, y0, y1, g2, c2, targetCtx, W);
    if (st.layers.has('lab')) paintLabOverlay(side, y0, y1, g2, c2, targetCtx, W);
    paintEdgeLine(side, y0, y1, g2, c2, targetCtx, W);
  }

  function forEachScreenCol(g2, targetCtx, W, cb) {
    const x0 = targetCtx ? 0 : g2.padX;
    const x1 = targetCtx ? W : g2.W - g2.padX;
    for (let x = x0; x <= x1; x++) {
      const u = targetCtx
        ? (x / W) : xToU(x);
      cb(x, clamp(u, 0, 1));
    }
  }

  function paintCurveOverlay(side, y0, y1, g2, c2, targetCtx, W) {
    const f = side === 'a' ? st.fa : st.fb;
    const kp = arcKappa(contourMm(f));
    const n = kp.length, H = y1 - y0;
    c2.save();
    c2.beginPath();
    c2.rect(targetCtx ? 0 : g2.padX, y0, targetCtx ? W : g2.W - g2.padX * 2, H);
    c2.clip();
    c2.strokeStyle = 'rgba(255,255,255,.25)';
    const baseY = y0 + H / 2;
    c2.beginPath();
    c2.moveTo(targetCtx ? 0 : g2.padX, baseY);
    c2.lineTo(targetCtx ? W : g2.W - g2.padX, baseY);
    c2.stroke();
    c2.strokeStyle = '#ffd35c'; c2.lineWidth = 1.6;
    c2.beginPath();
    forEachScreenCol(g2, targetCtx, W, (x, u) => {
      const m = mapU(u), idxF = side === 'a' ? m.ia : m.ib;
      const i = ((Math.round(idxF) % n) + n) % n;
      const yy = baseY - kp[i] * H * 0.42;
      x === (targetCtx ? 0 : g2.padX) ? c2.moveTo(x, yy) : c2.lineTo(x, yy);
    });
    c2.stroke();
    c2.restore();
  }

  function paintLabOverlay(side, y0, y1, g2, c2, targetCtx, W) {
    const f = side === 'a' ? st.fa : st.fb;
    const lab = f.features?.edge_lab;
    if (!lab) return;
    const n = lab.length, bar = 14;
    c2.save();
    c2.beginPath();
    c2.rect(targetCtx ? 0 : g2.padX, y1 - bar, targetCtx ? W : g2.W - g2.padX * 2, bar);
    c2.clip();
    forEachScreenCol(g2, targetCtx, W, (x, u) => {
      const m = mapU(u), idxF = side === 'a' ? m.ia : m.ib;
      const i = ((Math.round(idxF) % n) + n) % n;
      c2.fillStyle = labToCss(...lab[i]);
      c2.fillRect(x, y1 - bar, 2, bar);
    });
    c2.restore();
    c2.fillStyle = '#cfd6df'; c2.font = '10px sans-serif';
    c2.fillText('Lab', (targetCtx ? 4 : g2.padX + 4), y1 - bar - 3);
  }

  function paintEdgeLine(side, y0, y1, g2, c2, targetCtx, W) {
    const H = y1 - y0;
    const edgeY = side === 'a' ? y1 - 1 : y0 + 1;
    c2.save();
    c2.beginPath();
    c2.rect(targetCtx ? 0 : g2.padX, y0, targetCtx ? W : g2.W - g2.padX * 2, H);
    c2.clip();
    c2.lineWidth = 2;
    forEachScreenCol(g2, targetCtx, W, (x, u) => {
      const i = Math.round(clamp(u, 0, 1) * (STEPS - 1));
      const excluded = st.live.excluded?.[i];
      const gap = st.live.gaps?.[i] ?? 0;
      c2.strokeStyle = excluded ? 'rgba(201,106,95,.95)'
        : gap < 1.0 ? '#6fae6f' : gap < 2.0 ? '#d3b44a' : '#c96a5f';
      c2.beginPath(); c2.moveTo(x, edgeY); c2.lineTo(x + 1, edgeY); c2.stroke();
    });
    c2.restore();
  }

  function paintGap(g, c2) {
    // 对应连线（每个锚点一条）与 u 刻度
    c2.strokeStyle = 'rgba(127,182,232,.5)'; c2.lineWidth = 1;
    c2.fillStyle = '#7f93a8'; c2.font = '10px sans-serif';
    st.anchors.forEach((a, k) => {
      const x = uToX(a.u);
      if (x < g.padX || x > g.W - g.padX) return;
      c2.beginPath();
      c2.setLineDash(k === 0 || k === st.anchors.length - 1 ? [] : [4, 4]);
      c2.moveTo(x, g.ay1 + 2); c2.lineTo(x, g.by0 - 2); c2.stroke();
      c2.setLineDash([]);
      const ia = modIdx(a.ia, st.fa.contour.length);
      const ib = modIdx(a.ib, st.fb.contour.length);
      c2.fillStyle = '#7f93a8';
      c2.fillText(`${ia} ↔ ${ib}`, x + 3, (g.ay1 + g.by0) / 2 + 3);
    });
    // 每 10% 刻度
    c2.strokeStyle = 'rgba(255,255,255,.12)';
    for (let p = 0; p <= 10; p++) {
      const x = uToX(p / 10);
      c2.beginPath(); c2.moveTo(x, g.ay1); c2.lineTo(x, g.by0); c2.stroke();
    }
  }

  function paintZones(g, forPrint, c2) {
    c2 = c2 || ctx;
    st.zones.forEach((z, zi) => {
      const x0 = uToX(Math.min(z.u0, z.u1)), x1 = uToX(Math.max(z.u0, z.u1));
      const meta = ZONE_META[z.kind] || ZONE_META.other;
      const top = forPrint ? 0 : g.ay0, bot = forPrint ? 0 : g.by1;
      c2.fillStyle = hexA(meta.color, forPrint ? 0.28 : 0.16);
      c2.fillRect(x0, top, x1 - x0, bot - top);
      if (!forPrint) {
        c2.strokeStyle = meta.color; c2.lineWidth = zi === st.selectedZone ? 2.4 : 1.2;
        c2.setLineDash([6, 4]);
        c2.strokeRect(x0, g.ay0, x1 - x0, g.by1 - g.ay0);
        c2.setLineDash([]);
        c2.fillStyle = meta.color; c2.font = 'bold 11px sans-serif';
        c2.fillText(meta.label, x0 + 4, g.ay0 - 3 < 14 ? g.ay0 + 12 : g.ay0 + 12);
      }
    });
  }

  function paintAnchorKnobs(g, c2) {
    c2 = c2 || ctx;
    st.anchors.forEach((a, k) => {
      const x = uToX(a.u);
      if (x < g.padX - 8 || x > g.W - g.padX + 8) return;
      const isEnd = k === 0 || k === st.anchors.length - 1;
      knob(c2, x, g.ay1, isEnd ? '#ffd35c' : '#7fe08a', isEnd ? 6 : 5, isEnd ? 'square' : 'diamond');
      knob(c2, x, g.by0, isEnd ? '#ffd35c' : '#7fe08a', isEnd ? 6 : 5, isEnd ? 'square' : 'diamond');
    });
  }
  function knob(c2, x, y, color, r, shape) {
    c2.fillStyle = color; c2.strokeStyle = '#1c2026'; c2.lineWidth = 1.5;
    c2.beginPath();
    if (shape === 'square') c2.rect(x - r, y - r, r * 2, r * 2);
    else { c2.moveTo(x, y - r); c2.lineTo(x + r, y); c2.lineTo(x, y + r); c2.lineTo(x - r, y); c2.closePath(); }
    c2.fill(); c2.stroke();
  }

  function labToCss(L, a, b) {
    let Y = (L + 16) / 116, X = a / 500 + Y, Z = Y - b / 200;
    const f3 = t => t > 6 / 29 ? t ** 3 : 3 * (6 / 29) ** 2 * (t - 4 / 29);
    X = 0.95047 * f3(X); Y = f3(Y); Z = 1.08883 * f3(Z);
    const r = X * 3.2406 + Y * -1.5372 + Z * -0.4986;
    const gg = X * -0.9689 + Y * 1.8758 + Z * 0.0415;
    const bl = X * 0.0557 + Y * -0.2040 + Z * 1.0570;
    const enc = v => Math.round(255 * clamp(v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055, 0, 1));
    return `rgb(${enc(r)},${enc(gg)},${enc(bl)})`;
  }
  const hexA = (hex, a) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  };
  const modIdx = (v, n) => ((Math.round(v) % n) + n) % n;

  // ---------------------------------------------------------------- 命中与拖拽
  function hitAnchor(sx, sy) {
    const g = geom();
    let best = null, bd = 9;
    st.anchors.forEach((a, k) => {
      const x = uToX(a.u);
      [[g.ay1, 'a'], [g.by0, 'b']].forEach(([y, side]) => {
        const d = Math.hypot(sx - x, sy - y);
        if (d < bd) { bd = d; best = { k, side }; }
      });
    });
    return best;
  }
  function hitZone(u) {
    for (let i = st.zones.length - 1; i >= 0; i--) {
      const z = st.zones[i];
      if (u >= Math.min(z.u0, z.u1) && u <= Math.max(z.u0, z.u1)) return i;
    }
    return -1;
  }

  function eventPos(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  canvas.addEventListener('mousedown', (e) => {
    if (!st) return;
    const { x, y } = eventPos(e);
    const u = clamp(xToU(x), -0.05, 1.05);
    const g = geom();

    if (e.button === 1 || e.altKey) { drag = { type: 'pan', x, u0: st.view.u0, u1: st.view.u1 }; return; }
    if (e.button === 2) {            // 右键删除内部锚点
      const h = hitAnchor(x, y);
      if (h && h.k > 0 && h.k < st.anchors.length - 1) {
        pushHistory(); st.anchors.splice(h.k, 1); afterEdit();
      }
      return;
    }

    const h = hitAnchor(x, y);
    if (h && (st.mode === 'anchor' || st.mode === 'pan')) {
      pushHistory();
      drag = {
        type: 'anchor', ...h, startX: x,
        startIa: st.anchors[h.k].ia, startIb: st.anchors[h.k].ib,
        startU: st.anchors[h.k].u,
        startIa0: st.anchors[0].ia,
        startIaEnd: st.anchors[st.anchors.length - 1].ia,
        startIb0: st.anchors[0].ib,
        startIbEnd: st.anchors[st.anchors.length - 1].ib,
      };
      canvas.classList.add('dragging');
      return;
    }
    if (st.mode === 'zone' && y > g.ay0 - 4 && y < g.by1 + 4) {
      pushHistory();
      drag = { type: 'zone', u0: clamp(u, 0, 1), u1: clamp(u, 0, 1) };
      st.zones.push(drag_ref_zone(drag));
      canvas.classList.add('dragging');
      return;
    }
    if (st.mode === 'anchor') {
      // 在中间空隙点击：以当前对应插入新锚点并立即开始拖 A 侧
      if (y >= g.ay1 && y <= g.by0 && u >= 0 && u <= 1) {
        pushHistory();
        const m = mapU(u);
        const a = { u: roundU(u), ia: Math.round(m.ia), ib: Math.round(m.ib) };
        st.anchors.push(a); st.anchors.sort((p, q) => p.u - q.u);
        const k = st.anchors.indexOf(a);
        drag = {
          type: 'anchor', k, side: 'a', startX: x,
          startIa: a.ia, startIb: a.ib, startU: a.u,
        };
        canvas.classList.add('dragging');
        return;
      }
    }
    // 默认平移（同步滑动两条带）
    drag = { type: 'pan', x, u0: st.view.u0, u1: st.view.u1 };
    canvas.classList.add('dragging');
  });

  function drag_ref_zone(d) {
    const z = { u0: d.u0, u1: d.u1, kind: $('rv-zone-kind').value, note: '' };
    d.zoneRef = z;
    return z;
  }

  window.addEventListener('mousemove', (e) => {
    if (!st || !drag) return;
    const { x } = eventPos(e);
    if (drag.type === 'pan') {
      const du = (x - drag.x) / pxPerU();
      st.view.u0 = drag.u0 - du; st.view.u1 = drag.u1 - du;
      requestPaint();
    } else if (drag.type === 'anchor') {
      const a = st.anchors[drag.k];
      if (!a) return;
      const du = (x - drag.startX) / pxPerU();
      const isEnd = drag.k === 0 || drag.k === st.anchors.length - 1;
      if (isEnd) {
        // 黄方块：整条接缝在该侧轮廓上的起止点平移（保持接缝弧长不变）
        const n = drag.side === 'a' ? st.fa.contour.length : st.fb.contour.length;
        const key = drag.side === 'a' ? 'ia' : 'ib';
        const span = signedSpan(drag.startIa, drag.startIaEnd, n);
        const start0 = drag.side === 'a' ? drag.startIa0 : drag.startIb0;
        const newStart = start0 + Math.round(du * Math.abs(span || 1));
        st.anchors[0][key] = newStart;
        st.anchors[st.anchors.length - 1][key] = newStart + span;
      } else {
        // 绿菱形：横向移动对应点 u 位置；竖向拖动微调该侧对应
        a.u = clamp(drag.startU + du,
          st.anchors[drag.k - 1].u + 0.005, st.anchors[drag.k + 1].u - 0.005);
        const ev = eventPos(e), g = geom();
        if (ev.y < (g.ay1 + g.by0) / 2) a.ia = drag.startIa + Math.round((ev.y - g.ay1) * 0.12);
        else a.ib = drag.startIb + Math.round((ev.y - g.by0) * 0.12);
      }
      scheduleRecompute();
      requestPaint();
    } else if (drag.type === 'zone') {
      drag.u1 = clamp(xToU(x), 0, 1);
      drag.zoneRef.u1 = drag.u1;
      requestPaint();
    }
  });

  window.addEventListener('mouseup', () => {
    if (!st) return;
    if (drag) {
      if (drag.type === 'zone') {
        const z = drag.zoneRef;
        if (Math.abs(z.u1 - z.u0) < 0.012) {
          st.zones.splice(st.zones.indexOf(z), 1);  // 误点击，不成段
          st.undoStack.pop();
        } else {
          z.u0 = roundU(Math.min(z.u0, z.u1));
          z.u1 = roundU(Math.max(z.u0, z.u1));
          st.selectedZone = st.zones.indexOf(z);
        }
        afterEdit();
      } else if (drag.type === 'anchor') {
        afterEdit();
      }
    }
    drag = null;
    canvas.classList.remove('dragging');
  });
  canvas.addEventListener('contextmenu', e => e.preventDefault());

  canvas.addEventListener('wheel', (e) => {
    if (!st) return;
    e.preventDefault();
    const { x } = eventPos(e);
    if (e.shiftKey) {
      st.depth = clamp(st.depth + (e.deltaY < 0 ? 8 : -8), 20, 160);
      $('rv-depth').value = st.depth; $('rv-depth-val').textContent = st.depth + 'px';
    } else {
      zoomAt(x, e.deltaY < 0 ? 1.12 : 1 / 1.12);
    }
  }, { passive: false });

  canvas.addEventListener('click', (e) => {
    if (!st || st.mode !== 'zone') return;
    const { x } = eventPos(e);
    const zi = hitZone(xToU(x));
    if (zi >= 0) { st.selectedZone = zi; renderZoneList(); requestPaint(); }
  });

  function roundU(u) { return Math.round(u * 1000) / 1000; }

  function clampAnchor(k) {
    const a = st.anchors;
    const nA = st.fa.contour.length;
    if (k > 0) {
      const prev = a[k - 1];
      if (signedSpan(prev.ia, a[k].ia, nA) <= 1) a[k].ia = prev.ia + 2;
      if (signedSpan(prev.ib, a[k].ib, st.fb.contour.length) <= 1) a[k].ib = prev.ib + 2;
    }
    if (k < a.length - 1) {
      const nxt = a[k + 1];
      if (signedSpan(a[k].ia, nxt.ia, nA) <= 1) a[k].ia = nxt.ia - 2;
      if (signedSpan(a[k].ib, nxt.ib, st.fb.contour.length) <= 1) a[k].ib = nxt.ib - 2;
    }
  }

  // ---------------------------------------------------------------- 撤销重做
  function pushHistory() {
    if (!st) return;
    st.undoStack.push(clone({ anchors: st.anchors, zones: st.zones }));
    if (st.undoStack.length > 60) st.undoStack.shift();
    st.redoStack = [];
  }
  function undo() {
    if (!st?.undoStack.length) return;
    st.redoStack.push(clone({ anchors: st.anchors, zones: st.zones }));
    const s = st.undoStack.pop();
    st.anchors = s.anchors; st.zones = s.zones;
    afterEdit();
  }
  function redo() {
    if (!st?.redoStack.length) return;
    st.undoStack.push(clone({ anchors: st.anchors, zones: st.zones }));
    const s = st.redoStack.pop();
    st.anchors = s.anchors; st.zones = s.zones;
    afterEdit();
  }

  function afterEdit() {
    st.dirty = true;
    liveRecompute();
    renderZoneList();
    requestPaint();
    scheduleServer();
    scheduleSave();
  }

  // ---------------------------------------------------------------- 服务端节流复核 / 自动保存
  function scheduleRecompute() {
    st.dirty = true;
    renderSide();
    requestPaint();
    scheduleServer();
    scheduleSave();
  }

  function scheduleServer() {
    clearTimeout(serverTimer);
    setSaveState('busy', '重算中…');
    serverTimer = setTimeout(async () => {
      try {
        const r = await API.post(
          `/api/plans/${App.context().activePlan}/reviews/${st.cid}/recompute`,
          { adjustments: { anchors: st.anchors, zones: st.zones } });
        if (r.ok) {
          st.live.overlap = r.overlap;
          st.live.thick_diff = r.thick_diff ?? st.live.thick_diff;
          st.live.score = scoreOf(st.live);
          st.lastServer = r;
          renderSide();
        }
      } catch (e) { /* 实时值保留 */ }
    }, 350);
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    setSaveState('busy', '未保存');
    saveTimer = setTimeout(() => persist(st.status), 900);
  }

  async function persist(status, { closeAfter = false, snapAfter = false } = {}) {
    const planId = App.context().activePlan;
    setSaveState('busy', '保存中…');
    try {
      const r = await API.put(`/api/plans/${planId}/reviews/${st.cid}`, {
        status, note: st.note,
        adjustments: { anchors: st.anchors, zones: st.zones },
      });
      st.status = status; st.dirty = false;
      st.savedResult = r.result;
      st.lastServer = r.result;
      await App.onReviewSaved(st.cid, r.review, { snap: snapAfter });
      setSaveState('saved', '已保存 ' + new Date().toLocaleTimeString());
      renderSide();
      if (closeAfter) close();
      return true;
    } catch (e) {
      setSaveState('', '保存失败');
      toast('复核保存失败：' + e.message, 3500);
      return false;
    }
  }
  function setSaveState(cls, txt) {
    const el = $('rv-save-state');
    el.className = 'rv-save-state ' + (cls || '');
    el.textContent = txt;
  }

  // ---------------------------------------------------------------- 侧栏渲染
  function renderSide() {
    renderTable();
    renderZoneList();
    renderThickness();
  }

  function ROWS() {
    return [
      { key: 'score', name: '综合分', better: 'up', d: 1 },
      { key: 'contact_mm', name: '有效接触 mm', better: 'up', d: 1 },
      { key: 'rmse', name: '配准偏差 mm', better: 'down', d: 2 },
      { key: 'de', name: '色差 ΔE', better: 'down', d: 1 },
      { key: 'ncc', name: '曲率相关 NCC', better: 'up', d: 2 },
      { key: 'thick_diff', name: '厚度差 mm', better: 'down', d: 2 },
      { key: 'overlap', name: '重叠率', better: 'down', d: 2, pct: true },
      { key: 'valid_ratio', name: '有效区段占比', better: 'up', d: 0, pct: true },
    ];
  }

  function renderTable() {
    if (!st) return;
    const auto = st.auto || {};
    const cur = st.live.ok ? st.live : {};
    const tb = $('rv-table');
    tb.innerHTML = '<tr><th></th><th>自动</th><th>当前</th><th>差异</th></tr>' +
      ROWS().map(row => {
        const a = auto[key(row)];
        const v = cur[key(row)];
        const av = fmtVal(a, row), cv = fmtVal(v, row);
        let delta = '—', cls = '';
        if (Number.isFinite(+a) && Number.isFinite(+v)) {
          const d = +v - +a;
          if (Math.abs(d) >= 0.05) {
            const good = row.better === 'up' ? d > 0 : d < 0;
            cls = good ? 'delta-up' : 'delta-down';
            delta = (d > 0 ? '+' : '') + fmtVal(d, row);
          } else delta = '±0';
        }
        return `<tr><td>${row.name}</td>
          <td class="val auto">${av}</td>
          <td class="val"><b>${cv}</b></td>
          <td class="val ${cls}">${delta}</td></tr>`;
      }).join('');
  }
  function key(row) { return row.key; }
  function fmtVal(v, row) {
    if (v == null || !Number.isFinite(+v)) return '—';
    if (row.pct) return (v * 100).toFixed(0) + '%';
    return (+v).toFixed(row.d);
  }

  function renderZoneList() {
    if (!st) return;
    $('rv-zone-n').textContent = st.zones.length;
    const ul = $('rv-zone-list');
    if (!st.zones.length) {
      ul.innerHTML = '<li style="border-left-color:var(--line);color:var(--muted)">暂无；用“圈异常区段”在条带上拖选</li>';
      return;
    }
    ul.innerHTML = '';
    st.zones.forEach((z, i) => {
      const meta = ZONE_META[z.kind] || ZONE_META.other;
      const li = document.createElement('li');
      li.className = `k-${z.kind}` + (i === st.selectedZone ? ' sel' : '');
      li.innerHTML = `<span>${meta.label}</span>
        <span style="color:var(--muted)">${(Math.min(z.u0, z.u1) * 100).toFixed(0)}–${(Math.max(z.u0, z.u1) * 100).toFixed(0)}%</span>
        <button class="z-del" title="删除">×</button>`;
      li.querySelector('.z-del').onclick = () => {
        pushHistory(); st.zones.splice(i, 1); st.selectedZone = -1; afterEdit();
      };
      ul.appendChild(li);
    });
  }

  function renderThickness() {
    const el = $('rv-thick');
    const f = (x) => x.thickness ? `<b>${x.thickness} mm</b>${x.thickness_note ? '（' + escapeHtml(x.thickness_note) + '）' : ''}` : '未记录';
    el.innerHTML = `厚度 A：${f(st.fa)}<br>厚度 B：${f(st.fb)}`;
  }
  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ---------------------------------------------------------------- 工具栏
  document.querySelectorAll('#rv-layers button').forEach(b => {
    b.onclick = () => {
      const l = b.dataset.layer;
      if (l === 'orig' || l === 'cutout') {
        st.layers.delete('orig'); st.layers.delete('cutout'); st.layers.add(l);
      } else {
        st.layers.has(l) ? st.layers.delete(l) : st.layers.add(l);
      }
      syncLayerButtons(); requestPaint();
    };
  });
  function syncLayerButtons() {
    document.querySelectorAll('#rv-layers button').forEach(b =>
      b.classList.toggle('active', st?.layers.has(b.dataset.layer)));
  }
  document.querySelectorAll('#rv-modes button').forEach(b => {
    b.onclick = () => selectMode(b.dataset.mode);
  });
  function selectMode(mode) {
    if (!st) return;
    st.mode = mode;
    document.querySelectorAll('#rv-modes button').forEach(b =>
      b.classList.toggle('active', b.dataset.mode === mode));
    canvas.classList.toggle('zoning', mode === 'zone');
    canvas.classList.toggle('anchoring', mode === 'anchor');
    const hints = {
      pan: '拖动平移 · 滚轮缩放（Shift+滚轮调内探深度）· 可直接拖黄色起止点与绿色锚点',
      anchor: '在两带之间点击添加对应锚点；拖动 A/B 两侧锚头调整对应；右键删锚点',
      zone: '在条带上横向拖选出不可信区段（磨损/补配/反光），点击区段可在右侧删除',
    };
    $('rv-hint').textContent = hints[mode];
  }

  $('rv-depth').oninput = (e) => {
    st.depth = +e.target.value;
    $('rv-depth-val').textContent = st.depth + 'px';
    requestPaint();
  };
  $('rv-zoom-in').onclick = () => zoomAt(geom().W / 2, 1.3);
  $('rv-zoom-out').onclick = () => zoomAt(geom().W / 2, 1 / 1.3);
  $('rv-fit').onclick = () => { st.view = { u0: -0.03, u1: 1.03 }; requestPaint(); };
  $('rv-reset-anchors').onclick = async () => {
    const data = await API.get(`/api/plans/${App.context().activePlan}/reviews/${st.cid}`);
    pushHistory();
    st.anchors = clone(data.default_adj?.anchors || st.anchors);
    afterEdit();
    toast('已恢复自动接缝起止与对应');
  };

  $('rv-note').oninput = () => {
    st.note = $('rv-note').value;
    st.dirty = true;
    scheduleSave();
  };

  $('rv-undo').onclick = undo;
  $('rv-redo').onclick = redo;
  window.addEventListener('keydown', (e) => {
    if (!st || modal.classList.contains('hidden')) return;
    if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); }
    if (e.key === 'Delete' && st.selectedZone >= 0) {
      pushHistory(); st.zones.splice(st.selectedZone, 1); st.selectedZone = -1; afterEdit();
    }
  });

  $('rv-btn-accept').onclick = async () => {
    if (!(st.live.ok)) return toast('有效接触点过少，不能接受');
    const ok = await persist('accepted', { snapAfter: true, closeAfter: true });
    if (ok) toast('已接受人工校正：画布按修正变换吸附，并已重新参与冲突检查');
  };
  $('rv-btn-pending').onclick = () => persist('pending');
  $('rv-btn-exclude').onclick = async () => {
    const ok = await persist('excluded', { closeAfter: true });
    if (ok) toast('该候选已在本方案中排除');
  };
  $('rv-btn-clear').onclick = async () => {
    if (!confirm('放弃该候选在本方案中的全部人工调整，恢复自动候选？')) return;
    await API.del(`/api/plans/${App.context().activePlan}/reviews/${st.cid}`);
    await App.onReviewReset(st.cid);
    close();
    toast('已恢复为自动候选');
  };
  $('rv-close').onclick = () => {
    if (st?.dirty) { /* 自动保存已触发，直接关闭即可 */ }
    close();
  };
  $('rv-btn-print').onclick = () => printSheet();

  // ---------------------------------------------------------------- 打印核对单
  async function printSheet(cidArg) {
    if (cidArg && (!st || st.cid !== cidArg)) {
      await open(cidArg);
      // 等待条带图像解码（原图/抠图）
      await new Promise(r => setTimeout(r, 700));
    }
    if (!st) return;
    const session = st;
    // 先保存，保证打印的是最新结果
    if (session.dirty) await persist(session.status);
    const { cand, fa, fb, auto, zones, note, status } = session;
    const result = session.savedResult?.ok ? session.savedResult : await serverResult(session);
    const Wpx = 760, Hpx = 170;
    const mkShot = (side) => {
      const cv = document.createElement('canvas');
      cv.width = Wpx; cv.height = Hpx;
      const g2 = cv.getContext('2d');
      const savedView = { ...st.view };
      st.view = { u0: 0, u1: 1 };
      const fakeG = { W: Wpx, H: Hpx, padX: 0, ay0: 0, ay1: Hpx, by0: Hpx, by1: Hpx * 2, ribH: Hpx };
      paintRibbon(side, 0, Hpx, fakeG, g2, Wpx);
      // 打印图上标注异常区段
      st.zones.forEach(z => {
        const x0 = Math.min(z.u0, z.u1) * Wpx, x1 = Math.max(z.u0, z.u1) * Wpx;
        const meta = ZONE_META[z.kind] || ZONE_META.other;
        g2.fillStyle = hexA(meta.color, 0.22);
        g2.fillRect(x0, 0, x1 - x0, Hpx);
        g2.strokeStyle = meta.color; g2.lineWidth = 1.5; g2.setLineDash([6, 4]);
        g2.strokeRect(x0, 1, x1 - x0, Hpx - 2); g2.setLineDash([]);
        g2.fillStyle = meta.color; g2.font = 'bold 12px sans-serif';
        g2.fillText(meta.label, x0 + 4, 16);
      });
      st.view = savedView;
      return cv.toDataURL('image/png');
    };
    const imgA = mkShot('a'), imgB = mkShot('b');
    const app = App.context();
    const planName = app.plans.find(p => p.id === app.activePlan)?.name || '';
    const rows = ROWS();
    const statusLabel = { accepted: '接受', pending: '待定', excluded: '排除' }[status] || '待定';
    const zoneRows = zones.length ? zones.map(z => {
      const meta = ZONE_META[z.kind] || ZONE_META.other;
      return `<tr><td class="kind" style="color:${meta.color}">${meta.label}</td>
        <td>${(Math.min(z.u0, z.u1) * 100).toFixed(0)}% – ${(Math.max(z.u0, z.u1) * 100).toFixed(0)}%</td>
        <td>${escapeHtml(z.note || '')}</td></tr>`;
    }).join('') : '<tr><td colspan="3">无</td></tr>';

    const host = document.getElementById('print-sheet') || (() => {
      const d = document.createElement('div'); d.id = 'print-sheet'; d.hidden = true;
      document.body.appendChild(d); return d;
    })();
    host.innerHTML = `<div class="sheet-page">
      <h2>接缝人工核对单 #${cand.id}</h2>
      <div class="sheet-meta">
        项目：${escapeHtml(app.project.name)} ｜ 方案：${escapeHtml(planName)} ｜
        ${escapeHtml(fa.code)} ⌇ ${escapeHtml(fb.code)} ｜
        裁定：<b>${statusLabel}</b> ｜ 打印时间：${new Date().toLocaleString()}
      </div>
      <div class="sheet-shots">
        <div class="sheet-shot"><img src="${imgA}"><div class="cap">A 侧接缝：${escapeHtml(fa.code)}（边缘内探 ${st.depth}px）</div></div>
        <div class="sheet-shot"><img src="${imgB}"><div class="cap">B 侧接缝：${escapeHtml(fb.code)}</div></div>
      </div>
      <table class="sheet-table">
        <tr><th>指标</th><th>自动结果</th><th>人工校正后</th><th>差异</th></tr>
        ${rows.map(rw => {
          const a = auto[rw.key], v = result?.[rw.key];
          let delta = '—';
          if (Number.isFinite(+a) && Number.isFinite(+v) && Math.abs(v - a) >= 0.05)
            delta = (v > a ? '+' : '') + fmtVal(v - a, rw);
          return `<tr><td>${rw.name}</td><td>${fmtVal(a, rw)}</td>
            <td><b>${fmtVal(v, rw)}</b></td><td>${delta}</td></tr>`;
        }).join('')}
      </table>
      <h3 style="font-size:13px;margin:10px 0 4px">排除 / 不可信区段</h3>
      <table class="sheet-table sheet-zones">
        <tr><th style="width:70px">类型</th><th style="width:130px">接缝位置</th><th>说明</th></tr>
        ${zoneRows}
      </table>
      <h3 style="font-size:13px;margin:10px 0 4px">复核备注</h3>
      <div class="sheet-note">${escapeHtml(note) || ' '}</div>
      <div class="sheet-sign">
        <span>复核人签字 / 日期</span><span>负责人签字 / 日期</span>
      </div>
    </div>`;
    host.hidden = false;
    await new Promise(r => setTimeout(r, 100));
    window.print();
    host.hidden = true;
  }

  async function serverResult(session) {
    const r = await API.post(
      `/api/plans/${App.context().activePlan}/reviews/${session.cid}/recompute`,
      { adjustments: { anchors: session.anchors, zones: session.zones } });
    return r.ok ? r : session.savedResult;
  }

  // ---------------------------------------------------------------- 绘制调度
  function requestPaint() {
    if (rafQueued) return;
    rafQueued = true;
    requestAnimationFrame(() => { rafQueued = false; paint(); });
  }
  const clone = (o) => JSON.parse(JSON.stringify(o));

  return { open, close, printSheet };
})();
