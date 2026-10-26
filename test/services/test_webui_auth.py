import pytest

from app.services import webui_auth as auth


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.delenv("MPT_WEBUI_PASSWORD", raising=False)
    monkeypatch.delenv("MPT_WEBUI_USERNAME", raising=False)
    monkeypatch.setattr(auth, "_failures", [])
    for key in ("webui_password_hash", "webui_username"):
        monkeypatch.delitem(auth.config.ui, key, raising=False)


def test_hash_round_trip_and_salt():
    first = auth.hash_password("s3nha-forte", iterations=1000)
    second = auth.hash_password("s3nha-forte", iterations=1000)
    assert first != second
    assert first.startswith("pbkdf2_sha256$1000$")
    assert auth.verify_hash("s3nha-forte", first)
    assert not auth.verify_hash("errada", first)
    assert not auth.verify_hash("s3nha-forte", "garbage")
    assert not auth.verify_hash("s3nha-forte", "md5$1$a$b")


def test_disabled_without_password():
    assert not auth.is_enabled()


def test_config_hash_login(monkeypatch):
    monkeypatch.setitem(auth.config.ui, "webui_password_hash", auth.hash_password("abcdefgh", 1000))
    monkeypatch.setitem(auth.config.ui, "webui_username", "william")
    assert auth.is_enabled()
    assert not auth.is_managed_by_environment()
    assert auth.check_credentials("william", "abcdefgh")
    assert not auth.check_credentials("admin", "abcdefgh")
    assert not auth.check_credentials("william", "wrong")


def test_environment_password_takes_precedence(monkeypatch):
    monkeypatch.setitem(auth.config.ui, "webui_password_hash", auth.hash_password("config-pass", 1000))
    monkeypatch.setenv("MPT_WEBUI_PASSWORD", "env-pass")
    assert auth.is_managed_by_environment()
    assert auth.configured_username() == "admin"
    assert auth.check_credentials("admin", "env-pass")
    assert not auth.check_credentials("admin", "config-pass")


def test_lockout_after_repeated_failures(monkeypatch):
    monkeypatch.setenv("MPT_WEBUI_PASSWORD", "env-pass")
    for _ in range(auth.MAX_FAILURES):
        assert not auth.check_credentials("admin", "nope")
    assert auth.lockout_remaining() > 0
    # Even the right password is refused while locked out.
    assert not auth.check_credentials("admin", "env-pass")
    future = auth.time.monotonic() + auth.LOCKOUT_SECONDS + 1
    assert auth.lockout_remaining(now=future) == 0


@pytest.mark.parametrize(
    "password, confirmation, error",
    [("short", "short", "too_short"), ("abcdefgh", "abcdefgX", "mismatch"), ("abcdefgh", "abcdefgh", None)],
)
def test_new_password_validation(password, confirmation, error):
    assert auth.validate_new_password(password, confirmation) == error
