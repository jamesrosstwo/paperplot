# Plot designer

A small web app for designing [paperplot](../paperplot) figures *inside* the RSS paper: move sliders and dropdowns,
see the plot redraw in place on the compiled paper page, edit the TeX around it, recompile the paper, and commit the
figure to the paper's (Overleaf) git repo when happy. The full build contract is in [CONTRACT.md](CONTRACT.md).

- Backend: Flask (`designer/server.py`), matplotlib via paperplot, LaTeX via latexmk/pdflatex/synctex, git via the
  `git` CLI. Frontend: plain HTML/JS in `designer/static/` (pdf.js and CodeMirror from cdnjs).
- The same code runs natively on **Windows** (MiKTeX) and on **Linux** (TeX Live), and in **Docker**
  (see [docker/README.md](docker/README.md)).
- Nothing is pushed automatically: Push runs only from an explicit, confirmed click. Pull is `--ff-only`, never force.
- Data is never copied: the data store records absolute paths and (optionally) makes a junction/symlink.
- Source of truth: [github.com/jamesrosstwo/paperplot](https://github.com/jamesrosstwo/paperplot); the service runs in
  Docker on borabora ([docker/README.md](docker/README.md)). Run it natively only from a clone of that repo.

## Requirements

- Python 3.10 - 3.12 with `pip install -r designer/requirements.txt` (flask, matplotlib, pandas, numpy, scipy, pyyaml,
  waitress). On James's Windows machine everything except waitress is already in Anaconda; do not pip-install there.
- A TeX distribution with `latexmk`, `pdflatex`, `bibtex` and `synctex` (MiKTeX on Windows, TeX Live on Linux), and
  `git`.
- The paper's fonts available to matplotlib: Roboto Slab is bundled in `paperplot/fonts/` (registered automatically);
  Times New Roman (theme `acme_tnr`) must be installed (`ttf-mscorefonts-installer` on Debian/Ubuntu, then
  `rm -rf ~/.cache/matplotlib`). **Fonts never fall back silently**: if the chosen font, or a theme's first font
  family, cannot be found, preview and render fail with HTTP 422 `font unavailable` and the UI says which font is
  missing. `GET /api/fonts/check` lists whether each theme's primary font resolves. (STIX for math glyphs that Roboto
  Slab lacks, e.g. pi, is intended and stays.)

## Run locally (Windows, optional)

From a local clone of the repo (the old `Desktop\paperplot` copy is retired; the default paper root on Windows is
still the Overleaf clone at `C:\Users\James\Desktop\4D_Curricula_RSS_overleaf`):

```bat
cd <your clone of jamesrosstwo/paperplot>
set PYTHONUTF8=1
C:\ProgramData\anaconda3\python.exe -m designer
```

This serves the live Overleaf clone on http://127.0.0.1:8766/ (the default paper root on Windows), with no token
(loopback only). For another paper or state directory, or a second instance:

```bat
C:\ProgramData\anaconda3\python.exe C:\Users\James\Desktop\paperplot\designer\serve.py ^
    --paper-root D:\work\paper-copy --state-dir D:\work\designer-state --port 8768
```

## Run locally (Linux / macOS)

There is no default paper on Linux: pass `--paper-root` (or set `DESIGNER_PAPER_ROOT`).

```sh
cd ~/paperplot
python3 -m venv .venv && . .venv/bin/activate && pip install -r designer/requirements.txt
python designer/serve.py --paper-root ~/papers/rss --state-dir ~/.local/share/plot-designer
# -> http://127.0.0.1:8766/
```

## Flags and environment variables

Every flag sets the matching environment variable before the config is read, so either works.

| Flag | Variable | Default | Meaning |
|---|---|---|---|
| `--host` | `DESIGNER_HOST` | `127.0.0.1` | bind address. **Anything but loopback requires a token** (the server refuses to start). |
| `--port` | `DESIGNER_PORT` | `8766` | port |
| `--paper-root` | `DESIGNER_PAPER_ROOT` | Windows: the live Overleaf clone; elsewhere: required | the LaTeX project (git repo) |
| `--state-dir` | `DESIGNER_STATE_DIR` | `designer/` | specs, data registry (`data/registry.json`), links, cache |
| `--base-path` | `DESIGNER_BASE_PATH` | empty | URL prefix behind a reverse proxy, e.g. `/designer` |
| `--token` | `DESIGNER_TOKEN` | none | access token (prefer the variable: command lines are visible to other users) |
| `--readonly` | `DESIGNER_READONLY=1` | off | no writes to specs, the paper repo, the registry or git |
| `--latex-bin` | `DESIGNER_LATEX_BIN` | Windows: the MiKTeX bin dir if it exists; else `PATH` | directory with latexmk / pdflatex / synctex |
| `--prod` | | off | serve with waitress (8 threads) if installed; otherwise Flask's threaded server, loopback only |
| `--allow-dev-server` | | off | allow Flask's development server on a non-loopback address (not recommended) |
| | `DESIGNER_GIT_NAME`, `DESIGNER_GIT_EMAIL` | git config | author/committer of commits made from the designer |
| | `DESIGNER_DATA_ROOTS` | unset | directories datasets may live in, separated by `:` (Linux) or `;` (Windows). Unset on a tokenless loopback server: anywhere. Unset with a token or a non-loopback host: **nothing can be registered** until it is set. |
| | `DESIGNER_DEBUG=1` | off | include Python tracebacks in 500 responses (always on for the tokenless loopback tool) |

Seed specs ship in `designer/seeds/` and are copied into `<state dir>/specs/` once (never over an existing spec);
the designer never writes into `designer/seeds/`.

Other entry points: `python -m designer [flags]` (same flags) and `designer/wsgi.py` (`app`) for any WSGI server, e.g.
`waitress-serve --host 127.0.0.1 --port 8766 --threads 8 --no-clear-untrusted-proxy-headers designer.wsgi:app` run
from the paperplot folder (without `--no-clear-untrusted-proxy-headers`, waitress deletes `X-Forwarded-Host/-Proto`
and every write through a Host-rewriting reverse proxy fails the Origin check; `serve.py --prod` sets this itself). Use
**one process** (threads are fine): the compile queue and render lock live in the process. `wsgi.py` refuses to load
without `DESIGNER_TOKEN` unless `DESIGNER_HOST` is explicitly set to a loopback address.

## Using it (2026-11-09 additions)

- **Left: section editor.** One whole `.tex` file of the paper (the main file or anything it `\input`s / `\include`s)
  in CodeMirror. Ctrl+S (or Ctrl+Enter) saves and recompiles, Ctrl+/ toggles % comments, Ctrl+J or Ctrl+click shows the line on the page, a
  click on the page opens its source line. Saves are compare-and-swap on the file's hash: if the file changed on disk
  (an Overleaf pull, another editor) nothing is written and you choose Copy my edits / Reload from disk / Keep mine.
  The hamburger (or `M`) lists sections, pages and figures. `[` / `]` hide the editor / the controls.
- **Right: spec controls**, built from `GET /api/controls` for the spec's kind (`grouped_bars` or `lines`; the Kind
  dropdown switches, keeping every setting that fits the new kind).
  - *Data*: inline counts `k / n`; empty = n/a; the small ↻ in a cell flags it as re-run (`[k, n, {dup: true}]`), drawn
    with the yellow `dup_ci_color` interval. Grouped bars can be **split into stacked panels**: shared arms (name,
    color, slot) plus one tab per panel (title, optional own levels, counts); an arm missing from a panel is n/a there.
    Renaming an arm follows it into every panel and into the arm order.
  - *Order ▸ Arm order*: tick arms to draw them first; the rest as listed, by pooled or mean success rate (one order
    for every panel and group), or manually (↑ / ↓). `{ }` edits the raw value.
  - *Colors ▸ Group colors*: tick a group (x level) to fill all its bars with one color (e.g. Nominal in gray).
  - *Last render*: per-bar stats with a Panel column, ↻ for re-run cells, `p` for partial ones, and the render
    notes (e.g. "yellow CI = positions re-run; latest rollout counted").

Endpoints added for these (all behind the same token / Origin / `X-Designer` checks; writes refused in read-only
mode): `GET /api/tex/sections`, `GET /api/tex/file?path=`, `PUT /api/tex/file {path, text, base_hash}` (409 with the
current text when stale), `POST /api/synctex/forward {file, line}`, and `GET /api/controls?kind=`. `POST /api/preview`
returns the render notes in the `X-Render-Notes` header (URL-encoded JSON list) beside `X-Render-Warnings`.

- **Spec saves are compare-and-swap too.** `GET /api/specs/<id>` sends the file's sha256 in `X-Spec-Hash`; the UI
  sends it back as `X-Spec-Base-Hash` on every PUT (and on the last keepalive PUT when the tab closes). If the YAML
  changed since the tab loaded it (a hand edit, another tab, another designer), the PUT answers 409
  `{error: "stale", current, hash}`, nothing is written, and the UI offers Reload from disk / Keep mine (Keep mine
  asks again, then saves against the reported hash). A spec changed on disk while the tab has no unsaved edits
  reloads on window focus. A PUT without the header (scripts) still saves unconditionally. Saving from the UI
  rewrites the YAML, so header comments in a spec file are lost on the first edit.
- **One server per port.** The server refuses to start when something already answers on its port (on Windows,
  werkzeug's default socket would otherwise share a listening port with a second server and split requests between
  them); the Flask server binds with `SO_EXCLUSIVEADDRUSE` on Windows.
- **Seeds** are copied into a state dir once; a dir that already holds one of the seed specs (seeded before the
  `.seeded` marker existed) is only marked, so a newly added seed does not appear in a live specs folder.
- **Line ends.** Saving a section keeps each line's own ending (CRLF, LF or a lone CR); new lines take the file's
  majority style, so a one-line edit is a one-line diff even in a mixed-ending file.
- **Narrow windows.** At 1000 px or less the controls open as a drawer over the page (× or `]` closes it) and the
  page toolbar scrolls sideways. On wider windows the page view keeps at least min(560 px, 38vw); the editor gives way.

## Run as a service

### Linux: systemd user unit

`~/.config/systemd/user/plot-designer.service`:

```ini
[Unit]
Description=Plot designer (paperplot)
After=network-online.target

[Service]
WorkingDirectory=%h/paperplot
Environment=PYTHONUTF8=1
Environment=DESIGNER_PAPER_ROOT=%h/papers/rss
Environment=DESIGNER_STATE_DIR=%h/.local/share/plot-designer
Environment=DESIGNER_HOST=127.0.0.1
Environment=DESIGNER_PORT=8766
# the token lives in a file only you can read:  umask 077; python3 -c "import secrets; print('DESIGNER_TOKEN=' + secrets.token_urlsafe(32))" > ~/.config/plot-designer.env
EnvironmentFile=%h/.config/plot-designer.env
ExecStart=%h/paperplot/.venv/bin/python designer/serve.py --prod
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload && systemctl --user enable --now plot-designer
journalctl --user -u plot-designer -f          # logs go to stdout
loginctl enable-linger $USER                   # keep it running after logout
```

Reach it through an SSH tunnel (`ssh -L 8766:127.0.0.1:8766 host`) or a reverse proxy; to listen on the tailnet
directly set `DESIGNER_HOST` to the machine's tailnet IP **and** keep the token.

### Windows

A second, long-running instance (e.g. started from Task Scheduler "At log on"), token in the environment:

```bat
set PYTHONUTF8=1
set DESIGNER_TOKEN=<random string, e.g. from: python -c "import secrets; print(secrets.token_urlsafe(32))">
C:\ProgramData\anaconda3\python.exe C:\Users\James\Desktop\paperplot\designer\serve.py --prod ^
    --host 127.0.0.1 --port 8766 --state-dir C:\Users\James\AppData\Local\plot-designer
```

Without waitress installed, `--prod` prints a note and uses Flask's threaded server, which is fine on 127.0.0.1.

### Docker

See [docker/README.md](docker/README.md) (image, compose file, mounts, git credentials, healthcheck).

### Behind a reverse proxy (subpath)

Either strip the prefix in the proxy and set `DESIGNER_BASE_PATH=/designer`, or forward the full path (the server
strips `DESIGNER_BASE_PATH` itself), or send `X-Forwarded-Prefix: /designer`. The page gets a matching
`<base href>` and the frontend uses only relative URLs. nginx example:

```nginx
location /designer/ {
    proxy_pass http://127.0.0.1:8766/;          # trailing slash: strips /designer
    proxy_set_header Host $http_host;           # host:port as the browser sent it ($host drops the port)
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-Prefix /designer;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;                    # full paper compiles take a while
    client_max_body_size 32m;
}
```

## Health and operations

- `GET /healthz` (no token): `{ok, version, paper_root_exists, latex_found}`; no paths, no secrets.
- `GET /api/fonts/check` (token): `{ok, themes: [{theme, family, ok, file}]}`.
- One full compile at a time per server: further requests queue (`GET /api/jobs/<id>` reports `status` and
  `position`); a request while one is still queued joins it. Context previews are serialized too.
- Specs are written atomically (temp file + `os.replace`); registry read-modify-writes hold a cross-process file lock
  (`designer/locks.py`, msvcrt on Windows, fcntl on POSIX).
- Logs go to stdout/stderr.

## Security notes

- **Never expose the designer without a token.** It can rewrite TeX in the paper repo, commit, and push to Overleaf
  with the host's git credentials. A non-loopback `--host` without `DESIGNER_TOKEN` refuses to start; prefer binding
  127.0.0.1 and reaching it through SSH or a TLS-terminating reverse proxy.
- **Token.** Use a long random value (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), keep it in an
  environment file readable only by you, and never commit it. Browsers sign in at `<url>/login` (a form; the page
  sends you there when it gets a 401): the server sets an `HttpOnly`, `SameSite=Strict` cookie (`Secure` over https)
  for 30 days. `/?token=<t>` works too, but a token in a URL lands in browser history and in the access log of any
  proxy in front, so prefer the form (the Flask dev server's own log shows `token=***`). Scripts send
  `Authorization: Bearer <t>`. Tokens are compared with `hmac.compare_digest`. Rotate by restarting with a new value.
  The server never prints the token.
- Static assets, `/`, `/login` and `/healthz` are public; every other route (`/api/*`, `/paper.pdf`, `/context/*`)
  needs the token. Without a token the server only answers requests whose `Host` is loopback (DNS-rebinding
  protection).
- **Data roots.** With a token, the data store registers and previews only files inside `DESIGNER_DATA_ROOTS` (the
  Docker scripts set it to the mounted data dirs). Credentials and system places (`~/.ssh`, `~/.git-credentials`,
  `~/.config/git`, `/etc`, `/proc`, `/run`, ...), the designer's state dir and the paper's `.git` are refused always.
  Without this, a token holder could read any file the server can read through the data preview.
- **TeX.** pdflatex runs with `\write18` disabled and latexmk with `-norc` (a `latexmkrc` committed to the paper is
  Perl and would run on the next compile). On TeX Live the server forces `openin_any=p` / `openout_any=p`, so TeX
  cannot `\input` absolute paths or `..` (MiKTeX ignores these settings; the Windows tool is loopback-only). In
  read-only mode the context preview compiles only the stored figure text (plus the live slider settings), never
  TeX sent by the client. One context preview runs at a time with one waiting; more get 429.
- 500 responses carry a Python traceback only with `DESIGNER_DEBUG=1` or on the tokenless loopback tool.
- git output shown in the UI has credentials in URLs replaced by `***`; still prefer a credential helper over a
  token embedded in the remote URL.
- **CSRF.** Writes must be same-origin (Origin is checked against the request's own Host / `X-Forwarded-Host` and
  scheme / `X-Forwarded-Proto`) and must be JSON or carry `X-Designer: 1`; the server answers no CORS preflights.
- **Read-only** (`DESIGNER_READONLY=1`): spec saves, render-into-repo, TeX writes, figure insertion, dataset
  add/remove/refresh and git fetch/pull/commit/push answer 403; preview, context preview and recompile still work. The
  UI shows a "Read-only" badge and disables the write buttons. Use it for anyone who should only look.
- **Git push credentials** are the host user's own (credential helper, `~/.git-credentials` or SSH key). In Docker
  they are mounted read-only at runtime, never baked into an image. Pushing still needs the explicit, confirmed click.
- The designer writes only: its state dir, a spec's own output PDF/PNG in the paper repo, TeX environments you edit
  (with a stale-file check), inserted figures, and LaTeX build artefacts.
