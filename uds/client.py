"""UdsSession: wraps py-uds Client with TesterPresent background thread."""

from __future__ import annotations

import threading
from typing import Any

import can
from uds import (
    AddressingType,
    NormalCanAddressingInformation,
    PyCanTransportInterface,
    UdsMessage,
)
from uds.transport_interface import AbstractUdsTransportInterface

from .node_config import NodeConfig
from .security import compute_key

_SESSION_DEFAULT = 0x01
_SESSION_PROGRAMMING = 0x02
_SESSION_EXTENDED = 0x03

_SID_DSC = 0x10   # DiagnosticSessionControl
_SID_SA  = 0x27   # SecurityAccess
_SID_RDBI = 0x22  # ReadDataByIdentifier
_SID_WDBI = 0x2E  # WriteDataByIdentifier
_SID_RC  = 0x31   # RoutineControl
_SID_RD  = 0x34   # RequestDownload
_SID_TD  = 0x36   # TransferData
_SID_RTE = 0x37   # RequestTransferExit
_SID_ER  = 0x11   # ECUReset
_SID_TP  = 0x3E   # TesterPresent

_RC_START = 0x01


class UdsError(Exception):
    """Raised on negative UDS responses."""

    def __init__(self, sid: int, nrc: int):
        self.sid = sid
        self.nrc = nrc
        super().__init__(f"Negative response for SID 0x{sid:02X}: NRC 0x{nrc:02X}")


class UdsSession:
    def __init__(self, node: NodeConfig, channel: str, interface: str = "socketcan"):
        bus = can.Bus(interface=interface, channel=channel)
        addressing = NormalCanAddressingInformation(
            rx_physical={"can_id": node.response_can_id},
            tx_physical={"can_id": node.request_can_id},
        )
        self._transport: AbstractUdsTransportInterface = PyCanTransportInterface(
            network_manager=bus,
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

    def security_access(self) -> None:
        """Full seed → key exchange using the node's configured algorithm."""
        algo = self._node.security_algorithm
        buf_size = self._node.security_buffer_size
        kw = self._node.security_kw

        # Request seed (sub-function 0x01)
        resp = self._send_raw([_SID_SA, 0x01])
        self._check_positive(resp, _SID_SA)
        # Positive response: 67 01 <seed bytes>
        seed = bytes(resp[2:2 + buf_size])

        key = compute_key(algo, seed, kw)

        # Send key (sub-function 0x02)
        resp = self._send_raw([_SID_SA, 0x02] + list(key))
        self._check_positive(resp, _SID_SA)

    def read_did(self, did_id: int) -> bytes:
        resp = self._send_raw([_SID_RDBI, (did_id >> 8) & 0xFF, did_id & 0xFF])
        self._check_positive(resp, _SID_RDBI)
        # Positive response: 62 <DID high> <DID low> <data...>
        return bytes(resp[3:])

    def write_did(self, did_id: int, data: bytes) -> None:
        payload = [_SID_WDBI, (did_id >> 8) & 0xFF, did_id & 0xFF] + list(data)
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
        return max_block_len

    def transfer_data(self, payload: bytes, max_block_len: int) -> None:
        """Transfer payload in chunks. max_block_len includes the 1-byte sequence counter."""
        chunk_size = max_block_len - 2  # subtract SID byte + sequence counter byte
        seq = 0x01
        offset = 0
        while offset < len(payload):
            chunk = payload[offset:offset + chunk_size]
            resp = self._send_raw([_SID_TD, seq] + list(chunk))
            self._check_positive(resp, _SID_TD)
            offset += len(chunk)
            seq = (seq % 0xFF) + 1  # wrap 0x01..0xFF

    def request_transfer_exit(self) -> None:
        resp = self._send_raw([_SID_RTE])
        self._check_positive(resp, _SID_RTE)

    def ecu_reset(self, reset_type: int = 0x01) -> None:
        resp = self._send_raw([_SID_ER, reset_type])
        self._check_positive(resp, _SID_ER)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_raw(self, payload: list[int]) -> list[int]:
        msg = UdsMessage(
            payload=bytearray(payload),
            addressing_type=AddressingType.PHYSICAL,
        )
        _, responses = self._transport.send_message(msg)
        if not responses:
            return []
        return list(responses[0].payload)

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
            self._transport.disconnect()
        except Exception:
            pass
