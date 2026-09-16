@echo off
setlocal

REM ====== G-code Previewer ======

REM Force Python 3.10 (has Pillow installed)
set "PYTHON=C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe"

cd /d "%~dp0"

if not exist "%PYTHON%" (
    echo [ERROR] Python not found: %PYTHON%
    pause
    exit /b
)

if "%~1"=="" (
    echo ========================================
    echo  G-code Previewer
    echo ========================================
    echo  Usage: Drag a .txt file onto this .bat
    echo  Outputs 2 images:
    echo    * _full.png     - full path with rapid moves
    echo    * _cutting.png  - cutting paths only
    echo ========================================
    echo.
    pause
    exit /b
) else (
    set "NC_FILE=%~1"
)

if not exist "%NC_FILE%" (
    echo [ERROR] File not found: %NC_FILE%
    pause
    exit /b
)

echo.
echo ========================================
echo  G-code Previewer
echo ========================================
echo Python: %PYTHON%
echo File  : %NC_FILE%
echo.

"%PYTHON%" "%~dp0preview\preview_gcode.py" "%NC_FILE%"

pause
