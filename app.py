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
    home_screen(elder)
    storage = '영구 DB 연결' if PG else '데모 저장 · 재시작 시 유실 가능'
    st.markdown(f'<div class="foot">{storage} · 한국 시간 · 열린 화면에서 20초마다 확인<br>처방을 대체하지 않습니다. 성분·상호작용 전체 판별 불가.</div>', unsafe_allow_html=True)



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
        st.info('아직 기록이 없습니다. 오늘 복용 메뉴에서 사유를 남길 수 있어요.')
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
    :root{--ink:#163047;--accent:#176f70;}
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
    .home-summary{background:#163047;color:white;border-radius:18px;padding:22px;margin:12px 0 20px;}
    .home-summary h2{color:white;margin:0;font-size:28px;line-height:1.5;}
    .home-summary p{margin:8px 0 0;color:#dfebf0;line-height:1.6;}
    </style>""", unsafe_allow_html=True)
    page=st.session_state.get('page','홈')
    if page != '홈':
        if st.button('← 홈으로',use_container_width=True):
            st.session_state.page='홈';st.rerun()
        if page=='오늘 약 먹기':
            people=rows('SELECT * FROM fm_people ORDER BY name')
            if people:
                person=st.selectbox('가족',people,format_func=lambda p:p['name'])
                dashboard(person['id'],elder)
            else:
                st.info('처음이시군요. 가족과 약부터 등록해 주세요.')
                if st.button('가족 · 약 등록',type='primary'):registration()
        elif page=='놓친 약 확인':
            missed_flow(elder)
        elif page=='약 보관함':
            st.subheader('우리 집 약 보관함')
            if st.button('＋ 가족 · 약 등록',type='primary',use_container_width=True):registration()
            if st.button('약 목록 · 유효기간 확인',use_container_width=True):inventory()
            if st.button('등록 가족 보기',use_container_width=True):family_list()
        elif page=='기록':
            st.subheader('복용과 확인 기록')
            if st.button('먹은 기록 보기',use_container_width=True):history_dialog()
            with st.expander('놓친 약 · 후속 조치 기록'):
                followup_history()
        return
    today=now().date().isoformat()
    done=rows('SELECT id FROM fm_logs WHERE day=?',(today,))
    pending=unresolved_slots()
    st.markdown(f'<div class="home-summary"><h2>오늘도, 잊지 않도록.</h2><p>오늘 복용 기록 {len(done)}회 · 확인이 필요한 일정 {len(pending)}건</p></div>',unsafe_allow_html=True)
    if st.button('💊  오늘 약 먹기',type='primary',use_container_width=True):
        st.session_state.page='오늘 약 먹기';st.rerun()
    if st.button(f'🕒  놓친 약 확인 · {len(pending)}건',use_container_width=True):
        st.session_state.page='놓친 약 확인';st.rerun()
    c1,c2=st.columns(2)
    if c1.button('🧰 약 보관함',use_container_width=True):
        st.session_state.page='약 보관함';st.rerun()
    if c2.button('📋 기록',use_container_width=True):
        st.session_state.page='기록';st.rerun()
    with st.expander('가족 초대 · 사용 안내'):
        if st.button('QR로 가족 초대'):share_dialog()
        st.caption('복용 시각이 지나도 실제 미복용인지는 알 수 없습니다. 가족과 확인해 주세요. 앱 종료·화면 잠금 중 알림은 보장되지 않습니다.')


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
    st.subheader('놓친 약, 차근차근 확인해요')
    options=unresolved_slots()
    if not options:
        st.success('현재 확인이 필요한 일정이 없습니다.');return
    item=st.selectbox('확인할 약',options,format_func=lambda x:f"{x['day']} {x['slot']} · {x['m']['person']} · {x['m']['name']}")
    m=item['m'];day=item['day'];slot=item['slot'];key=f"{m['id']}_{day}_{slot}"
    st.caption('복용 기록이 없는 일정입니다. 실제로 안 드셨다는 뜻은 아닙니다.')
    status=st.radio('1. 지금 어떤 상황인가요?', ['선택해 주세요','먹었는지 확실하지 않아요','깜빡해서 못 먹었어요','사정상 먹지 못했어요','이미 먹었지만 기록하지 않았어요'],key='triage_'+key)
    if status=='선택해 주세요':return
    if status=='이미 먹었지만 기록하지 않았어요':
        st.info('추가로 먹지 말고 실제로 먹은 시각을 확인하세요. 오늘 약 먹기에서 기록하면 저장 시각이 남습니다. 과거 시각의 복용 기록으로 소급되지는 않습니다.')
        if st.button('오늘 약 먹기로 이동',use_container_width=True):
            st.session_state.page='오늘 약 먹기';st.rerun()
        return
    if status=='먹었는지 확실하지 않아요':
        st.warning('우선 약 봉투·약통과 가족의 기록을 확인하세요. 확실하지 않은 상태에서 추가 복용하지 말고 약사·의료진에게 확인하세요.')
    else:
        st.info('못 먹은 사유만으로 지금 복용해도 되는지 판단할 수 없습니다.')
    times=sorted(json.loads(m['times']))
    future=[datetime.combine(now().date()+timedelta(days=d),datetime.strptime(t,'%H:%M').time(),TZ) for d in (0,1) for t in times]
    nxt=min((t for t in future if t>now()),default=None)
    if nxt:st.caption('다음 등록 일정: '+nxt.strftime('%m월 %d일 %H:%M')+' · 다음 시각이 가깝다는 이유만으로 건너뛰기를 자동 판단하지 않습니다.')
    st.markdown('**2. 약별 안내를 확인하세요**')
    st.write('약 봉투·처방전의 놓쳤을 때 안내를 확인하거나 약사·처방 의료진에게 문의하세요. 두 회분을 한꺼번에 먹거나 용량을 임의로 늘리지 마세요.')
    st.caption('상담 시 전달: '+m['name']+' / '+m['dose']+' / 놓친 시각 '+day+' '+slot+' / 다음 일정 및 마지막 실제 복용 시각')
    with st.expander('갑자기 몸이 좋지 않다면'):
        st.write('호흡곤란·의식 저하 등 응급 증상이 있으면 앱 기록보다 119 또는 응급 진료가 우선입니다. 약을 먹기 어려운 상태가 지속되거나 중요한 약을 놓쳤다면 의료진에게 신속히 문의하세요.')
    st.markdown('**3. 확인한 후속 조치를 남겨주세요**')
    action_label=st.selectbox('확인 결과',['아직 확인 중이에요','확인한 안내에 따라 이번 회차를 건너뛰어요','확인한 안내에 따라 복용했어요','기록·가족 확인으로 정리됐어요'],key='action_'+key)
    source=st.text_input('확인한 곳 (예: 약 봉투, ○○약국)',max_chars=120,key='source_'+key)
    guidance=st.text_area('확인 내용 · 실제 복용 시각 등',max_chars=800,key='guidance_'+key)
    confirmed=st.checkbox('제가 확인한 내용을 기록합니다. 앱이 복용 여부를 판단한 것은 아닙니다.',key='ack_'+key)
    if st.button('확인 내용 저장',type='primary',use_container_width=True,disabled=not confirmed):
        action=['확인 대기','안내 확인 후 건너뜀','안내 확인 후 복용 보고','기록 확인 완료'][['아직 확인 중이에요','확인한 안내에 따라 이번 회차를 건너뛰어요','확인한 안내에 따라 복용했어요','기록·가족 확인으로 정리됐어요'].index(action_label)]
        if day==now().date().isoformat() and status in ['깜빡해서 못 먹었어요','사정상 먹지 못했어요']:
            save_missed(m['id'],day,slot,'깜빡했어요' if status.startswith('깜빡') else '사정상 못 먹었어요','후속 조치 화면에서 기록')
        ok,msg=save_followup(m['id'],day,slot,action,guidance,source)
        if ok:
            st.success('저장했습니다. 확인 중인 건은 목록에 남고, 확인이 끝난 건은 기록에서 볼 수 있습니다. 실제 복용 기록은 자동 생성되지 않습니다.')
        else:st.warning(msg)
    if elder:speech('놓친 약은 임의로 두 번 먹지 마세요. 약별 안내나 약사에게 확인하고 확인 내용을 남겨주세요.')


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
        st.caption('기록이 없다고 추가 복용하지 마세요. 놓쳤거나 확실하지 않다면 아래에서 확인하세요.')
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
    elif due:
        st.caption(f'🔔 현재 확인할 복용 일정 {len(due)}개 · 완료되지 않았다고 추가 복용하지 마세요.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # 비밀 연결 URL이나 개인정보가 예외 메시지에 노출되지 않게 합니다.
        st.error('처리 중 문제가 발생했습니다. 저장소 연결을 확인하고 새로고침하세요. 복용 기록이 저장됐는지 확인한 뒤 다시 진행하세요.')
