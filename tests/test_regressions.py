"""Regression tests for bugs identified in the v1.1 audit."""
import copy
import os
import sqlite3
import tempfile
from datetime import date

import pytest
from cryptography.fernet import Fernet

from domain import (blank_data, validated_data, sample_data,
                    valid_period_entry, validate_new_period, today_kst)
from mood_engine import parse_conversation, analyze
from security import encrypt_backup, decrypt_backup
from store import SecureStore, SaveConflictError


def test_deny_duplicate_ids_and_empty_id():
    d = blank_data()
    d['favorites'] = [
        {'id': 'same', 'category': '좋아하는 것', 'title': 'A', 'note': ''},
        {'id': 'same', 'category': '좋아하는 것', 'title': 'B', 'note': ''},
    ]
    with pytest.raises(ValueError, match='중복'):
        validated_data(d)
    d['favorites'][1]['id'] = ''
    with pytest.raises(ValueError, match='식별자'):
        validated_data(d)


def test_invalid_enums_are_rejected():
    d = blank_data()
    d['date_ideas'] = [{'id': '123', 'title': '동네 산책', 'area': '', 'budget': 0,
                        'status': '부정확한 문자열', 'note': ''}]
    with pytest.raises(ValueError, match='상태'):
        validated_data(d)
    d['date_ideas'][0]['status'] = '가고 싶어요'
    d['favorites'] = [{'id': '456', 'title': '꽃', 'category': 'INVALID', 'note': ''}]
    with pytest.raises(ValueError, match='카테고리'):
        validated_data(d)


def test_reject_implausible_period_duration_and_duplicate_start():
    today = date(2026, 11, 13)
    with pytest.raises(ValueError):
        valid_period_entry('2026-01-01', '2026-12-31', today=today)
    with pytest.raises(ValueError, match='오늘'):
        valid_period_entry('2027-02-01', today=today)
    existing = [{'id': 'same1', 'start': '2026-11-01', 'end': '', 'note': ''}]
    with pytest.raises(ValueError, match='동일한'):
        validate_new_period(existing, '2026-11-01', today=today)
    validate_new_period(existing, '2026-11-01', exclude_id='same1', today=today)


def test_kakao_multiline_continuation():
    text = '[상대] [오후 8:12] 오늘 너무 행복해\n그리고 맛있는 거 먹었어\n[나] [오후 8:13] 잘됐다'
    parsed = parse_conversation(text)
    assert len(parsed) == 2
    assert parsed[0]['speaker'] == '상대'
    assert '맛있는 거' in parsed[0]['text']


@pytest.mark.parametrize('sentence', [
    '상대: 요즘 힘들지 않아. 아주 괜찮아.',
    '상대: 동생이 오늘 힘들었대.',
    '상대: 힘들지 않아요.',
    '상대: 피곤하지?',
    '상대: 친구가 너무 스트레스 받더라.',
])
def test_no_self_mood_assumption_from_negation_other_or_question(sentence):
    r = analyze(parse_conversation(sentence), '상대')
    assert r['dominant'] == '알 수 없음'


def test_true_positive_still_detected():
    r = analyze(parse_conversation('상대: 오늘 너무 피곤하고 지쳤어. 좀 쉬고 싶어.'), '상대')
    assert r['dominant'] == '피로·휴식'


def test_revision_conflict_keeps_first_write():
    with tempfile.TemporaryDirectory() as td:
        store = SecureStore(Fernet.generate_key().decode(), sqlite_path=os.path.join(td, 'diary.db'))
        initial = sample_data(today_kst())
        rev = store.save(initial, expected_revision=0)
        a, ra = store.load_with_revision()
        b, rb = store.load_with_revision()
        assert (rev, ra, rb) == (1, 1, 1)
        a['profile']['owner_name'] = '첫 기기'
        store.save(a, expected_revision=ra)
        b['profile']['partner_name'] = '두번째 기기'
        with pytest.raises(SaveConflictError):
            store.save(b, expected_revision=rb)
        assert store.load()['profile']['owner_name'] == '첫 기기'
        assert store.load()['profile']['partner_name'] != '두번째 기기'


def test_legacy_sqlite_revision_migration():
    with tempfile.TemporaryDirectory() as td:
        filename = os.path.join(td, 'older.db')
        con = sqlite3.connect(filename)
        con.execute('CREATE TABLE hyunsense_data (id TEXT PRIMARY KEY, payload BLOB NOT NULL)')
        con.commit(); con.close()
        store = SecureStore(Fernet.generate_key().decode(), sqlite_path=filename)
        assert store.load_with_revision() == (None, 0)
        assert store.save(blank_data(), expected_revision=0) == 1
        assert store.load_with_revision()[1] == 1


def test_encrypted_backup_no_plaintext_and_wrong_passphrase():
    secret = 'my-private-backup-password'
    raw = '마법의 성 기록: 매우 개인적인 문구'.encode('utf-8')
    encrypted = encrypt_backup(raw, secret)
    assert raw not in encrypted
    assert decrypt_backup(encrypted, secret) == raw
    with pytest.raises(ValueError, match='틀렸거나'):
        decrypt_backup(encrypted, 'an-incorrect-password')


def test_migrated_existing_row_revision_zero_can_save():
    from domain import dumps
    from security import encrypt_json
    with tempfile.TemporaryDirectory() as td:
        filename = os.path.join(td, 'legacy.db')
        key = Fernet.generate_key().decode()
        old = sample_data(today_kst())
        con = sqlite3.connect(filename)
        con.execute('CREATE TABLE hyunsense_data (id TEXT PRIMARY KEY, payload BLOB NOT NULL)')
        con.execute('INSERT INTO hyunsense_data(id,payload) VALUES (?,?)',
                    ('owner', encrypt_json(dumps(old), key)))
        con.commit(); con.close()
        store = SecureStore(key, sqlite_path=filename)
        loaded, rev = store.load_with_revision()
        assert rev == 0
        loaded['profile']['owner_name'] = '마이그레이션 후 저장 성공'
        assert store.save(loaded, expected_revision=rev) == 1
        assert store.load()['profile']['owner_name'] == '마이그레이션 후 저장 성공'


def test_stale_zero_revision_delete_cannot_remove_new_record():
    with tempfile.TemporaryDirectory() as td:
        store = SecureStore(Fernet.generate_key().decode(), sqlite_path=os.path.join(td, 'records.db'))
        store.save(blank_data(), expected_revision=0)
        with pytest.raises(SaveConflictError):
            store.delete(expected_revision=0)
        assert store.load() is not None


def test_analysis_fingerprint_changes_with_transcript_speaker_and_consent():
    from mood_engine import analysis_fingerprint
    args = ('오늘 너무 좋아', '상대', True, '붙여넣기', '직접 물어보기', '')
    base = analysis_fingerprint(*args)
    assert len(base) == 64
    assert base != analysis_fingerprint('오늘 너무 힘들어', *args[1:])
    assert base != analysis_fingerprint(args[0], '나', *args[2:])
    assert base != analysis_fingerprint(args[0], args[1], False, *args[3:])


def test_login_process_throttle():
    from auth_limiter import login_allowed, record_login_failure, record_login_success
    record_login_success()
    for i in range(6):
        record_login_failure(now=100 + i)
    allowed, secs = login_allowed(now=110)
    assert not allowed and secs > 0
    assert login_allowed(now=2000)[0]
    record_login_success()
