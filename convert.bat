@echo off
setlocal

REM ====== DXF to G-code Converter ======

REM Force Python 3.10 (has ezdxf installed)
set "PYTHON=C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe"

cd /d "%~dp0"

REM Check Python exists
if not exist "%PYTHON%" (
    echo [ERROR] Python not found: %PYTHON%
    pause
    exit /b
)

REM If no argument, prompt to drag DXF onto this bat
if "%~1"=="" (
    echo ========================================
    echo  DXF to G-code Converter
    echo ========================================
    echo  Usage: Drag a .dxf file onto this .bat
    echo  Or   : Put .dxf in this folder, then run:
    echo         convert.bat filename.dxf
    echo ========================================
    echo.
    pause
    exit /b
) else (
    set "DXF_FILE=%~1"
)

if not exist "%DXF_FILE%" (
    echo [ERROR] File not found: %DXF_FILE%
    pause
    exit /b
)

echo.
echo ========================================
echo  DXF to G-code Converter
echo ========================================
echo Python: %PYTHON%
echo Input : %DXF_FILE%
echo.

REM Output: same name .txt
set "OUT_FILE=%~dpn1.txt"

"%PYTHON%" "%~dp0dxf2gcode.py" -i "%DXF_FILE%" -o "%OUT_FILE%"

if errorlevel 1 (
    echo.
    echo [ERROR] Conversion failed
    pause
    exit /b
)

echo.
echo ========================================
echo  Done! Saved to: %OUT_FILE%
echo ========================================
pause
