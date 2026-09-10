/* 极简 fetch 封装，所有请求均指向本机 Flask 服务 */
const API = (() => {
  async function req(method, url, body) {
    const opt = { method, headers: {} };
    if (body instanceof FormData) {
      opt.body = body;
    } else if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    const res = await fetch(url, opt);
    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON */ }
    if (!res.ok) {
      throw new Error((data && data.error) || `请求失败 ${res.status}`);
    }
    return data;
  }
  return {
    get: (u) => req('GET', u),
    post: (u, b) => req('POST', u, b ?? {}),
    put: (u, b) => req('PUT', u, b ?? {}),
    del: (u) => req('DELETE', u),
    upload(pid, files, side, prefix) {
      const fd = new FormData();
      for (const f of files) fd.append('files', f);
      fd.append('side', side);
      fd.append('code_prefix', prefix);
      return req('POST', `/api/projects/${pid}/fragments`, fd);
    },
  };
})();

function toast(msg, ms = 2600) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function fmt(n, digits = 1) {
  return Number.isFinite(+n) ? (+n).toFixed(digits) : '—';
}
