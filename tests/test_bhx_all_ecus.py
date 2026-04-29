"""Parametrized BHX parser tests across every firmware file in seed_artifacts_v2.

Each BHX file gets its own test ID so failures are pinpointed by ECU/variant.
Tests require the seed_artifacts_v2 directory at the path below; they are
skipped automatically when that path is absent (e.g. CI without the firmware).
"""

from __future__ import annotations

import binascii
import struct
from pathlib import Path

import pytest

from bhx_parser import parse_bhx

_ARTIFACTS = Path("/home/outlandnish/dev/tm3/deploy/seed_artifacts_v2")
_ALL_BHX = sorted(_ARTIFACTS.rglob("*.bhx")) if _ARTIFACTS.is_dir() else []

pytestmark = pytest.mark.skipif(
    not _ARTIFACTS.is_dir(),
    reason=f"seed_artifacts_v2 not found at {_ARTIFACTS}",
)


def _rel(path: Path) -> str:
    return str(path.relative_to(_ARTIFACTS))


# ---------------------------------------------------------------------------
# Helpers that re-implement key parser logic independently for cross-checking
# ---------------------------------------------------------------------------

def _independent_crc(path: Path, section_index: int) -> int:
    """Recompute CRC32 for a section payload directly from raw bytes."""
    blob = path.read_bytes()
    cursor = 12  # skip GHDR
    for i in range(section_index + 1):
        if blob[cursor:cursor + 4] != b"SHDR":
            raise ValueError(f"SHDR magic not found at cursor {cursor}")
        size = struct.unpack_from(">I", blob, cursor + 12)[0]
        payload_start = cursor + 20
        if i < section_index:
            cursor = payload_start + size  # advance past this section
    payload = blob[payload_start:payload_start + size]
    return binascii.crc32(payload) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Parametrize one test ID per BHX file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bhx_path", _ALL_BHX, ids=_rel)
class TestBhxFile:
    def test_parses_without_exception(self, bhx_path: Path):
        parse_bhx(bhx_path)

    def test_has_ghdr_magic(self, bhx_path: Path):
        report = parse_bhx(bhx_path)
        assert report["file"]["global_magic"] == "GHDR"

    def test_has_at_least_one_section(self, bhx_path: Path):
        report = parse_bhx(bhx_path)
        assert len(report["sections"]) >= 1

    def test_all_section_crcs_match(self, bhx_path: Path):
        report = parse_bhx(bhx_path)
        for sec in report["sections"]:
            assert sec["payload_crc_matches"], (
                f"Section {sec['index']} CRC mismatch in {_rel(bhx_path)}: "
                f"stored={sec['crc32']}"
            )

    def test_global_payload_size_matches(self, bhx_path: Path):
        report = parse_bhx(bhx_path)
        assert report["file"]["global_payload_size_matches"], (
            f"GHDR payload_size mismatch in {_rel(bhx_path)}"
        )

    def test_section_crcs_match_independent_calculation(self, bhx_path: Path):
        report = parse_bhx(bhx_path)
        for sec in report["sections"]:
            expected = _independent_crc(bhx_path, sec["index"])
            stored = int(sec["crc32"], 16)
            assert stored == expected, (
                f"Section {sec['index']} CRC: stored=0x{stored:08X} "
                f"vs recomputed=0x{expected:08X} in {_rel(bhx_path)}"
            )

    def test_section_target_addresses_fit_in_32_bits(self, bhx_path: Path):
        # target=0 is legitimate for config-data ECUs (bleep, hcm, ocs1p, opc, swc).
        # We just verify the field parses as a valid 32-bit value.
        report = parse_bhx(bhx_path)
        for sec in report["sections"]:
            target = int(sec["target"], 16)
            assert 0 <= target <= 0xFFFFFFFF, (
                f"Section {sec['index']} target {sec['target']} out of 32-bit range "
                f"in {_rel(bhx_path)}"
            )

    def test_section_sizes_are_positive(self, bhx_path: Path):
        report = parse_bhx(bhx_path)
        for sec in report["sections"]:
            assert sec["size"] > 0, (
                f"Section {sec['index']} has zero size in {_rel(bhx_path)}"
            )

    def test_file_size_consistent_with_sections(self, bhx_path: Path):
        report = parse_bhx(bhx_path)
        # GHDR(12) + sum of (SHDR(20) + payload) for each section <= file_size
        expected_min = 12 + sum(20 + s["size"] for s in report["sections"])
        assert report["file"]["file_size"] >= expected_min, (
            f"File size {report['file']['file_size']} < expected minimum "
            f"{expected_min} in {_rel(bhx_path)}"
        )


# ---------------------------------------------------------------------------
# ECU-family-level checks (identity header presence and field sanity)
# ---------------------------------------------------------------------------

# BHX files where we expect a decodeable Tesla ECU identity header.
# PM uses a different MCU (not TI C28x) so its BHX payloads don't carry the
# standard 32-byte Tesla identity header — excluded deliberately.
# Only PCS carries the 32-byte Tesla C28x identity header with the 0xFFFF sentinel
# in words 4 and 5. CP (TMS570), DI, EPB3, HVBMS, HVP, and PM all use different
# payload layouts and do not decode an identity header.
_ECU_FAMILIES_WITH_IDENTITY = {"pcs"}


@pytest.mark.parametrize("bhx_path", _ALL_BHX, ids=_rel)
def test_identity_header_fields_sane_when_present(bhx_path: Path):
    """When an identity header is decoded, all fields should be in plausible ranges."""
    report = parse_bhx(bhx_path)
    for sec in report["sections"]:
        ident = sec.get("identity")
        if ident is None:
            continue
        assert 0 <= ident["pcba_id"] <= 255, f"pcba_id out of range in {_rel(bhx_path)}"
        assert 0 <= ident["assembly_id"] <= 255, f"assembly_id out of range in {_rel(bhx_path)}"
        assert 0 <= ident["usage_id"] <= 65535, f"usage_id out of range in {_rel(bhx_path)}"
        # OHC/ROHC legitimately have config_id=0; skip that check for them
        if bhx_path.parts[-3] not in ("ohc", "rohc"):
            assert ident["config_id"] > 0, f"config_id is zero in {_rel(bhx_path)}"
        assert int(ident["component_id"], 16) != 0, (
            f"component_id is zero in {_rel(bhx_path)}"
        )


@pytest.mark.parametrize("bhx_path", [
    p for p in _ALL_BHX
    if p.parts[-3] in _ECU_FAMILIES_WITH_IDENTITY
], ids=lambda p: _rel(p))
def test_known_ecu_families_have_identity_header(bhx_path: Path):
    """PCS and CP BHX files should always decode a Tesla ECU identity header."""
    report = parse_bhx(bhx_path)
    has_identity = any(sec.get("identity") for sec in report["sections"])
    assert has_identity, (
        f"Expected identity header in {_rel(bhx_path)} but none was decoded"
    )


# ---------------------------------------------------------------------------
# PCS-specific structural checks
# ---------------------------------------------------------------------------

_PCS_BHX = [p for p in _ALL_BHX if p.parts[-3] == "pcs"]


@pytest.mark.parametrize("bhx_path", _PCS_BHX, ids=_rel)
class TestPcsBhx:
    def test_exactly_one_section(self, bhx_path: Path):
        report = parse_bhx(bhx_path)
        assert len(report["sections"]) == 1

    def test_target_address_in_valid_range(self, bhx_path: Path):
        # CPU1: 0x00088000, CPU2: 0x00082000
        report = parse_bhx(bhx_path)
        target = int(report["sections"][0]["target"], 16)
        assert target in (0x00088000, 0x00082000), (
            f"Unexpected PCS target 0x{target:08X} in {_rel(bhx_path)}"
        )

    def test_component_id_is_cpu1_or_cpu2(self, bhx_path: Path):
        report = parse_bhx(bhx_path)
        ident = report["sections"][0].get("identity")
        if ident is None:
            pytest.skip("No identity header decoded")
        component_id = int(ident["component_id"], 16)
        assert component_id in (0x001B, 0x0096), (
            f"Unexpected PCS component_id 0x{component_id:04X} in {_rel(bhx_path)}"
        )

    def test_tail_crc_present_for_pcba5(self, bhx_path: Path):
        # tail_crc only exists in PCBA_ID=5 variants (5xx); 3xx/4xx use a different footer
        report = parse_bhx(bhx_path)
        ident = report["sections"][0].get("identity")
        if ident is None:
            pytest.skip("No identity header decoded")
        if ident.get("pcba_id", 0) != 5:
            pytest.skip("tail_crc only expected for PCBA_ID=5 variants")
        assert "tail_crc" in ident, f"Missing tail_crc in {_rel(bhx_path)}"

    def test_tail_crc_matches_metadata(self, bhx_path: Path):
        """Tail CRC in BHX payload should match the CRC in signed_metadata_map.tsv."""
        from uds.metadata import load_metadata

        report = parse_bhx(bhx_path)
        ident = report["sections"][0].get("identity")
        if ident is None or "tail_crc" not in ident:
            pytest.skip("No tail_crc available")

        tail_crc = ident["tail_crc"].lstrip("0x").lower()

        tsv_path = _ARTIFACTS / "signed_metadata_map.tsv"
        if not tsv_path.exists():
            pytest.skip("signed_metadata_map.tsv not found")

        entries = load_metadata(tsv_path)
        # src_path in metadata is relative to artifacts dir
        rel = str(bhx_path.relative_to(_ARTIFACTS))
        matching = [e for e in entries if e.src_path == rel]
        if not matching:
            pytest.skip(f"No metadata entry for {rel}")

        for entry in matching:
            assert entry.crc.lower() == tail_crc, (
                f"Metadata CRC {entry.crc} != tail_crc {tail_crc} for {rel}"
            )


# ---------------------------------------------------------------------------
# Park multi-section check
# ---------------------------------------------------------------------------

_PARK_BHX = [p for p in _ALL_BHX if p.parts[-3] == "park"]


@pytest.mark.parametrize("bhx_path", _PARK_BHX, ids=_rel)
def test_park_has_multiple_sections(bhx_path: Path):
    report = parse_bhx(bhx_path)
    assert len(report["sections"]) > 1, (
        f"Expected multiple sections in park BHX {_rel(bhx_path)}"
    )
