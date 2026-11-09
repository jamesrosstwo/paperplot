#!/usr/bin/env bash
# Run the designer test suite inside the designer image (the same Python, TeX Live and fonts the service uses).
#
#   designer/docker/test.sh                               # whole suite
#   designer/docker/test.sh designer/tests/test_spec.py   # extra arguments go to pytest
#
# The repo is mounted read-only and copied into the container. PAPER_SOURCE (default ~/paper-rss) is mounted
# read-only and cloned inside the container as the tests' disposable paper, so the live clone is never written to.
# The LaTeX tests assert figures, labels and line numbers of one paper revision, so the clone is checked out at
# PAPER_REV (default: bd9b53a, "Split paper.tex into one file per section under sections/", 2026-11-05).
# Env: IMAGE (default paperplot-designer:latest), PAPER_SOURCE (set it to an empty string to run without a paper;
# tests that need one then skip), PAPER_REV.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${IMAGE:-paperplot-designer:latest}"
PAPER_SOURCE="${PAPER_SOURCE-$HOME/paper-rss}"
PAPER_REV="${PAPER_REV-bd9b53a3ba025603ce813b262b3797072ec19e25}"

mounts=(-v "$REPO:/src:ro")
if [[ -n "$PAPER_SOURCE" ]]; then
    [[ -d "$PAPER_SOURCE/.git" ]] || { echo "PAPER_SOURCE $PAPER_SOURCE is not a git clone" >&2; exit 2; }
    mounts+=(-v "$(cd "$PAPER_SOURCE" && pwd -P):/paper-source:ro")
fi

tty=()
if [[ -t 0 && -t 1 ]]; then tty=(-t); fi

# shellcheck disable=SC2016  # expanded inside the container
exec docker run --rm "${tty[@]}" --init "${mounts[@]}" -e PAPER_REV="${PAPER_REV:-}" --entrypoint bash "$IMAGE" -c '
set -euo pipefail
work=$(mktemp -d)
cp -r /src/. "$work/repo"
cd "$work/repo"
pip install -q --user --disable-pip-version-check -r designer/docker/requirements-test.txt
if [[ -d /paper-source ]]; then
    git -c safe.directory="*" clone -q /paper-source "$work/paper"
    if [[ -n "${PAPER_REV:-}" ]]; then git -C "$work/paper" checkout -q "$PAPER_REV"; fi
    export DESIGNER_TEST_PAPER="$work/paper"
fi
exec python -m pytest -p no:cacheprovider "$@"
' test.sh "${@:-designer/tests}"
