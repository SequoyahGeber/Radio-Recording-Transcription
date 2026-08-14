import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import tempfile

from backend.config import SECURITY_CONFIG_PATH, bool_setting, number_setting


SESSION_COOKIE = "radio_session"
# Browser sessions last a month by default and renew themselves while the
# dashboard is in use, so an operator is not signed out mid-shift.
SESSION_SECONDS = 30 * 24 * 60 * 60
MINIMUM_SESSION_SECONDS = 60 * 60
APP_KEY_COOKIE = "radio_app_key"
APP_KEY_HEADER = "X-Radio-App-Key"
BUILTIN_APP_USERNAME = "mac-app"
BUILTIN_APP_DISPLAY_NAME = "This Mac"
_config_lock = threading.RLock()
_setup_lock = threading.Lock()
_app_key_lock = threading.Lock()
_config_cache = None
_config_mtime = None
ROLE_LEVELS = {
    "viewer": 1,
    "operator": 2,
    "supervisor": 3,
    "admin": 4,
}


def session_lifetime_seconds():
    hours = number_setting(
        "session_lifetime_hours",
        "RADIO_SESSION_LIFETIME_HOURS",
        default=SESSION_SECONDS // 3600,
        minimum=1,
        maximum=24 * 3650,
    )
    return max(MINIMUM_SESSION_SECONDS, hours * 3600)


def _decode(value):
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii")


def load_security_config():
    global _config_cache, _config_mtime
    try:
        modified = os.path.getmtime(SECURITY_CONFIG_PATH)
    except OSError as exc:
        raise RuntimeError(
            "Security configuration is missing. Run scripts/setup_security.py."
        ) from exc

    with _config_lock:
        if _config_cache is None or modified != _config_mtime:
            with open(SECURITY_CONFIG_PATH, "r", encoding="utf-8") as handle:
                _config_cache = json.load(handle)
            _config_mtime = modified
        return dict(_config_cache)


def save_security_config(config):
    global _config_cache, _config_mtime
    directory = os.path.dirname(SECURITY_CONFIG_PATH)
    os.makedirs(directory, exist_ok=True)
    with _config_lock:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix="security-",
            suffix=".json",
            dir=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=2, sort_keys=True)
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, SECURITY_CONFIG_PATH)
            os.chmod(SECURITY_CONFIG_PATH, 0o600)
            _config_cache = dict(config)
            _config_mtime = os.path.getmtime(SECURITY_CONFIG_PATH)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise


def _configured_users(config):
    users = config.get("users")
    if isinstance(users, list):
        return [dict(user) for user in users if isinstance(user, dict)]
    if config.get("username"):
        return [
            {
                "username": config["username"],
                "display_name": config.get("username", "Operator"),
                "role": "admin",
                "active": True,
                "password_salt": config["password_salt"],
                "password_hash": config["password_hash"],
            }
        ]
    return []


def list_users():
    result = []
    for user in _configured_users(load_security_config()):
        result.append(
            {
                "username": user.get("username", ""),
                "display_name": user.get("display_name") or user.get("username", ""),
                "role": user.get("role", "viewer"),
                "active": user.get("active", True),
            }
        )
    return result


def setup_required():
    return not _configured_users(load_security_config())


def find_user(username):
    for user in _configured_users(load_security_config()):
        if hmac.compare_digest(
            str(user.get("username", "")).encode("utf-8"),
            str(username or "").encode("utf-8"),
        ):
            return user
    return None


def hash_password(password, salt=None):
    salt_bytes = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return _encode(salt_bytes), _encode(digest)


def authenticate_user(username, password):
    user = find_user(username)
    if not user or not user.get("active", True):
        return None
    _, candidate_hash = hash_password(password, _decode(user["password_salt"]))
    if not hmac.compare_digest(candidate_hash, user["password_hash"]):
        return None
    return user


def verify_password(username, password):
    return authenticate_user(username, password) is not None


def create_session_token(user, lifetime_seconds=None):
    config = load_security_config()
    if isinstance(user, str):
        user = find_user(user) or {"username": user, "role": "viewer"}
    lifetime = int(lifetime_seconds or session_lifetime_seconds())
    payload = {
        "u": user["username"],
        "r": user.get("role", "viewer"),
        "exp": int(time.time()) + lifetime,
        "n": secrets.token_urlsafe(12),
    }
    encoded_payload = _encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        _decode(config["session_secret"]),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_encode(signature)}"


def validate_session_token(token):
    if not token or "." not in token:
        return None
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        config = load_security_config()
        expected = hmac.new(
            _decode(config["session_secret"]),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, _decode(encoded_signature)):
            return None
        payload = json.loads(_decode(encoded_payload))
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        user = find_user(str(payload.get("u", "")))
        if not user or not user.get("active", True):
            return None
        payload["r"] = user.get("role", "viewer")
        payload["display_name"] = user.get("display_name") or user["username"]
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def rotate_session_secret():
    """Invalidate every signed-in browser at once.

    Sessions are signed with this secret, so replacing it makes every cookie
    already issued unverifiable. The native Mac app is unaffected: it
    authenticates with its own key on every request rather than with a session.
    """
    config = load_security_config()
    config["session_secret"] = _encode(secrets.token_bytes(32))
    config["sessions_reset_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_security_config(config)
    return config["sessions_reset_at"]


def app_sign_in_enabled():
    """Whether the native Mac app may sign itself in without a password."""
    return bool_setting("app_auto_sign_in", "RADIO_APP_AUTO_SIGN_IN", default=True)


def ensure_app_key():
    """Return the native app's private key, creating it on first use.

    The key lives in security.json, which is readable only by the Mac account
    that runs the services. The native app reads it directly from disk, so the
    app can authenticate itself without ever prompting for a password, while a
    browser - which cannot read that file - still has to sign in normally.
    """
    with _app_key_lock:
        config = load_security_config()
        existing = config.get("app_key")
        if isinstance(existing, str) and len(existing) >= 32:
            return existing
        key = secrets.token_urlsafe(32)
        config["app_key"] = key
        save_security_config(config)
        return key


def app_key_matches(candidate):
    if not candidate or not app_sign_in_enabled():
        return False
    try:
        expected = load_security_config().get("app_key")
    except RuntimeError:
        return False
    if not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(str(candidate).encode("utf-8"), expected.encode("utf-8"))


def app_identity():
    """The profile the native Mac app acts as.

    It prefers the administrator recorded as the app owner, then any active
    administrator, and finally a built-in local identity so a brand new
    installation is usable in the app before any profile exists.
    """
    config = load_security_config()
    users = _configured_users(config)
    administrators = [
        user
        for user in users
        if user.get("active", True) and user.get("role") == "admin"
    ]
    preferred = config.get("app_user")
    for user in administrators:
        if user.get("username") == preferred:
            return user
    if administrators:
        return administrators[0]
    return {
        "username": BUILTIN_APP_USERNAME,
        "display_name": BUILTIN_APP_DISPLAY_NAME,
        "role": "admin",
        "active": True,
    }


def app_session(candidate_key):
    """Build a session for a request that carries the native app's key."""
    if not app_key_matches(candidate_key):
        return None
    identity = app_identity()
    return {
        "u": identity["username"],
        "r": "admin",
        "display_name": identity.get("display_name") or identity["username"],
        "via": "app",
    }


def verify_internal_token(candidate):
    expected = load_security_config()["internal_token"]
    return bool(candidate) and hmac.compare_digest(
        candidate.encode("utf-8"), expected.encode("utf-8")
    )


def role_allows(session, minimum_role):
    if not session:
        return False
    return ROLE_LEVELS.get(session.get("r", "viewer"), 0) >= ROLE_LEVELS[minimum_role]


def upsert_user(username, display_name, role, password=None, active=True):
    username = (username or "").strip()
    if not username or role not in ROLE_LEVELS:
        raise ValueError("A valid username and role are required")
    if password and len(password) < 12:
        raise ValueError("Passwords must contain at least 12 characters")
    config = load_security_config()
    users = _configured_users(config)
    existing = next((user for user in users if user.get("username") == username), None)
    if existing is None:
        if not password:
            raise ValueError("A password is required for a new profile")
        salt, digest = hash_password(password)
        existing = {
            "username": username,
            "password_salt": salt,
            "password_hash": digest,
        }
        users.append(existing)
    elif password:
        salt, digest = hash_password(password)
        existing["password_salt"] = salt
        existing["password_hash"] = digest
    existing.update(
        {
            "display_name": (display_name or username).strip(),
            "role": role,
            "active": bool(active),
        }
    )
    if not any(user.get("active", True) and user.get("role") == "admin" for user in users):
        raise ValueError("At least one active administrator is required")
    config["users"] = users
    for legacy_key in ("username", "password_salt", "password_hash"):
        config.pop(legacy_key, None)
    save_security_config(config)
    return next(user for user in list_users() if user["username"] == username)


def create_initial_admin(username, display_name, password):
    username = (username or "").strip()
    display_name = (display_name or "").strip()
    if not 3 <= len(username) <= 64:
        raise ValueError("Username must contain between 3 and 64 characters")
    if not all(character.isalnum() or character in "._-" for character in username):
        raise ValueError("Username may only contain letters, numbers, dots, dashes, and underscores")
    if not display_name:
        raise ValueError("Administrator name is required")
    if len(display_name) > 100:
        raise ValueError("Administrator name must contain 100 characters or fewer")
    if len(password or "") < 12:
        raise ValueError("Password must contain at least 12 characters")

    with _setup_lock:
        config = load_security_config()
        if _configured_users(config):
            raise ValueError("Initial setup has already been completed")
        salt, digest = hash_password(password)
        config["users"] = [
            {
                "username": username,
                "display_name": display_name,
                "role": "admin",
                "active": True,
                "password_salt": salt,
                "password_hash": digest,
            }
        ]
        for legacy_key in ("username", "password_salt", "password_hash"):
            config.pop(legacy_key, None)
        save_security_config(config)
    return list_users()[0]
