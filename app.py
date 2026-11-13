"""가족약속 — Streamlit / GitHub용 가족 약 복용 알리미.

배포 (Python 3.11 이상 권장)
1. 이 파일과 requirements.txt를 GitHub 저장소 루트에 올립니다.
2. Streamlit Community Cloud에서 app.py를 선택해 배포합니다.
3. 앱 Settings > Secrets에 아래 값을 TOML로 설정합니다 (실제 값으로 교체).
   APP_ACCESS_CODE = "가족만-아는-충분히-긴-비밀번호"
   APP_URL = "https://YOUR-APP.streamlit.app"
   DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require"
   DATABASE_URL은 Neon/Supabase 등 영구 PostgreSQL 연결 문자열입니다.
   비밀번호/연결 문자열을 GitHub 코드에 넣지 마세요. URI 비밀번호는 URL 인코딩하세요.
4. QR 버튼에서 QR을 내려받아 가족과 공유합니다. 접속 후 가족 비밀번호가 필요합니다.

DATABASE_URL 미설정 시 SQLite 로컬 파일을 사용합니다. Community Cloud의 로컬 파일은
재시작/재배포에 사라질 수 있습니다. 실제 가족 기록은 반드시 외부 DB를 연결하세요.
가족 전체가 하나의 접근 코드를 공유하는 소규모 앱이며, 의료기관용 시스템이 아닙니다.
음성/화면 알림은 앱을 켠 상태에서만 동작합니다. 잠금화면/종료 상태 푸시가 아닙니다.
동일 성분 표시는 사용자가 등록한 성분이 정확히 일치할 때만 감지하며 상호작용 검사가 아닙니다.
예약은 매일 반복하는 고정 시각만 지원합니다. 필요시 복용/격일/감량 처방은 지원하지 않습니다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import io
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import qrcode
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="가족약속 · 약 복용 알리미", page_icon="💊", layout="centered", initial_sidebar_state="collapsed")
KST = ZoneInfo("Asia/Seoul")

def setting(name, default=""):
    try:
        return str(st.secrets.get(name, os.getenv(name, default)))
    except Exception:
        return os.getenv(name, default)

DB_URL = setting("DATABASE_URL")
ACCESS = setting("APP_ACCESS_CODE")
APP_URL = setting("APP_URL")
LOCK = threading.RLock()

def now():
    return datetime.now(KST)

def ident():
    return uuid.uuid4().hex

@contextmanager
def db(write=False):
    """書き込みは全端末で直列化。残量更新と記録を同じトランザクションで確定。"""
    if DB_URL:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(DB_URL, row_factory=dict_row, connect_timeout=12)
        try:
            if write:
                conn.execute("SELECT pg_advisory_xact_lock(746201912)")
            yield lambda sql, params=(): conn.execute(sql.replace("?", "%s"), params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        with LOCK:
            conn = sqlite3.connect("family_medicine.sqlite3", timeout=20)
            conn.row_factory = sqlite3.Row
            try:
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield lambda sql, params=(): conn.execute(sql, params)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

@st.cache_resource
def initialize(_url):
    with db(True) as q:
        q("CREATE TABLE IF NOT EXISTS members (id TEXT PRIMARY KEY, name TEXT NOT NULL, emoji TEXT NOT NULL)")
        q("""CREATE TABLE IF NOT EXISTS meds (
            id TEXT PRIMARY KEY, member_id TEXT NOT NULL, name TEXT NOT NULL,
            ingredient TEXT NOT NULL, times TEXT NOT NULL, expiry TEXT NOT NULL,
            stock INTEGER NOT NULL, dose INTEGER NOT NULL, min_gap INTEGER NOT NULL,
            missed_note TEXT NOT NULL, usage_note TEXT NOT NULL,
            kind TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)""")
        q("""CREATE TABLE IF NOT EXISTS doses (
            id TEXT PRIMARY KEY, med_id TEXT NOT NULL, day TEXT NOT NULL,
            slot TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL, UNIQUE(med_id, day, slot))""")
    return True

def rows(sql, params=()):
    with db() as q:
        return [dict(r) for r in q(sql, params).fetchall()]

def notice(text, icon="✅"):
    st.session_state.flash = (text, icon)
    st.rerun()

def norm(s):
    return " ".join(s.casefold().split())

def safe(s):
    return html.escape(str(s))

def scheduled(day, slot):
    return datetime.combine(date.fromisoformat(day), time.fromisoformat(slot), KST)

def advice(med, reason=""):
    """일반 안전 행동만 제시. 누락한 약의 임의 복용/두 배 복용은 권하지 않는다."""
    if med["missed_note"].strip():
        st.info("📋 등록해 둔 누락 시 지침\n\n" + med["missed_note"])
        st.caption("약 봉투·설명서의 실제 문구인지 확인하세요. 이 앱은 지침의 적절성을 판정하지 않습니다.")
    if reason == "먹었는지 모름":
        st.warning("추가로 먹기 전에 약통의 해당 칸, 남은 수량, 포장, 가족 기록을 확인하세요. 기록만 보고 미복용으로 단정하지 마세요.")
    elif reason == "몸이 불편함":
        st.warning("증상과 시작 시각, 마지막 복용 시각을 메모하세요. 임의로 약을 추가하거나 다른 약으로 바꾸지 마세요.")
    elif reason == "약이 없음":
        st.info("남은 포장과 보관 장소를 확인하고 가족에게 재고 확인을 요청하세요. 다른 사람의 처방약이나 비슷한 이름의 약으로 대체하지 마세요.")
    elif reason == "기한 지남":
        st.info("기한이 지난 약은 정상 약과 분리해 ‘복용 금지’로 표시하세요. 다른 가족 약으로 대신하지 마세요.")
    else:
        st.info("약 봉투의 ‘복용을 잊었을 때’ 항목과 마지막 실제 복용 시각을 확인하세요. 지침이 없으면 이 앱만으로 지금 복용할지 결정할 수 없습니다.")
    st.caption("⛔ 놓친 횟수를 한꺼번에 몰아 먹거나 두 배로 먹지 마세요. 다음 알림 시각은 보충 복용 지시가 아닙니다.")
    st.caption("🚨 호흡 곤란·의식 저하·심한 알레르기 또는 과량 복용 의심은 가정 내 조치로 해결하려 하지 말고 119에 도움을 요청하세요.")

def record(med_id, day, slot, status, reason=""):
    """신선한 DB 값으로 재검증. 같은 회차의 중복 클릭/동시 기록은 차단한다."""
    with db(True) as q:
        m = q("SELECT * FROM meds WHERE id=? AND active=1", (med_id,)).fetchone()
        if not m or slot not in json.loads(m["times"]) or m["kind"] != "정기약":
            return False, "약 또는 복용 일정이 변경됐어요. 새로고침해 주세요."
        if day != now().date().isoformat():
            return False, "날짜가 바뀌었어요. 오늘 화면에서 다시 확인해 주세요."
        existing = q("SELECT status FROM doses WHERE med_id=? AND day=? AND slot=?", (med_id, day, slot)).fetchone()
        if existing and existing["status"] == "taken":
            return False, "이미 복용 완료된 회차예요. 추가 복용하지 마세요."
        if status == "taken":
            if m["expiry"] < day:
                return False, "사용기한이 지난 약이에요. 복용 완료 처리를 차단했어요."
            if m["stock"] < m["dose"]:
                return False, "등록된 재고가 부족해요. 실제 수량을 확인해 주세요."
            if scheduled(day, slot) > now():
                return False, "아직 예정 시각 전이에요. 이 앱은 조기 복용을 판단하지 않습니다."
            recent = q("""SELECT d.recorded_at, m.id, m.ingredient FROM doses d
                JOIN meds m ON m.id=d.med_id WHERE m.member_id=? AND d.status='taken'""", (m["member_id"],)).fetchall()
            for r in recent:
                same = r["id"] == med_id
                same_ingredient = bool(m["ingredient"].strip()) and norm(r["ingredient"]) == norm(m["ingredient"])
                elapsed = (now() - datetime.fromisoformat(r["recorded_at"])).total_seconds() / 3600
                if (same or same_ingredient) and elapsed < m["min_gap"]:
                    return False, f"같은 약/등록 성분의 최근 복용이 최소 확인 간격({m['min_gap']}시간) 안에 있어요. 추가 복용을 차단했어요."
            q("UPDATE meds SET stock=stock-? WHERE id=?", (m["dose"], med_id))
        stamp = now().isoformat()
        if existing:
            q("UPDATE doses SET status=?, reason=?, recorded_at=? WHERE med_id=? AND day=? AND slot=?", (status, reason, stamp, med_id, day, slot))
        else:
            q("INSERT INTO doses VALUES (?,?,?,?,?,?,?)", (ident(), med_id, day, slot, status, reason, stamp))
    return True, "약 복용을 완료했습니다." if status == "taken" else "미복용 사유를 기록했어요."

# 화면은 한 대의 휴대폰을 기준으로 설계. 긴 목록은 페이지 이동, 입력·상세는 팝업.
st.markdown("""<style>
[data-testid="stHeader"]{display:none} #MainMenu,footer{display:none}
.block-container{max-width:540px!important;padding:0.7rem .85rem 1rem!important}
.stApp{background:#f2f5f9;color:#18273d}
h1,h2,h3,p{letter-spacing:-.035em} h3{margin:0!important;padding:.15rem 0!important;font-size:1.2rem!important}
[data-testid="stVerticalBlock"]{gap:.38rem} [data-testid="stHorizontalBlock"]{gap:.4rem}
.stButton button,.stDownloadButton button{min-height:44px;border-radius:14px;font-weight:700;border:1px solid #dce3ed}
.stButton button[kind="primary"]{background:#126855;border-color:#126855;color:white}
[data-testid="stMetric"]{background:white;border-radius:16px;padding:8px 12px}
.hero{background:#183b51;border-radius:22px;padding:15px 17px;color:#fff;display:flex;justify-content:space-between;align-items:center}
.hero b{font-size:23px}.hero small{color:#d1e5ee;font-size:12px}.hero .logo{font-size:32px}
.summary{display:flex;justify-content:space-between;background:#e3f1eb;border-radius:14px;padding:10px 13px;color:#15543e;font-weight:700}
.med{background:white;border:1px solid #e0e6ef;border-radius:16px;padding:9px 12px;margin-top:4px}
.med b{font-size:16px}.med small{color:#53637a;font-size:12px}.chip{font-size:11px;color:#a14220;background:#fff0e5;padding:3px 6px;border-radius:8px}
[data-testid="stToastContainer"]{top:12px!important;bottom:auto!important}
[data-testid="stDialog"] [role="dialog"]{max-height:88dvh;overflow-y:auto;border-radius:22px}
@media(max-width:600px){.block-container{padding-top:.4rem!important} [data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important}
[data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{min-width:0!important;flex:1!important}
.stButton button{padding:.35rem .25rem;font-size:13px} .stSelectbox{font-size:14px}}
</style>""", unsafe_allow_html=True)

if not ACCESS:
    st.title("💊 가족약속")
    st.warning("가족의 건강 정보를 보호하려면 먼저 가족 비밀번호를 설정하세요.")
    st.code('APP_ACCESS_CODE = "가족만-아는-긴-비밀번호"\nAPP_URL = "https://내앱.streamlit.app"', language="toml")
    st.info("Streamlit 앱 Settings → Secrets에 입력하세요. 영구 기록을 위해 DATABASE_URL도 설정해 주세요. 배포 방법은 app.py 맨 위에 있습니다.")
    st.stop()

# 비밀번호 원문은 세션에 남기지 않는다. 공유 코드를 바꾸면 기존 세션도 다시 로그인.
fingerprint = hashlib.sha256(ACCESS.encode()).hexdigest()
if st.session_state.get("auth") != fingerprint:
    st.markdown('<div class="hero"><div><b>가족약속</b><br><small>우리 가족, 안심 복약</small></div><div class="logo">💊</div></div>', unsafe_allow_html=True)
    with st.form("login"):
        pwd = st.text_input("🔒 가족 비밀번호", type="password")
        if st.form_submit_button("입장하기", use_container_width=True):
            if hmac.compare_digest(pwd.encode(), ACCESS.encode()):
                st.session_state.auth = fingerprint
                st.rerun()
            else:
                st.error("비밀번호를 확인하세요.")
    st.caption("QR에는 비밀번호를 담지 않습니다. 비밀번호를 아는 가족만 함께 기록할 수 있어요.")
    st.stop()

try:
    initialize(DB_URL)
except Exception:
    st.error("저장소에 연결하지 못했어요. DATABASE_URL과 DB 연결 상태를 확인해 주세요. 비밀 연결 정보는 화면에 표시하지 않습니다.")
    st.stop()

if "flash" in st.session_state:
    message, icon = st.session_state.pop("flash")
    st.toast(message, icon=icon, duration="short")

@st.dialog("👨‍👩‍👧 가족 · 약 등록")
def register():
    a, b = st.tabs(["👤 가족", "💊 약"])
    with a:
        with st.form("member_add"):
            name = st.text_input("가족 이름", max_chars=16)
            emoji = st.selectbox("아이콘", ["👩", "👨", "👵", "👴", "🧒", "👧"])
            if st.form_submit_button("가족 등록", use_container_width=True):
                if not name.strip():
                    st.error("이름을 입력하세요.")
                else:
                    with db(True) as q:
                        q("INSERT INTO members VALUES (?,?,?)", (ident(), name.strip(), emoji))
                    notice("가족을 등록했어요.", "👤")
    with b:
        family = rows("SELECT * FROM members ORDER BY name")
        if not family:
            st.info("가족을 먼저 등록해 주세요.")
            return
        with st.form("med_add"):
            who = st.selectbox("누구의 약인가요?", [r["id"] for r in family], format_func=lambda x: next(r["emoji"]+" "+r["name"] for r in family if r["id"]==x))
            name = st.text_input("약 이름", max_chars=50)
            ingredient = st.text_input("유효성분 (복합제는 전체 성분명을 동일한 순서로 입력)", max_chars=150, help="빈칸이면 다른 약과의 성분 중복을 확인할 수 없어요. 부분적으로 겹치는 성분은 자동 감지하지 못해요.")
            kind = st.selectbox("분류", ["정기약", "상비약"])
            st.caption("정기약은 매일 고정 시각에 복용하는 약만 등록하세요. 상비약은 재고만 관리합니다.")
            count = st.selectbox("하루 복용 횟수 (정기약)", [1, 2, 3, 4], index=2)
            # form 안에서는 선택 변경 시 즉시 재실행되지 않으므로 네 시간 입력을 항상 제공.
            st.caption("선택한 횟수만큼 앞에서부터 사용합니다. 예: 3회면 ①②③.")
            cols = st.columns(2)
            defaults = [time(8), time(13), time(19), time(22)]
            ts = [cols[i % 2].time_input(f"{i+1}회 시각", value=defaults[i], key=f"time{i}", step=900) for i in range(4)]
            c, d = st.columns(2)
            expiry = c.date_input("사용기한", value=now().date()+timedelta(days=90), min_value=date(2000,1,1), max_value=date(2100,12,31))
            stock = d.number_input("남은 수량 (정/포 등)", min_value=0, max_value=100000, value=30, step=1)
            dose = c.number_input("1회 차감 수량", min_value=1, max_value=1000, value=1, step=1)
            gap = d.number_input("최소 확인 간격(시간)", min_value=1, max_value=48, value=4, step=1, help="안전 확인용 차단값이지 권장 복용 간격이 아닙니다. 실제 복약 지침에 맞춰 설정하세요.")
            usage = st.text_input("복약 지침 (식후 등, 약 봉투 그대로)", max_chars=250)
            missed = st.text_area("복용을 놓쳤을 때의 지침 (설명서 문구 그대로, 없으면 비워 두세요)", max_chars=1000)
            if st.form_submit_button("약 등록", type="primary", use_container_width=True):
                slots = sorted(t.strftime("%H:%M") for t in ts[:count])
                if not name.strip() or len(set(slots)) != len(slots):
                    st.error("약 이름과 중복되지 않는 복용 시각을 확인하세요.")
                else:
                    with db(True) as q:
                        q("INSERT INTO meds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)", (ident(), who, name.strip(), ingredient.strip(), json.dumps(slots), expiry.isoformat(), stock, dose, gap, missed.strip(), usage.strip(), kind))
                    notice("약을 등록했어요.", "💊")

@st.dialog("🔎 복용 확인", width="small")
def detail(med_id):
    result = rows("SELECT * FROM meds WHERE id=? AND active=1", (med_id,))
    if not result:
        st.info("더 이상 활성화된 약이 아니에요.")
        return
    m = result[0]
    day = now().date().isoformat()
    st.subheader("💊 " + m["name"])
    if m["usage_note"]:
        st.caption(m["usage_note"])
    st.caption(f"사용기한 {m['expiry']} · 재고 {m['stock']} · 1회 {m['dose']}개")
    logs = {r["slot"]: r for r in rows("SELECT * FROM doses WHERE med_id=? AND day=?", (med_id, day))}
    slots = json.loads(m["times"])
    # 3회 약이면 세 구획을 가로로 나눠 각 회차 상태와 시각을 표시.
    cs = st.columns(len(slots))
    for i, slot in enumerate(slots):
        item = logs.get(slot)
        label = "✅ 완료" if item and item["status"]=="taken" else "⏸ 미복용" if item else "🕒 예정" if scheduled(day,slot)>now() else "🔔 미확인"
        with cs[i]:
            st.markdown(f"**{i+1}회 · {slot}**")
            st.caption(label)
            if item and item["reason"]:
                st.caption(item["reason"])
    slot = st.selectbox("확인할 회차", slots, format_func=lambda x: f"{slots.index(x)+1}회 · {x}", key=f"slot_{med_id}")
    item = logs.get(slot)
    if item and item["status"]=="taken":
        st.success("복용 완료 · " + datetime.fromisoformat(item["recorded_at"]).strftime("%H:%M"))
        st.caption("실수로 기록했다면 아래 ‘기록 정정’을 사용하세요. 실제로 한 번 더 먹는 기능이 아닙니다.")
        with st.expander("↩️ 기록 정정"):
            confirmed = st.checkbox("실제로 복용하지 않았는데 잘못 체크한 기록입니다.")
            if st.button("미확인으로 되돌리기", disabled=not confirmed, use_container_width=True):
                with db(True) as q:
                    old = q("SELECT * FROM doses WHERE med_id=? AND day=? AND slot=?", (med_id,day,slot)).fetchone()
                    if old and old["status"]=="taken":
                        q("DELETE FROM doses WHERE id=?", (old["id"],))
                        q("UPDATE meds SET stock=stock+? WHERE id=?", (m["dose"],med_id))
                notice("잘못된 기록을 정정했어요.", "↩️")
        return
    reason = st.selectbox("먹지 않은 이유 / 확인 사항", ["깜빡함", "먹었는지 모름", "몸이 불편함", "약이 없음", "기한 지남", "아직 복용 전"], index=5 if scheduled(day,slot)>now() else 0)
    st.caption(f"예정 시각: 오늘 {slot} · 실제 복용을 확인한 경우에만 완료를 누르세요.")
    a,b = st.columns(2)
    expired = m["expiry"] < day
    early = scheduled(day,slot)>now()
    if a.button("✅ 먹었어요", type="primary", disabled=expired or early, use_container_width=True):
        ok, msg = record(med_id,day,slot,"taken")
        if ok:
            notice(msg)
        else:
            st.error(msg)
    if b.button("⏸ 안 먹었어요", use_container_width=True):
        ok,msg = record(med_id,day,slot,"skipped",reason)
        if ok:
            notice(msg,"📝")
        else:
            st.error(msg)
    if expired:
        st.error("사용기한이 지나 복용 완료 처리가 차단돼요.")
    advice(m, reason)

@st.dialog("🧰 상비약 · 안전 · 기록")
def cabinet(member_id):
    tabs = st.tabs(["🧰 보관함", "🛡 안전", "📒 기록"])
    meds = rows("SELECT * FROM meds WHERE member_id=? AND active=1 ORDER BY kind, name", (member_id,))
    with tabs[0]:
        if not meds:
            st.info("등록된 약이 없어요.")
        for m in meds:
            with st.expander(f"{'💊' if m['kind']=='정기약' else '🧰'} {m['name']} · {m['stock']}개"):
                st.caption(f"{m['kind']} · 사용기한 {m['expiry']} · {m['ingredient'] or '성분 미등록'}")
                st.caption("새 수량은 기록 이후 실제로 남아 있는 총수량을 입력하세요.")
                qty = st.number_input("실제 남은 총수량", min_value=0, max_value=100000, value=int(m["stock"]), key="qty"+m["id"])
                exp = st.date_input("실제 사용기한", value=date.fromisoformat(m["expiry"]), key="exp"+m["id"])
                if st.button("보관 정보 저장", key="save"+m["id"]):
                    with db(True) as q:
                        q("UPDATE meds SET stock=?, expiry=? WHERE id=?", (qty,exp.isoformat(),m["id"]))
                    notice("보관 정보를 수정했어요.","🧰")
                archive = st.checkbox("더 이상 사용하지 않는 약입니다", key="arch"+m["id"])
                if st.button("보관 종료", key="archive"+m["id"], disabled=not archive):
                    with db(True) as q:
                        q("UPDATE meds SET active=0 WHERE id=?", (m["id"],))
                    notice("보관을 종료했어요. 기존 기록은 유지돼요.","📦")
        st.caption("상비약은 목록·기한·수량 관리만 지원합니다. 이 앱이 상비약 복용을 권하지 않습니다.")
    with tabs[1]:
        alerts = safety(meds)
        if alerts:
            for msg in alerts:
                st.warning(msg)
        else:
            st.success("등록 정보 기준 경고가 없어요.")
        st.info("중복 경고는 성분명 전체가 같은 경우만 확인합니다. 복합제 일부 성분 중복·상호작용·알레르기·용량 적절성은 판단하지 않습니다.")
        st.caption("안전 확인 간격은 처방이 아닙니다. 등록값이 잘못되면 필요한 경고가 누락되거나 정상 복용이 차단될 수 있어요.")
    with tabs[2]:
        logs = rows("""SELECT d.*, m.name FROM doses d JOIN meds m ON m.id=d.med_id
            WHERE m.member_id=? ORDER BY d.recorded_at DESC LIMIT 100""", (member_id,))
        if not logs:
            st.info("아직 기록이 없어요.")
        for r in logs:
            st.caption(f"{'✅' if r['status']=='taken' else '⏸'} {r['day']} {r['slot']} · {r['name']} · {r['reason'] or '복용 완료'}")
        st.caption("최근 100건 표시 · 예정 시각과 실제 기록 시각은 다를 수 있어요.")
        if logs:
            import csv
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["예정날짜","예정시각","약","상태","사유","실제기록시각"])
            for r in logs:
                # 스프레드시트 수식 주입 방지.
                cells = [r["day"],r["slot"],r["name"],"완료" if r["status"]=="taken" else "미복용",r["reason"],r["recorded_at"]]
                writer.writerow(["'"+x if x.startswith(("=","+","-","@")) else x for x in cells])
            st.download_button("⬇️ 기록 CSV", buffer.getvalue().encode("utf-8-sig"), "복용기록.csv", "text/csv", use_container_width=True)

def safety(meds):
    out=[]
    today=now().date()
    seen={}
    for m in meds:
        days=(date.fromisoformat(m["expiry"])-today).days
        if days<0:
            out.append(f"⛔ {m['name']} · 사용기한 지남")
        elif days<=30:
            out.append(f"📅 {m['name']} · 기한 {days}일 남음")
        if m["stock"]<m["dose"]:
            out.append(f"📦 {m['name']} · 재고 부족")
        key=norm(m["ingredient"])
        if key:
            if key in seen:
                out.append(f"⚠️ 등록 성분 중복: {seen[key]} / {m['name']}")
            else:
                seen[key]=m["name"]
    return out

@st.dialog("📱 QR로 가족 초대")
def share():
    url=st.text_input("배포된 앱 주소", value=APP_URL, placeholder="https://내앱.streamlit.app")
    st.caption("QR은 설치 파일이 아니라 이 앱의 접속 주소입니다. 비밀번호는 별도로 알려 주세요.")
    if url.startswith("https://"):
        image=qrcode.make(url)
        buff=io.BytesIO()
        image.save(buff,format="PNG")
        st.image(buff.getvalue(),width=220)
        st.download_button("⬇️ QR 저장",buff.getvalue(),"가족약속_QR.png","image/png",use_container_width=True)
    elif url:
        st.warning("https://로 시작하는 배포 주소를 입력하세요.")
    if st.button("🔒 로그아웃",use_container_width=True):
        st.session_state.clear()
        st.rerun()

st.markdown('<div class="hero"><div><b>가족약속</b><br><small>한 번만, 제때, 안전하게</small></div><div class="logo">💊</div></div>',unsafe_allow_html=True)
family=rows("SELECT * FROM members ORDER BY name")
controls=st.columns([3,1,1])
with controls[0]:
    if family:
        member_id=st.selectbox("가족",[r["id"] for r in family],format_func=lambda x:next(r["emoji"]+" "+r["name"] for r in family if r["id"]==x),label_visibility="collapsed")
    else:
        member_id=None
        st.caption("👋 가족을 등록해 주세요")
with controls[1]:
    if st.button("➕",help="가족·약 등록",use_container_width=True):
        register()
with controls[2]:
    if st.button("📱",help="QR 공유·로그아웃",use_container_width=True):
        share()
large=st.toggle("👓 어르신 모드",value=False)
if large:
    st.markdown('<style>.stButton button{font-size:17px!important;min-height:48px}.med b{font-size:21px}.med small{font-size:15px}.summary{font-size:19px}.hero b{font-size:27px}[data-testid="stDialog"] p{font-size:18px}</style>',unsafe_allow_html=True)
if not DB_URL:
    st.caption("⚠️ 임시 저장 모드 · 재배포 시 기록 유실 가능")

@st.fragment(run_every="30s")
def dashboard(member_id, large):
    day=now().date().isoformat()
    meds=rows("SELECT * FROM meds WHERE member_id=? AND active=1 ORDER BY name",(member_id,)) if member_id else []
    regular=[m for m in meds if m["kind"]=="정기약"]
    logs=rows("""SELECT d.* FROM doses d JOIN meds m ON m.id=d.med_id
        WHERE m.member_id=? AND d.day=?""",(member_id,day)) if member_id else []
    lookup={(r["med_id"],r["slot"]):r for r in logs}
    total=sum(len(json.loads(m["times"])) for m in regular)
    done=sum(1 for m in regular for slot in json.loads(m["times"]) if lookup.get((m["id"],slot),{}).get("status")=="taken")
    st.markdown(f'<div class="summary"><span>☀️ {now():%m.%d} 오늘</span><span>✅ {done} / {total}회</span></div>',unsafe_allow_html=True)
    alerts=safety(meds)
    nav=st.columns(2)
    if nav[0].button(f"🛡 안전 {len(alerts)}",use_container_width=True,key="safe_btn",disabled=not member_id):
        cabinet(member_id)
    if nav[1].button("🧰 상비약 · 기록",use_container_width=True,key="cabinet_btn",disabled=not member_id):
        cabinet(member_id)
    if not regular:
        st.info("➕ 가족과 매일 복용할 약을 등록하세요.")
    # 목록은 2건(어르신 1건)씩: 약이 늘어도 대시보드 높이가 계속 늘어나지 않는다.
    page_size=1 if large else 2
    max_page=max(0,(len(regular)-1)//page_size)
    page=min(st.session_state.get("page",0),max_page)
    st.session_state.page=page
    for m in regular[page*page_size:(page+1)*page_size]:
        slots=json.loads(m["times"])
        marks=[]
        for slot in slots:
            r=lookup.get((m["id"],slot))
            icon="✅" if r and r["status"]=="taken" else "⏸" if r else "🔔" if scheduled(day,slot)<=now() else "🕒"
            marks.append(f"{icon} {slot}")
        badge=' <span class="chip">기한 지남</span>' if m["expiry"]<day else ""
        st.markdown(f'<div class="med"><b>💊 {safe(m["name"])}</b>{badge}<br><small>{"　".join(marks)}</small></div>',unsafe_allow_html=True)
        if st.button("복용 확인  ›",key="open_"+m["id"],use_container_width=True):
            detail(m["id"])
    if max_page>0:
        l,c,r=st.columns([1,2,1])
        if l.button("‹",disabled=page==0,use_container_width=True):
            st.session_state.page=page-1
            st.rerun(scope="fragment")
        c.caption(f"약 목록 {page+1} / {max_page+1}")
        if r.button("›",disabled=page==max_page,use_container_width=True):
            st.session_state.page=page+1
            st.rerun(scope="fragment")
    # 음성은 사용자 클릭으로 브라우저 음성 기능을 활성화. 시간은 한국 표준시 기준.
    events=[]
    for m in regular:
        if m["expiry"]<day or m["stock"]<m["dose"]:
            continue
        for slot in json.loads(m["times"]):
            if (m["id"],slot) not in lookup:
                events.append({"id":m["id"]+day+slot,"name":m["name"],"slot":slot,"at":scheduled(day,slot).timestamp()*1000})
    payload=json.dumps(events,ensure_ascii=True).replace("<","\\u003c")
    components.html("""<!doctype html><html lang="ko"><meta charset="utf-8"><style>
    body{margin:0;font-family:system-ui;color:#183b51}button{width:100%;border:1px solid #c5d9d0;border-radius:13px;background:#e3f1eb;color:#15543e;padding:11px;font-size:15px;font-weight:700;cursor:pointer}
    </style><button id="voice">🔊 음성 알림 켜기</button><script>
    const events=PAYLOAD; const button=document.getElementById('voice');let active=false;const seen=new Set();
    function speak(text){if(!('speechSynthesis' in window))return;const u=new SpeechSynthesisUtterance(text);u.lang='ko-KR';u.rate=.85;window.speechSynthesis.speak(u);}
    function tick(){if(!active)return;let names=[];for(const e of events){const delta=Date.now()-e.at;let used=seen.has(e.id);try{used=used||localStorage.getItem('dose-'+e.id)==='1';}catch(_){}
    if(delta>=0&&delta<120000&&!used){seen.add(e.id);try{localStorage.setItem('dose-'+e.id,'1');}catch(_){}names.push(e.name);}}
    if(names.length)speak(names.join(', ')+' 복용 확인 시간입니다. 이미 복용했는지 먼저 확인하세요.');}
    button.onclick=()=>{if(!('speechSynthesis' in window)){button.textContent='이 브라우저는 음성을 지원하지 않아요';return;}active=!active;button.textContent=active?'🔊 음성 알림 켜짐 · 다시 누르면 끄기':'🔇 음성 알림 꺼짐';if(active){speak('음성 알림을 켰습니다. 앱을 열어 두세요.');tick();}else window.speechSynthesis.cancel();};setInterval(tick,10000);
    </script></html>""".replace("PAYLOAD",payload),height=48)
    st.caption("앱을 열어 둘 때만 알림 · 종료/화면 잠금 알림은 지원하지 않아요")

try:
    dashboard(member_id,large)
except Exception:
    st.error("데이터를 불러오지 못했어요. 연결 상태를 확인한 뒤 다시 시도해 주세요.")

# 접근성상 페이지 스크롤을 강제로 잠그지 않는다. 대시보드는 페이지당 최대 두 약.
# 극소형 화면·확대 글꼴·입력 팝업은 스크롤이 필요할 수 있다.
