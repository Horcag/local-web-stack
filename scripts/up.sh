#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
docker compose up -d --build --wait --wait-timeout 180 "$@"
bash scripts/install-watchdog.sh
printf '%s\n' 'local-web is healthy; automatic recovery is enabled.'
