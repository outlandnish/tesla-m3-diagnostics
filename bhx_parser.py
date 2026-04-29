#!/usr/bin/env python3

from __future__ import annotations

import argparse
import binascii
import json
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BhxSection:
    index: int
    offset_in_file: int
    version: int
    target: int
    size: int
    crc32: int
    payload_crc_matches: bool


@dataclass
class BhxFile:
    global_magic: str
    global_version: int
    global_payload_size: int
    file_size: int
    global_payload_size_matches: bool
    sections: list[BhxSection] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def read_u32be(blob: bytes, offset: int) -> int:
    return struct.unpack_from(">I", blob, offset)[0]


def swap_u16_halves(value: int) -> int:
    return ((value & 0xFFFF) << 16) | ((value >> 16) & 0xFFFF)


def parse_bhx(path: Path) -> dict[str, Any]:
    blob = path.read_bytes()

    if len(blob) < 12:
        raise ValueError("File too small to contain a BHX GHDR")
    if blob[0:4] != b"GHDR":
        raise ValueError(f"Expected GHDR magic, got {blob[0:4]!r}")

    global_version = read_u32be(blob, 4)
    global_payload_size = read_u32be(blob, 8)

    bhx = BhxFile(
        global_magic="GHDR",
        global_version=global_version,
        global_payload_size=global_payload_size,
        file_size=len(blob),
        global_payload_size_matches=False,  # updated after sections are parsed
    )

    # Walk sections — each SHDR immediately follows the previous section's payload
    cursor = 12
    section_index = 0
    while cursor + 20 <= len(blob):
        if blob[cursor:cursor + 4] != b"SHDR":
            break

        version = read_u32be(blob, cursor + 4)
        target = read_u32be(blob, cursor + 8)
        size = read_u32be(blob, cursor + 12)
        crc32 = read_u32be(blob, cursor + 16)

        payload_start = cursor + 20
        payload_end = payload_start + size
        section_payload = blob[payload_start:payload_end]
        computed_crc = binascii.crc32(section_payload) & 0xFFFFFFFF

        bhx.sections.append(BhxSection(
            index=section_index,
            offset_in_file=cursor,
            version=version,
            target=target,
            size=size,
            crc32=crc32,
            payload_crc_matches=(crc32 == computed_crc),
        ))

        cursor = payload_end
        section_index += 1

    # GHDR payload_size = sum of section payload bytes only (SHDR headers not counted)
    bhx.global_payload_size_matches = (
        global_payload_size == sum(s.size for s in bhx.sections)
    )

    return {
        "path": str(path),
        "file": asdict(bhx),
        "sections": [
            _section_report(blob, s, path)
            for s in bhx.sections
        ],
    }


# ---------------------------------------------------------------------------
# Per-section analysis
# ---------------------------------------------------------------------------

def _section_report(
    blob: bytes, section: BhxSection, path: Path
) -> dict[str, Any]:
    payload_start = section.offset_in_file + 20
    payload = blob[payload_start:payload_start + section.size]

    report: dict[str, Any] = {
        "index": section.index,
        "offset_in_file": f"0x{section.offset_in_file:08x}",
        "version": section.version,
        "target": f"0x{section.target:08x}",
        "size": section.size,
        "crc32": f"0x{section.crc32:08x}",
        "payload_crc_matches": section.payload_crc_matches,
    }

    # Decode Tesla ECU identity header if present (first 32 bytes of payload)
    identity = _decode_identity_header(payload, path)
    if identity:
        report["identity"] = identity

    return report


def _decode_identity_header(payload: bytes, path: Path) -> dict[str, Any] | None:
    """Decode the Tesla ECU identity header embedded at the start of the payload.

    Layout (confirmed for PCS; likely consistent across Tesla C28x-based ECUs):
      Word 0: [COMPONENT_ID:16][ASSEMBLY_ID:8][PCBA_ID:8]
      Word 1: [USAGE_ID:16][reserved:16]
      Word 2: reserved
      Word 3: reserved
      Word 4: [CONFIG_ID:16][0xFFFF:16]
      Word 5: [FW_VERSION:16][0xFFFF:16]
      Word 6: flash_range_start (16-bit half-swapped C28x address)
      Word 7: flash_range_end   (16-bit half-swapped C28x address)
    """
    if len(payload) < 32:
        return None

    w0 = read_u32be(payload, 0)
    w1 = read_u32be(payload, 4)
    w4 = read_u32be(payload, 16)
    w5 = read_u32be(payload, 20)
    w6 = read_u32be(payload, 24)
    w7 = read_u32be(payload, 28)

    # Sanity check: words 4 and 5 should have 0xFFFF in the low half if this
    # is a Tesla identity header. Skip decode if they don't look right.
    if (w4 & 0xFFFF) != 0xFFFF or (w5 & 0xFFFF) != 0xFFFF:
        return None

    result: dict[str, Any] = {
        "component_id": f"0x{(w0 >> 16) & 0xFFFF:04x}",
        "assembly_id": (w0 >> 8) & 0xFF,
        "pcba_id": w0 & 0xFF,
        "usage_id": (w1 >> 16) & 0xFFFF,
        "config_id": (w4 >> 16) & 0xFFFF,
        "fw_version": (w5 >> 16) & 0xFFFF,
        "flash_range_start": f"0x{swap_u16_halves(w6):08x}",
        "flash_range_end": f"0x{swap_u16_halves(w7):08x}",
    }

    # Trailing CRC: last word is a half-swapped CRC32, penultimate is 0xFFFFFFFF
    if len(payload) >= 8:
        tail_crc_raw = read_u32be(payload, len(payload) - 4)
        tail_sentinel = read_u32be(payload, len(payload) - 8)
        if tail_sentinel == 0xFFFFFFFF:
            result["tail_crc"] = f"0x{swap_u16_halves(tail_crc_raw):08x}"
            result["tail_crc_raw"] = f"0x{tail_crc_raw:08x}"

    # Filename hints (P/A/U/CPU parsed from filename)
    hints: dict[str, Any] = {}
    for part in path.stem.split("_"):
        if len(part) >= 2 and part[0] in {"P", "A", "U"} and part[1:].isdigit():
            hints[part[0]] = int(part[1:])
        elif part.startswith("CPU") and part[3:].isdigit():
            hints["CPU"] = int(part[3:])
    if hints:
        result["filename_hints"] = hints

    return result


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_sections(path: Path, output_dir: Path) -> list[Path]:
    blob = path.read_bytes()
    report = parse_bhx(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for sec in report["sections"]:
        idx = sec["index"]
        target = sec["target"]
        payload_offset = int(sec["offset_in_file"], 16) + 20
        payload = blob[payload_offset:payload_offset + sec["size"]]
        out = output_dir / f"{path.stem}.section{idx}.target{target}.bin"
        out.write_bytes(payload)
        written.append(out)
    return written


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def print_text_report(report: dict[str, Any]) -> None:
    f = report["file"]
    print(report["path"])
    print(f"  file_size:            {f['file_size']} (0x{f['file_size']:x})")
    print(f"  ghdr_version:         {f['global_version']}")
    print(f"  global_payload_size:  {f['global_payload_size']} "
          f"(0x{f['global_payload_size']:x})  "
          f"matches={'yes' if f['global_payload_size_matches'] else 'NO'}")
    print(f"  sections:             {len(report['sections'])}")

    for sec in report["sections"]:
        print(f"\n  [Section {sec['index']}]")
        print(f"    file_offset:       {sec['offset_in_file']}")
        print(f"    shdr_version:      {sec['version']}")
        print(f"    target:            {sec['target']}")
        print(f"    size:              {sec['size']} (0x{sec['size']:x})")
        print(f"    crc32:             {sec['crc32']}  "
              f"matches={'yes' if sec['payload_crc_matches'] else 'NO'}")

        ident = sec.get("identity")
        if ident:
            print(f"    identity:")
            print(f"      component_id:      {ident['component_id']}")
            print(f"      pcba_id:           {ident['pcba_id']}")
            print(f"      assembly_id:       {ident['assembly_id']}")
            print(f"      usage_id:          {ident['usage_id']}")
            print(f"      config_id:         {ident['config_id']} "
                  f"(0x{ident['config_id']:04x})")
            print(f"      fw_version:        {ident['fw_version']}")
            print(f"      flash_range_start: {ident['flash_range_start']}")
            print(f"      flash_range_end:   {ident['flash_range_end']}")
            if "tail_crc" in ident:
                print(f"      tail_crc:          {ident['tail_crc']}  "
                      f"(raw {ident['tail_crc_raw']})")
            if "filename_hints" in ident:
                print(f"      filename_hints:    {ident['filename_hints']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect Tesla BHX firmware containers"
    )
    parser.add_argument("paths", nargs="+", help="BHX files to parse")
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text"
    )
    parser.add_argument(
        "--extract-dir",
        help="Write extracted section payloads into this directory",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    reports = [parse_bhx(Path(p)) for p in args.paths]

    if args.extract_dir:
        extract_dir = Path(args.extract_dir)
        for p in args.paths:
            written = extract_sections(Path(p), extract_dir)
            for w in written:
                print(f"Extracted: {w}")

    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
    else:
        for i, report in enumerate(reports):
            if i:
                print()
            print_text_report(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
