import pytest

from api.security import SlidingWindowRateLimiter, validate_production_security
from maestro.config import SecuritySettings
from maestro.security import InputRejected, sanitize_task


def test_api_keys_strip_surrounding_quotes(monkeypatch):
    # Pasting MAESTRO_API_KEYS='"abc","def"' (quotes included) must still match.
    monkeypatch.setenv("MAESTRO_API_KEYS", '"abc", "def"')
    from maestro.config import SecuritySettings

    sec = SecuritySettings()
    assert sec.api_keys == ["abc", "def"]


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
    assert rl.check_sync("c1")[0] is True
    assert rl.check_sync("c1")[0] is True
    allowed, retry, scope = rl.check_sync("c1")
    assert allowed is False
    assert scope == "minute"
    assert retry >= 1
    # Different client has its own bucket.
    assert rl.check_sync("c2")[0] is True


def test_rate_limiter_day_window():
    rl = SlidingWindowRateLimiter(per_minute=1000, per_day=1)
    assert rl.check_sync("c1")[0] is True
    allowed, _, scope = rl.check_sync("c1")
    assert allowed is False
    assert scope == "day"


@pytest.mark.asyncio
async def test_rate_limiter_async_api():
    rl = SlidingWindowRateLimiter(per_minute=1, per_day=100)
    assert (await rl.check("c1"))[0] is True
    assert (await rl.check("c1"))[0] is False


def test_build_rate_limiter_selects_backend(monkeypatch):
    from api.security import build_rate_limiter
    from maestro.config import SecuritySettings

    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    assert build_rate_limiter(SecuritySettings()).backend == "memory"

    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    assert build_rate_limiter(SecuritySettings()).backend == "upstash-redis"


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
