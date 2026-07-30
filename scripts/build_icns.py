#!/usr/bin/env python3
"""Build a modern ICNS container from a standard macOS iconset directory."""

import argparse
import os
import struct


ICON_CHUNKS = (
    ("icp4", "icon_16x16.png"),
    ("icp5", "icon_32x32.png"),
    ("icp6", "icon_32x32@2x.png"),
    ("ic07", "icon_128x128.png"),
    ("ic08", "icon_256x256.png"),
    ("ic09", "icon_512x512.png"),
    ("ic10", "icon_512x512@2x.png"),
    ("ic11", "icon_16x16@2x.png"),
    ("ic12", "icon_32x32@2x.png"),
    ("ic13", "icon_128x128@2x.png"),
    ("ic14", "icon_256x256@2x.png"),
)


def build_icns(iconset_dir, destination):
    chunks = []
    for chunk_type, filename in ICON_CHUNKS:
        path = os.path.join(iconset_dir, filename)
        with open(path, "rb") as handle:
            payload = handle.read()
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"{filename} is not a PNG image")
        chunk = chunk_type.encode("ascii") + struct.pack(">I", len(payload) + 8) + payload
        chunks.append(chunk)
    body = b"".join(chunks)
    with open(destination, "wb") as handle:
        handle.write(b"icns")
        handle.write(struct.pack(">I", len(body) + 8))
        handle.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("iconset_dir")
    parser.add_argument("destination")
    args = parser.parse_args()
    build_icns(args.iconset_dir, args.destination)


if __name__ == "__main__":
    main()
