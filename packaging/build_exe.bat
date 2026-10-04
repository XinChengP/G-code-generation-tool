@echo off
setlocal
REM ====== Build standalone exe: dist\CNCGcodeTool.exe ======
REM The exe bundles core\dxf2gcode.py / core\preview_gcode.py /
REM _filter_dense.py plus all dependencies (ezdxf, matplotlib, Pillow).
REM Users do NOT need Python installed. Re-run this script to rebuild.

set "PY=C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe"
if not exist "%PY%" set "PY=python"

cd /d "%~dp0.."

"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name CNCGcodeTool ^
    --paths "core;." ^
    --specpath packaging ^
    ui\app.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See messages above.
    exit /b 1
)

echo.
echo [OK] Build finished: dist\CNCGcodeTool.exe
