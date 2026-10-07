#!/usr/bin/env python3
"""DeepSea CAN Diagnostic Tool
Embedded Linux challenge submission.

Author: JonathanDi
Date: 2026-10-07
Runtime: Python 3 on Linux with SocketCAN
Dependencies: Python standard library only
Purpose: Receive-only telemetry and fault monitoring with diagnostic-message reassembly.

Usage:
    python3 main.py --iface vcan0
    python3 main.py --iface vcan0 --grader
"""

import argparse
import json
import signal
import socket
import struct
import sys
import time
from collections import deque


# CAN identifiers and payload limits defined by the challenge.
TELEMETRY_BASE = 0x100
FAULT_ID = 0x1F0
DIAG_BASE = 0x6F0
NUM_MODULES = 4
MAX_DIAG_LENGTH = 64
STATS_INTERVAL = 2.0
DASHBOARD_INTERVAL = 0.5

# Linux struct can_frame: can_id, DLC, padding, and eight data bytes.
FRAME_FORMAT = "=IB3x8s"

# SocketCAN raw-socket filter constants and standard 11-bit identifier mask.
SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
CAN_RAW_FILTER = getattr(socket, "CAN_RAW_FILTER", 1)
CAN_ID_MASK = 0x7FF
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_FILTER_MASK = CAN_ID_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG


def emit(event):
    """Write one flushed NDJSON event to stdout."""
    line = json.dumps(event, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def create_can_socket(interface):
    """Open a receive-only SocketCAN socket with kernel-side ID filters."""
    can_socket = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)

    # The low two ID bits vary within each four-ID module range.
    filters = (
        (TELEMETRY_BASE, CAN_FILTER_MASK & ~0x003),
        (FAULT_ID, CAN_FILTER_MASK),
        (DIAG_BASE, CAN_FILTER_MASK & ~0x003),
    )
    packed_filters = b"".join(
        struct.pack("=II", can_id, can_mask)
        for can_id, can_mask in filters
    )

    try:
        can_socket.setsockopt(SOL_CAN_RAW, CAN_RAW_FILTER, packed_filters)
        can_socket.bind((interface,))
        can_socket.settimeout(0.25)
    except OSError:
        can_socket.close()
        raise

    return can_socket


def unpack_frame(packet):
    """Decode Linux's 16-byte can_frame structure."""
    if len(packet) != struct.calcsize(FRAME_FORMAT):
        return None

    raw_id, dlc, data = struct.unpack(FRAME_FORMAT, packet)
    if dlc > 8:
        return None

    # The challenge uses standard 11-bit CAN identifiers.
    can_id = raw_id & CAN_ID_MASK
    return can_id, dlc, data


class DiagnosticTool:
    """Decode telemetry, faults, and multi-frame module identifications."""

    def __init__(self, grader):
        self.grader = grader
        self.frames_processed = 0
        self.telemetry = {}
        self.faults = deque(maxlen=10)
        self.identifications = {}

        # At most one bounded reassembly state exists for each diagnostic ID.
        self.in_progress = {}

    def decode_telemetry(self, module, data):
        """Convert one 8-byte telemetry payload into engineering units."""
        voltage_raw, current_raw, temp_raw, status, seq = struct.unpack(
            "<HHBBH", data
        )
        event = {
            "type": "telemetry",
            "module": module,
            "seq": seq,
            "voltage": round(voltage_raw * 0.1, 1),
            "current": round(current_raw * 0.01, 2),
            "temp_c": temp_raw - 40,
            "enabled": bool(status & 0x01),
            "fault": bool(status & 0x02),
            "derated": bool(status & 0x04),
        }

        self.telemetry[module] = event
        if self.grader:
            emit(event)

    def decode_fault(self, data):
        """Decode the module number and fault code from a fault frame."""
        event = {
            "type": "fault",
            "module": data[0],
            "code": data[1],
        }
        self.faults.append(event)

        if self.grader:
            emit(event)

    def process_packet(self, packet):
        """Route one received CAN frame to the decoder for its identifier."""
        decoded = unpack_frame(packet)
        if decoded is None:
            return

        can_id, dlc, data = decoded

        if TELEMETRY_BASE <= can_id < TELEMETRY_BASE + NUM_MODULES:
            self.frames_processed += 1
            if dlc == 8:
                module = can_id - TELEMETRY_BASE
                self.decode_telemetry(module, data)

        elif can_id == FAULT_ID:
            self.frames_processed += 1
            if dlc == 8:
                self.decode_fault(data)

        elif DIAG_BASE <= can_id < DIAG_BASE + NUM_MODULES:
            self.frames_processed += 1
            self.process_diag_frame(can_id, data, dlc)

    def process_diag_frame(self, can_id, data, dlc):
        """Start, continue, or abandon one ID's diagnostic reassembly."""
        if dlc != 8:
            self.in_progress.pop(can_id, None)
            return

        frame_type = data[0] & 0xF0

        if frame_type == 0x10:
            self._start_diag_message(can_id, data)
        elif frame_type == 0x20:
            self._append_diag_frame(can_id, data)
        else:
            self.in_progress.pop(can_id, None)

    def _start_diag_message(self, can_id, data):
        """Replace any old attempt and store the First Frame's initial bytes."""
        self.in_progress.pop(can_id, None)

        length = ((data[0] & 0x0F) << 8) | data[1]
        if not 7 < length <= MAX_DIAG_LENGTH:
            return

        self.in_progress[can_id] = {
            "length": length,
            "payload": bytearray(data[2:8]),
            "next_seq": 1,
        }

    def _append_diag_frame(self, can_id, data):
        """Append a correctly sequenced Consecutive Frame, if one is expected."""
        state = self.in_progress.get(can_id)
        if state is None:
            return

        seq = data[0] & 0x0F
        if seq != state["next_seq"]:
            self.in_progress.pop(can_id, None)
            return

        state["payload"].extend(data[1:8])
        state["next_seq"] = (seq + 1) & 0x0F

        if len(state["payload"]) < state["length"]:
            return

        payload = bytes(state["payload"][:state["length"]])
        self.in_progress.pop(can_id, None)

        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError:
            return

        event = {
            "type": "diag_complete",
            "can_id": f"0x{can_id:x}",
            "string": text,
            "ts_ns": time.monotonic_ns(),
        }
        self.identifications[can_id] = text

        if self.grader:
            emit(event)


def draw_dashboard(tool):
    """Render the latest readings, identifications, and recent faults."""
    print("\033[2J\033[H", end="")
    print("DeepSea CAN Diagnostic Tool")
    print("---------------------------")

    for module in range(NUM_MODULES):
        reading = tool.telemetry.get(module)
        if reading is None:
            print(f"Module {module}: (waiting for telemetry)")
            continue

        print(
            f"Module {module}: {reading['voltage']:.1f}V "
            f"{reading['current']:.2f}A {reading['temp_c']}C "
            f"enabled={reading['enabled']} "
            f"fault={reading['fault']} "
            f"derated={reading['derated']}"
        )

    print("---------------------------")
    print("Identification strings:")
    for module in range(NUM_MODULES):
        can_id = DIAG_BASE + module
        text = tool.identifications.get(can_id, "(not yet received)")
        print(f"  0x{can_id:x}: {text}")

    print("---------------------------")
    print("Recent faults:")
    if tool.faults:
        for fault in tool.faults:
            print(f"  module {fault['module']}, code {fault['code']}")
    else:
        print("  (none)")

    sys.stdout.flush()


def stop_on_signal(_signum, _frame):
    """Convert SIGTERM into a normal shutdown so final stats are emitted."""
    raise KeyboardInterrupt


def run(interface, grader):
    """Listen until interrupted, periodically updating the selected output."""
    tool = DiagnosticTool(grader)
    signal.signal(signal.SIGTERM, stop_on_signal)

    try:
        can_socket = create_can_socket(interface)
    except OSError as exc:
        print(f"CAN error: {exc}", file=sys.stderr)
        return 1

    last_stats_time = time.monotonic()
    last_dashboard_time = 0.0

    try:
        while True:
            try:
                packet = can_socket.recv(16)
                tool.process_packet(packet)
            except socket.timeout:
                pass

            now = time.monotonic()
            if grader:
                if now - last_stats_time >= STATS_INTERVAL:
                    emit({
                        "type": "stats",
                        "frames_processed": tool.frames_processed,
                    })
                    last_stats_time = now
            elif now - last_dashboard_time >= DASHBOARD_INTERVAL:
                draw_dashboard(tool)
                last_dashboard_time = now

    except KeyboardInterrupt:
        pass
    finally:
        can_socket.close()
        if grader:
            emit({
                "type": "stats",
                "frames_processed": tool.frames_processed,
            })

    return 0


def main():
    """Parse command-line arguments and start the diagnostic tool."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", required=True)
    parser.add_argument("--grader", action="store_true")
    args = parser.parse_args()
    return run(args.iface, args.grader)


if __name__ == "__main__":
    raise SystemExit(main())

