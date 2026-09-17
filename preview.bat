@echo off
setlocal

REM ====== G-code Previewer ======
REM Draws two preview images from a .txt (or .nc) G-code file
REM Output goes to preview\img\ subfolder automatically

set "PYTHON=C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe"
set "ROOT=%~dp0"
set "CORE=%ROOT%core"
set "IMG=%ROOT%preview"

if not exist "%IMG%" mkdir "%IMG%"
cd /d "%ROOT%"

if not exist "%PYTHON%" (
    echo [ERROR] Python not found: %PYTHON%
    pause
    exit /b
)
if not exist "%CORE%\preview_gcode.py" (
    echo [ERROR] Script missing: core\preview_gcode.py
    pause
    exit /b
)

if "%~1"=="" (
    echo ========================================
    echo  G-code Previewer
    echo ========================================
    echo  Drag your G-code file onto this .bat
    echo  Output: preview\img\
    echo    * _full.png    - full path with rapid moves
    echo    * _cutting.png - cutting paths only
    echo ========================================
    echo.
    pause
    exit /b
) else (
    set "GCODE_FILE=%~1"
)

if not exist "%GCODE_FILE%" (
    echo [ERROR] File not found: %GCODE_FILE%
    pause
    exit /b
)

echo.
echo ========================================
echo  G-code Previewer
echo ========================================
echo File  : %GCODE_FILE%
echo Output: preview\img\
echo.

"%PYTHON%" "%CORE%\preview_gcode.py" "%GCODE_FILE%" --img-dir "%IMG%" --no-open

if errorlevel 1 (
    echo.
    echo [ERROR] Preview failed
    pause
    exit /b
)

echo.
echo ========================================
echo  Done -^> preview\img\
echo ========================================
pause
