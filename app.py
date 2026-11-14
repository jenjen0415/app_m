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
st.set_page_config(page_title='온약 ONYAK', page_icon='💊', layout='centered', initial_sidebar_state='collapsed')
st.markdown('''<style>
[data-testid="stLinkButton"] a,
[data-testid="stLinkButton"] a:visited,
[data-testid="stLinkButton"] a:hover {
 background:#F3F7F5 !important;color:#142D26 !important;
 border:1px solid #78958A !important;opacity:1 !important;
}
[data-testid="stLinkButton"] a * {color:#142D26 !important;opacity:1 !important;}
</style>''',unsafe_allow_html=True)



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
            'CREATE TABLE IF NOT EXISTS fm_hidden_people (person_id TEXT PRIMARY KEY)',
            '''CREATE TABLE IF NOT EXISTS fm_contact_tasks (id TEXT PRIMARY KEY, person_id TEXT NOT NULL, med_id TEXT NOT NULL, day TEXT NOT NULL, slot TEXT NOT NULL, medicine TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)''',
            '''CREATE TABLE IF NOT EXISTS fm_photos (med_id TEXT PRIMARY KEY, image_b64 TEXT NOT NULL, updated TEXT NOT NULL)''',
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


def supply_fields(key, existing=None):
    enabled=st.checkbox('남은 약 · 미리 알림 설정',value=existing is not None,key=key+'_enabled')
    left,right=st.columns(2)
    amount=left.number_input('남은 양 (회분)',min_value=0,max_value=100000,value=existing['remaining'] if existing else 0,step=1,key=key+'_remaining')
    lead=right.number_input('소진 며칠 전 알림',min_value=1,max_value=30,value=existing['lead'] if existing else 3,step=1,key=key+'_lead')
    st.caption('1회분 = 한 번 먹을 양 · 하루 복용 횟수로 며칠분인지 계산해요.')
    return enabled,int(amount),int(lead)


def write_supply(conn,med_id,amount,lead):
    execute(conn,'INSERT INTO fm_supply VALUES (?,?,?,?) ON CONFLICT(med_id) DO UPDATE SET remaining=excluded.remaining,updated=excluded.updated,remind_days=excluded.remind_days',(med_id,amount,now().isoformat(),lead))


@st.dialog('가족 · 약 등록', width='small')
def registration(mode):
    if mode == 'family':
        with st.form('person_form'):
            name = st.text_input('가족 이름 또는 별칭', max_chars=24)
            submitted = st.form_submit_button('가족 등록', use_container_width=True)
        if submitted:
            if not name.strip():
                st.warning('이름을 입력하세요.')
            else:
                try:
                    with database(True) as conn:
                        new_person_id = str(uuid.uuid4())
                        execute(conn, 'INSERT INTO fm_people(id,name) VALUES (?,?)', (new_person_id, name.strip()))
                    st.session_state['pending_family_selection'] = new_person_id
                    st.rerun()
                except Exception:
                    st.error('등록하지 못했습니다. 중복 이름 또는 DB 연결을 확인하세요.')
        members = rows('SELECT * FROM fm_people WHERE id NOT IN (SELECT person_id FROM fm_hidden_people) ORDER BY name')
        if members:
            st.markdown('**등록된 가족**')
        for member in members:
            left, right = st.columns([4,1])
            left.write('👤 '+member['name'])
            if right.button('',icon=':material/delete:',help='가족 삭제',key='remove_person_'+member['id']):
                st.session_state['confirm_remove_person']=member['id']
            if st.session_state.get('confirm_remove_person')==member['id']:
                st.warning(f"{member['name']}님을 삭제하시겠습니까? 가족 목록과 앞으로의 복용 일정에서 제외됩니다. 기존 기록은 보관됩니다.")
                yes,no=st.columns(2)
                if yes.button('삭제하기',key='yes_person_'+member['id']):
                    with database(True) as conn:
                        execute(conn,'INSERT INTO fm_hidden_people(person_id) VALUES (?) ON CONFLICT(person_id) DO NOTHING',(member['id'],))
                        execute(conn,'UPDATE fm_meds SET active=0 WHERE person_id=?',(member['id'],))
                        execute(conn,'UPDATE fm_notices SET is_read=1 WHERE person_id=?',(member['id'],))
                    st.session_state.pop('confirm_remove_person',None)
                    st.session_state['pending_family_selection']=''
                    st.rerun()
                if no.button('취소',key='no_person_'+member['id']):
                    st.session_state.pop('confirm_remove_person',None)
                    st.rerun()
    if mode == 'medicine':
        people = rows('SELECT * FROM fm_people WHERE id NOT IN (SELECT person_id FROM fm_hidden_people) ORDER BY name')
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
            times = ','.join(st.multiselect('매일 복용 시각', [f'{h:02d}:{minute:02d}' for h in range(24) for minute in range(60)],help='처방된 시각만 선택하세요. 상비약만 보관할 때는 비워두세요.'))
            gap = st.number_input('최소 복용 간격 (시간, 처방에 있는 경우만)', min_value=0.0, max_value=168.0, value=0.0, step=0.5, help='0은 간격 검사를 하지 않음. 입력한 동일 주성분 약 기록에도 적용됩니다. 의학적으로 안전한 간격을 앱이 결정하지 않습니다.')
            supply_tables()
            supply_on,supply_amount,supply_lead=supply_fields('register_supply')
            submit = st.form_submit_button('약 등록', use_container_width=True)
        if submit:
            parts = [v.strip() for v in times.split(',') if v.strip()]
            if not owners:
                st.warning('복용할 가족을 한 명 이상 선택하세요.')
            elif not name.strip() or not dose.strip():
                st.warning('약 이름과 복용량/주의사항을 입력하세요.')
            elif any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', x) for x in parts):
                st.caption('복용 시각 입력칸에 08:00 형식으로 입력해 주세요.')
            elif len(set(parts)) > 8:
                st.warning('하루 최대 8회까지 등록할 수 있습니다.')
            elif not parts and not stock:
                st.warning('복용 시각을 넣거나 상비약으로 표시하세요.')
            else:
                with database(True) as conn:
                    for owner in owners:
                        new_med_id=str(uuid.uuid4())
                        execute(conn, '''INSERT INTO fm_meds
                        (id,person_id,name,ingredient,dose,expiry,times,gap,stock,active)
                        VALUES (?,?,?,?,?,?,?,?,?,1)''', (new_med_id, owner['id'], name.strip(), ingredient.strip(), dose.strip(), expiry.isoformat(), json.dumps(sorted(set(parts))), gap, int(stock)))
                        if supply_on:write_supply(conn,new_med_id,supply_amount,supply_lead)
                st.rerun()


def supply_tables():
    with database(True) as conn:
        execute(conn,'CREATE TABLE IF NOT EXISTS fm_supply (med_id TEXT PRIMARY KEY, remaining INTEGER NOT NULL, updated TEXT NOT NULL, remind_days INTEGER NOT NULL)')
        execute(conn,'CREATE TABLE IF NOT EXISTS fm_supply_read (id TEXT PRIMARY KEY)')


def forecast_supply(remaining, times, current):
    times=sorted(set(times))
    if remaining<=0:return current
    if not times:return None
    today_slots=[datetime.combine(current.date(),datetime.strptime(t,'%H:%M').time(),TZ) for t in times]
    future=[x for x in today_slots if x>current]
    if remaining<=len(future):return future[remaining-1]
    index=remaining-len(future)-1
    day=current.date()+timedelta(days=1+index//len(times))
    return datetime.combine(day,datetime.strptime(times[index%len(times)],'%H:%M').time(),TZ)


def supply_status(m):
    supply_tables()
    result=rows('SELECT * FROM fm_supply WHERE med_id=?',(m['id'],))
    if not result:return None
    r=result[0]
    consumed=rows('SELECT COUNT(*) AS n FROM fm_logs WHERE med_id=? AND taken_at>?',(m['id'],r['updated']))[0]['n']
    remaining=max(0,r['remaining']-consumed)
    times=json.loads(m['times'])
    forecast=forecast_supply(remaining,times,now())
    days=remaining/len(set(times)) if times else None
    low=remaining==0 or (forecast is not None and forecast<=now()+timedelta(days=r['remind_days']))
    alert_id=m['id']+'|'+r['updated']+('|empty' if remaining==0 else '|low')
    return dict(remaining=remaining,days=days,forecast=forecast,low=low,id=alert_id,lead=r['remind_days'])


@st.dialog('약 받아왔어요',width='small')
def refill_dialog(m):
    st.subheader(m['name'])
    current=supply_status(m)
    remaining=current['remaining'] if current else 0
    daily=len(set(json.loads(m['times'])))
    st.write(f'현재 남은 약 {remaining}회분')
    mode=st.segmented_control('새로 받은 양',['며칠분','몇 회분'],default='며칠분' if daily else '몇 회분',key='refill_unit')
    amount=st.number_input('새로 받은 양 ('+('일분' if mode=='며칠분' else '회분')+')',min_value=1,max_value=10000,value=3,step=1,key='refill_amount')
    added=int(amount)*daily if mode=='며칠분' else int(amount)
    if mode=='며칠분' and not daily:
        st.info('복용 시각이 없는 약은 몇 회분으로 입력하세요.');return
    st.write(f'저장 후 남은 약 {remaining+added}회분')
    st.caption('1회분은 한 번 먹을 양이에요. 기존에 남은 양에 더해요.')
    if st.button('받아온 약 추가',type='primary',use_container_width=True):
        supply_tables()
        with database(True) as conn:
            if PG:execute(conn,'SELECT id FROM fm_meds WHERE id=? FOR UPDATE',(m['id'],))
            # Re-read inventory inside the transaction to avoid adding a stale count.
            base=execute(conn,'SELECT * FROM fm_supply WHERE med_id=?',(m['id'],)).fetchone()
            live=0
            if base:
                base=dict(base)
                n=execute(conn,'SELECT COUNT(*) AS n FROM fm_logs WHERE med_id=? AND taken_at>?',(m['id'],base['updated'])).fetchone()
                live=max(0,base['remaining']-n['n'])
            write_supply(conn,m['id'],live+added,current['lead'] if current else 3)
        st.session_state['supply_flash']='받아온 약을 추가했어요'
        st.rerun()


@st.dialog('복용 종료',width='small')
def end_course_dialog(m):
    st.subheader(m['name'])
    st.write('처방받은 복용 기간이 끝났거나 의료진에게 종료 안내를 받은 경우에만 선택하세요.')
    st.caption('약이 떨어졌다는 이유만으로 계속 먹는 약을 중단하지 마세요. 이 버튼은 종료 사실을 기록할 뿐, 중단해도 된다는 판단이 아니에요.')
    confirmed=st.checkbox('처방된 기간이 끝났거나 의료진에게 종료 안내를 받았어요',key='end_course_confirm')
    if st.button('복용 종료 기록',disabled=not confirmed,type='primary',use_container_width=True):
        supply_tables()
        with database(True) as conn:
            execute(conn,'UPDATE fm_meds SET active=0 WHERE id=?',(m['id'],))
            execute(conn,'DELETE FROM fm_supply WHERE med_id=?',(m['id'],))
            execute(conn,'DELETE FROM fm_notices WHERE person_id=? AND medicine=?',(m['person_id'],m['name']))
            execute(conn,'UPDATE fm_contact_tasks SET done=1 WHERE med_id=?',(m['id'],))
        st.session_state['supply_flash']='복용 종료를 기록했어요. 이전 기록은 유지돼요.'
        st.rerun()


def supply_editor(m):
    st.markdown('### 남은 약 · 미리 알림')
    status=supply_status(m)
    times=json.loads(m['times']);daily=len(set(times))
    if status:
        st.write(f"남은 약 약 {status['remaining']}회분"+(f" · 약 {status['days']:.1f}일분" if status['days'] is not None else ''))
        if status['forecast'] and status['remaining']>0:st.write('소진 예상: '+status['forecast'].strftime('%m/%d %H:%M'))
        elif status['remaining']==0:st.write('남은 약이 없어요.')
    mode=st.radio('남은 양 입력 방법',['며칠분','몇 회분'],horizontal=True,key='supply_mode_'+m['id']) if daily else '몇 회분'
    if mode=='며칠분':
        amount=st.number_input('지금 남은 약은 며칠분인가요?',min_value=0,max_value=3650,value=0,step=1,key='supply_days_'+m['id'])
        total=amount*daily
        st.caption(f'등록된 하루 {daily}회 기준 · {total}회분')
    else:
        total=st.number_input('지금 남은 약은 몇 회분인가요?',min_value=0,max_value=100000,value=0,step=1,key='supply_doses_'+m['id'])
    lead=st.number_input('다 떨어지기 며칠 전에 알릴까요?',min_value=1,max_value=30,value=status['lead'] if status else 3,step=1,key='supply_lead_'+m['id'])
    st.caption('1회분은 한 번 먹을 양이에요. 저장 이후 복용 기록 1건마다 1회분을 차감해 추정해요. 남은 양이 다르면 다시 입력하세요.')
    if not daily:st.caption('정해진 복용 시각이 없는 약은 소진일을 예상하지 않아요.')
    checked=st.checkbox('현재 남은 양을 확인했어요',key='supply_checked_'+m['id'])
    if st.button('남은 양 · 알림 저장',disabled=not checked,use_container_width=True,key='supply_save_'+m['id']):
        with database(True) as conn:
            execute(conn,'INSERT INTO fm_supply VALUES (?,?,?,?) ON CONFLICT(med_id) DO UPDATE SET remaining=excluded.remaining,updated=excluded.updated,remind_days=excluded.remind_days',(m['id'],int(total),now().isoformat(),int(lead)))
        st.rerun()


def supply_alerts(person_id):
    supply_tables()
    medicines=rows('SELECT * FROM fm_meds WHERE person_id=? AND active=1 AND id NOT IN (SELECT med_id FROM fm_removed)',(person_id,))
    read_ids={x['id'] for x in rows('SELECT id FROM fm_supply_read')}
    alerts=[]
    for m in medicines:
        r=supply_status(m)
        if r and r['low']:
            r['medicine']=m['name'];r['unread']=r['id'] not in read_ids;alerts.append(r)
    return alerts


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
    st.caption('남은 약과 미리 알림은 약통의 연필 버튼에서 설정하세요.')
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
    by_id={r['id']:r for r in logs}
    selected=st.multiselect('잘못 입력한 복용 기록 선택',list(by_id),format_func=lambda k:f"{by_id[k]['person']} · {by_id[k]['name']} · {by_id[k]['slot']}")
    st.warning('실제로 복용하지 않았는데 잘못 체크한 기록만 취소하세요. 실제 복용 기록을 지우면 중복 복용 방지에 영향을 줍니다.')
    checked=st.checkbox('약을 먹지 않았는데 ‘먹었어요’로 잘못 입력한 기록입니다')
    if st.button('잘못 입력한 기록 삭제',disabled=not checked or not selected,use_container_width=True):
        with database(True) as conn:
            for record_id in selected:execute(conn,'DELETE FROM fm_logs WHERE id=?',(record_id,))
        st.rerun()


@st.dialog('✏️ 약 정보 수정', width='small')
def edit_medicine(med_id):
    records=rows('SELECT * FROM fm_meds WHERE id=?',(med_id,))
    if not records:st.info('약을 찾을 수 없습니다.');return
    m=records[0]
    existing_supply=supply_status(m)
    with st.form('edit_med_'+med_id):
        name=st.text_input('약 이름',value=m['name'],max_chars=100)
        ingredient=st.text_input('주성분',value=m['ingredient'],max_chars=100)
        dose=st.text_input('1회 복용량',value=m['dose'],max_chars=100)
        expiry=st.date_input('유효기간',value=date.fromisoformat(m['expiry']))
        times=','.join(st.multiselect('복용 시각',[f'{h:02d}:{minute:02d}' for h in range(24) for minute in range(60)],default=json.loads(m['times'])))
        gap=st.number_input('최소 복용 간격 (시간)',min_value=0.0,max_value=168.0,value=float(m['gap']),step=0.5)
        stock=st.checkbox('상비약',value=bool(m['stock']))
        active=st.checkbox('복용 일정 사용',value=bool(m['active']))
        supply_on,supply_amount,supply_lead=supply_fields('edit_supply_'+med_id,existing_supply)
        submit=st.form_submit_button('변경 저장',use_container_width=True)
    if submit:
        values=[v.strip() for v in times.split(',') if v.strip()]
        if not name.strip() or not dose.strip():st.warning('약 이름과 복용량을 입력하세요.');return
        if (active and not values) or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',v) for v in values):
            st.warning('복용 시각을 08:00, 20:00 형식으로 입력하세요.');return
        with database(True) as conn:
            execute(conn,'UPDATE fm_meds SET name=?,ingredient=?,dose=?,expiry=?,times=?,gap=?,stock=?,active=? WHERE id=?',(name.strip(),ingredient.strip(),dose.strip(),expiry.isoformat(),json.dumps(sorted(set(values))),gap,int(stock),int(active),med_id))
            if supply_on:write_supply(conn,med_id,supply_amount,supply_lead)
            else:execute(conn,'DELETE FROM fm_supply WHERE med_id=?',(med_id,))
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


CHARACTER_IMAGES = ['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']

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
    [data-testid="stSegmentedControl"] button,[data-testid="stNumberInput"] input,[data-testid="stNumberInput"] button{background:#fff!important;color:#163047!important;-webkit-text-fill-color:#163047!important;}
    [data-testid="stSegmentedControl"] button[aria-pressed="true"]{background:#dceee7!important;color:#163047!important;}
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
    if not st.session_state.get('launch_seen_onyak_v1', False):
        st.session_state.launch_seen_onyak_v1 = True
        st.html('<style> .launch-screen{position:fixed;inset:0;background:#176f70;z-index:999990; display:flex;align-items:center;justify-content:center; animation:launch-out 4s ease-out forwards;pointer-events:none;} .launch-screen svg{width:96px;height:96px;display:block;flex:none;overflow:visible;} @keyframes launch-out{0%,75%{opacity:1;visibility:visible;}100%{opacity:0;visibility:hidden;}} @media(prefers-reduced-motion:reduce){.launch-screen{animation-duration:.15s;}} .launch-content{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:26px}.launch-progress{width:44px;height:44px;border-radius:50%;border:4px solid rgba(255,255,255,.25);border-top-color:#fff;animation:launch-spin .85s linear infinite;box-sizing:border-box}.launch-status{color:#fff!important;-webkit-text-fill-color:#fff!important;font:500 15px sans-serif;letter-spacing:.2px;text-align:center}@keyframes launch-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.launch-progress{animation:none;border-color:#fff}}</style><div class="launch-screen" aria-label="앱 시작"><div class="launch-content"><img src="data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNjgiIGhlaWdodD0iNjgiIHZpZXdCb3g9IjAgMCA2OCA2OCIgcm9sZT0iaW1nIiBhcmlhLWxhYmVsPSLqsIDsobEg7JW9IOyngO2CtOydtCDroZzqs6AiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+ICA8cGF0aCBkPSJNMTggMzJMMzQgMThMNTAgMzIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSI0IiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz4gPHBhdGggZD0iTTIzIDMxVjQ4QzIzIDUwIDI1IDUyIDI3IDUySDQxQzQzIDUyIDQ1IDUwIDQ1IDQ4VjMxIiBmaWxsPSJub25lIiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMy41IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz4gPHJlY3QgeD0iMjkiIHk9IjMwIiB3aWR0aD0iMTAiIGhlaWdodD0iMjAiIHJ4PSI1IiB0cmFuc2Zvcm09InJvdGF0ZSgzNSAzNCA0MCkiIGZpbGw9IiNmZmYiLz4gPHBhdGggZD0iTTMwIDM3TDM4IDQzIiBzdHJva2U9IiMxNzZmNzAiIHN0cm9rZS13aWR0aD0iMiIvPiA8L3N2Zz4=" alt="집과 알약 로고" style="width:96px;height:96px;display:block;max-width:none;object-fit:contain" /><div class="launch-name" style="color:#ffffff;font-size:32px;font-weight:800;letter-spacing:2px;line-height:1.3;text-align:center">온약<span style="display:block;font-size:12px;letter-spacing:5px;margin-top:8px;font-weight:600;color:#ffffff">ONYAK</span></div><div class="launch-progress" role="status" aria-label="시작 화면 표시 중"></div><div class="launch-status">앱을 시작하고 있어요</div></div></div>')
    elder = st.session_state.get('elder_mode',False)
    if elder:
        st.markdown('<style>.block-container button{height:auto!important;min-height:70px!important;white-space:normal!important;}.block-container button p,.block-container [data-testid="stMarkdownContainer"] p{font-size:26px!important;line-height:1.6!important;overflow-wrap:anywhere!important;}.block-container [data-testid="stHorizontalBlock"]{flex-wrap:wrap!important;}.block-container .badge{font-size:24px!important;white-space:normal!important;height:auto!important;}</style>',unsafe_allow_html=True)
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
        if reason not in ['깜빡했어요', '사정상 못 먹었어요','약이 없어요','몸이 불편해요','먹었는지 모르겠어요','다른 이유','미복용 확인']:
            return False, '사유를 선택하세요.'
        execute(conn, '''INSERT INTO fm_missed (id,med_id,day,slot,reason,note,recorded_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(med_id,day,slot)
            DO UPDATE SET reason=excluded.reason,note=excluded.note,recorded_at=excluded.recorded_at''',
            (str(uuid.uuid4()), med_id, day, slot, reason, note[:300], now().isoformat()))
    return True, '못 먹은 사유를 기록했습니다. 복용 완료로 처리하지 않습니다.'


def missed_history():
    st.subheader('못 먹은 기록')
    records = rows('''SELECT x.*,m.name AS medicine,p.name AS person FROM fm_missed x
        JOIN fm_meds m ON m.id=x.med_id JOIN fm_people p ON p.id=m.person_id
        ORDER BY x.day DESC,x.slot DESC LIMIT 100''')
    if not records:
        st.info('저장된 사유가 없습니다.')
    for r in records:
        taken = rows('SELECT id FROM fm_logs WHERE med_id=? AND day=? AND slot=?', (r['med_id'],r['day'],r['slot']))
        st.write(f"{r['day']} {r['slot']} · {r['person']} · {r['medicine']}")
        follow=rows('SELECT guidance FROM fm_followup WHERE med_id=? AND day=? AND slot=?',(r['med_id'],r['day'],r['slot']))
        state='이후 복용 기록 있음' if taken else '미복용 · 대처 안내 확인' if follow and follow[0]['guidance'] else '미복용 사유 저장'
        st.caption(r['reason']+' · '+state)
        if st.button('이 사유 기록 삭제', key='delete_missed_'+r['id']):
            with database(True) as conn:
                execute(conn, 'DELETE FROM fm_missed WHERE id=?', (r['id'],))
            st.rerun()


def compact_page(items,key,size=4):
    return items


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
    st.markdown("""<style>
    [role="dialog"]{background:#F7FAF8!important;color:#173C35!important;border-radius:26px!important;}
    [role="dialog"] h2,[role="dialog"] h3{color:#173C35!important;}
    [role="dialog"] [data-testid="stNumberInput"] input,
    [role="dialog"] [data-testid="stNumberInput"] button,
    [role="dialog"] [data-baseweb="input"],
    [role="dialog"] [data-baseweb="base-input"]{background:#FFFFFF!important;color:#173C35!important;-webkit-text-fill-color:#173C35!important;}
    [role="dialog"] [data-testid="stFormSubmitButton"] button{background:#DCEFE5!important;color:#173C35!important;border:1px solid #B0D6C3!important;border-radius:15px!important;}
    [role="dialog"] [data-testid="stFormSubmitButton"] button *{color:#173C35!important;-webkit-text-fill-color:#173C35!important;}
    [data-testid="stVerticalBlockBorderWrapper"]{border-color:#DAE7E0!important;border-radius:20px!important;}
    [data-testid="stAppViewContainer"]{background:#F7FAF8!important;}
    .st-key-medicine_tiles button p{font-size:17px!important;font-weight:750!important;}
    </style>""",unsafe_allow_html=True)
    st.markdown("""<style>
    [data-testid="stVerticalBlockBorderWrapper"],[data-testid="stForm"],
    [data-testid="stTextInput"] input,[data-testid="stNumberInput"] input,
    [data-testid="stDateInput"] input,[data-baseweb="select"]>div {
    border:1px solid #CFDDD6!important;border-radius:14px!important;box-shadow:none!important;}
    [data-testid="stDateInput"] input,[data-testid="stDateInput"] [data-baseweb="input"] {background:#FFFFFF!important;color:#173C35!important;-webkit-text-fill-color:#173C35!important;}
    </style>""",unsafe_allow_html=True)
    page=st.session_state.get('page','오늘 약 먹기')
    if page=='홈':page='오늘 약 먹기'
    active = '오늘' if page in ['오늘 약 먹기','놓친 약 확인'] else '약통' if page=='약 보관함' else '기록' if page=='기록' else '가족'
    with st.container(key='bottom_nav'):
        cols=st.columns(3)
        for col,icon,label,target in zip(cols,[':material/medication:',':material/medical_services:',':material/history:'],['오늘','약통','기록'],['오늘 약 먹기','약 보관함','기록']):
            if col.button(label if elder else '',icon=icon,help=label,key='nav_'+label,type='primary' if active==label else 'secondary',use_container_width=True):
                st.session_state.page=target;st.rerun()
    if page=='오늘 약 먹기':
        st.subheader('오늘의 복용')
        people=rows('SELECT * FROM fm_people WHERE id NOT IN (SELECT person_id FROM fm_hidden_people) ORDER BY name')
        if not people:
            st.info('약통 → 가족 관리에서 가족을 등록한 뒤 약을 등록하세요.')
        else:
            dashboard(people[0]['id'],elder)
    elif page=='미복용 목록':
        missed_overview()
    elif page=='놓친 약 확인':
        if st.button('← 오늘',key='back_today'):
            st.session_state.page='오늘 약 먹기';st.rerun()
        missed_flow(elder)
    elif page=='약 보관함':
        st.subheader('약통')
        if st.session_state.get('supply_flash'):st.success(st.session_state.pop('supply_flash'))
        st.markdown('<style>[data-testid="stVerticalBlockBorderWrapper"] h3{font-size:25px!important;font-weight:850!important;color:#153d32!important;line-height:1.35!important;}</style>',unsafe_allow_html=True)
        if st.button('가족 등록 · 삭제',icon=':material/group_add:',key='family_management'):
            registration('family')
        if st.button('＋ 약 등록',use_container_width=True):registration('medicine')
        meds=rows('SELECT m.*,p.name AS person FROM fm_meds m JOIN fm_people p ON p.id=m.person_id WHERE NOT EXISTS (SELECT 1 FROM fm_removed r WHERE r.med_id=m.id) ORDER BY m.expiry')
        if not meds:st.info('등록한 약이 없습니다.')
        groups={}
        for med in meds:groups.setdefault((med['name'].strip(),med['ingredient'].strip()),[]).append(med)
        for group_index,group in enumerate(groups.values()):
            with st.container(border=True):
                st.markdown('### '+safe(group[0]['name']))
                st.write(' · '.join(dict.fromkeys(x['person'] for x in group)))
                m=st.selectbox('家族'.replace('家族','가족별 약 정보'),group,format_func=lambda x:x['person'],key='cab_owner_'+str(group_index)) if len(group)>1 else group[0]
                expired=m['expiry']<now().date().isoformat()
                photo,info,edit,delete=st.columns([2,5,1,1],gap='small')
                saved_photo=rows('SELECT image_b64 FROM fm_photos WHERE med_id=?',(m['id'],))
                if saved_photo:
                    import base64
                    photo.image(base64.b64decode(saved_photo[0]['image_b64']),use_container_width=True)
                if photo.button('',icon=':material/photo_camera:',help='약 사진 촬영·변경',key='photo_'+m['id']):medicine_photo(m['id'],m['name'])
                info.write(f"{m['person']} · {m['dose']}")
                info.caption(('기한 지남 · ' if expired else '유효기간 · ')+m['expiry'])
                supply=supply_status(m) if m['active'] else None
                if supply:
                    info.markdown(f"**남은 약 {supply['remaining']}회분**"+(f" · 약 {supply['days']:.1f}일분" if supply['days'] is not None else ''))
                    if supply['remaining']>0 and supply['forecast']:info.caption('소진 예상 '+supply['forecast'].strftime('%m/%d'))
                    elif supply['remaining']==0:info.markdown('**남은 약이 없어요**')
                else:info.caption('남은 양 미등록' if m['active'] else '복용 종료 · 이전 기록 유지')
                if m['active'] and supply and supply['low']:
                    if st.button('약 받아왔어요',icon=':material/add:',key='refill_'+m['id'],use_container_width=True):refill_dialog(m)
                    if st.button('처방에 따라 복용 종료',icon=':material/task_alt:',key='end_course_'+m['id'],use_container_width=True):end_course_dialog(m)

                if edit.button('',icon=':material/edit:',help='약 정보 수정',key='edit_'+m['id']):edit_medicine(m['id'])
                if delete.button('',icon=':material/delete:',help='약 삭제',key='delete_'+m['id']):delete_medicine(m['id'])
    elif page=='기록':
        st.subheader('복용 현황')
        intake_table()
        if st.button('오기록 취소',icon=':material/history:',use_container_width=True):history_dialog()
        if st.button('못 먹은 기록',icon=':material/history:',use_container_width=True):missed_records_dialog()
    else:
        st.subheader('가족')
        if st.button('＋ 가족 등록',use_container_width=True):registration('family')
        people=rows('SELECT * FROM fm_people WHERE id NOT IN (SELECT person_id FROM fm_hidden_people) ORDER BY name')
        if st.session_state.pop('family_removed_notice',False):
            st.toast('가족을 삭제했습니다.',icon=':material/check_circle:')
        for person in compact_page(people,'family_list_page',4):
            with st.container(border=True):
                info,action=st.columns([4,1],gap='small')
                info.markdown('**'+safe(person['name'])+'**')
                if action.button('',icon=':material/delete:',help=person['name']+' 삭제',key='family_page_delete_'+person['id']):
                    remove_family_dialog(person['id'],person['name'])
        if st.button('📷 초대 QR',use_container_width=True):share_dialog()


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


def touch_choice(label, options, key):
    """Large full-width native buttons; no tiny radio targets."""
    if st.session_state.get(key) not in options:
        st.session_state[key]=options[0]
    with st.container(key='touch_'+key):
        for index, option in enumerate(options):
            active=st.session_state[key]==option
            if st.button(('✓  ' if active else '')+option,
                         key=key+'_pick_'+str(index),
                         type='primary' if active else 'secondary',
                         use_container_width=True):
                st.session_state[key]=option
                st.rerun()
    return st.session_state[key]


@st.dialog('빠뜨린 복용 기록',width='small')
def late_intake_dialog(item):
    m=item['m']
    st.subheader(m['name'])
    st.write(f"{m['person']} · {item['slot']}에 먹을 약")
    actual_day=st.date_input('실제로 먹은 날짜',value=date.fromisoformat(item['day']),max_value=now().date(),key='late_day')
    actual_time=st.time_input('실제로 먹은 시각',value=datetime.strptime(item['slot'],'%H:%M').time(),key='late_time')
    st.caption('지금 새로 먹는 버튼이 아니라, 이미 먹은 사실을 기록하는 버튼이에요.')
    confirmed=st.checkbox('선택한 약을 실제로 먹었어요',key='late_confirm')
    if st.button('복용 기록 저장',disabled=not confirmed,type='primary',use_container_width=True):
        actual=datetime.combine(actual_day,actual_time,TZ)
        if actual>now():st.error('미래 시각은 기록할 수 없어요.');return
        with database(True) as conn:
            if PG:execute(conn,'SELECT id FROM fm_people WHERE id=? FOR UPDATE',(m['person_id'],))
            exists=execute(conn,'SELECT id FROM fm_logs WHERE med_id=? AND day=? AND slot=?',(m['id'],item['day'],item['slot'])).fetchone()
            if exists:st.info('이미 복용 기록이 있어요.');return
            execute(conn,'INSERT INTO fm_logs(id,med_id,day,slot,taken_at) VALUES (?,?,?,?,?)',(str(uuid.uuid4()),m['id'],item['day'],item['slot'],actual.isoformat()))
            execute(conn,'DELETE FROM fm_missed WHERE med_id=? AND day=? AND slot=?',(m['id'],item['day'],item['slot']))
            execute(conn,'DELETE FROM fm_followup WHERE med_id=? AND day=? AND slot=?',(m['id'],item['day'],item['slot']))
            execute(conn,'UPDATE fm_contact_tasks SET done=1 WHERE med_id=? AND day=? AND slot=?',(m['id'],item['day'],item['slot']))
            execute(conn,'DELETE FROM fm_notices WHERE id=?',(f"{m['person_id']}|{item['day']}|{m['id']}|{item['slot']}",))
        st.session_state['dose_toast']=m['name']+' 복용 기록을 저장했어요'
        st.rerun()


def missed_overview():
    if st.button('오늘로 돌아가기',icon=':material/arrow_back:',key='overview_back'):
        st.session_state.page='오늘 약 먹기';st.rerun()
    st.subheader('복용 기록 확인')
    items=unresolved_slots()
    if not items:st.info('확인할 약이 없습니다.')
    for i,item in enumerate(items):
        m=item['m']
        with st.container(border=True):
            st.subheader(m['name'])
            st.write(f"{m['person']} · {item['day']} · {item['slot']}")
            if st.button('먹었는데 기록을 못 했어요',key='late_overview_'+str(i),use_container_width=True,icon=':material/check_circle:'):
                late_intake_dialog(item)
            if st.button('못 먹었어요 · 대처 방법 보기',key='miss_overview_'+str(i),use_container_width=True,icon=':material/pill_off:'):
                for key in list(st.session_state):
                    if key.startswith('mq_'):del st.session_state[key]
                st.session_state['mq_target']=item;st.session_state['mq_step']='reason'
                st.session_state.page='놓친 약 확인';st.rerun()


def missed_flow(elder):
    # One question per render. Every answer immediately advances.
    def advance(step):
        current=st.session_state.get('mq_step','select')
        if current!=step and step!='done':
            st.session_state.setdefault('mq_back_stack',[]).append(current)
        st.session_state['mq_step']=step
        st.rerun()
    def finish(result='확인 대기', detail='', source='', close=False):
        target=st.session_state['mq_target']
        m=target['m']; day=target['day']; slot=target['slot']
        if st.session_state.get('mq_reason')!='먹었는지 모르겠어요':
            ok,msg=save_missed(m['id'],day,slot,st.session_state.get('mq_reason','깜빡했어요'),'')
            if not ok:st.error(msg);return
        ok,msg=save_followup(m['id'],day,slot,result,detail,source)
        if not ok:st.error(msg);return
        if result!='확인 대기':
            with database(True) as conn:
                execute(conn,'UPDATE fm_contact_tasks SET done=1 WHERE med_id=? AND day=? AND slot=?',(m['id'],day,slot))
        if close:
            for key in list(st.session_state):
                if key.startswith('mq_'):del st.session_state[key]
            st.session_state.page='오늘 약 먹기'
            st.session_state['dose_toast']='미복용 안내를 확인했어요'
            st.rerun()
            return
        advance('done')
    step=st.session_state.get('mq_step','select')
    if step=='done':
        st.subheader('저장했어요')

        if st.button('확인',type='primary',use_container_width=True,key='mq_done'):
            for k in list(st.session_state):
                if k.startswith('mq_'):del st.session_state[k]
            st.session_state.page='오늘 약 먹기';st.rerun()
        return
    if step not in ('select','done') and st.session_state.get('mq_target'):
        target=st.session_state['mq_target']
        st.subheader(target['m']['name'])
        st.write(f"{target['m']['person']} · {target['day']} · {target['slot']}에 먹을 약")
    if not st.session_state.get('mq_back_stack'):
        if st.button('오늘로 돌아가기',icon=':material/arrow_back:',key='mq_home_back'):
            for key in list(st.session_state):
                if key.startswith('mq_'):del st.session_state[key]
            st.session_state.page='오늘 약 먹기';st.rerun()
    if step!='select' and st.session_state.get('mq_back_stack'):
        if st.button('이전 단계',icon=':material/arrow_back:',key='mq_previous'):
            st.session_state['mq_step']=st.session_state['mq_back_stack'].pop()
            st.rerun()
    if step=='select':
        options=unresolved_slots()
        if not options:
            st.write('확인할 약이 없습니다.')
            if st.button('확인',use_container_width=True):
                st.session_state.page='오늘 약 먹기';st.rerun()
            return
        if len(options)==1:
            st.session_state['mq_target']=options[0];advance('reason')
        st.subheader('어떤 약을 못 먹었나요?')
        for i,x in enumerate(options):
            if st.button(f"{x['m']['person']} · {x['m']['name']} · {x['slot']}",key='mq_select_'+str(i),use_container_width=True):
                st.session_state['mq_target']=x;advance('reason')
        return
    if step=='reason':
        st.subheader('이 약을 못 먹은 이유는 무엇인가요?')
        choices=[('먹는 걸 깜빡했어요','깜빡했어요','miss_count'),
                 ('약이 다 떨어졌어요','약이 없어요','out_of_stock'),
                 ('몸이 불편해서 못 먹었어요','몸이 불편해요','current_symptom'),
                 ('먹었는지 기억이 안 나요','먹었는지 모르겠어요','uncertain')]
        for i,(label,reason,dest) in enumerate(choices):
            if st.button(label,key='mq_reason_'+str(i),use_container_width=True):
                for field in ('mq_count','mq_group','mq_current_symptom','mq_leaflet','mq_saved_detail','mq_saved_source'):
                    st.session_state.pop(field,None)
                st.session_state['mq_reason']=reason
                advance(dest)
        return
    if step=='out_of_stock':
        st.subheader('약을 다시 준비해 주세요')
        st.write('1. 약 봉투와 보관함에 본인 약이 남아 있는지 확인하세요.')
        st.write('2. 남은 약이 없다면 약 이름·함량과 마지막 복용 시각을 준비해 처방·조제한 곳에 보충 방법을 확인하세요.')
        st.write('3. 다른 사람의 처방약으로 대신 먹거나, 새 약을 구한 뒤 두 회분을 한꺼번에 먹지 마세요.')
        st.caption('인슐린·발작 예방약·파킨슨병 약·항응고제·항암제·이식 후 약은 지체 없이 담당 의료진에게 확인하세요.')
        st.write('다음에는 약통 → 연필 → 남은 약 · 미리 알림에서 남은 양을 등록하세요.')
        if st.button('확인하고 나가기',type='primary',use_container_width=True):
            finish('확인 대기','약 소진. 약 보충 및 재개 시각 확인 필요.','사용자 확인',close=True)
        return
    if step=='uncertain':
        st.subheader('복용 기록이나 남은 약을 확인해 보세요')
        st.write('약통·포장에 약이 남아 있는지 확인하고, 함께 있던 가족에게 물어보세요. 기록만으로 실제 복용을 확정할 수는 없어요. 확인 없이 한 번 더 먹지는 마세요.')
        if st.button('앱의 복용 기록 열기',use_container_width=True):history_dialog()
        if st.button('안 먹은 걸 확인했어요',use_container_width=True):
            st.session_state['mq_reason']='미복용 확인';advance('miss_count')
        if st.button('확인이 안 돼요 · 확인 필요로 남기기',use_container_width=True):
            finish('확인 대기','실제 복용 여부 불명확. 추가 복용 전 제품별 지침 확인 필요.','사용자 확인')
        return
    if step in ('home_action','safety_check','dose_certainty','unwell','symptom_help'):
        # Compatibility for a session already open on a screen from the previous version.
        advance('current_symptom' if st.session_state.get('mq_reason')=='몸이 불편해요' else 'miss_count')
        return
    if step=='urgent_help':
        st.subheader('지금은 응급 도움이 먼저예요')
        st.write('1. 119에 전화하고 주변 사람을 부르세요.')
        st.write('2. 직접 운전하지 마세요. 약 포장과 증상 시작 시각을 준비하세요.')
        st.write('3. 의식이 흐리거나 삼키기 어려우면 약·물·음식을 먹이지 마세요.')
        st.link_button('119 전화','tel:119',use_container_width=True)
        return
    if step=='miss_count':
        st.subheader('이 약을 몇 번 연속 못 먹었나요?')
        for i,label in enumerate(['이번 한 번만 못 먹었어요','앞선 복용 시간에도 못 먹었어요','몇 번인지 기억이 안 나요']):
            if st.button(label,key='mq_count_'+str(i),use_container_width=True):
                st.session_state['mq_count']=['이번 1회','2회 이상','정확히 모르겠어요'][i];advance('medicine_group')
        return
    if step=='medicine_group':
        st.subheader('이 약은 무엇 때문에 사용하나요?')
        choices=[('당뇨 · 인슐린 주사','인슐린'),('뇌전증 · 발작 예방','뇌전증·경련 약'),
                 ('파킨슨병 치료','파킨슨병 약'),('혈전 예방 · 항응고제','항응고제'),
                 ('피임','피임약'),('항암 · 이식 후 치료 · 주 1회 복용','주 1회 약·항암제·이식 후 약'),
                 ('위 항목에 해당하지 않아요','그 외 약'),('어떤 약인지 잘 모르겠어요','모르겠어요')]
        for i,(label,group) in enumerate(choices):
            if st.button(label,key='mq_group_'+str(i),use_container_width=True):
                st.session_state['mq_group']=group;advance('leaflet_check' if st.session_state.get('mq_reason')=='깜빡했어요' else 'tailored_action')
        return
    if step=='current_symptom':
        st.subheader('어디가 불편해서 못 먹었나요?')
        choices=['어지러워요','메스껍거나 토했어요','약을 삼키기 어려워요','다른 증상이 있어요','숨쉬기 어렵거나 의식이 흐려요','갑자기 말이 어눌하거나 한쪽 팔다리에 힘이 없어요']
        for i,label in enumerate(choices):
            if st.button(label,key='mq_current_'+str(i),use_container_width=True):
                if i>=4:advance('urgent_help')
                else:
                    st.session_state['mq_current_symptom']=['어지러움','메스꺼움·구토','삼키기 어려움','다른 증상'][i]
                    advance('miss_count')
        return
    if step=='leaflet_check' and st.session_state.get('mq_reason')!='깜빡했어요':
        advance('tailored_action');return
    if step=='leaflet_check':
        st.subheader('약 설명서에 ‘먹는 걸 잊었을 때’ 안내가 있나요?')
        st.write('이 약에 대해 의사·약사에게 따로 받은 안내도 포함해요.')
        for i,label in enumerate(['안내가 있어요','없거나 찾지 못했어요']):
            if st.button(label,key='mq_leaflet_'+str(i),use_container_width=True):
                st.session_state['mq_leaflet']=i==0;advance('tailored_action')
        return
    if step=='tailored_action':
        target=st.session_state['mq_target'];med=target['m']
        group=st.session_state.get('mq_group','모르겠어요')
        symptom=st.session_state.get('mq_current_symptom','없어요')
        count=st.session_state.get('mq_count','정확히 모르겠어요')
        reason=st.session_state.get('mq_reason','깜빡했어요')
        has_guide=st.session_state.get('mq_leaflet',False)
        st.write('못 먹은 횟수: '+count)
        st.markdown('### 지금 할 일')
        actions=[]
        if symptom=='어지러움':
            actions += ['어지러움이 가라앉을 때까지 누워 쉬고, 일어날 때 천천히 움직이세요. 운전·사다리·기계 사용은 피하세요.']
        elif symptom=='삼키기 어려움':
            actions += ['억지로 삼키거나 약을 임의로 부수지 마세요. 물도 삼키기 어렵다면 신속히 진료를 받으세요.']
        elif symptom=='메스꺼움·구토':
            actions += ['구토했다면 약을 먹은 시각과 토한 시각을 적으세요. 흡수된 양을 알 수 없어 임의로 다시 복용하지 마세요.']
        elif symptom=='다른 증상':
            actions += ['증상 시작 시각과 변화, 마지막 복용 시각을 적으세요. 심해지거나 계속되면 진료를 받으세요.']
        if reason=='약이 없어요':
            actions += ['약 봉투와 보관함에서 본인 약의 남은 수량을 확인하세요. 다른 사람의 처방약이나 임의의 대체약은 먹지 마세요.']
        if group=='인슐린':
            actions += ['혈당계가 있으면 혈당을 확인하고, 앞으로 24시간 평소보다 자주 측정하세요. 본인의 당뇨 관리 지침을 따르세요. 누락분을 두 배로 주사하지 마세요.']
        if group=='뇌전증·경련 약':
            actions += ['누락 후에는 운전·수영 등 발작 시 위험한 활동을 피하세요.']
        if group=='파킨슨병 약':
            actions += ['넘어지지 않도록 이동을 줄이고 가족에게 도움을 요청하세요. 지연 자체가 위험할 수 있으므로 담당 진료팀에 지금 연락하세요.']
        if reason=='몸이 불편해요':
            actions += ['이 약'+'을 먹은 뒤 증상이 생겼다면 다음 복용 전에 제품명·함량·증상 시작 시각을 준비해 약사나 처방 의료진에게 확인하세요. 약에 따라 중단 위험이 달라 앱이 복용·중단을 결정하지 않아요.']
        elif has_guide:
            actions += ['설명서의 제품명·함량·제형이 내 약과 같은지 확인하고, 놓친 횟수와 경과 시간에 해당하는 지침을 따르세요.']
        else:
            actions += ['약 포장에서 제품명·함량·제형을 확인하고, 설명서의 ‘복용을 잊었을 때’를 찾아보세요. 이 정보 없이 앱은 지금 복용할지 건너뛸지 정하지 않습니다.']
        if count!='이번 1회' or group!='그 외 약':
            actions += ['놓친 횟수·예정 시각·마지막 실제 복용 시각과 약 포장을 준비해 담당 약사 또는 진료팀에 지체 없이 확인하세요. 확인 없이 계속 기다리거나 다음 회차까지 무조건 중단하지 마세요.']
        elif not has_guide:
            actions += ['설명서에도 지침이 없으면 다음 복용 결정 전에 약사에게 제품명·놓친 시각·다음 예정 시각을 알려 확인하세요.']
        actions += ['누락분을 보충하려고 두 회분을 한꺼번에 먹지 마세요. 의료진의 별도 지시가 있다면 그 지시를 따르세요.']
        render_action_steps('\n'.join(actions))
        if st.button('확인하고 나가기',use_container_width=True,key='mq_exit_advice'):
            finish('확인 대기','\n'.join(actions),'NHS SPS · 일반 안전 안내 확인; 복용 결정 완료 아님',close=True)
        st.link_button('안내 출처 · NHS SPS','https://www.sps.nhs.uk/articles/advising-on-missed-or-delayed-doses-of-medicines/',use_container_width=True)
        if symptom=='어지러움':st.link_button('어지러움 대처 · NHS','https://www.nhs.uk/conditions/dizziness/',use_container_width=True)
        if has_guide:
            if st.button('확인한 복용법 기록',type='primary',use_container_width=True):advance('instruction')
        else:
            if st.button('추가 확인할 내용 남기기',type='primary',use_container_width=True):advance('contact')
        return
    if step=='know':
        st.subheader('놓쳤을 때 복용법을 아나요?')
        if st.button('알고 있어요',use_container_width=True):advance('instruction')
        if st.button('아직 모르겠어요',use_container_width=True):advance('help')
        return
    if step=='help':
        st.subheader('약 봉투에 안내가 있나요?')
        if st.button('있어요',use_container_width=True):advance('instruction')
        if st.button('없어요 · 이해하기 어려워요',use_container_width=True):advance('contact')
        return
    if step=='contact':
        st.subheader('이 약을 언제 먹어야 할지 확인이 필요해요')
        st.write('확인 전 임의로 추가 복용하지 마세요.')
        phone=st.text_input('약국·병원 전화번호',placeholder='예시) 02-123-4567',key='mq_phone')
        cleaned=re.sub(r'[\s()-]','',phone)
        if cleaned and re.fullmatch(r'\+?[0-9]{7,15}',cleaned):
            st.link_button('전화하기','tel:'+cleaned,use_container_width=True)
        if st.button('답변을 받았어요',use_container_width=True):advance('instruction')
        if st.button('약국에 물어볼게요',use_container_width=True):
            target=st.session_state['mq_target'];m=target['m']
            with database(True) as conn:
                task_id=f"{m['id']}|{target['day']}|{target['slot']}"
                execute(conn,'INSERT INTO fm_contact_tasks VALUES (?,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET done=0',(task_id,m['person_id'],m['id'],target['day'],target['slot'],m['name']))
            finish()
        return
    if step=='instruction':
        st.subheader('설명서나 의료진의 안내를 적어주세요')
        detail=st.text_input('안내 내용',value=st.session_state.get('mq_saved_detail',''),key='mq_detail')
        source=st.text_input('확인한 곳',value=st.session_state.get('mq_saved_source',''),placeholder='예시) 약 봉투 / 약사',key='mq_source')
        if st.button('다음',type='primary',use_container_width=True):
            if not detail.strip() or not source.strip():st.toast('안내 내용과 확인한 곳을 입력하세요.')
            else:
                st.session_state['mq_saved_detail']=detail.strip()
                st.session_state['mq_saved_source']=source.strip()
                advance('result')
        return
    if step=='result':
        st.subheader('확인한 안내')
        st.write(st.session_state.get('mq_saved_detail',''))
        st.caption('출처: '+st.session_state.get('mq_saved_source',''))
        if st.button('이 안내 저장',type='primary',use_container_width=True):
            finish('확인 대기',st.session_state.get('mq_saved_detail',''),st.session_state.get('mq_saved_source',''))
        return


@st.dialog('못 먹은 기록',width='small')
def missed_records_dialog():
    followup_history()


def render_action_steps(text):
    parts=[x.strip() for x in re.split(r'\n+',text or '') if x.strip()]
    rules=[
      (('운전','발작'), '피할 활동', '운전과 수영은 피하세요.', '발작이 생기면 위험한 활동이에요.'),
      (('두 회분',), '추가 복용', '놓친 약까지 두 번 먹을 양을 한꺼번에 먹지 마세요.', '의사나 약사에게 따로 안내받았다면 그 안내를 따르세요.'),
      (('설명서의 제품명',), '설명서 확인', '설명서가 지금 먹는 약의 것인지 확인하세요.', '약 이름·함량·약 형태를 확인한 뒤, 놓친 횟수와 시간에 맞는 안내를 읽으세요.'),
      (('놓친 횟수','약 포장','마지막'), '지금 확인', '약사나 담당 진료팀에 복용 방법을 확인하세요.', '약 포장을 준비하고, 몇 번 놓쳤는지와 마지막으로 먹은 시각을 알려주세요.'),
      (('어지러움이 가라앉을',), '어지러울 때', '어지러움이 가라앉을 때까지 누워 쉬세요.', '일어날 때는 천천히 움직이고, 운전·사다리·기계 사용은 피하세요.'),
      (('억지로 삼키',), '삼키기 어려울 때', '약을 억지로 삼키거나 임의로 부수지 마세요.', '물도 삼키기 어렵다면 신속히 진료받으세요.'),
      (('구토했다면',), '구토했을 때', '약을 먹은 시각과 토한 시각을 적어두세요.', '약이 얼마나 흡수됐는지 알 수 없으므로 임의로 다시 먹지 마세요.'),
    ]
    seen=set()
    for raw in parts:
        raw=re.sub(r'^\d+[.)]\s*','',raw)
        match=next((r for r in rules if all(k in raw for k in r[0])),None)
        if match:
            _,label,action,note=match
            if label in seen:continue
            seen.add(label)
            st.markdown('**'+label+'**')
            st.markdown('- **'+action+'**')
            st.markdown('- '+note)
            if label=='지금 확인':st.markdown('- 확인하지 않은 채 계속 기다리거나, 약을 임의로 중단하지 마세요.')
        else:
            # Unmapped saved instructions are preserved verbatim, but always as bullets.
            for sentence in re.split(r'(?<=[.!?])\s+',raw):
                if sentence.strip():st.markdown('- '+sentence.strip())


def followup_history():
    labels={'확인 대기':'확인 중','안내 확인 후 건너뜀':'안내 확인 후 건너뜀','안내 확인 후 복용 보고':'안내 확인 후 복용했다고 보고','기록 확인 완료':'기록·가족 확인 완료'}
    records=rows('SELECT f.*,m.name AS medicine,p.name AS person FROM fm_followup f JOIN fm_meds m ON m.id=f.med_id JOIN fm_people p ON p.id=m.person_id ORDER BY f.recorded_at DESC LIMIT 100')
    if not records:st.info('후속 조치 기록이 없습니다.')
    for r in records:
        st.write(f"{r['day']} {r['slot']} · {r['person']} · {r['medicine']}")
        st.caption(('대처 안내 확인 · 추가 복용 판단과 별개' if r['action']=='확인 대기' and r['guidance'] else labels.get(r['action'],r['action']))+' · '+r['source'])
        render_action_steps(r['guidance'] or '확인 내용 미입력')
        if st.button('다시 확인하기',key='reopen_'+r['id']):
            with database(True) as conn:
                execute(conn,"UPDATE fm_followup SET action='확인 대기' WHERE id=?",(r['id'],))
                execute(conn,'''INSERT INTO fm_missed VALUES (?,?,?,?,?,?,?) ON CONFLICT(med_id,day,slot) DO NOTHING''',(str(uuid.uuid4()),r['med_id'],r['day'],r['slot'],'사정상 못 먹었어요','후속 조치 재확인',now().isoformat()))
            st.session_state.pop('missed_confirmation_complete',None);st.session_state.page='미복용 목록';st.rerun()
    st.markdown('**못 먹은 사유**')
    missed_history()


@st.dialog('약 사진', width='small')
def medicine_photo(med_id, name):
    import base64
    from PIL import Image, ImageOps, UnidentifiedImageError
    st.markdown('**'+safe(name)+'**')
    current=rows('SELECT image_b64 FROM fm_photos WHERE med_id=?',(med_id,))
    if current:
        st.caption('사진을 바꾸려면 아래에서 다시 촬영하거나 다른 사진을 선택한 뒤 저장하세요.')
        st.image(base64.b64decode(current[0]['image_b64']),use_container_width=True)
    st.caption('모양 확인용 사진입니다. 사진만으로 약 종류·복용법을 판별하지 않습니다.')
    mode=st.segmented_control('사진 추가 방법',['촬영','앨범'],default='촬영',key='photo_mode_'+med_id)
    image=st.camera_input('약이나 포장 사진 찍기',key='camera_'+med_id) if mode=='촬영' else st.file_uploader('사진 선택',type=['jpg','jpeg','png','webp'],key='upload_photo_'+med_id)
    if image and st.button('사진 저장',type='primary',use_container_width=True):
        try:
            if image.size>10*1024*1024:
                st.warning('10MB 이하 사진을 선택하세요.');return
            with Image.open(image) as original:
                if original.width*original.height>25000000:
                    st.warning('사진 크기가 너무 큽니다. 작은 사진을 선택하세요.');return
                pic=ImageOps.exif_transpose(original).convert('RGB')
                pic.thumbnail((1000,1000))
                buffer=io.BytesIO();pic.save(buffer,format='JPEG',quality=85)
            encoded=base64.b64encode(buffer.getvalue()).decode('ascii')
            with database(True) as conn:
                execute(conn,'INSERT INTO fm_photos VALUES (?,?,?) ON CONFLICT(med_id) DO UPDATE SET image_b64=excluded.image_b64,updated=excluded.updated',(med_id,encoded,now().isoformat()))
            st.rerun()
        except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError):
            st.error('사진을 읽지 못했습니다. JPG 또는 PNG 사진으로 다시 시도하세요.')
    if current and st.button('사진 삭제',use_container_width=True):
        with database(True) as conn:execute(conn,'DELETE FROM fm_photos WHERE med_id=?',(med_id,))
        st.rerun()


@st.dialog('가족 삭제')
def remove_family_dialog(person_id, name):
    st.subheader(f'{name}님을 삭제할까요?')
    st.write('가족 목록에서 제외하고 앞으로의 복용 일정을 중지합니다. 과거 기록은 보관합니다.')
    yes,no=st.columns(2)
    if yes.button('삭제하기',type='primary',use_container_width=True):
        with database(True) as conn:
            execute(conn,'INSERT INTO fm_hidden_people(person_id) VALUES (?) ON CONFLICT(person_id) DO NOTHING',(person_id,))
            execute(conn,'UPDATE fm_meds SET active=0 WHERE person_id=?',(person_id,))
            execute(conn,'SELECT 1')
            execute(conn,'UPDATE fm_contact_tasks SET done=1 WHERE person_id=?',(person_id,))
        st.session_state['pending_family_selection']=''
        st.session_state['family_removed_notice']=True
        st.rerun()
    if no.button('취소',use_container_width=True):st.rerun()


@st.dialog('등록된 가족')
def family_list():
    people=rows('SELECT * FROM fm_people WHERE id NOT IN (SELECT person_id FROM fm_hidden_people) ORDER BY name')
    if not people:
        st.info('등록된 가족이 없습니다.')
    for p in people:
        st.write('👤 '+p['name'])


def intake_table():
    chosen=st.date_input('날짜',value=now().date(),key='table_day')
    day=chosen.isoformat()
    people=rows('SELECT * FROM fm_people WHERE id NOT IN (SELECT person_id FROM fm_hidden_people) ORDER BY name')
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
                status='복용';actual=datetime.fromisoformat(logmap[slot]['taken_at']).astimezone(TZ).strftime('%H:%M')
            elif slot in fmap and fmap[slot]['action']=='안내 확인 후 건너뜀':status='건너뜀'
            elif slot in fmap and fmap[slot]['action']=='안내 확인 후 복용 보고':status='시각 미확인'
            elif slot in missmap:
                reason=missmap[slot]['reason'];status='확인 필요' if '모르' in reason else '못 먹음'
            else:
                scheduled=datetime.combine(chosen,datetime.strptime(slot,'%H:%M').time(),TZ)
                status='예정' if scheduled>now() else '확인 필요'
            if slot in missmap:reason=missmap[slot]['reason']+(' · '+missmap[slot]['note'] if missmap[slot]['note'] else '')
            output.append({'가족':m['person'],'약':m['name'],'예정':slot,'복용 시각':actual,'상태':status,'사유':reason})
    if output:
        output.sort(key=lambda r:(r['예정'],r['가족'],r['약']))
        symbols={'복용':'✓ 복용','못 먹음':'× 미복용','건너뜀':'↷ 건너뜀','확인 필요':'? 미확인','예정':'◷ 예정','시각 미확인':'? 시각 미확인'}
        for row in output:row['상태']=symbols.get(row['상태'],'? 미확인')
        details=st.checkbox('사유 보기',key='table_show_reason')
        view=output if details else [{k:v for k,v in row.items() if k!='사유'} for row in output]
        st.dataframe(view,hide_index=True,use_container_width=True)
    else:st.info('이 날짜에 남긴 기록이 없습니다.')
@st.dialog('🔔 복용 알림', width='small')
def dose_notifications(person_id, person_name, completed, total):
    st.markdown(f'**{safe(person_name)}의 알림**')
    expired_meds=rows('SELECT * FROM fm_meds WHERE person_id=? AND active=1 AND expiry<? AND id NOT IN (SELECT med_id FROM fm_removed)',(person_id,now().date().isoformat()))
    for em in expired_meds:
        st.error(em['name']+' · 유효기간 지남 — 복용하지 마세요. ('+em['expiry']+')')
    supplies=supply_alerts(person_id)
    for item in supplies:
        skey='supply_notice_'+str(supplies.index(item))
        st.markdown(f"<style>.st-key-{skey}{{background:{'#E7F5EF' if item['unread'] else '#FFFFFF'}!important;border:1px solid #D5E6DC!important;border-radius:18px!important;padding:16px!important;}}</style>",unsafe_allow_html=True)
        with st.container(key=skey):
            st.markdown(('**새 알림 · **' if item['unread'] else '')+'**'+safe(item['medicine'])+' · 약 보충**')
            if item['remaining']==0:st.write('남은 약이 없어요. 약을 보충해 주세요.')
            else:
                st.write(f"약 {item['remaining']}회분 남음 · 약 {item['days']:.1f}일분" if item['days'] is not None else f"약 {item['remaining']}회분 남음")
                if item['forecast']:st.write('소진 예상 '+item['forecast'].strftime('%m/%d %H:%M')+' · 미리 약을 준비하세요.')
            if item['unread'] and st.button('알림 확인했어요',key='read_'+skey,use_container_width=True):
                with database(True) as conn:
                    execute(conn,'INSERT INTO fm_supply_read VALUES (?) ON CONFLICT(id) DO NOTHING',(item['id'],))
                st.session_state['open_notices_'+person_id]=True;st.rerun()
    tasks=rows('SELECT * FROM fm_contact_tasks WHERE person_id=? ORDER BY day DESC,slot DESC',(person_id,))
    for task in tasks:
        with st.container(border=True):
            st.markdown(f"**{safe(task['medicine'])} · 약국 문의{' 완료' if task['done'] else ' 필요'}**")
            st.write(f"{task['day']} {task['slot']} 놓친 약의 복용법을 약사에게 물어보세요.")
            if not task['done'] and st.button('약사 답변 입력',key='task_answer_'+task['id'],use_container_width=True):
                med=rows('SELECT m.*,p.name AS person FROM fm_meds m JOIN fm_people p ON p.id=m.person_id WHERE m.id=?',(task['med_id'],))
                if med:
                    st.session_state['mq_target']={'m':med[0],'day':task['day'],'slot':task['slot']}
                    st.session_state['mq_step']='instruction'
                    st.session_state['mq_back_stack']=[]
                    st.session_state.page='놓친 약 확인'
                    st.rerun()
    notices=rows('SELECT * FROM fm_notices WHERE person_id=? ORDER BY is_read ASC,day DESC,slot DESC LIMIT 100',(person_id,))
    for n in notices:
        fresh=not n['is_read']
        key='notice_'+re.sub('[^a-zA-Z0-9]','_',n['id'])
        st.markdown(f"<style>.st-key-{key}{{background:{'#E7F5EF' if fresh else '#FFFFFF'}!important;border:1px solid {'#B6DBCB' if fresh else '#E2EAE6'}!important;border-radius:18px!important;padding:16px!important;}}</style>",unsafe_allow_html=True)
        with st.container(key=key):
            st.markdown(('**새 알림 · **' if fresh else '')+'**'+safe(n['medicine'])+'**')
            if not n['id'].startswith('expiry|'):st.write(f"{n['day']} · {n['slot']}에 먹을 약")
            st.write('유효기간이 지났어요. 복용하지 마세요.' if n['id'].startswith('expiry|') else '복용 기록이 비어 있어요. 먹었는지 확인해 주세요.')
            if not n['id'].startswith('expiry|') and st.button('복용 기록 확인하기',key='open_'+key,use_container_width=True):
                with database(True) as conn:execute(conn,'UPDATE fm_notices SET is_read=1 WHERE id=?',(n['id'],))
                st.session_state.page='미복용 목록';st.rerun()
            if fresh and st.button('알림 확인했어요',key='read_'+key,use_container_width=True):
                with database(True) as conn:execute(conn,'UPDATE fm_notices SET is_read=1 WHERE id=?',(n['id'],))
                st.session_state['open_notices_'+person_id]=True;st.rerun()

    if not notices and not tasks and not supplies:st.info('알림이 없습니다.')


@st.dialog('약 복용 확인',width='small')
def medicine_schedules_dialog(med,person,day,schedules):
    advice_key='advice_view_'+med['id']+'_'+day
    selected_advice=st.session_state.get(advice_key)
    if selected_advice:
        st.subheader(med['name']+' · 대처법')
        st.write(selected_advice['slot']+' · '+selected_advice['reason'])
        render_action_steps(selected_advice['guidance'])
        if st.button('복용 기록으로 돌아가기',key='back_'+advice_key,use_container_width=True):
            st.session_state.pop(advice_key,None);st.rerun()
        return
    st.markdown('<style>[data-testid="stSegmentedControl"] button,[data-testid="stNumberInput"] input,[data-testid="stNumberInput"] button{background:#fff!important;color:#163047!important;-webkit-text-fill-color:#163047!important;}[data-testid="stSegmentedControl"] button[aria-pressed="true"]{background:#dceee7!important;color:#163047!important;}</style>',unsafe_allow_html=True)
    st.subheader(med['name'])
    st.write(person+' · '+med['dose'])
    st.markdown('**오늘 복용 시각**')
    st.write('  /  '.join(slot for slot,done in schedules))
    logs=rows('SELECT slot FROM fm_logs WHERE med_id=? AND day=?',(med['id'],day))
    done_slots={x['slot'] for x in logs}
    missed_by_slot={x['slot']:x for x in rows('SELECT * FROM fm_missed WHERE med_id=? AND day=?',(med['id'],day))}
    follow_by_slot={x['slot']:x for x in rows('SELECT * FROM fm_followup WHERE med_id=? AND day=?',(med['id'],day))}
    for slot,old_done in schedules:
        with st.container(border=True):
            missed=missed_by_slot.get(slot);follow=follow_by_slot.get(slot)
            status='✓ 복용 기록' if slot in done_slots else '미복용 · 대처 안내 확인' if missed and follow else '미복용 기록' if missed else '확인 중' if follow else '기록 없음'
            st.markdown('**'+slot+' · '+status+'**')
            if slot in done_slots:continue
            if missed:
                st.write('사유: '+missed['reason'])
                if follow and follow['guidance']:
                    if st.button('대처법 보기',key='advice_'+med['id']+slot,use_container_width=True):
                        st.session_state[advice_key]=dict(slot=slot,reason=missed['reason'],guidance=follow['guidance']);st.rerun()
                continue
            eat,miss=st.columns(2)
            if eat.button('먹었어요',key='schedule_eat_'+med['id']+slot,use_container_width=True):
                ok,msg=take(med['id'],day,slot,False)
                if ok:
                    st.session_state['dose_toast']=f"{person} · {med['name']} {slot} 복용을 기록했습니다."
                    st.rerun()
                else:st.warning(msg)
            if miss.button('못 먹었어요',key='schedule_miss_'+med['id']+slot,use_container_width=True):
                for k in list(st.session_state):
                    if k.startswith('mq_'):del st.session_state[k]
                chosen=dict(med);chosen['person']=person
                st.session_state['mq_target']={'m':chosen,'day':day,'slot':slot}
                st.session_state['mq_step']='reason';st.session_state['mq_back_stack']=[]
                st.session_state.page='놓친 약 확인';st.rerun()


@st.dialog('복용 확인',width='small')
def dose_action_dialog(m, person, day, slot, done):
    st.subheader(m['name'])
    st.write(f"{person} · {slot} · {m['dose']}")
    if done:
        st.success('이미 복용 기록이 있습니다.');return
    early=datetime.combine(date.fromisoformat(day),datetime.strptime(slot,'%H:%M').time(),TZ)>now()
    early_ok=not early
    if early:
        st.warning('예정 시각 전입니다. 등록된 처방 시각을 확인하세요.')
        early_ok=st.checkbox('복용 시각을 확인했어요',key='dialog_early')
    st.caption('사진·가족·약 이름과 실제 복용 여부를 먼저 확인하세요.')
    confirm=st.checkbox('오늘 같은 성분의 다른 약 기록도 확인했어요',key='dialog_ingredient')
    if st.button('먹었어요',icon=':material/check:',type='primary',use_container_width=True,disabled=not early_ok):
        ok,msg=take(m['id'],day,slot,confirm)
        if ok:
            st.session_state['dose_toast']=f"{person} · {m['name']} ({slot}) 복용을 기록했습니다."
            st.rerun()
        else:st.warning(msg)
    if st.button('못 먹었어요 / 먹었는지 몰라요',icon=':material/pill_off:',use_container_width=True):
        for k in list(st.session_state):
            if k.startswith('mq_'):del st.session_state[k]
        selected=dict(m);selected['person']=person
        st.session_state['mq_target']={'m':selected,'day':day,'slot':slot}
        st.session_state['mq_step']='reason';st.session_state['mq_back_stack']=[]
        st.session_state.page='놓친 약 확인';st.rerun()


@st.fragment(run_every='20s')
def dashboard(person_id, elder):
    st.markdown('<style>\n.st-key-family_schedule_row [data-testid="stHorizontalBlock"]{display:flex!important;flex-direction:row!important;flex-wrap:nowrap!important;gap:8px!important;}\n.st-key-family_schedule_row [data-testid="stColumn"]:first-child{width:25%!important;flex:0 0 25%!important;min-width:0!important;}\n.st-key-family_schedule_row [data-testid="stColumn"]:last-child{width:calc(75% - 8px)!important;flex:1 1 0!important;min-width:0!important;}\n</style>',unsafe_allow_html=True)
    # Select family before querying schedules; scalar ids keep selection stable.
    people=rows('SELECT * FROM fm_people WHERE id NOT IN (SELECT person_id FROM fm_hidden_people) ORDER BY name')
    names={p['id']:p['name'] for p in people}
    preferred=st.session_state.pop('pending_family_selection',None)
    if preferred in names:
        st.session_state['today_family_compact']=preferred
    elif st.session_state.get('today_family_compact') not in names:
        st.session_state['today_family_compact']=next(iter(names),None)
    with st.container(key='family_schedule_row'):
        family_col,schedule_col=st.columns([1,3],gap='small')
    with family_col:
        person_id=st.selectbox('가족',list(names),format_func=lambda k:names[k],key='today_family_compact',label_visibility='collapsed')
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

    # Distinguish missing records from confirmed missed intake; never infer ingestion.
    missed_status=rows('SELECT med_id,slot,reason FROM fm_missed WHERE day=?',(today,))
    missed_map={(x['med_id'],x['slot']):x['reason'] for x in missed_status}
    outcomes=rows('SELECT med_id,slot,action FROM fm_followup WHERE day=?',(today,))
    outcome_map={(x['med_id'],x['slot']):x['action'] for x in outcomes}
    needs_attention=[]
    for item in due:
        ident=(item['m']['id'],item['time'])
        if outcome_map.get(ident) in ('안내 확인 후 건너뜀','안내 확인 후 복용 보고','기록 확인 완료'):
            continue
        reason=missed_map.get(ident,'')
        state_label='못 먹음 기록' if reason and '모르' not in reason else '복용 여부 미확인'
        needs_attention.append((item,state_label))
    if needs_attention:
        st.html('<div style="background:#fff4ee;border:1px solid #e6b29a;border-radius:12px;padding:10px 12px;color:#84341f;font-weight:750;font-size:15px">! 복용 확인 필요 '+str(len(needs_attention))+'건 · 아래 일정에서 확인</div>')
    if expired:st.warning(f'유효기간 지난 약 {expired}개 · 복용 금지')
    flash = st.session_state.pop('flash', None)
    if flash:
        (st.success if flash[0] else st.warning)(flash[1])
    if not slots:
        schedule_col.info('등록된 복용 일정이 없습니다.')
        return
    # 한 회차씩 선택하므로 약이 많아져도 본문 길이가 늘어나지 않습니다.
    ordered = slots  # chronological regardless of completion
    choice_keys=[x['m']['id']+'|'+x['time'] for x in ordered]
    choice_map=dict(zip(choice_keys,ordered))
    choice_key=f'slot_v3_{person_id}_{today}'
    if st.session_state.get(choice_key) not in choice_map:
        st.session_state[choice_key]=next((k for k,x in choice_map.items() if not x['done']),choice_keys[0])
    selected=choice_map[st.session_state[choice_key]]
    m, slot = selected['m'], selected['time']
    person_name=rows('SELECT name FROM fm_people WHERE id=?',(person_id,))
    expired_selected=m['expiry']<today
    early=selected['scheduled']>current
    state='복용 완료' if selected['done'] else '유효기간 경과' if expired_selected else '예정' if early else '확인 필요'
    pname=person_name[0]['name'] if person_name else '가족'
    completed_slots=sum(x['done'] for x in slots)
    status_icon='✓' if selected['done'] else '!' if expired_selected else '◷'
    status_label='복용 완료' if selected['done'] else '유효기간 경과' if expired_selected else '복용 예정' if early else '복용 여부 확인'
    # Vertical schedule, not a numeric dose chart: never implies dose quantity.
    missed_rows=rows('SELECT med_id,slot FROM fm_missed WHERE day=?',(today,))
    missed_keys={(r['med_id'],r['slot']) for r in missed_rows}
    follow_rows=rows('SELECT med_id,slot,action FROM fm_followup WHERE day=?',(today,))
    follow_map={(r['med_id'],r['slot']):r['action'] for r in follow_rows}
    st.html('<style>.st-key-dose_confirm button{background:#176f57!important;border-color:#176f57!important;min-height:58px!important;border-radius:16px!important}.st-key-dose_confirm button p,.st-key-dose_confirm button span{color:#fff!important;-webkit-text-fill-color:#fff!important}</style>')
    if st.session_state.get('dose_toast'):
        st.toast(st.session_state.pop('dose_toast'),icon=':material/check_circle:')
    st.markdown('**내 약**')
    # One tile per medicine, not per time. Keep individual schedules inside its dialog.
    import base64
    photos={r['med_id']:r['image_b64'] for r in rows('SELECT med_id,image_b64 FROM fm_photos')}
    med_groups={}
    for entry in slots:med_groups.setdefault(entry['m']['id'],[]).append(entry)
    def priority(entries):
        return 0 if any(not e['done'] and e['scheduled']<=current and follow_map.get((e['m']['id'],e['time'])) not in ('안내 확인 후 건너뜀','안내 확인 후 복용 보고','기록 확인 완료') for e in entries) else 2 if all(e['done'] for e in entries) else 1
    groups=sorted(med_groups.values(),key=lambda entries:(priority(entries),entries[0]['m']['name']))
    st.markdown("""<style>
    .st-key-medicine_tiles [data-testid="stHorizontalBlock"]{display:flex!important;flex-direction:row!important;flex-wrap:nowrap!important;gap:10px!important;}
    .st-key-medicine_tiles [data-testid="stColumn"]{flex:1 1 0!important;width:calc(50% - 5px)!important;min-width:0!important;}
    .st-key-medicine_tiles [data-testid="stImage"] img{aspect-ratio:1/1!important;object-fit:contain!important;width:100%!important;border:1px solid #d5e2da;border-radius:18px;background:#f5f9f6;}
    .st-key-medicine_tiles button{min-height:48px!important;border-radius:12px!important;padding:8px!important;}
    .st-key-medicine_tiles button p{font-size:15px!important;white-space:normal!important;overflow-wrap:anywhere!important;}
    </style>""",unsafe_allow_html=True)
    palette=['#A9CBE3','#C5B7DE','#F0C791','#A7D5C4','#E7ADBF','#CBD69C']
    st.markdown("""<style>
    .st-key-medicine_tiles [data-testid="stVerticalBlock"]{gap:6px!important;}
    .st-key-medicine_tiles button{min-height:48px!important;padding:7px!important;}
    .st-key-medicine_tiles button p{font-size:19px!important;font-weight:800!important;color:#153d32!important;line-height:1.4!important;}
    </style>""",unsafe_allow_html=True)
    character_counter=0
    with st.container(key='medicine_tiles'):
        for offset in range(0,len(groups),2):
            cols=st.columns(2,gap='small')
            for col,entries in zip(cols,groups[offset:offset+2]):
                med=entries[0]['m'];rank=priority(entries)
                color=palette[sum(med['id'].encode('utf-8'))%len(palette)]
                background='#fff0ec' if rank==0 else '#e7f4ec' if rank==2 else '#f3f6f8'
                border='#d28070' if rank==0 else '#9ecab0' if rank==2 else '#d5dfe2'
                has_missed=any((e['m']['id'],e['time']) in missed_keys and not e['done'] for e in entries)
                has_guidance=any((e['m']['id'],e['time']) in missed_keys and (e['m']['id'],e['time']) in follow_map and not e['done'] for e in entries)
                prefix='✓ 복용 완료' if all(e['done'] for e in entries) else '미복용 · 안내 확인' if has_guidance else '미복용' if has_missed else '! 복용 확인' if rank==0 else '◷ 예정'
                with col:
                    if med['id'] in photos:
                        graphic=f'<img src="data:image/jpeg;base64,{photos[med["id"]]}" alt="{safe(med["name"])} 사진" style="width:66px;height:66px;object-fit:contain;border-radius:10px;" />'
                    else:
                        character_index=character_counter % len(CHARACTER_IMAGES)
                        character_counter+=1
                        graphic=f'<img src="data:image/png;base64,{CHARACTER_IMAGES[character_index]}" alt="알약 캐릭터" style="width:90px;height:126px;object-fit:contain;border-radius:16px;" />'
                    st.html(f'<div style="min-height:154px;padding:14px 8px;box-sizing:border-box;background:{background};border:1px solid {border};border-radius:16px;display:flex;align-items:center;justify-content:center;position:relative;"><span style="position:absolute;top:7px;right:9px;font-size:14px;font-weight:800;color:#24473d;">{"!" if rank==0 else "✓" if rank==2 else "◷"}</span>{graphic}</div>')
                    if st.button(med['name']+' · '+prefix,key='tile_med_'+med['id'],use_container_width=True):
                        medicine_schedules_dialog(dict(med),pname,today,[(e['time'],e['done']) for e in entries])

    with database(True) as conn:
        execute(conn,"CREATE TABLE IF NOT EXISTS fm_notices (id TEXT PRIMARY KEY, person_id TEXT NOT NULL, day TEXT NOT NULL, slot TEXT NOT NULL, medicine TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0)")
        recorded_outcomes={(r['med_id'],r['slot']) for r in rows('SELECT med_id,slot FROM fm_missed WHERE day=?',(today,))}
        recorded_outcomes.update((r['med_id'],r['slot']) for r in rows('SELECT med_id,slot FROM fm_followup WHERE day=?',(today,)))
        for entry in due:
            if (entry['m']['id'],entry['time']) in recorded_outcomes:
                continue
            notice_id=f"{person_id}|{today}|{entry['m']['id']}|{entry['time']}"
            execute(conn,'INSERT INTO fm_notices VALUES (?,?,?,?,?,0) ON CONFLICT(id) DO NOTHING',(notice_id,person_id,today,entry['time'],entry['m']['name']))
    with database(True) as conn:
        execute(conn,"DELETE FROM fm_notices WHERE EXISTS (SELECT 1 FROM fm_logs l JOIN fm_meds m ON m.id=l.med_id WHERE fm_notices.id=m.person_id||'|'||l.day||'|'||l.med_id||'|'||l.slot)")
    with database(True) as conn:
        for em in meds:
            if em['expiry']<today:
                execute(conn,'INSERT INTO fm_notices VALUES (?,?,?,?,?,0) ON CONFLICT(id) DO NOTHING',('expiry|'+em['id']+'|'+em['expiry'],person_id,today,'유효기간',em['name']))
    unread=rows('SELECT id FROM fm_notices WHERE person_id=? AND is_read=0',(person_id,))
    pending_tasks=rows('SELECT id FROM fm_contact_tasks WHERE person_id=? AND done=0',(person_id,))
    unread_count=len(unread)+len(pending_tasks)+sum(x['unread'] for x in supply_alerts(person_id))
    bell_border = '#b42318' if unread_count else '#abc7c0'
    bell_bg = '#fff0ec' if unread_count else '#ffffff'
    st.markdown(f"""<style>
    [data-testid="stAppViewContainer"] .st-key-today_tools .st-key-notification_only [data-testid="stButton"] button,
    [data-testid="stAppViewContainer"] .st-key-today_tools .st-key-notification_only button{{
      border:{'3px' if unread_count else '1px'} solid {bell_border}!important;background:{bell_bg}!important;
      box-shadow:none!important;outline:none!important;animation:none!important;flex-direction:column!important;gap:2px!important;}}
    [data-testid="stAppViewContainer"] .st-key-today_tools .st-key-notification_only button *{{
      color:{'#b42318' if unread_count else '#163047'}!important;
      -webkit-text-fill-color:{'#b42318' if unread_count else '#163047'}!important;}}
    .st-key-rail_missed button{{flex-direction:row!important;justify-content:center!important;align-items:center!important;gap:0!important;min-height:44px!important;height:44px!important;padding:4px!important;}}
    </style>""",unsafe_allow_html=True)
    if st.session_state.pop('open_notices_'+person_id,False):
        dose_notifications(person_id,pname,len(done),len(slots))
    st.markdown("""<style>
    .st-key-today_tools{width:92px!important;}
    .st-key-today_voice_read button{width:82px!important;min-width:82px!important;height:auto!important;min-height:72px!important;display:flex!important;flex-direction:column!important;justify-content:center!important;align-items:center!important;gap:6px!important;padding:10px 5px!important;}
    .st-key-today_voice_read button p{white-space:nowrap!important;word-break:keep-all!important;writing-mode:horizontal-tb!important;font-size:15px!important;line-height:1.3!important;margin:0!important;}
    </style>""",unsafe_allow_html=True)
    st.markdown("""<style>
    .st-key-today_tools{position:fixed!important;right:8px!important;top:22%!important;width:84px!important;padding:4px!important;z-index:100;background:#fff;}
    .st-key-today_tools button{width:74px!important;min-width:74px!important;height:auto!important;min-height:76px!important;display:flex!important;flex-direction:column!important;align-items:center!important;gap:5px!important;padding:8px 2px!important;}
    .st-key-today_tools button p{font-size:14px!important;white-space:nowrap!important;writing-mode:horizontal-tb!important;line-height:1.4!important;}
    .block-container{padding-right:100px!important;}
    </style>""",unsafe_allow_html=True)
    with st.container(key='today_tools'):
        if st.button('큰 글씨',icon=':material/text_increase:',key='rail_large',help='큰 글씨 켜기·끄기'):
            st.session_state['elder_mode']=not st.session_state.get('elder_mode',False);st.rerun()
        if st.button('음성 안내',icon=':material/volume_up:',key='rail_read'):
            voice_dialog(pname+'님의 오늘 일정. '+'. '.join(e['time']+' '+e['m']['name']+' '+e['m']['dose'] for e in slots))
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
