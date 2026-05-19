"""미국 정규장 시간 helper.

MVP에서는 외부 market-calendar 의존성을 피하고 NYSE 정규장 시간만 본다.
휴장일 정밀 처리는 v0.4.1에서 exchange_calendars/pandas-market-calendars로 보강한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


@dataclass(frozen=True)
class MarketSession:
    trade_date: date
    is_open: bool
    now_et: datetime
    open_et: datetime
    close_et: datetime


def now_et() -> datetime:
    return datetime.now(tz=ET)


def session_for(ts: datetime | None = None) -> MarketSession:
    """ts 기준 정규장 상태. ts가 naive면 UTC로 간주한다."""
    if ts is None:
        ts_et = now_et()
    elif ts.tzinfo is None:
        ts_et = ts.replace(tzinfo=UTC).astimezone(ET)
    else:
        ts_et = ts.astimezone(ET)

    d = ts_et.date()
    open_et = datetime.combine(d, REGULAR_OPEN, tzinfo=ET)
    close_et = datetime.combine(d, REGULAR_CLOSE, tzinfo=ET)
    weekday_open = ts_et.weekday() < 5
    is_open = weekday_open and open_et <= ts_et <= close_et
    return MarketSession(
        trade_date=d,
        is_open=is_open,
        now_et=ts_et,
        open_et=open_et,
        close_et=close_et,
    )

