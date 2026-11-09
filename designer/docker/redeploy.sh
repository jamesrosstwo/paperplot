#!/usr/bin/env bash
# Test, build and (re)start the plot-designer service from this checkout: the one command after a code change.
#
#   designer/docker/redeploy.sh                # tests, build, replace the container, wait until healthy
#   designer/docker/redeploy.sh --skip-tests   # build and replace only
#
# Deployment settings live outside the repo in $DESIGNER_ENV_FILE (default ~/.config/plot-designer.env), a shell
# file of KEY=value lines read by run.sh: PAPER_DIR, STATE_DIR, PUBLISH, and optionally DATA_DIRS, GIT_CREDENTIALS,
# DESIGNER_GIT_NAME, DESIGNER_GIT_EMAIL, NAME. The token is read from $DESIGNER_TOKEN_FILE (default
# ~/.config/plot-designer.token). A failed test run or build leaves the running container untouched.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${DESIGNER_ENV_FILE:-$HOME/.config/plot-designer.env}"
TOKEN_FILE="${DESIGNER_TOKEN_FILE:-$HOME/.config/plot-designer.token}"

skip_tests=0
for a in "$@"; do
    case "$a" in
        --skip-tests) skip_tests=1 ;;
        *) echo "unknown argument $a (use --skip-tests)" >&2; exit 2 ;;
    esac
done

[[ -f "$ENV_FILE" ]] || { echo "no settings file $ENV_FILE (see designer/docker/README.md, step 2)" >&2; exit 2; }
[[ -s "$TOKEN_FILE" ]] || { echo "no token in $TOKEN_FILE (see designer/docker/README.md, step 2)" >&2; exit 2; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
NAME="${NAME:-paperplot-designer}"

if [[ -n "$(git -C "$HERE" status --porcelain 2>/dev/null)" ]]; then
    echo "note: deploying uncommitted changes ($(git -C "$HERE" status --porcelain | wc -l) files)" >&2
fi

"$HERE/build.sh"
if [[ $skip_tests == 0 ]]; then
    "$HERE/test.sh" -q
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true
DESIGNER_TOKEN="$(cat "$TOKEN_FILE")" "$HERE/run.sh"

for _ in $(seq 1 40); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo missing)"
    case "$status" in
        healthy) echo "$NAME is healthy ($(git -C "$HERE" log -1 --format='%h %s' 2>/dev/null || echo 'no git'))"; exit 0 ;;
        unhealthy|missing) break ;;
    esac
    sleep 3
done
echo "$NAME did not become healthy (status: $status); see: docker logs $NAME" >&2
exit 1
