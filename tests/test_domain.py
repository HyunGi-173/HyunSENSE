from datetime import date, timedelta
import pytest
from domain import (blank_data, sample_data, period_summary, next_occasion, upcoming,
                    mission_ideas, dumps, loads, validated_data)


def period(on, end="", note=""):
    return {"id": on, "start": on, "end": end, "note": note}


def test_empty_period():
    assert period_summary([], 29, 5, date(2026, 11, 13)) is None


def test_period_predicted_median():
    rows = [period("2026-08-01"), period("2026-08-30"), period("2026-09-28"), period("2026-10-27")]
    d = period_summary(rows, 30, 5, date(2026, 11, 13))
    assert d["cycle"] == 29
    assert d["next"] == date(2026, 11, 25)
    assert d["stale"] is False


def test_stale_period_disables_prediction():
    x = period_summary([period("2026-01-01")], 29, 5, date(2026, 11, 13))
    assert x["stale"] and x["next"] is None


def test_unrecorded_cycles_do_not_produce_a_fabricated_future_prediction():
    x = period_summary([period("2026-09-01")], 28, 5, date(2026, 11, 13))
    assert x["next"] is None
    assert x["needs_update"] is True
    assert x["stale"] is False


def test_annual_anniversary_with_leap_day():
    assert next_occasion("2024-02-29", True, date(2027, 1, 1)) == date(2027, 2, 28)
    assert next_occasion("2024-02-29", True, date(2028, 2, 28)) == date(2028, 2, 29)


def test_one_time_past_filtered():
    assert next_occasion("2024-01-01", False, date(2026, 1, 1)) is None


def test_upcoming_sorted():
    data = sample_data(date(2026, 11, 13))
    values = upcoming(data["anniversaries"], date(2026, 11, 13))
    assert values == sorted(values, key=lambda t: (t["days"], t["title"]))


def test_missions_are_respectful():
    data = sample_data(date(2026, 11, 13))
    ideas = mission_ideas(data, date(2026, 11, 13))
    assert len(ideas) >= 2
    assert all("기분이 나쁠" not in x["title"] for x in ideas)


def test_roundtrip_json():
    original = sample_data(date(2026, 11, 13))
    assert loads(dumps(original)) == original


def test_import_invalid_date_rejected():
    original = sample_data(date(2026, 11, 13))
    original["periods"][0]["start"] = "2026-88-99"
    with pytest.raises(ValueError):
        validated_data(original)


def test_import_oversized_rejected():
    with pytest.raises(ValueError):
        loads(b"{" + b" " * 2_000_000 + b"}")


def test_forged_script_text_is_data_only():
    original = blank_data()
    original["favorites"].append({"id": "a", "category": "좋아하는 것", "title": "<script>alert(1)</script>", "note": ""})
    assert loads(dumps(original))["favorites"][0]["title"] == "<script>alert(1)</script>"
