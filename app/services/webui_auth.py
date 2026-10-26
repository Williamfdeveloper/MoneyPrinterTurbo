"""Optional password protection for the WebUI.

Configure it in one of two ways (the environment wins):

* ``MPT_WEBUI_PASSWORD`` (and optionally ``MPT_WEBUI_USERNAME``) environment
  variables, convenient for Docker, Coolify or Easypanel;
* ``[ui] webui_username`` and ``webui_password_hash`` in config.toml, set from
  the WebUI. Only a salted PBKDF2 hash is stored, never the password.

Without either, the WebUI stays open as before.
"""

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

from app.config import config

HASH_ALGORITHM = "pbkdf2_sha256"
HASH_ITERATIONS = 310_000
DEFAULT_USERNAME = "admin"
MIN_PASSWORD_LENGTH = 8
# Failed logins share one budget per process to slow down guessing.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60

_failures_lock = threading.Lock()
_failures: list[float] = []


def hash_password(password: str, iterations: int = HASH_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        (
            HASH_ALGORITHM,
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_hash(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = str(encoded).split("$")
        if algorithm != HASH_ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _env_password() -> str:
    return os.environ.get("MPT_WEBUI_PASSWORD", "")


def configured_username() -> str:
    if _env_password():
        return os.environ.get("MPT_WEBUI_USERNAME", "").strip() or DEFAULT_USERNAME
    return str(config.ui.get("webui_username", "") or DEFAULT_USERNAME).strip()


def is_enabled() -> bool:
    return bool(_env_password() or config.ui.get("webui_password_hash"))


def is_managed_by_environment() -> bool:
    return bool(_env_password())


def lockout_remaining(now: float | None = None) -> int:
    now = time.monotonic() if now is None else now
    with _failures_lock:
        recent = [moment for moment in _failures if now - moment < LOCKOUT_SECONDS]
        _failures[:] = recent
        if len(recent) < MAX_FAILURES:
            return 0
        return int(LOCKOUT_SECONDS - (now - min(recent))) + 1


def check_credentials(username: str, password: str) -> bool:
    """Validate a login; failures count towards a short lockout."""
    if lockout_remaining() > 0:
        return False
    username_ok = hmac.compare_digest(
        (username or "").strip().encode("utf-8"), configured_username().encode("utf-8")
    )
    env_password = _env_password()
    if env_password:
        password_ok = hmac.compare_digest(
            (password or "").encode("utf-8"), env_password.encode("utf-8")
        )
    else:
        password_ok = verify_hash(password or "", config.ui.get("webui_password_hash", ""))
    if username_ok and password_ok:
        return True
    with _failures_lock:
        _failures.append(time.monotonic())
    return False


def validate_new_password(password: str, confirmation: str) -> str | None:
    """Return an error code for a weak or mismatched password, else None."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return "too_short"
    if password != confirmation:
        return "mismatch"
    return None
