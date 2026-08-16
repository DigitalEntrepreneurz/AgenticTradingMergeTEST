#!/usr/bin/env bash
# Bring the firm back up after a sandbox restart.
# Only files persist across restarts - installed packages do not.
set -e
cd "$(dirname "$0")"

python -c "import fastapi, uvicorn, yaml, httpx" 2>/dev/null || {
  echo "installing python deps..."
  pip install -q fastapi uvicorn pyyaml httpx youtube-transcript-api
}

pkill -f "uvicorn web.server" 2>/dev/null || true
sleep 1
echo "starting dashboard on http://0.0.0.0:8000"
exec python -m uvicorn web.server:app --host 0.0.0.0 --port 8000 --log-level warning
