"""가족약속 — 원본 유지 수정판 (기존 화면/메뉴/복용 버튼 유지).
추가: 글자 대비 보정, 약 이름 확대, 약 사진, 4개 상황 질문, 번호형 실천 안내.
실천 체크는 본인 확인이며 증상 호전/복용 완료를 뜻하지 않습니다.
DB에 action_tasks/med_photos 테이블만 추가하며 기존 테이블은 삭제하지 않습니다.

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
        q("""CREATE TABLE IF NOT EXISTS action_tasks (
            id TEXT PRIMARY KEY, member_id TEXT NOT NULL, med_id TEXT NOT NULL,
            day TEXT NOT NULL, slot TEXT NOT NULL, answers TEXT NOT NULL,
            steps TEXT NOT NULL, checked TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, resolved_at TEXT NOT NULL DEFAULT '',
            UNIQUE(med_id, day, slot))""")
        q("CREATE TABLE IF NOT EXISTS med_photos (med_id TEXT PRIMARY KEY, image TEXT NOT NULL)")
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

def build_plan(m, reason, certainty, symptom, supply, help_at_home):
    """결정론적 안전 행동 안내. 약명만으로 누락 용량/복용 시점을 추론하지 않는다."""
    emergency = symptom == "호흡 곤란·의식 저하·심한 알레르기" or certainty == "중복·과량 복용이 의심돼요"
    if emergency:
        return [
            {"title":"지금 119에 연락하세요", "body":"과량 복용 의심 또는 심한 증상을 알리세요. 직접 운전하지 말고 상담원의 지시를 따르세요."},
            {"title":"추가 복용·억지 구토를 멈추세요", "body":"의식이 흐리면 음식·물·약을 입에 넣지 마세요. 혼자 있다면 즉시 가까운 사람에게 연락하세요."},
            {"title":"약 포장과 복용 정보를 준비하세요", "body":"약 이름, 먹었을 수 있는 양, 마지막 복용 시각을 적어 구조대에 전달하세요. 준비 때문에 신고를 늦추지 마세요."}]
    steps=[]
    if certainty == "먹었는지 확실하지 않아요":
        steps.append({"title":"약통과 빈 포장을 대조하세요", "body":"해당 시간 칸·뜯은 포장·남은 수량을 확인하세요. 앱의 미체크만으로 안 먹었다고 판단하지 마세요."})
        steps.append({"title":"마지막 복용 흔적을 적으세요", "body":"확인된 시각과 확인되지 않은 내용을 나눠 메모하세요. 확실하지 않은 상태에서 추가 복용하지 마세요."})
    elif certainty == "이미 먹은 것이 확실해요":
        steps.append({"title":"이미 먹은 시각을 확인하세요", "body":"실제 복용 시각을 메모하고, 이번 안내를 마친 뒤 해당 회차의 ‘먹었어요’ 기록을 확인하세요. 또 먹지 마세요."})
    else:
        steps.append({"title":"놓친 회차를 따로 표시하세요", "body":"이 회차가 미복용임을 약통이나 메모에 표시하세요. 놓친 횟수를 몰아 먹거나 다음 용량을 두 배로 늘리지 마세요."})
    expired = m['expiry'] < now().date().isoformat() or supply == "사용기한이 지났어요" or reason == "기한 지남"
    if expired:
        steps.append({"title":"기한 지난 약을 분리하세요", "body":"정상 약과 다른 곳에 두고 ‘복용 금지’라고 적으세요. 같은 약의 유효한 포장이 있는지 확인하되 임의로 대체하지 마세요."})
    elif supply == "없거나 수량이 부족해요" or reason == "약이 없음":
        steps.append({"title":"보관 장소와 남은 수량을 확인하세요", "body":"약 봉투·보관함·가족이 보관한 여분을 확인하세요. 다른 사람의 처방약이나 비슷한 이름의 약으로 대신하지 마세요."})
    elif supply == "기한·수량을 모르겠어요":
        steps.append({"title":"포장의 기한과 수량을 확인하세요", "body":"포장에 적힌 사용기한을 오늘 날짜와 비교하고 실제 남은 양을 세세요. 확인 전에는 임의로 추가 복용하지 마세요."})
    if symptom == "가벼운 불편감이 있어요" or reason == "몸이 불편함":
        steps.append({"title":"증상의 변화를 짧게 기록하세요", "body":"어떤 증상인지, 시작 시각, 마지막 복용 시각을 메모하세요. 증상을 누르려고 다른 약을 임의로 더하지 마세요. 호흡 곤란·의식 저하가 생기면 119에 연락하세요."})
    if certainty != "이미 먹은 것이 확실해요":
        if m['missed_note'].strip():
            steps.append({"title":"등록된 누락 지침을 대조하세요", "body":m['missed_note'].strip()+" · 현재 약·용량에 대한 실제 설명서 문구인지 확인하세요. 앱은 이 문구의 정확성을 검증하지 않습니다."})
        else:
            steps.append({"title":"약 봉투의 누락 지침을 찾아보세요", "body":"‘복용을 잊었을 때’ 항목을 확인하세요. 지침이 없거나 이해되지 않으면 앱만으로 지금 먹을지 결정하지 말고, 약 이름·마지막 시각을 준비해 처방기관이나 약사에게 전화로 확인하세요."})
    if certainty == "이미 먹은 것이 확실해요":
        steps.append({"title":"다음 회차와 약을 구분하세요", "body":"약통에 남은 약이 어느 회차인지 표시하세요. 확인된 이번 회차를 다시 챙기지 말고, 이후 복용은 원래 복약 지침을 따르세요."})
    if help_at_home == "함께 확인할 가족이 있어요":
        steps.append({"title":"가족과 확인 결과를 공유하세요", "body":"함께 포장과 기록을 대조하고 누가 약을 챙길지 한 명을 정하세요. 두 사람이 각각 약을 주지 않도록 확인하세요."})
    else:
        steps.append({"title":"확인 결과를 한곳에 남기세요", "body":"확인한 시각과 남은 수량을 메모하고 가능하면 가족에게 메시지로 보내세요. 다음 알림은 보충 복용 지시가 아닙니다."})
    return steps

def photo_data(upload):
    from PIL import Image, ImageOps, UnidentifiedImageError
    if upload.size > 10 * 1024 * 1024:
        raise ValueError("사진은 10MB 이하로 올려 주세요.")
    try:
        im=Image.open(upload)
        if im.width*im.height > 24000000:
            raise ValueError("사진 크기가 너무 커요. 작은 사진으로 다시 올려 주세요.")
        im=ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((800,800))
        buf=io.BytesIO()
        im.save(buf,format="JPEG",quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except (UnidentifiedImageError, OSError):
        raise ValueError("JPG·PNG·WEBP 사진을 선택해 주세요.")

def save_photo(med_id, upload):
    encoded=photo_data(upload)
    with db(True) as q:
        q("""INSERT INTO med_photos VALUES (?,?) ON CONFLICT(med_id)
            DO UPDATE SET image=excluded.image""",(med_id,encoded))

def photo_html(med_id):
    images=rows("SELECT image FROM med_photos WHERE med_id=?",(med_id,))
    if images:
        return '<img class="pill-photo" alt="등록한 실제 약 사진" src="data:image/jpeg;base64,'+images[0]['image']+'">'
    return '<div class="photo-placeholder">실제 약사진을<br>찍어보세요.</div>'

def create_task(m,day,slot,answers,steps):
    with db(True) as q:
        old=q("SELECT id FROM action_tasks WHERE med_id=? AND day=? AND slot=?",(m['id'],day,slot)).fetchone()
        if old:
            return old['id']
        task_id=ident()
        q("INSERT INTO action_tasks (id,member_id,med_id,day,slot,answers,steps,checked,resolved,created_at,resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
          (task_id,m['member_id'],m['id'],day,slot,json.dumps(answers,ensure_ascii=False),json.dumps(steps,ensure_ascii=False),json.dumps([False]*len(steps)),0,now().isoformat(),''))
    return task_id

def task_content(task_id):
    results=rows("SELECT t.*,m.name FROM action_tasks t JOIN meds m ON m.id=t.med_id WHERE t.id=?",(task_id,))
    if not results:
        st.info("저장된 실천 알림이 없어요.")
        return
    task=results[0]
    if task['resolved']:
        st.success("완료한 실천이에요. 복용 완료와는 별개입니다.")
        return
    steps=json.loads(task['steps']); checks=json.loads(task['checked'])
    st.markdown(f"### {safe(task['name'])} · 실천 알림",unsafe_allow_html=True)
    st.caption(f"{task['day']} {task['slot']} 회차 · 직접 실행한 항목만 체크하세요.")
    for i,step in enumerate(steps):
        st.markdown(f'<div class="action-step"><span class="step-number">{i+1}</span><div><strong>{safe(step["title"])}</strong><p>{safe(step["body"])}</p></div></div>',unsafe_allow_html=True)
        key=f"task_{task_id}_{i}"
        checked=st.checkbox(f"{i+1}번 실천했어요",value=checks[i],key=key)
        if checked != checks[i]:
            # 회차별 체크는 DB에서 읽은 최신 배열에 합쳐 다른 가족의 체크를 덮어쓰지 않는다.
            with db(True) as q:
                latest=q("SELECT checked FROM action_tasks WHERE id=? AND resolved=0",(task_id,)).fetchone()
                if latest:
                    arr=json.loads(latest['checked']); arr[i]=checked
                    q("UPDATE action_tasks SET checked=? WHERE id=?",(json.dumps(arr),task_id))
            checks[i]=checked
    st.caption("실천 완료는 증상이 나았거나 복용해도 된다는 뜻이 아닙니다. 실제 실행 여부는 본인 확인으로 기록합니다.")
    if st.button("✅ 해결했어요",type="primary",disabled=not all(checks),use_container_width=True,key="resolve_"+task_id):
        with db(True) as q:
            latest=q("SELECT checked FROM action_tasks WHERE id=? AND resolved=0",(task_id,)).fetchone()
            if latest and all(json.loads(latest['checked'])):
                q("UPDATE action_tasks SET resolved=1,resolved_at=? WHERE id=?",(now().isoformat(),task_id))
        notice("실천 완료를 기록했어요. 알림을 해제했습니다.")

@st.dialog("🔔 남아 있는 실천 알림")
def action_center(member_id):
    pending=rows("SELECT t.id,t.day,t.slot,m.name FROM action_tasks t JOIN meds m ON m.id=t.med_id WHERE t.member_id=? AND t.resolved=0 ORDER BY t.created_at",(member_id,))
    if not pending:
        st.success("남은 실천 알림이 없어요.")
        return
    ids=[t['id'] for t in pending]
    tid=st.selectbox("실천할 알림",ids,format_func=lambda x:next(f"{t['name']} · {t['day']} {t['slot']}" for t in pending if t['id']==x))
    task_content(tid)


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
.med b{font-size:24px;line-height:1.2;overflow-wrap:anywhere}.med small{color:#53637a;font-size:12px}.chip{font-size:11px;color:#a14220;background:#fff0e5;padding:3px 6px;border-radius:8px}
[data-testid="stToastContainer"]{top:12px!important;bottom:auto!important}
[data-testid="stDialog"] [role="dialog"]{max-height:88dvh;overflow-y:auto;border-radius:22px}
@media(max-width:600px){.block-container{padding-top:.4rem!important} [data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important}
[data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{min-width:0!important;flex:1!important}
.stButton button{padding:.35rem .25rem;font-size:13px} .stSelectbox{font-size:14px}}
/* 다크 테마가 선택돼 있어도 배경과 글자를 함께 고정해 투명/흰 글자 문제 방지 */
:root{color-scheme:light!important;--text-color:#18273d;--background-color:#f2f5f9;--secondary-background-color:#ffffff;--primary-color:#126855}
[data-testid="stAppViewContainer"], [data-testid="stDialog"] [role="dialog"]{background:#f2f5f9!important;color:#18273d!important}
[data-testid="stMarkdownContainer"]{color:#18273d}
[data-testid="stWidgetLabel"] p,[data-testid="stCaptionContainer"]{color:#40536a!important}
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"],.stDownloadButton button{background:#fff!important;color:#18273d!important;border-color:#c3cfdc!important}
[data-testid="stBaseButton-primary"],[data-testid="stBaseButton-primaryFormSubmit"]{background:#126855!important;color:#fff!important}
.stButton button p,.stDownloadButton button p,[data-testid="stFormSubmitButton"] button p,button [data-testid="stMarkdownContainer"]{color:inherit!important}
button:disabled{background:#e5e9ef!important;color:#536175!important;opacity:1!important}
[data-baseweb="input"],[data-baseweb="input"] input,[data-baseweb="textarea"] textarea,[data-baseweb="select"]>div{background:#fff!important;color:#18273d!important;-webkit-text-fill-color:#18273d!important}
[data-baseweb="popover"],[role="listbox"],[role="option"]{background:#fff!important;color:#18273d!important}
[data-testid="stExpander"]{background:#fff;color:#18273d}
[data-testid="stAlert"]{background:#eaf1f8!important;color:#18273d!important}
[data-testid="stAlert"] p{color:#18273d!important}
[data-testid="stDialog"] [aria-label="Close"]{color:#18273d!important}
[data-testid="stRadio"] label,[data-testid="stCheckbox"] label{color:#18273d!important}
.hero b{color:#fff!important}.hero small{color:#d1e5ee!important}
.med{display:flex;gap:10px;align-items:center;min-height:80px}.med-content{flex:1;min-width:0}
.pill-photo{width:72px;height:72px;object-fit:cover;border-radius:12px;flex-shrink:0}
.photo-placeholder{width:72px;height:72px;display:flex;align-items:center;justify-content:center;text-align:center;background:#e7e9ed;color:#637080;font-size:11px;line-height:1.5;border:1px dashed #aab4c0;border-radius:12px;flex-shrink:0}
.action-step{display:flex;align-items:flex-start;gap:12px;background:#fff;border:1px solid #ccd9e5;border-radius:15px;padding:13px;margin:8px 0 3px;color:#18273d}
.step-number{display:flex;align-items:center;justify-content:center;flex-shrink:0;width:33px;height:33px;background:#126855;color:white;border-radius:50%;font-size:21px;font-weight:800}
.action-step strong{font-size:18px;color:#18273d}.action-step p{font-size:15px;line-height:1.6;color:#40536a;margin:6px 0 0}
[data-testid="stFormSubmitButton"] button{min-height:46px;border-radius:13px;font-weight:700}


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
    initialize(DB_URL + "|minimal-actions-photos-v3")
except Exception:
    st.error("저장소에 연결하지 못했어요. DATABASE_URL과 DB 연결 상태를 확인해 주세요. 비밀 연결 정보는 화면에 표시하지 않습니다.")
    st.stop()

if "flash" in st.session_state:
    message, icon = st.session_state.pop("flash")
    st.toast(message, icon=icon)

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

def guided_help(m,day,slot,reason):
    previous=rows("SELECT id,resolved FROM action_tasks WHERE med_id=? AND day=? AND slot=?",(m['id'],day,slot))
    if previous:
        task_content(previous[0]['id'])
        return
    st.markdown("**상황을 조금 더 확인할게요**")
    prefix=f"guide_{m['id']}_{day}_{slot}"
    with st.form(prefix):
        certainty=st.selectbox("① 실제 복용 여부",["확실히 안 먹었어요","먹었는지 확실하지 않아요","이미 먹은 것이 확실해요","중복·과량 복용이 의심돼요"],index=None,placeholder="선택해 주세요")
        symptom=st.selectbox("② 지금 몸 상태",["특별한 불편감이 없어요","가벼운 불편감이 있어요","호흡 곤란·의식 저하·심한 알레르기"],index=None,placeholder="선택해 주세요")
        supply=st.selectbox("③ 약의 사용기한·수량",["기한 내이고 수량도 있어요","없거나 수량이 부족해요","사용기한이 지났어요","기한·수량을 모르겠어요"],index=None,placeholder="선택해 주세요")
        support=st.selectbox("④ 함께 확인할 가족",["함께 확인할 가족이 있어요","혼자 확인해야 해요"],index=None,placeholder="선택해 주세요")
        submitted=st.form_submit_button("답변에 맞는 실천 안내",type="primary",use_container_width=True)
        if submitted:
            urgent=certainty=="중복·과량 복용이 의심돼요" or symptom=="호흡 곤란·의식 저하·심한 알레르기"
            if not urgent and any(v is None for v in (certainty,symptom,supply,support)):
                st.error("네 가지 질문에 답해 주세요.")
            else:
                certainty=certainty or "먹었는지 확실하지 않아요"
                symptom=symptom or "특별한 불편감이 없어요"
                supply=supply or "기한·수량을 모르겠어요"
                support=support or "혼자 확인해야 해요"
                answers=dict(reason=reason,certainty=certainty,symptom=symptom,supply=supply,support=support)
                steps=build_plan(m,reason,certainty,symptom,supply,support)
                create_task(m,day,slot,answers,steps)
                st.toast("실천 알림을 등록했어요.",icon="🔔")
                st.rerun(scope="fragment")
    st.caption("호흡 곤란·의식 저하 또는 과량 복용 의심이면 질문을 기다리지 말고 119에 연락하세요.")


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
        active_task=rows("SELECT id FROM action_tasks WHERE med_id=? AND day=? AND slot=? AND resolved=0",(med_id,day,slot))
        if active_task:
            task_content(active_task[0]['id'])
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
    st.divider()
    guided_help(m, day, slot, reason)

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
                st.markdown(photo_html(m['id']),unsafe_allow_html=True)
                source=st.radio("약 사진 등록",["사진 선택","직접 촬영"],horizontal=True,key="source_"+m['id'])
                if source=="직접 촬영":
                    upload=st.camera_input("실제 약사진을 찍어보세요.",key="camera_"+m['id'])
                else:
                    upload=st.file_uploader("사진 선택 또는 휴대폰 카메라",type=['jpg','jpeg','png','webp'],key="photo_"+m['id'])
                if st.button("📷 사진 저장",disabled=upload is None,key="photo_save_"+m['id'],use_container_width=True):
                    try:
                        save_photo(m['id'],upload)
                        notice("실제 약 사진을 저장했어요.","📷")
                    except ValueError as exc:
                        st.error(str(exc))
                st.caption("식별용 사진입니다. 사진으로 약을 자동 판별하지 않습니다. 개인정보가 적힌 라벨은 피해주세요.")
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
    st.markdown('<style>.stButton button{font-size:17px!important;min-height:48px}.med b{font-size:29px}.med small{font-size:15px}.summary{font-size:19px}.hero b{font-size:27px}[data-testid="stDialog"] p{font-size:18px}</style>',unsafe_allow_html=True)
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
    pending=rows("SELECT COUNT(*) AS n FROM action_tasks WHERE member_id=? AND resolved=0",(member_id,))[0]['n'] if member_id else 0
    if pending:
        if st.button(f"🔔 실천할 알림 {pending}건 · 확인하기",type="primary",use_container_width=True,key="pending_tasks"):
            action_center(member_id)
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
        st.markdown(f'<div class="med">{photo_html(m["id"])}<div class="med-content"><b>{safe(m["name"])}</b>{badge}<br><small>{"　".join(marks)}</small></div></div>',unsafe_allow_html=True)
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
