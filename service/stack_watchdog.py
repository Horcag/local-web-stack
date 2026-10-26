"""Host-only Compose health watchdog; requires Python and the Docker CLI only."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any

PROJECT = "local-web-stack"
TARGETS = {
    "byedpi": "local-web-byedpi",
    "redis": "local-web-valkey",
    "searxng": "local-web-searxng",
    "crawl4ai": "local-web-crawl4ai",
    "mcp": "local-web-mcp",
}
WINDOW = 900
LIMIT = 3
INITIAL_DELAY = 60
MAX_DELAY = 900
INSPECT_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"status":{{json .State.Status}},"paused":{{json .State.Paused}},'
    '"health":{{with index .State "Health"}}{{json .Status}}{{else}}null{{end}}}'
)

HEALTH_OUTPUT_FORMAT = (
    '{{ $last := "" }}{{with index .State "Health"}}{{range .Log}}'
    "{{ $last = .Output }}{{end}}{{end}}"
    + INSPECT_FORMAT[:-1]
    + ',"output":{{json (printf "%.2048s" $last)}}}'
)


def proxy_failed(container: dict[str, Any] | None) -> bool:
    if (
        container is None
        or container.get("status") != "running"
        or container.get("paused") is not False
        or container.get("health") != "unhealthy"
    ):
        return False
    output = container.get("output")
    if not isinstance(output, str) or len(output) > 2048:
        return False
    try:
        payload = json.loads(output)
    except (ValueError, RecursionError):
        return False
    if not isinstance(payload, dict) or set(payload) != {"failed_checks"}:
        return False
    checks = payload["failed_checks"]
    return (
        isinstance(checks, list)
        and len(checks) <= 3
        and all(isinstance(check, str) and check in {"dns", "proxy", "crawler"} for check in checks)
        and "proxy" in checks
    )


def emit(event: str, service: str | None = None, **fields: Any) -> None:
    print(json.dumps({"event": event, "service": service, **fields}, sort_keys=True), flush=True)


def docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=10, check=False
    )


def state_path() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return root / "local-web-watchdog/state.json"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.stat().st_size > 65536:
        raise ValueError("oversized state")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) - set(TARGETS):
        raise ValueError("invalid state targets")
    for record in data.values():
        if not isinstance(record, dict) or set(record) != {"attempts", "next", "delay"}:
            raise ValueError("invalid state record")
        attempts = record["attempts"]
        numbers = [record["next"], record["delay"]]
        if not isinstance(attempts, list) or len(attempts) > LIMIT:
            raise ValueError("invalid attempts")
        numbers += attempts
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in numbers
        ):
            raise ValueError("invalid timestamp")
        if not INITIAL_DELAY <= record["delay"] <= MAX_DELAY:
            raise ValueError("invalid delay")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Watchdog:
    def __init__(self, path: Path):
        self.path = path
        self.state = load_state(path)
        self.transitions: dict[str, str] = {}

    def transition(self, service: str, status: str) -> None:
        if self.transitions.get(service) != status:
            emit("transition", service, status=status)
            self.transitions[service] = status

    def inspect(self, service: str, name: str) -> dict[str, Any] | None:
        try:
            format_string = HEALTH_OUTPUT_FORMAT if service == "crawl4ai" else INSPECT_FORMAT
            result = docker("inspect", "--type", "container", "--format", format_string, name)
            if result.returncode:
                self.transition(service, "inspect_failed")
                return None
            record = json.loads(result.stdout)
            if (
                not isinstance(record, dict)
                or record.get("name") != f"/{name}"
                or record.get("project") != PROJECT
                or record.get("service") != service
                or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("id", "")))
            ):
                self.transition(service, "ownership_refused")
                return None
            return record
        except (OSError, subprocess.TimeoutExpired, ValueError):
            self.transition(service, "inspect_failed")
            return None

    def poll(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        snapshots = {service: self.inspect(service, name) for service, name in TARGETS.items()}
        proxy_fault = proxy_failed(snapshots.get("crawl4ai"))
        for service, container in snapshots.items():
            if container is None:
                continue
            if service == "crawl4ai" and proxy_fault:
                self.transition(service, "waiting_for_proxy")
                continue
            status, health = container.get("status"), container.get("health")
            if service == "byedpi" and proxy_fault:
                health = "unhealthy"
            record = self.state.setdefault(
                service, {"attempts": [], "next": 0, "delay": INITIAL_DELAY}
            )
            record["attempts"] = [stamp for stamp in record["attempts"] if stamp > now - WINDOW]
            if status != "running" or container.get("paused") is not False:
                self.transition(service, "inactive")
                continue
            if health == "healthy":
                self.transition(service, "healthy")
                record.update(next=0, delay=INITIAL_DELAY)
                continue
            if health != "unhealthy":
                self.transition(service, "awaiting_health")
                continue
            if len(record["attempts"]) >= LIMIT:
                self.transition(service, "rate_limited")
                continue
            if now < record["next"]:
                self.transition(service, "cooldown")
                continue
            # Reserve the attempt durably before invoking Docker, including failed restarts.
            record["attempts"].append(now)
            record["next"] = now + record["delay"]
            record["delay"] = min(MAX_DELAY, record["delay"] * 2)
            save_state(self.path, self.state)
            try:
                result = docker("restart", "--timeout", "5", container["id"])
                outcome = "restarted" if result.returncode == 0 else "restart_failed"
            except (OSError, subprocess.TimeoutExpired):
                outcome = "restart_failed"
            self.transition(service, outcome)
        save_state(self.path, self.state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Inspect each allowlisted target once")
    parser.add_argument("--interval", type=float, default=20)
    arguments = parser.parse_args(argv)
    if not math.isfinite(arguments.interval) or arguments.interval < 1:
        parser.error("--interval must be finite and at least one second")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                emit("already_running")
                return 1
            watchdog = Watchdog(path)
            while not stop.is_set():
                watchdog.poll()
                if arguments.once:
                    break
                stop.wait(arguments.interval)
    except (OSError, ValueError):
        # Do not log exception bodies: CLI output and malformed state may contain secrets.
        emit("state_unavailable")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
