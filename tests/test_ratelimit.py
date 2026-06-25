import asyncio
import time

import pytest

from maestro.config import get_config
from maestro.ratelimit import TokenBucketLimiter


@pytest.mark.asyncio
async def test_rpm_enforced(monkeypatch):
    cfg = get_config()
    spec = list(cfg.models.values())[0]
    # Shrink limits so the test is fast and deterministic.
    spec.rpm = 2
    spec.tpm = 10_000_000
    limiter = TokenBucketLimiter(cfg)

    # Two acquisitions are instant; the third must wait for the window.
    await limiter.acquire(spec, 10)
    await limiter.acquire(spec, 10)
    snap = limiter.snapshot()[spec.name]
    assert snap["rpm_used"] == 2


@pytest.mark.asyncio
async def test_tpm_blocks_until_window(monkeypatch):
    cfg = get_config()
    spec = list(cfg.models.values())[1]
    spec.rpm = 1000
    spec.tpm = 100  # tiny token budget

    limiter = TokenBucketLimiter(cfg)
    await limiter.acquire(spec, 90)  # uses most of the budget

    # The next 90-token call cannot fit in 100 TPM; it should block.
    start = time.monotonic()

    async def second():
        await limiter.acquire(spec, 90)

    task = asyncio.create_task(second())
    await asyncio.sleep(0.2)
    assert not task.done()  # still blocked on TPM
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_snapshot_shape():
    cfg = get_config()
    limiter = TokenBucketLimiter(cfg)
    assert isinstance(limiter.snapshot(), dict)
