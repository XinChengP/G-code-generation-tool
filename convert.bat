@echo off
setlocal

REM ====== CAD to G-code Converter ======
REM Convert DXF / DWF / DWFx to G-code .txt files
REM Output goes to gcode\ subfolder automatically

set "PYTHON=C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe"
set "ROOT=%~dp0"
set "CORE=%ROOT%core"
set "GCODE=%ROOT%gcode"

if not exist "%GCODE%" mkdir "%GCODE%"
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

if "%~1"=="" (
    echo ========================================
    echo  CAD to G-code Converter
    echo ========================================
    echo  Drag your CAD file onto this .bat
    echo  Output: gcode\<filename>.txt
    echo ========================================
    echo.
    pause
    exit /b
) else (
    set "IN_FILE=%~1"
)

if not exist "%IN_FILE%" (
    echo [ERROR] File not found: %IN_FILE%
    pause
    exit /b
)

set "OUT_FILE=%GCODE%\%~n1.txt"

echo.
echo ========================================
echo  CAD to G-code Converter
echo ========================================
echo Input : %IN_FILE%
echo Output: gcode\%~n1.txt
echo.

"%PYTHON%" "%CORE%\dxf2gcode.py" -i "%IN_FILE%" -o "%OUT_FILE%"

if errorlevel 1 (
    echo.
    echo [ERROR] Conversion failed
    pause
    exit /b
)

REM ---- Auto-preview ----
set "IMG=%ROOT%preview"
if not exist "%IMG%" mkdir "%IMG%"

echo.
echo ----------------------------------------
echo  Auto-preview: drawing trajectory...
echo ----------------------------------------
"%PYTHON%" "%CORE%\preview_gcode.py" "%OUT_FILE%" --img-dir "%IMG%"

if errorlevel 1 (
    echo [WARN] Preview failed (G-code is still OK)
)

echo.
echo ========================================
echo  Done -^> gcode\%~n1.txt
echo          preview\
echo ========================================
pause
