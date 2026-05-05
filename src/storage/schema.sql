-- Pre-Surge Daily Picker schema v1
-- 모든 immutable snapshot은 (date, ticker) PK, ALTER로만 확장.

CREATE TABLE IF NOT EXISTS universe (
    ticker          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    market_cap_usd  REAL NOT NULL,
    float_shares    INTEGER,
    exchange        TEXT,
    sector          TEXT,
    is_common_stock INTEGER NOT NULL DEFAULT 1,
    historical_max_mcap REAL,
    last_refreshed  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_universe_mcap ON universe(market_cap_usd);

CREATE TABLE IF NOT EXISTS daily_bars (
    ticker      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      INTEGER,
    vwap        REAL,
    PRIMARY KEY (ticker, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_bars_date ON daily_bars(trade_date);

CREATE TABLE IF NOT EXISTS filings (
    accession_no              TEXT PRIMARY KEY,
    ticker                    TEXT NOT NULL,
    cik                       TEXT,
    filed_at                  TEXT NOT NULL,
    form_type                 TEXT NOT NULL,
    items                     TEXT,
    raw_text_url              TEXT,
    classification            TEXT,
    classification_confidence REAL,
    contract_value_usd        REAL,
    counterparty              TEXT,
    key_quote                 TEXT,
    classified_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings(ticker, filed_at);
CREATE INDEX IF NOT EXISTS idx_filings_filed_at ON filings(filed_at);

CREATE TABLE IF NOT EXISTS short_interest (
    ticker         TEXT NOT NULL,
    settle_date    TEXT NOT NULL,
    si_shares      INTEGER,
    si_pct_float   REAL,
    days_to_cover  REAL,
    cost_to_borrow REAL,
    source         TEXT NOT NULL,
    PRIMARY KEY (ticker, settle_date, source)
);

CREATE TABLE IF NOT EXISTS social_mentions (
    ticker        TEXT NOT NULL,
    mention_date  TEXT NOT NULL,
    source        TEXT NOT NULL,
    mentions      INTEGER,
    bullish_pct   REAL,
    rank          INTEGER,
    PRIMARY KEY (ticker, mention_date, source)
);

CREATE TABLE IF NOT EXISTS toss_top_volume (
    rank_date  TEXT NOT NULL,
    rank       INTEGER NOT NULL,
    ticker     TEXT NOT NULL,
    PRIMARY KEY (rank_date, rank)
);
CREATE INDEX IF NOT EXISTS idx_toss_ticker ON toss_top_volume(ticker, rank_date);

CREATE TABLE IF NOT EXISTS pss_scores (
    score_date         TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    pattern_a          REAL NOT NULL DEFAULT 0,
    pattern_b          REAL NOT NULL DEFAULT 0,
    pattern_c          REAL NOT NULL DEFAULT 0,
    pattern_d          REAL NOT NULL DEFAULT 0,
    pattern_e          REAL NOT NULL DEFAULT 0,
    pattern_f          REAL NOT NULL DEFAULT 0,
    bonus_toss         REAL NOT NULL DEFAULT 0,
    penalty_run        REAL NOT NULL DEFAULT 0,
    penalty_earn       REAL NOT NULL DEFAULT 0,
    pss_total          REAL NOT NULL,
    tier               INTEGER,
    triggered_patterns TEXT,
    metadata_json      TEXT,
    PRIMARY KEY (score_date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_pss_date_total ON pss_scores(score_date, pss_total DESC);

CREATE TABLE IF NOT EXISTS watchlist_runs (
    run_date     TEXT PRIMARY KEY,
    tier1_json   TEXT,
    tier2_json   TEXT,
    tier3_json   TEXT,
    report_md    TEXT,
    pushed_at    TEXT,
    push_status  TEXT
);

CREATE TABLE IF NOT EXISTS trade_log (
    trade_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker             TEXT NOT NULL,
    entry_date         TEXT NOT NULL,
    entry_price        REAL NOT NULL,
    entry_pss          REAL,
    entry_tier         INTEGER,
    triggered_patterns TEXT,
    exit_date          TEXT,
    exit_price         REAL,
    exit_reason        TEXT,
    size_pct_capital   REAL,
    pnl_pct            REAL,
    is_paper           INTEGER NOT NULL DEFAULT 0,
    notes              TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_ticker ON trade_log(ticker, entry_date);
