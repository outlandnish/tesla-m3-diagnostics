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

_SCRIPT_DIR = Path(__file__).parent
_DATA_DIR = _SCRIPT_DIR / "data"
_NODES_JSON = _DATA_DIR / "nodes.json"
_ETH_COMPACT = _DATA_DIR / "Model3_ETH.compact.json"
_ODJ_DIR = _DATA_DIR / "odj"

# DIDs used during identity discovery
_DID_BOOTLOADER_VERSION = 0xF180  # 19 bytes: MODULES, COMPONENT_ID, PCBA_ID, ASSEMBLY_ID, USAGE_ID, ...

# Flash sequence routine IDs (from odin-architecture.md)
_ROUTINE_ERASE_FLASH           = 0xFF00
_ROUTINE_CHECK_COMP_REV        = 0x0202
_ROUTINE_VERIFY_CRC            = 0x0201


def _abort(msg: str) -> None:
    print(f"\nABORT: {msg}")
    sys.exit(1)


def _print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def phase1_identity(sess, node_name: str) -> dict:
    """Read ECU identity via DID 0xF180. Returns a dict of parsed fields."""
    _print_section("Phase 1: Identity Discovery")

    f180 = sess.read_did(_DID_BOOTLOADER_VERSION)
    if len(f180) < 6:
        _abort(f"DID 0xF180 response too short ({len(f180)} bytes, expected >=6)")

    # Layout: [MODULES:1][COMPONENT_ID:2][PCBA_ID:1][ASSEMBLY_ID:1][USAGE_ID:1][...]
    component_id = (f180[1] << 8) | f180[2]
    pcba_id      = f180[3]
    assembly_id  = f180[4]
    usage_id     = f180[5]

    # version_map packed key: PPAA00UU (big-endian 32-bit)
    packed = (pcba_id << 24) | (assembly_id << 16) | usage_id
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
            cond_str = ",".join(f"{k}={v}" for k, v in e.conditions.items()) or "*"
            print(f"  [{i}] {e.src_path} → {e.dest_name}  crc={e.crc}  cond={cond_str}")
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


def phase3_preflight(artifacts_dir: Path, selected: list, identity: dict, force: bool) -> None:
    """Cross-check BHX identity header against DID reads."""
    from bhx_parser import parse_bhx

    _print_section("Phase 3: Pre-flight Verification")

    for entry in selected:
        src = artifacts_dir / entry.src_path
        report = parse_bhx(src)
        for sec in report["sections"]:
            ident = sec.get("identity")
            if not ident:
                continue
            # component_id is hex string like "0x001b"
            bhx_component_id = int(ident["component_id"], 16)
            bhx_pcba_id = ident["pcba_id"]
            bhx_assembly_id = ident["assembly_id"]
            bhx_usage_id = ident["usage_id"]

            mismatches = []
            if bhx_pcba_id != identity["pcba_id"]:
                mismatches.append(f"PCBA_ID: BHX={bhx_pcba_id} vs ECU={identity['pcba_id']}")
            if bhx_assembly_id != identity["assembly_id"]:
                mismatches.append(f"ASSEMBLY_ID: BHX={bhx_assembly_id} vs ECU={identity['assembly_id']}")
            if bhx_usage_id != identity["usage_id"]:
                mismatches.append(f"USAGE_ID: BHX={bhx_usage_id} vs ECU={identity['usage_id']}")

            if mismatches:
                for m in mismatches:
                    print(f"  MISMATCH: {m}")
                if not force:
                    _abort("BHX identity mismatch. Use --force to override.")
                else:
                    print("  (--force: proceeding despite mismatch)")
            else:
                print(f"  {entry.src_path}: identity OK (component_id=0x{bhx_component_id:04X})")


def phase4_flash(sess, artifacts_dir: Path, selected: list) -> None:
    """Execute the 10-step UDS flash sequence for each firmware file."""
    from bhx_parser import parse_bhx

    for fw_index, entry in enumerate(selected):
        _print_section(f"Phase 4: Flash Sequence — {entry.dest_name}")

        src = artifacts_dir / entry.src_path
        report = parse_bhx(src)
        raw_blob = src.read_bytes()

        print("  Starting TesterPresent thread...")
        sess.start_tester_present()

        try:
            # Step 1: Enter programming session
            print("  Step 1: DiagnosticSessionControl(PROGRAMMING)")
            sess.diagnostic_session(0x02)

            # Step 2: Security access
            print("  Step 2: SecurityAccess")
            sess.security_access()

            # Step 3: Erase flash (routine 0xFF00, arg=01)
            print("  Step 3: RoutineControl ERASE_FLASH (0xFF00)")
            sess.routine_control(_ROUTINE_ERASE_FLASH, b"\x01")

            # Step 4: Check component/revision (routine 0x0202)
            print("  Step 4: RoutineControl CHECK_CORRECT_COMPONENT_AND_REV (0x0202)")
            sess.routine_control(_ROUTINE_CHECK_COMP_REV)

            # Steps 5-7: Per SHDR section
            for sec in report["sections"]:
                sec_idx = sec["index"]
                target = int(sec["target"], 16)
                size = sec["size"]
                payload_offset = int(sec["offset_in_file"], 16) + 20
                payload = raw_blob[payload_offset:payload_offset + size]

                print(f"  [Section {sec_idx}] target=0x{target:08X} size={size} bytes")

                # Step 5: RequestDownload
                print(f"  Step 5: RequestDownload addr=0x{target:08X} size={size}")
                max_block_len = sess.request_download(target, size)
                print(f"         maxBlockLen={max_block_len}")

                # Step 6: TransferData
                print(f"  Step 6: TransferData ({size} bytes in chunks of {max_block_len - 2})")
                sess.transfer_data(payload, max_block_len)

                # Step 7: RequestTransferExit
                print("  Step 7: RequestTransferExit")
                sess.request_transfer_exit()

            # Step 9: Verify CRC (routine 0x0201)
            print("  Step 9: RoutineControl VERIFY_CRC (0x0201)")
            sess.routine_control(_ROUTINE_VERIFY_CRC)

        finally:
            print("  Stopping TesterPresent thread...")
            sess.stop_tester_present()

        # Step 10: ECU Reset (only after last firmware file)
        if fw_index == len(selected) - 1:
            print("  Step 10: ECUReset")
            sess.ecu_reset(0x01)
            print("  Flash complete.")
        else:
            print(f"  (continuing to next firmware file...)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tesla Model 3 ECU firmware flashing tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--node", "-n", required=True, help="ECU node name (e.g. PCS)")
    parser.add_argument("--channel", "-c", default="vcan0", help="CAN interface (default: vcan0)")
    parser.add_argument("--interface", "-i", default="socketcan", help="python-can interface type")
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
    from uds.client import UdsSession

    cfg = load_node_config(args.node, _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)

    try:
        with UdsSession(cfg, args.channel, interface=args.interface) as sess:
            # Phase 1: Read identity
            sess.diagnostic_session(0x01)
            identity = phase1_identity(sess, args.node)

            # Phase 2: Select firmware
            selected = phase2_firmware_selection(artifacts_dir, identity, args.node)

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
