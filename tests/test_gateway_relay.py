import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from service import gateway_relay as relay


def writer():
    result = MagicMock()
    result.drain = AsyncMock()
    result.wait_closed = AsyncMock()
    result.can_write_eof.return_value = True
    return result


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"BIND_HOST": ""},
        {"BIND_HOST": "0.0.0.0"},
        {"BIND_HOST": "::"},
        {"BIND_HOST": "224.0.0.1"},
        {"BIND_HOST": "8.8.8.8"},
        {"BIND_HOST": "localhost"},
        {"BIND_HOST": "172.18.0.1", "BIND_PORT": "0"},
        {"BIND_HOST": "172.18.0.1", "BIND_PORT": "65536"},
        {"BIND_HOST": "172.18.0.1", "BIND_PORT": "abc"},
        {"BIND_HOST": "172.18.0.1", "TARGET_HOST": "172.18.0.2"},
        {"BIND_HOST": "172.18.0.1", "TARGET_HOST": "localhost"},
        {"BIND_HOST": "172.18.0.1", "TARGET_PORT": "0"},
    ],
)
def test_config_fails_closed(env):
    with pytest.raises(ValueError):
        relay.RelayConfig.from_env({relay.PREFIX + key: value for key, value in env.items()})


def test_config_defaults_and_ipv6():
    config = relay.RelayConfig.from_env({relay.PREFIX + "BIND_HOST": "172.18.0.1"})
    assert config == relay.RelayConfig("172.18.0.1", 18045, "127.0.0.1", 8045)
    assert relay.RelayConfig("fd00::1", target_host="::1").target_host == "::1"
    with pytest.raises(ValueError):
        relay.RelayConfig("172.18.0.1", bind_port=True)


def test_bidirectional_bytes_half_close_and_bounded_buffers(monkeypatch):
    async def scenario():
        client_reader, target_reader = asyncio.StreamReader(), asyncio.StreamReader()
        client_writer, target_writer = writer(), writer()
        client_reader.feed_data(b"request\x00")
        client_reader.feed_eof()

        async def target_drain():
            if target_writer.write_eof.called:
                target_reader.feed_data(b"response\xff")
                target_reader.feed_eof()

        target_writer.drain.side_effect = target_drain
        connect = AsyncMock(return_value=(target_reader, target_writer))
        monkeypatch.setattr(relay.asyncio, "open_connection", connect)
        await relay.forward(client_reader, client_writer, relay.RelayConfig("172.18.0.1"))
        connect.assert_awaited_once_with("127.0.0.1", 8045, limit=65536)
        target_writer.write.assert_called_once_with(b"request\x00")
        client_writer.write.assert_called_once_with(b"response\xff")
        for stream_writer in (client_writer, target_writer):
            stream_writer.transport.set_write_buffer_limits.assert_called_once_with(high=65536)
            stream_writer.write_eof.assert_called_once()
            stream_writer.close.assert_called_once()
            stream_writer.wait_closed.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [ConnectionRefusedError(), TimeoutError()])
def test_failed_target_closes_client(monkeypatch, failure):
    client_writer = writer()
    monkeypatch.setattr(relay.asyncio, "open_connection", AsyncMock(side_effect=failure))
    asyncio.run(relay.forward(MagicMock(), client_writer, relay.RelayConfig("172.18.0.1")))
    client_writer.close.assert_called_once()
    client_writer.wait_closed.assert_awaited_once()


@pytest.mark.parametrize("reason", ["idle", "disconnect", "cancel"])
def test_connection_cleanup_joins_owned_tasks(monkeypatch, reason):
    async def scenario():
        client_reader, target_reader = asyncio.StreamReader(), asyncio.StreamReader()
        client_writer, target_writer = writer(), writer()
        connected = asyncio.Event()

        async def connect(*args, **kwargs):
            connected.set()
            return target_reader, target_writer

        monkeypatch.setattr(relay.asyncio, "open_connection", connect)
        monkeypatch.setattr(relay, "IDLE_SECONDS", 0.01)
        if reason == "disconnect":
            client_reader.set_exception(ConnectionResetError())
        task = asyncio.create_task(
            relay.forward(client_reader, client_writer, relay.RelayConfig("172.18.0.1"))
        )
        await connected.wait()
        if reason == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 1)
        for stream_writer in (client_writer, target_writer):
            stream_writer.close.assert_called_once()
            stream_writer.wait_closed.assert_awaited_once()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_real_connect_timeout_is_bounded(monkeypatch):
    async def scenario():
        client_writer = writer()

        async def connect(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(relay.asyncio, "open_connection", connect)
        monkeypatch.setattr(relay, "CONNECT_SECONDS", 0.01)
        await asyncio.wait_for(
            relay.forward(asyncio.StreamReader(), client_writer, relay.RelayConfig("172.18.0.1")),
            1,
        )
        client_writer.close.assert_called_once()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


@pytest.mark.parametrize("address", [("0.0.0.0", 18045), ("172.18.0.1", 18046), None])
def test_listener_rewritten_or_missing_bind_fails_closed(monkeypatch, address):
    server = MagicMock()
    server.wait_closed = AsyncMock()
    server.start_serving = AsyncMock()
    sock = MagicMock()
    sock.getsockname.return_value = address
    server.sockets = [sock] if address else []
    monkeypatch.setattr(relay.asyncio, "start_server", AsyncMock(return_value=server))
    subject = relay.GatewayRelay(relay.RelayConfig("172.18.0.1"))
    with pytest.raises(RuntimeError, match="requested bind"):
        asyncio.run(subject.start())
    server.start_serving.assert_not_awaited()
    server.close.assert_called_once()
    server.wait_closed.assert_awaited_once()


def test_limit_and_shutdown_close_only_owned_connections(monkeypatch):
    async def scenario():
        subject = relay.GatewayRelay(relay.RelayConfig("172.18.0.1"))
        entered = asyncio.Event()
        finished = asyncio.Event()

        async def forwarding(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        monkeypatch.setattr(relay, "MAX_CLIENTS", 1)
        monkeypatch.setattr(relay, "forward", forwarding)
        subject.accept(MagicMock(), writer())
        await entered.wait()
        rejected = writer()
        subject.accept(MagicMock(), rejected)
        rejected.close.assert_called_once()
        assert len(subject.connections) == 1
        unrelated = asyncio.create_task(asyncio.Event().wait())
        await subject.close()
        assert finished.is_set()
        assert not subject.connections
        assert not unrelated.done()
        stopped = writer()
        subject.accept(MagicMock(), stopped)
        stopped.close.assert_called_once()
        unrelated.cancel()
        with pytest.raises(asyncio.CancelledError):
            await unrelated

    asyncio.run(scenario())


def test_shutdown_before_handler_runs_closes_client():
    async def scenario():
        subject = relay.GatewayRelay(relay.RelayConfig("172.18.0.1"))
        client_writer = writer()
        subject.accept(asyncio.StreamReader(), client_writer)
        await subject.close()
        assert client_writer.close.called
        client_writer.wait_closed.assert_awaited_once()
        assert not subject.connections
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_exact_listener_bind_starts_only_after_verification(monkeypatch):
    async def scenario():
        server = MagicMock()
        server.start_serving = AsyncMock()
        server.wait_closed = AsyncMock()
        sock = MagicMock()
        sock.getsockname.return_value = ("172.18.0.1", 18045)
        server.sockets = [sock]
        start = AsyncMock(return_value=server)
        monkeypatch.setattr(relay.asyncio, "start_server", start)
        subject = relay.GatewayRelay(relay.RelayConfig("172.18.0.1"))
        await subject.start()
        start.assert_awaited_once_with(
            subject.accept, "172.18.0.1", 18045, limit=65536, start_serving=False
        )
        server.start_serving.assert_awaited_once()
        await subject.close()
        server.close.assert_called_once()

    asyncio.run(scenario())


def test_one_active_direction_resets_shared_idle_timeout(monkeypatch):
    async def scenario():
        client_reader, target_reader = asyncio.StreamReader(), asyncio.StreamReader()
        client_writer, target_writer = writer(), writer()
        monkeypatch.setattr(
            relay.asyncio,
            "open_connection",
            AsyncMock(return_value=(target_reader, target_writer)),
        )
        clock = [0.0]
        wake = asyncio.Event()
        real_sleep = asyncio.sleep

        async def idle_sleep(delay):
            await wake.wait()
            wake.clear()

        monkeypatch.setattr(
            relay.asyncio, "get_running_loop", lambda: SimpleNamespace(time=lambda: clock[0])
        )
        monkeypatch.setattr(relay.asyncio, "sleep", idle_sleep)
        task = asyncio.create_task(
            relay.forward(client_reader, client_writer, relay.RelayConfig("172.18.0.1"))
        )
        for _ in range(6):
            client_reader.feed_data(b"active")
            await real_sleep(0)
            await real_sleep(0)
            clock[0] += 20
            wake.set()
            await real_sleep(0)
            await real_sleep(0)
            assert not task.done()
        client_reader.feed_eof()
        target_reader.feed_eof()
        await task
        assert target_writer.write.call_count == 6

    asyncio.run(scenario())


def test_run_registers_signals_and_shuts_down(monkeypatch):
    async def scenario():
        subject = MagicMock()
        subject.close = AsyncMock()
        loop = asyncio.get_running_loop()
        add_handler, remove_handler = MagicMock(), MagicMock()
        monkeypatch.setattr(loop, "add_signal_handler", add_handler)
        monkeypatch.setattr(loop, "remove_signal_handler", remove_handler)

        async def start():
            add_handler.call_args_list[0].args[1]()

        subject.start = AsyncMock(side_effect=start)
        monkeypatch.setattr(relay, "GatewayRelay", MagicMock(return_value=subject))
        await relay.run(relay.RelayConfig("172.18.0.1"))
        assert [call.args[0] for call in add_handler.call_args_list] == [
            relay.signal.SIGTERM,
            relay.signal.SIGINT,
        ]
        assert remove_handler.call_args_list == [
            ((relay.signal.SIGTERM,), {}),
            ((relay.signal.SIGINT,), {}),
        ]
        subject.close.assert_awaited_once()

    asyncio.run(scenario())
