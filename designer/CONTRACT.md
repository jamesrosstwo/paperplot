# Plot designer — build contract

A local web app for designing paperplot figures *inside* the RSS paper. The user moves sliders and dropdowns,
sees the plot redraw in place on the compiled paper page, edits the TeX around it, recompiles the paper, and
commits the figure to the Overleaf git repo when happy.

Run: from `C:\Users\James\Desktop\paperplot`, `C:\ProgramData\anaconda3\python.exe -m designer` → http://127.0.0.1:8766
(`PYTHONUTF8=1`). Python 3.12 (Anaconda) with Flask 3.0, matplotlib 3.8, pandas, PyYAML, jinja2. **No Node, no
PyMuPDF, no pypdf, no FastAPI.** Do not pip-install anything. The frontend is plain HTML/CSS/JS (ES modules) with
libraries from cdnjs pinned to exact versions (pdf.js `pdfjs-dist` 3.11.174 from
`https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js` + `pdf.worker.min.js`; CodeMirror 5.65.16 from cdnjs
for the TeX editor, `stex` mode).

All paths come from `designer/config.py` (`PAPER_ROOT`, `SPECS_DIR`, `REGISTRY_PATH`, `LINKS_DIR`, `CACHE_DIR`,
`STATIC_DIR`, `PORT`, `latex_env()`). Never hard-code them.

## Hard safety rules (every module, every test)

1. **The live Overleaf clone `C:\Users\James\Desktop\4D_Curricula_RSS_overleaf` must not be modified during the build.**
   All testing sets `DESIGNER_PAPER_ROOT` to the scratch clone (path given in your prompt) and `DESIGNER_STATE_DIR` to a
   scratch state dir. Never run git commit/push/pull in the live clone.
2. **Nothing is ever pushed automatically.** `gitops.push` runs only from an explicit user click (POST /api/git/push).
   Never force-push. Pull is `--ff-only`. Commit stages only the paths it is given.
3. **Data is never copied.** The data store records absolute paths and optionally makes a junction/symlink; removing a
   dataset removes only the registry entry and the link, never the target.
4. Do not edit the existing paperplot library (`paperplot/`), its themes, or `figures/*.py`. Import from them freely.
5. Writes into the paper repo are limited to: a spec's own output PDF/PNG, a figure environment the user edited in the
   TeX editor (with a stale-file check), and LaTeX build artefacts (already git-ignored). Nothing else.

## Module ownership (one owner per file; import others only through the APIs below)

| File | Owner |
|---|---|
| `designer/spec.py`, `designer/render.py`, `designer/seeds/*.yaml` (seed) | spec/render builder |
| `designer/latex.py` | LaTeX builder |
| `designer/datastore.py`, `designer/gitops.py` | data/git builder |
| `designer/server.py`, `designer/__main__.py`, `designer/jobs.py` | server builder |
| `designer/static/index.html`, `app.js`, `pdfview.js`, `controls.js`, `styles.css` (any files under `static/`) | frontend builder |
| `designer/config.py`, `designer/__init__.py`, this file | fixed (do not edit) |

Each backend module gets a `designer/tests/test_<module>.py` (plain asserts, runnable with `python -m pytest` if pytest
exists, else `python designer/tests/test_x.py`). Tests use only the scratch paper clone / scratch state dir.

## 1. Spec (`designer/spec.py`)

A figure is a YAML file `SPECS_DIR/<id>.yaml`:

```yaml
id: mug_ours_auxq2_levels_short          # [a-z0-9_]+, = file stem
kind: grouped_bars                       # only kind for now; keep the code open to more kinds
title: Mug SR by difficulty, base vs baselines vs ours
output: figures/evals/mug_ours_auxq2_levels_short.pdf   # relative to PAPER_ROOT; render also writes the .png beside it
data:
  source: inline                         # inline | dataset
  levels: [Nominal, Easy, Medium]        # x groups
  arms:                                  # legend entries, drawn left to right in each group
    - {name: '$\mathrm{\pi}_{0.5}$ (base policy)', color: tol-blue, counts: [[16, 20], [12, 20], [9, 20]]}
    - {name: Classical, color: tol-yellow, counts: [[3, 5], [3, 10], null]}     # null = not evaluated -> "n/a"
  # source: dataset ->
  # dataset: difficulty_eval             # name in the data store
  # query: {level: level, arm: method, successes: successes, trials: trials, filter: {task: mug},
  #         levels: [Nominal, Easy, Medium], arms: [{key: base, name: '...', color: tol-blue}, ...]}
style:
  theme: acme              # dropdown: themes in paperplot/themes/*.yaml
  font_family: null        # dropdown: null = theme's; else a family name (Roboto Slab, Times New Roman, DejaVu Serif, ...)
  width_pt: 252            # slider 120..516 (RSS single = 252, double = 516)
  height_pt: 69            # slider 40..300
  group_width: 0.66        # slider 0.3..1.0  — fraction of each group slot the bars span (inter-group space = 1 - this)
  bar_fill: 0.583          # slider 0.2..1.0  — bar width as a fraction of its sub-slot (gap between bars = 1 - this)
  legend_loc: above        # dropdown: above | right | none
  legend_cols: 5           # slider 1..8
  legend_fontsize: 6       # slider 4..12 (pt)
  tick_fontsize: null      # slider 4..12 (pt); null = theme
  xtick_fontsize: 7        # slider 4..14 (pt)
  ylabel: Successes        # text
  ylabel_fontsize: null    # slider; null = theme
  y_mode: count            # dropdown: percent | count   (count: tick labels k/out_of, bars still at each arm's rate)
  out_of: 20               # number (count mode)
  ytick_step: 25           # slider 5..50 (in percent units)
  ci: wilson               # dropdown: wilson | bayes | none   (bayes = paperplot.stats.rate_interval)
  ci_level: 0.95           # dropdown: 0.8 | 0.9 | 0.95 | 0.99
  ci_color: "#222222"
  ci_linewidth: 0.6        # slider 0.2..2 (pt)
  ci_capsize: 1.5          # slider 0..4 (pt)
  partial_below: 20        # number or null: CI bars of arms with fewer trials at a level drawn in partial_ci_color
  partial_ci_color: "#CC3311"
  divider_before: null     # null or level index: dashed vertical rule left of that group
  fill_darken: 0.7         # slider 0.4..1.0
  edge_darken: 0.6         # slider 0.3..1.0
  edge_width: null         # slider 0..1.5 (pt); null = theme
  palette: null            # dropdown: null (per-arm colors) | a theme palette name (e.g. categorical) to recolor arms in order
  na_label: n/a
  transparent: false
```

API:
- `list_specs() -> list[dict]` — `{id, title, kind, output}` for each spec.
- `load(id) -> dict`, `save(spec: dict) -> None` (validates; writes YAML with stable key order, keeps comments optional),
  `create(id, template_id=None) -> dict`, `delete(id)`.
- `validate(spec) -> list[str]` (empty = ok): ids, ranges, counts shape `len(counts)==len(levels)`, `k<=n`, colors resolvable.
- `defaults() -> dict` — a complete default `style` block.
- `controls() -> dict` — **drives the UI**: `{"style": [ {key, label, type: slider|dropdown|number|text|color|toggle,
  min, max, step, options: [{value, label}], nullable, help, group: "Size"|"Bars"|"Axes"|"Legend"|"Error bars"|"Colors"} ]}`.
  Dropdown options are discovered at call time: themes from `paperplot/themes/*.yaml`; fonts from the theme font files
  plus matplotlib's font manager (deduplicated, sorted, common paper fonts first); palettes from the selected theme's
  `colors:` lists; color swatches from the theme's `swatches:`.
- `swatches(theme) -> dict[name, hex]`.

## 2. Render (`designer/render.py`)

Generic renderer for spec kinds, ported from `figures/mug_view_mix_ab.py::chart()` (read it; reproduce its look exactly
for the seed spec) using `paperplot.use`, `paperplot.subplots`, `paperplot.bar`, `paperplot.save`.

- `render(spec, out_pdf: Path | None = None) -> dict` — writes PDF (exact size, never cropped) + same-size 600 dpi PNG
  via `paperplot.save`; default `out_pdf = PAPER_ROOT / spec["output"]`. Returns `{pdf, png, width_pt, height_pt,
  warnings: [...], stats: [{level, arm, k, n, rate, lo, hi}]}`.
- `preview_png(spec, dpi=200) -> bytes` — fast in-memory PNG at the same physical size (for live slider feedback; aim
  < 0.5 s). Must not write into the paper repo.
- `resolve_data(spec) -> {levels, arms:[{name, color, counts}]}` — inline as is; dataset via `datastore.load_table` +
  the query mapping.
- Thread safety: matplotlib global state — serialize renders with a module lock.
- Seed spec `designer/seeds/mug_ours_auxq2_levels_short.yaml` (copied once into `SPECS_DIR`; `designer/seeds/` is never
  a state dir, so a running designer cannot edit a seed) reproducing the current
  `figures/out/mug_ours_auxq2_levels_short.png` (find its chart() call in `figures/mug_view_mix_ab.py`; width "single",
  height 69pt, group_width 0.66, bar_fill 0.583, legend 5 cols, ylabel Successes, ytick_step 25, out_of 20,
  partial_below 20, theme acme). Verify by pixel-comparing your PNG with that file.
- A second seed spec, dataset-backed, from `data/difficulty_eval.tsv` (look at its columns) so the dataset path is exercised.

## 3. LaTeX (`designer/latex.py`)

- `scan_figures(root=PAPER_ROOT) -> list[dict]` — follow `\input{}`/`\include{}` from `MAIN_TEX` (relative to root,
  `.tex` optional), find every `figure`/`figure*` environment (skip fully-commented ones but report them with
  `commented: true`): `{fid (stable: "<relfile>:<label or graphics stem>"), file (rel, posix), begin_line, end_line (1-based),
  env, label, caption (raw TeX, first 200 chars), graphics: [{path (as written), resolved (rel path with extension,
  or null), options, line}], spec_id (id of the designer spec whose output == resolved, or null)}`.
- `compile_paper(root=PAPER_ROOT, timeout=240) -> dict` — `latexmk -pdf -interaction=nonstopmode -f -synctex=1 paper.tex`
  in `root` with `config.latex_env()`; one compile at a time (lock); returns `{ok, pdf (abs path), seconds, errors:
  [{file, line, message}], warnings_count, log_tail}`. Parse errors from the .log (`! ...` + `l.<n>`).
- `locate(fig, root=PAPER_ROOT) -> dict | None` — page and box of a figure in the last compiled PDF using
  `synctex view -i <line>:1:<file> -o paper.pdf` (run in root, with latex_env) on the `\includegraphics` line; parse
  `Page:`, `h:`, `v:`, `W:`, `H:` (synctex units are PDF bp, v is the baseline from the top). Return
  `{page (1-based), x, y, w, h}` in PDF points with origin top-left, plus `page_w, page_h` if known. Also
  `locate_all(root) -> {fid: box}` cached in `CACHE_DIR/locate.json` keyed by the PDF mtime.
- `reverse(page, x, y, root) -> {file, line}` via `synctex edit -o <page>:<x>:<y>:paper.pdf` (click on the page → source).
- `read_env(fid, root) -> {text, file, begin_line, end_line, sha}` and `write_env(fid, text, expected_sha, root)` —
  replace exactly that environment; refuse (raise `StaleError`) if the file changed since `sha` was taken.
- `insert_figure(file_rel, after_line, graphics_rel, caption, label, env="figure", width="\\columnwidth", root)` —
  insert a new figure environment (for a spec not yet in the paper); returns the new fid.
- `compile_context(fid, text_override=None, lines_before=12, lines_after=12, root) -> {ok, pdf, seconds, errors}` —
  fast preview: a standalone `.tex` in `CACHE_DIR/context/` that reuses the paper's preamble (everything before
  `\begin{document}` in MAIN_TEX, with `\graphicspath`/relative paths fixed to point at root) and contains only the
  surrounding paragraphs + the figure env (or `text_override`), compiled with `pdflatex` (one pass, no bibtex,
  `\nocite`-safe: define missing refs as ??). Should take a few seconds, not the full paper time.
- Tests against the scratch clone: scan finds `mug_conditions`, `difficulty_by_task`, `headline`, `projection`; a full
  compile succeeds; locate returns a box on the right page for each; write_env round-trips; StaleError works.

## 4. Data store (`designer/datastore.py`) and git (`designer/gitops.py`)

datastore — registry JSON at `REGISTRY_PATH`: `[{name, path (absolute, resolved), kind (tsv|csv|json|yaml|npz|dir|other),
link (abs path of the junction/symlink in LINKS_DIR or null), added (ISO), notes, size_bytes, mtime}]`.
- `add(name, ref, notes="", make_link=True) -> dict` — `ref` may be an absolute path, a `file:///C:/...` URL, a Windows
  `.lnk` shortcut (resolve its target; via PowerShell `(New-Object -ComObject WScript.Shell).CreateShortcut(p).TargetPath`),
  or an existing symlink/junction (resolve it). Must exist on this machine. Never copy. `make_link`: directories →
  junction (`cmd /c mklink /J`, needs no admin); files → try `os.symlink`, and if Windows refuses (no developer mode),
  fall back to `link: null` (registry reference only; no hard links, no copies) and say so in the returned `warning`.
- `list_datasets()`, `get(name)`, `remove(name)` (registry + link only; never the target; refuse if the link path is
  not inside LINKS_DIR), `refresh(name)` (size/mtime; flag `missing: true` if the target is gone).
- `preview(name, rows=50) -> {kind, columns, rows}` for tables; `{kind:'text', head}` for text; `{kind:'dir', entries}`
  for directories (first 200, with sizes); json/yaml → pretty head.
- `load_table(name, sheet=None) -> pandas.DataFrame` (tsv/csv/json records/yaml list).
gitops (repo = PAPER_ROOT; all via `subprocess` git with a timeout, return dicts, never raise on git failure):
- `status() -> {branch, upstream, ahead, behind, dirty: [{path, code}], last_commit: {sha, subject, when}}`
  (`git status --porcelain=v1 -b`; ahead/behind from the branch line; a separate `fetch()` updates the remote ref).
- `fetch()`, `pull()` (`git pull --ff-only`), `diff_stat(paths)`, `commit(paths, message)` (`git add -- <paths>` then
  `git commit -m` with only those paths; refuse paths outside the repo), `push()` (`git push`, never `--force`).
- Tests use a throwaway repo made with `git init` in a temp dir plus a bare repo as its remote.

## 5. Server (`designer/server.py`, `__main__.py`, `jobs.py`)

Flask, binds `DESIGNER_HOST:DESIGNER_PORT` (default 127.0.0.1:8766; a non-loopback host needs a token, see section 7). JSON in/out. Long tasks (full compile) run as background jobs (`jobs.py`: a thread
pool of 1 for LaTeX, job ids, status `queued|running|done|error`, result). Endpoints:

| Method + path | Does |
|---|---|
| `GET /` | `static/index.html` (and `/static/<path>`) |
| `GET /api/config` | `{paper_root, main_tex, port, has_pdf}` |
| `GET /api/specs` · `POST /api/specs` `{id, template_id?}` | list / create |
| `GET/PUT/DELETE /api/specs/<id>` | load / validate+save (400 with `errors`) / delete |
| `GET /api/controls?theme=` | `spec.controls()` |
| `POST /api/preview` body = spec | `image/png` from `render.preview_png` (no file writes) |
| `POST /api/specs/<id>/render` | `render.render` into the paper repo → `{pdf, png, stats, warnings}` |
| `POST /api/compile` · `GET /api/jobs/<job>` | full compile job; result includes `latex.locate_all` |
| `GET /paper.pdf` | the compiled PDF (no-cache headers) |
| `GET /api/figures` | `scan_figures` + boxes from `locate_all` (null when stale/missing) |
| `GET/PUT /api/figures/<fid>/tex` | `read_env` / `write_env` (409 on StaleError) — fid is URL-encoded |
| `POST /api/figures/insert` | `insert_figure` |
| `POST /api/context/<fid>` `{text?}` | `compile_context` → `{pdf_url: /context/<name>.pdf, ...}`; `GET /context/<name>.pdf` |
| `POST /api/synctex/reverse` `{page, x, y}` | `latex.reverse` |
| `GET/POST /api/data` · `GET/DELETE /api/data/<name>` · `GET /api/data/<name>/preview` | data store |
| `GET /api/git/status` · `POST /api/git/fetch|pull|commit|push` | gitops (commit body `{paths, message}`) |

Errors: JSON `{error, detail}` with proper status codes. `python -m designer` prints the URL and runs the server.
CLI flags (both entry points): `--paper-root PATH`, `--state-dir PATH`, `--port N` — they set `DESIGNER_PAPER_ROOT` /
`DESIGNER_STATE_DIR` / `DESIGNER_PORT` **before** `designer.config` is imported. `designer/serve.py` is a script runnable
from any cwd (`python C:/Users/James/Desktop/paperplot/designer/serve.py --paper-root ...`): it puts the paperplot root on
`sys.path`, applies the flags, and starts the server. `.claude/launch.json` (in paperplot) has two entries: `designer`
(live paper, port 8766) and `designer-test` (scratch clone, port 8767) — use only `designer-test` while building.

## 6. Frontend (`designer/static/`)

Layout (full window, desktop-first; light theme with neutral grays, accent = tol-red-ish `#B5454F`):

```
┌ top bar: Plot Designer · paper root · git pill (branch, ↑ahead ↓behind, ● N dirty) · [Fetch][Pull][Commit…][Push] · [Recompile paper] · status ┐
├──────────────┬──────────────────────────────────────────────────────────┬──────────────────────────┤
│ LEFT sidebar │ CENTER (largest): the compiled paper page at high zoom,   │ RIGHT controls           │
│ - page thumbs│ scrolled/zoomed to the selected figure, with the live     │ spec picker + New        │
│   (pdf.js),  │ preview PNG overlaid exactly on the figure's box (instant │ grouped sliders/dropdowns│
│   figure     │ feedback while dragging; "Render + recompile" to make it  │ from /api/controls       │
│   boxes drawn│ real). Toggle: Page | Context (fast context compile PDF) │ data editor: levels,     │
│   on thumbs  │ | Plot only. Below it, a collapsible TeX editor           │ arms (name, color, k/n   │
│ - figure list│ (CodeMirror stex) for the selected figure environment:    │ table) or dataset+query  │
│   by section │ caption, includegraphics options, surrounding text;      │ [Render + recompile]     │
│   (spec-backed│ [Save TeX] [Preview context] [Save + recompile].          │ auto-recompile toggle    │
│   ones marked)│ Click on the page → synctex reverse → jump editor.        │ (debounced 1.5 s)        │
├──────────────┴──────────────────────────────────────────────────────────┴──────────────────────────┤
│ bottom drawer (toggle "Data"): dataset registry table · add by path / file:// link / .lnk / symlink · preview grid │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- Controls are generated from `/api/controls`: slider (with live numeric readout and a reset-to-default ⟲), dropdown,
  number, text, color (swatch dropdown + hex), toggle; grouped and collapsible; nullable keys get an "auto" checkbox.
- Dragging a slider re-requests `/api/preview` (debounced ~120 ms, cancel stale requests) and swaps the overlay image.
  The overlay is positioned using the figure box from `/api/figures` scaled to the canvas; if the spec's figure is not
  in the paper yet, show "Plot only" mode plus an "Insert into paper…" button (pick section file + after which line).
- Spec changes are saved with PUT (debounced) — no explicit save needed; show a saved/unsaved indicator.
- Recompile shows progress, then reloads `/paper.pdf` keeping the current page/zoom and figure selection; LaTeX errors
  appear in a dismissible panel with file:line links that open the editor at that line.
- Selecting a figure (thumb box, list, or click on the page) highlights it everywhere; if it has `spec_id`, loads the
  spec into the controls; otherwise shows its TeX only (with "this figure is not designer-made").
- Git: Commit opens a dialog listing changed paths with checkboxes (default: the selected figure's pdf/png + edited
  .tex), message field; Push asks for confirmation ("Push to Overleaf?") — never pushes on its own.
- Keyboard: ←/→ pages, +/- zoom, Ctrl+Enter recompile.
- No console errors; works in the Claude desktop browser pane (Chromium).

## 7. Deployment as a network service (added 2026-11-08 — applies to every module)

The app must also run as a long-lived service published on a port (e.g. on borabora or this machine, reached over the
tailnet or behind a reverse proxy), not only as a localhost tool.

- **Bind/port/paths are configuration, not code.** Env vars (and matching CLI flags): `DESIGNER_HOST` (default
  `127.0.0.1`), `DESIGNER_PORT`, `DESIGNER_PAPER_ROOT`, `DESIGNER_STATE_DIR`, `DESIGNER_LATEX_BIN` (dir holding latexmk /
  pdflatex / synctex; default: MiKTeX path on Windows, `PATH` lookup elsewhere), `DESIGNER_BASE_PATH` (URL prefix when
  served under a reverse-proxy subpath, e.g. `/designer`; default empty), `DESIGNER_TOKEN`, `DESIGNER_READONLY`.
- **Cross-platform.** No Windows-only assumptions on the code path: `latex_env()` must work on Linux (no claude.exe PATH
  stripping needed there); data-store links use junctions on Windows and `os.symlink` on POSIX; `.lnk` resolution only on
  Windows; paths handled with `pathlib`, posix-style in JSON.
- **Auth.** When `DESIGNER_HOST` is not a loopback address, the server refuses to start unless `DESIGNER_TOKEN` is set.
  With a token, every `/api/*`, `/paper.pdf` and `/context/*` request needs it (`Authorization: Bearer <t>` header, or a
  `designer_token` HttpOnly cookie set by visiting `/?token=<t>` once, after which the URL is cleaned). Static assets may be
  public. Compare tokens with `hmac.compare_digest`.
- **Read-only mode** (`DESIGNER_READONLY=1`): everything that writes the paper repo, the registry, or git (render into
  repo, write_env, insert, data add/remove, commit/pull/push) returns 403; preview/context/compile into the cache still work.
- **Production server.** `serve.py --prod` uses `waitress` if importable (`waitress.serve(app, host, port, threads=8)`),
  else falls back to Flask's threaded server with a warning. Provide `designer/wsgi.py` exposing `app` for any WSGI server.
- **Subpath-safe frontend.** All frontend requests use paths relative to the page (no leading `/` in fetch URLs), or read
  the base path the server injects into index.html, so the UI works at `https://host/designer/`.
- **Health + ops.** `GET /healthz` (no auth) → `{ok, version, paper_root_exists, latex_found}`. Logs to stdout. Graceful
  handling of concurrent users: one compile at a time per paper root (queue, report position), specs saved atomically
  (write temp + rename), registry writes under a file lock.
- **Packaging.** `designer/requirements.txt` (flask, matplotlib, pandas, pyyaml, waitress optional), a
  `designer/README.md` with: local run, service run (env vars, systemd user unit example for Linux, a Windows example), and
  security notes (token, read-only, never expose without a token).

## 8. Docker (added 2026-11-08)

The service form of the app is a Docker image; the Windows-native run stays supported for local use.

- `designer/docker/Dockerfile`: one image with Python 3.12 + flask, matplotlib, pandas, pyyaml, waitress; a full enough
  TeX Live for the RSS paper (latexmk, pdflatex, synctex, bibtex, IEEEtran, the packages paper.tex uses — base on a pinned
  `texlive/texlive:<TL year>` tag or Debian + texlive-* packages; verify the paper compiles in it); fonts: Roboto Slab
  (bundled in paperplot/fonts) and Times New Roman (`ttf-mscorefonts-installer`, EULA accepted at build time; rebuild the
  matplotlib font cache). The image contains the `paperplot` package and `designer/`; it runs
  `python designer/serve.py --prod --host 0.0.0.0 --port 8766` as a non-root user with the host user's uid/gid (build args)
  so files written into mounted repos keep the right owner.
- Mounts (documented in `docker/docker-compose.yml` with env-var placeholders): the paper repo at `/paper`
  (`DESIGNER_PAPER_ROOT=/paper`, read-write), the state dir at `/state`, and data directories **at the same absolute path
  as on the host** (read-only) so registered dataset paths and symlinks resolve identically inside and outside the
  container. The data store must reject paths that do not exist inside the container with a clear message
  ("mount <dir> into the container").
- Git inside the container: commits use a configurable author (`DESIGNER_GIT_NAME`, `DESIGNER_GIT_EMAIL`); push uses the
  host's credentials mounted read-only (`~/.git-credentials` or a credential helper file) — documented, never baked in.
  `safe.directory` set for /paper.
- `DESIGNER_TOKEN` is required (the container binds 0.0.0.0). Healthcheck in the Dockerfile hits `/healthz`.
- Missing fonts must never silently fall back: render() raises (or returns a blocking warning shown in the UI) when the
  theme's font family is not available to matplotlib.
- Scripts: `docker/build.sh` and `docker/run.sh` (and `.ps1` equivalents for Docker Desktop on Windows).
