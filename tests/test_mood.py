"""Unit tests for privacy-preserving conversation helper."""
from mood_engine import (parse_conversation, detected_speakers, analyze, recommend,
                         ai_summary_payload, decode_upload)


def test_mobile_parse_and_speaker_selection():
    data = '''--------------- 2026년 11월 13일 금요일 ---------------
[나] [오후 6:10] 오늘 어때?
[달님] [오후 6:11] 오늘 너무 피곤해서 좀 쉬고 싶어
[나] [오후 6:12] 쉬어도 돼
[달님] [오후 6:13] 고마워!'''
    records = parse_conversation(data)
    assert len(records) == 4
    assert set(detected_speakers(records)) == {'나', '달님'}
    result = analyze(records, partner_speaker='달님')
    assert result['dominant'] == '피로·휴식'
    assert result['message_count'] == 2


def test_desktop_parse():
    text = '2026-11-13 18:11:20, 영희 : 많이 힘들었어\n2026-11-13 18:11:35, 철수 : 아 그랬구나'
    records = parse_conversation(text)
    assert records == [{'speaker': '영희', 'text': '많이 힘들었어'},
                       {'speaker': '철수', 'text': '아 그랬구나'}]


def test_unknown_mood_stays_unknown():
    data = parse_conversation('상대: 지금 밖이야\n상대: 밥 먹고 갈게')
    result = analyze(data, '상대')
    assert result['dominant'] == '알 수 없음'
    assert result['signals'] == []


def test_favorite_in_local_recommendation():
    hints = [{'category': '좋아하는 것', 'title': '딸기 아이스크림'}]
    result = analyze(parse_conversation('상대: 나 너무 스트레스 받아'), '상대')
    tips = recommend(result, hints, '직접 물어보기')
    assert len(tips) == 3
    assert any('딸기 아이스크림' in r['description'] for r in tips)
    assert all('title' in r for r in tips)


def test_ai_payload_has_no_raw_chat_or_names():
    full = parse_conversation('[이름 공개] [오후 8:00] 내 번호 010-1234-5678, 피곤해요')
    result = analyze(full, '이름 공개')
    payload = ai_summary_payload(result, 'INFP', '먼저 공감',
                                 [{'category': '좋아하는 것', 'title': '홍길동 010-1111-2222'}],
                                 '서울시 강남구 XXX')
    from json import dumps
    raw = dumps(payload, ensure_ascii=False)
    assert '010' not in raw
    assert '이름 공개' not in raw
    assert '홍길동' not in raw
    assert '강남구' not in raw
    assert payload['mbti_optional'] == 'INFP'


def test_decode_utf8_and_cp949():
    assert '안녕하세요' in decode_upload('안녕하세요'.encode('utf-8-sig'))
    assert '안녕하세요' in decode_upload('안녕하세요'.encode('cp949'))


def test_limit_upload():
    try:
        decode_upload(b'x'*(2*1024*1024+1))
        assert False
    except ValueError:
        pass


def test_unlabeled_paste():
    r = parse_conversation('회사에서 너무 힘들었어\n그래도 오늘은 좀 괜찮아')
    assert len(r) == 2
    assert all(x['speaker'] == '' for x in r)


def test_existing_profile_backup_compatible():
    from domain import blank_data, validated_data
    d = blank_data()
    del d['profile']['mbti']
    del d['profile']['support_style']
    valid = validated_data(d)
    assert valid['profile']['mbti'] == ''
    assert valid['profile']['support_style'] == '직접 물어보기'


def test_reject_invalid_mbti():
    from domain import blank_data, validated_data
    d = blank_data()
    d['profile']['mbti'] = 'XXXX'
    try:
        validated_data(d)
        assert False
    except ValueError:
        pass
