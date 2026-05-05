# Pre-Surge Daily Picker

토스앱 운용 미국 중소형주 사전 급등 신호 포착 시스템 (v0.2).
매일 한국시간 09:00 KST에 시가총액 $200M~$10B 미국 보통주 중 PSS 점수 상위 종목을 watchlist로 산출, 텔레그램으로 푸시한다.

## 문서

- [전략 v0.2 원본](docs/strategy_v0.2.md) — 시장 가설, 6개 패턴, 리스크 룰
- [개발 전략](docs/DEVELOPMENT_STRATEGY.md) — 8주 로드맵, 아키텍처, 데이터 모델

## 빠른 시작

```bash
# 1. Python 3.11+ 가상환경
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. 환경 변수
cp .env.example .env
# .env 편집: POLYGON_API_KEY, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN 등

# 3. DB 초기화
python -m src.storage.db --init

# 4. universe seed (시총 200M~10B 종목 마스터 적재)
python -m scripts.bootstrap_universe

# 5. 일일 실행 (수동)
python -m src.runner --mode=daily

# 6. 테스트
pytest
```

## 아키텍처

```
GitHub Actions cron (00:00 UTC)
    -> src.runner
       -> ingest.* (EDGAR, Polygon, Reddit, ...)
       -> score.pattern_a~f -> pss_aggregator
       -> storage (SQLite/Supabase)
       -> report.claude_summarizer -> telegram_pusher
```

상세 컴포넌트와 데이터 모델은 `docs/DEVELOPMENT_STRATEGY.md` 참조.

## 운용 룰

- **Tier 1 (PSS ≥ 70 + 패턴 2개+)**: 매수 후보, 자본 5~7% 진입, 최대 3종목
- **Tier 2 (PSS ≥ 50)**: 관찰
- **Tier 3 (PSS ≥ 30)**: 모니터
- **진입 트리거**: RVOL ≥ 2x + VWAP 상회 + 카탈리스트 ≤ 72h
- **청산**: +30/+50/+70% 분할, 5일 무반응 시 전량, -7% 손절

면책: 본 시스템은 watchlist 도구. 최종 매매 결정과 책임은 사용자.
