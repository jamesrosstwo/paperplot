// PDF rendering for the designer: document loading (pdf.js 3.11.174), the zoomable, continuously scrolling page stage with figure boxes and the
// live-preview overlay, the stacked context view, and the sidebar thumbnails. Coordinates: PDF points, origin top-left.

import { h, clamp } from './ui.js';

// pdf.js reads back from its own scratch canvases (masks, transparency groups); Chromium then warns
// "Multiple readback operations using getImageData..." on every page. Mark 2D contexts as read-frequently.
(() => {
  const proto = window.HTMLCanvasElement && HTMLCanvasElement.prototype;
  if (!proto || proto.__designerReadback) return;
  const orig = proto.getContext;
  proto.getContext = function (type, opts) {
    if (type === '2d') opts = Object.assign({ willReadFrequently: true }, opts || {});
    return orig.call(this, type, opts);
  };
  proto.__designerReadback = true;
})();


const WORKER = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
export const MIN_SCALE = 0.25;
export const MAX_SCALE = 8;
export const DEFAULT_SCALE = 1.5 * 96 / 72;   // 150% of physical size, the starting zoom in every mode

export function pdfjsAvailable() {
  const lib = window.pdfjsLib;
  if (!lib) return false;
  if (!lib.GlobalWorkerOptions.workerSrc) lib.GlobalWorkerOptions.workerSrc = WORKER;
  return true;
}

export class PdfDoc {
  constructor(doc, url) { this.doc = doc; this.url = url; this.numPages = doc.numPages; this._pages = new Map(); this._sizes = new Map(); }
  page(n) {
    if (!this._pages.has(n)) this._pages.set(n, this.doc.getPage(n));
    return this._pages.get(n);
  }
  async size(n) {
    if (!this._sizes.has(n)) {
      const vp = (await this.page(n)).getViewport({ scale: 1 });
      this._sizes.set(n, { w: vp.width, h: vp.height });
    }
    return this._sizes.get(n);
  }
  destroy() { try { this.doc.destroy(); } catch { /* already gone */ } }
}

/** Load a PDF; resolves to a PdfDoc. Throws Error with .status = 404 when the server has no PDF. */
export async function loadPdf(url) {
  if (!pdfjsAvailable()) throw new Error('pdf.js failed to load from cdnjs (offline?)');
  const task = window.pdfjsLib.getDocument({ url, isEvalSupported: false });
  try {
    return new PdfDoc(await task.promise, url);
  } catch (e) {
    const err = new Error(e && e.message ? e.message : 'Could not load PDF');
    err.status = e && e.status;
    if (e && e.name === 'MissingPDFException') err.status = 404;
    throw err;
  }
}

/**
 * Render page n of pdoc into canvas at cssScale CSS px per PDF point, sharp on HiDPI screens (devicePixelRatio).
 * The canvas keeps its old bitmap until the new one is ready (no flash); a newer call on the same canvas cancels this one.
 * Returns {cssW, cssH} or null when superseded.
 */
export async function renderPage(pdoc, n, canvas, cssScale, { maxPixels = 40e6 } = {}) {
  const page = await pdoc.page(n);
  const base = page.getViewport({ scale: 1 });
  const cssW = base.width * cssScale, cssH = base.height * cssScale;
  let px = cssScale * (window.devicePixelRatio || 1);
  const area = base.width * px * base.height * px;
  if (area > maxPixels) px *= Math.sqrt(maxPixels / area);
  const vp = page.getViewport({ scale: px });
  const off = document.createElement('canvas');
  off.width = Math.max(1, Math.floor(vp.width));
  off.height = Math.max(1, Math.floor(vp.height));
  if (canvas._task) { try { canvas._task.cancel(); } catch { /* finished */ } }
  const token = Symbol('render');
  canvas._token = token;
  const task = page.render({ canvasContext: off.getContext('2d', { alpha: false, willReadFrequently: true }), viewport: vp, background: '#ffffff' });
  canvas._task = task;
  try { await task.promise; } catch (e) {
    if (e && e.name === 'RenderingCancelledException') return null;
    throw e;
  }
  if (canvas._token !== token) return null;
  canvas._task = null;
  canvas.width = off.width; canvas.height = off.height;
  canvas.getContext('2d').drawImage(off, 0, 0);
  canvas.style.width = cssW + 'px'; canvas.style.height = cssH + 'px';
  return { cssW, cssH };
}

// ── includegraphics semantics for the overlay ──
export function parseGraphicsOptions(opts) {
  const s = String(opts || '');
  const get = (k) => { const m = new RegExp('(?:^|,)\\s*' + k + '\\s*=\\s*([^,]+)').exec(s); return m ? m[1].trim() : null; };
  const scale = parseFloat(get('scale'));
  return {
    width: get('width') != null,
    height: get('height') != null || get('totalheight') != null,
    keepaspectratio: /keepaspectratio/.test(s),
    scale: isFinite(scale) && scale > 0 ? scale : null,
    trim: /\b(trim|viewport|bb)\s*=/.test(s),
    angle: get('angle') != null,
  };
}

/**
 * Where the live preview should sit on the page, in PDF points (top-left origin).
 * box: the compiled figure's box; opts: its \includegraphics options; wPt/hPt: the spec's new physical size.
 * width=… fixes the width (height follows the aspect ratio); scale=k (or nothing) uses the natural size × k.
 * The graphic stays horizontally centred on the old box and top-aligned, as LaTeX lays out a centred float.
 */
export function overlayRect(box, opts, wPt, hPt) {
  const o = parseGraphicsOptions(opts);
  let w, h, mode;
  if (!(wPt > 0 && hPt > 0) || o.trim || o.angle) { w = box.w; h = box.h; mode = 'box'; }
  else if (o.width && o.height && !o.keepaspectratio) { w = box.w; h = box.h; mode = 'stretch'; }
  else if (o.width && o.height) { const k = Math.min(box.w / wPt, box.h / hPt); w = wPt * k; h = hPt * k; mode = 'fit'; }
  else if (o.width) { w = box.w; h = box.w * hPt / wPt; mode = 'width'; }
  else if (o.height) { h = box.h; w = box.h * wPt / hPt; mode = 'height'; }
  else { const k = o.scale || 1; w = wPt * k; h = hPt * k; mode = o.scale ? 'scale' : 'natural'; }
  return { x: box.x + (box.w - w) / 2, y: box.y, w, h, mode };
}

/**
 * The center stage: every compiled page stacked in one scrolling column (as in Overleaf), each drawn only while
 * it is within a screen of the viewport. Figure boxes, the overlay, drag-to-pan, ctrl+wheel zoom,
 * click → {page, x, y} in PDF points. pageNum is the page filling most of the viewport; it follows scrolling.
 */
export class PageViewer {
  constructor({ stage, pagesEl, overlayEl, onClick, onChange }) {
    Object.assign(this, { stage, pagesEl, overlayEl, onClick, onChange });
    this.pageEl = pagesEl;           // the app shows / hides the whole stack
    // Default 150% (100% = physical size, 96/72 CSS px per pt). Zoom only changes when the user zooms: no auto-fit
    // on load, resize or figure selection (James, 2026-11-09).
    this.doc = null; this.pageNum = 1; this.scale = DEFAULT_SCALE; this.fit = null;
    this.pages = [];                 // [{n, el, canvas, boxesEl, size, drawn: scale or null, pending}]
    this.boxes = []; this.selected = null; this.ov = null; this.mark = null;
    this._rerender = null; this._markEl = null; this._markT = null; this._pin = null; this._raf = 0;
    this._bindInput();
    const tick = () => { if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = 0; this._trackPage(); this._renderNear(); }); };
    stage.addEventListener('scroll', tick, { passive: true });
    new ResizeObserver(() => { if (this.fit === 'width' && this.doc && !this.pagesEl.classList.contains('hidden')) this.fitWidth(); tick(); }).observe(stage);
    new ResizeObserver(tick).observe(pagesEl);     // shown again after Context / Plot only
  }

  get numPages() { return this.doc ? this.doc.numPages : 0; }
  get size() { const p = this.pages[this.pageNum - 1]; return p ? p.size : { w: 612, h: 792 }; }

  async setDoc(doc, { keepView = false } = {}) {
    const keep = keepView && this.doc ? { page: this.pageNum, sl: this.stage.scrollLeft, st: this.stage.scrollTop } : null;
    this.doc = doc;
    if (!doc) { this._resize(0); return; }
    const sizes = [];
    for (let n = 1; n <= doc.numPages; n++) sizes.push(await doc.size(n));
    if (this.doc !== doc) return;
    this._resize(doc.numPages);
    // existing canvases keep their old bitmap until the new render lands: no flash on recompile
    this.pages.forEach((p, i) => { p.size = sizes[i]; p.drawn = null; p.pending = null; });
    this._pin = null;
    this._applySize(); this._layoutBoxes(); this._layoutOverlay(); this._layoutMark();
    if (keep) {
      this.pageNum = Math.min(keep.page, doc.numPages);
      this.stage.scrollLeft = keep.sl; this.stage.scrollTop = keep.st;
    } else {
      this.pageNum = 1;
      this.stage.scrollTop = 0;
      if (this.fit === 'width') this.fitWidth();
    }
    this._trackPage();
    await this._renderNear();
    this.onChange && this.onChange({ page: this.pageNum, scale: this.scale });
  }

  _resize(count) {
    while (this.pages.length > count) {
      const p = this.pages.pop();
      if (p.el.contains(this.overlayEl)) this.pagesEl.append(this.overlayEl);
      if (p.canvas._task) { try { p.canvas._task.cancel(); } catch { /* finished */ } }
      p.el.remove();
    }
    for (let n = this.pages.length + 1; n <= count; n++) {
      const canvas = h('canvas'), boxesEl = h('div.page-boxes');
      const el = h('div.page', { dataset: { page: n } }, canvas, boxesEl);
      this.pages.push({ n, el, canvas, boxesEl, size: { w: 612, h: 792 }, drawn: null, pending: null });
      this.pagesEl.append(el);
    }
  }

  /** Scroll to the top of page n (the pager, thumbnails, ← / →). */
  async show(n, { keepScroll = false } = {}) {
    if (!this.doc) return;
    n = clamp(n | 0, 1, this.doc.numPages);
    this.pageNum = n;
    if (!keepScroll) this._scrollTo({ top: n === 1 ? 0 : this._top(this.pages[n - 1].el) - 10 }, n);
    this._layoutMark();
    await this._renderNear();
    this.onChange && this.onChange({ page: this.pageNum, scale: this.scale });
  }

  /** el's top edge in the stage's scroll coordinates. */
  _top(el) { return el.getBoundingClientRect().top - this.stage.getBoundingClientRect().top + this.stage.scrollTop; }

  /** Programmatic scroll that lands on `page`: pageNum is that page once the scroll settles there. */
  _scrollTo({ left, top, behavior = 'auto' }, page) {
    const st = this.stage;
    if (top != null) top = clamp(top, 0, Math.max(0, st.scrollHeight - st.clientHeight));
    if (left != null) left = clamp(left, 0, Math.max(0, st.scrollWidth - st.clientWidth));
    this._pin = top != null ? { top, page } : null;
    st.scrollTo({ left: left ?? st.scrollLeft, top: top ?? st.scrollTop, behavior });
  }

  _trackPage() {
    if (!this.pages.length || this.pagesEl.classList.contains('hidden')) return;
    let best = null;
    if (this._pin && Math.abs(this.stage.scrollTop - this._pin.top) < 1.5) best = this._pin.page;
    else {
      const r = this.stage.getBoundingClientRect();
      let bv = -Infinity;
      for (const p of this.pages) {
        const pr = p.el.getBoundingClientRect();
        if (pr.top > r.bottom) break;
        const v = Math.min(pr.bottom, r.bottom) - Math.max(pr.top, r.top);
        if (v > bv + 0.5) { bv = v; best = p.n; }
      }
    }
    if (best && best !== this.pageNum) {
      this.pageNum = best;
      this.onChange && this.onChange({ page: best, scale: this.scale });
    }
  }

  _applySize() {
    const s = this.scale;
    for (const p of this.pages) {
      const w = p.size.w * s + 'px', hh = p.size.h * s + 'px';
      p.el.style.width = w; p.el.style.height = hh;
      p.canvas.style.width = w; p.canvas.style.height = hh;
    }
  }

  /** Draw the pages within one screen of the viewport at the current zoom; free the bitmaps of the others. */
  async _renderNear() {
    const doc = this.doc;
    if (!doc || !this.pages.length || this.pagesEl.classList.contains('hidden')) return;
    const r = this.stage.getBoundingClientRect(), s = this.scale;
    const lo = r.top - r.height, hi = r.bottom + r.height;
    const jobs = [];
    for (const p of this.pages) {
      const pr = p.el.getBoundingClientRect();
      const near = pr.bottom >= lo && pr.top <= hi;
      if (near) {
        if (p.drawn === s || (p.pending && p.pending.doc === doc && p.pending.s === s)) continue;
        const job = { doc, s };
        p.pending = job;
        jobs.push(renderPage(doc, p.n, p.canvas, s)
          .then((res) => { if (res && this.doc === doc && p.pending === job) p.drawn = s; })
          .catch((e) => console.warn('page render failed', e))
          .finally(() => { if (p.pending === job) p.pending = null; }));
      } else if (p.drawn != null || p.pending) {
        if (p.canvas._task) { try { p.canvas._task.cancel(); } catch { /* finished */ } }
        p.canvas._token = null; p.canvas.width = 0; p.canvas.height = 0;
        p.drawn = null; p.pending = null;
      }
    }
    await Promise.all(jobs);
  }

  /** The page under clientY, else the nearest one. */
  _pageAt(clientY) {
    let best = null, bd = Infinity;
    for (const p of this.pages) {
      const r = p.el.getBoundingClientRect();
      const d = clientY < r.top ? r.top - clientY : clientY > r.bottom ? clientY - r.bottom : 0;
      if (d < bd) { bd = d; best = p; }
      if (d === 0 || r.top > clientY) break;
    }
    return best;
  }

  /** Zoom to scale s; anchor = {clientX, clientY} keeps that point fixed (default: stage center). */
  setScale(s, anchor) {
    if (!this.doc || !this.pages.length) return;
    s = clamp(s, MIN_SCALE, MAX_SCALE);
    const r = this.stage.getBoundingClientRect();
    const ax = anchor ? anchor.clientX - r.left : r.width / 2, ay = anchor ? anchor.clientY - r.top : r.height / 2;
    const p = this._pageAt(ay + r.top);
    const pr = p.el.getBoundingClientRect();
    const px = (ax + r.left - pr.left) / this.scale, py = (ay + r.top - pr.top) / this.scale; // PDF pt under anchor
    this.scale = s;
    this._applySize(); this._layoutBoxes(); this._layoutOverlay(); this._layoutMark();
    const pr2 = p.el.getBoundingClientRect();
    this.stage.scrollLeft += (pr2.left - r.left + px * s) - ax;
    this.stage.scrollTop += (pr2.top - r.top + py * s) - ay;
    this._pin = null;
    clearTimeout(this._rerender);
    this._rerender = setTimeout(() => this._renderNear(), 140);
    this.onChange && this.onChange({ page: this.pageNum, scale: this.scale });
  }
  zoomBy(f, anchor) { this.fit = null; this.setScale(this.scale * f, anchor); }
  fitWidth() {      // one-shot (user clicked the zoom label / pressed 0): later resizes do not refit
    if (!this.doc) return;
    this.fit = null;
    const avail = this.stage.clientWidth - 56;
    this.setScale(Math.max(MIN_SCALE, avail / this.size.w));
  }

  /**
   * Scroll so the box (on page box.page, default the current page) is centred; with zoom=true also zoom so it
   * fills ~70% of the stage width.
   */
  focusBox(box, { zoom = false, smooth = false } = {}) {
    if (!box || !this.doc) return;
    const n = clamp((box.page || this.pageNum) | 0, 1, this.pages.length);
    if (zoom) {
      this.fit = null;
      const s = clamp(Math.min((this.stage.clientWidth * 0.7) / box.w, (this.stage.clientHeight * 0.75) / box.h), MIN_SCALE, 5);
      this.setScale(s);
    }
    const r = this.stage.getBoundingClientRect(), pr = this.pages[n - 1].el.getBoundingClientRect();
    const x0 = pr.left - r.left + this.stage.scrollLeft + box.x * this.scale;
    const y0 = pr.top - r.top + this.stage.scrollTop + box.y * this.scale;
    const bw = box.w * this.scale, bh = box.h * this.scale, pad = 12;
    // centred when it fits; a box wider (taller) than the stage starts at its left (top) edge instead of being
    // cut off on both sides
    const left = bw + 2 * pad <= r.width ? x0 + bw / 2 - r.width / 2 : x0 - pad;
    const top = bh + 2 * pad <= r.height ? y0 + bh / 2 - r.height / 2 : y0 - pad;
    this._scrollTo({ left, top, behavior: smooth ? 'smooth' : 'auto' }, n);
    if (n !== this.pageNum) { this.pageNum = n; this.onChange && this.onChange({ page: n, scale: this.scale }); }
    this._renderNear();
  }

  /**
   * Brief highlight of a source line's position (synctex forward): box {page, x, y, w, h} in PDF points (page
   * defaults to the current one). Scrolls it into view (no zoom change) and fades out after `ms`.
   */
  flashMark(box, { ms = 2200, scroll = true } = {}) {
    if (!box || !this.doc) return;
    this.mark = { page: box.page || this.pageNum, x: box.x, y: box.y, w: box.w, h: box.h };
    this._layoutMark(true);
    if (scroll) this.focusBox(this.mark, { smooth: true });
    clearTimeout(this._markT);
    this._markT = setTimeout(() => { this.mark = null; this._layoutMark(); }, ms);
  }

  _layoutMark(restart = false) {
    if (!this._markEl) this._markEl = h('div.pmark.hidden', { 'aria-hidden': 'true' });
    const el = this._markEl, m = this.mark, p = m && this.pages[m.page - 1];
    if (!p) { el.classList.add('hidden'); return; }
    if (el.parentNode !== p.el) p.el.append(el);
    const s = this.scale, pad = 2.5;
    Object.assign(el.style, { left: (m.x - pad) * s + 'px', top: (m.y - pad) * s + 'px', width: (m.w + 2 * pad) * s + 'px', height: (m.h + 2 * pad) * s + 'px' });
    el.classList.remove('hidden');
    if (restart) { el.classList.remove('run'); void el.offsetWidth; el.classList.add('run'); }
  }

  /** boxes: [{fid, box, label, spec}] for any page (each drawn on its own page). */
  setBoxes(boxes, selected) { this.boxes = boxes || []; this.selected = selected; this._layoutBoxes(); }
  setSelected(fid) { this.selected = fid; this._layoutBoxes(); }

  _layoutBoxes() {
    const s = this.scale;
    for (const p of this.pages) {
      p.boxesEl.replaceChildren(...this.boxes.filter((b) => b.box && b.box.page === p.n).map((b) => {
        const el = h('div.pbox' + (b.fid === this.selected ? '.on' : ''), { dataset: { fid: b.fid }, title: b.label },
          h('span.pbox-tag', (b.spec ? '◆ ' : '') + b.label));
        Object.assign(el.style, { left: b.box.x * s + 'px', top: b.box.y * s + 'px', width: b.box.w * s + 'px', height: b.box.h * s + 'px' });
        return el;
      }));
    }
  }

  /** ov: {box (old, pt), rect (new, pt), url, tag, state: 'ok'|'loading'|'failed'} or null to hide. */
  setOverlay(ov) { this.ov = ov; this._layoutOverlay(); }

  _layoutOverlay() {
    const el = this.overlayEl, ov = this.ov;
    const p = ov && ov.box && this.pages[ov.box.page - 1];
    if (!ov || !ov.rect || !p) { el.classList.add('hidden'); return; }
    if (el.parentNode !== p.el) p.el.append(el);
    const s = this.scale;
    const pos = (node, r) => Object.assign(node.style, { left: r.x * s + 'px', top: r.y * s + 'px', width: r.w * s + 'px', height: r.h * s + 'px' });
    const mask = el.querySelector('.ov-mask'), img = el.querySelector('.ov-img'), frame = el.querySelector('.ov-frame');
    // mask the compiled figure (slightly padded) so a smaller preview does not show the old one around it
    const pad = 1.5;
    pos(mask, { x: ov.box.x - pad, y: ov.box.y - pad, w: ov.box.w + 2 * pad, h: ov.box.h + 2 * pad });
    pos(img, ov.rect); pos(frame, ov.rect);
    if (ov.url && img.getAttribute('src') !== ov.url) img.src = ov.url;
    el.querySelector('.ov-tag').textContent = ov.tag || '';
    el.classList.toggle('loading', ov.state === 'loading');
    el.classList.toggle('failed', ov.state === 'failed');
    el.classList.toggle('hidden', !ov.url && ov.state !== 'failed');
  }

  setComparing(on) { this.overlayEl.classList.toggle('comparing', !!on); }

  /** Client coords → {page, x, y} in PDF points, or null between or beside the pages. */
  toPdf(clientX, clientY) {
    const p = this._pageAt(clientY);
    if (!p) return null;
    const pr = p.el.getBoundingClientRect();
    const x = (clientX - pr.left) / this.scale, y = (clientY - pr.top) / this.scale;
    if (x < 0 || y < 0 || x > p.size.w || y > p.size.h) return null;
    return { page: p.n, x, y };
  }

  _bindInput() {
    const st = this.stage;
    st.addEventListener('wheel', (e) => {
      if (!(e.ctrlKey || e.metaKey) || !this.doc || this.pageEl.classList.contains('hidden')) return;
      e.preventDefault();
      this.zoomBy(Math.exp(-e.deltaY * 0.0022), e);
    }, { passive: false });
    let drag = null;
    st.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 && e.button !== 1) return;
      if (e.target.closest('.stage-banner, .empty, button, a, input')) return;
      drag = { x: e.clientX, y: e.clientY, sl: st.scrollLeft, st: st.scrollTop, moved: false, id: e.pointerId, button: e.button, target: e.target };
      if (e.button === 1) e.preventDefault();
    });
    st.addEventListener('pointermove', (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) > 4) { drag.moved = true; st.classList.add('panning'); try { st.setPointerCapture(e.pointerId); } catch { /* ignore */ } }
      if (drag.moved) { st.scrollLeft = drag.sl - dx; st.scrollTop = drag.st - dy; }
    });
    const end = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag; drag = null; st.classList.remove('panning');
      if (d.moved || d.button !== 0 || e.type === 'pointercancel') return;
      if (this.pageEl.classList.contains('hidden') || !this.pageEl.contains(d.target)) return;
      const boxEl = d.target.closest('.pbox');
      const pt = this.toPdf(e.clientX, e.clientY);
      this.onClick && this.onClick({ ...(pt || { page: this.pageNum }), fid: boxEl ? boxEl.dataset.fid : null, event: e });
    };
    st.addEventListener('pointerup', end);
    st.addEventListener('pointercancel', end);
  }
}

/** Render every page of doc stacked into container (context mode). Returns the page elements. */
export async function renderStack(doc, container, scale) {
  const pages = [];
  container.replaceChildren();
  for (let n = 1; n <= doc.numPages; n++) {
    const size = await doc.size(n);
    const c = h('canvas');
    const pg = h('div.page', { style: { width: size.w * scale + 'px', height: size.h * scale + 'px' } }, c);
    container.append(pg);
    pages.push(pg);
  }
  for (let n = 1; n <= doc.numPages; n++) {
    try { await renderPage(doc, n, pages[n - 1].firstChild, scale); } catch (e) { console.warn('context render', e); }
  }
  return pages;
}

/** Sidebar thumbnails with figure boxes. */
export class Thumbs {
  constructor(container, { width = 100, onPage, onBox }) {
    Object.assign(this, { container, width, onPage, onBox });
    this.items = []; this.doc = null; this.boxes = []; this.current = 0; this.selected = null;
    this._io = new IntersectionObserver((ents) => {
      for (const en of ents) if (en.isIntersecting) this._draw(en.target._n);
    }, { root: container, rootMargin: '200px' });
  }

  async setDoc(doc) {
    this._io.disconnect();
    this.doc = doc; this.items = [];
    this.container.replaceChildren();
    if (!doc) return;
    for (let n = 1; n <= doc.numPages; n++) {
      const size = await doc.size(n);
      if (this.doc !== doc) return;
      const sc = this.width / size.w;
      const canvas = h('canvas', { style: { width: this.width + 'px', height: size.h * sc + 'px' } });
      const boxes = h('div.page-boxes');
      const pageEl = h('div.thumb-page', { style: { width: this.width + 'px', height: size.h * sc + 'px' } }, canvas, boxes);
      const el = h('div.thumb', { dataset: { page: n }, title: `Page ${n}` }, pageEl, h('div.thumb-num', n));
      el._n = n;
      el.addEventListener('click', (e) => {
        const b = e.target.closest('.tbox');
        if (b) this.onBox && this.onBox(b.dataset.fid);
        else this.onPage && this.onPage(n);
      });
      this.items.push({ n, el, canvas, boxes, sc, drawn: false });
      this.container.append(el);
      this._io.observe(el);
    }
    this._layoutBoxes(); this.setCurrent(this.current);
  }

  async _draw(n) {
    const it = this.items[n - 1];
    if (!it || it.drawn || !this.doc) return;
    it.drawn = true;
    try { await renderPage(this.doc, n, it.canvas, it.sc); } catch (e) { it.drawn = false; console.warn('thumb', e); }
  }

  setCurrent(n) {
    this.current = n;
    for (const it of this.items) it.el.classList.toggle('on', it.n === n);
    const it = this.items[n - 1];
    if (it) {
      const c = this.container, r = it.el.offsetTop - c.offsetTop;
      if (r < c.scrollTop || r + it.el.offsetHeight > c.scrollTop + c.clientHeight) c.scrollTo({ top: r - 8, behavior: 'smooth' });
    }
  }

  setBoxes(boxes, selected) { this.boxes = boxes || []; this.selected = selected; this._layoutBoxes(); }
  setSelected(fid) { this.selected = fid; this._layoutBoxes(); }

  _layoutBoxes() {
    for (const it of this.items) {
      it.boxes.replaceChildren(...this.boxes.filter((b) => b.box && b.box.page === it.n).map((b) => {
        const el = h('div.tbox' + (b.spec ? '.spec' : '') + (b.fid === this.selected ? '.on' : ''), { dataset: { fid: b.fid }, title: b.label });
        Object.assign(el.style, { left: b.box.x * it.sc + 'px', top: b.box.y * it.sc + 'px', width: Math.max(3, b.box.w * it.sc) + 'px', height: Math.max(3, b.box.h * it.sc) + 'px' });
        return el;
      }));
    }
  }
}
