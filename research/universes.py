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
    # Stable, liquid ETF universes for the first low-cost research wave. ETF
    # membership is declared before each experiment and avoids reconstructing
    # historical index constituents before the point-in-time equity dataset is
    # ready.
    "ETF_MARKET_CORE": ("SPY", "QQQ", "IWM", "SHY"),
    "ETF_SECTOR_SPDR": (
        "XLC", "XLY", "XLP", "XLE", "XLF", "XLV",
        "XLI", "XLB", "XLRE", "XLK", "XLU",
    ),
    "ETF_TIER1_RESEARCH": (
        "SPY", "QQQ", "IWM", "SHY",
        "XLC", "XLY", "XLP", "XLE", "XLF", "XLV",
        "XLI", "XLB", "XLRE", "XLK", "XLU",
    ),
    "ETF_GENERATION_3": (
        "SPY", "QQQ", "IWM", "SHY", "IEF", "TLT", "GLD", "DBC",
        "EFA", "EEM", "VNQ", "XLC", "XLY", "XLP", "XLE", "XLF",
        "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU",
    ),
    "ETF_CROSS_ASSET_LONG_HISTORY": (
        "SPY", "QQQ", "IWM", "SHY", "IEF", "TLT", "GLD", "DBC",
        "EFA", "EEM", "VNQ",
    ),
    "ETF_TIER2_MULTI_SLEEVE": (
        "SPY", "SHY", "BIL", "IEF", "GLD",
        "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU",
        "MTUM", "QUAL", "VLUE", "USMV", "IWF", "IWD",
        "XBI", "XRT", "XHB", "XME", "XOP", "KRE", "SMH", "IYT",
    ),
}

ETF_METADATA = {
    "SPY": "broad_market", "QQQ": "large_cap_growth", "IWM": "small_cap",
    "SHY": "short_treasury", "XLC": "communication_services",
    "XLY": "consumer_discretionary", "XLP": "consumer_staples",
    "XLE": "energy", "XLF": "financials", "XLV": "healthcare",
    "XLI": "industrials", "XLB": "materials", "XLRE": "real_estate",
    "XLK": "technology", "XLU": "utilities",
    "IEF": "intermediate_treasury", "TLT": "long_treasury",
    "GLD": "gold", "DBC": "broad_commodities",
    "EFA": "developed_ex_us_equity", "EEM": "emerging_market_equity",
    "VNQ": "real_estate_broad",
    "BIL": "treasury_bills", "MTUM": "momentum_factor",
    "QUAL": "quality_factor", "VLUE": "value_factor",
    "USMV": "minimum_volatility_factor", "IWF": "growth_factor",
    "IWD": "value_style", "XBI": "biotechnology_industry",
    "XRT": "retail_industry", "XHB": "homebuilders_industry",
    "XME": "metals_mining_industry", "XOP": "oil_gas_industry",
    "KRE": "regional_banks_industry", "SMH": "semiconductors_industry",
    "IYT": "transportation_industry",
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
        sector = SECTORS.get(symbol, ETF_METADATA.get(symbol, "unclassified"))
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
        "selection_policy": (
            "fixed_predeclared_liquid_etf_research_roster"
            if all(symbol in ETF_METADATA for symbol in symbols)
            else "fixed_predeclared_liquid_large_cap_research_roster"
        ),
        "survivorship_bias_controlled": False,
        "constituent_survivorship_avoided": bool(
            symbols and all(symbol in ETF_METADATA for symbol in symbols)
        ),
        "limitation": (
            "ETF membership is fixed before the experiment, but fund inception, closure, "
            "benchmark changes, and asset-class availability still require date-aware checks."
            if symbols and all(symbol in ETF_METADATA for symbol in symbols) else
            "Fixed present-day membership can introduce survivorship bias in historical tests; "
            "results require confirmation with point-in-time membership data."
        ),
    }
