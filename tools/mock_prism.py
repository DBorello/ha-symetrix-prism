#!/usr/bin/env python3
"""Asyncio mock of a Symetrix Composer DSP (Prism/Radius/Edge).

Emulates the protocol quirks observed on a real Prism:

* ``CS`` replies ``ACK`` (optionally delayed) or ``NAK`` for unassigned
  controls; selector values clamp to the last input index.
* ``GSB2`` replies ``#CCCCC=VVVVV`` lines, ``-0001`` for unassigned controls
  (optionally delayed, like the ~500 ms seen on a real Prism); more than 256
  controls is ``NAK``.
* ``GS2`` replies ``<cn> <value>``, or ``NAK`` for unassigned controls.
* Push lines use the ``GSB2`` format, are sent every push interval (max 64
  values each) and only start once the client has sent a command.
* Selectors transiently read ``-0001`` after connect and after changes.
* Tests can inject stray lines and drop connections.

Run standalone for offline work::

    python tools/mock_prism.py --port 48631 -v
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass, field
import logging
import re

_LOGGER = logging.getLogger("mock_prism")

MAX_VALUE = 65535
MAX_BLOCK = 256
MAX_PUSH_PER_INTERVAL = 64

# Default layout: power 2000+Z, source 2100+Z, volume 2200+Z,
# mute 2300+Z for zones 1-2; selectors have two inputs.
DEFAULT_CONTROLS: dict[int, int] = {
    2001: 0,
    2002: 0,
    2101: 0,
    2102: 0,
    2201: 9362,
    2202: 9362,
    2301: 0,
    2302: 0,
}
DEFAULT_SELECTORS: dict[int, int] = {2101: 2, 2102: 2}


@dataclass
class _Client:
    writer: asyncio.StreamWriter
    push_started: bool = False
    push_enabled: set[int] = field(default_factory=set)
    pending_push: dict[int, int] = field(default_factory=dict)
    transient: dict[int, int] = field(default_factory=dict)
    flusher: asyncio.Task[None] | None = None


class MockPrism:
    """In-process mock DSP server."""

    def __init__(
        self,
        controls: dict[int, int] | None = None,
        *,
        selectors: dict[int, int] | None = None,
        push_controls: set[int] | None = None,
        ack_delay: float = 0.0,
        unassigned_delay: float = 0.0,
        transient_reads: int = 0,
        push_interval_ms: int = 100,
        push_echo: bool = True,
        combine_push: bool = False,
    ) -> None:
        """Create the mock.

        controls:          assigned control numbers and initial values
        selectors:         selector control -> number of inputs
        push_controls:     controls with Push enabled in Composer (default all)
        ack_delay:         seconds before ACK/NAK for CS
        unassigned_delay:  extra seconds before replying to reads that
                           include unassigned controls
        transient_reads:   reads of a selector that return -0001 after a
                           connect or a change
        push_echo:         push changes back to the client that made them
        combine_push:      put all values of one push interval on one line
        """
        self.values: dict[int, int] = dict(
            DEFAULT_CONTROLS if controls is None else controls
        )
        if selectors is None:
            selectors = DEFAULT_SELECTORS if controls is None else {}
        self.selectors: dict[int, int] = dict(selectors)
        self.push_controls = push_controls
        self.ack_delay = ack_delay
        self.unassigned_delay = unassigned_delay
        self.transient_reads = transient_reads
        self.push_interval_ms = push_interval_ms
        self.push_echo = push_echo
        self.combine_push = combine_push
        self.received: list[str] = []
        self.clients: list[_Client] = []
        self.connections = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Start listening; returns the bound port."""
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        """Stop listening and drop every client."""
        if self._server is not None:
            self._server.close()
        await self.drop_clients()
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    async def drop_clients(self) -> None:
        """Close every client connection (simulates a network drop)."""
        for client in list(self.clients):
            if client.flusher is not None:
                client.flusher.cancel()
            client.writer.close()
        self.clients.clear()
        await asyncio.sleep(0)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def set_value(self, cn: int, value: int, source: _Client | None = None) -> None:
        """Change a control as if from the front panel/Composer; pushes it."""
        if cn in self.selectors:
            value = min(value, self.selectors[cn] - 1)
        value = max(0, min(MAX_VALUE, value))
        self.values[cn] = value
        if self.push_controls is not None and cn not in self.push_controls:
            return
        for client in self.clients:
            if cn in self.selectors:
                client.transient[cn] = self.transient_reads
            if client is source and not self.push_echo:
                continue
            client.pending_push[cn] = value

    async def inject(self, raw: str) -> None:
        """Send raw text to every client (stray/unsolicited lines)."""
        for client in self.clients:
            client.writer.write(raw.encode("ascii"))
            await client.writer.drain()

    def commands(self, prefix: str = "") -> list[str]:
        """Return received commands starting with ``prefix``."""
        return [c for c in self.received if c.startswith(prefix)]

    # ------------------------------------------------------------------
    # Protocol
    # ------------------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = _Client(
            writer,
            push_enabled=set(range(1, 10001)),
            transient=dict.fromkeys(self.selectors, self.transient_reads),
        )
        self.clients.append(client)
        if (task := asyncio.current_task()) is not None:
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
        self.connections += 1
        client.flusher = asyncio.create_task(self._flush_loop(client))
        buffer = ""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buffer += data.decode("ascii", errors="replace")
                *lines, buffer = re.split(r"[\r\n]+", buffer)
                for line in lines:
                    if line.strip():
                        await self._command(client, line.strip())
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if client.flusher is not None:
                client.flusher.cancel()
            if client in self.clients:
                self.clients.remove(client)
            writer.close()

    async def _send(self, client: _Client, text: str) -> None:
        client.writer.write(text.encode("ascii"))
        await client.writer.drain()

    async def _command(self, client: _Client, line: str) -> None:
        self.received.append(line)
        _LOGGER.debug("<- %s", line)
        client.push_started = True
        parts = line.split()
        name, args = parts[0].upper(), parts[1:]
        try:
            numbers = [int(a) for a in args]
        except ValueError:
            await self._send(client, "NAK\r")
            return

        if name == "CS" and len(numbers) == 2:
            await asyncio.sleep(self.ack_delay)
            cn, value = numbers
            if cn not in self.values or not 0 <= value <= MAX_VALUE:
                await self._send(client, "NAK\r")
                return
            self.set_value(cn, value, source=client)
            await self._send(client, "ACK\r")
        elif name == "CC" and len(numbers) == 3:
            await asyncio.sleep(self.ack_delay)
            cn, direction, amount = numbers
            if cn not in self.values:
                await self._send(client, "NAK\r")
                return
            delta = amount if direction else -amount
            self.set_value(cn, self.values[cn] + delta, source=client)
            await self._send(client, "ACK\r")
        elif name == "GS2" and len(numbers) == 1:
            current = self._read(client, numbers[0])
            if current is None:
                await asyncio.sleep(self.unassigned_delay)
                await self._send(client, "NAK\r")
            else:
                await self._send(client, f"{numbers[0]} {current}\r")
        elif name == "GSB2" and len(numbers) == 2:
            start, count = numbers
            if not 1 <= count <= MAX_BLOCK:
                await self._send(client, "NAK\r")
                return
            values = [self._read(client, cn) for cn in range(start, start + count)]
            if None in values:
                await asyncio.sleep(self.unassigned_delay)
            await self._send(
                client,
                "".join(
                    _format(cn, value)
                    for cn, value in zip(
                        range(start, start + count), values, strict=True
                    )
                ),
            )
        elif name in ("PUE", "PUD", "PUR") and len(numbers) <= 2:
            low = numbers[0] if numbers else 1
            high = numbers[-1] if numbers else 10000
            controls = set(range(low, high + 1))
            if name == "PUE":
                client.push_enabled |= controls
            elif name == "PUD":
                client.push_enabled -= controls
            else:
                for cn in controls & self.values.keys():
                    client.pending_push[cn] = self.values[cn]
            await self._send(client, "ACK\r")
        elif name == "PUI" and len(numbers) == 1 and 20 <= numbers[0] <= 30000:
            self.push_interval_ms = numbers[0]
            await self._send(client, "ACK\r")
        elif name == "NOP" and not numbers:
            await self._send(client, "ACK\r")
        else:
            await self._send(client, "NAK\r")

    def _read(self, client: _Client, cn: int) -> int | None:
        if cn not in self.values:
            return None
        if client.transient.get(cn, 0) > 0:
            client.transient[cn] -= 1
            return None
        return self.values[cn]

    async def _flush_loop(self, client: _Client) -> None:
        while True:
            await asyncio.sleep(self.push_interval_ms / 1000)
            if not client.push_started or not client.pending_push:
                continue
            ready = [cn for cn in client.pending_push if cn in client.push_enabled][
                :MAX_PUSH_PER_INTERVAL
            ]
            if not ready:
                continue
            lines = [_format(cn, client.pending_push.pop(cn)) for cn in ready]
            text = (
                "".join(line.rstrip("\r") for line in lines) + "\r"
                if self.combine_push
                else "".join(lines)
            )
            try:
                await self._send(client, text)
            except ConnectionError:
                return


def _format(cn: int, value: int | None) -> str:
    return f"#{cn:05d}=-0001\r" if value is None else f"#{cn:05d}={value:05d}\r"


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=48631)
    parser.add_argument("--ack-delay", type=float, default=0.04)
    parser.add_argument("--unassigned-delay", type=float, default=0.5)
    parser.add_argument("--transient-reads", type=int, default=2)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    mock = MockPrism(
        ack_delay=args.ack_delay,
        unassigned_delay=args.unassigned_delay,
        transient_reads=args.transient_reads,
    )
    port = await mock.start(args.host, args.port)
    _LOGGER.info("Mock Prism listening on %s:%s", args.host, port)
    await asyncio.Event().wait()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())
