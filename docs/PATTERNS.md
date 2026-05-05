# PSS 패턴 룰북 v0.2

작성: 2026-05-05 / Universe floor: $20M / 가중치 출처: `src/config.py`

전략 v0.2의 6개 사전 급등 시그널을 코드 레벨로 명세한 단일 출처. 가중치 변경은 본 문서와 `src/config.py`를 동시에 갱신해야 한다.

## 공통 인터페이스

모든 패턴 스코어러는 `src/score/base.py::PatternScorer`를 상속한다.

```python
class PatternScorer(ABC):
    name: str
    letter: str           # "A".."F"
    max_score: float
    trigger_threshold: float

    def compute(self, ticker, as_of, db) -> PatternScore
```

`PatternScore`는 `score`, `triggered`, `contributing_signals`(디버깅용 raw)를 담는다.

## Pattern A — Dilution Shutdown (max 30)

**가설**: ATM, standby equity 등 만성 매도 메커니즘이 종료되면 매도벽 제거 → 작은 호재로도 반등.

| 조건 | 점수 |
|---|---|
| 24시간 내 8-K Item 1.02 + 키워드 매칭 | +30 |
| 7일 내 (24h 외) 8-K Item 1.02 + 키워드 매칭 | +20 |
| 직전 6개월 발행주식수 증가율 < 5% | +5 (보너스) |

**키워드** (`PATTERN_A_KEYWORDS`):
`atm termination`, `at the market termination`, `equity purchase agreement terminated`, `standby equity`, `equity distribution agreement terminated`, `termination of sales agreement`

**Trigger**: score ≥ 20

**알려진 사례**: BNAI (YA II PN standby equity 종료), PAVS (ATM 종료)

**한계**: 발행주식수 증가율 프록시는 v0.2 MVP에서 항상 None. W3에서 historical Polygon ticker details로 보강.

## Pattern B — Index / ETF Inclusion (max 25 + 5 보너스 = 30 효과)

**가설**: Russell 2000/3000, MEME ETF 등 신규 편입 시 패시브 매수 유입.

| 조건 | 점수 |
|---|---|
| announced_at ≤ as_of < effective_at + 7d | +25 |
| effective_at 통과 후 1주일 (위 조건 불일치) | +15 |
| 시총 < $500M | +5 (보너스) |

**Trigger**: score ≥ 15

**데이터 소스**: `index_inclusion_events` 테이블. `db.upsert_index_event(...)` 로 적재. v0.2에서는 known case 수동 시드 + W2 후반에 FTSE Russell IR 페이지 + Roundhill csv 페처 추가.

**알려진 사례**: TNXP (Russell 2000 편입), BYND (Roundhill MEME ETF)

## Pattern C — Government / Tier-1 Contract (max 50)

**가설**: 매출 거의 없는 small-cap이 정부/대형 retailer와 계약 → 매출 가시성 + 신뢰성 점프.

| ratio = contract / market_cap | 점수 |
|---|---|
| ≥ 10% | 50 |
| ≥ 5% | 35 |
| ≥ 2% | 20 |
| > 0 | 10 |
| 분류만 됐고 금액 추출 실패 | 10 (보수적 fallback) |

**보너스**: 정부 기관 카운터파티(DOD/NIH/BARDA/DTRA/NASA/DARPA) → +5 (clamp at max)

**키워드**:
- 정부 (`PATTERN_C_GOV_KEYWORDS`): `dod`, `department of defense`, `nih`, `barda`, `dtra`, `nasa`, `darpa`
- Retail (`PATTERN_C_RETAIL_KEYWORDS`): `walmart`, `costco`, `amazon`, `target`, `kroger`, `home depot`

**Trigger**: score ≥ 20

**알려진 사례**: TNXP (DOD DTRA $34M 5년), BYND (Walmart 2,000개 매장)

**한계**: contract_value_usd 추출 정확도가 Claude 분류 품질에 의존. 분류 confidence < 0.6은 이후 -5 페널티 검토 (W4 튜닝).

## Pattern D — Short Squeeze Setup (max 30)

**가설**: 높은 SI + 저float + 신선 카탈리스트 → 강제 커버링 폭발.

| 시그널 | 점수 |
|---|---|
| SI% × 0.5 (clamp 15) | +0~15 |
| DTC × 1.5 (clamp 9) | +0~9 |
| CTB × 0.1 (clamp 6) | +0~6 |
| (50 - Float_M) × 0.1 | +0~5 (음수면 0) |
| 직전 30일 가격 -30% 이하 | +5 (shorts 자만 보너스) |

**임계치 (입력 자격)**:
- SI% ≥ 15%, DTC ≥ 4, CTB ≥ 30%, Float ≤ 50M주 중 하나라도 만족해야 시그널 진입
- 모두 미달 시 score=0

**Trigger**: score ≥ 18

**알려진 사례**: BYND (SI 63%, DTC 7, CTB 40%)

**한계**: CTB(cost to borrow)는 무료 데이터 소스 부재. v0.2 MVP는 FINRA SI만 사용 (CTB=0). Ortex 도입 시 정확도 상승.

## Pattern E — Brand Penny (max 25)

**가설**: 한때 메가캡이었던 잘 알려진 브랜드가 페니로 전락 → retail bottom-fishing 누적 → 작은 호재로 폭발.

**자격 조건** (모두 만족):
- 시총 / historical_max_mcap ≤ 10%
- 가격 $1 ≤ close ≤ $5

자격 미달 시 score=0.

**점수 계산**:
- (1 - recovery) × 20 (예: recovery 0.05 → 19점)
- 직전 90일 평균 멘션 ≥ 50건이면 + min(mentions × 0.05, 5)
- 부채 swap/refinancing/covenant 8-K 6개월 내 → +5

**Trigger**: score ≥ 15

**알려진 사례**: BYND (peak $14B → $80M = 0.6% recovery), TUP, KSS류

**한계**: historical_max_mcap는 universe 적재 시 별도 backfill 필요. Polygon `/v2/aggs` 30년 historical 또는 yfinance.

## Pattern F — Megatheme + AI Keyword (max 25)

**가설**: AI, 양자컴퓨팅, 우주 등 메가테마와 회사 사업의 연결 → retail 흥분.

| 시그널 | 점수 |
|---|---|
| 회사명/sector에 메가테마 키워드 포함 | min(hits × 5, 15) |
| 30일 내 8-K Item 8.01 + classification F or confidence ≥ 0.6 | +5 |
| 위와 같은 8-K이지만 confidence < 0.6 (vague) | -3 (키워드 stuffing 페널티) |
| WSB 멘션 24h 5x 이상 증가 | +5 |

**메가테마 키워드** (`MEGATHEME_KEYWORDS`):
`ai`, `artificial intelligence`, `agentic`, `llm`, `quantum`, `qubit`, `glp-1`, `glp1`, `obesity drug`, `fusion`, `lithium`, `uranium`, `robotics`, `humanoid`, `space`

**Trigger**: score ≥ 12

**알려진 사례**: BNAI (AI software + Valio AI 라이선스 + WSB 멘션 5x)

**한계**: 키워드 stuffing 사기 방지가 핵심. confidence < 0.6 페널티만으로 부족할 수 있음 → 실제 매출 대비 사업 비중 확인 (W4).

## PSS Total 산출

```
base = A + B + C + D + E + F
bonus_toss     = +10 if 직전 7일 토스 거래량 top30 진입
penalty_run    = -30 if 직전 30일 +50% 이상 상승
penalty_earn   = -20 if 7일 내 earnings (v0.3)

total = max(0, base + bonus_toss + penalty_run + penalty_earn)
```

## Tier 분류 + 캡

| Tier | 임계치 | 캡 |
|---|---|---|
| 1 | PSS ≥ 70 + 패턴 2개+ triggered | 최대 3종목 |
| 2 | PSS ≥ 50 | 최대 5종목 |
| 3 | PSS ≥ 30 | 최대 10종목 |

캡 적용은 PSS 내림차순 정렬 후 상위 N개 채택.

## 가중치 변경 프로토콜

1. `src/config.py` 상수 변경
2. 본 문서 해당 섹션 동시 갱신
3. `tests/integration/test_known_cases.py` 5종 사례 모두 통과 확인
4. 백테스트 (24개월)로 H1~H4 가설 hold-out 검증
5. PR 본문에 변경 전후 hit_rate / Spearman 표 첨부
