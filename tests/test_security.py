import pytest

from api.security import SlidingWindowRateLimiter, validate_production_security
from maestro.config import SecuritySettings
from maestro.security import InputRejected, sanitize_task


def test_sanitize_strips_control_chars():
    out = sanitize_task("hello\x07world", max_chars=100)
    assert out == "helloworld"


def test_sanitize_rejects_empty():
    with pytest.raises(InputRejected):
        sanitize_task("   ", max_chars=100)


def test_sanitize_rejects_nulls():
    with pytest.raises(InputRejected):
        sanitize_task("a\x00b", max_chars=100)


def test_sanitize_rejects_overlength():
    with pytest.raises(InputRejected):
        sanitize_task("x" * 101, max_chars=100)


def test_rate_limiter_minute_window():
    rl = SlidingWindowRateLimiter(per_minute=2, per_day=100)
    assert rl.check("c1")[0] is True
    assert rl.check("c1")[0] is True
    allowed, retry, scope = rl.check("c1")
    assert allowed is False
    assert scope == "minute"
    assert retry >= 1
    # Different client has its own bucket.
    assert rl.check("c2")[0] is True


def test_rate_limiter_day_window():
    rl = SlidingWindowRateLimiter(per_minute=1000, per_day=1)
    assert rl.check("c1")[0] is True
    allowed, _, scope = rl.check("c1")
    assert allowed is False
    assert scope == "day"


def test_production_security_flags(monkeypatch):
    monkeypatch.setenv("MAESTRO_ENV", "production")
    monkeypatch.setenv("MAESTRO_API_KEYS", "")
    monkeypatch.setenv("MAESTRO_CORS_ORIGINS", "*")
    monkeypatch.setenv("MAESTRO_ALLOW_MOCK", "true")
    monkeypatch.setenv("MAESTRO_SECRET_KEY", "")
    sec = SecuritySettings()
    problems = validate_production_security(sec)
    # All four misconfigurations should be flagged.
    assert len(problems) == 4
