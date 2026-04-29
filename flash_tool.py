#!/usr/bin/env python3
"""Firmware flashing tool for Tesla Model 3 ECUs.

Usage:
  python flash_tool.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
  python flash_tool.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2 --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from uds.client import UdsSession

_SCRIPT_DIR = Path(__file__).parent
_DATA_DIR = _SCRIPT_DIR / "data"
_NODES_JSON = _DATA_DIR / "nodes.json"
_ETH_COMPACT = _DATA_DIR / "Model3_ETH.compact.json"
_ODJ_DIR = _DATA_DIR / "odj"

# DID 0xF180: bootloader/firmware version — read during identity discovery (phase 1)
# DID 0x0101: component+firmware type validation — opcode 1 (varifyCompAndFirmwareType)
_DID_BOOTLOADER_VERSION = 0xF180
_DID_COMP_AND_FW_TYPE = 0x0101

# Routine IDs used in the flash sequence (FIRMWARE_UPDATE.md steps 5-8)
_ROUTINE_ERASE_FLASH = 0xFF00    # step 5  — initializeEraseModule
_ROUTINE_VERIFY_CRC = 0x0201     # step 7  — checkModuleProgrammedCorrectly
_ROUTINE_CHECK_COMP_REV = 0x0202 # step 8  — checkCorrectComponentAndRev


def _abort(msg: str) -> None:
    print(f"\nABORT: {msg}")
    sys.exit(1)


def _print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def phase1_identity(sess: UdsSession, node_name: str) -> dict:
    """Read ECU identity via DID 0xF180. Returns a dict of parsed fields."""
    _print_section("Phase 1: Identity Discovery")

    f180 = sess.read_did(_DID_BOOTLOADER_VERSION)
    if len(f180) < 7:
        _abort(
            f"DID 0xF180 response too short ({len(f180)} bytes, expected >=7)")

    # Layout: [MODULES:1][COMPONENT_ID:2][PCBA_ID:1][ASSEMBLY_ID:1][USAGE_ID:2][...]
    component_id = (f180[1] << 8) | f180[2]
    pcba_id = f180[3]
    assembly_id = f180[4]
    usage_id = (f180[5] << 8) | f180[6]

    # version_map packed key: PPAA00UU — low byte of usage_id (UU)
    packed = (pcba_id << 24) | (assembly_id << 16) | (usage_id & 0xFF)
    lookup_key = f"{node_name.lower()}:{packed}"

    identity = {
        "f180_raw": f180.hex(),
        "component_id": component_id,
        "pcba_id": pcba_id,
        "assembly_id": assembly_id,
        "usage_id": usage_id,
        "packed_key": packed,
        "lookup_key": lookup_key,
    }

    print(f"  Node:          {node_name}")
    print(f"  COMPONENT_ID:  0x{component_id:04X}")
    print(f"  PCBA_ID:       {pcba_id}")
    print(f"  ASSEMBLY_ID:   {assembly_id}")
    print(f"  USAGE_ID:      {usage_id}")
    print(f"  lookup_key:    {lookup_key}")

    return identity


def phase2_firmware_selection(
    artifacts_dir: Path,
    identity: dict,
    node_name: str,
) -> list:
    """Load metadata, filter for this ECU identity, and return selected FirmwareEntry list."""
    from uds.metadata import load_metadata, find_firmware

    _print_section("Phase 2: Firmware Selection")

    tsv_path = artifacts_dir / "signed_metadata_map.tsv"
    if not tsv_path.exists():
        _abort(f"signed_metadata_map.tsv not found in {artifacts_dir}")

    entries = load_metadata(tsv_path)
    matches = find_firmware(entries, node_name, identity["packed_key"])

    if not matches:
        _abort(f"No firmware found for {identity['lookup_key']} in {tsv_path}")

    if len(matches) == 1:
        selected = matches
    else:
        print(f"  Found {len(matches)} firmware options:")
        for i, e in enumerate(matches):
            cond_str = ",".join(
                f"{k}={v}" for k, v in e.conditions.items()) or "*"
            print(
                f"  [{i}] {e.src_path} → {e.dest_name}  crc={e.crc}  cond={cond_str}")
        # If multiple are different dest_names (CPU1/CPU2), use all; else ask user
        dest_names = {e.dest_name for e in matches}
        if len(dest_names) == len(matches):
            # Each entry has a unique dest (e.g. pcs.bhx + pcscpu2.bhx) — flash all
            selected = matches
        else:
            raw = input("  Select index (or 'all'): ").strip()
            if raw.lower() == "all":
                selected = matches
            else:
                try:
                    selected = [matches[int(raw)]]
                except (ValueError, IndexError):
                    _abort(f"Invalid selection: {raw!r}")

    for e in selected:
        src = artifacts_dir / e.src_path
        if not src.exists():
            _abort(f"Firmware file not found: {src}")
        print(f"  Selected: {e.src_path} → {e.dest_name}  crc={e.crc}")

    return selected


def _decode_pcs_identity(data: bytes) -> dict | None:
    """Decode the 32-byte Tesla C28x ECU identity header from segment data.

    Returns None if the data doesn't match the expected layout (words 4 and 5
    must have 0xFFFF in the low half).
    """
    import struct
    if len(data) < 32:
        return None
    w0, w1, _, _, w4, w5 = struct.unpack_from(">IIIIII", data, 0)
    if (w4 & 0xFFFF) != 0xFFFF or (w5 & 0xFFFF) != 0xFFFF:
        return None
    return {
        "component_id": (w0 >> 16) & 0xFFFF,
        "assembly_id": (w0 >> 8) & 0xFF,
        "pcba_id": w0 & 0xFF,
        "usage_id": (w1 >> 16) & 0xFFFF,  # high 16 bits of word 1
    }


def phase3_preflight(artifacts_dir: Path, selected: list, identity: dict, force: bool) -> None:
    """Cross-check BHX identity header against DID reads."""
    import bhx

    _print_section("Phase 3: Pre-flight Verification")

    for entry in selected:
        src = artifacts_dir / entry.src_path
        bhx_file = bhx.parse_file(src)
        found_identity = False
        for seg in bhx_file.segments:
            ident = _decode_pcs_identity(seg.data)
            if not ident:
                continue
            found_identity = True
            bhx_component_id = ident["component_id"]
            mismatches = []
            if ident["pcba_id"] != identity["pcba_id"]:
                mismatches.append(
                    f"PCBA_ID: BHX={ident['pcba_id']} vs ECU={identity['pcba_id']}")
            if ident["assembly_id"] != identity["assembly_id"]:
                mismatches.append(
                    f"ASSEMBLY_ID: BHX={ident['assembly_id']} vs ECU={identity['assembly_id']}")
            if ident["usage_id"] != identity["usage_id"]:
                mismatches.append(
                    f"USAGE_ID: BHX={ident['usage_id']} vs ECU={identity['usage_id']}")

            if mismatches:
                for m in mismatches:
                    print(f"  MISMATCH: {m}")
                if not force:
                    _abort("BHX identity mismatch. Use --force to override.")
                else:
                    print("  (--force: proceeding despite mismatch)")
            else:
                print(
                    f"  {entry.src_path}: identity OK (component_id=0x{bhx_component_id:04X})")
        if not found_identity:
            print(f"  {entry.src_path}: no identity header found, skipping pre-flight check")


def _inter_shdr_cycle(sess: UdsSession) -> None:
    """Re-auth + re-erase between SHDRs for GHDR v2 ramAppPayload ECUs (step 6d)."""
    print("    [inter-SHDR] RC 0x0201 checkModuleProgrammedCorrectly")
    sess.routine_control(_ROUTINE_VERIFY_CRC)
    print("    [inter-SHDR] RC 0x0202 checkCorrectComponentAndRev")
    sess.routine_control(_ROUTINE_CHECK_COMP_REV)
    print("    [inter-SHDR] DiagnosticSessionControl(PROGRAMMING)")
    sess.diagnostic_session(0x02)
    print("    [inter-SHDR] SecurityAccess")
    sess.security_access()
    print("    [inter-SHDR] moduleToProgram DiagnosticSessionControl(PROGRAMMING)")
    sess.diagnostic_session(0x02)
    print("    [inter-SHDR] RC 0xFF00 initializeEraseModule")
    sess.routine_control(_ROUTINE_ERASE_FLASH, b"\x01")


def phase4_flash(sess: UdsSession, artifacts_dir: Path, selected: list) -> None:
    """Execute the UDS flash sequence (steps 0-9) for each firmware file."""
    import bhx

    for fw_index, entry in enumerate(selected):
        _print_section(f"Phase 4: Flash Sequence — {entry.dest_name}")

        src = artifacts_dir / entry.src_path
        bhx_file = bhx.parse_file(src)
        needs_inter_shdr = (
            bhx_file.ghdr_version == 2 and bhx_file.ram_app_payload == 1
        )

        # Step 0: Soft reset — fire and forget, ECU reboots into bootloader
        print("  Step 0: ECUReset (soft, no response wait)")
        sess.ecu_reset_no_wait(0x01)

        # Step 1: Read board part/serial DIDs (logged only, does not gate flash)
        print("  Step 1: Board part/serial DIDs (0xF012-0xF015)")
        for did in (0xF012, 0xF013, 0xF014, 0xF015):
            try:
                data = sess.read_did(did)
                print(f"    0x{did:04X}: {data.decode('ascii', errors='replace').rstrip(chr(0))!r}")
            except Exception:
                pass

        # Step 2: Enter programming session, then start keepalive
        print("  Step 2: DiagnosticSessionControl(PROGRAMMING)")
        sess.diagnostic_session(0x02)
        print("  Starting TesterPresent keepalive...")
        sess.start_tester_present()

        try:
            # Step 3: Verify component and firmware type
            print("  Step 3: ReadDataByIdentifier COMP_AND_FW_TYPE (0x0101)")
            comp_fw = sess.read_did(_DID_COMP_AND_FW_TYPE)
            if len(comp_fw) < 3:
                _abort(
                    f"DID 0x0101 too short ({len(comp_fw)} bytes, expected 3)")
            print(
                f"    component_key=0x{comp_fw[0]:02X}"
                f"  fw_type=0x{comp_fw[1]:02X}"
                f"  protocol_ver=0x{comp_fw[2]:02X}")

            # Step 4: Security access
            print("  Step 4: SecurityAccess")
            sess.security_access()

            # Step 5: Erase flash sectors
            print("  Step 5: RC 0xFF00 initializeEraseModule")
            sess.routine_control(_ROUTINE_ERASE_FLASH, b"\x01")

            # Step 6: Per-SHDR transfer loop
            for seg_idx, seg in enumerate(bhx_file.segments):
                print(
                    f"  Step 6 [SHDR {seg_idx}]:"
                    f" addr=0x{seg.start_address:08X}"
                    f" size={seg.length} bytes")

                # 6a: RequestDownload
                max_block_len = sess.request_download(
                    seg.start_address, seg.length)
                print(f"    RequestDownload → maxBlockLen={max_block_len}")

                # 6b: TransferData
                chunk_size = max_block_len - 2
                print(
                    f"    TransferData ({seg.length} bytes,"
                    f" {chunk_size}-byte chunks)")
                sess.transfer_data(seg.data, max_block_len)

                # 6c: RequestTransferExit
                print("    RequestTransferExit")
                sess.request_transfer_exit()

                # 6d: Inter-SHDR re-auth + re-erase (GHDR v2 ramAppPayload only)
                is_last_seg = seg_idx == len(bhx_file.segments) - 1
                if needs_inter_shdr and not is_last_seg:
                    _inter_shdr_cycle(sess)

            # Step 7: Verify programming (CRC check)
            print("  Step 7: RC 0x0201 checkModuleProgrammedCorrectly")
            sess.routine_control(_ROUTINE_VERIFY_CRC)

            # Step 8: Verify component / revision match
            print("  Step 8: RC 0x0202 checkCorrectComponentAndRev")
            sess.routine_control(_ROUTINE_CHECK_COMP_REV)

        finally:
            sess.stop_tester_present()
            print("  TesterPresent stopped.")

        # Step 9: Hard reset (wait for positive response), then done
        print("  Step 9: ECUReset (hard reset, wait for response)")
        sess.ecu_reset(0x01)
        print("  Flash complete." if fw_index == len(selected) - 1
              else "  Continuing to next firmware file...")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tesla Model 3 ECU firmware flashing tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--node", "-n", required=True,
                        help="ECU node name (e.g. PCS)")
    parser.add_argument("--channel", "-c", default="vcan0",
                        help="CAN interface (default: vcan0)")
    parser.add_argument("--interface", "-i", default="socketcan",
                        help="python-can interface type")
    parser.add_argument("--artifacts", "-a", required=True,
                        help="Path to seed_artifacts_v2 directory (contains signed_metadata_map.tsv)")
    parser.add_argument("--force", action="store_true",
                        help="Skip pre-flight identity mismatch abort")
    args = parser.parse_args()

    artifacts_dir = Path(args.artifacts).expanduser().resolve()
    if not artifacts_dir.is_dir():
        print(f"Error: artifacts directory not found: {artifacts_dir}")
        return 1

    from uds.node_config import load_node_config

    cfg = load_node_config(args.node, _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)

    try:
        with UdsSession(cfg, args.channel, interface=args.interface) as sess:
            # Phase 1: Read identity
            sess.diagnostic_session(0x01)
            identity = phase1_identity(sess, args.node)

            # Phase 2: Select firmware
            selected = phase2_firmware_selection(
                artifacts_dir, identity, args.node)

            # Phase 3: Pre-flight check
            phase3_preflight(artifacts_dir, selected, identity, args.force)

            # Phase 4: Flash
            confirm = input("\nProceed with flashing? [y/N] ").strip().lower()
            if confirm != "y":
                print("Aborted by user.")
                return 0

            phase4_flash(sess, artifacts_dir, selected)

    except KeyboardInterrupt:
        print("\nAborted.")
        return 1
    except Exception as e:
        print(f"\nError: {e}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
