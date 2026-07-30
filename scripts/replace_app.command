#!/bin/bash
set -euo pipefail

APP_PATH="$1"
STAGED_APP="$2"
DATA_DIR="$3"
BUNDLE_ID="$4"
NEW_VERSION="$5"
OLD_VERSION="$6"
OLD_PID="$7"

UPDATES_DIR="$DATA_DIR/updates"
STAGED_ROOT="$(/usr/bin/dirname "$STAGED_APP")"
BACKUP_DIR="$DATA_DIR/update-backups"
PREVIOUS_APP="$BACKUP_DIR/Radio Command Center.previous.app"
OLDER_APP="$BACKUP_DIR/Radio Command Center.older.app"
DATA_SNAPSHOT="$BACKUP_DIR/user-data-before-update"
OLDER_DATA_SNAPSHOT="$BACKUP_DIR/user-data-older"
TARGET_MOVED=0
NEW_INSTALLED=0
NEW_PID=""

log() {
    /bin/echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"
}

fail() {
    log "Update failed: $*"
    exit 1
}

case "$APP_PATH" in
    /*.app) ;;
    *) fail "invalid application path" ;;
esac
case "$STAGED_APP" in
    "$UPDATES_DIR"/*.app) ;;
    *) fail "staged application is outside the update directory" ;;
esac
case "$STAGED_ROOT" in
    "$UPDATES_DIR"/staged-*) ;;
    *) fail "staged update has an unexpected location" ;;
esac
[[ "$DATA_DIR" != "$APP_PATH"* && "$APP_PATH" != "$DATA_DIR"* ]] \
    || fail "application and data paths overlap"
[[ "$BUNDLE_ID" == "local.radio.command-center" ]] || fail "unexpected bundle identifier"
[[ "$OLD_PID" =~ ^[0-9]+$ ]] || fail "invalid application process"

rollback() {
    local exit_code=$?
    if [[ "$exit_code" -eq 0 ]]; then
        return
    fi
    set +e
    log "Rolling back to version $OLD_VERSION"
    if [[ -n "$NEW_PID" ]]; then
        /bin/kill -TERM "$NEW_PID" 2>/dev/null
        for _ in {1..30}; do
            /bin/kill -0 "$NEW_PID" 2>/dev/null || break
            /bin/sleep 0.1
        done
    fi
    if [[ "$NEW_INSTALLED" -eq 1 && -e "$APP_PATH" ]]; then
        /bin/rm -rf "$APP_PATH"
    fi
    if [[ "$TARGET_MOVED" -eq 1 && -e "$PREVIOUS_APP" ]]; then
        /bin/mv "$PREVIOUS_APP" "$APP_PATH"
    fi
    if [[ -e "$OLDER_APP" && ! -e "$PREVIOUS_APP" ]]; then
        /bin/mv "$OLDER_APP" "$PREVIOUS_APP"
    fi
    if [[ -e "$OLDER_DATA_SNAPSHOT" ]]; then
        if [[ -e "$DATA_SNAPSHOT" ]]; then
            /bin/rm -rf "$DATA_SNAPSHOT"
        fi
        /bin/mv "$OLDER_DATA_SNAPSHOT" "$DATA_SNAPSHOT"
    fi
    if [[ -e "$APP_PATH" ]]; then
        /usr/bin/open -n "$APP_PATH"
    fi
    log "Rollback complete"
}
trap rollback EXIT

log "Waiting for Radio Command Center to close"
for _ in {1..150}; do
    if ! /bin/kill -0 "$OLD_PID" 2>/dev/null; then
        break
    fi
    /bin/sleep 0.2
done
if /bin/kill -0 "$OLD_PID" 2>/dev/null; then
    fail "the running app did not close"
fi

[[ -d "$APP_PATH" ]] || fail "installed application is missing"
[[ -d "$STAGED_APP" ]] || fail "staged application is missing"

STAGED_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$STAGED_APP/Contents/Info.plist")"
STAGED_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$STAGED_APP/Contents/Info.plist")"
[[ "$STAGED_BUNDLE_ID" == "$BUNDLE_ID" ]] || fail "staged bundle identifier does not match"
[[ "$STAGED_VERSION" == "$NEW_VERSION" ]] || fail "staged version does not match"
/usr/bin/codesign --verify --deep --strict "$STAGED_APP"

log "Creating a recovery snapshot without touching recordings"
/bin/mkdir -p "$BACKUP_DIR"
if [[ -e "$OLDER_DATA_SNAPSHOT" ]]; then
    /bin/rm -rf "$OLDER_DATA_SNAPSHOT"
fi
if [[ -e "$DATA_SNAPSHOT" ]]; then
    /bin/mv "$DATA_SNAPSHOT" "$OLDER_DATA_SNAPSHOT"
fi
/bin/mkdir -p "$DATA_SNAPSHOT"
if [[ -f "$DATA_DIR/settings.json" ]]; then
    /bin/cp -p "$DATA_DIR/settings.json" "$DATA_SNAPSHOT/settings.json"
fi
if [[ -d "$DATA_DIR/security" ]]; then
    /bin/cp -R "$DATA_DIR/security" "$DATA_SNAPSHOT/security"
fi
if [[ -d "$DATA_DIR/databases" ]]; then
    /bin/cp -R "$DATA_DIR/databases" "$DATA_SNAPSHOT/databases"
fi

if [[ -e "$OLDER_APP" ]]; then
    /bin/rm -rf "$OLDER_APP"
fi
if [[ -e "$PREVIOUS_APP" ]]; then
    /bin/mv "$PREVIOUS_APP" "$OLDER_APP"
fi

log "Installing Radio Command Center $NEW_VERSION"
/bin/mv "$APP_PATH" "$PREVIOUS_APP"
TARGET_MOVED=1
/bin/mv "$STAGED_APP" "$APP_PATH"
NEW_INSTALLED=1
/usr/bin/xattr -cr "$APP_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_PATH"

INSTALLED_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP_PATH/Contents/Info.plist")"
INSTALLED_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_PATH/Contents/Info.plist")"
[[ "$INSTALLED_BUNDLE_ID" == "$BUNDLE_ID" ]] || fail "installed bundle identifier does not match"
[[ "$INSTALLED_VERSION" == "$NEW_VERSION" ]] || fail "installed version does not match"

/usr/bin/open -n "$APP_PATH"
LAUNCHED=0
for _ in {1..50}; do
    NEW_PID="$(/usr/bin/pgrep -f "$APP_PATH/Contents/MacOS/RadioCommandCenter" | /usr/bin/head -1 || true)"
    if [[ -n "$NEW_PID" ]]; then
        LAUNCHED=1
        break
    fi
    /bin/sleep 0.2
done
[[ "$LAUNCHED" -eq 1 ]] || fail "the updated application did not launch"
/bin/sleep 3
/usr/bin/codesign --verify --deep --strict "$APP_PATH" \
    || fail "the updated application changed after launch"

NEW_INSTALLED=0
TARGET_MOVED=0
trap - EXIT
if [[ -e "$OLDER_APP" ]]; then
    /bin/rm -rf "$OLDER_APP" || log "Could not remove the older app backup"
fi
if [[ -e "$OLDER_DATA_SNAPSHOT" ]]; then
    /bin/rm -rf "$OLDER_DATA_SNAPSHOT" \
        || log "Could not remove the older data snapshot"
fi
/bin/rm -rf "$STAGED_ROOT" || log "Could not remove staged update files"
log "Update to $NEW_VERSION installed successfully"
