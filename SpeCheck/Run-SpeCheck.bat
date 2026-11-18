@echo off
cd /d "%~dp0"
py -3 specheck.py
if errorlevel 1 (
  echo.
  echo SpeCheck could not start. Install Python 3 for Windows and enable Add python.exe to PATH.
  pause
)
