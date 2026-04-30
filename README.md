# tm3diag

Tesla Model 3 diagnostics tools for CAN

> **Untested against real hardware**
> These tools have been developed and validated via dry-run and static analysis only. They have not yet been exercised against a live CAN network or real ECUs. Use with caution — bugs in the flash sequence or UDS session handling could leave an ECU in an unrecoverable state.

## Tools

### `tm3diag.py` — Interactive diagnostic terminal

Interactive terminal for exploring ECU state. When run without `--node`, opens a pre-connection menu where you can scan the bus for live nodes before connecting. Once connected, reads identity on startup then lets you read DIDs by name (with tab completion), run routines by name or hex ID, switch sessions, and trigger a firmware update — all in one session.

```
python tm3diag.py --channel vcan0                          # opens pre-connection menu (scan / connect)
python tm3diag.py --node PCS --channel vcan0
python tm3diag.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
```

**Options**

| Flag                | Default             | Description                                                       |
| ------------------- | ------------------- | ----------------------------------------------------------------- |
| `--node`, `-n`      | —                   | ECU node name. Opens pre-connection menu if omitted.              |
| `--channel`, `-c`   | `TM3_CHANNEL`       | CAN interface                                                     |
| `--interface`, `-i` | `TM3_INTERFACE`     | python-can interface type                                         |
| `--artifacts`, `-a` | `TM3_ARTIFACTS_DIR` | Path to `seed_artifacts_v2` (needed for DFU; prompted if missing) |

**Pre-connection commands** (shown when `--node` is omitted)

| Command          | Description                                        |
| ---------------- | -------------------------------------------------- |
| `scan`           | Probe all known nodes on the bus for TesterPresent |
| `connect <node>` | Connect to a node by name                          |
| `quit`           | Exit                                               |

**Connected commands**

| Command       | Description                                                                        |
| ------------- | ---------------------------------------------------------------------------------- |
| `dids`        | Read DIDs by name (tab complete) or hex ID; auto-decodes fields from ODJ           |
| `routine`     | Run a routine by name or hex ID (see named routines below)                         |
| `board-parts` | Read board part/serial DIDs `0xF012`–`0xF015`, `0xF030`/`0xF031` (opcode 14)       |
| `clear-dtc`   | ClearDiagnosticInformation group `0xFFFFFF` (UDS 0x14)                             |
| `dfu`         | Full firmware update using `dfu.py` phases (identity → select → preflight → flash) |
| `session`     | Switch diagnostic session                                                          |
| `reset`       | ECU hard reset                                                                     |
| `quit`        | Disconnect and exit                                                                |

**Named routines** (from hashpicker_sim VM opcode table)

| Name                       | Routine ID | Description                                                      |
| -------------------------- | ---------- | ---------------------------------------------------------------- |
| `erase`                    | `0xFF00`   | `initializeEraseModule` — EraseMemory (requires security access) |
| `verify-crc`               | `0x0201`   | `checkModuleProgrammedCorrectly` — CRC verify                    |
| `check-component`          | `0x0202`   | `checkCorrectComponentAndRev`                                    |
| `ota-wait`                 | `0x0540`   | `vcWaitForOTAMode` / `otaStateRoutineControl`                    |
| `ibst-power`               | `0x0543`   | `ibstPowerControl` (requires security access)                    |
| `bms-contactor-close`      | `0x0204`   | `bmsContactorControl` — close contactor                          |
| `bms-contactor-open`       | `0x0304`   | `bmsContactorControl` — open contactor                           |
| `disable-intrusion-sensor` | `0x0601`   | `disableIntrusionSensor`                                         |

---

### `tm3uds.py` — UDS diagnostic CLI

General-purpose UDS client for reading/writing DIDs, running routines, managing sessions, and scanning the network.

```
python tm3uds.py scan --channel vcan0
python tm3uds.py --node PCS --channel vcan0 read-did BOOTLOADER_VERSION
python tm3uds.py --node PCS --channel vcan0 read-did 0xF180
python tm3uds.py --node PCS --channel vcan0 write-did 0x0102 deadbeef
python tm3uds.py --node PCS --channel vcan0 routine 0xFF00 01
python tm3uds.py --node CP  --channel vcan0 security-access
python tm3uds.py --node PCS --channel vcan0 session programming
python tm3uds.py --node PCS --channel vcan0 reset
```

**Subcommands**

| Command                 | Description                                                                           |
| ----------------------- | ------------------------------------------------------------------------------------- |
| `scan`                  | Probe all nodes for a TesterPresent response                                          |
| `read-did <did>`        | Read a DID by name or hex ID (UDS 0x22)                                               |
| `write-did <did> <hex>` | Write a DID (UDS 0x2E)                                                                |
| `routine <id> [arg]`    | Execute a RoutineControl (UDS 0x31)                                                   |
| `security-access`       | Enter programming session and complete seed/key exchange                              |
| `session <mode>`        | Switch diagnostic session (`default`, `programming`, `extended`, `safety`, or `0xNN`) |
| `reset`                 | Send ECU hard reset (UDS 0x11 01)                                                     |
| `clear-dtc`             | ClearDiagnosticInformation group `0xFFFFFF` (UDS 0x14)                                |

**Options**

| Flag                | Default         | Description                             |
| ------------------- | --------------- | --------------------------------------- |
| `--node`, `-n`      | —               | ECU node name (e.g. `PCS`, `CP`, `RCM`) |
| `--channel`, `-c`   | `TM3_CHANNEL`   | CAN interface                           |
| `--interface`, `-i` | `TM3_INTERFACE` | python-can interface type               |

---

### `dfu.py` — Firmware flash CLI

Flashes firmware to an ECU using the correct ECU-specific UDS programming sequence: identity discovery → firmware selection → pre-flight verification → flash. The flash sequence is selected automatically from `flash_scripts.py` based on the ECU type reported in `signed_metadata_map.tsv`, covering all 21 script variants reverse-engineered from `hashpicker_sim`.

```
python dfu.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
python dfu.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2 --force
```

**Options**

| Flag                | Default             | Description                             |
| ------------------- | ------------------- | --------------------------------------- |
| `--node`, `-n`      | —                   | ECU node name                           |
| `--channel`, `-c`   | `TM3_CHANNEL`       | CAN interface                           |
| `--interface`, `-i` | `TM3_INTERFACE`     | python-can interface type               |
| `--artifacts`, `-a` | `TM3_ARTIFACTS_DIR` | Path to `seed_artifacts_v2` directory   |
| `--force`           | —                   | Proceed despite BHX identity mismatches |

The tool reads `signed_metadata_map.tsv` from the artifacts directory to select the correct firmware file for the connected ECU's identity (PCBA_ID / ASSEMBLY_ID / USAGE_ID).

---

### `bhx.py` — BHX firmware image library

Parser and builder for the Tesla BHX firmware container format. Can be used as a library or run directly.

```
python bhx.py info   firmware.bhx
python bhx.py extract firmware.bhx [output_dir]
python bhx.py create  out.bhx 0x88000 segment.bin
```

**As a library**

```python
import bhx

# Parse
bhx_file = bhx.parse_file("firmware.bhx")
for seg in bhx_file.segments:
    print(f"addr=0x{seg.start_address:08X} len={seg.length} crc_ok={seg.checksum == seg.compute_crc32()}")

# Build
bhx_file = bhx.from_binary_segments([(0x88000, data)])
bhx.build_file(bhx_file, "out.bhx")
```

---

### `pcs_send.py` — Interactive PCS CAN scripting shell

Interactive Python REPL (IPython if available, otherwise stdlib) for experimenting with Tesla PCS CAN control messages. Runs alongside `can_live.py` on the same interface — frames you send appear in the live viewer in real time.

```
python pcs_send.py
python pcs_send.py --channel can0
```

**Options**

| Flag          | Default     | Description               |
| ------------- | ----------- | ------------------------- |
| `--channel`   | `vcan0`     | CAN interface             |
| `--interface` | `socketcan` | python-can interface type |
| `--bitrate`   | —           | CAN bitrate (optional)    |

**Available functions**

| Function                           | Frame   | Description                                                  |
| ---------------------------------- | ------- | ------------------------------------------------------------ |
| `pcs_mode(mode, hv_voltage)`       | `0x22A` | Set PCS mode: `'off'`, `'charge'`, `'dcdc'`, `'both'`        |
| `charger_enable(current_a)`        | `0x13D` | Enable charger with AC current limit                         |
| `charger_disable()`                | `0x13D` | Disable charger                                              |
| `charge_power(watts, on)`          | `0x2B2` | Charge power request in watts                                |
| `dcdc_voltage(volts)`              | `0x3A1` | DCDC output voltage setpoint                                 |
| `evse_limit(current_a)`            | `0x21D` | EVSE current limit                                           |
| `bms_heartbeat()`                  | `0x3B2` | One BMS keepalive frame                                      |
| `vcfront_heartbeat()`              | `0x545` | One VCFront keepalive frame (with counter + checksum)        |
| `raw(can_id, data)`                | any     | Send an arbitrary frame                                      |
| `start_heartbeats()`               | —       | Background BMS@10ms + VCFront@50ms keepalives                |
| `stop_heartbeats()`                | —       | Stop background heartbeats                                   |
| `start_listener(node)`             | —       | Print decoded incoming frames to terminal (default: `'PCS'`) |
| `stop_listener()`                  | —       | Stop listener                                                |
| `send_loop(name, fn, interval_ms)` | —       | Repeat any callable at a fixed interval                      |
| `stop_loop(name)`                  | —       | Stop a named loop                                            |
| `list_loops()`                     | —       | Show active loops                                            |

**`DEFAULTS` dict** — tune once, all helpers pick it up:

```python
DEFAULTS['hv_voltage']   = 400    # V
DEFAULTS['ac_limit']     = 32     # A
DEFAULTS['dcdc_voltage'] = 13.8   # V
DEFAULTS['charge_power'] = 0      # W
DEFAULTS['evse_limit']   = 32     # A
```

The PCS requires `0x3B2` (BMS) and `0x545` (VCFront) heartbeats at fixed rates or it raises `bmsMia`/`vcfrontMia` faults. Call `start_heartbeats()` before sending any control frames.

---

### `compact_to_dbc.py` — Convert compact JSON to DBC

Converts `Model3_ETH.compact.json` to a standard DBC file for use in tools like CANdb++, Vector CANalyzer, Cangaroo, or SavvyCAN.

```
python compact_to_dbc.py                          # data/Model3_ETH.compact.json -> Model3_ETH.dbc
python compact_to_dbc.py input.json output.dbc
```

The output DBC includes:

- All messages (`BO_`) with correct ID, length, and sender
- All signals (`SG_`) with start bit, width, byte order, sign, scale, offset, min/max, units, and receivers
- Multiplexed signals — muxer (`M`) and muxed (`mN`) indicators
- Value descriptions (`VAL_`) for enum signals
- Cycle-time attributes (`BA_ "GenMsgCycleTime"`) for periodic messages

Both little-endian (Intel) and big-endian (Motorola) signals are handled correctly.

---

### `can_live.py` — Live CAN signal viewer

Web-based live viewer that decodes CAN frames in real time using `Model3_ETH.compact.json`. Select an ECU node from the sidebar to see all its signals updating live, with age indicators and enum labels.

```
python can_live.py --channel vcan0
python can_live.py --channel vcan0 --interface socketcan --port 8765
```

Opens `http://localhost:8765` automatically in your browser.

**Options**

| Flag           | Default         | Description                      |
| -------------- | --------------- | -------------------------------- |
| `--channel`    | `TM3_CHANNEL`   | CAN interface                    |
| `--interface`  | `TM3_INTERFACE` | python-can interface type        |
| `--bitrate`    | `TM3_BITRATE`   | CAN bitrate (optional)           |
| `--port`       | `8765`          | HTTP/WebSocket port              |
| `--no-browser` | —               | Don't auto-open browser on start |

**Features**

- Node sidebar — all 39 ECU nodes listed; click to subscribe
- Live signal table — per-message sections with signal name, value, unit, and enum label
- Value flash — cells briefly highlight when a value changes
- Age indicator — shows time since last frame per message; turns red when stale (>1 s)
- Signal filter — type to narrow signals by name
- FPS counter — shows decoded frames/second in the top bar

**Signal decoding** handles little-endian and big-endian bit packing, multiplexed signals (`mux_id`), `value_description` enum mappings, and signed/unsigned scale+offset.

---

## Configuration

All tools read defaults from a `.env` file in the project root, so you don't have to repeat `--channel can0` on every command. Copy the example and edit:

```
cp .env.example .env
```

```sh
# CAN interface
TM3_CHANNEL=can0
TM3_INTERFACE=socketcan
# TM3_BITRATE=500000

# Data files (defaults to data/ in the project root)
# TM3_NODES_JSON=/path/to/nodes.json
# TM3_ETH_COMPACT=/path/to/Model3_ETH.compact.json
# TM3_ODJ_DIR=/path/to/odj

# Firmware artifacts
# TM3_ARTIFACTS_DIR=/path/to/seed_artifacts_v2
```

CLI flags always override `.env` values. `.env` is gitignored.

## Data files

Node configurations live in `data/`:

- `nodes.json` — CAN IDs, security parameters, and ODJ sources for each ECU
- `Model3_ETH.compact.json` — CAN message ID map
- `odj/` — Per-ECU DID definitions (name, hex ID, read/write sizes, security level)

## Requirements

```
pip install -r requirements.txt
```

Requires Python 3.10+, `python-can`, `python-dotenv`, `aiohttp`, and a SocketCAN interface (real or virtual via `vcan`).

## Tests

```
pytest tests/ -v
```

Tests require the `seed_artifacts_v2` firmware artifact directory. Tests that depend on it are automatically skipped when the path is absent.
