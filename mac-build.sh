#!/bin/bash
# One-shot SMO -> IPA build on a rented cloud Mac.
#
#   ./mac-build.sh ~/Games/MarioOdyssey ~/Games/MarioOdyssey/prod.keys
#
# Checks every prerequisite first and refuses to start a 40-minute build that
# would fail at minute 35. Nothing here touches key material beyond handing the
# path to the official script.

set -uo pipefail

NSP_DIR="${1:?usage: $0 <NSP dir with the two NSPs> <prod.keys>}"
KEYS="${2:?usage: $0 <NSP dir> <prod.keys>}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [ok]   %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; FAIL=1; }

say '== 1. system =='
sw_vers -productVersion | awk '{print "  macOS " $0}'
[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 26 ] \
  && ok "macOS 26+ (iOS 26 SDK available)" \
  || bad "macOS $(sw_vers -productVersion): needs 26 for the iOS 26 SDK"

say '== 2. Xcode =='
if command -v xcodebuild >/dev/null 2>&1; then
  XV="$(xcodebuild -version 2>/dev/null | head -1 | awk '{print $2}')"
  case "$XV" in
    26.*|27.*|28.*) ok "Xcode $XV" ;;
    *) bad "Xcode $XV: needs 26 or later (xcode-select -p = $(xcode-select -p 2>/dev/null))" ;;
  esac
else
  bad "xcodebuild missing: install Xcode from the App Store"
fi

say '== 3. tools =='
for t in xcodegen zstd dotnet make; do
  command -v "$t" >/dev/null 2>&1 && ok "$t ($(command -v "$t"))" || bad "$t missing"
done

if command -v spirv-cross >/dev/null 2>&1; then
  REV="$(spirv-cross --revision 2>/dev/null | tr -d '\r')"
  if printf '%s' "$REV" | grep -q '1\.4\.357'; then
    ok "spirv-cross $REV"
  else
    bad "spirv-cross '$REV': must be 1.4.357.0 (a different version translates shaders differently and the build stops on a digest mismatch)"
  fi
else
  bad "spirv-cross missing"
fi

say '== 4. python deps =='
python3 - <<'PY' 2>/dev/null && ok "lz4, capstone, fontTools" || bad "python deps: pip3 install lz4 capstone fonttools"
import lz4, capstone, fontTools
PY

say '== 5. inputs =='
[ -d "$NSP_DIR" ] && ok "NSP dir $NSP_DIR" || bad "no dir $NSP_DIR"
ls "$NSP_DIR"/*.nsp >/dev/null 2>&1 \
  && ok "$(ls "$NSP_DIR"/*.nsp | wc -l | tr -d ' ') NSP file(s)" \
  || bad "no .nsp in $NSP_DIR"
[ -f "$KEYS" ] && ok "keys $KEYS" || bad "no key file $KEYS"

say '== 6. disk =='
AVAIL="$(df -g "$NSP_DIR" | awk 'NR==2{print $4}')"
[ "${AVAIL:-0}" -ge 25 ] && ok "${AVAIL} GB free" || bad "${AVAIL} GB free: needs ~25 GB (NSP x2 + 10 GB)"

say '== 7. repo =='
[ -f "$REPO_DIR/scripts/build_ipa_from_nsp.py" ] \
  && ok "nxnative at $REPO_DIR" \
  || bad "run this from inside the nxnative checkout"

if [ "$FAIL" -ne 0 ]; then
  say ''
  say 'Stopping before the build. Fix the [FAIL] lines above and rerun.'
  exit 1
fi

say ''
say '== building (30-60 min; Metal shader compile ~15 min of it) =='
cd "$REPO_DIR" || exit 1
python3 scripts/build_ipa_from_nsp.py "$NSP_DIR" -k "$KEYS"
RC=$?

if [ "$RC" -ne 0 ]; then
  say ''
  say "Build exited $RC. Check the last stage line above; the common causes are a"
  say "spirv-cross version mismatch, a damaged NSP file, or the disk filling up."
  exit "$RC"
fi

say ''
say '== done =='
find "$NSP_DIR" -maxdepth 1 -name '*.ipa' -exec ls -lh {} \;
