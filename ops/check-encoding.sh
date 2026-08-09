#!/usr/bin/env bash
# Guard against the codepage trap that broke setup-access.ps1 on first run.
#
# Windows PowerShell 5.1 decodes .ps1 files with the system ANSI codepage
# (CP1251 on a Russian Windows) unless the file carries a UTF-8 BOM - and
# Windows 11 Notepad saves UTF-8 WITHOUT a BOM. The byte 0x94 sits inside the
# UTF-8 encoding of an em-dash (E2 80 94) and of the box-drawing dash
# (E2 94 80); under CP1251 it decodes to a right double quotation mark, which
# PowerShell accepts as a STRING DELIMITER. A single em-dash in a comment is
# therefore enough to make the whole script fail to parse.
#
# cmd.exe has the same problem with .bat files and has no BOM escape hatch.
#
# Rule: .ps1 and .bat files stay pure ASCII. Russian user-facing strings belong
# in bot.js / ask.py / update.py, which Node and Python read as UTF-8
# unconditionally.
#
# Run from the repo root:  ops/check-encoding.sh

set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
while IFS= read -r f; do
    [ -f "$f" ] || continue
    n=$(grep -oP '[^\x00-\x7F]' "$f" 2>/dev/null | wc -l)
    if [ "$n" -ne 0 ]; then
        printf '  FAIL  %-46s %s non-ASCII char(s)\n' "$f" "$n"
        grep -n -P '[^\x00-\x7F]' "$f" | sed 's/^/          /'
        fail=1
    else
        printf '  ok    %-46s pure ASCII\n' "$f"
    fi
done < <(find . -type f \( -name '*.ps1' -o -name '*.bat' -o -name '*.ps1.example' \) \
                -not -path './node_modules/*' | sort)

if [ "$fail" -ne 0 ]; then
    echo
    echo "Replace em-dashes with '-', box-drawing with '-', ellipses with '...'."
    echo "Move any Cyrillic into a .js/.py file instead."
    exit 1
fi
echo "All Windows-parsed files are pure ASCII."
