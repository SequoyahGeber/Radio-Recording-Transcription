#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

APP_DIR="dist/Radio Command Center.app"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/radio-command-center-build.XXXXXX")"
STAGED_APP="$BUILD_ROOT/Radio Command Center.app"
PREVIOUS_APP="$BUILD_ROOT/previous.app"
CONTENTS_DIR="$STAGED_APP/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
ICONSET_DIR="$BUILD_ROOT/AppIcon.iconset"
MODULE_CACHE_DIR="${TMPDIR:-/tmp}/radio-command-center-swift-cache"
RUNTIME_DIR="$RESOURCES_DIR/runtime"
PYTHON_RESOURCES_DIR="$RESOURCES_DIR/python"
PYTHON_VERSION="3.12"

cleanup() {
    rm -rf "$BUILD_ROOT"
}
trap cleanup EXIT

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$ICONSET_DIR" "$MODULE_CACHE_DIR" "$(dirname "$APP_DIR")"
xcrun swiftc \
    -module-cache-path "$MODULE_CACHE_DIR" \
    macos/RadioCommandCenter.swift \
    -framework AppKit \
    -framework WebKit \
    -o "$MACOS_DIR/RadioCommandCenter"

sips -z 16 16 macos/AppIcon.png --out "$ICONSET_DIR/icon_16x16.png" >/dev/null
sips -z 32 32 macos/AppIcon.png --out "$ICONSET_DIR/icon_16x16@2x.png" >/dev/null
sips -z 32 32 macos/AppIcon.png --out "$ICONSET_DIR/icon_32x32.png" >/dev/null
sips -z 64 64 macos/AppIcon.png --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null
sips -z 128 128 macos/AppIcon.png --out "$ICONSET_DIR/icon_128x128.png" >/dev/null
sips -z 256 256 macos/AppIcon.png --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null
sips -z 256 256 macos/AppIcon.png --out "$ICONSET_DIR/icon_256x256.png" >/dev/null
sips -z 512 512 macos/AppIcon.png --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null
sips -z 512 512 macos/AppIcon.png --out "$ICONSET_DIR/icon_512x512.png" >/dev/null
sips -z 1024 1024 macos/AppIcon.png --out "$ICONSET_DIR/icon_512x512@2x.png" >/dev/null
./venv/bin/python scripts/build_icns.py "$ICONSET_DIR" "$RESOURCES_DIR/AppIcon.icns"

PYTHON_FRAMEWORK_SOURCE="$(
    ./venv/bin/python -c \
        'import pathlib, sys; print(pathlib.Path(sys.base_prefix).parents[1])'
)"
PYTHON_FRAMEWORK_DESTINATION="$PYTHON_RESOURCES_DIR/Python.framework"
SITE_PACKAGES_SOURCE="venv/lib/python${PYTHON_VERSION}/site-packages"

if [[ ! -d "$PYTHON_FRAMEWORK_SOURCE" || ! -d "$SITE_PACKAGES_SOURCE" ]]; then
    echo "The bundled Python runtime is unavailable. Run scripts/install.command first." >&2
    exit 1
fi

mkdir -p "$RUNTIME_DIR/scripts" "$PYTHON_RESOURCES_DIR"
cp -R backend "$RUNTIME_DIR/backend"
cp -R frontend "$RUNTIME_DIR/frontend"
for runtime_script in \
    app_updater.py \
    replace_app.command \
    service_control.py \
    setup_security.py \
    supervisor.py
do
    cp "scripts/$runtime_script" "$RUNTIME_DIR/scripts/$runtime_script"
done
cp -R "$SITE_PACKAGES_SOURCE" "$RUNTIME_DIR/site-packages"
# mlx-whisper declares PyTorch for an optional conversion helper, but the app's
# native MLX transcription path does not import it. Excluding it keeps the
# self-contained release below GitHub's release-asset size ceiling.
find "$RUNTIME_DIR/site-packages" -maxdepth 1 \
    \( -name 'torch' -o -name 'torch-*.dist-info' -o -name 'torchgen' \) \
    -exec rm -rf {} +
find "$RUNTIME_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} +
cp sync.py "$RUNTIME_DIR/sync.py"
cp -R "$PYTHON_FRAMEWORK_SOURCE" "$PYTHON_FRAMEWORK_DESTINATION"

EMBEDDED_PYTHON="$PYTHON_FRAMEWORK_DESTINATION/Versions/$PYTHON_VERSION/bin/python$PYTHON_VERSION"
EMBEDDED_FRAMEWORK_SITE_PACKAGES="$PYTHON_FRAMEWORK_DESTINATION/Versions/$PYTHON_VERSION/lib/python$PYTHON_VERSION/site-packages"
if [[ -L "$EMBEDDED_FRAMEWORK_SITE_PACKAGES" ]]; then
    unlink "$EMBEDDED_FRAMEWORK_SITE_PACKAGES"
fi
PYTHON_LIBRARY_SOURCE="$(
    otool -L "$EMBEDDED_PYTHON" |
        awk '/Python.framework.*Versions.*3.12.*Python/ {print $1; exit}'
)"
install_name_tool \
    -change "$PYTHON_LIBRARY_SOURCE" \
    "@executable_path/../Python" \
    "$EMBEDDED_PYTHON"
chmod +x "$EMBEDDED_PYTHON"
codesign --force --sign - "$EMBEDDED_PYTHON"

cp macos/Info.plist "$CONTENTS_DIR/Info.plist"
chmod +x "$MACOS_DIR/RadioCommandCenter"
xattr -cr "$STAGED_APP"
codesign --force --deep --sign - "$STAGED_APP"

# Sign outside Desktop first: file-provider metadata can make codesign reject
# an existing app bundle even though the application itself is valid.
if [[ -e "$APP_DIR" ]]; then
    mv "$APP_DIR" "$PREVIOUS_APP"
fi
if ! mv "$STAGED_APP" "$APP_DIR"; then
    [[ ! -e "$APP_DIR" && -e "$PREVIOUS_APP" ]] && mv "$PREVIOUS_APP" "$APP_DIR"
    exit 1
fi

echo "Built: $APP_DIR"
