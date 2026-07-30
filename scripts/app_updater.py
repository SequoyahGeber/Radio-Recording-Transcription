#!/usr/bin/env python3
"""Secure GitHub-release updater for the packaged Radio Command Center app."""

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REPOSITORY = "SequoyahGeber/Radio-Recording-Transcription"
EXPECTED_BUNDLE_ID = "local.radio.command-center"
UPDATE_USER_AGENT = "Radio-Command-Center-Updater/1"
VERSION_PATTERN = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
SHA256_PATTERN = re.compile(r"\b([a-fA-F0-9]{64})\b")


class UpdateError(RuntimeError):
    pass


def parse_version(value):
    match = VERSION_PATTERN.fullmatch(str(value).strip())
    if not match:
        raise UpdateError(f"Unsupported release version: {value}")
    return tuple(int(part or 0) for part in match.groups())


def normalized_version(value):
    return ".".join(str(part) for part in parse_version(value))


def validate_repository(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value or ""):
        raise UpdateError("The GitHub repository setting is invalid")
    return value


def request_bytes(url, timeout=30):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": UPDATE_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise UpdateError(f"Could not contact GitHub: {exc}") from exc


def latest_release(repository):
    repository = validate_repository(repository)
    payload = request_bytes(
        f"https://api.github.com/repos/{repository}/releases/latest"
    )
    try:
        release = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("GitHub returned an invalid release response") from exc
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise UpdateError("GitHub did not return a stable published release")
    return release


def find_release_assets(release):
    tag = release.get("tag_name", "")
    latest_version = normalized_version(tag)
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("The GitHub release has no downloadable assets")

    suffix = "-arm64.dmg"
    candidates = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and str(asset.get("name", "")).endswith(suffix)
        and asset.get("browser_download_url")
    ]
    if len(candidates) != 1:
        raise UpdateError(
            "The release must contain exactly one Apple-silicon DMG asset"
        )
    dmg_asset = candidates[0]
    expected_name = f"Radio-Command-Center-{latest_version}-arm64.dmg"
    if dmg_asset.get("name") != expected_name:
        raise UpdateError(
            f"The release DMG must be named {expected_name}"
        )

    checksum_names = {
        f"{expected_name}.sha256",
        f"Radio-Command-Center-{latest_version}-arm64.sha256",
    }
    checksum_assets = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and asset.get("name") in checksum_names
        and asset.get("browser_download_url")
    ]
    checksum_asset = checksum_assets[0] if len(checksum_assets) == 1 else None

    digest = str(dmg_asset.get("digest") or "")
    if digest.startswith("sha256:"):
        digest = digest.removeprefix("sha256:").lower()
    else:
        digest = ""
    if digest and not SHA256_PATTERN.fullmatch(digest):
        raise UpdateError("The release asset contains an invalid SHA-256 digest")
    if not digest and checksum_asset is None:
        raise UpdateError(
            "The release has no GitHub SHA-256 digest or checksum asset"
        )
    return latest_version, dmg_asset, checksum_asset, digest


def release_status(current_version, repository=DEFAULT_REPOSITORY, release=None):
    release = release or latest_release(repository)
    latest_version, dmg_asset, checksum_asset, digest = find_release_assets(release)
    comparison = parse_version(latest_version) > parse_version(current_version)
    return {
        "status": "available" if comparison else "current",
        "current_version": normalized_version(current_version),
        "latest_version": latest_version,
        "release_name": str(release.get("name") or release.get("tag_name") or ""),
        "release_notes": str(release.get("body") or "")[:12000],
        "release_url": str(release.get("html_url") or ""),
        "published_at": str(release.get("published_at") or ""),
        "asset": {
            "name": dmg_asset["name"],
            "url": dmg_asset["browser_download_url"],
            "size": int(dmg_asset.get("size") or 0),
            "sha256": digest,
        },
        "checksum_url": (
            checksum_asset.get("browser_download_url") if checksum_asset else ""
        ),
    }


def require_update(current_version, repository):
    status = release_status(current_version, repository)
    if status["status"] != "available":
        raise UpdateError(
            f"Version {status['current_version']} is already current"
        )
    return status


def ensure_preservation_boundary(app_path, data_dir):
    app_path = os.path.realpath(os.path.abspath(app_path))
    data_dir = os.path.realpath(os.path.abspath(data_dir))
    if not app_path.endswith(".app") or not os.path.isdir(app_path):
        raise UpdateError("The running application bundle could not be found")
    if app_path.startswith("/Volumes/"):
        raise UpdateError(
            "Drag Radio Command Center to Applications before installing updates"
        )
    if os.path.commonpath([app_path, data_dir]) in {app_path, data_dir}:
        raise UpdateError(
            "The application bundle and its data directory must be separate"
        )
    return app_path, data_dir


def bundle_metadata(app_path):
    plist_path = os.path.join(app_path, "Contents", "Info.plist")
    try:
        with open(plist_path, "rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise UpdateError("The application has an invalid Info.plist") from exc
    return {
        "bundle_id": payload.get("CFBundleIdentifier", ""),
        "version": normalized_version(payload.get("CFBundleShortVersionString", "")),
    }


def run_checked(arguments):
    try:
        return subprocess.run(
            arguments,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        output = getattr(exc, "stdout", "") or str(exc)
        raise UpdateError(output.strip() or f"Command failed: {arguments[0]}") from exc


def download_file(url, destination, expected_size=0):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": UPDATE_USER_AGENT,
        },
    )
    digest = hashlib.sha256()
    written = 0
    temporary_path = f"{destination}.downloading"
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with open(temporary_path, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
        if expected_size and written != expected_size:
            raise UpdateError(
                f"The release download was incomplete ({written} of {expected_size} bytes)"
            )
        os.replace(temporary_path, destination)
    except UpdateError:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise UpdateError(f"Could not download the release: {exc}") from exc
    return digest.hexdigest()


def expected_checksum(status):
    expected = status["asset"]["sha256"]
    if expected:
        return expected
    payload = request_bytes(status["checksum_url"])
    match = SHA256_PATTERN.search(payload.decode("ascii", errors="ignore"))
    if not match:
        raise UpdateError("The release checksum file is invalid")
    return match.group(1).lower()


def check_free_space(data_dir, asset_size):
    free = shutil.disk_usage(data_dir).free
    required = max(2 * 1024**3, int(asset_size * 3.25))
    if free < required:
        raise UpdateError(
            f"At least {required // 1024**3 + 1} GB free is required to stage this update"
        )


def mounted_app(dmg_path):
    output = run_checked(
        ["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-plist", dmg_path]
    )
    try:
        payload = plistlib.loads(output.encode("utf-8"))
    except plistlib.InvalidFileException as exc:
        raise UpdateError("macOS could not read the mounted release") from exc
    mount_points = [
        entity.get("mount-point")
        for entity in payload.get("system-entities", [])
        if entity.get("mount-point")
    ]
    if len(mount_points) != 1:
        for mount_point in mount_points:
            try:
                run_checked(["/usr/bin/hdiutil", "detach", mount_point])
            except UpdateError:
                pass
        raise UpdateError("The release DMG has an unexpected volume layout")
    mount_point = mount_points[0]
    app_path = os.path.join(mount_point, "Radio Command Center.app")
    if not os.path.isdir(app_path):
        run_checked(["/usr/bin/hdiutil", "detach", mount_point])
        raise UpdateError("The release DMG does not contain Radio Command Center.app")
    return mount_point, app_path


def atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".update-manifest-", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def prepare_update(current_version, repository, app_path, data_dir):
    if sys.platform != "darwin":
        raise UpdateError("Application updates are supported only on macOS")
    app_path, data_dir = ensure_preservation_boundary(app_path, data_dir)
    current_metadata = bundle_metadata(app_path)
    if current_metadata["bundle_id"] != EXPECTED_BUNDLE_ID:
        raise UpdateError("The running application has an unexpected bundle identifier")
    if parse_version(current_metadata["version"]) != parse_version(current_version):
        raise UpdateError("The running application version does not match its bundle")

    status = require_update(current_version, repository)
    check_free_space(data_dir, status["asset"]["size"])
    update_root = os.path.join(
        data_dir, "updates", f"staged-{status['latest_version']}"
    )
    os.makedirs(os.path.dirname(update_root), exist_ok=True)
    if os.path.exists(update_root):
        shutil.rmtree(update_root)
    os.makedirs(update_root)

    dmg_path = os.path.join(update_root, status["asset"]["name"])
    actual_checksum = download_file(
        status["asset"]["url"],
        dmg_path,
        status["asset"]["size"],
    )
    if actual_checksum != expected_checksum(status):
        raise UpdateError("The downloaded release failed SHA-256 verification")
    run_checked(["/usr/bin/hdiutil", "verify", dmg_path])

    mount_point = None
    try:
        mount_point, source_app = mounted_app(dmg_path)
        release_metadata = bundle_metadata(source_app)
        if release_metadata["bundle_id"] != EXPECTED_BUNDLE_ID:
            raise UpdateError("The release has an unexpected bundle identifier")
        if release_metadata["version"] != status["latest_version"]:
            raise UpdateError("The release version does not match its GitHub tag")
        run_checked(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", source_app]
        )

        staged_app = os.path.join(update_root, "Radio Command Center.app")
        run_checked(["/usr/bin/ditto", "--noqtn", source_app, staged_app])
        run_checked(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", staged_app]
        )
    finally:
        if mount_point:
            try:
                run_checked(["/usr/bin/hdiutil", "detach", mount_point])
            except UpdateError:
                run_checked(["/usr/bin/hdiutil", "detach", "-force", mount_point])

    helper_source = os.path.join(PROJECT_ROOT, "scripts", "replace_app.command")
    if not os.path.isfile(helper_source):
        raise UpdateError("The updater installation helper is missing")
    helper_path = os.path.join(update_root, "replace_app.command")
    shutil.copy2(helper_source, helper_path)
    os.chmod(helper_path, 0o700)

    manifest_path = os.path.join(update_root, "update-manifest.json")
    manifest = {
        "app_path": app_path,
        "bundle_id": EXPECTED_BUNDLE_ID,
        "current_version": normalized_version(current_version),
        "data_dir": data_dir,
        "helper_path": helper_path,
        "latest_version": status["latest_version"],
        "staged_app": staged_app,
    }
    atomic_json(manifest_path, manifest)
    return {
        "status": "ready",
        "current_version": manifest["current_version"],
        "latest_version": manifest["latest_version"],
        "manifest_path": manifest_path,
    }


def path_within(path, parent):
    path = os.path.realpath(os.path.abspath(path))
    parent = os.path.realpath(os.path.abspath(parent))
    return os.path.commonpath([path, parent]) == parent


def launch_update(manifest_path, current_pid):
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError("The prepared update manifest is invalid") from exc

    required = {
        "app_path",
        "bundle_id",
        "current_version",
        "data_dir",
        "helper_path",
        "latest_version",
        "staged_app",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise UpdateError("The prepared update manifest is incomplete")

    app_path, data_dir = ensure_preservation_boundary(
        manifest["app_path"], manifest["data_dir"]
    )
    manifest["app_path"] = app_path
    manifest["data_dir"] = data_dir
    update_root = os.path.join(data_dir, "updates")
    if not path_within(manifest_path, update_root):
        raise UpdateError("The update manifest is outside the protected update area")
    if not path_within(manifest["staged_app"], update_root):
        raise UpdateError("The staged application is outside the protected update area")
    if not path_within(manifest["helper_path"], update_root):
        raise UpdateError("The install helper is outside the protected update area")
    if manifest["bundle_id"] != EXPECTED_BUNDLE_ID:
        raise UpdateError("The update manifest has an unexpected bundle identifier")
    if int(current_pid) <= 1:
        raise UpdateError("The running application process could not be identified")

    current_metadata = bundle_metadata(app_path)
    if current_metadata["bundle_id"] != EXPECTED_BUNDLE_ID:
        raise UpdateError("The installed application has an unexpected bundle identifier")
    if current_metadata["version"] != normalized_version(
        manifest["current_version"]
    ):
        raise UpdateError("The installed application changed after update preparation")
    staged_metadata = bundle_metadata(manifest["staged_app"])
    if staged_metadata["bundle_id"] != EXPECTED_BUNDLE_ID:
        raise UpdateError("The staged application has an unexpected bundle identifier")
    if staged_metadata["version"] != normalized_version(
        manifest["latest_version"]
    ):
        raise UpdateError("The staged application version is invalid")
    log_path = os.path.join(update_root, "install.log")
    log_handle = open(log_path, "a", encoding="utf-8")
    subprocess.Popen(
        [
            "/bin/bash",
            manifest["helper_path"],
            manifest["app_path"],
            manifest["staged_app"],
            manifest["data_dir"],
            manifest["bundle_id"],
            manifest["latest_version"],
            manifest["current_version"],
            str(int(current_pid)),
        ],
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log_handle.close()
    return {"status": "installing", "latest_version": manifest["latest_version"]}


def build_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--current-version", required=True)
    check_parser.add_argument("--repository", default=DEFAULT_REPOSITORY)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--current-version", required=True)
    prepare_parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    prepare_parser.add_argument("--app-path", required=True)
    prepare_parser.add_argument("--data-dir", required=True)

    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("--manifest", required=True)
    launch_parser.add_argument("--current-pid", required=True, type=int)
    return parser


def main():
    args = build_parser().parse_args()
    if args.action == "check":
        result = release_status(args.current_version, args.repository)
    elif args.action == "prepare":
        result = prepare_update(
            args.current_version,
            args.repository,
            args.app_path,
            args.data_dir,
        )
    else:
        result = launch_update(args.manifest, args.current_pid)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        sys.exit(1)
