ATX Logger
==========
Runs the vendor's ATX data collection (atx-pymodbus-client-v4.py logic)
continuously and saves the raw data to one CSV file per day.
Output files (one pair per day):
    YYYY-MM-DD_api.csv  MAIN OUTPUT - MES screw-data API format (18 columns)
    YYYY-MM-DD_raw.csv  Date-Time, 4 SN columns (Scanned_SN, SN_Scan_Time,
                        Screws_Since_Scan, SN_Check), then the T1 columns selected
                        in headers1.txt. Same row order as the _api file.
                        Turn off with write_raw = false.

API columns:
    SORT               screw position 1..12, counted by the app: 1 on every new SN,
                       next screw after each OK (NG lock), back to 1 after
                       screws_per_board (config.ini, default 12).
                       screws_per_board = 0 uses the controller's own progress.
    orgCode            org_code in config.ini (DEMO_ORG)
    SERIAL_NUMBER      scanned SN
    SCREWDRIVER_ID     screwdriver_id in config.ini (1)
    NUMBER_OF_TURNS    Total_Angle / 360
    TORQUE, TORQUE_LOW_LIMIT, TORQUE_HIGH_LIMIT
                       Final_Torque, Min/Max_Torque_Final_Stage, divided by 1000
    LOCKING_TIME       Operation_Time as-is
    S_RESULTS          Status as-is (1 OK, 2 NG)
    S_ANGLE            Rotation_Angle as-is. Records with a NEGATIVE angle
                       (loosening / reverse) are left out of the _api file;
                       the _raw file keeps them (SN_Check = NEGATIVE_ANGLE_SKIPPED).
    S_ANGLE_LOW_LIMIT / S_ANGLE_HIGH_LIMIT   Min/Max_Total_Angle as-is
    PROGRAM_NUMBER, PROGRAM_NAME, DETAILED_INFO, S_FALG, S_GROUP: empty
  Extra columns (not in the API doc):
    Pass/Fail          Pass if Min total running angle <= Rotation angle <= Max total
                       running angle AND Min. torque of final stage <= Final torque
                       <= Max. torque of final stage
    Error_Code         controller error code as-is
    Record_Time        time the screw finished (host PC clock), same as Date-Time
                       in the _raw file
    Attempt            n-th tightening try (OK/NG) on this screw in the current
                       board cycle; restarts on a new SN or when the screw
                       position goes back (e.g. 12 -> 1). Loosening rows repeat
                       the count so far. Retries = Attempt - 1.
                       Relies on the controller's NG lock (an NG screw must be
                       reversed and re-tightened before the next screw).
Stale data protection: right after a screw finishes, the controller can still
hold the PREVIOUS screw's data (realtime registers and/or report). A run is
accepted only when the report differs from the last recorded one AND its angle
matches the realtime Total_Angle; both are re-read for up to ~4 s. At every
(re)connect the current report is taken as the baseline.
  - report never changes -> no new result: not written to the _api file,
    _raw file row flagged DUPLICATE_SKIPPED
  - report changes but angle never matches -> written, flagged REPORT_STALE
If today's file already exists with different columns (after an update or a
headers.txt change), new rows go to YYYY-MM-DD_api_2.csv instead of being
mixed under the old header.
The API columns are read straight from the controller registers, so editing
headers1.txt only changes the _raw file.

Tool ports and torque units (config.ini, defaults shown):
    tools = T1             only tool port 1 (one driver). T1,T2 for two drivers.
    torque_scale = 0.001   torque columns are divided by 1000 (5012 -> 5.012)
    torque_decimals = 3
    torque_columns = ...   which columns are torque values (names without T1-).
                           Torque_Unit, Torque_UOM_ID and Target_Torque_Rate are
                           codes / rates and are NOT converted.
An existing config.ini without these lines automatically uses the defaults.

How the SN is linked
--------------------
The barcode scanner (keyboard mode) sends the SN to the MES client on this PC.
ATX Logger listens to the keyboard in the background without blocking it - the
MES client still receives every scan as before. Only input that looks like a
scan (very fast characters + Enter) is kept; normal typing is ignored and never
stored. The last scanned SN is attached to every screw record until the next
scan (linking by order, not by time). Products that do not use the smart tool
produce no screw records, so their SN is simply replaced by the next scan.
Optional: write_scans = true also writes every detected scan to
scans_YYYY-MM-DD.csv (off by default).

SN_Check column:  NO_SN  = no scan since the app started
                  SN_OLD = last scan older than sn_max_age_min (config.ini)

IMPORTANT
- A keyboard listener can be flagged by antivirus / security policy.
  Get IT approval before installing on the production PC.
- If the MES client runs "as administrator", ATX Logger must also run as
  administrator, otherwise Windows hides the keystrokes from it.
- First run on site: scan a board, check that "Current SN" in the window shows
  it AND that the MES client still received it normally.


1. Build the .exe (once, on a Windows PC with Python 3.10+)
-----------------------------------------------------------
Put these files in one folder:
    atx_logger_app.py   build_exe.bat   headers1.txt   headers2.txt
Double-click build_exe.bat. When it finishes, the "dist" folder contains:
    ATX_Logger.exe   headers1.txt   headers2.txt


2. Install on the production PC (no Python needed)
--------------------------------------------------
Copy the three files from "dist" into one folder, e.g. C:\ATX_Logger\
The PC must be able to reach the controller (default 192.168.0.10:502).

Double-click ATX_Logger.exe:
    - It starts collecting automatically.
    - The window shows: connection status, rows saved today, last record time.
    - Closing the window asks for confirmation, then stops collecting.

Files created next to the .exe:
    config.ini        controller IP / port, scanner settings - edit, then restart
    YYYY-MM-DD_api.csv / _raw.csv   data, a new pair every day
    pending_cache.db  rows waiting to be written (do not delete while rows wait)

Opening the CSV in Excel while the app runs:
    Excel locks the file, so new rows cannot be written to it. They are kept in
    pending_cache.db and the window shows "N row(s) waiting in cache". When you
    close the file in Excel, the rows are written automatically within ~5 s, in
    the original order. Nothing is lost, even if the app is closed meanwhile:
    leftover rows are written at the next start.
    Tip: to look at today's data without blocking it, copy the CSV and open the copy.
    atx_logger.log    log for troubleshooting

Optional - start automatically when Windows starts:
    Win+R -> shell:startup -> put a shortcut to ATX_Logger.exe in that folder.


Scanner settings in config.ini:
    scanner_enabled      true / false
    scanner_max_gap_ms   50   max ms between characters of one scan
                              (raise to 80-100 if scans are missed)
    scanner_min_length   6    shorter input is ignored
    scanner_pattern      optional regex the SN must match, e.g. ^MBB[A-Z0-9]{12}$
    sn_max_age_min       30   flag SN_OLD after this many minutes (0 = off)


3. Connection problems - run the probe (on a PC with Python)
-------------------------------------------------------------
If the log shows "No Response received from the remote slave", the TCP port
is open but the controller does not answer Modbus with the current settings.
    pip install pymodbus==3.6.9
    python atx_probe.py <controller IP>
It tries ports 502 / 1001, unit id 1 / 0 / 255 and both framings, and prints
the settings to put in config.ini (port, unit_id, framer).


4. Test without the controller (on any PC with Python)
------------------------------------------------------
    pip install pymodbus==3.6.9
    python fake_atx_controller.py --csv sample_data/demo_controller_export.csv --port 5020
    (--update-delay 0.8 --report-delay 0.3 imitate the controller's late update)
Then set in config.ini:
    host = 127.0.0.1
    port = 5020
and start ATX_Logger.exe (or: python atx_logger_app.py).
Remember to set host/port back to the real controller afterwards.


Differences from the vendor v4 script (robustness only)
-------------------------------------------------------
1. Reconnects automatically when the controller is offline or the link drops
   (v4 crashed on the first connection timeout).
2. A failed Report Info / Tool Info query leaves those fields empty
   (v4 crashed, or re-used the previous screw's report without warning).
3. New CSV file every day (v4 kept writing to the start-up date's file).
4. Null padding removed from barcode / tool info text.
5. Query commands go through the same Modbus client (function code 16 at
   register 200) instead of opening a second raw socket - same request.
6. Files are read/written next to the .exe, not the current directory.

Not verified yet against the real controller - first run on site should be
checked: rows appear in the CSV, values look reasonable, log has no errors.
