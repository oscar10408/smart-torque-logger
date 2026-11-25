@echo off
REM ==========================================================================
REM  Build ATX_Logger.exe  (run this on a Windows PC that has Python installed)
REM  Result: dist\ATX_Logger.exe  - copy it together with headers1.txt and
REM          headers2.txt to the production PC (no Python needed there).
REM ==========================================================================

cd /d "%~dp0"

echo [1/3] Creating build environment...
python -m venv .build_env || goto :error
call .build_env\Scripts\activate.bat

echo [2/3] Installing packages...
python -m pip install --upgrade pip >nul
python -m pip install "pymodbus==3.6.9" pynput pyinstaller || goto :error

echo [3/3] Building ATX_Logger.exe...
pyinstaller --onefile --windowed --noconfirm --name ATX_Logger ^
    --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
    atx_logger_app.py || goto :error

copy /Y headers1.txt dist\ >nul
copy /Y headers2.txt dist\ >nul

echo.
echo Done. Copy the whole "dist" folder to the production PC:
echo   dist\ATX_Logger.exe
echo   dist\headers1.txt
echo   dist\headers2.txt
call .build_env\Scripts\deactivate.bat
pause
exit /b 0

:error
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1
