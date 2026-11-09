#!/usr/bin/env bash
# Container entrypoint: git identity + credentials from the environment / read-only mounts, then exec the command.
# Nothing here is baked into the image; everything comes from `docker run -e/-v` or docker-compose.yml.
set -euo pipefail

# --- git author for commits made by the designer -----------------------------------------------------------------
# Env vars beat any config file and need no writable HOME.
if [[ -n "${DESIGNER_GIT_NAME:-}" ]]; then
    export GIT_AUTHOR_NAME="$DESIGNER_GIT_NAME" GIT_COMMITTER_NAME="$DESIGNER_GIT_NAME"
fi
if [[ -n "${DESIGNER_GIT_EMAIL:-}" ]]; then
    export GIT_AUTHOR_EMAIL="$DESIGNER_GIT_EMAIL" GIT_COMMITTER_EMAIL="$DESIGNER_GIT_EMAIL"
fi

# --- git credentials (push), mounted read-only -------------------------------------------------------------------
# HTTPS (Overleaf git uses https + a token): mount the host's ~/.git-credentials at /run/designer/git-credentials.
# The helper below only answers `get` (never `store`/`erase`), so git never tries to write the read-only file.
CRED_FILE="${DESIGNER_GIT_CREDENTIALS_FILE:-/run/designer/git-credentials}"
if [[ -f "$CRED_FILE" ]]; then
    export GIT_CONFIG_COUNT=1
    export GIT_CONFIG_KEY_0=credential.helper
    export GIT_CONFIG_VALUE_0="!f() { test \"\$1\" = get && git credential-store --file='$CRED_FILE' get; }; f"
fi
# SSH remotes: mount the host's ~/.ssh read-only at /home/designer/.ssh (same uid, so permissions stay valid).
export GIT_TERMINAL_PROMPT=0          # never hang waiting for a password

# --- refuse to serve on a non-loopback address without a token (the server checks this too) ----------------------
if [[ "$*" == *serve.py* ]]; then
    host="${DESIGNER_HOST:-127.0.0.1}"
    for ((i = 1; i <= $#; i++)); do
        if [[ "${!i}" == "--host" ]]; then j=$((i + 1)); host="${!j:-$host}"; fi
    done
    case "$host" in
        127.*|localhost|::1) ;;
        *)
            if [[ -z "${DESIGNER_TOKEN:-}" ]]; then
                echo "ERROR: the designer binds $host inside the container; set DESIGNER_TOKEN (e.g. \`openssl rand -hex 24\`)." >&2
                exit 64
            fi ;;
    esac
    if [[ ! -d "${DESIGNER_PAPER_ROOT:-/paper}" ]] || [[ -z "$(ls -A "${DESIGNER_PAPER_ROOT:-/paper}" 2>/dev/null)" ]]; then
        echo "WARNING: ${DESIGNER_PAPER_ROOT:-/paper} is empty: mount the paper repo there (-v /path/to/paper:/paper)." >&2
    fi
fi

exec "$@"
