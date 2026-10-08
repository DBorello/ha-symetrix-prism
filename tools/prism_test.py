#!/usr/bin/env python3
"""Command-line tester for a Symetrix DSP using the integration's client.

Examples::

    tools/prism_test.py --host 192.168.1.50 --read-only  # decode zone controls
    tools/prism_test.py --host 192.168.1.50 --listen --duration 60
    tools/prism_test.py --host 192.168.1.50 --probe 2201 2301
    tools/prism_test.py --host 192.168.1.50   # full write cycle, then restore

The full cycle toggles power and mute, cycles sources and steps the volume
DOWN from its current level, checking each read-back, then restores every
original value. A volume raw above the ceiling (0 dB = 56173 by default) is
never sent.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.symetrix_prism.protocol import (
    PrismClient,
    PrismError,
)

FADER_MIN_DB = -72.0
FADER_MAX_DB = 12.0


def raw_to_db(raw: int) -> float:
    """Convert a raw fader value to dB (-72..+12 fader)."""
    return FADER_MIN_DB + raw * (FADER_MAX_DB - FADER_MIN_DB) / 65535


def db_to_raw(db: float) -> int:
    """Convert dB to a raw fader value (-72..+12 fader)."""
    return round((db - FADER_MIN_DB) * 65535 / (FADER_MAX_DB - FADER_MIN_DB))


class Tester:
    """Runs the checks against one DSP."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.failures = 0
        self.client = PrismClient(args.host, args.port, on_values=self._on_values)
        self.listening = False
        self.volume_cns: set[int] = set()

    def _on_values(self, values: dict[int, int | None]) -> None:
        if self.listening:
            stamp = time.strftime("%H:%M:%S")
            for cn, value in values.items():
                print(f"{stamp}  {cn:5d} = {self.describe(cn, value)}")

    def controls(self, zone: int) -> dict[str, int]:
        power, source, volume, mute = self.args.bases
        return {
            "power": power + zone,
            "source": source + zone,
            "volume": volume + zone,
            "mute": mute + zone,
        }

    def describe(self, cn: int, value: int | None) -> str:
        if value is None:
            return "-0001 (unassigned)"
        power, source, volume, mute = self.args.bases
        kind = (cn // 100) * 100
        if kind == volume:
            return f"{value:5d}  ({raw_to_db(value):+.1f} dB)"
        if kind == power:
            return f"{value:5d}  ({'off' if value >= 32768 else 'on'})"
        if kind == mute:
            return f"{value:5d}  ({'muted' if value >= 32768 else 'unmuted'})"
        if kind == source:
            return f"{value:5d}  (input index)"
        return f"{value:5d}"

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
        if not ok:
            self.failures += 1

    async def read_only(self) -> None:
        for zone in self.args.zones:
            print(f"Zone {zone}")
            controls = self.controls(zone)
            values = await self.client.read_many(controls.values())
            for name, cn in controls.items():
                print(f"  {name:6s} {cn:5d} = {self.describe(cn, values[cn])}")

    async def probe(self) -> None:
        values = await self.client.read_many(self.args.probe)
        for cn, value in sorted(values.items()):
            print(f"{cn:5d} = {self.describe(cn, value)}")

    async def listen(self) -> None:
        print(f"Listening for pushes for {self.args.duration}s (Ctrl-C to stop)")
        self.listening = True
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self.args.duration):
                await asyncio.Event().wait()

    async def write_and_verify(self, label: str, cn: int, value: int) -> None:
        if cn in self.volume_cns and value > self.args.max_raw:
            raise SystemExit(
                f"Refusing to send {value} to {cn} (> {self.args.max_raw})"
            )
        await self.client.set(cn, value)
        await asyncio.sleep(self.args.settle)
        read = await self.client.read(cn)
        self.check(label, read == value, f"(wrote {value}, read {read})")

    async def full_cycle(self) -> None:
        self.volume_cns = {self.controls(z)["volume"] for z in self.args.zones}
        for zone in self.args.zones:
            controls = self.controls(zone)
            original = await self.client.read_many(controls.values())
            print(f"Zone {zone}: original {original}")
            if any(v is None for v in original.values()):
                self.check(f"zone {zone} controls assigned", False, str(original))
                continue
            try:
                power, mute = controls["power"], controls["mute"]
                await self.write_and_verify("power off", power, 65535)
                await self.write_and_verify("power on", power, 0)
                await self.write_and_verify("mute", mute, 65535)
                await self.write_and_verify("unmute", mute, 0)
                for index in range(self.args.sources):
                    await self.write_and_verify(
                        f"source {index}", controls["source"], index
                    )
                current = min(original[controls["volume"]] or 0, self.args.max_raw)
                for step in (2, 4):
                    target = max(0, db_to_raw(raw_to_db(current) - step))
                    await self.write_and_verify(
                        f"volume -{step} dB", controls["volume"], target
                    )
            finally:
                for cn, value in original.items():
                    if value is not None:
                        await self.client.set(cn, value)
                await asyncio.sleep(self.args.settle)
                restored = await self.client.read_many(controls.values())
                self.check(f"zone {zone} restored", restored == original, str(restored))

    async def run(self) -> int:
        await self.client.connect()
        try:
            if self.args.probe:
                await self.probe()
            elif self.args.read_only:
                await self.read_only()
            elif self.args.listen:
                await self.listen()
            else:
                await self.full_cycle()
        finally:
            await self.client.close()
        if self.failures:
            print(f"{self.failures} check(s) failed")
            return 1
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True, help="DSP control IP")
    parser.add_argument("--port", type=int, default=48631)
    parser.add_argument("--zones", type=int, nargs="+", default=[1, 2])
    parser.add_argument(
        "--bases",
        type=int,
        nargs=4,
        default=[2000, 2100, 2200, 2300],
        metavar=("POWER", "SOURCE", "VOLUME", "MUTE"),
    )
    parser.add_argument("--sources", type=int, default=2, help="number of inputs")
    parser.add_argument("--max-raw", type=int, default=56173, help="volume ceiling")
    parser.add_argument("--settle", type=float, default=0.2)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--listen", action="store_true")
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--probe", type=int, nargs="+", metavar="CN")
    args = parser.parse_args()
    try:
        return asyncio.run(Tester(args).run())
    except PrismError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
