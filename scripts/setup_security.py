#!/usr/bin/env python3
import argparse
import base64
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

SCRIPT_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_PROJECT_ROOT not in sys.path:
    sys.path.insert(0, SCRIPT_PROJECT_ROOT)

from backend.config import (
    PROJECT_ROOT,
    SECURITY_CONFIG_PATH as CONFIG_PATH,
    SECURITY_DIR,
    TLS_CA_CERT_PATH as CA_CERT_PATH,
    TLS_CERT_PATH as SERVER_CERT_PATH,
    TLS_KEY_PATH as SERVER_KEY_PATH,
)


CA_KEY_PATH = os.path.join(SECURITY_DIR, "ca.key")


def encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii")


def password_hash(password, salt):
    import hashlib

    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )


def run(command):
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def generate_certificates(host):
    os.makedirs(SECURITY_DIR, exist_ok=True)
    if not os.path.exists(CA_CERT_PATH) or not os.path.exists(CA_KEY_PATH):
        run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes",
                "-keyout", CA_KEY_PATH, "-out", CA_CERT_PATH, "-days", "3650",
                "-sha256", "-subj", "/CN=Radio Dashboard Local CA",
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            ]
        )

    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    needs_server_cert = (
        not os.path.exists(SERVER_CERT_PATH)
        or not os.path.exists(SERVER_KEY_PATH)
        or config.get("tls_host") != host
    )
    if not needs_server_cert:
        return

    try:
        ipaddress.ip_address(host)
        primary_san = f"IP:{host}"
    except ValueError:
        primary_san = f"DNS:{host}"

    with tempfile.TemporaryDirectory() as temporary_dir:
        csr_path = os.path.join(temporary_dir, "server.csr")
        extension_path = os.path.join(temporary_dir, "server.ext")
        with open(extension_path, "w", encoding="utf-8") as handle:
            handle.write(
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n"
                f"subjectAltName={primary_san},IP:127.0.0.1,DNS:localhost\n"
            )
        run(
            [
                "openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
                "-keyout", SERVER_KEY_PATH, "-out", csr_path,
                "-subj", f"/CN={host}",
            ]
        )
        run(
            [
                "openssl", "x509", "-req", "-in", csr_path,
                "-CA", CA_CERT_PATH, "-CAkey", CA_KEY_PATH, "-CAcreateserial",
                "-out", SERVER_CERT_PATH, "-days", "825", "-sha256",
                "-extfile", extension_path,
            ]
        )


def write_credentials(host, port, username, password, destination):
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(
            "RADIO COMMAND CENTER LOGIN\n"
            "==========================\n\n"
            f"Dashboard: https://{host}:{port}\n"
            f"Username: {username}\n"
            f"Password: {password}\n\n"
            "Keep this file private. Install the accompanying CA certificate on each "
            "device that will use the dashboard, then mark it as trusted.\n"
        )
    os.chmod(destination, 0o600)
    certificate_destination = os.path.join(
        os.path.dirname(destination), "Radio Dashboard CA Certificate.crt"
    )
    shutil.copyfile(CA_CERT_PATH, certificate_destination)
    os.chmod(certificate_destination, 0o644)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--reset-password", action="store_true")
    parser.add_argument("--host", default=os.environ.get("RADIO_HOST", "127.0.0.1"))
    parser.add_argument("--port", default=os.environ.get("RADIO_PORT", "8000"))
    parser.add_argument(
        "--credentials-file",
        default=os.environ.get(
            "RADIO_CREDENTIALS_FILE",
            os.path.expanduser("~/Desktop/Radio Dashboard Login.txt"),
        ),
    )
    args = parser.parse_args()
    os.makedirs(SECURITY_DIR, exist_ok=True)
    generate_certificates(args.host)

    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            config = json.load(handle)

    password = None
    existing_users = config.get("users")
    if not existing_users and config.get("username"):
        existing_users = [
            {
                "username": config["username"],
                "display_name": "Administrator",
                "role": "admin",
                "active": True,
                "password_salt": config["password_salt"],
                "password_hash": config["password_hash"],
            }
        ]
    if args.reset_password:
        password = secrets.token_urlsafe(18)
        salt = secrets.token_bytes(16)
        if not existing_users:
            existing_users = []
        administrator = next(
            (user for user in existing_users if user.get("role") == "admin"),
            None,
        )
        if administrator is None:
            administrator = {"username": "operator"}
            existing_users.append(administrator)
        administrator.update(
            {
                "display_name": "Administrator",
                "role": "admin",
                "active": True,
                "password_salt": encode(salt),
                "password_hash": encode(password_hash(password, salt)),
            }
        )
        config["users"] = existing_users
    elif not existing_users:
        config["users"] = []
    else:
        config["users"] = existing_users

    config["session_secret"] = config.get(
        "session_secret", encode(secrets.token_bytes(32))
    )
    config["internal_token"] = config.get(
        "internal_token", secrets.token_urlsafe(32)
    )
    config["created_at"] = config.get("created_at", datetime.now().isoformat())
    for legacy_key in ("username", "password_salt", "password_hash"):
        config.pop(legacy_key, None)

    config["tls_host"] = args.host
    temporary_path = f"{CONFIG_PATH}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)
    os.chmod(CA_KEY_PATH, 0o600)
    os.chmod(SERVER_KEY_PATH, 0o600)

    if password:
        write_credentials(
            args.host,
            args.port,
            administrator["username"],
            password,
            args.credentials_file,
        )
        print(f"[SECURITY] Initial login saved to: {args.credentials_file}")
    print(f"[SECURITY] HTTPS certificate ready for {args.host}.")


if __name__ == "__main__":
    main()
