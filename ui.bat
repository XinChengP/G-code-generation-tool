@echo off
setlocal

REM ====== CNC G-code Tool - UI launcher ======
REM Double-click to open the graphical interface.
REM The UI calls core\dxf2gcode.py / core\preview_gcode.py
REM / _filter_dense.py as-is (no core code modified).

set "ROOT=%~dp0"
set "PY=C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe"

REM Fallback to PATH python if the hardcoded one is missing
if not exist "%PY%" set "PY=python"

if not exist "%ROOT%core\dxf2gcode.py" (
    echo [ERROR] Missing: core\dxf2gcode.py
    echo         Run this .bat from the project root folder.
    pause
    exit /b 1
)

echo Starting CNC G-code UI ...
"%PY%" "%ROOT%ui\app.py"

if errorlevel 1 (
    echo.
    echo [ERROR] UI exited with an error. See message above.
    pause
)
