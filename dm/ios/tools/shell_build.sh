#!/bin/bash
# Build the "shell": an unsigned zip holding a complete iOS app that contains NO game content.
#
#   GAME=0100000000010000 dm/ios/tools/shell_build.sh [output dir]
#
# The shell carries our own hosts (host/, ios/Sources, the game profile's shader tables and the
# fonts) and nothing else. What comes from the player's own game files is left out and listed in
# dm/shell_manifest.txt inside the app; the packing tool (scripts/nx_pack.py, runs on Windows)
# builds exactly those files from the player's NSP folder, drops them in, and writes an IPA.
#
# Output: <out>/nxnative-shell-<GAME>.zip  (Build input, not installable: a shell installed as is
# stops at boot with "this install package has no game program yet".)
#
# This is the only step that needs macOS + Xcode. It does not need the game files at all.
set -u
cd "$(dirname "$0")/.." || exit 1
IOS=$PWD; DM=$(cd .. && pwd)
t() { echo "[$(date +%T)] $*"; }; die() { t "FAIL: $*"; exit 1; }

[ -n "${GAME:-}" ] || die "GAME is not set: GAME=<base game title ID> $0; profiles: $(python3 "$DM/games/game.py" list | awk -F'\t' '{printf "%s (%s)  ", $1, $3}')"
export GAME; GD=$(python3 "$DM/games/game.py" dir "$GAME") || die "no game profile $GAME"
gp() { python3 "$DM/games/game.py" "$GAME" "$1" || die "game profile $GD: $1"; }
[ -f "$GD/game.tsv" ] || die "no game profile $GD/game.tsv"

for c in xcodebuild xcodegen python3 make; do command -v "$c" > /dev/null || die "$c is missing"; done
[ "$(xcodebuild -version 2>/dev/null | head -1 | awk '{print $2}' | cut -d. -f1)" -ge 26 ] || die "Xcode 26 or later is required (deployment target is iOS 26)"

OUT=${1:-$DM/../shell-out}; mkdir -p "$OUT" || die "mkdir $OUT"
O=$OUT/build; DD=$O/dd
rm -rf "$O"; mkdir -p "$O" || die "mkdir $O"

PKG=$(gp package)
export LC_ALL=C DM_BUNDLE_ID=${DM_BUNDLE_ID:-org.nxnative.$(echo "$GAME" | tr 'A-Z' 'a-z')} DM_TEAM_ID=${DM_TEAM_ID:-0000000000}
MODS=$(gp modules)
GFXAPI=$(python3 "$DM/games/game.py" "$GAME" graphics 2> /dev/null) || GFXAPI=nvn
V=$(sed -n 's/^#define DMSH_CHAIN "\(.*\)"/\1/p' "$GD/dmsh_chain.h")
if [ "$GFXAPI" = vulkan ]; then MSLDIR=vk; else MSLDIR=b-$V; fi
[ -n "$MSLDIR" ] || die "no shader chain version in $GD/dmsh_chain.h"

# The app's display name/version come from placeholders: a shell has no game data to read them
# from. The packing tool rewrites Info.plist with the real ones from the player's control data.
export DM_DISPLAY_NAME=${DM_DISPLAY_NAME:-"Nintendo Switch Game"} DM_APP_VERSION=${DM_APP_VERSION:-0.0.0} DM_APP_CATEGORY=$(gp app_category)

t "generate Gen/ (host sources, tables) for $GAME"
rm -rf Gen && mkdir -p Gen/dm || die "mkdir Gen"
python3 tools/gen_build.py ../host Gen > Gen/dm.xcconfig || die "gen_build"
# No module images in the shell: the loader takes them from Frameworks/Guest.framework/Guest,
# which the packing tool writes from the player's game files.
echo '// shell: module images live in Frameworks/Guest.framework (dm/ios/tools/shell_build.sh)' > Gen/embed.S
# Turn on the loader's shell branch and drop the embedded segments' link flags
sed -i '' -e 's/^OTHER_CFLAGS = \$(inherited) /OTHER_CFLAGS = $(inherited) -DDM_SHELL /' Gen/dm.xcconfig 2> /dev/null \
  || sed -i -e 's/^OTHER_CFLAGS = \$(inherited) /OTHER_CFLAGS = $(inherited) -DDM_SHELL /' Gen/dm.xcconfig
grep -q -- '-DDM_SHELL' Gen/dm.xcconfig || die "could not add -DDM_SHELL to Gen/dm.xcconfig"
# The rewriter tables (*.fixups.tsv and friends) are generated per game build by patch_nso.py on
# the player's machine; shader_map/pso_manifest come from the profile and ship in the repository.
if [ "$GFXAPI" = vulkan ]; then cp "$GD/shader_map.tsv" "$GD/pso_manifest.tsv" Gen/dm/ || die "copy shader tables"
else cp "$GD/shader_map.tsv" "$GD/pso_manifest.tsv" "$GD/c1_full.tsv" Gen/dm/ || die "copy shader tables"; fi
mkdir -p Gen/dm/font && cp "${STD_FONT:-/System/Library/Fonts/Supplemental/Arial Unicode.ttf}" Gen/dm/font/standard.ttf || die "copy standard font (STD_FONT=a TTF)"
"${FONT_PY:-python3}" "$DM/tools/make_ext_font.py" Gen/dm/font/nintendo_ext.ttf > /dev/null || die "make_ext_font.py (pip install fonttools)"
# No app icon: the game's own icon is game content. A generic one is drawn here so the app is not
# blank on the home screen before packing; the packing tool replaces it when the player has one.
mkdir -p Gen/Assets.xcassets && printf '{"info":{"author":"xcode","version":1}}\n' > Gen/Assets.xcassets/Contents.json || die "asset catalog"
if [ -n "${SHELL_ICON:-}" ] && [ -f "${SHELL_ICON:-}" ]; then
  AI=Gen/Assets.xcassets/AppIcon.appiconset; mkdir -p "$AI"
  sips -s format png -z 1024 1024 "$SHELL_ICON" --out "$AI/icon.png" > /dev/null || die "sips $SHELL_ICON"
  printf '{"images":[{"filename":"icon.png","idiom":"universal","platform":"ios","size":"1024x1024"}],"info":{"author":"xcode","version":1}}\n' > "$AI/Contents.json"
  echo "DM_APPICON = AppIcon" >> Gen/dm.xcconfig
fi

xcodegen generate -q > /dev/null || die "xcodegen"
t "xcodebuild (Release, unsigned, iPhone + iPad, DM_SHELL)"
xcodebuild -project DmIOS.xcodeproj -scheme DmIOS -configuration Release -destination 'generic/platform=iOS' -derivedDataPath "$DD" \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY= TARGETED_DEVICE_FAMILY='1,2' build > "$O/build.log" 2>&1 \
  || { grep -E 'error:' "$O/build.log" | head -20; die "xcodebuild, log $O/build.log"; }
APP=$DD/Build/Products/Release-iphoneos/DmIOS.app; [ -d "$APP" ] || APP=$(grep -o '/[^ ]*/Release-iphoneos/DmIOS\.app' "$O/build.log" | tail -1)
[ -n "$APP" ] && [ -d "$APP" ] || die "no built app"

t "strip game content and record what the shell lacks"
A=$O/app; rm -rf "$A"; cp -R "$APP" "$A" || die "copy app"
MAN=$A/dm/shell_manifest.txt; : > "$MAN"
take() {  # path inside the package, trailing / = directory
  local p=${1%/}; [ -e "$A/$p" ] || return 0
  rm -rf "$A/$p"; echo "$1" >> "$MAN"; }
take "romfs/"
take "aoc/"
take "msl/"
take "Frameworks/Guest.framework/"
for f in "$A"/dm/*.fixups.tsv "$A"/dm/*.gates.tsv "$A"/dm/*.params.tsv "$A"/dm/*.cuts.tsv "$A"/dm/*.obs.tsv; do
  [ -e "$f" ] && take "dm/$(basename "$f")"; done
rm -rf "$A/_CodeSignature" "$A/embedded.mobileprovision"

# Version info the packing tool checks before it writes anything
printf 'PRODUCT=nxnative\nGAME=%s\nPACKAGE=%s\nMODULES=%s\nCHAIN=%s\nMSLDIR=%s\nGFXAPI=%s\n' \
  "$GAME" "$PKG" "$MODS" "$V" "$MSLDIR" "$GFXAPI" > "$A/dm/shell_info.txt"
echo "Info.plist display name and version are placeholders; nx_pack.py rewrites them from control.nacp" >> "$A/dm/shell_info.txt"

# Check: no module image sections in the shell executable
if command -v otool > /dev/null; then
  n=$(otool -l "$A/DmIOS" | grep -c -E 'sectname __dm.*x[01]|segname __DM.*W')
  [ "$n" = 0 ] || die "the shell executable still has $n module image section(s)"
fi
t "shell manifest:"; sed 's/^/    /' "$MAN"

t "pack the shell zip"
# Ad-hoc sign the bundle. Without this the executable carries no LC_CODE_SIGNATURE at all
# (xcodebuild ran with CODE_SIGNING_ALLOWED=NO), and signing tools such as i4Tools refuse to
# re-sign a binary that has no signature blob to replace (they report error 96). An ad-hoc
# signature makes the shell a well-formed unsigned app that any sideloading tool can re-sign.
if command -v codesign > /dev/null; then
  codesign --force --sign - --timestamp=none "$A/DmIOS" > /dev/null 2>&1 \
    || die "ad-hoc codesign failed"
  codesign -dv "$A/DmIOS" 2>&1 | head -3 || true
else
  t "warning: no codesign on this machine; the shell will have no LC_CODE_SIGNATURE"
fi
ZIP=$OUT/nxnative-shell-$GAME.zip
rm -f "$ZIP"
python3 tools/pack_ipa.py "$ZIP" --skip Payload/DmIOS.app/_CodeSignature --skip Payload/DmIOS.app/embedded.mobileprovision \
  "Payload/DmIOS.app=$A" || die "pack_ipa"
t "done: $ZIP ($(du -h "$ZIP" | cut -f1))"
t "next: copy it to your Windows machine and run:"
t "  python scripts/nx_pack.py \"$ZIP\" <NSP folder with ExeFS/ and RomFS/> -k prod.keys -o SuperMarioOdyssey.ipa"
