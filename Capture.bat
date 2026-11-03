@echo off
rem Drag one or more PDFs or folders onto this file, or double-click it and paste a path.
rem Results go to an "ocr_output" folder next to the input. Re-running continues where it stopped.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup is needed. Running install.ps1 ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
  if errorlevel 1 goto :failed
)
if "%~1"=="" goto :ask
".venv\Scripts\python.exe" -m doc_capture_harness batch %*
goto :done
:ask
set "TARGET="
set /p "TARGET=Drag a PDF or folder into this window, then press Enter: "
if not defined TARGET goto :ask
".venv\Scripts\python.exe" -m doc_capture_harness batch %TARGET%
goto :done
:failed
echo Setup failed - see the messages above.
:done
echo.
echo Finished. Press any key to close this window.
pause >nul
