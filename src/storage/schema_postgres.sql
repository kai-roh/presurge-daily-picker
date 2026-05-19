-- Pre-Surge Daily Picker schema for Supabase Postgres.
-- Keep logically aligned with schema.sql. Dates/timestamps are stored as ISO text
-- for compatibility with the existing SQLite-first application code.

CREATE TABLE IF NOT EXISTS universe (
    ticker              TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    market_cap_usd      DOUBLE PRECISION NOT NULL,
    float_shares        BIGINT,
    exchange            TEXT,
    sector              TEXT,
    is_common_stock     INTEGER NOT NULL DEFAULT 1,
    historical_max_mcap DOUBLE PRECISION,
    last_refreshed      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_universe_mcap ON universe(market_cap_usd);

CREATE TABLE IF NOT EXISTS daily_bars (
    ticker      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      BIGINT,
    vwap        DOUBLE PRECISION,
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
    classification_confidence DOUBLE PRECISION,
    contract_value_usd        DOUBLE PRECISION,
    counterparty              TEXT,
    key_quote                 TEXT,
    classified_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings(ticker, filed_at);
CREATE INDEX IF NOT EXISTS idx_filings_filed_at ON filings(filed_at);

CREATE TABLE IF NOT EXISTS short_interest (
    ticker         TEXT NOT NULL,
    settle_date    TEXT NOT NULL,
    si_shares      BIGINT,
    si_pct_float   DOUBLE PRECISION,
    days_to_cover  DOUBLE PRECISION,
    cost_to_borrow DOUBLE PRECISION,
    source         TEXT NOT NULL,
    PRIMARY KEY (ticker, settle_date, source)
);

CREATE TABLE IF NOT EXISTS social_mentions (
    ticker        TEXT NOT NULL,
    mention_date  TEXT NOT NULL,
    source        TEXT NOT NULL,
    mentions      BIGINT,
    bullish_pct   DOUBLE PRECISION,
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

CREATE TABLE IF NOT EXISTS index_inclusion_events (
    event_id      BIGSERIAL PRIMARY KEY,
    ticker        TEXT NOT NULL,
    index_name    TEXT NOT NULL,
    announced_at  TEXT,
    effective_at  TEXT,
    source        TEXT,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_index_events_ticker ON index_inclusion_events(ticker, effective_at);

CREATE TABLE IF NOT EXISTS pss_scores (
    score_date         TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    pattern_a          DOUBLE PRECISION NOT NULL DEFAULT 0,
    pattern_b          DOUBLE PRECISION NOT NULL DEFAULT 0,
    pattern_c          DOUBLE PRECISION NOT NULL DEFAULT 0,
    pattern_d          DOUBLE PRECISION NOT NULL DEFAULT 0,
    pattern_e          DOUBLE PRECISION NOT NULL DEFAULT 0,
    pattern_f          DOUBLE PRECISION NOT NULL DEFAULT 0,
    pattern_g          DOUBLE PRECISION NOT NULL DEFAULT 0,
    bonus_toss         DOUBLE PRECISION NOT NULL DEFAULT 0,
    penalty_run        DOUBLE PRECISION NOT NULL DEFAULT 0,
    penalty_earn       DOUBLE PRECISION NOT NULL DEFAULT 0,
    pss_total          DOUBLE PRECISION NOT NULL,
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

CREATE TABLE IF NOT EXISTS options_activity (
    snap_date       TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    expiry          TEXT,
    call_volume     BIGINT,
    put_volume      BIGINT,
    call_oi         BIGINT,
    put_oi          BIGINT,
    cp_volume_ratio DOUBLE PRECISION,
    PRIMARY KEY (snap_date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_options_date ON options_activity(snap_date);
CREATE INDEX IF NOT EXISTS idx_options_ticker ON options_activity(ticker, snap_date);

CREATE TABLE IF NOT EXISTS surge_events (
    surge_date     TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    surge_type     TEXT NOT NULL,
    surge_pct      DOUBLE PRECISION NOT NULL,
    prev_close     DOUBLE PRECISION,
    surge_high     DOUBLE PRECISION,
    surge_close    DOUBLE PRECISION,
    prev_pss_total DOUBLE PRECISION,
    prev_tier      INTEGER,
    prev_patterns  TEXT,
    was_picked     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (surge_date, ticker, surge_type)
);
CREATE INDEX IF NOT EXISTS idx_surge_date ON surge_events(surge_date);
CREATE INDEX IF NOT EXISTS idx_surge_ticker ON surge_events(ticker, surge_date);

CREATE TABLE IF NOT EXISTS trade_log (
    trade_id           BIGSERIAL PRIMARY KEY,
    ticker             TEXT NOT NULL,
    entry_date         TEXT NOT NULL,
    entry_price        DOUBLE PRECISION NOT NULL,
    entry_pss          DOUBLE PRECISION,
    entry_tier         INTEGER,
    triggered_patterns TEXT,
    exit_date          TEXT,
    exit_price         DOUBLE PRECISION,
    exit_reason        TEXT,
    size_pct_capital   DOUBLE PRECISION,
    pnl_pct            DOUBLE PRECISION,
    is_paper           INTEGER NOT NULL DEFAULT 0,
    notes              TEXT,
    pnl_high_1d_pct    DOUBLE PRECISION,
    pnl_close_1d_pct   DOUBLE PRECISION,
    pnl_high_2d_pct    DOUBLE PRECISION,
    pnl_close_2d_pct   DOUBLE PRECISION,
    pnl_high_3d_pct    DOUBLE PRECISION,
    pnl_close_3d_pct   DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_trade_ticker ON trade_log(ticker, entry_date);
CREATE INDEX IF NOT EXISTS idx_trade_date ON trade_log(entry_date);

CREATE TABLE IF NOT EXISTS signal_events (
    signal_id          BIGSERIAL PRIMARY KEY,
    signal_ts          TEXT NOT NULL,
    trade_date         TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    signal_type        TEXT NOT NULL,
    trigger_code       TEXT NOT NULL,
    price              DOUBLE PRECISION NOT NULL,
    ref_price          DOUBLE PRECISION,
    pss_total          DOUBLE PRECISION,
    tier               INTEGER,
    triggered_patterns TEXT,
    source             TEXT NOT NULL,
    metadata_json      TEXT,
    telegram_sent_at   TEXT,
    telegram_status    TEXT,
    UNIQUE(trade_date, ticker, signal_type, trigger_code, signal_ts)
);
CREATE INDEX IF NOT EXISTS idx_signal_date ON signal_events(trade_date, signal_ts);
CREATE INDEX IF NOT EXISTS idx_signal_ticker ON signal_events(ticker, trade_date);
CREATE INDEX IF NOT EXISTS idx_signal_type ON signal_events(trade_date, signal_type);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id          BIGINT PRIMARY KEY REFERENCES signal_events(signal_id),
    max_10m_pct        DOUBLE PRECISION,
    close_10m_pct      DOUBLE PRECISION,
    max_30m_pct        DOUBLE PRECISION,
    close_30m_pct      DOUBLE PRECISION,
    max_60m_pct        DOUBLE PRECISION,
    close_60m_pct      DOUBLE PRECISION,
    max_eod_pct        DOUBLE PRECISION,
    close_eod_pct      DOUBLE PRECISION,
    min_after_pct      DOUBLE PRECISION,
    evaluated_at       TEXT
);

ALTER TABLE universe ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_bars ENABLE ROW LEVEL SECURITY;
ALTER TABLE filings ENABLE ROW LEVEL SECURITY;
ALTER TABLE short_interest ENABLE ROW LEVEL SECURITY;
ALTER TABLE social_mentions ENABLE ROW LEVEL SECURITY;
ALTER TABLE toss_top_volume ENABLE ROW LEVEL SECURITY;
ALTER TABLE index_inclusion_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE pss_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE watchlist_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE options_activity ENABLE ROW LEVEL SECURITY;
ALTER TABLE surge_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE trade_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE signal_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE signal_outcomes ENABLE ROW LEVEL SECURITY;

