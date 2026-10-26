"""Offline checks of host restart authority, persistence, and bounded failure handling."""

import json
from pathlib import Path
import subprocess

import pytest

from service import stack_watchdog as watchdog


class FakeDocker:
    def __init__(self):
        self.containers = {
            name: {
                "id": str(index) * 64,
                "name": f"/{name}",
                "project": watchdog.PROJECT,
                "service": service,
                "status": "running",
                "paused": False,
                "health": "healthy",
            }
            for index, (service, name) in enumerate(watchdog.TARGETS.items(), 1)
        }
        self.calls = []
        self.inspect_failure = None
        self.restart_failure = None

    def __call__(self, *arguments):
        self.calls.append(arguments)
        failure = self.inspect_failure if arguments[0] == "inspect" else self.restart_failure
        if isinstance(failure, Exception):
            raise failure
        if failure:
            return subprocess.CompletedProcess(arguments, 1, "", "sensitive failure details")
        if arguments[0] == "inspect":
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps(self.containers[arguments[-1]]), ""
            )
        return subprocess.CompletedProcess(arguments, 0, arguments[-1], "")

    @property
    def restarts(self):
        return [call for call in self.calls if call[0] == "restart"]


@pytest.fixture
def fake(monkeypatch):
    docker = FakeDocker()
    monkeypatch.setattr(watchdog, "docker", docker)
    return docker


def unhealthy(fake, service="crawl4ai"):
    container = fake.containers[watchdog.TARGETS[service]]
    container["health"] = "unhealthy"
    return container


def test_unhealthy_restarts_only_verified_id_with_durable_attempt(tmp_path, fake):
    container = unhealthy(fake)
    path = tmp_path / "state.json"
    monitor = watchdog.Watchdog(path)
    monitor.poll(1000)
    assert fake.restarts == [("restart", "--timeout", "5", container["id"])]
    assert watchdog.load_state(path)["crawl4ai"] == {
        "attempts": [1000],
        "next": 1060,
        "delay": 120,
    }
    assert all(call[-1] in watchdog.TARGETS.values() for call in fake.calls if call[0] == "inspect")


def test_healthy_is_noop_and_resets_backoff_without_erasing_rate_window(tmp_path, fake):
    container = unhealthy(fake)
    monitor = watchdog.Watchdog(tmp_path / "state.json")
    monitor.poll(1000)
    container["health"] = "healthy"
    monitor.poll(1010)
    assert len(fake.restarts) == 1
    assert monitor.state["crawl4ai"] == {"attempts": [1000], "next": 0, "delay": 60}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project", "foreign"),
        ("service", "foreign"),
        ("project", None),
        ("name", "/other-container"),
        ("id", "invalid-id"),
    ],
)
def test_foreign_or_unknown_ownership_refused(tmp_path, fake, field, value, capsys):
    container = unhealthy(fake)
    container[field] = value
    watchdog.Watchdog(tmp_path / "state.json").poll(1000)
    assert not fake.restarts
    assert '"status": "ownership_refused"' in capsys.readouterr().out


@pytest.mark.parametrize("status", ["exited", "paused", "restarting", "removing", "created"])
def test_nonrunning_containers_untouched(tmp_path, fake, status):
    unhealthy(fake)["status"] = status
    watchdog.Watchdog(tmp_path / "state.json").poll(1000)
    assert not fake.restarts


def test_paused_flag_blocks_restart(tmp_path, fake):
    unhealthy(fake)["paused"] = True
    watchdog.Watchdog(tmp_path / "state.json").poll(1000)
    assert not fake.restarts


@pytest.mark.parametrize("health", [None, "starting", "unknown"])
def test_missing_or_starting_health_untouched(tmp_path, fake, health):
    unhealthy(fake)["health"] = health
    watchdog.Watchdog(tmp_path / "state.json").poll(1000)
    assert not fake.restarts


def test_backoff_and_rate_limit_survive_watcher_restart(tmp_path, fake):
    unhealthy(fake)
    path = tmp_path / "state.json"
    monitor = watchdog.Watchdog(path)
    for now in (1000, 1059, 1060, 1179, 1180, 1420, 1899):
        monitor.poll(now)
    assert len(fake.restarts) == 3
    restarted_monitor = watchdog.Watchdog(path)
    restarted_monitor.poll(1899)
    assert len(fake.restarts) == 3
    restarted_monitor.poll(1900)
    assert len(fake.restarts) == 4
    assert restarted_monitor.state["crawl4ai"]["delay"] == 900


def test_healthy_recovery_keeps_restart_budget(tmp_path, fake):
    container = unhealthy(fake)
    monitor = watchdog.Watchdog(tmp_path / "state.json")
    for now in (1000, 1010, 1020):
        container["health"] = "unhealthy"
        monitor.poll(now)
        container["health"] = "healthy"
        monitor.poll(now + 1)
    container["health"] = "unhealthy"
    monitor.poll(1030)
    assert len(fake.restarts) == 3


@pytest.mark.parametrize(
    "failure", [True, OSError("private data"), subprocess.TimeoutExpired("docker", 10)]
)
def test_inspect_failure_does_not_restart_or_log_output(tmp_path, fake, failure, capsys):
    unhealthy(fake)
    fake.inspect_failure = failure
    monitor = watchdog.Watchdog(tmp_path / "state.json")
    monitor.poll(1000)
    monitor.poll(1020)
    output = capsys.readouterr().out
    assert not fake.restarts
    assert output.count("inspect_failed") == len(watchdog.TARGETS)
    assert "private data" not in output
    assert "sensitive" not in output


@pytest.mark.parametrize(
    "failure", [True, OSError("private data"), subprocess.TimeoutExpired("docker", 10)]
)
def test_restart_failure_still_consumes_durable_budget(tmp_path, fake, failure, capsys):
    unhealthy(fake)
    fake.restart_failure = failure
    path = tmp_path / "state.json"
    watchdog.Watchdog(path).poll(1000)
    watchdog.Watchdog(path).poll(1020)
    assert len(fake.restarts) == 1
    assert watchdog.load_state(path)["crawl4ai"]["attempts"] == [1000]
    assert "restart_failed" in capsys.readouterr().out


def test_state_failure_prevents_restart(tmp_path, fake, monkeypatch):
    unhealthy(fake)

    def fail(*_):
        raise OSError("disk unavailable")

    monkeypatch.setattr(watchdog, "save_state", fail)
    with pytest.raises(OSError):
        watchdog.Watchdog(tmp_path / "state.json").poll(1000)
    assert not fake.restarts


@pytest.mark.parametrize("data", ["invalid", "[]", '{"foreign": {}}', '{"mcp": {}}'])
def test_invalid_state_fails_closed(tmp_path, data):
    path = tmp_path / "state.json"
    path.write_text(data)
    with pytest.raises(ValueError):
        watchdog.Watchdog(path)


def test_docker_calls_have_timeout_and_capture_only(monkeypatch):
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    watchdog.docker("inspect", "target")
    assert calls == [
        (
            ["docker", "inspect", "target"],
            {"capture_output": True, "text": True, "timeout": 10, "check": False},
        )
    ]
    assert "Env" not in watchdog.INSPECT_FORMAT


def test_once_uses_xdg_state_and_polls_each_allowlisted_target(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(watchdog.signal, "signal", lambda *_: None)
    assert watchdog.main(["--once"]) == 0
    assert len(fake.calls) == len(watchdog.TARGETS)
    assert Path(tmp_path / "local-web-watchdog/state.json").is_file()


def test_attempt_is_saved_before_restart(tmp_path, fake, monkeypatch):
    unhealthy(fake)
    path = tmp_path / "state.json"

    def invoke(*arguments):
        if arguments[0] == "restart":
            assert watchdog.load_state(path)["crawl4ai"]["attempts"] == [1000]
        return fake(*arguments)

    monkeypatch.setattr(watchdog, "docker", invoke)
    watchdog.Watchdog(path).poll(1000)
    assert len(fake.restarts) == 1


def test_malformed_inspection_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        watchdog,
        "docker",
        lambda *args: subprocess.CompletedProcess(args, 0, "invalid json private data", ""),
    )
    monitor = watchdog.Watchdog(tmp_path / "state.json")
    monitor.poll(1000)
    assert set(monitor.transitions.values()) == {"inspect_failed"}


def test_second_watcher_refused(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(watchdog.signal, "signal", lambda *_: None)
    lock_path = watchdog.state_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a") as lock:
        watchdog.fcntl.flock(lock, watchdog.fcntl.LOCK_EX | watchdog.fcntl.LOCK_NB)
        assert watchdog.main(["--once"]) == 1
    assert not fake.calls
    assert "already_running" in capsys.readouterr().out


def test_corrupt_state_cli_reports_sanitized_failure(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(watchdog.signal, "signal", lambda *_: None)
    path = watchdog.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("private invalid state")
    assert watchdog.main(["--once"]) == 1
    output = capsys.readouterr().out
    assert "state_unavailable" in output
    assert "private" not in output
    assert not fake.calls


@pytest.mark.parametrize("interval", ["0", "nan", "inf"])
def test_invalid_interval_rejected(interval):
    with pytest.raises(SystemExit) as error:
        watchdog.main(["--interval", interval])
    assert error.value.code == 2


def test_crawl_proxy_failure_restarts_healthy_byedpi_instead(tmp_path, fake):
    unhealthy(fake)["output"] = '{"failed_checks":["proxy"]}'
    monitor = watchdog.Watchdog(tmp_path / "state.json")
    monitor.poll(1000)
    proxy_id = fake.containers[watchdog.TARGETS["byedpi"]]["id"]
    assert fake.restarts == [("restart", "--timeout", "5", proxy_id)]
    assert monitor.transitions["crawl4ai"] == "waiting_for_proxy"
    assert monitor.state["byedpi"]["attempts"] == [1000]
    crawl_inspect = next(call for call in fake.calls if call[-1] == watchdog.TARGETS["crawl4ai"])
    assert crawl_inspect[-2] == watchdog.HEALTH_OUTPUT_FORMAT


def test_crawl_proxy_failure_refuses_foreign_byedpi(tmp_path, fake):
    unhealthy(fake)["output"] = '{"failed_checks":["proxy"]}'
    fake.containers[watchdog.TARGETS["byedpi"]]["project"] = "foreign"
    monitor = watchdog.Watchdog(tmp_path / "state.json")
    monitor.poll(1000)
    assert not fake.restarts
    assert monitor.transitions["byedpi"] == "ownership_refused"


def test_proxy_fault_cooldown_persists_despite_healthy_listener(tmp_path, fake):
    unhealthy(fake)["output"] = '{"failed_checks":["proxy"]}'
    path = tmp_path / "state.json"
    monitor = watchdog.Watchdog(path)
    monitor.poll(1000)
    monitor.poll(1020)
    assert len(fake.restarts) == 1
    monitor = watchdog.Watchdog(path)
    monitor.poll(1040)
    assert len(fake.restarts) == 1
    for now in (1060, 1180, 1420):
        monitor.poll(now)
    assert len(fake.restarts) == 3
    assert monitor.state["byedpi"]["delay"] == 480
    assert monitor.transitions["byedpi"] == "rate_limited"
    fake.containers[watchdog.TARGETS["crawl4ai"]]["health"] = "healthy"
    monitor.poll(1440)
    assert monitor.state["byedpi"]["next"] == 0
    assert monitor.state["byedpi"]["delay"] == 60
    assert monitor.state["byedpi"]["attempts"] == [1000, 1060, 1180]


@pytest.mark.parametrize(
    "output",
    [
        "invalid JSON",
        "x" * 2049,
        '{"failed_checks":["crawler"]}',
        '{"failed_checks":["proxy","unknown"]}',
        '{"failed_checks":"proxy"}',
        '{"failed_checks":[["proxy"]]}',
        '{"failed_checks":["proxy"],"extra":true}',
        "[]",
    ],
)
def test_unknown_health_output_keeps_normal_crawl_restart(tmp_path, fake, output):
    container = unhealthy(fake)
    container["output"] = output
    watchdog.Watchdog(tmp_path / "state.json").poll(1000)
    assert fake.restarts == [("restart", "--timeout", "5", container["id"])]


def test_foreign_crawl_cannot_request_proxy_restart(tmp_path, fake):
    container = unhealthy(fake)
    container.update(output='{"failed_checks":["proxy"]}', project="foreign")
    watchdog.Watchdog(tmp_path / "state.json").poll(1000)
    assert not fake.restarts
