# tesla-m3-diagnostics

> **Untested against real hardware**
> These tools have been developed and validated via dry-run and static analysis only. They have not yet been exercised against a live CAN network or real ECUs. Use with caution — bugs in the flash sequence or UDS session handling could leave an ECU in an unrecoverable state.

Tesla Model 3 diagnostics tools for CAN

## Tools

### `diag_tool.py` — Interactive diagnostic terminal

Interactive terminal for exploring ECU state. Connects to a node, reads identity on startup, then lets you read DIDs by name (with tab completion), run routines by name or hex ID, switch sessions, and trigger a firmware update — all in one session.

```
python diag_tool.py --channel vcan0                          # picks node interactively
python diag_tool.py --node PCS --channel vcan0
python diag_tool.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
```

**Options**

| Flag                | Default     | Description                                                       |
| ------------------- | ----------- | ----------------------------------------------------------------- |
| `--node`, `-n`      | —           | ECU node name. Prompted interactively if omitted.                 |
| `--channel`, `-c`   | `vcan0`     | CAN interface                                                     |
| `--interface`, `-i` | `socketcan` | python-can interface type                                         |
| `--artifacts`, `-a` | —           | Path to `seed_artifacts_v2` (needed for DFU; prompted if missing) |

**Commands**

| Command       | Description                                                                            |
| ------------- | -------------------------------------------------------------------------------------- |
| `dids`        | Read DIDs by name (tab complete) or hex ID; auto-decodes fields from ODJ               |
| `routine`     | Run a routine by name or hex ID (see named routines below)                             |
| `board-parts` | Read board part/serial DIDs `0xF012`–`0xF015`, `0xF030`/`0xF031` (opcode 14)           |
| `clear-dtc`   | ClearDiagnosticInformation group `0xFFFFFF` (UDS 0x14)                                 |
| `dfu`         | Full firmware update using `flash_tool` phases (identity → select → preflight → flash) |
| `session`     | Switch diagnostic session                                                              |
| `reset`       | ECU hard reset                                                                         |
| `quit`        | Disconnect and exit                                                                    |

**Named routines** (from hashpicker_sim VM opcode table)

| Name                 | Routine ID | Description                                                      |
| -------------------- | ---------- | ---------------------------------------------------------------- |
| `erase`              | `0xFF00`   | `initializeEraseModule` — EraseMemory (requires security access) |
| `verify-crc`         | `0x0201`   | `checkModuleProgrammedCorrectly` — CRC verify                    |
| `check-component`    | `0x0202`   | `checkCorrectComponentAndRev`                                    |
| `ota-wait`           | `0x0540`   | `vcWaitForOTAMode` / `otaStateRoutineControl`                    |
| `ibst-power`         | `0x0543`   | `ibstPowerControl` (requires security access)                    |
| `ota-validate`       | `0x0402`   | OTA state validation (session-dependent response check)          |
| `ota-validate-noack` | `0x0403`   | OTA state validation (no response check)                         |
| `routine-0601`       | `0x0601`   | Opcode 37 unnamed routine                                        |

---

### `uds_tool.py` — UDS diagnostic CLI

General-purpose UDS client for reading/writing DIDs, running routines, managing sessions, and scanning the network.

```
python uds_tool.py scan --channel vcan0
python uds_tool.py --node PCS --channel vcan0 read-did BOOTLOADER_VERSION
python uds_tool.py --node PCS --channel vcan0 read-did 0xF180
python uds_tool.py --node PCS --channel vcan0 write-did 0x0102 deadbeef
python uds_tool.py --node PCS --channel vcan0 routine 0xFF00 01
python uds_tool.py --node CP  --channel vcan0 security-access
python uds_tool.py --node PCS --channel vcan0 session programming
python uds_tool.py --node PCS --channel vcan0 reset
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

| Flag                | Default     | Description                             |
| ------------------- | ----------- | --------------------------------------- |
| `--node`, `-n`      | —           | ECU node name (e.g. `PCS`, `CP`, `RCM`) |
| `--channel`, `-c`   | `vcan0`     | CAN interface                           |
| `--interface`, `-i` | `socketcan` | python-can interface type               |

---

### `flash_tool.py` — Firmware flash CLI

Flashes firmware to an ECU using the correct ECU-specific UDS programming sequence: identity discovery → firmware selection → pre-flight verification → flash. The flash sequence is selected automatically from `flash_scripts.py` based on the ECU type reported in `signed_metadata_map.tsv`, covering all 21 script variants reverse-engineered from `hashpicker_sim`.

```
python flash_tool.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
python flash_tool.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2 --force
```

**Options**

| Flag                | Default     | Description                             |
| ------------------- | ----------- | --------------------------------------- |
| `--node`, `-n`      | —           | ECU node name                           |
| `--channel`, `-c`   | `vcan0`     | CAN interface                           |
| `--interface`, `-i` | `socketcan` | python-can interface type               |
| `--artifacts`, `-a` | —           | Path to `seed_artifacts_v2` directory   |
| `--force`           | —           | Proceed despite BHX identity mismatches |

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

### `can_live.py` — Live CAN signal viewer

Web-based live viewer that decodes CAN frames in real time using `Model3_ETH.compact.json`. Select an ECU node from the sidebar to see all its signals updating live, with age indicators and enum labels.

```
python can_live.py --channel vcan0
python can_live.py --channel vcan0 --interface socketcan --port 8765
```

Opens `http://localhost:8765` automatically in your browser.

**Options**

| Flag           | Default     | Description                      |
| -------------- | ----------- | -------------------------------- |
| `--channel`    | `vcan0`     | CAN interface                    |
| `--interface`  | `socketcan` | python-can interface type        |
| `--bitrate`    | —           | CAN bitrate (optional)           |
| `--port`       | `8765`      | HTTP/WebSocket port              |
| `--no-browser` | —           | Don't auto-open browser on start |

**Features**

- Node sidebar — all 39 ECU nodes listed; click to subscribe
- Live signal table — per-message sections with signal name, value, unit, and enum label
- Value flash — cells briefly highlight when a value changes
- Age indicator — shows time since last frame per message; turns red when stale (>1 s)
- Signal filter — type to narrow signals by name
- FPS counter — shows decoded frames/second in the top bar

**Signal decoding** handles little-endian and big-endian bit packing, multiplexed signals (`mux_id`), `value_description` enum mappings, and signed/unsigned scale+offset.

---

## Data files

Node configurations live in `data/`:

- `nodes.json` — CAN IDs, security parameters, and ODJ sources for each ECU
- `Model3_ETH.compact.json` — CAN message ID map
- `odj/` — Per-ECU DID definitions (name, hex ID, read/write sizes, security level)

## Requirements

```
pip install -r requirements.txt
```

Requires Python 3.10+, `python-can`, `aiohttp`, and a SocketCAN interface (real or virtual via `vcan`). `can_live.py` also requires `aiohttp`.

## Tests

```
pytest tests/ -v
```

Tests require the `seed_artifacts_v2` firmware artifact directory. Tests that depend on it are automatically skipped when the path is absent.
