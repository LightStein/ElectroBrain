@echo off
rem Update-Standards.bat - George double-clicks this after adding or removing
rem documents. Detects changes, extracts new docs, rebuilds the index, and
rem removes deleted ones.
rem
rem THIS FILE IS PURE ASCII ON PURPOSE. cmd.exe parses .bat using the console
rem codepage, so Cyrillic here renders as mojibake no matter what chcp says.
rem All Russian user-facing text lives in pipeline\update.py, because Python 3
rem writes Unicode to the Windows console through the console API directly and
rem is not affected by the codepage.
chcp 65001 >nul
cd /d C:\Standards
set STANDARDS_ROOT=C:\Standards
rem George keeps his documents here; the pipeline reads them in place
rem rather than duplicating them into C:\Standards\raw.
set STANDARDS_RAW=D:\LLM_FILES

rem "python" resolves to the real interpreter today, but the Microsoft Store
rem stub sits right behind it on PATH and is NOT an interpreter - if the order
rem ever flips, this opens the Store instead of updating anything. The py
rem launcher never resolves to the stub, so prefer it when present.
set PY=python
where py >nul 2>&1 && set PY=py -3

%PY% pipeline\update.py
if errorlevel 1 (
  echo.
  echo ============================================
  echo  ERROR - send this window to Anri.
  echo ============================================
)
echo.
pause
