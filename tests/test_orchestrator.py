import pytest

from maestro.decision_log import to_export_dict
from maestro.orchestrator import Orchestrator
from maestro.schemas import Mode


def test_registry_builds_with_mock_disabled(monkeypatch):
    """Regression: build_registry must not crash when MAESTRO_ALLOW_MOCK=false.

    Previously dict.setdefault(..., _missing(name)) evaluated _missing eagerly and
    raised on every provider, crashing startup in production (mock off).
    """
    monkeypatch.setenv("MAESTRO_ALLOW_MOCK", "false")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    import maestro.config as cfg
    from maestro.providers import build_registry

    cfg.get_security.cache_clear()
    reg = build_registry(cfg.get_config())
    # groq has a key -> registered; gemini has none + mock off -> unregistered (fallback handles it).
    assert reg.for_model("groq") is not None
    cfg.get_security.cache_clear()


@pytest.mark.asyncio
async def test_conductor_run_completes():
    orch = Orchestrator()
    try:
        run = await orch.run("What is 2+2?", mode=Mode.conductor)
    finally:
        await orch.aclose()
    assert run.status == "complete"
    assert run.final_answer
    roles = [s.role for s in run.steps]
    assert "conductor" in roles
    assert "verifier" in roles
    assert "synthesizer" in roles


@pytest.mark.asyncio
async def test_single_mode_one_call():
    orch = Orchestrator()
    try:
        run = await orch.run("Say hi", mode=Mode.single)
    finally:
        await orch.aclose()
    assert run.status == "complete"
    assert len(run.steps) == 1
    assert run.steps[0].role == "worker"


@pytest.mark.asyncio
async def test_consensus_mode_aggregates():
    orch = Orchestrator()
    try:
        run = await orch.run("Name a color", mode=Mode.consensus)
    finally:
        await orch.aclose()
    assert run.status == "complete"
    roles = [s.role for s in run.steps]
    assert "proposer" in roles
    assert "aggregator" in roles


@pytest.mark.asyncio
async def test_export_schema_shape():
    orch = Orchestrator()
    try:
        run = await orch.run("hello", mode=Mode.conductor)
    finally:
        await orch.aclose()
    export = to_export_dict(run)
    for key in ("run_id", "task", "mode", "plan", "steps", "final_answer",
                "verification_status", "totals"):
        assert key in export
    assert export["totals"]["calls"] == len(run.steps)
