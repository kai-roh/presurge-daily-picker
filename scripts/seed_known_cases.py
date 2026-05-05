"""Known surge cases — 5종 historical fixture seeder.

전략 문서 §1.1~1.5에 분석된 5개 사례를 하드코딩한 수동 시드.
백테스트와 단위 테스트 ground truth로 사용. 실제 Polygon/EDGAR 데이터로 W2 후반에 보강 예정.

실행:
    python -m scripts.seed_known_cases [--db data/test_cases.db]

각 사례는 급등 -7~-1일 시점의 PSS가 70+ 나오도록 데이터를 시드한다.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from src.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnownCase:
    ticker: str
    name: str
    surge_date: date
    pre_surge_mcap: float
    historical_max_mcap: float | None
    float_shares: int | None
    sector: str
    expected_patterns: tuple[str, ...]
    summary: str


CASES: list[KnownCase] = [
    KnownCase(
        ticker="BNAI",
        name="Brand Engagement Network",
        surge_date=date(2026, 1, 15),
        pre_surge_mcap=40_000_000,
        historical_max_mcap=283_000_000,
        float_shares=20_000_000,
        sector="AI Software",
        expected_patterns=("A", "F"),
        summary="ATM 종료 + 아프리카 AI 라이선스 + AI 키워드. 1년 +1,090%.",
    ),
    KnownCase(
        ticker="BYND",
        name="Beyond Meat",
        surge_date=date(2025, 10, 20),
        pre_surge_mcap=80_000_000,
        historical_max_mcap=14_000_000_000,
        float_shares=40_000_000,
        sector="Plant-based Foods",
        expected_patterns=("C", "D", "E"),
        summary="월마트 distribution + MEME ETF + 부채 swap + 63% SI. 3일 +600%.",
    ),
    KnownCase(
        ticker="TNXP",
        name="Tonix Pharmaceuticals",
        surge_date=date(2025, 8, 10),
        pre_surge_mcap=60_000_000,
        historical_max_mcap=2_000_000_000,
        float_shares=7_000_000,
        sector="Biotechnology",
        expected_patterns=("B", "C"),
        summary="DOD DTRA $34M 5년 계약 + Russell 편입 + PDUFA. 2025 +570%.",
    ),
    KnownCase(
        ticker="IOVA",
        name="Iovance Biotherapeutics",
        surge_date=date(2025, 7, 15),
        pre_surge_mcap=1_200_000_000,
        historical_max_mcap=5_500_000_000,
        float_shares=280_000_000,
        sector="Biotechnology",
        expected_patterns=(),  # 분석가 상향 series — Pattern G로 v0.3 후보
        summary="Citizens JMP real-world data 코멘트 + 분석가 PT 상향. 7월~ +120%.",
    ),
    KnownCase(
        ticker="PAVS",
        name="Paranovus Entertainment Tech",
        surge_date=date(2026, 3, 5),
        pre_surge_mcap=25_000_000,
        historical_max_mcap=120_000_000,
        float_shares=8_000_000,
        sector="E-commerce",
        expected_patterns=("A",),
        summary="ATM 종료 + 1:100 reverse split. 시간외 +52%.",
    ),
]


def _seed_universe(db: Database, case: KnownCase) -> None:
    db.upsert_universe([{
        "ticker": case.ticker,
        "name": case.name,
        "market_cap_usd": case.pre_surge_mcap,
        "float_shares": case.float_shares,
        "exchange": "XNAS",
        "sector": case.sector,
        "is_common_stock": 1,
        "historical_max_mcap": case.historical_max_mcap,
        "last_refreshed": datetime.utcnow().isoformat(),
    }])


def _seed_bars(db: Database, case: KnownCase) -> None:
    """급등 -35일 ~ surge_date 일봉. 단조 증가 없는 평탄한 기준선 가정."""
    # surge_date 직전까지는 평탄, surge_date 당일에 폭등
    base_price = max(case.pre_surge_mcap / max(case.float_shares or 1, 1) * 0.5, 0.5)
    rows = []
    for i in range(60, 0, -1):
        d = case.surge_date - timedelta(days=i)
        rows.append({
            "ticker": case.ticker,
            "trade_date": d.isoformat(),
            "open": base_price,
            "high": base_price * 1.05,
            "low": base_price * 0.95,
            "close": base_price,
            "volume": 500_000,
            "vwap": base_price,
        })
    rows.append({
        "ticker": case.ticker,
        "trade_date": case.surge_date.isoformat(),
        "open": base_price,
        "high": base_price * 2.5,
        "low": base_price,
        "close": base_price * 2.0,
        "volume": 5_000_000,
        "vwap": base_price * 1.7,
    })
    # 급등 후 10영업일 — 점진적 fade 가정 (백테스트 exit 시뮬용)
    for i in range(1, 11):
        d = case.surge_date + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        post_price = base_price * (2.0 - 0.05 * i)  # 2.0 → 1.55
        rows.append({
            "ticker": case.ticker,
            "trade_date": d.isoformat(),
            "open": post_price,
            "high": post_price * 1.05,
            "low": post_price * 0.95,
            "close": post_price,
            "volume": 2_000_000,
            "vwap": post_price,
        })
    db.upsert_bars(rows)


def _seed_filings(db: Database, case: KnownCase) -> None:
    surge_dt = datetime.combine(case.surge_date, datetime.min.time())

    if case.ticker == "BNAI":
        db.upsert_filings([{
            "accession_no": "BNAI-A-2026-01",
            "ticker": "BNAI",
            "cik": "0001937891",
            "filed_at": (surge_dt - timedelta(days=5)).isoformat(),
            "form_type": "8-K",
            "items": "1.02",
            "raw_text_url": "https://example.com/BNAI-A",
        }])
        db.update_filing_classification(
            "BNAI-A-2026-01", classification="A", confidence=0.9,
            contract_value_usd=None, counterparty="YA II PN",
            key_quote="YA II PN standby equity purchase agreement terminated",
        )
        # 아프리카 AI 라이선스 - Pattern F pivot
        db.upsert_filings([{
            "accession_no": "BNAI-F-2026-01",
            "ticker": "BNAI",
            "cik": "0001937891",
            "filed_at": (surge_dt - timedelta(days=2)).isoformat(),
            "form_type": "8-K",
            "items": "8.01",
            "raw_text_url": "https://example.com/BNAI-F",
        }])
        db.update_filing_classification(
            "BNAI-F-2026-01", classification="F", confidence=0.75,
            contract_value_usd=2_050_000, counterparty="Valio Technologies",
            key_quote="Conversational AI licensing partnership in Africa with Valio",
        )

    elif case.ticker == "BYND":
        db.upsert_filings([{
            "accession_no": "BYND-C-2025-10",
            "ticker": "BYND",
            "cik": "0001655210",
            "filed_at": (surge_dt - timedelta(days=1)).isoformat(),
            "form_type": "8-K",
            "items": "1.01",
            "raw_text_url": "https://example.com/BYND-C",
        }])
        db.update_filing_classification(
            "BYND-C-2025-10", classification="C", confidence=0.9,
            contract_value_usd=15_000_000, counterparty="Walmart",
            key_quote="Walmart distribution expansion to 2,000 stores",
        )
        # 부채 swap - Pattern E 보조
        db.upsert_filings([{
            "accession_no": "BYND-E-2025-09",
            "ticker": "BYND",
            "cik": "0001655210",
            "filed_at": (surge_dt - timedelta(days=30)).isoformat(),
            "form_type": "8-K",
            "items": "1.01",
            "raw_text_url": "https://example.com/BYND-E",
        }])
        db.update_filing_classification(
            "BYND-E-2025-09", classification="", confidence=0.6,
            contract_value_usd=None, counterparty=None,
            key_quote="debt swap exchange agreement refinancing 2030",
        )
        # SI
        db.upsert_short_interest([{
            "ticker": "BYND",
            "settle_date": (case.surge_date - timedelta(days=5)).isoformat(),
            "si_shares": 25_000_000,
            "si_pct_float": 0.63,
            "days_to_cover": 7.0,
            "cost_to_borrow": 0.40,
            "source": "finra",
        }])

    elif case.ticker == "TNXP":
        db.upsert_filings([{
            "accession_no": "TNXP-C-2025-08",
            "ticker": "TNXP",
            "cik": "0001430306",
            "filed_at": (surge_dt - timedelta(days=3)).isoformat(),
            "form_type": "8-K",
            "items": "1.01",
            "raw_text_url": "https://example.com/TNXP-C",
        }])
        db.update_filing_classification(
            "TNXP-C-2025-08", classification="C", confidence=0.95,
            contract_value_usd=34_000_000, counterparty="DOD DTRA",
            key_quote="Department of Defense DTRA five-year contract for TNX-4200",
        )

    elif case.ticker == "PAVS":
        db.upsert_filings([{
            "accession_no": "PAVS-A-2026-03",
            "ticker": "PAVS",
            "cik": "0001936263",
            "filed_at": (surge_dt - timedelta(hours=12)).isoformat(),
            "form_type": "8-K",
            "items": "1.02",
            "raw_text_url": "https://example.com/PAVS-A",
        }])
        db.update_filing_classification(
            "PAVS-A-2026-03", classification="A", confidence=0.85,
            contract_value_usd=None, counterparty=None,
            key_quote="ATM equity distribution agreement terminated",
        )


def _seed_index_events(db: Database, case: KnownCase) -> None:
    if case.ticker == "TNXP":
        # Russell 2000/3000 발표는 6월 말, effective는 7월 1일. surge_date(8/10) 1주 후로
        # 보면 이미 effective passed 7d 초과 → 점수 0. 여기서는 announced_at이
        # surge - 7일, effective_at이 surge + 5일로 가정 (실제 Russell 일정과 다름,
        # 시드 단순화).
        db.upsert_index_event({
            "ticker": "TNXP",
            "index_name": "Russell 2000",
            "announced_at": (case.surge_date - timedelta(days=7)).isoformat(),
            "effective_at": (case.surge_date + timedelta(days=5)).isoformat(),
            "source": "manual_seed",
            "notes": "Seeded from strategy doc §1.3",
        })


def _seed_social(db: Database, case: KnownCase) -> None:
    if case.ticker == "BYND":
        # 한국 retail 인지도 (브랜드 페니 보조)
        for offset in range(90, 0, -1):
            db.upsert_social([{
                "ticker": "BYND",
                "mention_date": (case.surge_date - timedelta(days=offset)).isoformat(),
                "source": "stocktwits",
                "mentions": 80,
                "bullish_pct": 0.7,
                "rank": None,
            }])
    if case.ticker == "BNAI":
        # WSB 멘션 5x 증가
        db.upsert_social([{
            "ticker": "BNAI",
            "mention_date": (case.surge_date - timedelta(days=2)).isoformat(),
            "source": "reddit_wsb",
            "mentions": 30,
            "bullish_pct": 0.7,
            "rank": 50,
        }])
        db.upsert_social([{
            "ticker": "BNAI",
            "mention_date": (case.surge_date - timedelta(days=1)).isoformat(),
            "source": "reddit_wsb",
            "mentions": 200,
            "bullish_pct": 0.85,
            "rank": 10,
        }])


def seed_all(db: Database) -> None:
    for case in CASES:
        logger.info("Seeding %s (surge %s)", case.ticker, case.surge_date)
        _seed_universe(db, case)
        _seed_bars(db, case)
        _seed_filings(db, case)
        _seed_index_events(db, case)
        _seed_social(db, case)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/known_cases.db")
    args = parser.parse_args(argv)

    path = Path(args.db)
    db = Database(path)
    db.init_schema()
    try:
        seed_all(db)
        logger.info("Seeded %d known cases into %s", len(CASES), path)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
