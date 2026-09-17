@echo off
setlocal enabledelayedexpansion

REM ====== Batch Converter: process ALL CAD files in input\ ======
REM Supported: .dxf  .dwf  .dwfx
REM Each file -> gcode\<name>.txt + preview\<name>_full.png + _cutting.png
REM Program number auto-increments: O0001, O0002, O0003 ...

set "PYTHON=C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe"
set "ROOT=%~dp0"
set "CORE=%ROOT%core"
set "INPUT=%ROOT%input"
set "GCODE=%ROOT%gcode"
set "IMG=%ROOT%preview"

if not exist "%GCODE%"  mkdir "%GCODE%"
if not exist "%IMG%"    mkdir "%IMG%"
if not exist "%INPUT%"  mkdir "%INPUT%"

cd /d "%ROOT%"

if not exist "%PYTHON%" (
    echo [ERROR] Python not found: %PYTHON%
    pause
    exit /b
)
if not exist "%CORE%\dxf2gcode.py" (
    echo [ERROR] Script missing: core\dxf2gcode.py
    pause
    exit /b
)

set "COUNT=0"
set "OK=0"
set "FAIL=0"
set "PROG=0"

echo.
echo ========================================
echo  BATCH Converter
echo  Scan input\ for DXF / DWF / DWFx
echo ========================================
echo.

for %%F in ("%INPUT%\*.dxf" "%INPUT%\*.dwf" "%INPUT%\*.dwfx") do (
    if exist "%%F" (
        set /a COUNT+=1
        REM Reserve a program number BEFORE converting. If conversion fails the
        REM number is given back below, so successful files always end up with
        REM consecutive numbers: O0001, O0002, O0003 ...
        set /a PROG+=1
        echo ----------------------------------------
        echo [#!COUNT!] %%~nxF
        echo ----------------------------------------

        "%PYTHON%" "%CORE%\dxf2gcode.py" -i "%%F" -o "%GCODE%\%%~nF.txt" -n !PROG!
        if errorlevel 1 (
            echo [FAIL] %%~nxF
            set /a FAIL+=1
            set /a PROG-=1
        ) else (
            echo.
            "%PYTHON%" "%CORE%\preview_gcode.py" "%GCODE%\%%~nF.txt" --img-dir "%IMG%" --no-open
            if errorlevel 1 (
                echo [WARN] preview failed for %%~nxF
            )
            REM Pad program number to 4 digits for display: 1 -> 0001, 12 -> 0012
            set "PNUM=000!PROG!"
            set "PNUM=!PNUM:~-4!"
            echo [OK] %%~nxF -^> gcode\%%~nF.txt  [O!PNUM!]
            set /a OK+=1
        )
        echo.
    )
)

if %COUNT%==0 (
    echo [INFO] No CAD files found in input\
    echo        Put .dxf / .dwf / .dwfx there first
)

echo ========================================
echo  Done: %OK% OK, %FAIL% failed, %COUNT% total
echo ========================================
pause
