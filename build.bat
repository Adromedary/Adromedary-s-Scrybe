@echo off
title Building Adromedary's Scrybe 1.0
cd /d "%~dp0"

rem ---- find Python (python, then the py launcher) ----
set PY=
python --version >nul 2>&1 && set PY=python
if not defined PY (py --version >nul 2>&1 && set PY=py)
if not defined PY (
  echo.
  echo  Python isn't installed, or isn't on PATH.
  echo  Install it from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" on the first screen, then run this again.
  echo.
  pause
  exit /b 1
)

echo Using: & %PY% --version
echo.
echo Installing the builder (PyInstaller)...
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 goto fail

echo.
echo Building Scrybe.exe ...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name Scrybe --icon scrybe.ico --version-file version_info.txt scrybe.py
if errorlevel 1 goto fail

echo.
echo  Done!  Your app is here:  %~dp0dist\Scrybe.exe
echo  You can copy that single file anywhere - nothing else is needed.
echo.
explorer "%~dp0dist"
pause
exit /b 0

:fail
echo.
echo  The build failed - scroll up for the error message.
pause
exit /b 1
