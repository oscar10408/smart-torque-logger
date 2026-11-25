"""
ATX probe - find which Modbus settings the controller answers to.

Tries every combination of port / unit id / framing and reads register 22
(the "driver running" register used by v4). Prints which combinations answer.

Usage:
    python atx_probe.py 192.168.0.10
    python atx_probe.py 192.168.0.10 --ports 502 1001 --units 1 0 255

Requires: pymodbus==3.6.9
"""

import argparse
import logging
import socket

from pymodbus import Framer
from pymodbus.client import ModbusTcpClient

logging.getLogger("pymodbus").setLevel(logging.CRITICAL)   # keep the output readable


def port_open(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def try_read(host, port, unit, framer):
    client = ModbusTcpClient(host, port=port, framer=framer, timeout=2, retries=0)
    try:
        if not client.connect():
            return "cannot connect"
        rr = client.read_holding_registers(22, 1, slave=unit)
        if rr.isError():
            return f"error: {rr}"
        return f"OK -> register 22 = {rr.registers[0]}"
    except Exception as e:
        return f"exception: {e}"
    finally:
        client.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("host")
    p.add_argument("--ports", type=int, nargs="+", default=[502, 1001])
    p.add_argument("--units", type=int, nargs="+", default=[1, 0, 255])
    args = p.parse_args()

    print(f"Probing {args.host}\n")
    print("Step 1 - is the TCP port open?")
    open_ports = []
    for port in args.ports:
        ok = port_open(args.host, port)
        print(f"  port {port:<5} {'OPEN' if ok else 'closed / no answer'}")
        if ok:
            open_ports.append(port)
    if not open_ports:
        print("\nNo port is open: check cable, IP address and that Modbus TCP is enabled.")
        return

    print("\nStep 2 - does it answer a Modbus read (register 22)?")
    working = []
    for port in open_ports:
        for framer in (Framer.SOCKET, Framer.RTU):
            for unit in args.units:
                result = try_read(args.host, port, unit, framer)
                print(f"  port {port:<5} framer {framer.value:<7} unit {unit:<4} {result}")
                if result.startswith("OK"):
                    working.append((port, framer.value, unit))

    print()
    if working:
        port, framer, unit = working[0]
        print("Working settings found. Put these in config.ini:")
        print(f"  port = {port}\n  unit_id = {unit}\n  framer = {framer}")
    else:
        print("No combination answered. The port is open but does not speak Modbus TCP")
        print("with these settings - check the controller's communication settings")
        print("(Modbus TCP enabled? which port? which unit/slave id?) or ask the vendor.")


if __name__ == "__main__":
    main()
