// Right-panel editors: the style controls generated from GET /api/controls, and the spec data editor
// (inline levels/arms/k-n counts, or dataset + query mapping).

import { h, store, clamp, plainTex } from './ui.js';

// fallback when GET /api/controls carries no `groups` (spec.GROUPS is the reference order)
const GROUP_ORDER = ['Theme', 'Size', 'Layout', 'Panels', 'Bars', 'Lines', 'Order', 'Axes', 'Legend', 'Error bars', 'Colors'];

// Sidebar groups (controls, Data, Last render) start closed; a group the viewer opened stays open (per browser).
const GROUPS_KEY = 'groups.open';
export function groupCollapsed(name) { return !store.get(GROUPS_KEY, {})[name]; }
export function rememberGroup(name, collapsed) {
  const open = store.get(GROUPS_KEY, {});
  if (collapsed) delete open[name]; else open[name] = true;
  store.set(GROUPS_KEY, open);
}
/** A collapsible sidebar group: head (click toggles) + body, closed unless the viewer opened it before. */
export function collapsibleGroup(name, headChildren, body) {
  const grp = h('div.cgroup' + (groupCollapsed(name) ? '.collapsed' : ''), { dataset: { group: name } },
    h('div.cgroup-head', { on: { click: () => {
      grp.classList.toggle('collapsed');
      rememberGroup(name, grp.classList.contains('collapsed'));
    } } }, ...headChildren),
    body);
  return grp;
}
/** Open (false) or close (true) every sidebar group; remembered like a click on each. */
export function setAllGroups(collapsed) {
  document.querySelectorAll('#right .cgroup').forEach((g) => {
    g.classList.toggle('collapsed', collapsed);
    if (g.dataset.group) rememberGroup(g.dataset.group, collapsed);
  });
}
const ARM_ORDER_MODES = [['as_listed', 'As listed'], ['pooled', 'Pooled rate (best first)'], ['mean', 'Mean rate (best first)'], ['manual', 'Manual']];
const GREEK = { alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', theta: 'θ', lambda: 'λ', mu: 'μ', pi: 'π', rho: 'ρ', sigma: 'σ', tau: 'τ', phi: 'φ', psi: 'ψ', omega: 'ω' };
/** One-line plain label for an arm / legend name written in TeX ("$\mathrm{\pi}_{0.5}$ (base)" -> "π0.5 (base)"). */
export function armLabel(s) {
  const t = String(s ?? '').replace(/\\(alpha|beta|gamma|delta|epsilon|theta|lambda|mu|pi|rho|sigma|tau|phi|psi|omega)(?![A-Za-z])/g, (m, g) => GREEK[g]);
  return plainTex(t).replace(/[_^]/g, '') || String(s ?? '');
}

/** (k, n, dup) of a count as written in a spec ([k, n], [k, n, {dup}], {k, n, dup}) or null. Mirrors spec.count_parts. */
export function countParts(c) {
  if (c == null) return null;
  if (Array.isArray(c)) return c.length >= 2 ? { k: c[0], n: c[1], dup: !!(c[2] && c[2].dup) } : null;
  if (typeof c === 'object' && 'k' in c && 'n' in c) return { k: c.k, n: c.n, dup: !!c.dup };
  return null;
}

/** A count in the same written form as `old` (list or {k, n}), with the dup flag set or cleared. */
export function makeCount(k, n, dup, old) {
  if (old && !Array.isArray(old) && typeof old === 'object') { const o = { k, n }; if (dup) o.dup = true; return o; }
  if (dup) return [k, n, { dup: true }];
  return [k, n];
}

/** Drawing order of arm `names` under style.arm_order (null = as listed); mirrors render.arm_order. cells[name] = [[k, n], ...]
 * over every panel and level. Pooled ties are exact (cross-multiplied); mean uses floats (display only). */
export function effectiveArmOrder(ao, names, cells) {
  if (!ao) return [...names];
  const fixed = (ao.fixed || []).filter((n, i, a) => names.includes(n) && a.indexOf(n) === i);
  let rest = names.filter((n) => !fixed.includes(n));
  const mode = ao.rest || 'as_listed';
  if (mode === 'pooled' || mode === 'mean') {
    const val = (n) => {
      const cs = cells[n] || [];
      if (!cs.length) return null;
      if (mode === 'pooled') return [cs.reduce((s, c) => s + c[0], 0), cs.reduce((s, c) => s + c[1], 0)];
      return [cs.reduce((s, c) => s + c[0] / c[1], 0) / cs.length, 1];
    };
    const v = new Map(rest.map((n) => [n, val(n)]));
    const cmp = (a, b) => {
      const x = v.get(a), y = v.get(b);
      if (!x || !y) return (x ? 0 : 1) - (y ? 0 : 1);
      const d = y[0] * x[1] - x[0] * y[1];                 // > 0: b better
      return Math.abs(d) < 1e-12 ? 0 : d;
    };
    rest = rest.map((n, i) => [n, i]).sort((a, b) => cmp(a[0], b[0]) || a[1] - b[1]).map((x) => x[0]);
  } else if (mode === 'manual') {
    const man = (ao.manual || []).filter((n, i, a) => rest.includes(n) && a.indexOf(n) === i);
    rest = [...man, ...rest.filter((n) => !man.includes(n))];
  }
  return [...fixed, ...rest];
}
const HEX_RE = /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
// Display-only fallback for chips when the backend's options carry no hex (Paul Tol "bright").
const FALLBACK_HEX = { 'tol-blue': '#4477AA', 'tol-red': '#EE6677', 'tol-green': '#228833', 'tol-yellow': '#CCBB44', 'tol-cyan': '#66CCEE', 'tol-purple': '#AA3377', 'tol-grey': '#BBBBBB', black: '#000000', white: '#FFFFFF' };

const same = (a, b) => a === b || (a != null && b != null && String(a) === String(b)) || JSON.stringify(a) === JSON.stringify(b);
const decimals = (step) => { const s = String(step ?? 1); return s.includes('e-') ? parseInt(s.split('e-')[1], 10) : (s.split('.')[1] || '').length; };
const fmtNum = (v, step) => (v == null || v === '' || isNaN(v) ? '' : Number(v).toFixed(Math.min(4, decimals(step))).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, ''));

/** Normalise color options to [{value, label, hex}] (hex may be null when unknown). */
export function swatchesFrom(options) {
  const out = [];
  if (options && !Array.isArray(options) && typeof options === 'object') options = Object.entries(options).map(([k, v]) => ({ value: k, label: k, hex: v }));
  for (const o of options || []) {
    if (o == null) continue;
    const opt = typeof o === 'object' ? o : { value: o, label: String(o) };
    if (opt.value == null) continue;
    const v = String(opt.value);
    const hex = opt.hex || opt.color || (HEX_RE.test(v) ? v : null) || ((/#[0-9a-f]{6}\b/i.exec(String(opt.label || '')) || [])[0]) || FALLBACK_HEX[v] || null;
    out.push({ value: v, label: opt.label != null ? String(opt.label) : v, hex });
  }
  return out;
}

// name → hex for every swatch the backend told us about (filled by ControlPanel.build)
const HEX_MAP = {};

export function colorHex(value, list) {
  if (value == null) return null;
  const v = String(value);
  if (HEX_RE.test(v)) return v;
  const hit = (list || []).find((o) => o.value === v);
  return (hit && hit.hex) || HEX_MAP[v] || FALLBACK_HEX[v] || null;
}

function toHex6(c) {
  if (!c) return '#000000';
  if (/^#[0-9a-f]{3}$/i.test(c)) return '#' + c.slice(1).split('').map((x) => x + x).join('');
  return c.slice(0, 7);
}

/** Swatch chip + swatch select + hex field. list = [{value, label, hex}]; onChange(value) gets a swatch value or a hex. */
export function colorField(value, list, onChange, { compact = false } = {}) {
  list = list || [];
  const chip = h('span.swatch', { title: 'Pick a custom color' });
  const native = h('input', { type: 'color' });
  chip.append(native);
  const sel = h('select.input.input-xs', { title: 'Theme swatch' });
  const hex = compact ? null : h('input.input.input-xs.hex', { placeholder: '#rrggbb', spellcheck: 'false' });
  const values = list.map((o) => o.value);
  const eq = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
  const paint = (v) => {
    const hx = colorHex(v, list);
    chip.style.background = hx || '';
    chip.classList.toggle('unknown', !hx);
    native.value = toHex6(hx || '#000000');
    sel.replaceChildren(...list.map((o) => h('option', { value: o.value }, o.label)));
    const idx = v == null ? -1 : values.findIndex((x) => eq(x, v));
    if (idx < 0) sel.prepend(h('option', { value: '__custom' }, v == null ? '—' : (HEX_RE.test(String(v)) ? 'custom' : String(v) + (hx ? '' : ' (?)'))));
    sel.value = idx >= 0 ? values[idx] : '__custom';
    if (hex && document.activeElement !== hex) hex.value = hx || (v == null ? '' : String(v));
  };
  paint(value);
  native.addEventListener('input', () => { const v = native.value.toUpperCase(); paint(v); onChange(v); });
  sel.addEventListener('change', () => { if (sel.value !== '__custom') { paint(sel.value); onChange(sel.value); } });
  if (hex) hex.addEventListener('change', () => {
    const v = hex.value.trim();
    if (HEX_RE.test(v) || values.includes(v) || HEX_MAP[v]) { hex.classList.remove('invalid'); paint(v); onChange(v); } else hex.classList.add('invalid');
  });
  const node = h('div.ctl-main', chip, sel, hex);
  node.setValue = paint;
  node.setDisabled = (d) => { sel.disabled = d; native.disabled = d; if (hex) hex.disabled = d; };
  return node;
}

// ─────────────────────────────── style controls ───────────────────────────────

export class ControlPanel {
  /** onChange(key, value) is called on every edit (sliders fire continuously while dragging). */
  /** getData() -> {arms: [names], colors: {name: color}, levels: [names], cells: {name: [[k, n], ...]}} of the current spec
   * (feeds the arm-order and group-color editors). */
  constructor(container, { onChange, getData }) {
    this.container = container; this.onChange = onChange; this.getData = getData || (() => ({ arms: [], colors: {}, levels: [], cells: {} }));
    this.defs = []; this.rows = new Map(); this.style = {}; this.resetTo = {}; this.armSwatches = []; this.groupOrder = GROUP_ORDER;
  }

  /** Redraw the editors that list the spec's arms / levels (after a data edit). */
  refreshData() {
    for (const key of ['arm_order', 'level_colors', 'hidden_arms', 'hidden_levels']) { const r = this.rows.get(key); if (r) r.set(this.style[key]); }
  }

  /**
   * Build from controls().style. baseline = values the reset button returns to (when a control has no default).
   * extra = the whole controls() response; its optional swatches / arm_colors feed the arm color pickers.
   */
  build(defs, style, baseline, extra = {}) {
    this.defs = Array.isArray(defs) ? defs : [];
    this.style = style || {};
    this.resetTo = {};
    for (const c of this.defs) this.resetTo[c.key] = c.default !== undefined ? c.default : (baseline || {})[c.key];
    const fromControls = [];
    for (const c of this.defs) if (c.type === 'color') fromControls.push(...swatchesFrom(c.options));
    const named = swatchesFrom(extra.arm_colors || extra.swatches || []);
    for (const o of [...fromControls, ...named, ...swatchesFrom(extra.swatches || [])]) if (o.hex && !HEX_RE.test(o.value)) HEX_MAP[o.value] = o.hex;
    const seen = new Set();
    this.armSwatches = (named.length ? named : fromControls).filter((o) => !seen.has(o.value) && seen.add(o.value));
    this.rows.clear();
    const groups = new Map();
    for (const c of this.defs) {
      const g = c.group || 'Other';
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(c);
    }
    const go = Array.isArray(extra.groups) && extra.groups.length ? extra.groups : GROUP_ORDER;
    this.groupOrder = go;
    const order = [...groups.keys()].sort((a, b) => {
      const ia = go.indexOf(a), ib = go.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    const scrollParent = this.container.closest('.spec-scroll');
    const keep = scrollParent ? scrollParent.scrollTop : 0;
    this.container.replaceChildren(...order.map((g) => this._group(g, groups.get(g))));
    if (scrollParent) scrollParent.scrollTop = keep;
  }

  _group(name, defs) {
    const body = h('div.cgroup-body', defs.map((c) => this._row(c)));
    return collapsibleGroup(name, [h('span.chev', '▾'), name, h('span.count', String(defs.length))], body);
  }

  _row(c) {
    const key = c.key;
    const row = h('div.ctl', { dataset: { key } });
    const lbl = h('label.lbl', { title: (c.help ? c.help + '\n' : '') + key }, c.label || key);
    const inWrap = h('div.ctl-in');
    row.append(lbl, inWrap);
    let api; // {set(v), disable(bool)}
    const emit = (v) => { this.style[key] = v; this._mark(key); this.onChange(key, v); };
    const t = c.type || 'text';
    let lastNonNull = this.style[key] ?? this.resetTo[key] ?? c.min ?? null;

    if (t === 'slider') {
      const min = Number(c.min ?? 0), max = Number(c.max ?? 1), step = Number(c.step || (max - min) / 100 || 1);
      const range = h('input', { type: 'range', min, max, step });
      const read = h('input.readout', { type: 'text', inputmode: 'decimal', spellcheck: 'false' });
      const pct = () => range.style.setProperty('--pct', ((range.value - min) / (max - min || 1)) * 100 + '%');
      range.addEventListener('input', () => { const v = Number(range.value); read.value = fmtNum(v, step); pct(); lastNonNull = v; emit(v); });
      range.addEventListener('dblclick', () => this.reset(key));
      read.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') read.blur();
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
          e.preventDefault();
          const v = clamp((parseFloat(read.value) || 0) + (e.key === 'ArrowUp' ? 1 : -1) * step * (e.shiftKey ? 10 : 1), min, max);
          const r = Number(fmtNum(v, step)); read.value = fmtNum(r, step); range.value = r; pct(); lastNonNull = r; emit(r);
        }
      });
      read.addEventListener('change', () => {
        const v = parseFloat(read.value);
        if (isNaN(v)) { read.value = fmtNum(range.value, step); return; }
        const r = clamp(v, min, max); range.value = r; read.value = fmtNum(r, step); pct(); lastNonNull = r; emit(r);
      });
      read.addEventListener('focus', () => read.select());
      inWrap.append(h('div.ctl-main', range, read));
      api = {
        set: (v) => { const x = v == null ? Number(lastNonNull ?? (min + max) / 2) : Number(v); range.value = x; read.value = v == null ? 'auto' : fmtNum(x, step); pct(); },
        disable: (d) => { range.disabled = d; read.disabled = d; },
      };
    } else if (t === 'dropdown') {
      let opts = (c.options || []).map((o) => (o && typeof o === 'object' && 'value' in o ? o : { value: o, label: String(o) }));
      if (c.nullable && !opts.some((o) => o.value == null)) opts = [{ value: null, label: 'auto' }, ...opts];
      const sel = h('select.input.input-xs');
      const fill = (v) => {
        let list = opts;
        if (v !== undefined && !opts.some((o) => same(o.value, v))) list = [...opts, { value: v, label: `${v} (current)` }];
        sel.replaceChildren(...list.map((o, i) => h('option', { value: String(i), disabled: !!o.disabled }, o.label ?? String(o.value))));
        sel._list = list;
        const i = list.findIndex((o) => same(o.value, v));
        sel.value = String(Math.max(0, i));
      };
      sel.addEventListener('change', () => emit(sel._list[Number(sel.value)].value));
      inWrap.append(h('div.ctl-main', sel));
      api = { set: fill, disable: (d) => { sel.disabled = d; } };
    } else if (t === 'color') {
      const opts = swatchesFrom(c.options);
      const cf = colorField(this.style[key], opts.length ? opts : this.armSwatches, (v) => { lastNonNull = v; emit(v); });
      inWrap.append(cf);
      api = { set: (v) => cf.setValue(v ?? lastNonNull), disable: (d) => cf.setDisabled(d) };
    } else if (t === 'toggle') {
      const cb = h('input', { type: 'checkbox' });
      const sw = h('label.switch', cb, h('span'));
      cb.addEventListener('change', () => emit(cb.checked));
      inWrap.append(h('div.ctl-main', sw));
      api = { set: (v) => { cb.checked = !!v; }, disable: (d) => { cb.disabled = d; } };
    } else if (t === 'number') {
      const inp = h('input.input.input-xs', { type: 'number', min: c.min, max: c.max, step: c.step || 'any' });
      inp.addEventListener('input', () => {
        if (inp.value === '') return;
        let v = Number(inp.value);
        if (isNaN(v)) return;
        inp.classList.toggle('invalid', (c.min != null && v < c.min) || (c.max != null && v > c.max));
        lastNonNull = v; emit(v);
      });
      inWrap.append(h('div.ctl-main', inp));
      api = { set: (v) => { if (document.activeElement !== inp) inp.value = v == null ? (lastNonNull ?? '') : v; }, disable: (d) => { inp.disabled = d; } };
    } else if (t === 'json' && key === 'arm_order') {
      row.classList.add('wide');
      api = this._armOrderEditor(c, inWrap, (v) => { lastNonNull = v; emit(v); });
    } else if (t === 'json' && (key === 'hidden_arms' || key === 'hidden_levels')) {
      row.classList.add('wide');
      api = this._shownEditor(key === 'hidden_arms' ? 'arms' : 'levels', inWrap, (v) => { lastNonNull = v; emit(v); });
    } else if (t === 'json' && key === 'level_colors') {
      row.classList.add('wide');
      api = this._levelColorsEditor(c, inWrap, (v) => { lastNonNull = v; emit(v); });
    } else if (t === 'json') {
      // structured value (e.g. arm_order {fixed, rest, manual}, level_colors {group: color}) edited as JSON;
      // emitted only when it parses, so a half-typed value never reaches the spec
      if (lastNonNull == null && c.example !== undefined) lastNonNull = c.example;
      const ta = h('textarea.input.input-xs', { rows: 3, spellcheck: 'false', placeholder: c.example !== undefined ? JSON.stringify(c.example) : '{}', title: c.help || '' });
      ta.style.fontFamily = 'monospace'; ta.style.width = '100%'; ta.style.resize = 'vertical';
      ta.addEventListener('input', () => {
        let v;
        try { v = JSON.parse(ta.value); } catch (e) { ta.classList.add('invalid'); return; }
        ta.classList.remove('invalid'); lastNonNull = v; emit(v);
      });
      inWrap.append(h('div.ctl-main', ta));
      api = {
        set: (v) => { if (document.activeElement !== ta) { const x = v == null ? lastNonNull : v; ta.value = x == null ? '' : JSON.stringify(x); ta.classList.remove('invalid'); } },
        disable: (d) => { ta.disabled = d; },
      };
    } else {
      const inp = h('input.input.input-xs', { type: 'text', spellcheck: 'false' });
      inp.addEventListener('input', () => { lastNonNull = inp.value; emit(inp.value); });
      inWrap.append(h('div.ctl-main', inp));
      api = { set: (v) => { if (document.activeElement !== inp) inp.value = v == null ? '' : v; }, disable: (d) => { inp.disabled = d; } };
    }

    // "auto" checkbox for nullable keys (dropdowns carry an "auto" option instead)
    let autoCb = null;
    if (c.nullable && t !== 'dropdown' && !api.structured) {
      autoCb = h('input', { type: 'checkbox' });
      inWrap.append(h('label.auto', { title: 'auto: use the theme / computed value (null)' }, autoCb, 'auto'));
      autoCb.addEventListener('change', () => {
        const isAuto = autoCb.checked;
        row.classList.toggle('is-auto', isAuto);
        api.disable(isAuto);
        let v = null;
        if (!isAuto) {
          v = lastNonNull;
          if (v == null) v = t === 'slider' || t === 'number' ? Number(c.min ?? 0) : t === 'color' ? ((swatchesFrom(c.options)[0] || this.armSwatches[0] || {}).value || '#000000') : '';
        }
        api.set(v);
        emit(v);
      });
    }
    const reset = h('button.reset', { type: 'button', title: 'Reset to default', on: { click: () => this.reset(key) } }, '⟲');
    inWrap.append(reset);

    const set = (v) => {
      api.set(v);
      if (autoCb) { autoCb.checked = v == null; row.classList.toggle('is-auto', v == null); api.disable(v == null); }
    };
    this.rows.set(key, { row, set, def: c });
    set(this.style[key]);
    this._mark(key);
    return row;
  }

  /** style.hidden_arms / hidden_levels as a checklist of the spec's names: ticked = shown. Emits the hidden names. */
  _shownEditor(what, inWrap, emit) {
    let cur = [];
    const arms = what === 'arms';
    const list = h('div.ao-list');
    const hint = h('div.ao-hint', arms ? 'Unticked arms are left out of the plot and the legend; their counts stay in the spec.'
      : 'Unticked levels are left out of the plot; their counts stay in the spec.');
    inWrap.append(h('div.ctl-main.col', list, hint));
    const label = (n) => (arms ? armLabel(n) : n);
    const commit = (next) => { cur = next; emit(cur.slice()); paint(); };
    const paint = () => {
      const d = this.getData();
      const names = (arms ? d.arms : d.levels) || [];
      const rows = names.map((n) => {
        const cb = h('input', { type: 'checkbox', title: 'Shown in the plot' });
        cb.checked = !cur.includes(n);
        cb.addEventListener('change', () => commit(cb.checked ? cur.filter((x) => x !== n) : [...cur.filter((x) => x !== n), n]));
        const dot = arms ? h('span.ao-dot', { style: { background: colorHex((d.colors || {})[n], this.armSwatches) || '#ccc' } }) : null;
        return h('div.ao-item' + (cb.checked ? '' : '.off'), cb, dot, h('span.ao-name', { title: n }, label(n)));
      });
      const unknown = cur.filter((n) => !names.includes(n)).map((n) => h('div.ao-item.unknown',
        h('span.ao-name', { title: n }, `“${label(n)}” is not ${arms ? 'an arm' : 'a level'}`),
        h('span.arm-tools', h('button.del', { type: 'button', title: 'Remove this name', on: { click: () => commit(cur.filter((x) => x !== n)) } }, '×'))));
      list.replaceChildren(...rows, ...unknown, ...(names.length ? [] : [h('div.muted', arms ? 'No arms in the data' : 'No levels in the data')]));
    };
    return {
      set: (v) => { cur = Array.isArray(v) ? v.slice() : []; paint(); },
      disable: (dd) => { list.querySelectorAll('input,button').forEach((x) => { x.disabled = dd; }); },
    };
  }

  /**
   * style.arm_order as a list: tick = drawn first (fixed, in list order); the others follow the "rest" mode. Moving an
   * unticked arm switches to Manual with the order shown. Emits null for "as listed, nothing fixed". { } = raw JSON.
   */
  _armOrderEditor(c, inWrap, emit) {
    let cur = null;
    const mode = h('select.input.input-xs.ao-mode', { title: 'How the arms that are not ticked are ordered' }, ARM_ORDER_MODES.map(([v, l]) => h('option', { value: v }, l)));
    const list = h('div.ao-list');
    const raw = h('textarea.input.input-xs.ao-raw.hidden', { rows: 3, spellcheck: 'false', placeholder: 'null = as listed' });
    const rawBtn = h('button.btn.btn-xs.btn-ghost', { type: 'button', title: 'Edit as JSON' }, '{ }');
    const hint = h('div.ao-hint', 'Tick = drawn first. One order for every panel and group.');
    inWrap.append(h('div.ctl-main.col', h('div.ao-head', mode, rawBtn), list, hint, raw));
    const norm = (v) => ({ fixed: [...((v && v.fixed) || [])], rest: (v && v.rest) || 'as_listed', manual: [...((v && v.manual) || [])] });
    const out = (o) => {
      if (!o.fixed.length && o.rest === 'as_listed') return null;
      const r = {};
      if (o.fixed.length) r.fixed = o.fixed;
      r.rest = o.rest;
      if (o.rest === 'manual') r.manual = o.manual;
      return r;
    };
    const commit = (o) => { cur = out(o); emit(cur); paint(); };
    const paint = () => {
      const d = this.getData();
      const names = d.arms || [], cells = d.cells || {};
      const o = norm(cur);
      mode.value = o.rest;
      const order = effectiveArmOrder(cur, names, cells);
      const fixed = o.fixed.filter((n) => names.includes(n));
      const restOrder = order.filter((n) => !fixed.includes(n));
      const rows = order.map((n, i) => {
        const isFixed = fixed.includes(n);
        const sect = isFixed ? fixed : restOrder;
        const j = sect.indexOf(n);
        const pin = h('input', { type: 'checkbox', title: 'Drawn first (fixed), in this order' });
        pin.checked = isFixed;
        pin.addEventListener('change', () => {
          const o2 = norm(cur);
          if (pin.checked) { o2.fixed = [...o2.fixed.filter((x) => x !== n), n]; o2.manual = o2.manual.filter((x) => x !== n); }
          else { o2.fixed = o2.fixed.filter((x) => x !== n); if (o2.rest === 'manual') o2.manual = [n, ...o2.manual.filter((x) => x !== n)]; }
          commit(o2);
        });
        const move = (dlt) => {
          const k = j + dlt;
          if (k < 0 || k >= sect.length) return;
          const arr = sect.slice(); [arr[j], arr[k]] = [arr[k], arr[j]];
          const o2 = norm(cur);
          if (isFixed) o2.fixed = [...arr, ...o2.fixed.filter((x) => !names.includes(x))];
          else { o2.rest = 'manual'; o2.manual = arr; }
          commit(o2);
        };
        const cs = cells[n] || [];
        const K = cs.reduce((s, x) => s + x[0], 0), N = cs.reduce((s, x) => s + x[1], 0);
        const hx = colorHex((d.colors || {})[n], this.armSwatches);
        return h('div.ao-item' + (isFixed ? '.fixed' : ''), { dataset: { arm: n } },
          pin, h('span.ao-pos', String(i + 1)), h('span.ao-dot', { style: { background: hx || '#ccc' } }),
          h('span.ao-name', { title: n }, armLabel(n)),
          h('span.ao-stat', { title: 'Pooled over every evaluated cell (all panels)' }, N ? `${K}/${N}` : 'no data'),
          h('span.arm-tools',
            h('button', { type: 'button', title: isFixed ? 'Move up' : 'Move up (switches to Manual)', disabled: j === 0, on: { click: () => move(-1) } }, '↑'),
            h('button', { type: 'button', title: isFixed ? 'Move down' : 'Move down (switches to Manual)', disabled: j === sect.length - 1, on: { click: () => move(1) } }, '↓')));
      });
      const unknown = [...new Set([...o.fixed, ...(o.rest === 'manual' ? o.manual : [])])].filter((n) => !names.includes(n));
      const bad = unknown.map((n) => h('div.ao-item.unknown', h('span.ao-name', { title: n }, `“${armLabel(n)}” is not an arm`),
        h('span.arm-tools', h('button.del', { type: 'button', title: 'Remove this name', on: { click: () => { const o2 = norm(cur); o2.fixed = o2.fixed.filter((x) => x !== n); o2.manual = o2.manual.filter((x) => x !== n); commit(o2); } } }, '×'))));
      list.replaceChildren(...rows, ...bad, ...(names.length ? [] : [h('div.muted', 'No arms in the data')]));
      if (document.activeElement !== raw) { raw.value = cur == null ? 'null' : JSON.stringify(cur); raw.classList.remove('invalid'); }
    };
    mode.addEventListener('change', () => {
      const o2 = norm(cur);
      const d = this.getData();
      if (mode.value === 'manual') {
        const order = effectiveArmOrder(cur, d.arms || [], d.cells || {});
        o2.manual = order.filter((n) => !o2.fixed.includes(n));
      }
      o2.rest = mode.value;
      commit(o2);
    });
    rawBtn.addEventListener('click', () => { raw.classList.toggle('hidden'); rawBtn.classList.toggle('on', !raw.classList.contains('hidden')); });
    raw.addEventListener('input', () => {
      let v;
      try { v = JSON.parse(raw.value || 'null'); } catch { raw.classList.add('invalid'); return; }
      if (v !== null && (typeof v !== 'object' || Array.isArray(v))) { raw.classList.add('invalid'); return; }
      raw.classList.remove('invalid'); cur = v; emit(cur); paint();
    });
    return {
      structured: true,
      set: (v) => { cur = v == null ? null : JSON.parse(JSON.stringify(v)); paint(); },
      disable: (dd) => { mode.disabled = dd; raw.disabled = dd; list.querySelectorAll('input,button').forEach((x) => { x.disabled = dd; }); },
    };
  }

  /** style.level_colors as one row per group (level): tick to give that group its own fill color. null when none. */
  _levelColorsEditor(c, inWrap, emit) {
    let cur = null;
    const box = h('div.lc');
    inWrap.append(h('div.ctl-main.col', box));
    const dflt = () => (this.armSwatches.find((o) => o.value === 'tol-grey') || this.armSwatches[0] || { value: '#BBBBBB' }).value;
    const commit = (val, repaint = true) => { cur = Object.keys(val).length ? val : null; emit(cur); if (repaint) paint(); };
    const paint = () => {
      const levels = this.getData().levels || [];
      const val = cur && typeof cur === 'object' ? cur : {};
      const keys = [...levels, ...Object.keys(val).filter((k) => !levels.includes(k))];
      box.replaceChildren(...keys.map((lv) => {
        const has = Object.prototype.hasOwnProperty.call(val, lv), known = levels.includes(lv);
        const cb = h('input', { type: 'checkbox', title: has ? 'Use the arm colors again' : 'Give this group its own fill color' });
        cb.checked = has;
        const cf = colorField(has ? val[lv] : null, this.armSwatches, (v) => commit({ ...(cur || {}), [lv]: v }, false), { compact: true });
        cf.setDisabled(!has);
        cb.addEventListener('change', () => {
          const v2 = { ...(cur || {}) };
          if (cb.checked) v2[lv] = dflt(); else delete v2[lv];
          commit(v2);
        });
        return h('label.lc-row' + (known ? '' : '.unknown'), cb, h('span.lc-name', { title: lv }, known ? lv : `${lv} (not a group)`), cf);
      }), ...(levels.length || keys.length ? [] : [h('div.muted', 'No groups in the data')]));
    };
    return {
      structured: true,
      set: (v) => { cur = v == null ? null : JSON.parse(JSON.stringify(v)); paint(); },
      disable: (dd) => { box.querySelectorAll('input,select').forEach((x) => { x.disabled = dd; }); },
    };
  }

  _mark(key) {
    const r = this.rows.get(key);
    if (r) r.row.classList.toggle('changed', this.resetTo[key] !== undefined && !same(this.style[key], this.resetTo[key]));
  }

  reset(key) {
    const r = this.rows.get(key);
    if (!r || this.resetTo[key] === undefined) return;
    const v = this.resetTo[key];
    this.style[key] = v; r.set(v); this._mark(key); this.onChange(key, v);
  }

  /** Update displayed values without rebuilding (e.g. after an external change). */
  setValues(style) {
    this.style = style;
    for (const [k, r] of this.rows) { r.set(style[k]); this._mark(k); }
  }
}

// ─────────────────────────────── data editor ───────────────────────────────

export class DataEditor {
  /**
   * opts: onChange(), getSwatches() → {name: hex}, getDatasets() → [{name,...}], fetchPreview(name) → Promise<preview>,
   * onManageData()
   */
  constructor(container, opts) {
    this.container = container; Object.assign(this, opts);
    this.spec = null; this._preview = null; this._previewName = null; this._stash = {};
  }

  build(spec) {
    this.spec = spec;
    if (!spec) { this.container.replaceChildren(); return; }
    spec.data = spec.data || { source: 'inline', levels: [], arms: [] };
    const d = spec.data;
    const src = d.source === 'dataset' ? 'dataset' : 'inline';
    const seg = h('div.seg.small',
      h('button', { type: 'button', class: src === 'inline' ? 'on' : '', on: { click: () => this._switch('inline') } }, 'Inline'),
      h('button', { type: 'button', class: src === 'dataset' ? 'on' : '', on: { click: () => this._switch('dataset') } }, 'Dataset'));
    const body = h('div.de', h('div.row', { style: { justifyContent: 'space-between' } }, h('span.de-label', 'Source'), seg),
      src === 'inline' ? this._inline(d) : this._dataset(d));
    this.container.replaceChildren(collapsibleGroup('Data', [h('span.chev', '▾'), 'Data', h('span.count', src)], h('div.cgroup-body', body)));
  }

  _changed(rebuild = false) { this.onChange(); if (rebuild) this.build(this.spec); }

  _switch(src) {
    const d = this.spec.data;
    if ((d.source || 'inline') === src) return;
    this._stash[d.source || 'inline'] = JSON.parse(JSON.stringify(d));
    if (this._stash[src]) this.spec.data = this._stash[src];
    else if (src === 'dataset') {
      const ds = (this.getDatasets() || [])[0];
      this.spec.data = { source: 'dataset', dataset: ds ? ds.name : '', query: {
        level: 'level', arm: 'arm', successes: 'successes', trials: 'trials', filter: {},
        levels: [...(d.levels || [])], arms: (d.arms || []).map((a) => ({ key: plainKey(a.name), name: a.name, color: a.color })) } };
    } else {
      const q = d.query || {};
      this.spec.data = { source: 'inline', levels: [...(q.levels || [])], arms: (q.arms || []).map((a) => ({ name: a.name, color: a.color, counts: (q.levels || []).map(() => null) })) };
    }
    this._changed(true);
  }

  /** Level chips. onStructural(op, i) after add/remove; renaming a level also renames its style.level_colors key. */
  _levels(list, onStructural, label = 'Levels (x groups)') {
    const chips = list.map((lv, i) => {
      const inp = h('input', { value: lv, spellcheck: 'false', size: Math.max(4, String(lv).length), ...(this.spec && this.spec.data && this.spec.data.source === 'dataset' && this._levelsListId ? { list: this._levelsListId } : {}) });
      inp.addEventListener('input', () => {
        const old = list[i];
        list[i] = inp.value; inp.size = Math.max(4, inp.value.length);
        this._renameLevelColor(old, inp.value);
        this._changed(); this._relabel();
      });
      return h('span.chip', inp, h('button', { type: 'button', title: 'Remove level', on: { click: () => { const gone = list[i]; list.splice(i, 1); this._forgetLevel(gone); onStructural('remove', i); } } }, '×'));
    });
    const add = h('button.add-chip', { type: 'button', on: { click: () => { list.push(`Level ${list.length + 1}`); onStructural('add', list.length - 1); } } }, '+ level');
    return h('div', h('div.de-label', label), h('div.chips', chips, add));
  }

  _renameLevelColor(old, nu) {
    const st = this.spec && this.spec.style;
    if (st && Array.isArray(st.hidden_levels) && old !== nu && !st.hidden_levels.includes(nu)) st.hidden_levels = st.hidden_levels.map((x) => (x === old ? nu : x));
    const lc = st && st.level_colors;
    if (!lc || typeof lc !== 'object' || !(old in lc) || nu in lc || old === nu) return;
    st.level_colors = Object.fromEntries(Object.entries(lc).map(([k, v]) => [k === old ? nu : k, v]));
  }

  /** A level was removed: drop it from style.hidden_levels once no level list of the spec has it any more. */
  _forgetLevel(name) {
    const st = this.spec && this.spec.style, d = this.spec && this.spec.data;
    if (!st || !Array.isArray(st.hidden_levels) || !d) return;
    const lists = [d.levels || [], ...(Array.isArray(d.panels) ? d.panels.map((p) => p.levels || []) : [])];
    if (!lists.some((l) => l.includes(name))) st.hidden_levels = st.hidden_levels.filter((x) => x !== name);
  }

  /** An arm (legend name) was renamed: follow it in every panel and in style.arm_order. */
  _renameArm(old, nu) {
    const d = this.spec.data;
    for (const p of Array.isArray(d.panels) ? d.panels : []) for (const a of p.arms || []) if (a.name === old) a.name = nu;
    const ao = this.spec.style && this.spec.style.arm_order;
    if (ao && typeof ao === 'object') for (const k of ['fixed', 'manual']) if (Array.isArray(ao[k])) ao[k] = ao[k].map((x) => (x === old ? nu : x));
    const st = this.spec.style;
    if (st && Array.isArray(st.hidden_arms)) st.hidden_arms = st.hidden_arms.map((x) => (x === old ? nu : x));
  }

  /** An arm was removed: drop it from every panel and from style.arm_order. */
  _forgetArm(name) {
    const d = this.spec.data;
    for (const p of Array.isArray(d.panels) ? d.panels : []) p.arms = (p.arms || []).filter((a) => a.name !== name);
    const ao = this.spec.style && this.spec.style.arm_order;
    if (ao && typeof ao === 'object') for (const k of ['fixed', 'manual']) if (Array.isArray(ao[k])) ao[k] = ao[k].filter((x) => x !== name);
    const st = this.spec.style;
    if (st && Array.isArray(st.hidden_arms)) st.hidden_arms = st.hidden_arms.filter((x) => x !== name);
  }

  _relabel() {
    const d = this.spec.data;
    const levels = d.source === 'dataset' ? (d.query || {}).levels || [] : d.levels || [];
    this.container.querySelectorAll('.cnt-lvl').forEach((el) => {
      const p = el.dataset.p != null && el.dataset.p !== '' ? (d.panels || [])[Number(el.dataset.p)] : null;
      el.textContent = ((p && p.levels) || levels)[Number(el.dataset.i)] ?? '';
    });
  }

  _armTools(arms, i, onDelete) {
    const mv = (d) => { const j = i + d; if (j < 0 || j >= arms.length) return; [arms[i], arms[j]] = [arms[j], arms[i]]; this._changed(true); };
    return h('span.arm-tools',
      h('button', { type: 'button', title: 'Move up (drawn earlier)', disabled: i === 0, on: { click: () => mv(-1) } }, '↑'),
      h('button', { type: 'button', title: 'Move down', disabled: i === arms.length - 1, on: { click: () => mv(1) } }, '↓'),
      h('button.del', { type: 'button', title: 'Remove arm', on: { click: () => { const [gone] = arms.splice(i, 1); if (onDelete && gone) onDelete(gone); this._changed(true); } } }, '×'));
  }

  _armColor(arm) {
    return colorField(arm.color, this.getSwatches() || [], (v) => { arm.color = v; this._changed(); }, { compact: true });
  }

  /** Legend-name input that refuses a name another arm already has, and carries a rename into panels / arm_order. */
  _armName(arm, arms) {
    const name = h('input.input.input-xs', { value: arm.name ?? '', placeholder: 'legend name (TeX ok)', spellcheck: 'false' });
    name.addEventListener('input', () => {
      const nu = name.value;
      const clash = arms.some((a) => a !== arm && a.name === nu);
      name.classList.toggle('invalid', clash || !nu);
      name.title = clash ? 'Another arm already has this name' : (!nu ? 'A name is required' : '');
      if (clash || !nu) return;
      const old = arm.name;
      arm.name = nu;
      this._renameArm(old, nu);
      this._changed();
    });
    return name;
  }

  _newArm(arms, levels) {
    const names = (this.getSwatches() || []).map((o) => o.value);
    let i = arms.length + 1;
    while (arms.some((a) => a.name === `Arm ${i}`)) i++;
    const a = { name: `Arm ${i}`, color: names[arms.length % Math.max(1, names.length)] || '#888888' };
    if (levels) a.counts = levels.map(() => null);
    return a;
  }

  _inline(d) {
    if (Array.isArray(d.panels)) return this._panels(d);
    d.levels = d.levels || []; d.arms = d.arms || [];
    const levels = this._levels(d.levels, (op, i) => {
      for (const a of d.arms) {
        a.counts = a.counts || [];
        if (op === 'remove') a.counts.splice(i, 1); else a.counts.splice(i, 0, null);
      }
      this._changed(true);
    });
    const arms = d.arms.map((arm, ai) => {
      arm.counts = arm.counts || [];
      while (arm.counts.length < d.levels.length) arm.counts.push(null);
      const cells = d.levels.map((_, li) => this._countCell(arm, li, d.levels));
      return h('div.arm', h('div.arm-head', this._armColor(arm), this._armName(arm, d.arms), this._armTools(d.arms, ai, (g) => this._forgetArm(g.name))), h('div.counts', cells));
    });
    const addArm = h('button.add-chip', { type: 'button', on: { click: () => { d.arms.push(this._newArm(d.arms, d.levels)); this._changed(true); } } }, '+ arm');
    const split = (this.spec.kind || 'grouped_bars') === 'grouped_bars'
      ? h('button.btn.btn-xs', { type: 'button', title: 'Turn this chart into the first of several stacked panels (one shared arm order, colors and y axis)', on: { click: () => {
        d.panels = [{ title: 'Panel 1', arms: d.arms.map((a) => ({ name: a.name, counts: a.counts || d.levels.map(() => null) })) }];
        d.arms = d.arms.map((a) => { const o = { ...a }; delete o.counts; return o; });
        this._panelTab = 0;
        this._changed(true);
      } } }, 'Split into panels…') : null;
    return h('div', { class: 'de-stack' }, levels,
      h('div', h('div.de-label', h('span', 'Arms (legend, left → right)'), h('span.muted', 'k / n · empty = n/a · ↻ = re-run')), h('div', { class: 'de-stack tight' }, arms, addArm)),
      split ? h('div.row.gap6', split) : null);
  }

  /**
   * data.panels: default levels, the shared arm list (names, colors, slots) and one tab per panel with its title,
   * optional own levels and an arms x levels count grid. An arm missing from a panel is n/a there.
   */
  _panels(d) {
    d.levels = d.levels || [];
    if (!Array.isArray(d.arms) || !d.arms.length) {
      // no shared list: arms are every panel's in order of first appearance (colors from their first colored entry);
      // make that list explicit so it can be edited in one place (same picture)
      const seen = new Map();
      for (const p of d.panels) for (const a of p.arms || []) {
        const e = seen.get(a.name) || { name: a.name };
        for (const k of ['color', 'hatch']) if (a[k] != null && e[k] == null) e[k] = a[k];
        seen.set(a.name, e);
        delete a.color; delete a.hatch;
      }
      d.arms = [...seen.values()];
    }
    for (const a of d.arms) if (Array.isArray(a.counts) && a.counts.every((c) => c == null)) delete a.counts;
    const nP = d.panels.length;
    this._panelTab = clamp(this._panelTab || 0, 0, Math.max(0, nP - 1));
    const levelsOf = (p) => (Array.isArray(p.levels) ? p.levels : d.levels);
    const resize = (p, op, i) => {
      for (const a of p.arms || []) {
        a.counts = a.counts || [];
        if (op === 'remove') a.counts.splice(i, 1); else a.counts.splice(i, 0, null);
      }
    };
    const defLevels = this._levels(d.levels, (op, i) => {
      for (const p of d.panels) if (!Array.isArray(p.levels)) resize(p, op, i);
      this._changed(true);
    }, 'Levels (every panel, unless it has its own)');

    const shared = d.arms.map((arm, ai) => {
      const ignored = Array.isArray(arm.counts) && arm.counts.some((c) => c != null);
      return h('div.arm-head.shared-arm', this._armColor(arm), this._armName(arm, d.arms),
        ignored ? h('span.badge.warn', { title: 'data.arms counts are ignored when the spec has panels (each panel has its own)' }, 'counts ignored') : null,
        this._armTools(d.arms, ai, (g) => this._forgetArm(g.name)));
    });
    const addArm = h('button.add-chip', { type: 'button', title: 'A new arm is n/a in every panel until you add it to one', on: { click: () => { d.arms.push(this._newArm(d.arms, null)); this._changed(true); } } }, '+ arm');

    const tabs = h('div.ptabs', d.panels.map((p, pi) => h('button' + (pi === this._panelTab ? '.on' : ''), { type: 'button', title: `Panel ${pi + 1}`, on: { click: () => { this._panelTab = pi; this.build(this.spec); } } },
      p.title ? armLabel(p.title) : `Panel ${pi + 1}`)),
    h('button.add-chip', { type: 'button', title: 'Add a panel below the others', on: { click: () => {
      // every shared arm, not evaluated yet (null = n/a); a panel needs at least one arm entry to be valid
      d.panels.push({ title: `Panel ${nP + 1}`, arms: d.arms.map((a) => ({ name: a.name, counts: d.levels.map(() => null) })) });
      this._panelTab = d.panels.length - 1; this._changed(true);
    } } }, '+ panel'));

    const p = d.panels[this._panelTab];
    p.arms = p.arms || [];
    const pi = this._panelTab;
    const lv = levelsOf(p);
    const title = h('input.input.input-xs', { value: p.title ?? '', placeholder: 'panel title (drawn in its corner; TeX ok)', spellcheck: 'false' });
    title.addEventListener('input', () => {
      if (title.value) p.title = title.value; else delete p.title;
      const tb = tabs.children[pi]; if (tb) tb.textContent = title.value ? armLabel(title.value) : `Panel ${pi + 1}`;
      this._changed();
    });
    const mvP = (dl) => { const j = pi + dl; if (j < 0 || j >= nP) return; [d.panels[pi], d.panels[j]] = [d.panels[j], d.panels[pi]]; this._panelTab = j; this._changed(true); };
    const del = h('button.del', { type: 'button', title: nP === 1 ? 'The last panel cannot be removed (use “Back to one chart”)' : 'Remove this panel (click twice)', disabled: nP === 1 }, '×');
    del.addEventListener('click', () => {
      if (!del.classList.contains('confirm')) { del.classList.add('confirm'); del.textContent = 'remove?'; setTimeout(() => { del.classList.remove('confirm'); del.textContent = '×'; }, 2500); return; }
      d.panels.splice(pi, 1); this._panelTab = Math.max(0, pi - 1); this._changed(true);
    });
    const ptools = h('span.arm-tools',
      h('button', { type: 'button', title: 'Move this panel up', disabled: pi === 0, on: { click: () => mvP(-1) } }, '↑'),
      h('button', { type: 'button', title: 'Move this panel down', disabled: pi === nP - 1, on: { click: () => mvP(1) } }, '↓'), del);

    const own = h('input', { type: 'checkbox' });
    own.checked = Array.isArray(p.levels);
    own.addEventListener('change', () => {
      if (own.checked) p.levels = [...d.levels];
      else {
        const old = p.levels || [];
        delete p.levels;
        // keep each arm's counts by level name where the default levels have the same name
        for (const a of p.arms) a.counts = d.levels.map((name) => { const j = old.indexOf(name); return j >= 0 ? (a.counts || [])[j] ?? null : null; });
      }
      this._changed(true);
    });
    const ownLevels = Array.isArray(p.levels) ? this._levels(p.levels, (op, i) => { resize(p, op, i); this._changed(true); }, 'This panel\'s levels') : null;

    const byName = new Map(p.arms.map((a) => [a.name, a]));
    const grid = d.arms.map((sa) => {
      const a = byName.get(sa.name);
      const dot = h('span.ao-dot', { style: { background: colorHex(sa.color, this.getSwatches() || []) || '#ccc' } });
      if (!a) {
        return h('div.arm.absent', h('div.arm-head', dot, h('span.arm-label', { title: sa.name }, armLabel(sa.name)), h('span.muted', 'n/a in this panel'),
          h('button.add-chip', { type: 'button', title: 'Add counts for this arm in this panel', on: { click: () => { p.arms.push({ name: sa.name, counts: lv.map(() => null) }); this._changed(true); } } }, '+ add')));
      }
      a.counts = a.counts || [];
      while (a.counts.length < lv.length) a.counts.push(null);
      const cells = lv.map((_, li) => this._countCell(a, li, lv, pi));
      return h('div.arm', h('div.arm-head', dot, h('span.arm-label', { title: sa.name }, armLabel(sa.name)),
        h('span.arm-tools', h('button.del', { type: 'button', title: p.arms.length > 1 ? 'Remove this arm from this panel (n/a here)' : 'A panel needs at least one arm (empty its counts instead)', disabled: p.arms.length <= 1, on: { click: () => { p.arms = p.arms.filter((x) => x !== a); this._changed(true); } } }, '×'))),
      h('div.counts', cells));
    });
    const stray = p.arms.filter((a) => !d.arms.some((sa) => sa.name === a.name)).map((a) => h('div.arm.absent', h('div.arm-head',
      h('span.badge.err', 'unknown'), h('span.arm-label', { title: a.name }, armLabel(a.name)), h('span.muted', 'not in the arm list'),
      h('button.add-chip', { type: 'button', on: { click: () => { d.arms.push({ name: a.name, color: (this._newArm(d.arms, null)).color }); this._changed(true); } } }, '+ list'),
      h('span.arm-tools', h('button.del', { type: 'button', title: 'Remove from this panel', on: { click: () => { p.arms = p.arms.filter((x) => x !== a); this._changed(true); } } }, '×')))));

    const flatten = nP === 1 ? h('button.btn.btn-xs', { type: 'button', title: 'Back to a single chart (this panel\'s counts; its title is dropped)', on: { click: () => {
      const only = d.panels[0];
      const lvl = levelsOf(only);
      const by = new Map((only.arms || []).map((a) => [a.name, a]));
      d.levels = [...lvl];
      d.arms = d.arms.map((sa) => ({ ...sa, counts: by.get(sa.name) ? [...by.get(sa.name).counts] : lvl.map(() => null) }));
      delete d.panels;
      this._changed(true);
    } } }, 'Back to one chart') : null;

    return h('div', { class: 'de-stack' }, defLevels,
      h('div', h('div.de-label', h('span', 'Arms (shared: names, colors, slots)'), h('span.muted', 'order: Order ▸ Arm order')), h('div', { class: 'de-stack tight' }, shared, addArm)),
      h('div', h('div.de-label', h('span', `Panels (${nP}, stacked top → bottom)`), h('span.muted', 'k / n · empty = n/a · ↻ = re-run')), tabs),
      h('div.panel-body', { dataset: { panel: String(pi) } },
        h('div.arm-head', title, ptools),
        h('label.check.small', own, 'Own levels'),
        ownLevels,
        h('div', { class: 'de-stack tight' }, grid, stray)),
      flatten ? h('div.row.gap6', flatten) : null);
  }

  /** k / n inputs plus a re-run (dup) toggle for one count; keeps the count's written form and its dup flag. */
  _countCell(arm, li, levels, panelIdx = null) {
    const c = countParts(arm.counts[li]);
    const k = h('input', { type: 'text', inputmode: 'numeric', value: c ? c.k : '', placeholder: '–', title: 'successes' });
    const n = h('input', { type: 'text', inputmode: 'numeric', value: c ? c.n : '', placeholder: '–', title: 'trials' });
    const rate = h('span.cnt-pct');
    const dup = h('button.cnt-dup', { type: 'button', title: 'Re-run: positions were re-run and only the latest rollout is counted. Its interval is drawn in the re-run color (yellow).' }, '↻');
    const cell = h('div.cnt', h('div.cnt-lvl', { dataset: { i: li, p: panelIdx == null ? '' : String(panelIdx) } }, (levels || [])[li] ?? ''), h('div.cnt-in', k, h('span', '/'), n), h('div.cnt-rate', dup, rate));
    const paint = () => {
      const v = countParts(arm.counts[li]);
      rate.textContent = v && v.n > 0 ? Math.round((100 * v.k) / v.n) + '%' : (v ? '' : 'n/a');
      cell.classList.toggle('na', !v);
      cell.classList.toggle('dup', !!(v && v.dup));
      dup.classList.toggle('on', !!(v && v.dup));
      dup.disabled = !v;
    };
    const upd = () => {
      const ks = k.value.trim(), ns = n.value.trim();
      k.classList.remove('bad'); n.classList.remove('bad');
      if (ks === '' && ns === '') { arm.counts[li] = null; paint(); this._changed(); return; }
      const kv = Number(ks), nv = Number(ns);
      const okK = ks !== '' && Number.isInteger(kv) && kv >= 0, okN = ns !== '' && Number.isInteger(nv) && nv > 0;
      if (!okK) k.classList.add('bad');
      if (!okN) n.classList.add('bad');
      if (!okK || !okN) return;
      if (kv > nv) { k.classList.add('bad'); n.classList.add('bad'); }
      const old = arm.counts[li], p = countParts(old);
      arm.counts[li] = makeCount(kv, nv, !!(p && p.dup), old); paint(); this._changed();
    };
    dup.addEventListener('click', () => {
      const old = arm.counts[li], p = countParts(old);
      if (!p) return;
      arm.counts[li] = makeCount(p.k, p.n, !p.dup, old); paint(); this._changed();
    });
    k.addEventListener('input', upd); n.addEventListener('input', upd);
    for (const inp of [k, n]) inp.addEventListener('focus', () => inp.select());
    paint();
    return cell;
  }

  _dataset(d) {
    const q = d.query = d.query || {};
    q.levels = q.levels || []; q.arms = q.arms || []; q.filter = q.filter || {};
    const datasets = this.getDatasets() || [];
    const dsSel = h('select.input.input-xs');
    const names = datasets.map((x) => x.name);
    const opts = names.map((n) => h('option', { value: n }, n));
    if (d.dataset && !names.includes(d.dataset)) opts.unshift(h('option', { value: d.dataset }, `${d.dataset} (not registered)`));
    if (!d.dataset) opts.unshift(h('option', { value: '' }, '— choose dataset —'));
    dsSel.replaceChildren(...opts); dsSel.value = d.dataset || '';
    dsSel.addEventListener('change', () => { d.dataset = dsSel.value; this._changed(true); });
    const manage = h('button.btn.btn-xs', { type: 'button', on: { click: () => this.onManageData && this.onManageData() } }, 'Data…');

    const colsHost = h('div.map-grid');
    const filterHost = h('div', { class: 'de-stack tighter' });
    const valuesList = h('datalist', { id: 'arm-keys-' + Math.random().toString(36).slice(2) });
    const levelsList = h('datalist', { id: 'level-keys-' + Math.random().toString(36).slice(2) });
    this._levelsListId = levelsList.id;
    const renderMapping = (cols, rows) => {
      const colSel = (key, label) => {
        let el;
        if (cols && cols.length) {
          el = h('select.input.input-xs', [...new Set([...(q[key] && !cols.includes(q[key]) ? [q[key]] : []), ...cols])].map((c) => h('option', { value: c }, c)));
          if (!q[key]) el.prepend(h('option', { value: '' }, '—'));
          el.value = q[key] || '';
          el.addEventListener('change', () => { q[key] = el.value; this._changed(); fillValues(); });
        } else {
          el = h('input.input.input-xs', { value: q[key] || '', placeholder: 'column', spellcheck: 'false' });
          el.addEventListener('input', () => { q[key] = el.value; this._changed(); });
        }
        return [h('label', label), el];
      };
      colsHost.replaceChildren(...colSel('level', 'Level col'), ...colSel('arm', 'Arm col'), ...colSel('successes', 'Successes'), ...colSel('trials', 'Trials'));
      const fillValues = () => {
        const ai = cols ? cols.indexOf(q.arm) : -1;
        const vals = ai >= 0 && rows ? [...new Set(rows.map((r) => (Array.isArray(r) ? r[ai] : r[q.arm])).filter((v) => v != null))] : [];
        valuesList.replaceChildren(...vals.slice(0, 200).map((v) => h('option', { value: String(v) })));
        const li = cols ? cols.indexOf(q.level) : -1;
        const lvals = li >= 0 && rows ? [...new Set(rows.map((r) => (Array.isArray(r) ? r[li] : r[q.level])).filter((v) => v != null))] : [];
        levelsList.replaceChildren(...lvals.slice(0, 200).map((v) => h('option', { value: String(v) })));
      };
      fillValues();
      // filter rows
      const entries = Object.entries(q.filter);
      filterHost.replaceChildren(...entries.map(([fk, fv], i) => {
        const kIn = h('input.input.input-xs', { value: fk, placeholder: 'column', spellcheck: 'false' });
        const vIn = h('input.input.input-xs', { value: fv == null ? '' : String(fv), placeholder: 'value', spellcheck: 'false' });
        const commit = () => {
          const e2 = Object.entries(q.filter); e2[i] = [kIn.value, vIn.value];
          q.filter = Object.fromEntries(e2.filter(([a]) => a !== '')); this._changed();
        };
        kIn.addEventListener('change', commit); vIn.addEventListener('input', commit);
        return h('div.filter-row', kIn, h('span.muted', '='), vIn,
          h('button.btn.btn-icon.btn-ghost.btn-sm', { type: 'button', title: 'Remove filter', on: { click: () => { delete q.filter[fk]; this._changed(true); } } }, '×'));
      }), h('button.add-chip', { type: 'button', style: { justifySelf: 'start' }, on: { click: () => { q.filter[''] = ''; renderMapping(cols, rows); } } }, '+ filter'));
    };
    renderMapping(null, null);
    if (d.dataset && names.includes(d.dataset) && this.fetchPreview) {
      const got = this._previewName === d.dataset ? Promise.resolve(this._preview) : this.fetchPreview(d.dataset);
      got.then((p) => {
        this._preview = p; this._previewName = d.dataset;
        if (this.spec && this.spec.data === d && p && Array.isArray(p.columns)) renderMapping(p.columns, p.rows);
      }).catch(() => { /* mapping stays free-text */ });
    }

    const levels = this._levels(q.levels, () => this._changed(true));
    // fill levels + arms from the distinct values of the chosen columns (after the filter), keeping existing arms
    const fromData = h('button.btn.btn-xs', { type: 'button', title: 'Set the levels and arms to the distinct values of the level / arm columns (rows matching the filter; at most 12 each)', on: { click: async () => {
      if (!d.dataset || !q.level || !q.arm) { fromData.title = 'Choose the dataset and the level / arm columns first'; fromData.classList.add('shake'); setTimeout(() => fromData.classList.remove('shake'), 400); return; }
      let p;
      try { p = await this.fetchPreview(d.dataset, 5000); } catch { return; }
      if (!p || !Array.isArray(p.columns) || !Array.isArray(p.rows)) return;
      const col = (r, c) => { const i = p.columns.indexOf(c); return Array.isArray(r) ? r[i] : r[c]; };
      const rows = p.rows.filter((r) => Object.entries(q.filter || {}).every(([fk, fv]) => fk === '' || String(col(r, fk)) === String(fv)));
      const distinct = (c) => [...new Set(rows.map((r) => col(r, c)).filter((v) => v != null && v !== '').map(String))].slice(0, 12);
      const lv = distinct(q.level), av = distinct(q.arm);
      if (!lv.length || !av.length) return;
      const sw = (this.getSwatches() || []).map((o) => o.value);
      const old = new Map(q.arms.map((a) => [String(a.key), a]));
      q.levels.splice(0, q.levels.length, ...lv);
      q.arms.splice(0, q.arms.length, ...av.map((k, i) => old.get(k) || { key: k, name: k, color: sw[i % Math.max(1, sw.length)] || '#888888' }));
      this._changed(true);
    } } }, 'Use values from data');
    const arms = q.arms.map((arm, ai) => {
      const key = h('input.input.input-xs', { value: arm.key ?? '', placeholder: 'value in arm column', spellcheck: 'false', list: valuesList.id });
      key.addEventListener('input', () => { arm.key = key.value; this._changed(); });
      const name = h('input.input.input-xs', { value: arm.name ?? '', placeholder: 'legend name', spellcheck: 'false' });
      name.addEventListener('input', () => { arm.name = name.value; this._changed(); });
      return h('div.arm', h('div.arm-head', this._armColor(arm), key, this._armTools(q.arms, ai)), h('div.arm-head', name));
    });
    const addArm = h('button.add-chip', { type: 'button', on: { click: () => {
      const sw = (this.getSwatches() || []).map((o) => o.value);
      q.arms.push({ key: '', name: `Arm ${q.arms.length + 1}`, color: sw[q.arms.length % Math.max(1, sw.length)] || '#888888' }); this._changed(true);
    } } }, '+ arm');

    return h('div', { class: 'de-stack' },
      h('div', h('div.de-label', 'Dataset'), h('div.row.gap6', dsSel, manage)),
      h('div', h('div.de-label', 'Columns'), colsHost),
      h('div', h('div.de-label', 'Filter (rows where column = value)'), filterHost),
      h('div', { class: 'row gap6' }, fromData, h('span.muted', { style: { fontSize: '11px' } }, 'levels + arms from the columns above')),
      levels,
      h('div', h('div.de-label', 'Arms (key → legend)'), h('div', { class: 'de-stack tight' }, arms, addArm)),
      valuesList, levelsList);
  }
}

function plainKey(s) { return String(s || '').replace(/\$[^$]*\$/g, '').replace(/[^A-Za-z0-9]+/g, '_').replace(/^_|_$/g, '').toLowerCase() || 'arm'; }
