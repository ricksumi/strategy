#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
set -a
[ -f .env ] && . ./.env
set +a

exec python3 bn_stra_high_risk_1.py --config config.json --once --log-level INFO
