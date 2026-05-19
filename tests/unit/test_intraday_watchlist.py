from __future__ import annotations

from datetime import date

from src.intraday.watchlist import load_intraday_watchlist


def test_load_intraday_watchlist_caps_and_prioritizes_pattern_g(db, seed_universe):
    run_date = date(2026, 5, 18)
    rows = []
    tier3 = []
    for i in range(25):
        ticker = f"T{i:02d}"
        seed_universe(ticker)
        patterns = "E,G" if i == 19 else "D,E"
        tier = 2 if i < 5 else 3
        pss = 60 - i if tier == 2 else 40 + i / 100
        db.upsert_pss(run_date, ticker, {
            "pattern_a": 0,
            "pattern_b": 0,
            "pattern_c": 0,
            "pattern_d": 20 if "D" in patterns else 0,
            "pattern_e": 20 if "E" in patterns else 0,
            "pattern_f": 0,
            "pattern_g": 10 if "G" in patterns else 0,
            "bonus_toss": 0,
            "penalty_run": 0,
            "penalty_earn": 0,
            "pss_total": pss,
            "tier": tier,
            "triggered_patterns": patterns,
            "metadata_json": {},
        })
        item = {"ticker": ticker}
        if tier == 2:
            rows.append(item)
        else:
            tier3.append(item)

    db.save_watchlist_run(run_date, [], rows, tier3, "report")

    out = load_intraday_watchlist(db, run_date, max_tickers=20)

    assert len(out) == 20
    assert [c.ticker for c in out[:5]] == [f"T{i:02d}" for i in range(5)]
    assert "T19" in [c.ticker for c in out[5:8]]

