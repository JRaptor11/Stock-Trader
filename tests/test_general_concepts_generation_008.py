import json
import unittest
from pathlib import Path
from research.tier1_etf_replay import DEFENSIVE_ETFS, SECTOR_ETFS, Tier1Config, _targets

ROOT=Path(__file__).resolve().parents[1]/"research"
CANDIDATES=("CROSS_ASSET_RELATIVE_MOMENTUM_DEFENSIVE","TREND_VOLATILITY_SCALED_EQUITY","DEFENSIVE_TREND_PERSISTENCE","BREADTH_DIVERGENCE_DEFENSIVE","RELATIVE_STRENGTH_BREAKOUT","CONFIRMED_CRASH_RECOVERY")

class Generation008Tests(unittest.TestCase):
    def test_declaration_matches_job_and_is_isolated(self):
        declaration=json.loads((ROOT/"general-concepts-generation-008.json").read_text()); job=json.loads((ROOT/"general-concepts-generation-008-job.json").read_text())
        self.assertEqual(set(CANDIDATES),{row["strategy"] for row in declaration["candidates"]})
        self.assertTrue(set(CANDIDATES)<=set(job["tier1_config"]["strategy_names"]))
        self.assertTrue(all(declaration["constraints"].values()))
    def test_candidates_are_long_only_and_normalized(self):
        symbols=("SPY","SHY","QQQ","IWM","TLT","IEF","GLD","DBC","EFA","EEM","VNQ","BIL")+SECTOR_ETFS
        histories={s:[100+i*(.03+j*.001) for i in range(300)] for j,s in enumerate(symbols)}
        config=Tier1Config(universe_name="ETF_GENERAL_CONCEPTS")
        for strategy in CANDIDATES:
            target=_targets(strategy,histories,config)
            self.assertAlmostEqual(1.0,sum(target.values())); self.assertTrue(all(v>=0 for v in target.values()))

if __name__=="__main__": unittest.main()
