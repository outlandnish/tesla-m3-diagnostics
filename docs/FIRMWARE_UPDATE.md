# Tesla ECU Firmware Update — UDS Protocol Reference

Reverse engineered from `hashpicker_sim`. All frames are ISO-TP over CAN.
See [UDS_VM_OPCODES.md](UDS_VM_OPCODES.md) for the VM layer and
[README.md](README.md) for the BHX parser tool.

---

## BHX File Format

BHX is a big-endian container. Headers are parsed locally — **never sent over UDS**.
Only the raw SHDR payload bytes are transmitted.

### GHDR — Global Header

| Offset | Size | Field              | Notes                                                 |
| ------ | ---- | ------------------ | ----------------------------------------------------- |
| `0x00` | 4    | Magic `"GHDR"`     |                                                       |
| `0x04` | 4    | Version            | `1` or `2`, big-endian                                |
| `0x08` | 4    | Total payload size | Sum of all SHDR payload sizes, not headers            |
| `0x0C` | 4    | Total size         | **v2 only** — alternate total (redundant with `0x08`) |

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

- GHDR v1: `0x20` (12-byte GHDR + 20-byte SHDR header)
- GHDR v2: `0x24` (16-byte GHDR + 20-byte SHDR header)

---

## Flash Scripts

17 unique scripts exist in the binary at address `0x00650fa0`–`0x006512e0`. Each
script contains one or more 48-byte program slots at `+0x00`, `+0x30`, `+0x60`, etc.
The orchestration layer selects which slot to run based on context.

The `module` byte in the ECU node table (`+0x20`) is loaded into `context+0x29`
before the VM runs. `moduleToProgram` reads and consumes it, sending `2E 01 02 <module>`
to select the target CPU/region in the bootloader. For single-CPU ECUs this is `0x00`.

### ECU → Script Map

| Script       | ECUs                                                                                                                     |
| ------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `0x00650fa0` | gtw3                                                                                                                     |
| `0x00650fb0` | hvbms, cp, epas3p, epas3s, epbl, epbr, hvp, ocs1p, sccmk, vcsec, tas                                                     |
| `0x00651000` | vcfront, ibstcal                                                                                                         |
| `0x00651030` | vcright                                                                                                                  |
| `0x00651050` | vcleft                                                                                                                   |
| `0x00651070` | pcs (mod=0x00), pcscpu2 (mod=0x0c), pm (mod=0x00), pms (mod=0x00), di (mod=0x0c), dis (mod=0x0c)                         |
| `0x006510d0` | park                                                                                                                     |
| `0x006510f0` | park, aps                                                                                                                |
| `0x00651110` | vcleftramapp (mod=0x06), vcrightramapp (mod=0x0f), vcfrontramapp (mod=0x0f), vcsecrumapp (mod=0x0f), sccmksub (mod=0x06) |
| `0x00651140` | ibst                                                                                                                     |
| `0x00651170` | espcal (mod=0x07), rcmcal (mod=0x07)                                                                                     |
| `0x00651190` | esp                                                                                                                      |
| `0x006511b0` | ibstcal (bootloader path)                                                                                                |
| `0x006511d0` | rcm                                                                                                                      |
| `0x006511f0` | tpms                                                                                                                     |
| `0x00651230` | cmp                                                                                                                      |
| `0x00651270` | ptc                                                                                                                      |
| `0x00651290` | vcrightramapp, vcfrontramapp, vcsecrumapp, bleepcenter (mod=0x0f)                                                        |
| `0x006512b0` | vcleftramapp (mod=0x0f)                                                                                                  |
| `0x006512d0` | opc (mod=0x0c), opcs (mod=0x0c)                                                                                          |
| `0x006512e0` | ths (mod=0x0c), swc (mod=0x0c), lumbarl/lumbar/lumbarr (mod=0x0b), bleep\* (various)                                     |

---

### `0x00650fa0` — gtw3

Single unrecognized opcode. No UDS flash sequence — stub only.

---

### `0x00650fb0` — Standard (hvbms, cp, epas3p/s, epbl/r, hvp, ocs1p, sccmk, vcsec, tas)

```
[prog 0]
reset(soft)
orFlags(4)                     suppress-error flag on
boardPartSerialGet             22 F012/F013/F014/F015  (logged, not validated)
andNotFlags(4)                 suppress-error flag off
diagnosticSession(2)           10 02
varifyCompAndFirmware(1)       22 01 01
securityAccess(0)              27 01/02  (tesla_hash, level idx 0)
netSetTimeout(5)               P2=5s P2*=10s
CALL sub1:
  moduleToProgram              2E 01 02 <module>
  initializeEraseModule        31 01 FF 00 01
  transferData                 RequestDownload + blocks + TransferExit
checkModuleProgrammed          31 01 02 01
checkCorrectComponentAndRev    31 01 02 02
reset(soft)
sleep(300ms)
```

---

### `0x00651000` — vcfront / ibstcal (power context variant)

```
[prog 0]  — power rail on, flash, power rail off
orFlags(4)
CALL sub2              setSecurityAccessLevel(3) + securityAccess + vcWaitForOTAMode + ibstPowerControl(1)
andNotFlags(4)
CALL sub4              setSecurityAccessLevel(3) + securityAccess + ibstPowerControl(2)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware(1)  securityAccess(0)
CALL sub1
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)
orFlags(4)
CALL sub2
CALL sub5              OTA mode + contextSwitch + erase variant
vcFrontLockIOControl(0)

[prog 1]  — standard flash only
reset(soft)  orFlags(4) / andNotFlags(4)
diagnosticSession(2)  varifyCompAndFirmware(1)  securityAccess(0)
CALL sub1
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)
```

---

### `0x00651030` — vcright

```
[prog 0]  — standard flash
reset(soft)  orFlags/andNotFlags
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)

[prog 1]  — resume: auth + flash only (no reset or part read)
securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)
```

---

### `0x00651050` — vcleft (pre-flash vendor routine)

```
[prog 0]
netSetTimeout(5)
orFlags(4)
routineControl0601(0)          31 01 06 01 — vendor pre-flash routine
andNotFlags(4)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(500ms)
```

---

### `0x00651070` — pcs / pcscpu2 / di / dis / pm / pms (multi-CPU)

The `module` byte selects which CPU the bootloader programs (`2E 01 02 <module>`).

```
[prog 0]  — standard flash with extended erase timeout
reset(soft)
orFlags(4)  boardPartSerialGet  andNotFlags(4)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
netSetTimeout(5)               P2=5s P2*=10s
CALL sub1                      moduleToProgram(2E 01 02 <module>) + erase + transfer
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(300ms)

[prog 1]  — dual-CPU in-sequence (hardcoded subfunction, no context+0x29 override)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
moduleToProgram(4)             2E 01 02 04 — CPU2 flash region
netSetTimeout(30)              P2=30s P2*=60s
initializeEraseModule(0)
netSetTimeout(1)
transferData
checkModuleProgrammed
moduleToProgram(0)             2E 01 02 00 — CPU1 flash region
netSetTimeout(30)
initializeEraseModule(0)
transferData
netSetTimeout(4)
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 2]  — quick re-flash (5s post-reset sleep, no part read)
reset(soft)  orFlags/andNotFlags
diagnosticSession(2)  netSetTimeout(1)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(5000ms)

[prog 3]  — auth + flash only (no reset preamble)
securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x006510d0` — park (extended erase timeout)

```
[prog 0]
reset(soft)  orFlags/andNotFlags
diagnosticSession(2)  netSetTimeout(1)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)  sleep(5000ms)

[prog 1]  — auth + flash only
securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x006510f0` — park / aps

```
[prog 0]
reset(soft)  orFlags  boardPartSerialGet  andNotFlags
diagnosticSession(2)  netSetTimeout(1)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 1]  — reset only (stub)
reset(soft)
```

---

### `0x00651110` — RAM app scripts (vcleft/vcright/vcfront/vcsec ramapp, sccmksub)

```
[prog 0]  — RAM app flash (no boardPartSerialGet)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 1]  — flash count check + DTC clear + security level 3
checkFlashCount(2)
orFlags(4)  clearDTC(0)  andNotFlags(4)
reset(soft)
orFlags(4)  boardPartSerialGet  andNotFlags(4)
diagnosticSession(2)  varifyCompAndFirmware
securityAccess(3)
CALL sub1
checkModuleProgrammed
orFlags(4)  checkCorrectComponentAndRev  andNotFlags(4)
reset(soft)
```

---

### `0x00651140` — ibst

```
[prog 0]  — flash count check + DTC clear + security level 3
checkFlashCount(2)
orFlags  clearDTC  andNotFlags
reset(soft)  orFlags  boardPartSerialGet  andNotFlags
diagnosticSession(2)  varifyCompAndFirmware
securityAccess(3)
CALL sub1
checkModuleProgrammed
orFlags  checkCorrectComponentAndRev  andNotFlags
reset(soft)

[prog 1]  — direct explicit erase (no sub1)
reset(soft)
diagnosticSession(2)  netSetTimeout(4)
varifyCompAndFirmware  securityAccess(3)
moduleToProgram(0)     2E 01 02 00
initializeEraseModule  transferData
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x00651170` — espcal / rcmcal (calibration flash, security level 3)

```
[prog 0]
reset(soft)
diagnosticSession(2)  netSetTimeout(4)
varifyCompAndFirmware  securityAccess(3)
moduleToProgram(0)     2E 01 02 00
initializeEraseModule  transferData
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 1]  — reset only (stub)
reset(soft)
```

---

### `0x00651190` — esp (flash count check + security level 3)

```
[prog 0]
checkFlashCount(1)
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware
securityAccess(3)
CALL sub1
checkModuleProgrammed
reset(soft)
```

---

### `0x006511b0` — ibstcal bootloader path (hard reset with retries)

```
[prog 0]
checkFlashCount(0)
reset(2)               hard reset, 3 retries + 10s delay each
diagnosticSession(2)  netSetTimeout(4)
varifyCompAndFirmware  securityAccess(3)
CALL sub1
checkModuleProgrammed
reset(2)
sleep(100ms)
```

---

### `0x006511d0` — rcm (Pektron: flash count + hard reset + explicit erase)

```
[prog 0]
checkFlashCount(0)
reset(2)               hard reset with retries
diagnosticSession(2)  netSetTimeout(4)
varifyCompAndFirmware  securityAccess(3)
moduleToProgram(0)     2E 01 02 00
initializeEraseModule  transferData
checkModuleProgrammed  checkCorrectComponentAndRev
sleep(100ms)
reset(2)
```

---

### `0x006511f0` — tpms (security level 4 — baolong_hash)

```
[prog 0]
reset(soft)
diagnosticSession(2)  netSetTimeout(3)
varifyCompAndFirmware
securityAccess(4)      baolong_hash algorithm
CALL sub1
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x00651230` — cmp (security level 7 — pektron-style, non-standard erase)

```
[prog 0]
reset(soft)  orFlags  boardPartSerialGet  andNotFlags
diagnosticSession(2)  varifyCompAndFirmware
securityAccess(7)      FUN_0040be8e algorithm
moduleToProgram(0)
initializeEraseModule(1)   accepts non-standard response count
transferData
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 1]  — transfer-only resume (no re-auth or re-erase)
transferData
checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)
```

---

### `0x00651270` — ptc (non-standard erase, timeout 10s)

```
[prog 0]
reset(soft)
diagnosticSession(2)  netSetTimeout(10)
varifyCompAndFirmware  securityAccess(0)
moduleToProgram(0)
initializeEraseModule(1)   accepts non-standard response
transferData
checkModuleProgrammed(1)
checkCorrectComponentAndRev(1)
reset(soft)

[prog 1]  — empty
```

---

### `0x00651290` — vcright/vcfront/vcsec ramapp, bleepcenter

```
[prog 0]  — flash without boardPartSerialGet
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 1]  — resume: auth + flash only
securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 2]  — OTA session with securityAccess(13)
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
reset(soft)

[prog 3]  — same, no reset
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev
```

---

### `0x006512b0` — vcleftramapp (pre-flash routine + OTA paths)

```
[prog 0]  — pre-flash routineControl0601 + standard flash
netSetTimeout(5)  orFlags  routineControl0601  andNotFlags
reset(soft)
diagnosticSession(2)  varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 1]  — OTA: securityAccess(13) + reset
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)

[prog 2]  — OTA: same, no reset
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 3]  — OTA: sleep(5000ms) + same
sleep(5000ms)
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 4]  — standard flash, timeout 3s
reset(soft)
diagnosticSession(2)  netSetTimeout(3)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)
```

---

### `0x006512d0` — opc / opcs (OTA state machine)

```
[prog 0]  — OTA: securityAccess(13)
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)

[prog 1]  — standard flash, timeout 3s
reset(soft)
diagnosticSession(2)  netSetTimeout(3)
varifyCompAndFirmware  securityAccess(0)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)
```

---

### `0x006512e0` — ths / swc / lumbar* / bleep* (OTA + multi-CPU style)

```
[prog 0]  — OTA: securityAccess(13)
diagnosticSession(2)  securityAccess(13)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev

[prog 1]  — verify only + reset
checkCorrectComponentAndRev  reset(soft)

[prog 2]  — standard flash, timeout 3s, sleep preamble
sleep(1000ms)
diagnosticSession(2)  varifyCompAndFirmware(2)
securityAccess(0)  netSetTimeout(3)
CALL sub1  checkModuleProgrammed  checkCorrectComponentAndRev  reset(soft)

[prog 3]  — CALL sub5 only
CALL sub5
```

---

## Frame-by-Frame Reference

### 0. Soft Reset (before session)

```
→ 11 01    ECUReset subfunction 0x01
  (no response wait — reconnects with TesterPresent retries)
```

Reconnect loop waits up to 334×10 ms, retries TesterPresent up to 14 times.

---

### 1. Read Part / Serial Info _(logged only — failure does not abort)_

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

Results written to `modinfo.log`.

---

### 2. Enter Programming Session

```
→ 10 02    DiagnosticSessionControl — programming session
← 50 02 <P2_hi> <P2_lo> <P2star_hi> <P2star_lo>
```

Response bytes 1–2: P2 timeout (ms). Bytes 3–4: P2\* enhanced timeout (×10 ms).
Applied to the CAN handle immediately.

**Start TesterPresent keepalive here: send `3E 80` every ~2 s for the duration.**

---

### 3. moduleToProgram — CPU/region selection

```
→ 2E 01 02 <module>    WriteDataByIdentifier DID 0x0102 — select CPU/region
← 6E 01 02
```

The `module` byte is taken from the ECU node table entry (`+0x20`) and placed in
`context+0x29` before the VM runs. `moduleToProgram` reads and consumes it. For
single-CPU ECUs the module byte is `0x00`.

Module byte values for script `0x00651070` (PCS/DI/PM family):

| ECU                    | Module byte | Meaning                       |
| ---------------------- | ----------- | ----------------------------- |
| `pcs`, `pm`, `pms`     | `0x00`      | CPU1 / primary flash region   |
| `di`, `dis`, `pcscpu2` | `0x0c`      | CPU2 / secondary flash region |

---

### 4. Verify Component and Firmware Type

```
→ 22 01 01    ReadDataByIdentifier DID 0x0101
← 62 01 01 <component_key> <fw_type> <protocol_ver>
```

`fw_type` must match the expected value for this ECU. Mismatch → abort.

---

### 5. Security Access

```
→ 27 <level>              RequestSeed
← 67 <level> <seed bytes>

→ 27 <level+1> <key bytes>    SendKey
← 67 <level+1>
```

The seed level and key algorithm vary by ECU:

| Security idx | Algorithm       | Level  | ECUs                               |
| ------------ | --------------- | ------ | ---------------------------------- |
| 0            | `tesla_hash`    | 0x01   | most ECUs                          |
| 3            | `tesla_hash`    | varies | ibst, esp, espcal, rcmcal, rcm     |
| 4            | `baolong_hash`  | varies | tpms                               |
| 7            | `FUN_0040be8e`  | varies | cmp                                |
| 13           | OTA session key | varies | opc, opcs, ths, swc, lumbar, bleep |

`tesla_hash` — computed by your security provider (not shipped):

```python
# tesla_hash is supplied by your security provider; see docs/SECURITY_PROVIDER.md
    raise NotImplementedError  # not shipped
```

If ECU responds with NRC `0x35` (already unlocked), silently accepted.

---

### 6. Erase Flash Sectors

```
→ 31 01 FF 00 01    RoutineControl startRoutine 0xFF00, arg=0x01
← 71 01 FF 00 <status>
```

`status` must be `0x00`. The arg byte `0x01` is required.

Timeout during erase — set before sending, restore after:

| Script              | Erase timeout     |
| ------------------- | ----------------- |
| Standard            | P2=3s / P2\*=6s   |
| `0x00651070` (PCS)  | P2=5s / P2\*=10s  |
| `0x006510d0` (park) | P2=1s / P2\*=2s   |
| `0x00651140` (ibst) | P2=4s / P2\*=8s   |
| `0x006511d0` (rcm)  | P2=4s / P2\*=8s   |
| `0x006511f0` (tpms) | P2=3s / P2\*=6s   |
| `0x00651270` (ptc)  | P2=10s / P2\*=20s |

---

### 7. RequestDownload

```
→ 34 00 44 <addr:4BE> <size:4BE>
← 74 <lenFmt> <maxBlockSize:var>
```

- `addr` and `size` are taken verbatim from the SHDR target address and payload size fields
- `maxBlockSize` extracted from response; capped at 512 bytes

Failures: `uploadDownloadNotAccepted (0x70)`, `requestOutOfRange (0x31)`

---

### 8. TransferData

```
→ 36 <seq> <up to maxBlockSize bytes>
← 76 <seq> <crc_hi> <crc_lo>
```

- `seq` starts at `0x01`, increments per block, wraps `0xFF → 0x00`
- Send raw SHDR payload bytes in order
- ECU returns 2-byte CRC per block — verify it matches

Failures: `wrongBlockSequenceCounter (0x73)`, `transferDataSuspended (0x71)`

---

### 9. RequestTransferExit

```
→ 37
← 77
```

---

### 10. Multi-SHDR Re-auth and Re-erase (GHDR v2, `ramAppPayload == 1` only)

Between SHDRs (after TransferExit, before next RequestDownload):

```
→ 31 01 02 01    checkModuleProgrammedCorrectly
← 71 01 02 01 <status>

→ 31 01 02 02    checkCorrectComponentAndRev
← 71 01 02 02 <status>

→ 10 02          re-enter programming session
← 50 02 ...

→ 27 xx / 27 xx+1    re-authenticate
← 67 xx ... / 67 xx+1

→ 2E 01 02 <module>  moduleToProgram
← 50 01 ...

→ 31 01 FF 00 01     erase next region
← 71 01 FF 00 00
```

---

### 11. Verify Programming

```
→ 31 01 02 01    RoutineControl startRoutine 0x0201
← 71 01 02 01 <status>
```

ECU recomputes CRC of flashed image against trailing CRC word in payload.
`status` must be `0x00`.

---

### 12. Verify Component / Revision Match

```
→ 31 01 02 02    RoutineControl startRoutine 0x0202
← 71 01 02 02 <status>
```

Validates COMPONENT_ID, PCBA_ID, ASSEMBLY_ID, USAGE_ID against stored identity.
`status` must be `0x00`.

---

### 13. ECU Reset

```
→ 11 01    ECUReset
← 51 01
```

Bootloader re-validates CRC on next boot and jumps to application if valid.
Stop TesterPresent keepalive after receiving the positive response.

---

## Multi-CPU ECU: PCS

`pcs` and `pcscpu2` share the same CAN IDs (`0x628` / `0x629`) and the same script
(`0x00651070`). The bootloader selects which CPU to program via the `moduleToProgram`
subfunction byte, which comes from the `module` field in each node table entry.

|                         | `pcs.bhx` (CPU1) | `pcscpu2.bhx` (CPU2) |
| ----------------------- | ---------------- | -------------------- |
| CAN request ID          | `0x628`          | `0x628`              |
| CAN response ID         | `0x629`          | `0x629`              |
| `moduleToProgram` frame | `2E 01 02 00`    | `2E 01 02 0C`        |
| RequestDownload address | `0x00088000`     | `0x00082000`         |
| Flash sectors           | 4, 5, 6          | 1, 2, 3, 4           |
| Component ID            | `0x001b`         | `0x0096`             |
| Payload size (var. 531) | 154,172 bytes    | 88,292 bytes         |

Bootloader lives in Sector 0 (`0x80000–0x81FFF`) and is never erased.
Flash order between `pcs` and `pcscpu2` is set by the orchestration layer.

---

## Security Detail

### `tesla_hash` (most ECUs, security level idx 0 and 3)

Stateless, no secret key. Each seed byte XOR'd with `0x35`:

```
key[i] = seed[i] ^ 0x35   for i in 0..15
```

16-byte seed from `27 xx` response. 16-byte key sent as `27 xx+1` payload.

### Other algorithms

- **`baolong_hash`** (tpms, security idx 4): different algorithm, 2-byte buffer
- **`FUN_0040be8e`** (cmp, security idx 7): pektron-style, different algorithm

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

## Tools

```bash
python3 tools/bhx_parser.py <file.bhx>
python3 tools/bhx_parser.py --json <file.bhx>
python3 tools/bhx_parser.py --extract-dir /tmp/out/ <file.bhx>
```
