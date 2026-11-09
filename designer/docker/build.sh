#!/usr/bin/env bash
# Build the plot-designer image. Context = the paperplot folder (two levels up from this script).
#
#   designer/docker/build.sh                       # paperplot-designer:latest, uid/gid of the current user
#   IMAGE=paperplot-designer:test designer/docker/build.sh --no-cache
#
# Env: IMAGE (default paperplot-designer:latest), DESIGNER_UID / DESIGNER_GID (default: id -u / id -g).
# Extra arguments go to `docker build`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT="$(cd "$HERE/../.." && pwd)"
IMAGE="${IMAGE:-paperplot-designer:latest}"
UID_ARG="${DESIGNER_UID:-$(id -u)}"
GID_ARG="${DESIGNER_GID:-$(id -g)}"

if [[ "$UID_ARG" == 0 ]]; then
    echo "refusing to build with UID=0; set DESIGNER_UID to a normal user's uid" >&2
    exit 2
fi

echo "building $IMAGE from $CONTEXT (UID=$UID_ARG GID=$GID_ARG)"
start=$(date +%s)
docker build \
    -f "$HERE/Dockerfile" \
    --build-arg "UID=$UID_ARG" --build-arg "GID=$GID_ARG" \
    -t "$IMAGE" \
    "$@" \
    "$CONTEXT"
echo "built $IMAGE in $(( $(date +%s) - start )) s"
docker image ls "$IMAGE"
