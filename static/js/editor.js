/* 碎片图像处理弹窗：标尺校准 / 裁剪重分割 / 手工修轮廓。
   全部操作把像素坐标发回后端，由 OpenCV 完成计算。 */
const Editor = (() => {
  const canvas = document.getElementById('editor-canvas');
  const ctx = canvas.getContext('2d');
  const statusEl = document.getElementById('editor-status');
  const optionsEl = document.getElementById('tool-options');
  const modal = document.getElementById('editor-modal');

  let state = null;

  function open(fragment) {
    state = {
      frag: fragment,
      img: null,
      tool: 'scale',
      scalePts: [],
      cropRect: null,
      polygon: (fragment.contour || []).map(p => ({ x: p[0], y: p[1] })),
      drawing: false,
    };
    modal.classList.remove('hidden');
    document.getElementById('editor-title').textContent =
      `碎片 ${fragment.code} — 图像处理`;
    loadImage();
    selectTool('scale');
  }

  function close() {
    modal.classList.add('hidden');
    state = null;
  }

  function loadImage() {
    const url = `/api/img/${state.frag.id}?t=${Date.now()}`;
    const img = new Image();
    img.onload = () => {
      state.img = img;
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      redraw();
    };
    img.src = url;
  }

  function setStatus(s) { statusEl.textContent = s; }

  function selectTool(tool) {
    state.tool = tool;
    document.querySelectorAll('#editor-modal .tool').forEach(b =>
      b.classList.toggle('active', b.dataset.tool === tool));
    state.scalePts = [];
    state.cropRect = null;
    state.drawing = false;
    renderOptions();
    redraw();
  }

  function renderOptions() {
    const f = state.frag;
    if (state.tool === 'scale') {
      optionsEl.innerHTML = `
        在图中沿标尺拖出（点击两个端点），输入该段实际长度：
        <label>长度 mm <input id="scale-mm" type="number" step="0.1" value="100"></label>
        <button class="primary" id="apply-scale">应用校准</button>
        <span class="hint">${f.mm_per_px ? '当前 1 px = ' + f.mm_per_px.toFixed(4) + ' mm' : '尚未校准'}</span>`;
      document.getElementById('apply-scale').onclick = applyScale;
      setStatus('点击标尺两端确定像素长度；可重新点击替换。');
    } else if (state.tool === 'crop') {
      optionsEl.innerHTML = `
        拖出只含碎片的矩形框，然后重新分割：
        <button class="primary" id="apply-crop">按裁剪框去背景</button>
        <button id="clear-crop">清除裁剪框</button>`;
      document.getElementById('apply-crop').onclick = applyCrop;
      document.getElementById('clear-crop').onclick = () => {
        state.cropRect = null; redraw();
      };
      setStatus('在图上按住左键拖出裁剪区域。');
    } else {
      optionsEl.innerHTML = `
        沿碎片边缘点击描绘多边形（首尾自动闭合）：
        <button class="primary" id="apply-polygon">应用轮廓</button>
        <button id="undo-point">撤销一点</button>
        <button id="clear-polygon">清空重画</button>`;
      document.getElementById('apply-polygon').onclick = applyPolygon;
      document.getElementById('undo-point').onclick = () => {
        state.polygon.pop(); redraw();
      };
      document.getElementById('clear-polygon').onclick = () => {
        state.polygon = []; redraw();
      };
      setStatus('点击添加轮廓顶点，鼠标移动预览闭合线。');
    }
  }

  function eventPos(ev) {
    const r = canvas.getBoundingClientRect();
    return {
      x: (ev.clientX - r.left) * canvas.width / r.width,
      y: (ev.clientY - r.top) * canvas.height / r.height,
    };
  }

  canvas.addEventListener('click', (ev) => {
    if (!state || !state.img) return;
    const p = eventPos(ev);
    if (state.tool === 'scale') {
      state.scalePts.push(p);
      if (state.scalePts.length > 2) state.scalePts.shift();
      redraw();
    } else if (state.tool === 'contour') {
      state.polygon.push(p);
      redraw();
    }
  });

  canvas.addEventListener('mousedown', (ev) => {
    if (!state || state.tool !== 'crop') return;
    state.drawing = true;
    const p = eventPos(ev);
    state.cropRect = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
  });
  canvas.addEventListener('mousemove', (ev) => {
    if (!state || !state.drawing) return;
    const p = eventPos(ev);
    state.cropRect.x1 = p.x; state.cropRect.y1 = p.y;
    redraw();
  });
  window.addEventListener('mouseup', () => {
    if (state) state.drawing = false;
  });

  function redraw() {
    if (!state || !state.img) return;
    const { img } = state;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0);
    // 已有抠图叠加半透明绿膜，便于核对
    if (state.frag.cutout_url) {
      // 仅在非轮廓工具轻微提示边缘
      ctx.save();
      ctx.globalAlpha = 0.0;
      ctx.restore();
    }
    if (state.tool === 'scale') {
      const pts = state.scalePts;
      if (pts.length >= 1) {
        ctx.fillStyle = '#ffd35c';
        pts.forEach(p => {
          ctx.beginPath(); ctx.arc(p.x, p.y, 5, 0, 7); ctx.fill();
        });
      }
      if (pts.length === 2) {
        ctx.strokeStyle = '#ffd35c'; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(pts[0].x, pts[0].y);
        ctx.lineTo(pts[1].x, pts[1].y); ctx.stroke();
        const px = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y);
        ctx.fillStyle = '#ffd35c'; ctx.font = '16px sans-serif';
        ctx.fillText(`${px.toFixed(1)} px`, (pts[0].x + pts[1].x) / 2 + 8,
                     (pts[0].y + pts[1].y) / 2);
      }
    } else if (state.tool === 'crop' && state.cropRect) {
      const r = state.cropRect;
      const x = Math.min(r.x0, r.x1), y = Math.min(r.y0, r.y1);
      const w = Math.abs(r.x1 - r.x0), h = Math.abs(r.y1 - r.y0);
      ctx.strokeStyle = '#7fb6e8'; ctx.lineWidth = 2;
      ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = 'rgba(127,182,232,.12)';
      ctx.fillRect(x, y, w, h);
    } else if (state.tool === 'contour') {
      const poly = state.polygon;
      if (poly.length > 1) {
        ctx.strokeStyle = '#7fe08a'; ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(poly[0].x, poly[0].y);
        for (const p of poly.slice(1)) ctx.lineTo(p.x, p.y);
        ctx.stroke();
        if (poly.length > 2) {
          ctx.fillStyle = 'rgba(127,224,138,.10)';
          ctx.beginPath();
          ctx.moveTo(poly[0].x, poly[0].y);
          for (const p of poly.slice(1)) ctx.lineTo(p.x, p.y);
          ctx.closePath(); ctx.fill();
        }
      }
      poly.forEach((p, i) => {
        ctx.fillStyle = i === 0 ? '#ffd35c' : '#7fe08a';
        ctx.beginPath(); ctx.arc(p.x, p.y, 3.5, 0, 7); ctx.fill();
      });
    }
  }

  async function applyScale() {
    const pts = state.scalePts;
    if (pts.length !== 2) return toast('请先点击标尺的两个端点');
    const length = parseFloat(document.getElementById('scale-mm').value);
    if (!(length > 0)) return toast('请输入有效长度（mm）');
    try {
      const r = await API.post(`/api/fragments/${state.frag.id}/calibrate`, {
        length_mm: length, p1: [pts[0].x, pts[0].y], p2: [pts[1].x, pts[1].y],
      });
      state.frag.mm_per_px = r.mm_per_px;
      state.frag.scale_px = r.scale_px; state.frag.scale_mm = length;
      renderOptions();
      toast(`校准完成：1 px = ${r.mm_per_px.toFixed(4)} mm`);
      App.onFragmentChanged();
    } catch (e) { toast(e.message); }
  }

  async function applyCrop() {
    if (!state.cropRect) return toast('请先拖出裁剪框');
    const r = state.cropRect;
    const crop = [
      Math.min(r.x0, r.x1), Math.min(r.y0, r.y1),
      Math.abs(r.x1 - r.x0), Math.abs(r.y1 - r.y0),
    ];
    try {
      setStatus('OpenCV 正在重新分割…');
      const frag = await API.post(`/api/fragments/${state.frag.id}/segment`,
                                  { crop });
      Object.assign(state.frag, frag);
      state.cropRect = null;
      redraw();
      renderOptions();
      setStatus('分割完成，可切换到“修轮廓”核对边缘。');
      toast('去背景已更新');
      App.onFragmentChanged();
    } catch (e) { toast(e.message); setStatus(e.message); }
  }

  async function applyPolygon() {
    if (state.polygon.length < 3) return toast('至少描 3 个点');
    try {
      const frag = await API.post(
        `/api/fragments/${state.frag.id}/correct-contour`,
        { polygon_px: state.polygon.map(p => [p.x, p.y]) });
      Object.assign(state.frag, frag);
      state.polygon = frag.contour.map(p => ({ x: p[0], y: p[1] }));
      redraw();
      toast('轮廓已更新');
      App.onFragmentChanged();
    } catch (e) { toast(e.message); }
  }

  document.getElementById('editor-close').onclick = close;
  document.querySelectorAll('#editor-modal .tool').forEach(b =>
    b.addEventListener('click', () => selectTool(b.dataset.tool)));

  return { open, close };
})();
