"""Tests for the Symetrix protocol client against the mock DSP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import socket

import pytest

from custom_components.symetrix_prism.protocol import (
    MAX_VALUE,
    PrismClient,
    PrismCommandError,
    PrismConnectionError,
    PrismTimeoutError,
    group_runs,
    parse_values,
)
from tools.mock_prism import MockPrism


@pytest.fixture(autouse=True)
def allow_sockets(socket_enabled: None) -> None:
    """The client talks to the mock DSP over localhost TCP."""


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait until predicate() is true."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def free_port() -> int:
    """Return a TCP port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class Recorder:
    """Collects client callbacks."""

    def __init__(self) -> None:
        self.values: dict[int, int | None] = {}
        self.batches: list[dict[int, int | None]] = []
        self.connection: list[bool] = []

    def on_values(self, values: dict[int, int | None]) -> None:
        self.values.update(values)
        self.batches.append(values)

    def on_connection(self, connected: bool) -> None:
        self.connection.append(connected)


@pytest.fixture
async def mock() -> AsyncIterator[MockPrism]:
    prism = MockPrism(push_interval_ms=20)
    await prism.start()
    yield prism
    await prism.stop()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def make_client(port: int, recorder: Recorder, **kwargs: float) -> PrismClient:
    options: dict[str, float] = {
        "command_timeout": 1.0,
        "read_retry_delay": 0.01,
        "backoff_min": 0.05,
        "backoff_max": 0.2,
    }
    options.update(kwargs)
    return PrismClient(
        "127.0.0.1",
        port,
        on_values=recorder.on_values,
        on_connection=recorder.on_connection,
        **options,  # type: ignore[arg-type]
    )


@pytest.fixture
async def client(mock: MockPrism, recorder: Recorder) -> AsyncIterator[PrismClient]:
    prism_client = make_client(mock.port, recorder)
    await prism_client.connect()
    yield prism_client
    await prism_client.close()


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("#02201=55847", {2201: 55847}),
        ("#02301=-0001", {2301: None}),
        ("#02201=00010#02202=65535", {2201: 10, 2202: 65535}),
        ("#02201=00010 #02202=00020", {2201: 10, 2202: 20}),
        ("2201 0", {2201: 0}),
        ("ACK", {}),
        ("NAK", {}),
        ("garbage = 12", {}),
    ],
)
def test_parse_values(line: str, expected: dict[int, int | None]) -> None:
    assert parse_values(line) == expected


def test_group_runs() -> None:
    assert group_runs([2102, 2001, 2002, 2101, 2001]) == [(2001, 2), (2101, 2)]
    assert group_runs([]) == []
    runs = group_runs(range(1, 601))
    assert runs == [(1, 256), (257, 256), (513, 88)]


# ----------------------------------------------------------------------
# Connecting
# ----------------------------------------------------------------------


async def test_connect_sends_pue_first(
    mock: MockPrism, client: PrismClient, recorder: Recorder
) -> None:
    assert client.connected
    assert mock.received[0] == "PUE"
    assert recorder.connection == [True]
    assert client.stats.connects == 1


async def test_connect_sets_push_interval(mock: MockPrism, recorder: Recorder) -> None:
    prism_client = make_client(mock.port, recorder)
    prism_client._push_interval = 250
    await prism_client.connect()
    assert mock.received[:2] == ["PUE", "PUI 250"]
    await prism_client.close()


async def test_connect_failure_raises(recorder: Recorder) -> None:
    prism_client = make_client(free_port(), recorder)
    with pytest.raises(PrismConnectionError):
        await prism_client.connect()
    assert not prism_client.connected
    assert prism_client.stats.connect_failures == 1
    assert recorder.connection == []


async def test_commands_require_connection(recorder: Recorder) -> None:
    prism_client = make_client(free_port(), recorder)
    with pytest.raises(PrismConnectionError):
        await prism_client.set(2201, 0)


# ----------------------------------------------------------------------
# Setting values
# ----------------------------------------------------------------------


async def test_set_acked(mock: MockPrism, client: PrismClient) -> None:
    await client.set(2201, 24966)
    assert mock.values[2201] == 24966
    assert "CS 2201 24966" in mock.received


@pytest.mark.parametrize(("value", "sent"), [(70000, MAX_VALUE), (-5, 0)])
async def test_set_clamps_value(
    mock: MockPrism, client: PrismClient, value: int, sent: int
) -> None:
    await client.set(2301, value)
    assert mock.commands("CS")[-1] == f"CS 2301 {sent}"


async def test_set_nak_raises(mock: MockPrism, client: PrismClient) -> None:
    with pytest.raises(PrismCommandError):
        await client.set(2999, 1)
    # The connection stays usable after a NAK.
    await client.set(2201, 100)
    assert mock.values[2201] == 100


@pytest.mark.parametrize("cn", [0, 10001])
async def test_set_rejects_bad_control(client: PrismClient, cn: int) -> None:
    with pytest.raises(ValueError, match="outside"):
        await client.set(cn, 0)


async def test_slow_ack(mock: MockPrism, client: PrismClient) -> None:
    mock.ack_delay = 0.05
    await client.set(2201, 500)
    assert mock.values[2201] == 500


async def test_concurrent_sets_are_serialised(
    mock: MockPrism, client: PrismClient
) -> None:
    mock.ack_delay = 0.005
    await asyncio.gather(*(client.set(2201, value) for value in range(10)))
    assert mock.commands("CS") == [f"CS 2201 {value}" for value in range(10)]
    assert mock.values[2201] == 9


async def test_selector_clamps_to_last_input(
    mock: MockPrism, client: PrismClient
) -> None:
    await client.set(2101, 7)
    assert mock.values[2101] == 1


async def test_ack_timeout_drops_connection(
    mock: MockPrism, recorder: Recorder
) -> None:
    prism_client = make_client(mock.port, recorder, command_timeout=0.1)
    await prism_client.connect()
    mock.ack_delay = 0.3
    with pytest.raises(PrismTimeoutError):
        await prism_client.set(2201, 1)
    # A late ACK must not be matched to a later command: reconnect instead.
    assert not prism_client.connected
    assert recorder.connection == [True, False]
    assert prism_client.stats.timeouts == 1
    await prism_client.close()


# ----------------------------------------------------------------------
# Reading values
# ----------------------------------------------------------------------


async def test_read_block(mock: MockPrism, client: PrismClient) -> None:
    mock.values[2201] = 40569
    assert await client.read_block(2201, 2) == {2201: 40569, 2202: 9362}
    assert "GSB2 2201 2" in mock.received


async def test_read_single(mock: MockPrism, client: PrismClient) -> None:
    assert await client.read(2201) == 9362


async def test_read_unassigned_returns_none_after_retries(
    mock: MockPrism, client: PrismClient
) -> None:
    result = await client.read_block(2301, 3)
    assert result == {2301: 0, 2302: 0, 2303: None}
    # Initial block read plus three retries of just the missing control.
    assert mock.commands("GSB2") == ["GSB2 2301 3"] + ["GSB2 2303 1"] * 3


async def test_read_slow_unassigned(mock: MockPrism, client: PrismClient) -> None:
    mock.unassigned_delay = 0.1
    assert await client.read(2999) is None


async def test_read_block_chunks_at_256(
    recorder: Recorder,
) -> None:
    prism = MockPrism({cn: cn for cn in range(1, 601)}, push_interval_ms=20)
    await prism.start()
    prism_client = make_client(prism.port, recorder)
    await prism_client.connect()
    try:
        result = await prism_client.read_block(1, 600)
        assert result == {cn: cn for cn in range(1, 601)}
        assert prism.commands("GSB2") == [
            "GSB2 1 256",
            "GSB2 257 256",
            "GSB2 513 88",
        ]
    finally:
        await prism_client.close()
        await prism.stop()


async def test_read_many_groups_runs(mock: MockPrism, client: PrismClient) -> None:
    result = await client.read_many([2001, 2002, 2201, 2202, 2301])
    assert result == {2001: 0, 2002: 0, 2201: 9362, 2202: 9362, 2301: 0}
    assert mock.commands("GSB2") == ["GSB2 2001 2", "GSB2 2201 2", "GSB2 2301 1"]


async def test_transient_selector_unassigned_is_retried(
    recorder: Recorder,
) -> None:
    prism = MockPrism(transient_reads=2, push_interval_ms=20)
    prism.values[2101] = 1
    await prism.start()
    prism_client = make_client(prism.port, recorder)
    await prism_client.connect()
    try:
        assert await prism_client.read_block(2101, 2) == {2101: 1, 2102: 0}
        # Transient again after a change.
        await prism_client.set(2102, 1)
        assert await prism_client.read(2102) == 1
    finally:
        await prism_client.close()
        await prism.stop()


async def test_read_nak_raises(mock: MockPrism, client: PrismClient) -> None:
    with pytest.raises(PrismCommandError):
        await client._command("GSB2 1 300", wanted=range(1, 301))
    assert await client.read(2201) == 9362


async def test_read_rejects_bad_range(client: PrismClient) -> None:
    with pytest.raises(ValueError, match="outside"):
        await client.read_block(9990, 20)
    assert await client.read_block(2201, 0) == {}


# ----------------------------------------------------------------------
# Push and unsolicited lines
# ----------------------------------------------------------------------


async def test_push_updates_reported(
    mock: MockPrism, client: PrismClient, recorder: Recorder
) -> None:
    mock.set_value(2201, 56173)
    await until(lambda: recorder.values.get(2201) == 56173)


async def test_combined_push_lines(
    mock: MockPrism, client: PrismClient, recorder: Recorder
) -> None:
    mock.combine_push = True
    mock.set_value(2201, 111)
    mock.set_value(2202, 222)
    mock.set_value(2001, 65535)
    await until(lambda: len(recorder.values) >= 3)
    assert recorder.values == {2201: 111, 2202: 222, 2001: 65535}
    assert {2201: 111, 2202: 222, 2001: 65535} in recorder.batches


async def test_push_echo_of_own_set(
    mock: MockPrism, client: PrismClient, recorder: Recorder
) -> None:
    await client.set(2301, 65535)
    await until(lambda: recorder.values.get(2301) == 65535)


async def test_push_only_after_a_command(mock: MockPrism) -> None:
    """The mock reproduces the real DSP: no push until a command is sent."""
    reader, writer = await asyncio.open_connection("127.0.0.1", mock.port)
    try:
        await until(lambda: len(mock.clients) == 1)
        mock.set_value(2201, 1)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.15):
                await reader.readuntil(b"\r")
        writer.write(b"NOP\r")
        await writer.drain()
        assert await reader.readuntil(b"\r") == b"ACK\r"
        mock.set_value(2201, 2)
        line = await asyncio.wait_for(reader.readuntil(b"\r"), 1)
        assert parse_values(line.decode()) == {2201: 2}
    finally:
        writer.close()


async def test_push_during_read_and_stray_lines(
    mock: MockPrism, client: PrismClient, recorder: Recorder
) -> None:
    # Unsolicited lines while idle must not desynchronise later commands.
    await mock.inject("hello\rACK\r\n#02999=00042\r")
    await until(lambda: recorder.values.get(2999) == 42)
    assert client.stats.stray_lines == 2
    with pytest.raises(PrismCommandError):
        await client.set(2999, 1)
    await client.set(2201, 7)
    assert mock.values[2201] == 7

    # A push for a requested control satisfies a block read; others pass on.
    # 2203 is unassigned, so the mock holds the reply back like a real Prism.
    mock.unassigned_delay = 0.1
    read = asyncio.create_task(client.read_block(2201, 3))
    await asyncio.sleep(0.05)
    await mock.inject("#02202=00033\r#02001=00009\r")
    result = await read
    assert result == {2201: 7, 2202: 33, 2203: None}
    assert recorder.values[2001] == 9


async def test_split_lines_across_reads(
    mock: MockPrism, client: PrismClient, recorder: Recorder
) -> None:
    await mock.inject("#022")
    await asyncio.sleep(0.02)
    await mock.inject("01=01234\r")
    await until(lambda: recorder.values.get(2201) == 1234)


async def test_callback_errors_are_contained(
    mock: MockPrism, recorder: Recorder
) -> None:
    def broken(values: dict[int, int | None]) -> None:
        raise RuntimeError("boom")

    prism_client = PrismClient("127.0.0.1", mock.port, on_values=broken)
    await prism_client.connect()
    try:
        assert await prism_client.read(2201) == 9362
    finally:
        await prism_client.close()


# ----------------------------------------------------------------------
# Reconnecting
# ----------------------------------------------------------------------


async def test_reconnects_after_drop(mock: MockPrism, recorder: Recorder) -> None:
    prism_client = make_client(mock.port, recorder)
    await prism_client.start()
    try:
        async with asyncio.timeout(2):
            await prism_client.wait_connected()
        await mock.drop_clients()
        await until(lambda: recorder.connection == [True, False, True])
        assert mock.connections == 2
        assert await prism_client.read(2201) == 9362
        assert prism_client.stats.disconnects == 1
    finally:
        await prism_client.close()


async def test_pending_command_fails_on_drop(
    mock: MockPrism, client: PrismClient
) -> None:
    mock.ack_delay = 0.5
    command = asyncio.create_task(client.set(2201, 1))
    await asyncio.sleep(0.05)
    await mock.drop_clients()
    with pytest.raises(PrismConnectionError):
        await command


async def test_backoff_until_dsp_appears(recorder: Recorder) -> None:
    port = free_port()
    prism_client = make_client(port, recorder)
    await prism_client.start()
    prism = MockPrism()
    try:
        await asyncio.sleep(0.3)
        assert not prism_client.connected
        assert prism_client.stats.connect_failures >= 2
        await prism.start(port=port)
        async with asyncio.timeout(2):
            await prism_client.wait_connected()
        assert recorder.connection == [True]
    finally:
        await prism_client.close()
        await prism.stop()


async def test_close_stops_reconnecting(mock: MockPrism, recorder: Recorder) -> None:
    prism_client = make_client(mock.port, recorder)
    await prism_client.start()
    async with asyncio.timeout(2):
        await prism_client.wait_connected()
    await prism_client.close()
    assert not prism_client.connected
    await asyncio.sleep(0.2)
    assert mock.connections == 1
    assert recorder.connection == [True]
    with pytest.raises(PrismConnectionError):
        await prism_client.set(2201, 1)
