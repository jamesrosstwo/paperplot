#!/usr/bin/env bash
# Run the plot-designer container with plain `docker run` (docker-compose.yml is the declarative equivalent).
#
#   PAPER_DIR=~/paper STATE_DIR=~/designer_state DATA_DIRS="/data/evals /data/other" \
#   DESIGNER_TOKEN=$(openssl rand -hex 24) designer/docker/run.sh
#
# Required: PAPER_DIR, STATE_DIR, DESIGNER_TOKEN.
# Optional:
#   DATA_DIRS          space-separated host data dirs, each mounted READ-ONLY at the same absolute path; they also
#                      become DESIGNER_DATA_ROOTS (the only places datasets may be registered from)
#   GIT_CREDENTIALS    host file (e.g. ~/.git-credentials) mounted read-only for HTTPS push
#   SSH_DIR            host ~/.ssh mounted read-only for SSH push
#   IMAGE              default paperplot-designer:latest
#   NAME               container name, default paperplot-designer
#   PUBLISH            host bind address:port, default 127.0.0.1:8766 (loopback only; see README for the tailnet)
#   DESIGNER_GIT_NAME, DESIGNER_GIT_EMAIL, DESIGNER_READONLY, DESIGNER_BASE_PATH   passed through
# Extra arguments replace the container command (e.g. `bash`, or `latexmk ...` for a one-off); the one-off gets a
# terminal only when this script has one (so it also works from cron or `ssh host run.sh ...` without -t).
set -euo pipefail

: "${PAPER_DIR:?set PAPER_DIR to the paper git repo}"
: "${STATE_DIR:?set STATE_DIR to a host dir for designer state}"
: "${DESIGNER_TOKEN:?set DESIGNER_TOKEN (e.g. export DESIGNER_TOKEN=\$(openssl rand -hex 24))}"
IMAGE="${IMAGE:-paperplot-designer:latest}"
NAME="${NAME:-paperplot-designer}"
PUBLISH="${PUBLISH:-127.0.0.1:8766}"

abs() { (cd "$1" && pwd -P); }
[[ -d "$PAPER_DIR" ]] || { echo "PAPER_DIR $PAPER_DIR does not exist" >&2; exit 2; }
mkdir -p "$STATE_DIR"

# mounts and environment shared by the service and one-off commands
common=(-v "$(abs "$PAPER_DIR"):/paper"
        -v "$(abs "$STATE_DIR"):/state"
        -e DESIGNER_TOKEN
        -e DESIGNER_GIT_NAME -e DESIGNER_GIT_EMAIL -e DESIGNER_READONLY -e DESIGNER_BASE_PATH)

read -r -a data_dirs <<< "${DATA_DIRS:-}"
roots=""
for d in "${data_dirs[@]}"; do
    [[ -d "$d" || -f "$d" ]] || { echo "data dir $d does not exist" >&2; exit 2; }
    case "$d" in /*) ;; *) echo "data dir $d must be an absolute path (it is mounted at the same path)" >&2; exit 2;; esac
    case "$d" in *:*) echo "data dir $d must not contain ':'" >&2; exit 2;; esac
    common+=(-v "$d:$d:ro")
    roots="${roots:+$roots:}$d"
done
common+=(-e "DESIGNER_DATA_ROOTS=$roots")
if [[ -n "${GIT_CREDENTIALS:-}" ]]; then
    common+=(-v "$GIT_CREDENTIALS:/run/designer/git-credentials:ro")
fi
if [[ -n "${SSH_DIR:-}" ]]; then
    common+=(-v "$SSH_DIR:/home/designer/.ssh:ro")
fi

if [[ $# -gt 0 ]]; then
    # one-off command: removed afterwards, no port, no restart policy; a TTY only if we have one
    tty=(-i)
    if [[ -t 0 && -t 1 ]]; then tty=(-it); fi
    exec docker run --rm "${tty[@]}" --init "${common[@]}" "$IMAGE" "$@"
fi

docker run --name "$NAME" -d --init --restart unless-stopped -p "$PUBLISH:8766" "${common[@]}" "$IMAGE"
echo "started $NAME on http://$PUBLISH/  (sign in at http://$PUBLISH/login with DESIGNER_TOKEN)"
echo "logs: docker logs -f $NAME    stop: docker rm -f $NAME"
