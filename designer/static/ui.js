// Small DOM/UI helpers shared by the designer modules: element builder, toasts, modals, menus, storage, splitters.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** h('div.cls#id', {attrs, on: {click}}, ...children) */
export function h(tag, props, ...kids) {
  const m = /^([a-z0-9-]+)?((?:[.#][\w-]+)*)$/i.exec(tag) || [];
  const node = document.createElement(m[1] || 'div');
  (m[2] || '').replace(/([.#])([\w-]+)/g, (_, t, v) => { if (t === '.') node.classList.add(v); else node.id = v; return ''; });
  if (props && (typeof props !== 'object' || props instanceof Node || Array.isArray(props))) { kids.unshift(props); props = null; }
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      else if (k === 'class') node.className += ' ' + v;
      else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k in node && typeof v !== 'string') node[k] = v;
      else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected') node[k] = v;
      else node.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

export function debounce(fn, ms) {
  let t = null;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => { t = null; fn(...a); }, ms); };
  d.cancel = () => { clearTimeout(t); t = null; };
  d.flush = (...a) => { if (t) { clearTimeout(t); t = null; fn(...a); } };
  d.pending = () => t != null;
  return d;
}

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// Per-viewer conveniences only (panel sizes, collapsed groups...). Always optional.
export const store = {
  get(k, dflt) { try { const v = localStorage.getItem('pd.' + k); return v == null ? dflt : JSON.parse(v); } catch { return dflt; } },
  set(k, v) { try { localStorage.setItem('pd.' + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

export function fmtBytes(n) {
  if (n == null || isNaN(n)) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + ' ' + u[i];
}

export function fmtTime(s) {
  if (!s) return '';
  const d = new Date(typeof s === 'number' ? (s < 1e12 ? s * 1000 : s) : s);
  if (isNaN(d)) return String(s);
  const pad = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function errText(e) {
  if (!e) return 'Unknown error';
  if (e.error || e.detail) return { title: String(e.error || 'Error'), detail: e.detail || '' };
  return { title: e.message || String(e), detail: '' };
}

// ── toasts ──
export function toast(title, { kind = 'info', detail = '', ms } = {}) {
  const root = $('#toasts');
  const close = () => { node.classList.add('out'); setTimeout(() => node.remove(), 220); };
  const node = h('div.toast.' + kind,
    h('div.t-body', h('div.t-title', title), detail ? h('div.t-detail', detail) : null),
    h('button', { title: 'Dismiss', on: { click: close } }, '×'));
  root.prepend(node);
  while (root.children.length > 5) root.lastChild.remove();
  const life = ms ?? (kind === 'err' ? 9000 : kind === 'warn' ? 6000 : 3200);
  if (life > 0) setTimeout(close, life);
  return close;
}
export function toastError(e, prefix) {
  if (e && e.name === 'AbortError') return;
  const t = errText(e);
  toast(prefix ? `${prefix}: ${t.title}` : t.title, { kind: 'err', detail: t.detail });
  console.warn(prefix || 'error', e);
}

// ── modal ──
/**
 * modal({title, body: Node|string, actions:[{label, kind:'primary'|'danger', value, onClick}], wide})
 * Resolves with the clicked action's value (or null on cancel/Escape). onClick may return false to keep it open,
 * or a Promise (button shows busy state; rejecting keeps the dialog open).
 */
export function modal({ title, body, actions = [{ label: 'OK', kind: 'primary', value: true }], wide = false, cancel = 'Cancel', init } = {}) {
  return new Promise((resolve) => {
    const prevFocus = document.activeElement;
    const back = h('div.modal-back');
    const done = (v) => { back.remove(); document.removeEventListener('keydown', onKey, true); if (prevFocus && prevFocus.focus) prevFocus.focus(); resolve(v); };
    const btns = [];
    if (cancel) btns.push(h('button.btn', { type: 'button', on: { click: () => done(null) } }, cancel));
    for (const a of actions) {
      const b = h('button.btn' + (a.kind === 'primary' ? '.btn-primary' : '') + (a.kind === 'danger' ? '.btn-danger.btn-primary' : ''), { type: 'button' }, a.label);
      b.addEventListener('click', async () => {
        if (!a.onClick) return done(a.value ?? a.label);
        b.disabled = true; const old = b.textContent; b.textContent = a.busy || old;
        try {
          const r = await a.onClick();
          if (r === false) { b.disabled = false; b.textContent = old; return; }
          done(r === undefined ? (a.value ?? a.label) : r);
        } catch (e) { b.disabled = false; b.textContent = old; toastError(e); }
      });
      btns.push(b);
    }
    const dlg = h('div.modal' + (wide ? '.wide' : ''), { role: 'dialog', 'aria-modal': 'true' },
      h('div.modal-head', title), h('div.modal-body', typeof body === 'string' ? h('p', body) : body), h('div.modal-foot', btns));
    back.append(dlg);
    back.addEventListener('mousedown', (e) => { if (e.target === back) done(null); });
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); done(null); }
      else if (e.key === 'Enter' && !e.shiftKey && e.target.tagName !== 'TEXTAREA' && e.target.tagName !== 'BUTTON') {
        const primary = btns.find((b) => b.classList.contains('btn-primary') && !b.classList.contains('btn-danger'));
        if (primary) { e.preventDefault(); e.stopPropagation(); primary.click(); }
      }
    };
    document.addEventListener('keydown', onKey, true);
    $('#modal-root').append(back);
    if (init) init(dlg);
    // never start on a destructive button (Enter on a focused button clicks it): last safe one instead
    const safe = btns.filter((b) => !b.classList.contains('btn-danger'));
    const first = dlg.querySelector('input:not([type=checkbox]), select, textarea') || safe[safe.length - 1] || btns[btns.length - 1];
    setTimeout(() => first && first.focus(), 0);
  });
}

export const confirmDialog = (title, text, label = 'OK', kind = 'primary') =>
  modal({ title, body: typeof text === 'string' ? h('p', text) : text, actions: [{ label, kind, value: true }] }).then((v) => v === true);

// ── popover menus ──
let openPop = null;
export function closePopover() { if (openPop) { openPop.remove(); openPop = null; } }
export function popover(anchor, content, { cls = '', align = 'left' } = {}) {
  closePopover();
  const pop = h('div.popover' + (cls ? '.' + cls : ''), content);
  $('#popover-root').append(pop);
  const r = anchor.getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let x = align === 'right' ? r.right - pw : r.left;
  let y = r.bottom + 4;
  x = clamp(x, 6, window.innerWidth - pw - 6);
  if (y + ph > window.innerHeight - 6) y = Math.max(6, r.top - ph - 4);
  pop.style.left = x + 'px'; pop.style.top = y + 'px';
  openPop = pop;
  setTimeout(() => {
    const off = (e) => { if (!pop.contains(e.target) && e.target !== anchor) { closePopover(); document.removeEventListener('mousedown', off, true); } };
    document.addEventListener('mousedown', off, true);
  }, 0);
  return pop;
}
export function menu(anchor, items, opts) {
  const nodes = items.map((it) => it === '-' ? h('div.sep') :
    h('button.mi' + (it.danger ? '.danger' : ''), { type: 'button', disabled: !!it.disabled, on: { click: () => { closePopover(); it.onClick(); } } }, it.label));
  return popover(anchor, nodes, opts);
}

// ── splitters: drag to resize panels, sizes kept per viewer ──
export function makeSplitter(handle, { cssVar, axis, min, max, invert = false, onEnd, target = document.documentElement, unit = 'px', getBase }) {
  handle.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const start = axis === 'x' ? e.clientX : e.clientY;
    const base = getBase();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add('drag'); document.body.classList.add('resizing');
    const move = (ev) => {
      const d = (axis === 'x' ? ev.clientX : ev.clientY) - start;
      const v = clamp(base + (invert ? -d : d), min(), max());
      target.style.setProperty(cssVar, v + unit);
    };
    const up = () => {
      handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); handle.removeEventListener('pointercancel', up);
      handle.classList.remove('drag'); document.body.classList.remove('resizing');
      onEnd && onEnd(parseFloat(getComputedStyle(target).getPropertyValue(cssVar)));
    };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up); handle.addEventListener('pointercancel', up);
  });
}

/** Strip TeX markup from a caption for one-line display. */
export function plainTex(s) {
  if (!s) return '';
  return String(s)
    .replace(/(^|[^\\])%.*$/gm, '$1')
    .replace(/\\(label|ref|eqref|autoref|cref|Cref)\{[^}]*\}/g, (m, c) => c === 'label' ? '' : '[ref]')
    .replace(/\\cite[pt]?\*?(\[[^\]]*\])?\{[^}]*\}/g, '[cite]')
    .replace(/~/g, ' ')
    .replace(/\\(textbf|textit|emph|texttt|textsc|mathrm|mathbf|text|underline)\{([^{}]*)\}/g, '$2')
    .replace(/\$([^$]*)\$/g, '$1')
    .replace(/\\[a-zA-Z]+\*?/g, '')
    .replace(/[{}\\]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}
