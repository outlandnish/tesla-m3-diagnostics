# Tesla ECU Firmware Update — Reverse Engineered Protocol

Findings from analysis of Tesla Model 3 firmware dump (2019.20.4.2), ODIN decompiled
source, BHX format reverse engineering, and Ghidra decompilation of hashpicker_sim.

---

## BHX File Format

### Outer Container (never sent over UDS)

BHX is a generic container: one GHDR followed by one or more SHDR sections.

**GHDR — Global Header**

Two versions exist:

```
Offset  Size  Field                        v1   v2
0x00    4     Magic "GHDR"                 ✓    ✓
0x04    4     Version (big-endian)         1    2
0x08    4     Total payload size           ✓    ✓    sum of all SHDR payload sizes
0x0C    4     (reserved / extra field)     —    ✓    v2 only
```

**SHDR — Section Header (20 bytes) + payload, repeated per section**

```
Offset  Size  Field
+0x00   4     Magic "SHDR"
+0x04   4     Version (big-endian, = 1)
+0x08   4     Target address (big-endian, load base for this section)
+0x0C   4     Payload size (big-endian, bytes in this section's payload)
+0x10   4     CRC32 of this section's payload
[+0x14] N     Payload — this is what goes over UDS TransferData
```

All fields big-endian. Single-CPU ECUs have one SHDR; multi-CPU or multi-region ECUs
(e.g. park, PCS CPU1+CPU2) use multiple SHDRs. Each SHDR triggers a separate
RequestDownload + TransferData + RequestTransferExit sequence.

### Firmware File Formats

Selection is by filename extension:

| Extension | Format | Handling |
|---|---|---|
| `.bhx` | BHX container | Parse GHDR+SHDRs; one download sequence per SHDR |
| `.hex` | Intel HEX | Parse records; one RequestDownload per contiguous address region |
| `.img` | Raw binary | Single RequestDownload for the whole file |

For BHX files the GHDR and SHDR headers are consumed locally and never sent over UDS.
The target address and size for each RequestDownload come from the SHDR fields.

---

## UDS Flashing Sequence

### Transport

All Tesla ECUs use ISO-TP over CAN. Each ECU has a dedicated request/response CAN ID pair.
See the ECU-specific appendix for CAN IDs. GTW acts as CAN gateway for ECUs not directly
on the ODIN CAN bus.

Security uses the `tesla_hash` algorithm with a 16-byte seed buffer (see Step 2).

### Pre-flight: Verify Hardware Identity

Read these DIDs in DEFAULT session and confirm they match the firmware file's identity
header before proceeding:

| DID | Name | Match against |
|---|---|---|
| `0x101` | `COMP_AND_FW_TYPE` | COMPONENT_KEY, FIRMWARE_TYPE, BOOTLOADER_PROTOCOL_VERSION |
| `0xF180` | `BOOTLOADER_VERSION` | COMPONENT_ID, PCBA_ID, ASSEMBLY_ID from payload header |
| `0xF01D` | `USAGE_ID` | USAGE_ID from payload header |
| `0xF01E` | `SUB_USAGE_ID` | secondary node verification |

`BOOTLOADER_VERSION` (DID `0xF180`) layout — 19 bytes:

```
Byte  Field
0     MODULES
1–2   COMPONENT_ID (big-endian)
3     PCBA_ID
4     ASSEMBLY_ID
5–6   USAGE_ID
7     UNUSED
8     FIRMWARE_TYPE
9–16  GIT_HASH (8 bytes)
17–18 BUILD_CONFIG_ID
```

### Step 1: Enter Programming Session

```
→ 10 02          DiagnosticSessionControl(PROGRAMMING)
← 50 02 xx xx xx xx
```

Send TesterPresent (`3E 80`) every ~2 seconds throughout to keep the session alive.

### Step 2: Security Access

```
→ 27 01                    RequestSeed
← 67 01 <16 bytes seed>

→ 27 02 <16 bytes key>     SendKey  (key = tesla_hash(seed))
← 67 02
```

`tesla_hash` — computed by your security provider (not shipped):

```python
# tesla_hash is supplied by your security provider; see docs/SECURITY_PROVIDER.md
    raise NotImplementedError  # not shipped
```

No secret key, no state. Example: seed byte `0xA0` → key byte `0xA0 ^ 0x35 = 0x95`.

### Step 3: Erase Target Sectors

`initializeEraseModule` — erases the flash sectors occupied by the incoming image.

```
→ 31 01 FF 00 01    RoutineControl startRoutine, routine 0xFF00, arg=0x01
← 71 01 FF 00 <status>
```

- Routine ID: **`0xFF00`**
- arg byte `0x01` authorizes erase; any other value → error `0x150000`
- Response `<status>` non-zero → erase failure (`status | 0x20000`)

### Step 4: Check Component/Revision

`checkCorrectComponentAndRev` — bootloader validates the firmware identity header against
its own COMPONENT_ID, PCBA_ID, ASSEMBLY_ID, and USAGE_ID.

```
→ 31 01 02 02    RoutineControl startRoutine, routine 0x0202
← 71 01 02 02 <status>
```

- Routine ID: **`0x0202`**
- Response `<status>` non-zero → wrong component or revision (`0x160000`)
- Mismatch aborts with `INCORRECT_COMPONENT_AND_REV`

### Step 5: RequestDownload

```
→ 34 00 44 <addr:4BE> <size:4BE>
← 74 <lenFmt> <maxBlockLen>
```

- `addr` = target address from SHDR (big-endian 4 bytes)
- `size` = payload size from SHDR (big-endian 4 bytes)
- `maxBlockLen` from response = maximum payload bytes per TransferData block

Failure: `REQUEST_DOWNLOAD_FAILED`, `NAK_uploadDownloadNotAccepted`

### Step 6: TransferData

Send the raw SHDR payload bytes — GHDR and SHDR headers are never transmitted.

```
→ 36 <seq> <up to maxBlockLen-2 bytes>
← 76 <seq>
```

- `seq` starts at `0x01`, increments per block, wraps `0xFF → 0x00`
- Send TesterPresent (`3E 01`) every 5 blocks to suppress the S3 timeout
- Repeat until the full SHDR payload is sent

Failures: `NAK_wrongBlockSequenceCounter`, `BHX_TRANSFER_DATA_ERROR`, `BLOCK_CHECKSUM_MISMATCH`

### Step 7: RequestTransferExit

```
→ 37
← 77
```

Failure: `BHX_TRANSFER_exit_ERROR`

### Step 8: Repeat per SHDR

For multi-SHDR BHX files, repeat Steps 5–7 for each SHDR in file order before
proceeding to Step 9.

### Step 9: Verify Programming

`checkModuleProgrammedCorrectly` — bootloader recomputes the image CRC and compares it
against the trailing CRC word in the payload.

```
→ 31 01 02 01    RoutineControl startRoutine, routine 0x0201
← 71 01 02 01 <status>
```

- Routine ID: **`0x0201`**
- Response `<status>` non-zero → CRC mismatch (`status | 0x170000`)

Failure: `INCORRECT_MODULE_PROGRAMMED`

### Step 10: ECU Reset

```
→ 11 01    ECUReset(hardReset)
← 51 01
```

Bootloader validates the CRC on next boot and jumps to application if valid.

---

## Gotchas

1. **Never send BHX headers** — only the SHDR payload bytes go over UDS. For a
   single-section file, payload starts at file offset `0x20` (12-byte GHDR + 20-byte SHDR header — note GHDR v2 is 16 bytes).
2. **Wrong component/variant** → `INCORRECT_COMPONENT_AND_REV`; check COMPONENT_ID,
   PCBA_ID, ASSEMBLY_ID, USAGE_ID all match before starting.
3. **Skip erase** → writing onto un-erased flash corrupts the image.
4. **Wrong block sequence counter** → transfer aborted with sectors already erased.
5. **Flash count limit** — some ECUs track programming cycles and enforce a hard limit
   (`FLASH_COUNT_LIMIT_EXCEEDED`).
6. **Power loss mid-flash** — the bootloader sector survives; the application is gone
   until re-flashed.
7. **Multi-SHDR order** — SHDRs must be flashed in file order; each has its own
   RequestDownload/TransferData/TransferExit cycle before the verify step.

---

## Key DIDs

| DID | Name | Notes |
|---|---|---|
| `0xF00` | `Application_CRC` | 4 bytes; cross-check after Step 9 |
| `0xF02` | `Subcomponent2_CRC` | 4 bytes; secondary CPU/region |
| `0xF07` | `BOOTLOADER_CRC` | 4 bytes |
| `0xF180` | `BOOTLOADER_VERSION` | 19-byte identity record (see Pre-flight) |
| `0xF01D` | `USAGE_ID` | 2 bytes |
| `0x101` | `COMP_AND_FW_TYPE` | component key + firmware type |

---

## Tools

- `tools/bhx_parser.py` — parse and inspect BHX files, extract payload
  ```
  python3 tools/bhx_parser.py <file.bhx>
  python3 tools/bhx_parser.py --extract-dir /tmp/payload/ <file.bhx>
  ```

---

## ECU Reference

### PCS (Power Conversion System)

| | CPU1 | CPU2 |
|---|---|---|
| CAN request ID | `0x628` (1576) | `0x628` (1576) |
| CAN response ID | `0x629` (1577) | `0x629` (1577) |
| Target address | `0x00088000` | `0x00082000` |
| Flash sectors | 4, 5, 6 | 1, 2, 3, 4 |
| Component ID | `0x001b` | `0x0096` |
| Payload size (531) | 154,172 bytes | 88,292 bytes |
| Flash span | `0x88000–0x9AD1D` | `0x82000–0x8CC71` |

Bootloader lives in Sector 0 (`0x80000–0x81FFF`) — never erased.

**Variant 531 (P5/A3/U1) identity:**

```
pcs:<key>  where key = 0xPPAA00UU (big-endian uint32)
  PP = PCBA_ID (0x05), AA = ASSEMBLY_ID (0x03), UU = USAGE_ID (0x01)
  531 → pcs:84082689 = 0x05030001
```

`signed_metadata_map.tsv` entries for variant 531:
- dest `pcs.bhx` / component `pcs` → CPU1 file
- dest `pcscpu2.bhx` / component `pcscpu2` → CPU2 file

Flash sequence: CPU1 first, then CPU2. Run the full Steps 1–10 for each independently.
