import json
from pathlib import Path

from research.strategy_registry import validate_experiment_declaration
from research.universes import resolve_universe


ROOT = Path(__file__).resolve().parents[1] / "research"


def load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_phase006_is_locked_exact_and_router_free():
    declaration = load("long-term-prehistory-validation-006.json")
    assert declaration["status"] == "locked_before_return_analysis"
    assert declaration["generation_015_status"] == "paused_and_unchanged"
    assert declaration["design"]["router"] is False
    assert declaration["design"]["parameter_retuning"] is False
    assert declaration["design"]["proxy_rule"].startswith("Proxy or reconstructed")
    excluded = declaration["excluded_primary_concepts"][0]["strategies"]
    assert {"VALUE_QUALITY_STATIC", "MULTIFACTOR_STATIC", "FACTOR_ETF_MOMENTUM"} <= set(excluded)


def test_phase006_jobs_preserve_costs_states_and_exact_windows():
    expected = {
        "cross-asset": ("CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE", "2007-05-30"),
        "industry": ("INDUSTRY_ETF_MOMENTUM", "2006-06-22"),
        "sector": ("SECTOR_ETF_ROTATION", "2002-07-30"),
        "inflation": ("STATIC_INFLATION_AWARE", "2006-02-06"),
    }
    for suffix, (strategy, start) in expected.items():
        job = load(f"long-term-prehistory-validation-006-{suffix}-job.json")
        config = job["tier1_config"]
        declaration = validate_experiment_declaration(job["experiment"])
        assert declaration["hypothesis_id"] == "LONG_TERM_PREHISTORY_VALIDATION_006"
        assert job["experiment"]["hypothesis_id"] == "LONG_TERM_PREHISTORY_VALIDATION_006"
        assert job["research_evaluation"]["router"] is False
        assert job["research_evaluation"]["evaluate_all_hierarchical_states"] is True
        assert config["strategy_names"] == ["SPY_BUY_HOLD", strategy]
        assert config["required_common_start_date"] == start
        assert config["cost_ladder_bps"] == [1.0, 5.0, 10.0, 20.0]
        assert config["primary_cost_bps"] == 10.0
        assert config["market_state_universe_name"] == "ETF_LONG_TERM_STATE_CANONICAL"
        assert resolve_universe(config["universe_name"])


def test_phase006_primary_universes_exclude_late_launch_factor_and_sector_etfs():
    for name in (
        "ETF_LONG_TERM_PREHISTORY_SECTOR",
        "ETF_LONG_TERM_PREHISTORY_INDUSTRY",
        "ETF_LONG_TERM_PREHISTORY_CROSS_ASSET",
        "ETF_LONG_TERM_PREHISTORY_INFLATION",
    ):
        symbols = set(resolve_universe(name))
        assert "XLC" not in symbols
        assert "XLRE" not in symbols
        assert not symbols.intersection({"MTUM", "QUAL", "VLUE", "USMV"})
