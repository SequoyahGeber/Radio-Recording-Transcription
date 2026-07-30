import os
import struct
import tempfile
import unittest

from scripts.build_icns import ICON_CHUNKS, build_icns


class IcnsBuildTests(unittest.TestCase):
    def test_builds_valid_container_with_all_modern_chunks(self):
        with tempfile.TemporaryDirectory() as root:
            iconset = os.path.join(root, "AppIcon.iconset")
            destination = os.path.join(root, "AppIcon.icns")
            os.makedirs(iconset)
            for _, filename in ICON_CHUNKS:
                with open(os.path.join(iconset, filename), "wb") as handle:
                    handle.write(b"\x89PNG\r\n\x1a\npayload")

            build_icns(iconset, destination)

            with open(destination, "rb") as handle:
                payload = handle.read()
            self.assertEqual(payload[:4], b"icns")
            self.assertEqual(struct.unpack(">I", payload[4:8])[0], len(payload))
            for chunk_type, _ in ICON_CHUNKS:
                self.assertIn(chunk_type.encode("ascii"), payload)


if __name__ == "__main__":
    unittest.main()
