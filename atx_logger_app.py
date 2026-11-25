"""
ATX Logger - desktop app that runs the vendor's ATX data collection
(atx-pymodbus-client-v4.py) continuously and saves the raw data to CSV.

Double-click the .exe -> it connects to the ATX controller and starts logging
automatically. The window shows connection status, rows saved today and a log.

Files (all in the same folder as the .exe):
    config.ini        controller IP / port (created on first run, edit if needed)
    headers1.txt      column selection for Tool 1 (same format as v4)
    headers2.txt      column selection for Tool 2 (same format as v4)
    YYYY-MM-DD_api.csv  MAIN OUTPUT: MES screw-data API format (18 columns)
    YYYY-MM-DD_raw.csv  all v4 columns + scanned SN columns (write_raw = true)
    scans_YYYY-MM-DD.csv  every detected scan (time + SN) - only if write_scans = true
    atx_logger.log    application log (rotating, max ~5 MB x 3)
    pending_cache.db  rows waiting to be written to CSV (see "Write cache")

Write cache:
    Every row is first committed to pending_cache.db (SQLite), then appended to
    the CSV. If the CSV cannot be written - typically because it is open in
    Excel, which locks the file on Windows - the rows stay in the cache and are
    retried every few seconds, in their original order, into the file of the
    day they were recorded. Rows left in the cache when the app closes are
    written at the next start. Data collection never stops because of this.

Serial number (SN) linking:
    The barcode scanner is a keyboard-mode (HID) scanner whose input goes to the
    MES client on this PC. ATX Logger listens to the keyboard in the background
    WITHOUT blocking or changing anything (the MES client still receives the SN)
    and keeps only input that looks like a scan: characters arriving very fast
    (gap <= scanner_max_gap_ms) and ending with Enter. Normal typing is ignored
    and never stored.
    Linking is by ORDER, not by timestamps: the last scanned SN is attached to
    every screw record until the next scan. Products that do not use the smart
    tool produce no records, so their SN is simply replaced by the next scan.
    Extra columns: Scanned_SN, SN_Scan_Time, Screws_Since_Scan, SN_Check

API output (YYYY-MM-DD_api.csv), one row per screw record:
    SORT               screw position 1..12: starts at 1 on every new SN, next screw
                       after each OK, back to 1 after screws_per_board (12).
                       screws_per_board = 0 uses the controller's own progress.
    orgCode            org_code (DEMO_ORG)
    SERIAL_NUMBER      scanned SN
    SCREWDRIVER_ID     screwdriver_id (1)
    NUMBER_OF_TURNS    Total_Angle (realtime register 36) / 360
    TORQUE / LIMITS    Final_Torque, Min/Max_Torque_Final_Stage, divided by 1000
    LOCKING_TIME       Operation_Time as-is
    S_RESULTS          Status as-is (1 OK, 2 NG)
    S_ANGLE            Rotation_Angle as-is. Records with a NEGATIVE angle
                       (loosening / reverse) are not written to the API file;
                       the _raw file keeps them, SN_Check = NEGATIVE_ANGLE_SKIPPED.
    S_ANGLE limits     Min/Max_Total_Angle as-is
    PROGRAM_NUMBER, PROGRAM_NAME, DETAILED_INFO, S_FALG, S_GROUP: empty
    Pass/Fail          Pass if Min total running angle <= Rotation angle <= Max total
                       running angle AND Min. torque of final stage <= Final torque
                       <= Max. torque of final stage
    Error_Code         controller error code as-is
    Record_Time        time the screw finished (host PC clock, YYYY-MM-DD HH:MM:SS),
                       same value as Date-Time in the _raw file
    Attempt            n-th tightening try (OK/NG) on this screw for this SN;
                       restarts on a new SN and after the last screw (12).
                       Retries = Attempt - 1

Tool ports and units (config.ini):
    tools = T1          only tool port 1 is polled and written (one driver).
                        Use T1,T2 for two drivers. T2 = headers2.txt columns.
    torque_scale        torque columns are divided by 1000 (5012 -> 5.012);
                        the list of torque columns is torque_columns.
    (SN_Check: NO_SN = nothing scanned yet, SN_OLD = scan older than sn_max_age_min).

Data collection logic is the same as v4:
    poll registers 22 / 72 -> driver starts -> driver stops ->
    read realtime (0-45, 50-95), barcodes (8100, 8301), query Report Info
    (code 0x02EE, registers 316-356) -> map with headers -> append one CSV row

Changes compared to v4 (robustness only, output columns are identical):
    1. Never crashes on connection loss: reconnects automatically with back-off.
    2. A failed Report Info / Tool Info query leaves those fields empty instead
       of crashing (v4) or silently re-using the previous screw's data (v4).
    3. The CSV file name follows the current date, so a new file starts every day
       (v4 fixed the name at start-up).
    4. Null padding is stripped from barcode / tool info strings.
    5. Query commands are sent with the same Modbus TCP client (function code 16,
       write multiple registers at 200) instead of a second raw socket. The bytes
       on the wire are the same request as v4's hand-built frame.
    6. Files are read / written next to the .exe, not the current directory.

Requires: pymodbus==3.6.9 (the version in the vendor README), pynput (keyboard listener).
"""

import configparser
import csv
import json
import logging
import queue
import re
import sqlite3
import struct
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pymodbus import Framer
from pymodbus.client import ModbusTcpClient


# =============================================================================
# Paths and settings
# =============================================================================

def app_dir():
    """Folder of the .exe when frozen by PyInstaller, else folder of this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
CONFIG_PATH = APP_DIR / "config.ini"
LOG_PATH = APP_DIR / "atx_logger.log"
CACHE_PATH = APP_DIR / "pending_cache.db"
FLUSH_RETRY_SECONDS = 5         # retry writing cached rows to CSV this often

DEFAULT_CONFIG = {
    "host": "192.168.0.10",     # default IP of the ATX controller (v4)
    "port": "502",              # Modbus TCP port (v4)
    "unit_id": "1",             # Modbus unit / slave id
    "framer": "socket",         # "socket" = standard Modbus TCP, "rtu" = RTU over TCP
    "headers1": "headers1.txt",
    "headers2": "headers2.txt",
    "tools": "T1",                  # tool ports to record: T1, T2 or T1,T2
    "org_code": "DEMO_ORG",         # API orgCode, same for every row
    "screwdriver_id": "1",          # API SCREWDRIVER_ID for T1 (T2 gets this + 1)
    "write_raw": "true",            # also write YYYY-MM-DD_raw.csv (all v4 columns)
    "write_scans": "false",         # also write scans_YYYY-MM-DD.csv (every detected scan)
    # SORT (screw position). N > 0: counted by this app - starts at 1 on every new
    # SN, moves to the next screw after each OK (NG lock), back to 1 after N.
    # 0: use the controller's own screw progress instead.
    "screws_per_board": "12",
    "torque_scale": "0.001",        # raw torque integer x scale (e.g. 5012 -> 5.012)
    "torque_decimals": "3",
    # header names (without T1-/T2-) that hold torque values and get scaled.
    # Codes and rates (Torque_Unit, Torque_UOM_ID, Target_Torque_Rate) are NOT torque values.
    "torque_columns": ("Target_Torque, Final_Torque, Final_Stage_Max_Torque, Final_Stage_Min_Torque, "
                       "Max_Torque_Final_Stage, Min_Torque_Final_Stage, Max_Torque, Min_Torque, "
                       "Final_and_Prevailing_Torque, Prevailing_Torque, Final+Prevailing_Torque, "
                       "PreTighten, Clamp_Torque, Max_Clamp, Min_Clamp, "
                       "Torque_Tool_Rating, Max_Tool_Rating"),
    "output_dir": "./Data",          # "." = same folder as the .exe
    "scanner_enabled": "true",      # listen to the keyboard-mode barcode scanner
    "scanner_max_gap_ms": "50",     # max time between characters of one scan
    "scanner_min_length": "6",      # shorter input is not treated as a scan
    "scanner_pattern": "ABC.*",          # optional regex the SN must fully match, e.g. ^[A-Z0-9]{15}$
    "sn_max_age_min": "30",         # flag SN_OLD when the scan is older than this (0 = off)
}

# Register map, taken unchanged from v4
# API output (MES screw-data API spec) - exact names and order
API_COLUMNS = [
    "Record_Time",
    "orgCode", "SERIAL_NUMBER", "SORT", "SCREWDRIVER_ID", "PROGRAM_NUMBER",
    "PROGRAM_NAME", "NUMBER_OF_TURNS", "TORQUE", "TORQUE_LOW_LIMIT",
    "TORQUE_HIGH_LIMIT", "LOCKING_TIME", "S_RESULTS", "DETAILED_INFO",
    "S_FALG", "S_ANGLE", "S_ANGLE_LOW_LIMIT", "S_ANGLE_HIGH_LIMIT", "S_GROUP",
    # extra columns requested for this station (not in the API doc)
    "Pass/Fail", "Error_Code", "Attempt",
]
# Positions inside the 41 Report Info registers (316-356), same order as
# headers1.txt lines 49-89 and the controller's USB export.
# Screw progress (position in the sequence) is a 32-bit value, LOW word first.
# Verified on the real controller 2026-11-23: register 1 = position, register 2 = 0.
# (headers1.txt line 50/51 names them Screw_H/Screw_L - the names are swapped.)
RPT_SCREW_L = 1                 # low word  = screw position 1..12
RPT_SCREW_H = 2                 # high word = 0
RPT_FINAL_TORQUE = 8
RPT_ROTATION_ANGLE = 10
RPT_STATUS = 11
RPT_OPERATION_TIME = 12
RPT_ERROR_CODE = 13
RPT_MAX_TORQUE_FINAL_STAGE = 16
RPT_MIN_TORQUE_FINAL_STAGE = 17
RPT_MAX_TOTAL_ANGLE = 23
RPT_MIN_TOTAL_ANGLE = 39
RT_TOTAL_ANGLE = 36             # realtime register index (headers1.txt line 37)

# Per tool port: running flag, realtime block, barcode block, headers file key
TOOL_REGISTERS = {
    "T1": {"running": 22, "realtime": (0, 46),  "barcode": (8100, 100), "headers": "headers1"},
    "T2": {"running": 72, "realtime": (50, 46), "barcode": (8301, 100), "headers": "headers2"},
}
REG_QUERY_CMD = 200             # 0x00C8, where query commands are written
REG_QUERY_STATUS = (207, 3)     # [echo of query code, success flag, -]
REG_TOOL_INFO = (210, 14)
REG_REPORT = (316, 41)
QUERY_TOOL_INFO = 0x028A        # 650
QUERY_REPORT = 0x02EE           # 750
QUERY_SEND = 0x0100             # "sendcmd" in v4

POLL_INTERVAL = 0.02            # v4: 20 ms between status reads
SETTLE_AFTER_RUN = 0.25         # v4: wait after the driver stops
QUERY_WAIT = 0.25               # v4: wait after sending a query
MAX_RETRIES = 5                 # v4
RETRY_DELAY = 0.25              # v4
# Right after the driver stops, the controller can still hold the PREVIOUS
# screw's data (seen on site 2026-11-23): sometimes only the report is old,
# sometimes the realtime registers AND the report are both old. A run is
# accepted only when
#   1. the report differs from the last recorded report (controller updated), and
#   2. its Rotation angle matches the realtime Total_Angle (register 36).
# Both are re-read until this holds. If the report never changes, the
# controller produced no new result (e.g. trigger touched without tightening):
# the run is not written to the API file (raw file: DUPLICATE_SKIPPED).
REPORT_FRESH_TRIES = 12         # x (query wait + delay) ~ up to about 4 s
REPORT_FRESH_DELAY = 0.1        # extra pause between re-queries (s)
ANGLE_MATCH_TOLERANCE = 3       # degrees
RECONNECT_DELAYS = [1, 2, 5, 10, 30]   # seconds, grows on repeated failures


def load_config():
    """Read config.ini; create it with defaults if it does not exist."""
    parser = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        parser["atx"] = DEFAULT_CONFIG
        with open(CONFIG_PATH, "w") as f:
            f.write("; ATX Logger settings - edit and restart the app\n")
            parser.write(f)
    parser.read(CONFIG_PATH)
    cfg = dict(DEFAULT_CONFIG)
    if parser.has_section("atx"):
        cfg.update(parser["atx"])
    return cfg


def setup_logging():
    logger = logging.getLogger("atx")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


# =============================================================================
# v4 helpers (same logic as the vendor script)
# =============================================================================

def filter_header_data(header_path):
    """{header name: line index} for lines that do not start with '/' (v4)."""
    collected, indexes = [], []
    with open(header_path, "r") as f:
        for index, line in enumerate(f):
            text = line.strip()
            if not text.startswith("/"):
                collected.append(text)
                indexes.append(index)
    return dict(zip(collected, indexes))


def retrieve_register_data(header_data, realtime_regs, barcode, tool_info, report_regs):
    """Map header indexes to values (v4):
    0-45 realtime registers, 46 barcode, 47 tool info, 48+ report registers."""
    out = []
    for index in header_data.values():
        if index < 46:
            out.append(realtime_regs[index] if realtime_regs and index < len(realtime_regs) else "")
        elif index == 46:
            out.append(barcode)
        elif index == 47:
            out.append(tool_info)
        else:
            i = index - 48
            out.append(report_regs[i] if report_regs and 0 <= i < len(report_regs) else "")
    return out


def registers_to_string(registers):
    """Decode a text field. Each register holds 2 characters, low byte first
    (same result as v4's byte swap + BinaryPayloadDecoder). Null padding removed."""
    raw = b"".join(struct.pack("<H", r & 0xFFFF) for r in registers)
    return raw.split(b"\x00", 1)[0].decode("latin-1").strip()


# =============================================================================
# Write cache: rows wait here until the CSV can be written
# =============================================================================

class PendingStore:
    """Durable FIFO of CSV rows (SQLite). Rows are committed here first, then
    flushed to their CSV file. A locked / unwritable CSV only delays writing;
    nothing is lost, and leftover rows are flushed at the next start.

    Each call opens its own connection, so different threads (collector,
    scanner listener) can use the same store safely."""

    def __init__(self, path):
        self.path = Path(path)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pending (
                              id INTEGER PRIMARY KEY AUTOINCREMENT,
                              file TEXT NOT NULL,          -- target CSV path
                              encoding TEXT NOT NULL,
                              header TEXT NOT NULL,        -- JSON list, written if file is new
                              row TEXT NOT NULL)""")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, file, header, row, encoding="latin-1"):
        with self._connect() as db:            # commits on exit
            db.execute("INSERT INTO pending (file, encoding, header, row) VALUES (?, ?, ?, ?)",
                       (str(file), encoding, json.dumps(header), json.dumps(row)))

    def count(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]

    @staticmethod
    def _matching_file(path, header, encoding):
        """If the file already exists with different columns (app updated or
        headers.txt edited during the day), use name_2.csv, name_3.csv ...
        so rows never end up under the wrong header."""
        candidate, n = path, 1
        while candidate.exists():
            try:
                with open(candidate, "r", encoding=encoding, newline="", errors="replace") as f:
                    first = next(csv.reader(f), None)
            except OSError:
                return candidate             # locked: let the write fail and retry later
            if first == header:
                return candidate
            n += 1
            candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        return candidate

    def flush(self):
        """Write cached rows to their CSV files in original order.
        Returns (rows_written, rows_still_pending, error_or_None).
        Stops at the first file that cannot be written, so order is preserved."""
        written = 0
        with self._connect() as db:
            rows = db.execute("SELECT id, file, encoding, header, row FROM pending ORDER BY id").fetchall()
            i = 0
            while i < len(rows):
                file, encoding, header = rows[i][1], rows[i][2], rows[i][3]
                batch = []                              # consecutive rows for the same file
                while i < len(rows) and rows[i][1] == file:
                    batch.append(rows[i])
                    i += 1
                path = self._matching_file(Path(file), json.loads(header), encoding)
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    new_file = not path.exists()
                    with open(path, "a", encoding=encoding, newline="", errors="replace") as f:
                        writer = csv.writer(f)
                        if new_file:
                            writer.writerow(json.loads(header))
                        for r in batch:
                            writer.writerow(json.loads(r[4]))
                except OSError as e:                   # locked by Excel, missing drive ...
                    db.commit()
                    return written, len(rows) - written, f"{path.name}: {e.strerror or e}"
                db.executemany("DELETE FROM pending WHERE id = ?", [(r[0],) for r in batch])
                db.commit()
                written += len(batch)
        return written, 0, None


# =============================================================================
# Barcode scanner (keyboard mode) -> current serial number
# =============================================================================

class ScanDetector:
    """Separates scanner input from human typing.

    A scanner "types" a whole barcode within a few milliseconds per character and
    ends with Enter. Characters that arrive slower than max_gap reset the buffer,
    so normal typing never forms a scan. Nothing except accepted scans is kept.
    Times are in seconds (time.monotonic()).
    """

    def __init__(self, max_gap_ms=50, min_length=6, pattern=""):
        self.max_gap = max_gap_ms / 1000
        self.min_length = min_length
        self.pattern = re.compile(pattern) if pattern else None
        self._buffer = []
        self._last = None

    def feed_char(self, ch, t):
        if self._last is None or t - self._last > self.max_gap:
            self._buffer = []                 # too slow -> not part of a scan
        self._buffer.append(ch)
        self._last = t

    def feed_enter(self, t):
        """Returns the scanned text, or None if the input was not a scan."""
        text = "".join(self._buffer)
        fast_enough = self._last is not None and t - self._last <= self.max_gap
        self._buffer, self._last = [], None
        if not fast_enough or len(text) < self.min_length:
            return None
        text = text.strip()
        if self.pattern and not self.pattern.fullmatch(text):
            return None
        return text

    def reset(self):
        self._buffer, self._last = [], None


class ScanState:
    """Thread-safe holder of the last scanned SN, shared by the keyboard
    listener (writer) and the collector (reader)."""

    def __init__(self, output_dir, events=None, logger=None, store=None, write_scans=False):
        self.output_dir = output_dir
        self.store = store
        self.write_scans = write_scans
        self.events = events
        self.log = logger or logging.getLogger("atx")
        self._lock = threading.Lock()
        self.sn = ""
        self.scan_time = None             # datetime of the last scan
        self.screws_since_scan = 0

    def set_sn(self, sn):
        now = datetime.now()
        with self._lock:
            self.sn, self.scan_time, self.screws_since_scan = sn, now, 0
        if self.write_scans:
            self._append_scan_log(now, sn)
        self.log.info(f"Scanned SN: {sn}")
        if self.events:
            self.events.put(("scan", {"sn": sn, "time": f"{now:%Y-%m-%d %H:%M:%S}"}))

    def take_for_record(self):
        """Called once per screw record: returns (sn, scan_time_text, screws_since_scan)."""
        with self._lock:
            self.screws_since_scan += 1
            when = f"{self.scan_time:%Y-%m-%d %H:%M:%S}" if self.scan_time else ""
            return self.sn, when, self.screws_since_scan, self.scan_time

    def peek(self):
        """Current [SN, scan time text, screws_since_scan, ""] without counting a record."""
        with self._lock:
            when = f"{self.scan_time:%Y-%m-%d %H:%M:%S}" if self.scan_time else ""
            return [self.sn, when, self.screws_since_scan, ""]

    def _append_scan_log(self, now, sn):
        path = self.output_dir / f"scans_{now:%Y-%m-%d}.csv"
        header, row = ["Scan_Time", "SN"], [f"{now:%Y-%m-%d %H:%M:%S}", sn]
        try:
            if self.store:                     # via the cache: survives a locked file
                self.store.add(path, header, row, encoding="utf-8")
                self.store.flush()             # failure is retried by the collector
                return
            self.output_dir.mkdir(parents=True, exist_ok=True)
            new_file = not path.exists()
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if new_file:
                    writer.writerow(header)
                writer.writerow(row)
        except Exception as e:
            self.log.warning(f"Cannot write scan log: {e}")


def start_scanner_listener(cfg, scan_state, logger):
    """Start a passive global keyboard listener (pynput). Returns the listener,
    or None if scanning is disabled or the listener cannot start.
    The listener only observes key presses; it never suppresses them, so other
    programs (e.g. the MES client) receive the scanner input unchanged."""
    if cfg.get("scanner_enabled", "true").strip().lower() not in ("1", "true", "yes"):
        logger.info("Scanner listener disabled in config.ini")
        return None
    try:
        from pynput import keyboard
    except Exception as e:
        logger.warning(f"Scanner listener not available (pynput): {e}")
        return None

    detector = ScanDetector(int(cfg["scanner_max_gap_ms"]), int(cfg["scanner_min_length"]),
                            cfg.get("scanner_pattern", "").strip())

    def on_press(key):
        try:
            t = time.monotonic()
            if key in (keyboard.Key.enter,):
                sn = detector.feed_enter(t)
                if sn:
                    scan_state.set_sn(sn)
            elif getattr(key, "char", None):
                detector.feed_char(key.char, t)
            elif key not in (keyboard.Key.shift, keyboard.Key.shift_r, keyboard.Key.caps_lock):
                detector.reset()               # other keys break a scan sequence
        except Exception:
            logger.exception("Scanner listener error")

    try:
        listener = keyboard.Listener(on_press=on_press, suppress=False)
        listener.daemon = True
        listener.start()
    except Exception as e:
        logger.warning(f"Cannot start scanner listener: {e}")
        return None
    logger.info("Scanner listener started")
    return listener


# =============================================================================
# Data collector (runs in a background thread)
# =============================================================================

class AtxCollector:
    """Collects data from one ATX controller until stop() is called.

    Status updates are sent to `events` (a queue.Queue) as (kind, value) tuples:
        ("status", text) ("row", {"time": ..., "file": ..., "today": n}) ("log", text)
    """

    SN_COLUMNS = ["Scanned_SN", "SN_Scan_Time", "Screws_Since_Scan", "SN_Check"]

    def __init__(self, cfg, events=None, logger=None, scan_state=None, store=None):
        self.cfg = cfg
        self.store = store or PendingStore(CACHE_PATH)
        self._flush_error = None             # last write error, to avoid log spam
        self._last_flush = 0.0
        self.scan_state = scan_state
        self.sn_max_age = float(cfg.get("sn_max_age_min", "30") or 0) * 60
        self.events = events or queue.Queue()
        self.log = logger or logging.getLogger("atx")
        self.unit = int(cfg["unit_id"])
        self.output_dir = (APP_DIR / cfg["output_dir"]).resolve()
        self.client = ModbusTcpClient(cfg["host"], port=int(cfg["port"]),
                                      framer=Framer(cfg.get("framer", "socket").strip().lower()),
                                      timeout=3)
        self._stop = threading.Event()
        self.tool_info = ""
        self.rows_today = 0
        self._today = None

        # Only the configured tool ports are polled, read and written
        names = [t.strip().upper() for t in cfg.get("tools", "T1").split(",") if t.strip()]
        unknown = [t for t in names if t not in TOOL_REGISTERS]
        if unknown or not names:
            raise ValueError(f"config.ini 'tools' must be T1, T2 or T1,T2 (got {cfg.get('tools')!r})")
        self.tools = [dict(TOOL_REGISTERS[t], name=t,
                           header=filter_header_data(APP_DIR / cfg[TOOL_REGISTERS[t]["headers"]]))
                      for t in names]

        # API output settings
        self.org_code = cfg.get("org_code", "DEMO_ORG").strip()
        self.screwdriver_id = int(cfg.get("screwdriver_id", "1") or 1)
        self.write_raw = cfg.get("write_raw", "true").strip().lower() in ("1", "true", "yes")
        # Attempt counter: tightening tries per screw position for the current SN
        self._attempt_sn = None
        self._attempt_counts = {}
        self._last_position = None
        self._ok_count = 0                 # OK screws since the current SN was scanned
        self.screws_per_board = int(cfg.get("screws_per_board", "12") or 0)
        self._last_report = None           # last report written, to detect stale reads

        # Torque conversion
        self.torque_scale = float(cfg.get("torque_scale", "1") or 1)
        self.torque_decimals = int(cfg.get("torque_decimals", "3") or 0)
        self.torque_columns = {c.strip() for c in cfg.get("torque_columns", "").split(",") if c.strip()}

    # ---- messaging ---------------------------------------------------------
    def _emit(self, kind, value):
        self.events.put((kind, value))

    def _info(self, text):
        self.log.info(text)
        self._emit("log", text)

    def _warn(self, text):
        self.log.warning(text)
        self._emit("log", "WARNING: " + text)

    # ---- control -----------------------------------------------------------
    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.is_set()

    def _sleep(self, seconds):
        """Sleep that wakes up immediately when stop() is called."""
        self._stop.wait(seconds)

    # ---- Modbus primitives -------------------------------------------------
    def _connect_forever(self):
        """Block until connected (or stopped). Back-off between attempts."""
        attempt = 0
        while not self.stopped():
            self._emit("status", f"Connecting to {self.cfg['host']}:{self.cfg['port']} ...")
            if self.client.connect():
                self._info(f"Connected to {self.cfg['host']}:{self.cfg['port']}")
                return True
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            self._warn(f"Cannot connect to {self.cfg['host']}:{self.cfg['port']}, retry in {delay}s")
            self._emit("status", f"Disconnected - retrying in {delay}s")
            attempt += 1
            self._sleep(delay)
            self._flush_if_due()
        return False

    def _read(self, address, count):
        """Read holding registers. Returns a list, or None on any error."""
        try:
            rr = self.client.read_holding_registers(address, count, slave=self.unit)
        except Exception as e:                       # connection dropped, timeout ...
            self._warn(f"Read {address} x{count} failed: {e}")
            return None
        if rr.isError():
            self._warn(f"Read {address} x{count} returned error: {rr}")
            return None
        return list(rr.registers)

    def _query(self, code, result_reg):
        """Send a query command and read the result (v4 send_query_command + status check).
        Returns the result registers, or None after MAX_RETRIES failures."""
        address, count = result_reg
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Same request as v4's raw frame: FC16 at 200, values
                # [code, vnumber, param1..4, sendcmd]
                wr = self.client.write_registers(REG_QUERY_CMD,
                                                 [code, 0, 0, 0, 0, 0, QUERY_SEND],
                                                 slave=self.unit)
                if wr.isError():
                    raise RuntimeError(wr)
            except Exception as e:
                self._warn(f"Query {code} send failed (attempt {attempt}): {e}")
                self._sleep(RETRY_DELAY)
                continue

            self._sleep(QUERY_WAIT)
            status = self._read(*REG_QUERY_STATUS)
            if status and status[0] == code and status[1] == 1:
                return self._read(address, count)
            self._sleep(RETRY_DELAY)
        self._warn(f"Query {code} failed after {MAX_RETRIES} attempts - fields left empty")
        return None

    def _running(self):
        """Running flag of each configured tool, or None on read error."""
        flags = []
        for tool in self.tools:
            r = self._read(tool["running"], 1)
            if r is None:
                return None
            flags.append(r[0])
        return flags

    # ---- CSV ---------------------------------------------------------------
    def _csv_path(self, kind):
        """kind = "api" or "raw" -> YYYY-MM-DD_api.csv / YYYY-MM-DD_raw.csv"""
        return self.output_dir / f"{datetime.now():%Y-%m-%d}_{kind}.csv"

    def _write_rows(self, raw_values, api_rows):
        """Commit the rows to the cache, then try to write the cache to the CSVs.
        api_rows: one dict per tool. Returns the API file path."""
        api_path = self._csv_path("api")
        today = api_path.stem
        if today != self._today:                     # new day -> new file / counter
            self._today = today
            self.rows_today = self._count_rows(api_path)
        if self.write_raw:
            raw_header = (["Date-Time"] + self.SN_COLUMNS
                          + [name for t in self.tools for name in t["header"]])
            self.store.add(self._csv_path("raw"), raw_header, raw_values)
        for api in api_rows:
            self.store.add(api_path, API_COLUMNS, [api[c] for c in API_COLUMNS], encoding="utf-8")
        if api_rows:
            self.rows_today += 1
        self._flush()
        return api_path

    def _flush(self):
        """Write cached rows to CSV. Logs once when writing starts failing and
        once when it recovers, instead of on every retry."""
        self._last_flush = time.monotonic()
        try:
            written, pending, error = self.store.flush()
        except Exception as e:                       # cache itself unusable (very rare)
            written, pending, error = 0, -1, f"cache error: {e}"
        if error:
            if error != self._flush_error:
                self._warn(f"Cannot write {error} - is the file open in Excel? "
                           f"{pending} row(s) kept in cache, retrying every {FLUSH_RETRY_SECONDS}s")
            self._flush_error = error
        elif self._flush_error:
            self._info(f"CSV writable again - wrote {written} cached row(s)")
            self._flush_error = None
        self._emit("pending", max(pending, 0))

    def _flush_if_due(self):
        if self._flush_error and time.monotonic() - self._last_flush >= FLUSH_RETRY_SECONDS:
            self._flush()

    @staticmethod
    def _count_rows(path):
        if not path.exists():
            return 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return max(sum(1 for _ in f) - 1, 0)

    def _convert_torque(self, tool, row):
        """Scale torque columns (e.g. 5012 -> 5.012). Other columns unchanged."""
        if self.torque_scale == 1 or not self.torque_columns:
            return row
        out = []
        for name, value in zip(tool["header"], row):
            base = name.split("-", 1)[1] if "-" in name else name     # strip "T1-"
            if base in self.torque_columns and isinstance(value, int):
                value = f"{value * self.torque_scale:.{self.torque_decimals}f}"
            out.append(value)
        return out

    def _screw_and_attempt(self, report, sn):
        """Screw position and attempt number for this record.

        The controller has NG lock: after an NG the operator must reverse and
        re-tighten the SAME screw until OK; only an OK moves the controller's
        progress to the next screw. So the progress = the physical screw.

        Screw position (screws_per_board = N > 0, default 12): counted by this
                 app - 1 on every new SN, +1 after each OK, back to 1 after N.
                 (screws_per_board = 0: the controller's progress, report reg 1.)
        Attempt: n-th tightening try (OK or NG) on this screw in the current
                 board cycle. Loosening (status 3) is not a try; it shows the
                 count so far. Counts restart when a new SN is scanned OR when
                 the screw position goes back (e.g. 12 -> 1 = next board), so a
                 missed scan does not merge two boards."""
        if not report or len(report) <= RPT_STATUS:
            return "", ""
        status = report[RPT_STATUS]
        if sn != self._attempt_sn:                     # new SN -> new board, start at screw 1
            self._attempt_sn, self._attempt_counts, self._ok_count = sn, {}, 0
            self._last_position = None

        if self.screws_per_board > 0:
            # Counted here, independent of the controller: a missed screw or a
            # controller that is out of step cannot shift the next boards.
            position = self._ok_count % self.screws_per_board + 1
        else:
            position = (report[RPT_SCREW_H] << 16) + report[RPT_SCREW_L]
            if self._last_position is not None and position < self._last_position:
                self._attempt_counts = {}              # controller wrapped -> new cycle
            self._last_position = position

        count = self._attempt_counts.get(position, 0)
        if status in (1, 2):                           # 1 = OK, 2 = NG
            count += 1
            self._attempt_counts[position] = count
        if status == 1:                                # OK -> next screw (NG lock)
            self._ok_count += 1
            if self.screws_per_board > 0 and self._ok_count % self.screws_per_board == 0:
                self._attempt_counts = {}              # all N done -> next cycle starts clean
        return position, count

    def _api_row(self, tool, realtime, report, sn, screw, attempt, timestamp=""):
        """Build one MES API row (18 columns, all strings, never null)
        directly from the registers, independent of headers.txt."""
        def rpt(i):
            return report[i] if report and i < len(report) else None

        def signed(v):                       # Modbus is unsigned: 65064 -> -472
            return None if v is None else (v - 65536 if v > 32767 else v)

        def text(v):
            return "" if v is None else str(v)

        def torque(v):                       # 5012 -> 5.012
            return "" if v is None else f"{v * self.torque_scale:.{self.torque_decimals}f}"

        total_angle = signed(realtime[RT_TOTAL_ANGLE]) if realtime and len(realtime) > RT_TOTAL_ANGLE else None

        # Pass/Fail: angle inside the total running angle limits AND torque inside
        # the final-stage torque limits (raw integers, same scale)
        angle = signed(rpt(RPT_ROTATION_ANGLE))
        a_lo, a_hi = signed(rpt(RPT_MIN_TOTAL_ANGLE)), signed(rpt(RPT_MAX_TOTAL_ANGLE))
        t_val = rpt(RPT_FINAL_TORQUE)
        t_lo, t_hi = rpt(RPT_MIN_TORQUE_FINAL_STAGE), rpt(RPT_MAX_TORQUE_FINAL_STAGE)
        if None in (angle, a_lo, a_hi, t_val, t_lo, t_hi):
            pass_fail = ""                   # report missing
        else:
            pass_fail = "Pass" if (a_lo <= angle <= a_hi and t_lo <= t_val <= t_hi) else "Fail"
        tool_index = list(TOOL_REGISTERS).index(tool["name"])
        return {
            "SORT":               text(screw),                   # screw position on the board (1..12)
            "orgCode":            self.org_code,
            "SERIAL_NUMBER":      sn,
            "SCREWDRIVER_ID":     str(self.screwdriver_id + tool_index),
            "PROGRAM_NUMBER":     "",
            "PROGRAM_NAME":       "",
            "NUMBER_OF_TURNS":    "" if total_angle is None else f"{total_angle / 360:.2f}",
            "TORQUE":             torque(rpt(RPT_FINAL_TORQUE)),
            "TORQUE_LOW_LIMIT":   torque(rpt(RPT_MIN_TORQUE_FINAL_STAGE)),
            "TORQUE_HIGH_LIMIT":  torque(rpt(RPT_MAX_TORQUE_FINAL_STAGE)),
            "LOCKING_TIME":       text(rpt(RPT_OPERATION_TIME)),   # as-is (controller value)
            "S_RESULTS":          text(rpt(RPT_STATUS)),           # as-is: 1 OK, 2 NG, 3 loosening
            "DETAILED_INFO":      "",
            "S_FALG":             "",
            "S_ANGLE":            text(signed(rpt(RPT_ROTATION_ANGLE))),
            "S_ANGLE_LOW_LIMIT":  text(rpt(RPT_MIN_TOTAL_ANGLE)),
            "S_ANGLE_HIGH_LIMIT": text(rpt(RPT_MAX_TOTAL_ANGLE)),
            "S_GROUP":            "",
            "Pass/Fail":          pass_fail,
            "Error_Code":         text(rpt(RPT_ERROR_CODE)),
            "Attempt":            text(attempt),                 # n-th try on this screw for this SN
            "Record_Time":        timestamp,                     # PC time when the screw finished
        }

    def _sn_values(self):
        """[Scanned_SN, SN_Scan_Time, Screws_Since_Scan, SN_Check] for this record."""
        if self.scan_state is None:
            return ["", "", "", "NO_SN"]
        sn, when, count, scan_time = self.scan_state.take_for_record()
        if not sn:
            check = "NO_SN"
        elif self.sn_max_age and (datetime.now() - scan_time).total_seconds() > self.sn_max_age:
            check = "SN_OLD"
        else:
            check = ""
        return [sn, when, count, check]

    # ---- one screw ---------------------------------------------------------
    @staticmethod
    def _signed(v):
        return v - 65536 if v is not None and v > 32767 else v

    def _report_matches(self, realtime, report):
        """True if the report belongs to the run that just finished: its Rotation
        angle equals the realtime Total_Angle. None if it cannot be checked."""
        if not realtime or len(realtime) <= RT_TOTAL_ANGLE or not report:
            return None
        rt_angle = self._signed(realtime[RT_TOTAL_ANGLE])
        rp_angle = self._signed(report[RPT_ROTATION_ANGLE])
        return abs(rt_angle - rp_angle) <= ANGLE_MATCH_TOLERANCE

    def _read_fresh(self):
        """Read realtime registers + Report Info until they describe the run that
        just finished (see REPORT_FRESH_TRIES). Returns (realtimes, report, state):
            realtimes  list of realtime registers, one per configured tool
            state      "fresh"      new report, angle matches realtime
                       "unchecked"  new report, realtime unreadable (cannot compare)
                       "stale"      report changed but never matched realtime
                       "duplicate"  report never changed - no new result
                       "missing"    report could not be read at all"""
        realtimes, report, changed = [], None, False
        for attempt in range(1, REPORT_FRESH_TRIES + 1):
            realtimes = [self._read(*t["realtime"]) for t in self.tools]
            report = self._query(QUERY_REPORT, REG_REPORT)
            if report is None:
                return realtimes, None, "missing"
            changed = self._last_report is None or report != self._last_report
            match = self._report_matches(realtimes[0], report)
            if changed and match is not False:
                if attempt > 1:
                    self.log.info(f"Controller data up to date after {attempt} reads")
                return realtimes, report, ("fresh" if match else "unchecked")
            self._sleep(REPORT_FRESH_DELAY)
        if not changed:
            self._warn("No new result from the controller (same report as the last screw) "
                       "- not written to the API file")
            return realtimes, report, "duplicate"
        self._warn(f"Report did not match the realtime angle after {REPORT_FRESH_TRIES} reads "
                   f"- row flagged REPORT_STALE")
        return realtimes, report, "stale"

    def _record_run(self):
        """Called after the driver stopped: read everything and append one row."""
        self._sleep(SETTLE_AFTER_RUN)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        barcodes = [self._read(*t["barcode"]) for t in self.tools]
        realtimes, report, state = self._read_fresh()
        duplicate = state == "duplicate"

        if duplicate:                            # no new result: raw only, flagged
            sn_values = list(self.scan_state.peek()) if self.scan_state else ["", "", "", ""]
        else:
            sn_values = self._sn_values()        # [SN, scan time, n-th record, check]
            if report is not None:
                self._last_report = report
        flag = {"duplicate": "DUPLICATE_SKIPPED", "stale": "REPORT_STALE"}.get(state)
        if flag:
            sn_values[3] = ";".join(filter(None, [str(sn_values[3]), flag]))

        # Negative rotation angle = loosening / reverse: not a tightening result,
        # so it is left out of the API file (still in the _raw file, flagged).
        negative = (not duplicate and report is not None and len(report) > RPT_ROTATION_ANGLE
                    and self._signed(report[RPT_ROTATION_ANGLE]) < 0)
        if negative:
            sn_values[3] = ";".join(filter(None, [str(sn_values[3]), "NEGATIVE_ANGLE_SKIPPED"]))
        skip_api = duplicate or negative

        raw_values, api_rows = [], []
        if not skip_api:
            screw, attempt = self._screw_and_attempt(report, sn_values[0])
        for tool, realtime, barcode in zip(self.tools, realtimes, barcodes):
            row = retrieve_register_data(tool["header"], realtime,
                                         registers_to_string(barcode) if barcode else "",
                                         self.tool_info, report)
            raw_values += self._convert_torque(tool, row)
            if not skip_api:
                api_rows.append(self._api_row(tool, realtime, report, sn_values[0], screw, attempt,
                                              timestamp))
        path = self._write_rows([timestamp] + sn_values + raw_values, api_rows)
        if skip_api:
            if negative:
                self.log.info("Negative rotation angle (loosening) - not written to the API file")
            return

        note = "" if report else " (report missing)"
        note += f" SN={sn_values[0] or '-'}"
        state = "cached, CSV locked" if self._flush_error else path.name
        self._info(f"Saved row {self.rows_today} ({state}){note}")
        self._emit("row", {"time": timestamp, "file": path.name, "today": self.rows_today})

    # ---- main loop ---------------------------------------------------------
    def run(self):
        """Runs until stop(). Any unexpected error is logged and the loop
        restarts after a short pause - collection never ends on its own."""
        self._info(f"Saving data to {self.output_dir}")
        leftover = self.store.count()
        if leftover:
            self._info(f"{leftover} row(s) left in cache from last run - writing them now")
        self._flush()
        try:
            while not self.stopped():
                try:
                    self.output_dir.mkdir(parents=True, exist_ok=True)
                    if not self._connect_forever():
                        break
                    info = self._query(QUERY_TOOL_INFO, REG_TOOL_INFO)
                    self.tool_info = registers_to_string(info) if info else ""
                    self._info(f"Tool info: {self.tool_info or '(not available)'}")
                    # Baseline: the report already in the controller belongs to a
                    # screw finished BEFORE we connected - never record it as new.
                    self._last_report = self._query(QUERY_REPORT, REG_REPORT)
                    self._emit("status", "Ready - waiting for driver")
                    self._poll_until_disconnected()
                except Exception as e:
                    self.log.exception("Unexpected error in collector loop")
                    self._emit("log", f"ERROR: {e} - restarting in 5s (details in atx_logger.log)")
                    self._sleep(5)
                finally:
                    self.client.close()
        finally:
            self._flush()                    # last attempt; leftovers are kept for next start
            self._emit("status", "Stopped")
            self._info("Collector stopped")

    def _poll_until_disconnected(self):
        """v4 main loop: wait for start, wait for stop, record. Returns when the
        connection is lost (caller reconnects) or stop() is called."""
        failures = 0
        while not self.stopped():
            self._sleep(POLL_INTERVAL)
            self._flush_if_due()
            state = self._running()
            if state is None:
                failures += 1
                if failures >= 3:
                    self._warn("Lost connection to controller")
                    return
                continue
            failures = 0

            if any(v > 0 for v in state):              # a driver started
                self._emit("status", "Driver running")
                while not self.stopped():             # wait until both drivers stop
                    self._sleep(POLL_INTERVAL)
                    state = self._running()
                    if state is None:
                        failures += 1
                        if failures >= 3:
                            self._warn("Lost connection while driver was running")
                            return
                        continue
                    failures = 0
                    if all(v == 0 for v in state):
                        break
                if self.stopped():
                    return
                try:
                    self._record_run()
                except Exception as e:           # one bad record must not stop collection
                    self.log.exception("Failed to record run")
                    self._emit("log", f"ERROR: could not record this run ({e}) - continuing")
                self._emit("status", "Ready - waiting for driver")


# =============================================================================
# Window (tkinter)
# =============================================================================

def run_gui():
    import tkinter as tk
    from tkinter import messagebox, scrolledtext

    logger = setup_logging()
    try:
        cfg = load_config()
    except Exception as e:
        messagebox.showerror("ATX Logger", f"Cannot read config.ini:\n{e}")
        return

    events = queue.Queue()
    state = {"collector": None, "thread": None}
    store = PendingStore(CACHE_PATH)
    scan_state = ScanState((APP_DIR / cfg["output_dir"]).resolve(), events, logger, store,
                           write_scans=cfg.get("write_scans", "false").strip().lower() in ("1", "true", "yes"))
    listener = start_scanner_listener(cfg, scan_state, logger)

    root = tk.Tk()
    root.title("ATX Logger")
    root.geometry("700x480")

    top = tk.Frame(root, padx=10, pady=8)
    top.pack(fill="x")
    tk.Label(top, text=f"Controller: {cfg['host']}:{cfg['port']}").grid(row=0, column=0, sticky="w")
    tk.Label(top, text=f"Folder: {APP_DIR}").grid(row=1, column=0, columnspan=3, sticky="w")

    status_var = tk.StringVar(value="Starting ...")
    rows_var = tk.StringVar(value="Rows saved today: 0")
    last_var = tk.StringVar(value="Last record: -")
    sn_var = tk.StringVar(value="Current SN: (waiting for scan)" if listener
                          else "Current SN: scanner listener OFF - see log")
    tk.Label(top, textvariable=status_var, font=("Arial", 12, "bold")).grid(row=2, column=0, sticky="w", pady=(6, 0))
    tk.Label(top, textvariable=rows_var).grid(row=3, column=0, sticky="w")
    tk.Label(top, textvariable=last_var).grid(row=4, column=0, sticky="w")
    tk.Label(top, textvariable=sn_var, font=("Arial", 11)).grid(row=5, column=0, sticky="w")
    pending_var = tk.StringVar(value="")
    tk.Label(top, textvariable=pending_var, fg="#b00000").grid(row=6, column=0, sticky="w")

    buttons = tk.Frame(top)
    buttons.grid(row=2, column=2, rowspan=4, sticky="e", padx=(40, 0))
    start_btn = tk.Button(buttons, text="Start", width=10)
    stop_btn = tk.Button(buttons, text="Stop", width=10)
    start_btn.pack(pady=2)
    stop_btn.pack(pady=2)

    log_box = scrolledtext.ScrolledText(root, height=14, state="disabled")
    log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def append_log(text):
        log_box.configure(state="normal")
        log_box.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        log_box.see("end")
        if int(log_box.index("end-1c").split(".")[0]) > 1000:   # keep the box small
            log_box.delete("1.0", "200.0")
        log_box.configure(state="disabled")

    def start():
        if state["thread"] and state["thread"].is_alive():
            return
        try:
            collector = AtxCollector(cfg, events, logger, scan_state, store)
        except FileNotFoundError as e:
            messagebox.showerror("ATX Logger", f"Missing file next to the app:\n{e.filename}")
            return
        except ValueError as e:
            messagebox.showerror("ATX Logger", f"Invalid setting in config.ini:\n{e}")
            return
        state["collector"] = collector
        state["thread"] = threading.Thread(target=collector.run, daemon=True)
        state["thread"].start()
        start_btn.configure(state="disabled")
        stop_btn.configure(state="normal")

    def stop():
        if state["collector"]:
            state["collector"].stop()
        start_btn.configure(state="normal")
        stop_btn.configure(state="disabled")

    def poll_events():
        while True:
            try:
                kind, value = events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                status_var.set(value)
            elif kind == "row":
                rows_var.set(f"Rows saved today: {value['today']}  ({value['file']})")
                last_var.set(f"Last record: {value['time']}")
            elif kind == "scan":
                sn_var.set(f"Current SN: {value['sn']}   (scanned {value['time'][11:]})")
                append_log(f"Scanned SN: {value['sn']}")
            elif kind == "pending":
                pending_var.set(f"{value} row(s) waiting in cache - close the CSV in Excel"
                                if value else "")
            elif kind == "log":
                append_log(value)
        root.after(200, poll_events)

    def on_close():
        running = state["thread"] and state["thread"].is_alive()
        if running and not messagebox.askyesno(
                "ATX Logger", "Data collection is running.\nStop collecting and close?"):
            return
        stop()
        root.destroy()

    start_btn.configure(command=start)
    stop_btn.configure(command=stop)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(200, poll_events)
    if listener is None:
        root.after(250, lambda: append_log("WARNING: scanner listener is not running - SN will be empty"))
    root.after(300, start)            # start collecting automatically on launch
    root.mainloop()


def run_console():
    """Run without a window (useful for testing): python atx_logger_app.py --console"""
    logger = setup_logging()
    events = queue.Queue()
    cfg = load_config()
    store = PendingStore(CACHE_PATH)
    scan_state = ScanState((APP_DIR / cfg["output_dir"]).resolve(), events, logger, store,
                           write_scans=cfg.get("write_scans", "false").strip().lower() in ("1", "true", "yes"))
    start_scanner_listener(cfg, scan_state, logger)
    collector = AtxCollector(cfg, events, logger, scan_state, store)
    thread = threading.Thread(target=collector.run, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            try:
                kind, value = events.get(timeout=0.5)
                print(f"[{kind}] {value}")
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        collector.stop()
        thread.join(timeout=5)


if __name__ == "__main__":
    if "--console" in sys.argv:
        run_console()
    else:
        run_gui()
