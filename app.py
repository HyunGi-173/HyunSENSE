"""HyunSENSE — thoughtful care diary for consenting couples.

Run: streamlit run app.py
Public/demo mode is transient. Auth + encryption secrets enable persistence.
"""
from __future__ import annotations

import calendar
import copy
import html
import os
import time
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st

from domain import (blank_data, sample_data, period_summary, upcoming, mission_ideas,
                    next_occasion, uid, dumps, loads, parse_day, today_kst, validate_new_period, revoke_period_consent, purge_health_session_buffers)
from security import verify_password, encrypt_backup, decrypt_backup
from mood_engine import (decode_upload, parse_conversation, detected_speakers, analyze,
                         recommend, ai_summary_payload, compose_ai_guidance, analysis_fingerprint)
from store import SecureStore, SaveConflictError
from reminder_calendar import build_calendar_export
from auth_limiter import login_allowed, record_login_failure, record_login_success, session_timed_out

st.set_page_config(page_title="HyunSENSE | 센스 있는 남친(편)", page_icon="💗", layout="wide",
                   initial_sidebar_state="expanded", menu_items={"About": "HyunSENSE — 사랑은 작은 것을 기억하는 것부터."})

ROOT = Path(__file__).resolve().parent
st.markdown(f"<style>{(ROOT / 'styles.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

TODAY = today_kst()
LOG = logging.getLogger("hyunsense")
NAV_ITEMS = ["🏡 오늘의 센스", "🌙 마법의 성", "💬 마음 레이더", "💌 기념일 레이더", "🎁 취향 노트", "🍰 데이트 연구소", "✅ 센스 미션", "⚙️ 설정·데이터"]


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def _secret(name: str) -> str:
    # st.secrets raises FileNotFoundError when no .streamlit/secrets.toml exists.
    try:
        return str(st.secrets.get(name, os.environ.get(name, "")))
    except FileNotFoundError:
        return os.environ.get(name, "")


AUTH_HASH = _secret("ACCESS_PASSWORD_HASH")
ENCRYPTION_KEY = _secret("DATA_ENCRYPTION_KEY")
DATABASE_URL = _secret("DATABASE_URL")
SECURE = bool(AUTH_HASH and ENCRYPTION_KEY)
CONFIG_INCOMPLETE = bool(AUTH_HASH) != bool(ENCRYPTION_KEY)
SESSION_IDLE_SECONDS = 30 * 60  # lock after 30 minutes without an interaction/rerun


def clear_private_session() -> None:
    """Clear sensitive widgets, uploaded text and pending undo copies on lock."""
    for key in list(st.session_state.keys()):
        del st.session_state[key]


def authenticate() -> bool:
    if CONFIG_INCOMPLETE:
        st.error("보안 설정이 불완전합니다. ACCESS_PASSWORD_HASH와 DATA_ENCRYPTION_KEY를 모두 설정하세요.")
        st.stop()
    if not SECURE:
        return True
    if st.session_state.get("authenticated"):
        now = time.monotonic()
        last = st.session_state.get("_last_activity_at", now)
        if session_timed_out(last, now, idle_seconds=SESSION_IDLE_SECONDS):
            clear_private_session()
            st.info("30분 이상 사용하지 않아 잠겼습니다. 다시 로그인해 주세요.")
        else:
            st.session_state["_last_activity_at"] = now
            return True
    st.markdown('<div class="lock-card"><div class="lock-icon">♡</div><div class="eyebrow">PRIVATE SPACE</div>'
                '<h1>우리만의 작은 다이어리</h1><p>암호화된 기록을 열려면 비밀번호를 입력해 주세요.</p></div>',
                unsafe_allow_html=True)
    with st.form("login_form", clear_on_submit=True):
        password = st.text_input("비밀번호", type="password", max_chars=1024,
                                 placeholder="12자 이상으로 설정한 비밀번호")
        submit = st.form_submit_button("다이어리 열기 →", use_container_width=True, type="primary")
    if submit:
        allowed, seconds = login_allowed()
        if not allowed:
            st.error(f"반복된 로그인 실패로 접속을 잠시 제한했습니다. 약 {max(1, seconds // 60 + 1)}분 뒤 다시 시도하세요.")
        elif verify_password(password, AUTH_HASH):
            record_login_success()
            st.session_state["authenticated"] = True
            st.session_state["_last_activity_at"] = time.monotonic()
            st.rerun()
        else:
            record_login_failure()
            st.error("비밀번호가 올바르지 않습니다.")
    st.caption("처음 방문하셨나요? 배포 안내서의 `python setup_secrets.py`로 접속 정보를 생성할 수 있습니다.")
    return False


if not authenticate():
    st.stop()


@st.cache_resource(show_spinner=False)
def get_store(key: str, db_url: str) -> SecureStore:
    return SecureStore(secret_key=key, database_url=db_url,
                       sqlite_path=str(ROOT / "data" / "hyunsense.sqlite3"))


STORE = get_store(ENCRYPTION_KEY, DATABASE_URL) if SECURE else None
if "records" not in st.session_state:
    if STORE:
        try:
            loaded, rev = STORE.load_with_revision()
            st.session_state["records"] = loaded or blank_data()
            st.session_state["store_revision"] = rev
        except Exception:
            LOG.exception("Failed to load encrypted records")
            st.error("암호화 저장소에 접속할 수 없습니다. DB 연결 및 키 설정을 확인하세요.")
            st.stop()
    else:
        st.session_state["records"] = blank_data()
        st.session_state["store_revision"] = 0
    st.session_state["last_committed"] = copy.deepcopy(st.session_state["records"])


def data() -> dict:
    return st.session_state["records"]


def clear_temporary_analysis():
    for key in ("mood_analysis", "mood_ai", "mood_fingerprint"):
        st.session_state.pop(key, None)


def save(message: str = "저장했어요 💗", quiet: bool = False) -> None:
    """Persist before acknowledging, and roll back UI on invalid/stale writes."""
    try:
        if STORE:
            new_revision = STORE.save(data(), expected_revision=st.session_state["store_revision"])
            st.session_state["store_revision"] = new_revision
        else:
            dumps(data())  # Same schema validation in ephemeral demo mode.
    except SaveConflictError:
        st.session_state.pop("pending_deleted", None)
        st.session_state.pop("undo_pending", None)
        st.session_state["unsaved_draft"] = copy.deepcopy(data())
        try:
            latest, rev = STORE.load_with_revision()
            st.session_state["records"] = latest or blank_data()
            st.session_state["store_revision"] = rev
            st.session_state["last_committed"] = copy.deepcopy(st.session_state["records"])
        except Exception:
            st.session_state["records"] = copy.deepcopy(st.session_state["last_committed"])
        st.session_state["save_alert"] = ("다른 기기에서 먼저 수정했습니다. 현재 기기 변경은 저장되지 않았으며 "
                                         "'설정·데이터'에서 미저장 초안을 암호화해 보관할 수 있습니다.")
        st.rerun()
    except Exception:
        st.session_state.pop("pending_deleted", None)
        st.session_state.pop("undo_pending", None)
        LOG.exception("Failed to save records")
        st.session_state["unsaved_draft"] = copy.deepcopy(data())
        st.session_state["records"] = copy.deepcopy(st.session_state["last_committed"])
        st.session_state["save_alert"] = ("저장에 실패해 마지막 저장 상태로 복원했습니다. "
                                         "미저장 편집본은 설정 화면에서 별도 백업할 수 있습니다.")
        st.rerun()
    st.session_state["last_committed"] = copy.deepcopy(data())
    if st.session_state.pop("undo_pending", False):
        st.session_state.pop("last_deleted", None)
    pending_deleted = st.session_state.pop("pending_deleted", None)
    if pending_deleted:
        st.session_state["last_deleted"] = pending_deleted
    st.session_state.pop("save_alert", None)
    if not quiet:
        st.toast(message, icon="💗")
    st.rerun()


if st.session_state.get("save_alert"):
    st.error(st.session_state["save_alert"])


def icon_card(icon: str, name: str, value: str, description: str, color: str = "rose") -> None:
    st.markdown(f"""<div class="stat-card {color}">
      <div class="stat-icon">{esc(icon)}</div>
      <div class="stat-label">{esc(name)}</div>
      <div class="stat-value">{esc(value)}</div>
      <div class="stat-caption">{esc(description)}</div>
    </div>""", unsafe_allow_html=True)


def section_heading(kicker: str, title: str, subtitle: str = "") -> None:
    st.markdown(f'<div class="section-head"><div class="eyebrow">{esc(kicker)}</div>'
                f'<h2>{esc(title)}</h2><p>{esc(subtitle)}</p></div>', unsafe_allow_html=True)


def note_box(text: str, icon: str = "💌") -> None:
    st.markdown(f'<div class="note-box"><span>{esc(icon)}</span><p>{esc(text)}</p></div>', unsafe_allow_html=True)


def label_chip(text: str, kind: str = "rose") -> str:
    return f'<span class="chip {esc(kind)}">{esc(text)}</span>'


def entry_html(icon: str, title: str, subtitle: str, right: str = "", tag: str = "") -> None:
    st.markdown(f'<div class="entry"><div class="entry-emoji">{esc(icon)}</div>'
                f'<div class="entry-content"><strong>{esc(title)}</strong><small>{esc(subtitle)}</small></div>'
                f'<div class="entry-right">{label_chip(tag) if tag else esc(right)}</div></div>',
                unsafe_allow_html=True)


def dshort(d: date) -> str:
    return f"{d.month}월 {d.day}일"


def dday(days: int) -> str:
    return "D-DAY" if days == 0 else f"D-{days}"


def remove_row(section: str, row_id: str):
    previous = next((copy.deepcopy(r) for r in data()[section] if r["id"] == row_id), None)
    if previous is None:
        return
    data()[section] = [r for r in data()[section] if r["id"] != row_id]
    # Only offer Undo after the database write has actually succeeded.
    st.session_state["pending_deleted"] = {"section": section, "row": previous}
    save("기록을 삭제했어요 · 사이드바에서 되돌릴 수 있어요")


def badge_banner() -> None:
    if not SECURE:
        st.info("🧪 **체험 모드** — 입력 데이터는 현재 브라우저 세션에서만 유지되며, 영구 저장되지 않습니다. "
                "실제 건강정보 대신 예시 데이터로 먼저 사용하세요. 설정에서 JSON 백업을 받을 수 있습니다.", icon="🔒")
    elif not DATABASE_URL:
        st.warning("🔒 **암호화 로컬 저장 중** — 개인 PC에서는 유지되지만 Streamlit Community Cloud에서는 "
                   "서버 재시작 시 파일이 사라질 수 있습니다. 영구 보관하려면 DATABASE_URL을 설정하세요.", icon="💾")


def side_nav() -> str:
    with st.sidebar:
        st.markdown('<div class="brand-wrap"><div class="brand-mark">♥</div><div><b>Hyun<span>SENSE</span></b>'
                    '<small>Love, in the little things.</small></div></div>', unsafe_allow_html=True)
        st.caption("센스 있는 남친(편)을 위한 다정한 비서")
        st.write("")
        selected = st.radio("페이지", NAV_ITEMS, label_visibility="collapsed", key="navigation")
        st.divider()
        deletion = st.session_state.get("last_deleted")
        if deletion:
            if deletion["section"] == "periods" and not data()["profile"]["consent"]:
                st.session_state.pop("last_deleted", None)
            elif st.button("↩️ 마지막 삭제 되돌리기", use_container_width=True):
                if not any(r["id"] == deletion["row"]["id"] for r in data()[deletion["section"]]):
                    data()[deletion["section"]].append(copy.deepcopy(deletion["row"]))
                    st.session_state["undo_pending"] = True
                    save("삭제한 항목을 복원했어요")
                else:
                    st.session_state.pop("last_deleted", None)
                    st.rerun()
        p = data()["profile"]
        st.markdown(f'<div class="sidebar-profile"><div class="avatar-heart">♡</div>'
                    f'<div><strong>{esc(p["owner_name"])} × {esc(p["partner_name"])}</strong>'
                    '<small>작은 마음을 모으는 공간</small></div></div>', unsafe_allow_html=True)
        if not SECURE:
            st.caption("🌿 현재 체험 모드 · 자동 저장 없음")
            if st.button("✨ 예시 데이터로 둘러보기", use_container_width=True):
                st.session_state["records"] = sample_data(TODAY)
                save("예시 데이터를 불러왔어요")
        else:
            if st.button("🔒 잠그기", use_container_width=True):
                clear_private_session()
                st.rerun()
    return selected


def page_home() -> None:
    d = data()
    p = d["profile"]
    name = esc(p["partner_name"])
    st.markdown(f'''<div class="hero">
       <div class="hero-spark spark-a">✦</div><div class="hero-spark spark-b">✧</div>
       <div class="hero-copy"><div class="hero-kicker">A LITTLE MORE THOUGHTFUL, EVERY DAY</div>
       <h1>사랑은, <span>기억하는 것</span>부터.</h1>
       <p>오늘도 {name}의 작은 행복을 챙길 준비가 됐나요?<br>사소하지만 다정한 순간을 기록해 보세요.</p>
       <div class="hero-pill">♥ HyunSENSE가 함께할게요</div></div>
       <div class="hero-art"><div class="art-big">♡</div><div class="art-mini">✿</div><div class="art-star">✦</div></div>
       </div>''', unsafe_allow_html=True)

    events = upcoming(d["anniversaries"], TODAY)
    ps = period_summary(d["periods"], p["cycle_days"], p["duration_days"], TODAY) if p["consent"] else None
    left, middle, right = st.columns(3)
    with left:
        if events:
            icon_card("💌", "다가오는 기념일", dday(events[0]["days"]), events[0]["title"], "rose")
        else:
            icon_card("💌", "다가오는 기념일", "아직 없어요", "소중한 날을 등록해 보세요", "rose")
    with middle:
        if ps and ps["next"]:
            icon_card("🌙", "마법의 성 · 예상일", dshort(ps["next"]),
                      "실제 시작일은 달라질 수 있어요", "lilac")
        elif ps and ps.get("needs_update"):
            icon_card("🌙", "마법의 성", "기록 확인", "예상일이 지나 새 기록이 필요해요", "lilac")
        else:
            icon_card("🌙", "마법의 성", "기록해요", "동의받은 기록으로 추정", "lilac")
    with right:
        due = sum((not x["done"]) and parse_day(x["due"]) <= TODAY for x in d["missions"])
        icon_card("✨", "오늘 챙길 일", f"{due}개", "작은 배려를 실천하는 날", "peach")

    col1, col2 = st.columns([1.16, .84], gap="large")
    with col1:
        section_heading("TODAY'S INSPIRATION", "오늘의 센스 제안", "상대방의 의사를 먼저 물어보는 배려를 추천해요.")
        for i, idea in enumerate(mission_ideas(d, TODAY)):
            st.markdown(f'<div class="inspo-card"><div class="inspo-num">0{i+1}</div>'
                        f'<div><small>{esc(idea["tag"])}</small><strong>{esc(idea["title"])}</strong>'
                        f'<p>{esc(idea["subtitle"])}</p></div></div>', unsafe_allow_html=True)
            if st.button("＋ 내 미션으로 담기", key=f"inspo{i}"):
                if not any(r["title"] == idea["title"] and r["due"] == TODAY.isoformat() for r in d["missions"]):
                    d["missions"].append({"id": uid(), "title": idea["title"],
                                          "due": TODAY.isoformat(), "done": False, "note": ""})
                save("오늘의 미션을 담았어요")
    with col2:
        section_heading("COMING SOON", "다가오는 우리의 날", "잊고 싶지 않은 중요한 날짜")
        if events:
            for event in events[:4]:
                entry_html("💗", event["title"], dshort(event["when"]), tag=dday(event["days"]))
        else:
            note_box("기념일 레이더에서 첫 기념일을 등록해 보세요.", "💌")
        section_heading("HER FAVORITES", "기억해 둘 취향", "대화 중 알아낸 작은 취향들")
        if d["favorites"]:
            for f in d["favorites"][-3:][::-1]:
                entry_html("🎀", f["title"], f["note"] or f["category"], tag=f["category"])
        else:
            note_box("좋아하는 음식, 선물 힌트를 기록해 두세요.", "🎁")

    st.markdown('<div class="quote-strip">“센스는 거창한 선물이 아니라, <b>작은 것을 기억하는 마음.</b>” <span>— HyunSENSE</span></div>',
                unsafe_allow_html=True)


def calendar_html(year: int, month: int, actual: set[str], estimated: set[str]) -> str:
    weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(year, month)
    weekdays = ["일", "월", "화", "수", "목", "금", "토"]
    cells = ''.join(f'<div class="cal-weekday">{name}</div>' for name in weekdays)
    for week in weeks:
        for current in week:
            classes = ["cal-day"]
            if current.month != month:
                classes.append("muted")
            if current.isoformat() in actual:
                classes.append("actual")
            elif current.isoformat() in estimated:
                classes.append("forecast")
            if current == TODAY:
                classes.append("today")
            cells += f'<div class="{" ".join(classes)}" title="{esc(current.isoformat())}">{current.day}</div>'
    return f'<div class="calendar-panel"><div class="calendar-grid">{cells}</div><div class="calendar-legend"><span>● 실제 기록</span><span>● 예상일 (참고용)</span></div></div>'


def page_magic() -> None:
    section_heading("MAGIC CASTLE", "🌙 마법의 성", "예측보다는 이해와 배려. 당사자가 공유하기로 한 기록만 관리해요.")
    d = data(); p = d["profile"]
    if not p["consent"]:
        st.warning("이 메뉴는 **상대방이 월경 관련 정보의 기록·공유에 동의한 경우에만** 사용해 주세요. 동의하지 않으면 입력하지 않는 것이 좋습니다.")
        if st.checkbox("상대방이 이 정보의 기록과 공유에 명확히 동의했습니다.", key="magic_consent"):
            if st.button("동의 확인하고 계속하기", type="primary"):
                p["consent"] = True
                save("동의 여부를 저장했어요")
        return

    ps = period_summary(d["periods"], p["cycle_days"], p["duration_days"], TODAY)
    if ps and ps["next"]:
        diff = (ps["next"] - TODAY).days
        st.markdown(f'''<div class="magic-hero"><div class="magic-orb">☾</div>
          <div><div class="eyebrow">NEXT ESTIMATED START</div>
          <h2>{esc(dshort(ps["next"]))} <small>예상</small></h2>
          <p>약 {diff}일 후 · 과거 기록의 간격으로 산출한 대략적인 추정일 (확률·신뢰구간 아님)</p></div></div>''', unsafe_allow_html=True)
    elif ps and ps["stale"]:
        note_box("마지막 월경 기록이 120일보다 오래되었습니다. 오래된 기록으로 다음 시작일을 계산하지 않습니다.", "🌙")
    elif ps and ps.get("needs_update"):
        note_box("예상 시작일이 지났지만 실제 기록이 없습니다. 여러 주기를 건너뛴 날짜를 임의로 표시하지 않습니다. 공유된 새 기록이 있을 때만 업데이트해 주세요.", "🌙")
    else:
        st.markdown('<div class="magic-hero"><div class="magic-orb">☾</div><div><div class="eyebrow">A GENTLE REMINDER</div>'
                    '<h2>작은 배려, 큰 안심</h2><p>월경 시작일을 기록하면 대략적인 다음 시작일을 볼 수 있어요.</p></div></div>',
                    unsafe_allow_html=True)

    a, b, c = st.columns(3)
    with a:
        icon_card("🗓️", "평균 주기", f"{ps['cycle'] if ps else p['cycle_days']}일", "기록이 충분하면 중앙값 적용", "lilac")
    with b:
        icon_card("🪻", "평균 월경 기간", f"{p['duration_days']}일", "직접 설정한 참고값", "rose")
    with c:
        icon_card("📖", "기록한 시작일", f"{len(d['periods'])}회", "본인이 공유한 일자", "peach")

    left, right = st.columns([1, 1.04], gap="large")
    with left:
        section_heading("RECORD", "월경 기록 추가", "기록은 저장 설정에 따라 비공개로 관리됩니다.")
        with st.form("period_form", clear_on_submit=False):
            start = st.date_input("시작일", TODAY, min_value=date(1900, 1, 1), max_value=TODAY, key="period_start")
            has_end = st.checkbox("종료일까지 기록하기", value=False)
            end = st.date_input("종료일 (위 체크가 꺼져 있으면 저장하지 않아요)", TODAY,
                                min_value=date(1900, 1, 1), max_value=TODAY, key="period_end")
            note = st.text_area("메모 (선택)", placeholder="예: 쉬고 싶을 때 먼저 물어보기", max_chars=300)
            submitted = st.form_submit_button("＋ 기록 저장", type="primary", use_container_width=True)
            if submitted:
                try:
                    validate_new_period(d["periods"], start.isoformat(), end.isoformat() if has_end else "", today=TODAY)
                except ValueError as error:
                    st.warning(str(error))
                else:
                    d["periods"].append({"id": uid(), "start": start.isoformat(),
                                         "end": end.isoformat() if has_end else "", "note": note.strip()})
                    save("월경 시작일을 저장했어요")
        st.caption("⚕️ 주기 예측은 의학적 진단, 배란·임신 가능성 판정, 피임 목적으로 사용할 수 없습니다.")
    with right:
        section_heading("MONTHLY VIEW", "월간 달력", "실제 입력된 날짜와 추정 시작일을 구분해서 표시합니다.")
        options = [(TODAY.year + (TODAY.month-1 + i)//12, (TODAY.month-1+i)%12+1) for i in range(-1, 5)]
        month = st.selectbox("달 선택", options, index=1,
                             format_func=lambda x: f"{x[0]}년 {x[1]}월", label_visibility="collapsed")
        actual = set()
        for r in d["periods"]:
            first = parse_day(r["start"])
            last = parse_day(r["end"]) if r["end"] else first
            for n in range(min((last-first).days + 1, 12)):
                actual.add((first + timedelta(days=n)).isoformat())
        predicted = set()
        if ps and ps["next"]:
            # Show only the immediate estimate. Extrapolating multiple missing
            # cycles can imply observed periods that have not happened.
            predicted.add(ps["next"].isoformat())
        st.markdown(calendar_html(month[0], month[1], actual, predicted), unsafe_allow_html=True)

    section_heading("HISTORY", "저장된 시작일", "필요 없는 기록은 언제든 삭제할 수 있어요.")
    if d["periods"]:
        for r in sorted(d["periods"], key=lambda x: x["start"], reverse=True)[:24]:
            c1, c2 = st.columns([8, 1.25], vertical_alignment="center")
            with c1:
                subtitle = f"종료일 {r['end']}" if r["end"] else "종료일 미입력"
                entry_html("🌷", r["start"], f"{subtitle} · {r['note'][:80]}")
            with c2:
                if st.button("삭제", key=f"dp{r['id']}"):
                    remove_row("periods", r["id"])
    else:
        note_box("아직 등록한 기록이 없습니다.", "🌙")
    if d["periods"]:
        with st.expander("✏️ 월경 기록 수정"):
            row = st.selectbox("수정할 시작일", d["periods"],
                               format_func=lambda r: r["start"], key="period_edit_pick")
            with st.form(f"edit_period_{row['id']}"):
                estart = st.date_input("시작일 수정", parse_day(row["start"]), min_value=date(1900, 1, 1), max_value=TODAY)
                eend_on = st.checkbox("종료일 유지 / 입력", value=bool(row["end"]))
                eend = st.date_input("종료일 수정 (체크가 꺼져 있으면 제거)",
                                     parse_day(row["end"]) if row["end"] else estart,
                                     min_value=date(1900, 1, 1), max_value=TODAY)
                enote = st.text_area("메모 수정", value=row["note"], max_chars=300)
                if st.form_submit_button("수정 저장"):
                    try:
                        validate_new_period(d["periods"], estart.isoformat(), eend.isoformat() if eend_on else "",
                                            exclude_id=row["id"], today=TODAY)
                    except ValueError as error:
                        st.warning(str(error))
                    else:
                        row.update({"start": estart.isoformat(), "end": eend.isoformat() if eend_on else "", "note": enote.strip()})
                        save("월경 기록을 수정했어요")
    with st.expander("💜 배려 노트 · 평균 주기 설정"):
        with st.form("care_form"):
            cycle = st.number_input("기본 주기(일)", min_value=20, max_value=45,
                                    value=int(p["cycle_days"]))
            duration = st.number_input("평균 월경 기간(일)", min_value=2, max_value=10,
                                       value=int(p["duration_days"]))
            care_notes = st.text_area("상대방이 직접 알려 준 배려 방법", value=p["care_notes"], max_chars=2000,
                                      placeholder="예: 혼자 쉬고 싶으면 존중해주기, 좋아하는 음료 물어보기")
            if st.form_submit_button("배려 노트 저장"):
                p.update({"cycle_days": cycle, "duration_days": duration, "care_notes": care_notes})
                save()
        if p["care_notes"]:
            note_box(p["care_notes"], "💜")
    st.caption("건강기록은 상대방의 지속적인 동의 아래 관리하고, 공유를 원치 않으면 삭제해 주세요. 월경 시점을 통해 기분이나 행동을 추론하지 않습니다.")



def page_mood() -> None:
    section_heading('HEART RADAR', '💬 마음 레이더',
                    '카톡 속 표현을 조심스럽게 살펴보고, 상대의 마음을 직접 확인할 배려를 제안해요.')
    st.markdown('''<div class="mood-hero"><div class="mood-heart">♡</div><div>
        <div class="eyebrow">LISTEN FIRST, LOVE BETTER</div>
        <h2>마음을 맞히는 대신,<br><span>더 잘 들어줄 준비.</span></h2>
        <p>감정은 단정할 수 없지만, 대화 속 힌트로 오늘의 다정한 행동을 생각할 수 있어요.</p>
        </div></div>''', unsafe_allow_html=True)
    st.info('🔐 **서로 동의한 대화만 사용하세요.** 최근 메시지를 붙여넣거나 TXT 파일을 가져오면 서버 메모리에서만 분석하며 '
            '원문과 분석 결과를 데이터베이스·백업 파일에 저장하지 않습니다. 다만 공개 클라우드 서버로 파일을 업로드하는 방식이므로 '
            '민감한 대화는 개인 PC에서 실행하거나 필요한 문장만 익명화해 붙여넣는 편이 안전합니다.')
    if 'mood_nonce' not in st.session_state:
        st.session_state['mood_nonce'] = 0
    nonce = st.session_state['mood_nonce']
    p = data()['profile']
    left, right = st.columns([1.12, .88], gap='large')
    with left:
        section_heading('CHAT INPUT', '최근 대화 가져오기', '직접 붙여넣거나, 카카오톡에서 내보낸 TXT 파일을 선택하세요.')
        entry_method = st.radio('입력 방법', ['대화 일부 붙여넣기', 'TXT 파일 업로드'],
                                horizontal=True, key='mood_method')
        if entry_method == 'TXT 파일 업로드':
            upload = st.file_uploader('카카오톡에서 내보낸 TXT 파일 (2MB 이하)', type=['txt'],
                                      key=f'mood_file_{nonce}')
            if upload:
                try:
                    raw_text = decode_upload(upload.getvalue())
                except ValueError as error:
                    st.error(str(error))
                    raw_text = ''
            else:
                raw_text = ''
        else:
            raw_text = st.text_area('최근 메시지', height=195, max_chars=250000,
                                     placeholder='예: [달님] [오후 8:01] 오늘 회사에서 정말 지쳤어\n[나] [오후 8:03] 힘들었겠다.\n[달님] [오후 8:05] 좀 쉬고 싶어',
                                     key=f'mood_text_{nonce}')
        try:
            parsed = parse_conversation(raw_text) if raw_text.strip() else []
        except ValueError as error:
            st.error(str(error)); parsed = []
        speakers = detected_speakers(parsed)
        if len(speakers) > 1:
            partner = st.selectbox('어느 사람이 상대방인가요?', ['선택해 주세요'] + speakers,
                                   key=f'mood_speaker_{nonce}')
        elif len(speakers) == 1:
            partner = speakers[0]
            st.caption(f'감지된 대화 이름: {partner}')
        else:
            partner = ''
            st.caption('이름이 구분되지 않는 입력은 **상대방이 직접 작성한 메시지**만 붙여넣어 주세요.')
        st.caption('표현이 잘 드러나는 최근 80개 메시지만 분석합니다. 사진·음성·이모티콘은 해석하지 않습니다.')
        agree = st.checkbox('대화 당사자 모두가 이 내용을 분석하는 데 동의했습니다.',
                            key=f'mood_agree_{nonce}')
        # Streamlit has already updated widget state before this rerun.
        # Use the VISIBLE draft preferences, not only last-saved profile values,
        # so changing them cannot leave outdated analyses/AI advice on screen.
        selected_style = st.session_state.get('mood_support', p.get('support_style', '직접 물어보기'))
        selected_mbti = st.session_state.get('mood_mbti', p.get('mbti', ''))
        if selected_mbti == '미선택':
            selected_mbti = ''
        fingerprint = analysis_fingerprint(raw_text, partner, agree, entry_method,
                                           selected_style, selected_mbti)
        if (not agree or st.session_state.get('mood_fingerprint') != fingerprint):
            clear_temporary_analysis()
        analyze_clicked = st.button('💗 마음 단서 분석하기', type='primary',
                                    use_container_width=True, key=f'mood_run_{nonce}')
        if analyze_clicked:
            if not agree:
                st.error('대화 당사자의 명확한 동의를 먼저 확인해 주세요.')
            elif len(speakers) > 1 and partner == '선택해 주세요':
                st.error('상대방의 메시지만 선택해 분석할 수 있도록 이름을 선택해 주세요.')
            elif not parsed:
                st.warning('분석할 메시지를 입력해 주세요.')
            else:
                result = analyze(parsed, partner_speaker=partner)
                st.session_state['mood_analysis'] = result  # only aggregate + draft, no raw chat
                st.session_state['mood_fingerprint'] = fingerprint
                st.session_state['mood_analysis_rev'] = st.session_state.get('mood_analysis_rev', 0) + 1
                st.session_state.pop('mood_ai', None)
                st.toast('대화 속 표현을 살펴봤어요', icon='💗')
        if st.button('🗑️ 입력 대화 · 분석 결과 지우기', use_container_width=True, key=f'mood_clear_{nonce}'):
            clear_temporary_analysis()
            st.session_state['mood_nonce'] += 1
            st.rerun()
    with right:
        section_heading('PERSONAL TOUCH', '우리만의 대화 취향', 'MBTI보다 직접 알려준 선호 방식을 더 중요하게 사용해요.')
        mbtis = ['미선택'] + [a+b+c+d for a in 'EI' for b in 'SN' for c in 'TF' for d in 'JP']
        mbti_default = mbtis.index(p.get('mbti', '')) if p.get('mbti', '') in mbtis else 0
        # Empty stored value maps to the "미선택" display option.
        mbti = st.selectbox('상대방 MBTI (선택)', mbtis, index=mbti_default, key='mood_mbti')
        styles = ['직접 물어보기', '먼저 공감', '해결책 함께', '혼자 쉬는 시간']
        support = st.selectbox('상대방이 실제로 선호하는 배려', styles,
                               index=styles.index(p.get('support_style', styles[0])), key='mood_support')
        st.caption('MBTI 유형만으로 지금의 기분이나 필요한 위로를 판단할 수는 없습니다.')
        if st.button('대화 취향 저장', key='mood_save_style', use_container_width=True):
            p['mbti'] = '' if mbti == '미선택' else mbti
            p['support_style'] = support
            save('대화 취향을 저장했어요')
        st.markdown('<div class="mood-privacy-note"><b>🔒 개인정보 보호 원칙</b><p>원문 대화는 자동 저장하지 않습니다. '
                    'AI 추천을 선택해도 대화 원문 대신 주제별 표현 횟수와 일부 취향 힌트만 전송합니다. '
                    '기분, 관계, 건강 상태의 정확한 판정이 아닙니다.</p></div>', unsafe_allow_html=True)
    # Changing MBTI/support settings or revoking consent cannot reuse stale AI output.
    if st.session_state.get('mood_fingerprint') != fingerprint or not agree:
        clear_temporary_analysis()
    result = st.session_state.get('mood_analysis')
    if not result:
        note_box('동의받은 예시 메시지부터 시작해 보세요. 분석이 어려운 경우에는 추측 대신 안부를 묻도록 추천합니다.', '💬')
        return
    st.divider()
    section_heading('GENTLE INSIGHTS', '오늘의 대화 힌트', '원문에서 관찰된 표현일 뿐 실제 감정의 확정 판정이 아닙니다.')
    b1, b2 = st.columns([1.18, .82], gap='large')
    with b1:
        st.markdown(f'<div class="mood-result"><small>OBSERVED LANGUAGE CUES</small>'
                    f'<h3>{esc(result["dominant"])}</h3><p>{esc(result["summary"])}</p></div>',
                    unsafe_allow_html=True)
        if result['signals']:
            for sig in result['signals']:
                st.markdown(f'<div class="mood-signal"><b>{esc(sig["theme"])}</b>'
                            f'<span>관련 표현 {int(sig["mentions"])}개 메시지</span></div>',
                            unsafe_allow_html=True)
        else:
            st.caption('뚜렷하게 감지된 표현이 없습니다. 아무런 감정도 없다는 뜻은 아닙니다.')
    with b2:
        st.markdown('<div class="mood-draft"><small>MESSAGE IDEA</small><h3>이런 말은 어때요?</h3></div>',
                    unsafe_allow_html=True)
        st.text_area('보내기 전에 자연스럽게 다듬기', value=result['draft'], height=126,
                     key=f'mood_draft_{nonce}_{st.session_state.get("mood_analysis_rev", 0)}')
        st.caption('자동 전송하지 않습니다. 상대가 실제로 원하는 표현으로 직접 수정해 주세요.')
    section_heading('THOUGHTFUL ACTIONS', '오늘의 배려 전략', '해결하려 하기보다 필요를 먼저 확인해요.')
    preferred = st.session_state.get('mood_support', p.get('support_style', '직접 물어보기'))
    actions = recommend(result, data()['favorites'], preferred)
    columns = st.columns(3, gap='medium')
    for i, action in enumerate(actions):
        with columns[i]:
            st.markdown(f'<div class="mood-action"><div class="mood-action-icon">{esc(action["icon"])}</div>'
                        f'<h4>{esc(action["title"])}</h4><p>{esc(action["description"])}</p></div>',
                        unsafe_allow_html=True)
            if st.button('＋ 센스 미션으로 저장', key=f'mood_mission_{nonce}_{i}', use_container_width=True):
                data()['missions'].append({'id': uid(), 'title': action['title'],
                                           'due': TODAY.isoformat(), 'done': False,
                                           'note': action['description']})
                save('센스 미션에 담았어요')
    st.divider()
    section_heading('AI COMPANION', 'AI 심화 배려 코치 (선택)',
                    '원본 대화는 보내지 않고, 감지된 표현 주제·횟수와 취향 힌트만 전송해 추가 아이디어를 생성합니다.')
    ai_key = _secret('OPENAI_API_KEY')
    if not ai_key:
        st.caption('🔐 OpenAI API 키가 설정되지 않아 AI 모드는 비활성화 상태예요. 기본 배려 추천은 별도 키 없이 사용 가능합니다.')
    else:
        st.warning('외부 AI 서비스로 비식별화된 요약 데이터가 전송됩니다. 완벽한 익명화는 보장할 수 없으며, 대화 원문은 전송하지 않습니다.')
        remote_ok = st.checkbox('요약 데이터의 외부 AI 전송에 동의합니다.', key=f'mood_remote_{nonce}')
        if not remote_ok:
            # Prevent displaying previously generated coaching under revoked consent.
            st.session_state.pop('mood_ai', None)
        if st.button('✨ AI 추천 더 받기', disabled=not remote_ok, type='primary', key=f'mood_ai_run_{nonce}'):
            payload = ai_summary_payload(result, '' if mbti == '미선택' else mbti, preferred,
                                         data()['favorites'])
            try:
                with st.spinner('추가 배려 아이디어를 정리하고 있어요…'):
                    st.session_state['mood_ai'] = compose_ai_guidance(
                        payload, ai_key, _secret('OPENAI_MODEL') or 'gpt-4.1-mini')
                st.rerun()
            except Exception:
                st.error('AI 서버 연결 또는 키 설정을 확인해 주세요. 대화 원문은 전송되지 않았습니다.')
        if st.session_state.get('mood_ai'):
            st.markdown('#### ✨ AI가 제안하는 배려')
            st.write(st.session_state['mood_ai'])
    st.caption('주의: 분석은 연인의 심리를 알아내거나 행동을 예측하는 도구가 아닙니다. '
               '상대방과 직접 대화하며 동의와 경계를 존중하는 것이 가장 중요합니다.')


def page_anniversary() -> None:
    section_heading("LOVE RADAR", "💌 기념일 레이더", "좋은 순간을 놓치지 않도록 특별한 날짜를 차곡차곡.")
    d = data(); events = upcoming(d["anniversaries"], TODAY)
    top1, top2 = st.columns([1.25, .75], gap="large")
    with top1:
        if events:
            first = events[0]
            st.markdown(f'<div class="countdown-hero"><small>OUR NEXT SPECIAL DAY</small>'
                        f'<h2>{esc(first["title"])}</h2><div class="countdown-num">{esc(dday(first["days"]))}</div>'
                        f'<p>{esc(dshort(first["when"]))} · {esc(first["note"] or "좋은 추억 만들기")}</p></div>',
                        unsafe_allow_html=True)
        else:
            note_box("첫 기념일을 등록하고 D-DAY 카운트다운을 시작해 보세요.", "💌")
    with top2:
        st.markdown('<div class="soft-feature"><span>♥</span><b>기억은 배려의 시작</b><p>생일, 첫 만남, 결혼기념일 또는 중요한 개인 일정을 담아보세요.</p></div>',
                    unsafe_allow_html=True)
    a, b = st.columns([.86, 1.14], gap="large")
    with a:
        section_heading("ADD A DATE", "소중한 날 추가")
        with st.form("anniv_form"):
            title = st.text_input("기념일 이름", placeholder="예: 처음 만난 날", max_chars=65)
            day = st.date_input("날짜", TODAY, min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
            category = st.selectbox("유형", ["우리의 날", "생일", "결혼기념일", "특별한 일정", "기타"])
            annual = st.checkbox("매년 반복하기", value=True)
            note = st.text_area("챙길 것 · 메모", placeholder="예: 작은 케이크, 식당 예약", max_chars=500)
            if st.form_submit_button("＋ 기념일 추가", type="primary", use_container_width=True):
                if not title.strip():
                    st.error("기념일 이름을 입력해 주세요.")
                else:
                    d["anniversaries"].append({"id": uid(), "title": title.strip(), "day": day.isoformat(),
                                               "category": category, "annual": annual, "note": note.strip()})
                    save()
    with b:
        section_heading("UPCOMING DATES", "우리의 특별한 날", "D-30 · D-7 · D-1에 준비할 일을 미리 확인해요.")
        if not events:
            note_box("다가오는 기념일이 없습니다.", "💌")
        for e in events[:30]:
            l, r = st.columns([7, 1], vertical_alignment="center")
            with l:
                entry_html("💝", e["title"], f'{dshort(e["when"])} · {e["category"]} · {e["note"][:80]}',
                           tag=dday(e["days"]))
            with r:
                if st.button("삭제", key=f"da{e['id']}"):
                    remove_row("anniversaries", e["id"])
        st.caption("📍 앱 자체는 알림을 발송하지 않지만, '설정·데이터 → 캘린더'에서 개인 캘린더용 일정 파일을 만들 수 있어요.")
    st.divider()
    if d["anniversaries"]:
        with st.expander("✏️ 기념일 수정"):
            row = st.selectbox("수정할 기념일", d["anniversaries"],
                               format_func=lambda r: f"{r['title']} · {r['day']}", key="anniv_edit_pick")
            with st.form(f"edit_anniv_{row['id']}"):
                etitle = st.text_input("기념일 이름", value=row["title"], max_chars=65)
                eday = st.date_input("날짜", parse_day(row["day"]), min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
                kinds = ["우리의 날", "생일", "결혼기념일", "특별한 일정", "기타"]
                ecategory = st.selectbox("유형", kinds, index=kinds.index(row["category"]) if row["category"] in kinds else 4)
                eannual = st.checkbox("매년 반복", value=row["annual"])
                enote = st.text_area("메모", value=row["note"], max_chars=500)
                if st.form_submit_button("수정 저장") and etitle.strip():
                    row.update({"title": etitle.strip(), "day": eday.isoformat(),
                                "category": ecategory, "annual": eannual, "note": enote.strip()})
                    save("기념일을 수정했어요")
        with st.expander("지난 일정 · 등록된 모든 기념일 보기"):
            for e in d["anniversaries"]:
                st.write(f'• **{e["title"]}** · {e["day"]}' + (" · 매년 반복" if e["annual"] else ""))


def page_favorites() -> None:
    section_heading("THE LITTLE DETAILS", "🎁 취향 노트", "지나가듯 말한 한마디도, 나중에는 특별한 힌트가 되니까.")
    d = data()
    types = ["좋아하는 것", "피하고 싶은 것", "선물 힌트", "중요한 취향"]
    a, b = st.columns([.87, 1.13], gap="large")
    with a:
        section_heading("NEW DISCOVERY", "취향 발견하기")
        with st.form("favorites_form"):
            typ = st.selectbox("카테고리", types)
            title = st.text_input("이름", placeholder="예: 라벤더 향, 조용한 카페", max_chars=80)
            note = st.text_area("자세한 메모", placeholder="언제, 어떤 이야기를 했는지 기록해 보세요", max_chars=500)
            if st.form_submit_button("＋ 취향 저장", type="primary", use_container_width=True):
                if title.strip():
                    d["favorites"].append({"id": uid(), "category": typ, "title": title.strip(), "note": note.strip()})
                    save()
                else:
                    st.error("이름을 입력해 주세요.")
        st.markdown('<div class="tip-panel">✦ <b>오늘의 센스 팁</b><p>선물을 미리 정하기보다, 먼저 상대가 실제로 원하는 것이 무엇인지 물어보세요.</p></div>',
                    unsafe_allow_html=True)
    with b:
        type_filter = st.selectbox("분류별 보기", ["전체"] + types)
        rows = [f for f in d["favorites"] if type_filter == "전체" or f["category"] == type_filter]
        if not rows:
            note_box("아직 기록된 취향이 없어요. 작고 소중한 취향부터 시작해 보세요.", "🎁")
        for f in reversed(rows):
            l, r = st.columns([7, 1], vertical_alignment="center")
            with l:
                icons = {"좋아하는 것": "💗", "피하고 싶은 것": "🕊️", "선물 힌트": "🎀", "중요한 취향": "✨"}
                entry_html(icons.get(f["category"], "💗"), f["title"], f["note"] or "메모 없음", tag=f["category"])
            with r:
                if st.button("삭제", key=f"df{f['id']}"):
                    remove_row("favorites", f["id"])
        if d["favorites"]:
            with st.expander("✏️ 취향 수정"):
                row = st.selectbox("수정할 항목", d["favorites"],
                                   format_func=lambda r: r["title"], key="favorite_edit_pick")
                with st.form(f"edit_fav_{row['id']}"):
                    ecat = st.selectbox("유형", types, index=types.index(row["category"]) if row["category"] in types else 0)
                    etitle = st.text_input("이름", value=row["title"], max_chars=80)
                    enote = st.text_area("메모", value=row["note"], max_chars=500)
                    if st.form_submit_button("수정 저장") and etitle.strip():
                        row.update({"category": ecat, "title": etitle.strip(), "note": enote.strip()})
                        save("취향을 수정했어요")


def page_dates() -> None:
    section_heading("DATE LAB", "🍰 데이트 연구소", "다음엔 어디로 갈까? 둘만의 데이트 버킷리스트를 만들어봐요.")
    d = data()
    a, b = st.columns([.87, 1.13], gap="large")
    with a:
        section_heading("PLAN A MOMENT", "데이트 아이디어 추가")
        with st.form("date_form"):
            title = st.text_input("데이트 이름", placeholder="예: 서울숲 산책 후 브런치", max_chars=95)
            area = st.text_input("장소 / 지역", placeholder="예: 성수동", max_chars=100)
            budget = st.number_input("예상 예산 (원)", min_value=0, max_value=100_000_000, step=10000, value=50000)
            status = st.selectbox("상태", ["가고 싶어요", "일정 잡는 중", "다녀왔어요"])
            note = st.text_area("데이트 메모", placeholder="예: 조용한 창가 자리, 예약 필수", max_chars=400)
            if st.form_submit_button("＋ 데이트 추가", type="primary", use_container_width=True):
                if title.strip():
                    d["date_ideas"].append({"id": uid(), "title": title.strip(), "area": area.strip(),
                                            "budget": int(budget), "status": status, "note": note.strip()})
                    save()
                else:
                    st.error("데이트 이름을 입력해 주세요.")
    with b:
        open_count = sum(x["status"] != "다녀왔어요" for x in d["date_ideas"])
        budget_total = sum(x["budget"] for x in d["date_ideas"] if x["status"] != "다녀왔어요")
        q1, q2 = st.columns(2)
        with q1: icon_card("🌷", "가고 싶은 데이트", f"{open_count}개", "함께 계획해 볼 장소", "rose")
        with q2: icon_card("💳", "계획 예산 합계", f"{budget_total:,}원", "미완료 데이트 기준", "peach")
        show = st.selectbox("상태별 필터", ["전체", "가고 싶어요", "일정 잡는 중", "다녀왔어요"])
        rows = [x for x in d["date_ideas"] if show == "전체" or x["status"] == show]
        if not rows: note_box("데이트 버킷리스트를 만들어 보세요.", "🍰")
        for x in reversed(rows):
            l, m, r = st.columns([5, 2, 1.1], vertical_alignment="center")
            with l:
                entry_html("🍓", x["title"], f'{x["area"] or "지역 미정"} · {x["budget"]:,}원 · {x["note"][:60]}')
            with m:
                next_states = ["가고 싶어요", "일정 잡는 중", "다녀왔어요"]
                chosen = st.selectbox("상태", next_states, index=next_states.index(x["status"]),
                                       key=f"status{x['id']}", label_visibility="collapsed")
                if chosen != x["status"]:
                    x["status"] = chosen
                    save("데이트 상태를 변경했어요")
            with r:
                if st.button("삭제", key=f"dd{x['id']}"):
                    remove_row("date_ideas", x["id"])
        if d["date_ideas"]:
            with st.expander("✏️ 데이트 계획 수정"):
                row = st.selectbox("수정할 데이트", d["date_ideas"],
                                   format_func=lambda r: r["title"], key="date_edit_pick")
                with st.form(f"edit_date_{row['id']}"):
                    etitle = st.text_input("데이트 이름", value=row["title"], max_chars=95)
                    earea = st.text_input("장소", value=row["area"], max_chars=100)
                    ebudget = st.number_input("예산 (원)", min_value=0, max_value=100_000_000,
                                              value=row["budget"], step=10000)
                    enote = st.text_area("메모", value=row["note"], max_chars=400)
                    if st.form_submit_button("수정 저장") and etitle.strip():
                        row.update({"title": etitle.strip(), "area": earea.strip(),
                                    "budget": int(ebudget), "note": enote.strip()})
                        save("데이트 정보를 수정했어요")


def page_missions() -> None:
    section_heading("A LITTLE EVERY DAY", "✅ 센스 미션", "완벽한 연인보다, 매일 조금씩 더 다정한 연인.")
    st.caption("📅 미완료 미션은 '설정·데이터 → 캘린더'에서 개인 일정 앱으로 내보낼 수 있어요.")
    d = data()
    a, b = st.columns([.85, 1.15], gap="large")
    with a:
        section_heading("NEW MISSION", "센스 있는 할 일")
        with st.form("mission_form"):
            title = st.text_input("할 일", placeholder="예: 다음 주 중요한 발표 응원하기", max_chars=100)
            due = st.date_input("예정일", TODAY)
            note = st.text_area("메모", max_chars=350, placeholder="어떻게 도와주면 좋을까?")
            if st.form_submit_button("＋ 할 일 추가", type="primary", use_container_width=True):
                if title.strip():
                    d["missions"].append({"id": uid(), "title": title.strip(), "due": due.isoformat(),
                                          "done": False, "note": note.strip()})
                    save()
                else:
                    st.error("할 일을 적어 주세요.")
    with b:
        tasks = sorted(d["missions"], key=lambda x: (x["done"], x["due"]))
        pending = sum(not t["done"] for t in tasks)
        icon_card("✨", "아직 남은 센스 미션", f"{pending}개", "작은 행동을 하나씩 실천해 봐요", "lilac")
        if not tasks: note_box("첫 센스 미션을 등록해 보세요.", "✨")
        for t in tasks:
            l, m, r = st.columns([1.2, 7.1, 1], vertical_alignment="center")
            with l:
                done = st.checkbox("완료", value=t["done"], key=f"check{t['id']}", label_visibility="collapsed")
                if done != t["done"]:
                    t["done"] = done
                    save("미션 완료!" if done else "다시 할 일로 등록했어요")
            with m:
                entry_html("🌟" if t["done"] else "💞", t["title"],
                           f'{t["due"]} · {t["note"][:80]}', tag="완료" if t["done"] else "진행 중")
            with r:
                if st.button("삭제", key=f"dm{t['id']}"):
                    remove_row("missions", t["id"])
        if d["missions"]:
            with st.expander("✏️ 미션 수정"):
                row = st.selectbox("수정할 미션", d["missions"],
                                   format_func=lambda r: r["title"], key="mission_edit_pick")
                with st.form(f"edit_mission_{row['id']}"):
                    etitle = st.text_input("할 일", value=row["title"], max_chars=100)
                    edue = st.date_input("예정일", parse_day(row["due"]))
                    enote = st.text_area("메모", value=row["note"], max_chars=350)
                    if st.form_submit_button("수정 저장") and etitle.strip():
                        row.update({"title": etitle.strip(), "due": edue.isoformat(), "note": enote.strip()})
                        save("미션을 수정했어요")


def page_settings() -> None:
    section_heading("PREFERENCES & PRIVACY", "⚙️ 설정 · 나의 데이터", "누구의 정보인지보다, 그 정보를 어떻게 존중하는지가 더 중요해요.")
    p = data()["profile"]
    left, right = st.columns([1, 1], gap="large")
    with left:
        section_heading("OUR LITTLE SPACE", "기본 프로필")
        with st.form("profile_form"):
            owner = st.text_input("나의 별명", value=p["owner_name"], max_chars=60)
            partner = st.text_input("상대방의 별명", value=p["partner_name"], max_chars=60)
            consent = st.checkbox("상대방이 월경 기록과 공유에 동의했습니다.", value=p["consent"])
            if st.form_submit_button("프로필 저장", use_container_width=True, type="primary"):
                if not consent and p["consent"]:
                    # Revoke sensitive records, related undo buffers and drafts.
                    revoke_period_consent(data())
                    purge_health_session_buffers(st.session_state)
                    clear_temporary_analysis()
                p.update({"owner_name": owner.strip() or "나", "partner_name": partner.strip() or "그 사람",
                          "consent": consent})
                save("설정을 저장했어요")
        note_box("동의를 철회하면 저장된 월경 기록과 배려 메모를 즉시 삭제합니다. 개인에게 백업 파일이 남아 있다면 별도로 삭제해야 합니다.", "🔒")
    with right:
        section_heading("CALENDAR EXPORT", "📅 일정 캘린더로 옮기기",
                        "기념일과 미완료 미션만 선택적으로 내보내요. 월경 기록이나 채팅은 포함하지 않습니다.")
        with st.expander("캘린더에 넣을 알림 파일 만들기", expanded=False):
            st.caption("기념일은 7일·1일 전, 미션은 1일 전 알림이 포함됩니다. "
                       "실제 알림 동작은 Google·Apple·Outlook 캘린더 앱 설정에 따라 달라집니다.")
            detailed_names = st.checkbox("캘린더 일정 제목을 실제 이름으로 표시하기 (공유 캘린더 주의)", value=False)
            export_confirm = st.checkbox(".ics 파일은 암호화되지 않으며 가져온 캘린더에 날짜가 보임을 확인했습니다.")
            if export_confirm:
                ics_blob = build_calendar_export(data(), today=TODAY, include_titles=detailed_names)
                st.download_button("📆 기념일·미션 캘린더 다운로드 (.ics)", data=ics_blob,
                                   file_name=f"HyunSENSE_calendar_{TODAY.isoformat()}.ics",
                                   mime="text/calendar", use_container_width=True)
            else:
                st.caption("개인 캘린더에서 사용하세요. 기본 설정은 민감한 제목을 일반 이름으로 가립니다.")
        st.divider()
        section_heading("MY DATA", "데이터 백업과 복원")
        st.write("기본적으로 **암호화 백업**을 권장합니다. 파일과 암호는 별도로 보관하세요.")
        backup_pass = st.text_input("백업 암호 (12자 이상)", type="password", key="backup_password",
                                    help="앱 로그인 암호와 별도로 정하세요. 분실하면 복원할 수 없습니다.")
        if len(backup_pass) >= 12:
            protected = encrypt_backup(dumps(data()), backup_pass)
            st.download_button("🔐 암호화 백업 다운로드 (.hyunsense)", data=protected,
                               file_name=f"HyunSENSE_private_{TODAY.isoformat()}.hyunsense",
                               mime="application/octet-stream", use_container_width=True)
        else:
            st.caption("암호를 12자 이상 입력하면 암호화 백업 다운로드가 활성화됩니다.")
        with st.expander("⚠️ 호환용 평문 JSON 백업 (비추천)"):
            st.warning("이 파일에는 건강정보가 평문으로 들어갑니다. GitHub·공개 클라우드에 올리지 마세요.")
            st.download_button("평문 JSON 다운로드", data=dumps(data()),
                               file_name=f"HyunSENSE_plain_{TODAY.isoformat()}.json",
                               mime="application/json", use_container_width=True)
        if st.session_state.get("unsaved_draft") is not None:
            st.warning("다른 기기와 충돌했거나 저장 실패 시 남겨 둔 미저장 편집본이 있습니다. "
                       "필요하면 암호화 백업으로 보관하고, 최신 데이터와 수동으로 비교해 주세요.")
            if len(backup_pass) >= 12:
                st.download_button("🔐 미저장 편집본 백업", data=encrypt_backup(
                    dumps(st.session_state["unsaved_draft"]), backup_pass),
                    file_name="HyunSENSE_unsaved_draft.hyunsense", mime="application/octet-stream")
            if st.button("미저장 편집본 메모리에서 버리기"):
                st.session_state.pop("unsaved_draft", None)
                st.rerun()
        uploaded = st.file_uploader("백업 복원 (.hyunsense 또는 구버전 .json)",
                                    type=["hyunsense", "json"], accept_multiple_files=False)
        if uploaded:
            try:
                contents = uploaded.getvalue()
                if uploaded.name.lower().endswith('.hyunsense'):
                    if len(backup_pass) < 12:
                        raise ValueError("복원하려면 백업 당시 설정한 암호(12자 이상)를 입력하세요.")
                    contents = decrypt_backup(contents, backup_pass)
                imported = loads(contents)
                st.success("백업 검사 완료. 복원하면 현재 기록을 모두 교체합니다.")
                confirmed = st.checkbox("기존 기록이 교체된다는 점을 확인했습니다.", key="confirm_restore")
                restore_consent = True
                if imported["periods"]:
                    restore_consent = st.checkbox("백업의 월경 기록에 대해 상대방의 현재 공유 동의를 확인했습니다.",
                                                   key="confirm_restore_periods")
                if st.button("검사된 백업으로 교체", type="primary", disabled=not (confirmed and restore_consent)):
                    st.session_state["records"] = imported
                    st.session_state.pop("last_deleted", None)
                    st.session_state.pop("unsaved_draft", None)
                    clear_temporary_analysis()
                    save("백업을 복원했어요")
            except (ValueError, UnicodeError) as error:
                st.error(f"백업을 읽을 수 없습니다: {error}")
        st.divider()
        section_heading("DELETE", "서버 기록 삭제")
        confirm = st.text_input('삭제하려면 "모두삭제"를 입력하세요', key="erase_confirm")
        if st.button("🗑️ 모든 기록 삭제", disabled=confirm != "모두삭제", use_container_width=True):
            if STORE:
                try:
                    STORE.delete(expected_revision=st.session_state["store_revision"])
                except SaveConflictError:
                    st.error("다른 기기에서 변경한 데이터가 있어 삭제를 중단했습니다. 페이지를 새로고침하고 다시 확인하세요.")
                    st.stop()
                except Exception:
                    LOG.exception("Failed to delete records")
                    st.error("저장소 삭제에 실패했습니다. 다시 확인해 주세요.")
                    st.stop()
            st.session_state["records"] = blank_data()
            st.session_state["last_committed"] = blank_data()
            st.session_state["store_revision"] = 0
            st.session_state.pop("last_deleted", None)
            st.session_state.pop("unsaved_draft", None)
            clear_temporary_analysis()
            st.toast("활성 저장소의 기록을 삭제했습니다", icon="🗑️")
            st.rerun()
        st.caption("활성 저장소의 현재 기록을 삭제합니다. 서버·DB 제공업체의 과거 백업이나 별도로 내려받은 파일은 별도로 관리해야 합니다.")
    st.divider()
    section_heading("A NOTE ON PRIVACY", "개인정보 · 건강정보 안내")
    st.markdown("""- 이 서비스는 **당사자가 동의해 공유한 정보**만 다루는 1인용 기록 도구입니다. 무단 기록이나 감시에 사용하지 마세요.
- 공개 체험 모드에서는 데이터가 저장되지 않고, 페이지 세션이 종료되면 사라질 수 있습니다.
- 보안 설정 모드에서는 단일 공유 비밀번호로 접근을 제한하고, 저장소의 기록은 암호화합니다. 이는 의료기관 수준의 보안이나 다중 사용자 인증을 보장하지 않습니다.
- Streamlit Community Cloud의 로컬 디스크는 영구 저장소가 아닙니다. 외부 PostgreSQL 연결이 필요합니다.
- 예정일은 통계적 참고용일 뿐 **의학적 진단·배란/가임기 추정·피임**에 사용하지 않습니다.
- 앱 자체는 푸시 알림을 자동 발송하지 않습니다. 원하면 기념일·미션을 `.ics`로 내려받아 개인 캘린더에 등록하세요. 이 파일은 평문이며 월경 기록은 포함되지 않습니다.
- 로그인 시도 제한은 현재 서버 프로세스 내에서만 적용됩니다. 다중 사용자 대상 공개 서비스는 별도 인증·분산 제한·데이터 분리가 필요합니다.""")


selection = side_nav()
with st.container():
    if selection == NAV_ITEMS[0]:
        page_home()
    elif selection == NAV_ITEMS[1]:
        page_magic()
    elif selection == NAV_ITEMS[2]:
        page_mood()
    elif selection == NAV_ITEMS[3]:
        page_anniversary()
    elif selection == NAV_ITEMS[4]:
        page_favorites()
    elif selection == NAV_ITEMS[5]:
        page_dates()
    elif selection == NAV_ITEMS[6]:
        page_missions()
    else:
        page_settings()

badge_banner()
st.markdown('<div class="app-footer">HyunSENSE v1.3 · Love, in the little things. <span>Made with care ♡</span></div>', unsafe_allow_html=True)
