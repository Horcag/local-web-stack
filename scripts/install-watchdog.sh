#!/usr/bin/env bash
# Install the host watchdog without granting containers access to the Docker socket.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
install -d -m 700 "$HOME/.local/libexec" "$HOME/.config/systemd/user"
install -m 700 "$root/service/stack_watchdog.py" "$HOME/.local/libexec/local-web-watchdog.py"
install -m 600 "$root/scripts/local-web-watchdog.service" "$HOME/.config/systemd/user/local-web-watchdog.service"
systemctl --user daemon-reload
systemctl --user enable --now local-web-watchdog.service
systemctl --user restart local-web-watchdog.service
systemctl --user is-active --quiet local-web-watchdog.service
