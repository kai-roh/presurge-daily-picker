from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.intraday.monitor import _is_quiet_kst

KST = ZoneInfo("Asia/Seoul")


def test_quiet_kst_window():
    assert _is_quiet_kst(datetime(2026, 5, 20, 3, 30, tzinfo=KST), "03:00", "06:00")
    assert not _is_quiet_kst(datetime(2026, 5, 20, 2, 59, tzinfo=KST), "03:00", "06:00")
    assert not _is_quiet_kst(datetime(2026, 5, 20, 6, 0, tzinfo=KST), "03:00", "06:00")
