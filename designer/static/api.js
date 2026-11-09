// Thin client for the designer backend. Every URL used by the frontend lives here (contract section 5).
// All URLs are RELATIVE (no leading '/'): the server injects <base href="<mount point>/"> into index.html, so the app
// works at the server root and behind a reverse proxy under a subpath (e.g. /designer/) alike (CONTRACT section 7).

/** Absolute URL for a path relative to the app's mount point (pdf.js resolves against window.location, not <base>). */
export function abs(path) { return new URL(String(path).replace(/^\/+/, ''), document.baseURI).href; }

export class ApiError extends Error {
  constructor(status, error, detail, body) {
    super(detail ? `${error}: ${detail}` : String(error || `HTTP ${status}`));
    this.status = status;
    this.error = error;
    this.detail = detail || '';
    this.body = body || null;
  }
}

const enc = encodeURIComponent;

async function req(method, url, body, { signal, raw, headers } = {}) {
  const opts = { method, headers: { 'X-Designer': '1', ...(headers || {}) }, signal, cache: 'no-store' };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(abs(url), opts);
  } catch (e) {
    if (e && e.name === 'AbortError') throw e;
    throw new ApiError(0, 'Backend not reachable', `${method} ${url} failed (is the designer server running?)`);
  }
  if (res.status === 401) window.dispatchEvent(new CustomEvent('designer-unauthorized'));
  if (!res.ok) {
    let j = null, text = '';
    try { text = await res.text(); j = JSON.parse(text); } catch { /* not JSON */ }
    const error = (j && (j.error || j.message)) || `HTTP ${res.status} ${res.statusText}`;
    let detail = (j && j.detail) || '';
    if (!j && text) detail = text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 300);
    throw new ApiError(res.status, error, typeof detail === 'string' ? detail : JSON.stringify(detail), j);
  }
  if (raw) return res;
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return res.json();
  const t = await res.text();
  try { return JSON.parse(t); } catch { return t; }
}

/** Normalise list-ish responses: accept a bare array or {key: [...]}. */
export function asList(x, ...keys) {
  if (Array.isArray(x)) return x;
  if (x && typeof x === 'object') {
    for (const k of keys) if (Array.isArray(x[k])) return x[k];
  }
  return [];
}

export const api = {
  config: () => req('GET', 'api/config'),
  fontsCheck: () => req('GET', 'api/fonts/check'),

  specs: async () => asList(await req('GET', 'api/specs'), 'specs'),
  createSpec: (id, template_id) => req('POST', 'api/specs', template_id ? { id, template_id } : { id }),
  getSpec: (id) => req('GET', `api/specs/${enc(id)}`),
  /** The spec plus its file hash (X-Spec-Hash): the version token that saveSpec's compare-and-swap checks. */
  async getSpecVersioned(id) {
    const res = await req('GET', `api/specs/${enc(id)}`, undefined, { raw: true });
    return { spec: await res.json(), hash: res.headers.get('X-Spec-Hash') || null };
  },
  /** PUT the full spec. With baseHash the server refuses (409, body {current, hash}) if the file changed since. */
  saveSpec: (spec, baseHash) => req('PUT', `api/specs/${enc(spec.id)}`, spec, baseHash ? { headers: { 'X-Spec-Base-Hash': baseHash } } : {}),
  deleteSpec: (id) => req('DELETE', `api/specs/${enc(id)}`),
  controls: (theme) => req('GET', 'api/controls' + (theme ? `?theme=${enc(theme)}` : '')),

  /** POST /api/preview → PNG Blob. */
  async preview(spec, signal, dpi) {
    const res = await req('POST', 'api/preview' + (dpi ? `?dpi=${Math.round(dpi)}` : ''), spec, { signal, raw: true });
    const ct = res.headers.get('content-type') || '';
    if (!ct.startsWith('image/')) {
      let j = null; try { j = await res.json(); } catch { /* ignore */ }
      throw new ApiError(res.status, (j && j.error) || 'Preview did not return an image', j && j.detail);
    }
    let warnings = [];
    try { warnings = JSON.parse(decodeURIComponent(res.headers.get('X-Render-Warnings') || '%5B%5D')); } catch { /* none */ }
    let notes = [];
    try { notes = JSON.parse(decodeURIComponent(res.headers.get('X-Render-Notes') || '%5B%5D')); } catch { /* none */ }
    const blob = await res.blob();
    blob.warnings = Array.isArray(warnings) ? warnings : [];
    blob.notes = Array.isArray(notes) ? notes : [];
    return blob;
  },
  render: (id) => req('POST', `api/specs/${enc(id)}/render`, {}),

  compile: () => req('POST', 'api/compile', {}),
  job: (id) => req('GET', `api/jobs/${enc(id)}`),
  pdfUrl: () => abs(`paper.pdf?t=${Date.now()}`),

  figures: () => req('GET', 'api/figures'),
  getTex: (fid) => req('GET', `api/figures/${enc(fid)}/tex`),
  putTex: (fid, text, sha) => req('PUT', `api/figures/${enc(fid)}/tex`, { text, expected_sha: sha, sha }),
  insertFigure: (body) => req('POST', 'api/figures/insert', body),
  context: (fid, text, spec) => req('POST', `api/context/${enc(fid)}`, { ...(text == null ? {} : { text }), ...(spec ? { spec } : {}) }),
  reverse: (page, x, y) => req('POST', 'api/synctex/reverse', { page, x, y }),
  forward: (file, line) => req('POST', 'api/synctex/forward', { file, line }),

  // section editor: whole .tex files reachable from the main file (PUT is compare-and-swap on base_hash; 409 = stale)
  texSections: () => req('GET', 'api/tex/sections'),
  texFile: (path) => req('GET', `api/tex/file?path=${enc(path)}`),
  saveTexFile: (path, text, base_hash) => req('PUT', 'api/tex/file', { path, text, base_hash }),

  datasets: async () => asList(await req('GET', 'api/data'), 'datasets', 'data'),
  addDataset: (body) => req('POST', 'api/data', body),
  getDataset: (name) => req('GET', `api/data/${enc(name)}`),
  removeDataset: (name) => req('DELETE', `api/data/${enc(name)}`),
  previewDataset: (name, rows) => req('GET', `api/data/${enc(name)}/preview` + (rows ? `?rows=${rows}` : '')),

  git: {
    status: () => req('GET', 'api/git/status'),
    fetch: () => req('POST', 'api/git/fetch', {}),
    pull: () => req('POST', 'api/git/pull', {}),
    commit: (paths, message) => req('POST', 'api/git/commit', { paths, message }),
    push: () => req('POST', 'api/git/push', {}),
  },
};
