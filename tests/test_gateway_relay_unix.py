import asyncio
import os
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from service import gateway_relay as relay

UNIX_PATH = "/run/model-gateway.sock"


def inherited():
    sock = MagicMock()
    sock.family = socket.AF_UNIX
    sock.type = socket.SOCK_STREAM
    sock.getsockname.return_value = UNIX_PATH
    sock.getsockopt.return_value = 1
    return sock


def test_activation_requires_explicit_path():
    config = relay.RelayConfig("172.18.0.1")
    with pytest.raises(ValueError, match="explicit Unix listener path"):
        relay.activated_socket(config, {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1"})
    assert relay.activated_socket(config, {}) is None


@pytest.mark.parametrize(
    "pid,fds", [("0", "1"), ("", "1"), (None, "1"), ("self", "2"), ("self", "0")]
)
def test_activation_rejects_foreign_pid_or_descriptor_count(monkeypatch, pid, fds):
    factory = MagicMock()
    monkeypatch.setattr(relay.socket, "socket", factory)
    env = {"LISTEN_FDS": fds}
    if pid is not None:
        env["LISTEN_PID"] = str(os.getpid()) if pid == "self" else pid
    with pytest.raises(ValueError, match="this process"):
        relay.activated_socket(relay.RelayConfig(listen_unix_path=UNIX_PATH), env)
    factory.assert_not_called()


@pytest.mark.parametrize("invalid", ["family", "type", "path", "listening"])
def test_activation_validates_owned_fd_and_closes_on_failure(monkeypatch, invalid):
    sock = inherited()
    if invalid == "family":
        sock.family = socket.AF_INET
    elif invalid == "type":
        sock.type = socket.SOCK_DGRAM
    elif invalid == "path":
        sock.getsockname.return_value = "/run/foreign.sock"
    else:
        sock.getsockopt.return_value = 0
    factory = MagicMock(return_value=sock)
    monkeypatch.setattr(relay.socket, "socket", factory)
    with pytest.raises(ValueError):
        relay.activated_socket(
            relay.RelayConfig(listen_unix_path=UNIX_PATH),
            {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1"},
        )
    factory.assert_called_once_with(fileno=3)
    sock.close.assert_called_once()


@pytest.mark.parametrize("version", [(3, 12), (3, 13), (3, 14)])
def test_valid_activation_and_unix_listener_never_own_path(monkeypatch, version):
    async def scenario():
        sock = inherited()
        monkeypatch.setattr(relay, "sys", SimpleNamespace(version_info=version))
        monkeypatch.setattr(relay.socket, "socket", MagicMock(return_value=sock))
        config = relay.RelayConfig.from_env({relay.PREFIX + "LISTEN_UNIX_PATH": UNIX_PATH})
        activated = relay.activated_socket(
            config, {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1"}
        )
        assert activated is sock
        server = MagicMock()
        server.sockets = [sock]
        server.start_serving = AsyncMock()
        server.wait_closed = AsyncMock()
        start = AsyncMock(return_value=server)
        monkeypatch.setattr(relay.asyncio, "start_unix_server", start)
        unlink = MagicMock(side_effect=AssertionError("Foreign path must never be removed"))
        monkeypatch.setattr(relay.os, "unlink", unlink)
        subject = relay.GatewayRelay(config)
        await subject.start(activated)
        options = {"cleanup_socket": False} if version >= (3, 13) else {}
        start.assert_awaited_once_with(
            subject.accept, sock=sock, limit=65536, start_serving=False, **options
        )
        server.start_serving.assert_awaited_once()
        await subject.close()
        server.close.assert_called_once()
        unlink.assert_not_called()

    asyncio.run(scenario())


def test_unix_listener_without_activation_is_rejected():
    subject = relay.GatewayRelay(relay.RelayConfig(listen_unix_path=UNIX_PATH))
    with pytest.raises(ValueError, match="activated socket"):
        asyncio.run(subject.start())


@pytest.mark.parametrize("field", ["TARGET_UNIX_PATH", "LISTEN_UNIX_PATH"])
@pytest.mark.parametrize("path", ["relative.sock", "\x00/run/socket"])
def test_unix_path_must_be_absolute_filesystem_path(field, path):
    with pytest.raises(ValueError):
        relay.RelayConfig.from_env(
            {relay.PREFIX + "BIND_HOST": "172.18.0.1", relay.PREFIX + field: path}
        )


def test_unix_listener_must_forward_to_loopback_tcp():
    with pytest.raises(ValueError, match="loopback gateway"):
        relay.RelayConfig(listen_unix_path=UNIX_PATH, target_unix_path=UNIX_PATH)


@pytest.mark.parametrize(
    "docker,address,valid",
    [
        (False, "172.18.0.2", False),
        (True, "8.8.8.8", False),
        (True, "0.0.0.0", False),
        (True, "172.18.0.2", True),
    ],
)
def test_container_bind_resolves_only_own_docker_private_ipv4(monkeypatch, docker, address, valid):
    monkeypatch.setattr(relay.Path, "is_file", lambda path: docker)
    monkeypatch.setattr(relay.socket, "gethostname", lambda: "own-container")
    resolve = MagicMock(return_value=address)
    monkeypatch.setattr(relay.socket, "gethostbyname", resolve)
    env = {relay.PREFIX + "BIND_HOST": "container", relay.PREFIX + "TARGET_UNIX_PATH": UNIX_PATH}
    if valid:
        config = relay.RelayConfig.from_env(env)
        assert config.bind_host == address
        assert config.target_unix_path == UNIX_PATH
    else:
        with pytest.raises(ValueError):
            relay.RelayConfig.from_env(env)
    if docker:
        resolve.assert_called_once_with("own-container")
    else:
        resolve.assert_not_called()


def test_unix_target_forwards_bytes_without_opening_tcp(monkeypatch):
    async def scenario():
        readers = [asyncio.StreamReader(), asyncio.StreamReader()]
        writers = [MagicMock(), MagicMock()]
        for reader, data in zip(readers, [b"request", b"response"]):
            reader.feed_data(data)
            reader.feed_eof()
        for writer in writers:
            writer.drain = AsyncMock()
            writer.wait_closed = AsyncMock()
            writer.can_write_eof.return_value = True
        unix_connect = AsyncMock(return_value=(readers[1], writers[1]))
        tcp_connect = AsyncMock(side_effect=AssertionError("Must use mounted Unix target"))
        monkeypatch.setattr(relay.asyncio, "open_unix_connection", unix_connect)
        monkeypatch.setattr(relay.asyncio, "open_connection", tcp_connect)
        await relay.forward(
            readers[0], writers[0], relay.RelayConfig("172.18.0.2", target_unix_path=UNIX_PATH)
        )
        unix_connect.assert_awaited_once_with(UNIX_PATH, limit=65536)
        tcp_connect.assert_not_awaited()
        writers[1].write.assert_called_once_with(b"request")
        writers[0].write.assert_called_once_with(b"response")
        for writer in writers:
            writer.close.assert_called_once()
            writer.wait_closed.assert_awaited_once()

    asyncio.run(scenario())


def test_activated_mode_does_not_allow_unspecified_bind():
    with pytest.raises(ValueError, match="explicit private IP"):
        relay.RelayConfig("0.0.0.0", listen_unix_path=UNIX_PATH)


def test_run_releases_activated_descriptor_on_start_failure(monkeypatch):
    async def scenario():
        subject = MagicMock()
        subject.start = AsyncMock(side_effect=RuntimeError("start failed"))
        subject.close = AsyncMock()
        monkeypatch.setattr(relay, "GatewayRelay", MagicMock(return_value=subject))
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", MagicMock())
        monkeypatch.setattr(loop, "remove_signal_handler", MagicMock())
        sock = inherited()
        with pytest.raises(RuntimeError, match="start failed"):
            await relay.run(relay.RelayConfig(listen_unix_path=UNIX_PATH), sock)
        subject.start.assert_awaited_once_with(sock)
        subject.close.assert_awaited_once()
        sock.close.assert_called_once()

    asyncio.run(scenario())


def test_external_unix_socket_path_survives_relay_shutdown(tmp_path, monkeypatch):
    path = tmp_path / "gateway.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # AF_UNIX limits the address, not the filesystem path. Keep managed scratch usable.
    monkeypatch.chdir(tmp_path)

    async def scenario():
        subject = relay.GatewayRelay(relay.RelayConfig(listen_unix_path=str(path)))
        try:
            await subject.start(sock)
            assert path.is_socket()
        finally:
            await subject.close()
        assert path.is_socket(), "Relay shutdown removed the external owner's pathname"

    try:
        sock.bind(path.name)
        sock.listen()
        asyncio.run(scenario())
    finally:
        sock.close()
        # Only the test's socket owner removes its registered temporary pathname.
        path.unlink(missing_ok=True)
    assert not path.exists()
