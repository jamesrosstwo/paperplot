# syntax=docker/dockerfile:1
#
# Plot designer as a service image (designer/CONTRACT.md section 8).
#
# Build context = the paperplot folder (it holds both designer/ and the paperplot/ library):
#     docker build -f designer/docker/Dockerfile --build-arg UID=$(id -u) --build-arg GID=$(id -g) \
#                  -t paperplot-designer:latest .
# or simply designer/docker/build.sh. See designer/docker/README.md.
#
# Contains: Python 3.12 + flask/matplotlib/pandas/pyyaml/scipy/waitress, Debian TeX Live (enough for the RSS paper:
# latexmk, pdflatex, bibtex, synctex), Times New Roman (ttf-mscorefonts-installer) and Roboto Slab (bundled in
# paperplot/fonts), git. It contains NO paper, NO data and NO credentials: those are mounted at runtime.

FROM python:3.12-slim-bookworm

ARG UID=1000
ARG GID=1000
ARG DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------------------------------------------
# System packages. contrib is needed for ttf-mscorefonts-installer; its EULA is accepted via debconf.
# The installer downloads the fonts from SourceForge in its postinst and does NOT fail apt when that download
# fails, so the explicit test below turns a failed download into a failed build.
# ---------------------------------------------------------------------------------------------------------------
RUN set -eux; \
    sed -i 's/^Components: main$/Components: main contrib/' /etc/apt/sources.list.d/debian.sources; \
    grep -q '^Components: main contrib' /etc/apt/sources.list.d/debian.sources; \
    echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        git openssh-client ca-certificates tini fontconfig \
        latexmk \
        texlive-latex-base texlive-latex-recommended texlive-latex-extra \
        texlive-fonts-recommended texlive-bibtex-extra \
        ttf-mscorefonts-installer; \
    if ! test -s /usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf; then \
        echo "ERROR: ttf-mscorefonts-installer did not install Times New Roman" >&2; \
        echo "       (the SourceForge download in its postinst probably failed; retry the build)" >&2; \
        exit 1; \
    fi; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /usr/share/doc/texlive* /usr/share/doc/texmf

# Roboto Slab: paperplot registers it by path from paperplot/fonts; also install it system-wide so fc-list and
# matplotlib's font cache see it without registration.
COPY paperplot/fonts/ /usr/local/share/fonts/paperplot/
RUN set -eux; \
    fc-cache -f; \
    fc-list : family | grep -qi 'Times New Roman'; \
    fc-list : family | grep -qi 'Roboto Slab'

# ---------------------------------------------------------------------------------------------------------------
# Python dependencies. Pinned to the versions of James's Windows Anaconda env so renders match; if
# designer/requirements.txt exists it is installed on top of these further down.
# ---------------------------------------------------------------------------------------------------------------
COPY designer/docker/requirements-pinned.txt /tmp/requirements-pinned.txt
RUN pip install --no-cache-dir -r /tmp/requirements-pinned.txt

# ---------------------------------------------------------------------------------------------------------------
# Non-root user with the host user's uid/gid so files written into the mounted paper repo keep the host owner.
# ---------------------------------------------------------------------------------------------------------------
RUN set -eux; \
    if ! getent group "${GID}" >/dev/null; then groupadd -g "${GID}" designer; fi; \
    useradd -m -u "${UID}" -g "${GID}" -s /bin/bash designer; \
    mkdir -p /paper /state /opt/mplconfig; \
    chown "${UID}:${GID}" /paper /state /opt/mplconfig; \
    chmod 0777 /opt/mplconfig; \
    git config --system --add safe.directory '*'

ENV PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/opt/mplconfig \
    DESIGNER_PAPER_ROOT=/paper \
    DESIGNER_STATE_DIR=/state \
    DESIGNER_HOST=0.0.0.0 \
    DESIGNER_PORT=8766

WORKDIR /app
COPY paperplot/ /app/paperplot/
COPY designer/ /app/designer/

RUN set -eux; \
    if [ -f /app/designer/requirements.txt ]; then pip install --no-cache-dir -r /app/designer/requirements.txt; \
    else echo "designer/requirements.txt not present; using designer/docker/requirements-pinned.txt only"; fi; \
    python -c "import waitress, flask, matplotlib, pandas, yaml, scipy"; \
    cd /app && MPLCONFIGDIR=/tmp/mplroot python -c "import paperplot" && rm -rf /tmp/mplroot; \
    sed -i 's/\r$//' /app/designer/docker/entrypoint.sh; \
    chmod 0755 /app/designer/docker/entrypoint.sh

USER ${UID}:${GID}

# Build-time font check: fails the build if matplotlib cannot find Times New Roman or Roboto Slab without falling
# back (also builds matplotlib's font cache in MPLCONFIGDIR as the runtime user).
RUN python /app/designer/docker/check_fonts.py

EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/designer/docker/healthcheck.py"]

# tini -s works both as PID 1 and under `docker run --init` / compose `init: true` (as a subreaper).
ENTRYPOINT ["/usr/bin/tini", "-s", "--", "/app/designer/docker/entrypoint.sh"]
CMD ["python", "/app/designer/serve.py", "--prod", "--host", "0.0.0.0", "--port", "8766"]
