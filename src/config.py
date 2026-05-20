"""전략 v0.2의 임계치/가중치 단일 출처. 변경 시 PATTERNS.md 룰북도 함께 갱신."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
# 전략 v0.2 원안은 $200M floor였지만 실제 알파 사례(BNAI $40M, BYND $80M, TNXP $60M,
# PAVS $25M)가 모두 그 아래에 위치. Pattern E(페니화)와 D(저float 스퀴즈)도 sub-$200M에서
# 빈번. Universe floor를 $20M으로 낮춰 사전 신호 후보를 확보. Tier 1 진입 시 추가
# 유동성 가드(평균 거래량 등)는 v0.3.
MARKET_CAP_MIN_USD = 20_000_000
MARKET_CAP_MAX_USD = 10_000_000_000

# ---------------------------------------------------------------------------
# PSS Tier 임계치 (ENV override 가능, W4 weight tuning 시 사용)
# ---------------------------------------------------------------------------
TIER1_PSS_MIN = float(os.environ.get("TIER1_PSS_MIN_OVERRIDE", 70.0))
TIER1_PATTERNS_MIN = int(os.environ.get("TIER1_PATTERNS_MIN_OVERRIDE", 2))
TIER1_MAX_TICKERS = 3

TIER2_PSS_MIN = float(os.environ.get("TIER2_PSS_MIN_OVERRIDE", 50.0))
TIER2_MAX_TICKERS = 5

TIER3_PSS_MIN = float(os.environ.get("TIER3_PSS_MIN_OVERRIDE", 30.0))
TIER3_MAX_TICKERS = 10

# ---------------------------------------------------------------------------
# 패턴 max_score (W4 튜닝 후 갱신, ENV override 지원)
# ---------------------------------------------------------------------------
PATTERN_A_MAX = float(os.environ.get("PATTERN_A_MAX_OVERRIDE", 30.0))
# W4 #5 finding: Russell 2000 편입 자체로는 5d 단위 alpha 거의 없음. n=704
# 표본에서 Pattern B 활성화 시 H4 Spearman 0.261 → 0.04 폭락 (alpha 희석).
# 5점으로 다운 = Tier 임계 영향 최소화하면서도 시그널 자체는 보존. v0.3에 별도 패턴
# 분리 (effective day 후 1주일 운용 = 다른 alpha) 검토.
PATTERN_B_MAX = float(os.environ.get("PATTERN_B_MAX_OVERRIDE", 5.0))
PATTERN_C_MAX = float(os.environ.get("PATTERN_C_MAX_OVERRIDE", 50.0))
PATTERN_D_MAX = float(os.environ.get("PATTERN_D_MAX_OVERRIDE", 30.0))
PATTERN_E_MAX = float(os.environ.get("PATTERN_E_MAX_OVERRIDE", 25.0))
PATTERN_F_MAX = float(os.environ.get("PATTERN_F_MAX_OVERRIDE", 25.0))
# v0.3 Pattern G — Volume Spike (RVOL). 24mo 데이터로 검증된 lift:
# RVOL>=5 → surge 발생 확률 4.6x, RVOL>=3 → 2.7x baseline.
PATTERN_G_MAX = float(os.environ.get("PATTERN_G_MAX_OVERRIDE", 20.0))
PATTERN_G_RVOL_LOOKBACK_DAYS = 30

# ---------------------------------------------------------------------------
# 보너스 / 페널티
# ---------------------------------------------------------------------------
BONUS_TOSS_TOP30 = 10.0
PENALTY_RECENT_RUN_PCT = 0.50
PENALTY_RECENT_RUN = -30.0
PENALTY_EARNINGS_DAYS = 7
PENALTY_EARNINGS = -20.0

# ---------------------------------------------------------------------------
# Pattern A — Dilution shutdown
# ---------------------------------------------------------------------------
PATTERN_A_KEYWORDS = (
    "atm termination",
    "at the market termination",
    "equity purchase agreement terminated",
    "standby equity",
    "equity distribution agreement terminated",
    "termination of sales agreement",
)
PATTERN_A_ITEMS = ("1.02",)
PATTERN_A_DILUTION_LOW_RATE = 0.05  # 6개월 5% 미만 → 보너스

# ---------------------------------------------------------------------------
# Pattern C — Contract
# ---------------------------------------------------------------------------
PATTERN_C_GOV_KEYWORDS = ("dod", "department of defense", "nih", "barda", "dtra", "nasa", "darpa")
PATTERN_C_RETAIL_KEYWORDS = ("walmart", "costco", "amazon", "target", "kroger", "home depot")
PATTERN_C_RATIOS = (
    (0.10, 50.0),
    (0.05, 35.0),
    (0.02, 20.0),
    (0.0, 10.0),
)

# ---------------------------------------------------------------------------
# Pattern D — Squeeze
# ---------------------------------------------------------------------------
SI_PCT_MIN = 0.15
DTC_MIN = 4.0
CTB_MIN = 0.30
FLOAT_MAX_M = 50.0  # M주
PRICE_DROP_30D_THRESHOLD = -0.30

# ---------------------------------------------------------------------------
# Pattern E — Brand penny
# ---------------------------------------------------------------------------
BRAND_PENNY_RECOVERY_MAX = 0.10
BRAND_PENNY_PRICE_MIN = 1.0
BRAND_PENNY_PRICE_MAX = 5.0
BRAND_PENNY_MENTIONS_FLOOR = 50

# ---------------------------------------------------------------------------
# Pattern F — Megatheme
# ---------------------------------------------------------------------------
MEGATHEME_KEYWORDS = (
    "ai", "artificial intelligence", "agentic", "llm",
    "quantum", "qubit",
    "glp-1", "glp1", "obesity drug",
    "fusion",
    "lithium", "uranium",
    "robotics", "humanoid",
    "space",
)
WSB_MENTION_GROWTH_MIN = 5.0  # 5x 증가율

# ---------------------------------------------------------------------------
# Entry trigger (자동화는 v0.3, v0.2는 알림용 임계치만)
# ---------------------------------------------------------------------------
ENTRY_RVOL_MIN = 2.0
ENTRY_CATALYST_FRESHNESS_HOURS = 72

# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
MAX_POSITION_PCT = 0.07
MAX_CONCURRENT_TIER1 = 3
DAILY_DRAWDOWN_HALT = -0.02
WEEKLY_DRAWDOWN_HALT = -0.05

# ---------------------------------------------------------------------------
# API rate limits
# ---------------------------------------------------------------------------
SEC_RPS = 10
# Polygon — 현재 키는 무료 티어이므로 5 calls / **60s** 로 한정. Stocks Starter
# ($29/월) 업그레이드 시 POLYGON_PERIOD_SECONDS=1 로 바꾸면 5 RPS unlimited.
# universe details enrichment(/v3/reference/tickers/{T})는 무료 티어에서 비현실 → Finnhub 우회 사용.
POLYGON_RPS = 5
POLYGON_PERIOD_SECONDS = 60.0
# Finnhub free tier: 60 calls/min steady → rps=1.
FINNHUB_RPS = 1
STOCKTWITS_RPH = 200


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class Settings:
    """런타임 설정. .env / 환경변수에서 로드."""

    polygon_api_key: str = ""
    anthropic_api_key: str = ""
    finnhub_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_alert_chat_id: str = ""
    sec_user_agent: str = "presurge-picker contact@example.com"
    database_url: str = "sqlite:///data/presurge.db"
    claude_report_model: str = "claude-haiku-4-5"
    claude_classify_model: str = "claude-sonnet-4-6"
    dry_run: bool = False
    log_level: str = "INFO"
    intraday_enabled: bool = False
    intraday_max_tickers: int = 20
    intraday_interval_seconds: int = 300
    intraday_use_yfinance: bool = True
    intraday_use_finnhub_fallback: bool = True
    intraday_min_tier: int = 3
    intraday_max_alerts_per_loop: int = 5
    intraday_buy_cooldown_minutes: int = 30
    intraday_regular_session_only: bool = True
    intraday_include_extended_hours: bool = False
    intraday_yfinance_prepost: bool = False
    intraday_quiet_start_kst: str = "03:00"
    intraday_quiet_end_kst: str = "06:00"
    intraday_mute_quiet_hours: bool = False
    missing_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> Settings:
        s = cls(
            polygon_api_key=os.environ.get("POLYGON_API_KEY", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            finnhub_api_key=os.environ.get("FINNHUB_API_KEY", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            telegram_alert_chat_id=os.environ.get("TELEGRAM_ALERT_CHAT_ID", ""),
            sec_user_agent=os.environ.get("SEC_USER_AGENT", "presurge-picker contact@example.com"),
            database_url=os.environ.get("DATABASE_URL", "sqlite:///data/presurge.db"),
            claude_report_model=os.environ.get("CLAUDE_REPORT_MODEL", "claude-haiku-4-5"),
            claude_classify_model=os.environ.get("CLAUDE_CLASSIFY_MODEL", "claude-sonnet-4-6"),
            dry_run=os.environ.get("DRY_RUN", "false").lower() in {"true", "1", "yes"},
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            intraday_enabled=_env_bool("INTRADAY_ENABLED", False),
            intraday_max_tickers=int(os.environ.get("INTRADAY_MAX_TICKERS", "20")),
            intraday_interval_seconds=int(os.environ.get("INTRADAY_INTERVAL_SECONDS", "300")),
            intraday_use_yfinance=_env_bool("INTRADAY_USE_YFINANCE", True),
            intraday_use_finnhub_fallback=_env_bool("INTRADAY_USE_FINNHUB_FALLBACK", True),
            intraday_min_tier=int(os.environ.get("INTRADAY_MIN_TIER", "3")),
            intraday_max_alerts_per_loop=int(os.environ.get("INTRADAY_MAX_ALERTS_PER_LOOP", "5")),
            intraday_buy_cooldown_minutes=int(os.environ.get("INTRADAY_BUY_COOLDOWN_MINUTES", "30")),
            intraday_regular_session_only=_env_bool("INTRADAY_REGULAR_SESSION_ONLY", True),
            intraday_include_extended_hours=_env_bool("INTRADAY_INCLUDE_EXTENDED_HOURS", False),
            intraday_yfinance_prepost=_env_bool("INTRADAY_YFINANCE_PREPOST", False),
            intraday_quiet_start_kst=os.environ.get("INTRADAY_QUIET_START_KST", "03:00"),
            intraday_quiet_end_kst=os.environ.get("INTRADAY_QUIET_END_KST", "06:00"),
            intraday_mute_quiet_hours=_env_bool("INTRADAY_MUTE_QUIET_HOURS", False),
        )
        for name in ("polygon_api_key", "anthropic_api_key", "telegram_bot_token", "telegram_chat_id"):
            if not getattr(s, name):
                s.missing_keys.append(name)
        return s
