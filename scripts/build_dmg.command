#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

APP_NAME="Radio Command Center"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' macos/Info.plist)"
DMG_PATH="$PWD/dist/Radio-Command-Center-${VERSION}-arm64.dmg"
STAGING_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/radio-command-center-dmg.XXXXXX")"
STAGING_DIR="$STAGING_ROOT/$APP_NAME"

cleanup() {
    rm -rf "$STAGING_ROOT"
}
trap cleanup EXIT

APP_SOURCE="dist/$APP_NAME.app"
if [[ "${SKIP_APP_BUILD:-0}" != "1" ]]; then
    ./scripts/build_app.command
    BUNDLED_APP="$STAGING_ROOT/built-$APP_NAME.app"
    mv "$APP_SOURCE" "$BUNDLED_APP"
    APP_SOURCE="$BUNDLED_APP"
fi

mkdir -p "$STAGING_DIR"
ditto "$APP_SOURCE" "$STAGING_DIR/$APP_NAME.app"
xattr -cr "$STAGING_DIR/$APP_NAME.app"
codesign --force --deep --sign - "$STAGING_DIR/$APP_NAME.app"
codesign --verify --deep --strict "$STAGING_DIR/$APP_NAME.app"
ln -s /Applications "$STAGING_DIR/Applications"
cp macos/DMG_README.txt "$STAGING_DIR/Read Me.txt"

hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$STAGING_DIR" \
    -format UDZO \
    -imagekey zlib-level=6 \
    -ov \
    "$DMG_PATH"

hdiutil verify "$DMG_PATH"
echo "Built and verified: $DMG_PATH"
