// Plot Designer — main controller. Wires the backend (api.js) to the page viewer (pdfview.js), the controls
// (controls.js), the TeX editor, git, recompiles and the data drawer.

import { api, asList, abs } from './api.js';
import { $, $$, h, debounce, store, toast, toastError, modal, confirmDialog, menu, popover, closePopover, makeSplitter, plainTex, fmtBytes, fmtTime, errText, clamp } from './ui.js';
import { PageViewer, Thumbs, loadPdf, renderStack, overlayRect, pdfjsAvailable, MIN_SCALE, MAX_SCALE, DEFAULT_SCALE } from './pdfview.js';
import { ControlPanel, DataEditor, armLabel, countParts, collapsibleGroup, setAllGroups } from './controls.js';

const PT_100 = 96 / 72;               // CSS px per pt at "100%" (physical size)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clone = (x) => JSON.parse(JSON.stringify(x));
const norm = (p) => String(p || '').replace(/\\/g, '/').replace(/^\.\//, '');

// ───────────────────────────── state ─────────────────────────────
const S = {
  config: null, backendOk: false,
  specs: [], figures: [], datasets: [], git: null,
  pdf: null, pdfState: 'idle',            // idle | loading | ok | missing | error
  pdfError: '',
  mode: 'page', modeAuto: false,
  selFid: null,
  spec: null, specBaseline: null, controlsCache: {},
  editSeq: 0, savedSeq: 0, saveOk: true, saving: null, specErrors: null,
  specHash: null,                         // sha256 of the spec file this tab edits from (PUT is compare-and-swap)
  specConflict: null,                     // {hash, current}: the file changed on disk; saving paused until resolved
  preview: { url: null, state: 'idle', w: 0, h: 0, ms: 0, error: null }, previewSeq: 0, previewActive: false,
  liveOverlay: true, plotZoom: store.get('plotZoom.v2', DEFAULT_SCALE), plotFit: false, ctxScale: DEFAULT_SCALE,
  ctx: null,                              // {fid, doc|loading|error}
  sections: [],                           // editable .tex files (main + \input/\include), document order
  tex: null, texDirty: false, texSeq: 0, texSaving: null,   // open section: {path, text, hash, mtime, lines, ...}
  compiling: null, compileAgain: false, compileT0: 0, lastCompile: null,
  editedTex: new Set(), renderedOutputs: new Set(),
  lastReverse: null, lastStats: null,
  auto: store.get('autoRecompile', false),
  readonly: false,                        // DESIGNER_READONLY: no writes (specs, paper, registry, git)
};

const figById = (fid) => S.figures.find((f) => f.fid === fid) || null;
const selFig = () => figById(S.selFid);
const figForSpec = (id) => S.figures.find((f) => f.spec_id === id && !f.commented) || null;
const graphicsStem = (f) => ((f.graphics || [])[0] ? norm(f.graphics[0].path).split('/').pop().replace(/\.[a-z]+$/i, '') : '');
const figLabel = (f) => (f.label ? f.label.replace(/^(figure|fig):/, '') : '') || ((f.graphics || [])[0] ? norm(f.graphics[0].path).split('/').pop().replace(/\.[a-z]+$/i, '') : f.fid);

// ───────────────────────────── components ─────────────────────────────
let viewer, thumbs, controls, dataEditor, ed;

function initComponents() {
  viewer = new PageViewer({
    stage: $('#stage'), pagesEl: $('#page'), overlayEl: $('#overlay'),
    onClick: onPageClick,
    onChange: () => { updatePager(); updateZoomLabel(); thumbs && thumbs.setCurrent(viewer.pageNum); refreshPreviewDpi(); },
  });
  thumbs = new Thumbs($('#thumbs'), {
    width: 100,
    onPage: (n) => { setMode('page'); viewer.show(n); },
    onBox: (fid) => { closeNav(); selectFigure(fid, { zoom: true }); },
  });
  controls = new ControlPanel($('#controls'), { onChange: onControlChange, getData: specDataSummary });
  dataEditor = new DataEditor($('#data-editor'), {
    onChange: () => { specEdited(); controls.refreshData(); },
    getSwatches: () => controls.armSwatches,
    getDatasets: () => S.datasets,
    fetchPreview: (name, rows) => api.previewDataset(name, rows),
    onManageData: () => toggleDrawer(true),
  });
  ed = makeEditor();
}

// ───────────────────────────── boot ─────────────────────────────
async function boot() {
  initComponents();
  setupLayout(); bindTopbar(); bindCenter(); bindEditor(); bindNav(); bindRight(); bindDrawer(); bindKeys();
  updateSaveUI(); updateStage();
  window.addEventListener('designer-unauthorized', onUnauthorized);
  try {
    S.config = await api.config();
    S.backendOk = true;
  } catch (e) {
    S.backendOk = false; S.pdfError = errText(e).detail || errText(e).title; updateStage();
    return;
  }
  S.readonly = !!S.config.readonly;
  if (S.readonly) applyReadonly();
  const linkLabel = $('#da-link') && $('#da-link').closest('label');
  if (linkLabel && S.config.platform && S.config.platform !== 'windows') {
    linkLabel.title = 'Make a symlink to the data in the designer state folder. Data is never copied.';
  }
  api.fontsCheck().then((r) => {              // a theme whose font is missing refuses to render: say so up front
    const bad = ((r && r.themes) || []).filter((t) => !t.ok);
    if (bad.length) toast('Fonts missing on the server', { kind: 'warn', ms: 0, detail: bad.map((t) => `${t.theme}: ${t.family || t.error}`).join('\n') + '\nFigures using these themes will not render (no silent font fallback).' });
  }).catch(() => { /* older backend */ });
  const root = S.config.paper_root || '';
  $('#paper-root').textContent = root; $('#paper-root').title = `Paper: ${root}\nMain: ${S.config.main_tex || ''}`;
  // the PDF loads alongside, not before the rest: pdf.js page rendering pauses in a hidden tab, and the editor,
  // spec and controls must not wait for it (loadPaper focuses the selected figure once the page is there)
  loadPaper();
  await Promise.all([loadSpecs(), loadDatasets(), refreshGit(), loadFigures(), loadSections()]);
  await restoreSelection();
  if (!S.tex) {
    // nothing selected opened a section: the last one viewed, else the first with a designer figure, else main
    const last = store.get('tex.path', null);
    const pick = (last && sectionByPath(last)) || sectionByPath((S.figures.find((f) => f.spec_id && !f.commented) || {}).file) || S.sections[0];
    if (pick) await openSection(pick.path, { confirm: false });
    else $('#editor-empty').textContent = 'No TeX files found in the paper.';
  }
  setInterval(() => { if (!document.hidden) { refreshGit(); checkDiskChange(); checkSpecDiskChange(); } }, 30000);
}

// ───────────────────────────── read-only mode / auth ─────────────────────────────
const RO_IDS = ['btn-fetch', 'btn-pull', 'btn-commit', 'btn-push', 'btn-insert', 'tex-save', 'tex-recompile', 'spec-new', 'btn-render'];

function applyReadonly() {
  document.body.classList.add('readonly');
  $('#ro-badge').classList.remove('hidden');
  for (const id of RO_IDS) {
    const b = document.getElementById(id);
    if (!b) continue;
    b.disabled = true; b.classList.add('ro-disabled');
    b.title = `${b.title ? b.title + ' — ' : ''}disabled: read-only mode`;
  }
  const sub = document.querySelector('#data-add button[type=submit]');
  if (sub) { sub.disabled = true; sub.classList.add('ro-disabled'); }
  updateSaveUI();
}

/** True (and a toast) when a write is attempted in read-only mode. */
function roBlocked(what) {
  if (!S.readonly) return false;
  toast('Read-only mode', { kind: 'warn', detail: `${what} is disabled: this designer runs read-only (DESIGNER_READONLY).` });
  return true;
}

let unauthorizedShown = false;
function onUnauthorized() {
  if (unauthorizedShown) return;
  unauthorizedShown = true;
  // every API call fails without the token, so go to the sign-in form (relative: works under a base path)
  toast('Not signed in', { kind: 'err', ms: 0, detail: 'This designer needs its access token. Opening the sign-in page…' });
  setTimeout(() => { window.location.assign(abs('login')); }, 600);
}

let lastFontError = '';
function reportFontError(e) {
  const d = (e && (e.detail || e.message)) || '';
  if (d === lastFontError) return;
  lastFontError = d;
  toast('Font not available — nothing rendered', { kind: 'err', detail: d });
}

async function restoreSelection() {
  const last = store.get('sel', null);
  if (last && last.fid && figById(last.fid)) return selectFigure(last.fid, { zoom: true });
  if (last && last.spec && S.specs.some((s) => s.id === last.spec)) return selectSpec(last.spec);
  const first = S.figures.find((f) => f.spec_id && f.box && !f.commented);
  if (first) return selectFigure(first.fid, { zoom: true });
  // prefer a spec whose data is available (an unregistered dataset would only show a failed preview)
  const have = new Set(S.datasets.map((d) => d.name));
  const ready = S.specs.find((s) => s.source !== 'dataset' || (s.dataset && have.has(s.dataset)));
  if (ready || S.specs.length) return selectSpec((ready || S.specs[0]).id);
}

// ───────────────────────────── data loading ─────────────────────────────
async function loadSpecs() {
  try { S.specs = await api.specs(); } catch (e) { toastError(e, 'Could not list specs'); S.specs = []; }
  renderSpecPicker(); renderFigList();
}

async function loadFigures() {
  try {
    const r = await api.figures();
    const boxes = (r && !Array.isArray(r) && (r.boxes || r.locate)) || {};
    S.figures = asList(r, 'figures').map((f) => ({ ...f, file: norm(f.file), box: f.box !== undefined ? f.box : (boxes[f.fid] || null) }));
  } catch (e) {
    toastError(e, 'Could not scan figures'); S.figures = [];
  }
  renderFigList(); updateBoxes(); updateSpecMeta(); updateStage();
}

async function loadDatasets() {
  try { S.datasets = await api.datasets(); } catch (e) { toastError(e, 'Could not list datasets'); S.datasets = []; }
  renderDataTable();
}

async function loadPaper({ keepView = false } = {}) {
  if (!pdfjsAvailable()) { S.pdfState = 'error'; S.pdfError = 'pdf.js could not be loaded from cdnjs.cloudflare.com (offline?).'; updateStage(); return; }
  if (S.config && S.config.has_pdf === false && !keepView && !S.pdf) {
    // nothing compiled yet: skip the request (and its 404) until the first compile
    S.pdfState = 'missing';
    $('#thumbs').replaceChildren(h('div.empty.small', { style: { gridColumn: '1 / -1' } }, 'No compiled PDF yet.'));
    updateStage();
    return;
  }
  const firstLoad = !S.pdf;
  if (firstLoad) { S.pdfState = 'loading'; updateStage(); }
  try {
    const doc = await loadPdf(api.pdfUrl());
    const old = S.pdf;
    S.pdf = doc; S.pdfState = 'ok';
    updateStage();
    await viewer.setDoc(doc, { keepView: keepView && !!old });
    if (old) old.destroy();
    $('#pages-count').textContent = `${doc.numPages}`;
    thumbs.setDoc(doc).then(() => { updateBoxes(); thumbs.setCurrent(viewer.pageNum); });
    updateBoxes(); updateOverlay(); updatePager();
    if (firstLoad && !keepView) {       // a figure selected before the PDF arrived (boot): show it now, zoom unchanged
      const f = selFig();
      if (f && f.box && S.mode === 'page') viewer.focusBox(f.box);
    }
  } catch (e) {
    if (S.pdf && keepView) { toast('Could not reload the PDF', { kind: 'err', detail: e.message }); return; }
    S.pdfState = e.status === 404 ? 'missing' : 'error'; S.pdfError = e.message;
    $('#thumbs').replaceChildren(h('div.empty.small', { style: { gridColumn: '1 / -1' } }, S.pdfState === 'missing' ? 'No compiled PDF yet.' : 'PDF unavailable.'));
    $('#pages-count').textContent = '';
    updateStage();
  }
}

// ───────────────────────────── layout / splitters ─────────────────────────────
function setupLayout() {
  const root = document.documentElement;
  const sizes = store.get('sizes', {});
  for (const [k, v] of Object.entries(sizes)) root.style.setProperty(k, v + 'px');
  const save = (k) => (v) => { const s = store.get('sizes', {}); s[k] = v; store.set('sizes', s); if (k === '--tex-w') ed.refresh(); };
  // left = section editor: wide by default (CSS --tex-w), dragged width remembered per viewer
  makeSplitter($('.vsplit[data-split=left]'), { cssVar: '--tex-w', axis: 'x', min: () => 240, max: () => Math.max(260, window.innerWidth - ($('#app').classList.contains('right-hidden') ? 0 : $('#right').offsetWidth) - 240), getBase: () => $('#left').offsetWidth, onEnd: save('--tex-w') });
  makeSplitter($('.vsplit[data-split=right]'), { cssVar: '--right-w', axis: 'x', invert: true, min: () => 280, max: () => 620, getBase: () => $('#right').offsetWidth, onEnd: save('--right-w') });
  makeSplitter($('.hsplit[data-split=drawer]'), { cssVar: '--drawer-h', axis: 'y', invert: true, min: () => 120, max: () => window.innerHeight - 220, getBase: () => $('#drawer').offsetHeight, onEnd: save('--drawer-h') });
  makeSplitter($('.hsplit[data-split=pages]'), { cssVar: '--pages-h', axis: 'y', min: () => 70, max: () => $('#nav-drawer').offsetHeight - $('#sections-sec').offsetHeight - 90, getBase: () => $('#pages-sec').offsetHeight, onEnd: save('--pages-h') });
  setPanelOpen('left', store.get('panel.tex', true), { save: false });
  // at <= 1000 px the controls are an overlay drawer over the page: start closed there whatever was stored
  setPanelOpen('right', window.innerWidth > 1000 && store.get('panel.right', true), { save: false });
  $('#tg-left').addEventListener('click', () => togglePanel('left'));
  $('#tg-right').addEventListener('click', () => togglePanel('right'));
  if (store.get('drawer.open', false)) toggleDrawer(true);
  $('#auto-toggle').checked = S.auto;
}

// ───────────────────────────── top bar: git + compile ─────────────────────────────
function bindTopbar() {
  $('#btn-recompile').addEventListener('click', () => compilePaper());
  $('#git-pill').addEventListener('click', showGitInfo);
  $('#btn-fetch').addEventListener('click', () => gitAction('fetch', 'Fetched', $('#btn-fetch')));
  $('#btn-pull').addEventListener('click', once(doPull));
  $('#btn-commit').addEventListener('click', once(openCommitDialog));
  $('#btn-push').addEventListener('click', once(doPush));
  $('#btn-help').addEventListener('click', showShortcuts);
}

async function refreshGit() {
  try {
    S.git = await api.git.status();
  } catch (e) {
    S.git = { error: errText(e).title, detail: errText(e).detail };
  }
  renderGitPill();
}

function renderGitPill() {
  const g = S.git || {};
  const pill = $('#git-pill');
  const bad = !!g.error && !g.branch;
  pill.classList.toggle('err', bad);
  $('#git-branch').textContent = bad ? 'git unavailable' : (g.branch || 'detached');
  const ab = [];
  if (g.ahead) ab.push(`↑${g.ahead}`);
  if (g.behind) ab.push(`↓${g.behind}`);
  $('#git-ab').textContent = ab.join(' ');
  const n = realDirty(g).length;              // LaTeX build files (paper.aux, paper.pdf, ...) are not counted
  const nArt = (g.dirty || []).length - n;
  $('#git-dirty').textContent = n ? `● ${n}` : '';
  pill.title = bad ? `${g.error}${g.detail ? '\n' + g.detail : ''}` : `${g.branch || ''}${g.upstream ? ' → ' + g.upstream : ' (no upstream)'}\n${g.ahead || 0} ahead, ${g.behind || 0} behind, ${n} changed${nArt ? ` (+${nArt} LaTeX build files)` : ''}`;
  const off = bad || S.readonly;
  $('#btn-push').disabled = off; $('#btn-pull').disabled = off; $('#btn-fetch').disabled = off; $('#btn-commit').disabled = off;
}

/** Changed paths that are not untracked LaTeX build files (gitops flags those with `artifact`). */
function realDirty(g) { return ((g && g.dirty) || []).filter((d) => !d.artifact); }

function showGitInfo() {
  const g = S.git || {};
  if (g.error && !g.branch) { toast(g.error, { kind: 'err', detail: g.detail }); return; }
  const lc = g.last_commit || {};
  popover($('#git-pill'), h('div',
    h('div.kv',
      h('b', 'Branch'), h('span.mono', g.branch || '—'),
      h('b', 'Upstream'), h('span.mono', g.upstream || 'none'),
      h('b', 'Sync'), h('span', `${g.ahead || 0} ahead · ${g.behind || 0} behind`),
      h('b', 'Last commit'), h('span', lc.sha ? h('span', h('span.mono', String(lc.sha).slice(0, 7)), ' ', lc.subject || '', h('span.muted', lc.when ? ` · ${lc.when}` : '')) : '—')),
    (g.dirty || []).length ? h('div.dirty-list', (g.dirty || []).map((d) => h('div', h('span', (d.code || '').trim() || '?'), d.path))) : h('div.muted', { style: { marginTop: '8px' } }, 'Working tree clean')),
  { cls: 'info' });
}

function gitOk(r) { return r && r.ok !== false && !r.error; }
function gitMsg(r) { return (r && (r.error || r.stderr || r.output || r.stdout || r.message || r.detail)) || ''; }

async function gitAction(op, okTitle, btn) {
  if (roBlocked(`git ${op}`)) return null;
  if (btn) btn.disabled = true;
  try {
    const r = await api.git[op]();
    if (gitOk(r)) toast(okTitle, { kind: 'ok', detail: gitMsg(r) });
    else toast(`git ${op} failed`, { kind: 'err', detail: gitMsg(r) });
    return r;
  } catch (e) { toastError(e, `git ${op}`); return null; }
  finally { if (btn) btn.disabled = false; await refreshGit(); }
}

async function doPull() {
  if (modalOpen() || roBlocked('Pull')) return;
  if (S.texDirty && !(await confirmDialog('Pull with unsaved TeX edits?', 'Your unsaved edits stay in the editor, but the file on disk may change under them (saving then asks before overwriting anything).', 'Pull anyway'))) return;
  const r = await gitAction('pull', 'Pulled (fast-forward)', $('#btn-pull'));
  if (gitOk(r)) {
    await Promise.all([loadFigures(), loadSpecs()]);
    await checkDiskChange();      // reloads the open section if it changed (or warns when it has unsaved edits)
    toast('Recompile to see the pulled changes', { detail: 'Ctrl+Enter recompiles the paper.' });
  }
}

async function doPush() {
  if (modalOpen() || roBlocked('Push')) return;
  await refreshGit();
  const g = S.git || {};
  const body = h('div', { style: { display: 'grid', gap: '8px' } },
    h('p', 'This runs ', h('span.mono', 'git push'), ` on `, h('b', g.branch || '?'), g.upstream ? [' → ', h('span.mono', g.upstream)] : ' (no upstream configured)', '. Collaborators will see the commits in Overleaf.'),
    h('p', g.ahead ? `${g.ahead} commit${g.ahead === 1 ? '' : 's'} will be pushed.` : 'Nothing appears to be ahead of the upstream (fetch to be sure).'),
    realDirty(g).length ? h('p.muted', `${realDirty(g).length} uncommitted change(s) stay local.`) : null);
  // Nothing committed yet but files changed: pushing would send nothing (and used to toast "Pushed to Overleaf").
  // Offer to commit first instead.
  if (!g.ahead && realDirty(g).length) {
    const next = await modal({ title: 'Nothing to push yet', body: h('div', { style: { display: 'grid', gap: '8px' } },
      h('p', `Your ${realDirty(g).length} changed file(s) are not committed, so a push would send nothing to Overleaf.`),
      h('p', 'Commit them first, then push.')),
      actions: [{ label: 'Commit…', kind: 'primary', value: 'commit' }] });
    if (next === 'commit') openCommitDialog();
    return;
  }
  const ok = await modal({ title: 'Push to Overleaf?', body, actions: [{ label: 'Push', kind: 'primary', value: true }] });
  if (ok !== true) return;
  const before = g.ahead || 0;
  await gitAction('push', before ? `Pushed ${before} commit${before === 1 ? '' : 's'} to Overleaf` : 'Push finished — nothing was ahead of Overleaf', $('#btn-push'));
}

async function openCommitDialog() {
  if (roBlocked('Commit')) return;
  if (modalOpen()) return;
  await refreshGit();
  const g = S.git || {};
  const dirty = g.dirty || [];
  if (!dirty.length) { toast('Nothing to commit', { detail: 'The working tree is clean.' }); return; }
  const want = new Set([...S.editedTex, ...S.renderedOutputs].map(norm));
  if (S.spec && S.spec.output) { want.add(norm(S.spec.output)); want.add(norm(S.spec.output).replace(/\.pdf$/i, '.png')); }
  const isWanted = (p) => { p = norm(p); return want.has(p) || [...want].some((w) => p.endsWith('/') && w.startsWith(p)); };
  const mkBox = (d) => {
    const cb = h('input', { type: 'checkbox', checked: !d.artifact && isWanted(d.path) });
    cb._path = d.path;
    return h('label', cb, h('span.code', (d.code || '').trim() || '?'), h('span', d.path));
  };
  const boxes = dirty.filter((d) => !d.artifact).map(mkBox);
  const artBoxes = dirty.filter((d) => d.artifact).map(mkBox);   // build output: listed apart, never pre-checked
  const artGroup = artBoxes.length ? h('details.art-files', h('summary.muted', `${artBoxes.length} LaTeX build file${artBoxes.length === 1 ? '' : 's'} (paper.aux, paper.pdf, …) — normally not committed`), h('div.path-list', artBoxes)) : null;
  const msg = h('textarea.input', { rows: 2, spellcheck: 'true', value: S.spec ? `Update ${S.spec.id} figure` : 'Update figures' });
  const allBtn = h('button.btn.btn-xs', { type: 'button', on: { click: () => { const on = boxes.some((b) => !b.firstChild.checked); boxes.forEach((b) => { b.firstChild.checked = on; }); } } }, 'Toggle all');
  const body = h('div', { style: { display: 'grid', gap: '8px' } },
    h('div.row', { style: { justifyContent: 'space-between' } }, h('span', `Changed paths (${boxes.length}) — only checked paths are staged and committed.`), allBtn),
    boxes.length ? h('div.path-list', boxes) : h('p.muted', 'Only LaTeX build files changed.'),
    artGroup,
    h('label', { style: { color: 'var(--text-2)' } }, 'Message'), msg,
    h('p.muted', 'Commit stays local until you press Push.'));
  await modal({
    title: 'Commit to the paper repo', body, wide: true,
    actions: [{ label: 'Commit', kind: 'primary', busy: 'Committing…', onClick: async () => {
      const paths = [...boxes, ...artBoxes].map((b) => b.firstChild).filter((c) => c.checked).map((c) => c._path);
      if (!paths.length) { toast('Select at least one path', { kind: 'warn' }); return false; }
      if (!msg.value.trim()) { msg.classList.add('invalid'); msg.focus(); return false; }
      const r = await api.git.commit(paths, msg.value.trim());
      if (!gitOk(r)) { toast('Commit failed', { kind: 'err', detail: gitMsg(r) }); return false; }
      toast(`Committed ${paths.length} path${paths.length === 1 ? '' : 's'}`, { kind: 'ok', detail: gitMsg(r) });
      S.editedTex.clear(); S.renderedOutputs.clear();
      await refreshGit();
      return true;
    } }],
  });
}

// ── compile ──
async function compilePaper() {
  if (!S.backendOk) return null;
  if (S.compiling) { S.compileAgain = true; setCompileStatus('queued'); return S.compiling; }
  S.compiling = (async () => {
    S.compileT0 = Date.now();
    setCompileStatus('running');
    const timer = setInterval(() => setCompileStatus('running'), 1000);
    let result = null;
    try {
      const r = await api.compile();
      const jid = r && (r.job || r.job_id || r.id);
      let j = r;
      if (jid) {
        for (;;) {
          await sleep(900);
          j = await api.job(jid);
          if (['done', 'error', 'failed'].includes(j.status)) break;
        }
      }
      if (j.status === 'error' || j.status === 'failed') {
        result = j.result || { ok: false, errors: [], log_tail: j.error || j.detail || '' };
        if (!j.result) toast('Compile job failed', { kind: 'err', detail: j.error || j.detail || '' });
      } else result = j.result || j;
      S.lastCompile = result;
      showErrors(result, 'LaTeX');
      await loadFigures();
      await loadPaper({ keepView: true });
      setCompileStatus(result && result.ok !== false ? 'ok' : 'fail', result);
    } catch (e) {
      toastError(e, 'Compile failed'); setCompileStatus('fail');
    } finally {
      clearInterval(timer);
      refreshGit();
    }
    return result;
  })();
  const res = await S.compiling;
  S.compiling = null;
  if (S.compileAgain) { S.compileAgain = false; return compilePaper(); }
  return res;
}

function setCompileStatus(state, result) {
  const el = $('#compile-status');
  const prog = $('#progress');
  const btn = $('#btn-recompile');
  if (state === 'running' || state === 'queued') {
    const s = Math.round((Date.now() - S.compileT0) / 1000);
    el.replaceChildren(h('span.spinner'), `Compiling… ${s}s${S.compileAgain ? ' (+1 queued)' : ''}`);
    prog.classList.add('on'); btn.disabled = false;
  } else if (state === 'ok') {
    const secs = result && result.seconds ? ` in ${Number(result.seconds).toFixed(1)}s` : '';
    const w = result && result.warnings_count ? ` · ${result.warnings_count} warnings` : '';
    el.replaceChildren(h('span.ok', '✓'), `Compiled${secs}${w}`);
    prog.classList.remove('on');
  } else if (state === 'fail') {
    const n = result && result.errors ? result.errors.length : 0;
    el.replaceChildren(h('span.bad', '✕'), n ? `${n} LaTeX error${n === 1 ? '' : 's'}` : 'Compile failed');
    prog.classList.remove('on');
  } else { el.replaceChildren(); prog.classList.remove('on'); }
  el.title = el.textContent;
}

// ── LaTeX error panel ──
function showErrors(result, kind = 'LaTeX') {
  const errs = (result && result.errors) || [];
  const failed = result && result.ok === false;
  const chip = $('#err-chip');
  if (!errs.length && !failed) {
    if (kind === 'LaTeX') { $('#err-panel').classList.add('hidden'); chip.classList.add('hidden'); }
    return;
  }
  $('#err-title').textContent = errs.length ? `${kind}: ${errs.length} error${errs.length === 1 ? '' : 's'}` : `${kind}: compile failed`;
  $('#err-list').replaceChildren(...errs.map((e) => h('li',
    e.file ? h('a', { title: 'Open in the editor', on: { click: () => openSource(norm(e.file), Number(e.line) || 0) } }, `${norm(e.file)}${e.line ? ':' + e.line : ''}`) : h('span.muted', '—'),
    h('span.msg', e.message || ''))));
  if (!errs.length) $('#err-list').append(h('li', h('span.msg', 'No parsable error lines; see the log tail.')));
  const tail = $('#err-tail');
  tail.textContent = (result && result.log_tail) || '';
  tail.classList.toggle('hidden', !!errs.length || !tail.textContent);
  $('#err-log').classList.toggle('hidden', !tail.textContent);
  $('#err-panel').classList.remove('hidden');
  chip.textContent = `✕ ${errs.length || 1} ${kind} error${errs.length === 1 ? '' : 's'}`;
  chip.classList.remove('hidden');
}

// ───────────────────────────── center: modes, page, overlay ─────────────────────────────
function bindCenter() {
  $$('#mode-seg button').forEach((b) => b.addEventListener('click', () => setMode(b.dataset.mode)));
  $('#pg-prev').addEventListener('click', () => pageStep(-1));
  $('#pg-next').addEventListener('click', () => pageStep(1));
  $('#zm-in').addEventListener('click', () => zoom(1.2));
  $('#zm-out').addEventListener('click', () => zoom(1 / 1.2));
  $('#zm-label').addEventListener('click', () => zoomFit());
  $('#zm-fig').addEventListener('click', () => zoomFigure());
  $('#live-toggle').addEventListener('change', (e) => {
    S.liveOverlay = e.target.checked;
    if (S.liveOverlay && S.spec) { S.previewActive = true; if (!S.preview.url) runPreview(); }
    updateOverlay();
  });
  const cmp = $('#btn-compare');
  const on = () => { cmp.classList.add('held'); viewer.setComparing(true); };
  const off = () => { cmp.classList.remove('held'); viewer.setComparing(false); };
  cmp.addEventListener('pointerdown', on); cmp.addEventListener('pointerup', off); cmp.addEventListener('pointerleave', off);
  $('#btn-insert').addEventListener('click', openInsertDialog);
  $('#err-close').addEventListener('click', () => $('#err-panel').classList.add('hidden'));
  $('#err-log').addEventListener('click', () => $('#err-tail').classList.toggle('hidden'));
  $('#err-chip').addEventListener('click', () => $('#err-panel').classList.toggle('hidden'));
  new ResizeObserver(debounce(() => { if (S.mode === 'plot') updatePlotOnly(); }, 100)).observe($('#stage'));
}

function setMode(m, { auto = false } = {}) {
  if (!['page', 'context', 'plot'].includes(m)) return;
  const prev = S.mode;
  S.mode = m; S.modeAuto = auto;
  $$('#mode-seg button').forEach((b) => b.classList.toggle('on', b.dataset.mode === m));
  if (m === 'context' && (!S.ctx || S.ctx.fid !== S.selFid) && S.selFid) compileContext();
  if (m === 'plot' && S.spec && !S.preview.url) runPreview();
  updateStage();
  if (m === 'page' && prev !== 'page' && S.pdf) {
    // the page may have been laid out while hidden
    requestAnimationFrame(() => { if (viewer.fit === 'width') viewer.fitWidth(); const f = selFig(); if (f && f.box && f.box.page === viewer.pageNum) viewer.focusBox(f.box); });
  }
}

function empty(icon, title, text, ...actions) {
  return [h('div.big-ico', icon), h('h3', title), text ? h('p', text) : null, actions.length ? h('div.row.gap6', actions) : null];
}

function updateStage() {
  const m = S.mode;
  const pageEl = $('#page'), ctxEl = $('#ctx-pages'), plotEl = $('#plot-only'), emptyEl = $('#stage-empty'), banner = $('#stage-banner');
  let showPage = false, showCtx = false, showPlot = false, emptyKids = null, bannerKids = null;
  const fig = selFig();
  if (!S.backendOk && S.config === null && S.pdfError) {
    emptyKids = empty('!', "Can't reach the designer backend", S.pdfError,
      h('button.btn.btn-primary', { on: { click: () => location.reload() } }, 'Retry'));
  } else if (m === 'page') {
    if (S.pdfState === 'ok') {
      showPage = true;
      if (S.spec && !figForSpec(S.spec.id) && !S.selFid) bannerKids = ['This spec is not in the paper yet.', h('button.btn', { on: { click: openInsertDialog } }, 'Insert into paper…'), h('button.btn', { on: { click: () => setMode('plot') } }, 'Plot only')];
      else if (fig && !fig.box && !fig.commented) bannerKids = [`Position of ${figLabel(fig)} unknown (PDF stale or figure not placed).`, h('button.btn', { on: { click: () => compilePaper() } }, 'Recompile')];
      else if (fig && fig.commented) bannerKids = [`${figLabel(fig)} is commented out in the source.`];
    } else if (S.pdfState === 'loading' || S.pdfState === 'idle') emptyKids = [h('span.spinner'), h('p', 'Loading paper…')];
    else if (S.pdfState === 'missing') emptyKids = empty('⎙', 'No compiled PDF yet', 'Compile the paper once to see its pages, locate figures and overlay live previews.',
      h('button.btn.btn-primary', { on: { click: () => compilePaper() } }, 'Recompile paper'));
    else emptyKids = empty('!', 'Could not load the paper PDF', S.pdfError || '', h('button.btn', { on: { click: () => loadPaper() } }, 'Retry'), h('button.btn.btn-primary', { on: { click: () => compilePaper() } }, 'Recompile'));
  } else if (m === 'context') {
    if (!S.selFid || !fig) emptyKids = empty('¶', 'Context preview', 'Select a figure that is in the paper to compile it with its surrounding paragraphs.');
    else if (!S.ctx || S.ctx.fid !== S.selFid) emptyKids = empty('¶', 'Context preview', `Fast compile of ${figLabel(fig)} with the text around it.`, h('button.btn.btn-primary', { on: { click: compileContext } }, 'Compile context'));
    else if (S.ctx.loading) emptyKids = [h('span.spinner'), h('p', `Compiling context for ${figLabel(fig)}…`)];
    else if (S.ctx.error) emptyKids = empty('!', 'Context compile failed', S.ctx.error.title + (S.ctx.error.detail ? ` — ${S.ctx.error.detail}` : ''), h('button.btn', { on: { click: compileContext } }, 'Retry'));
    else showCtx = true;
  } else if (m === 'plot') {
    if (!S.spec) emptyKids = empty('◆', fig ? 'Not a designer figure' : 'No spec selected', fig ? `${figLabel(fig)} was not made with the designer, so there is no plot to show.` : 'Pick a spec on the right, or create one with New.');
    else { showPlot = true; updatePlotOnly(); }
  }
  pageEl.classList.toggle('hidden', !showPage);
  ctxEl.classList.toggle('hidden', !showCtx);
  plotEl.classList.toggle('hidden', !showPlot);
  emptyEl.classList.toggle('hidden', !emptyKids);
  if (emptyKids) emptyEl.replaceChildren(...emptyKids.filter(Boolean));
  banner.classList.toggle('hidden', !bannerKids);
  if (bannerKids) banner.replaceChildren(...bannerKids);
  $('#btn-insert').classList.toggle('hidden', !(S.spec && !figForSpec(S.spec.id)));
  const ctxBtn = $('#mode-seg button[data-mode=context]');
  ctxBtn.disabled = !(fig && !fig.commented);
  $('#zm-fig').disabled = !(m === 'page' && fig && fig.box);
  updatePager(); updateZoomLabel(); updateOverlay();
  if (showPlot) { const st = $('#stage'); if (st.scrollWidth <= st.clientWidth + 1) st.scrollLeft = 0; }
  document.title = `Plot Designer${S.spec ? ' — ' + S.spec.id : fig ? ' — ' + figLabel(fig) : ''}`;
}

function updatePager() {
  const lbl = $('#pg-label');
  const active = S.mode === 'page' && S.pdf;
  lbl.textContent = active ? `${viewer.pageNum} / ${viewer.numPages}` : S.mode === 'context' && S.ctx && S.ctx.doc ? `${S.ctx.doc.numPages} p` : '–';
  $('#pg-prev').disabled = !active || viewer.pageNum <= 1;
  $('#pg-next').disabled = !active || viewer.pageNum >= viewer.numPages;
}

function curScale() { return S.mode === 'plot' ? S.plotZoom : S.mode === 'context' ? (S.ctxScale || 1.5) : viewer.scale; }
function updateZoomLabel() {
  const el = $('#zm-label');
  el.textContent = Math.round((curScale() / PT_100) * 100) + '%';
  el.title = 'Zoom: 100% = physical size (1 pt = 1/72 in at 96 dpi). Click to fit the width (0).';
}

function pageStep(d) {
  if (S.mode !== 'page' || !S.pdf) return;
  viewer.show(viewer.pageNum + d);
}

const rerenderCtx = debounce(() => { if (S.ctx && S.ctx.doc) renderStack(S.ctx.doc, $('#ctx-pages'), S.ctxScale); }, 160);
function zoom(f, anchor) {
  refreshPreviewDpi();
  if (S.mode === 'page') viewer.zoomBy(f, anchor);
  else if (S.mode === 'plot') { S.plotZoom = clamp(S.plotZoom * f, 0.5, 12); store.set('plotZoom.v2', S.plotZoom); updatePlotOnly(); }
  else if (S.mode === 'context' && S.ctx && S.ctx.doc) {
    S.ctxScale = clamp((S.ctxScale || 1.5) * f, MIN_SCALE, MAX_SCALE);
    $$('#ctx-pages .page').forEach((p) => { const c = p.firstChild; const w = parseFloat(p.style.width) * f, hh = parseFloat(p.style.height) * f; p.style.width = c.style.width = w + 'px'; p.style.height = c.style.height = hh + 'px'; });
    rerenderCtx();
  }
  updateZoomLabel();
}
function zoomFit() {
  if (S.mode === 'page') viewer.fitWidth();
  // user-requested fits are one-shot: the zoom stays put afterwards (no refit on resize or spec change)
  else if (S.mode === 'plot') { S.plotZoom = fitPlotZoom(); store.set('plotZoom.v2', S.plotZoom); updatePlotOnly(); }
  else if (S.mode === 'context' && S.ctx && S.ctx.doc) { S.ctxScale = null; renderContextStack(); }
  updateZoomLabel();
}
function zoomFigure() {
  const f = selFig();
  const done = () => { updateZoomLabel(); refreshPreviewDpi(); };
  if (f && f.box) { setMode('page'); viewer.focusBox(f.box, { zoom: true }); done(); }
}

function fitPlotZoom() {
  const st = (S.spec && S.spec.style) || {};
  const w = Number(st.width_pt) || 252, hh = Number(st.height_pt) || 100;
  const stage = $('#stage');
  return clamp(Math.min((stage.clientWidth - 80) / w, (stage.clientHeight - 140) / hh), 0.5, 6);
}

function updatePlotOnly() {
  if (S.mode !== 'plot' || !S.spec) return;
  const st = S.spec.style || {};
  const p = S.preview;
  if (S.plotFit) S.plotZoom = fitPlotZoom();
  const w = p.w || Number(st.width_pt) || 252, hh = p.h || Number(st.height_pt) || 100;
  const img = $('#po-img');
  if (p.url) { if (img.getAttribute('src') !== p.url) img.src = p.url; img.classList.remove('hidden'); } else img.classList.add('hidden');
  img.style.width = w * S.plotZoom + 'px'; img.style.height = hh * S.plotZoom + 'px';
  img.parentElement.style.minWidth = w * S.plotZoom + 'px'; img.parentElement.style.minHeight = hh * S.plotZoom + 'px';
  img.style.opacity = p.state === 'loading' ? '.6' : '1';
  $('#po-dims').textContent = `${fmt(w)} × ${fmt(hh)} pt · ${(w / 72).toFixed(2)} × ${(hh / 72).toFixed(2)} in · ${Math.round((S.plotZoom / PT_100) * 100)}%`;
  const note = $('#po-note');
  note.replaceChildren();
  if (p.state === 'failed' && p.error) {
    note.append(h('div', { style: { color: 'var(--err)' } }, `Preview failed: ${p.error.title}`), p.error.detail ? h('div.mono', { style: { color: 'var(--err)' } }, p.error.detail) : null);
    // a dataset-backed spec whose dataset is not registered yet: point at the Data drawer
    if (/no dataset named/i.test(p.error.detail || '')) note.append(h('div', { style: { marginTop: '6px' } }, h('button.btn.btn-sm', { on: { click: () => toggleDrawer(true) } }, 'Register it in Data…')));
  }
  else if (!figForSpec(S.spec.id)) note.append(h('span', 'Not in the paper yet. '), h('button.btn.btn-sm', { on: { click: openInsertDialog } }, 'Insert into paper…'));
  updateZoomLabel();
}
const fmt = (v) => (Math.round(v * 10) / 10).toString();

function updateBoxes() {
  const list = S.figures.filter((f) => f.box && !f.commented).map((f) => ({ fid: f.fid, box: f.box, label: figLabel(f), spec: !!f.spec_id }));
  viewer.setBoxes(list, S.selFid);
  thumbs.setBoxes(list, S.selFid);
}

function updateOverlay() {
  const fig = selFig();
  const p = S.preview;
  if (S.mode !== 'page' || !S.spec || !fig || fig.spec_id !== S.spec.id || !fig.box || !S.liveOverlay || !S.previewActive || (!p.url && p.state !== 'failed')) {
    viewer.setOverlay(null); return;
  }
  const out = norm(S.spec.output);
  const g = (fig.graphics || []).find((x) => norm(x.resolved) === out) || (fig.graphics || [])[0] || {};
  // the box is in PDF points (bp, 1/72 in); width_pt / height_pt are TeX points (1/72.27 in), as paperplot sizes the
  // figure (a 180 pt figure has a 179.33 bp MediaBox). Matters for scale= / natural size, not for width= (ratio only).
  const BP_PER_PT = 72 / 72.27;
  const rect = overlayRect(fig.box, g.options, p.w * BP_PER_PT, p.h * BP_PER_PT);
  const tag = p.state === 'failed' ? 'preview failed' : `live · ${fmt(p.w)}×${fmt(p.h)} pt${rect.mode === 'width' ? ' · width fixed by TeX' : rect.mode === 'scale' ? ` · scale ${(g.options || '').match(/scale\s*=\s*([\d.]+)/)?.[1] || ''}` : ''}`;
  const stale = (fig.box.stale ? ' · TeX edited since compile' : '') + (p.state === 'ok' && layoutWarnings(p.warnings).length ? ' · ⚠ clipped' : '');
  viewer.setOverlay({ box: fig.box, rect, url: p.url, tag: tag + stale, state: p.state });
}

// ── page click → select / synctex reverse ──
async function onPageClick({ page, x, y, fid }) {
  if (fid) { selectFigure(fid, { focus: false }); return; }
  if (x == null) return;
  try {
    const r = await api.reverse(page, Math.round(x * 100) / 100, Math.round(y * 100) / 100);
    if (!r || !r.file) {
      // no figure has a box ⇒ there is no synctex for this PDF (built outside the designer): say how to fix it
      if (!S.figures.some((f) => f.box)) toast('Click-to-source needs synctex data — click “Recompile paper” once.', { kind: 'warn' });
      else setMsg('No source found at that spot');
      return;
    }
    S.lastReverse = { file: norm(r.file), line: Number(r.line) || 0 };
    setMsg(`↳ ${S.lastReverse.file}:${S.lastReverse.line}`);
    openSource(S.lastReverse.file, S.lastReverse.line, { fromClick: true });
  } catch (e) { toastError(e, 'synctex'); }
}

/** Jump to file:line: opens that section in the editor with the cursor on the line. A click on a figure's text
 * (caption, ...) also selects that figure. */
async function openSource(file, line, { fromClick = false } = {}) {
  file = norm(file);
  if (S.sections.length && !sectionByPath(file)) {
    toast(`${file}${line ? ':' + line : ''}`, { detail: 'Not one of the paper\'s editable TeX files (only the main file and its \\input/\\include sections).', ms: 4000 });
    return;
  }
  const f = fromClick ? S.figures.find((x) => norm(x.file) === file && !x.commented && line >= x.begin_line && line <= x.end_line) : null;
  if (f) { await selectFigure(f.fid, { focus: false, texLine: line }); return; }
  setLeftOpen(true);
  await openSection(file, { line: line || 1 });
}

// ───────────────────────────── figure list ─────────────────────────────
function renderFigList() {
  const host = $('#fig-list');
  const q = ($('#fig-filter').value || '').toLowerCase();
  const match = (...xs) => !q || xs.some((x) => String(x || '').toLowerCase().includes(q));
  const groups = new Map();
  for (const f of S.figures) {
    if (!match(f.label, f.caption, f.file, f.spec_id, ...(f.graphics || []).map((g) => g.path))) continue;
    if (!groups.has(f.file)) groups.set(f.file, []);
    groups.get(f.file).push(f);
  }
  const out = [];
  for (const [file, figs] of groups) {
    out.push(h('div.fl-group', h('div.fl-group-head', h('span', sectionName(file)), h('span', figs.length)),
      figs.map((f) => h('div.fl-item' + (f.fid === S.selFid ? '.on' : '') + (f.commented ? '.commented' : ''), {
        dataset: { fid: f.fid }, title: `${f.fid}\n${plainTex(f.caption)}`,
        on: { click: () => { closeNav(); selectFigure(f.fid, { zoom: true }); } },
      },
      h('div.fl-ico' + (f.spec_id ? '.spec' : ''), f.spec_id ? '◆' : f.env === 'figure*' ? '▭' : '▢'),
      h('div.fl-body', h('div.fl-label', figLabel(f), graphicsStem(f) && graphicsStem(f) !== figLabel(f) ? h('span.fl-file', ' · ' + graphicsStem(f)) : null),
        h('div.fl-cap', plainTex(f.caption) || (f.graphics || []).map((g) => g.path).join(', '))),
      h('div.fl-meta', f.box ? h('span.fl-page', `p.${f.box.page}`) : h('span.fl-page', '—'),
        f.commented ? h('span.badge', '%') : f.spec_id ? h('span.badge.spec', 'spec') : null)))));
  }
  const orphan = S.specs.filter((s) => !figForSpec(s.id) && match(s.id, s.title, s.output));
  if (orphan.length) {
    out.push(h('div.fl-group', h('div.fl-group-head', h('span', 'Specs not in the paper'), h('span', orphan.length)),
      orphan.map((s) => h('div.fl-item' + (!S.selFid && S.spec && S.spec.id === s.id ? '.on' : ''), { title: s.output || '', on: { click: () => { closeNav(); selectSpec(s.id); } } },
        h('div.fl-ico.spec', '◆'), h('div.fl-body', h('div.fl-label', s.id), h('div.fl-cap', s.title || s.output || '')),
        h('div.fl-meta', h('span.badge.warn', 'new'))))));
  }
  if (!out.length) out.push(h('div.empty.small', S.figures.length || S.specs.length ? 'No matches.' : 'No figures found in the paper.'));
  host.replaceChildren(...out);
}

function sectionName(file) {
  if (!file) return '?';
  if (S.config && file === norm(S.config.main_tex)) return `${file} (main)`;
  const sec = sectionByPath(file);
  if (sec && sec.heading && !sec.main) return sec.heading;
  const base = file.split('/').pop().replace(/\.tex$/, '');
  const m = /^(\d+)_?(.*)$/.exec(base);
  return m ? `${m[1]} · ${m[2].replace(/_/g, ' ')}` : base.replace(/_/g, ' ');
}

// ───────────────────────────── selection ─────────────────────────────
async function selectFigure(fid, { focus = true, texLine = 0 } = {}) {
  const fig = figById(fid);
  if (!fig) return;
  // the figure's section opens in the editor: ask first if that would leave unsaved edits in another file
  if (S.tex && S.tex.path !== norm(fig.file) && !(await confirmLeaveSection(`open ${fig.file}`))) return;
  S.selFid = fid;
  store.set('sel', { fid });
  viewer.setSelected(fid); thumbs.setSelected(fid);
  $$('#fig-list .fl-item').forEach((el) => el.classList.toggle('on', el.dataset.fid === fid));
  if (fig.box) {
    if (S.mode === 'plot' && S.modeAuto) setMode('page');
    if (S.mode === 'page' && S.pdf) {
      // scroll to it (all pages are one column); zoom changes only on user zoom actions (F zooms to it). A click on
      // the page (focus: false) leaves the scroll where it is.
      if (focus) viewer.focusBox(fig.box);
    }
  }
  // whole section in the editor, scrolled to the figure environment (or the clicked line)
  openSection(fig.file, texLine ? { line: texLine, confirm: false } : { range: [fig.begin_line, fig.end_line], confirm: false });
  if (fig.spec_id) {
    if (!S.spec || S.spec.id !== fig.spec_id) await loadSpec(fig.spec_id);
  } else {
    await unloadSpec();
    showSpecNote(fig);
  }
  if (S.mode === 'context') { if (!fig.commented) compileContext(); else S.ctx = null; }
  renderFigList(); updateStage();
}

async function selectSpec(id) {
  const fig = figForSpec(id);
  if (fig) return selectFigure(fig.fid, { zoom: true });
  S.selFid = null;
  store.set('sel', { spec: id });
  viewer.setSelected(null); thumbs.setSelected(null);
  ed.clearHighlight();          // the section stays open in the editor
  await loadSpec(id);
  setMode('plot', { auto: true });
  renderFigList(); updateStage();
}

// ───────────────────────────── spec panel ─────────────────────────────
function bindRight() {
  $('#spec-picker').addEventListener('change', (e) => { if (e.target.value) selectSpec(e.target.value); });
  $('#spec-new').addEventListener('click', () => openNewSpecDialog());
  $('#spec-menu').addEventListener('click', (e) => menu(e.currentTarget, [
    { label: 'Duplicate…', disabled: !S.spec, onClick: () => openNewSpecDialog({ template: S.spec && S.spec.id }) },
    { label: 'Reload from disk', disabled: !S.spec, onClick: () => S.spec && loadSpec(S.spec.id, { force: true }) },
    { label: 'Copy output path', disabled: !S.spec, onClick: () => navigator.clipboard && navigator.clipboard.writeText(S.spec.output || '').then(() => toast('Copied', { kind: 'ok' })) },
    '-',
    { label: 'Delete spec…', danger: true, disabled: !S.spec, onClick: deleteSpec },
  ], { align: 'right' }));
  $('#btn-render').addEventListener('click', renderAndRecompile);
  $('#save-status').addEventListener('click', () => { if (S.specConflict) resolveSpecConflict(); });
  $('#right-close').addEventListener('click', () => setPanelOpen('right', false, { save: window.innerWidth > 1000 }));
  $('#auto-toggle').addEventListener('change', (e) => { S.auto = e.target.checked; store.set('autoRecompile', S.auto); if (!S.auto) autoRecompile.cancel(); });
  renderSpecPanel();
}

function renderSpecPicker() {
  const sel = $('#spec-picker');
  const cur = S.spec ? S.spec.id : '';
  sel.replaceChildren(h('option', { value: '' }, S.specs.length ? '— select a spec —' : 'No specs yet'), ...S.specs.map((s) => h('option', { value: s.id }, s.id)));
  sel.value = S.specs.some((s) => s.id === cur) ? cur : '';
}

function renderSpecPanel() {
  const has = !!S.spec;
  $('#btn-render').disabled = !has || S.readonly;
  $('#spec-menu').disabled = !has;
  const meta = $('#spec-meta');
  const fields = $('#spec-fields');
  if (!has) {
    meta.replaceChildren();
    fields.replaceChildren();
    controls.container.replaceChildren();
    dataEditor.build(null);
    $('#stats-box').replaceChildren();
    if (!S.selFid) showSpecNote(null);
    return;
  }
  $('#spec-note').classList.add('hidden');
  updateSpecMeta();
  const title = h('input.input.input-xs', { value: S.spec.title || '', placeholder: 'Title (for you; not drawn)', spellcheck: 'true' });
  title.addEventListener('input', () => { S.spec.title = title.value; specEdited({ noPreview: true }); });
  const output = h('input.input.input-xs.mono', { value: S.spec.output || '', placeholder: 'figures/evals/<id>.pdf', spellcheck: 'false', title: 'Output PDF, relative to the paper root (a PNG is written beside it)' });
  output.addEventListener('input', () => { S.spec.output = output.value; specEdited({ noPreview: true }); });
  const kinds = ((S.controlsCache[(S.spec.style && S.spec.style.theme) || ''] || Object.values(S.controlsCache)[0] || {}).kinds) || ['grouped_bars', 'lines'];
  const kindSel = h('select.input.input-xs', { title: 'Chart kind: grouped bars (optionally stacked panels) or one line per arm' },
    [...new Set([...kinds, S.spec.kind || 'grouped_bars'])].map((k) => h('option', { value: k }, k === 'grouped_bars' ? 'grouped bars' : k)));
  kindSel.value = S.spec.kind || 'grouped_bars';
  kindSel.addEventListener('change', () => changeKind(kindSel.value).then((ok) => { if (!ok) kindSel.value = S.spec.kind || 'grouped_bars'; }));
  fields.replaceChildren(
    h('div.ctl', h('label.lbl', 'Title'), h('div.ctl-in', title)),
    h('div.ctl', h('label.lbl', { title: 'Relative to the paper root' }, 'Output'), h('div.ctl-in', output)),
    h('div.ctl', h('label.lbl', 'Kind'), h('div.ctl-in', kindSel)));
}

/** Switch the open spec to another kind: style = that kind's defaults, keeping every shared key whose value fits it. */
async function changeKind(kind) {
  const spec = S.spec;
  if (!spec || (spec.kind || 'grouped_bars') === kind) return true;
  if (kind !== 'grouped_bars' && spec.data && Array.isArray(spec.data.panels)) {
    toast('Only grouped bars have panels', { kind: 'warn', detail: 'Use “Back to one chart” in the Data section (with a single panel left) before switching to ' + kind + '.' });
    return false;
  }
  let ctl;
  try { ctl = await getControls(spec.style.theme); } catch (e) { toastError(e, 'Could not load controls'); return false; }
  if (S.spec !== spec) return false;
  const rows = (ctl.style_by_kind || {})[kind], defs = (ctl.defaults_by_kind || {})[kind];
  if (!rows || !defs) { toast(`Unknown kind ${kind}`, { kind: 'err' }); return false; }
  const fits = (row, v) => {
    if (v == null) return !!row.nullable || row.default == null;
    if (row.type === 'dropdown' && Array.isArray(row.options) && row.options.length && row.key !== 'font_family' && row.key !== 'theme') return row.options.some((o) => (o && typeof o === 'object' ? o.value : o) === v);
    if (row.type === 'slider' || row.type === 'number') return typeof v === 'number' && (row.min == null || v >= row.min) && (row.max == null || v <= row.max);
    if (row.type === 'toggle') return typeof v === 'boolean';
    return true;
  };
  const old = spec.style || {}, style = {}, kept = [];
  for (const row of rows) {
    if (row.key in old && fits(row, old[row.key])) { style[row.key] = old[row.key]; kept.push(row.key); } else style[row.key] = clone(defs[row.key]);
  }
  spec.kind = kind; spec.style = style; S.specBaseline = clone(style);
  dataEditor.build(spec);
  controls.build(kindRows(ctl, spec), spec.style, S.specBaseline, ctl);
  updateSpecMeta();
  toast(`Kind: ${kind}`, { kind: 'ok', detail: `Kept ${kept.length} style settings that also exist for ${kind}; the others are ${kind}'s defaults.` });
  specEdited();
  return true;
}

function updateSpecMeta() {
  if (!S.spec) return;
  const fig = figForSpec(S.spec.id);
  const where = S.config && S.config.specs_dir ? `${S.config.specs_dir.replace(/\\/g, '/')}/${S.spec.id}.yaml` : `${S.spec.id}.yaml`;
  $('#spec-meta').replaceChildren(h('span.badge', S.spec.kind || 'grouped_bars'),
    fig ? h('span.badge.ok', `in paper · p.${fig.box ? fig.box.page : '?'}`) : h('span.badge.warn', 'not in paper'),
    h('span.badge', { title: `The spec is stored in the designer state dir:\n${where}\nIt is not part of the paper repo, so Commit/Push only carry the rendered PDF/PNG.` }, 'spec: designer state (not in git)'));
}

function showSpecNote(fig) {
  const note = $('#spec-note');
  if (fig) {
    const g = (fig.graphics || [])[0];
    note.replaceChildren(h('b', 'This figure is not designer-made.'), h('div', { style: { marginTop: '4px' } },
      g ? ['It includes ', h('span.mono', g.path), '. Its LaTeX is editable below; to design it here, create a spec that writes this file.'] : 'It has no \\includegraphics. Its LaTeX is editable below.'),
    g && g.resolved ? h('button.btn.btn-sm', { on: { click: () => {
      const id = figLabel(fig).toLowerCase().replace(/[^a-z0-9_]+/g, '_');
      const dir = g.resolved.includes('/') ? g.resolved.slice(0, g.resolved.lastIndexOf('/') + 1) : '';
      openNewSpecDialog({ id, output: `${dir}${id}_designer.pdf`, replaces: g.resolved });
    } } }, 'New spec for this figure…') : null);
  } else {
    note.replaceChildren(h('b', 'No spec selected.'), h('div', { style: { marginTop: '4px' } }, 'Select a figure marked ◆ (designer-made), pick a spec above, or create one with New.'));
  }
  note.classList.remove('hidden');
}

function showSpecErrors(errs) {
  const box = $('#spec-errors');
  S.specErrors = errs && errs.length ? errs : null;
  if (!S.specErrors) { box.classList.add('hidden'); return; }
  box.replaceChildren(h('b', `Spec not saved — ${errs.length} problem${errs.length === 1 ? '' : 's'}:`), h('ul', errs.map((e) => h('li', typeof e === 'string' ? e : JSON.stringify(e)))));
  box.classList.remove('hidden');
}

async function getControls(theme) {
  const k = theme || '';
  if (!S.controlsCache[k]) {
    const r = await api.controls(theme);
    S.controlsCache[k] = Array.isArray(r) ? { style: r } : (r || { style: [] });
  }
  return S.controlsCache[k];
}

/** The style control rows for the spec's kind (grouped_bars / lines / ...): controls() returns every kind's rows. */
const kindRows = (ctl, spec) => (ctl.style_by_kind && ctl.style_by_kind[(spec && spec.kind) || 'grouped_bars']) || ctl.style || [];

/** Arms, levels and evaluated cells of the open spec (for the arm-order and group-color editors). */
function specDataSummary() {
  const out = { arms: [], colors: {}, levels: [], cells: {} };
  const d = S.spec && S.spec.data;
  if (!d) return out;
  const addLevel = (l) => { const name = l && typeof l === 'object' ? String(l.label ?? l.key) : String(l); if (!out.levels.includes(name)) out.levels.push(name); };
  const addCells = (name, counts) => {
    const cs = out.cells[name] || (out.cells[name] = []);
    for (const c of counts || []) { const p = countParts(c); if (p && Number.isFinite(p.k) && p.n > 0) cs.push([p.k, p.n]); }
  };
  if (d.source === 'dataset') {
    const q = d.query || {};
    for (const a of q.arms || []) { const n = a.name ?? String(a.key); out.arms.push(n); out.colors[n] = a.color; }
    for (const l of q.levels || []) addLevel(l);
    for (const s of (S.lastStats && S.lastStats.stats) || []) if (s.k != null && s.n) (out.cells[s.arm] || (out.cells[s.arm] = [])).push([s.k, s.n]);
    return out;
  }
  for (const a of d.arms || []) { if (!out.arms.includes(a.name)) out.arms.push(a.name); out.colors[a.name] = a.color; }
  for (const l of d.levels || []) addLevel(l);
  if (Array.isArray(d.panels)) {
    for (const p of d.panels) {
      for (const l of p.levels || []) addLevel(l);
      for (const a of p.arms || []) {
        if (!out.arms.includes(a.name)) { out.arms.push(a.name); if (a.color) out.colors[a.name] = a.color; }
        addCells(a.name, a.counts);
      }
    }
  } else for (const a of d.arms || []) addCells(a.name, a.counts);
  return out;
}

async function loadSpec(id, { force = false } = {}) {
  if (S.spec && S.spec.id === id && !force) return;
  await saveNow();
  let spec, hash;
  try { ({ spec, hash } = await api.getSpecVersioned(id)); } catch (e) { toastError(e, `Could not load spec ${id}`); return; }
  S.spec = spec; S.specHash = hash; S.specConflict = null;
  spec.style = spec.style || {};
  S.specBaseline = clone(spec.style);
  S.editSeq = 0; S.savedSeq = 0; S.saveOk = true;
  S.previewActive = false; S.lastStats = null;
  if (previewAbort) previewAbort.abort();
  S.previewSeq++; setPreviewUrl(null);
  Object.assign(S.preview, { state: 'idle', w: Number(spec.style.width_pt) || 0, h: Number(spec.style.height_pt) || 0, error: null });
  showSpecErrors(null);
  renderSpecPicker(); renderSpecPanel();
  try {
    const ctl = await getControls(spec.style.theme);
    if (S.spec !== spec) return;
    dataEditor.build(spec);          // first: it may normalise data.panels, which the arm-order editor reads
    controls.build(kindRows(ctl, spec), spec.style, S.specBaseline, ctl);
  } catch (e) { toastError(e, 'Could not load controls'); controls.container.replaceChildren(h('div.empty.small', 'Controls unavailable.')); }
  if (S.spec === spec && dataEditor.spec !== spec) dataEditor.build(spec);
  renderStats();
  updateSaveUI();
  runPreview();
}

async function unloadSpec() {
  if (!S.spec) return;
  await saveNow();
  S.spec = null; S.specBaseline = null; S.previewActive = false;
  setPreviewUrl(null); S.preview.state = 'idle';
  setPreviewStatus();
  showSpecErrors(null);
  renderSpecPicker(); renderSpecPanel(); updateSaveUI();
}

async function onControlChange(key) {
  if (key === 'theme' && S.spec) {
    try {
      const spec = S.spec;
      const ctl = await getControls(spec.style.theme);
      if (S.spec === spec) controls.build(kindRows(ctl, spec), spec.style, S.specBaseline, ctl);
      dataEditor.build(spec);
    } catch (e) { toastError(e, 'Could not load controls for theme'); }
  }
  specEdited();
}

function specEdited({ noPreview = false } = {}) {
  if (!S.spec) return;
  S.editSeq++;
  updateSaveUI();
  scheduleSave();
  if (!noPreview) { S.previewActive = true; schedulePreview(); }
  if (!noPreview && S.mode === 'context' && S.selFid && figForSpec(S.spec.id) && figForSpec(S.spec.id).fid === S.selFid) scheduleContext();
  if (S.auto) autoRecompile();
}
const scheduleContext = debounce(() => { if (S.mode === 'context' && S.spec) compileContext(); }, 900);

// ── preview ──
let previewAbort = null;
const schedulePreview = debounce(() => runPreview(), 120);

/** Preview resolution for the current zoom (device px per inch), in steps of 50 so small zooms do not refetch. */
function previewDpi() {
  const s = S.mode === 'page' || S.mode === 'plot' ? curScale() : 1.5;
  const want = s * 72 * (window.devicePixelRatio || 1);
  return clamp(Math.ceil(want / 50) * 50, 150, 450);
}
/** After a zoom settles: refetch the overlay if it is now shown sharper than it was rendered. */
const refreshPreviewDpi = debounce(() => {
  if (!S.spec || !S.previewActive || S.preview.state !== 'ok' || !S.preview.url) return;
  if (previewDpi() > (S.preview.dpi || 0)) runPreview();
}, 350);

function setPreviewUrl(url) {
  const old = S.preview.url;
  S.preview.url = url;
  if (old && old !== url) setTimeout(() => URL.revokeObjectURL(old), 1500);
}

async function runPreview() {
  if (!S.spec) return;
  schedulePreview.cancel();
  if (previewAbort) previewAbort.abort();
  const ac = new AbortController(); previewAbort = ac;
  const seq = ++S.previewSeq;
  const spec = clone(S.spec);
  const w = Number(spec.style.width_pt) || 0, hh = Number(spec.style.height_pt) || 0;
  const t0 = performance.now();
  S.preview.state = 'loading';
  setPreviewStatus(); updateOverlay();
  if (S.mode === 'plot') updatePlotOnly();
  try {
    const dpi = previewDpi();
    const blob = await api.preview(spec, ac.signal, dpi);
    if (seq !== S.previewSeq) return;
    const url = URL.createObjectURL(blob);
    const img = new Image(); img.src = url;
    // decode() never settles in a hidden page: do not let that stall the preview (the image is usable regardless)
    try { await Promise.race([img.decode(), new Promise((r) => setTimeout(r, 1500))]); } catch { /* still usable */ }
    if (seq !== S.previewSeq) { URL.revokeObjectURL(url); return; }
    setPreviewUrl(url);
    Object.assign(S.preview, { state: 'ok', w: w || img.naturalWidth * 72 / dpi, h: hh || img.naturalHeight * 72 / dpi, ms: Math.round(performance.now() - t0), error: null, dpi, warnings: blob.warnings || [], notes: blob.notes || [] });
    if (!S.lastStats) renderStats();
    const lw = layoutWarnings(blob.warnings).join('\n');
    if (lw && lw !== S.lastLayoutWarn) toast('The plot does not fit its size', { kind: 'warn', detail: lw });
    S.lastLayoutWarn = lw;
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    if (seq !== S.previewSeq) return;
    if (e && e.status === 422) reportFontError(e);
    Object.assign(S.preview, { state: 'failed', error: errText(e), w: w || S.preview.w, h: hh || S.preview.h });
  } finally {
    if (previewAbort === ac) previewAbort = null;
  }
  setPreviewStatus(); updateOverlay();
  if (S.mode === 'plot') updatePlotOnly();
}

function setPreviewStatus() {
  const el = $('#preview-status');
  const p = S.preview;
  el.classList.toggle('err', p.state === 'failed');
  el.onclick = null; el.style.cursor = '';
  if (!S.spec) el.replaceChildren();
  else if (p.state === 'loading') el.replaceChildren(h('span.spinner'), 'Rendering…');
  else if (p.state === 'failed') {
    el.replaceChildren('⚠ Preview failed');
    el.style.cursor = 'pointer'; el.title = `${p.error.title}\n${p.error.detail}`;
    el.onclick = () => toast('Preview failed: ' + p.error.title, { kind: 'err', detail: p.error.detail });
  } else if (p.state === 'ok') {
    const lw = layoutWarnings(p.warnings);
    if (lw.length) {
      el.classList.add('err'); el.replaceChildren(`⚠ ${lw.length} layout warning${lw.length === 1 ? '' : 's'}`);
      el.style.cursor = 'pointer'; el.title = lw.join('\n');
      el.onclick = () => toast('Layout', { kind: 'warn', detail: lw.join('\n') });
    } else { el.replaceChildren(`Preview ${p.ms} ms`); el.title = 'Time for the last live preview'; }
  }
  else el.replaceChildren();
}
/** Render warnings about the plot not fitting its fixed size (legend/labels clipped, axes squeezed, n/a overlap). */
function layoutWarnings(ws) { return (ws || []).filter((w) => /clipped|squeezed|does not fit|overlap/i.test(w)); }

// ── auto-save ──
const scheduleSave = debounce(() => saveNow(), 650);

async function saveNow() {
  scheduleSave.cancel();
  while (S.saving) await S.saving;
  if (!S.spec || S.savedSeq === S.editSeq) return S.saveOk;
  if (S.readonly) { S.savedSeq = S.editSeq; S.saveOk = true; updateSaveUI(); return true; }   // edits stay in this tab only
  if (S.specConflict) { updateSaveUI(); return false; }      // paused until the user picks reload / keep mine
  const seq = S.editSeq, snap = clone(S.spec), spec = S.spec;
  let stale = false;
  S.saving = (async () => {
    updateSaveUI(true);
    try {
      const r = await api.saveSpec(snap, S.specHash);
      if (S.spec === spec) { S.saveOk = true; showSpecErrors(null); if (r && r.hash) S.specHash = r.hash; }
    } catch (e) {
      if (S.spec === spec) {
        S.saveOk = false;
        if (e.status === 409) { stale = true; S.specConflict = { hash: (e.body && e.body.hash) || null, current: (e.body && e.body.current) || null }; }
        else if (e.status === 400) showSpecErrors((e.body && e.body.errors) || [e.detail || e.error]);
        else toastError(e, 'Save failed');
      }
    }
    if (S.spec === spec && !stale) S.savedSeq = seq;     // a stale save stays unsaved: nothing was written
  })();
  await S.saving;
  S.saving = null;
  updateSaveUI();
  if (stale && S.spec === spec) { resolveSpecConflict(); return false; }
  if (S.spec === spec && S.editSeq !== seq) scheduleSave();
  return S.saveOk;
}

/**
 * The spec file changed on disk since this tab loaded it (a hand edit of the YAML, another tab, another agent):
 * the save was refused and nothing was written. Reload drops this tab's edits; Keep mine overwrites, after a
 * second confirm, still compare-and-swap against the version just reported. Cancel leaves saving paused; the
 * save indicator reopens this dialog.
 */
async function resolveSpecConflict() {
  const c = S.specConflict, spec = S.spec;
  if (!c || !spec || modalOpen()) return;
  const v = await modal({
    title: 'Spec changed on disk', cancel: 'Decide later',
    body: h('div', { style: { display: 'grid', gap: '8px' } },
      h('p', h('span.mono', `${spec.id}.yaml`), ' was changed outside this tab since it was loaded (a hand edit, another tab or another designer). ', h('b', 'Nothing was written.')),
      h('p.muted', 'Reload shows the version on disk and drops the edits made in this tab. Keep mine writes this tab\'s version over it, losing the other changes (and the file\'s YAML comments).')),
    actions: [{ label: 'Reload from disk', kind: 'primary', value: 'reload' }, { label: 'Keep mine…', kind: 'danger', value: 'keep' }],
  });
  if (S.spec !== spec || S.specConflict !== c) return;
  if (v === 'reload') {
    S.savedSeq = S.editSeq; S.specConflict = null;
    await loadSpec(spec.id, { force: true });
    toast(`Reloaded ${spec.id} from disk`, { kind: 'ok' });
  } else if (v === 'keep') {
    const ok = await confirmDialog('Overwrite the spec on disk?', h('div', h('p', 'This replaces ', h('span.mono', `${spec.id}.yaml`), ' with this tab\'s version. ', h('b', 'The changes made elsewhere will be lost.'))), 'Overwrite', 'danger');
    if (!ok || S.spec !== spec || S.specConflict !== c) { updateSaveUI(); return; }
    S.specHash = c.hash; S.specConflict = null;
    await saveNow();
  }
  updateSaveUI();
}

/** On focus / every 30 s: a spec changed on disk reloads silently when this tab has nothing unsaved. */
let specCheckBusy = false;
async function checkSpecDiskChange() {
  if (specCheckBusy || !S.spec || !S.backendOk || S.saving || S.specConflict || modalOpen()) return;
  specCheckBusy = true;
  const spec = S.spec;
  try {
    let r;
    try { r = await api.getSpecVersioned(spec.id); } catch { return; }
    if (S.spec !== spec || !r.hash || !S.specHash || r.hash === S.specHash || S.saving) return;
    if (S.editSeq === S.savedSeq) {
      await loadSpec(spec.id, { force: true });
      toast(`${spec.id}.yaml changed on disk — reloaded`, { detail: 'This tab had no unsaved spec edits, so the new version is shown.' });
    } else {
      S.specConflict = { hash: r.hash, current: r.spec };
      updateSaveUI();
      resolveSpecConflict();
    }
  } finally { specCheckBusy = false; }
}

function updateSaveUI(saving = false) {
  const el = $('#save-status');
  el.className = 'save-status';
  if (!S.spec) { el.textContent = ''; return; }
  if (S.readonly) { el.classList.add('dirty'); el.textContent = 'Read-only · edits are not saved'; return; }
  if (S.specConflict) { el.classList.add('err', 'conflict'); el.textContent = '✕ Changed on disk — not saved (click to resolve)'; el.title = 'The spec file changed outside this tab; nothing was written'; return; }
  el.title = '';
  if (saving || S.saving) { el.classList.add('saving'); el.replaceChildren(h('span.spinner'), 'Saving…'); }
  else if (S.editSeq !== S.savedSeq) { el.classList.add('dirty'); el.textContent = '● Unsaved changes'; }
  else if (!S.saveOk) { el.classList.add('err'); el.textContent = S.specErrors ? '✕ Spec invalid — not saved' : '✕ Save failed'; }
  else { el.classList.add('ok'); el.textContent = ` Saved · ${S.spec.id}.yaml`; }
}

// ── render + recompile ──
async function renderAndRecompile() {
  if (!S.spec || roBlocked('Render into the paper')) return;
  const btn = $('#btn-render');
  const ok = await saveNow();
  if (!ok && S.specConflict) { resolveSpecConflict(); return; }
  if (!ok) { toast('Spec has errors — fix them before rendering', { kind: 'err', detail: (S.specErrors || []).join('\n') }); return; }
  btn.disabled = true; const old = btn.textContent; btn.textContent = 'Rendering…';
  let r;
  try {
    r = await api.render(S.spec.id);
  } catch (e) { if (e && e.status === 422) reportFontError(e); else toastError(e, 'Render failed'); btn.disabled = false; btn.textContent = old; return; }
  S.renderedOutputs.add(norm(S.spec.output)); S.renderedOutputs.add(norm(S.spec.output).replace(/\.pdf$/i, '.png'));
  S.lastStats = r;
  renderStats();
  if (r && r.warnings && r.warnings.length) toast(`${r.warnings.length} render warning${r.warnings.length === 1 ? '' : 's'}`, { kind: 'warn', detail: r.warnings.join('\n') });
  btn.textContent = 'Recompiling…';
  const res = await compilePaper();
  btn.disabled = false; btn.textContent = old;
  if (res && res.ok !== false) { S.previewActive = false; updateOverlay(); toast('Figure rendered into the paper', { kind: 'ok', detail: norm(S.spec.output) }); }
}

const autoRecompile = debounce(async () => {
  if (!S.auto || !S.spec || S.readonly) return;
  if (S.editSeq !== S.savedSeq) await saveNow();
  if (!S.saveOk) return;
  renderAndRecompile();
}, 1500);

/** Legend-free notes under the stats (e.g. "yellow CI = positions re-run; latest rollout counted"), with a swatch. */
function notesBlock(notes) {
  if (!notes || !notes.length) return null;
  const st = (S.spec && S.spec.style) || {};
  return h('div.render-notes', notes.map((n) => h('div', /yellow|re-run/i.test(n) ? h('span.note-sw', { style: { background: st.dup_ci_color || '#DDAA33' } }) : null, n)));
}

function renderStats() {
  const host = $('#stats-box');
  const r = S.lastStats;
  const previewNotes = S.preview && S.preview.state === 'ok' ? (S.preview.notes || []) : [];
  if (!r || !((r.stats || []).length || (r.warnings || []).length)) {
    host.replaceChildren(...(previewNotes.length && S.spec ? [h('div.stats', notesBlock(previewNotes))] : []));
    return;
  }
  const pct = (v) => (v == null || isNaN(v) ? '' : Math.round(v * 1000) / 10 + '%');
  const st = (S.spec && S.spec.style) || {};
  const partialBelow = st.partial_below, ciOn = st.ci && st.ci !== 'none';
  const stats = r.stats || [];
  const hasPanel = stats.some((s) => s.panel != null);
  const flag = (s) => {
    if (s.dup) return h('span.flag.dup', { title: (s.note || 'positions re-run; latest rollout counted') + (ciOn ? '' : ' (no interval drawn: Interval is none)') }, '↻');
    if (partialBelow != null && s.n != null && s.n < partialBelow) return h('span.flag.partial', { title: `fewer than ${partialBelow} trials${ciOn ? ' (interval in the partial color)' : ''}` }, 'p');
    return null;
  };
  const heads = [...(hasPanel ? ['Panel'] : []), 'Level', 'Arm', 'k/n', '', 'Rate', 'CI'];
  const grp = collapsibleGroup('Stats', [h('span.chev', '▾'), 'Last render', h('span.count', `${stats.length} bars`)],
    h('div.cgroup-body', h('div.stats' + (hasPanel ? '.has-panel' : ''),
      (r.warnings || []).length ? h('div.warnings', r.warnings.map((w) => h('div', '⚠ ' + w))) : null,
      notesBlock(r.notes && r.notes.length ? r.notes : previewNotes),
      stats.length ? h('table', h('thead', h('tr', heads.map((x) => h('th', x)))),
        h('tbody', stats.map((s) => h('tr' + (s.dup ? '.dup' : ''),
          hasPanel ? h('td', { title: s.panel || '' }, armLabel(s.panel || '')) : null,
          h('td', s.level), h('td', { title: s.arm }, armLabel(s.arm).slice(0, 18)), h('td', s.k == null ? 'n/a' : `${s.k}/${s.n}`),
          h('td.flag-cell', flag(s)), h('td', pct(s.rate)), h('td', s.lo == null ? '' : `${pct(s.lo)}–${pct(s.hi)}`))))) : null)));
  host.replaceChildren(grp);
}

// ── new / delete spec ──
async function openNewSpecDialog({ template = null, id = '', output = null, replaces = null } = {}) {
  if (modalOpen() || roBlocked('Creating a spec')) return;
  const idIn = h('input.input.mono', { value: id, placeholder: 'e.g. mug_levels_v2', pattern: '[a-z0-9_]+', spellcheck: 'false' });
  const tpl = h('select.input', h('option', { value: '' }, '(defaults)'), ...S.specs.map((s) => h('option', { value: s.id }, s.id)));
  tpl.value = template || (S.spec ? S.spec.id : '') || '';
  const outIn = output ? h('input.input.mono', { value: output, spellcheck: 'false' }) : null;
  const body = h('div.form-grid',
    h('label', 'Spec id'), idIn, h('div.hint', 'Lowercase letters, digits and _ — also the YAML file name.'),
    h('label', 'Start from'), tpl,
    outIn ? [h('label', 'Output'), outIn, h('div.hint', replaces
      ? ['A new file, so the paper\'s figure is not replaced by placeholder data. Once the plot is right, point the figure\'s \\includegraphics at it in the TeX editor — or type ', h('span.mono', replaces), ' here to overwrite the current file on Render.']
      : 'Rendering will overwrite this file in the paper repo.')] : null);
  const res = await modal({
    title: template && !id ? 'Duplicate spec' : 'New spec', body,
    actions: [{ label: 'Create', kind: 'primary', busy: 'Creating…', onClick: async () => {
      const v = idIn.value.trim();
      if (!/^[a-z0-9_]+$/.test(v)) { idIn.classList.add('invalid'); idIn.focus(); return false; }
      if (S.specs.some((s) => s.id === v)) { idIn.classList.add('invalid'); toast(`Spec ${v} already exists`, { kind: 'warn' }); return false; }
      if (outIn && replaces && norm(outIn.value.trim()) === norm(replaces)
          && !(await confirmDialog('Overwrite the paper\'s figure?', h('div', h('p', 'Rendering this spec will replace ', h('span.mono', replaces), ' (now made by another script) with this spec\'s plot, which starts from placeholder data.')), 'Use this file', 'danger'))) return false;
      const created = await api.createSpec(v, tpl.value || undefined);
      if (outIn && outIn.value.trim()) {
        const spec = created && created.id ? created : await api.getSpec(v);
        spec.output = outIn.value.trim();
        await api.saveSpec(spec);
      }
      return v;
    } }],
  });
  if (typeof res === 'string') {
    await loadSpecs();
    await loadFigures();
    toast(`Created ${res}`, { kind: 'ok' });
    selectSpec(res);
  }
}

async function deleteSpec() {
  if (!S.spec || roBlocked('Deleting a spec')) return;
  const id = S.spec.id;
  const ok = await confirmDialog(`Delete spec ${id}?`, h('div', h('p', `Removes ${id}.yaml from the designer. The rendered `, h('span.mono', S.spec.output || ''), ' and the paper TeX are not touched.')), 'Delete', 'danger');
  if (!ok) return;
  try {
    scheduleSave.cancel(); S.savedSeq = S.editSeq;
    await api.deleteSpec(id);
    S.spec = null;
    await unloadSpec();
    await loadSpecs(); await loadFigures();
    toast(`Deleted ${id}`, { kind: 'ok' });
    if (S.selFid) showSpecNote(selFig()); else showSpecNote(null);
    updateStage();
  } catch (e) { toastError(e, 'Delete failed'); }
}

// ── insert into paper ──
async function openInsertDialog() {
  if (modalOpen() || roBlocked('Inserting a figure')) return;
  if (!S.spec) return;
  const files = [...new Set(S.figures.map((f) => norm(f.file)).filter(Boolean))];
  const lr = S.lastReverse;
  const fileIn = h('input.input.mono', { value: (lr && lr.file) || files.find((f) => /experiment/.test(f)) || files[0] || '', list: 'ins-files', spellcheck: 'false' });
  const dl = h('datalist#ins-files', files.map((f) => h('option', { value: f })));
  const lineIn = h('input.input', { type: 'number', min: 0, value: lr ? lr.line : '' });
  const useClick = lr ? h('button.btn.btn-xs', { type: 'button', on: { click: () => { fileIn.value = lr.file; lineIn.value = lr.line; } } }, `use last click (${lr.file.split('/').pop()}:${lr.line})`) : null;
  const ec = S.tex && S.tex.path && !S.tex.loading && !S.tex.error ? { file: S.tex.path, line: ed.cursorLine() } : null;
  const useCursor = ec ? h('button.btn.btn-xs', { type: 'button', on: { click: () => { fileIn.value = ec.file; lineIn.value = ec.line; } } }, `use editor cursor (${ec.file.split('/').pop()}:${ec.line})`) : null;
  const cap = h('textarea.input', { rows: 2, value: S.spec.title || '' });
  const label = h('input.input.mono', { value: `fig:${S.spec.id}`, spellcheck: 'false' });
  const env = h('select.input', h('option', { value: 'figure' }, 'figure (one column)'), h('option', { value: 'figure*' }, 'figure* (both columns)'));
  const width = h('select.input', ['\\columnwidth', '\\linewidth', '\\textwidth'].map((w) => h('option', { value: w }, w)));
  env.addEventListener('change', () => { width.value = env.value === 'figure*' ? '\\textwidth' : '\\columnwidth'; });
  const body = h('div', h('div.form-grid',
    h('label', 'TeX file'), fileIn, h('div.hint', 'Relative to the paper root.'),
    h('label', 'After line'), h('div.row.gap6', { style: { flexWrap: 'wrap' } }, lineIn, useClick, useCursor), h('div.hint', 'Tip: click a spot on the page first — synctex fills in the file and line (or put the editor cursor there).'),
    h('label', 'Environment'), env,
    h('label', 'Width'), width,
    h('label', 'Caption'), cap,
    h('label', 'Label'), label,
    h('label', 'Graphics'), h('span.mono', S.spec.output || '(spec has no output)')), dl);
  const res = await modal({
    title: 'Insert figure into the paper', body, wide: true,
    actions: [{ label: 'Insert + recompile', kind: 'primary', busy: 'Inserting…', onClick: async () => {
      if (!fileIn.value.trim()) { fileIn.classList.add('invalid'); return false; }
      if (lineIn.value === '') { lineIn.classList.add('invalid'); lineIn.focus(); return false; }
      return api.insertFigure({
        file_rel: fileIn.value.trim(), after_line: Number(lineIn.value), graphics_rel: S.spec.output,
        caption: cap.value, label: label.value.trim(), env: env.value, width: width.value, spec_id: S.spec.id,
      });
    } }],
  });
  if (res && typeof res === 'object') {
    S.editedTex.add(norm(fileIn.value));
    if (S.tex && S.tex.path === norm(fileIn.value) && !S.texDirty) openSection(S.tex.path, { reload: true, keepView: true, confirm: false });
    loadSections();
    toast('Figure inserted', { kind: 'ok', detail: `${fileIn.value}, after line ${lineIn.value}` });
    // make sure the graphic exists, then compile and select it
    try { await api.render(S.spec.id); S.renderedOutputs.add(norm(S.spec.output)); } catch (e) { toastError(e, 'Render failed'); }
    await loadFigures();
    const fid = res.fid || (figForSpec(S.spec.id) || {}).fid;
    if (fid && figById(fid)) { S.modeAuto = true; await selectFigure(fid); }
    await compilePaper();
    if (fid && figById(fid)) selectFigure(fid, { zoom: true });
  }
}

// ───────────────────────────── section TeX editor (left panel) ─────────────────────────────
// Edits whole .tex files: the main file and every \input/\include section. Saving is compare-and-swap on the file's
// hash (409 = changed on disk → Reload / Keep mine, never a silent overwrite).
function makeEditor() {
  const host = $('#editor-host');
  let cm = null, ta = null, flashMark = null, gen = 0;
  let figLines = [];                       // line handles of the highlighted figure environment
  const onChange = () => { if (!ed._muted) updateTexDirty(); };
  if (window.CodeMirror) {
    const keys = {
      'Ctrl-S': () => saveTexAndRecompile(), 'Cmd-S': () => saveTexAndRecompile(),     // save + recompile
      'Ctrl-Enter': () => saveTexAndRecompile(), 'Cmd-Enter': () => saveTexAndRecompile(),
      'Ctrl-J': () => showOnPage(), 'Cmd-J': () => showOnPage(),
      'Ctrl-F': 'findPersistent', 'Cmd-F': 'findPersistent',
      // comment / uncomment the selected lines (or the cursor line) with % (addon/comment, stex lineComment)
      'Ctrl-/': 'toggleComment', 'Cmd-/': 'toggleComment',
      Tab: (c) => (c.somethingSelected() ? c.execCommand('indentMore') : c.replaceSelection('    ')),
      'Shift-Tab': 'indentLess',
    };
    cm = window.CodeMirror(host, {
      mode: 'stex', lineNumbers: true, lineWrapping: store.get('tex.wrap', true), indentUnit: 4, tabSize: 4,
      viewportMargin: 30, styleActiveLine: true, extraKeys: keys, spellcheck: false,
    });
    cm.on('change', onChange);
    cm.on('cursorActivity', () => {
      const c = cm.getCursor();
      $('#tex-cursor').textContent = `Ln ${c.line + 1}, Col ${c.ch + 1}`;
    });
    // Ctrl+click (Cmd+click) a line: synctex forward to the page (instead of CodeMirror's add-a-cursor)
    cm.on('mousedown', (c, e) => {
      if (!(e.ctrlKey || e.metaKey) || e.button !== 0) return;
      e.preventDefault();
      const pos = c.coordsChar({ left: e.clientX, top: e.clientY }, 'window');
      c.focus(); c.setCursor(pos);
      showOnPage(pos.line + 1);
    });
  } else {
    ta = h('textarea.fallback', { spellcheck: 'false' });
    ta.addEventListener('input', onChange);
    host.append(ta);
    console.warn('CodeMirror not available; using a plain textarea');
  }
  const clearFig = () => {
    if (cm) for (const lh of figLines) cm.removeLineClass(lh, 'background', 'cm-figenv');
    figLines = [];
  };
  const flash = (from, to) => {
    if (!cm) return;
    if (flashMark) for (const lh of flashMark) cm.removeLineClass(lh, 'background', 'cm-flash');
    const hs = [];
    for (let i = from; i <= to; i++) hs.push(cm.addLineClass(i, 'background', 'cm-flash'));
    flashMark = hs;
    setTimeout(() => { if (flashMark === hs) { for (const lh of hs) cm.removeLineClass(lh, 'background', 'cm-flash'); flashMark = null; } }, 1600);
  };
  const ed = {
    _muted: false,
    get: () => (cm ? cm.getValue() : ta.value),
    /** Replace the whole document (a freshly loaded file): history cleared, marked clean. */
    set(text, { readOnly = false, keepView = false } = {}) {
      ed._muted = true;
      clearFig();
      if (cm) {
        const view = keepView ? { s: cm.getScrollInfo(), c: cm.getCursor() } : null;
        cm.setValue(text || ''); cm.clearHistory();
        cm.setOption('readOnly', readOnly);
        gen = cm.changeGeneration(true);
        if (view) { cm.setCursor(view.c); cm.scrollTo(view.s.left, view.s.top); }
        else { cm.setCursor({ line: 0, ch: 0 }); cm.scrollTo(0, 0); }
        cm.refresh();
      } else { ta.value = text || ''; ta.readOnly = readOnly; ta._clean = ta.value; }
      ed._muted = false;
    },
    /** Snapshot of the document generation (pass it to markClean after a successful save). */
    generation: () => (cm ? cm.changeGeneration(true) : (ta ? ta.value : '')),
    markClean(g) { if (cm) gen = g; else ta._clean = g; },
    isDirty: () => (cm ? !cm.isClean(gen) : ta.value !== ta._clean),
    cursorLine: () => (cm ? cm.getCursor().line + 1 : ta.value.slice(0, ta.selectionStart).split('\n').length),
    lineCount: () => (cm ? cm.lineCount() : ta.value.split('\n').length),
    /** Cursor to `line` (1-based), scrolled into view, briefly flashed. */
    jump(line, { focus = true } = {}) {
      const i = clamp((line | 0) - 1, 0, ed.lineCount() - 1);
      if (cm) {
        if (focus) cm.focus();
        cm.setCursor({ line: i, ch: 0 });
        cm.scrollIntoView({ from: { line: Math.max(0, i - 3), ch: 0 }, to: { line: Math.min(cm.lineCount() - 1, i + 8), ch: 0 } }, 40);
        flash(i, i);
      } else if (ta) {
        const lines = ta.value.split('\n'); let pos = 0;
        for (let k = 0; k < Math.min(i, lines.length); k++) pos += lines[k].length + 1;
        if (focus) ta.focus();
        ta.setSelectionRange(pos, pos);
      }
    },
    /** Highlight lines b..e (1-based, inclusive) as the selected figure environment and scroll them into view. */
    highlightRange(b, e) {
      if (!cm) { ed.jump(b, { focus: false }); return; }
      clearFig();
      const n = cm.lineCount();
      const from = clamp(b - 1, 0, n - 1), to = clamp(e - 1, from, n - 1);
      for (let i = from; i <= to; i++) figLines.push(cm.addLineClass(i, 'background', 'cm-figenv'));
      cm.setCursor({ line: from, ch: 0 });
      const top = cm.charCoords({ line: from, ch: 0 }, 'local').top;
      const bottom = cm.charCoords({ line: to, ch: 0 }, 'local').bottom;
      const vis = cm.getScrollInfo().clientHeight;
      // show the whole environment if it fits, else its beginning, with a little context above
      cm.scrollTo(null, bottom - top < vis - 60 ? Math.max(0, top - Math.max(30, (vis - (bottom - top)) / 3)) : Math.max(0, top - 30));
      flash(from, to);
    },
    clearHighlight: clearFig,
    setWrap(on) { if (cm) { cm.setOption('lineWrapping', on); cm.refresh(); } },
    openFind() { if (cm) { cm.focus(); cm.execCommand('findPersistent'); } else if (ta) ta.focus(); },
    focus: () => (cm ? cm.focus() : ta.focus()),
    refresh: () => { if (cm) cm.refresh(); },
    hasFocus: () => (cm ? cm.hasFocus() || !!(document.activeElement && document.activeElement.closest && document.activeElement.closest('#left .CodeMirror-dialog')) : document.activeElement === ta),
  };
  return ed;
}

function bindEditor() {
  $('#tex-save').addEventListener('click', () => saveTex());
  $('#tex-recompile').addEventListener('click', () => saveTexAndRecompile());
  $('#tex-show').addEventListener('click', () => showOnPage());
  $('#tex-find').addEventListener('click', () => ed.openFind());
  $('#tex-file').addEventListener('click', () => openNav('sections'));
  const wrapBtn = $('#tex-wrap');
  const setWrapUI = (on) => { wrapBtn.classList.toggle('on', on); wrapBtn.setAttribute('aria-pressed', String(on)); };
  setWrapUI(store.get('tex.wrap', true));
  wrapBtn.addEventListener('click', () => { const on = !store.get('tex.wrap', true); store.set('tex.wrap', on); ed.setWrap(on); setWrapUI(on); });
  setEditorButtons();
  // pick up changes made on disk (Overleaf pull, another editor) when the window regains focus
  window.addEventListener('focus', () => { checkDiskChange(); checkSpecDiskChange(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { checkDiskChange(); checkSpecDiskChange(); } });
}

function setEditorButtons() {
  const t = S.tex && !S.tex.loading && !S.tex.error ? S.tex : null;
  const canWrite = !!t && !S.readonly && t.editable !== false;
  $('#tex-save').disabled = !canWrite; $('#tex-recompile').disabled = !canWrite;
  $('#tex-show').disabled = !t; $('#tex-find').disabled = !t;
}

function updateTexDirty(force = false) {
  const dirty = !!(S.tex && !S.tex.loading && !S.tex.error && ed.isDirty());
  if (dirty === S.texDirty && !force) return;
  S.texDirty = dirty;
  $('#editor-dirty').classList.toggle('hidden', !dirty);
  $('#left').classList.toggle('dirty', dirty);
  renderSectionList();
}

const sectionByPath = (p) => S.sections.find((s) => s.path === norm(p)) || null;
function sectionTitle(s) {
  if (!s) return '';
  if (s.main) return 'Main file';
  return s.heading || sectionName(s.path);
}

async function loadSections() {
  try {
    const r = await api.texSections();
    S.sections = asList(r, 'sections').map((s) => ({ ...s, path: norm(s.path) }));
  } catch (e) {
    if (!S.sections.length) toastError(e, 'Could not list the TeX files');
  }
  renderSectionList();
  return S.sections;
}

function renderSectionList() {
  const host = $('#section-list');
  if (!host) return;
  const cur = S.tex ? S.tex.path : null;
  const nFigs = (p) => S.figures.filter((f) => f.file === p && !f.commented).length;
  $('#sections-count').textContent = S.sections.length ? String(S.sections.length) : '';
  if (!S.sections.length) { host.replaceChildren(h('div.empty.small', 'No TeX files found.')); return; }
  host.replaceChildren(...S.sections.map((s) => {
    const on = s.path === cur, n = nFigs(s.path);
    return h('button.sec-item' + (on ? '.on' : '') + (s.main ? '.main' : ''), {
      type: 'button', role: 'option', 'aria-selected': String(on), dataset: { path: s.path },
      title: `${s.path}\n${s.lines} lines${s.heading ? '\n' + s.heading : ''}`,
      style: { paddingLeft: 8 + Math.max(0, (s.depth || 0) - 1) * 12 + 'px' },
      on: { click: () => { closeNav(); openSection(s.path, { focus: true, reveal: true }); } },
    },
    h('span.sec-ico', s.main ? '⌂' : '§'),
    h('span.sec-body', h('span.sec-title', sectionTitle(s)), h('span.sec-path', s.path)),
    h('span.sec-meta', on && S.texDirty ? h('span.dot', { title: 'Unsaved edits' }) : null,
      n ? h('span.badge', { title: `${n} figure${n === 1 ? '' : 's'}` }, `${n} fig`) : null,
      h('span.sec-lines', `${s.lines}`)));
  }));
}

function renderTexHeader() {
  const t = S.tex;
  const s = t ? sectionByPath(t.path) : null;
  $('#tex-title').textContent = t ? (s ? sectionTitle(s) : t.path.split('/').pop()) : 'TeX';
  $('#tex-path').textContent = t ? t.path : '';
  $('#tex-file').title = t ? `${t.path} — switch section (M)` : 'Switch section (M)';
  const loc = $('#editor-loc');
  if (!t || t.loading) loc.textContent = t ? `Loading ${t.path}…` : '';
  else if (t.error) loc.textContent = '';
  else loc.textContent = `${t.lines} lines${t.newline === '\r\n' ? ' · CRLF' : ''}${t.editable === false ? ' · read-only (not UTF-8)' : S.readonly ? ' · read-only server' : ''}`;
}

/** Ask before leaving a section with unsaved edits. Resolves true when it is OK to go on (saved or discarded). */
async function confirmLeaveSection(what = 'switch sections') {
  if (!S.texDirty || !S.tex) return true;
  if (S.readonly) return confirmDialog('Discard TeX edits?', `${S.tex.path} has edits that cannot be saved on a read-only designer.`, 'Discard', 'danger').then((ok) => { if (ok) { S.texDirty = false; } return ok; });
  const v = await modal({
    title: 'Unsaved TeX edits', cancel: 'Cancel',
    body: h('p', h('span.mono', S.tex.path), ` has unsaved edits. Save them before you ${what}?`),
    actions: [{ label: 'Discard', kind: 'danger', value: 'discard' }, { label: 'Save', kind: 'primary', value: 'save' }],
  });
  if (v === 'save') return saveTex();
  if (v === 'discard') { S.texDirty = false; return true; }
  return false;
}

/**
 * Open a section file in the editor. opts: line (cursor there), range [b, e] (highlight a figure environment),
 * reload (re-read even if open), keepView (keep cursor/scroll on reload), focus. Resolves true when shown.
 */
async function openSection(path, { line = 0, range = null, reload = false, keepView = false, focus = false, confirm = true, reveal = false } = {}) {
  path = norm(path);
  if (!path) return false;
  if (!S.config) return false;
  if (S.tex && S.tex.path === path && !S.tex.loading && !S.tex.error && !reload) {
    if (reveal) setLeftOpen(true);
    applyEditorTarget({ line, range, focus });
    return true;
  }
  if (confirm && S.tex && S.tex.path !== path && !(await confirmLeaveSection(`open ${path}`))) return false;
  if (S.sections.length && !sectionByPath(path)) {
    toast(`${path} is not one of the paper's TeX files`, { kind: 'warn', detail: `Only ${S.config.main_tex || 'the main file'} and the files it \\input/\\includes can be edited here.` });
    return false;
  }
  if (reveal) setLeftOpen(true);
  const seq = ++S.texSeq;
  const prevPath = S.tex && S.tex.path;
  const prevTex = S.tex;
  // a reload of the open file (e.g. it changed on disk while clean): remember the buffer, so keys typed while the
  // GET is in flight are not silently replaced by ed.set() below
  const typedGuard = reload && prevPath === path && prevTex && !prevTex.loading && !prevTex.error ? ed.get() : null;
  S.tex = { path, loading: true }; S.texDirty = false;
  $('#editor-dirty').classList.add('hidden'); $('#left').classList.remove('dirty');
  hideTexBanner();
  const emptyEl = $('#editor-empty');
  if (!(keepView && prevPath === path)) { emptyEl.replaceChildren(h('span.spinner'), ` Loading ${path}…`); emptyEl.classList.remove('hidden'); }
  renderTexHeader(); setEditorButtons(); renderSectionList();
  try {
    const t = await api.texFile(path);
    if (seq !== S.texSeq) return false;
    if (typedGuard !== null && ed.get() !== typedGuard) {
      // the user typed during the reload: keep their buffer (still based on the old hash) and say the disk changed
      S.tex = prevTex;
      emptyEl.classList.add('hidden');
      updateTexDirty(true);
      renderTexHeader(); setEditorButtons(); renderSectionList();
      if (t.hash !== prevTex.hash) showDiskChangedBanner(path);
      return false;
    }
    S.tex = { ...t, path: norm(t.path) };
    ed.set(t.text, { readOnly: S.readonly || t.editable === false, keepView: keepView && prevPath === path });
    S.texDirty = false;
    emptyEl.classList.add('hidden');
    store.set('tex.path', S.tex.path);
    renderTexHeader(); setEditorButtons(); renderSectionList();
    if (t.warning) toast('Read-only file', { kind: 'warn', detail: t.warning });
    applyEditorTarget({ line, range, focus });
    return true;
  } catch (e) {
    if (seq !== S.texSeq) return false;
    S.tex = { path, error: errText(e) };
    const et = errText(e);
    emptyEl.replaceChildren(h('div', `Could not open ${path}: ${et.title}`), et.detail ? h('div.mono', et.detail) : null);
    emptyEl.classList.remove('hidden');
    renderTexHeader(); setEditorButtons();
    return false;
  }
}

function applyEditorTarget({ line = 0, range = null, focus = false } = {}) {
  // the editor may have been hidden/zero-sized while loading: measure after layout
  requestAnimationFrame(() => {
    ed.refresh();
    if (range) ed.highlightRange(range[0], range[1]);
    else if (line) ed.jump(line, { focus: true });
    else if (focus) ed.focus();
  });
}

/** Re-read the open file (after a pull / an external edit). Unsaved edits are never replaced without asking. */
async function reloadSection({ quiet = false } = {}) {
  if (!S.tex || !S.tex.path) return;
  if (S.texDirty && !(await confirmDialog('Reload from disk?', h('p', 'Your unsaved edits to ', h('span.mono', S.tex.path), ' will be lost.'), 'Reload', 'danger'))) return;
  const ok = await openSection(S.tex.path, { reload: true, keepView: true, confirm: false });
  if (ok && !quiet) toast(`Reloaded ${S.tex.path}`, { kind: 'ok' });
  const f = selFig();
  if (ok && f && f.file === S.tex.path) ed.highlightRange(f.begin_line, f.end_line);
}

let diskCheckBusy = false;
/** Compare the open file's hash with the disk; reload when clean, warn (banner) when there are unsaved edits. */
async function checkDiskChange() {
  if (diskCheckBusy || !S.backendOk || !S.tex || S.tex.loading || S.tex.error || S.texSaving || modalOpen()) return;
  diskCheckBusy = true;
  try {
    await loadSections();
    const s = sectionByPath(S.tex.path);
    if (!s || !S.tex.hash || s.hash === S.tex.hash || S.texSaving) return;
    if (!S.texDirty) {
      const path = S.tex.path;
      if (await openSection(path, { reload: true, keepView: true, confirm: false })) {
        toast(`${path} changed on disk — reloaded`, { detail: 'You had no unsaved edits, so the new version is shown.' });
        loadFigures();
      }
    } else showDiskChangedBanner(S.tex.path);
  } finally { diskCheckBusy = false; }
}

function showDiskChangedBanner(path) {
  showTexBanner(h('span', h('b', 'Changed on disk. '), h('span.mono', path), ' was modified elsewhere since you opened it. Saving will ask before overwriting.'),
    h('button.btn.btn-xs', { on: { click: () => reloadSection() } }, 'Reload (discard mine)'),
    h('button.btn.btn-xs.btn-ghost', { on: { click: hideTexBanner } }, 'Dismiss'));
}

function showTexBanner(...kids) { const b = $('#tex-banner'); b.replaceChildren(...kids); b.classList.remove('hidden'); }
function hideTexBanner() { $('#tex-banner').classList.add('hidden'); }

async function saveTex() {
  if (!S.tex || S.tex.loading || S.tex.error) return false;
  if (roBlocked('Saving TeX')) return false;
  if (S.tex.editable === false) { toast('This file is read-only here', { kind: 'warn', detail: 'It is not valid UTF-8, so the designer will not rewrite it.' }); return false; }
  if (S.texSaving) { await S.texSaving; if (!S.texDirty) return true; }
  if (!S.texDirty) { setMsg(`No changes to save in ${S.tex.path}`); return true; }
  const { path, hash } = S.tex;
  const g = ed.generation();
  const text = ed.get();
  S.texSaving = putSection(path, text, hash, g);
  try { return await S.texSaving; } finally { S.texSaving = null; }
}

async function putSection(path, text, base, g) {
  const btn = $('#tex-save');
  btn.classList.add('busy');
  try {
    const r = await api.saveTexFile(path, text, base);
    if (S.tex && S.tex.path === path) {
      Object.assign(S.tex, { hash: r.hash, text, mtime: r.mtime, lines: r.lines });
      ed.markClean(g);
      updateTexDirty(true);                              // clean unless typed during the save
      hideTexBanner();
    }
    S.editedTex.add(path);
    setMsg(`Saved ${path}${r.changed === false ? ' (no change on disk)' : ''}`);
    renderTexHeader();
    await Promise.all([loadFigures(), loadSections()]);
    const f = selFig();
    if (f && f.file === path) updateBoxes();
    refreshGit();
    return true;
  } catch (e) {
    if (e.status === 409) return resolveConflict(path, text, g, e.body && e.body.current);
    if (e.status === 403 && e.body && e.body.readonly) { toast('Read-only mode', { kind: 'warn', detail: e.detail }); return false; }
    toastError(e, `Could not save ${path}`);
    return false;
  } finally { btn.classList.remove('busy'); }
}

/** The file changed on disk since it was loaded: Reload (discard mine) or Keep mine (overwrite after a confirm). */
async function resolveConflict(path, mine, g, current) {
  if (!current) { try { current = await api.texFile(path); } catch { current = null; } }
  const mineLines = mine ? mine.split('\n').length - (mine.endsWith('\n') ? 1 : 0) : 0;   // as the server counts
  const body = h('div', { style: { display: 'grid', gap: '8px' } },
    h('p', h('span.mono', path), ' changed on disk since you opened it (an Overleaf pull, another editor, …). ', h('b', 'Nothing was written.')),
    current ? h('p.muted', `On disk now: ${current.lines} lines, modified ${fmtTime(current.mtime)}. Your version: ${mineLines} lines.`) : null,
    h('p.muted', 'Reload shows the disk version and drops your edits. Keep mine writes your version over it, losing the other changes.'));
  const v = await modal({
    title: 'File changed on disk', cancel: 'Keep editing', body,
    actions: [{ label: 'Copy my edits', value: 'copy' }, { label: 'Reload from disk', value: 'reload' }, { label: 'Keep mine…', kind: 'danger', value: 'keep' }],
  });
  if (v === 'copy') {
    try { await navigator.clipboard.writeText(mine); toast('Your edits are on the clipboard', { kind: 'ok' }); } catch { toast('Clipboard unavailable', { kind: 'warn' }); }
    return false;
  }
  if (v === 'reload') {
    S.texDirty = false;
    await openSection(path, { reload: true, keepView: true, confirm: false });
    loadFigures(); loadSections();
    toast(`Reloaded ${path} from disk`, { kind: 'ok' });
    return false;
  }
  if (v === 'keep') {
    if (!current || !current.hash) { toast('Could not read the disk version', { kind: 'err' }); return false; }
    const ok = await confirmDialog('Overwrite the file on disk?', h('div', { style: { display: 'grid', gap: '8px' } },
      h('p', 'This replaces ', h('span.mono', path), ' on disk with your version.'),
      h('p', h('b', 'The changes made elsewhere will be lost'), ' (they stay in git history only if they were committed).')), 'Overwrite', 'danger');
    if (!ok) return false;
    if (S.tex && S.tex.path === path) S.tex.hash = current.hash;
    return putSection(path, mine, current.hash, g);      // still compare-and-swap: against the version just shown
  }
  return false;
}

async function saveTexAndRecompile() {
  if (S.texDirty) { const ok = await saveTex(); if (!ok) return; }
  compilePaper();
}

/** Synctex forward: the editor's cursor line (or `line`) → its spot on the page, briefly highlighted. */
async function showOnPage(line) {
  if (!S.tex || S.tex.loading || S.tex.error) return;
  line = line || ed.cursorLine();
  const path = S.tex.path;
  if (S.pdfState !== 'ok') { toast('No compiled PDF yet', { kind: 'warn', detail: 'Recompile the paper (Ctrl+Enter) to see this line on the page.' }); return; }
  try {
    const r = await api.forward(path, line);
    if (!r || !r.page || !r.box) {
      if (!S.figures.some((f) => f.box)) toast('Show on page needs synctex data — click “Recompile paper” once.', { kind: 'warn' });
      else toast(`Line ${line} is not on any page`, { kind: 'warn', detail: S.texDirty || (r && r.stale) ? 'The file changed since the last compile: recompile (Ctrl+Enter) and try again.' : 'Nothing near it was typeset (a comment, the preamble, or skipped text).' });
      return;
    }
    if (S.mode !== 'page') setMode('page');
    viewer.flashMark({ ...r.box, page: r.page });
    const approx = S.texDirty || r.stale;
    setMsg(`${path}:${r.line} → page ${r.page}${approx ? ' · approximate (edited since the last compile)' : ''}`);
  } catch (e) { toastError(e, 'Show on page'); }
}

/** The selected figure's environment as it is in the editor buffer (unsaved edits), or undefined if not found. */
function envTextFromEditor(fig) {
  if (!S.tex || S.tex.path !== norm(fig.file) || !S.texDirty || fig.commented) return undefined;
  const lines = ed.get().split('\n');
  const esc = String(fig.env || 'figure').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const beginRe = new RegExp('\\\\begin\\s*\\{' + esc + '\\}'), endRe = new RegExp('\\\\end\\s*\\{' + esc + '\\}');
  let anchor = -1;
  if (fig.label) anchor = lines.findIndex((l) => !/^\s*%/.test(l) && l.includes('\\label{' + fig.label + '}'));
  if (anchor < 0 && beginRe.test(lines[fig.begin_line - 1] || '')) anchor = fig.begin_line - 1;
  if (anchor < 0) return undefined;
  let b = anchor; while (b >= 0 && !beginRe.test(lines[b])) b--;
  let e = anchor; while (e < lines.length && !endRe.test(lines[e])) e++;
  if (b < 0 || e >= lines.length) return undefined;
  return lines.slice(b, e + 1).join('\n');
}

// ───────────────────────────── hamburger drawer: sections, pages, figures ─────────────────────────────
function bindNav() {
  $('#btn-menu').addEventListener('click', () => toggleNav());
  document.addEventListener('mousedown', (e) => {
    if (!navOpen()) return;
    const t = e.target;
    if ($('#nav-drawer').contains(t) || $('#btn-menu').contains(t) || $('#tex-file').contains(t) || (t.closest && t.closest('.modal-back, .popover, .toast'))) return;
    closeNav();
  }, true);
}
const navOpen = () => $('#app').classList.contains('nav-open');
function openNav(focusSec) {
  $('#app').classList.add('nav-open');
  $('#nav-drawer').setAttribute('aria-hidden', 'false');
  $('#btn-menu').setAttribute('aria-expanded', 'true'); $('#btn-menu').classList.add('on');
  renderSectionList();
  if (thumbs && viewer) thumbs.setCurrent(viewer.pageNum);
  if (focusSec === 'sections') {
    const cur = $('#section-list .sec-item.on') || $('#section-list .sec-item');
    if (cur) setTimeout(() => { cur.scrollIntoView({ block: 'nearest' }); cur.focus(); }, 30);
  }
}
function closeNav() {
  if (!navOpen()) return;
  $('#app').classList.remove('nav-open');
  $('#nav-drawer').setAttribute('aria-hidden', 'true');
  $('#btn-menu').setAttribute('aria-expanded', 'false'); $('#btn-menu').classList.remove('on');
}
function toggleNav(sec) { if (navOpen()) closeNav(); else openNav(sec); }

function setPanelOpen(side, open, { save = true } = {}) {
  $('#app').classList.toggle(`${side}-hidden`, !open);
  const b = $(`#tg-${side}`);
  if (b) { b.classList.toggle('on', open); b.setAttribute('aria-pressed', String(open)); }
  if (save) store.set(side === 'left' ? 'panel.tex' : `panel.${side}`, open);
  // the stage changed width: keep a width-fitted page fitted; the editor re-measures once visible
  requestAnimationFrame(() => { if (viewer && viewer.fit === 'width') viewer.fitWidth(); if (S.mode === 'plot') updatePlotOnly(); if (side === 'left' && open && ed) ed.refresh(); });
}
function togglePanel(side) { setPanelOpen(side, $('#app').classList.contains(`${side}-hidden`)); }
function setLeftOpen(open) { if ($('#app').classList.contains('left-hidden') === open) setPanelOpen('left', open); }

async function compileContext() {
  const fid = S.selFid;
  const fig = figById(fid);
  if (!fig) { toast('Select a figure in the paper first'); return; }
  // read-only servers preview only the stored figure text (plus the live slider settings), never edited TeX
  const sameFile = S.tex && S.tex.path === norm(fig.file) && S.texDirty;
  let text = null;
  if (sameFile && !S.readonly) {
    const t = envTextFromEditor(fig);
    if (t !== undefined) text = t;
    else toast('Context uses the saved file', { detail: 'The figure environment could not be found in your unsaved edits; save (Ctrl+S) to preview them.' });
  }
  if (sameFile && S.readonly) toast('Read-only mode', { kind: 'warn', detail: 'Context shows the saved TeX: edited TeX cannot be previewed on a read-only designer.' });
  const liveSpec = S.spec && fig.spec_id === S.spec.id ? clone(S.spec) : null;
  S.ctx = { fid, loading: true };
  updateStage();
  try {
    const r = await api.context(fid, text, liveSpec);
    if (S.selFid !== fid) return;
    if ((r.errors && r.errors.length) || r.ok === false) showErrors(r, 'Context');
    if (!r.pdf_url) { S.ctx = { fid, error: { title: 'No PDF produced', detail: (r.errors || []).map((e) => e.message).join('; ') } }; updateStage(); return; }
    const url = abs(r.pdf_url) + (r.pdf_url.includes('?') ? '&' : '?') + 't=' + Date.now();
    const doc = await loadPdf(url);
    if (S.selFid !== fid) { doc.destroy(); return; }
    if (S.ctx && S.ctx.doc) S.ctx.doc.destroy();
    S.ctx = { fid, doc, seconds: r.seconds };
    updateStage();
    await renderContextStack();
    setMsg(`Context compiled${r.seconds ? ` in ${Number(r.seconds).toFixed(1)}s` : ''}${r.live ? ' with the current slider settings' : ''}${text != null ? ' (unsaved TeX)' : ''}`);
  } catch (e) {
    if (S.selFid !== fid) return;
    S.ctx = { fid, error: errText(e) };
    updateStage();
  }
}

async function renderContextStack() {
  if (!S.ctx || !S.ctx.doc) return;
  if (!S.ctxScale) {
    const sz = await S.ctx.doc.size(1);
    S.ctxScale = clamp(($('#stage').clientWidth - 56) / sz.w, MIN_SCALE, 3);
  }
  await renderStack(S.ctx.doc, $('#ctx-pages'), S.ctxScale);
  updateZoomLabel(); updatePager();
}

// ───────────────────────────── data drawer ─────────────────────────────
function bindDrawer() {
  $('#btn-data').addEventListener('click', () => toggleDrawer());
  const form = $('#data-add'), ref = $('#da-ref'), name = $('#da-name');
  name.dataset.auto = '1';
  name.addEventListener('input', () => { name.dataset.auto = name.value ? '' : '1'; });
  ref.addEventListener('input', () => {
    if (!name.dataset.auto) return;
    let v = ref.value.trim().replace(/^"|"$/g, '');
    try { if (/^file:/i.test(v)) v = decodeURIComponent(v.replace(/^file:\/*/i, '')); } catch { /* keep raw */ }
    const base = v.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '';
    name.value = base.replace(/\.lnk$/i, '').replace(/\.[A-Za-z0-9]{1,5}$/, '').replace(/[^A-Za-z0-9_.-]+/g, '_').replace(/^_+|_+$/g, '');
  });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (roBlocked('Adding a dataset')) return;
    const body = { name: name.value.trim(), ref: ref.value.trim().replace(/^"|"$/g, ''), notes: $('#da-notes').value.trim(), make_link: $('#da-link').checked };
    if (!body.ref || !body.name) return;
    const btn = form.querySelector('button[type=submit]');
    btn.disabled = true;
    try {
      const r = await api.addDataset(body);
      toast(`Registered ${body.name}`, { kind: r && r.warning ? 'warn' : 'ok', detail: [r && r.path, r && r.warning].filter(Boolean).join('\n') });
      ref.value = ''; name.value = ''; name.dataset.auto = '1'; $('#da-notes').value = '';
      await loadDatasets();
      selectDataset(body.name);
      if (S.spec) dataEditor.build(S.spec);
    } catch (err) { toastError(err, 'Could not add dataset'); }
    finally { btn.disabled = false; }
  });
  // Browsers expose dropped text/links (e.g. a file:/// URL), never local file paths.
  form.addEventListener('dragover', (e) => { e.preventDefault(); form.classList.add('dragover'); });
  form.addEventListener('dragleave', () => form.classList.remove('dragover'));
  form.addEventListener('drop', (e) => {
    e.preventDefault(); form.classList.remove('dragover');
    const t = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
    if (t) { ref.value = t.split('\n')[0].trim(); ref.dispatchEvent(new Event('input')); ref.focus(); }
    else if (e.dataTransfer.files && e.dataTransfer.files.length) toast('Paste the path instead', { kind: 'warn', detail: 'Browsers hide local file paths. In Explorer: Shift+right-click → "Copy as path", then paste here. Nothing is uploaded or copied.' });
  });
}

function toggleDrawer(force) {
  const d = $('#drawer');
  const open = force === undefined ? d.classList.contains('hidden') : force;
  d.classList.toggle('hidden', !open);
  $('.hsplit[data-split=drawer]').classList.toggle('hidden', !open);
  $('#btn-data').classList.toggle('on', open);
  $('#btn-data').textContent = (open ? '▾' : '▴') + ' Data';
  store.set('drawer.open', open);
  if (open && S.backendOk) loadDatasets();
}

let selDataset = null;
function renderDataTable() {
  const tb = $('#data-table tbody');
  tb.replaceChildren(...S.datasets.map((d) => {
    const tr = h('tr' + (d.missing ? '.missing' : '') + (d.name === selDataset ? '.on' : ''), { dataset: { name: d.name }, on: { click: () => selectDataset(d.name) } },
      h('td', h('b', d.name), d.notes ? h('div.muted', { style: { fontSize: '11px' } }, d.notes) : null),
      h('td', d.missing ? h('span.badge.err', 'missing') : h('span.badge', d.kind || '?')),
      h('td.path', { title: d.path }, d.path || ''),
      h('td', { title: d.link || 'registry reference only (no link)' }, d.link ? h('span.badge.ok', 'linked') : h('span.muted', 'ref')),
      h('td.num', fmtBytes(d.size_bytes)),
      h('td', fmtTime(d.mtime)),
      h('td.actions',
        h('button.btn.btn-xs.btn-ghost', { title: 'Re-check size / mtime / existence', on: { click: (e) => { e.stopPropagation(); refreshDataset(d.name); } } }, '↻'),
        h('button.btn.btn-xs.btn-ghost', { title: 'Copy path', on: { click: (e) => { e.stopPropagation(); navigator.clipboard && navigator.clipboard.writeText(d.path).then(() => toast('Path copied')); } } }, '⧉'),
        h('button.btn.btn-xs.btn-ghost.btn-danger', { title: 'Remove from the registry (data is not touched)', on: { click: (e) => { e.stopPropagation(); removeDataset(d); } } }, 'Remove')));
    return tr;
  }));
  $('#data-empty').classList.toggle('hidden', S.datasets.length > 0);
}

async function refreshDataset(name) {
  try { await api.getDataset(name); await loadDatasets(); if (selDataset === name) selectDataset(name); } catch (e) { toastError(e, 'Refresh failed'); }
}

async function removeDataset(d) {
  if (roBlocked('Removing a dataset')) return;
  const ok = await confirmDialog(`Remove ${d.name}?`, h('div', h('p', 'Removes the registry entry', d.link ? ' and its link' : '', '. The data itself is never deleted:'), h('p.mono', d.path)), 'Remove', 'danger');
  if (!ok) return;
  try {
    await api.removeDataset(d.name);
    toast(`Removed ${d.name}`, { kind: 'ok', detail: 'Target data untouched.' });
    if (selDataset === d.name) { selDataset = null; $('#data-preview').replaceChildren(h('div.empty.small', 'Select a dataset to preview it.')); $('#dp-title').textContent = ''; }
    await loadDatasets();
  } catch (e) { toastError(e, 'Remove failed'); }
}

async function selectDataset(name) {
  selDataset = name;
  $$('#data-table tbody tr').forEach((tr) => tr.classList.toggle('on', tr.dataset.name === name));
  const host = $('#data-preview');
  $('#dp-title').textContent = name;
  host.replaceChildren(h('div.empty.small', h('span.spinner'), 'Loading preview…'));
  try {
    const p = await api.previewDataset(name);
    if (selDataset !== name) return;
    host.replaceChildren(previewNode(p));
  } catch (e) {
    if (selDataset !== name) return;
    const t = errText(e);
    host.replaceChildren(h('div.empty.small', { style: { color: 'var(--err)' } }, `${t.title}${t.detail ? ' — ' + t.detail : ''}`));
  }
}

function previewNode(p) {
  if (!p) return h('div.empty.small', 'Empty preview.');
  if (Array.isArray(p.columns)) {
    const rows = p.rows || [];
    return h('table.table', h('thead', h('tr', h('th.rn', '#'), p.columns.map((c) => h('th', c)))),
      h('tbody', rows.map((r, i) => h('tr', h('td.rn', i + 1), p.columns.map((c, j) => { const v = Array.isArray(r) ? r[j] : r[c]; return h('td', { title: v == null ? '' : String(v) }, v == null ? '' : String(v)); })))));
  }
  if (p.kind === 'dir' || Array.isArray(p.entries)) {
    const ents = p.entries || [];
    return h('table.table', h('thead', h('tr', h('th', 'Name'), h('th.num', 'Size'))),
      h('tbody', ents.map((e) => typeof e === 'string' ? h('tr', h('td', e), h('td')) :
        h('tr', h('td', (e.is_dir || e.dir || e.type === 'dir' ? '📁 ' : '') + (e.name || e.path || '')), h('td.num', fmtBytes(e.size_bytes ?? e.size))))));
  }
  const text = p.head ?? p.text ?? p.content;
  if (text != null) return h('pre', typeof text === 'string' ? text : JSON.stringify(text, null, 2));
  return h('pre', JSON.stringify(p, null, 2));
}

// ───────────────────────────── keyboard ─────────────────────────────
function bindKeys() {
  document.addEventListener('keydown', (e) => {
    const t = e.target;
    const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || (t.closest && t.closest('.CodeMirror')));
    const ctrl = e.ctrlKey || e.metaKey;
    if (document.querySelector('.modal-back')) return;
    if (e.defaultPrevented) return;      // already handled (CodeMirror's own Ctrl+S / Ctrl+Enter / Ctrl+F / Ctrl+J)
    if (ctrl && e.key === 'Enter') { e.preventDefault(); if (ed.hasFocus()) saveTexAndRecompile(); else compilePaper(); return; }
    if (ctrl && (e.key === 's' || e.key === 'S')) {
      e.preventDefault();
      if (ed.hasFocus() || S.texDirty) saveTexAndRecompile(); else saveNow().then((ok) => ok && S.spec && toast('Spec saved', { kind: 'ok' }));
      return;
    }
    // Ctrl+F outside a text field searches the section editor (the page itself is a canvas)
    if (ctrl && !e.shiftKey && !e.altKey && (e.key === 'f' || e.key === 'F') && !typing && S.tex && !S.tex.loading && !S.tex.error) {
      e.preventDefault(); setLeftOpen(true); closeNav(); ed.openFind(); return;
    }
    if (e.key === 'Escape') {
      if (navOpen()) { closeNav(); return; }
      closePopover(); if (!$('#err-panel').classList.contains('hidden')) $('#err-panel').classList.add('hidden'); return;
    }
    if (typing || ctrl || e.altKey) return;
    switch (e.key) {
      case 'ArrowLeft': case 'PageUp': if (S.mode === 'page') { e.preventDefault(); pageStep(-1); } break;
      case 'ArrowRight': case 'PageDown': if (S.mode === 'page') { e.preventDefault(); pageStep(1); } break;
      case '+': case '=': e.preventDefault(); zoom(1.2); break;
      case '-': case '_': e.preventDefault(); zoom(1 / 1.2); break;
      case '0': zoomFit(); break;
      case 'f': case 'F': zoomFigure(); break;
      case '1': setMode('page'); break;
      case '2': setMode('context'); break;
      case '3': setMode('plot'); break;
      case 'e': case 'E': e.preventDefault(); setLeftOpen(true); ed.focus(); break;
      case 'm': case 'M': toggleNav(); break;
      case '[': togglePanel('left'); break;
      case ']': togglePanel('right'); break;
      case ',': setAllGroups(false); break;
      case '.': setAllGroups(true); break;
      case 'd': case 'D': toggleDrawer(); break;
      case 'l': case 'L': $('#live-toggle').click(); break;
      case 'c': case 'C': if (!e.repeat) { $('#btn-compare').classList.add('held'); viewer.setComparing(true); } break;
      case 'r': case 'R': if (S.spec) renderAndRecompile(); break;
      case '?': showShortcuts(); break;
      default: break;
    }
  });
  document.addEventListener('keyup', (e) => { if (e.key === 'c' || e.key === 'C') { $('#btn-compare').classList.remove('held'); viewer.setComparing(false); } });
  window.addEventListener('beforeunload', (e) => {
    // last unsaved spec edit: still compare-and-swap (X-Spec-Base-Hash), so a stale tab can only fail safely (409)
    if (S.spec && S.editSeq !== S.savedSeq && !S.readonly && !S.specConflict && S.specHash) {
      try { fetch(`api/specs/${encodeURIComponent(S.spec.id)}`, { method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json', 'X-Designer': '1', 'X-Spec-Base-Hash': S.specHash }, body: JSON.stringify(S.spec) }); } catch { /* best effort */ }
    }
    if (S.texDirty) { e.preventDefault(); e.returnValue = ''; }
  });
}

function showShortcuts() {
  const rows = [
    ['← / →', 'Previous / next page'], ['+ / −', 'Zoom in / out (Ctrl+wheel zooms at the cursor)'], ['0', 'Fit width'], ['F', 'Zoom to the selected figure'],
    ['1 / 2 / 3', 'Page / Context / Plot only'], ['Ctrl+Enter', 'Recompile the paper (in the editor: save the section + recompile)'], ['Ctrl+S', 'Save the section and recompile (editor), or flush the spec save'],
    ['Ctrl+F', 'Find in the section (Shift+Ctrl+F replace, Alt+G go to line)'], ['Ctrl+J', 'Show the editor line on the page (or Ctrl+click the line)'], ['Ctrl+/', 'Comment / uncomment the selected lines with % (in the editor)'], ['M', 'Menu: sections, pages, figures'],
    ['R', 'Render + recompile the current spec'], ['L', 'Toggle the live overlay'], ['C (hold)', 'Compare: hide the overlay to see the compiled figure'],
    ['E', 'Focus the TeX editor'], ['[ / ]', 'Hide / show the left (TeX editor) / right (controls) panel'], [', / .', 'Open / close every group in the right panel'], ['D', 'Toggle the Data drawer'], ['Drag', 'Pan the page · click a figure to select it · click text to open its line in the editor'], ['Double-click slider', 'Reset to default'],
  ];
  modal({ title: 'Keyboard shortcuts', cancel: null, actions: [{ label: 'Close', kind: 'primary', value: true }],
    body: h('div.kbd-table', rows.map(([k, v]) => [h('span', k.split(' / ').map((x, i) => [i ? ' / ' : '', h('kbd', x)])), h('span', v)])) });
}

/** Wrap an async dialog opener so double clicks during its awaits do not open it twice. */
function once(fn) {
  let busy = false;
  return async (...a) => { if (busy || modalOpen()) return; busy = true; try { return await fn(...a); } finally { busy = false; } };
}

function modalOpen() { return !!document.querySelector('.modal-back'); }

function setMsg(text) { $('#sb-msg').textContent = text || ''; }

boot().catch((e) => { console.error(e); toastError(e, 'Startup failed'); });
