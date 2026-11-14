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
                        character_index=int.from_bytes(med['id'].encode(),'little') % len(CHARACTER_IMAGES)
                        graphic=f'<img src="data:image/png;base64,{CHARACTER_IMAGES[character_index]}" alt="알약 캐릭터" style="width:76px;height:96px;object-fit:contain;border-radius:16px;" />'
                    st.html(f'<div style="height:96px;background:{background};border:1px solid {border};border-radius:16px;display:flex;align-items:center;justify-content:center;position:relative;"><span style="position:absolute;top:7px;right:9px;font-size:14px;font-weight:800;color:#24473d;">{"!" if rank==0 else "✓" if rank==2 else "◷"}</span>{graphic}</div>')
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
