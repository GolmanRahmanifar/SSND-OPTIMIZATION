#!/usr/bin/env python3
"""Freight Network Lab — self-hosted classroom submission & live referee.
Python 3.10+, `pip install -r requirements.txt`, then `python app.py`.
User metrics are self-reported; facilitator must verify feasibility before ranking.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_M

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static'
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
UPLOADS = DATA / 'uploads'
UPLOADS.mkdir(exist_ok=True)
DB = DATA / 'freight_network_lab.sqlite3'
SECRET_FILE = DATA / '.facilitator_password'
PORT = int(os.environ.get('PORT', '8765'))
HOST = os.environ.get('HOST', '0.0.0.0')
DB_LOCK = threading.RLock()
SESSIONS = {}


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def db():
    conn = sqlite3.connect(str(DB), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def setup():
    with DB_LOCK, db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS teams (
              team_id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL,
              trucks INTEGER, distance REAL, load_factor REAL,
              status TEXT NOT NULL DEFAULT 'waiting',
              proof TEXT, submitted_at TEXT, updated_at TEXT,
              review_note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_at TEXT NOT NULL, team_id INTEGER, action TEXT NOT NULL, payload TEXT
            );
        ''')
        for i in range(1, 5):
            conn.execute('INSERT OR IGNORE INTO teams(team_id,token) VALUES (?,?)',(i,secrets.token_urlsafe(22)))
        defaults = {
          'timer_duration': '1800',
          'timer_remaining': '1800',
          'timer_started_at': '0',
          'timer_running': 'false',
          'results_visible': 'false',
          'accepting_submissions': 'true',
        }
        for k,v in defaults.items():
            conn.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)',(k,v))
    password = os.environ.get('FNL_ADMIN_PASSWORD','')
    if not password:
        if SECRET_FILE.exists():
            password = SECRET_FILE.read_text(encoding='utf-8').strip()
        else:
            password = secrets.token_urlsafe(15)
            SECRET_FILE.write_text(password,encoding='utf-8')
            try: SECRET_FILE.chmod(0o600)
            except OSError: pass
    return password

PASSWORD = setup()


def settings(conn):
    return {x['key']: x['value'] for x in conn.execute('SELECT key,value FROM settings')}


def set_settings(conn, **values):
    for k,v in values.items():
        conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(k,str(v).lower() if isinstance(v,bool) else str(v)))


def timer(s):
    remaining = max(0,int(s['timer_remaining']))
    if s['timer_running']=='true':
        remaining=max(0,remaining-int(time.time()-float(s['timer_started_at'])))
    return {'seconds':remaining,'running':s['timer_running']=='true' and remaining>0,
            'duration':int(s['timer_duration'])}


def snapshot_public(conn, include_tokens=False, admin=False):
    s=settings(conn)
    t=timer(s)
    reveal=s['results_visible']=='true'
    result=[]
    for x in conn.execute('SELECT * FROM teams ORDER BY team_id'):
        item={'team':x['team_id'],'status':x['status'],'submitted_at':x['submitted_at']}
        if admin or (reveal and x['status']=='verified'):
            item.update({'trucks':x['trucks'],'distance':x['distance'],
                         'load_factor':x['load_factor'],
                         'cost':int(x['trucks']*500 + x['distance']) if x['trucks'] is not None else None})
        if admin:
            item.update({'proof':bool(x['proof']),'review_note':x['review_note']})
        if include_tokens:
            item['token']=x['token']
        result.append(item)
    ranked=sorted([x for x in result if x['status']=='verified' and x.get('cost') is not None],
                  key=lambda x:(x['cost'],x['distance'],x['trucks'],x['team']))
    for i,x in enumerate(ranked,1):x['rank']=i
    return {'teams':result,'ranked':ranked,'timer':t,'revealed':reveal,
            'open':s['accepting_submissions']=='true','updated_at':now()}


def local_ip():
    # No outbound connection is actually sent by UDP connect.
    try:
        soc=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        soc.connect(('192.0.2.1',80))
        ip=soc.getsockname()[0]
        soc.close()
        return ip
    except OSError:
        return '127.0.0.1'

PUBLIC_BASE=(os.environ.get('PUBLIC_BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/') or f'http://{local_ip()}:{PORT}'


def audit(conn,team_id,action,payload=None):
    conn.execute('INSERT INTO audit(event_at,team_id,action,payload) VALUES (?,?,?,?)',
                 (now(),team_id,action,json.dumps(payload or {},ensure_ascii=False)))


def validate_submission(d):
    try:
        trucks=d.get('trucks')
        distance=d.get('distance')
        factor=d.get('load_factor')
        if isinstance(trucks,bool) or str(trucks).strip()=='':return None,'Enter the number of trucks.'
        if isinstance(distance,bool) or str(distance).strip()=='':return None,'Enter the total distance.'
        if isinstance(factor,bool) or str(factor).strip()=='':return None,'Enter the average load factor.'
        if str(trucks).strip() != str(int(trucks)):
            return None,'Number of trucks must be a whole number.'
        n=int(trucks)
        km=float(distance)
        lf=float(factor)
        if not (1<=n<=24):return None,'Trucks must be between 1 and 24.'
        if not (0 < km <= 30000) or not (km==km and abs(km)<float('inf')):
            return None,'Enter a positive distance (up to 30,000 km).'
        if not (0<=lf<=100) or not (lf==lf and abs(lf)<float('inf')):
            return None,'Average load factor must be between 0 and 100%.'
        if round(km,2)!=km: return None,'Use at most two decimal places for distance.'
        if round(lf,2)!=lf: return None,'Use at most two decimal places for load factor.'
        return {'trucks':n,'distance':km,'load_factor':lf},None
    except (ValueError,TypeError,OverflowError):return None,'Please enter valid numerical values.'


class Handler(BaseHTTPRequestHandler):
    server_version='FNL-Live/1.0'
    def log_message(self,fmt,*args):
        if not self.path.startswith('/api/public') and not self.path.startswith('/api/team/') and not self.path.startswith('/team/'):
            super().log_message(fmt,*args)
    def parsed(self):return urllib.parse.urlsplit(self.path)
    def segments(self):return self.parsed().path.strip('/').split('/')
    def query(self):return urllib.parse.parse_qs(self.parsed().query)
    def _send(self,body,status=200,content_type='application/json; charset=utf-8',headers=None):
        if isinstance(body,str):body=body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store' if content_type.startswith('application/json') else 'public, max-age=300')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        if headers:
            for k,v in headers.items():self.send_header(k,v)
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
    def js(self,body,status=200,headers=None):
        self._send(json.dumps(body,ensure_ascii=False,separators=(',',':')),status,headers=headers)
    def fail(self,msg,status=400):self.js({'ok':False,'error':msg},status)
    def json_body(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size<1 or size>6_000_000:return None
            return json.loads(self.rfile.read(size).decode('utf-8'))
        except (TypeError,ValueError,UnicodeError,json.JSONDecodeError):return None
    def admin(self):
        cookie=self.headers.get('Cookie','')
        for chunk in cookie.split(';'):
            name,_,value=chunk.strip().partition('=')
            if name=='fnl_session':
                exp=SESSIONS.get(value,0)
                if exp>time.time():return True
        return False
    def do_GET(self):
        p=self.parsed().path
        if p in ('/','/team','/leaderboard','/control') or p.startswith('/team/'):
            return self.serve_file('index.html')
        if p=='/api/public':
            with DB_LOCK,db() as conn:
                state=snapshot_public(conn)
                state['links']={str(x['team_id']):f'{PUBLIC_BASE}/team/{x["team_id"]}?token={urllib.parse.quote(x["token"])}' for x in conn.execute('SELECT team_id, token FROM teams')}
            return self.js(state)
        if p.startswith('/api/qr/') and p.endswith('.png'):
            try:i=int(p.split('/')[-1].split('.')[0])
            except ValueError:return self.fail('Unknown team.',404)
            with DB_LOCK,db() as conn:
                x=conn.execute('SELECT token FROM teams WHERE team_id=?',(i,)).fetchone()
            if not x:return self.fail('Unknown team.',404)
            url=f'{PUBLIC_BASE}/team/{i}?token={urllib.parse.quote(x["token"])}'
            qr=qrcode.QRCode(box_size=10,border=3,error_correction=ERROR_CORRECT_M)
            qr.add_data(url);qr.make(fit=True)
            img=qr.make_image(fill_color='#081c39',back_color='white')
            io_buf=io.BytesIO();img.save(io_buf,format='PNG')
            return self._send(io_buf.getvalue(),content_type='image/png',headers={'Cache-Control':'no-store'})
        if p.startswith('/api/team/'):
            try:i=int(p.split('/')[-1])
            except ValueError:return self.fail('Invalid team.',404)
            token=self.query().get('token',[''])[0]
            with DB_LOCK,db() as conn:
                x=conn.execute('SELECT * FROM teams WHERE team_id=?',(i,)).fetchone()
                if not x or not hmac.compare_digest(token,x['token']):return self.fail('Invalid team invitation.',403)
                snap=snapshot_public(conn)
                return self.js({'ok':True,'team':i,'status':x['status'],'trucks':x['trucks'],
                                'distance':x['distance'],'load_factor':x['load_factor'],
                                'proof':bool(x['proof']),'submitted_at':x['submitted_at'],
                                'open':snap['open'],'timer':snap['timer'],
                                'review_note':x['review_note']})
        if p=='/api/admin/snapshot':
            if not self.admin():return self.fail('Sign in to the control tower.',401)
            with DB_LOCK,db() as conn:return self.js(snapshot_public(conn,include_tokens=False,admin=True))
        if p.startswith('/api/admin/photo/'):
            if not self.admin():return self.fail('Sign in first.',401)
            try:i=int(p.rsplit('/',1)[-1])
            except ValueError:return self.fail('Invalid team.',404)
            with DB_LOCK,db() as conn:
                x=conn.execute('SELECT proof FROM teams WHERE team_id=?',(i,)).fetchone()
            if not x or not x['proof']:return self.fail('No photo submitted.',404)
            file=UPLOADS/x['proof']
            if not file.is_file():return self.fail('Photo unavailable.',404)
            mime='image/png' if file.suffix=='.png' else ('image/webp' if file.suffix=='.webp' else 'image/jpeg')
            return self._send(file.read_bytes(),content_type=mime,headers={'Cache-Control':'no-store'})
        if p=='/api/admin/export.csv':
            if not self.admin():return self.fail('Sign in first.',401)
            buf=io.StringIO();writer=csv.writer(buf)
            writer.writerow(['Team','Status','Trucks','Truck-km','Avg LF %','Total cost EUR','Submitted at','Reviewed note','Photo present'])
            with DB_LOCK,db() as conn:
                for x in conn.execute('SELECT * FROM teams ORDER BY team_id'):
                    n=x['trucks'];km=x['distance']
                    writer.writerow([x['team_id'],x['status'],n,km,x['load_factor'],
                                     n*500+km if n is not None else '',x['submitted_at'],x['review_note'],bool(x['proof'])])
            return self._send(buf.getvalue(),content_type='text/csv; charset=utf-8',
                              headers={'Content-Disposition':'attachment; filename="FNL_results.csv"','Cache-Control':'no-store'})
        if p.startswith('/static/'):
            rel=p[len('/static/'):]
            return self.serve_file(rel)
        return self.fail('Not found.',404)
    def serve_file(self,rel):
        target=(STATIC / rel).resolve()
        if not target.is_file() or STATIC.resolve() not in target.parents:
            return self.fail('File not found.',404)
        mime={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8',
              '.css':'text/css; charset=utf-8','.webp':'image/webp','.svg':'image/svg+xml','.png':'image/png'}
        return self._send(target.read_bytes(),content_type=mime.get(target.suffix,'application/octet-stream'))
    def do_POST(self):
        p=self.parsed().path
        data=self.json_body()
        if not isinstance(data,dict):return self.fail('Invalid request or file too large.')
        if p=='/api/admin/login':
            provided=str(data.get('password',''))
            if not hmac.compare_digest(provided,PASSWORD):return self.fail('Incorrect facilitator password.',403)
            tok=secrets.token_urlsafe(34)
            SESSIONS[tok]=time.time()+3600*10
            cookie=f'fnl_session={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age=36000'
            if self.headers.get('X-Forwarded-Proto','')=='https': cookie+='; Secure'
            return self.js({'ok':True},headers={'Set-Cookie':cookie})
        if p=='/api/submit':
            try:i=int(data.get('team'))
            except (ValueError,TypeError):return self.fail('Invalid team.')
            if i not in range(1,5):return self.fail('Invalid team.')
            token=str(data.get('token',''))
            values,error=validate_submission(data)
            if error:return self.fail(error)
            proof=data.get('proof')
            suffix=''
            raw=None
            if proof:
                if not isinstance(proof,str) or len(proof)>4_300_000:return self.fail('Photo is too large (max 3 MB).')
                try:
                    header,b64=proof.split(',',1)
                    mime={'data:image/jpeg;base64':'.jpg','data:image/png;base64':'.png','data:image/webp;base64':'.webp'}
                    suffix=mime.get(header,'')
                    if not suffix:return self.fail('Only JPEG, PNG or WebP photos are accepted.')
                    raw=base64.b64decode(b64,validate=True)
                    if len(raw)>3_200_000:return self.fail('Photo is too large.')
                    signatures={'.jpg':(b'\xff\xd8\xff',),'.png':(b'\x89PNG\r\n\x1a\n',),'.webp':(b'RIFF',)}
                    if not any(raw.startswith(s) for s in signatures[suffix]):return self.fail('Image file is invalid.')
                except (ValueError,base64.binascii.Error):return self.fail('Could not read the photo.')
            with DB_LOCK,db() as conn:
                x=conn.execute('SELECT * FROM teams WHERE team_id=?',(i,)).fetchone()
                if not x or not hmac.compare_digest(token,x['token']):return self.fail('Invalid team link.',403)
                if settings(conn)['accepting_submissions']!='true':return self.fail('Submissions are closed.',403)
                if x['status']=='verified':return self.fail('The verified result is locked. Ask the facilitator to reopen it.',409)
                imgname=x['proof']
                if raw is not None:
                    imgname=f'team_{i}_{secrets.token_hex(12)}{suffix}'
                    (UPLOADS/imgname).write_bytes(raw)
                conn.execute('''UPDATE teams SET trucks=?,distance=?,load_factor=?,status='submitted',
                    proof=?,submitted_at=?,updated_at=?,review_note='' WHERE team_id=?''',
                    (values['trucks'],values['distance'],values['load_factor'],imgname,now(),now(),i))
                audit(conn,i,'submitted',values)
            return self.js({'ok':True,'status':'submitted','cost':values['trucks']*500+values['distance']})
        if p=='/api/admin/action':
            if not self.admin():return self.fail('Sign in first.',401)
            action=data.get('action')
            with DB_LOCK,db() as conn:
                s=settings(conn)
                t=timer(s)
                if action=='start':
                    set_settings(conn,timer_remaining=t['seconds'] if t['seconds'] else int(s['timer_duration']),
                                 timer_started_at=time.time(),timer_running=True)
                elif action=='pause':
                    set_settings(conn,timer_remaining=t['seconds'],timer_running=False)
                elif action=='reset_timer':
                    set_settings(conn,timer_remaining=int(s['timer_duration']),timer_running=False)
                elif action=='set_minutes':
                    try:m=int(data.get('minutes'))
                    except (ValueError,TypeError):return self.fail('Invalid duration.')
                    if m<1 or m>180:return self.fail('Choose 1–180 minutes.')
                    set_settings(conn,timer_duration=m*60,timer_remaining=m*60,timer_running=False)
                elif action=='reveal':set_settings(conn,results_visible=True)
                elif action=='hide':set_settings(conn,results_visible=False)
                elif action=='open':set_settings(conn,accepting_submissions=True)
                elif action=='close':set_settings(conn,accepting_submissions=False)
                elif action in ('verify','reject','reopen'):
                    try:i=int(data.get('team'))
                    except (ValueError,TypeError):return self.fail('Invalid team.')
                    x=conn.execute('SELECT * FROM teams WHERE team_id=?',(i,)).fetchone()
                    if not x:return self.fail('Unknown team.',404)
                    if action=='verify' and x['status']!='submitted':return self.fail('Team must submit before verification.',409)
                    if action in ('reject','reopen') and x['status'] not in ('submitted','verified','rejected'):
                        return self.fail('No submission to review.',409)
                    target={'verify':'verified','reject':'rejected','reopen':'submitted'}[action]
                    conn.execute('UPDATE teams SET status=?,review_note=?,updated_at=? WHERE team_id=?',
                                 (target,str(data.get('note',''))[:250],now(),i))
                    audit(conn,i,action,{'note':str(data.get('note',''))[:250]})
                elif action=='reset_teams':
                    if str(data.get('confirm',''))!='RESET':return self.fail('Type RESET to clear the four results.',400)
                    conn.execute("UPDATE teams SET trucks=NULL,distance=NULL,load_factor=NULL,status='waiting',proof=NULL,submitted_at=NULL,updated_at=NULL,review_note=''")
                    set_settings(conn,results_visible=False,accepting_submissions=True)
                    audit(conn,None,'reset_teams')
                elif action=='load_demo':
                    # Explicit rehearsal only; scores remain unverified until facilitator approves.
                    ref=[(9,1716,23.43),(6,1244,32.32),(4,912,47.19),(3,912,47.19)]
                    for i,(n,km,lf) in enumerate(ref,1):
                        conn.execute('UPDATE teams SET trucks=?,distance=?,load_factor=?,status=?,submitted_at=?,updated_at=?,review_note=? WHERE team_id=?',
                                     (n,km,lf,'submitted',now(),now(),'DEMO DATA — verify before revealing',i))
                    set_settings(conn,results_visible=False)
                    audit(conn,None,'load_demo')
                else:return self.fail('Unknown action.',404)
                out=snapshot_public(conn,admin=True)
            return self.js({'ok':True,'state':out})
        return self.fail('Unknown route.',404)


if __name__=='__main__':
    print('\n'+'='*67)
    print('  FREIGHT NETWORK LAB — LIVE CLASSROOM CONTROL TOWER')
    print('='*67)
    print(f'  Facilitator computer: http://localhost:{PORT}/control')
    print(f'  Projector / QR board : {PUBLIC_BASE}/')
    print(f'  Live leaderboard     : {PUBLIC_BASE}/leaderboard')
    print(f'  Facilitator password : {PASSWORD}')
    print('  All phones must be on the same Wi-Fi for local use.')
    print('  Set PUBLIC_BASE_URL for a hosted HTTPS server.')
    print('  Keep the password private; review results before verifying.')
    print('='*67+'\n')
    try:ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
    except KeyboardInterrupt:print('\nStopped.')