@echo off
rem Update-Standards.bat - George double-clicks this after adding or removing
rem documents in C:\Standards\raw. Detects changes, extracts/OCRs new docs, runs
rem the Claude cleanup, and removes deleted docs from the index.
rem
rem THIS FILE IS PURE ASCII ON PURPOSE. cmd.exe parses .bat using the console
rem codepage, so Cyrillic here renders as mojibake no matter what chcp says.
rem All Russian user-facing text lives in pipeline\update.py, because Python 3
rem writes Unicode to the Windows console through the console API directly and
rem is not affected by the codepage.
chcp 65001 >nul
cd /d C:\Standards
set STANDARDS_ROOT=C:\Standards
python pipeline\update.py
echo.
pause
