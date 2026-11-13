"""가족 약 지킴이 — Streamlit Community Cloud / 로컬 실행

실행: pip install -r requirements.txt && streamlit run app.py
GitHub 저장소에는 이 파일과 requirements.txt 두 개만 올립니다.
Streamlit Cloud: New app → 저장소 선택 → Main file path = app.py.
Settings → Secrets 에 아래 선택 설정을 입력할 수 있습니다.
APP_URL = "https://실제주소.streamlit.app"
# 선택, 권장: 영구 저장 가능한 PostgreSQL 연결 URL (TLS 사용)
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require"
# 선택: 접속된 DB를 다른 앱과 구분하려면 별도 DB를 사용하세요.

DATABASE_URL 미설정 시 SQLite 데모 저장소입니다. 같은 서버의 브라우저들은
공유하지만 Community Cloud의 재배포/재시작 시 데이터가 유실될 수 있습니다.
PostgreSQL은 앱이 필요한 테이블을 자동 생성하므로 테이블 생성 권한이 필요합니다.
이 버전은 비밀번호 없이 열리며 앱 주소를 아는 사람은 전체 정보를 조회·수정할 수 있습니다. 의료기록 서비스가
아니며 실제 개인정보 사용 전 접근권한·DB 백업·서비스 보안정책을 검토하세요.
QR에는 앱 주소만 담습니다. QR 스캔 후 바로 앱으로 접속합니다.
브라우저가 닫히거나 휴대폰이 잠기면 자동 알림이 보장되지 않습니다.

기능: 가족/약 등록, 매일 정해진 시각별 복용 기록, 유효기간 차단,
동일 회차 중복 차단, 사용자가 설정한 최소 간격 차단, 동일 성분 주의,
상비약 목록, 기록 조회·취소, 어르신 모드, QR 접속.
모든 일정은 한국 시간, 매일 반복입니다. 날짜별/요일별 처방은 지원하지 않습니다.
최소 간격·용량·복용 시각은 반드시 처방/약사 안내대로 입력하세요.
"""
from __future__ import annotations

import html
import logging
import io
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import qrcode
import streamlit as st
import streamlit.components.v1 as components

TZ = ZoneInfo('Asia/Seoul')
st.set_page_config(page_title='가족 약 지킴이', page_icon='💊', layout='centered', initial_sidebar_state='collapsed')


def setting(key, default=''):
    try:
        return str(st.secrets.get(key, os.environ.get(key, default)))
    except Exception:
        return str(os.environ.get(key, default))


DB_URL = setting('DATABASE_URL')
PG = bool(DB_URL)


@contextmanager
def database(write=False):
    if PG:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(DB_URL, row_factory=dict_row, connect_timeout=10)
    else:
        conn = sqlite3.connect('family_medicine.db', timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        if write:
            conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(conn, sql, args=()):
    return conn.execute(sql.replace('?', '%s') if PG else sql, args)


def rows(sql, args=()):
    with database() as conn:
        return [dict(r) for r in execute(conn, sql, args).fetchall()]


@st.cache_resource
def initialize(_url):
    with database(True) as conn:
        for sql in [
            'CREATE TABLE IF NOT EXISTS fm_removed (med_id TEXT PRIMARY KEY, removed_at TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS fm_people (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE)',
            '''CREATE TABLE IF NOT EXISTS fm_meds (
            id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES fm_people(id),
            name TEXT NOT NULL, ingredient TEXT NOT NULL, dose TEXT NOT NULL,
            expiry TEXT NOT NULL, times TEXT NOT NULL, gap REAL NOT NULL,
            stock INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1)''',
            '''CREATE TABLE IF NOT EXISTS fm_logs (
            id TEXT PRIMARY KEY, med_id TEXT NOT NULL REFERENCES fm_meds(id),
            day TEXT NOT NULL, slot TEXT NOT NULL, taken_at TEXT NOT NULL,
            UNIQUE(med_id, day, slot))'''
            ,'''CREATE TABLE IF NOT EXISTS fm_followup (
            id TEXT PRIMARY KEY, med_id TEXT NOT NULL REFERENCES fm_meds(id),
            day TEXT NOT NULL, slot TEXT NOT NULL, action TEXT NOT NULL, guidance TEXT NOT NULL,
            source TEXT NOT NULL, recorded_at TEXT NOT NULL, UNIQUE(med_id,day,slot))'''
            ,'''CREATE TABLE IF NOT EXISTS fm_missed (
            id TEXT PRIMARY KEY, med_id TEXT NOT NULL REFERENCES fm_meds(id),
            day TEXT NOT NULL, slot TEXT NOT NULL, reason TEXT NOT NULL,
            note TEXT NOT NULL, recorded_at TEXT NOT NULL, UNIQUE(med_id,day,slot))'''
        ]:
            execute(conn, sql)
    return True


def now():
    return datetime.now(TZ)


def norm(text):
    return re.sub(r'\s+', '', text).casefold()


def safe(text):
    return html.escape(str(text))


def speech(text, automatic=False):
    # 실제 음성 재생은 휴대폰 브라우저의 음성 합성 기능을 사용합니다.
    encoded = json.dumps(text, ensure_ascii=False).replace('<', '\\u003c')
    components.html('''<button id="s" style="width:100%;min-height:64px;border:0;border-radius:12px;
    background:#e2f4ec;color:#114c38;font-size:20px;font-weight:800;white-space:nowrap">▶ 안내 듣기</button>
    <script>const text=TEXT;function speak(){if(!('speechSynthesis' in window)){
    document.getElementById('s').innerText='이 브라우저는 음성을 지원하지 않아요';return;}
    speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(text);u.lang='ko-KR';
    u.rate=0.85;speechSynthesis.speak(u);}document.getElementById('s').onclick=speak;
    AUTO</script>'''.replace('TEXT', encoded).replace('AUTO', 'speak();' if automatic else ''), height=76)


@st.dialog('🔊 음성 안내', width='small')
def voice_dialog(text):
    st.write(text)
    speech(text)


def take(med_id, day, slot, duplicate_confirmed=False):
    """등록 정보/중복/간격을 서버에서 재검증하고 한 트랜잭션으로 기록합니다."""
    with database(True) as conn:
        # PostgreSQL은 가족 행을 잠가 서로 다른 약의 동시 기록도 직렬화합니다.
        m = execute(conn, 'SELECT * FROM fm_meds WHERE id=?', (med_id,)).fetchone()
        if not m:
            return False, '약 정보를 찾지 못했습니다.'
        m = dict(m)
        if PG:
            execute(conn, 'SELECT id FROM fm_people WHERE id=? FOR UPDATE', (m['person_id'],)).fetchone()
        current = now()
        if day != current.date().isoformat():
            return False, '날짜가 바뀌었습니다. 새로고침 후 다시 확인하세요.'
        if not m['active'] or slot not in json.loads(m['times']):
            return False, '현재 유효한 복용 일정이 아닙니다.'
        if m['expiry'] < day:
            return False, '유효기간이 지난 약입니다. 복용 기록을 차단했습니다.'
        if execute(conn, 'SELECT id FROM fm_logs WHERE med_id=? AND day=? AND slot=?', (med_id, day, slot)).fetchone():
            return False, '이미 기록된 회차입니다. 다시 복용하지 마세요.'
        history = [dict(r) for r in execute(conn, '''SELECT l.*, m.name, m.ingredient FROM fm_logs l
            JOIN fm_meds m ON m.id=l.med_id WHERE m.person_id=? ORDER BY l.taken_at DESC''', (m['person_id'],)).fetchall()]
        ingredient_warning = False
        for record in history:
            same = record['med_id'] == med_id
            same_ingredient = bool(norm(m['ingredient'])) and norm(m['ingredient']) == norm(record['ingredient'])
            if same or same_ingredient:
                elapsed = (current - datetime.fromisoformat(record['taken_at'])).total_seconds() / 3600
                if elapsed < float(m['gap']):
                    return False, f"설정한 최소 간격 {m['gap']:g}시간이 지나지 않았습니다."
            if record['day'] == day and record['med_id'] != med_id and same_ingredient:
                ingredient_warning = True
        if ingredient_warning and not duplicate_confirmed:
            return False, '오늘 같은 성분의 다른 약 기록이 있습니다. 약사 확인 후 확인란을 체크하세요.'
        execute(conn, 'INSERT INTO fm_logs(id,med_id,day,slot,taken_at) VALUES (?,?,?,?,?)',
                (str(uuid.uuid4()), med_id, day, slot, current.isoformat()))
    return True, '복용을 기록했습니다. 같은 회차는 다시 복용하지 마세요.'


@st.dialog('가족 · 약 등록', width='small')
def registration():
    tab1, tab2 = st.tabs(['👤 가족', '💊 약'])
    with tab1:
        with st.form('person_form'):
            name = st.text_input('가족 이름 또는 별칭', max_chars=24)
            submitted = st.form_submit_button('가족 등록', use_container_width=True)
        if submitted:
            if not name.strip():
                st.warning('이름을 입력하세요.')
            else:
                try:
                    with database(True) as conn:
                        execute(conn, 'INSERT INTO fm_people(id,name) VALUES (?,?)', (str(uuid.uuid4()), name.strip()))
                    st.rerun()
                except Exception:
                    st.error('등록하지 못했습니다. 중복 이름 또는 DB 연결을 확인하세요.')
    with tab2:
        people = rows('SELECT * FROM fm_people ORDER BY name')
        if not people:
            st.info('먼저 가족을 등록하세요.')
            return
        with st.form('med_form'):
            owners = st.multiselect('복용할 가족 (여러 명 선택 가능)', people, default=[people[0]], format_func=lambda p: p['name'])
            st.caption('각자 처방받았거나 복용이 적합하다고 확인된 가족만 선택하세요. 용량·시각이 다르면 따로 등록하세요. 처방약을 가족끼리 나눠 먹지 마세요.')
            name = st.text_input('약 이름', max_chars=70)
            ingredient = st.text_input('주성분 (모르면 비워두세요)', max_chars=120, help='동일 성분 비교는 입력한 문자열이 같은 경우만 가능합니다. 복합제·다른 표기는 자동 판별하지 않습니다.')
            dose = st.text_input('1회 복용량 / 주의사항', placeholder='예시) 처방대로 입력: 예) 1정, 식후', max_chars=160)
            expiry = st.date_input('포장에 표시된 사용기한 / 유효기간', value=date.today() + timedelta(days=30), min_value=date(2000, 1, 1), max_value=date(2100, 12, 31))
            stock = st.checkbox('상비약 목록에도 표시')
            times = st.text_input('매일 복용 시각 (쉼표로 구분)', placeholder='예시) 08:00, 13:00, 19:00', help='상비약만 보관할 때는 비워두세요. 필요시 복용 약의 임의 일정을 만들지 마세요.')
            gap = st.number_input('최소 복용 간격 (시간, 처방에 있는 경우만)', min_value=0.0, max_value=168.0, value=0.0, step=0.5, help='0은 간격 검사를 하지 않음. 입력한 동일 주성분 약 기록에도 적용됩니다. 의학적으로 안전한 간격을 앱이 결정하지 않습니다.')
            submit = st.form_submit_button('약 등록', use_container_width=True)
        if submit:
            parts = [v.strip() for v in times.split(',') if v.strip()]
            if not owners:
                st.warning('복용할 가족을 한 명 이상 선택하세요.')
            elif not name.strip() or not dose.strip():
                st.warning('약 이름과 복용량/주의사항을 입력하세요.')
            elif any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', x) for x in parts):
                st.warning('시각은 08:00처럼 입력하세요.')
            elif len(set(parts)) > 8:
                st.warning('하루 최대 8회까지 등록할 수 있습니다.')
            elif not parts and not stock:
                st.warning('복용 시각을 넣거나 상비약으로 표시하세요.')
            else:
                with database(True) as conn:
                    for owner in owners:
                        execute(conn, '''INSERT INTO fm_meds
                        (id,person_id,name,ingredient,dose,expiry,times,gap,stock,active)
                        VALUES (?,?,?,?,?,?,?,?,?,1)''', (str(uuid.uuid4()), owner['id'], name.strip(), ingredient.strip(), dose.strip(), expiry.isoformat(), json.dumps(sorted(set(parts))), gap, int(stock)))
                st.rerun()


@st.dialog('상비약 · 약 관리', width='small')
def inventory():
    meds = rows('''SELECT m.*, p.name AS person FROM fm_meds m JOIN fm_people p
        ON p.id=m.person_id WHERE m.active=1 ORDER BY m.expiry''')
    stock_only = st.toggle('상비약만 보기', value=True)
    available = [m for m in meds if m['stock'] or not stock_only]
    if not available:
        st.info('등록된 약이 없습니다.')
        return
    selected = st.selectbox('약 목록 (유효기간 순)', available, format_func=lambda m: f"{m['person']} · {m['name']} · {m['expiry']}")
    remaining = (date.fromisoformat(selected['expiry']) - now().date()).days
    st.write(f"**{selected['name']}** · {selected['dose']}")
    st.write(f"성분: {selected['ingredient'] or '미입력'} / 유효기간: {selected['expiry']}")
    if remaining < 0:
        st.error('유효기간 경과 · 복용 금지, 약사에게 폐기 방법 문의')
    elif remaining <= 30:
        st.warning(f'유효기간까지 {remaining}일 · 보관/개봉 후 기한도 확인하세요.')
    else:
        st.success(f'유효기간까지 {remaining}일')
    st.caption('보관 수량·재고 차감은 지원하지 않습니다. 개봉·조제 후 사용기한은 별도로 확인하세요.')
    confirm = st.checkbox('이 약의 일정을 중지하고 목록에서 숨깁니다 (기록은 유지)')
    if st.button('선택 약 보관 중지', disabled=not confirm, use_container_width=True):
        with database(True) as conn:
            execute(conn, 'UPDATE fm_meds SET active=0 WHERE id=?', (selected['id'],))
        st.rerun()


@st.dialog('복용 기록', width='small')
def history_dialog():
    chosen = st.date_input('조회 날짜 (한국 시간)', value=now().date())
    logs = rows('''SELECT l.*, m.name, p.name AS person FROM fm_logs l JOIN fm_meds m
        ON m.id=l.med_id JOIN fm_people p ON p.id=m.person_id WHERE l.day=? ORDER BY l.taken_at DESC''', (chosen.isoformat(),))
    if not logs:
        st.info('이 날짜의 복용 기록이 없습니다.')
        return
    people_names = sorted({r['person'] for r in logs})
    person_filter = st.selectbox('가족', ['전체'] + people_names, key='history_person') if len(people_names)>1 else people_names[0]
    shown = [r for r in logs if person_filter=='전체' or r['person']==person_filter]
    shown = sorted(shown, key=lambda r:r['taken_at'])
    st.subheader('시간순 복용 기록')
    for r in shown:
        actual=datetime.fromisoformat(r['taken_at']).astimezone(TZ).strftime('%H:%M')
        with st.container(border=True):
            st.markdown(f"**🕒 {actual} · {html.escape(r['name'])}**")
            st.caption(f"{r['person']} · 예정 {r['slot']} · 복용 기록됨")
    logs=shown
    selected = st.selectbox('잘못 체크한 기록', logs, format_func=lambda r: f"{r['person']} · {r['name']} · {r['slot']}")
    checked = st.checkbox('실제로 복용하지 않은 오기록만 취소합니다')
    st.caption('실제로 복용한 기록은 지우지 마세요. 취소하면 중복 방지 판단에서도 제외됩니다.')
    if st.button('오기록 취소', disabled=not checked, use_container_width=True):
        with database(True) as conn:
            execute(conn, 'DELETE FROM fm_logs WHERE id=?', (selected['id'],))
        st.rerun()


@st.dialog('✏️ 약 정보 수정', width='small')
def edit_medicine(med_id):
    records=rows('SELECT * FROM fm_meds WHERE id=?',(med_id,))
    if not records:st.info('약을 찾을 수 없습니다.');return
    m=records[0]
    st.caption('이 약을 등록한 가족의 정보만 바뀝니다. 기존 복용 기록의 시각은 유지됩니다.')
    with st.form('edit_med_'+med_id):
        name=st.text_input('약 이름',value=m['name'],max_chars=100)
        ingredient=st.text_input('주성분',value=m['ingredient'],max_chars=100)
        dose=st.text_input('1회 복용량',value=m['dose'],max_chars=100)
        expiry=st.date_input('유효기간',value=date.fromisoformat(m['expiry']))
        times=st.text_input('복용 시각',value=', '.join(json.loads(m['times'])),placeholder='예시) 08:00, 20:00')
        gap=st.number_input('최소 복용 간격 (시간)',min_value=0.0,max_value=168.0,value=float(m['gap']),step=0.5)
        stock=st.checkbox('상비약',value=bool(m['stock']))
        active=st.checkbox('복용 일정 사용',value=bool(m['active']))
        submit=st.form_submit_button('변경 저장',use_container_width=True)
    if submit:
        values=[v.strip() for v in times.split(',') if v.strip()]
        if not name.strip() or not dose.strip():st.warning('약 이름과 복용량을 입력하세요.');return
        if (active and not values) or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',v) for v in values):
            st.warning('복용 시각을 08:00, 20:00 형식으로 입력하세요.');return
        with database(True) as conn:
            execute(conn,'UPDATE fm_meds SET name=?,ingredient=?,dose=?,expiry=?,times=?,gap=?,stock=?,active=? WHERE id=?',(name.strip(),ingredient.strip(),dose.strip(),expiry.isoformat(),json.dumps(sorted(set(values))),gap,int(stock),int(active),med_id))
        st.rerun()


@st.dialog('🗑️ 약 삭제', width='small')
def delete_medicine(med_id):
    records=rows('SELECT m.*,p.name AS person FROM fm_meds m JOIN fm_people p ON p.id=m.person_id WHERE m.id=?',(med_id,))
    if not records:st.info('약을 찾을 수 없습니다.');return
    m=records[0]
    st.markdown(f"**{safe(m['person'])} · {safe(m['name'])}**")
    st.write('정말 삭제하시겠습니까?')
    st.caption('약통과 앞으로의 복용 일정에서 제외됩니다. 과거 복용·미복용 기록은 남습니다.')
    left,right=st.columns(2)
    if left.button('취소',use_container_width=True):st.rerun()
    if right.button('삭제하기',type='primary',use_container_width=True):
        with database(True) as conn:
            execute(conn,'UPDATE fm_meds SET active=0 WHERE id=?',(med_id,))
            execute(conn,'INSERT INTO fm_removed VALUES (?,?) ON CONFLICT(med_id) DO NOTHING',(med_id,now().isoformat()))
        st.rerun()


@st.dialog('QR로 가족 초대', width='small')
def share_dialog():
    url = st.text_input('배포된 앱 주소', value=setting('APP_URL'), placeholder='예시) https://이름.streamlit.app')
    from urllib.parse import urlparse
    parsed = urlparse(url.strip())
    if parsed.scheme == 'https' and parsed.netloc and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
        buffer = io.BytesIO()
        qrcode.make(url.strip()).save(buffer, format='PNG')
        st.image(buffer.getvalue(), width=220)
        st.caption('휴대폰 카메라로 QR 스캔 → 바로 앱 접속. 앱 주소를 아는 사람은 정보를 보고 수정할 수 있습니다.')
    elif url:
        st.warning('로그인 정보·쿼리·# 없는 HTTPS 앱 주소를 입력하세요.')
    st.info('알림은 앱이 열린 동안 동작합니다. 잠금 화면·백그라운드 알림이 아닙니다.')


def main():
    try:
        initialize(DB_URL)
    except Exception:
        st.error('저장소에 연결하지 못했습니다. DATABASE_URL과 DB 권한을 확인하세요.')
        return
    st.markdown('''<style>
    .block-container{max-width:520px;padding:1.1rem 0.9rem 0.8rem!important;}
    /* Hide the entire header, not just its painted height: prevents an invisible
       header layer from intercepting taps on the first widget. */
    [data-testid="stHeader"]{display:none!important;}
    [data-testid="stToolbar"]{display:none!important;}
    .app-heading{font-size:1.55rem;font-weight:800;line-height:1.45;
        margin:0;padding:0;overflow-wrap:anywhere;color:var(--text-color);}
    .app-notice{font-size:0.82rem;line-height:1.6;margin:6px 0 0;
        padding:0;color:var(--text-color);opacity:0.82;overflow-wrap:anywhere;}
    [data-testid="stCheckbox"] label{min-height:48px;align-items:center;
        cursor:pointer;touch-action:manipulation;}
    [data-testid="stCheckbox"] label p{line-height:1.5!important;}

    .hero{background:#e7f4ee;border:1px solid #badbcc;border-radius:18px;padding:12px 14px;color:#123b30;}
    .hero .name{font-size:1.25rem;font-weight:800;line-height:1.4;overflow-wrap:anywhere;}
    .hero .small{font-size:0.88rem;color:#355f50;line-height:1.5;overflow-wrap:anywhere;}
    .badge{display:inline-block;background:#fff;border-radius:20px;padding:3px 10px;font-size:0.84rem;margin-bottom:4px;}
    .foot{font-size:0.73rem;color:#515b57;line-height:1.35;margin-top:5px;}
    button{min-height:42px!important;}
    [data-testid="stDialog"] [role="dialog"]{max-height:90dvh;overflow-y:auto;}
    </style>''', unsafe_allow_html=True)
    if not st.session_state.get('launch_seen_v8', False):
        st.session_state.launch_seen_v8 = True
        st.html('<style> .launch-screen{position:fixed;inset:0;background:#176f70;z-index:999990; display:flex;align-items:center;justify-content:center; animation:launch-out 4s ease-out forwards;pointer-events:none;} .launch-screen svg{width:96px;height:96px;display:block;flex:none;overflow:visible;} @keyframes launch-out{0%,75%{opacity:1;visibility:visible;}100%{opacity:0;visibility:hidden;}} @media(prefers-reduced-motion:reduce){.launch-screen{animation-duration:.15s;}} .launch-content{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:26px}.launch-progress{width:44px;height:44px;border-radius:50%;border:4px solid rgba(255,255,255,.25);border-top-color:#fff;animation:launch-spin .85s linear infinite;box-sizing:border-box}.launch-status{color:#fff!important;-webkit-text-fill-color:#fff!important;font:500 15px sans-serif;letter-spacing:.2px;text-align:center}@keyframes launch-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.launch-progress{animation:none;border-color:#fff}}</style><div class="launch-screen" aria-label="앱 시작"><div class="launch-content"><img src="data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNjgiIGhlaWdodD0iNjgiIHZpZXdCb3g9IjAgMCA2OCA2OCIgcm9sZT0iaW1nIiBhcmlhLWxhYmVsPSLqsIDsobEg7JW9IOyngO2CtOydtCDroZzqs6AiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+ICA8cGF0aCBkPSJNMTggMzJMMzQgMThMNTAgMzIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSI0IiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz4gPHBhdGggZD0iTTIzIDMxVjQ4QzIzIDUwIDI1IDUyIDI3IDUySDQxQzQzIDUyIDQ1IDUwIDQ1IDQ4VjMxIiBmaWxsPSJub25lIiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMy41IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz4gPHJlY3QgeD0iMjkiIHk9IjMwIiB3aWR0aD0iMTAiIGhlaWdodD0iMjAiIHJ4PSI1IiB0cmFuc2Zvcm09InJvdGF0ZSgzNSAzNCA0MCkiIGZpbGw9IiNmZmYiLz4gPHBhdGggZD0iTTMwIDM3TDM4IDQzIiBzdHJva2U9IiMxNzZmNzAiIHN0cm9rZS13aWR0aD0iMiIvPiA8L3N2Zz4=" alt="집과 알약 로고" style="width:96px;height:96px;display:block;max-width:none;object-fit:contain" /><div class="launch-progress" role="status" aria-label="시작 화면 표시 중"></div><div class="launch-status">앱을 시작하고 있어요</div></div></div>')
    elder = st.toggle('👵 큰 글씨 · 음성 안내', key='elder_mode')
    if elder:
        st.markdown('''<style>
        /* Accessible large-print layout. Do not force a fixed viewport height:
           readable text and touch targets take priority over no scrolling. */
        .block-container{max-width:620px;padding:1.2rem 1rem!important;}
        [data-testid="stAppViewContainer"], [role="dialog"]{font-size:24px;}
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li,
        [data-testid="stWidgetLabel"] p,
        [data-testid="stCheckbox"] label p,
        [data-testid="stAlert"] p{font-size:24px!important;line-height:1.65!important;}
        [data-testid="stCaptionContainer"] p{font-size:20px!important;line-height:1.6!important;}
        .app-heading{font-size:34px!important;line-height:1.45!important;}
        .app-notice{font-size:20px!important;line-height:1.65!important;opacity:1;}
        .hero{padding:22px 18px;border:2px solid #276749;border-radius:20px;}
        .hero .name{font-size:36px!important;line-height:1.5!important;color:#102e24;}
        .hero .small{font-size:26px!important;line-height:1.75!important;color:#173e2f;}
        .badge{font-size:24px!important;font-weight:800;line-height:1.6;
            padding:8px 12px;margin-bottom:12px;white-space:normal;}
        .foot{font-size:20px!important;line-height:1.7!important;color:var(--text-color);}
        [data-testid="stButton"] button,
        [data-testid="stFormSubmitButton"] button{
            min-height:68px!important;padding:14px 16px!important;border-width:2px!important;
            border-radius:14px!important;touch-action:manipulation;}
        [data-testid="stButton"] button p,
        [data-testid="stFormSubmitButton"] button p{
            font-size:26px!important;font-weight:750!important;line-height:1.4!important;}
        [data-testid="stBaseButton-primary"]{background:#125b3d!important;color:#fff!important;}
        [data-testid="stCheckbox"] label{min-height:64px!important;padding:8px 0!important;}
        [data-baseweb="select"] > div{min-height:64px!important;}
        [data-baseweb="select"] div,
        [data-baseweb="popover"] li,
        [role="option"]{font-size:24px!important;line-height:1.5!important;}
        [role="option"]{padding-top:14px!important;padding-bottom:14px!important;}
        input,textarea{font-size:24px!important;line-height:1.5!important;min-height:56px;}
        [role="tab"]{min-height:60px!important;}
        [role="tab"] p{font-size:24px!important;}
        button:focus-visible,input:focus-visible{outline:4px solid #c17a00!important;outline-offset:3px;}
        </style>''', unsafe_allow_html=True)
    home_screen(elder)
    storage = '영구 DB 연결' if PG else '데모 저장 · 재시작 시 유실 가능'
    with st.expander('ⓘ 안전 · 저장 안내'):
        st.caption(f'{storage} · 한국 시간 · 열린 화면에서만 일정 확인. 처방을 대체하지 않으며 약물 상호작용 전체를 판별하지 않습니다.')



MISSED_GUIDE = ('놓친 약의 대처는 약마다 다릅니다. 두 회분을 한꺼번에 먹거나 다음 복용량을 '
                '임의로 늘리지 마세요. 처방전·약 봉투의 누락 시 안내를 확인하고, '
                '안내가 없거나 불확실하면 약사 또는 처방 의료진에게 문의하세요. '
                '중요한 약을 놓쳤거나 몸 상태가 좋지 않다면 신속히 의료진에게 연락하세요.')


def save_missed(med_id, day, slot, reason, note):
    with database(True) as conn:
        m = execute(conn, 'SELECT * FROM fm_meds WHERE id=?', (med_id,)).fetchone()
        if not m:
            return False, '약을 찾을 수 없습니다.'
        if PG:
            execute(conn, 'SELECT id FROM fm_people WHERE id=? FOR UPDATE', (m['person_id'],))
        if day != now().date().isoformat() or slot not in json.loads(m['times']):
            return False, '오늘의 등록된 일정만 기록할 수 있습니다.'
        if execute(conn, 'SELECT id FROM fm_logs WHERE med_id=? AND day=? AND slot=?', (med_id, day, slot)).fetchone():
            return False, '이미 복용한 기록이 있습니다. 복용 기록을 먼저 확인하세요.'
        if reason not in ['깜빡했어요', '사정상 못 먹었어요']:
            return False, '사유를 선택하세요.'
        execute(conn, '''INSERT INTO fm_missed (id,med_id,day,slot,reason,note,recorded_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(med_id,day,slot)
            DO UPDATE SET reason=excluded.reason,note=excluded.note,recorded_at=excluded.recorded_at''',
            (str(uuid.uuid4()), med_id, day, slot, reason, note[:300], now().isoformat()))
    return True, '못 먹은 사유를 기록했습니다. 복용 완료로 처리하지 않습니다.'


def missed_history():
    st.subheader('📝 못 먹은 기록')
    st.warning(MISSED_GUIDE)
    records = rows('''SELECT x.*,m.name AS medicine,p.name AS person FROM fm_missed x
        JOIN fm_meds m ON m.id=x.med_id JOIN fm_people p ON p.id=m.person_id
        ORDER BY x.day DESC,x.slot DESC LIMIT 100''')
    if not records:
        st.info('저장된 사유가 없습니다.')
    for r in records:
        taken = rows('SELECT id FROM fm_logs WHERE med_id=? AND day=? AND slot=?', (r['med_id'],r['day'],r['slot']))
        st.write(f"{r['day']} {r['slot']} · {r['person']} · {r['medicine']}")
        st.caption(f"{r['reason']} · {r['note']} · {'이후 복용 기록 있음' if taken else '복용 기록 없음'}")
        if st.button('이 사유 기록 삭제', key='delete_missed_'+r['id']):
            with database(True) as conn:
                execute(conn, 'DELETE FROM fm_missed WHERE id=?', (r['id'],))
            st.rerun()


def home_screen(elder):
    st.markdown("""<style>
    .st-key-family_schedule_row [data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;gap:10px!important;align-items:stretch;}
    .st-key-family_schedule_row [data-testid="stColumn"]:first-child{flex:1 1 0!important;min-width:76px!important;}
    .st-key-family_schedule_row [data-testid="stColumn"]:last-child{flex:3 1 0!important;min-width:0!important;}
    .st-key-family_schedule_row [data-baseweb="select"]>div{min-height:70px!important;border:2px solid #b3ccc6!important;border-radius:14px!important;background:#fff!important;}
    </style>""",unsafe_allow_html=True)

    st.markdown("""<style>
    input::placeholder,textarea::placeholder{color:#737d89!important;-webkit-text-fill-color:#737d89!important;opacity:1!important;}
    .dose-row{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:12px;}
    .dose-row strong{font-size:23px;color:#163047;}
    .top-notice{background:#e2f2ed;border:2px solid #176f70;border-radius:16px;padding:14px;margin:0 0 14px;color:#163047;font-size:19px;font-weight:700;}
    .st-key-today_tools{position:fixed;right:max(6px,calc((100vw - 580px)/2));top:35%;z-index:100;width:46px!important;background:#fff;border:1px solid #abc7c0;border-radius:18px;padding:4px;box-shadow:0 3px 12px #16304720;}
    .st-key-today_tools button{min-width:36px!important;padding:4px!important;}
    .block-container{padding-right:62px!important;padding-bottom:110px!important;}
    </style>""",unsafe_allow_html=True)

    st.markdown("""<style>
    :root{--ink:#163047;--accent:#176f70;}
    [data-testid="stAlert"]{background:#fff3d8!important;color:#163047!important;
      border:1px solid #dfc889!important;border-radius:12px;}
    [data-testid="stAlert"] *{color:#163047!important;-webkit-text-fill-color:#163047!important;}
    [data-testid="stAppViewContainer"] [data-testid="stButton"] button:disabled{
      opacity:0.55!important;cursor:not-allowed!important;box-shadow:none!important;}
    /* Set the actual inner alert surface, not only its outer wrapper. */
    [data-testid="stAlert"],
    [data-testid="stAlertContainer"],
    [data-testid="stAlert"] > div,
    [data-testid="stAlert"] [role="alert"]{
      background:#fff2cf!important;background-color:#fff2cf!important;
      color:#163047!important;border-color:#d8bd78!important;color-scheme:light;}
    [data-testid="stAlert"] p,
    [data-testid="stAlert"] span,
    [data-testid="stAlert"] svg,
    [data-testid="stAlertContainer"] p{
      color:#163047!important;-webkit-text-fill-color:#163047!important;opacity:1!important;}
    .safety-note{background:#fff2cf!important;color:#163047!important;
      border:1px solid #d8bd78;border-left:5px solid #a46b00;
      border-radius:14px;padding:18px 20px;margin:14px 0;
      font-size:20px;line-height:1.65;overflow-wrap:anywhere;}
    .safety-note *{color:#163047!important;-webkit-text-fill-color:#163047!important;}
    /* All ordinary text is dark, including radio labels inherited from dark theme. */
    [data-testid="stAppViewContainer"] [data-testid="stMarkdownContainer"],
    [data-testid="stAppViewContainer"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stAppViewContainer"] h1,
    [data-testid="stAppViewContainer"] h2,
    [data-testid="stAppViewContainer"] h3,
    [data-testid="stRadio"] label,
    [data-testid="stRadio"] label p,
    [data-testid="stRadio"] label span,
    [data-testid="stCheckbox"] label span,
    [data-testid="stToggle"] label span,
    [data-testid="stExpander"] summary *{
      color:#163047!important;opacity:1!important;-webkit-text-fill-color:#163047!important;}
    [data-testid="stRadio"]{background:#fff!important;padding:12px;border-radius:12px;}
    [data-testid="stRadio"] label{min-height:44px;padding:5px 0;}
    [data-testid="stAppViewContainer"] [data-testid="stButton"] button,
    [data-testid="stAppViewContainer"] [data-testid="stButton"] button p,
    [data-testid="stAppViewContainer"] [data-testid="stFormSubmitButton"] button,
    [data-testid="stAppViewContainer"] [data-testid="stFormSubmitButton"] button p{
      background:#e7f1f1!important;color:#143e44!important;
      -webkit-text-fill-color:#143e44!important;border-color:#acc7ca!important;}
    [data-testid="stAppViewContainer"] [data-testid="stBaseButton-primary"]{
      border:2px solid #176f70!important;}
    [data-baseweb="popover"],[role="listbox"],[role="option"]{
      background:#fff!important;color:#163047!important;}
    [role="option"] *{color:#163047!important;-webkit-text-fill-color:#163047!important;}
    /* Explicit contrast for browser/Streamlit dark-theme inheritance. */
    [data-testid="stAppViewContainer"]{color:#163047!important;color-scheme:light;}
    [data-testid="stWidgetLabel"] p,
    [data-testid="stCheckbox"] label p,
    [data-testid="stToggle"] label p,
    [data-testid="stCaptionContainer"] p,
    [data-testid="stExpander"] summary,
    [data-testid="stExpander"] summary p,
    [data-testid="stExpander"] summary span,
    [data-testid="stExpander"] summary svg{color:#163047!important;opacity:1!important;}
    [data-testid="stExpander"]{background:#fff!important;border-color:#c6d5df!important;}
    [data-testid="stCheckbox"], [data-testid="stToggle"]{background:#edf4f6!important;
      border-radius:12px;padding:8px 12px;color:#163047!important;}
    [data-testid="stWidgetLabel"]{color:#163047!important;}
    [data-testid="stBaseButton-secondary"] p{color:#163047!important;}
    [data-testid="stBaseButton-primary"] p{color:#fff!important;}
    [data-baseweb="select"] > div{background:#fff!important;color:#163047!important;}
    [data-baseweb="select"] span,[data-baseweb="select"] input{color:#163047!important;}
    [data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea{
      background:#fff!important;color:#163047!important;-webkit-text-fill-color:#163047!important;}
    [role="dialog"]{background:#f3f6f8!important;color:#163047!important;}
    [role="dialog"] [data-testid="stMarkdownContainer"]{color:#163047!important;}

    [data-testid="stAppViewContainer"]{background:#f3f6f8;}
    .block-container{max-width:580px;}
    .app-heading{color:#163047;letter-spacing:-.6px;}
    .app-notice{color:#526574;}
    .hero{background:white;border:1px solid #dbe5eb;border-left:5px solid #176f70;
      border-radius:16px;padding:22px;color:#163047;box-shadow:0 4px 16px #16304708;}
    .hero .name{color:#163047;}.hero .small{color:#415866;}
    .badge{background:#e8f4f2;color:#155d5f;}
    [data-testid="stButton"] button{border:1px solid #d5e0e6;border-radius:14px;
      min-height:58px;background:#fff;color:#163047;box-shadow:0 2px 5px #16304705;}
    [data-testid="stBaseButton-primary"]{background:#176f70!important;color:white!important;border-color:#176f70!important;}
    [data-testid="stButton"] button p{font-weight:650;}
    .home-summary{background:#e7eef3;color:#163047;border-radius:18px;padding:22px;margin:12px 0 20px;}
    .home-summary h2{color:#163047;margin:0;font-size:28px;line-height:1.5;}
    .home-summary p{margin:8px 0 0;color:#324e61;line-height:1.6;}
    </style>""", unsafe_allow_html=True)
    st.markdown("""<style>
    .block-container{padding-bottom:175px!important;}
    .st-key-bottom_nav{position:fixed;bottom:0;left:50%;transform:translateX(-50%);
      width:min(100%,580px);z-index:990;background:#fff!important;
      border-top:1px solid #c8d9de;padding:10px 10px max(12px,env(safe-area-inset-bottom));
      box-shadow:0 -4px 18px #16304712;}
    .st-key-bottom_nav .bottom-brand svg{display:block!important;flex:0 0 42px!important;width:42px!important;height:42px!important;max-width:none!important;overflow:visible!important;margin:0 auto!important;}
    .st-key-bottom_nav [data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;gap:6px;}
    .st-key-bottom_nav [data-testid="stColumn"]{min-width:0!important;flex:1!important;}
    .st-key-bottom_nav button{min-height:58px!important;padding:6px 2px!important;}
    .st-key-bottom_nav button p{font-size:16px!important;line-height:1.4!important;}
    </style>""",unsafe_allow_html=True)
    # Unified mobile visual system. Keep explicit foreground/surface pairs.
    st.markdown("""<style>
    [data-testid="stAppViewContainer"]{background:#F4F7F6!important;color:#173B38!important;}
    .block-container{max-width:540px!important;padding-left:20px!important;padding-right:20px!important;}
    h2,h3{letter-spacing:-0.7px;line-height:1.35!important;}
    [data-testid="stVerticalBlock"]{gap:14px;}
    .hero{background:#FFFFFF!important;border:1px solid #DAE5E1!important;
      border-radius:24px!important;padding:24px!important;
      box-shadow:0 5px 18px rgba(24,65,55,.045)!important;margin:8px 0 14px;}
    .hero .name{font-weight:750;letter-spacing:-.6px;line-height:1.4;}
    .hero .small{line-height:1.65!important;margin-top:10px;}
    .badge{display:inline-block;padding:5px 11px;border-radius:100px;background:#E2F1EA!important;color:#174E40!important;font-weight:650;}
    [data-testid="stAppViewContainer"] [data-testid="stButton"] button,
    [data-testid="stAppViewContainer"] [data-testid="stFormSubmitButton"] button{
      background:#FFFFFF!important;border:1px solid #D2E0DA!important;
      border-radius:17px!important;color:#173B38!important;min-height:54px;
      box-shadow:none!important;transition:background .15s,border-color .15s;}
    [data-testid="stAppViewContainer"] [data-testid="stButton"] button p,
    [data-testid="stAppViewContainer"] [data-testid="stFormSubmitButton"] button p{
      background:transparent!important;color:#173B38!important;-webkit-text-fill-color:#173B38!important;font-weight:650;}
    [data-testid="stAppViewContainer"] button[kind="primary"]{
      background:#DCEFE5!important;border:2px solid #528F76!important;}
    [data-testid="stAppViewContainer"] button:hover{border-color:#528F76!important;}
    [data-testid="stAppViewContainer"] button:focus-visible{outline:3px solid #528F76!important;outline-offset:3px;}
    [data-baseweb="select"]>div{border-radius:14px!important;min-height:50px;border-color:#D2E0DA!important;}
    [data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea{border-radius:12px!important;}
    [data-testid="stExpander"]{border-radius:16px!important;border:1px solid #DAE5E1!important;}
    .st-key-bottom_nav{border-radius:24px 24px 0 0!important;border-top:1px solid #DAE5E1!important;
      box-shadow:0 -5px 24px rgba(24,65,55,.07)!important;padding-top:12px!important;}
    .st-key-bottom_nav [data-testid="stButton"] button{border:none!important;background:#FFFFFF!important;min-height:56px!important;}
    .st-key-bottom_nav [data-testid="stButton"] button[kind="primary"]{background:#DCEFE5!important;border:1px solid #BAD8C9!important;}
    .st-key-bottom_nav .bottom-brand{padding-bottom:10px!important;}
    [data-testid="stToggle"]{border:1px solid #DAE5E1!important;background:#FFFFFF!important;}
    @media(prefers-reduced-motion:reduce){*{transition:none!important;}.launch-screen{animation-duration:.05s!important;}}
    </style>""",unsafe_allow_html=True)
    st.markdown("""<style>
    .hero .name{font-size:30px;font-weight:800!important;color:#163047!important;}
    .hero .small{font-size:19px;color:#29483F!important;}
    .badge{font-size:18px;font-weight:750!important;}
    [data-testid="stWidgetLabel"] p{font-size:18px;font-weight:700!important;}
    [data-testid="stCaptionContainer"] p{font-size:16px;color:#344F48!important;opacity:1!important;}
    [data-testid="stAppViewContainer"] [data-testid="stButton"] button p{font-size:19px;}
    [data-testid="stAppViewContainer"] [data-testid="stButton"] button[kind="primary"]{
      background:#CBE8D8!important;border:2px solid #316D52!important;}
    [data-testid="stAppViewContainer"] [data-testid="stButton"] button:disabled{opacity:.75!important;}
    [role="option"]{border-bottom:1px solid #DCE6E1!important;padding:16px!important;font-size:19px!important;}
    [role="option"][aria-selected="true"]{background:#D5EBDD!important;font-weight:750!important;}
    .st-key-bottom_nav [data-testid="stButton"] button p{font-size:18px!important;font-weight:750!important;}
    </style>""",unsafe_allow_html=True)
    st.markdown("""<style>
    .st-key-bottom_nav button [data-testid="stIconMaterial"]{font-size:30px!important;width:32px!important;height:32px!important;color:#17483e!important;}
    .st-key-bottom_nav button{min-height:60px!important;}
    </style>""",unsafe_allow_html=True)
    page=st.session_state.get('page','오늘 약 먹기')
    if page=='홈':page='오늘 약 먹기'
    active = '오늘' if page in ['오늘 약 먹기','놓친 약 확인'] else '약통' if page=='약 보관함' else '기록' if page=='기록' else '가족'
    with st.container(key='bottom_nav'):
        cols=st.columns(4)
        for col,icon,label,target in zip(cols,[':material/medication:',':material/medical_services:',':material/history:',':material/group:'],['오늘','약통','기록','가족'],['오늘 약 먹기','약 보관함','기록','가족']):
            if col.button(label if elder else '',icon=icon,help=label,key='nav_'+label,type='primary' if active==label else 'secondary',use_container_width=True):
                st.session_state.page=target;st.rerun()
    if page=='오늘 약 먹기':
        title,add=st.columns([3,1])
        title.subheader('오늘의 복용')
        if add.button('＋ 등록',key='quick_add',use_container_width=True):registration()
        people=rows('SELECT * FROM fm_people ORDER BY name')
        if not people:
            st.info('먼저 가족과 약을 등록하세요.')
            if st.button('＋ 가족 · 약 등록',type='primary',use_container_width=True):registration()
        else:
            dashboard(people[0]['id'],elder)
    elif page=='놓친 약 확인':
        if st.button('← 오늘',key='back_today'):
            st.session_state.page='오늘 약 먹기';st.rerun()
        missed_flow(elder)
    elif page=='약 보관함':
        st.subheader('약통')
        c1,c2=st.columns(2)
        if c1.button('＋ 약 등록',use_container_width=True):registration()
        if c2.button('💊 목록 보기',use_container_width=True):inventory()
        meds=rows('SELECT m.*,p.name AS person FROM fm_meds m JOIN fm_people p ON p.id=m.person_id WHERE NOT EXISTS (SELECT 1 FROM fm_removed r WHERE r.med_id=m.id) ORDER BY m.expiry')
        if not meds:st.info('등록한 약이 없습니다.')
        for m in meds:
            expired=m['expiry']<now().date().isoformat()
            with st.container(border=True):
                info,edit,delete=st.columns([6,1,1],gap='small')
                info.markdown(f"**💊 {safe(m['name'])}**")
                info.write(f"{m['person']} · {m['dose']}")
                info.caption(('⛔ 기한 지남 ' if expired else '📅 유효기간 ')+m['expiry'])
                if edit.button('',icon=':material/edit:',help='약 정보 수정',key='edit_'+m['id']):edit_medicine(m['id'])
                if delete.button('',icon=':material/delete:',help='약 삭제',key='delete_'+m['id']):delete_medicine(m['id'])
    elif page=='기록':
        st.subheader('복용 현황')
        intake_table()
        if st.button('✅ 먹은 기록',use_container_width=True):history_dialog()
        if st.button('✏️ 못 먹은 사유 적기',key='reason_from_history',use_container_width=True):
            st.session_state.page='놓친 약 확인';st.rerun()
        with st.expander('🕒 못 먹은 기록'):followup_history()
    else:
        st.subheader('가족')
        if st.button('＋ 가족 · 약 등록',use_container_width=True):registration()
        people=rows('SELECT * FROM fm_people ORDER BY name')
        for person in people:st.write('👤 '+person['name'])
        if st.button('📷 초대 QR',use_container_width=True):share_dialog()
        with st.expander('사용 안내'):
            st.write('오늘: 복용 확인 · 약통: 약 관리 · 기록: 복용 내역 · 가족: 등록과 초대')
            st.caption('로그인 없는 체험용입니다. 주소를 아는 사람은 기록을 수정할 수 있습니다. 별칭을 사용하세요. 앱 종료·화면 잠금 중 알림은 보장되지 않습니다.')


def unresolved_slots():
    current=now();today=current.date().isoformat()
    meds=rows('SELECT m.*,p.name AS person FROM fm_meds m JOIN fm_people p ON p.id=m.person_id WHERE m.active=1 ORDER BY p.name,m.name')
    result=[]
    for m in meds:
        # Today's elapsed schedules, plus recorded unresolved older misses.
        candidates={(today,t) for t in json.loads(m['times']) if datetime.combine(current.date(),datetime.strptime(t,'%H:%M').time(),TZ)<=current}
        candidates.update((r['day'],r['slot']) for r in rows('SELECT day,slot FROM fm_missed WHERE med_id=?',(m['id'],)))
        for day,slot in candidates:
            if rows('SELECT id FROM fm_logs WHERE med_id=? AND day=? AND slot=?',(m['id'],day,slot)):continue
            follow=rows('SELECT * FROM fm_followup WHERE med_id=? AND day=? AND slot=?',(m['id'],day,slot))
            if follow and follow[0]['action'] in ['안내 확인 후 건너뜀','안내 확인 후 복용 보고','기록 확인 완료']:
                continue
            result.append(dict(m=m,day=day,slot=slot,follow=follow[0] if follow else None))
    return sorted(result,key=lambda x:(x['day'],x['slot'],x['m']['person']))


def save_followup(med_id,day,slot,action,guidance,source):
    if action not in ['확인 대기','안내 확인 후 건너뜀','안내 확인 후 복용 보고','기록 확인 완료']:
        return False,'올바른 항목을 선택해 주세요.'
    if action!='확인 대기' and (not guidance.strip() or not source.strip()):
        return False,'확인한 곳과 내용을 입력해 주세요.'
    with database(True) as conn:
        m=execute(conn,'SELECT * FROM fm_meds WHERE id=?',(med_id,)).fetchone()
        if not m:return False,'약을 찾을 수 없습니다.'
        if slot not in json.loads(m['times']) or day>now().date().isoformat():return False,'일정을 확인해 주세요.'
        execute(conn,'''INSERT INTO fm_followup VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(med_id,day,slot) DO UPDATE SET action=excluded.action,
            guidance=excluded.guidance,source=excluded.source,recorded_at=excluded.recorded_at''',
            (str(uuid.uuid4()),med_id,day,slot,action,guidance[:800],source[:120],now().isoformat()))
    return True,'저장했습니다.'


def missed_flow(elder):
    st.subheader('⏰ 못 먹었어요')
    options=unresolved_slots()
    if not options:
        st.success('확인할 약이 없습니다.');return
    item=options[0] if len(options)==1 else st.selectbox('약 선택',options,format_func=lambda x:f"{x['day']} {x['slot']} · {x['m']['person']} · {x['m']['name']}")
    m=item['m'];day=item['day'];slot=item['slot'];key=f"{m['id']}_{day}_{slot}"
    st.markdown(f"**👤 {safe(m['person'])}　💊 {safe(m['name'])}**")
    st.write(f"{day} · {slot} · {m['dose']}")
    with database(True) as conn:
        execute(conn,"CREATE TABLE IF NOT EXISTS fm_help (med_id TEXT PRIMARY KEY REFERENCES fm_meds(id), guidance TEXT NOT NULL, source TEXT NOT NULL, phone TEXT NOT NULL, updated TEXT NOT NULL)")
    guides=rows('SELECT * FROM fm_help WHERE med_id=?',(m['id'],))
    h=guides[0] if guides else {'guidance':'','source':'','phone':''}
    st.markdown('### 1. 왜 못 먹었나요?')
    reason=st.radio('사유',['깜빡했어요','약이 없었어요','몸이 불편했어요','먹었는지 모르겠어요','다른 이유'],key='why_'+key,label_visibility='collapsed')
    note=st.text_input('어떤 이유인가요?',placeholder='예시) 식사 시간이 달라졌어요',key='why_note_'+key) if reason=='다른 이유' else ''
    if reason=='몸이 불편했어요':
        st.warning('이상 반응이 의심되면 의료진에게 문의하세요. 호흡곤란·의식 저하 등 심한 증상은 119에 연락하세요.')
    if reason=='먹었는지 모르겠어요':
        st.info('다시 먹기 전에 복용 기록과 가족에게 먼저 확인하세요.')
        if st.button('📋 복용 기록 확인',key='check_history_'+key):history_dialog()
    st.markdown('### 2. 놓쳤을 때의 지침을 아나요?')
    known=st.radio('지침 확인',['아직 모르겠어요','알고 있어요'],key='know_'+key,horizontal=True,label_visibility='collapsed')
    detail='';source='';result='확인 대기'
    if known=='알고 있어요':
        if h['guidance']:st.info('등록된 지침: '+h['guidance']+' · 출처: '+h['source'])
        detail=st.text_input('확인한 지침',value=h['guidance'],placeholder='예시) 이 약을 놓쳤을 때 약사에게 안내받은 내용',key='guide_'+key)
        source=st.text_input('어디서 확인했나요?',value=h['source'],placeholder='예시) 약 봉투 / ○○약국',key='guide_source_'+key)
        choice=st.radio('지금 상태',['아직 처리 전','안내에 따라 건너뛰었어요','안내에 따라 복용했어요'],key='guide_result_'+key)
        result={'아직 처리 전':'확인 대기','안내에 따라 건너뛰었어요':'안내 확인 후 건너뜀','안내에 따라 복용했어요':'안내 확인 후 복용 보고'}[choice]
    else:
        st.markdown('**약 봉투에서 ‘복용을 잊었을 때’를 확인하세요.**')
        st.write('없거나 이해하기 어렵다면 약국·처방 의료진에게 문의하세요.')
        phone=st.text_input('문의할 약국·병원 전화번호',value=h['phone'],placeholder='예시) 02-123-4567',key='ask_phone_'+key)
        cleaned=re.sub(r'[\s()-]','',phone)
        if cleaned and re.fullmatch(r'\+?[0-9]{7,15}',cleaned):
            st.link_button('📞 전화해서 확인하기','tel:'+cleaned,use_container_width=True)
        elif cleaned:st.warning('전화번호를 확인하세요.')
        with st.expander('👪 혼자 확인하기 어렵다면 가족에게 요청'):
            last=rows('SELECT taken_at FROM fm_logs WHERE med_id=? ORDER BY taken_at DESC LIMIT 1',(m['id'],))
            message=f"복용 확인을 도와주세요.\n{m['person']} / {m['name']} / {m['dose']}\n놓친 일정: {day} {slot}\n이유: {reason} {note}\n마지막 복용 기록: {last[0]['taken_at'] if last else '없음 (미복용 확정 아님)'}\n약사에게 놓친 회차의 대처법을 확인해 주세요."
            st.text_area('복사해서 보내세요 · 자동 전송 아님',value=message,height=180,key='ask_family_'+key)
    st.info('두 회분을 한꺼번에 먹지 마세요. 앱은 지금 복용할지 대신 결정하지 않습니다.')
    if st.button('확인 대기로 저장' if result=='확인 대기' else '확인 결과 저장',type='primary',use_container_width=True,key='save_branch_'+key):
        if known=='알고 있어요' and (not detail.strip() or not source.strip()):
            st.warning('확인한 지침과 출처를 입력하세요.');return
        ok,msg=save_missed(m['id'],day,slot,reason,note)
        if not ok:st.warning(msg);return
        ok,msg=save_followup(m['id'],day,slot,result,detail,source)
        if not ok:st.warning(msg);return
        if known=='알고 있어요':
            with database(True) as conn:
                execute(conn,"INSERT INTO fm_help VALUES (?,?,?,?,?) ON CONFLICT(med_id) DO UPDATE SET guidance=excluded.guidance,source=excluded.source,updated=excluded.updated",(m['id'],detail.strip(),source.strip(),h['phone'],now().isoformat()))
        st.success('확인 대기로 남겼어요. 확인한 뒤 다시 열어 결과를 남겨주세요.' if result=='확인 대기' else '결과를 저장했어요. 실제 복용 기록은 오늘 메뉴에서 따로 남겨주세요.')



def followup_history():
    labels={'확인 대기':'확인 중','안내 확인 후 건너뜀':'안내 확인 후 건너뜀','안내 확인 후 복용 보고':'안내 확인 후 복용했다고 보고','기록 확인 완료':'기록·가족 확인 완료'}
    records=rows('SELECT f.*,m.name AS medicine,p.name AS person FROM fm_followup f JOIN fm_meds m ON m.id=f.med_id JOIN fm_people p ON p.id=m.person_id ORDER BY f.recorded_at DESC LIMIT 100')
    if not records:st.info('후속 조치 기록이 없습니다.')
    for r in records:
        st.write(f"{r['day']} {r['slot']} · {r['person']} · {r['medicine']}")
        st.caption(labels.get(r['action'],r['action'])+' · '+r['source'])
        st.write(r['guidance'] or '확인 내용 미입력')
        if st.button('다시 확인하기',key='reopen_'+r['id']):
            with database(True) as conn:
                execute(conn,"UPDATE fm_followup SET action='확인 대기' WHERE id=?",(r['id'],))
                execute(conn,'''INSERT INTO fm_missed VALUES (?,?,?,?,?,?,?) ON CONFLICT(med_id,day,slot) DO NOTHING''',(str(uuid.uuid4()),r['med_id'],r['day'],r['slot'],'사정상 못 먹었어요','후속 조치 재확인',now().isoformat()))
            st.session_state.page='놓친 약 확인';st.rerun()
    with st.expander('이전에 남긴 못 먹은 사유'):
        missed_history()


@st.dialog('등록된 가족')
def family_list():
    people=rows('SELECT * FROM fm_people ORDER BY name')
    if not people:
        st.info('등록된 가족이 없습니다.')
    for p in people:
        st.write('👤 '+p['name'])


def intake_table():
    chosen=st.date_input('날짜',value=now().date(),key='table_day')
    day=chosen.isoformat()
    people=rows('SELECT * FROM fm_people ORDER BY name')
    ids=['all']+[p['id'] for p in people]
    names={p['id']:p['name'] for p in people};names['all']='가족 전체'
    who=st.selectbox('가족',ids,format_func=lambda x:names[x],key='table_person')
    medicines=rows('SELECT m.*,p.name AS person FROM fm_meds m JOIN fm_people p ON p.id=m.person_id')
    output=[]
    for m in medicines:
        if who!='all' and m['person_id']!=who:continue
        logs=rows('SELECT * FROM fm_logs WHERE med_id=? AND day=?',(m['id'],day))
        missed=rows('SELECT * FROM fm_missed WHERE med_id=? AND day=?',(m['id'],day))
        follow=rows('SELECT * FROM fm_followup WHERE med_id=? AND day=?',(m['id'],day))
        logmap={r['slot']:r for r in logs};missmap={r['slot']:r for r in missed};fmap={r['slot']:r for r in follow}
        times=set(logmap)|set(missmap)|set(fmap)
        # Historical schedules were not snapshotted; do not invent past missed doses.
        if chosen==now().date() and m['active']:times.update(json.loads(m['times']))
        for slot in sorted(times):
            actual='—';reason=''
            if slot in logmap:
                status='✅ 복용';actual=datetime.fromisoformat(logmap[slot]['taken_at']).astimezone(TZ).strftime('%H:%M')
            elif slot in fmap and fmap[slot]['action']=='안내 확인 후 건너뜀':status='⏭ 안내에 따라 건너뜀'
            elif slot in fmap and fmap[slot]['action']=='안내 확인 후 복용 보고':status='📝 복용 보고 · 시각 확인 필요'
            elif slot in missmap:
                reason=missmap[slot]['reason'];status='❔ 복용 여부 불확실' if '모르' in reason else '❌ 못 먹음 기록'
            else:
                scheduled=datetime.combine(chosen,datetime.strptime(slot,'%H:%M').time(),TZ)
                status='🕒 예정' if scheduled>now() else '❔ 확인 필요'
            if slot in missmap:reason=missmap[slot]['reason']+(' · '+missmap[slot]['note'] if missmap[slot]['note'] else '')
            output.append({'가족':m['person'],'약':m['name'],'예정':slot,'복용 시각':actual,'상태':status,'사유':reason})
    if output:
        output.sort(key=lambda r:(r['예정'],r['가족'],r['약']))
        st.dataframe(output,hide_index=True,use_container_width=True)
    else:st.info('이 날짜에 남긴 기록이 없습니다.')
    st.caption('확인 필요는 미복용 확정이 아닙니다. 지난 날짜는 저장된 기록만 표시합니다. 예정은 현재 등록 일정 기준입니다.')


@st.dialog('🔔 복용 알림', width='small')
def dose_notifications(person_id, person_name, completed, total):
    st.markdown(f'**👤 {safe(person_name)}**')
    notices=rows('SELECT * FROM fm_notices WHERE person_id=? ORDER BY day DESC,slot DESC LIMIT 100',(person_id,))
    for n in notices:
        with st.container(border=True):
            st.markdown(f"**💊 {safe(n['medicine'])} 복용 시간 안내**")
            st.write(f"{n['day']} {n['slot']} 예정 · 복용 여부를 확인해 주세요.")

    if not notices:st.info('알림이 없습니다.')


@st.fragment(run_every='20s')
def dashboard(person_id, elder):
    st.markdown('<style>\n.st-key-family_schedule_row [data-testid="stHorizontalBlock"]{display:flex!important;flex-direction:row!important;flex-wrap:nowrap!important;gap:8px!important;}\n.st-key-family_schedule_row [data-testid="stColumn"]:first-child{width:25%!important;flex:0 0 25%!important;min-width:0!important;}\n.st-key-family_schedule_row [data-testid="stColumn"]:last-child{width:calc(75% - 8px)!important;flex:1 1 0!important;min-width:0!important;}\n</style>',unsafe_allow_html=True)
    # Select family before querying schedules; scalar ids keep selection stable.
    people=rows('SELECT * FROM fm_people ORDER BY name')
    names={p['id']:p['name'] for p in people}
    with st.container(key='family_schedule_row'):
        family_col,schedule_col=st.columns([1,3],gap='small')
    with family_col:
        person_id=st.selectbox('가족',list(names),format_func=lambda k:'👤 '+names[k],key='today_family_compact',label_visibility='collapsed')
    current = now()
    today = current.date().isoformat()
    meds = rows('SELECT * FROM fm_meds WHERE person_id=? AND active=1 ORDER BY name', (person_id,))
    logs = rows('''SELECT l.* FROM fm_logs l JOIN fm_meds m ON m.id=l.med_id
                  WHERE m.person_id=? AND l.day=?''', (person_id, today))
    done = {(l['med_id'], l['slot']) for l in logs}
    slots = []
    for m in meds:
        for t in json.loads(m['times']):
            scheduled = datetime.combine(current.date(), datetime.strptime(t, '%H:%M').time(), TZ)
            slots.append({'m': m, 'time': t, 'scheduled': scheduled, 'done': (m['id'], t) in done})
    slots.sort(key=lambda s: (s['time'], s['m']['name']))
    pending = [s for s in slots if not s['done']]
    due = [s for s in pending if s['scheduled'] <= current and s['m']['expiry'] >= today]
    expired = sum(m['expiry'] < today for m in meds)
    soon = sum(0 <= (date.fromisoformat(m['expiry']) - current.date()).days <= 30 for m in meds)

    if expired:st.warning(f'유효기간 지난 약 {expired}개 · 복용 금지')
    flash = st.session_state.pop('flash', None)
    if flash:
        (st.success if flash[0] else st.warning)(flash[1])
    if not slots:
        schedule_col.info('등록된 복용 일정이 없습니다.')
        if elder:
            speech('오늘 등록된 복용 일정이 없습니다.')
        return
    # 한 회차씩 선택하므로 약이 많아져도 본문 길이가 늘어나지 않습니다.
    ordered = slots  # chronological regardless of completion
    choice_keys=[x['m']['id']+'|'+x['time'] for x in ordered]
    choice_map=dict(zip(choice_keys,ordered))
    choice_key=f'slot_v3_{person_id}_{today}'
    if st.session_state.get(choice_key) not in choice_map:
        st.session_state[choice_key]=next((k for k,x in choice_map.items() if not x['done']),choice_keys[0])
    with schedule_col:
        selected_key=st.selectbox('복용할 약 · 기록',choice_keys,
            format_func=lambda k:f"{'✅ 먹음' if choice_map[k]['done'] else '🕒 예정'} · {choice_map[k]['time']} · {choice_map[k]['m']['name']}",
            key=choice_key,label_visibility='collapsed')
    selected=choice_map[selected_key]
    m, slot = selected['m'], selected['time']
    person_name=rows('SELECT name FROM fm_people WHERE id=?',(person_id,))
    expired_selected=m['expiry']<today
    early=selected['scheduled']>current
    state='복용 완료' if selected['done'] else '유효기간 경과' if expired_selected else '예정' if early else '확인 필요'
    pname=person_name[0]['name'] if person_name else '가족'
    st.write(f"💊 {m['dose']} · {state}")
    with database(True) as conn:
        execute(conn,"CREATE TABLE IF NOT EXISTS fm_notices (id TEXT PRIMARY KEY, person_id TEXT NOT NULL, day TEXT NOT NULL, slot TEXT NOT NULL, medicine TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0)")
        for entry in due:
            notice_id=f"{person_id}|{today}|{entry['m']['id']}|{entry['time']}"
            execute(conn,'INSERT INTO fm_notices VALUES (?,?,?,?,?,0) ON CONFLICT(id) DO NOTHING',(notice_id,person_id,today,entry['time'],entry['m']['name']))
    unread=rows('SELECT id FROM fm_notices WHERE person_id=? AND is_read=0',(person_id,))
    unread_count=len(unread)
    bell_border = '#b42318' if unread_count else '#abc7c0'
    bell_bg = '#fff0ec' if unread_count else '#ffffff'
    st.markdown(f'<style>.st-key-notification_only button{{border:3px solid {bell_border}!important;background:{bell_bg}!important;box-shadow:none!important;animation:none!important;}}</style>',unsafe_allow_html=True)
    if st.session_state.pop('open_notices_'+person_id,False):
        dose_notifications(person_id,pname,len(done),len(slots))
    with st.container(key='today_tools'):
        with st.container(key='notification_only'):
            if st.button(str(unread_count) if unread_count else '',icon=':material/notifications_active:' if unread_count else ':material/notifications:',help=f'안 읽은 알림 {unread_count}건',key='rail_notifications'):
                with database(True) as conn:
                    execute(conn,'UPDATE fm_notices SET is_read=1 WHERE person_id=?',(person_id,))
                st.session_state['open_notices_'+person_id]=True
                st.rerun(scope='fragment')
        if st.button('',icon=':material/alarm_off:',help='못 먹은 약',key='rail_missed'):
            st.session_state.page='놓친 약 확인';st.rerun()
        if st.button('',icon=':material/volume_up:',help='음성 안내',key='rail_voice'):
            voice_dialog(f"{pname}, {slot}, {m['name']}, {m['dose']}",True)
        if st.button('',icon=':material/history:',help='복용 기록',key='rail_history'):history_dialog()
    same_ingredient = False
    if norm(m['ingredient']):
        hist = rows('''SELECT m.id,m.ingredient FROM fm_logs l JOIN fm_meds m ON m.id=l.med_id
                      WHERE m.person_id=? AND l.day=?''', (person_id, today))
        same_ingredient = any(r['id'] != m['id'] and norm(r['ingredient']) == norm(m['ingredient']) for r in hist)
    confirmed = False
    if same_ingredient and not selected['done']:
        confirmed = st.checkbox('⚠ 동일 성분 기록 있음 · 약사에게 확인했어요', key=f'confirm_{m["id"]}_{slot}_{today}')
    early_confirmed = True
    if early and not selected['done']:
        early_confirmed = st.checkbox('예정 시각 전입니다 · 처방에 맞는 복용인지 확인했어요', key=f'early_{m["id"]}_{slot}_{today}')
    follow = rows('SELECT action FROM fm_followup WHERE med_id=? AND day=? AND slot=?',(m['id'],today,slot))
    resolved = bool(follow and follow[0]['action'] != '확인 대기')
    if resolved:
        st.info('후속 조치가 기록된 회차입니다. 수정은 기록 → 후속 조치 → 다시 확인하기에서 진행하세요.')
    disabled = resolved or selected['done'] or expired_selected or not early_confirmed or (same_ingredient and not confirmed)
    if st.button('✅ 먹었어요' if not selected['done'] else '✅ 이미 기록했어요', type='primary', disabled=disabled, use_container_width=True):
        result = take(m['id'], today, slot, confirmed)
        st.session_state.flash = result
        st.rerun(scope='fragment')
    if not selected['done'] and not early:

        if st.button('못 먹었거나, 먹었는지 모르겠어요',use_container_width=True):
            st.session_state.page='놓친 약 확인'
            st.rerun()
    if elder:
        message = f"{slot}, {m['name']}. {m['dose']}. {state}."
        signature = '|'.join(f"{s['m']['id']}:{s['time']}" for s in due)
        auto = bool(signature and st.session_state.get('announced') != signature)
        if auto:
            message = f'복용 시간이 된 일정이 {len(due)}개 있습니다. 화면에서 확인하세요. ' + message
            st.session_state.announced = signature
        speech(message, auto)



if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logging.getLogger(__name__).exception('App operation failed (%s)', type(exc).__name__)
        # 비밀 연결 URL이나 개인정보가 예외 메시지에 노출되지 않게 합니다.
        st.error('앱 처리 중 오류가 발생했습니다. 복용 기록을 확인한 뒤 다시 시도하세요.')
        import traceback
        frames=traceback.extract_tb(exc.__traceback__)
        app_frames=[f for f in frames if f.filename == __file__]
        line=app_frames[-1].lineno if app_frames else 0
        st.caption(f'오류 확인 코드: {type(exc).__name__} · 줄 {line}')
