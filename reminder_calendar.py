"""Private, on-demand iCalendar export for milestones and pending missions.

No chat, health logs or period estimates ever enter this export.  The .ics file
itself is plaintext and remains private only to the extent its recipient
calendar/account is private.  The app does not send notifications.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any

from domain import next_occasion, parse_day, today_kst


def _escape(value: str) -> str:
    """RFC 5545 TEXT escaping, before RFC-style line folding."""
    return (value.replace('\\', '\\\\').replace('\n', '\\n')
            .replace('\r', '').replace(';', '\\;').replace(',', '\\,'))


def _fold(line: str) -> str:
    """Fold to at most 75 UTF-8 octets per physical line."""
    lines: list[str] = []
    piece = ''
    for char in line:
        # A folded line begins with one space, counted in the 75 octets.
        if len((piece + char).encode('utf-8')) > (75 if not lines else 74):
            if not piece:
                raise ValueError('iCalendar 문자를 처리할 수 없습니다.')
            lines.append(piece)
            piece = char
        else:
            piece += char
    if piece:
        lines.append(piece)
    return '\r\n '.join(lines)


def build_calendar_export(records: dict[str, Any], *, today: date | None = None,
                          include_titles: bool = False, days: int = 366) -> bytes:
    """Return a plaintext .ics: upcoming anniversaries and undone future missions.

    Anniversaries are one next occurrence (not an unbounded recurrence), and
    nothing health-related is exported. Every entry uses a deterministic opaque
    UID, which prevents duplicates in calendars that honor UIDs on import.
    """
    today = today or today_kst()
    if not 1 <= days <= 730:
        raise ValueError('내보내기 기간은 1~730일이어야 합니다.')
    until = today + timedelta(days=days)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//HyunSENSE//Private Calendar Export 1.3//KO',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:HyunSENSE 개인 일정']

    events: list[tuple[date, str, str, str, tuple[int, ...]]] = []
    for row in records.get('anniversaries', []):
        when = next_occasion(row['day'], row['annual'], today)
        if when and today <= when <= until:
            title = row['title'] if include_titles else '개인 기념일'
            events.append((when, row['id'], title, 'anniversary', (7, 1)))
    for row in records.get('missions', []):
        if row['done']:
            continue
        when = parse_day(row['due'])
        if today <= when <= until:
            title = row['title'] if include_titles else '개인 할 일'
            events.append((when, row['id'], title, 'mission', (1,)))

    for when, row_id, title, event_type, reminders in sorted(events):
        hashed_id = hashlib.sha256(f'{event_type}|{row_id}|{when.isoformat()}'.encode()).hexdigest()[:24]
        lines.extend(['BEGIN:VEVENT', f'UID:{hashed_id}@hyunsense.local', f'DTSTAMP:{timestamp}',
                      f'DTSTART;VALUE=DATE:{when:%Y%m%d}',
                      f'DTEND;VALUE=DATE:{(when + timedelta(days=1)):%Y%m%d}',
                      'CLASS:PRIVATE', 'TRANSP:TRANSPARENT',
                      f'SUMMARY:{_escape(title)}',
                      'DESCRIPTION:HyunSENSE에서 직접 선택하여 내보낸 개인 일정'])
        for n in reminders:
            lines.extend(['BEGIN:VALARM', 'ACTION:DISPLAY',
                          f'DESCRIPTION:{_escape(title)}', f'TRIGGER:-P{n}D', 'END:VALARM'])
        lines.append('END:VEVENT')
    lines.append('END:VCALENDAR')
    return ('\r\n'.join(_fold(x) for x in lines) + '\r\n').encode('utf-8')
