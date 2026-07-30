@echo off
rem Update-Standards.bat — George double-clicks this after adding/removing
rem documents in C:\Standards\raw. Detects changes, extracts/OCRs new docs,
rem runs the Claude cleanup, removes deleted docs from the index.
chcp 65001 >nul
cd /d C:\Standards
echo ============================================
echo  Обновление базы стандартов...
echo  (это может занять несколько минут)
echo ============================================
set STANDARDS_ROOT=C:\Standards
python pipeline\update.py
echo.
echo ============================================
echo  Готово. Отчёт: C:\Standards\state\inventory_report.md
echo ============================================
pause
