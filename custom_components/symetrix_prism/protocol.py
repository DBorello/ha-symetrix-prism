"""Asyncio client for the Symetrix Composer control protocol.

Pure asyncio, no Home Assistant imports, so it can be unit tested and used
from command-line tools.

Protocol summary (plain ASCII over TCP, commands terminated with ``\\r``):

* ``CS <cn> <v>``        set a control, reply ``ACK`` or ``NAK``
* ``GSB2 <start> <n>``   read ``n`` (max 256) controls, reply ``n`` lines of
                         ``#CCCCC=VVVVV``; unassigned controls read ``-0001``
* ``PUE`` / ``PUI <ms>`` enable push / set push interval, reply ``ACK``
* ``NOP``                keepalive, reply ``ACK``

Behaviour observed on real hardware that shapes this client:

* Push lines use the same ``#CCCCC=VVVVV`` format as ``GSB2`` replies, so every
  value line is treated as a state update whatever triggered it, and a block
  read completes once every requested control has been seen.
* Several values can share one line, so lines are scanned with ``findall``.
* Push only starts after the client has sent a command, so ``PUE`` is sent
  right after connecting.
* Selector controls transiently read ``-0001``; reads retry those controls a
  few times before reporting them as unassigned (``None``).
* Unsolicited lines can arrive at any time, so a single reader task
  dispatches every line instead of reading replies inline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import contextlib
from dataclasses import dataclass, field
import logging
import re
import socket
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

MIN_CONTROL = 1
MAX_CONTROL = 10000
MIN_VALUE = 0
MAX_VALUE = 65535
MAX_BLOCK = 256
UNASSIGNED = -1

DEFAULT_PORT = 48631
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_COMMAND_TIMEOUT = 2.0
DEFAULT_READ_RETRIES = 3
DEFAULT_READ_RETRY_DELAY = 0.25
DEFAULT_BACKOFF_MIN = 1.0
DEFAULT_BACKOFF_MAX = 60.0

# Extra time allowed per control in a block read on top of the command timeout.
_BLOCK_TIMEOUT_PER_CONTROL = 0.02
# A connection that stayed up this long resets the reconnect backoff.
_STABLE_CONNECTION_SECONDS = 30.0

_VALUE_RE = re.compile(r"#(\d{1,5})=(-?\d{1,5})")
_GS2_RE = re.compile(r"^(\d{1,5}) (-?\d{1,5})$")
_LINE_SPLIT_RE = re.compile(r"[\r\n]+")

type ValuesCallback = Callable[[dict[int, int | None]], None]
type ConnectionCallback = Callable[[bool], None]


class PrismError(Exception):
    """Base class for Symetrix protocol errors."""


class PrismConnectionError(PrismError):
    """The DSP could not be reached or the connection was lost."""


class PrismTimeoutError(PrismConnectionError):
    """The DSP did not answer a command in time."""


class PrismCommandError(PrismError):
    """The DSP rejected a command with ``NAK``."""


def parse_values(line: str) -> dict[int, int | None]:
    """Parse every ``#CCCCC=VVVVV`` value in a line.

    A bare ``GS2`` reply (``<cn> <value>``) is also accepted. ``-0001``
    (unassigned control) is returned as ``None``.
    """
    matches = _VALUE_RE.findall(line)
    if not matches and (gs2 := _GS2_RE.match(line.strip())):
        matches = [gs2.groups()]
    return {
        int(cn): None if int(value) == UNASSIGNED else int(value)
        for cn, value in matches
    }


def group_runs(controls: Iterable[int]) -> list[tuple[int, int]]:
    """Group control numbers into contiguous ``(start, count)`` runs.

    Runs never exceed ``MAX_BLOCK`` controls.
    """
    runs: list[tuple[int, int]] = []
    for cn in sorted(set(controls)):
        if runs:
            start, count = runs[-1]
            if cn == start + count and count < MAX_BLOCK:
                runs[-1] = (start, count + 1)
                continue
        runs.append((cn, 1))
    return runs


def _check_control(cn: int) -> None:
    if not MIN_CONTROL <= cn <= MAX_CONTROL:
        raise ValueError(f"Control number {cn} outside {MIN_CONTROL}-{MAX_CONTROL}")


@dataclass
class _Pending:
    """A command waiting for its reply."""

    command: str
    future: asyncio.Future[dict[int, int | None]]
    # Controls still expected for a block read; empty for ACK commands.
    wanted: set[int] = field(default_factory=set)
    results: dict[int, int | None] = field(default_factory=dict)


@dataclass
class ConnectionStats:
    """Counters exposed for diagnostics."""

    connects: int = 0
    disconnects: int = 0
    connect_failures: int = 0
    lines_received: int = 0
    values_received: int = 0
    stray_lines: int = 0
    commands_sent: int = 0
    naks: int = 0
    timeouts: int = 0
    last_connected: float | None = None
    last_disconnected: float | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the counters as a plain dict."""
        return dict(self.__dict__)


class PrismClient:
    """Persistent connection to one Symetrix DSP."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        on_values: ValuesCallback | None = None,
        on_connection: ConnectionCallback | None = None,
        push_interval: int | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        read_retries: int = DEFAULT_READ_RETRIES,
        read_retry_delay: float = DEFAULT_READ_RETRY_DELAY,
        backoff_min: float = DEFAULT_BACKOFF_MIN,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
    ) -> None:
        """Initialise the client. Nothing is opened until connect/start."""
        self.host = host
        self.port = port
        self._on_values = on_values
        self._on_connection = on_connection
        self._push_interval = push_interval
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._read_retries = read_retries
        self._read_retry_delay = read_retry_delay
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._disconnected = asyncio.Event()
        self._disconnected.set()
        self._connected = False
        self._connected_event = asyncio.Event()
        self._closing = False
        self.stats = ConnectionStats()

    @property
    def connected(self) -> bool:
        """Return True once connected and push is enabled."""
        return self._connected

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect once and enable push.

        Raises PrismConnectionError if the DSP cannot be reached or does not
        acknowledge ``PUE``. Does nothing if already connected.
        """
        async with self._connect_lock:
            if self._connected:
                return
            self._closing = False
            try:
                async with asyncio.timeout(self._connect_timeout):
                    reader, writer = await asyncio.open_connection(self.host, self.port)
            except (OSError, TimeoutError) as err:
                self.stats.connect_failures += 1
                self.stats.last_error = f"connect: {err!r}"
                raise PrismConnectionError(
                    f"Cannot connect to {self.host}:{self.port}: {err!r}"
                ) from err

            if (sock := writer.get_extra_info("socket")) is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self._reader = reader
            self._writer = writer
            self._disconnected.clear()
            self._reader_task = asyncio.create_task(
                self._read_loop(reader), name=f"symetrix {self.host} reader"
            )
            try:
                # Push only starts once the client has sent a command.
                await self._command("PUE")
                if self._push_interval is not None:
                    await self._command(f"PUI {int(self._push_interval)}")
            except PrismError as err:
                self.stats.connect_failures += 1
                self.stats.last_error = f"handshake: {err!r}"
                self._drop(f"handshake failed: {err!r}")
                raise PrismConnectionError(
                    f"{self.host}:{self.port} did not accept PUE: {err!r}"
                ) from err

            self._connected = True
            self._connected_event.set()
            self.stats.connects += 1
            self.stats.last_connected = time.time()
            _LOGGER.debug("Connected to %s:%s", self.host, self.port)
        self._notify_connection(True)

    async def start(self) -> None:
        """Keep a connection open in the background, reconnecting as needed."""
        self._closing = False
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(
                self._supervise(), name=f"symetrix {self.host} supervisor"
            )

    async def wait_connected(self) -> None:
        """Wait until connected; wrap in ``asyncio.timeout`` to bound it."""
        await self._connected_event.wait()

    async def close(self) -> None:
        """Close the connection and stop reconnecting."""
        self._closing = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        writer = self._writer
        self._drop("closed")
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if writer is not None:
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass

    async def _supervise(self) -> None:
        delay = self._backoff_min
        while not self._closing:
            try:
                await self.connect()
            except PrismConnectionError as err:
                _LOGGER.debug(
                    "Connect to %s:%s failed, retrying in %.1fs: %s",
                    self.host,
                    self.port,
                    delay,
                    err,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._backoff_max)
                continue
            started = time.monotonic()
            await self._disconnected.wait()
            if self._closing:
                break
            if time.monotonic() - started >= _STABLE_CONNECTION_SECONDS:
                delay = self._backoff_min
            _LOGGER.debug("Reconnecting to %s:%s in %.1fs", self.host, self.port, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max)

    def _drop(self, reason: str) -> None:
        """Tear down the transport and fail anything waiting on it."""
        was_connected = self._connected
        self._connected = False
        self._connected_event.clear()
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._reader = None
        if self._reader_task is not None and self._reader_task is not (
            asyncio.current_task()
        ):
            self._reader_task.cancel()
        if self._pending is not None and not self._pending.future.done():
            self._pending.future.set_exception(
                PrismConnectionError(f"Connection lost: {reason}")
            )
        if not self._disconnected.is_set():
            self._disconnected.set()
        if was_connected:
            self.stats.disconnects += 1
            self.stats.last_disconnected = time.time()
            _LOGGER.debug("Disconnected from %s:%s: %s", self.host, self.port, reason)
            if not self._closing:
                # Only report unexpected losses, not close().
                self.stats.last_error = reason
                self._notify_connection(False)

    def _notify_connection(self, connected: bool) -> None:
        if self._on_connection is None:
            return
        try:
            self._on_connection(connected)
        except Exception:
            _LOGGER.exception("Error in connection callback")

    # ------------------------------------------------------------------
    # Reader
    # ------------------------------------------------------------------

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        buffer = ""
        reason = "connection closed by DSP"
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buffer += data.decode("ascii", errors="replace")
                *lines, buffer = _LINE_SPLIT_RE.split(buffer)
                for line in lines:
                    if line:
                        self._handle_line(line)
        except asyncio.CancelledError:
            return
        except OSError as err:
            reason = f"read error: {err!r}"
        if self._reader is reader:
            self._drop(reason)

    def _handle_line(self, line: str) -> None:
        self.stats.lines_received += 1
        line = line.strip()
        pending = self._pending
        if values := parse_values(line):
            self.stats.values_received += len(values)
            if pending is not None and pending.wanted:
                for cn, value in values.items():
                    if cn in pending.wanted:
                        pending.wanted.discard(cn)
                        pending.results[cn] = value
                if not pending.wanted and not pending.future.done():
                    pending.future.set_result(pending.results)
            if self._on_values is not None:
                try:
                    self._on_values(values)
                except Exception:
                    _LOGGER.exception("Error in values callback")
            return

        if line in ("ACK", "NAK"):
            if line == "NAK":
                self.stats.naks += 1
            if pending is not None and not pending.future.done():
                if line == "NAK":
                    pending.future.set_exception(
                        PrismCommandError(f"NAK for {pending.command!r}")
                    )
                    return
                if not pending.wanted:
                    pending.future.set_result({})
                    return
        self.stats.stray_lines += 1
        _LOGGER.debug("Ignoring unsolicited line from %s: %r", self.host, line)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _command(
        self,
        command: str,
        wanted: Iterable[int] = (),
        reply_timeout: float | None = None,
    ) -> dict[int, int | None]:
        """Send one command and wait for its reply.

        For ACK commands (``wanted`` empty) returns ``{}``; for block reads
        returns the values of every wanted control.
        """
        async with self._lock:
            writer = self._writer
            if writer is None or writer.is_closing():
                raise PrismConnectionError("Not connected")
            loop = asyncio.get_running_loop()
            pending = _Pending(command, loop.create_future(), set(wanted))
            self._pending = pending
            try:
                writer.write(f"{command}\r".encode("ascii"))
                self.stats.commands_sent += 1
                async with asyncio.timeout(reply_timeout or self._command_timeout):
                    await writer.drain()
                    return await pending.future
            except TimeoutError as err:
                self.stats.timeouts += 1
                if not pending.wanted:
                    # A late ACK would be matched to the next command, so
                    # resynchronise by reconnecting.
                    self._drop(f"timeout waiting for reply to {command!r}")
                raise PrismTimeoutError(
                    f"No reply to {command!r} from {self.host}"
                ) from err
            except OSError as err:
                self._drop(f"write error: {err!r}")
                raise PrismConnectionError(f"Write failed: {err!r}") from err
            finally:
                self._pending = None
                if pending.future.done() and not pending.future.cancelled():
                    pending.future.exception()

    async def set(self, cn: int, value: int) -> None:
        """Set a control (``CS``); the value is clamped to 0-65535.

        Raises PrismCommandError if the DSP answers ``NAK`` (control not
        assigned).
        """
        _check_control(cn)
        value = max(MIN_VALUE, min(MAX_VALUE, int(value)))
        await self._command(f"CS {cn} {value}")

    async def nop(self) -> None:
        """Send a keepalive."""
        await self._command("NOP")

    async def set_push_interval(self, interval_ms: int) -> None:
        """Set the minimum time between pushes (20-30000 ms)."""
        self._push_interval = interval_ms
        await self._command(f"PUI {int(interval_ms)}")

    async def read_block(self, start: int, count: int) -> dict[int, int | None]:
        """Read ``count`` consecutive controls starting at ``start``.

        Splits into ``GSB2`` requests of at most 256 controls. Controls that
        read ``-0001`` are retried; ones still unassigned are ``None``.
        """
        if count < 1:
            return {}
        _check_control(start)
        _check_control(start + count - 1)
        return await self.read_many(range(start, start + count))

    async def read(self, cn: int) -> int | None:
        """Read one control (``None`` if unassigned)."""
        return (await self.read_block(cn, 1))[cn]

    async def read_many(self, controls: Iterable[int]) -> dict[int, int | None]:
        """Read arbitrary controls, grouped into contiguous block reads."""
        wanted = sorted(set(controls))
        for cn in wanted:
            _check_control(cn)
        results = await self._read_runs(group_runs(wanted))
        for attempt in range(self._read_retries):
            missing = [cn for cn, value in results.items() if value is None]
            if not missing:
                break
            _LOGGER.debug(
                "Retrying %d unassigned controls (attempt %d): %s",
                len(missing),
                attempt + 1,
                missing[:10],
            )
            await asyncio.sleep(self._read_retry_delay)
            results.update(await self._read_runs(group_runs(missing)))
        return results

    async def _read_runs(self, runs: list[tuple[int, int]]) -> dict[int, int | None]:
        results: dict[int, int | None] = {}
        for start, count in runs:
            results.update(
                await self._command(
                    f"GSB2 {start} {count}",
                    wanted=range(start, start + count),
                    reply_timeout=self._command_timeout
                    + count * _BLOCK_TIMEOUT_PER_CONTROL,
                )
            )
        return results
