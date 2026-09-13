#!/usr/bin/env bash
# Developer bootstrap: virtualenv, dev installs, hooks, and the dev stack.
#
#   ./scripts/bootstrap.sh          # install + start docker services
#   ./scripts/bootstrap.sh --no-up  # install only

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
START_STACK=1
[[ "${1:-}" == "--no-up" ]] && START_STACK=0

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || PY=python
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || {
  echo "Python 3.12+ required (found: $("$PY" --version))" >&2
  exit 1
}

echo "==> creating virtualenv at .venv"
[[ -d .venv ]] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate

echo "==> installing python packages"
pip install --quiet --upgrade pip
pip install --quiet -e "backend[dev]" -e "edge-agent[dev]" -e "ai-service[dev]" pre-commit

echo "==> installing git hooks"
[[ -d .git ]] || git init --quiet
pre-commit install

echo "==> preparing .env"
[[ -f .env ]] || { cp .env.example .env; echo "    wrote .env from .env.example"; }

echo "==> checking optional tooling"
command -v ffprobe >/dev/null || echo "    WARNING: ffprobe missing - RTSP/playback probes stay UNKNOWN"
command -v flutter >/dev/null || echo "    note: flutter not installed (mobile builds unavailable)"

echo "==> running tests"
python -m pytest edge-agent/tests backend/tests ai-service/tests -q

if [[ "$START_STACK" == "1" ]]; then
  echo "==> starting docker stack"
  docker compose -f infra/docker-compose.yml up -d --build
  docker compose -f infra/docker-compose.yml ps
fi

cat <<EOF

Ready.

  probe a device : python scripts/dahua_probe.py --host <XVR_IP> --username <user> --ask-password
  api health     : curl http://localhost:8000/health
  run tests      : make test

Repo: $ROOT
EOF
