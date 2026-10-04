import json
from pathlib import Path

from research.strategy_registry import validate_experiment_declaration
from research.tier1_etf_replay import STRATEGIES
from research.universes import resolve_universe


ROOT = Path(__file__).resolve().parents[1] / "research"


def load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_phase007_is_locked_and_has_no_router_or_retuning():
    declaration = load("long-term-state-specialists-phase-007.json")
    assert declaration["status"] == "locked_before_return_analysis"
    assert declaration["generation_015_status"] == "paused_and_unchanged"
    assert declaration["design"]["router"] is False
    assert declaration["design"]["paper_trading"] is False
    assert declaration["design"]["parameter_retuning"] is False
    assert declaration["design"]["primary_market_state"] == "BEAR_DETERIORATING"
    assert declaration["design"]["acceptance_standard"]["minimum_episode_win_rate"] == 0.60


def test_phase007_jobs_are_synchronized_and_production_valid():
    jobs = [
        load("long-term-state-specialists-phase-007-prehistory-job.json"),
        load("long-term-state-specialists-phase-007-modern-job.json"),
    ]
    expected_strategies = jobs[0]["tier1_config"]["strategy_names"]
    assert len(expected_strategies) == 11
    assert set(expected_strategies) <= set(STRATEGIES)
    for job in jobs:
        declaration = validate_experiment_declaration(job["experiment"])
        config = job["tier1_config"]
        assert declaration["hypothesis_id"] == "LONG_TERM_STATE_SPECIALISTS_PHASE_007"
        assert job["research_evaluation"]["router"] is False
        assert job["research_evaluation"]["primary_market_state"] == "BEAR_DETERIORATING"
        assert config["strategy_names"] == expected_strategies
        assert config["universe_name"] == "ETF_LONG_TERM_STATE_SPECIALISTS_EXACT"
        assert config["market_state_universe_name"] == "ETF_LONG_TERM_STATE_CANONICAL"
        assert config["cost_ladder_bps"] == [1.0, 5.0, 10.0, 20.0]
        assert config["primary_cost_bps"] == 10.0
        assert resolve_universe(config["universe_name"])


def test_phase007_exact_universe_excludes_late_launch_assets():
    symbols = set(resolve_universe("ETF_LONG_TERM_STATE_SPECIALISTS_EXACT"))
    assert {"SPY", "BIL", "GLD", "DBC", "XBI", "SMH"} <= symbols
    assert not symbols.intersection({"XLC", "XLRE", "MTUM", "QUAL", "VLUE", "USMV"})
