#!/bin/bash
# set_tm_passphrase.sh — S73. Store the Time Machine volume's unlock passphrase
# and IMMEDIATELY prove whether it works.
#
# Buddy, 2026-08-22: "do I need to go down stairs and look at what Time Machine
# is asking or what is a passphrase?"
#
# Background: the OWC Envoy Pro FX is an encrypted APFS volume with exactly one
# cryptographic user (Type: Disk User, Hint: "Backup"). Normally macOS unlocks
# it from the LOGIN KEYCHAIN, which is why it only ever worked while somebody
# was logged in — and why converting CIRRUS to run with nobody logged in broke
# Time Machine silently.
#
# WHY THIS SCRIPT EXISTS rather than "just edit the file":
#   * `read -rs` means the passphrase is never echoed to the screen, never lands
#     in shell history, and never appears in `ps` (which is how a token leaked
#     earlier today — see docs/TOOLING-TRAPS.md T21).
#   * It writes with the right owner and mode in one step, so a half-done edit
#     cannot leave a world-readable secret.
#   * It VERIFIES. The previous attempt wrote a file that looked perfectly fine
#     — 23 bytes, root:wheel, 600 — and was simply the wrong value. Nobody found
#     out until a reboot 21 hours later. A write that is not verified is not a
#     fix, it is a hope.
#
# Run it ON CIRRUS (Terminal, or over Screen Sharing). Nothing is stored unless
# the passphrase actually unlocks the volume.
set -uo pipefail

PASSFILE="/etc/cirrus-tm-passphrase"
VOL="${1:-disk5s2}"

if [ "$(id -u)" != "0" ]; then
    echo "Run with sudo:  sudo bash $0 ${VOL}"
    exit 2
fi

echo "Time Machine volume: $VOL"
diskutil apfs listCryptoUsers "$VOL" 2>/dev/null | grep -E "Type:|Hint:" | sed 's/^/  /'
echo
echo "Enter the passphrase for that volume (it will NOT be shown)."
echo "Find it in Keychain Access -> search the volume name -> Show password."
printf 'passphrase: '
read -rs PASS
echo
if [ -z "$PASS" ]; then
    echo "Nothing entered — no change made."
    exit 2
fi

# Test BEFORE writing. A wrong value must not replace a right one.
echo -n "testing... "
if printf '%s' "$PASS" | diskutil apfs unlockVolume "$VOL" -stdinpassphrase >/dev/null 2>&1; then
    echo "unlocked ✅"
else
    # Already-unlocked reports failure too, so distinguish the two.
    if diskutil info "$VOL" 2>/dev/null | grep -q "Mounted:.*Yes"; then
        echo "volume is ALREADY unlocked — cannot test the passphrase against it."
        echo "Lock it first (eject in Finder), then re-run. NOTHING WAS WRITTEN."
        unset PASS
        exit 3
    fi
    echo "REJECTED ❌"
    echo "That is not the passphrase for $VOL. NOTHING WAS WRITTEN —"
    echo "the existing file is untouched."
    unset PASS
    exit 1
fi

umask 077
printf '%s' "$PASS" > "$PASSFILE"
unset PASS
chown root:wheel "$PASSFILE"
chmod 600 "$PASSFILE"
echo "stored in $PASSFILE ($(wc -c < "$PASSFILE" | tr -d ' ') bytes, mode 600, root:wheel)"
echo
echo "Now the real test — can the DAEMON use it?"
launchctl kickstart -k system/com.cirrus.tmunlock 2>/dev/null
sleep 4
tail -3 /var/log/cirrus-tm-unlock.log 2>/dev/null | sed 's/^/  /'
echo
echo "Verify from the MacBook with:  runner tm-freshness"
echo "(it now fails when the destination is not mounted, so a green result means"
echo " the volume is genuinely there — not just that an old attempt succeeded)"
