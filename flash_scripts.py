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
    protocol_ver: int | None = None  # set by step_verify_comp_fw, consumed by step_security_access
    expected_fw_type: int = 0x01  # 1 = regular firmware; 2 = bootloader image
    # CAN access plumbing for steps that need to open a transient session to
    # another ECU (e.g. SCRIPT_BL_UPDATER_VCFRONT's VCRIGHT prep). Populated by
    # phase 4 / `FlashScript.run` from the caller's CLI args.
    channel: str | None = None
    interface: str | None = None


# Seed level table from DAT_00650e08[idx*16] (uds_security_access at 0x0040c090).
# idx 0 is overridden to 0x01 if protocol_ver < 3.
_SECURITY_SEED_LEVEL = {
    0: 0x05,
    1: 0x01,
    2: 0x05,
    3: 0x11,
    4: 0x11,
    5: 0x03,
    6: 0x01,
    7: 0x05,
}


StepFn = Callable[["UdsSession", FlashContext], None]


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

def step_soft_reset(sess: "UdsSession", ctx: FlashContext) -> None:
    """ECUReset `11 81` (subfunction 0x01 with SPR set) — fire-and-forget.

    Matches VM `reset(0)`. Used both as the opening reset that hands control to
    the bootloader (must be followed by `step_wait_for_bootloader`) and as the
    trailing reset that returns control to the application (no handover wait
    needed).
    """
    print("  Step: ECUReset 11 81 (SPR, no wait)")
    sess.ecu_reset_no_wait(0x01)
    sess.sleep(0.5)


def step_wait_for_bootloader(sess: "UdsSession", ctx: FlashContext) -> None:
    """Wait for the bootloader to take over after a reset (VM `enterBootloader(0)`).

    Required after the opening reset of every flash script. Without this, the
    application services DSC / RDBI / SecurityAccess (they all succeed there)
    but `WDBI 0x0102` (`moduleToProgram`) is bootloader-only and gets rejected.
    """
    print("  Step: Wait for bootloader handover")
    sess.wait_for_bootloader()


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


def step_start_tester_present(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: TesterPresent keepalive start")
    sess.start_tester_present()


def step_stop_tester_present(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: TesterPresent keepalive stop")
    sess.stop_tester_present()


def step_programming_session(sess: "UdsSession", ctx: FlashContext) -> None:
    print("  Step: DiagnosticSessionControl(PROGRAMMING)")
    sess.diagnostic_session(0x02)
    sess.sleep(0.5)  # ECU reboots into bootloader after programming session


def step_verify_comp_fw(sess: "UdsSession", ctx: FlashContext) -> None:
    """RDBI 0x0101 — validate fw_type, stash protocol_ver on the context.

    Wire byte order (per ODJ + VM `uds_varify_comp_and_firmware`):
      byte[0] = COMPONENT_KEY (informational)
      byte[1] = FIRMWARE_TYPE — must be 0x01 for prog-0 flash flows
      byte[2] = BOOTLOADER_PROTOCOL_VERSION — drives security level branching
    """
    print("  Step: ReadDataByIdentifier COMP_AND_FW_TYPE (0x0101)")
    comp_fw = sess.read_did(0x0101)
    if len(comp_fw) < 3:
        from uds_local.client import UdsError
        raise UdsError(0x22, 0x00)
    component_key = comp_fw[0]
    fw_type = comp_fw[1]
    protocol_ver = comp_fw[2]
    print(
        f"    component_key=0x{component_key:02X}"
        f"  fw_type=0x{fw_type:02X}"
        f"  protocol_ver=0x{protocol_ver:02X}"
    )
    if fw_type != ctx.expected_fw_type:
        raise ValueError(
            f"Unexpected FIRMWARE_TYPE 0x{fw_type:02X} at DID 0x0101 "
            f"(expected 0x{ctx.expected_fw_type:02X} — wrong file or wrong ECU?)"
        )
    ctx.protocol_ver = protocol_ver


def step_security_access(sess: "UdsSession", ctx: FlashContext) -> None:
    """SecurityAccess with the seed level chosen from protocol_ver + idx."""
    idx = ctx.security_level
    base_level = _SECURITY_SEED_LEVEL.get(idx, idx * 2 + 1)
    if idx == 0 and ctx.protocol_ver is not None and ctx.protocol_ver < 3:
        seed_level = 0x01  # legacy-protocol override
    else:
        seed_level = base_level
    print(
        f"  Step: SecurityAccess (idx={idx} protocol_ver={ctx.protocol_ver}"
        f" seed_level=0x{seed_level:02X})"
    )
    sess.security_access(level_idx=idx, seed_level=seed_level)


def step_module_to_program(sess: "UdsSession", ctx: FlashContext) -> None:
    """Set extended timeout then select CPU/flash region (WDBI 0x0102).

    netSetTimeout must precede moduleToProgram per the flash protocol.
    """
    print(f"  Step: moduleToProgram (module=0x{ctx.module_byte:02X})")
    sess.set_timeout(ctx.erase_timeout)
    sess.module_to_program(ctx.module_byte)


def step_erase(sess: "UdsSession", ctx: FlashContext) -> None:
    """RC 0xFF00 initializeEraseModule (timeout already set by step_module_to_program)."""
    print(f"  Step: RC 0xFF00 initializeEraseModule (P2={ctx.erase_timeout}s)")
    sess.start_tester_present()
    try:
        sess.routine_control(_RC_ERASE, b"\x01")
    finally:
        sess.set_timeout(3.0)
        sess.stop_tester_present()


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


def step_sleep_1000ms(sess: "UdsSession", ctx: FlashContext) -> None:
    """Wait 1s — used at the start of SCRIPT_BL to let the bu update agent boot."""
    print("  Step: sleep 1000ms (waiting for bootloader-update agent)")
    time.sleep(1.0)


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


def step_vcright_ota_prep(sess: "UdsSession", ctx: FlashContext) -> None:
    """sub4 (`0x006513a0`) — VCRIGHT-side OTA prep before flashing VCFRONT bu.

    Opens a transient UDS session to VCRIGHT (CAN IDs 0x608/0x609) on the
    same physical CAN channel as the parent VCFRONT session, runs:

        diagnosticSession(3)            extended session
        setSecurityAccessLevel(3)       internal — sets the override gate
        securityAccess(0)               seed level 0x05 (the override-protected path)
        VCWaitForOTAMode(0)             RC 0x540 polling, response[0] must == 2
        vcFrontLockoutIOControl(1)      IOCBI 0x218 controlParam=3, byte 1
        restoreUdsContext(0)            (handled by closing the session)

    then closes the VCRIGHT session.

    **Operational prerequisite:** VCWaitForOTAMode requires the vehicle to be
    actively in OTA state — initiated by the vehicle's overall state machine,
    not by this tool. On a bench setup this will time out.
    """
    if ctx.channel is None:
        raise RuntimeError(
            "step_vcright_ota_prep needs a CAN channel on FlashContext. "
            "Pass it via FlashScript.run(channel=..., interface=...)."
        )

    # Local imports to avoid pulling these into the module-level dependency
    # graph (keeps unit tests light).
    import config as _cfg
    from uds_local.client import UdsSession
    from uds_local.node_config import load_node_config

    print("  Step: VCRIGHT-side OTA prep (sub4) — opening transient session")
    vcright_cfg = load_node_config(
        "vcright", _cfg.NODES_JSON, _cfg.ETH_COMPACT, _cfg.ODJ_DIR
    )

    interface = ctx.interface or "socketcan"
    with UdsSession(vcright_cfg, ctx.channel, interface=interface) as vsess:
        print("    [VCRIGHT] DiagnosticSessionControl(EXTENDED 0x03)")
        vsess.diagnostic_session(0x03)
        # setSecurityAccessLevel(3) is internal: it sets context+0x02 = 3 so
        # the protocol_ver < 3 override does NOT fire when securityAccess(0)
        # runs. We replicate that by passing seed_level=0x05 explicitly — the
        # table value at idx 0 — bypassing the protocol_ver branch altogether.
        print("    [VCRIGHT] SecurityAccess (idx=0, forced seed_level=0x05)")
        vsess.security_access(level_idx=0, seed_level=0x05)
        print("    [VCRIGHT] VCWaitForOTAMode (RC 0x0540 poll for response[0]==2)")
        vsess.wait_for_ota_mode()
        print("    [VCRIGHT] IOCBI 0x0218 shortTermAdjustment, control byte 0x01")
        vsess.io_control_short_term_adjustment(0x0218, 1)
        print("    [VCRIGHT] closing transient session (restoreUdsContext)")


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
    expected_fw_type: int = 0x01  # 1 = regular firmware; 2 = bootloader image (for SCRIPT_BL)

    def run(
        self,
        sess: "UdsSession",
        bhx_file: object,
        entry: object,
        channel: str | None = None,
        interface: str | None = None,
    ) -> None:
        ctx = FlashContext(
            bhx_file=bhx_file,
            entry=entry,
            module_byte=self.module_byte,
            erase_timeout=self.erase_timeout,
            security_level=self.security_level,
            expected_fw_type=self.expected_fw_type,
            channel=channel,
            interface=interface,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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
        step_wait_for_bootloader,
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

# 0x00651300 — bootloader-updater (`*bu` files: parkbu, hvbmsbu, hvpbu)
# Standard prog-0 flash with fw_type=1 — flashes the update agent into the regular
# app slot. Decoded from the binary at 0x00651300:
#   reset(0) + enterBootloader(0)
#   diagnosticSession(2)  netSetTimeout(3)
#   varifyCompAndFirmwareType(1)  securityAccess(0)
#   CALL sub1 [moduleToProgram(0) + erase + transfer + RET]
#   checkModuleProgrammed  checkCorrectComponentAndRev
#   reset(0)  halt
SCRIPT_BL_UPDATER = FlashScript(
    steps=[
        step_soft_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,           # expected_fw_type=1 (default)
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

# 0x00651340 — bootloader image (`*bl` files: parkbl, hvbmsbl, hvpbl)
# Runs immediately after SCRIPT_BL_UPDATER without an intervening reset/handover —
# the bu's trailing reset boots the update agent, and step_sleep_1000ms gives it
# time to come up. Then DSC + verify(fw_type=2) + auth + erase + transfer +
# verify + reset against the agent. Decoded from binary at 0x00651340:
#   sleep(1000ms)
#   diagnosticSession(2)
#   varifyCompAndFirmwareType(2)  ← fw_type = 2 (BOOTLOADER)
#   securityAccess(0)  netSetTimeout(3)
#   CALL sub1 [moduleToProgram(0) + erase + transfer + RET]
#   checkModuleProgrammed  checkCorrectComponentAndRev
#   reset(0)  halt
SCRIPT_BL = FlashScript(
    steps=[
        step_sleep_1000ms,
        step_programming_session,
        step_verify_comp_fw,           # expected_fw_type=2 set below
        step_security_access,
        step_module_to_program,
        step_erase,
        step_transfer_loop,
        step_verify_crc,
        step_check_rev,
        step_soft_reset,
    ],
    erase_timeout=3.0,
    expected_fw_type=0x02,
)

# 0x00651320 — vcfront-specific bootloader-updater (`vcfrontbu`)
# Same as SCRIPT_BL_UPDATER but with a `CALL sub4` preamble that opens a
# transient session to VCRIGHT, runs OTA-mode prep + IOCBI lockout, and
# closes it. Used because VCFRONT cannot enter the bootloader-update flow
# without VCRIGHT being held in a coordinated OTA state first.
#
# Requires: VCRIGHT reachable on the same CAN channel; vehicle actively in
# OTA state (RC 0x0540 must return response[0]==2).
SCRIPT_BL_UPDATER_VCFRONT = FlashScript(
    steps=[
        step_vcright_ota_prep,         # ← sub4 (VCRIGHT detour)
        step_soft_reset,
        step_wait_for_bootloader,
        step_programming_session,
        step_verify_comp_fw,           # expected_fw_type=1 (default)
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
    # NOTE: module bytes here are from the binary's node table at +0x20.
    # Earlier versions of this map had `0x00` for all of these, which works
    # only because most single-CPU bootloaders ignore the operand. The
    # binary's authoritative values are below.
    "hvbms":  (SCRIPT_STANDARD, 0x02),
    "cp":     (SCRIPT_STANDARD, 0x05),
    "epas3p": (SCRIPT_STANDARD, 0x00),  # TODO: verify against binary
    "epas3s": (SCRIPT_STANDARD, 0x00),  # TODO: verify against binary
    "epbl":   (SCRIPT_STANDARD, 0x00),  # TODO: verify against binary
    "epbr":   (SCRIPT_STANDARD, 0x00),  # TODO: verify against binary
    "hvp":    (SCRIPT_STANDARD, 0x00),  # TODO: verify against binary
    "ocs1p":  (SCRIPT_STANDARD, 0x00),  # TODO: verify against binary
    "sccmk":  (SCRIPT_STANDARD, 0x00),  # TODO: verify against binary
    "vcsec":  (SCRIPT_STANDARD, 0x1B),
    "tas":    (SCRIPT_STANDARD, 0x00),  # TODO: verify against binary

    # CP PLC modem subcomponents — flashed via the CP MCU's bootloader using the
    # same SCRIPT_STANDARD as the regular CP app. Module byte is 0x05 (CP MCU);
    # the CP MCU's bootloader routes the .hex file contents to the PLC modem
    # over its internal interconnect based on each record's address range.
    # `cpPlcFw` is the modem firmware, `cpPlcPib` is the modem PIB
    # (Personality Identifier Block — modem config).
    "cpplcfw":  (SCRIPT_STANDARD, 0x05),
    "cpplcpib": (SCRIPT_STANDARD, 0x05),

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

    # Bootloader-updater pairs (`*bu` first, then `*bl`) — see _BL_PARENT_NODE.
    # Module bytes are from the binary's node table (+0x20). They use the
    # parent ECU's CAN IDs; nothing extra to set up at the transport layer.
    # Scripts: bu uses 0x00651300, bl uses 0x00651340.
    "parkbu":    (SCRIPT_BL_UPDATER,         0x12),
    "parkbl":    (SCRIPT_BL,                 0x12),
    "hvbmsbu":   (SCRIPT_BL_UPDATER,         0x02),
    "hvbmsbl":   (SCRIPT_BL,                 0x02),
    "hvpbu":     (SCRIPT_BL_UPDATER,         0x0E),
    "hvpbl":     (SCRIPT_BL,                 0x0E),
    "vcfrontbu": (SCRIPT_BL_UPDATER_VCFRONT, 0x0D),
    "vcfrontbl": (SCRIPT_BL,                 0x0D),
}


# Suffix → parent ECU node name for bootloader nodes. Used by phase 2 to
# verify that the user's --node argument can drive the bootloader flash, and
# by display logic to group bu+bl with the parent app entry.
_BL_PARENT_NODE: dict[str, str] = {
    "parkbu":    "park",
    "parkbl":    "park",
    "hvbmsbu":   "hvbms",
    "hvbmsbl":   "hvbms",
    "hvpbu":     "hvp",
    "hvpbl":     "hvp",
    "vcfrontbu": "vcfront",
    "vcfrontbl": "vcfront",
}


def is_bootloader_ecu_type(ecu_type: str) -> bool:
    """True if ecu_type names a bootloader updater/image (bu/bl)."""
    return ecu_type.lower() in _BL_PARENT_NODE


def parent_node_for_bootloader(ecu_type: str) -> str | None:
    """Return the parent ECU node name for a bootloader ecu_type, or None."""
    return _BL_PARENT_NODE.get(ecu_type.lower())


def find_bootloader_entries(selected: list) -> tuple[list, list, list]:
    """Split `selected` into (bu_entries, bl_entries, app_entries).

    `bu` and `bl` lists hold the bootloader-update entries; `app_entries`
    is everything else (regular firmware, ramapp, etc.). Order within each
    list preserves the input order.
    """
    bus, bls, apps = [], [], []
    for e in selected:
        ecu_type = e.component.lower()
        if ecu_type.endswith("bu") and ecu_type in _BL_PARENT_NODE:
            bus.append(e)
        elif ecu_type.endswith("bl") and ecu_type in _BL_PARENT_NODE:
            bls.append(e)
        else:
            apps.append(e)
    return bus, bls, apps


# ECU subcomponents that are flashed alongside their parent app via the parent's
# UDS endpoint (parent MCU bootloader routes the file contents to the
# subcomponent based on address ranges). Maps subcomponent ecu_type → parent.
# The CP MCU's bootloader handles cpPlcFw (PLC modem firmware) and cpPlcPib
# (PLC modem Personality Identifier Block) over its internal interconnect.
_SUBCOMPONENT_PARENT: dict[str, str] = {
    "cpplcfw":  "cp",
    "cpplcpib": "cp",
}


def is_subcomponent_ecu_type(ecu_type: str) -> bool:
    """True if ecu_type names a subcomponent flashed via a parent ECU."""
    return ecu_type.lower() in _SUBCOMPONENT_PARENT


def parent_node_for_subcomponent(ecu_type: str) -> str | None:
    """Return the parent ECU node name for a subcomponent ecu_type, or None."""
    return _SUBCOMPONENT_PARENT.get(ecu_type.lower())


def find_subcomponent_entries(selected: list) -> tuple[list, list]:
    """Split `selected` into (subcomponent_entries, other_entries).

    Subcomponents are entries whose ecu_type is in `_SUBCOMPONENT_PARENT` (e.g.
    `cpPlcFw`, `cpPlcPib`). Order within each list preserves input order.
    """
    subs, others = [], []
    for e in selected:
        if e.component.lower() in _SUBCOMPONENT_PARENT:
            subs.append(e)
        else:
            others.append(e)
    return subs, others


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


# ---------------------------------------------------------------------------
# Dual-CPU prog 1 (script 0x00651070): one authenticated session, both CPUs
# ---------------------------------------------------------------------------
#
# Script 0x00651070 prog 1 flashes both CPUs of a PCS-family ECU in a single
# authenticated session. CPU2 (the secondary, ecu_type=pcscpu2/di/dis) is
# flashed first with bootloader-internal module byte 0x04, then CPU1
# (ecu_type=pcs/pm/pms) with module byte 0x00. The 0x04/0x00 codes are
# distinct from the prog-0 node module bytes (0x0C / 0x00) — see
# FIRMWARE_UPDATE.md "Prog 1 module bytes vs node module bytes".
#
# When find_firmware returns both a primary and a secondary entry for the same
# lookup, dfu.py auto-switches to this sequence instead of running prog 0
# twice.

# ecu_type → role in PCS-family dual-CPU pairings
_PCS_PRIMARY_TYPES   = frozenset({"pcs", "pm", "pms"})
_PCS_SECONDARY_TYPES = frozenset({"pcscpu2", "di", "dis"})

# Bootloader-internal module bytes used by prog 1 (NOT the prog-0 node bytes)
_PROG1_MODULE_SECONDARY = 0x04
_PROG1_MODULE_PRIMARY   = 0x00


def find_dual_cpu_pair(selected: list) -> tuple[object, object] | None:
    """Detect a PCS-family dual-CPU pairing in `selected`.

    Returns `(primary_entry, secondary_entry)` if both a primary and secondary
    PCS-family ecu_type are present; otherwise `None`. Each entry must expose a
    `component` attribute (matches metadata.FirmwareEntry).
    """
    primary: object | None = None
    secondary: object | None = None
    for entry in selected:
        ecu_type = entry.component.lower()
        if ecu_type in _PCS_PRIMARY_TYPES and primary is None:
            primary = entry
        elif ecu_type in _PCS_SECONDARY_TYPES and secondary is None:
            secondary = entry
    if primary is not None and secondary is not None:
        return primary, secondary
    return None


def run_pcs_dual_cpu(
    sess: "UdsSession",
    primary_bhx: object,
    primary_entry: object,
    secondary_bhx: object,
    secondary_entry: object,
) -> None:
    """Execute script 0x00651070 prog 1 — both CPUs in one authenticated session.

    Sequence (matches the decoded VM bytecode at 0x00651070+0x30):
      reset(soft) + enterBootloader(0)
      diagnosticSession(2)
      varifyCompAndFirmwareType(1)
      securityAccess(0)                       — protocol_ver-aware
      moduleToProgram(4)   netSetTimeout(30)  — CPU2/secondary
      initializeEraseModule(0)
      netSetTimeout(1)
      transferData(secondary_bhx)
      checkModuleProgrammedCorrectly
      moduleToProgram(0)   netSetTimeout(30)  — CPU1/primary
      initializeEraseModule(0)
      transferData(primary_bhx)
      netSetTimeout(4)
      checkModuleProgrammedCorrectly
      checkCorrectComponentAndRev
      reset(soft)
    """
    print(f"  Dual-CPU sequence (prog 1, single auth session):")
    print(f"    primary   ({primary_entry.dest_name}, ecu_type={primary_entry.component})"
          f"  → moduleToProgram(0x{_PROG1_MODULE_PRIMARY:02X})")
    print(f"    secondary ({secondary_entry.dest_name}, ecu_type={secondary_entry.component})"
          f"  → moduleToProgram(0x{_PROG1_MODULE_SECONDARY:02X}) [flashed first]")

    print("  Step: ECUReset 11 81 (SPR, no wait)")
    sess.ecu_reset_no_wait(0x01)
    sess.sleep(0.5)
    print("  Step: Wait for bootloader handover")
    sess.wait_for_bootloader()

    print("  Step: DiagnosticSessionControl(PROGRAMMING)")
    sess.diagnostic_session(0x02)
    sess.sleep(0.5)

    print("  Step: ReadDataByIdentifier COMP_AND_FW_TYPE (0x0101)")
    comp_fw = sess.read_did(0x0101)
    if len(comp_fw) < 3:
        from uds_local.client import UdsError
        raise UdsError(0x22, 0x00)
    component_key, fw_type, protocol_ver = comp_fw[0], comp_fw[1], comp_fw[2]
    print(
        f"    component_key=0x{component_key:02X}"
        f"  fw_type=0x{fw_type:02X}"
        f"  protocol_ver=0x{protocol_ver:02X}"
    )
    if fw_type != 0x01:
        raise ValueError(
            f"Unexpected FIRMWARE_TYPE 0x{fw_type:02X} at DID 0x0101 (expected 0x01)"
        )

    seed_level = 0x01 if protocol_ver < 3 else 0x05
    print(
        f"  Step: SecurityAccess (idx=0 protocol_ver={protocol_ver}"
        f" seed_level=0x{seed_level:02X})"
    )
    sess.security_access(level_idx=0, seed_level=seed_level)

    # ---- CPU2 / secondary first ----
    _flash_one_cpu_in_session(
        sess,
        module_byte=_PROG1_MODULE_SECONDARY,
        bhx_file=secondary_bhx,
        label=f"secondary ({secondary_entry.component})",
        verify_rev_at_end=False,
    )

    # ---- CPU1 / primary second ----
    _flash_one_cpu_in_session(
        sess,
        module_byte=_PROG1_MODULE_PRIMARY,
        bhx_file=primary_bhx,
        label=f"primary ({primary_entry.component})",
        verify_rev_at_end=True,
    )

    print("  Step: ECUReset 11 81 (SPR, no wait) — return to application")
    sess.ecu_reset_no_wait(0x01)
    sess.sleep(0.3)


def _flash_one_cpu_in_session(
    sess: "UdsSession",
    module_byte: int,
    bhx_file: object,
    label: str,
    verify_rev_at_end: bool,
) -> None:
    """One CPU's slice of prog 1 — assumes session + auth already established."""
    print(f"  --- {label} ---")
    print(f"  Step: netSetTimeout(30) + moduleToProgram(0x{module_byte:02X})")
    sess.set_timeout(30.0)
    sess.module_to_program(module_byte)

    print("  Step: RC 0xFF00 initializeEraseModule (P2=30s)")
    sess.start_tester_present()
    try:
        sess.routine_control(_RC_ERASE, b"\x01")
    finally:
        sess.stop_tester_present()

    print("  Step: netSetTimeout(1)")
    sess.set_timeout(1.0)

    for seg_idx, seg in enumerate(bhx_file.segments):
        print(
            f"  Step: Transfer SHDR {seg_idx}"
            f" addr=0x{seg.start_address:08X} size={seg.length} bytes"
        )
        max_block_len = sess.request_download(seg.start_address, seg.length)
        sess.transfer_data(seg.data, max_block_len)
        sess.request_transfer_exit()

    if verify_rev_at_end:
        print("  Step: netSetTimeout(4)")
        sess.set_timeout(4.0)
    print("  Step: RC 0x0201 checkModuleProgrammedCorrectly")
    sess.routine_control(_RC_VERIFY_CRC)
    if verify_rev_at_end:
        print("  Step: RC 0x0202 checkCorrectComponentAndRev")
        sess.routine_control(_RC_CHECK_REV)
