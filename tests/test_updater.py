import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts.app_updater import (
    UpdateError,
    ensure_preservation_boundary,
    find_release_assets,
    normalized_version,
    parse_version,
    path_within,
    release_status,
    run_checked,
)


def sample_release(version="1.2.0", digest=True, checksum=False):
    dmg_name = f"Radio-Command-Center-{version}-arm64.dmg"
    assets = [
        {
            "name": dmg_name,
            "browser_download_url": f"https://example.test/{dmg_name}",
            "size": 1234,
            "digest": f"sha256:{'a' * 64}" if digest else None,
        }
    ]
    if checksum:
        assets.append(
            {
                "name": f"{dmg_name}.sha256",
                "browser_download_url": f"https://example.test/{dmg_name}.sha256",
            }
        )
    return {
        "tag_name": f"v{version}",
        "name": f"Radio Command Center {version}",
        "html_url": f"https://example.test/releases/v{version}",
        "published_at": "2026-07-30T12:00:00Z",
        "body": "Safe update",
        "assets": assets,
        "draft": False,
        "prerelease": False,
    }


class VersionTests(unittest.TestCase):
    def test_version_comparison_is_numeric(self):
        self.assertGreater(parse_version("v1.10.0"), parse_version("1.2.9"))
        self.assertEqual(normalized_version("v2.4"), "2.4.0")

    def test_newer_release_is_available(self):
        status = release_status("1.1.9", release=sample_release("1.2.0"))
        self.assertEqual(status["status"], "available")
        self.assertEqual(status["latest_version"], "1.2.0")

    def test_same_or_older_release_is_current(self):
        self.assertEqual(
            release_status("1.2.0", release=sample_release("1.2.0"))["status"],
            "current",
        )
        self.assertEqual(
            release_status("1.3.0", release=sample_release("1.2.0"))["status"],
            "current",
        )


class CommandOutputTests(unittest.TestCase):
    def test_structured_stdout_is_not_polluted_by_hdiutil_warning(self):
        with mock.patch(
            "scripts.app_updater.subprocess.run",
            return_value=SimpleNamespace(
                stdout="<plist><dict/></plist>",
                stderr="hdiutil: WARNING: deprecated",
            ),
        ):
            output = run_checked(["hdiutil", "attach", "-plist", "release.dmg"])

        self.assertEqual(output, "<plist><dict/></plist>")


class ReleaseValidationTests(unittest.TestCase):
    def test_github_asset_digest_is_accepted(self):
        version, asset, checksum, digest = find_release_assets(sample_release())
        self.assertEqual(version, "1.2.0")
        self.assertTrue(asset["name"].endswith("-arm64.dmg"))
        self.assertIsNone(checksum)
        self.assertEqual(digest, "a" * 64)

    def test_companion_checksum_is_accepted(self):
        version, _, checksum, digest = find_release_assets(
            sample_release(digest=False, checksum=True)
        )
        self.assertEqual(version, "1.2.0")
        self.assertTrue(checksum["name"].endswith(".sha256"))
        self.assertEqual(digest, "")

    def test_unsigned_release_is_rejected(self):
        with self.assertRaisesRegex(UpdateError, "checksum"):
            find_release_assets(sample_release(digest=False, checksum=False))

    def test_mismatched_asset_version_is_rejected(self):
        release = sample_release("1.2.0")
        release["assets"][0]["name"] = "Radio-Command-Center-9.9.9-arm64.dmg"
        with self.assertRaisesRegex(UpdateError, "must be named"):
            find_release_assets(release)


class PreservationBoundaryTests(unittest.TestCase):
    def test_app_and_data_must_be_separate(self):
        with tempfile.TemporaryDirectory() as root:
            app = os.path.join(root, "Radio Command Center.app")
            data = os.path.join(root, "data")
            os.makedirs(app)
            os.makedirs(data)
            checked_app, checked_data = ensure_preservation_boundary(app, data)
            self.assertEqual(checked_app, os.path.realpath(app))
            self.assertEqual(checked_data, os.path.realpath(data))

            nested_data = os.path.join(app, "data")
            os.makedirs(nested_data)
            with self.assertRaisesRegex(UpdateError, "separate"):
                ensure_preservation_boundary(app, nested_data)

    def test_manifest_paths_must_remain_in_update_area(self):
        with tempfile.TemporaryDirectory() as root:
            updates = os.path.join(root, "updates")
            os.makedirs(updates)
            self.assertTrue(path_within(os.path.join(updates, "manifest.json"), updates))
            self.assertFalse(path_within(os.path.join(root, "settings.json"), updates))


if __name__ == "__main__":
    unittest.main()
