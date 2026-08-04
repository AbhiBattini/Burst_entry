#!/usr/bin/env bash
# One-shot setup for Strategy A on a fresh AWS Tokyo (ap-northeast-1) Ubuntu box. Idempotent.
#   ./setup.sh              # venv + deps + .env + smoke test
#   ./setup.sh --service    # also install + start the systemd service (runs on boot, auto-restart)
#   ./setup.sh --live       # also install the live-execution deps (hyperliquid sdk)
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"; PYBIN="${PYBIN:-python3}"

echo "[1/5] python venv"
command -v "$PYBIN" >/dev/null || { echo "install python3.11+ first: sudo apt-get update && sudo apt-get install -y python3-venv"; exit 1; }
[ -d .venv ] || "$PYBIN" -m venv .venv
. .venv/bin/activate
pip install --upgrade pip >/dev/null

echo "[2/5] dependencies"
pip install -r requirements.txt
if [[ "${*:-}" == *--live* ]]; then pip install hyperliquid-python-sdk eth-account; fi

echo "[3/5] config / secrets"
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "  created .env (edit it before mode: live)"; }
[ -f seed.json ] || echo "  WARNING: seed.json missing -> run: .venv/bin/python tools/build_seed.py (needs archive data)"

echo "[4/5] smoke test (90s TEST-mode paper — verifies the feed + signal path)"
timeout 100 .venv/bin/python -m src.run --duration 90 --test || echo "  (smoke test ended)"

echo "[5/5] systemd"
if [[ "${*:-}" == *--service* ]]; then
  sed "s#__WORKDIR__#$HERE#g; s#__USER__#$(whoami)#g" systemd/strat-a.service | sudo tee /etc/systemd/system/strat-a.service >/dev/null
  sudo systemctl daemon-reload && sudo systemctl enable --now strat-a.service
  echo "  service installed. logs: journalctl -u strat-a -f"
else
  echo "  (skipped — run with --service to install. Manual start: .venv/bin/python -m src.run)"
fi
echo "DONE. Review config.yaml (mode stays 'paper' until you deliberately go live)."
