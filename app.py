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
