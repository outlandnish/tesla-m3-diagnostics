#!/usr/bin/env python3
"""Firmware flashing tool for Tesla Model 3 ECUs.

Usage:
  python dfu.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2
  python dfu.py --node PCS --channel vcan0 --artifacts ~/seed_artifacts_v2 --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import config as _cfg
from flash_scripts import (
    find_bootloader_entries,
    find_dual_cpu_pair,
    get_script,
    parent_node_for_bootloader,
    run_pcs_dual_cpu,
)
from uds_local.client import UdsSession

_NODES_JSON  = _cfg.NODES_JSON
_ETH_COMPACT = _cfg.ETH_COMPACT
_ODJ_DIR     = _cfg.ODJ_DIR

_DID_BOOTLOADER_VERSION = 0xF180


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
            f"DID 0xF180 response too short "
            f"({len(f180)} bytes, expected >=7)"
        )

    # Layout: [MODULES:1][COMPONENT_ID:2][PCBA_ID:1][ASSEMBLY_ID:1][USAGE_ID:2]
    component_id = (f180[1] << 8) | f180[2]
    pcba_id = f180[3]
    assembly_id = f180[4]
    usage_id = (f180[5] << 8) | f180[6]

    # version_map packed key: PPAA00UU
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
    """Load metadata and return selected FirmwareEntry list."""
    from uds_local.metadata import load_metadata, find_firmware

    _print_section("Phase 2: Firmware Selection")

    tsv_path = artifacts_dir / "signed_metadata_map.tsv"
    if not tsv_path.exists():
        _abort(f"signed_metadata_map.tsv not found in {artifacts_dir}")

    entries = load_metadata(tsv_path)
    matches = find_firmware(entries, node_name, identity["packed_key"])

    if not matches:
        _abort(
            f"No firmware found for {identity['lookup_key']} in {tsv_path}"
        )

    if len(matches) == 1:
        selected = matches
    else:
        print(f"  Found {len(matches)} firmware options:")
        for i, e in enumerate(matches):
            cond_str = (
                ",".join(f"{k}={v}" for k, v in e.conditions.items()) or "*"
            )
            print(
                f"  [{i}] {e.src_path} → {e.dest_name}"
                f"  crc={e.crc}  cond={cond_str}"
            )
        dest_names = {e.dest_name for e in matches}
        if len(dest_names) == len(matches):
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

    selected = _prompt_bootloader_choice(selected, node_name)
    selected = _prompt_dual_cpu_choice(selected)

    for e in selected:
        src = artifacts_dir / e.src_path
        if not src.exists():
            _abort(f"Firmware file not found: {src}")
        print(f"  Selected: {e.src_path} → {e.dest_name}  crc={e.crc}")

    return selected


def _prompt_bootloader_choice(selected: list, node_name: str) -> list:
    """If `selected` includes bootloader updater/image entries, ask whether to flash them.

    Bootloader flashing has real bricking risk — if interrupted, the ECU may not
    be recoverable without external programmer hardware. Default is **skip**.
    Apps that are part of a complete BL update package stay in the list either
    way; only the `*bu` and `*bl` entries are filtered out on a "no" answer.
    """
    bus, bls, apps = find_bootloader_entries(selected)
    if not bus and not bls:
        return selected

    print("\n  Bootloader updates detected for this firmware package:")
    for e in bus:
        print(f"    [{e.component}] {e.dest_name} (bootloader updater — installed into app slot first)")
    for e in bls:
        print(f"    [{e.component}] {e.dest_name} (bootloader image — written by the updater agent)")

    print()
    print("  WARNING: Bootloader flashing can brick the ECU if interrupted.")
    print("  After bu+bl complete, the regular app slot still holds the")
    print("  update agent — the regular application MUST be reflashed to")
    print("  restore normal operation.")
    if apps:
        app_summary = ", ".join(e.dest_name for e in apps)
        print(f"  (Selected app entries that will reflash after BL: {app_summary})")
    else:
        print("  (No regular app entries selected — the ECU will be left running")
        print("   the bootloader update agent. Add the app file to your selection.)")

    while True:
        raw = input("  Include bootloader updates? [y/N]: ").strip().lower() or "n"
        if raw in ("n", "no"):
            print("  → Skipping bootloader files.")
            return apps
        if raw in ("y", "yes"):
            # Sanity: warn if --node arg doesn't match the bootloader's parent
            for e in bus + bls:
                parent = parent_node_for_bootloader(e.component)
                if parent and parent.lower() != node_name.lower():
                    print(
                        f"  Warning: --node {node_name} but bootloader entry"
                        f" {e.component} expects parent {parent!r}"
                    )
            print("  → Including bootloader files (bu → bl → app).")
            # Order: bu first, bl second, then everything else.
            return bus + bls + apps
        print(f"  Invalid choice: {raw!r}")


def _prompt_dual_cpu_choice(selected: list) -> list:
    """If `selected` contains a PCS-family dual-CPU pair, ask which CPU(s) to flash.

    Returns the (possibly narrowed) entry list, preserving any non-pair entries
    (e.g. bootloader updates). Default (empty input) is "both", which lets phase
    4 dispatch to prog 1 (single auth session). Picking one CPU drops to prog 0.
    """
    pair = find_dual_cpu_pair(selected)
    if pair is None:
        return selected
    primary, secondary = pair
    print("\n  Dual-CPU PCS-family pairing detected:")
    print(f"    [1] primary only   ({primary.dest_name}, ecu_type={primary.component})")
    print(f"    [2] secondary only ({secondary.dest_name}, ecu_type={secondary.component})")
    print(f"    [3] both           — prog 1, single authenticated session [default]")
    while True:
        raw = input("  Flash which? [1/2/3]: ").strip() or "3"
        if raw == "1":
            keep_primary, keep_secondary = True, False
        elif raw == "2":
            keep_primary, keep_secondary = False, True
        elif raw == "3":
            keep_primary, keep_secondary = True, True
        else:
            print(f"  Invalid choice: {raw!r}")
            continue
        # Preserve original order; drop the unchosen pair member but keep
        # everything else (e.g. bootloader entries flashed before the apps).
        result = []
        for e in selected:
            if e is primary:
                if keep_primary:
                    result.append(e)
            elif e is secondary:
                if keep_secondary:
                    result.append(e)
            else:
                result.append(e)
        return result


def _decode_pcs_identity(data: bytes) -> dict | None:
    """Decode 32-byte Tesla C28x ECU identity header from segment data."""
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
        "usage_id": (w1 >> 16) & 0xFFFF,
    }


def phase3_preflight(
    artifacts_dir: Path,
    selected: list,
    identity: dict,
    force: bool,
) -> None:
    """Cross-check BHX identity header against DID reads."""
    import bhx

    _print_section("Phase 3: Pre-flight Verification")

    for entry in selected:
        src = artifacts_dir / entry.src_path
        if src.suffix.lower() != ".bhx":
            print(f"  {entry.src_path}: not a BHX file, skipping pre-flight check")
            continue
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
                    f"PCBA_ID: BHX={ident['pcba_id']}"
                    f" vs ECU={identity['pcba_id']}"
                )
            if ident["assembly_id"] != identity["assembly_id"]:
                mismatches.append(
                    f"ASSEMBLY_ID: BHX={ident['assembly_id']}"
                    f" vs ECU={identity['assembly_id']}"
                )
            if ident["usage_id"] != identity["usage_id"]:
                mismatches.append(
                    f"USAGE_ID: BHX={ident['usage_id']}"
                    f" vs ECU={identity['usage_id']}"
                )

            if mismatches:
                for m in mismatches:
                    print(f"  MISMATCH: {m}")
                if not force:
                    _abort(
                        "BHX identity mismatch. Use --force to override."
                    )
                else:
                    print("  (--force: proceeding despite mismatch)")
            else:
                print(
                    f"  {entry.src_path}: identity OK"
                    f" (component_id=0x{bhx_component_id:04X})"
                )
        if not found_identity:
            print(
                f"  {entry.src_path}: no identity header found,"
                " skipping pre-flight check"
            )


def _parse_firmware(src: Path):
    """Parse a firmware file into a bhx-compatible object based on extension."""
    import bhx
    import ihex
    ext = src.suffix.lower()
    if ext == ".bhx":
        return bhx.parse_file(src)
    if ext == ".hex":
        return ihex.parse_file(src)
    _abort(f"Unsupported firmware file type: {src.suffix!r} ({src.name})")


def phase4_dry_run(artifacts_dir: Path, selected: list) -> None:
    """Print what would be flashed without sending any UDS frames."""
    _print_section("Phase 4: Dry Run — Flash Plan")

    pair = find_dual_cpu_pair(selected)
    if pair is not None:
        primary_entry, secondary_entry = pair
        primary_bhx = _parse_firmware(artifacts_dir / primary_entry.src_path)
        secondary_bhx = _parse_firmware(artifacts_dir / secondary_entry.src_path)
        print("\n  PCS-family dual-CPU pairing detected — using prog 1 (single auth session)")
        print(f"    primary   ({primary_entry.dest_name}, ecu_type={primary_entry.component})"
              f"  → moduleToProgram(0x00)")
        for i, seg in enumerate(primary_bhx.segments):
            print(f"      [{i}] addr=0x{seg.start_address:08X}  size={seg.length} bytes")
        print(f"    secondary ({secondary_entry.dest_name}, ecu_type={secondary_entry.component})"
              f"  → moduleToProgram(0x04) [flashed first]")
        for i, seg in enumerate(secondary_bhx.segments):
            print(f"      [{i}] addr=0x{seg.start_address:08X}  size={seg.length} bytes")
        print("\n  (dry run complete — no frames sent)")
        return

    for entry in selected:
        ecu_type = entry.component.lower()
        try:
            script, module_byte = get_script(ecu_type)
        except KeyError:
            print(f"\n  {entry.dest_name}: no flash script for '{ecu_type}', skipping")
            continue

        src = artifacts_dir / entry.src_path
        bhx_file = _parse_firmware(src)

        step_names = [s.__name__ for s in script.steps]
        print(f"\n  {entry.dest_name}  ({entry.src_path})")
        print(f"    ecu_type:       {ecu_type}")
        print(f"    module_byte:    0x{module_byte:02X}")
        print(f"    security_level: {script.security_level}")
        print(f"    erase_timeout:  {script.erase_timeout}s")
        print(f"    steps:          {' → '.join(step_names)}")
        print(f"    segments:")
        for i, seg in enumerate(bhx_file.segments):
            print(
                f"      [{i}] addr=0x{seg.start_address:08X}"
                f"  size={seg.length} bytes"
            )

    print("\n  (dry run complete — no frames sent)")


def phase4_flash(
    sess: UdsSession,
    artifacts_dir: Path,
    selected: list,
    channel: str | None = None,
    interface: str | None = None,
) -> None:
    """Execute the flash sequence for the selected firmware entries.

    If `selected` contains a PCS-family dual-CPU pairing (primary +
    secondary), runs script 0x00651070 prog 1 once with both files in a
    single authenticated session. Otherwise loops per-entry through prog 0.

    `channel`/`interface` are forwarded to the FlashContext so steps that need
    to open a transient session to a different ECU (e.g. SCRIPT_BL_UPDATER_VCFRONT
    talking to VCRIGHT) can share the user's CAN interface.
    """
    pair = find_dual_cpu_pair(selected)
    if pair is not None:
        primary_entry, secondary_entry = pair
        _print_section(
            f"Phase 4: Flash (dual-CPU) — "
            f"{primary_entry.dest_name} + {secondary_entry.dest_name}"
        )
        primary_bhx = _parse_firmware(artifacts_dir / primary_entry.src_path)
        secondary_bhx = _parse_firmware(artifacts_dir / secondary_entry.src_path)
        run_pcs_dual_cpu(
            sess,
            primary_bhx, primary_entry,
            secondary_bhx, secondary_entry,
        )
        print("  Flash complete.")
        return

    for fw_index, entry in enumerate(selected):
        _print_section(f"Phase 4: Flash — {entry.dest_name}")

        ecu_type = entry.component.lower()
        try:
            script, module_byte = get_script(ecu_type)
        except KeyError:
            print(f"  Skipping {entry.dest_name}: no flash script for '{ecu_type}'")
            continue

        src = artifacts_dir / entry.src_path
        bhx_file = _parse_firmware(src)

        print(f"  Script: {script}  module=0x{module_byte:02X}")
        script.module_byte = module_byte
        script.run(sess, bhx_file, entry, channel=channel, interface=interface)

        if fw_index < len(selected) - 1:
            print("  Continuing to next firmware file...")
        else:
            print("  Flash complete.")


def run_flash(
    sess: UdsSession,
    artifacts_dir: Path,
    node_name: str,
    force: bool = False,
    channel: str | None = None,
    interface: str | None = None,
) -> None:
    """Run all four flash phases against an already-open UdsSession."""
    identity = phase1_identity(sess, node_name)
    selected = phase2_firmware_selection(artifacts_dir, identity, node_name)
    phase3_preflight(artifacts_dir, selected, identity, force)
    confirm = input("\nProceed with flashing? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted by user.")
        return
    phase4_flash(sess, artifacts_dir, selected, channel=channel, interface=interface)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tesla Model 3 ECU firmware flashing tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--node", "-n", required=True,
                        help="ECU node name (e.g. PCS)")
    parser.add_argument("--channel", "-c",
                        help="CAN interface channel")
    parser.add_argument("--interface", "-i",
                        help="python-can interface type")
    parser.add_argument(
        "--artifacts", "-a",
        help="Path to seed_artifacts_v2 directory",
    )
    _cfg.apply_defaults(parser)
    parser.add_argument("--force", action="store_true",
                        help="Skip pre-flight identity mismatch abort")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print flash plan without sending any UDS frames")
    parser.add_argument(
        "--packed-key", type=lambda s: int(s, 0),
        help=(
            "Offline identity override (decimal or 0x hex). "
            "Skips live ECU reads — implies --dry-run. "
            "Use the packed_key printed by a previous run "
            "(PPAA00UU from DID 0xF180)."
        ),
    )
    args = parser.parse_args()

    if args.packed_key is not None and not args.dry_run:
        parser.error("--packed-key requires --dry-run")

    if not args.artifacts:
        parser.error("--artifacts is required (or set TM3_ARTIFACTS_DIR in .env)")

    artifacts_dir = Path(args.artifacts).expanduser().resolve()
    if not artifacts_dir.is_dir():
        print(f"Error: artifacts directory not found: {artifacts_dir}")
        return 1

    from uds_local.node_config import load_node_config

    try:
        if args.packed_key is not None:
            # Offline path: no CAN connection needed
            identity = {
                "f180_raw": "(offline)",
                "component_id": 0,
                "pcba_id": (args.packed_key >> 24) & 0xFF,
                "assembly_id": (args.packed_key >> 16) & 0xFF,
                "usage_id": args.packed_key & 0xFF,
                "packed_key": args.packed_key,
                "lookup_key": f"{args.node.lower()}:{args.packed_key}",
            }
            _print_section("Phase 1: Identity (offline)")
            print(f"  Node:        {args.node}")
            print(f"  packed_key:  {args.packed_key}")
            print(f"  lookup_key:  {identity['lookup_key']}")

            selected = phase2_firmware_selection(
                artifacts_dir, identity, args.node
            )
            phase3_preflight(
                artifacts_dir, selected, identity, args.force
            )
            phase4_dry_run(artifacts_dir, selected)
            return 0

        cfg = load_node_config(args.node, _NODES_JSON, _ETH_COMPACT, _ODJ_DIR)

        with UdsSession(cfg, args.channel, interface=args.interface) as sess:
            sess.diagnostic_session(0x01)

            if args.dry_run:
                identity = phase1_identity(sess, args.node)
                selected = phase2_firmware_selection(artifacts_dir, identity, args.node)
                phase3_preflight(artifacts_dir, selected, identity, args.force)
                phase4_dry_run(artifacts_dir, selected)
                return 0

            run_flash(
                sess, artifacts_dir, args.node, force=args.force,
                channel=args.channel, interface=args.interface,
            )

    except KeyboardInterrupt:
        print("\nAborted.")
        return 1
    except Exception as e:
        print(f"\nError: {e}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
