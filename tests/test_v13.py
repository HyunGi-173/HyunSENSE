"""HyunSENSE v1.3 — privacy, reminders and conservative forecast regressions."""
from datetime import date, timedelta

import pytest

from domain import (blank_data, sample_data, validated_data, dumps, loads,
                    revoke_period_consent, purge_health_session_buffers, period_summary)
from reminder_calendar import build_calendar_export, _fold, _escape
from security import hash_password, verify_password

TODAY = date(2026, 11, 13)


def synthetic():
    return sample_data(TODAY)


def test_calendar_is_generic_and_never_exports_health_or_chat():
    d = synthetic()
    d['profile']['care_notes'] = '비밀월경노트'
    d['periods'][0]['note'] = '건강비밀단어'
    d['anniversaries'][0]['title'] = '특별한비밀기념일'
    d['missions'][0]['note'] = '민감한메모'
    ics = build_calendar_export(d, today=TODAY).decode('utf-8')
    assert 'BEGIN:VCALENDAR' in ics
    assert '개인 기념일' in ics and '개인 할 일' in ics
    for secret in ('비밀월경노트', '건강비밀단어', '특별한비밀기념일', '민감한메모', '달님'):
        assert secret not in ics


def test_calendar_only_one_reminder_for_each_mission_and_three_events():
    d = synthetic()
    ics = build_calendar_export(d, today=TODAY).decode()
    assert ics.count('BEGIN:VEVENT') == 3
    assert ics.count('BEGIN:VALARM') == 5
    assert 'TRIGGER:-P7D' in ics and 'TRIGGER:-P1D' in ics
    assert 'CLASS:PRIVATE' in ics


def test_calendar_title_opt_in_escapes_and_folds_unicode():
    d = blank_data()
    d['anniversaries'] = [{'id': 'evt', 'title': '꽃,카페;편지\\\n' + ('축하' * 90),
                           'day': '2026-11-15', 'category': '우리의 날', 'annual': False, 'note': ''}]
    raw = build_calendar_export(d, today=TODAY, include_titles=True)
    assert b'SUMMARY:' in raw
    assert '\\,' in raw.decode()
    assert '\\;' in raw.decode()
    assert '\\n' in raw.decode()
    assert all(len(part) <= 75 for part in raw.split(b'\r\n') if part)


def test_calendar_filters_completed_past_and_far_future():
    d = blank_data()
    d['missions'] = [
        {'id': 'done', 'title': '완료', 'due': '2026-11-14', 'done': True, 'note': ''},
        {'id': 'old', 'title': '지남', 'due': '2026-11-01', 'done': False, 'note': ''},
        {'id': 'future', 'title': '멀리', 'due': '2029-11-20', 'done': False, 'note': ''},
    ]
    assert build_calendar_export(d, today=TODAY).decode().count('BEGIN:VEVENT') == 0


def test_calendar_rejects_invalid_horizon():
    for days in (0, -1, 731):
        with pytest.raises(ValueError):
            build_calendar_export(blank_data(), today=TODAY, days=days)


def test_inconsistent_consent_archive_is_rejected():
    d = synthetic()
    d['profile']['consent'] = False
    with pytest.raises(ValueError, match='동의'):
        validated_data(d)
    with pytest.raises(ValueError, match='동의'):
        dumps(d)


def test_purging_consent_deletes_periods_and_related_session_copies():
    d = synthetic()
    session = {
        'last_deleted': {'section': 'periods', 'row': d['periods'][0]},
        'pending_deleted': {'section': 'periods', 'row': d['periods'][1]},
        'unsaved_draft': d.copy(),
        'undo_pending': True,
        'unrelated': 'keep',
    }
    revoke_period_consent(d)
    purge_health_session_buffers(session)
    assert d['periods'] == [] and d['profile']['care_notes'] == ''
    assert d['profile']['consent'] is False
    assert loads(dumps(d)) == d
    assert session == {'unrelated': 'keep'}


def test_non_health_undo_is_not_cleared_on_consent_revocation():
    session = {'last_deleted': {'section': 'favorites', 'row': {'id': 'a'}}, 'unsaved_draft': {}}
    purge_health_session_buffers(session)
    assert session['last_deleted']['section'] == 'favorites'
    assert 'unsaved_draft' not in session


def test_forecast_if_expected_start_already_passed_is_not_projected():
    p = [{'start': '2026-10-01'}]
    x = period_summary(p, 29, 5, TODAY)
    assert x['needs_update'] and x['next'] is None


def test_forecast_future_keeps_single_next_date():
    p = [{'start': '2026-11-01'}]
    x = period_summary(p, 29, 5, TODAY)
    assert x['next'] == date(2026, 11, 30)
    assert not x['needs_update']


def test_malformed_password_hash_does_not_crash_login():
    for value in ('pbkdf2_sha256$600000$%%%$$', 'pbkdf2_sha256$600000$%%%$!!', 'not-a-hash'):
        assert verify_password('password-long-enough', value) is False


def test_idle_session_boundary_and_clock_reset():
    from auth_limiter import session_timed_out
    assert not session_timed_out(10.0, 1810.0)
    assert session_timed_out(10.0, 1810.01)
    assert session_timed_out(100.0, 20.0)


def test_giant_login_input_is_rejected_early():
    secret = hash_password('password-long-enough')
    assert verify_password('x' * 5000, secret) is False
    with pytest.raises(ValueError):
        hash_password('x' * 5000)
