"""가족 약 지킴이 — Streamlit Community Cloud / 로컬 실행

실행: pip install -r requirements.txt && streamlit run app.py
GitHub 저장소에는 이 파일과 requirements.txt 두 개만 올립니다.
Streamlit Cloud: New app → 저장소 선택 → Main file path = app.py.
Settings → Secrets 에 아래 값을 설정하세요(비밀번호는 GitHub에 올리지 마세요).
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
    background:#e2f4ec;color:#114c38;font-size:24px;font-weight:800">🔊 눌러서 음성 안내</button>
    <script>const text=TEXT;function speak(){if(!('speechSynthesis' in window)){
    document.getElementById('s').innerText='이 브라우저는 음성을 지원하지 않아요';return;}
    speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(text);u.lang='ko-KR';
    u.rate=0.85;speechSynthesis.speak(u);}document.getElementById('s').onclick=speak;
    AUTO</script>'''.replace('TEXT', encoded).replace('AUTO', 'speak();' if automatic else ''), height=76)


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
            owner = st.selectbox('복용할 가족', people, format_func=lambda p: p['name'])
            name = st.text_input('약 이름', max_chars=70)
            ingredient = st.text_input('주성분 (모르면 비워두세요)', max_chars=120, help='동일 성분 비교는 입력한 문자열이 같은 경우만 가능합니다. 복합제·다른 표기는 자동 판별하지 않습니다.')
            dose = st.text_input('1회 복용량 / 주의사항', placeholder='처방대로 입력: 예) 1정, 식후', max_chars=160)
            expiry = st.date_input('포장에 표시된 사용기한 / 유효기간', value=date.today() + timedelta(days=30), min_value=date(2000, 1, 1), max_value=date(2100, 12, 31))
            stock = st.checkbox('상비약 목록에도 표시')
            times = st.text_input('매일 복용 시각 (쉼표로 구분)', placeholder='08:00, 13:00, 19:00', help='상비약만 보관할 때는 비워두세요. 필요시 복용 약의 임의 일정을 만들지 마세요.')
            gap = st.number_input('최소 복용 간격 (시간, 처방에 있는 경우만)', min_value=0.0, max_value=168.0, value=0.0, step=0.5, help='0은 간격 검사를 하지 않음. 입력한 동일 주성분 약 기록에도 적용됩니다. 의학적으로 안전한 간격을 앱이 결정하지 않습니다.')
            submit = st.form_submit_button('약 등록', use_container_width=True)
        if submit:
            parts = [v.strip() for v in times.split(',') if v.strip()]
            if not name.strip() or not dose.strip():
                st.warning('약 이름과 복용량/주의사항을 입력하세요.')
            elif any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', x) for x in parts):
                st.warning('시각은 08:00처럼 입력하세요.')
            elif len(set(parts)) > 8:
                st.warning('하루 최대 8회까지 등록할 수 있습니다.')
            elif not parts and not stock:
                st.warning('복용 시각을 넣거나 상비약으로 표시하세요.')
            else:
                with database(True) as conn:
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
    st.dataframe([{'가족': r['person'], '약': r['name'], '예정': r['slot'], '실제': datetime.fromisoformat(r['taken_at']).strftime('%H:%M:%S')} for r in logs], hide_index=True, use_container_width=True)
    selected = st.selectbox('잘못 체크한 기록', logs, format_func=lambda r: f"{r['person']} · {r['name']} · {r['slot']}")
    checked = st.checkbox('실제로 복용하지 않은 오기록만 취소합니다')
    st.caption('실제로 복용한 기록은 지우지 마세요. 취소하면 중복 방지 판단에서도 제외됩니다.')
    if st.button('오기록 취소', disabled=not checked, use_container_width=True):
        with database(True) as conn:
            execute(conn, 'DELETE FROM fm_logs WHERE id=?', (selected['id'],))
        st.rerun()


@st.dialog('QR로 가족 초대', width='small')
def share_dialog():
    url = st.text_input('배포된 앱 주소', value=setting('APP_URL'), placeholder='https://이름.streamlit.app')
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
    st.markdown(
        '<div class="app-heading" role="heading" aria-level="1">💊 가족 약 지킴이</div>'
        '<p class="app-notice">로그인 없는 체험용 · 실제 이름 대신 별칭을 사용하세요.</p>',
        unsafe_allow_html=True,
    )
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
    people = rows('SELECT * FROM fm_people ORDER BY name')
    if not people:
        st.info('가족과 약을 등록하면 오늘의 복용 일정이 표시됩니다.')
        if st.button('＋ 첫 가족 · 약 등록', type='primary', use_container_width=True):
            registration()
    else:
        person = st.selectbox('누가 드실 약인가요?', people, format_func=lambda p: '👤 ' + p['name'], label_visibility='visible' if elder else 'collapsed')
        dashboard(person['id'], elder)
    if elder:
        # One full-width control per row: no cramped four-column buttons.
        if st.button('＋ 가족 · 약 등록', use_container_width=True):
            registration()
        if st.button('💊 보관 중인 약 보기', use_container_width=True):
            inventory()
        if st.button('📋 먹은 기록 보기', use_container_width=True):
            history_dialog()
        if st.button('📷 가족 초대 QR', use_container_width=True):
            share_dialog()
    else:
        c1, c2, c3, c4 = st.columns(4)
        if c1.button('＋등록', use_container_width=True):
            registration()
        if c2.button('상비약', use_container_width=True):
            inventory()
        if c3.button('기록', use_container_width=True):
            history_dialog()
        if c4.button('QR', use_container_width=True):
            share_dialog()
    storage = '영구 DB 연결' if PG else '데모 저장 · 재시작 시 유실 가능'
    st.markdown(f'<div class="foot">{storage} · 한국 시간 · 열린 화면에서 20초마다 확인<br>처방을 대체하지 않습니다. 성분·상호작용 전체 판별 불가.</div>', unsafe_allow_html=True)


@st.fragment(run_every='20s')
def dashboard(person_id, elder):
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
    st.caption(f"오늘 {len(done)}/{len(slots)}회 기록  ·  기한 경과 {expired}개 / 30일 이내 {soon}개")
    flash = st.session_state.pop('flash', None)
    if flash:
        (st.success if flash[0] else st.warning)(flash[1])
    if not slots:
        st.info('오늘의 일정이 없습니다. ＋등록에서 복용 시각을 입력하세요.')
        if elder:
            speech('오늘 등록된 복용 일정이 없습니다.')
        return
    # 한 회차씩 선택하므로 약이 많아져도 본문 길이가 늘어나지 않습니다.
    ordered = pending + [s for s in slots if s['done']]
    selected = st.selectbox('드실 약과 시간을 고르세요' if elder else '복용 회차', ordered,
        format_func=lambda s: f"{'✅' if s['done'] else '🕒'} {s['time']} · {s['m']['name']}", key=f'slot_{person_id}_{today}')
    m, slot = selected['m'], selected['time']
    expired_selected = m['expiry'] < today
    early = selected['scheduled'] > current
    state = '복용 완료' if selected['done'] else '기한 경과 · 복용 금지' if expired_selected else '예정 시각 전' if early else '복용 확인이 필요해요'
    st.markdown(f'''<div class="hero"><span class="badge">{safe(slot)} · {state}</span>
    <div class="name">{safe(m['name'])}</div><div class="small">{safe(m['dose'])}<br>
    유효기간 {safe(m['expiry'])}</div></div>''', unsafe_allow_html=True)
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
    disabled = selected['done'] or expired_selected or not early_confirmed or (same_ingredient and not confirmed)
    if st.button('✅ 먹었어요' if not selected['done'] else '✅ 이미 기록했어요', type='primary', disabled=disabled, use_container_width=True):
        result = take(m['id'], today, slot, confirmed)
        st.session_state.flash = result
        st.rerun(scope='fragment')
    if elder:
        message = f"{slot}, {m['name']}. {m['dose']}. {state}."
        signature = '|'.join(f"{s['m']['id']}:{s['time']}" for s in due)
        auto = bool(signature and st.session_state.get('announced') != signature)
        if auto:
            message = f'복용 시간이 된 일정이 {len(due)}개 있습니다. 화면에서 확인하세요. ' + message
            st.session_state.announced = signature
        speech(message, auto)
    elif due:
        st.caption(f'🔔 현재 확인할 복용 일정 {len(due)}개 · 완료되지 않았다고 추가 복용하지 마세요.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # 비밀 연결 URL이나 개인정보가 예외 메시지에 노출되지 않게 합니다.
        st.error('처리 중 문제가 발생했습니다. 저장소 연결을 확인하고 새로고침하세요. 복용 기록이 저장됐는지 확인한 뒤 다시 진행하세요.')
