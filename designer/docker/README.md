# Plot designer in Docker

One image (`paperplot-designer`) holding the designer, the `paperplot` library, Python 3.12, a Debian TeX Live that
compiles the RSS paper (latexmk, pdflatex, bibtex, synctex), Times New Roman and Roboto Slab, and git. The paper repo,
the designer state, data and git credentials are **mounted at runtime**; none of them is in the image.

## Deploy on borabora (step by step)

Everything below runs as `james` (uid/gid 1002) on borabora, except steps 1 and 6, which run in Git Bash on Windows.
Port 8766 is already taken on borabora, so the designer is published on **127.0.0.1:8797** (loopback only); pick any
other free port if that one is in use (`ss -ltn`).

**1. Copy the code** (Windows Git Bash; `paperplot` is not a git repo, so it is copied, not cloned):

```bash
tar -C /c/Users/James/Desktop/paperplot -cf - --exclude=__pycache__ --exclude=.pytest_cache --exclude=.cache \
    --exclude=designer/data --exclude=designer/specs --exclude=designer/tests/out \
    .dockerignore designer paperplot | ssh borabora 'mkdir -p ~/paperplot && tar -xf - -C ~/paperplot'
```

`designer/specs` and `designer/data` are the Windows designer's own state; they stay on Windows.

**2. Paper clone, state dir, data dir, token** (borabora):

```bash
git clone https://git.overleaf.com/<project-id> ~/paper-rss     # its own clone: the container writes into it
mkdir -p ~/designer_state                                       # specs, data registry, cache (survives upgrades)
umask 077; mkdir -p ~/.config && openssl rand -hex 24 > ~/.config/plot-designer.token
```

For Push, put the Overleaf git token in `~/.git-credentials` (`https://git:<overleaf-token>@git.overleaf.com`, mode
600) rather than in the clone's remote URL. Data dirs are any directories holding tables you want to plot (e.g. eval
TSVs); they are mounted read-only and are the only places datasets can be registered from.

**3. Build** (cached rebuilds take ~20 s; the first one ~2 min):

```bash
cd ~/paperplot && designer/docker/build.sh          # -> paperplot-designer:latest, for $(id -u):$(id -g)
```

**4. Run:**

```bash
cd ~/paperplot
DESIGNER_TOKEN=$(cat ~/.config/plot-designer.token) \
PAPER_DIR=~/paper-rss STATE_DIR=~/designer_state DATA_DIRS="/path/to/data1 /path/to/data2" \
GIT_CREDENTIALS=~/.git-credentials DESIGNER_GIT_NAME="James Ross" DESIGNER_GIT_EMAIL=<email> \
PUBLISH=127.0.0.1:8797 designer/docker/run.sh
```

`run.sh` starts the container `paperplot-designer` with `--restart unless-stopped`, mounts each data dir read-only at
the same absolute path and sets `DESIGNER_DATA_ROOTS` to them. Drop `GIT_CREDENTIALS` to run without push access.

**5. Check it:**

```bash
docker ps --filter name=paperplot-designer          # STATUS shows (healthy) after a few seconds
curl -s 127.0.0.1:8797/healthz                      # {"ok":true,...,"paper_root_exists":true,"latex_found":true}
docker logs -f paperplot-designer
```

**6. Use it from Windows:** open a tunnel and sign in once.

```bash
ssh -N -L 8797:127.0.0.1:8797 borabora              # leave running
```

Open <http://127.0.0.1:8797/> in a browser: it sends you to the sign-in form; paste the token from
`~/.config/plot-designer.token`. The browser keeps an HttpOnly cookie for 30 days. Scripts use
`Authorization: Bearer <token>`. To reach it over the tailnet without a tunnel, see "Exposing it over the tailnet".

**Upgrade** (after editing the code on Windows): repeat step 1, then

```bash
cd ~/paperplot && designer/docker/build.sh && docker rm -f paperplot-designer && <the run.sh command from step 4>
```

**Stop:** `docker rm -f paperplot-designer` (the state, paper clone and data are untouched).

**A read-only instance for others** (look, preview, recompile; no edits, no git): run a second container with its own
name, port and state dir, e.g. `NAME=paperplot-designer-ro PUBLISH=127.0.0.1:8798 STATE_DIR=~/designer_state_ro
DESIGNER_READONLY=1 DESIGNER_TOKEN=<another token>` plus the same `PAPER_DIR`, and no `GIT_CREDENTIALS`.

Things to know:

- One container per paper clone: compiles and renders are serialized inside one process only.
- Datasets registered in the Windows designer (`C:\...` paths) do not exist on borabora; register them again from the
  data drawer by their borabora path (inside one of `DATA_DIRS`).
- Figures and TeX edits made in the container land in `~/paper-rss`; commit and push them from the designer (or with
  git on borabora). Pull in your Windows clone as usual.

## Files here

| File | What |
|---|---|
| `Dockerfile` | the image (build context = the `paperplot` folder) |
| `docker-compose.yml` | the service, with env-var placeholders for every mount |
| `build.sh` / `run.sh` | Linux: build with your uid/gid; run with `docker run` |
| `build.ps1` / `run.ps1` | Windows (Docker Desktop) equivalents |
| `entrypoint.sh` | git author + read-only credentials from env/mounts, token check, then exec |
| `check_fonts.py` | fails unless matplotlib finds Times New Roman and Roboto Slab with no fallback |
| `healthcheck.py` | Docker HEALTHCHECK on `/healthz` |
| `requirements-pinned.txt` | Python deps pinned to the Windows Anaconda versions; `designer/requirements.txt` goes on top |
| `../../.dockerignore` | allow-list: only `paperplot/` and `designer/` enter the build context (no figures/out, data, caches, live specs) |

## Build

```bash
cd ~/paperplot                      # the folder holding designer/ and paperplot/
designer/docker/build.sh            # -> paperplot-designer:latest, built for $(id -u):$(id -g)
IMAGE=paperplot-designer:test DESIGNER_UID=1002 DESIGNER_GID=1002 designer/docker/build.sh
```

The container runs as a non-root user whose uid/gid are the `UID`/`GID` build args, so build it **for the user that
owns the paper repo** (on borabora `james` is 1002/1002). Files the designer writes into `/paper` (figure PDF/PNG,
edited TeX, `paper.pdf`, `paper.synctex.gz`, aux files) then belong to that user on the host.

Build with **one** tool per host: `build.sh` (recommended), or compose's `--build`. The two give the same layers but
different labels, so alternating between them retags the image each time and leaves a dangling copy behind.

The build fails loudly if

- the Times New Roman download from SourceForge (done by `ttf-mscorefonts-installer`) fails: just rebuild;
- matplotlib cannot find `Times New Roman` or `Roboto Slab` (regular and bold) with `fallback_to_default=False`.

The image is 1.11 GB (Debian TeX Live layer ~0.6 GB). In it the RSS paper compiles to the same 6 pages as on Windows
with MiKTeX (same 3 bibtex errors from the paper itself, no LaTeX errors), with `paper.synctex.gz` and files owned by
the host user, and the seed figure renders at the Windows PNG size in Roboto Slab (0.1% of pixels differ, all glyph
anti-aliasing).

## Run (reference)

Compose (declarative; reads env vars or a `.env` file next to `docker-compose.yml`). Build first with `build.sh`:

```bash
cd ~/paperplot && designer/docker/build.sh
cd designer/docker
export PAPER_DIR=$HOME/paper-rss STATE_DIR=$HOME/designer_state DATA_DIR=/data/evals
export DESIGNER_TOKEN=$(cat ~/.config/plot-designer.token) DESIGNER_PUBLISH_PORT=8797
export DESIGNER_GIT_NAME="James Ross" DESIGNER_GIT_EMAIL=...
docker compose up -d
docker compose logs -f
```

Compose mounts one data dir (`DATA_DIR`, also used as `DESIGNER_DATA_ROOTS`); `run.sh` takes any number.

Plain `docker run` via `run.sh` (variables documented at the top of the script): see step 4 above.

One-off commands in the same image (a manual paper build, a shell); `run.sh` adds a terminal only when it has one, so
this also works over `ssh host ...` without `-t`:

```bash
PAPER_DIR=~/paper-rss STATE_DIR=~/designer_state DESIGNER_TOKEN=x designer/docker/run.sh \
    latexmk -norc -pdf -synctex=1 -interaction=nonstopmode -f -cd /paper/paper.tex
docker run --rm paperplot-designer:latest python /app/designer/docker/check_fonts.py
```

### Environment

| Variable | Default in image | Meaning |
|---|---|---|
| `DESIGNER_TOKEN` | — (required) | access token; the container binds `0.0.0.0`, so the server and the entrypoint refuse to start without it |
| `DESIGNER_DATA_ROOTS` | — | `:`-separated dirs datasets may be registered from (`run.sh`/compose set it to the data mounts). Unset with a token = no dataset can be registered |
| `DESIGNER_PAPER_ROOT` | `/paper` | paper repo inside the container (mount it here) |
| `DESIGNER_STATE_DIR` | `/state` | specs, data registry, cache (mount a host dir so they survive upgrades) |
| `DESIGNER_HOST` / `DESIGNER_PORT` | `0.0.0.0` / `8766` | bind inside the container (publish with `-p`, see below) |
| `DESIGNER_BASE_PATH` | empty | URL prefix behind a reverse-proxy subpath, e.g. `/designer` |
| `DESIGNER_READONLY` | empty | `1` = no writes to specs/paper/registry/git; preview, recompile and context preview of the stored TeX still work |
| `DESIGNER_GIT_NAME` / `DESIGNER_GIT_EMAIL` | empty | author/committer of commits made from the designer |
| `DESIGNER_GIT_CREDENTIALS_FILE` | `/run/designer/git-credentials` | where the entrypoint looks for a mounted credentials file |
| `DESIGNER_DEBUG` | empty | `1` = Python tracebacks in 500 responses (otherwise only in `docker logs`) |
| `MPLCONFIGDIR` | `/opt/mplconfig` | matplotlib cache (font list built at image build time) |
| `PYTHONUTF8` | `1` | |

Compose-only: `PAPER_DIR`, `STATE_DIR`, `DATA_DIR`, `DESIGNER_PUBLISH_PORT`, `DESIGNER_UID`, `DESIGNER_GID`, `DESIGNER_IMAGE`, `DESIGNER_CONTAINER_NAME`.

### Mounting data: same absolute path, read-only

The data store records **absolute paths** and never copies data. Mount every data directory read-only **at the same
absolute path it has on the host** (`-v /data/evals:/data/evals:ro`; compose: `${DATA_DIR}:${DATA_DIR}:ro`), and
register datasets by that path. Then the registry, the symlinks in `STATE_DIR/data/links` and the paths in specs
resolve identically inside and outside the container. A path that is not mounted is rejected with "mount <dir> into
the container"; a mounted path outside `DESIGNER_DATA_ROOTS` (e.g. `/paper/...`) is rejected as outside the data
directories; credentials and system places (`/run`, `/etc`, `/proc`, `~/.ssh`, the state dir) are always refused.
Symlinks inside a mounted dir must point inside a mounted data dir too.

On **Windows** (Docker Desktop) a `C:\...` path cannot exist in a Linux container: `run.ps1 -Data @{ "C:\data\evals" =
"/data/evals" }` mounts it at a Linux path (and sets `DESIGNER_DATA_ROOTS` to the Linux paths), and datasets must be
registered from the container under `/data/evals/...`. NTFS junctions and `.lnk` shortcuts do not cross into the
container: mount the target directory (`run.ps1` does this for a junction). Use a separate `STATE_DIR` for the
container. `run.ps1` publishes on `127.0.0.1:8776` by default (8766 is the Windows-native designer).

### Git credentials (Push)

Commit needs only `DESIGNER_GIT_NAME`/`DESIGNER_GIT_EMAIL`. Push uses the host's credentials, **mounted read-only at
run time, never baked into the image**:

- HTTPS (Overleaf git = https + token): `-v ~/.git-credentials:/run/designer/git-credentials:ro`
  (`GIT_CREDENTIALS=~/.git-credentials` for `run.sh`, the commented line in compose). The entrypoint installs a
  credential helper that only *reads* that file.
- SSH remotes: `-v ~/.ssh:/home/designer/.ssh:ro` (`SSH_DIR=~/.ssh`). The uid matches, so ssh accepts the key
  permissions.

The data store refuses to register either location, and TeX runs with `openin_any=p`, so neither can be read back
through the UI. git output shown in the UI has `user:token@` in URLs replaced by `***@`. `git` never prompts
(`GIT_TERMINAL_PROMPT=0`): with no credentials Push fails with an error in the UI. The designer never pushes on its
own, never force-pushes, and pulls `--ff-only`. `safe.directory` is set system-wide, so the mounted repo is accepted
even if its owner differs from the container user.

### Exposing it over the tailnet (always with a token)

By default the port is published on `127.0.0.1` only. Options, best first:

1. Keep loopback and let tailscale proxy it with HTTPS: `tailscale serve --bg --https=443 http://127.0.0.1:8797`
   (or a subpath: `tailscale serve --bg --set-path /designer http://127.0.0.1:8797` together with
   `DESIGNER_BASE_PATH=/designer`).
2. Publish on the machine's tailscale IP only: compose `- "100.x.y.z:8797:8766"`, or `PUBLISH=100.x.y.z:8797 run.sh`.

Behind any reverse proxy that rewrites `Host` (nginx's default, `tailscale serve`), the proxy must send
`X-Forwarded-Host` with the port the browser used (nginx: `$http_host`, not `$host`) and `X-Forwarded-Proto` for
https: the server compares the browser's `Origin` with them before accepting a write. waitress drops these headers by
default; `serve.py --prod` (the image's command) turns that off. Sign in through the `/login` form rather than
`?token=`, so the token never appears in a proxy's access log.

Never publish on `0.0.0.0` of a machine with a public interface, and never run without `DESIGNER_TOKEN`. For people
who should only look, run a read-only instance (see the deploy steps).

## Troubleshooting

- **Fonts.** `docker run --rm paperplot-designer:latest python /app/designer/docker/check_fonts.py` prints which file
  each family resolves to; `docker run --rm paperplot-designer:latest fc-list | grep -i "times new roman\|roboto slab"`.
  If Times New Roman is missing the SourceForge download failed during the build: rebuild with `--no-cache`. Roboto
  Slab comes from `paperplot/fonts/*.ttf` (copied into `/usr/local/share/fonts/paperplot` and registered by path).
  The renderer refuses to fall back to DejaVu silently.
- **"no data directories are configured"** when adding a dataset: start the container with `DATA_DIRS` (run.sh) or
  `DATA_DIR` (compose), which set `DESIGNER_DATA_ROOTS`.
- **Files in the paper repo owned by the wrong user.** The image was built for another uid: rebuild with
  `DESIGNER_UID=$(id -u) DESIGNER_GID=$(id -g)`.
- **"dubious ownership" from git.** Shouldn't happen (`safe.directory '*'` is set system-wide).
- **LaTeX package missing.** The image uses Debian's `texlive-latex-base/-recommended/-extra`, `texlive-fonts-recommended`
  and `texlive-bibtex-extra` (TeX Live 2022). If the paper starts using a package from another collection (e.g.
  `texlive-science` for `algorithm2e`/`siunitx`), add it to the `apt-get install` line and rebuild. The designer runs
  latexmk with `-norc` and `\write18` off: a paper that needs `--shell-escape` (minted, ...) or its own `latexmkrc`
  will not build here.
- **Context preview says busy (429).** One context preview runs at a time with one waiting; wait a moment.
- **Container restarts in a loop.** `docker logs paperplot-designer`; most often `DESIGNER_TOKEN` is unset or `/paper`
  is not mounted.
- **Healthcheck** `docker inspect --format '{{json .State.Health}}' paperplot-designer`.
- **Disk.** Old tags: `docker image rm paperplot-designer:<tag>`; never `docker system prune` on a shared machine.
