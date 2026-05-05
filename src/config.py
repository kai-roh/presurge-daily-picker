"""전략 v0.2의 임계치/가중치 단일 출처. 변경 시 PATTERNS.md 룰북도 함께 갱신."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
MARKET_CAP_MIN_USD = 200_000_000
MARKET_CAP_MAX_USD = 10_000_000_000

# ---------------------------------------------------------------------------
# PSS Tier 임계치
# ---------------------------------------------------------------------------
TIER1_PSS_MIN = 70.0
TIER1_PATTERNS_MIN = 2
TIER1_MAX_TICKERS = 3

TIER2_PSS_MIN = 50.0
TIER2_MAX_TICKERS = 5

TIER3_PSS_MIN = 30.0
TIER3_MAX_TICKERS = 10

# ---------------------------------------------------------------------------
# 패턴 max_score (W4 튜닝 후 갱신)
# ---------------------------------------------------------------------------
PATTERN_A_MAX = 30.0  # Dilution shutdown
PATTERN_B_MAX = 25.0  # Index inclusion
PATTERN_C_MAX = 50.0  # Government / tier-1 contract
PATTERN_D_MAX = 30.0  # Short squeeze setup
PATTERN_E_MAX = 25.0  # Brand penny
PATTERN_F_MAX = 25.0  # Megatheme

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
POLYGON_RPS = 5
STOCKTWITS_RPH = 200


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
        )
        for name in ("polygon_api_key", "anthropic_api_key", "telegram_bot_token", "telegram_chat_id"):
            if not getattr(s, name):
                s.missing_keys.append(name)
        return s
