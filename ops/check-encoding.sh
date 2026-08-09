#!/usr/bin/env bash
# Guards against the two transport bugs that broke setup-access.ps1 in the
# field. Run from the repo root:  ops/check-encoding.sh
#
# RULE 1 (all .ps1/.bat): pure ASCII.
#   Windows PowerShell 5.1 decodes .ps1 with the system ANSI codepage (CP1251
#   on a Russian Windows) unless the file carries a UTF-8 BOM - and Windows 11
#   Notepad saves UTF-8 WITHOUT one. Byte 0x94 sits inside the UTF-8 encoding
#   of an em-dash (E2 80 94) and of the box-drawing dash (E2 94 80); under
#   CP1251 it decodes to a right double quotation mark, which PowerShell
#   accepts as a STRING DELIMITER. One em-dash in a comment is enough to make
#   the whole script fail to parse. cmd.exe has the same problem with .bat and
#   no BOM escape hatch. Russian user-facing strings therefore belong in
#   bot.js / ask.py / update.py, which Node and Python read as UTF-8 always.
#
# RULE 2 (hand-pasted files only): every line under 72 characters.
#   A terminal that soft-wraps at 73 columns turns those wraps into REAL
#   newlines when the text is copied, splitting long lines mid-word. That
#   silently corrupted both the tunnel token (201 chars) and the SSH public
#   key (125 chars): the script still reported success, but authentication
#   could never work. Only files that travel by copy-paste need this; anything
#   delivered by git clone arrives byte-exact.
#
# RULE 3: the public key in the setup script is assembled from short string
#   fragments to satisfy rule 2, so verify it still concatenates to the real
#   key. A typo there installs a key that silently never authenticates.

set -uo pipefail
cd "$(dirname "$0")/.."

MAXLEN=72
PASTED="./ops/laptop-setup/setup-access.ps1"   # the only hand-transported file
REAL_KEY="$HOME/.ssh/george_laptop.pub"
fail=0

# --- Rule 1: ASCII everywhere -----------------------------------------------
while IFS= read -r f; do
    [ -f "$f" ] || continue
    n=$(grep -oP '[^\x00-\x7F]' "$f" 2>/dev/null | wc -l)
    if [ "$n" -ne 0 ]; then
        printf '  FAIL  %-42s %s non-ASCII char(s)\n' "$f" "$n"
        grep -n -P '[^\x00-\x7F]' "$f" | sed 's/^/          /'
        fail=1
    else
        printf '  ok    %-42s pure ASCII\n' "$f"
    fi
done < <(find . -type f \( -name '*.ps1' -o -name '*.bat' \) \
                -not -path './node_modules/*' | sort)

# --- Rule 2: line length, hand-pasted file only -----------------------------
if [ -f "$PASTED" ]; then
    long=$(awk -v m="$MAXLEN" 'length($0)>m{c++} END{print c+0}' "$PASTED")
    if [ "$long" -ne 0 ]; then
        printf '  FAIL  %-42s %s line(s) over %s cols\n' \
               "$(basename "$PASTED")" "$long" "$MAXLEN"
        awk -v m="$MAXLEN" 'length($0)>m{printf "          line %d: %d chars\n", NR, length($0)}' "$PASTED"
        fail=1
    else
        printf '  ok    %-42s all lines <=%s cols\n' \
               "$(basename "$PASTED")" "$MAXLEN"
    fi
fi

# --- Rule 3: the split public key still reassembles --------------------------
if [ -f "$REAL_KEY" ] && [ -f "$PASTED" ]; then
    if python3 - "$PASTED" "$REAL_KEY" <<'PY'
import re, sys
src = open(sys.argv[1]).read()
want = " ".join(open(sys.argv[2]).read().split()[:2])
m = re.search(r'^\$PublicKey = (.+?)(?=\n\n)', src, re.S | re.M)
if not m:
    print("          could not locate the $PublicKey assignment"); sys.exit(1)
# pair the quotes properly - a naive '"\K[^"]*' also captures the text
# AFTER each closing quote (that bug reported a bogus mismatch once)
got = " ".join("".join(re.findall(r'"([^"]*)"', m.group(1))).split()[:2])
if got != want:
    print(f"          want: {want}"); print(f"          got : {got}"); sys.exit(1)
PY
    then
        printf '  ok    %-42s reassembles to the real key\n' '$PublicKey'
    else
        printf '  FAIL  %-42s key mismatch\n' '$PublicKey'
        fail=1
    fi
fi

if [ "$fail" -ne 0 ]; then
    echo
    echo "Fix: em-dash -> '-', box-drawing -> '-', ellipsis -> '...';"
    echo "move Cyrillic into a .js/.py file; split lines over $MAXLEN cols."
    exit 1
fi
echo "All checks passed."
