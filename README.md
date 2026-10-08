# Symetrix Prism for Home Assistant

A Home Assistant integration for [Symetrix](https://www.symetrix.co) Composer DSPs (Prism, and by extension Radius and Edge). Each audio **zone** becomes a `media_player` with power, source, volume and mute. It speaks the Symetrix Composer control protocol over TCP (port 48631) — no cloud, no extra hardware.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=DBorello&repository=ha-symetrix-prism&category=integration)

## Features

- **GUI setup** — add a DSP by IP address; add, edit and delete zones from the integration page. No YAML.
- **A media player per zone** with power, source, volume (set and step) and mute, each on its own device (with an optional area), grouped under the DSP's device.
- **Nothing hard-coded** — control-number layout, on/off values, fader range, volume range, sources and per-zone overrides are all editable in the UI.
- **Safe volume** — the 100 % level is a hard ceiling (0 dB by default) that is never exceeded, and an optional *turn-on cap* lowers a loud zone before switching it on.
- **Local push** — changes made in Composer or on another controller appear in Home Assistant within a second; a periodic full re-read guards against drift.
- **Robust connection handling** — automatic reconnect with backoff, entities unavailable while the DSP is unreachable, diagnostics download.
- **Several DSPs** — add the integration once per DSP.

## Requirements

- Home Assistant **2026.9** or newer.
- A Symetrix Composer DSP reachable on its **control** IP address (on Dante models this is not the Dante interface's address).
- Zone controls assigned to control numbers in Composer, with **Push** enabled — see [Composer setup](#composer-setup).

## Installation

### HACS (recommended)

1. Click the badge above, or in HACS open the ⋮ menu → *Custom repositories* and add `https://github.com/DBorello/ha-symetrix-prism` as type *Integration*.
2. Install **Symetrix Prism** and restart Home Assistant.

### Manual

Copy `custom_components/symetrix_prism/` into your Home Assistant `config/custom_components/` directory and restart.

## Configuration

*Settings → Devices & Services → Add Integration → Symetrix Prism.*

1. Enter a name, the DSP's control IP address and port (default 48631). Leave **Create default zones and sources** ticked to start with two zones and two sources you can rename.
2. The DSP is contacted on submit. If any zone control reads as unassigned you get a warning and can continue anyway.

Afterwards:

- **Configure** — DSP-wide settings (below).
- **Reconfigure** — change the IP address or port; entities keep their IDs.
- **Add zone** / a zone's ⋮ menu — add, edit or delete zones.

### DSP settings (Configure)

| Setting | Meaning |
| --- | --- |
| Sources | One per line as `index: name`; the index is the selector input (0, 1, …). |
| Control numbers | A base per control type; zone *N* uses base + *N*. |
| Button values | Raw values for power on/off and muted/unmuted. Power is usually a selector output mute, so on = 0 and off = 65535. |
| Fader minimum / maximum | dB at raw 0 and 65535, as set on the fader in Composer (default −72 … +12 dB). |
| Volume 0 % / 100 % | dB range of the volume slider (default −72 … 0 dB). Volume is never set above the 100 % level. |
| Volume step | dB per volume up/down. |
| Turn-on volume cap | When a zone that is **off** is turned on, it is first lowered to this level if louder. Blank disables it. |
| Resync / push interval | Full re-read period; minimum time between DSP pushes. |

### Zones

| Field | Meaning |
| --- | --- |
| Name, zone number | Zone number 1–100; control numbers are the type base + this number. |
| Controls | Untick power, source, volume or mute to leave that feature out. |
| Sources | Subset of sources this zone may select (none selected = all). |
| Area | Area for the zone's device. Applied when the zone is created and whenever you change it here; an area set on the device page is kept. |
| Overrides | Explicit control number for any control, and per-zone volume range and turn-on cap. |

## Composer setup

Per zone, the DSP needs a **Matrix Selector output** (its Mute is the zone's power, its input selection the source) feeding a **Gain module** (Master Fader = volume, Master Mute = mute), each control with a control number and **Push** enabled. With the default layout:

| Control | Composer control | Control number | Values |
| --- | --- | --- | --- |
| Power | Stereo Matrix Selector → Output *n* Mute | 2000 + zone | 65535 = off, 0 = on |
| Source | Stereo Matrix Selector → Output *n* input source | 2100 + zone | input index (0, 1, …) |
| Volume | Zone Gain → Master Fader | 2200 + zone | fader −72 … +12 dB |
| Mute | Zone Gain → Master Mute | 2300 + zone | 65535 = muted |

**See [docs/composer-setup.md](docs/composer-setup.md)** for the full signal flow, module settings, how to assign control numbers and enable push, and a checklist.

## Entities

| Entity | Where | What |
| --- | --- | --- |
| `media_player.<zone>` | zone device | Power, source, volume, mute. Attributes `zone` and `volume_db`. |

## Lutron Pico remotes

[![Open your Home Assistant instance and show the blueprint import dialog.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FDBorello%2Fha-symetrix-prism%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fpico_media_button.yaml)

The [Lutron Pico media button](blueprints/automation/pico_media_button.yaml) blueprint turns a Pico's raise/lower buttons into zone controls. Create one automation per button:

| Gesture | Raise | Lower |
| --- | --- | --- |
| Tap | Volume up — or turn on / unmute if off / muted | Volume down — or turn on / unmute |
| Hold | Volume keeps rising until released | Volume keeps falling until released |
| Double tap | Next source | Power off |

Releases are triggers (restart mode), so a hold always stops when the button is let go, and a hold is capped at a number of steps in case a release is lost. A tap acts after a short double-tap window (0.6 s by default), so a double tap never nudges the volume first. Timing is adjustable in the blueprint's *Timing* section. Works with any `media_player`, not just Symetrix zones.

## Notes and limitations

- The state is `on`/`off`; there is no play/pause or track metadata (the DSP routes and levels audio, it doesn't play it).
- The protocol exposes no serial number or MAC, so a DSP is identified by its address; *Reconfigure* keeps entities when the address changes.
- Turning a zone off only changes its power control — source, volume and mute are left as they were.
- Debug logs: enable debug logging on the integration page. *Download diagnostics* includes connection statistics and the last values read.

## Tools

- `tools/prism_test.py --host <ip> --read-only` — decode every zone control on a real DSP. Also `--listen`, `--probe CN…`, and (no flags) a write/verify/restore cycle that never raises volume.
- `tools/mock_prism.py` — a mock DSP for offline development and the tests.

## Development

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python pytest-homeassistant-custom-component mypy pytest-cov
.venv/bin/python -m pytest
.venv/bin/python -m mypy
ruff check . && ruff format --check .
```

## License

MIT
