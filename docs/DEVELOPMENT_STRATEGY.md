# Pre-Surge Daily Picker — 상세 개발 전략 (v0.2 기반)

작성일: 2026-05-05
대상 전략 문서: 토스앱 운용 미국 중소형주 Pre-Surge Daily Picker v0.2
개발 목표: 매일 한국시간 09:00 KST에 5~10종목 watchlist를 자동 산출하는 일 1회 배치 시스템 구축
실행 환경: GitHub Actions cron (서버리스), Python 3.11, SQLite/Supabase, Telegram 푸시
총 개발 기간: 8주 (W1~W8, 전략 문서의 로드맵과 정합)

---

## 0. 개발 원칙과 비목표 (Non-Goals)

### 0.1 개발 원칙

1. **배치 우선, 실시간 후순위**: 일 1회 09:00 KST 실행이 MVP. 인트라데이 모니터링은 v0.3 이후.
2. **데이터 신선도 > 코드 정교함**: 데이터 파이프라인의 신뢰성 / 멱등성을 최우선.
3. **AI는 보조, 점수는 결정적(deterministic)**: PSS 점수 계산은 순수 Python 룰 기반. Claude API는 자연어 리포트 생성과 8-K 텍스트 분류에만 사용.
4. **무료/저비용 인프라**: 월 $5 이하 운영비 목표. GitHub Actions + 무료 티어 DB.
5. **재현 가능성(reproducibility)**: 모든 일별 PSS 산출 결과를 SQLite에 immutable snapshot으로 보관. 사후 백테스트 / 디버깅용.
6. **Fail-safe over feature-rich**: 데이터 소스 1곳 장애 시 시스템 전체가 멎지 않도록 partial degradation 설계.

### 0.2 비목표 (이번 v0.2에서 안 함)

- 자동 주문 실행 (토스앱 API 미공개, 수동 실행)
- 옵션/파생 헷지 (토스 미지원)
- 인트라데이 추격 / 1분봉 분석
- 기관 13F 정밀 트래킹 (분기 데이터, 사전 시그널 늦음)
- 한국 주식 / 크립토 (미국 small-cap만)

---

## 1. 시스템 아키텍처

### 1.1 컴포넌트 다이어그램

```
+---------------------------------------------------------------+
|                  GitHub Actions (cron)                        |
|             trigger: 매일 00:00 UTC = 09:00 KST                |
+---------------------------------+-----------------------------+
                                  |
                                  v
+---------------------------------------------------------------+
|                    runner.py (entrypoint)                     |
+---------------------------------+-----------------------------+
                                  |
        +-------------------------+-------------------------+
        |                                                   |
        v                                                   v
+--------------------+                          +-----------------------+
|  ingest/           |                          |  score/               |
|  (data fetchers)   |                          |  (pattern scorers)    |
|                    |                          |                       |
|  - edgar_8k.py     |                          |  - pattern_a.py       |
|  - polygon_bars.py |                          |  - pattern_b.py       |
|  - finnhub.py      |                          |  - pattern_c.py       |
|  - ortex_si.py     |                          |  - pattern_d.py       |
|  - reddit_ape.py   |                          |  - pattern_e.py       |
|  - stocktwits.py   |                          |  - pattern_f.py       |
|  - toss_volume.py  |                          |  - pss_aggregator.py  |
+----------+---------+                          +-----------+-----------+
           |                                                |
           v                                                v
+---------------------------------------------------------------+
|                  storage/  (SQLite or Supabase)               |
|                                                                |
|  tables:                                                       |
|   - universe          (시총 200M~10B 종목 마스터)              |
|   - daily_bars        (일봉 OHLCV)                             |
|   - filings           (8-K raw + 분류 결과)                    |
|   - short_interest    (격주 SI 데이터)                          |
|   - social_mentions   (Reddit/StockTwits 일별 카운트)          |
|   - pss_scores        (일별 PSS 패턴별 + 합계 snapshot)         |
|   - watchlist_runs    (일별 Tier 1/2/3 결과)                   |
|   - trade_log         (수동 입력, 백테스트용)                   |
+---------------------------------+-----------------------------+
                                  |
                                  v
+---------------------------------------------------------------+
|                  report/  (리포트 생성)                       |
|                                                                |
|  - claude_summarizer.py   : Claude API 호출, 자연어 변환       |
|  - telegram_pusher.py     : 텔레그램 봇 푸시                   |
|  - email_pusher.py        : 폴백 SMTP 푸시 (선택)              |
|  - notion_pusher.py       : Notion DB upsert (선택)            |
+---------------------------------------------------------------+
```

### 1.2 핵심 데이터 플로우 (일별)

```
09:00 KST  cron 트리거
   ├─ T+0  universe refresh (주 1회 일요일만)
   ├─ T+1  daily_bars 어제 종가 fetch (Polygon)
   ├─ T+2  filings 24h 신규 8-K fetch (EDGAR)
   ├─ T+3  short_interest 격주 갱신일이면 fetch (Ortex/Finra)
   ├─ T+4  social_mentions Reddit/StockTwits 24h fetch
   ├─ T+5  toss_volume 한국 retail 거래량 상위 30 매핑
   ├─ T+6  pattern A~F 스코어 계산 → pss_scores 적재
   ├─ T+7  PSS Total + Tier 분류 → watchlist_runs 적재
   ├─ T+8  Claude API 호출, 자연어 리포트 생성
   └─ T+9  Telegram 푸시
```

각 단계는 멱등(같은 날 재실행해도 동일 결과). 실패 시 그 단계만 재시도, 후속 단계는 전 단계 성공 마커 확인 후 진행.

---

## 2. 디렉토리 구조

```
presurge-daily-picker/
├── .github/
│   └── workflows/
│       ├── daily_pick.yml          # 메인 cron
│       ├── universe_refresh.yml    # 주 1회 종목 마스터 갱신
│       └── backtest.yml            # 수동 트리거 백테스트
├── docs/
│   ├── DEVELOPMENT_STRATEGY.md     # (이 파일)
│   ├── strategy_v0.2.md            # 원본 전략
│   ├── PATTERNS.md                 # 6개 패턴 룰북
│   └── RUNBOOK.md                  # 장애 대응
├── src/
│   ├── __init__.py
│   ├── runner.py                   # 일별 실행 엔트리포인트
│   ├── config.py                   # 환경변수, 임계치 상수
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── edgar_8k.py
│   │   ├── polygon_bars.py
│   │   ├── finnhub.py
│   │   ├── ortex_si.py
│   │   ├── reddit_ape.py
│   │   ├── stocktwits.py
│   │   └── toss_volume.py
│   ├── score/
│   │   ├── __init__.py
│   │   ├── pattern_a_dilution.py
│   │   ├── pattern_b_index.py
│   │   ├── pattern_c_contract.py
│   │   ├── pattern_d_squeeze.py
│   │   ├── pattern_e_brand_penny.py
│   │   ├── pattern_f_megatheme.py
│   │   └── pss_aggregator.py
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── db.py                   # SQLite/Supabase 추상화
│   │   ├── schema.sql
│   │   └── migrations/
│   ├── report/
│   │   ├── __init__.py
│   │   ├── claude_summarizer.py
│   │   ├── telegram_pusher.py
│   │   └── templates/
│   │       └── daily_report.j2
│   └── backtest/
│       ├── __init__.py
│       ├── runner.py
│       └── hypotheses.py
├── tests/
│   ├── unit/
│   │   ├── test_pattern_a.py
│   │   ├── ...
│   │   └── test_pss_aggregator.py
│   ├── integration/
│   │   └── test_runner_e2e.py
│   └── fixtures/
│       ├── edgar_sample.xml
│       ├── polygon_sample.json
│       └── known_surge_cases/      # BNAI, BYND, TNXP 등 historical
├── notebooks/
│   ├── 01_universe_eda.ipynb
│   ├── 02_pattern_calibration.ipynb
│   ├── 03_backtest_h1_h4.ipynb
│   └── 04_weight_tuning.ipynb
├── scripts/
│   ├── bootstrap_universe.py       # 1회성 시총 200M~10B 추출
│   └── seed_known_cases.py         # 검증용 5개 historical seed
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## 3. 데이터 모델 (SQLite 스키마)

### 3.1 테이블 정의

```sql
-- 종목 마스터 (주 1회 갱신)
CREATE TABLE universe (
    ticker          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    market_cap_usd  REAL NOT NULL,
    float_shares    INTEGER,
    exchange        TEXT,        -- NASDAQ / NYSE / AMEX
    sector          TEXT,
    is_common_stock BOOLEAN DEFAULT 1,
    historical_max_mcap REAL,    -- pattern E 용
    last_refreshed  TIMESTAMP NOT NULL
);
CREATE INDEX idx_universe_mcap ON universe(market_cap_usd);

-- 일봉 (OHLCV)
CREATE TABLE daily_bars (
    ticker      TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      INTEGER,
    vwap        REAL,
    PRIMARY KEY (ticker, trade_date)
);

-- SEC 8-K 공시
CREATE TABLE filings (
    accession_no    TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    filed_at        TIMESTAMP NOT NULL,
    form_type       TEXT NOT NULL,         -- 8-K, S-3, 424B, ...
    items           TEXT,                  -- "1.01,8.01" CSV
    raw_text_url    TEXT,
    classification  TEXT,                  -- pattern_a/c/f 분류 결과
    classification_confidence REAL,
    classified_at   TIMESTAMP
);
CREATE INDEX idx_filings_ticker_date ON filings(ticker, filed_at);

-- Short Interest (격주, FINRA settle date 기준)
CREATE TABLE short_interest (
    ticker          TEXT NOT NULL,
    settle_date     DATE NOT NULL,
    si_shares       INTEGER,
    si_pct_float    REAL,
    days_to_cover   REAL,
    cost_to_borrow  REAL,
    source          TEXT,                  -- 'ortex' | 'finra'
    PRIMARY KEY (ticker, settle_date, source)
);

-- 소셜 멘션 일별 카운트
CREATE TABLE social_mentions (
    ticker        TEXT NOT NULL,
    mention_date  DATE NOT NULL,
    source        TEXT NOT NULL,           -- 'reddit_wsb' | 'stocktwits' | 'apewisdom'
    mentions      INTEGER,
    bullish_pct   REAL,
    rank          INTEGER,
    PRIMARY KEY (ticker, mention_date, source)
);

-- 한국 토스앱 거래량 상위 30
CREATE TABLE toss_top_volume (
    rank_date  DATE NOT NULL,
    rank       INTEGER NOT NULL,
    ticker     TEXT NOT NULL,
    PRIMARY KEY (rank_date, rank)
);

-- 일별 PSS 점수 snapshot (immutable)
CREATE TABLE pss_scores (
    score_date     DATE NOT NULL,
    ticker         TEXT NOT NULL,
    pattern_a      REAL DEFAULT 0,
    pattern_b      REAL DEFAULT 0,
    pattern_c      REAL DEFAULT 0,
    pattern_d      REAL DEFAULT 0,
    pattern_e      REAL DEFAULT 0,
    pattern_f      REAL DEFAULT 0,
    bonus_toss     REAL DEFAULT 0,
    penalty_run    REAL DEFAULT 0,         -- 직전 30일 +50% 페널티
    penalty_earn   REAL DEFAULT 0,         -- 7일 내 earnings 페널티
    pss_total      REAL NOT NULL,
    tier           INTEGER,                -- 1/2/3/null
    triggered_patterns TEXT,               -- "A,C" CSV
    metadata_json  TEXT,                   -- 디버깅용 raw 입력값
    PRIMARY KEY (score_date, ticker)
);
CREATE INDEX idx_pss_date_total ON pss_scores(score_date, pss_total DESC);

-- 일별 watchlist 결과
CREATE TABLE watchlist_runs (
    run_date     DATE PRIMARY KEY,
    tier1_json   TEXT,                     -- 종목 + 점수 + 패턴
    tier2_json   TEXT,
    tier3_json   TEXT,
    report_md    TEXT,                     -- Claude 생성 리포트
    pushed_at    TIMESTAMP,
    push_status  TEXT
);

-- 거래 일지 (수동 + 백테스트 통합)
CREATE TABLE trade_log (
    trade_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    entry_date      DATE NOT NULL,
    entry_price     REAL NOT NULL,
    entry_pss       REAL,
    entry_tier      INTEGER,
    triggered_patterns TEXT,
    exit_date       DATE,
    exit_price      REAL,
    exit_reason     TEXT,                  -- 'target' | 'stop' | 'time' | 'fade'
    size_pct_capital REAL,
    pnl_pct         REAL,
    is_paper        BOOLEAN DEFAULT 0,
    notes           TEXT
);
```

### 3.2 마이그레이션 정책

- `src/storage/migrations/0001_init.sql` 부터 순번. SQLite의 `user_version` PRAGMA로 현재 버전 추적.
- 컬럼 추가만 허용, 삭제 금지 (historical pss_scores 보존).
- 패턴 가중치 변경 시: 기존 점수 보존, 신규 컬럼 `pss_total_v2` 추가.

---

## 4. 패턴별 스코어링 모듈 상세 설계

### 4.1 공통 인터페이스

```python
# src/score/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

@dataclass
class PatternScore:
    score: float                    # 0~max_score
    contributing_signals: dict      # 디버깅용 raw 시그널
    triggered: bool                 # 임계치 통과 여부

class PatternScorer(ABC):
    name: str
    max_score: float

    @abstractmethod
    def compute(self, ticker: str, as_of: date, db) -> PatternScore:
        ...
```

### 4.2 Pattern A — Dilution Shutdown (max 30점)

**입력 데이터**:
- `filings` 테이블에서 직전 7일 8-K Item 1.02 (Termination)
- 키워드: "ATM termination", "equity purchase agreement terminated", "standby equity"
- `daily_bars` 직전 6개월 발행주식수 변화율 (Polygon ticker details)

**스코어링 룰**:
```python
def compute(ticker, as_of, db) -> PatternScore:
    score = 0
    signals = {}

    # 24시간 내 종료 공시
    recent_24h = db.query_filings(ticker, as_of, hours_back=24,
                                   item="1.02", keywords=["ATM", "equity purchase"])
    if recent_24h:
        score += 30
        signals['recent_termination'] = recent_24h[0]['accession_no']

    # 7일 내 종료 공시 (24시간 내 없을 때만)
    elif db.query_filings(ticker, as_of, hours_back=168,
                           item="1.02", keywords=["ATM", "equity purchase"]):
        score += 20

    # 발행주식수 증가율 둔화 (보너스)
    growth_rate = db.share_growth_rate(ticker, months=6)
    if growth_rate is not None and growth_rate < 0.05:  # 6개월 5% 미만
        score += 5
        signals['low_dilution_rate'] = growth_rate

    return PatternScore(
        score=min(score, 30),
        contributing_signals=signals,
        triggered=score >= 20
    )
```

### 4.3 Pattern B — Index Inclusion (max 25점)

**입력 데이터**:
- Russell 재구성 발표 (FTSE Russell IR 페이지 스크래핑, 6월)
- ETF 운용사 보유 종목 일별 변경 (iShares, Direxion, Roundhill 공식 csv)
- 인덱스 effective day 캘린더

**스코어링 룰**:
- 편입 발표일 ~ effective day 사이: +25
- effective day 통과 후 1주일: +15 (이미 패시브 매수 일부 진행)
- 편입 + 시총 < $500M: +5 (수급 임팩트 큼)

**구현 메모**:
- ETF 운용사 csv는 매일 갱신되지 않음 → 주 1회 일요일에 fetch
- Russell은 연 1회 이벤트, 5~7월에 집중 모니터링

### 4.4 Pattern C — Government / Tier-1 Contract (max 50점)

**입력 데이터**:
- `filings` 8-K Item 1.01 (Material Definitive Agreement 신규)
- 키워드 분류: DOD, NIH, BARDA, DTRA, NASA / Walmart, Costco, Amazon, Target
- Claude API로 계약 규모 (USD) 추출

**스코어링 룰**:
```python
contract_value = claude_extract_contract_value(filing_text)
mcap = db.market_cap(ticker)
ratio = contract_value / mcap

if ratio >= 0.10:    score = 50
elif ratio >= 0.05:  score = 35
elif ratio >= 0.02:  score = 20
elif ratio > 0:      score = 10  # 작은 계약도 신호
else:                score = 0   # 규모 추출 실패
```

**Claude 프롬프트** (`src/report/templates/extract_contract.txt`):
```
다음 8-K 본문에서 계약 정보를 JSON으로 추출:
- counterparty (정부 기관 또는 회사명)
- contract_value_usd (숫자, 미상이면 null)
- contract_type ("government" | "retail_partnership" | "supply" | "other")
- duration_months
- confidence (0~1)

8-K 본문:
{filing_text[:8000]}

JSON만 출력:
```

### 4.5 Pattern D — Short Squeeze Setup (max 30점)

**입력 데이터**:
- `short_interest` 최신 settle_date
- `universe.float_shares`
- `daily_bars` 직전 30일 가격 변화

**스코어링 룰**:
```python
si_pct = latest_si.si_pct_float
dtc = latest_si.days_to_cover
ctb = latest_si.cost_to_borrow
float_m = universe.float_shares / 1_000_000
price_30d_chg = (close_today - close_30d) / close_30d

score = 0
if si_pct >= 15:        score += min(si_pct * 0.5, 15)
if dtc >= 4:            score += min(dtc * 1.5, 9)
if ctb >= 30:           score += min(ctb * 0.1, 6)
if float_m <= 50:       score += (50 - float_m) * 0.1
if price_30d_chg <= -0.30: score += 5  # shorts 자만 보너스

return min(score, 30)
```

### 4.6 Pattern E — Brand Penny (max 25점)

**입력 데이터**:
- `universe.historical_max_mcap` (수동 시드 + Polygon 30년 historical)
- 직전 종가
- `social_mentions` Reddit/StockTwits 안정성 (지난 90일 평균 멘션 수)
- `filings` 부채 구조조정 / 커버넌트 8-K

**스코어링 룰**:
```python
recovery_pct = current_mcap / historical_max
if recovery_pct > 0.10:  # 90% 회복 안 했으면 패스
    return 0

price = bars.close
if not (1.0 <= price <= 5.0):
    return 0

score = (1 - recovery_pct) * 20  # 0.10 → 18, 0.05 → 19
mentions_stability = social.avg_mentions_90d(ticker)
if mentions_stability >= 50:  # retail 인지도 안정
    score += min(mentions_stability * 0.05, 5)

debt_resolved = filings.has_recent("debt swap" | "covenant", days=180)
if debt_resolved:
    score += 5

return min(score, 25)
```

### 4.7 Pattern F — Megatheme + AI Keyword (max 25점)

**입력 데이터**:
- 회사 사업 설명 (Polygon ticker details)
- 직전 30일 8-K Item 8.01 (Other Events) 키워드
- ApeWisdom Reddit r/wallstreetbets 멘션 증가율

**스코어링 룰**:
- 메가테마 키워드 매칭 (AI, quantum, GLP-1, fusion, lithium, robotics 등): +0~15
- 신규 pivot 8-K: +5
- WSB 멘션 24h 5x↑: +5

**키워드 사기 방지**:
- 회사 매출 / 사업 부문에 실제 매핑되는지 Claude로 검증
- "we will explore AI" 류 vague 표현은 -5 감점

### 4.8 PSS Aggregator

```python
# src/score/pss_aggregator.py
def compute_pss(ticker, as_of, db) -> dict:
    scores = {
        'a': PatternA().compute(ticker, as_of, db),
        'b': PatternB().compute(ticker, as_of, db),
        'c': PatternC().compute(ticker, as_of, db),
        'd': PatternD().compute(ticker, as_of, db),
        'e': PatternE().compute(ticker, as_of, db),
        'f': PatternF().compute(ticker, as_of, db),
    }

    base = sum(s.score for s in scores.values())
    triggered = [k for k, v in scores.items() if v.triggered]

    bonus = 10 if db.in_toss_top30(ticker, as_of) else 0
    penalty_run = -30 if db.price_change_pct(ticker, as_of, days=30) >= 0.50 else 0
    penalty_earn = -20 if db.has_earnings_within(ticker, as_of, days=7) else 0

    total = max(0, base + bonus + penalty_run + penalty_earn)

    if total >= 70 and len(triggered) >= 2:
        tier = 1
    elif total >= 50:
        tier = 2
    elif total >= 30:
        tier = 3
    else:
        tier = None

    return {
        'pss_total': total,
        'tier': tier,
        'triggered_patterns': ','.join(triggered),
        'breakdown': {k: v.score for k, v in scores.items()},
        'bonus_toss': bonus,
        'penalty_run': penalty_run,
        'penalty_earn': penalty_earn,
    }
```

**Tier 캡 적용**:
- Tier 1: 최대 3종목 (점수 상위순)
- Tier 2: 최대 5종목
- Tier 3: 최대 10종목

---

## 5. 데이터 인제스트 모듈 상세

### 5.1 EDGAR 8-K Poller (`src/ingest/edgar_8k.py`)

**소스**: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom`

**전략**:
- 매일 09:00 KST 실행 시 직전 24h 신규 filing fetch
- User-Agent에 contact 이메일 명시 (SEC 정책)
- Rate limit: 10 req/sec
- 본문 다운로드는 lazy (스코어링 시점에 필요한 종목만)
- ticker → CIK 매핑은 SEC `company_tickers.json` 일 1회 갱신

**파싱**:
- atom feed → 각 entry의 `accession_no`, `cik`, `form_type`, `filed_at`
- Item 추출은 본문 첫 페이지 텍스트에서 정규식: `r'Item\s+(\d+\.\d+)'`

**에러 처리**:
- 429 / 503 → exponential backoff 5회 retry
- 파싱 실패 → 해당 filing skip + Sentry 로그
- 전체 fetch 실패 → 어제 filings 재사용 + 경고 플래그

### 5.2 Polygon Bars (`src/ingest/polygon_bars.py`)

**API**: `/v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}`
**플랜**: Stocks Starter $29/월 (시작) → 검증 후 결정

**전략**:
- 매일 어제 종가 fetch (전날 KST 23:59 = US 동부 마감 후)
- universe 1,500 종목 × 1일 = 1,500 호출 → API 한도 5 req/s 기준 5분
- 종목 단위 grouped daily bars 엔드포인트 사용 시 1콜로 전체 시장 → 우선 검토
- VWAP 포함 응답 → daily_bars.vwap 직접 저장

**캐싱**:
- 동일 (ticker, date) 재요청 금지 → DB SELECT 후 missing만 호출
- universe 재구성 후 historical 일괄 backfill 별도 스크립트

### 5.3 Ortex Short Interest (`src/ingest/ortex_si.py`)

**플랜 검토**:
- Ortex 유료 플랜 $69/월 → 비용 부담
- 무료 대안: FINRA SI 격주 데이터 (`https://www.finra.org/finra-data/short-sale-volume`) + Yahoo Finance 스크래핑

**격주 갱신 룰**:
- FINRA settle date는 매월 15일 / 말일 → 다음 영업일에 fetch
- cost_to_borrow는 무료로 안 나옴 → Yahoo statisticsapi 또는 IB API 검토

**MVP 단순화**:
- W1~W4: FINRA SI + Yahoo float만 사용 (DTC, CTB 없이)
- W5+: Ortex 도입 검토 (비용 대비 alpha 검증 후)

### 5.4 Reddit ApeWisdom (`src/ingest/reddit_ape.py`)

**API**: `https://apewisdom.io/api/v1.0/filter/all-stocks/page/1` (무료)
**전략**:
- 매일 09:00 KST 실행 시 직전 24h ranking fetch
- ticker, mentions, mentions_24h_ago, rank 저장
- 5x 증가율은 mentions / mentions_24h_ago로 직접 계산

### 5.5 StockTwits (`src/ingest/stocktwits.py`)

**API**: `https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json` (무료, 200 req/h)
**전략**:
- bullish_pct는 message-level sentiment → 24h 메시지 100건 평균
- 한국어 채팅방 별도 추적 (BNAI 사례) → message 내 한국어 검출 → 별도 카운트 컬럼

### 5.6 Toss 거래량 (`src/ingest/toss_volume.py`)

**소스**: 토스증권 앱 또는 웹 (공식 API 없음)
**전략 옵션**:
- A. 토스 웹 일간 거래량 상위 페이지 헤드리스 브라우저 스크래핑 (Playwright)
- B. 한국 retail 우회 지표: KRX/예탁결제원 미국주식 결제대금 상위 (월 1회)
- C. 우회 프록시: WeBull / Robinhood 인기 종목 (한국 동조성 가정)

**MVP**: 옵션 A (Playwright in GitHub Actions). robots.txt 확인 + 일 1회 정중한 스크래핑.

**페일오버**: 스크래핑 실패 시 bonus_toss = 0 (페널티 없이 그냥 보너스 미적용).

---

## 6. 리포트 생성 (Claude API)

### 6.1 호출 정책

- **모델**: claude-haiku-4-5 (저비용, 리포트 생성 충분) / claude-sonnet-4-6 (8-K 텍스트 분류)
- **호출 빈도**: 일 1회 리포트 + 신규 8-K 건당 1회 분류 (일평균 30~50건)
- **비용 예상**: 월 $2~5
- **prompt caching 활용**: 시스템 프롬프트 + 6개 패턴 정의는 캐시 (히트율 90%+ 목표)

### 6.2 8-K 분류 프롬프트 구조

```
[system, cached]
당신은 SEC 8-K filing을 분석하는 전문 분류기입니다.
다음 6개 패턴 중 해당 항목을 식별합니다:
- A. Dilution shutdown (ATM 종료, equity purchase agreement 종료)
- C. Government / Tier-1 contract (DOD, NIH, BARDA, Walmart 등 계약)
- F. Megatheme pivot (AI, quantum, GLP-1 등 새로운 사업 영역)

응답 JSON 스키마:
{
  "patterns": ["A" | "C" | "F" | "none"],
  "contract_value_usd": number | null,
  "counterparty": string | null,
  "key_quote": string,  // 본문에서 핵심 문장 1개
  "confidence": 0.0~1.0
}

[user, per-call]
티커: {ticker}
8-K Items: {items}
본문 (처음 8000자): {filing_text}
```

### 6.3 일일 리포트 프롬프트

```
[system, cached]
당신은 미국 small-cap pre-surge 종목 분석 리포트를 작성합니다.
입력은 일별 PSS 점수 데이터(JSON)이고, 출력은 한국어 마크다운 리포트입니다.

리포트 구조:
1. 헤더: 날짜, 시장 환경 한 줄 요약
2. Tier 1 (PSS ≥70 + 패턴 2개+): 최대 3종목, 종목당 6~8줄
   - 티커 (PSS 점수) - 가격, 시총
   - 활성 패턴과 핵심 시그널
   - 진입 트리거 / 목표가 / 손절가
3. Tier 2: 최대 5종목, 종목당 2~3줄
4. Tier 3: 최대 10종목, 1줄씩
5. 푸터: "최종 진입 결정은 본인이 직접" 면책

[user, per-call]
{watchlist_data_json}
```

### 6.4 텔레그램 푸시 (`src/report/telegram_pusher.py`)

- Bot API: `https://api.telegram.org/bot{TOKEN}/sendMessage`
- 메시지 길이 4096자 제한 → Tier 1만 인라인, Tier 2/3은 링크 (Notion 또는 GitHub Pages)
- parse_mode=Markdown, disable_web_page_preview=true
- 푸시 성공 시 watchlist_runs.pushed_at 갱신

---

## 7. CI/CD & 인프라

### 7.1 GitHub Actions Workflow

**daily_pick.yml**:
```yaml
name: Daily Pre-Surge Pick

on:
  schedule:
    - cron: '0 0 * * *'   # 매일 00:00 UTC = 09:00 KST
  workflow_dispatch:       # 수동 트리거 허용

jobs:
  daily-pick:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    env:
      POLYGON_API_KEY: ${{ secrets.POLYGON_API_KEY }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
      TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      DATABASE_URL: ${{ secrets.SUPABASE_URL }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
      - run: pip install -r requirements.txt
      - run: python -m src.runner --mode=daily
      - name: Upload artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: run-logs-${{ github.run_id }}
          path: logs/
```

### 7.2 시크릿 관리

- 모든 API 키는 GitHub Actions secrets
- 로컬 개발: `.env` (gitignore)
- 프로덕션 DB는 Supabase 무료 티어 (500MB) → 1년 운영 충분

### 7.3 모니터링

- 실행 실패 시 텔레그램 별도 채널로 알림 (`@TELEGRAM_ALERT_CHAT_ID`)
- watchlist_runs.push_status 가 'success'가 아니면 알림
- 데이터 fetch 통계 (8-K건수, 종목 커버리지)를 매일 리포트 푸터에 포함

---

## 8. 백테스트 프레임워크

### 8.1 목표 가설

전략 문서 §9의 H1~H4 검증:
- **H1**: PSS Tier 1 종목 5일 후 +20% 도달율 ≥ 35%
- **H2**: 패턴 C+D 조합 5일 평균 수익 ≥ +30%
- **H3**: 토스 거래량 상위 진입 종목 7일 평균 수익률 vs baseline
- **H4**: PSS 점수와 5일 수익률 Spearman 상관 ≥ 0.25

### 8.2 백테스트 러너 (`src/backtest/runner.py`)

```python
def backtest(start_date, end_date, db):
    for d in trading_days(start_date, end_date):
        # 그날 09:00 KST 시점에서 가용한 데이터만 사용
        # (look-ahead bias 방지: filing.filed_at < d)
        scored = score_all_universe(as_of=d, db=db)
        tier1 = [s for s in scored if s['tier'] == 1][:3]

        for s in tier1:
            entry_price = bars.open(s['ticker'], d + 1_business_day)
            for hold_days in [1, 2, 3, 5]:
                exit_price = bars.close(s['ticker'], d + hold_days)
                ret = (exit_price - entry_price) / entry_price
                results.append({
                    'date': d,
                    'ticker': s['ticker'],
                    'pss': s['pss_total'],
                    'patterns': s['triggered_patterns'],
                    'hold_days': hold_days,
                    'return': ret,
                })
    return pd.DataFrame(results)
```

### 8.3 데이터 적재 전략

- W1~W2: universe 1,500 종목 × 24개월 = 1,500 × 504 = ~75만 row
- 8-K: 약 50만 건 / 24개월 → 그 중 universe만 ~10만 건
- SI: 격주 × 1,500 = ~7.8만 row
- SQLite 단일 파일로 충분 (예상 1~2GB)

### 8.4 검증 지표

```python
# H1
hit_rate = (df[df.tier == 1].groupby('ticker')['return_5d'].max() >= 0.20).mean()

# H4
from scipy.stats import spearmanr
rho, p = spearmanr(df['pss_total'], df['return_5d'])
```

### 8.5 가중치 튜닝

- W4: H1~H4 결과 기반 패턴별 max_score 조정
- 그리드 서치: 각 패턴 max [20, 25, 30, 35, 40, 50] × 6패턴 → 너무 많음
- 베이지안 최적화 (Optuna): 50 trial × Tier 1 hit_rate 목적함수
- 과적합 방지: 18개월 train + 6개월 holdout

---

## 9. 테스트 전략

### 9.1 단위 테스트

- 각 패턴 스코어러: known surge 케이스(BNAI, BYND, TNXP, IOVA, PAVS) fixture로 expected score 검증
- 5개 케이스의 급등 -7~-1일 데이터를 fixture로 박제
- pytest + freezegun (시점 고정)

```python
def test_bnai_pattern_a_termination():
    db = load_fixture('bnai_2026_01_pre_surge')
    score = PatternA().compute('BNAI', date(2026, 1, 8), db)
    assert score.score >= 25
    assert 'recent_termination' in score.contributing_signals
```

### 9.2 통합 테스트

- runner.py 전체 플로우를 mock API로 1회 실행
- watchlist_runs row 생성 확인
- 텔레그램 푸시는 dry-run 모드 (실제 호출 X)

### 9.3 회귀 테스트 (regression)

- 매주 일요일 자동 실행
- 직전 1주일 watchlist 결과를 다시 계산 → 동일성 확인
- 결과 다르면 데이터 변경 또는 코드 변경 → 알림

### 9.4 코드 품질

- ruff (lint + format)
- mypy strict mode (type hints 강제)
- pre-commit hook으로 커밋 전 자동 실행
- 커버리지 70%+ 목표 (스코어링 모듈 90%+)

---

## 10. 8주 개발 로드맵 (전략 §10 매핑)

### W1: 인프라 부트스트랩

**산출물**: 데이터 파이프라인 v0
- [ ] 저장소 init, 디렉토리 구조 생성
- [ ] `pyproject.toml`, requirements.txt
- [ ] SQLite 스키마 + migrations/0001_init.sql
- [ ] EDGAR 8-K poller + universe table seed
- [ ] Polygon API 연동, daily_bars 적재 (어제만)
- [ ] GitHub Actions cron 골격 (실제 데이터 적재까지)
- [ ] `.env.example` + secrets 가이드

**검증**: cron 1회 성공 실행, daily_bars에 1,500 row 적재

### W2: Historical 데이터 적재 + 패턴 코딩

**산출물**: 패턴 점수 함수 6개
- [ ] universe bootstrap 스크립트 (시총 200M~10B 추출)
- [ ] 24개월 historical daily_bars 일괄 fetch
- [ ] 24개월 historical 8-K 일괄 fetch + Claude 분류
- [ ] FINRA SI historical 적재
- [ ] Pattern A~F 각 스코어러 구현 + 단위 테스트
- [ ] Known surge cases fixture 5종

**검증**: BNAI/BYND/TNXP의 급등 -1~-7일 PSS 점수가 70+ 산출되는지 수동 확인

### W3: PSS 통합 + 백테스트 baseline

**산출물**: 백테스트 노트북
- [ ] pss_aggregator.py 구현
- [ ] tier 분류 로직
- [ ] backtest/runner.py
- [ ] 24개월 backtest 1차 결과
- [ ] 03_backtest_h1_h4.ipynb 작성

**검증**: H1~H4 가설 1차 결과 (튜닝 전)

### W4: 가설 검증 + 가중치 튜닝

**산출물**: 패턴별 weight 확정
- [ ] H1~H4 정밀 검증 (holdout 분리)
- [ ] Optuna 가중치 튜닝
- [ ] 패턴별 손익 기여도 (Shapley value 또는 단순 ablation)
- [ ] 04_weight_tuning.ipynb
- [ ] PATTERNS.md 룰북 v1 (가중치 기록)

**검증**: holdout에서 H1 hit_rate ≥ 35%, H4 rho ≥ 0.25

### W5: 리포트 자동화

**산출물**: 일일 리포트 자동 발송
- [ ] claude_summarizer.py + prompt caching
- [ ] daily_report.j2 템플릿
- [ ] telegram_pusher.py
- [ ] 실패 시 fallback (이메일 또는 GitHub Issue 자동 생성)
- [ ] runner.py 전체 통합

**검증**: 7일 연속 09:00 KST 자동 푸시 성공

### W6: 페이퍼 트레이드

**산출물**: 페이퍼 일지
- [ ] trade_log 수동 입력 가이드 (Notion / 폼)
- [ ] Tier 1만 진입 (가상 사이즈)
- [ ] 매일 보유 종목 PSS 재계산 + Telegram 알림
- [ ] 청산 룰 자동 알림 (목표가/손절가/시간 stop)

**검증**: 2주 페이퍼 결과, hit_rate ≥ 30%, 평균 수익 ≥ 백테스트 결과의 70%

### W7: 라이브 진입

**산출물**: 라이브 일지
- [ ] 자본 30% 한도, Tier 1 1종목만
- [ ] 손절 알람 시스템 (KST 21:00 강제 모니터링)
- [ ] 일주일 결과 회고

### W8: 회고 + v0.3 설계

**산출물**: 전략 v0.3 회고
- [ ] 패턴별 실제 성과 분석
- [ ] 잘못된 시그널 케이스 deep dive (false positive)
- [ ] 데이터 소스 정확도 검증
- [ ] v0.3에 추가할 패턴 도출 (예: 옵션 unusual activity, 임원 매수)

---

## 11. 주요 리스크와 완화책 (개발 관점)

| 리스크 | 영향 | 완화 |
|---|---|---|
| Polygon 무료 한도 초과 | universe scan 실패 | $29 Stocks Starter 즉시 업그레이드, grouped daily endpoint 사용 |
| EDGAR rate limit / IP 차단 | 8-K 누락 | 10 req/s 엄수, User-Agent 명시, 야간 분산 |
| 토스 스크래핑 차단 | bonus_toss 미적용 | 페일오버: bonus 0 처리, 시스템 미중단 |
| Claude API 비용 폭증 | 운영비 증가 | 일일 budget cap (예: $1) + prompt caching 강제 |
| GitHub Actions 30분 timeout | 실행 중단 | 단계별 분리 workflow + 결과 artifact로 다음 step 호출 |
| 토스 거래량 데이터 부재 | Pattern E/F 약화 | KRX 미국주식 결제대금 월간 데이터 보조 |
| 8-K 분류 오류 (false positive) | Tier 1 오염 | confidence < 0.7 필터링, Tier 1 진입 시 사람 최종 확인 |
| 데이터 look-ahead bias | 백테스트 과대평가 | filing.filed_at, settle_date 엄격 가드 |
| 패턴 가중치 과적합 | 라이브에서 무력화 | holdout 6개월 강제, 가중치 변경 시 새 컬럼 보존 |
| SQLite 동시성 | concurrent run 충돌 | GitHub Actions concurrency group으로 직렬화 |

---

## 12. 즉시 실행 가능한 첫 5개 액션 (W1 Day 1)

전략 문서 §11의 5개 액션을 코드 레벨로 구체화:

1. **Polygon.io 가입 + API 키 → GitHub secret 등록**
   - https://polygon.io/dashboard
   - secret name: `POLYGON_API_KEY`
   - 무료 티어로 시작 (5 req/s 제한 확인)

2. **EDGAR 8-K atom RSS 1회 fetch 검증**
   ```bash
   curl -A "presurge-picker contact@example.com" \
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"
   ```
   - 응답 파싱 가능한지 검증 후 `src/ingest/edgar_8k.py` 첫 커밋

3. **Universe bootstrap 스크립트** (`scripts/bootstrap_universe.py`)
   - Polygon `/v3/reference/tickers?market=stocks&active=true` 페이지네이션
   - 시총 200M~10B + 보통주 + 미국 거래소 필터
   - 결과 `universe` 테이블 적재

4. **Historical 5종 케이스 fixture 생성** (`scripts/seed_known_cases.py`)
   - BNAI, BYND, TNXP, IOVA, PAVS
   - 각 케이스 급등 -14일 ~ +5일 데이터 SQLite 별도 파일로 박제
   - 패턴 스코어러 단위 테스트의 ground truth

5. **Claude API 8-K 분류 프롬프트 v0** (`src/report/templates/extract_contract.txt` 등)
   - 5개 케이스의 실제 8-K로 프롬프트 검증
   - JSON 스키마 준수율 / 정확도 측정
   - 부적절한 응답은 프롬프트 iteration

---

## 13. 운영 후 v0.3 확장 후보

W8 회고 시점에 검토:

- **Pattern G**: 임원 매수 (Form 4)
- **Pattern H**: 옵션 unusual activity (call IV spike)
- **Pattern I**: PDUFA 캘린더 사전 등록 (FDA 일정 API)
- **Pattern J**: 특허 grant / clinical trial 결과 발표
- **인트라데이 모니터링**: 22:30 KST 정규장 오픈 시 RVOL 알림 봇
- **Self-learning weights**: 실거래 trade_log를 reward로 weight 점진 조정
- **자동 주문 (Webull/IBKR)**: 토스 외 채널 도입 시

---

## 14. 부록: 핵심 임계치 상수 (`src/config.py` 초안)

```python
# 종목 유니버스
MARKET_CAP_MIN_USD = 200_000_000
MARKET_CAP_MAX_USD = 10_000_000_000

# Tier 임계치
TIER1_PSS_MIN = 70
TIER1_PATTERNS_MIN = 2
TIER1_MAX_TICKERS = 3

TIER2_PSS_MIN = 50
TIER2_MAX_TICKERS = 5

TIER3_PSS_MIN = 30
TIER3_MAX_TICKERS = 10

# 패턴 max_score
PATTERN_A_MAX = 30
PATTERN_B_MAX = 25
PATTERN_C_MAX = 50
PATTERN_D_MAX = 30
PATTERN_E_MAX = 25
PATTERN_F_MAX = 25

# 보너스/페널티
BONUS_TOSS_TOP30 = 10
PENALTY_RECENT_RUN_PCT = 0.50
PENALTY_RECENT_RUN = -30
PENALTY_EARNINGS_DAYS = 7
PENALTY_EARNINGS = -20

# Pattern D 임계치
SI_PCT_MIN = 0.15
DTC_MIN = 4
CTB_MIN = 0.30
FLOAT_MAX_M = 50

# Pattern E 임계치
BRAND_PENNY_RECOVERY_MAX = 0.10
BRAND_PENNY_PRICE_MIN = 1.0
BRAND_PENNY_PRICE_MAX = 5.0

# 진입 트리거 (Tier 1 한정, v0.2 추후 자동화)
ENTRY_RVOL_MIN = 2.0
ENTRY_CATALYST_FRESHNESS_HOURS = 72

# 리스크 한도
MAX_POSITION_PCT = 0.07
MAX_CONCURRENT_TIER1 = 3
DAILY_DRAWDOWN_HALT = -0.02
WEEKLY_DRAWDOWN_HALT = -0.05

# API rate limits
SEC_RPS = 10
POLYGON_RPS = 5
STOCKTWITS_RPH = 200
```

---

## 마무리

본 문서는 전략 v0.2를 8주 안에 자동화 시스템으로 구현하기 위한 청사진입니다. 핵심은 **W1~W4 데이터 + 백테스트로 가설 검증**, **W5~W6 자동화 + 페이퍼**, **W7~W8 라이브 + 회고**의 단계적 진행입니다.

운영자가 지켜야 할 단 하나의 개발 룰: **백테스트로 검증되지 않은 패턴 변경은 라이브에 반영하지 않는다.** 그 외 모든 사항은 codify해서 결정 부담을 시스템에 위임합니다.
