"""Versioned, research-only candidate universes.

Membership is fixed before an experiment and recorded in its manifest. These
lists are not claims that a security will outperform; they are liquid,
large-cap candidates selected to broaden sector representation.
"""

BASELINE_10 = (
    "AAPL", "AMD", "AMZN", "AVGO", "COST",
    "GOOGL", "META", "MSFT", "NVDA", "TSLA",
)

DIVERSIFIED_20 = BASELINE_10 + (
    "CRM", "HD", "JPM", "LLY", "NFLX",
    "ORCL", "UNH", "V", "WMT", "XOM",
)

DIVERSIFIED_30 = DIVERSIFIED_20 + (
    "ABBV", "CAT", "GE", "IBM", "KO",
    "MA", "MCD", "PEP", "QCOM", "TXN",
)

UNIVERSES = {
    "BASELINE_10": BASELINE_10,
    "DIVERSIFIED_20": DIVERSIFIED_20,
    "DIVERSIFIED_30": DIVERSIFIED_30,
}

SECTORS = {
    "AAPL": "technology", "AMD": "technology", "AMZN": "consumer_discretionary",
    "AVGO": "technology", "COST": "consumer_staples", "GOOGL": "communication_services",
    "META": "communication_services", "MSFT": "technology", "NVDA": "technology",
    "TSLA": "consumer_discretionary", "CRM": "technology", "HD": "consumer_discretionary",
    "JPM": "financials", "LLY": "healthcare", "NFLX": "communication_services",
    "ORCL": "technology", "UNH": "healthcare", "V": "financials",
    "WMT": "consumer_staples", "XOM": "energy", "ABBV": "healthcare",
    "CAT": "industrials", "GE": "industrials", "IBM": "technology",
    "KO": "consumer_staples", "MA": "financials", "MCD": "consumer_discretionary",
    "PEP": "consumer_staples", "QCOM": "technology", "TXN": "technology",
}


def resolve_universe(name: str) -> tuple[str, ...]:
    key = str(name or "").strip().upper()
    if not key:
        return ()
    if key not in UNIVERSES:
        raise ValueError(f"unknown research universe: {name}")
    return UNIVERSES[key]


def universe_metadata(name: str, symbols: list[str] | tuple[str, ...]) -> dict:
    sector_counts = {}
    for symbol in symbols:
        sector = SECTORS.get(symbol, "unclassified")
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
    count = len(symbols)
    return {
        "universe_name": name or "CUSTOM",
        "symbol_count": count,
        "symbols": list(symbols),
        "sector_counts": dict(sorted(sector_counts.items())),
        "largest_sector_count": max(sector_counts.values(), default=0),
        "largest_sector_pct": round(
            max(sector_counts.values(), default=0) / count * 100.0, 4
        ) if count else 0.0,
        "selection_policy": "fixed_predeclared_liquid_large_cap_research_roster",
        "survivorship_bias_controlled": False,
        "limitation": (
            "Fixed present-day membership can introduce survivorship bias in historical tests; "
            "results require confirmation with point-in-time membership data."
        ),
    }
