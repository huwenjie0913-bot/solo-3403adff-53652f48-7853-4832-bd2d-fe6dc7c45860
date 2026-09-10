/* ===================================================================
   陶瓷碎片修复工作台 — 主逻辑（无限画布、方案、候选、指标、导出）
   画布世界单位 = mm；每个项目由标尺校准得到 mm_per_px 后换算。
=================================================================== */

const App = (() => {
  const board = document.getElementById('board');
  const bctx = board.getContext('2d');

  const S = {
    project: null,
    fragments: new Map(),
    candidates: new Map(),
    reviews: new Map(),          // cid -> 当前方案的复核记录
    plans: [],
    activePlan: null,
    layout: {},                 // {fid: {x,y,rot,flip,placed}} 世界 mm / 度
    decisions: {},              // {cid: {status, note}}
    selected: null,
    activeCandidate: null,
    view: { scale: 4, ox: 0, oy: 0 },   // px per mm 与原点偏移(px)
    images: new Map(),
    dragging: null,
    panning: null,
    dirtyLayout: false,
    undo: [], redo: [],
    overlays: [],               // 装配规划等模块注册的画布叠加层
    canvasTool: null,           // 当前画布标注工具（由装配模块设置）
    ghostCheck: null,           // 装配播放时的碎片幽灵化判断 fn(fid)
    planListeners: [],          // 方案切换后的回调（装配模块重载数据）
  };
  let saveTimer = null;

  // ---------------------------------------------------------------- 工具
  const $ = (id) => document.getElementById(id);

  function frag(id) { return S.fragments.get(+id); }
  function cand(id) { return S.candidates.get(+id); }

  // 候选在当前方案中的裁定（复核保存时已同步 decision，这里仅做兜底）
  function candStatus(c) {
    return S.decisions[c.id]?.status || 'pending';
  }
  // 生效参数：已接受复核用修正后的 ia/ib/R/t/接缝中心
  function effParams(c) {
    const rv = S.reviews.get(c.id);
    if (rv?.status === 'accepted' && rv.result?.ok) {
      const r = rv.result, p = { ...(c.params || {}) };
      return {
        ...p,
        ia: r.ia, ib: r.ib, R: r.R, t: r.t,
        seam_center_a: r.seam_center_a, seam_center_b: r.seam_center_b,
        review_corrected: true,
      };
    }
    return c.params || {};
  }
  function manualScore(c) {
    const rv = S.reviews.get(c.id);
    return rv?.result?.ok ? rv.result.score : null;
  }

  function worldToScreen(x, y) {
    return [x * S.view.scale + S.view.ox, y * S.view.scale + S.view.oy];
  }
  function screenToWorld(x, y) {
    return [(x - S.view.ox) / S.view.scale, (y - S.view.oy) / S.view.scale];
  }

  // ---------------------------------------------------------------- 右栏标签页
  document.querySelectorAll('.tabs .tab').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('.tabs .tab').forEach(b =>
        b.classList.toggle('active', b === btn));
      document.querySelectorAll('.tab-pane').forEach(p =>
        p.classList.toggle('active', p.id === 'pane-' + btn.dataset.tab));
      if (btn.dataset.tab === 'metrics') refreshMetrics(false);
      if (btn.dataset.tab === 'assembly' && window.Assembly) {
        Assembly.onTabShown();
      }
    };
  });

  // ---------------------------------------------------------------- 启动页
  async function loadHome() {
    const list = await API.get('/api/projects');
    const ul = $('project-list');
    ul.innerHTML = '';
    if (!list.length) {
      ul.innerHTML = '<li style="cursor:default;opacity:.6">还没有项目，请新建</li>';
      return;
    }
    for (const p of list) {
      const li = document.createElement('li');
      li.innerHTML = `<div><div class="pj-name">${escapeHtml(p.name)}</div>
        <div class="pj-info">${p.fragments} 件碎片 · ${p.plans} 个方案 · ${p.created_at}</div></div>
        <button>打开</button>`;
      li.onclick = () => openProject(p.id);
      ul.appendChild(li);
    }
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  $('btn-new-project').onclick = async () => {
    const name = prompt('项目名称（如：XX窑址出土青瓷碗 K12）', '新修复项目');
    if (!name) return;
    const r = await API.post('/api/projects', { name });
    openProject(r.id);
  };
  $('btn-back-home').onclick = () => {
    $('workspace').classList.add('hidden');
    $('home').classList.remove('hidden');
    loadHome();
  };

  // ---------------------------------------------------------------- 打开项目
  async function openProject(pid) {
    const p = await API.get(`/api/projects/${pid}`);
    S.project = p;
    S.fragments = new Map(p.fragments.map(f => [f.id, f]));
    S.candidates = new Map(p.candidates.map(c => [c.id, c]));
    S.plans = p.plans;
    S.layout = {}; S.decisions = {}; S.selected = null;
    S.reviews = new Map();
    S.undo = []; S.redo = [];
    $('home').classList.add('hidden');
    $('workspace').classList.remove('hidden');
    $('project-name').value = p.name;
    $('project-meta').textContent =
      `${p.vessel || ''} · ${S.fragments.size} 件碎片`;
    for (const f of S.fragments.values()) preloadImage(f);
    renderFragmentList();
    renderPlanSelect();
    await activatePlan(S.plans.find(x => x.is_active)?.id || S.plans[0]?.id);
    renderCandidates();
    fitView();
  }

  $('project-name').onchange = async () => {
    if (!S.project) return;
    await API.put(`/api/projects/${S.project.id}`,
      { name: $('project-name').value, vessel: S.project.vessel || '', note: '' });
  };

  function preloadImage(f) {
    if (!f.cutout_url || S.images.has(f.id)) return;
    const im = new Image();
    im.src = f.cutout_url + `?t=${f.created_at}`;
    S.images.set(f.id, im);
    im.onload = () => requestDraw();
  }

  // ---------------------------------------------------------------- 上传
  $('btn-upload').onclick = () => $('file-input').click();
  $('upload-zone').ondragover = (e) => { e.preventDefault(); };
  $('upload-zone').ondrop = (e) => {
    e.preventDefault();
    uploadFiles(e.dataTransfer.files);
  };
  $('file-input').onchange = async (e) => {
    await uploadFiles(e.target.files);
    e.target.value = '';
  };

  async function uploadFiles(fileList) {
    if (!fileList || !fileList.length || !S.project) return;
    const prog = $('upload-progress');
    prog.classList.remove('hidden');
    try {
      const side = $('upload-side').value;
      const prefix = $('upload-prefix').value || 'S';
      let done = 0;
      // 分片上传，避免单次请求过大
      const files = [...fileList];
      const batch = 4;
      for (let i = 0; i < files.length; i += batch) {
        const part = files.slice(i, i + batch);
        prog.textContent = `上传与本地分割中… ${Math.min(i + batch, files.length)}/${files.length}`;
        const r = await API.upload(S.project.id, part, side, prefix);
        done += r.created.length;
        if (r.errors.length) console.warn('分割失败：', r.errors);
      }
      const p = await API.get(`/api/projects/${S.project.id}`);
      for (const f of p.fragments) {
        if (!S.fragments.has(f.id)) {
          S.fragments.set(f.id, f);
          preloadImage(f);
          S.layout[f.id] = S.layout[f.id] || defaultLayout(f);
        } else {
          const old = S.fragments.get(f.id);
          Object.assign(old, f);
        }
      }
      prog.textContent = `完成：新增 ${done} 件，共 ${S.fragments.size} 件`;
      setTimeout(() => prog.classList.add('hidden'), 2500);
      renderFragmentList();
      $('project-meta').textContent =
        `${S.project.vessel || ''} · ${S.fragments.size} 件碎片`;
      saveLayoutSoon();
      requestDraw();
    } catch (e) {
      prog.classList.add('hidden');
      toast('上传失败：' + e.message, 4000);
    }
  }

  function defaultLayout(f) {
    // 新碎片按网格排到画布一侧，等待人工摆位
    const n = Object.keys(S.layout).length;
    return {
      x: -(200 + (n % 5) * 120),
      y: -160 + Math.floor(n / 5) * 120,
      rot: 0, flip: false, placed: false,
    };
  }

  // ---------------------------------------------------------------- 碎片清单
  function renderFragmentList() {
    $('frag-count').textContent = S.fragments.size;
    const ul = $('frag-list');
    ul.innerHTML = '';
    for (const f of [...S.fragments.values()].sort((a, b) => a.id - b.id)) {
      const li = document.createElement('li');
      li.className = 'frag-row' + (S.selected === f.id ? ' selected' : '')
        + (f.mm_per_px ? '' : ' uncalibrated');
      const sub = f.mm_per_px
        ? `${(f.features?.area_mm2 ?? 0).toLocaleString()} mm²`
        : '⚠ 未校准';
      li.innerHTML = `
        <div class="frag-thumb">${f.thumb_url ? `<img src="${f.thumb_url}?t=${f.created_at}" alt="">` : ''}</div>
        <div class="frag-info">
          <div class="frag-code">${escapeHtml(f.code)}</div>
          <div class="frag-sub">${sub}${f.thickness ? ' · ' + f.thickness + 'mm' : ''}</div>
        </div>`;
      li.onclick = () => selectFragment(f.id);
      ul.appendChild(li);
    }
  }

  function selectFragment(id) {
    S.selected = id;
    renderFragmentList();
    renderProps();
    requestDraw();
  }

  function onFragmentChanged() {
    renderFragmentList();
    renderProps();
    const f = S.selected ? frag(S.selected) : null;
    if (f) preloadImage(f);
    requestDraw();
  }

  // ---------------------------------------------------------------- 属性面板
  function renderProps() {
    const f = S.selected ? frag(S.selected) : null;
    $('props-empty').classList.toggle('hidden', !!f);
    $('props-body').classList.toggle('hidden', !f);
    if (!f) return;
    $('f-code').value = f.code;
    $('f-thickness').value = f.thickness || '';
    $('f-thickness-note').value = f.thickness_note || '';
    $('f-weight').value = f.weight_g || '';
    $('f-weight-hint').textContent = f.weight_g
      ? '' : (f.features?.area_mm2
        ? `未补录，装配规划按面积×厚度估算约 ${(f.features.area_mm2 * (f.thickness || 5) * 0.0024).toFixed(0)} g`
        : '未补录，装配规划将估算');
    const ft = f.features || {};
    $('f-scale-info').textContent = f.mm_per_px
      ? `校准：${f.scale_mm} mm = ${f.scale_px?.toFixed(0)} px（1px=${f.mm_per_px.toFixed(4)}mm）`
      : '尚未用标尺校准，面积与匹配以像素计，无法参与尺寸匹配。';
    $('f-metrics').innerHTML = `
      <div class="metric-cell"><b>${fmt(ft.area_mm2, 0)}</b><span>面积 mm²</span></div>
      <div class="metric-cell"><b>${fmt(ft.perimeter_mm, 1)}</b><span>周长 mm</span></div>
      <div class="metric-cell"><b>${fmt(f.thickness, 2)}</b><span>厚度 mm</span></div>
      <div class="metric-cell"><b>${(f.contour || []).length}</b><span>轮廓点数</span></div>`;
    const bands = f.color_bands || [];
    $('f-bands').innerHTML = bands.length ? `<h4>边缘色带（Lab 聚类）</h4>` +
      bands.map(b => {
        const [L, a, b2] = b.lab;
        const rgb = labToCss(L, a, b2);
        return `<div class="band-row"><span class="band-swatch" style="background:${rgb}"></span>
          占比 ${(b.ratio * 100).toFixed(0)}% · L ${L} a ${a} b ${b2}</div>`;
      }).join('') : '';
  }

  function labToCss(L, a, b) {
    // 简化 Lab->RGB（仅用于色带预览）
    let Y = (L + 16) / 116, X = a / 500 + Y, Z = Y - b / 200;
    const f3 = t => { t = t > 6 / 29 ? t ** 3 : 3 * (6 / 29) ** 2 * (t - 4 / 29); return t; };
    X = 0.95047 * f3(X); Y = f3(Y); Z = 1.08883 * f3(Z);
    let r = X * 3.2406 + Y * -1.5372 + Z * -0.4986;
    let g = X * -0.9689 + Y * 1.8758 + Z * 0.0415;
    let bl = X * 0.0557 + Y * -0.2040 + Z * 1.0570;
    const enc = c => Math.round(255 * Math.max(0, Math.min(1, c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055)));
    return `rgb(${enc(r)},${enc(g)},${enc(bl)})`;
  }

  $('f-code').onchange = async () => {
    const f = frag(S.selected); if (!f) return;
    await API.put(`/api/fragments/${f.id}`, { code: $('f-code').value });
    f.code = $('f-code').value;
    renderFragmentList();
    renderCandidates();
  };
  async function saveThickness() {
    const f = frag(S.selected); if (!f) return;
    f.thickness = parseFloat($('f-thickness').value) || 0;
    f.thickness_note = $('f-thickness-note').value;
    await API.post(`/api/fragments/${f.id}/thickness`,
      { thickness: f.thickness, note: f.thickness_note });
    renderFragmentList();
  }
  $('f-thickness').onchange = saveThickness;
  $('f-thickness-note').onchange = saveThickness;
  $('f-weight').onchange = async () => {
    const f = frag(S.selected); if (!f) return;
    f.weight_g = parseFloat($('f-weight').value) || 0;
    await API.post(`/api/fragments/${f.id}/weight`, { weight_g: f.weight_g });
    renderProps();
    if (window.Assembly) Assembly.onTabShown();
  };
  $('btn-edit-image').onclick = () => {
    const f = frag(S.selected);
    if (f) Editor.open(f);
  };

  // ---------------------------------------------------------------- 方案
  function renderPlanSelect() {
    const sel = $('plan-select');
    sel.innerHTML = '';
    for (const pl of S.plans) {
      const o = document.createElement('option');
      o.value = pl.id; o.textContent = pl.name;
      o.selected = S.activePlan === pl.id;
      sel.appendChild(o);
    }
  }

  $('plan-select').onchange = () => activatePlan(+$('plan-select').value);

  async function activatePlan(id) {
    if (!id) return;
    S.activePlan = id;
    await API.post(`/api/projects/${S.project.id}/plans/activate`, { plan_id: id });
    const pl = S.plans.find(x => x.id === id);
    S.plans.forEach(x => x.is_active = x.id === id);
    S.layout = { ...(pl.layout || {}) };
    // 给新碎片补默认位姿
    for (const f of S.fragments.values()) {
      if (!S.layout[f.id]) S.layout[f.id] = defaultLayout(f);
    }
    const dec = await API.get(`/api/plans/${id}/decisions`);
    S.decisions = dec;
    const rv = await API.get(`/api/plans/${id}/reviews`);
    S.reviews = new Map((rv.reviews || []).map(r => [r.candidate_id, r]));
    renderPlanSelect();
    renderCandidates();
    renderMetricsPane();
    requestDraw();
    for (const fn of S.planListeners) {
      try { fn(); } catch (e) { console.warn('plan listener', e); }
    }
  }

  $('btn-new-plan').onclick = async () => {
    const name = prompt('新方案名称', `方案 ${String.fromCharCode(65 + S.plans.length)}`);
    if (!name) return;
    pushHistory();
    const r = await API.post(`/api/projects/${S.project.id}/plans`,
      { name, layout: S.layout });
    S.plans.push({ id: r.id, name, is_active: true, layout: S.layout });
    await activatePlan(r.id);
    toast('已另存为新方案');
  };

  $('btn-dup-plan').onclick = async () => {
    const r = await API.post(`/api/plans/${S.activePlan}/duplicate`);
    const p = await API.get(`/api/projects/${S.project.id}`);
    S.plans = p.plans;
    await activatePlan(r.id);
    toast('方案已复制');
  };

  // ---------------------------------------------------------------- 撤销重做
  function pushHistory() {
    S.undo.push(JSON.parse(JSON.stringify(S.layout)));
    if (S.undo.length > 50) S.undo.shift();
    S.redo = [];
  }
  $('btn-undo').onclick = undo;
  $('btn-redo').onclick = redo;
  function undo() {
    if (!S.undo.length) return;
    S.redo.push(JSON.parse(JSON.stringify(S.layout)));
    S.layout = S.undo.pop();
    afterLayoutChange(true);
  }
  function redo() {
    if (!S.redo.length) return;
    S.undo.push(JSON.parse(JSON.stringify(S.layout)));
    S.layout = S.redo.pop();
    afterLayoutChange(true);
  }

  window.addEventListener('keydown', (e) => {
    if (modalOpen()) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA'
        || e.target.tagName === 'SELECT') return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
    if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === 'y'
        || (e.shiftKey && e.key.toLowerCase() === 'z'))) { e.preventDefault(); redo(); return; }
    const l = S.layout[S.selected];
    if (!l) return;
    const step = e.shiftKey ? 15 : 5;
    if (e.key === 'r' || e.key === 'R') { pushHistory(); l.rot = (l.rot + step) % 360; afterLayoutChange(); }
    if (e.key === 'l' || e.key === 'L') { pushHistory(); l.rot = (l.rot - step + 360) % 360; afterLayoutChange(); }
    if (e.key === 'f' || e.key === 'F') { pushHistory(); l.flip = !l.flip; afterLayoutChange(); }
    if (e.key === 'Delete' || e.key === 'Backspace') {
      pushHistory(); l.placed = !l.placed; afterLayoutChange();
      toast(l.placed ? '碎片已放上台面' : '碎片收回库中（位姿保留）');
    }
  });

  function afterLayoutChange(skipHistory = false) {
    if (!skipHistory) S.dirtyLayout = true;
    requestDraw();
    saveLayoutSoon();
    renderMetricsPaneSoon();
  }
  function saveLayoutSoon() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveLayout, 600);
  }
  async function saveLayout() {
    if (!S.activePlan) return;
    await API.put(`/api/plans/${S.activePlan}/layout`, { layout: S.layout });
    const pl = S.plans.find(x => x.id === S.activePlan);
    if (pl) pl.layout = S.layout;
  }

  // ---------------------------------------------------------------- 画布
  function resizeBoard() {
    const wrap = $('canvas-wrap');
    const dpr = window.devicePixelRatio || 1;
    board.width = wrap.clientWidth * dpr;
    board.height = wrap.clientHeight * dpr;
    board.style.width = wrap.clientWidth + 'px';
    board.style.height = wrap.clientHeight + 'px';
    bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    requestDraw();
  }
  window.addEventListener('resize', resizeBoard);

  let drawQueued = false;
  function requestDraw() {
    if (drawQueued) return;
    drawQueued = true;
    requestAnimationFrame(() => { drawQueued = false; draw(); });
  }

  function draw() {
    const wrap = $('canvas-wrap');
    const W = wrap.clientWidth, H = wrap.clientHeight;
    bctx.clearRect(0, 0, W, H);
    drawGrid(W, H);
    drawAcceptedSeams();
    for (const [id, f] of S.fragments) {
      const l = S.layout[id];
      if (!l || l.placed === false) continue;
      drawFragment(f, l);
    }
    for (const fn of S.overlays) {
      try { fn(bctx); } catch (e) { console.warn('overlay', e); }
    }
  }

  function drawGrid(W, H) {
    const g = 10; // 10mm
    const px = g * S.view.scale;
    bctx.strokeStyle = '#23282f'; bctx.lineWidth = 1;
    const startX = ((S.view.ox % px) + px) % px;
    const startY = ((S.view.oy % px) + px) % px;
    bctx.beginPath();
    for (let x = startX; x < W; x += px) { bctx.moveTo(x, 0); bctx.lineTo(x, H); }
    for (let y = startY; y < H; y += px) { bctx.moveTo(0, y); bctx.lineTo(W, y); }
    bctx.stroke();
  }

  function drawFragment(f, l) {
    const [sx, sy] = worldToScreen(l.x, l.y);
    const img = S.images.get(f.id);
    const ghosted = S.ghostCheck ? !!S.ghostCheck(f.id) : false;
    bctx.save();
    if (ghosted) bctx.globalAlpha = 0.12;   // 装配播放：尚未装到的碎片
    bctx.translate(sx, sy);
    bctx.rotate(l.rot * Math.PI / 180);
    bctx.scale(l.flip ? -1 : 1, 1);
    const k = (f.mm_per_px || 0.5) * S.view.scale;
    if (img && f.features?.centroid_px) {
      const [cx, cy] = f.features.centroid_px;
      bctx.drawImage(img, -cx * k, -cy * k, img.naturalWidth * k, img.naturalHeight * k);
    } else if ((f.contour || []).length) {
      // 无图像时用轮廓多边形占位
      const mm = f.mm_per_px || 1;
      bctx.beginPath();
      f.contour.forEach((p, i) => {
        const x = (p[0] - (f.features?.centroid_px?.[0] ?? 0)) * mm,
              y = (p[1] - (f.features?.centroid_px?.[1] ?? 0)) * mm;
        i ? bctx.lineTo(x, y) : bctx.moveTo(x, y);
      });
      bctx.closePath();
      bctx.fillStyle = '#5a6b7a'; bctx.fill();
    }
    if (S.selected === f.id) {
      bctx.strokeStyle = '#e0a85c'; bctx.lineWidth = 2 / Math.max(S.view.scale, .3);
      if (f.contour?.length) {
        const mm = f.mm_per_px || 1;
        const cx = f.features?.centroid_px?.[0] ?? 0;
        const cy = f.features?.centroid_px?.[1] ?? 0;
        bctx.beginPath();
        f.contour.forEach((p, i) => {
          const x = (p[0] - cx) * mm, y = (p[1] - cy) * mm;
          i ? bctx.lineTo(x, y) : bctx.moveTo(x, y);
        });
        bctx.closePath(); bctx.stroke();
      }
    }
    bctx.restore();
    // 编号始终正向显示
    bctx.font = '12px sans-serif';
    bctx.fillStyle = ghosted ? 'rgba(207,214,223,.3)'
      : S.selected === f.id ? '#ffd35c' : '#cfd6df';
    bctx.fillText(f.code, sx + 4, sy - 4);
  }

  function drawAcceptedSeams() {
    for (const c of S.candidates.values()) {
      const d = S.decisions[c.id];
      if (!d || d.status !== 'accepted') continue;
      const corrected = S.reviews.get(c.id)?.status === 'accepted';
      drawSeam(c, corrected ? 'rgba(224,168,92,.95)' : 'rgba(111,174,111,.9)');
    }
    // 待定但刚计算的候选，仅在其两件都选中时高亮提示
  }

  function drawSeam(c, color) {
    const fa = frag(c.frag_a), fb = frag(c.frag_b);
    if (!fa || !fb) return;
    const la = S.layout[c.frag_a], lb = S.layout[c.frag_b];
    if (!la || !lb) return;
    const p = effParams(c);
    if (!p.ia || !p.R) return;
    const pts = p.ia.map((ia, k) => {
      const ib = p.ib[k];
      const pa = fragContourWorld(fa, la, ia);
      const pb = fragContourWorld(fb, lb, ib, p);
      return [(pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2];
    });
    bctx.save();
    bctx.strokeStyle = color; bctx.lineWidth = 2.5;
    bctx.beginPath();
    pts.forEach((q, i) => {
      const [x, y] = worldToScreen(q[0], q[1]);
      i ? bctx.lineTo(x, y) : bctx.moveTo(x, y);
    });
    bctx.stroke();
    bctx.restore();
  }

  function fragContourWorld(f, l, idx) {
    const pt = f.contour[idx % f.contour.length];
    const mm = f.mm_per_px || 1;
    const cx = f.features?.centroid_px?.[0] ?? 0;
    const cy = f.features?.centroid_px?.[1] ?? 0;
    const q = rotatePt((pt[0] - cx) * mm, (pt[1] - cy) * mm, l.rot, l.flip);
    return [l.x + q[0], l.y + q[1]];
  }

  function rotatePt(x, y, deg, flip) {
    if (flip) x = -x;
    const a = deg * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
    return [x * c - y * s, x * s + y * c];
  }

  // 命中检测：屏幕点是否落在某碎片上（用轮廓多边形）
  function hitTest(sx, sy) {
    const [wx, wy] = screenToWorld(sx, sy);
    const ids = [...S.fragments.keys()].reverse(); // 后画的在上
    for (const id of ids) {
      const f = frag(id), l = S.layout[id];
      if (!l || l.placed === false || !(f.contour || []).length) continue;
      // 把世界点逆变换到碎片局部 mm
      const dx = wx - l.x, dy = wy - l.y;
      const a = -l.rot * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
      let lx = dx * c - dy * s, ly = dx * s + dy * c;
      if (l.flip) lx = -lx;
      const mm = f.mm_per_px || 1;
      const cx = f.features?.centroid_px?.[0] ?? 0;
      const cy = f.features?.centroid_px?.[1] ?? 0;
      const px = lx / mm + cx, py = ly / mm + cy;
      if (pointInPolygon(px, py, f.contour)) return id;
    }
    return null;
  }

  function pointInPolygon(x, y, poly) {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if (((yi > y) !== (yj > y)) &&
          (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }

  function modalOpen() {
    return !$('editor-modal').classList.contains('hidden')
      || !$('review-modal').classList.contains('hidden');
  }

  // ---------------------------------------------------------------- 指针交互
  board.addEventListener('mousedown', (e) => {
    if (modalOpen()) return;
    const rect = board.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    // 装配规划等模块的画布标注工具优先
    if (S.canvasTool && S.canvasTool.mousedown
        && S.canvasTool.mousedown(e, sx, sy)) return;
    if (e.button === 1 || e.button === 2 || e.altKey) {
      S.panning = { x: sx, y: sy, ox: S.view.ox, oy: S.view.oy };
      board.classList.add('dragging');
      return;
    }
    const id = hitTest(sx, sy);
    if (id != null) {
      selectFragment(id);
      const l = S.layout[id];
      S.dragging = {
        id, sx, sy,
        wx: l.x, wy: l.y, moved: false,
      };
      pushHistory();
      board.classList.add('dragging');
    } else {
      S.panning = { x: sx, y: sy, ox: S.view.ox, oy: S.view.oy };
      S.selected = null; renderFragmentList(); renderProps(); requestDraw();
      board.classList.add('dragging');
    }
  });

  window.addEventListener('mousemove', (e) => {
    if (modalOpen()) return;
    const rect = board.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    if (S.canvasTool && S.canvasTool.mousemove
        && S.canvasTool.mousemove(e, sx, sy)) return;
    if (!S.dragging && !S.panning) return;
    if (S.panning) {
      S.view.ox = S.panning.ox + (sx - S.panning.x);
      S.view.oy = S.panning.oy + (sy - S.panning.y);
      requestDraw();
    } else if (S.dragging) {
      const d = S.dragging, l = S.layout[d.id];
      const dx = (sx - d.sx) / S.view.scale;
      const dy = (sy - d.sy) / S.view.scale;
      // 沿碎片当前旋转方向拖动
      const a = l.rot * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
      l.x = d.wx + dx * c + dy * s;
      l.y = d.wy - dx * s + dy * c;
      if (Math.abs(sx - d.sx) + Math.abs(sy - d.sy) > 3) d.moved = true;
      if (l.placed === false) l.placed = true;
      maybeSnap(d.id);
      requestDraw();
    }
  });

  window.addEventListener('mouseup', (e) => {
    if (S.canvasTool && S.canvasTool.mouseup && S.canvasTool.mouseup(e)) {
      return;
    }
    if (S.dragging) {
      if (!S.dragging.moved && S.undo.length) S.undo.pop();
      afterLayoutChange(true);
    }
    S.dragging = null; S.panning = null;
    board.classList.remove('dragging');
  });
  board.addEventListener('contextmenu', e => e.preventDefault());

  board.addEventListener('wheel', (e) => {
    if (modalOpen()) return;
    e.preventDefault();
    const rect = board.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const [wx, wy] = screenToWorld(mx, my);
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    S.view.scale = Math.max(0.15, Math.min(60, S.view.scale * factor));
    S.view.ox = mx - wx * S.view.scale;
    S.view.oy = my - wy * S.view.scale;
    updateZoomLabel();
    requestDraw();
  }, { passive: false });

  $('btn-zoom-in').onclick = () => zoomBy(1.2);
  $('btn-zoom-out').onclick = () => zoomBy(1 / 1.2);
  function zoomBy(f) {
    const wrap = $('canvas-wrap');
    const cx = wrap.clientWidth / 2, cy = wrap.clientHeight / 2;
    const [wx, wy] = screenToWorld(cx, cy);
    S.view.scale = Math.max(0.15, Math.min(60, S.view.scale * f));
    S.view.ox = cx - wx * S.view.scale;
    S.view.oy = cy - wy * S.view.scale;
    updateZoomLabel(); requestDraw();
  }
  function updateZoomLabel() {
    $('zoom-label').textContent = Math.round(S.view.scale / 4 * 100) + '%';
  }
  $('btn-fit-view').onclick = fitView;

  function fitView() {
    const placed = Object.entries(S.layout).filter(([id, l]) =>
      S.fragments.has(+id) && l.placed !== false);
    if (!placed.length) {
      S.view.scale = 4; S.view.ox = 100; S.view.oy = 100;
      updateZoomLabel(); requestDraw(); return;
    }
    // 用面积估计范围
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [id, l] of placed) {
      const f = frag(id); const r = fragRadiusMm(f);
      minX = Math.min(minX, l.x - r); maxX = Math.max(maxX, l.x + r);
      minY = Math.min(minY, l.y - r); maxY = Math.max(maxY, l.y + r);
    }
    const wrap = $('canvas-wrap');
    const sx = (maxX - minX) || 100, sy = (maxY - minY) || 100;
    S.view.scale = Math.min(
      (wrap.clientWidth - 80) / sx, (wrap.clientHeight - 80) / sy, 12);
    S.view.scale = Math.max(0.15, S.view.scale);
    S.view.ox = wrap.clientWidth / 2 - (minX + maxX) / 2 * S.view.scale;
    S.view.oy = wrap.clientHeight / 2 - (minY + maxY) / 2 * S.view.scale;
    updateZoomLabel(); requestDraw();
  }
  function fragRadiusMm(f) {
    const b = f.features?.bbox_px;
    if (!b) return 40;
    const mm = f.mm_per_px || 1;
    return Math.max(b[2] - b[0], b[3] - b[1]) / 2 * mm;
  }

  // ---------------------------------------------------------------- 吸附
  const SNAP_MM = 3.0;
  let snapHint = null;
  function maybeSnap(movingId) {
    snapHint = null;
    if (!$('snap-toggle').checked) return;
    const fm = frag(movingId), lm = S.layout[movingId];
    let best = null;
    for (const c of S.candidates.values()) {
      const d = S.decisions[c.id];
      if (d && d.status === 'excluded') continue;
      let other = null, selfIsB = false;
      if (c.frag_a === movingId) other = c.frag_b;
      else if (c.frag_b === movingId) { other = c.frag_a; selfIsB = true; }
      if (other == null || !S.layout[other] || S.layout[other].placed === false) continue;
      // 比较两件接缝中心的当前距离
      const p = effParams(c);
      if (!p.seam_center_a) continue;
      const pa = seamCenterWorld(c.frag_a, c, !selfIsB);
      const pb = seamCenterWorld(c.frag_b, c, selfIsB);
      const dist = Math.hypot(pa[0] - pb[0], pa[1] - pb[1]);
      if (!best || dist < best.dist) best = { c, other, dist, selfIsB };
    }
    if (best && best.dist < 25) {
      snapHint = best;
      if (best.dist < SNAP_MM * 3) snapToCandidate(best.c);
    }
  }

  function seamCenterWorld(fid, c, isMoving) {
    const f = frag(fid), l = S.layout[fid];
    const p = effParams(c);
    const key = fid === c.frag_a ? 'seam_center_a' : 'seam_center_b';
    const center = p[key];
    if (!center) return [l.x, l.y];
    // center 为分析时的图像系 mm 坐标；转为相对质心
    const mm = f.mm_per_px || 1;
    const cx = f.features?.centroid_px?.[0] ?? 0;
    const cy = f.features?.centroid_px?.[1] ?? 0;
    const dx = (center[0] / mm - cx) * mm;
    const dy = (center[1] / mm - cy) * mm;
    const q = rotatePt(dx, dy, l.rot, l.flip);
    return [l.x + q[0], l.y + q[1]];
  }

  function snapToCandidate(c) {
    // 依据候选 R/t，把两件之一摆到另一件旁边：
    // q_b = (b_local) R^T + seam_center_a；再换算到各自布局坐标。
    const la = S.layout[c.frag_a], lb = S.layout[c.frag_b];
    const R = effParams(c).R;
    const p = effParams(c);
    const a0 = p.seam_center_a, b0 = p.seam_center_b;
    // 期望 B 在世界系中的朝向：B 局部旋转 R（相对 A 分析坐标），
    // 这里采用“保持 A 当前位姿、推导 B 位姿”的近似刚体放置。
    const angB = Math.atan2(R[1][0], R[0][0]) * 180 / Math.PI;
    lb.rot = (la.rot + angB + 180) % 360;
    lb.flip = !!la.flip;
    const mmB = frag(c.frag_b).mm_per_px || 1;
    const mma = frag(c.frag_a).mm_per_px || 1;
    const cA = frag(c.frag_a).features?.centroid_px || [0, 0];
    const cB = frag(c.frag_b).features?.centroid_px || [0, 0];
    // A 接缝中心在 A 布局中的世界坐标
    const da = rotatePt((a0[0] - cA[0] * mma), (a0[1] - cA[1] * mma),
                        la.rot, la.flip);
    const seamWX = la.x + da[0], seamWY = la.y + da[1];
    const db = rotatePt((b0[0] - cB[0] * mmB), (b0[1] - cB[1] * mmB),
                        lb.rot, lb.flip);
    lb.x = seamWX - db[0];
    lb.y = seamWY - db[1];
    lb.placed = true;
  }

  $('btn-apply-cand').onclick = () => {
    // 按当前选中候选（候选面板里高亮项）吸附
    const c = S.activeCandidate ? cand(S.activeCandidate) : null;
    if (!c) return toast('请先在右侧“候选拼接”里选择一条候选');
    if (!frag(c.frag_a)?.mm_per_px || !frag(c.frag_b)?.mm_per_px)
      return toast('两件碎片都完成标尺校准后才能按候选吸附');
    pushHistory();
    ensurePlaced(c.frag_a); ensurePlaced(c.frag_b);
    snapToCandidate(c);
    selectFragment(c.frag_b);
    afterLayoutChange();
    toast(`已按候选 #${c.id} 摆放 ${frag(c.frag_b).code}`);
  };
  function ensurePlaced(id) {
    if (!S.layout[id]) S.layout[id] = defaultLayout(frag(id));
    S.layout[id].placed = true;
  }

  // ---------------------------------------------------------------- 匹配
  $('btn-match').onclick = async () => {
    const uncal = [...S.fragments.values()].filter(f => !f.mm_per_px);
    if (uncal.length) {
      if (!confirm(`有 ${uncal.length} 件碎片未做标尺校准，尺寸匹配将跳过它们。仍要继续吗？`)) return;
    }
    const btn = $('btn-match');
    btn.disabled = true; btn.textContent = '本地分析中…';
    try {
      const r = await API.post(`/api/projects/${S.project.id}/match`, {});
      S.candidates = new Map(r.candidates.map(c => [c.id, c]));
      renderCandidates();
      let msg = `找到 ${r.candidates.length} 条候选拼接`;
      if (r.skipped_uncalibrated?.length)
        msg += `；${r.skipped_uncalibrated.length} 件未校准被跳过`;
      toast(msg, 3500);
    } catch (e) {
      toast('匹配失败：' + e.message, 4000);
    } finally {
      btn.disabled = false; btn.textContent = '计算候选拼接';
    }
  };

  // ---------------------------------------------------------------- 候选面板
  function filteredCandidates(filter) {
    return [...S.candidates.values()].filter(c => {
      const status = candStatus(c);
      const rv = S.reviews.get(c.id);
      switch (filter) {
        case 'pending': case 'accepted': case 'excluded':
          return status === filter;
        case 'unreviewed':
          return !rv;
        case 'changed': {
          const ms = manualScore(c);
          return ms != null && Math.abs(ms - c.score) >= 1;
        }
        case 'zones':
          return (rv?.adjustments?.zones?.length || 0) > 0;
        default:
          return true;
      }
    }).sort((a, b) => b.score - a.score);
  }

  function renderCandidates() {
    $('cand-count').textContent = S.candidates.size;
    const ul = $('cand-list');
    ul.innerHTML = '';
    const filter = $('cand-filter').value;
    const list = filteredCandidates(filter);
    if (!list.length) {
      ul.innerHTML = '<div class="empty-hint">暂无候选，点击顶部“计算候选拼接”</div>';
      return;
    }
    for (const c of list) {
      const status = candStatus(c);
      const rv = S.reviews.get(c.id);
      const li = document.createElement('li');
      li.className = `cand-item s-${status}` + (S.activeCandidate === c.id ? ' ring' : '');
      const m = c.metrics || {};
      const ms = manualScore(c);
      const scoreHtml = ms != null
        ? `<span class="cand-score" title="自动分 ${c.score.toFixed(0)} → 人工 ${ms.toFixed(0)}">
             <span class="auto-score">${c.score.toFixed(0)}</span>→<span class="manual">${ms.toFixed(0)}</span></span>`
        : `<span class="cand-score">${c.score.toFixed(0)}</span>`;
      const tag = rv
        ? `<span class="cand-review-tag s-${rv.status}">${{
          accepted: '✓ 复核接受', pending: '复核待定', excluded: '✕ 复核排除'
        }[rv.status] || '已复核'}${rv.adjustments?.zones?.length ? ` · 异常×${rv.adjustments.zones.length}` : ''}</span>`
        : '<span class="cand-review-tag">未复核</span>';
      li.innerHTML = `
        <div class="cand-head">
          <span class="cand-pair">#${c.id} ${escapeHtml(frag(c.frag_a)?.code || c.frag_a)}
            ⌇ ${escapeHtml(frag(c.frag_b)?.code || c.frag_b)}</span>
          ${scoreHtml}
        </div>
        <ul class="cand-reasons">${(c.reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join('')}</ul>
        <div class="cand-actions">
          <button data-s="accepted" class="${status === 'accepted' ? 'on-accept' : ''}">接受</button>
          <button data-s="pending">待定</button>
          <button data-s="excluded" class="${status === 'excluded' ? 'on-exclude' : ''}">排除</button>
        </div>
        <div class="cand-foot">
          <button class="rv-entry primary">🔍 接缝人工复核</button>
          ${tag}
        </div>`;
      li.onclick = (e) => {
        if (e.target.tagName === 'BUTTON') return;
        S.activeCandidate = c.id;
        renderCandidates();
        selectCandidateHighlight(c);
      };
      li.querySelectorAll('.cand-actions button').forEach(b => b.onclick = async (e) => {
        e.stopPropagation();
        await setDecision(c.id, b.dataset.s);
      });
      li.querySelector('.rv-entry').onclick = (e) => {
        e.stopPropagation();
        Review.open(c.id);
      };
      ul.appendChild(li);
    }
  }
  $('cand-filter').onchange = renderCandidates;
  $('btn-print-all-sheets').onclick = async () => {
    const list = filteredCandidates($('cand-filter').value);
    if (!list.length) return toast('当前筛选下没有候选');
    toast(`正在生成 ${list.length} 份核对单…`, 2200);
    for (const c of list) {
      // 逐份打开检查器并触发打印（浏览器会合并到同一打印任务队列）
      await Review.printSheet(c.id);
    }
  };

  function selectCandidateHighlight(c) {
    // 在画布上把两件都选中高亮（选 b 件），并提示接缝
    ensurePlaced(c.frag_a); ensurePlaced(c.frag_b);
    selectFragment(c.frag_b);
    toast(`候选 #${c.id}：${frag(c.frag_a).code} ↔ ${frag(c.frag_b).code}，评分 ${c.score.toFixed(0)}`);
  }

  async function setDecision(cid, status) {
    await API.put(`/api/plans/${S.activePlan}/decisions/${cid}`, { status });
    S.decisions[cid] = { ...(S.decisions[cid] || {}), status };
    renderCandidates();
    requestDraw();
    checkConflictsSoon();
  }

  // ---------------------------------------------------------------- 人工复核回调
  async function onReviewSaved(cid, review, { snap = false } = {}) {
    S.reviews.set(cid, review);
    if (review.status) S.decisions[cid] = { status: review.status, note: review.note || '' };
    renderCandidates();
    const c = cand(cid);
    if (c && review.status === 'accepted') {
      ensurePlaced(c.frag_a); ensurePlaced(c.frag_b);
      if (snap) snapToCandidate(c);   // 用修正后的变换吸附
    }
    requestDraw();
    checkConflictsSoon();
    renderMetricsPaneSoon();
    if (snap) afterLayoutChange();
  }
  async function onReviewReset(cid) {
    S.reviews.delete(cid);
    if (S.decisions[cid]) S.decisions[cid] = { status: 'pending', note: '' };
    renderCandidates();
    requestDraw();
    checkConflictsSoon();
  }

  // ---------------------------------------------------------------- 矛盾提示
  let conflictTimer = null;
  function checkConflictsSoon() {
    clearTimeout(conflictTimer);
    conflictTimer = setTimeout(checkConflicts, 250);
  }
  async function checkConflicts() {
    const accepted = Object.entries(S.decisions)
      .filter(([, d]) => d.status === 'accepted').map(([id]) => +id);
    const banner = $('conflict-banner');
    if (!accepted.length) { banner.classList.add('hidden'); return; }
    try {
      const r = await API.post(`/api/projects/${S.project.id}/conflicts`,
        { accepted_ids: accepted, plan_id: S.activePlan });
      if (r.conflicts.length) {
        banner.innerHTML = '⚠ 检测到互相矛盾的拼接：<br>' +
          r.conflicts.slice(0, 3).map(x => escapeHtml(x.message)).join('<br>');
        banner.classList.remove('hidden');
      } else {
        banner.classList.add('hidden');
      }
      renderMetricsPane(r.conflicts);
    } catch (e) { /* 静默 */ }
  }

  // ---------------------------------------------------------------- 指标
  let metricsTimer = null;
  function renderMetricsPaneSoon() {
    clearTimeout(metricsTimer);
    metricsTimer = setTimeout(() => refreshMetrics(false), 400);
  }
  $('btn-refresh-metrics').onclick = () => refreshMetrics(true);

  async function refreshMetrics(showToast) {
    const accepted = Object.entries(S.decisions)
      .filter(([, d]) => d.status === 'accepted').map(([id]) => +id);
    try {
      const [m, cf] = await Promise.all([
        API.post(`/api/projects/${S.project.id}/metrics`,
          { layout: S.layout, accepted_ids: accepted, plan_id: S.activePlan }),
        accepted.length
          ? API.post(`/api/projects/${S.project.id}/conflicts`,
            { accepted_ids: accepted, plan_id: S.activePlan })
          : Promise.resolve({ conflicts: [] }),
      ]);
      const body = $('metrics-body');
      body.innerHTML = `
        <div class="metrics-grid">
          <div class="metric-cell"><b>${m.placed_count}/${m.total_count}</b><span>已上台碎片</span></div>
          <div class="metric-cell"><b>${(m.coverage_of_hull * 100).toFixed(1)}%</b><span>凸包覆盖率</span></div>
          <div class="metric-cell"><b>${fmt(m.placed_area_mm2, 0)}</b><span>碎片面积和 mm²</span></div>
          <div class="metric-cell"><b>${fmt(m.assembly_hull_mm2, 0)}</b><span>复原外轮廓 mm²</span></div>
          <div class="metric-cell"><b>${fmt(m.unplaced_edge_mm, 1)}</b><span>未归位边缘 mm</span></div>
          <div class="metric-cell"><b>${(m.unplaced_edge_ratio * 100).toFixed(0)}%</b><span>未归位边占比</span></div>
        </div>
        <h4>方案一致性</h4>
        ${cf.conflicts.length
          ? cf.conflicts.map(x => `<div class="conflict-line">⚠ ${escapeHtml(x.message)}</div>`).join('')
          : '<div class="conflict-line ok">已接受候选之间未发现矛盾</div>'}
        <p class="hint">覆盖率 = 碎片面积之和 / 已摆放碎片外凸包面积；未归位边缘 = 未被已接受接缝占用的轮廓长度。</p>`;
      if (showToast) toast('指标已刷新');
    } catch (e) {
      if (showToast) toast('指标计算失败：' + e.message);
    }
  }
  function renderMetricsPane(conflicts) {
    if ($('pane-metrics').classList.contains('active') || conflicts) refreshMetrics(false);
  }

  // ---------------------------------------------------------------- 导出
  $('btn-export').onclick = exportPrint;

  function exportPrint() {
    // 在离屏画布上重绘：碎片 + 编号 + 已接受接缝及置信度 + 比例尺
    const placed = Object.entries(S.layout)
      .filter(([id, l]) => S.fragments.has(+id) && l.placed !== false);
    if (!placed.length) return toast('画布上还没有碎片');
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [id, l] of placed) {
      const r = fragRadiusMm(frag(id));
      minX = Math.min(minX, l.x - r); maxX = Math.max(maxX, l.x + r);
      minY = Math.min(minY, l.y - r); maxY = Math.max(maxY, l.y + r);
    }
    const pad = 60, scalePxMm = 6;   // 出图 6px/mm
    const W = (maxX - minX) * scalePxMm + pad * 2;
    const H = (maxY - minY) * scalePxMm + pad * 2 + 90;
    const cnv = document.createElement('canvas');
    cnv.width = W; cnv.height = H;
    const g = cnv.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, W, H);
    const T = (x, y) => [(x - minX) * scalePxMm + pad, (y - minY) * scalePxMm + pad];

    for (const [id, l] of placed) {
      const f = frag(id), img = S.images.get(id);
      const [ox, oy] = T(l.x, l.y);
      g.save(); g.translate(ox, oy); g.rotate(l.rot * Math.PI / 180);
      g.scale(l.flip ? -1 : 1, 1);
      const k = (f.mm_per_px || 0.5) * scalePxMm;
      if (img && f.features?.centroid_px) {
        const [cx, cy] = f.features.centroid_px;
        g.drawImage(img, -cx * k, -cy * k, img.naturalWidth * k, img.naturalHeight * k);
      }
      g.restore();
      g.fillStyle = '#000'; g.font = 'bold 15px sans-serif';
      g.fillText(f.code, ox + 4, oy - 6);
    }
    // 接缝与置信度
    for (const c of S.candidates.values()) {
      if (S.decisions[c.id]?.status !== 'accepted') continue;
      const p = effParams(c);
      if (!p.ia || !p.R) continue;
      // 用接缝中心两件世界位置的中点折线近似（与屏幕 drawSeam 相同策略）
      const la = S.layout[c.frag_a], lb = S.layout[c.frag_b];
      g.strokeStyle = 'rgba(40,120,60,.8)'; g.lineWidth = 2;
      g.beginPath();
      p.ia.forEach((ia, k) => {
        const qa = contourPointWorld(frag(c.frag_a), la, ia);
        const qb = contourPointWorld(frag(c.frag_b), lb, p.ib[k]);
        const [qx, qy] = T((qa[0] + qb[0]) / 2, (qa[1] + qb[1]) / 2);
        k ? g.lineTo(qx, qy) : g.moveTo(qx, qy);
      });
      g.stroke();
      const ca = T(...seamCenterWorld(c.frag_a, c, false));
      g.fillStyle = '#1f6b39'; g.font = '12px sans-serif';
      g.fillText(`接缝#${c.id} 置信度 ${c.score.toFixed(0)}`, ca[0], ca[1] - 8);
    }
    // 比例尺
    const barMm = niceBarMm((maxX - minX) / 6);
    g.strokeStyle = '#000'; g.lineWidth = 3;
    g.beginPath(); g.moveTo(pad, H - 50); g.lineTo(pad + barMm * scalePxMm, H - 50); g.stroke();
    g.fillStyle = '#000'; g.font = '15px sans-serif';
    g.fillText(`${barMm} mm`, pad, H - 58);
    g.font = '13px sans-serif'; g.fillStyle = '#444';
    g.fillText(`${S.project.name} · 方案 ${S.plans.find(x => x.id === S.activePlan)?.name || ''} · ${new Date().toLocaleString()}`, pad, H - 18);

    cnv.toBlob(blob => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `复原图_${S.project.name}_方案${S.activePlan}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      // 同时开新窗口供打印
      const url = cnv.toDataURL('image/png');
      const w = window.open('', '_blank');
      if (w) {
        w.document.write(`<html><head><title>复原图打印</title>
          <style>@page{size:A4 landscape;margin:10mm}body{margin:0;text-align:center}
          img{max-width:100%;max-height:92vh}</style></head>
          <body><img src="${url}" onload="setTimeout(()=>window.print(),300)"></body></html>`);
      }
    }, 'image/png');
  }

  function contourPointWorld(f, l, idx) {
    const pt = f.contour[idx % f.contour.length];
    const mm = f.mm_per_px || 1;
    const c = f.features?.centroid_px || [0, 0];
    const q = rotatePt((pt[0] - c[0]) * mm, (pt[1] - c[1]) * mm, l.rot, l.flip);
    return [l.x + q[0], l.y + q[1]];
  }
  function niceBarMm(target) {
    const steps = [10, 20, 25, 50, 100, 200, 250, 500, 1000];
    return steps.find(s => s >= target) || 1000;
  }

  // ---------------------------------------------------------------- 初始化
  resizeBoard();
  loadHome();
  updateZoomLabel();

  return {
    onFragmentChanged,
    context: () => ({
      project: S.project, fragments: S.fragments, candidates: S.candidates,
      reviews: S.reviews, plans: S.plans, activePlan: S.activePlan,
      layout: S.layout, decisions: S.decisions,
    }),
    onReviewSaved,
    onReviewReset,
    // —— 装配规划模块钩子 ——
    registerOverlay: (fn) => S.overlays.push(fn),
    setCanvasTool: (tool) => {
      S.canvasTool = tool;
      board.style.cursor = tool ? 'crosshair' : '';
    },
    getCanvasTool: () => S.canvasTool,
    setGhostCheck: (fn) => { S.ghostCheck = fn; requestDraw(); },
    onPlanChanged: (fn) => S.planListeners.push(fn),
    worldToScreen, screenToWorld, requestDraw,
    contourPointWorld, fragRadiusMm, rotatePt, effParams, hitTest,
    imageFor: (id) => S.images.get(+id),
    selectFragment,
    saveLayoutNow: saveLayout,
    escapeHtml,
  };
})();
