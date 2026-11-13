"""Pure domain logic for HyunSENSE. No Streamlit dependency."""
from __future__ import annotations

import calendar
import copy
import json
import re
import statistics
import uuid
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from typing import Any

SCHEMA_VERSION = 1
SECTIONS = ("periods", "anniversaries", "favorites", "date_ideas", "missions")
DATE_STATUSES = ("가고 싶어요", "일정 잡는 중", "다녀왔어요")
FAVORITE_CATEGORIES = ("좋아하는 것", "피하고 싶은 것", "선물 힌트", "중요한 취향")
ANNIVERSARY_CATEGORIES = ("우리의 날", "생일", "결혼기념일", "특별한 일정", "기타")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,50}$")


def today_kst() -> date:
    """Use one explicit timezone for dates throughout the application."""
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def valid_period_entry(start: str, end: str = "", *, today: date | None = None) -> None:
    """Validate an actual recorded period; don't infer a diagnosis."""
    today = today or today_kst()
    first = parse_day(start)
    last = parse_day(end) if end else first
    if first < date(1900, 1, 1) or first > today:
        raise ValueError("월경 시작일은 1900년 이후, 오늘 이전 날짜여야 합니다.")
    if last < first or last > today or (last - first).days >= 11:
        raise ValueError("종료일은 시작일 이후, 오늘 이전이며 기록 기간은 11일 이하여야 합니다.")


def validate_new_period(periods: list[dict], start: str, end: str = "", *, exclude_id: str = "",
                        today: date | None = None) -> None:
    valid_period_entry(start, end, today=today)
    for existing in periods:
        if existing.get("id") != exclude_id and existing["start"] == start:
            raise ValueError("동일한 월경 시작일이 이미 등록되어 있습니다.")



def uid() -> str:
    return uuid.uuid4().hex[:12]


def iso(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def blank_data() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "profile": {"partner_name": "그 사람", "owner_name": "나", "consent": False,
                    "cycle_days": 29, "duration_days": 5, "care_notes": "",
                    "mbti": "", "support_style": "직접 물어보기"},
        "periods": [], "anniversaries": [], "favorites": [], "date_ideas": [], "missions": [],
    }


def sample_data(today: date | None = None) -> dict[str, Any]:
    """Entirely synthetic demo data. Never mistake it for live user information."""
    today = today or today_kst()
    d = blank_data()
    d["profile"].update({"partner_name": "달님", "owner_name": "햇살", "consent": True,
                         "care_notes": "쉬고 싶을 때는 먼저 물어보기 · 따뜻한 차를 좋아해요"})
    for n in (64, 35, 6):
        start = today - timedelta(days=n)
        d["periods"].append({"id": uid(), "start": start.isoformat(),
                             "end": (start + timedelta(days=4)).isoformat(), "note": ""})
    d["anniversaries"] = [
        {"id": uid(), "title": "함께한 첫날", "day": (today-timedelta(days=336)).isoformat(),
         "category": "우리의 날", "annual": True, "note": "작은 케이크 준비하기"},
        {"id": uid(), "title": "기억하고 싶은 하루", "day": (today+timedelta(days=9)).isoformat(),
         "category": "특별한 일정", "annual": False, "note": "일정 확인하기"},
    ]
    d["favorites"] = [
        {"id": uid(), "category": "좋아하는 것", "title": "딸기 디저트", "note": "달지 않은 스타일"},
        {"id": uid(), "category": "선물 힌트", "title": "작은 꽃다발", "note": "과한 포장보다 자연스러운 꽃"},
        {"id": uid(), "category": "피하고 싶은 것", "title": "갑작스러운 약속", "note": "미리 일정 묻기"},
    ]
    d["date_ideas"] = [
        {"id": uid(), "title": "브런치 먹고 서점 데이트", "area": "우리 동네", "budget": 50000,
         "status": "가고 싶어요", "note": "조용한 곳"},
        {"id": uid(), "title": "노을 보러 걷기", "area": "강변", "budget": 10000,
         "status": "가고 싶어요", "note": "일몰 시간 확인"},
    ]
    d["missions"] = [
        {"id": uid(), "title": "좋아하는 간식 챙기기", "due": today.isoformat(), "done": False, "note": ""},
    ]
    return d


def validated_data(value: Any) -> dict[str, Any]:
    """Conservative shape and size validation for imported archives."""
    if not isinstance(value, dict):
        raise ValueError("JSON 최상위 값은 객체여야 합니다.")
    if value.get("version") != SCHEMA_VERSION:
        raise ValueError("지원하지 않는 데이터 버전입니다.")
    source = value.get("profile")
    if not isinstance(source, dict):
        raise ValueError("프로필 형식이 올바르지 않습니다.")
    default = blank_data()
    profile = default["profile"]
    for name in ("partner_name", "owner_name", "care_notes"):
        raw = source.get(name, profile[name])
        if not isinstance(raw, str) or len(raw) > (2000 if name == "care_notes" else 60):
            raise ValueError(f"프로필 {name} 값이 올바르지 않습니다.")
        profile[name] = raw
    if not isinstance(source.get("consent", False), bool):
        raise ValueError("동의 값이 올바르지 않습니다.")
    profile["consent"] = source.get("consent", False)
    valid_mbti = {a+b+c+d for a in 'EI' for b in 'SN' for c in 'TF' for d in 'JP'}
    mbti = source.get('mbti', '')
    if not isinstance(mbti, str) or mbti not in valid_mbti | {''}:
        raise ValueError('MBTI 형식이 올바르지 않습니다.')
    profile['mbti'] = mbti
    support_style = source.get('support_style', '직접 물어보기')
    if support_style not in ('직접 물어보기', '먼저 공감', '해결책 함께', '혼자 쉬는 시간'):
        raise ValueError('대화 선호 형식이 올바르지 않습니다.')
    profile['support_style'] = support_style
    for name, low, high in (("cycle_days", 20, 45), ("duration_days", 2, 10)):
        raw = source.get(name, profile[name])
        if type(raw) is not int or not low <= raw <= high:
            raise ValueError(f"{name} 값이 허용 범위를 벗어났습니다.")
        profile[name] = raw
    fields = {
        "periods": ("start", "end", "note"),
        "anniversaries": ("title", "day", "category", "annual", "note"),
        "favorites": ("title", "category", "note"),
        "date_ideas": ("title", "area", "budget", "status", "note"),
        "missions": ("title", "due", "done", "note"),
    }
    seen_ids: set[str] = set()
    for section, names in fields.items():
        rows = value.get(section, [])
        if not isinstance(rows, list) or len(rows) > 1500:
            raise ValueError(f"{section} 목록이 올바르지 않습니다.")
        for idx, item in enumerate(rows, 1):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not IDENTIFIER.fullmatch(item["id"]):
                raise ValueError(f"{section} {idx}번째 항목의 식별자가 올바르지 않습니다.")
            if item["id"] in seen_ids:
                raise ValueError(f"{section} {idx}번째 항목: 중복된 식별자가 있습니다.")
            seen_ids.add(item["id"])
            cleaned = {"id": item["id"]}
            for key in names:
                v = item.get(key)
                if key in ("annual", "done"):
                    if type(v) is not bool:
                        raise ValueError(f"{section} {idx}번째 항목: {key}는 참/거짓 값이어야 합니다.")
                elif key == "budget":
                    if type(v) is not int or not 0 <= v <= 100_000_000:
                        raise ValueError(f"{section} {idx}번째 항목: 예산이 허용 범위를 벗어났습니다.")
                else:
                    if not isinstance(v, str) or len(v) > 2000:
                        raise ValueError(f"{section} {idx}번째 항목: {key} 문자열이 올바르지 않습니다.")
                    if key in ("start", "day", "due"):
                        parsed = parse_day(v)
                        if not date(1900, 1, 1) <= parsed <= date(2100, 12, 31):
                            raise ValueError(f"{section} {idx}번째 항목: 날짜 범위를 확인해 주세요.")
                    if key == "end" and v:
                        parse_day(v)
                cleaned[key] = v
            if section in ("favorites", "anniversaries", "date_ideas", "missions") and not cleaned["title"].strip():
                raise ValueError(f"{section} {idx}번째 항목: 이름이 비어 있습니다.")
            if section == "favorites" and cleaned["category"] not in FAVORITE_CATEGORIES:
                raise ValueError(f"favorites {idx}번째 항목: 알 수 없는 카테고리입니다.")
            if section == "anniversaries" and cleaned["category"] not in ANNIVERSARY_CATEGORIES:
                raise ValueError(f"anniversaries {idx}번째 항목: 알 수 없는 유형입니다.")
            if section == "date_ideas" and cleaned["status"] not in DATE_STATUSES:
                raise ValueError(f"date_ideas {idx}번째 항목: 알 수 없는 데이트 상태입니다.")
            if section == "periods":
                valid_period_entry(cleaned["start"], cleaned["end"])
                validate_new_period(default["periods"], cleaned["start"], cleaned["end"])
            default[section].append(cleaned)
    # A period history must never survive as hidden data when consent is false.
    # Reject inconsistent imports rather than making the user think it was erased.
    if not profile["consent"] and default["periods"]:
        raise ValueError("월경 기록을 가져오려면 당사자의 현재 동의가 필요합니다. 동의 없는 백업은 복원할 수 없습니다.")
    return default


def revoke_period_consent(records: dict[str, Any]) -> None:
    """Erase partner health records on consent withdrawal (in-place)."""
    records["periods"] = []
    records["profile"]["care_notes"] = ""
    records["profile"]["consent"] = False


def purge_health_session_buffers(session: Any) -> None:
    """Prevent Undo/failed-save drafts from resurrecting revoked health data.

    The active record and last committed snapshot are handled by the normal
    save transaction; this helper purges recoverable ancillary copies only.
    """
    if (session.get("last_deleted") or {}).get("section") == "periods":
        session.pop("last_deleted", None)
    for key in ("pending_deleted", "undo_pending", "unsaved_draft"):
        session.pop(key, None)


def period_summary(periods: list[dict], cycle_default: int, duration_default: int,
                   today: date | None = None) -> dict[str, Any] | None:
    """Predict approximate future period start from self-reported history.

    We do not infer fertile windows, ovulation, mood or health status.
    """
    if not periods:
        return None
    today = today or today_kst()
    starts = sorted(set(parse_day(p["start"]) for p in periods))
    intervals = [(b-a).days for a, b in zip(starts, starts[1:])]
    intervals = [v for v in intervals if 15 <= v <= 60][-6:]
    cycle = int(round(statistics.median(intervals))) if len(intervals) >= 2 else cycle_default
    cycle = min(45, max(20, cycle))
    # Observed variability is descriptive, NOT a clinical confidence interval.
    uncertainty = (min(8, max(3, int(round(statistics.pstdev(intervals))) + 2))
                   if len(intervals) >= 3 else None)
    latest = starts[-1]
    next_start = latest + timedelta(days=cycle)
    # Never leap over an unrecorded period to invent a new future date.
    # Missing a projected start is not a diagnosis; ask for an updated log.
    stale = (today - latest).days > 120
    needs_update = next_start < today and not stale
    if stale or needs_update:
        return {"latest": latest, "next": None, "cycle": cycle, "duration": duration_default,
                "uncertainty": uncertainty, "intervals": intervals,
                "stale": stale, "needs_update": needs_update}
    return {"latest": latest, "next": next_start, "cycle": cycle,
            "duration": duration_default, "uncertainty": uncertainty,
            "intervals": intervals, "stale": False, "needs_update": False}


def next_occasion(day: str, annual: bool, today: date | None = None) -> date | None:
    today = today or today_kst()
    target = parse_day(day)
    if not annual:
        return target if target >= today else None
    for year in (today.year, today.year + 1, today.year + 2):
        month, dd = target.month, target.day
        dd = min(dd, calendar.monthrange(year, month)[1])  # Feb 29 -> Feb 28
        candidate = date(year, month, dd)
        if candidate >= today:
            return candidate
    return None


def upcoming(anniversaries: list[dict], today: date | None = None) -> list[dict]:
    today = today or today_kst()
    result = []
    for entry in anniversaries:
        occasion = next_occasion(entry["day"], entry["annual"], today)
        if occasion:
            result.append({**entry, "when": occasion, "days": (occasion-today).days})
    return sorted(result, key=lambda x: (x["days"], x["title"]))


def mission_ideas(data: dict, today: date | None = None) -> list[dict]:
    today = today or today_kst()
    results = [
        {"tag": "따뜻한 한마디", "title": "오늘 고마웠던 일 하나 말해주기",
         "subtitle": "거창한 이벤트보다 구체적인 표현이 더 기억에 남아요."},
        {"tag": "작은 배려", "title": "오늘 필요한 도움이 있는지 먼저 물어보기",
         "subtitle": "추측하지 않고 상대의 선택을 존중해요."},
    ]
    events = upcoming(data["anniversaries"], today)
    if events and events[0]["days"] <= 30:
        first = events[0]
        results.insert(0, {"tag": f"D-{first['days']}",
                           "title": f"{first['title']} 준비하기",
                           "subtitle": "일정과 취향을 확인하고 부담 없는 계획을 세워보세요."})
    return results[:3]


def dumps(data: dict) -> bytes:
    raw = json.dumps(validated_data(data), ensure_ascii=False, indent=2).encode("utf-8")
    if len(raw) > 2_000_000:
        raise ValueError("전체 기록이 2MB를 초과해 안전하게 저장·백업할 수 없습니다. 오래된 기록을 정리해 주세요.")
    return raw


def loads(raw: bytes) -> dict:
    if len(raw) > 2_000_000:
        raise ValueError("가져올 수 있는 파일은 2MB 이하여야 합니다.")
    return validated_data(json.loads(raw.decode("utf-8")))
