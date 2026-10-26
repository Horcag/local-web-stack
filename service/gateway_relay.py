"""Optional, explicitly bound TCP relay to a local loopback gateway."""

import asyncio
import ipaddress
import os
import signal
import socket
import sys
from pathlib import Path
from collections.abc import Mapping
from dataclasses import dataclass

BUFFER_BYTES = 64 * 1024
CONNECT_SECONDS = 5.0
IDLE_SECONDS = 60.0
MAX_CLIENTS = 16
PREFIX = "LOCAL_WEB_GATEWAY_RELAY_"


@dataclass(frozen=True)
class RelayConfig:
    bind_host: str = ""
    bind_port: int = 18045
    target_host: str = "127.0.0.1"
    target_port: int = 8045
    target_unix_path: str = ""
    listen_unix_path: str = ""

    def __post_init__(self) -> None:
        if self.bind_host or not self.listen_unix_path:
            bind = ipaddress.ip_address(self.bind_host)
            if not bind.is_private or bind.is_unspecified or bind.is_multicast:
                raise ValueError("Relay bind must be an explicit private IP address")
        for path in (self.target_unix_path, self.listen_unix_path):
            if path and (not Path(path).is_absolute() or "\x00" in path):
                raise ValueError("Relay Unix paths must be absolute filesystem paths")
        if self.target_unix_path and self.listen_unix_path:
            raise ValueError("Activated Unix listener must target the loopback gateway")
        target = ipaddress.ip_address(self.target_host)
        if not target.is_loopback:
            raise ValueError("Relay target must be a loopback IP address")
        for port in (self.bind_port, self.target_port):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("Relay ports must be integers in 1..65535")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RelayConfig":
        values = os.environ if env is None else env
        host = values.get(PREFIX + "BIND_HOST", "")
        listen_path = values.get(PREFIX + "LISTEN_UNIX_PATH", "")
        if host == "container":
            if not Path("/.dockerenv").is_file():
                raise ValueError("Container bind requires Docker container execution")
            host = socket.gethostbyname(socket.gethostname())
            address = ipaddress.ip_address(host)
            if address.version != 4 or address.is_loopback:
                raise ValueError("Container bind requires its own private IPv4")
        if not host and not listen_path:
            raise ValueError("LOCAL_WEB_GATEWAY_RELAY_BIND_HOST is required")
        return cls(
            host,
            int(values.get(PREFIX + "BIND_PORT", "18045")),
            values.get(PREFIX + "TARGET_HOST", "127.0.0.1"),
            int(values.get(PREFIX + "TARGET_PORT", "8045")),
            values.get(PREFIX + "TARGET_UNIX_PATH", ""),
            listen_path,
        )


def verify_unix_socket(sock: socket.socket, path: str) -> None:
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
        raise ValueError("Activated listener must be a Unix stream socket")
    if sock.getsockname() != path:
        raise ValueError("Activated listener does not match the requested Unix path")
    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1:
        raise ValueError("Activated Unix socket must already be listening")


def activated_socket(
    config: RelayConfig, env: Mapping[str, str] | None = None
) -> socket.socket | None:
    values = os.environ if env is None else env
    if not config.listen_unix_path:
        if values.get("LISTEN_FDS") or values.get("LISTEN_PID"):
            raise ValueError("Socket activation requires an explicit Unix listener path")
        return None
    if values.get("LISTEN_PID") != str(os.getpid()) or values.get("LISTEN_FDS") != "1":
        raise ValueError("Unix listener requires one socket activated for this process")
    sock = socket.socket(fileno=3)
    try:
        verify_unix_socket(sock, config.listen_unix_path)
    except BaseException:
        sock.close()
        raise
    return sock


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        async with asyncio.timeout(CONNECT_SECONDS):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        pass


async def forward(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    config: RelayConfig,
) -> None:
    target_writer = None
    tasks: list[asyncio.Task[None]] = []
    try:
        async with asyncio.timeout(CONNECT_SECONDS):
            if config.target_unix_path:
                target_reader, target_writer = await asyncio.open_unix_connection(
                    config.target_unix_path, limit=BUFFER_BYTES
                )
            else:
                target_reader, target_writer = await asyncio.open_connection(
                    config.target_host, config.target_port, limit=BUFFER_BYTES
                )
        target_writer.transport.set_write_buffer_limits(high=BUFFER_BYTES)
        client_writer.transport.set_write_buffer_limits(high=BUFFER_BYTES)
        loop = asyncio.get_running_loop()
        last_activity = loop.time()

        async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal last_activity
            while data := await reader.read(BUFFER_BYTES):
                last_activity = loop.time()
                writer.write(data)
                await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()

        async def idle_watch() -> None:
            while True:
                remaining = IDLE_SECONDS - (loop.time() - last_activity)
                if remaining <= 0:
                    raise TimeoutError("Relay connection idle")
                await asyncio.sleep(remaining)

        tasks = [
            asyncio.create_task(pump(client_reader, target_writer)),
            asyncio.create_task(pump(target_reader, client_writer)),
            asyncio.create_task(idle_watch()),
        ]
        pending = set(tasks)
        # A half-close still permits a response from the other direction.
        while tasks[0] in pending or tasks[1] in pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except (OSError, TimeoutError):
        # Never include gateway payloads or credentials in diagnostics.
        pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if target_writer is not None:
            await close_writer(target_writer)
        await close_writer(client_writer)


class GatewayRelay:
    def __init__(self, config: RelayConfig) -> None:
        self.config = config
        self.server: asyncio.Server | None = None
        self.connections: dict[asyncio.Task[None], asyncio.StreamWriter] = {}
        self.stopping = False

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.stopping or len(self.connections) >= MAX_CLIENTS:
            writer.close()
            return
        task = asyncio.create_task(forward(reader, writer, self.config))
        self.connections[task] = writer

        def finished(completed: asyncio.Task[None]) -> None:
            self.connections.pop(completed, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)

    async def start(self, inherited_socket: socket.socket | None = None) -> None:
        if self.config.listen_unix_path:
            if inherited_socket is None:
                raise ValueError("Unix listener requires an activated socket")
            verify_unix_socket(inherited_socket, self.config.listen_unix_path)
            # systemd owns the pathname; Python 3.13 added automatic socket cleanup.
            if sys.version_info >= (3, 13):
                self.server = await asyncio.start_unix_server(
                    self.accept,
                    sock=inherited_socket,
                    limit=BUFFER_BYTES,
                    start_serving=False,
                    cleanup_socket=False,
                )
            else:
                self.server = await asyncio.start_unix_server(
                    self.accept, sock=inherited_socket, limit=BUFFER_BYTES, start_serving=False
                )
        else:
            if inherited_socket is not None:
                raise ValueError("Unexpected activated socket for TCP listener")
            self.server = await asyncio.start_server(
                self.accept,
                self.config.bind_host,
                self.config.bind_port,
                limit=BUFFER_BYTES,
                start_serving=False,
            )
        try:
            sockets = self.server.sockets
            if not sockets:
                raise RuntimeError("Relay listener does not match the requested bind")
            for sock in sockets:
                if self.config.listen_unix_path:
                    verify_unix_socket(sock, self.config.listen_unix_path)
                elif (
                    ipaddress.ip_address(sock.getsockname()[0])
                    != ipaddress.ip_address(self.config.bind_host)
                    or sock.getsockname()[1] != self.config.bind_port
                ):
                    raise RuntimeError("Relay listener does not match the requested bind")
            await self.server.start_serving()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self.stopping = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        connections = list(self.connections.items())
        tasks = [task for task, _ in connections]
        # Also close clients whose handler was cancelled before its first execution.
        for _, writer in connections:
            writer.close()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if connections:
            await asyncio.gather(*(close_writer(writer) for _, writer in connections))
        self.connections.clear()


async def run(config: RelayConfig, inherited_socket: socket.socket | None = None) -> None:
    relay = GatewayRelay(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    installed = []
    try:
        for sig in signals:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        if inherited_socket is None:
            await relay.start()
        else:
            await relay.start(inherited_socket)
        await stop.wait()
    finally:
        await relay.close()
        if inherited_socket is not None:
            inherited_socket.close()
        for sig in installed:
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    config = RelayConfig.from_env()
    asyncio.run(run(config, activated_socket(config)))
