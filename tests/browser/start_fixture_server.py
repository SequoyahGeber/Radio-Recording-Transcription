#!/usr/bin/env python3
"""Start the Radio Command Center against an isolated browser-test fixture."""

import atexit
import os
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_MODULE_DIR = PROJECT_ROOT / "tests" / "fixtures"
for path in (PROJECT_ROOT, FIXTURE_MODULE_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from dashboard_fixture import create_fixture


def main():
    fixture_root = Path(
        os.environ.get(
            "RADIO_BROWSER_FIXTURE_ROOT",
            "/tmp/radio-command-center-browser-fixture",
        )
    ).resolve()
    create_fixture(fixture_root)

    if os.environ.get("RADIO_KEEP_BROWSER_FIXTURE") != "1":
        atexit.register(shutil.rmtree, fixture_root, True)

    from scripts.setup_security import generate_certificates
    from backend.config import TLS_CERT_PATH, TLS_KEY_PATH

    host = os.environ.get("RADIO_BROWSER_TEST_HOST", "127.0.0.1")
    port = int(os.environ.get("RADIO_BROWSER_TEST_PORT", "8765"))
    generate_certificates(host)

    import uvicorn
    from backend.server import app

    server_options = {
        "host": host,
        "port": port,
        "log_level": "warning",
    }
    if os.environ.get("RADIO_BROWSER_TEST_TLS", "1") != "0":
        server_options.update(
            {
                "ssl_certfile": TLS_CERT_PATH,
                "ssl_keyfile": TLS_KEY_PATH,
            }
        )
    uvicorn.run(app, **server_options)


if __name__ == "__main__":
    main()
