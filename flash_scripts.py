"""Flash script definitions for Tesla Model 3 ECUs.

Each ECU family uses a distinct UDS flash sequence reverse-engineered from
hashpicker_sim (see docs/FIRMWARE_UPDATE.md). This module expresses those
sequences as composable step functions assembled into FlashScript instances.

ECU_SCRIPT_MAP maps lowercase ecu_type names (from signed_metadata_map.tsv)
to the FlashScript they should use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from uds_local.client import UdsSession

# Routine IDs
_RC_ERASE      = 0xFF00  # initializeEraseModule
_RC_VERIFY_CRC = 0x0201  # checkModuleProgrammedCorrectly
_RC_CHECK_REV  = 0x0202  # checkCorrectComponentAndRev

# DIDs read for logging during step_board_info (not validated, failure ignored)
_BOARD_INFO_DIDS = (0xF012, 0xF013, 0xF014, 0xF015)

# Flash count limits indexed by operand (0–2) matching hashpicker_sim table
FLASH_COUNT_LIMITS = (200, 100, 50)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

@dataclass
class FlashContext:
    """Carries per-flash-run state shared between step functions."""
    bhx_file: object           # bhx.BhxFile
    entry: object              # metadata.FirmwareEntry
    module_byte: int = 0x00    # written by module_to_program step
    erase_timeout: float = 3.0 # P2 seconds applied around erase (restored after)
    security_level: int = 0    # security access level index


StepFn = Callable[["UdsSession", FlashContext], None]


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

def step_soft_reset(sess: "UdsSession", ctx: FlashContext) -> None:
    """ECUReset with suppressPositiveResponse (opcode 8 operand 1 — no response wait)."""
    print("  Step: ECUReset (suppress response, no wait)")
    sess.ecu_reset_no_wait(0x01)


def step_hard_reset(sess: "UdsSession", ctx: FlashContext) -> None:
    """ECUReset hard reset, wait for response (opcode 8 operand 0)."""
    print("  Step: ECUReset (hard reset, wait for response)")
    sess.ecu_reset(0x01)


def step_hard_reset_with_retries(sess: "UdsSession", ctx: FlashContext) -> None:
    """ECUReset hard reset with 3 retries + 10 s delay each (opcode 8 operand 2)."""
    print("  Step: ECUReset (hard reset, 3 retries + 10 s delay)")
    for attempt in range(3):
        try:
            sess.ecu_reset(0x01)
            return
        except Exception:
            if attempt < 2:
                print(f"    Reset attempt {attempt + 1} failed, retrying in 10 s")
                time.sleep(10)
    sess.ecu_reset(0x01)


def step_board_info(sess: "UdsSession", ctx: FlashContext) -> None:
    """Read board part/serial DIDs — logged only, failure does not abort."""
    print("  Step: Board part/serial DIDs (0xF012-0xF015)")
    for did in _BOARD_INFO_DIDS:
        try:
            data = sess.read_did(did)
            print(
                f"    0x{did:04X}: "
                f"{data.decode('ascii', errors='replace').rstrip(chr(0))!r}"
            )
        except Exception:
            pass


def step_programming_session(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: DiagnosticSessionControl(PROGRAMMING)")
    sess.diagnostic_session(0x02)
    sess.sleep(0.5)  # ECU reboots into bootloader after programming session


def step_verify_comp_fw(sess: "UdsSession", ctx: FlashContext) -> None:
    """RDBI 0x0101 — log component key, fw type, protocol ver."""
    print("  Step: ReadDataByIdentifier COMP_AND_FW_TYPE (0x0101)")
    comp_fw = sess.read_did(0x0101)
    if len(comp_fw) < 3:
        from uds_local.client import UdsError
        raise UdsError(0x22, 0x00)
    print(
        f"    component_key=0x{comp_fw[0]:02X}"
        f"  fw_type=0x{comp_fw[1]:02X}"
        f"  protocol_ver=0x{comp_fw[2]:02X}"
    )


def step_security_access(sess: "UdsSession", ctx: FlashContext) -> None:
    print(f"  Step: SecurityAccess (level_idx={ctx.security_level})")
    sess.security_access(ctx.security_level)


def step_module_to_program(sess: "UdsSession", ctx: FlashContext) -> None:
    """WDBI 0x0102 — select CPU/flash region."""
    print(f"  Step: moduleToProgram (module=0x{ctx.module_byte:02X})")
    sess.module_to_program(ctx.module_byte)


def step_erase(sess: "UdsSession", ctx: FlashContext) -> None:
    """RC 0xFF00 initializeEraseModule with extended timeout."""
    print(f"  Step: RC 0xFF00 initializeEraseModule (P2={ctx.erase_timeout}s)")
    sess.set_timeout(ctx.erase_timeout)
    try:
        sess.routine_control(_RC_ERASE, b"\x01")
    finally:
        sess.set_timeout(3.0)


def step_transfer_loop(sess: "UdsSession", ctx: FlashContext) -> None:
    """RequestDownload + TransferData + RequestTransferExit for each SHDR."""
    for seg_idx, seg in enumerate(ctx.bhx_file.segments):
        print(
            f"  Step: Transfer SHDR {seg_idx}"
            f" addr=0x{seg.start_address:08X} size={seg.length} bytes"
        )
        max_block_len = sess.request_download(seg.start_address, seg.length)
        print(f"    RequestDownload → maxBlockLen={max_block_len}")
        chunk_size = max_block_len - 2
        print(
            f"    TransferData ({seg.length} bytes, {chunk_size}-byte chunks)"
        )
        sess.transfer_data(seg.data, max_block_len)
        print("    RequestTransferExit")
        sess.request_transfer_exit()


def step_transfer_loop_inter_shdr(sess: "UdsSession", ctx: FlashContext) -> None:
    """Transfer loop with inter-SHDR re-auth + re-erase (GHDR v2 ramAppPayload)."""
    segments = ctx.bhx_file.segments
    for seg_idx, seg in enumerate(segments):
        print(
            f"  Step: Transfer SHDR {seg_idx}"
            f" addr=0x{seg.start_address:08X} size={seg.length} bytes"
        )
        max_block_len = sess.request_download(seg.start_address, seg.length)
        print(f"    RequestDownload → maxBlockLen={max_block_len}")
        sess.transfer_data(seg.data, max_block_len)
        print("    RequestTransferExit")
        sess.request_transfer_exit()

        is_last = seg_idx == len(segments) - 1
        if not is_last:
            print("    [inter-SHDR] RC 0x0201 checkModuleProgrammedCorrectly")
            sess.routine_control(_RC_VERIFY_CRC)
            print("    [inter-SHDR] RC 0x0202 checkCorrectComponentAndRev")
            sess.routine_control(_RC_CHECK_REV)
            print("    [inter-SHDR] DiagnosticSessionControl(PROGRAMMING)")
            sess.diagnostic_session(0x02)
            print("    [inter-SHDR] SecurityAccess")
            sess.security_access(ctx.security_level)
            print("    [inter-SHDR] moduleToProgram")
            sess.diagnostic_session(0x02)
            print("    [inter-SHDR] RC 0xFF00 initializeEraseModule")
            sess.routine_control(_RC_ERASE, b"\x01")


def step_verify_crc(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: RC 0x0201 checkModuleProgrammedCorrectly")
    sess.routine_control(_RC_VERIFY_CRC)


def step_check_rev(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: RC 0x0202 checkCorrectComponentAndRev")
    sess.routine_control(_RC_CHECK_REV)


def step_sleep_100ms(sess: "UdsSession", ctx: FlashContext) -> None:
    time.sleep(0.1)


def step_sleep_300ms(sess: "UdsSession", ctx: FlashContext) -> None:
    time.sleep(0.3)


def step_sleep_500ms(sess: "UdsSession", ctx: FlashContext) -> None:
    time.sleep(0.5)


def step_sleep_5000ms(sess: "UdsSession", ctx: FlashContext) -> None:
    time.sleep(5.0)


def step_check_flash_count_0(sess: "UdsSession", ctx: FlashContext) -> None:
    sess.check_flash_count(FLASH_COUNT_LIMITS[0])


def step_check_flash_count_1(sess: "UdsSession", ctx: FlashContext) -> None:
    sess.check_flash_count(FLASH_COUNT_LIMITS[1])


def step_check_flash_count_2(sess: "UdsSession", ctx: FlashContext) -> None:
    sess.check_flash_count(FLASH_COUNT_LIMITS[2])


def step_clear_dtc(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: ClearDiagnosticInformation")
    sess.clear_dtc()


def step_vendor_preflight(sess: "UdsSession", ctx: FlashContext) -> None:
    """RC 0x0601 — vcleft/vcleftramapp vendor pre-flash routine."""
    print("  Step: routineControl 0x0601 (vendor pre-flash)")
    sess.vendor_preflight_routine()


# ---------------------------------------------------------------------------
# FlashScript
# ---------------------------------------------------------------------------

@dataclass
class FlashScript:
    """An ordered sequence of step functions that implement one ECU flash flow.

    module_byte:     WDBI 0x0102 value sent before erase (0x00 for single-CPU)
    erase_timeout:   P2 seconds applied around RC 0xFF00 (restored to 3.0 after)
    security_level:  level_idx passed to security_access (0 = tesla_hash/0x01)
    """
    steps: list[StepFn]
    module_byte: int = 0x00
    erase_timeout: float = 3.0
    security_level: int = 0

    def run(self, sess: "UdsSession", bhx_file: object, entry: object) -> None:
        ctx = FlashContext(
            bhx_file=bhx_file,
            entry=entry,
            module_byte=self.module_byte,
            erase_timeout=self.erase_timeout,
            security_level=self.security_level,
        )
        for step in self.steps:
            step(sess, ctx)


# ---------------------------------------------------------------------------
# Script definitions (matching FIRMWARE_UPDATE.md script map)
# ---------------------------------------------------------------------------

# 0x00650fa0 — gtw3: stub only, no flash sequence
SCRIPT_GTW3 = FlashScript(steps=[])

# 0x00650fb0 — Standard: hvbms, cp, epas3p/s, epbl/r, hvp, ocs1p, sccmk, vcsec, tas
SCRIPT_STANDARD = FlashScript(
    steps=[
        step_soft_reset,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
        step_sleep_300ms,
    ],
)

# 0x00651000 — vcfront / ibstcal (prog 1: standard flash only)
SCRIPT_VCFRONT = FlashScript(
    steps=[
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
        step_sleep_500ms,
    ],
)

# 0x00651030 — vcright (prog 0: standard flash)
SCRIPT_VCRIGHT = FlashScript(
    steps=[
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
        step_sleep_500ms,
    ],
)

# 0x00651050 — vcleft (pre-flash vendor routine)
SCRIPT_VCLEFT = FlashScript(
    steps=[
        step_vendor_preflight,
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
        step_sleep_500ms,
    ],
)

# 0x00651070 — pcs/pcscpu2/di/dis/pm/pms (prog 0: extended erase timeout)
# module_byte is set per-entry from ECU_SCRIPT_MAP; erase_timeout=5s per doc
SCRIPT_PCS = FlashScript(
    steps=[
        step_soft_reset,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
        step_sleep_300ms,
    ],
    erase_timeout=5.0,
)

# 0x006510d0 — park (prog 0: extended erase timeout, 5 s post-reset sleep)
SCRIPT_PARK = FlashScript(
    steps=[
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
        step_sleep_5000ms,
    ],
    erase_timeout=1.0,
)

# 0x006510f0 — park / aps (prog 0)
SCRIPT_APS = FlashScript(
    steps=[
        step_soft_reset,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
    erase_timeout=1.0,
)

# 0x00651110 — RAM app scripts: vcleftramapp, vcrightramapp, vcfrontramapp,
#              vcsecrumapp, sccmksub (prog 0: no boardPartSerialGet)
SCRIPT_RAMAPP = FlashScript(
    steps=[
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
)

# 0x00651140 — ibst (prog 0: flash count check + DTC clear + security level 3)
SCRIPT_IBST = FlashScript(
    steps=[
        step_check_flash_count_2,
        step_clear_dtc,
        step_soft_reset,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
    security_level=3,
    erase_timeout=4.0,
)

# 0x00651170 — espcal / rcmcal (calibration flash, security level 3)
SCRIPT_ESPCAL = FlashScript(
    steps=[
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
    security_level=3,
    erase_timeout=4.0,
)

# 0x00651190 — esp (flash count check + security level 3)
SCRIPT_ESP = FlashScript(
    steps=[
        step_check_flash_count_1,
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_soft_reset,
    ],
    security_level=3,
)

# 0x006511b0 — ibstcal bootloader path (hard reset with retries)
SCRIPT_IBSTCAL = FlashScript(
    steps=[
        step_check_flash_count_0,
        step_hard_reset_with_retries,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_hard_reset_with_retries,
        step_sleep_100ms,
    ],
    security_level=3,
    erase_timeout=4.0,
)

# 0x006511d0 — rcm (Pektron: flash count + hard reset + explicit erase)
SCRIPT_RCM = FlashScript(
    steps=[
        step_check_flash_count_0,
        step_hard_reset_with_retries,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_sleep_100ms,
        step_hard_reset_with_retries,
    ],
    security_level=3,
    erase_timeout=4.0,
)

# 0x006511f0 — tpms (security level 4 — baolong_hash)
SCRIPT_TPMS = FlashScript(
    steps=[
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
    security_level=4,
    erase_timeout=3.0,
)

# 0x00651230 — cmp (security level 7 — pektron-style)
SCRIPT_CMP = FlashScript(
    steps=[
        step_soft_reset,
        step_board_info,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
    security_level=7,
)

# 0x00651270 — ptc (non-standard erase, 10 s timeout)
SCRIPT_PTC = FlashScript(
    steps=[
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
    erase_timeout=10.0,
)

# 0x00651290 — vcright/vcfront/vcsec ramapp, bleepcenter (prog 0)
SCRIPT_RAMAPP_ALT = FlashScript(
    steps=[
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
    ],
)

# 0x006512b0 — vcleftramapp (prog 0: pre-flash vendor routine)
SCRIPT_VCLEFTRAMAPP = FlashScript(
    steps=[
        step_vendor_preflight,
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
    ],
    erase_timeout=5.0,
)

# 0x006512d0 — opc / opcs (prog 1: standard flash, 3 s timeout)
SCRIPT_OPC = FlashScript(
    steps=[
        step_soft_reset,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
    erase_timeout=3.0,
)

# 0x006512e0 — ths / swc / lumbar* / bleep* (prog 2: standard flash, 3 s timeout)
SCRIPT_THS = FlashScript(
    steps=[
        step_sleep_500ms,
        step_programming_session,
        step_verify_comp_fw,
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
    erase_timeout=3.0,
)


# ---------------------------------------------------------------------------
# ECU → script map
# Keys are lowercase ecu_type values from signed_metadata_map.tsv.
# module_byte is set per-entry via a (script, module_byte) tuple so we can
# share one FlashScript instance across module variants.
# ---------------------------------------------------------------------------

# (FlashScript, module_byte)
_Entry = tuple[FlashScript, int]

ECU_SCRIPT_MAP: dict[str, _Entry] = {
    # gtw3 — stub
    "gtw3": (SCRIPT_GTW3, 0x00),

    # Standard script (0x00650fb0)
    "hvbms":  (SCRIPT_STANDARD, 0x00),
    "cp":     (SCRIPT_STANDARD, 0x00),
    "epas3p": (SCRIPT_STANDARD, 0x00),
    "epas3s": (SCRIPT_STANDARD, 0x00),
    "epbl":   (SCRIPT_STANDARD, 0x00),
    "epbr":   (SCRIPT_STANDARD, 0x00),
    "hvp":    (SCRIPT_STANDARD, 0x00),
    "ocs1p":  (SCRIPT_STANDARD, 0x00),
    "sccmk":  (SCRIPT_STANDARD, 0x00),
    "vcsec":  (SCRIPT_STANDARD, 0x00),
    "tas":    (SCRIPT_STANDARD, 0x00),

    # vcfront / ibstcal (0x00651000)
    "vcfront": (SCRIPT_VCFRONT, 0x00),
    "ibstcal": (SCRIPT_IBSTCAL, 0x00),

    # vcright (0x00651030)
    "vcright": (SCRIPT_VCRIGHT, 0x00),

    # vcleft (0x00651050)
    "vcleft": (SCRIPT_VCLEFT, 0x00),

    # pcs/pcscpu2/di/dis/pm/pms (0x00651070)
    "pcs":     (SCRIPT_PCS, 0x00),
    "pcscpu2": (SCRIPT_PCS, 0x0C),
    "pm":      (SCRIPT_PCS, 0x00),
    "pms":     (SCRIPT_PCS, 0x00),
    "di":      (SCRIPT_PCS, 0x0C),
    "dis":     (SCRIPT_PCS, 0x0C),

    # park (0x006510d0)
    "park": (SCRIPT_PARK, 0x00),

    # aps (0x006510f0)
    "aps": (SCRIPT_APS, 0x00),

    # RAM app scripts (0x00651110)
    "vcleftramapp":  (SCRIPT_RAMAPP, 0x06),
    "vcrightramapp": (SCRIPT_RAMAPP, 0x0F),
    "vcfrontramapp": (SCRIPT_RAMAPP, 0x0F),
    "vcsecrumapp":   (SCRIPT_RAMAPP, 0x0F),
    "sccmksub":      (SCRIPT_RAMAPP, 0x06),

    # ibst (0x00651140)
    "ibst": (SCRIPT_IBST, 0x00),

    # espcal / rcmcal (0x00651170)
    "espcal": (SCRIPT_ESPCAL, 0x07),
    "rcmcal": (SCRIPT_ESPCAL, 0x07),

    # esp (0x00651190)
    "esp": (SCRIPT_ESP, 0x00),

    # rcm (0x006511d0)
    "rcm": (SCRIPT_RCM, 0x00),

    # tpms (0x006511f0)
    "tpms": (SCRIPT_TPMS, 0x00),

    # cmp (0x00651230)
    "cmp": (SCRIPT_CMP, 0x00),

    # ptc (0x00651270)
    "ptc": (SCRIPT_PTC, 0x00),

    # vcright/vcfront/vcsec ramapp, bleepcenter (0x00651290)
    "bleepcenter": (SCRIPT_RAMAPP_ALT, 0x0F),

    # vcleftramapp alt (0x006512b0)
    # (same key as RAMAPP above; 0x006512b0 is the prog-0 path with vendor preflight)
    # Differentiated by ecu_type suffix in TSV when needed; default to VCLEFTRAMAPP.

    # opc / opcs (0x006512d0)
    "opc":  (SCRIPT_OPC, 0x0C),
    "opcs": (SCRIPT_OPC, 0x0C),

    # ths / swc / lumbar* / bleep* (0x006512e0)
    "ths":      (SCRIPT_THS, 0x0C),
    "swc":      (SCRIPT_THS, 0x0C),
    "lumbarl":  (SCRIPT_THS, 0x0B),
    "lumbar":   (SCRIPT_THS, 0x0B),
    "lumbarr":  (SCRIPT_THS, 0x0B),
    "bleep":    (SCRIPT_THS, 0x0F),
    "bleepleft":  (SCRIPT_THS, 0x0F),
    "bleepright": (SCRIPT_THS, 0x0F),
}


def get_script(ecu_type: str) -> _Entry:
    """Look up (FlashScript, module_byte) for an ecu_type name.

    Raises KeyError with a helpful message if the type is unknown.
    """
    key = ecu_type.lower()
    if key not in ECU_SCRIPT_MAP:
        raise KeyError(
            f"No flash script defined for ecu_type {ecu_type!r}. "
            f"Known types: {sorted(ECU_SCRIPT_MAP)}"
        )
    return ECU_SCRIPT_MAP[key]
