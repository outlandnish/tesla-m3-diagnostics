# tesla_uds_tools

UDS diagnostic and firmware flashing tools for Tesla Model 3 ECUs over CAN.

## Tools

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

**Options**

| Flag                | Default     | Description                             |
| ------------------- | ----------- | --------------------------------------- |
| `--node`, `-n`      | —           | ECU node name (e.g. `PCS`, `CP`, `RCM`) |
| `--channel`, `-c`   | `vcan0`     | CAN interface                           |
| `--interface`, `-i` | `socketcan` | python-can interface type               |

---

### `flash_tool.py` — Firmware flash CLI

Flashes firmware to an ECU using the Tesla 10-step UDS programming sequence: identity discovery → firmware selection → pre-flight verification → flash.

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

## Data files

Node configurations live in `data/`:

- `nodes.json` — CAN IDs, security parameters, and ODJ sources for each ECU
- `Model3_ETH.compact.json` — CAN message ID map
- `odj/` — Per-ECU DID definitions (name, hex ID, read/write sizes, security level)

## Requirements

```
pip install -r requirements.txt
```

Requires Python 3.10+, `python-can`, and a SocketCAN interface (real or virtual via `vcan`).

## Tests

```
pytest tests/ -v
```

Tests require the `seed_artifacts_v2` firmware artifact directory. Tests that depend on it are automatically skipped when the path is absent.
