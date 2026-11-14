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

def character_for(m):
    import hashlib
    key=m['name'].strip()+'|'+m['ingredient'].strip()
    registered=rows('SELECT name,ingredient FROM fm_meds ORDER BY name,ingredient')
    keys=sorted(set(r['name'].strip()+'|'+r['ingredient'].strip() for r in registered))
    index=keys.index(key) if key in keys else int(hashlib.sha256(key.encode()).hexdigest()[:8],16)
    return CHARACTER_IMAGES[index % len(CHARACTER_IMAGES)]

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
                else:
                    import base64
                    photo.image(base64.b64decode(character_for(m)),use_container_width=True)
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
    st.markdown("""<style>
    [role="dialog"] [data-testid="stSegmentedControl"] button,
    [role="dialog"] [data-testid="stSegmentedControl"] button *,
    [role="dialog"] [data-testid="stButtonGroup"] button,
    [role="dialog"] [data-testid="stButtonGroup"] button * {
      background:#ffffff!important;color:#163047!important;
      -webkit-text-fill-color:#163047!important;border-color:#b7ccc5!important;
    }
    [role="dialog"] button[aria-pressed="true"],
    [role="dialog"] button[aria-pressed="true"] * {
      background:#dcefe6!important;color:#123b30!important;
      -webkit-text-fill-color:#123b30!important;
    }
    [role="dialog"] [data-testid="stCameraInput"] button,
    [role="dialog"] [data-testid="stFileUploader"] button {
      background:#fff!important;color:#163047!important;
      -webkit-text-fill-color:#163047!important;
    }
    </style>""",unsafe_allow_html=True)
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
                        graphic=f'<img src="data:image/png;base64,{character_for(med)}" alt="알약 그림" style="width:90px;height:126px;object-fit:contain;border-radius:16px;" />'
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
