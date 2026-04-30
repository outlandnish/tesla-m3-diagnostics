#!/usr/bin/env python3
"""Interactive PCS CAN scripting shell.

Launches a REPL with a live CAN bus and PCS-specific helpers for experimenting
with the Tesla Power Conversion System. Runs alongside can_live.py on the same
socketcan channel.

Usage:
  python pcs_send.py --channel can0
  python pcs_send.py --channel vcan0 --interface socketcan
"""

from __future__ import annotations

import argparse
import code
import threading
import time
from typing import Any

import can
import config as _cfg

try:
    from can_decoder import CanDatabase
    _DB: CanDatabase | None = CanDatabase()
except Exception:
    _DB = None

# ---------------------------------------------------------------------------
# Mutable defaults — helpers read these so you can tune once and resend many
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "hv_voltage": 400,    # volts
    "ac_limit": 32,       # amps
    "dcdc_voltage": 13.8, # volts
    "charge_power": 0,    # watts
    "evse_limit": 32,     # amps
}

# ---------------------------------------------------------------------------
# State for heartbeat counter / multiplexer
# ---------------------------------------------------------------------------

_hb_lock = threading.Lock()
_hb_counter = 0   # VCFront counter 0-15
_bms_mux = False  # 0x3B2 alternates between two payloads
_vcf_mux = False  # 0x545 alternates between two payloads

# ---------------------------------------------------------------------------
# Raw send helper
# ---------------------------------------------------------------------------

_bus: can.BusABC | None = None


def _send(can_id: int, data: list[int] | bytes) -> None:
    """Send a CAN frame silently (no output). Used internally for heartbeats."""
    if _bus is None:
        return
    try:
        _bus.send(can.Message(arbitration_id=can_id, data=bytes(data), is_extended_id=False))
    except can.CanOperationError:
        pass  # drop frame on buffer-full; next tick will retry


def raw(can_id: int, data: list[int] | bytes) -> None:
    """Send an arbitrary CAN frame and print confirmation."""
    if _bus is None:
        print("Bus not connected")
        return
    _send(can_id, data)
    print(f"TX  0x{can_id:03X}  [{len(data)}]  {bytes(data).hex()}")


# ---------------------------------------------------------------------------
# PCS TX helpers
# ---------------------------------------------------------------------------

def pcs_mode(mode: str = "off", hv_voltage: int | None = None) -> None:
    """Send 0x22A - main PCS mode control.

    mode: 'off' | 'charge' | 'dcdc' | 'both'
    hv_voltage: HV voltage setpoint in volts (default: DEFAULTS['hv_voltage'])
    """
    modes = {"off": 0x00, "charge": 0x05, "dcdc": 0x09, "both": 0x0D}
    if mode not in modes:
        print(f"Unknown mode '{mode}'. Choose: {list(modes)}")
        return
    v = hv_voltage if hv_voltage is not None else DEFAULTS["hv_voltage"]
    v_raw = int(v)
    mode_nibble = modes[mode]
    # byte2: [7:4]=voltage LSBs [3:0]=mode
    byte2 = ((v_raw & 0xF) << 4) | mode_nibble
    byte3 = (v_raw >> 4) & 0xFF
    raw(0x22A, [0x00, 0x00, byte2, byte3, 0x00, 0x00, 0x00, 0x00])


def charger_enable(current_a: float | None = None) -> None:
    """Send 0x13D - enable charger with AC current limit.

    current_a: AC current limit in amps (default: DEFAULTS['ac_limit'])
    """
    a = current_a if current_a is not None else DEFAULTS["ac_limit"]
    raw(0x13D, [0x05, int(a * 2), 0xAA, 0x1A, 0xFF, 0x02, 0x00, 0x00])


def charger_disable() -> None:
    """Send 0x13D - disable charger."""
    a = DEFAULTS["ac_limit"]
    raw(0x13D, [0x0A, int(a * 2), 0xAA, 0x1A, 0xFF, 0x02, 0x00, 0x00])


def charge_power(watts: int | None = None, on: bool = True) -> None:
    """Send 0x2B2 - charge power request.

    watts: power in watts (default: DEFAULTS['charge_power'])
    on: True = charging enabled, False = off
    """
    w = watts if watts is not None else DEFAULTS["charge_power"]
    w_int = int(w)
    enable_byte = 0x02 if on else 0x00
    raw(0x2B2, [w_int & 0xFF, (w_int >> 8) & 0xFF, enable_byte, 0x00, 0x00])


def dcdc_voltage(volts: float | None = None) -> None:
    """Send 0x3A1 - DCDC output voltage setpoint.

    volts: desired output voltage (default: DEFAULTS['dcdc_voltage'])
    """
    v = volts if volts is not None else DEFAULTS["dcdc_voltage"]
    v_raw = int(v * 100)
    raw(0x3A1, [0x09, 0x62, v_raw & 0xFF, ((v_raw >> 8) & 0x0F) | 0x90, 0x08, 0x2C, 0x12, 0x5A])


def evse_limit(current_a: float | None = None) -> None:
    """Send 0x21D - EVSE and cable current limits.

    current_a: EVSE current limit in amps (default: DEFAULTS['evse_limit'])
    """
    a = current_a if current_a is not None else DEFAULTS["evse_limit"]
    raw(0x21D, [0x2D, int(a * 2), 0x00, 0x80, 0x00, 0x60, 0x10, 0x00])


def bms_heartbeat() -> None:
    """Send one 0x3B2 BMS heartbeat frame (prevents bmsMia fault)."""
    global _bms_mux
    with _hb_lock:
        mux = _bms_mux
        _bms_mux = not mux
    if mux:
        _send(0x3B2, [0xE5, 0x0D, 0xEB, 0xFF, 0x0C, 0x66, 0xBB, 0x11])
    else:
        _send(0x3B2, [0xE3, 0x5D, 0xFB, 0xFF, 0x0C, 0x66, 0xBB, 0x06])


def vcfront_heartbeat() -> None:
    """Send one 0x545 VCFront heartbeat frame (prevents vcfrontMia fault).

    Alternates between two base payloads each call; counter in bits[7:4] of
    byte 6 increments 0-15; byte 7 is CRC = sum(bytes[0:7]) + 0x45 + 0x05.
    """
    global _hb_counter, _vcf_mux
    with _hb_lock:
        ctr = _hb_counter
        mux = _vcf_mux
        _hb_counter = (ctr + 1) & 0xF
        _vcf_mux = not mux
    if mux:
        payload = [0x14, 0x00, 0x3F, 0x70, 0x9F, 0x01, (ctr << 4) | 0x0A, 0x00]
    else:
        payload = [0x03, 0x19, 0x64, 0x32, 0x19, 0x00, (ctr << 4), 0x00]
    payload[7] = (sum(payload[:7]) + 0x45 + 0x05) & 0xFF
    _send(0x545, payload)


# ---------------------------------------------------------------------------
# send_loop — repeat any callable at a fixed interval
# ---------------------------------------------------------------------------

_loops: dict[str, threading.Thread] = {}
_loop_stop: dict[str, threading.Event] = {}


def send_loop(name: str, fn, interval_ms: int) -> None:
    """Call fn() repeatedly every interval_ms milliseconds.

    Example:
        send_loop('dcdc', lambda: dcdc_voltage(13.8), 100)
        send_loop('bms', bms_heartbeat, 10)
    """
    if name in _loops and _loops[name].is_alive():
        print(f"Loop '{name}' already running. Call stop_loop('{name}') first.")
        return
    stop = threading.Event()
    _loop_stop[name] = stop

    def _run() -> None:
        interval_s = interval_ms / 1000.0
        while not stop.is_set():
            fn()
            stop.wait(interval_s)

    t = threading.Thread(target=_run, daemon=True, name=f"loop-{name}")
    _loops[name] = t
    t.start()
    print(f"Loop '{name}' started ({interval_ms}ms interval)")


def stop_loop(name: str) -> None:
    """Stop a named send_loop."""
    ev = _loop_stop.get(name)
    if ev:
        ev.set()
        print(f"Loop '{name}' stopped")
    else:
        print(f"No loop named '{name}'")


def list_loops() -> None:
    """Show all active send_loops."""
    alive = [n for n, t in _loops.items() if t.is_alive()]
    print("Active loops:", alive if alive else "(none)")


# ---------------------------------------------------------------------------
# Background heartbeats — all 14 periodic messages the PCS expects
# ---------------------------------------------------------------------------

_hb_stop: threading.Event | None = None
_hb_thread: threading.Thread | None = None


def _send_10ms() -> None:
    """Messages sent every 10ms."""
    v = int(DEFAULTS["hv_voltage"])
    mode_nibble = 0x00  # off by default; use pcs_mode() to change
    byte2 = ((v & 0xF) << 4) | mode_nibble
    byte3 = (v >> 4) & 0xFF
    _send(0x22A, [0x00, 0x00, byte2, byte3, 0x00, 0x00, 0x00, 0x00])

    a = int(DEFAULTS["ac_limit"] * 2)
    _send(0x13D, [0x0A, a, 0xAA, 0x1A, 0xFF, 0x02, 0x00, 0x00])  # charger off

    bms_heartbeat()


def _send_50ms() -> None:
    """Messages sent every 50ms."""
    vcfront_heartbeat()


def _send_100ms(slot: int) -> None:
    """Messages sent every 100ms, spread across 10 slots (one slot per 10ms tick).

    Spreading avoids blasting all frames at once and overflowing the TX buffer.
    """
    if slot == 0:
        _send(0x20A, [0xF6, 0x15, 0x09, 0x82, 0x18, 0x01])
    elif slot == 1:
        _send(0x212, [0xB9, 0x1C, 0x94, 0xAD, 0xC3, 0x15, 0x06, 0x63])
    elif slot == 2:
        _send(0x232, [0x0A, 0x02, 0xD5, 0x09, 0xCB, 0x04, 0x00, 0x00])
    elif slot == 3:
        _send(0x25D, [0xD9, 0x8C, 0x01, 0xB5, 0x4A, 0xC1, 0x0A, 0xE0])
    elif slot == 4:
        _send(0x321, [0x2C, 0xB6, 0xA8, 0x7F, 0x02, 0x7F, 0x00, 0x00])
    elif slot == 5:
        _send(0x333, [0x04, 0x30, 0x29, 0x07])
    elif slot == 6:
        a = int(DEFAULTS["evse_limit"] * 2)
        _send(0x21D, [0x2D, a, 0x00, 0x80, 0x00, 0x60, 0x10, 0x00])
    elif slot == 7:
        ac = int(DEFAULTS["ac_limit"] * 2)
        _send(0x23D, [0x0A, ac, 0xFF, 0x0F])
    elif slot == 8:
        w = int(DEFAULTS["charge_power"])
        _send(0x2B2, [w & 0xFF, (w >> 8) & 0xFF, 0x00, 0x00, 0x00])
    elif slot == 9:
        v_raw = int(DEFAULTS["dcdc_voltage"] * 100)
        _send(0x3A1, [0x09, 0x62, v_raw & 0xFF, ((v_raw >> 8) & 0x0F) | 0x90, 0x08, 0x2C, 0x12, 0x5A])


def start_heartbeats() -> None:
    """Start all periodic PCS keepalive messages.

    10ms:  0x22A (mode), 0x13D (charger), 0x3B2 (bmsMia)
    50ms:  0x545 (vcfrontMia)
    100ms: 0x20A, 0x212, 0x21D, 0x232, 0x23D, 0x25D, 0x2B2, 0x321, 0x333 (uiMia), 0x3A1
    """
    global _hb_stop, _hb_thread
    if _hb_thread and _hb_thread.is_alive():
        print("Heartbeats already running")
        return

    _hb_stop = threading.Event()

    def _run() -> None:
        # Initial burst so PCS doesn't MIA-fault during startup
        _send_10ms()
        _send_50ms()
        for slot in range(10):
            _send_100ms(slot)

        # Absolute-time scheduling avoids drift from accumulated sleep jitter
        tick = 0
        next_tick = time.monotonic() + 0.010
        while not _hb_stop.is_set():
            now = time.monotonic()
            delay = next_tick - now
            if delay > 0:
                _hb_stop.wait(delay)
            next_tick += 0.010
            tick += 1
            _send_10ms()
            if tick % 5 == 0:
                _send_50ms()
            _send_100ms(tick % 10)

    _hb_thread = threading.Thread(target=_run, daemon=True, name="pcs-heartbeat")
    _hb_thread.start()
    print("Heartbeats started (10ms/50ms/100ms groups, 14 messages total)")


def stop_heartbeats() -> None:
    """Stop all background heartbeat messages."""
    if _hb_stop:
        _hb_stop.set()
    print("Heartbeats stopped")


# ---------------------------------------------------------------------------
# Background listener — prints decoded PCS frames to terminal
# ---------------------------------------------------------------------------

_listener_stop: threading.Event | None = None
_listener_thread: threading.Thread | None = None


def start_listener(node: str = "PCS") -> None:
    """Print incoming CAN frames (decoded) to the terminal.

    node: filter to frames from this originNode ('' = all frames)
    """
    global _listener_stop, _listener_thread
    if _listener_thread and _listener_thread.is_alive():
        print("Listener already running. Call stop_listener() first.")
        return
    if _bus is None:
        print("Bus not connected")
        return
    if _DB is None:
        print("Warning: CanDatabase not available, will show raw frames only")

    _listener_stop = threading.Event()
    _last_data: dict[int, bytes] = {}

    def _run() -> None:
        while not _listener_stop.is_set():
            msg = _bus.recv(timeout=0.1)
            if msg is None:
                continue
            data = bytes(msg.data)
            if _last_data.get(msg.arbitration_id) == data:
                continue
            _last_data[msg.arbitration_id] = data
            if _DB:
                db_msg = _DB.messages.get(msg.arbitration_id)
                if db_msg is None:
                    continue
                origin = db_msg.get("originNode", "")
                if node and origin != node:
                    continue
                decoded = _DB.decode_frame(msg.arbitration_id, data)
                signals_str = "  ".join(
                    f"{s['signal']}={s['value']}{s.get('units','')}"
                    + (f"({s['label']})" if s.get("label") else "")
                    for s in (decoded or [])
                )
                print(f"\nRX  0x{msg.arbitration_id:03X}  {db_msg['name']}  {signals_str}")
            else:
                print(f"\nRX  0x{msg.arbitration_id:03X}  [{msg.dlc}]  {data.hex()}")

    _listener_thread = threading.Thread(target=_run, daemon=True, name="pcs-listener")
    _listener_thread.start()
    print(f"Listener started (node filter: '{node or 'all'}')")


def stop_listener() -> None:
    """Stop the background listener thread."""
    if _listener_stop:
        _listener_stop.set()
    print("Listener stopped")


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

_HELP = """
PCS scripting shell — available functions:

  SEND
    pcs_mode(mode, hv_voltage)   0x22A  mode: 'off','charge','dcdc','both'
    charger_enable(current_a)    0x13D  enable charger
    charger_disable()            0x13D  disable charger
    charge_power(watts, on)      0x2B2  charge power request
    dcdc_voltage(volts)          0x3A1  DCDC output voltage setpoint
    evse_limit(current_a)        0x21D  EVSE current limit
    bms_heartbeat()              0x3B2  one BMS keepalive frame
    vcfront_heartbeat()          0x545  one VCFront keepalive frame
    raw(can_id, data)                   send arbitrary frame

  LOOPS
    send_loop(name, fn, interval_ms)    repeat fn() at fixed interval
    stop_loop(name)                     stop a named loop
    list_loops()                        show active loops

  KEEPALIVES
    start_heartbeats()           BMS@10ms + VCFront@50ms in background
    stop_heartbeats()

  MONITOR
    start_listener(node='PCS')   print decoded incoming frames
    stop_listener()

  DEFAULTS dict — tweak once, all helpers pick it up:
    DEFAULTS['hv_voltage'] = 400
    DEFAULTS['ac_limit']   = 32
    DEFAULTS['dcdc_voltage'] = 13.8
    DEFAULTS['charge_power'] = 0
    DEFAULTS['evse_limit']  = 32
"""


def help() -> None:
    print(_HELP)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _bus

    parser = argparse.ArgumentParser(description="Interactive PCS CAN scripting shell")
    parser.add_argument("--channel", help="CAN channel")
    parser.add_argument("--interface", help="python-can interface")
    parser.add_argument("--bitrate", type=int, default=None, help="CAN bitrate (optional)")
    _cfg.apply_defaults(parser)
    args = parser.parse_args()

    kwargs: dict = {"channel": args.channel, "interface": args.interface}
    if args.bitrate:
        kwargs["bitrate"] = args.bitrate

    _bus = can.Bus(**kwargs)
    print(f"Connected: {args.channel} ({args.interface})")
    print(_HELP)

    local_ns = {
        "bus": _bus,
        "DEFAULTS": DEFAULTS,
        "raw": raw,
        "pcs_mode": pcs_mode,
        "charger_enable": charger_enable,
        "charger_disable": charger_disable,
        "charge_power": charge_power,
        "dcdc_voltage": dcdc_voltage,
        "evse_limit": evse_limit,
        "bms_heartbeat": bms_heartbeat,
        "vcfront_heartbeat": vcfront_heartbeat,
        "send_loop": send_loop,
        "stop_loop": stop_loop,
        "list_loops": list_loops,
        "start_heartbeats": start_heartbeats,
        "stop_heartbeats": stop_heartbeats,
        "start_listener": start_listener,
        "stop_listener": stop_listener,
        "help": help,
    }

    try:
        import IPython
        IPython.start_ipython(argv=[], user_ns=local_ns)
    except ImportError:
        code.interact(local=local_ns, banner="")

    _bus.shutdown()


if __name__ == "__main__":
    main()
