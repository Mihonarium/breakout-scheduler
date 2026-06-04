from app.auth import issue_token, read_token, SessionToken
from app.csrf import new_token, verify
from app.ratelimit import Limiter


def test_token_roundtrip():
    t = issue_token(42, "participant", "secret")
    assert read_token(t, "secret") == SessionToken(pid=42, scope="participant")


def test_token_wrong_secret_rejected():
    t = issue_token(42, "admin", "secret")
    assert read_token(t, "other") is None


def test_admin_scope_cannot_be_forged_from_participant_token():
    t = issue_token(7, "participant", "secret")
    tok = read_token(t, "secret")
    assert tok is not None and not tok.is_admin
    # tampering the payload breaks the signature
    assert read_token(t[:-3] + "abc", "secret") is None


def test_csrf_roundtrip_and_reject():
    t = new_token("secret")
    assert verify(t, "secret") is True
    assert verify(t, "other") is False
    assert verify(None, "secret") is False


def test_ratelimit_backoff_and_reset():
    lim = Limiter(base_s=1.0, cap_s=60.0)
    assert lim.allowed("ip1", now=0.0)
    lim.record_failure("ip1", now=0.0)        # blocked until 1.0
    assert not lim.allowed("ip1", now=0.5)
    assert lim.allowed("ip1", now=1.0)
    lim.record_failure("ip1", now=1.0)        # 2nd failure -> 2s
    assert not lim.allowed("ip1", now=2.5)
    lim.reset("ip1")
    assert lim.allowed("ip1", now=2.5)
