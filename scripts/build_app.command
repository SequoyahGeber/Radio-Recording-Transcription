#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

APP_DIR="dist/Radio Command Center.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
MODULE_CACHE_DIR="${TMPDIR:-/tmp}/radio-command-center-swift-cache"

mkdir -p "$MACOS_DIR" "$MODULE_CACHE_DIR"
xcrun swiftc \
    -module-cache-path "$MODULE_CACHE_DIR" \
    macos/RadioCommandCenter.swift \
    -framework AppKit \
    -o "$MACOS_DIR/RadioCommandCenter"
cp macos/Info.plist "$CONTENTS_DIR/Info.plist"
chmod +x "$MACOS_DIR/RadioCommandCenter"
xattr -cr "$APP_DIR"
xattr -dr com.apple.FinderInfo "$APP_DIR" 2>/dev/null || true
xattr -dr 'com.apple.fileprovider.fpfs#P' "$APP_DIR" 2>/dev/null || true
xattr -d com.apple.FinderInfo "$APP_DIR" 2>/dev/null || true
xattr -d 'com.apple.fileprovider.fpfs#P' "$APP_DIR" 2>/dev/null || true
codesign --force --deep --sign - "$APP_DIR"

echo "Built: $APP_DIR"
