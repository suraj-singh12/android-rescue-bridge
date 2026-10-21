#!/usr/bin/env python3
"""Rescue Bridge relay: authenticated TLS rendezvous for one ADB byte stream per device.

The relay is intentionally dumb: it only pairs a waiting Android agent with an
operator client after both present the same enrolled device credential. Run it on
a VPS/server reachable from both sides. No user accounts or external service are
required.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

MAX_HANDSHAKE = 8192
COPY_CHUNK = 128 * 1024


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Registry:
    def __init__(self, path: Path):
        self.path = path

    def expected_hash(self, device_id: str) -> Optional[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            record = data.get("devices", {}).get(device_id)
            if isinstance(record, dict):
                value = record.get("token_sha256")
                if isinstance(value, str):
                    return value.lower()
        except Exception as exc:
            logging.error("Could not read registry %s: %s", self.path, exc)
        return None

    def authenticate(self, device_id: str, token: str) -> bool:
        expected = self.expected_hash(device_id)
        if not expected:
            return False
        return hmac.compare_digest(expected, token_hash(token))


def peer(writer: asyncio.StreamWriter) -> str:
    info = writer.get_extra_info("peername")
    return str(info or "unknown")


async def write_line(writer: asyncio.StreamWriter, line: str) -> None:
    writer.write(line.encode("utf-8") + b"\n")
    await writer.drain()


async def read_json_line(reader: asyncio.StreamReader, timeout: float = 10.0) -> dict:
    raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
    if not raw or len(raw) > MAX_HANDSHAKE or not raw.endswith(b"\n"):
        raise ValueError("invalid or oversized handshake")
    obj = json.loads(raw.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("handshake must be an object")
    return obj


def tune_socket(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


@dataclass
class AgentSession:
    device_id: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    control_task: Optional[asyncio.Task] = None
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    closed_event: asyncio.Event = field(default_factory=asyncio.Event)
    failed_reason: Optional[str] = None
    busy: bool = False


class Relay:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.agents: Dict[str, AgentSession] = {}
        self.lock = asyncio.Lock()

    async def control_loop(self, session: AgentSession) -> None:
        try:
            while True:
                raw = await session.reader.readline()
                if not raw:
                    session.failed_reason = "agent disconnected"
                    session.ready_event.set()
                    return
                if len(raw) > 1024:
                    session.failed_reason = "oversized agent control message"
                    session.ready_event.set()
                    return
                line = raw.decode("utf-8", "replace").strip()
                if line == "PING":
                    await write_line(session.writer, "PONG")
                elif line == "READY":
                    session.ready_event.set()
                    return
                elif line.startswith("ERROR "):
                    session.failed_reason = line[6:][:500]
                    session.ready_event.set()
                    return
                else:
                    logging.warning("Unexpected control message from %s: %r", session.device_id, line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.failed_reason = f"agent control error: {exc}"
            session.ready_event.set()
        finally:
            # A normal READY ends only the control phase; the same socket becomes
            # the raw ADB tunnel. Only failures/disconnects close the waiting agent.
            if session.failed_reason:
                session.closed_event.set()

    async def register_agent(self, device_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = AgentSession(device_id=device_id, reader=reader, writer=writer)
        async with self.lock:
            previous = self.agents.get(device_id)
            if previous is not None:
                logging.info("Replacing previous waiting agent for %s", device_id)
                try:
                    previous.writer.close()
                except Exception:
                    pass
            self.agents[device_id] = session
        await write_line(writer, "WAIT")
        logging.info("Agent waiting: %s from %s", device_id, peer(writer))
        session.control_task = asyncio.create_task(self.control_loop(session), name=f"agent-control-{device_id}")
        try:
            await session.closed_event.wait()
        finally:
            async with self.lock:
                if self.agents.get(device_id) is session:
                    self.agents.pop(device_id, None)
            if not writer.is_closing():
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            logging.info("Agent ended: %s", device_id)

    async def pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(COPY_CHUNK)
                if not chunk:
                    return
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            return

    async def bridge_client(self, device_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self.lock:
            session = self.agents.get(device_id)
            if session is None or session.writer.is_closing():
                await write_line(writer, "ERROR agent_offline")
                return
            if session.busy:
                await write_line(writer, "ERROR agent_busy")
                return
            session.busy = True
            session.ready_event.clear()
            session.failed_reason = None

        logging.info("Client requesting %s from %s", device_id, peer(writer))
        try:
            await write_line(session.writer, "PREPARE")
            try:
                await asyncio.wait_for(session.ready_event.wait(), timeout=12.0)
            except asyncio.TimeoutError:
                await write_line(writer, "ERROR agent_prepare_timeout")
                return
            if session.failed_reason:
                await write_line(writer, "ERROR " + session.failed_reason.replace("\n", " ")[:500])
                return

            if session.control_task and not session.control_task.done():
                session.control_task.cancel()
                try:
                    await session.control_task
                except asyncio.CancelledError:
                    pass

            await write_line(writer, "GO")
            logging.info("Tunnel active: %s", device_id)
            a = asyncio.create_task(self.pipe(reader, session.writer), name=f"client-to-agent-{device_id}")
            b = asyncio.create_task(self.pipe(session.reader, writer), name=f"agent-to-client-{device_id}")
            done, pending = await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*done, return_exceptions=True)
        finally:
            try:
                session.writer.close()
                await session.writer.wait_closed()
            except Exception:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            async with self.lock:
                if self.agents.get(device_id) is session:
                    self.agents.pop(device_id, None)
            session.closed_event.set()
            logging.info("Tunnel ended: %s", device_id)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tune_socket(writer)
        try:
            hello = await read_json_line(reader)
            if hello.get("v") != 1:
                raise ValueError("unsupported protocol version")
            role = str(hello.get("role", ""))
            device_id = str(hello.get("device_id", ""))
            token = str(hello.get("token", ""))
            if not device_id or len(device_id) > 128 or not token or len(token) > 256:
                raise ValueError("invalid credentials")
            if not self.registry.authenticate(device_id, token):
                logging.warning("Authentication failed for %s (%s) from %s", device_id, role, peer(writer))
                await write_line(writer, "ERROR authentication_failed")
                return
            if role == "agent":
                await self.register_agent(device_id, reader, writer)
                return
            if role == "client":
                await self.bridge_client(device_id, reader, writer)
                return
            if role == "status":
                async with self.lock:
                    session = self.agents.get(device_id)
                    state = "OFFLINE" if session is None or session.writer.is_closing() else ("BUSY" if session.busy else "ONLINE")
                await write_line(writer, "STATUS " + state)
                return
            raise ValueError("invalid role")
        except Exception as exc:
            logging.warning("Connection rejected from %s: %s", peer(writer), exc)
            try:
                await write_line(writer, "ERROR bad_request")
            except Exception:
                pass
        finally:
            if not writer.is_closing():
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass


async def amain(args: argparse.Namespace) -> None:
    ssl_ctx = None
    if not args.insecure:
        if not args.cert or not args.key:
            raise SystemExit("TLS is the default. Supply --cert and --key, or use --insecure only for isolated testing.")
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_ctx.load_cert_chain(args.cert, args.key)

    registry = Registry(Path(args.registry))
    relay = Relay(registry)
    server = await asyncio.start_server(relay.handle, args.listen, args.port, ssl=ssl_ctx, backlog=128)
    sockets = ", ".join(str(s.getsockname()) for s in (server.sockets or []))
    logging.info("Rescue Bridge relay listening on %s (%s)", sockets, "PLAINTEXT TEST MODE" if args.insecure else "TLS")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    async with server:
        await stop_event.wait()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Self-hosted TLS relay for Rescue Bridge")
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--registry", default="devices.json")
    p.add_argument("--cert", help="TLS certificate PEM")
    p.add_argument("--key", help="TLS private key PEM")
    p.add_argument("--insecure", action="store_true", help="Plain TCP for isolated testing only; never use across the internet")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(amain(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
