# 장중 모니터링 MVP 개발 기획서

작성일: 2026-05-18  
목표 버전: v0.4 Intraday Monitor MVP  
운영 목적: 매일 09:00 KST watchlist를 요약 발송하고, 해당 후보를 미국장 동안 5분 주기로 감시해 매수/매도 시그널을 Telegram으로 즉시 알림한다.

---

## 1. 배경과 목표

현재 시스템은 하루 한 번 PSS 기반 후보를 선별해 Telegram으로 전달한다. 이 방식은 "오늘 볼 종목"을 정하는 데는 유효하지만, 초단타 급등주 운용 관점에서는 다음 문제가 있다.

1. 매수 시점이 모호하다.
2. 후보가 실제로 움직이기 시작했는지 장중 확인이 필요하다.
3. 1일 고가 기준 alpha는 있으나, 종가 기준 alpha는 약하다.
4. 따라서 "선별"보다 "진입/청산 타이밍"이 성과에 더 직접적으로 영향을 준다.

MVP의 목표는 자동 주문이 아니라 **장중 행동 가능한 알림 레이어**를 붙이는 것이다.

```
09:00 KST daily picker
  -> 오늘 감시 후보 <= 20개 Telegram 요약

US regular market
  -> 후보만 5분 주기 모니터링
  -> buy / reduce / exit / caution 시그널 즉시 Telegram

장 마감 후
  -> 시그널 이후 10m/30m/1h/EOD 성과 저장
  -> 어떤 장중 트리거가 먹혔는지 학습
```

---

## 2. MVP 범위

### 2.1 포함

- 아침 watchlist에서 최대 20개 감시 종목 확정
- 5분 주기 장중 가격/거래량 모니터링
- yfinance 5분봉 batch fetch 우선, Finnhub quote fallback
- Telegram 즉시 알림
- 중복 알림 억제
- signal_events 테이블에 모든 알림 저장
- 시그널 후 성과 자동 평가
- paper trade 관점의 시그널 품질 리포트

### 2.2 제외

- 자동 주문 실행
- 1분봉 고빈도 트레이딩
- 전체 universe 실시간 스캔
- 옵션 실시간 체결 기반 UOA
- 유료 실시간 데이터 의존
- 확정 수익률 보장형 추천 문구

MVP는 "오늘 볼 종목을 더 적극적으로 거래할 수 있게 만드는 알림 시스템"이지, 자동매매 엔진이 아니다.

---

## 3. 운영 방식

### 3.1 일별 후보 선정

기존 `src/runner.py` daily run이 산출한 `watchlist_runs`와 `pss_scores`를 사용한다.

추천 감시 후보 구성:

| 그룹 | 최대 수 | 기준 |
|---|---:|---|
| Tier 1 | 3 | 있으면 전부 포함 |
| Tier 2 | 5 | 있으면 전부 포함 |
| Tier 3 상위 | 12 | PSS 내림차순, Pattern G/E/D 우선 |

최대 20개를 넘지 않는다. 5분 주기에서 20개를 넘기면 무료 데이터 소스의 rate limit과 알림 품질이 동시에 나빠진다.

### 3.2 장중 모니터링 시간

미국 정규장 기준:

- 09:30-16:00 ET
- KST 기준 서머타임 22:30-05:00, 겨울 23:30-06:00

MVP에서는 `exchange_calendars` 또는 간단한 NYSE calendar helper를 두고, 정규장 시간에만 monitor loop를 실행한다.

프리마켓은 v0.4.1 후보로 둔다. yfinance prepost 데이터가 불안정하고, 무료 데이터에서 체결/호가 품질이 낮기 때문이다.

---

## 4. 데이터 소스 전략

### 4.1 기본 전략

| 용도 | 1차 | fallback | 비고 |
|---|---|---|---|
| 5분 OHLCV | yfinance batch download | 없음 | 여러 ticker를 한 번에 가져와 호출 수 절감 |
| 현재가 | yfinance latest close | Finnhub quote | yfinance 실패 시 price-only mode |
| 전일 기준가 | local `daily_bars` | Finnhub quote `pc` | 전일 high/close/open은 DB 우선 |
| 알림 발송 | Telegram Bot API | alert chat | 기존 `TelegramPusher` 재사용 |

### 4.2 yfinance 우선 이유

- API key가 필요 없다.
- 다중 ticker batch fetch가 가능하다.
- 5분봉 OHLCV가 있어 volume spike, VWAP, opening range를 계산할 수 있다.
- 이미 프로젝트가 yfinance SI/options snapshot을 사용하고 있어 의존성 추가 부담이 작다.

단점:

- rate limit/429가 불규칙하다.
- 데이터가 지연되거나 특정 ticker에서 비어 있을 수 있다.
- 실시간 체결 데이터가 아니라 "MVP 관찰용"으로 봐야 한다.

### 4.3 Finnhub fallback 이유

- 무료 티어 기준 quote 호출이 비교적 단순하다.
- 후보 20개를 5분마다 조회하면 약 4 calls/min이라 free tier에서도 안전한 편이다.
- yfinance가 막힐 때 최소한 전일 고가 돌파, 전일 종가 대비 급등, 급락 경고는 유지할 수 있다.

Fallback mode에서는 volume/VWAP 기반 시그널을 비활성화하고 price-only 시그널만 사용한다.

---

## 5. 시그널 설계

MVP는 처음부터 복잡한 모델을 만들지 않는다. "장중 움직임이 실제로 시작됐는가"를 확인하는 deterministic rule로 시작한다.

### 5.1 BUY 시그널

#### BUY-1: Opening Range Breakout

장 초반 급등주에 가장 적합한 기본 시그널.

조건:

- market open 후 15분 이상 경과
- 현재가 > 당일 첫 15분 high
- 현재가 > 전일 high 또는 전일 close 대비 +3% 이상
- 최근 5분 거래량 >= 당일 이전 5분 평균의 2배
- PSS tier가 1 또는 2이면 우선순위 상향

알림 예시:

```text
[BUY WATCH] BMEA
Trigger: ORB + volume spike
Price: 3.42
Ref: prev_high 3.31 / OR15 high 3.38
Risk: fail below OR15 high or VWAP
```

#### BUY-2: VWAP Reclaim

장 초반 튀었다가 눌린 뒤 다시 살아나는 후보용.

조건:

- 현재가가 intraday VWAP 아래에서 위로 재돌파
- 최근 5분 거래량 >= 이전 6개 5분봉 평균의 1.5배
- 현재가 >= 전일 close 대비 +2%
- 당일 저점 대비 +5% 이상 회복

#### BUY-3: Relative Volume Continuation

Pattern G 후보에 맞춘 거래량 지속 시그널.

조건:

- 누적 당일 거래량이 같은 시간대 추정 평균 대비 3배 이상
- 현재가가 5분 EMA 또는 최근 3개 5분봉 고점 위
- 전일 close 대비 +5% 이상
- 직전 알림 이후 30분 이상 경과

### 5.2 SELL / REDUCE 시그널

매수 알림만 있으면 실제 운용이 어렵다. MVP부터 청산/감축 알림을 같이 둔다.

#### SELL-1: VWAP Loss

조건:

- BUY 알림 이후 현재가가 VWAP 아래로 이탈
- 이탈 후 2개 5분봉 연속 회복 실패
- 또는 BUY 알림 가격 대비 -5% 이하

#### SELL-2: Momentum Exhaustion

조건:

- BUY 알림 이후 +10% 이상 상승
- 최근 2개 5분봉에서 고점 갱신 실패
- 거래량 감소와 함께 lower high 형성

#### TAKE-PROFIT 알림

조건:

- BUY 알림 기준 +10%, +20%, +30% 도달
- 각 구간마다 한 번만 알림

이 알림은 매도 강제 신호가 아니라 "분할 익절 검토"로 표현한다.

### 5.3 CAUTION 시그널

후보가 망가졌을 때 watchlist에서 사실상 제외하기 위한 알림.

조건:

- 전일 close 대비 -8% 이하
- 당일 VWAP 아래에서 30분 이상 체류
- 거래량 급증 없이 하락 지속

---

## 6. 알림 억제 정책

초단타 알림은 너무 자주 오면 오히려 매매 품질을 떨어뜨린다. MVP에서는 강한 rate limit을 둔다.

| 정책 | 값 |
|---|---:|
| ticker별 BUY 알림 | 하루 최대 2회 |
| ticker별 SELL/REDUCE 알림 | BUY 이후 상태별 1회 |
| 동일 signal_type 재알림 cooldown | 30분 |
| 전체 Telegram 알림 | 5분 loop당 최대 5개 |
| 후보 제외 조건 | CAUTION 이후 새 high 회복 전까지 buy 비활성 |

중복 억제는 DB의 `signal_events`를 기준으로 처리한다.

---

## 7. 데이터 모델

### 7.1 signal_events

장중 알림의 원장. Telegram 발송 여부와 관계없이 발생한 신호를 저장한다.

```sql
CREATE TABLE IF NOT EXISTS signal_events (
    signal_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_ts        TEXT NOT NULL,
    trade_date       TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    signal_type      TEXT NOT NULL, -- BUY_WATCH | TAKE_PROFIT | SELL_WATCH | CAUTION
    trigger_code     TEXT NOT NULL, -- ORB | VWAP_RECLAIM | RVOL_CONT | VWAP_LOSS | EXHAUSTION
    price            REAL NOT NULL,
    ref_price        REAL,
    pss_total        REAL,
    tier             INTEGER,
    triggered_patterns TEXT,
    source           TEXT NOT NULL, -- yfinance | finnhub | mixed
    metadata_json    TEXT,
    telegram_sent_at TEXT,
    telegram_status  TEXT
);

CREATE INDEX IF NOT EXISTS idx_signal_date ON signal_events(trade_date, signal_ts);
CREATE INDEX IF NOT EXISTS idx_signal_ticker ON signal_events(ticker, trade_date);
```

### 7.2 signal_outcomes

시그널 이후 성과 평가. 기존 `trade_log`는 daily watchlist/paper trade 중심이므로, 장중 알림 평가는 별도 테이블이 낫다.

```sql
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id          INTEGER PRIMARY KEY,
    max_10m_pct        REAL,
    close_10m_pct      REAL,
    max_30m_pct        REAL,
    close_30m_pct      REAL,
    max_60m_pct        REAL,
    close_60m_pct      REAL,
    max_eod_pct        REAL,
    close_eod_pct      REAL,
    min_after_pct      REAL,
    evaluated_at       TEXT,
    FOREIGN KEY(signal_id) REFERENCES signal_events(signal_id)
);
```

평가 기준:

- BUY 계열: 이후 최대 상승률, 종가 수익률, 최대 역행폭
- SELL/CAUTION 계열: 이후 하락 회피 성과, false exit 여부

---

## 8. 구현 컴포넌트

### 8.1 신규 파일

```text
src/intraday/
├── __init__.py
├── calendar.py          # 미국 정규장 판별
├── watchlist.py         # 오늘 감시 후보 <=20 추출
├── market_data.py       # yfinance/Finnhub 데이터 fetch + fallback
├── indicators.py        # VWAP, opening range, 5m volume spike
├── signals.py           # BUY/SELL/CAUTION rule engine
├── monitor.py           # 5분 loop entrypoint
└── outcomes.py          # 시그널 후 성과 평가

scripts/
├── run_intraday_monitor.py
├── evaluate_intraday_signals.py
└── com.presurge.intraday.plist
```

### 8.2 기존 파일 변경

| 파일 | 변경 |
|---|---|
| `src/storage/schema.sql` | `signal_events`, `signal_outcomes` 추가 |
| `src/storage/db.py` | signal upsert/query helper 추가 |
| `src/ingest/finnhub.py` | `quote(symbol)` 추가 |
| `src/report/telegram_pusher.py` | 장중 알림 템플릿 helper는 별도 함수로 추가 가능 |
| `docs/RUNBOOK.md` | intraday launchd 운영/정지/로그 경로 추가 |
| `.env.example` | intraday 관련 설정값 추가 |

---

## 9. 설정값

`.env` 또는 `src/config.py`에 추가할 설정:

```bash
INTRADAY_ENABLED=1
INTRADAY_MAX_TICKERS=20
INTRADAY_INTERVAL_SECONDS=300
INTRADAY_USE_YFINANCE=1
INTRADAY_USE_FINNHUB_FALLBACK=1
INTRADAY_YFINANCE_PREPOST=1
INTRADAY_INCLUDE_EXTENDED_HOURS=1
INTRADAY_MIN_TIER=3
INTRADAY_MAX_ALERTS_PER_LOOP=5
INTRADAY_BUY_COOLDOWN_MINUTES=30
INTRADAY_REGULAR_SESSION_ONLY=0
INTRADAY_MUTE_QUIET_HOURS=0
INTRADAY_QUIET_START_KST=03:00
INTRADAY_QUIET_END_KST=06:00
```

초기값은 보수적으로 둔다. 알림이 너무 적으면 threshold를 낮추고, 너무 많으면 volume 조건과 cooldown을 강화한다.

---

## 10. 실행 방식

### 10.1 로컬 수동 실행

```bash
python -m scripts.run_intraday_monitor --dry-run --once
python -m scripts.run_intraday_monitor --dry-run --loop
python -m scripts.run_intraday_monitor --loop
```

### 10.2 launchd 운영

MVP에서는 macOS launchd로 정규장 시간대에만 실행한다.

권장 방식:

- 장 시작 전 script start
- loop 내부에서 market open 여부 확인
- market close 후 정상 종료

로그:

```text
data/intraday_monitor.log
data/intraday_launchd_stdout.log
data/intraday_launchd_stderr.log
```

---

## 11. 개발 순서

### Phase 1: Read-only dry run

- 오늘 watchlist <=20 추출
- yfinance batch 5분봉 fetch
- Finnhub quote fallback 구현
- signal rule을 계산하되 Telegram 발송은 하지 않음
- `signal_events.telegram_status='dry_run'`으로 저장

완료 기준:

- 정규장 1일 dry run에서 crash 없음
- ticker별 중복 알림 억제 작동
- yfinance 실패 시 price-only fallback 작동

### Phase 2: Telegram alert

- BUY/TAKE_PROFIT/SELL/CAUTION 알림 발송
- loop당 최대 5개 제한
- alert 내용에 price, trigger, risk level, 기준가 포함

완료 기준:

- 실제 Telegram에서 읽기 쉬운 형식
- 같은 ticker가 5분마다 반복 발송되지 않음
- 통신 실패 시 DB에는 signal이 남음

### Phase 3: Outcome learning

- `evaluate_intraday_signals.py` 추가
- 시그널 이후 10m/30m/60m/EOD 성과 계산
- `report_intraday_signals.py`로 일간/주간 요약 리포트 생성
- 리포트는 추천안만 제공하고 파라미터 자동 변경은 하지 않음

완료 기준:

- BUY trigger별 hit rate 산출
- false positive trigger 식별 가능
- 기존 daily `trade_log`와 별도로 장중 trigger 품질 추적
- Tue-Sat 11:30 KST 일간 리포트, Sat 12:00 KST 주간 리포트 발송

### Phase 4: Threshold tuning

- 최소 2주 forward 데이터 수집
- ORB/VWAP/RVOL trigger별 성과 비교
- ticker price bucket, PSS tier, Pattern G 여부별 성과 분해

완료 기준:

- "가장 쓸모 있는 매수 트리거 1-2개"를 남기고 나머지 축소
- Telegram 알림 수를 하루 3-15개 범위로 유지

---

## 12. 검증 지표

장중 MVP는 daily backtest의 H1/H4와 다른 지표로 본다.

### 12.1 Precision

BUY 알림 이후:

- 30분 내 +5% 도달률
- 60분 내 +10% 도달률
- EOD high +10% 도달률
- 최대 역행폭 -5% 이하 비율

### 12.2 Actionability

- 알림 발생 후 실제 매수 가능 시간이 있었는가
- 알림이 너무 늦게 왔는가
- 같은 ticker에 과도한 반복 알림이 있었는가
- 장중 알림 수가 사람이 처리 가능한 수준인가

### 12.3 Risk

- BUY 이후 -5% 먼저 도달한 비율
- SELL/CAUTION 이후 다시 +10% 간 비율
- gap-up 고점 추격 false positive 비율

---

## 13. Telegram 메시지 포맷

### 13.1 BUY WATCH

```text
[BUY WATCH] BMEA
PSS 50.2 / Tier 2 / D,E
Trigger: ORB + 5m volume spike
Price: 3.42
Refs: prev_high 3.31 / OR15 3.38 / VWAP 3.29
Plan: invalid below 3.29, watch +10%/+20%
```

### 13.2 TAKE PROFIT

```text
[TAKE PROFIT] BMEA +12.4%
From signal: 3.42 -> now 3.84
Reason: first +10% target reached
Plan: consider trim / trail above VWAP
```

### 13.3 SELL WATCH

```text
[SELL WATCH] BMEA
Reason: VWAP lost after BUY signal
Price: 3.27 / VWAP 3.31
Signal PnL: -4.4%
```

문구는 단정적인 "매수하세요/매도하세요"가 아니라, 실행 판단을 돕는 watch 문구로 유지한다.

---

## 14. 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| yfinance 429/빈 데이터 | volume/VWAP signal 누락 | Finnhub price-only fallback |
| 무료 데이터 지연 | 알림이 늦음 | MVP 한계로 명시, 성과 검증 후 유료 API 검토 |
| 알림 과다 | 사용자가 대응 불가 | ticker별 cooldown, loop cap |
| gap-up 고점 추격 | false buy 증가 | ORB/VWAP 조건과 max extension guard 추가 |
| PC sleep | monitor 중단 | macOS sleep 방지 또는 Supabase/서버 이전 |
| market holiday | 불필요 실행 | calendar helper로 정규장 체크 |

---

## 15. 성공 기준

2주 forward 운영 후 다음 조건을 만족하면 MVP 성공으로 본다.

- 장중 monitor가 정규장 동안 안정적으로 동작
- 하루 Telegram intraday 알림 수가 평균 3-15개
- BUY 알림 후 60분 내 +5% 도달률이 baseline 대비 유의하게 높음
- BUY 알림 후 최대 역행폭 -5% 이상 비율이 과도하지 않음
- 사용자가 실제로 매수/매도 판단에 활용 가능한 메시지 품질 확보

이후 개선 방향:

1. Polygon/IEX/Alpaca 유료 intraday API 검토
2. 프리마켓 모니터링 추가
3. options_activity를 장중 signal priority에 반영
4. signal outcome 기반 threshold 자동 튜닝
5. Supabase/서버 상시 가동으로 PC 의존성 제거

---

## 16. 권장 구현 판단

바로 유료 실시간 API로 넘어가기보다, 먼저 이 MVP로 "알림 규칙 자체가 실제 매매 판단에 도움이 되는지"를 검증한다.

현재 학습 결과상 PSS + Pattern G는 1일 고가 기준 alpha가 있다. 문제는 종목 선별이 아니라 **움직이는 순간을 잡는 것**이다. 따라서 v0.4의 핵심은 모델 복잡도를 높이는 것이 아니라, watchlist를 장중 행동 가능한 신호로 변환하고 그 신호별 성과를 저장하는 것이다.
