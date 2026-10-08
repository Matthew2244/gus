#!/bin/zsh
# Build Gus with xcodebuild, wrap it into Gus.app, ad-hoc sign it, install to /Applications.
#
#   ./build.sh            build, test, install
#   ./build.sh --no-test  skip the unit tests
#   ./build.sh --no-install  leave the app in build/Gus.app
set -euo pipefail

HERE="${0:A:h}"
cd "$HERE"
BUILD="$HERE/build"
DERIVED="$BUILD/DerivedData"
APP="$BUILD/Gus.app"
DEST="/Applications/Gus.app"
RUN_TESTS=1
INSTALL=1
for a in "$@"; do
  case "$a" in
    --no-test) RUN_TESTS=0 ;;
    --no-install) INSTALL=0 ;;
    *) echo "Unknown option: $a" >&2; exit 2 ;;
  esac
done

if (( RUN_TESTS )); then
  echo "Testing…"
  xcodebuild test -scheme Gus -destination 'platform=macOS,arch=arm64' \
    -test-timeouts-enabled YES -default-test-execution-time-allowance 60 \
    -derivedDataPath "$DERIVED" -quiet || { echo "Tests failed." >&2; exit 1; }
fi

echo "Building…"
xcodebuild build -scheme Gus -configuration Release -destination 'platform=macOS,arch=arm64' \
  -derivedDataPath "$DERIVED" -quiet
BIN="$DERIVED/Build/Products/Release/Gus"
[[ -x "$BIN" ]] || { echo "No binary at $BIN" >&2; exit 1; }

echo "Wrapping into Gus.app…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Gus"
cp "$HERE/Resources/Info.plist" "$APP/Contents/Info.plist"

# The icon is drawn by the app itself (GusMark), so it always matches the header.
ICONSET="$BUILD/Gus.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
"$APP/Contents/MacOS/Gus" --render-icon "$BUILD/icon-1024.png"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon-1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$BUILD/icon-1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Gus.icns"

# Ad-hoc signature. It changes with every build, so after a rebuild macOS asks once more
# for permission to read the Documents folder (where the WING show files live).
# (A real signing identity would keep that permission, but codesign then needs the login
# keychain unlocked by hand, which a one-command build can't do unattended.)
codesign --force --sign - --timestamp=none "$APP"
codesign --verify "$APP"

if (( INSTALL )); then
  echo "Installing to $DEST…"
  osascript -e 'tell application id "net.matthewwhitaker.gus" to quit' >/dev/null 2>&1 || true
  rm -rf "$DEST"
  ditto "$APP" "$DEST"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" >/dev/null 2>&1 || true
  echo "Installed: $DEST"
else
  echo "Built: $APP"
fi
