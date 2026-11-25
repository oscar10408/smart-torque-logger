"""
Fake ATX controller for testing ATX Logger without the real hardware.

Starts a Modbus TCP server on this PC that behaves like the ATX controller as
far as the vendor's v4 script uses it, and replays screw runs from a USB-export
CSV (e.g. sample_data/demo_controller_export.csv), or random data if no CSV is given:

    register 22         goes 1 while a "screw" is running, back to 0 when done
    registers 316-356   Report Info of the last run (after query code 750)
    registers 210-223   Tool Info string (after query code 650)
    registers 207-208   query status [echo code, 1 = success]
    registers 8100+     barcode of tool 1

Usage (two terminals):
    python fake_atx_controller.py --csv sample_data/demo_controller_export.csv --port 5020
    # in config.ini of ATX Logger set:  host = 127.0.0.1   port = 5020
    python atx_logger_app.py            (or double-click the .exe)

Simulating the barcode scanner (--type-sn):
    If the CSV has a "Saved scanner string" (the board SN), the simulator types
    that SN on the keyboard + Enter whenever it changes, fast like a real
    keyboard-mode scanner, so ATX Logger links the runs exactly as on site.
    Keystrokes go to the focused window: click on an empty Notepad first.
    Without --type-sn the SN is only written to barcode register 8100.

    python fake_atx_controller.py --csv sample_data/demo_controller_export.csv --type-sn

Requires: pymodbus==3.6.9  (and pynput for --type-sn)
"""

import argparse
import csv
import logging
import random
import struct
import threading
import time

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import StartTcpServer

# USB export columns that make up the 41 report registers (progress -> H, L)
REPORT_COLUMNS = [
    "Tool 1|2", "Current screw progress ID", "Sequence ID", "Parameter ID",
    "Target torque", "Target angle", "Target torque rate", "Final torque",
    "Tightening angle", "Rotation angle", "Current status", "Operation time",
    "Error code", "Max. angle of final stage", "Min. angle of final stage",
    "Max. torque of final stage", "Min. torque of final stage", "Torque unit",
    "Tool spec. torque", "Tool max. torque", "Pre-tightening torque",
    "Set total operation time", "Max total running angle", "Max. torque",
    "Min. torque", "Max. angle", "Min. angle", "Max. operation time",
    "Min. operation time", "Prevail torque", "Final + Prevail torque",
    "Final Current", "Clamp torque", "Max. clamp torque", "Min. clamp torque",
    "Clamp Angle", "Max. clamp angle", "Min. clamp angle",
    "Min total running angle", "User ID",
]
TOOL_INFO_TEXT = "FAKE-ATX-TOOL-0001"


def text_to_registers(text, n_registers):
    data = text.encode("latin-1")[: n_registers * 2].ljust(n_registers * 2, b"\x00")
    return list(struct.unpack("<" + "H" * n_registers, data))


def report_from_usb_row(row):
    regs = []
    for col in REPORT_COLUMNS:
        v = int(row.get(col) or 0)
        if col == "Current screw progress ID":
            regs += [v & 0xFFFF, (v >> 16) & 0xFFFF]   # low word first (as the real controller)
        else:
            regs.append(v & 0xFFFF)           # negative -> unsigned 16-bit, like a real register
    return regs


def random_run():
    ok = random.random() > 0.2
    row = {c: "0" for c in REPORT_COLUMNS}
    row.update({"Current screw progress ID": str(random.randint(1, 6)), "Sequence ID": "1",
                "Parameter ID": "2", "Target torque": "5000",
                "Final torque": str(random.randint(4850, 5150) if ok else random.randint(1000, 4000)),
                "Rotation angle": str(random.randint(1200, 1800) if ok else 800),
                "Current status": "1" if ok else "2", "Operation time": str(random.randint(900, 1400)),
                "Error code": "0" if ok else "1001", "Max. torque of final stage": "5500",
                "Min. torque of final stage": "4500", "Max total running angle": "2000",
                "Min total running angle": "1000", "User ID": "1"})
    return row


class FakeHoldingRegisters(ModbusSequentialDataBlock):
    """Holding registers that answer query commands written at register 200."""

    def __init__(self):
        super().__init__(0, [0] * 8500)
        self.pending_report = [0] * 41

    def setValues(self, address, values):
        super().setValues(address, values)
        if address == 200 and values:
            code = values[0]
            if code == 0x02EE:                                   # Report Info
                super().setValues(316, self.pending_report)
                super().setValues(207, [code, 1, 0])
            elif code == 0x028A:                                 # Tool Info
                super().setValues(210, text_to_registers(TOOL_INFO_TEXT, 14))
                super().setValues(207, [code, 1, 0])
            else:
                super().setValues(207, [code, 0, 0])


def type_like_scanner(text, char_delay=0.01):
    """Type text + Enter like a keyboard-mode barcode scanner (a few ms per key)."""
    from pynput.keyboard import Controller, Key
    kb = Controller()
    for ch in text:
        kb.press(ch); kb.release(ch)
        time.sleep(char_delay)
    kb.press(Key.enter); kb.release(Key.enter)


def simulate(block, rows, interval, barcode_prefix, report_delay=0.0, update_delay=0.0,
             type_sn=False):
    """Replay runs forever: driver on -> wait -> result ready -> driver off."""
    time.sleep(2)
    i = 0
    last_sn = None
    while True:
        row = rows[i % len(rows)] if rows else random_run()
        sn = (row.get("Saved scanner string") or "").strip()
        # A new board starts when the SN changes, or when the file loops back to row 1
        if type_sn and sn and (sn != last_sn or (rows and i % len(rows) == 0)):
            print(f"--- scanning SN {sn} ---")
            type_like_scanner(sn)
            time.sleep(1.0)
        last_sn = sn
        report = report_from_usb_row(row)
        op_seconds = min(int(row.get("Operation time") or 500) / 1000, 2.0)

        block.setValues(22, [1])                                  # driver starts
        time.sleep(max(op_seconds, 0.1))
        def publish_realtime(r=report):
            block.setValues(19, [r[16], r[17]])                   # final-stage torque limits
            block.setValues(36, [r[10]])                          # total angle (stand-in)
            block.setValues(38, [r[11]])                          # tightening result (stand-in)
        if update_delay > 0:      # realtime AND report update late (seen on site)
            threading.Timer(update_delay, publish_realtime).start()
            threading.Timer(update_delay + report_delay,
                            lambda r=report: setattr(block, "pending_report", r)).start()
        elif report_delay > 0:    # only the report updates late
            block.pending_report_later = None
            threading.Timer(report_delay, lambda r=report: setattr(block, "pending_report", r)).start()
            publish_realtime()
        else:
            block.pending_report = report
            publish_realtime()
        barcode = row.get("Saved scanner string") or f"{barcode_prefix}{i // 6:05d}"
        block.setValues(8100, text_to_registers(barcode, 100))
        block.setValues(22, [0])                                  # driver stops
        print(f"run {i + 1}: status={row.get('Current status')} torque={row.get('Final torque')} "
              f"angle={row.get('Rotation angle')} barcode={barcode}")
        i += 1
        time.sleep(interval)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", help="USB export CSV to replay (default: random data)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5020)
    p.add_argument("--interval", type=float, default=2.0, help="seconds between runs")
    p.add_argument("--report-delay", type=float, default=0.0,
                   help="seconds before the new report is available (real controller lag)")
    p.add_argument("--update-delay", type=float, default=0.0,
                   help="seconds before realtime registers AND report update (real controller lag)")
    p.add_argument("--barcode-prefix", default="FAKESN", help="used when the CSV has no barcode")
    p.add_argument("--type-sn", action="store_true",
                   help="type the CSV's SN on the keyboard (like the scanner) whenever it changes")
    args = p.parse_args()

    rows = []
    if args.csv:
        with open(args.csv, encoding="latin-1", newline="") as f:
            rows = [r for r in csv.DictReader(f) if (r.get("Year") or "").strip()]
        print(f"Replaying {len(rows)} runs from {args.csv}")

    block = FakeHoldingRegisters()
    context = ModbusServerContext(slaves=ModbusSlaveContext(hr=block, zero_mode=True), single=True)
    threading.Thread(target=simulate, args=(block, rows, args.interval, args.barcode_prefix, args.report_delay, args.update_delay, args.type_sn),
                     daemon=True).start()
    logging.basicConfig(level=logging.WARNING)
    print(f"Fake ATX controller listening on {args.host}:{args.port}  (Ctrl+C to stop)")
    StartTcpServer(context=context, address=(args.host, args.port))


if __name__ == "__main__":
    main()
