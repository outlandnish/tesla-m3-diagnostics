# Tesla ECU Firmware Update — UDS Protocol Reference

Reverse engineered from `hashpicker_sim`. All frames are ISO-TP over CAN.
See [UDS_VM_OPCODES.md](UDS_VM_OPCODES.md) for the VM layer and
[README.md](README.md) for BHX file format details.

---

## BHX File Format

BHX is a big-endian container. Headers are parsed locally — **never sent over UDS**.
Only the raw SHDR payload bytes are transmitted.

### GHDR — Global Header

| Offset | Size | Field              | Notes                                                      |
| ------ | ---- | ------------------ | ---------------------------------------------------------- |
| `0x00` | 4    | Magic `"GHDR"`     |                                                            |
| `0x04` | 4    | Version            | `1` or `2`, big-endian                                     |
| `0x08` | 4    | Total payload size | Sum of all SHDR payload sizes, not headers                 |
| `0x0C` | 4    | `ramAppPayload`    | **v2 only** — if `1`, triggers re-auth+erase between SHDRs |

### SHDR — Section Header (20 bytes) + payload

| Offset  | Size | Field          | Notes                                                  |
| ------- | ---- | -------------- | ------------------------------------------------------ |
| `+0x00` | 4    | Magic `"SHDR"` |                                                        |
| `+0x04` | 4    | Version        | `1`, big-endian                                        |
| `+0x08` | 4    | Target address | Big-endian — used verbatim in `RequestDownload`        |
| `+0x0C` | 4    | Payload size   | Big-endian — used verbatim in `RequestDownload`        |
| `+0x10` | 4    | CRC32          | Of this section's payload; validated by ECU bootloader |
| `+0x14` | N    | **Payload**    | The only bytes sent over UDS                           |

Payload offset in file:

- GHDR v1: `0x20` (12-byte GHDR + 20-byte SHDR header — first SHDR)
- GHDR v2: `0x24` (16-byte GHDR + 20-byte SHDR header — first SHDR)

---

## Standard Flash Sequence

This is the bytecode script executed by the VM for most ECUs
(decoded from `0x00650fb0`):

```
reset(soft)                 → no UDS frame; reconnects after boot
boardPartSerialGet          → 22 F0 12 / F0 13 / F0 14 / F0 15  (logged, not validated)
DiagnosticSessionControl    → 10 02
varifyCompAndFirmware       → 22 01 01  (verify component matches)
SecurityAccess              → 27 01 / 27 02  (seed+key)
CALL sub1:
  moduleToProgram           → 10 02
  initializeEraseModule     → 31 01 FF 00 01
  transferData              → [RequestDownload + TransferData loop + TransferExit]
checkModuleProgrammed       → 31 01 02 01
checkCorrectComponentAndRev → 31 01 02 02
reset(soft)
```

---

## Frame-by-Frame Reference

### 0. Soft Reset (before session)

```
→ 11 01    ECUReset subfunction 0x01
  (no response wait — reconnects with TesterPresent retries)
```

Opcode `reset(0)`. Reconnect loop waits up to 334×10 ms, retries TesterPresent
up to 14 times.

---

### 1. Read Part / Serial Info _(logged only, does not gate flashing)_

```
→ 22 F0 12    ReadDataByIdentifier — board part number
← 62 F0 12 <data>

→ 22 F0 13    ReadDataByIdentifier — serial number
← 62 F0 13 <data>

→ 22 F0 14    ReadDataByIdentifier — board revision
← 62 F0 14 <data>

→ 22 F0 15    ReadDataByIdentifier — assembly info
← 62 F0 15 <data>
```

Results written to `modinfo.log`. Failure here does not abort the flash.

---

### 2. Enter Default / Extended Session

```
→ 10 02    DiagnosticSessionControl — programming session
← 50 02 <P2_hi> <P2_lo> <P2star_hi> <P2star_lo>
```

Response bytes 1–2: P2 timeout (ms). Bytes 3–4: P2\* enhanced timeout (×10 ms).
These are applied to the CAN handle immediately.

**Start TesterPresent keepalive here: send `3E 80` every ~2 s for the duration.**

---

### 3. Verify Component and Firmware Type

```
→ 22 01 01    ReadDataByIdentifier DID 0x101
← 62 01 01 <component_key> <fw_type> <protocol_ver>
```

`fw_type` (byte 1 of response data) must match the expected value for this ECU.
Mismatch → abort. Result stored and used to select security access level in step 4.

---

### 4. Security Access — `tesla_hash`

```
→ 27 01              RequestSeed (level 0x01)
← 67 01 <16 bytes seed>

→ 27 02 <16 bytes key>    SendKey
← 67 02
```

Key computation (`tesla_hash`) — computed by your security provider (not shipped):

```python
# tesla_hash is supplied by your security provider; see docs/SECURITY_PROVIDER.md
    raise NotImplementedError  # not shipped
```

Example: seed `A0 B1 C2` → key `95 84 F7`.

If ECU responds with NRC `0x35` (already unlocked), this is silently accepted
and the step succeeds.

**Seed level** is ECU-specific. Level `0x01` is the standard programming level.
Some ECUs use higher levels (e.g. level `0x07` for the Pektron variant, which uses
a different key algorithm entirely — `FUN_0040be8e`).

---

### 5. Erase Flash Sectors

```
→ 31 01 FF 00 01    RoutineControl startRoutine 0xFF00, arg=0x01
← 71 01 FF 00 <status>
```

`status` must be `0x00`. Any non-zero value means erase failed.
The arg byte `0x01` is required — the ECU rejects any other value.

Set P2 timeout to 3000 ms / P2\* to 6000 ms before sending (erase can be slow).
Restore to 1000 ms / 2000 ms after.

---

### 6. For Each SHDR in the BHX File

#### 6a. RequestDownload

```
→ 34 00 44 <addr:4BE> <size:4BE>
← 74 <lenFmt> <maxBlockSize:var>
```

- `addr` = SHDR target address field (big-endian, as-is from file)
- `size` = SHDR payload size field (big-endian, as-is from file)
- `maxBlockSize` = extracted from response; capped at 512 bytes

Failures: `uploadDownloadNotAccepted (0x70)`, `requestOutOfRange (0x31)`

#### 6b. TransferData

```
→ 36 <seq> <up to maxBlockSize bytes>
← 76 <seq> <crc_hi> <crc_lo>
```

- `seq` starts at `0x01`, increments per block, wraps `0xFF → 0x00`
- Send the raw SHDR payload bytes in order; stop when all payload bytes are sent
- ECU returns a 2-byte CRC of the block in the response — verify it matches

Failures: `wrongBlockSequenceCounter (0x73)`, `transferDataSuspended (0x71)`

#### 6c. RequestTransferExit

```
→ 37
← 77
```

#### 6d. Multi-SHDR: Re-auth and Re-erase (GHDR v2, `ramAppPayload == 1` only)

If the BHX file has multiple SHDRs and `ramAppPayload == 1` in the GHDR, run the
following between each SHDR (after TransferExit, before the next RequestDownload):

```
→ 31 01 02 01    checkModuleProgrammedCorrectly — verify last SHDR's CRC
← 71 01 02 01 <status>

→ 31 01 02 02    checkCorrectComponentAndRev — re-validate identity
← 71 01 02 02 <status>

→ 10 02          re-enter programming session
← 50 02 ...

→ 27 01 / 27 02  re-authenticate
← 67 01 ... / 67 02

→ 10 02          moduleToProgram
← 50 02 ...

→ 31 01 FF 00 01  erase next region
← 71 01 FF 00 00
```

Then continue with `RequestDownload` for the next SHDR.

---

### 7. Verify Programming

```
→ 31 01 02 01    RoutineControl startRoutine 0x0201
← 71 01 02 01 <status>
```

ECU bootloader recomputes the CRC of the flashed image and compares it to the
trailing CRC word in the payload. `status` must be `0x00`.

---

### 8. Verify Component / Revision Match

```
→ 31 01 02 02    RoutineControl startRoutine 0x0202
← 71 01 02 02 <status>
```

Bootloader validates the identity header (COMPONENT_ID, PCBA_ID, ASSEMBLY_ID,
USAGE_ID) in the flashed payload against its own stored identity.
`status` must be `0x00`.

---

### 9. ECU Reset

```
→ 11 01    ECUReset hardReset
← 51 01
```

Bootloader re-validates CRC on next boot and jumps to application if valid.
Stop TesterPresent keepalive after receiving the positive response.

---

## Alternate Script: Extended Timeout Variant

Some ECUs (decoded from `0x00651230`) skip the pre-session soft reset and use an
extended security access level, with an explicit timeout increase before erase:

```
DiagnosticSessionControl    → 10 02
varifyCompAndFirmware       → 22 01 01
SecurityAccess(7)           → 27 <level> / 27 <level+1>  (Pektron key algorithm)
moduleToProgram             → 10 02
netSetTimeout(5)            → P2 = 5000 ms, P2* = 10000 ms
initializeEraseModule(1)    → 31 01 FF 00 01  (accept non-standard response count)
transferData                → [RequestDownload + blocks + TransferExit]
checkModuleProgrammed       → 31 01 02 01
checkCorrectComponentAndRev → 31 01 02 02
reset(soft)
```

---

## Security: `tesla_hash` Detail

Applies to standard ECUs (security level `0x01`). No secret key, no nonce, stateless.

```
key[i] = seed[i] ^ 0x35   for each byte i in 0..15
```

The 16-byte seed comes directly from the `27 01` response bytes 1–16.
The 16-byte key is sent verbatim as the `27 02` payload.

---

## Timing

| Timeout        | Value                 | When                                            |
| -------------- | --------------------- | ----------------------------------------------- |
| Default P2     | from DSC response     | after `10 02`                                   |
| Default P2\*   | from DSC response ×10 | after `10 02`                                   |
| During erase   | 3000 ms / 6000 ms     | set before `31 01 FF 00`, restored after        |
| Extended erase | 5000 ms / 10000 ms    | alternate script variant                        |
| TesterPresent  | every ~2 s            | `3E 80` (suppress response), throughout session |

---

## Key DIDs

| DID      | Name                 | Bytes | Notes                                     |
| -------- | -------------------- | ----- | ----------------------------------------- |
| `0x0101` | `COMP_AND_FW_TYPE`   | 3     | `[component_key, fw_type, protocol_ver]`  |
| `0xF012` | Board part number    | var   | logged to `modinfo.log`                   |
| `0xF013` | Serial number        | var   | logged to `modinfo.log`                   |
| `0xF014` | Board revision       | var   | logged to `modinfo.log`                   |
| `0xF015` | Assembly info        | var   | logged to `modinfo.log`                   |
| `0xF100` | Flash count          | 4     | enforced per-ECU limit; exceeding → abort |
| `0xF180` | `BOOTLOADER_VERSION` | 19    | identity record (see below)               |
| `0xF01D` | `USAGE_ID`           | 2     |                                           |
| `0xF01E` | `SUB_USAGE_ID`       | 2     | secondary node                            |

`BOOTLOADER_VERSION` (DID `0xF180`) — 19 bytes:

```
Byte   Field
0      MODULES
1–2    COMPONENT_ID  (big-endian)
3      PCBA_ID
4      ASSEMBLY_ID
5–6    USAGE_ID
7      (unused)
8      FIRMWARE_TYPE
9–16   GIT_HASH  (8 bytes)
17–18  BUILD_CONFIG_ID
```

---

## Error Reference

| Code                            | Meaning                                               |
| ------------------------------- | ----------------------------------------------------- |
| `REQUEST_DOWNLOAD_FAILED`       | `34` rejected — wrong address, size, or session state |
| `NAK_uploadDownloadNotAccepted` | ECU not ready to receive download                     |
| `BHX_TRANSFER_DATA_ERROR`       | `36` block rejected                                   |
| `BLOCK_CHECKSUM_MISMATCH`       | CRC in `76` response doesn't match computed value     |
| `NAK_wrongBlockSequenceCounter` | Sequence byte out of order — transfer aborted         |
| `BHX_TRANSFER_exit_ERROR`       | `37` rejected                                         |
| `BHX_INVALID_GLOBAL_HEADER`     | File doesn't start with valid `GHDR` magic+version    |
| `BHX_INVALID_SEGMENT_HEADER`    | SHDR magic/version unrecognized                       |
| `BHX_READ_FILE_FAILED_A/B/C/D`  | Short read from BHX file                              |
| `INCORRECT_MODULE_PROGRAMMED`   | Routine `0x0201` returned non-zero status             |
| `INCORRECT_COMPONENT_AND_REV`   | Routine `0x0202` returned non-zero status             |
| `FLASH_COUNT_LIMIT_EXCEEDED`    | DID `0xF100` at or over per-ECU limit                 |

---

## Multi-CPU ECU Example: PCS

|                 | CPU1 (`pcs.bhx`) | CPU2 (`pcscpu2.bhx`) |
| --------------- | ---------------- | -------------------- |
| CAN request ID  | `0x628`          | `0x628`              |
| CAN response ID | `0x629`          | `0x629`              |
| Target address  | `0x00088000`     | `0x00082000`         |
| Flash sectors   | 4, 5, 6          | 1, 2, 3, 4           |
| Component ID    | `0x001b`         | `0x0096`             |
| Payload size    | 154,172 bytes    | 88,292 bytes         |

CPU1 and CPU2 are flashed as independent ECU nodes — full sequence steps 0–9 for each.
Bootloader lives in Sector 0 (`0x80000–0x81FFF`) and is never erased.

---

## Tools

```bash
python3 tools/bhx_parser.py <file.bhx>
python3 tools/bhx_parser.py --json <file.bhx>
python3 tools/bhx_parser.py --extract-dir /tmp/out/ <file.bhx>
```
