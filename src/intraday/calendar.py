"""미국 장 시간 helper.

MVP에서는 외부 market-calendar 의존성을 피하고 NYSE 장 시간만 본다.
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
EXTENDED_OPEN = time(4, 0)
EXTENDED_CLOSE = time(20, 0)


@dataclass(frozen=True)
class MarketSession:
    trade_date: date
    is_open: bool
    is_regular: bool
    is_extended: bool
    session_label: str
    now_et: datetime
    open_et: datetime
    close_et: datetime
    regular_open_et: datetime
    regular_close_et: datetime


def now_et() -> datetime:
    return datetime.now(tz=ET)


def session_for(ts: datetime | None = None, *, include_extended: bool = False) -> MarketSession:
    """ts 기준 장 상태. ts가 naive면 UTC로 간주한다."""
    if ts is None:
        ts_et = now_et()
    elif ts.tzinfo is None:
        ts_et = ts.replace(tzinfo=UTC).astimezone(ET)
    else:
        ts_et = ts.astimezone(ET)

    d = ts_et.date()
    regular_open_et = datetime.combine(d, REGULAR_OPEN, tzinfo=ET)
    regular_close_et = datetime.combine(d, REGULAR_CLOSE, tzinfo=ET)
    extended_open_et = datetime.combine(d, EXTENDED_OPEN, tzinfo=ET)
    extended_close_et = datetime.combine(d, EXTENDED_CLOSE, tzinfo=ET)
    open_et = extended_open_et if include_extended else regular_open_et
    close_et = extended_close_et if include_extended else regular_close_et
    weekday_open = ts_et.weekday() < 5
    is_regular = weekday_open and regular_open_et <= ts_et <= regular_close_et
    is_extended = weekday_open and extended_open_et <= ts_et <= extended_close_et
    is_open = is_extended if include_extended else is_regular
    if is_regular:
        label = "regular"
    elif is_extended and ts_et < regular_open_et:
        label = "premarket"
    elif is_extended:
        label = "postmarket"
    else:
        label = "closed"
    return MarketSession(
        trade_date=d,
        is_open=is_open,
        is_regular=is_regular,
        is_extended=is_extended,
        session_label=label,
        now_et=ts_et,
        open_et=open_et,
        close_et=close_et,
        regular_open_et=regular_open_et,
        regular_close_et=regular_close_et,
    )
