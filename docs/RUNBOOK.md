# 운영 RUNBOOK — 일별 자동화 (W5)

매일 09:00 KST에 자동으로 watchlist를 Telegram에 푸시한다.

**현재 운영 모드 (옵션 A — macOS launchd)**: 로컬 PC가 켜진 동안 매일 09:00 KST 실행. SQLite DB가 로컬에 있어 GitHub Actions(stateless)로는 못 돌리는 W3 데이터(universe + bars + filings + SI)를 그대로 활용.

**다음 단계 (옵션 C — Supabase)**: W6 페이퍼 4-8주 후 알파 검증되면 Supabase로 마이그레이션해서 GitHub Actions 24/7 가동 (PC 의존성 제거).

---

## 1. 옵션 A — macOS launchd 가동 절차 (현재)

이미 가동 중. 점검 명령:

```bash
# 등록 확인
launchctl list | grep presurge

# plist 위치
ls -l ~/Library/LaunchAgents/com.presurge.daily.plist

# 즉시 트리거 (테스트용)
launchctl start com.presurge.daily

# 비활성화 / 재활성화
launchctl unload ~/Library/LaunchAgents/com.presurge.daily.plist
launchctl load ~/Library/LaunchAgents/com.presurge.daily.plist
```

**스케줄**: 매일 **09:00 KST** (`StartCalendarInterval Hour=9 Minute=0`).
**로그**: `data/runner.log` (메인), `data/launchd_stdout.log`, `data/launchd_stderr.log`.

**PC가 09:00 KST에 꺼져 있던 경우** — launchd는 wake 후 다음 trigger를 기다림 (그날 watchlist 결손). 미보장. 안정적 24/7 운영은 옵션 C로 마이그레이션 필요.

### 1.1 secrets는 .env 파일에 보관

launchd 실행 시 `set -a; source .env; set +a` 로 ENV 주입. GitHub Actions secrets와 분리됨.

`.env`에 들어있는 값:
- POLYGON_API_KEY, ANTHROPIC_API_KEY, FINNHUB_API_KEY
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALERT_CHAT_ID
- SEC_USER_AGENT
- TOSS_TOP30_TICKERS (선택)

> ⚠ `.env`에 trailing whitespace 주의. shell `awk '{$1=$1};1`로 trim 권장.

---

## 2. 옵션 C — Supabase 마이그레이션 (W6 작업, 4-8주 후 검토)

GitHub Actions cron + Supabase Postgres로 전환.

Supabase connection string은 dashboard **Connect** 메뉴의 Postgres connection string을 사용한다. GitHub Actions 같은 임시 실행 환경은 IPv4 호환 pooler URL을 권장한다.

1. Supabase 프로젝트 생성
2. dashboard에서 Session/Transaction pooler connection string 복사
3. 로컬 `.env`에 `SUPABASE_DATABASE_URL="postgresql://..."` 추가
4. 스키마 생성 + 로컬 SQLite 데이터 이전

```bash
# 기본은 운영용 슬림 이전: 대형 time-series 테이블은 최근 220일만 이전.
# 현재 SQLite 전체 DB는 600MB+라 Supabase Free 500MB 한도를 넘을 수 있음.
python -m scripts.migrate_sqlite_to_supabase --replace

# 24개월 백테스트 원장까지 전부 이전해야 하면 Pro plan 검토 후:
python -m scripts.migrate_sqlite_to_supabase --replace --full
```

5. GitHub Actions secret 등록

```bash
gh secret set DATABASE_URL --body "$SUPABASE_DATABASE_URL"
```

6. workflows 재활성화

```bash
gh workflow enable daily_pick.yml
gh workflow enable universe_refresh.yml
gh workflow enable intraday_monitor.yml
```

7. smoke

```bash
gh workflow run daily_pick.yml -f skip="push"
gh workflow run intraday_monitor.yml -f dry_run=true
```

이 시점에 launchd 비활성화:
```bash
launchctl unload ~/Library/LaunchAgents/com.presurge.daily.plist
launchctl unload ~/Library/LaunchAgents/com.presurge.intraday.plist
rm ~/Library/LaunchAgents/com.presurge.daily.plist
rm ~/Library/LaunchAgents/com.presurge.intraday.plist
```

GitHub Actions cron은 현재 `gh workflow disable`로 정지된 상태 (W5 가동 시 옵션 A 채택으로 SQLite local DB와 충돌 방지).

---

## 3. 장중 모니터링 MVP — com.presurge.intraday

Daily picker가 만든 watchlist 중 최대 20개를 미국 정규장 동안 5분 주기로 감시하고, BUY/TAKE_PROFIT/SELL/CAUTION watch signal을 Telegram alert chat으로 보낸다.

### 3.1 수동 smoke

```bash
# 시장 시간 밖에서도 dry-run 1회 실행
python -m scripts.run_intraday_monitor --dry-run --once --force-market-closed

# 정규장 동안 dry-run loop
python -m scripts.run_intraday_monitor --dry-run --loop

# 실발송 loop
python -m scripts.run_intraday_monitor --loop
```

로그:

```bash
tail -f data/intraday_monitor.log
```

### 3.2 launchd 등록

`.env`에서 live alert를 켠다.

```bash
INTRADAY_ENABLED=1
```

```bash
cp scripts/com.presurge.intraday.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.presurge.intraday.plist
launchctl list | grep presurge
```

**스케줄**: 매일 **22:20 KST** 시작. 미국 서머타임에는 정규장 10분 전, 겨울에는 script가 약 70분 대기 후 장 시작부터 monitor한다.  
**로그**: `data/intraday_monitor.log`, `data/intraday_launchd_stdout.log`, `data/intraday_launchd_stderr.log`.

정지:

```bash
launchctl unload ~/Library/LaunchAgents/com.presurge.intraday.plist
```

### 3.3 outcome 평가

장 마감 후 또는 다음날 실행:

```bash
python -m scripts.evaluate_intraday_signals
```

결과는 `signal_outcomes`에 저장된다. 장중 signal 품질은 `signal_events`와 `signal_outcomes`를 조인해 본다.

---

## 4. 정상 동작 체크리스트

매일 cron 후 확인:

- [ ] Telegram 메시지 도착 (KST 09:00 ~ 09:30 사이)
- [ ] Tier 1 / Tier 2 / Tier 3 종목 표시
- [ ] Actions 탭에서 run status `success`
- [ ] 알림 채널 (alert chat)에 에러 메시지 없음

장애 시 alert chat으로 자동 푸시:
```
Daily run YYYY-MM-DD 부분 실패
errors:
- filings: HTTPStatusError: ...
- ...
watchlist Tier1=N Tier2=M
```

---

## 5. 자주 발생하는 장애와 대응

### 5.1 Polygon 429 (rate limit)
- 원인: 무료 티어 5/min 한도 초과
- 영향: bars 미적재, 그날 수익률 계산 일부 오차
- 대응:
  - daily는 grouped daily 1콜/일이라 정상이면 안 발생
  - 발생 시 Polygon Stocks Starter ($29/월) 업그레이드 + `POLYGON_PERIOD_SECONDS=1` 변경

### 5.2 EDGAR atom 응답 0건
- 원인: 24h 신규 8-K 없음 (주말, 공휴일 흔함)
- 대응: 정상. PSS 점수에는 영향 없음 (이미 적재된 historical 8-K 사용).

### 5.3 Telegram 400 parse error
- 원인: 리포트 마크다운에 미escape된 특수문자
- 대응: `claude_summarizer.py`의 generate_report 프롬프트 강화 또는 parse_mode를 HTML로 전환

### 5.4 Claude API 비용 폭주
- 원인: 신규 1.01/1.02 8-K 다수 발생 시 분류 비용 증가
- 가드: `step_classify_new_filings`의 `--max-cost-usd 5` 일별 cap
- 대응: cap 초과해도 그날만 일부 분류 누락. 다음 cron이 backfill.

### 5.5 SQLite cache 만료
- 원인: GitHub Actions cache 7일 미사용 시 자동 삭제
- 대응: `actions/cache@v4`의 `restore-keys` prefix 매칭 → 가장 가까운 백업 복원
- 영구 저장 필요 시 v0.3에서 Supabase 마이그레이션

### 5.6 Intraday yfinance 429 / 빈 5분봉
- 원인: Yahoo rate limit 또는 ticker별 데이터 공백
- 영향: VWAP/volume 기반 BUY signal 누락
- 대응: Finnhub quote fallback으로 price-only 감시. 반복되면 유료 intraday API 검토.

### 5.7 Intraday 알림 과다
- 원인: threshold 과민, 장중 급등장
- 가드: ticker별 BUY 하루 2회, 동일 trigger cooldown 30분, loop당 최대 5개
- 대응: `INTRADAY_MAX_ALERTS_PER_LOOP` 하향 또는 BUY rule threshold 상향

---

## 6. 주간 / 월간 작업

| 주기 | 작업 | 명령 |
|---|---|---|
| 일 | watchlist 도착 확인 | (자동) |
| 주 | Yahoo SI snapshot 갱신 | `python -m scripts.snapshot_short_interest --force` (~60분) |
| 주 | universe refresh | `.github/workflows/universe_refresh.yml` (cron 자동) |
| 월 | 누적 비용 점검 | Anthropic console + GitHub Actions usage |
| 분기 | trade_log 회고 | 직접 trade_log 테이블 조회 |
| 연 1회 | Russell reconstitution events 추가 | `python -m scripts.seed_russell_events` |

---

## 7. 비상 정지 / 재시작

```bash
# 정지: workflow 비활성화
gh workflow disable daily_pick.yml

# 특정 stage만 skip하고 수동 실행
gh workflow run daily_pick.yml -f skip="filings,classify"

# 재활성화
gh workflow enable daily_pick.yml
```

DB 초기화 필요 시 (W3 데이터 잃음 주의):
```bash
rm data/presurge.db data/presurge.db-shm data/presurge.db-wal
python -m src.storage.db --init
# universe + bars + filings 다시 적재 필요 (~3시간)
```

---

## 8. v0.2 데이터 한계 (W4 검증)

- **Pattern D (squeeze)**: 현재 SI snapshot 1회 (2026-04-15)만 → backtest에서 거의 0점. forward-only 운영 시 매주 갱신 필요.
- **Pattern B (index)**: Russell 2000 alpha 약함 (W4 #5). max=5 다운, 향후 v0.3에서 ETF inclusion 별도 분석.
- **historical_max_mcap**: 47% 커버 (yfinance 한계). 신생 IPO는 데이터 없음.
- **TOSS_TOP30_TICKERS**: 수동 / 미설정 시 bonus 미적용. v0.3 스크래핑 도입.
