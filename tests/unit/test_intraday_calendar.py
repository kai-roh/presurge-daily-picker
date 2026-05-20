from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.intraday.calendar import session_for

ET = ZoneInfo("America/New_York")


def test_regular_session_default_only_opens_regular_hours():
    premarket = datetime(2026, 5, 18, 8, 0, tzinfo=ET)
    regular = datetime(2026, 5, 18, 10, 0, tzinfo=ET)

    assert not session_for(premarket).is_open
    assert session_for(regular).is_open


def test_extended_session_labels_pre_and_post_market():
    premarket = session_for(
        datetime(2026, 5, 18, 8, 0, tzinfo=ET),
        include_extended=True,
    )
    postmarket = session_for(
        datetime(2026, 5, 18, 17, 0, tzinfo=ET),
        include_extended=True,
    )

    assert premarket.is_open
    assert premarket.session_label == "premarket"
    assert postmarket.is_open
    assert postmarket.session_label == "postmarket"
