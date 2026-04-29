"""UdsSession: wraps py-uds Client with TesterPresent background thread."""

from __future__ import annotations

import threading
import time
from typing import Any

import can
from uds.addressing import AddressingType
from uds.can.addressing import NormalCanAddressingInformation
from uds.can.transport_interface import PyCanTransportInterface
from uds.message import UdsMessage

from .node_config import NodeConfig
from .security import compute_key

_SESSION_DEFAULT = 0x01
_SESSION_PROGRAMMING = 0x02
_SESSION_EXTENDED = 0x03

_SID_DSC  = 0x10  # DiagnosticSessionControl
_SID_SA   = 0x27  # SecurityAccess
_SID_RDBI = 0x22  # ReadDataByIdentifier
_SID_WDBI = 0x2E  # WriteDataByIdentifier
_SID_RC   = 0x31  # RoutineControl
_SID_RD   = 0x34  # RequestDownload
_SID_TD   = 0x36  # TransferData
_SID_RTE  = 0x37  # RequestTransferExit
_SID_ER   = 0x11  # ECUReset
_SID_TP   = 0x3E  # TesterPresent
_SID_CDI  = 0x14  # ClearDiagnosticInformation

_RC_START = 0x01

# DID 0x0102: moduleToProgram — selects CPU/flash region in bootloader
_DID_MODULE_TO_PROGRAM = 0x0102
# DID 0xF100: flash count — enforced per-ECU limit
_DID_FLASH_COUNT = 0xF100
# RC 0x0601: vendor pre-flash routine (vcleft / vcleftramapp)
_RC_VENDOR_PREFLIGHT = 0x0601


class UdsError(Exception):
    """Raised on negative UDS responses."""

    def __init__(self, sid: int, nrc: int):
        self.sid = sid
        self.nrc = nrc
        super().__init__(
            f"Negative response for SID 0x{sid:02X}: NRC 0x{nrc:02X}"
        )


class FlashCountError(Exception):
    """Raised when the ECU's flash count is at or over its per-ECU limit."""

    def __init__(self, count: int, limit: int):
        self.count = count
        self.limit = limit
        super().__init__(
            f"Flash count {count} at or over limit {limit}"
        )


class UdsSession:
    def __init__(
        self,
        node: NodeConfig,
        channel: str,
        interface: str = "socketcan",
    ):
        self._bus = can.Bus(interface=interface, channel=channel)
        addressing = NormalCanAddressingInformation(
            rx_physical_params={"can_id": node.response_can_id},
            tx_physical_params={"can_id": node.request_can_id},
            rx_functional_params={"can_id": 0x7E8},
            tx_functional_params={"can_id": 0x7DF},
        )
        self._transport: PyCanTransportInterface = PyCanTransportInterface(
            network_manager=self._bus,
            addressing_information=addressing,
        )
        self._node = node
        self._tp_stop = threading.Event()
        self._tp_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # TesterPresent keep-alive
    # ------------------------------------------------------------------

    def start_tester_present(self) -> None:
        """Send TesterPresent (3E 80, suppress positive response) at 2 Hz."""
        self._tp_stop.clear()
        self._tp_thread = threading.Thread(target=self._tp_loop, daemon=True)
        self._tp_thread.start()

    def stop_tester_present(self) -> None:
        self._tp_stop.set()
        if self._tp_thread:
            self._tp_thread.join()
            self._tp_thread = None

    def _tp_loop(self) -> None:
        while not self._tp_stop.wait(0.5):
            self._send_raw([_SID_TP, 0x80])

    # ------------------------------------------------------------------
    # UDS services
    # ------------------------------------------------------------------

    def diagnostic_session(self, mode: int = _SESSION_DEFAULT) -> None:
        resp = self._send_raw([_SID_DSC, mode])
        self._check_positive(resp, _SID_DSC)

    def security_access(self, level_idx: int = 0) -> None:
        """Full seed → key exchange.

        level_idx selects the seed/key sub-function pair and algorithm:
          0 → seed=0x01/key=0x02, tesla_hash (most ECUs)
          3 → seed=0x07/key=0x08, tesla_hash (ibst, esp, espcal, rcmcal, rcm)
          4 → seed=0x09/key=0x0A, baolong_hash (tpms)
          7 → seed=0x0F/key=0x10, pektron_hash (cmp)
         13 → seed=0x1B/key=0x1C, OTA session key (opc, ths, swc, lumbar, bleep)
        """
        algo = self._node.security_algorithm
        buf_size = self._node.security_buffer_size
        kw = self._node.security_kw

        seed_subfn = level_idx * 2 + 1
        key_subfn = seed_subfn + 1

        resp = self._send_raw([_SID_SA, seed_subfn])
        # NRC 0x35 (requestSequenceError) = already unlocked
        if resp and resp[0] == 0x7F and len(resp) >= 3 and resp[2] == 0x35:
            return
        self._check_positive(resp, _SID_SA)
        seed = bytes(resp[2:2 + buf_size])

        key = compute_key(algo, seed, kw)

        resp = self._send_raw([_SID_SA, key_subfn] + list(key))
        self._check_positive(resp, _SID_SA)

    def read_did(self, did_id: int) -> bytes:
        resp = self._send_raw(
            [_SID_RDBI, (did_id >> 8) & 0xFF, did_id & 0xFF]
        )
        self._check_positive(resp, _SID_RDBI)
        # Positive response: 62 <DID high> <DID low> <data...>
        return bytes(resp[3:])

    def write_did(self, did_id: int, data: bytes) -> None:
        payload = (
            [_SID_WDBI, (did_id >> 8) & 0xFF, did_id & 0xFF] + list(data)
        )
        resp = self._send_raw(payload)
        self._check_positive(resp, _SID_WDBI)

    def routine_control(self, routine_id: int, arg: bytes = b"") -> bytes:
        payload = [
            _SID_RC, _RC_START,
            (routine_id >> 8) & 0xFF, routine_id & 0xFF,
        ] + list(arg)
        resp = self._send_raw(payload)
        self._check_positive(resp, _SID_RC)
        # Positive response: 71 01 <routine high> <routine low> <result...>
        return bytes(resp[4:])

    def module_to_program(self, module_byte: int) -> None:
        """WDBI DID 0x0102 — select CPU/flash region before erase+transfer."""
        self.write_did(_DID_MODULE_TO_PROGRAM, bytes([module_byte]))

    def set_timeout(self, p2_seconds: float) -> None:
        """Update the transport timeouts (seconds → milliseconds).

        Sets both N_Bs (inter-frame) and N_Cr (consecutive frame) timeouts.
        Call before long operations (erase, large transfers) and restore after.
        """
        p2_ms = p2_seconds * 1000
        self._transport.n_bs_timeout = p2_ms
        self._transport.n_cr_timeout = p2_ms

    def check_flash_count(self, limit: int) -> None:
        """Read DID 0xF100 and raise FlashCountError if count >= limit."""
        data = self.read_did(_DID_FLASH_COUNT)
        count = int.from_bytes(data[:4], "big")
        if count >= limit:
            raise FlashCountError(count, limit)
        print(f"    Flash count: {count} / {limit}")

    def vendor_preflight_routine(self) -> None:
        """RC 0x0601 — vcleft/vcleftramapp pre-flash vendor routine.

        Starts the routine, then polls requestResults (subfunction 3) up to
        50 times (100 ms apart) until the response byte equals 1.
        """
        import time as _time
        _RC_REQ_RESULTS = 0x03
        payload_start = [
            _SID_RC, _RC_START,
            (_RC_VENDOR_PREFLIGHT >> 8) & 0xFF, _RC_VENDOR_PREFLIGHT & 0xFF,
        ]
        resp = self._send_raw(payload_start)
        self._check_positive(resp, _SID_RC)

        payload_poll = [
            _SID_RC, _RC_REQ_RESULTS,
            (_RC_VENDOR_PREFLIGHT >> 8) & 0xFF, _RC_VENDOR_PREFLIGHT & 0xFF,
        ]
        for _ in range(50):
            _time.sleep(0.1)
            resp = self._send_raw(payload_poll)
            self._check_positive(resp, _SID_RC)
            if len(resp) >= 5 and resp[4] == 0x01:
                return
        raise UdsError(_SID_RC, 0x00)

    def request_download(self, address: int, size: int) -> int:
        """Returns maxBlockLen (number of bytes including sequence counter)."""
        # Data format: 0x00 (no compression/encryption)
        # Address and length format: 0x44 (4-byte address, 4-byte size)
        addr_bytes = address.to_bytes(4, "big")
        size_bytes = size.to_bytes(4, "big")
        payload = [_SID_RD, 0x00, 0x44] + list(addr_bytes) + list(size_bytes)
        resp = self._send_raw(payload)
        self._check_positive(resp, _SID_RD)
        # Positive response: 74 <length_format> <maxBlockLen...>
        # length_format nibble high = number of bytes for maxBlockLen
        length_format = resp[1]
        max_block_len_size = (length_format >> 4) & 0xF
        max_block_len = int.from_bytes(resp[2:2 + max_block_len_size], "big")
        return min(max_block_len, 512)

    def transfer_data(self, payload: bytes, max_block_len: int) -> None:
        """Transfer payload in chunks.

        max_block_len includes the 1-byte sequence counter.
        """
        chunk_size = max_block_len - 2  # subtract SID + seq bytes
        seq = 0x01
        offset = 0
        while offset < len(payload):
            chunk = payload[offset:offset + chunk_size]
            resp = self._send_raw([_SID_TD, seq] + list(chunk))
            self._check_positive(resp, _SID_TD)
            offset += len(chunk)
            seq = 0x00 if seq == 0xFF else seq + 1  # wrap 0xFF → 0x00

    def request_transfer_exit(self) -> None:
        resp = self._send_raw([_SID_RTE])
        self._check_positive(resp, _SID_RTE)

    def ecu_reset(self, reset_type: int = 0x01) -> None:
        resp = self._send_raw([_SID_ER, reset_type])
        self._check_positive(resp, _SID_ER)

    def ecu_reset_no_wait(self, reset_type: int = 0x01) -> None:
        """Send ECUReset with no response wait (soft reset / fire-and-forget)."""
        self._send_raw([_SID_ER, reset_type])

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def clear_dtc(self, group: int = 0xFFFFFF) -> None:
        """ClearDiagnosticInformation (0x14) — group 0xFFFFFF clears all."""
        b = group.to_bytes(3, "big")
        resp = self._send_raw([_SID_CDI, b[0], b[1], b[2]])
        self._check_positive(resp, _SID_CDI)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_raw(self, payload: list[int]) -> list[int]:
        msg = UdsMessage(
            payload=bytearray(payload),
            addressing_type=AddressingType.PHYSICAL,
        )
        self._transport.send_message(msg)
        try:
            record = self._transport.receive_message(start_timeout=1000, end_timeout=1000)
            return list(record.payload)
        except Exception:
            return []

    @staticmethod
    def _check_positive(resp: list[int], expected_sid: int) -> None:
        if not resp:
            raise UdsError(expected_sid, 0x00)
        if resp[0] == 0x7F:
            nrc = resp[2] if len(resp) >= 3 else 0x00
            raise UdsError(expected_sid, nrc)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "UdsSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop_tester_present()
        try:
            self._bus.shutdown()
        except Exception:
            pass
