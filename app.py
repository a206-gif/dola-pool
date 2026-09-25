from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import sys
sys.path.insert(0, '/opt/dola-pool/gen')
import sqlite3, json, csv, io, secrets, hashlib, datetime, os, threading, queue, time, urllib.request, urllib.error
import dola_gen_clean as dgc

DB = os.getenv('DOLA_DB', 'dola.db')
OUTPUT_DIR = '/opt/dola-pool/output'
os.makedirs(OUTPUT_DIR, exist_ok=True)
MAX_DAILY_PER_ACCOUNT = 20  # 每个号每天最多20条
app = FastAPI(title='Dola Pool Operations')
app.mount('/static', StaticFiles(directory='/opt/dola-pool/static'), name='static')
app.mount('/output', StaticFiles(directory='/opt/dola-pool/output'), name='output')
CONCURRENCY_LIMIT = max(1, min(int(os.getenv('VIDEO_MAX_CONCURRENCY', '100')), 100))
MAX_WORKERS = 100
PROVIDER_URL = os.getenv('VIDEO_PROVIDER_URL', '').strip()
PROVIDER_TOKEN = os.getenv('VIDEO_PROVIDER_TOKEN', '').strip()
TASK_QUEUE = queue.Queue()
# IP轮换由dola_gen_clean.py内部处理（每45分钟或出片后自动换台湾IP）
WORKER_THREADS = []
ACTIVE_WORKERS = 0
ACTIVE_LOCK = threading.Lock()

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init():
    c=db(); c.executescript('''
    create table if not exists accounts(id integer primary key, name text, cookie text, status text default 'active', points integer default 0, created_at text);
    create table if not exists proxies(id integer primary key, host text, port integer, username text, password text, status text default 'unknown');
    create table if not exists tasks(id text primary key, prompt text, duration integer, ratio text, model text, account_id integer, status text, progress integer default 0, result text, created_at text);
    create table if not exists api_keys(id integer primary key, label text, key text, enabled integer default 1, created_at text);
    create table if not exists settings(k text primary key, v text);
    create table if not exists errors(id integer primary key, level text, message text, created_at text);
    create table if not exists audits(id integer primary key, action text, detail text, created_at text);
    '''); c.commit(); c.close()
    c=db()
    for stmt in ["alter table tasks add column quality text default '720p'", "alter table tasks add column min_bytes integer default 30000000", "alter table accounts add column check_state text default 'unchecked'", "alter table accounts add column check_detail text default ''", "alter table accounts add column last_checked text", "alter table accounts add column text_ok integer default 0", "alter table accounts add column image_ok integer default 0"]:
        try: c.execute(stmt)
        except sqlite3.OperationalError: pass
    c.commit(); c.close()
init()
try:
    _saved_db = db(); _saved_limit = _saved_db.execute("select v from settings where k='max_concurrency'").fetchone(); _saved_db.close()
    if _saved_limit: CONCURRENCY_LIMIT = max(1, min(int(_saved_limit[0]), 20))
except (ValueError, TypeError, sqlite3.Error):
    pass

def get_today_str():
    return datetime.datetime.utcnow().strftime('%Y-%m-%d')

def select_account_for_task():
    """智能选号：选今天使用量最少、状态正常、同时没有任务在跑的号"""
    today = get_today_str()
    c = db()
    # 找所有active/ready的号
    accounts = c.execute("select id, name, cookie, status from accounts where status in ('active','ready') and cookie != ''").fetchall()
    if not accounts:
        c.close()
        return None, '没有可用账号'
    
    # 统计每个号今天的使用量和正在运行的任务
    best_account = None
    best_usage = 999999
    for acc in accounts:
        # 今天使用量
        usage = c.execute("select count(*) from tasks where account_id=? and date(created_at)=?", (acc['id'], today)).fetchone()[0]
        # 正在运行的任务
        running = c.execute("select count(*) from tasks where account_id=? and status='running'", (acc['id'],)).fetchone()[0]
        if running > 0:
            continue  # 这个号正在跑任务，跳过（每号同时仅1个任务）
        if usage >= MAX_DAILY_PER_ACCOUNT:
            continue  # 今天额度用完了
        if usage < best_usage:
            best_usage = usage
            best_account = acc
    
    c.close()
    if not best_account:
        return None, '所有号都在运行中或今日额度已用完'
    return best_account, f'今日已用{best_usage}/{MAX_DAILY_PER_ACCOUNT}'

def now(): return datetime.datetime.utcnow().isoformat(timespec='seconds')+'Z'
def audit(action, detail=''):
    c=db(); c.execute('insert into audits(action,detail,created_at) values(?,?,?)',(action,detail,now())); c.commit(); c.close()

def error_log(level, message):
    c=db(); c.execute('insert into errors(level,message,created_at) values(?,?,?)',(level,message,now())); c.commit(); c.close()

def local_account_check(account_id):
    c=db(); row=c.execute('select id,cookie from accounts where id=?',(account_id,)).fetchone()
    if not row:
        c.close(); return {'ok':False,'state':'missing','detail':'账号不存在'}
    cookie=(row['cookie'] or '').strip()
    if not cookie:
        state, detail, text_ok = 'invalid', '未检测到 Cookie', 0
    elif '=' not in cookie or len(cookie) < 8:
        state, detail, text_ok = 'invalid', 'Cookie 格式不完整', 0
    else:
        state, detail, text_ok = 'local_ready', '本地格式检查通过；等待官方接口验证', 1
    checked=now(); c.execute('update accounts set check_state=?,check_detail=?,last_checked=?,text_ok=?,image_ok=0,status=? where id=?',(state,detail,checked,text_ok,('ready' if text_ok else 'invalid'),account_id)); c.commit(); c.close(); audit('account.local_check',str(account_id))
    return {'ok':True,'id':account_id,'state':state,'detail':detail,'text_ok':bool(text_ok),'image_ok':False,'last_checked':checked}

def provider_request(task):
    if not PROVIDER_URL:
        return None, 'VIDEO_PROVIDER_URL is not configured'
    payload=json.dumps({k:task.get(k) for k in ('prompt','duration','ratio','model','quality')}).encode()
    headers={'Content-Type':'application/json','User-Agent':'DolaPool/1.0'}
    if PROVIDER_TOKEN: headers['Authorization']='Bearer '+PROVIDER_TOKEN
    req=urllib.request.Request(PROVIDER_URL, data=payload, headers=headers, method='POST')
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                body=json.loads(r.read().decode('utf-8','replace') or '{}')
                return body.get('result_url') or body.get('url') or body.get('id'), None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(30, 2 ** attempt * 2)); continue
            return None, f'provider HTTP {e.code}'
        except Exception as e:
            if attempt < 3: time.sleep(min(15, 2 ** attempt)); continue
            return None, str(e)
    return None, 'provider rate limit after retries'

def process_task(task_id):
    # 每2条任务换一次台湾IP
    # IP轮换由dola_gen_clean.py内部处理
    c=db(); row=c.execute('select * from tasks where id=?',(task_id,)).fetchone(); c.close()
    if not row: return
    task=dict(row)
    
    # 如果没有指定账号，智能选号
    account_id = task.get('account_id')
    if not account_id:
        acc, info = select_account_for_task()
        if not acc:
            c=db(); c.execute("update tasks set status='failed',progress=0,result=? where id=?",(info,task_id)); c.commit(); c.close()
            error_log('account', f'{task_id}: {info}')
            return
        account_id = acc['id']
        cookie = acc['cookie']
        c=db(); c.execute("update tasks set account_id=? where id=?",(account_id,task_id)); c.commit(); c.close()
    else:
        c=db(); row_acc=c.execute('select cookie from accounts where id=?',(account_id,)).fetchone(); c.close()
        cookie = row_acc['cookie'] if row_acc else ''
    
    if not cookie:
        c=db(); c.execute("update tasks set status='failed',progress=0,result=? where id=?",('账号Cookie为空',task_id)); c.commit(); c.close()
        return
    
    c=db(); c.execute("update tasks set status='running',progress=10 where id=?",(task_id,)); c.commit(); c.close()
    
    # 用dola_gen_clean生成视频
    try:
        prompt = task.get('prompt','')
        ratio = task.get('ratio','16:9')
        duration = int(task.get('duration',30))
        model = task.get('model','seedance_v2.0')
        
        # 提交任务
        ref_images_str = task.get('ref_images', '') or ''
        image_paths = [p.strip() for p in ref_images_str.split(',') if p.strip()] if ref_images_str else None
        conv_id = dgc.submit_video(prompt, ratio, duration, model, cookie, image_paths=image_paths)
        if not conv_id:
            raise Exception('提交失败，未返回conversation_id')
        
        c=db(); c.execute("update tasks set progress=30,result=? where id=?",('已提交，等待生成...',task_id)); c.commit(); c.close()
        
        # 轮询并下载
        output_name = f"task_{task_id}"
        video_path = dgc.poll_and_download(conv_id, cookie, OUTPUT_DIR, output_name, ratio, duration, model)
        
        if video_path and os.path.exists(video_path):
            file_size = os.path.getsize(video_path)
            c=db(); c.execute("update tasks set status='completed',progress=100,result=? where id=?",(f'{video_path}|{file_size}bytes',task_id)); c.commit(); c.close()
            audit('task.complete', task_id)
        else:
            raise Exception('生成失败或下载失败')
            
    except Exception as e:
        err_msg = str(e)
        c=db(); c.execute("update tasks set status='failed',progress=0,result=? where id=?",(err_msg,task_id)); c.commit(); c.close()
        error_log('generate', f'{task_id}: {err_msg}')
        audit('task.failed', task_id)
def worker():
    while True:
        task_id=TASK_QUEUE.get()
        global ACTIVE_WORKERS
        while True:
            with ACTIVE_LOCK:
                if ACTIVE_WORKERS < CONCURRENCY_LIMIT:
                    ACTIVE_WORKERS += 1
                    break
            time.sleep(.1)
        try:
            process_task(task_id)
        finally:
            TASK_QUEUE.task_done()
            with ACTIVE_LOCK: ACTIVE_WORKERS -= 1

def start_workers():
    if WORKER_THREADS: return
    for _ in range(MAX_WORKERS):
        t=threading.Thread(target=worker,daemon=True); t.start(); WORKER_THREADS.append(t)
start_workers()

# 服务启动时恢复卡住的任务（running/queued状态重新入队）
def recover_stuck_tasks():
    try:
        c=db()
        stuck = c.execute("select id from tasks where status in ('running','queued')").fetchall()
        c.close()
        for row in stuck:
            tid = row['id']
            c=db()
            c.execute("update tasks set status='queued',progress=0 where id=?",(tid,))
            c.commit()
            c.close()
            TASK_QUEUE.put(tid)
            print(f"[recover] 恢复卡住的任务: {tid}")
        if stuck:
            print(f"[recover] 共恢复 {len(stuck)} 个卡住的任务")
    except Exception as e:
        print(f"[recover] 恢复任务失败: {e}")

recover_stuck_tasks()

def rows(table):
    c=db(); r=[dict(x) for x in c.execute(f'select * from {table} order by rowid desc')]; c.close(); return r

@app.get('/api/proxy-status')
def proxy_status():
    try:
        import sys
        sys.path.insert(0, 'gen')
        from dola_gen_clean import _build_sticky_proxies
        import requests
        proxies = _build_sticky_proxies('status')
        r = requests.get('https://api.ipify.org?format=json', proxies=proxies, timeout=10)
        ip = r.json().get('ip', 'unknown')
        return {'connected': True, 'ip': ip, 'region': '台湾', 'sticky_min': 50, 'provider': 'udealproxy'}
    except Exception as e:
        return {'connected': False, 'error': str(e), 'region': '台湾', 'sticky_min': 50}

@app.get('/api/stats')
def get_stats():
    c=db()
    total_accounts = c.execute('select count(*) from accounts').fetchone()[0]
    active_accounts = c.execute("select count(*) from accounts where status in ('active','ready')").fetchone()[0]
    invalid_accounts = c.execute("select count(*) from accounts where status='invalid'").fetchone()[0]
    cooling_accounts = c.execute("select count(*) from accounts where check_state in ('rate_limited','cooling')").fetchone()[0]
    today_24h = c.execute("select count(*) from tasks where status='completed' and created_at >= datetime('now','-1 day')").fetchone()[0]
    total_tasks = c.execute('select count(*) from tasks').fetchone()[0]
    completed_tasks = c.execute("select count(*) from tasks where status='completed'").fetchone()[0]
    failed_tasks = c.execute("select count(*) from tasks where status='failed'").fetchone()[0]
    running_tasks = c.execute("select count(*) from tasks where status in ('running','queued')").fetchone()[0]
    c.close()
    return {'total_accounts':total_accounts,'active_accounts':active_accounts,'invalid_accounts':invalid_accounts,'cooling_accounts':cooling_accounts,'today_24h':today_24h,'total_tasks':total_tasks,'completed_tasks':completed_tasks,'failed_tasks':failed_tasks,'running_tasks':running_tasks}

@app.post('/api/ai-reason')
async def ai_reason(request: Request):
    x = await request.json()
    from ai_reason_module import ai_reason_script
    scenes, err = ai_reason_script(
        x.get('script', ''),
        x.get('style', 'movie quality'),
        int(x.get('duration', 15)),
        x.get('image_url', '')
    )
    if err:
        return JSONResponse({'error': err}, 500)
    return {'ok': True, 'scenes': scenes, 'count': len(scenes)}

@app.post('/api/upload-image')
async def upload_image(request: Request):
    try:
        form = await request.form()
        if 'file' not in form:
            return JSONResponse({'error': 'missing file'}, 400)
        file = form['file']
        import uuid
        os.makedirs('/opt/dola-pool/uploads', exist_ok=True)
        ext = os.path.splitext(file.filename)[1] or '.jpg'
        filename = str(uuid.uuid4())[:8] + ext
        filepath = os.path.join('/opt/dola-pool/uploads', filename)
        with open(filepath, 'wb') as f:
            f.write(await file.read())
        return {'ok': True, 'path': filepath, 'url': '/uploads/' + filename}
    except Exception as e:
        return JSONResponse({'error': str(e)}, 500)


@app.post('/api/assets')
async def add_asset(request: Request):
    try:
        x = await request.json()
        name = x.get('name') or ('asset_'+str(int(__import__('time').time())))
        atype = x.get('type') or 'character'
        features = x.get('features') or ''
        path = x.get('path') or x.get('url') or ''
        filename = x.get('filename') or ''
        c = db()
        c.execute("INSERT INTO assets(name,type,features,path,filename,created_at) VALUES(?,?,?,?,?,?)",(name,atype,features,path,filename,now()))
        c.commit(); aid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.close()
        return {'ok':True,'id':aid}
    except Exception as e:
        return JSONResponse({'error':str(e)},500)

@app.put('/api/assets/{asset_id}')
async def update_asset(asset_id: int, request: Request):
    try:
        x = await request.json()
        c = db()
        fields = []
        params = []
        for k in ['name','type','features','path','filename']:
            if k in x:
                fields.append(f"{k}=?")
                params.append(x[k])
        if fields:
            params.append(asset_id)
            c.execute(f"UPDATE assets SET {','.join(fields)} WHERE id=?", params)
            c.commit()
        c.close()
        return {'ok':True}
    except Exception as e:
        return JSONResponse({'error':str(e)},500)

@app.delete('/api/assets/{asset_id}')
async def delete_asset_api(asset_id: int):
    try:
        c = db()
        c.execute("DELETE FROM assets WHERE id=?", (asset_id,))
        c.commit()
        c.close()
        return {'ok':True}
    except Exception as e:
        return JSONResponse({'error':str(e)},500)


@app.get('/api/{table}')
def list_table(table: str):
    if table == 'queue': return queue_status()
    if table not in {'accounts','proxies','tasks','api_keys','settings','errors','audits','assets'}: return JSONResponse({'error':'bad table'},400)
    return [{k: ('••••••' if k in {'cookie','password','key'} and v else v) for k,v in r.items()} for r in rows(table)]

@app.post('/api/accounts')
async def add_account(request: Request):
    x=await request.json(); c=db(); c.execute('insert into accounts(name,cookie,points,created_at) values(?,?,?,?)',(x.get('name','Account'),x.get('cookie',''),x.get('points',0),now())); c.commit(); c.close(); audit('account.create',x.get('name','')); return {'ok':True}

@app.post('/api/accounts/{account_id}/check')
def check_account(account_id: int):
    return local_account_check(account_id)

@app.post('/api/accounts/check-all')
def check_all_accounts():
    c=db(); ids=[r['id'] for r in c.execute('select id from accounts order by id')]; c.close()
    results=[local_account_check(i) for i in ids]
    return {'ok':True,'count':len(results),'results':results}

@app.post('/api/import')
async def import_accounts(file: UploadFile=File(None), cookies: str=Form('')):
    text=cookies
    if file: text=(await file.read()).decode('utf-8-sig','ignore')
    items=[]
    try:
        if file and file.filename.lower().endswith('.json'): items=json.loads(text); items=items if isinstance(items,list) else [items]
        elif file and file.filename.lower().endswith('.csv'): items=list(csv.DictReader(io.StringIO(text)))
        else: items=[{'cookie':line.strip()} for line in text.splitlines() if line.strip()]
    except Exception as e: return JSONResponse({'error':str(e)},400)
    c=db()
    for i,x in enumerate(items):
        x = {'cookie': x} if isinstance(x,str) else x
        if not isinstance(x,dict): continue
        cookie=x.get('cookie') or x.get('cookies') or x.get('value') or str(x)
        name=x.get('name') or f'Imported {i+1}'; c.execute('insert into accounts(name,cookie,created_at) values(?,?,?)',(name,cookie,now()))
    c.commit(); c.close(); audit('account.import',str(len(items))); return {'ok':True,'count':len(items)}

@app.post('/api/proxies')
async def add_proxy(request: Request):
    x=await request.json(); c=db(); c.execute('insert into proxies(host,port,username,password) values(?,?,?,?)',(x['host'],x.get('port',8080),x.get('username',''),x.get('password',''))); c.commit(); c.close(); audit('proxy.create',x['host']); return {'ok':True}

@app.post('/api/tasks')
async def add_task(request: Request):
    x=await request.json(); tid=secrets.token_hex(6); duration=int(x.get('duration',30)); model=x.get('model','dola-video-2.5-30s')
    if duration not in (10,15,30): return JSONResponse({'error':'duration must be 10, 15 or 30 seconds'},400)
    c=db(); c.execute('insert into tasks(id,prompt,duration,ratio,model,account_id,status,quality,min_bytes,created_at) values(?,?,?,?,?,?,?,?,?,?)',(tid,x.get('prompt',''),duration,x.get('ratio','16:9'),model,x.get('account_id'),'queued',x.get('quality','720p'),max(30000000,int(x.get('min_bytes',30000000))),now())); c.commit(); c.close(); TASK_QUEUE.put(tid); audit('task.create',tid); return {'ok':True,'id':tid,'queue_depth':TASK_QUEUE.qsize()}

@app.get('/api/queue')
def queue_status():
    c=db(); counts={r['status']:r['n'] for r in c.execute('select status,count(*) n from tasks group by status')}; c.close()
    return {'queue_depth':TASK_QUEUE.qsize(),'workers':CONCURRENCY_LIMIT,'worker_pool':MAX_WORKERS,'active':ACTIVE_WORKERS,'provider_configured':bool(PROVIDER_URL),'counts':counts}

@app.post('/api/tasks/{task_id}/retry')
def retry_task(task_id: str):
    c=db(); r=c.execute('select id from tasks where id=?',(task_id,)).fetchone()
    if not r: c.close(); return JSONResponse({'error':'task not found'},404)
    c.execute("update tasks set status='queued',progress=0,result=null where id=?",(task_id,)); c.commit(); c.close(); TASK_QUEUE.put(task_id); audit('task.retry',task_id); return {'ok':True,'queue_depth':TASK_QUEUE.qsize()}

@app.post('/api/keys')
async def add_key(request: Request):
    x=await request.json(); key='dola_'+secrets.token_urlsafe(24); c=db(); c.execute('insert into api_keys(label,key,created_at) values(?,?,?)',(x.get('label','API key'),key,now())); c.commit(); c.close(); audit('key.create',x.get('label','')); return {'key':key}

@app.post('/api/settings')
async def save_settings(request: Request):
    x=await request.json()
    if not isinstance(x,dict): return JSONResponse({'error':'settings must be an object'},400)
    global CONCURRENCY_LIMIT
    if 'max_concurrency' in x:
        try: CONCURRENCY_LIMIT=max(1,min(int(x['max_concurrency']),100))
        except (TypeError,ValueError): return JSONResponse({'error':'max_concurrency must be 1-20'},400)
    c=db()
    for k,v in x.items(): c.execute('insert into settings(k,v) values(?,?) on conflict(k) do update set v=excluded.v',(str(k),str(v)))
    c.commit(); c.close(); audit('settings.save'); return {'ok':True}

@app.delete('/api/{table}/{item}')
def delete_item(table: str, item: str):
    if table not in {'accounts','proxies','tasks','api_keys','assets'}: return JSONResponse({'error':'invalid table'},400)
    c=db(); c.execute(f'delete from {table} where id=?',(item,)); c.commit(); c.close(); audit(table+'.delete',item); return {'ok':True}

@app.get('/', response_class=HTMLResponse)
def index(): return HTML

HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dola Pool</title><style>
*{box-sizing:border-box}body{margin:0;font:14px Inter,Arial;color:#e7eeee;
background-image:url('/static/bg_images/bg_01.jpg');background-size:cover;background-position:center;background-attachment:fixed;
background-size:cover;
background-position:center;
background-attachment:fixed;
background-repeat:no-repeat;
min-height:100vh}
body::before{content:'';position:fixed;inset:0;background:rgba(15,20,25,0.88);-webkit-z-index:-1}aside{position:fixed;inset:0 auto 0 0;width:182px;background:rgba(20,40,48,0.85);-webkit-color:#b8c9cc;padding:18px 12px;border-right:1px solid rgba(38,61,67,0.5)}aside h2{font-size:17px;margin:0 8px 24px;color:#fff}nav button{display:block;width:100%;text-align:left;background:none;border:0;color:#b8c9cc;padding:13px 12px;border-radius:5px;font-size:14px;cursor:pointer}nav button.active,nav button:hover{background:#078d78;color:#fff}main{margin-left:182px;padding:28px 30px;background:transparent;min-height:100vh}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;border-bottom:1px solid rgba(43,54,57,0.5);padding-bottom:16px}h1{font-size:22px;margin:0;color:#f3f8f7}.panel{background:rgba(28,32,33,0.85);-webkit-border:1px solid rgba(48,59,62,0.5);border-radius:8px;padding:18px;margin-bottom:18px;box-shadow:0 10px 25px rgba(0,0,0,0.2)}.toolbar{display:flex;gap:10px;margin-bottom:14px}input,select,textarea{border:1px solid #3a484b;background:#151819;color:#e7eeee;border-radius:4px;padding:9px;font:inherit}input::placeholder,textarea::placeholder{color:#708184}button.primary{background:#078d78;color:#fff;border:0;border-radius:4px;padding:9px 16px;cursor:pointer}button.primary:hover{background:#0ca58c}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:12px 10px;border-bottom:1px solid rgba(44,53,55,0.5)}th{font-weight:600;color:#92a6a9}td{color:#d9e4e4}.badge{padding:4px 8px;border-radius:4px;background:#4b3517;color:#ffc56d}.modal{position:fixed;inset:0;background:#000b;display:none;align-items:center;justify-content:center}.modal>div{background:#f7faf9;color:#1d3035;width:620px;border-radius:8px;padding:24px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.stat{padding:14px;background:#183a35;border:1px solid #24685d;border-radius:6px;color:#c8e5df}.stat b{display:block;font-size:22px;margin-top:6px;color:#fff}.muted{color:#819396}.work{background:rgba(24,26,27,0.85);-webkit-color:#eee;min-height:560px;border-radius:8px;padding:18px;border:1px solid rgba(48,54,56,0.5)}.work textarea{background:#111;color:#eee;width:100%;height:130px;border-color:#343e40}.work .preview{height:260px;background:#050606;border:1px solid #2b3435;margin-top:14px;display:flex;align-items:center;justify-content:center;color:#718078}.story-top{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;background:rgba(27,30,31,0.85);-webkit-border:1px solid rgba(48,55,56,0.5);border-radius:8px;padding:12px;margin-bottom:14px}.story-top button,.story-top select{margin:3px}.story-top button.active{background:#f4f6f5;color:#111}.scene-card{background:rgba(27,29,30,0.85);-webkit-border:1px solid rgba(48,55,57,0.5);border-radius:9px;padding:16px;margin-bottom:18px}.scene-card header{display:flex;align-items:center;gap:10px;margin-bottom:12px}.scene-no{font-size:34px;color:#45494a}.scene-actions{margin-left:auto}.scene-actions button{margin-left:5px}.danger{color:#ff8b87}.scene-grid{display:grid;grid-template-columns:1fr 1fr 1.08fr;gap:14px}.scene-grid section{min-width:0}.scene-grid label{display:block;color:#9aabad;margin-bottom:7px}.scene-grid textarea{width:100%;height:500px;resize:vertical;background:#111415}.prompt-head{display:flex;justify-content:space-between;align-items:center}.prompt-head button{font-size:11px;padding:5px}.shot-prompt{border:1px solid #303b3e;border-radius:6px;padding:10px;margin-top:10px;min-height:100px;color:#e7eeee}.preview-tools{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.preview-tools button{font-size:12px}.video-preview{height:500px;background:#050607;border:1px solid #303739;border-radius:7px;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#849496}.preview-art{width:94%;height:78%;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#17231e,#253d2c 45%,#121716);text-align:center}.preview-art img{max-width:100%;max-height:100%}.preview-label{padding:7px}.timeline{display:flex;gap:5px;align-items:center;margin-top:8px;overflow:auto}.timeline span{font-size:11px;color:#8c999b;white-space:nowrap}.timeline i{font-style:normal;min-width:46px;padding:5px;text-align:center;border-radius:6px;background:#194c37;color:#7be2ad}.timeline i.bad{background:#4b2428;color:#ff9b98}.timeline small{display:block}.scene-card footer{color:#728184;padding-top:10px;font-size:12px}.add-scene{width:100%;padding:15px;background:rgba(27,37,37,0.8);-webkit-border:1px dashed #347d6f;color:#7bd1be;border-radius:8px;cursor:pointer}.settings-wrap{max-width:1100px}.usage-card,.settings-card{background:#f8fbfa;color:#17333a;border:1px solid #d3dfdd;border-radius:8px;padding:20px;margin-bottom:16px}.usage-card{display:flex;justify-content:space-between;align-items:center}.usage-card b{display:block;font-size:28px;color:#101f23;margin-top:3px}.lease-box{display:flex;align-items:center;gap:12px;color:#48666a}.lease-bars{display:flex;gap:4px}.lease-bars i{width:18px;height:9px;border-radius:2px;background:#d8e1df}.lease-bars i.on{background:#078d78}.lease-box button{background:transparent;color:#36575a;border:0;font-size:20px}.settings-title{display:flex;justify-content:space-between;align-items:start}.settings-title h2,.settings-card h2{margin:0 0 6px}.settings-title b{font-size:25px;color:#078d78}.setting-row{display:flex;justify-content:space-between;margin:25px 0 10px}.settings-card input[type=range]{width:100%;accent-color:#078d78}.quick-row{display:grid;grid-template-columns:repeat(6,1fr);gap:5px;margin-top:16px}.quick-row button{background:#eef3f2;color:#31565b;border:1px solid #cfddda;border-radius:4px;padding:8px}.quick-row button:hover{border-color:#078d78;color:#078d78}.save-conc{width:100%;margin-top:18px}.strategy-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.strategy-grid label{display:flex;flex-direction:column;gap:7px;color:#547074}.strategy-grid select,.strategy-grid input{background:#fff;color:#17333a}@media(max-width:700px){.usage-card{display:block}.lease-box{margin-top:15px}.strategy-grid{grid-template-columns:1fr}}.toast{position:fixed;right:24px;bottom:24px;background:#078d78;color:#fff;padding:12px 18px;border-radius:6px;opacity:0;transform:translateY(8px);pointer-events:none;transition:.2s;z-index:20}.toast.show{opacity:1;transform:none}.hidden{display:none}


/* ========== 移动端精致设计 v2 ========== */
@media(max-width:768px){
  /* === 全局移动端重置 === */
  *{box-sizing:border-box!important;}
  body{overflow-x:hidden!important;}
  main{margin-left:0!important;padding:12px!important;padding-top:70px!important;}
  .top{padding:10px 12px!important;margin-bottom:12px!important;}
  .top h1{font-size:18px!important;}
  .panel{padding:14px!important;margin-bottom:12px!important;border-radius:12px!important;}
  .toolbar{flex-wrap:wrap!important;gap:8px!important;}
  .toolbar input,.toolbar select{flex:1!important;min-width:120px!important;font-size:13px!important;padding:10px!important;}
  .toolbar button{font-size:13px!important;padding:10px 14px!important;border-radius:8px!important;min-height:40px!important;}
  input,select,textarea{font-size:14px!important;padding:10px!important;border-radius:8px!important;}
  button{font-size:13px!important;padding:10px 14px!important;border-radius:8px!important;min-height:40px!important;}
  table{font-size:12px!important;}
  th,td{padding:8px 6px!important;font-size:12px!important;}
  .grid{grid-template-columns:1fr!important;gap:10px!important;}
  .stat{padding:12px!important;border-radius:10px!important;}
  .stat b{font-size:20px!important;}
  /* 确保背景不遮挡内容 */
  body::before{background:rgba(15,20,25,0.9)!important;}

  /* === 全局 === */
  *{
    -webkit-tap-highlight-color:transparent;
    -webkit-touch-callout:none;
    box-sizing:border-box;
  }
  html{font-size:14px;}
  body{
    overflow-x:hidden!important;
    font-size:14px!important;
    background:#0a0e12!important;
  }
  
  /* === 顶部导航 - 深色高级感 === */
  aside{
    position:fixed!important;
    top:0!important;left:0!important;right:0!important;bottom:auto!important;
    width:100%!important;height:54px!important;
    padding:0!important;
    background:linear-gradient(180deg,rgba(12,18,24,0.98),rgba(15,22,28,0.96))!important;
    border-right:0!important;
    border-bottom:1px solid rgba(45,70,75,0.4)!important;
    display:flex!important;flex-direction:row!important;align-items:center!important;
    overflow-x:auto!important;overflow-y:hidden!important;
    z-index:9999!important;white-space:nowrap!important;
    box-shadow:0 2px 16px rgba(0,0,0,0.4)!important;
  }
  aside::-webkit-scrollbar{display:none!important;}
  aside .logo{
    font-size:14px!important;font-weight:700!important;
    padding:0 14px!important;flex-shrink:0!important;
    color:#5fd4c4!important;
    height:54px!important;display:flex!important;align-items:center!important;
    letter-spacing:0.5px!important;
  }
  aside .logo-sub{display:none!important;}
  aside nav{
    display:flex!important;flex-direction:row!important;gap:2px!important;
    overflow-x:auto!important;padding:0 6px!important;
    height:100%!important;align-items:center!important;
  }
  aside nav::-webkit-scrollbar{display:none!important;}
  aside nav button{
    padding:0 12px!important;height:38px!important;
    font-size:12px!important;font-weight:500!important;
    flex-shrink:0!important;border-radius:9px!important;
    border:1px solid transparent!important;
    background:transparent!important;color:#6b8a8f!important;
    transition:all 0.2s ease!important;
    display:flex!important;align-items:center!important;gap:4px!important;
  }
  aside nav button.active{
    background:rgba(45,110,100,0.35)!important;
    color:#5fd4c4!important;
    border-color:rgba(95,212,196,0.25)!important;
  }
  aside nav button:active{transform:scale(0.95)!important;}
  aside nav button span{display:none!important;}
  
  /* === 主内容区 === */
  main{
    margin-left:0!important;
    padding:68px 12px 24px 12px!important;
    min-height:100vh!important;
    max-width:100%!important;
    overflow-x:hidden!important;
  }
  
  /* === 页头 === */
  .page-header{
    flex-wrap:wrap!important;gap:8px!important;
    margin-bottom:14px!important;align-items:center!important;
  }
  .page-header h1{
    font-size:18px!important;font-weight:700!important;
    color:#e0f0f0!important;
  }
  .proxy-info{
    font-size:10px!important;padding:5px 10px!important;
    border-radius:12px!important;
    background:rgba(45,110,100,0.15)!important;
    border:1px solid rgba(95,212,196,0.2)!important;
    color:#5fd4c4!important;
  }
  
  /* === 面板 === */
  .panel{
    padding:14px!important;margin-bottom:12px!important;
    border-radius:14px!important;
    background:rgba(18,24,30,0.92)!important;
    border:1px solid rgba(50,70,75,0.35)!important;
    box-shadow:0 4px 20px rgba(0,0,0,0.25)!important;
  }
  
  /* === 分镜进度栏 === */
  .story-progress{
    font-size:11px!important;padding:10px 12px!important;
    border-radius:10px!important;
    background:rgba(30,40,45,0.7)!important;
    border:1px solid rgba(60,80,85,0.3)!important;
    margin-bottom:12px!important;
    line-height:1.9!important;color:#9ab8bd!important;
  }
  .story-progress strong{color:#5fd4c4!important;}
  
  /* === 工具栏 === */
  .toolbar{
    flex-wrap:wrap!important;gap:8px!important;
    margin-bottom:12px!important;align-items:center!important;
  }
  .toolbar input,.toolbar select{
    font-size:13px!important;padding:9px 12px!important;
    border-radius:9px!important;
    background:rgba(25,32,38,0.9)!important;
    border:1px solid rgba(65,85,90,0.4)!important;
    color:#d0e0e0!important;min-height:38px!important;
  }
  .toolbar input:focus,.toolbar select:focus{
    outline:none!important;
    border-color:rgba(95,212,196,0.5)!important;
    box-shadow:0 0 0 3px rgba(95,212,196,0.1)!important;
  }
  .toolbar button{
    font-size:12px!important;padding:9px 14px!important;
    border-radius:9px!important;min-height:38px!important;
    font-weight:500!important;transition:all 0.2s ease!important;
    background:rgba(35,45,50,0.8)!important;
    border:1px solid rgba(70,90,95,0.4)!important;
    color:#b0c8cc!important;
  }
  .toolbar button:active{transform:scale(0.96)!important;}
  .toolbar button.primary{
    background:linear-gradient(135deg,#2d8a7a,#1a6a7a)!important;
    color:#e8f8f5!important;border:none!important;
    box-shadow:0 3px 12px rgba(45,138,122,0.3)!important;
  }
  
  /* === 统计卡片 === */
  .stats-grid{
    grid-template-columns:1fr 1fr!important;
    gap:8px!important;margin-bottom:12px!important;
  }
  .stat-card{
    padding:12px!important;border-radius:12px!important;
    background:rgba(22,30,35,0.9)!important;
    border:1px solid rgba(55,75,80,0.35)!important;
    text-align:center!important;
  }
  .stat-card .stat-num{
    font-size:22px!important;font-weight:700!important;
    color:#5fd4c4!important;
  }
  .stat-card .stat-label{
    font-size:10px!important;color:#6b8a8f!important;
    margin-top:3px!important;
  }
  
  /* === 分镜卡片 - 关键修复 === */
  .scene-card{
    padding:14px!important;margin-bottom:12px!important;
    border-radius:14px!important;
    background:rgba(20,28,32,0.92)!important;
    border:1px solid rgba(55,75,80,0.35)!important;
    overflow:hidden!important;
  }
  .scene-grid{
    grid-template-columns:1fr!important;
    gap:14px!important;
    width:100%!important;
  }
  .scene-col{
    min-width:0!important;width:100%!important;
  }
  .scene-col textarea,
  .scene-grid textarea{
    font-size:13px!important;
    height:auto!important;
    min-height:120px!important;
    max-height:none!important;
    padding:12px!important;border-radius:10px!important;
    background:rgba(25,32,38,0.9)!important;
    border:1px solid rgba(65,85,90,0.4)!important;
    color:#d0e0e0!important;line-height:1.7!important;
    resize:vertical!important;width:100%!important;
    display:block!important;
    box-sizing:border-box!important;
  }
  .scene-col textarea:focus{
    outline:none!important;
    border-color:rgba(95,212,196,0.5)!important;
    box-shadow:0 0 0 3px rgba(95,212,196,0.1)!important;
  }
  .scene-col label{
    font-size:11px!important;font-weight:600!important;
    color:#7a9a9f!important;margin-bottom:6px!important;
    display:block!important;
    text-transform:uppercase!important;letter-spacing:0.5px!important;
  }
  .prompt-head{
    display:flex!important;
    flex-wrap:wrap!important;
    gap:8px!important;
    margin-bottom:8px!important;
    align-items:center!important;
  }
  .prompt-head label{
    margin-bottom:0!important;
    flex:1!important;
    min-width:100px!important;
  }
  .prompt-head > div{
    display:flex!important;
    gap:4px!important;
    flex-wrap:wrap!important;
  }
  .prompt-head button{
    font-size:11px!important;
    padding:5px 10px!important;
    border-radius:8px!important;
    min-height:30px!important;
  }
  
  /* === 分镜顶部 === */
  .scene-top{
    flex-wrap:wrap!important;gap:8px!important;
    margin-bottom:14px!important;padding-bottom:12px!important;
    border-bottom:1px solid rgba(55,75,80,0.25)!important;
    align-items:center!important;
  }
  .scene-top input{
    font-size:14px!important;padding:8px 12px!important;
    border-radius:9px!important;
    background:rgba(25,32,38,0.9)!important;
    border:1px solid rgba(65,85,90,0.4)!important;
    color:#e0f0f0!important;font-weight:600!important;
    flex:1!important;min-width:120px!important;
  }
  .scene-top button{
    font-size:12px!important;padding:8px 12px!important;
    border-radius:9px!important;min-height:36px!important;
    font-weight:500!important;
    background:rgba(35,45,50,0.8)!important;
    border:1px solid rgba(70,90,95,0.4)!important;
    color:#b0c8cc!important;
  }
  .scene-top button.primary{
    background:linear-gradient(135deg,#2d8a7a,#1a6a7a)!important;
    color:#e8f8f5!important;border:none!important;
  }
  
  /* === 视频提示词区域按钮组 === */
  .scene-col .prompt-actions{
    display:flex!important;flex-wrap:wrap!important;
    gap:6px!important;margin-top:8px!important;
  }
  .scene-col .prompt-actions button{
    font-size:11px!important;padding:6px 10px!important;
    border-radius:8px!important;min-height:32px!important;
    background:rgba(35,45,50,0.8)!important;
    border:1px solid rgba(70,90,95,0.4)!important;
    color:#b0c8cc!important;
  }
  
  /* === 参数选择 === */
  .param-group{
    gap:8px!important;flex-wrap:wrap!important;
    margin-top:10px!important;
  }
  .param-group select{
    font-size:12px!important;padding:8px 10px!important;
    border-radius:9px!important;
    background:rgba(25,32,38,0.9)!important;
    border:1px solid rgba(65,85,90,0.4)!important;
    color:#d0e0e0!important;min-height:36px!important;
  }
  
  /* === 参考图上传区 === */
  .ref-images{
    gap:8px!important;margin-top:10px!important;
    flex-wrap:wrap!important;
  }
  .ref-image-item{
    width:60px!important;height:60px!important;
    border-radius:10px!important;overflow:hidden!important;
    border:1px solid rgba(65,85,90,0.4)!important;
  }
  .upload-ref-btn{
    width:60px!important;height:60px!important;
    border-radius:10px!important;font-size:10px!important;
    background:rgba(25,32,38,0.9)!important;
    border:1px dashed rgba(70,90,95,0.5)!important;
    color:#6b8a8f!important;
    display:flex!important;flex-direction:column!important;
    align-items:center!important;justify-content:center!important;
    gap:4px!important;
  }
  
  /* === 任务列表 === */
  .task-table{font-size:12px!important;width:100%!important;}
  .task-table th,.task-table td{
    padding:10px 8px!important;vertical-align:top!important;
  }
  .task-id{
    font-size:11px!important;word-break:break-all!important;
    font-family:monospace!important;color:#7a9a9f!important;
  }
  .task-prompt{
    font-size:11px!important;max-width:130px!important;
    line-height:1.5!important;color:#b0c8cc!important;
  }
  
  /* === 进度条 === */
  .progress-bar{
    height:8px!important;border-radius:4px!important;
    background:rgba(50,70,75,0.4)!important;overflow:hidden!important;
    margin-top:6px!important;
  }
  .progress-bar .progress-fill{
    height:100%!important;
    background:linear-gradient(90deg,#2d8a7a,#1a6a7a)!important;
    border-radius:4px!important;transition:width 0.3s ease!important;
  }
  
  /* === 按钮组 === */
  .btn-group{
    flex-wrap:wrap!important;gap:6px!important;
    margin-top:8px!important;
  }
  .btn-group button{
    font-size:11px!important;padding:6px 10px!important;
    border-radius:8px!important;min-height:32px!important;
    background:rgba(35,45,50,0.8)!important;
    border:1px solid rgba(70,90,95,0.4)!important;
    color:#b0c8cc!important;
  }
  
  /* === 视频预览区 === */
  .video-preview{
    min-height:160px!important;border-radius:10px!important;
    background:rgba(15,20,25,0.8)!important;
    border:1px solid rgba(50,70,75,0.3)!important;
    display:flex!important;align-items:center!important;
    justify-content:center!important;margin-top:10px!important;
    overflow:hidden!important;
  }
  .video-preview video{
    max-height:220px!important;border-radius:10px!important;
    width:100%!important;object-fit:contain!important;
  }
  
  /* === 表单元素 === */
  input,select,textarea,button{font-size:14px!important;}
  
  /* === 标题 === */
  h1,h2,h3{font-size:16px!important;font-weight:700!important;color:#e0f0f0!important;}
  
  /* === 模态框 === */
  .modal{
    padding:20px!important;margin:10px!important;
    max-width:calc(100vw - 20px)!important;
    max-height:calc(100vh - 20px)!important;
    overflow-y:auto!important;border-radius:18px!important;
    background:rgba(18,24,30,0.98)!important;
  }
  
  /* === 表格横向滚动 === */
  .table-wrap{
    overflow-x:auto!important;-webkit-overflow-scrolling:touch!important;
  }
  
  /* === 复选框 === */
  input[type="checkbox"]{
    width:18px!important;height:18px!important;
    accent-color:#2d8a7a!important;
  }
  
  /* === 滚动条 === */
  ::-webkit-scrollbar{width:4px!important;height:4px!important;}
  ::-webkit-scrollbar-track{background:transparent!important;}
  ::-webkit-scrollbar-thumb{background:rgba(95,212,196,0.3)!important;border-radius:2px!important;}
  
  /* ============================================
     === 强制任务表格转卡片布局 - 最高优先级 ===
     ============================================ */
  #taskTableContainer{overflow:visible!important;}
  #taskTableContainer > div{overflow:visible!important;}
  table.task-table,
  #taskTableContainer table{
    display:block!important;
    width:100%!important;
    border:none!important;
    background:transparent!important;
  }
  table.task-table thead,
  #taskTableContainer table thead{
    display:none!important;
  }
  table.task-table tbody,
  #taskTableContainer table tbody{
    display:block!important;
  }
  table.task-table tr,
  #taskTableContainer table tr{
    display:block!important;
    background:rgba(20,28,32,0.95)!important;
    border:1px solid rgba(65,85,90,0.4)!important;
    border-radius:14px!important;
    padding:14px!important;
    margin-bottom:12px!important;
    box-shadow:0 4px 16px rgba(0,0,0,0.3)!important;
    position:relative!important;
  }
  table.task-table td,
  #taskTableContainer table td{
    display:block!important;
    width:100%!important;
    border:none!important;
    padding:6px 0!important;
    font-size:13px!important;
    color:#c8d8d8!important;
    position:relative!important;
    padding-left:75px!important;
    min-height:22px!important;
    line-height:1.6!important;
    word-break:break-word!important;
  }
  table.task-table td::before,
  #taskTableContainer table td::before{
    position:absolute!important;
    left:0!important;
    top:6px!important;
    width:70px!important;
    font-size:10px!important;
    font-weight:700!important;
    color:#5a8a8f!important;
    text-transform:uppercase!important;
    letter-spacing:0.5px!important;
  }
  table.task-table td:nth-child(1)::before,
  #taskTableContainer table td:nth-child(1)::before{content:"任务"!important;}
  table.task-table td:nth-child(2)::before,
  #taskTableContainer table td:nth-child(2)::before{content:"状态"!important;}
  table.task-table td:nth-child(3)::before,
  #taskTableContainer table td:nth-child(3)::before{content:"参数"!important;}
  table.task-table td:nth-child(4)::before,
  #taskTableContainer table td:nth-child(4)::before{content:"账号"!important;}
  table.task-table td:nth-child(5)::before,
  #taskTableContainer table td:nth-child(5)::before{content:"操作"!important;}
  table.task-table td:nth-child(6)::before,
  #taskTableContainer table td:nth-child(6)::before{content:"时间"!important;}
  /* 第一列（任务ID+提示词）特殊处理 */
  table.task-table td:first-child,
  #taskTableContainer table td:first-child{
    padding-left:0!important;
    margin-bottom:4px!important;
    padding-bottom:10px!important;
    border-bottom:1px solid rgba(65,85,90,0.2)!important;
  }
  table.task-table td:first-child::before,
  #taskTableContainer table td:first-child::before{
    display:none!important;
  }
  /* 最后一列（操作）特殊处理 */
  table.task-table td:last-child,
  #taskTableContainer table td:last-child{
    padding-left:0!important;
    margin-top:8px!important;
    padding-top:10px!important;
    border-top:1px solid rgba(65,85,90,0.2)!important;
  }
  table.task-table td:last-child::before,
  #taskTableContainer table td:last-child::before{
    display:none!important;
  }
  /* 进度条 */
  table.task-table .progress-bar,
  #taskTableContainer .progress-bar{
    width:100%!important;
    height:10px!important;
    margin:8px 0!important;
    border-radius:5px!important;
  }
  /* 操作按钮 */
  table.task-table button,
  #taskTableContainer button,
  table.task-table a,
  #taskTableContainer a{
    font-size:12px!important;
    padding:8px 14px!important;
    border-radius:8px!important;
    min-height:36px!important;
    display:inline-flex!important;
    align-items:center!important;
    justify-content:center!important;
    margin:2px!important;
  }
  /* 状态标签 */
  table.task-table span[style*="background"],
  #taskTableContainer span[style*="background"]{
    display:inline-block!important;
    padding:4px 10px!important;
    border-radius:6px!important;
    font-size:11px!important;
    font-weight:600!important;
  }
  
  /* ============================================
     === 全面移动端交互优化 ===
     ============================================ */
  /* 所有按钮最小44px，方便手指点击 */
  button, input[type="button"], input[type="submit"]{
    min-height:44px!important;
    font-size:14px!important;
    padding:10px 16px!important;
    border-radius:10px!important;
    font-weight:500!important;
  }
  button:active{
    transform:scale(0.96)!important;
    opacity:0.8!important;
  }
  /* 所有输入框 */
  input, select, textarea{
    font-size:15px!important;
    padding:12px!important;
    border-radius:10px!important;
    min-height:44px!important;
  }
  textarea{
    min-height:100px!important;
    line-height:1.7!important;
  }
  /* 防止iOS输入缩放 */
  input, select, textarea{
    font-size:16px!important;
  }
  
  /* ============================================
     === 生成页面优化 ===
     ============================================ */
  .scene-card header{
    flex-wrap:wrap!important;
    gap:8px!important;
  }
  .scene-card .scene-actions{
    width:100%!important;
    display:flex!important;
    gap:6px!important;
    flex-wrap:wrap!important;
    margin-top:8px!important;
  }
  .scene-card .scene-actions button{
    flex:1!important;
    min-width:80px!important;
  }
  .scene-no{
    font-size:28px!important;
  }
  
  /* ============================================
     === 统计卡片优化 ===
     ============================================ */
  .stat, .stat-card{
    padding:14px!important;
    border-radius:12px!important;
    text-align:center!important;
  }
  .stat b, .stat-card .stat-num{
    font-size:24px!important;
    font-weight:700!important;
  }
  
  /* ============================================
     === 全局颜色优化 ===
     ============================================ */
  body{
    background:#0a0e12!important;
    color:#d0e0e0!important;
  }
  .panel, .work, .scene-card, .story-top{
    background:rgba(18,24,30,0.94)!important;
    border:1px solid rgba(55,75,80,0.4)!important;
  }
  /* 主色调 - 青绿色 */
  button.primary{
    background:linear-gradient(135deg,#2d8a7a,#1a6a7a)!important;
    color:#fff!important;
    border:none!important;
    box-shadow:0 4px 14px rgba(45,138,122,0.35)!important;
  }
  /* 文字颜色 */
  h1,h2,h3,h4{
    color:#e8f4f4!important;
  }
  .muted{
    color:#7a8a8d!important;
  }
  
  /* ============================================
     === 视频播放模态框优化 ===
     ============================================ */
  .video-modal, #videoModal{
    padding:0!important;
    margin:0!important;
    max-width:100vw!important;
    max-height:100vh!important;
    width:100vw!important;
    height:100vh!important;
    border-radius:0!important;
  }
  .video-modal > div, #videoModal > div{
    width:100%!important;
    max-width:100%!important;
    border-radius:0!important;
    padding:16px!important;
  }
  #videoPlayer{
    max-height:70vh!important;
  }
  ::-webkit-scrollbar-thumb{background:rgba(95,212,196,0.25)!important;border-radius:2px!important;}
}

/* === 小屏手机 === */
@media(max-width:400px){
  main{padding:64px 10px 20px 10px!important;}
  .panel{padding:12px!important;border-radius:12px!important;}
  .stats-grid{grid-template-columns:1fr 1fr!important;gap:6px!important;}
  .stat-card .stat-num{font-size:20px!important;}
  .scene-card{padding:12px!important;}
  aside nav button{padding:0 10px!important;font-size:11px!important;}
  .page-header h1{font-size:17px!important;}
}
</style></head><body><aside><h2>▣ Dola Pool<small style="display:block;font-size:10px;color:#7fb8ad">OPERATIONS</small></h2><nav id="nav"> <button data-page="generate">✧　生成</button><button data-page="node-canvas" onclick="location.href='/static/node-canvas.html'" style="color:#f5a623">⚡　节点画布</button><button data-page="accounts">♧　账号</button><button data-page="proxies">♧　代理池</button><button data-page="import">⇧　导入</button><button data-page="assets">◈　资产库</button><button data-page="tasks">☷　任务</button><button data-page="keys">⚿　密钥</button><button data-page="settings">☷　设置</button><button data-page="errors">◉　错误</button><button data-page="audits">◴　审计</button></nav></aside><main><div class="top"><h1 id="title">生成</h1><span class="muted">租约 <b id="lease">未连接</b></span></div><section id="content"></section></main><div class="modal" id="modal"><div><h2>导入账号</h2><p class="muted">Cookie，每行一个</p><textarea id="cookies" placeholder="sessionid=...; sid_guard=..." style="width:100%;height:170px"></textarea><p><input type="file" id="file" accept=".txt,.csv,.json"></p><div style="text-align:right"><button onclick="closeModal()">取消</button> <button class="primary" onclick="doImport()">⇧ 开始导入</button></div></div></div><div class="video-modal" id="videoModal" style="position:fixed;inset:0;background:rgba(0,0,0,0.85);display:none;align-items:center;justify-content:center;z-index:300"><div style="background:#1a1f20;border-radius:12px;padding:20px;width:80%;max-width:900px"><button onclick="closeVideoModal()" style="float:right;background:none;border:0;color:#8a9a9d;font-size:20px;cursor:pointer;margin-bottom:10px">×</button><h3 style="margin:0 0 12px;color:#fff" id="videoModalTitle">视频播放</h3><video id="videoPlayer" controls style="width:100%;border-radius:8px;background:#000"></video></div></div><script>
function esc(x){return String(x??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))} const C=document.getElementById('content'), T=document.getElementById('title'); let page='generate';
async function api(path,opt){let r=await fetch('/api/'+path,opt);return r.json()}
function nav(){document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{page=b.dataset.page;T.textContent=b.textContent.trim();render()})}
async function render(){document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.page==page));if(page==='generate')return gen(); if(page==='import')return importPage(); if(page==='assets')return assetsPage(); if(page==='node-canvas'||page==='node'||page==='canvas'||(typeof page!=='undefined'&&page&&page.indexOf('node')>=0)){location.href='/static/node-canvas.html';return;} let data=await api(page==='keys'?'api_keys':page); let heads=data[0]?Object.keys(data[0]):[]; C.innerHTML='<div class="panel"><div class="toolbar"><input placeholder="搜索" id="search" oninput="filterTable()"><button id="refresh" onclick="render()" style="padding:9px 14px;background:#2a4a5a;color:#7bd1be;border:1px solid #3a6a6a;border-radius:4px;cursor:pointer">↻ 刷新</button><button id="clearBtn" onclick="clearFilters()" style="padding:9px 14px;background:#4a2428;color:#ff9b98;border:1px solid #6a3438;border-radius:4px;cursor:pointer">✕ 清空</button><button class="primary" onclick="addRow()">+ 新增</button></div><table><thead><tr>'+heads.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+data.map(r=>'<tr>'+heads.map(h=>'<td>'+((r[h]??'')+'').replaceAll('<','&lt;')+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>'}
function gen(){
 const saved=JSON.parse(localStorage.getItem('dola_story')||'null'); const defaultScenes=[{title:'镜头_1',script:'这里放这个镜头的原始分镜文案',prompt:'二牛和一只小猫在跑步',duration:15,status:'ready'},{title:'镜头_2',script:'这里放这个镜头的原始分镜文案',prompt:'沙漠中远处的队伍缓慢前行',duration:15,status:'ready'}]; const finalSaved=(saved&&saved.length>0)?saved:defaultScenes;
 localStorage.setItem('dola_story',JSON.stringify(finalSaved));
 C.innerHTML='<div class="story-top"><div><b>分镜进度</b> <span id="storyProgress">1/'+finalSaved.length+'</span>　<span class="badge">运行中 0</span> <button onclick="topAction(\'timeout\')">搜查超时</button> <button onclick="topAction(\'bind\')">识别绑定</button> <button class="active" onclick="topAction(\'mode\')">视频模式</button> <button onclick="topAction(\'clear\')">清除</button> <button onclick="importStoryboard()">导入分镜</button> <button onclick="aiReasonAll()" style="background:#6366f1;color:#fff;border:0;border-radius:4px;padding:6px 12px;font-size:12px;cursor:pointer">✨ AI全推理</button></div><div><select id="storyModel"><option>官方SD2.5 (30秒)</option><option>SD2.0 (15秒)</option><option>SD2.0 (10秒)</option></select><select id="accountSelect" style="font-size:12px;padding:5px"><option value="">智能选号</option></select><label style="font-size:11px;color:#9aabad;cursor:pointer"><input type="checkbox" id="watermarkToggle" checked style="vertical-align:middle"> 去水印</label><button onclick="searchReplace()" style="font-size:11px;padding:5px 8px;background:#3a484b;color:#b8c9cc;border:0;border-radius:4px;cursor:pointer">搜索替换</button><button onclick="filterFailed()" style="font-size:11px;padding:5px 8px;background:#3a484b;color:#b8c9cc;border:0;border-radius:4px;cursor:pointer">筛选失败</button><button onclick="openParams()">参数</button><button class="primary" onclick="batchGenerate()">批量生成</button><button onclick="exportStory()">导出剪辑</button></div></div><div id="sceneList"></div><button class="add-scene" onclick="newScene()">＋ 新增镜头</button>';
 renderScenes();
 loadAccounts();Promise.all(['accounts','tasks','proxies'].map(x=>api(x))).then(([a,t,p])=>{
  const q=document.getElementById('storyProgress');
  if(q){
    const running = t.filter(x=>x.status==='running').length;
    const completed = t.filter(x=>x.status==='completed').length;
    const failed = t.filter(x=>x.status==='failed').length;
    q.textContent='镜头 '+saved.length+' 个 | 账号 '+a.length+' 个 | 运行中 '+running+' | 完成 '+completed+' | 失败 '+failed;
  }
});
}
function toast(msg){let t=document.getElementById('toast');if(!t){t=document.createElement('div');t.id='toast';t.className='toast';document.body.appendChild(t)}t.textContent=msg;t.classList.add('show');clearTimeout(window.__toast);window.__toast=setTimeout(()=>t.classList.remove('show'),2600)}
function topAction(kind){if(kind==='timeout'){toast('已检查当前镜头：没有超时任务');return}if(kind==='bind'){const x=story();x.forEach(s=>s.bound=true);persistStory(x);renderScenes();toast('镜头识别绑定已完成');return}if(kind==='mode'){toast('当前为视频模式');return}if(kind==='clear'){if(!confirm('清空全部本地分镜？'))return;localStorage.removeItem('dola_story');gen();toast('已清空分镜');}}
function sceneMore(i){const x=story()[i];toast('镜头 '+(i+1)+'：状态 '+(x.status||'ready')+'，时长 '+x.duration+' 秒')}
function openParams(){const model=document.getElementById('storyModel')?.value||'sd-2.5-30s';const x=prompt('当前模型：'+model+'\n输入默认画质（720p/1080p）：','720p');if(x){localStorage.setItem('dola_quality',x);toast('参数已保存：'+x)}}
function importStoryboard(){
 const input=document.createElement('input'); input.type='file'; input.accept='.json,.txt';
 input.onchange=()=>{const f=input.files[0];if(!f)return;const reader=new FileReader();
  reader.onload=()=>{let data=[];try{data=JSON.parse(reader.result);if(!Array.isArray(data))data=[data]}catch(e){data=String(reader.result).split(/\r?\n/).filter(Boolean).map((prompt,i)=>({title:'镜头_'+(i+1),script:'',prompt,duration:15,status:'ready'}))}
   data=data.map((x,i)=>({title:x.title||'镜头_'+(i+1),script:x.script||'',prompt:x.prompt||'',duration:Number(x.duration)||15,status:x.status||'ready'}));persistStory(data);gen();toast('已导入 '+data.length+' 个镜头');
  }; reader.readAsText(f,'utf-8');
 }; input.click();
}

function story(){try{return JSON.parse(localStorage.getItem('dola_story')||'[]')}catch(e){return []}}
function persistStory(d){localStorage.setItem('dola_story',JSON.stringify(d))}
function closeModal(){document.getElementById('modal').style.display='none'}
function newScene(){const x=story();x.push({title:'镜头_'+(x.length+1),script:'',prompt:'',duration:15,status:'ready'});persistStory(x);gen();toast('已新增镜头 '+(x.length))}
function updateScene(i,field,value){const x=story();if(!x[i])return;x[i][field]=value;persistStory(x)}
function deleteScene(i){if(!confirm('删除这个镜头？'))return;const x=story();x.splice(i,1);x.forEach((s,idx)=>s.title='镜头_'+(idx+1));persistStory(x);gen();toast('已删除')}
function renderScenes(){
  const list=document.getElementById('sceneList');
  if(!list)return;
  let x=story();
  if(window._failOnly){x=x.filter(function(s){return s.status==='failed'})}
  if(!x.length){list.innerHTML='<div style="text-align:center;padding:40px;color:#6a7a7d">'+(window._failOnly?'没有失败的镜头':'暂无镜头，点击下方按钮添加')+'</div>';return}
  list.innerHTML=x.map((s,i)=>{setTimeout(function(){renderRefImages(i)},50);
    const statusColor={ready:'#7be2ad',running:'#7bc8e2',completed:'#7be2ad',failed:'#ff9b98'}[s.status]||'#8a9a9d';
    return '<div class="scene-card"><header><span class="scene-no">'+(i+1)+'</span><div><input value="'+esc(s.title||'')+'" oninput="updateScene('+i+',\'title\',this.value)" style="width:160px;background:#111;color:#eee;border:1px solid #303b3e;border-radius:4px;padding:5px"><div style="font-size:11px;color:'+statusColor+';margin-top:4px">状态: '+(s.status||'ready')+(s.progress?' · '+s.progress+'%':'')+'</div></div><div class="scene-actions"><button onclick="generateSingle('+i+')" class="primary" style="padding:6px 12px;font-size:12px">生成</button><button onclick="sceneMore('+i+')" style="padding:6px 10px;font-size:12px;background:#2a3538;color:#b8c9cc;border:1px solid #3a484b;border-radius:4px;cursor:pointer">⋯</button><button onclick="deleteScene('+i+')" class="danger" style="padding:6px 10px;font-size:12px;background:transparent;border:0;cursor:pointer">✕</button></div></header><div class="scene-grid"><section><label>原始分镜文案</label><textarea oninput="updateScene('+i+',\'script\',this.value)" placeholder="这里放这个镜头的原始分镜文案">'+esc(s.script||'')+'</textarea></section><section><div class="prompt-head"><label>视频提示词（投喂模型）</label><div><button onclick="aiReasonScene('+i+')" style="font-size:11px;padding:4px 8px;background:#078d78;color:#fff;border:0;border-radius:4px;cursor:pointer">AI推理</button><button onclick="openAssetPicker('+i+')" style="font-size:11px;padding:4px 8px;background:#6366f1;color:#fff;border:0;border-radius:4px;cursor:pointer;margin-left:4px">@资产</button><button onclick="clearPrompt('+i+')" style="font-size:11px;padding:4px 8px;background:#3a484b;color:#b8c9cc;border:0;border-radius:4px;cursor:pointer;margin-left:4px">清空</button></div></div><textarea oninput="updateScene('+i+',\'prompt\',this.value)" placeholder="详细的视频生成提示词...">'+esc(s.prompt||'')+'</textarea></section><section><label>参数与预览</label><div style="display:flex;gap:8px;margin-bottom:10px"><select onchange="updateScene('+i+',\'duration\',Number(this.value))" style="flex:1">'+[5,10,15,30].map(d=>'<option value="'+d+'"'+(Number(s.duration)===d?' selected':'')+'>'+d+'秒</option>').join('')+'</select><select onchange="updateScene('+i+',\'ratio\',this.value)" style="flex:1">'+['9:16','16:9','1:1'].map(r=>'<option value="'+r+'"'+(s.ratio===r?' selected':'')+'>'+r+'</option>').join('')+'</select></div><div style="margin-bottom:8px"><label style="font-size:11px;color:#9aabad;display:block;margin-bottom:4px">参考图（最多9张）</label><div id="refImages_'+i+'" style="display:flex;gap:4px;flex-wrap:wrap;min-height:36px"></div><input type="file" id="refInput_'+i+'" accept="image/*" multiple style="display:none" onchange="uploadRefImages('+i+',this.files)"><button onclick="document.getElementById(&quot;refInput_'+i+'&quot;).click()" style="font-size:10px;padding:3px 8px;background:#2a3538;color:#7bd1be;border:1px dashed #347d6f;border-radius:4px;cursor:pointer;margin-top:4px">+ 上传参考图</button></div><div class="video-preview"><div class="preview-art">'+(s.result&&s.result.includes('|')?'<video src="/output/'+s.result.split('|')[0].split('/').pop()+'" controls style="max-width:100%;max-height:100%"></video>':'<span style="color:#5a6a6d;font-size:13px">视频预览区<br>生成后自动显示</span>')+'</div><div class="preview-label">'+(s.status==='completed'?'✅ 已完成':s.status==='running'?'⏳ 生成中...':s.status==='failed'?'❌ 失败: '+esc(s.result||''):'等待生成')+'</div></div></section></div><footer>镜头 '+(i+1)+' / '+x.length+' · 时长 '+(s.duration||15)+'s · 比例 '+(s.ratio||'9:16')+'</footer></div>';
  }).join('');
}
async function generateSingle(i){
  const x=story();
  const s=x[i];
  if(!s||!s.prompt){toast('请先填写提示词');return}
  const model=document.getElementById('storyModel')?.value||'官方SD2.5 (30秒)';
  const durationMap={'官方SD2.5 (30秒)':30,'SD2.0 (15秒)':15,'SD2.0 (10秒)':10};
  const duration=durationMap[model]||Number(s.duration)||15;
  s.status='running';s.progress=0;persistStory(x);renderScenes();
  try{
    const r=await api('tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:s.prompt,duration:duration,ratio:s.ratio||'9:16',model:model.includes('2.5')?'seedance_v2.5':'seedance_v2.0',account_id:document.getElementById('accountSelect')?.value||'',ref_images:s.ref_images||''})});
    if(r.error){throw Error(r.error)}
    s.task_id=r.id;
    persistStory(x);
    toast('任务已提交: '+r.id);
    pollTask(i,r.id);
  }catch(e){
    s.status='failed';s.result=e.message;persistStory(x);renderScenes();
    toast('生成失败: '+e.message);
  }
}
async function pollTask(i,taskId){
  const x=story();
  for(let t=0;t<120;t++){
    await new Promise(r=>setTimeout(r,5000));
    try{
      const tasks=await api('tasks');
      const task=tasks.find(t=>t.id===taskId);
      if(!task)continue;
      x[i].status=task.status;
      x[i].progress=task.progress||0;
      if(task.status==='completed'){
        x[i].result=task.result||'';
        persistStory(x);renderScenes();
        toast('镜头 '+(i+1)+' 生成完成！');
        return;
      }
      if(task.status==='failed'){
        x[i].result=task.result||'生成失败';
        persistStory(x);renderScenes();
        toast('镜头 '+(i+1)+' 生成失败');
        return;
      }
      persistStory(x);renderScenes();
    }catch(e){}
  }
}
async function aiReasonScene(i){
  const x=story();
  const s=x[i];
  if(!s.script&&!s.prompt){toast('请先填写原始文案');return}
  toast('AI推理中...');
  try{
    const r=await api('ai-reason',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({script:s.script||s.prompt,style:'cinematic movie quality',duration:Number(s.duration)||15,image_url:(s.ref_images||'').split(',')[0]||''})});
    if(r.error){throw Error(r.error)}
    if(r.scenes&&r.scenes.length){
      s.prompt=r.scenes[0].prompt||s.prompt;
      persistStory(x);renderScenes();
      toast('AI推理完成！');
    }
  }catch(e){
    toast('AI推理失败: '+e.message);
  }
}

async function aiReasonAll(){
  const x=story();
  if(!x.length){toast('请先添加镜头');return}
  if(!confirm('确定要对全部 '+x.length+' 个镜头进行AI推理吗？'))return
  toast('AI全推理中... (0/'+x.length+')');
  let success=0;
  for(let i=0;i<x.length;i++){
    const s=x[i];
    if(!s.script&&!s.prompt)continue;
    try{
      const r=await api('ai-reason',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({script:s.script||s.prompt,style:'cinematic movie quality',duration:Number(s.duration)||15,image_url:(s.ref_images||'').split(',')[0]||''})});
      if(r.error)continue;
      if(r.scenes&&r.scenes.length){
        x[i].prompt=r.scenes[0].prompt||x[i].prompt;
        success++;
        persistStory(x);
        toast('AI推理中... ('+success+'/'+x.length+')');
      }
    }catch(e){}
  }
  renderScenes();
  toast('AI全推理完成！成功 '+success+'/'+x.length);
}

function clearPrompt(i){const x=story();x[i].prompt='';persistStory(x);renderScenes()}
function searchReplace(){var s=prompt('搜索文本:');if(!s)return;var r=prompt('替换为:')||'';var x=story();var n=0;x.forEach(function(sc){if(sc.script&&sc.script.indexOf(s)>=0){sc.script=sc.script.split(s).join(r);n++}if(sc.prompt&&sc.prompt.indexOf(s)>=0){sc.prompt=sc.prompt.split(s).join(r);n++}});persistStory(x);renderScenes();toast('替换完成，共'+n+'处')}
function filterFailed(){window._failOnly=!window._failOnly;renderScenes();toast(window._failOnly?'只显示失败镜头':'显示全部镜头')}
function loadAccounts(){api('accounts').then(function(a){var s=document.getElementById('accountSelect');if(!s)return;var cur=s.value;s.innerHTML='<option value=>智能选号</option>'+a.filter(function(x){return x.status==='active'||x.status==='normal'}).map(function(x){return '<option value='+x.id+'>账号#'+x.id+'</option>'}).join('');s.value=cur}).catch(function(){})}
function assetsPage(){
  var h = '<div class="panel"><div class="toolbar" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">';
  h += '<select id="assetFilter" style="font-size:13px"><option value="">全部</option><option value="character">人物</option><option value="scene">场景</option><option value="prop">道具</option></select>';
  h += '<button class="primary" onclick="document.getElementById(&quot;assetFileInput&quot;).click()" style="font-size:13px">+ 上传资产</button>';
  h += '<input type="file" id="assetFileInput" accept="image/*" multiple style="display:none" onchange="uploadAssets(this.files)">';
  h += '<button onclick="renderAssets()" style="font-size:13px;padding:9px 14px;background:#2a4a5a;color:#7bd1be;border:1px solid #3a6a6a;border-radius:4px;cursor:pointer">↻ 刷新</button>';
  h += '<button onclick="clearAllAssets()" style="font-size:13px;padding:9px 14px;background:#4a2428;color:#ff9b98;border:1px solid #6a3438;border-radius:4px;cursor:pointer">✕ 清空全部</button>';
  h += '<span id="assetCount" style="color:#7bd1be;font-size:12px;margin-left:auto"></span>';
  h += '</div><div id="assetGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;margin-top:14px"></div></div>';
  C.innerHTML = h;
  document.getElementById("assetFilter").onchange = renderAssets;
  renderAssets();
}
function renderAssets(){
  var filter = document.getElementById("assetFilter") ? document.getElementById("assetFilter").value : "";
  api("assets").then(function(list){
    if(filter) list = list.filter(function(a){return a.type === filter});
    var grid = document.getElementById("assetGrid");
    if(!grid) return;
    if(!list.length){grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:60px;color:#6a7a7d">暂无资产<br><br>点击右上角「+ 上传资产」<br>支持人物/场景/道具分类</div>';return}
    var h = "";
    list.forEach(function(a){
      var img = a.path ? '<img src="'+a.path+'" style="width:100%;height:130px;object-fit:cover">' : '<div style="width:100%;height:130px;background:#0a0c0d;display:flex;align-items:center;justify-content:center;color:#5a6a6d">无图</div>';
      h += '<div style="background:rgba(27,29,30,0.85);border:1px solid rgba(48,55,57,0.5);border-radius:8px;overflow:hidden">';
      h += img;
      h += '<div style="padding:10px">';
      h += '<input value="'+esc(a.name||"")+'" style="width:100%;margin-bottom:5px;font-size:12px;background:#151819;color:#e7eeee;border:1px solid #3a484b;border-radius:3px;padding:4px" placeholder="名字" onblur="updateAssetField('+a.id+',&quot;name&quot;,this.value)">';
      h += '<select style="width:100%;margin-bottom:5px;font-size:11px;background:#151819;color:#e7eeee;border:1px solid #3a484b;border-radius:3px;padding:3px" onchange="updateAssetField('+a.id+',&quot;type&quot;,this.value)">';
      h += '<option value="character"'+(a.type==="character"?" selected":"")+'>人物</option>';
      h += '<option value="scene"'+(a.type==="scene"?" selected":"")+'>场景</option>';
      h += '<option value="prop"'+(a.type==="prop"?" selected":"")+'>道具</option>';
      h += '</select>';
      h += '<textarea placeholder="特征描述（外貌/服装/道具，@引用时插入）" style="width:100%;height:45px;font-size:10px;background:#151819;color:#e7eeee;border:1px solid #3a484b;border-radius:3px;padding:3px;resize:vertical" onblur="updateAssetField('+a.id+',&quot;features&quot;,this.value)">'+esc(a.features||"")+'</textarea>';
      h += '<div style="display:flex;gap:3px;margin-top:6px">';
      h += '<button onclick="insertAtRef(&quot;@'+a.name+' &quot;)" style="flex:1;font-size:10px;padding:3px;background:#078d78;color:#fff;border:0;border-radius:3px;cursor:pointer">@名字</button>';
      h += '<button onclick="insertAtRef(&quot;'+esc(a.features||"")+'&quot;)" style="flex:1;font-size:10px;padding:3px;background:#2a4a5a;color:#7bd1be;border:0;border-radius:3px;cursor:pointer">@特征</button>';
      h += '<button onclick="deleteAsset('+a.id+')" style="font-size:10px;padding:3px;background:#4a2428;color:#ff9b98;border:0;border-radius:3px;cursor:pointer">删</button>';
      h += '</div></div></div>';
    });
    grid.innerHTML = h;
    var countEl = document.getElementById("assetCount");
    if(countEl) countEl.textContent = "共 " + list.length + " 个资产";
  }).catch(function(e){document.getElementById("assetGrid").innerHTML = '<div style="color:#ff9b98">加载失败:'+e.message+'</div>'});
}
function clearAllAssets(){
  if(!confirm("确定要清空全部资产吗？此操作不可恢复！")) return;
  api("assets").then(function(list){
    var done = 0;
    list.forEach(function(a){
      api("assets/"+a.id, {method:"DELETE"}).then(function(){
        done++;
        if(done >= list.length){
          renderAssets();
          toast("已清空全部资产");
        }
      }).catch(function(){});
    });
  }).catch(function(e){toast("清空失败:"+e.message)});
}

async function updateAssetField(id, field, value){
  var body = {}; body[field] = value;
  await api("assets/"+id, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
}
function insertAtRef(text){
  var ta = document.querySelector("textarea[placeholder*=视频]") || document.querySelector("textarea[placeholder*=提示词]") || document.querySelector(".shot-prompt textarea") || document.querySelector("textarea");
  if(!ta){toast("请先在生成页点击提示词框");return}
  var s = ta.selectionStart || 0, e = ta.selectionEnd || 0;
  ta.value = ta.value.substring(0,s) + text + ta.value.substring(e);
  ta.selectionStart = ta.selectionEnd = s + text.length;
  ta.focus();
  toast("已插入@引用");
}

async function uploadAssets(files){
  if(!files || !files.length) return;
  for(var i=0;i<files.length;i++){
    var fd = new FormData(); fd.append("file", files[i]);
    try{
      var r = await fetch("/api/upload-image", {method:"POST", body:fd});
      var d = await r.json();
      if(d.url){
        var name = files[i].name.replace(/\.[^.]+$/, "");
        await api("assets", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:name, type:"character", features:"", path:d.url, filename:files[i].name})});
      }
    }catch(e){console.error(e)}
  }
  renderAssets();
  toast("上传完成");
}

function deleteAsset(id){
  if(!confirm('删除这个资产？'))return;
  api('assets/'+id,{method:'DELETE'}).then(function(){renderAssets();toast('已删除')}).catch(function(e){toast('删除失败:'+e.message)});
}
function openAssetPicker(i){
  window._assetPickerScene=i;
  var modal=document.createElement('div');
  modal.id='assetPickerModal';
  modal.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,0.85);display:flex;align-items:center;justify-content:center;z-index:400';
  modal.innerHTML='<div style="background:#1a1f20;border-radius:12px;padding:20px;width:90%;max-width:800px;max-height:80vh;overflow:auto"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px"><h3 style="margin:0;color:#fff">选择资产（点击添加到参考图）</h3><button onclick="closeAssetPicker()" style="background:none;border:0;color:#8a9a9d;font-size:20px;cursor:pointer">x</button></div><div style="display:flex;gap:8px;margin-bottom:14px"><button onclick="filterAssetsPicker(\'character\')" style="padding:6px 14px;background:#2a3538;color:#b8c9cc;border:1px solid #3a484b;border-radius:4px;cursor:pointer">人物</button><button onclick="filterAssetsPicker(\'scene\')" style="padding:6px 14px;background:#2a3538;color:#b8c9cc;border:1px solid #3a484b;border-radius:4px;cursor:pointer">场景</button><button onclick="filterAssetsPicker(\'prop\')" style="padding:6px 14px;background:#2a3538;color:#b8c9cc;border:1px solid #3a484b;border-radius:4px;cursor:pointer">道具</button><button onclick="filterAssetsPicker(\'\')" style="padding:6px 14px;background:#078d78;color:#fff;border:0;border-radius:4px;cursor:pointer">全部</button></div><div id="assetPickerGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px"></div></div>';
  document.body.appendChild(modal);
  filterAssetsPicker('');
}
function filterAssetsPicker(type){
  api('assets').then(function(list){
    if(type)list=list.filter(function(a){return a.type===type});
    var grid=document.getElementById('assetPickerGrid');
    if(!grid)return;
    grid.innerHTML=list.map(function(a){return '<div onclick="addAssetToScene('+a.id+')" style="cursor:pointer;border:2px solid #3a484b;border-radius:8px;overflow:hidden;transition:border-color .2s" onmouseover="this.style.borderColor=&quot;#078d78&quot;" onmouseout="this.style.borderColor=&quot;#3a484b&quot;"><div style="height:100px;background:#0a0c0d;display:flex;align-items:center;justify-content:center"><img src="'+(a.path||a.url||'')+'" style="max-width:100%;max-height:100%;object-fit:cover"></div><div style="padding:6px;font-size:11px;color:#b8c9cc;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+(a.name||'')+'</div></div>'}).join('')||'<div style="grid-column:1/-1;text-align:center;padding:40px;color:#6a7a7d">暂无资产</div>';
  });
}
function addAssetToScene(assetId){
  api('assets').then(function(list){
    var asset=list.find(function(a){return a.id===assetId});
    if(!asset)return;
    var i=window._assetPickerScene;
    var x=story();
    var s=x[i];
    var refs=(s.ref_images||'').split(',').filter(function(p){return p.trim()});
    if(refs.length>=9){toast('最多9张参考图');return}
    refs.push(asset.path||asset.url||'');
    s.ref_images=refs.join(',');
    persistStory(x);renderScenes();
    toast('已添加资产: '+(asset.name||''));
    closeAssetPicker();
  });
}
function closeAssetPicker(){
  var m=document.getElementById('assetPickerModal');
  if(m)m.remove();
}

function renderRefImages(i){var el=document.getElementById('refImages_'+i);if(!el)return;var x=story();var s=x[i];var refs=(s.ref_images||'').split(',').filter(function(p){return p.trim()});el.innerHTML=refs.map(function(p,idx){return '<div style=position:relative;width:36px;height:36px;border-radius:4px;overflow:hidden;border:1px solid #3a484b><img src='+p+' style=width:100%;height:100%;object-fit:cover onerror=this.style.display=&quot;none&quot;><button onclick=removeRefImage('+i+','+idx+') style=position:absolute;top:0;right:0;background:rgba(0,0,0,0.7);color:#fff;border:0;width:14px;height:14px;font-size:10px;cursor:pointer;line-height:1>x</button></div>'}).join('')}
async function uploadRefImages(i,files){if(!files||!files.length)return;var x=story();var s=x[i];var refs=(s.ref_images||'').split(',').filter(function(p){return p.trim()});for(var j=0;j<files.length;j++){if(refs.length>=9){toast('最多9张参考图');break}var fd=new FormData();fd.append('file',files[j]);try{var r=await fetch('/api/upload-image',{method:'POST',body:fd});var d=await r.json();if(d.url){refs.push(d.url)}}catch(e){toast('上传失败:'+e.message)}}s.ref_images=refs.join(',');persistStory(x);renderScenes()}
function removeRefImage(i,idx){var x=story();var s=x[i];var refs=(s.ref_images||'').split(',').filter(function(p){return p.trim()});refs.splice(idx,1);s.ref_images=refs.join(',');persistStory(x);renderScenes()}


async function batchGenerate(){
  const x=story();
  const model = document.getElementById('storyModel')?.value || 'seedance_v2.0';
  const durationMap = {'sd-2.5-30s':30, 'sd-2.0-15s':15, 'sd-2.0-10s':10};
  const duration = durationMap[model] || 15;
  let success = 0, failed = 0;
  for(let i=0; i<x.length; i++){
    const s = x[i];
    if(s.status === 'completed') continue;
    try {
      const r = await api('tasks', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:s.prompt,model:model,duration:duration,ratio:'16:9',quality:'720p'})});
      if(r.error) { failed++; toast('镜头'+(i+1)+'失败: '+r.error); }
      else { success++; s.status='running'; s.taskId=r.id; persistStory(x); }
    } catch(e) { failed++; toast('镜头'+(i+1)+'错误: '+e.message); }
  }
  renderScenes();
  toast('已提交 '+success+' 个任务'+(failed>0?'，失败 '+failed+' 个':''));
  // 开始轮询任务状态
  pollTasks();
}

let pollTimer = null;
async function pollTasks(){
  if(pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async ()=>{
    const x=story();
    let hasRunning = false;
    for(const s of x){
      if(s.status === 'running' && s.taskId){
        hasRunning = true;
        try {
          const tasks = await api('tasks');
          const t = tasks.find(t=>t.id===s.taskId);
          if(t){
            if(t.status === 'completed'){
              s.status = 'completed';
              s.videoUrl = t.result;
              persistStory(x);
              renderScenes();
              toast('镜头生成完成！');
            } else if(t.status === 'failed'){
              s.status = 'failed';
              s.error = t.result;
              persistStory(x);
              renderScenes();
            }
          }
        } catch(e){}
      }
    }
    if(!hasRunning && pollTimer){ clearInterval(pollTimer); pollTimer=null; }
  }, 5000);
}
function exportStory(){const b=new Blob([JSON.stringify(story(),null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='dola-storyboard.json';a.click()}
function importPage(){C.innerHTML='<div class="panel"><h2>Cookie Import</h2><p class="muted">支持 TXT、CSV、JSON，或直接粘贴 Cookie，每行一个。</p><textarea id="cookies2" style="width:100%;height:220px" placeholder="sessionid=...; sid_guard=..."></textarea><p><input type="file" id="file2" accept=".txt,.csv,.json"></p><button class="primary" onclick="doImport2()">开始导入</button></div>'}
async function createTask(){let r=await api('tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:document.getElementById('prompt').value,model:model.value,duration:Number(duration.value),quality:quality.value,ratio:ratio.value,min_bytes:30000000})});alert(r.error||'任务已创建');render()}
async function doImport2(){let f=file2.files[0],fd=new FormData();fd.append('cookies',cookies2.value);if(f)fd.append('file',f);let r=await fetch('/api/import',{method:'POST',body:fd});alert(JSON.stringify(await r.json()));render()}
function closeModal(){modal.style.display='none'} function addRow(){alert('请使用生成页或导入页的专用表单新增数据')}
</script>
<style>.modal>div{background:#202425;color:#e7eeee;max-width:94vw}button{cursor:pointer;background:#283334;color:#eee;border:1px solid #425354;border-radius:5px;padding:8px}table{font-size:13px}.panel{overflow:auto}td{max-width:340px;overflow-wrap:anywhere}.toolbar{flex-wrap:wrap}select,input{max-width:100%}.empty{padding:48px;text-align:center;color:#92a6a9}

/* ============================================
   === 全新移动端设计 - 最高优先级 ===
   ============================================ */
@media(max-width:768px){
  /* 全局重置 */
  *{box-sizing:border-box!important;-webkit-tap-highlight-color:transparent!important;}
  html{font-size:16px!important;}
  body{
    overflow-x:hidden!important;background:#0d1117!important;font-size:15px!important;
    background-image:none!important;background-color:#0d1117!important;
  }
  body::before{
    background:#0d1117!important;background-image:none!important;
    opacity:1!important;z-index:-1!important;
  }
  body::before{background:rgba(10,14,18,0.95)!important;}
  
  /* 顶部导航 - 固定顶部，横向滚动 */
  aside{
    position:fixed!important;top:0!important;left:0!important;right:0!important;bottom:auto!important;
    width:100%!important;height:56px!important;padding:0!important;
    background:linear-gradient(180deg,#0d141a,#111a22)!important;
    border-right:none!important;border-bottom:1px solid rgba(45,80,85,0.5)!important;
    display:flex!important;flex-direction:row!important;align-items:center!important;
    overflow-x:auto!important;overflow-y:hidden!important;z-index:9999!important;
    box-shadow:0 2px 20px rgba(0,0,0,0.5)!important;
  }
  aside::-webkit-scrollbar{display:none!important;}
  aside h2{
    font-size:15px!important;margin:0 12px!important;padding:0!important;
    color:#5fd4c4!important;flex-shrink:0!important;height:56px!important;
    display:flex!important;align-items:center!important;font-weight:700!important;
  }
  aside h2 small{display:none!important;}
  aside nav{
    display:flex!important;flex-direction:row!important;gap:2px!important;
    overflow-x:auto!important;padding:0 8px!important;height:100%!important;
    align-items:center!important;
  }
  aside nav::-webkit-scrollbar{display:none!important;}
  aside nav button{
    display:inline-flex!important;align-items:center!important;justify-content:center!important;
    width:auto!important;text-align:center!important;padding:0 14px!important;
    height:40px!important;border-radius:10px!important;font-size:13px!important;
    font-weight:500!important;flex-shrink:0!important;
    background:transparent!important;color:#6b8a8f!important;border:1px solid transparent!important;
  }
  aside nav button.active{
    background:rgba(45,138,122,0.3)!important;color:#5fd4c4!important;
    border-color:rgba(95,212,196,0.3)!important;
  }
  
  /* 主内容区 */
  main{
    margin-left:0!important;padding:70px 14px 30px 14px!important;
    min-height:100vh!important;max-width:100%!important;overflow-x:hidden!important;
    background:transparent!important;position:relative!important;z-index:1!important;
  }
  .top{
    padding:12px 0!important;margin-bottom:14px!important;
    border-bottom:1px solid rgba(45,60,65,0.3)!important;
    flex-wrap:wrap!important;gap:8px!important;
  }
  .top h1{font-size:20px!important;font-weight:700!important;color:#e8f4f4!important;}
  .top .muted{font-size:11px!important;}
  .top .muted b{color:#5fd4c4!important;}
  
  /* 面板 */
  .panel{
    padding:16px!important;margin-bottom:14px!important;border-radius:16px!important;
    background:#161d24!important;border:1px solid rgba(70,100,105,0.5)!important;
    box-shadow:0 4px 24px rgba(0,0,0,0.5)!important;overflow:visible!important;
  }
  
  /* 工具栏 */
  .toolbar{
    flex-wrap:wrap!important;gap:8px!important;margin-bottom:14px!important;
  }
  .toolbar input,.toolbar select{
    flex:1!important;min-width:130px!important;font-size:15px!important;
    padding:12px!important;border-radius:12px!important;min-height:48px!important;
    background:rgba(20,28,34,0.9)!important;border:1px solid rgba(60,85,90,0.4)!important;
    color:#d8e8e8!important;
  }
  .toolbar input:focus,.toolbar select:focus{
    outline:none!important;border-color:rgba(95,212,196,0.6)!important;
    box-shadow:0 0 0 3px rgba(95,212,196,0.15)!important;
  }
  .toolbar button{
    font-size:14px!important;padding:12px 18px!important;border-radius:12px!important;
    min-height:48px!important;font-weight:600!important;
    background:rgba(35,48,54,0.9)!important;border:1px solid rgba(70,95,100,0.4)!important;
    color:#b8d0d4!important;
  }
  .toolbar button:active{transform:scale(0.96)!important;}
  .toolbar button.primary{
    background:linear-gradient(135deg,#2d8a7a,#1a6a7a)!important;color:#fff!important;
    border:none!important;box-shadow:0 4px 16px rgba(45,138,122,0.4)!important;
  }
  
  /* 所有按钮 */
  button, input[type="button"], input[type="submit"]{
    min-height:48px!important;font-size:15px!important;padding:12px 18px!important;
    border-radius:12px!important;font-weight:600!important;
  }
  button:active{transform:scale(0.96)!important;opacity:0.85!important;}
  button.primary{
    background:linear-gradient(135deg,#2d8a7a,#1a6a7a)!important;color:#fff!important;
    border:none!important;
  }
  
  /* 所有输入框 */
  input, select, textarea{
    font-size:16px!important;padding:12px!important;border-radius:12px!important;
    min-height:48px!important;background:rgba(20,28,34,0.9)!important;
    border:1px solid rgba(60,85,90,0.4)!important;color:#d8e8e8!important;
  }
  textarea{min-height:120px!important;line-height:1.8!important;resize:vertical!important;}
  input:focus, select:focus, textarea:focus{
    outline:none!important;border-color:rgba(95,212,196,0.6)!important;
    box-shadow:0 0 0 3px rgba(95,212,196,0.15)!important;
  }
  
  /* 统计卡片 */
  .grid{grid-template-columns:1fr 1fr!important;gap:10px!important;}
  .stat{
    padding:16px!important;border-radius:14px!important;text-align:center!important;
    background:rgba(20,30,36,0.9)!important;border:1px solid rgba(50,75,80,0.4)!important;
  }
  .stat b{font-size:26px!important;font-weight:700!important;color:#5fd4c4!important;}
  
  /* ============================================
     === 任务页面 - 完全卡片化 ===
     ============================================ */
  #taskTableContainer{overflow:visible!important;}
  #taskTableContainer > div{overflow:visible!important;}
  #taskTableContainer table,
  table.task-table{
    display:block!important;width:100%!important;border:none!important;background:transparent!important;
  }
  #taskTableContainer thead,
  table.task-table thead{display:none!important;}
  #taskTableContainer tbody,
  table.task-table tbody{display:block!important;}
  #taskTableContainer tr,
  table.task-table tr{
    display:block!important;background:#161d24!important;
    border:1px solid rgba(70,100,105,0.5)!important;border-radius:16px!important;
    padding:16px!important;margin-bottom:14px!important;
    box-shadow:0 4px 20px rgba(0,0,0,0.5)!important;position:relative!important;
  }
  #taskTableContainer td,
  table.task-table td{
    display:block!important;width:100%!important;border:none!important;
    padding:8px 0 8px 80px!important;font-size:14px!important;color:#e0ecec!important;
    position:relative!important;min-height:24px!important;line-height:1.7!important;
    word-break:break-word!important;max-width:100%!important;
  }
  #taskTableContainer td::before,
  table.task-table td::before{
    position:absolute!important;left:0!important;top:8px!important;width:72px!important;
    font-size:11px!important;font-weight:700!important;color:#5a8a8f!important;
    text-transform:uppercase!important;letter-spacing:0.5px!important;
  }
  #taskTableContainer td:nth-child(1)::before,
  table.task-table td:nth-child(1)::before{content:"任务"!important;}
  #taskTableContainer td:nth-child(2)::before,
  table.task-table td:nth-child(2)::before{content:"状态"!important;}
  #taskTableContainer td:nth-child(3)::before,
  table.task-table td:nth-child(3)::before{content:"参数"!important;}
  #taskTableContainer td:nth-child(4)::before,
  table.task-table td:nth-child(4)::before{content:"账号"!important;}
  #taskTableContainer td:nth-child(5)::before,
  table.task-table td:nth-child(5)::before{content:"操作"!important;}
  #taskTableContainer td:nth-child(6)::before,
  table.task-table td:nth-child(6)::before{content:"时间"!important;}
  /* 第一列特殊处理 */
  #taskTableContainer td:first-child,
  table.task-table td:first-child{
    padding-left:0!important;margin-bottom:6px!important;padding-bottom:12px!important;
    border-bottom:1px solid rgba(55,80,85,0.25)!important;
  }
  #taskTableContainer td:first-child::before,
  table.task-table td:first-child::before{display:none!important;}
  /* 最后一列（操作）特殊处理 */
  #taskTableContainer td:last-child,
  table.task-table td:last-child{
    padding-left:0!important;margin-top:10px!important;padding-top:12px!important;
    border-top:1px solid rgba(55,80,85,0.25)!important;
  }
  #taskTableContainer td:last-child::before,
  table.task-table td:last-child::before{display:none!important;}
  /* 进度条 */
  #taskTableContainer .progress-bar,
  table.task-table .progress-bar{
    width:100%!important;height:12px!important;margin:10px 0!important;border-radius:6px!important;
    background:rgba(50,70,75,0.4)!important;overflow:hidden!important;
  }
  /* 操作按钮 */
  #taskTableContainer button,
  table.task-table button,
  #taskTableContainer a,
  table.task-table a{
    font-size:13px!important;padding:10px 16px!important;border-radius:10px!important;
    min-height:44px!important;display:inline-flex!important;align-items:center!important;
    justify-content:center!important;margin:3px!important;font-weight:600!important;
  }
  /* 状态标签 */
  #taskTableContainer span[style*="background"],
  table.task-table span[style*="background"]{
    display:inline-block!important;padding:6px 12px!important;border-radius:8px!important;
    font-size:12px!important;font-weight:700!important;
  }
  
  /* ============================================
     === 生成页面优化 ===
     ============================================ */
  .story-top{
    flex-wrap:wrap!important;gap:10px!important;padding:14px!important;
    border-radius:14px!important;margin-bottom:14px!important;
  }
  .story-top button,.story-top select{
    margin:2px!important;font-size:13px!important;padding:10px 14px!important;
    border-radius:10px!important;min-height:44px!important;
  }
  .scene-card{
    padding:16px!important;margin-bottom:14px!important;border-radius:16px!important;
  }
  .scene-card header{
    flex-wrap:wrap!important;gap:10px!important;margin-bottom:14px!important;
  }
  .scene-no{font-size:32px!important;font-weight:700!important;}
  .scene-card input{
    font-size:15px!important;padding:10px 14px!important;border-radius:10px!important;
    flex:1!important;min-width:140px!important;
  }
  .scene-actions{
    width:100%!important;display:flex!important;gap:8px!important;flex-wrap:wrap!important;
    margin-left:0!important;margin-top:8px!important;
  }
  .scene-actions button{flex:1!important;min-width:90px!important;}
  .scene-grid{
    grid-template-columns:1fr!important;gap:16px!important;width:100%!important;
  }
  .scene-grid section{min-width:0!important;width:100%!important;}
  .scene-grid textarea{
    width:100%!important;height:auto!important;min-height:140px!important;max-height:none!important;
    font-size:14px!important;padding:14px!important;border-radius:12px!important;
    line-height:1.8!important;
  }
  .scene-grid label{
    font-size:12px!important;font-weight:700!important;color:#7a9a9f!important;
    margin-bottom:8px!important;display:block!important;text-transform:uppercase!important;
    letter-spacing:0.5px!important;
  }
  .prompt-head{
    display:flex!important;flex-wrap:wrap!important;gap:10px!important;
    margin-bottom:10px!important;align-items:center!important;
  }
  .prompt-head label{margin-bottom:0!important;flex:1!important;min-width:120px!important;}
  .prompt-head > div{display:flex!important;gap:6px!important;flex-wrap:wrap!important;}
  .prompt-head button{
    font-size:12px!important;padding:8px 12px!important;border-radius:10px!important;
    min-height:38px!important;font-weight:600!important;
  }
  .video-preview{
    min-height:200px!important;border-radius:14px!important;margin-top:14px!important;
  }
  .video-preview video{max-height:280px!important;border-radius:12px!important;}
  
  /* 表格通用 */
  table{font-size:14px!important;}
  th,td{padding:12px 8px!important;font-size:13px!important;}
  
  /* 标题 */
  h1,h2,h3,h4{color:#e8f4f4!important;font-weight:700!important;}
  h1{font-size:22px!important;}
  h2{font-size:18px!important;}
  h3{font-size:16px!important;}
  
  /* 模态框 */
  .modal{padding:20px!important;margin:10px!important;max-width:calc(100vw - 20px)!important;
    max-height:calc(100vh - 20px)!important;overflow-y:auto!important;border-radius:20px!important;
    background:rgba(18,26,32,0.98)!important;}
  .modal>div{background:rgba(18,26,32,0.98)!important;color:#e7eeee!important;
    max-width:94vw!important;border-radius:20px!important;padding:24px!important;}
  
  /* 复选框 */
  input[type="checkbox"]{width:22px!important;height:22px!important;accent-color:#2d8a7a!important;}
  
  /* 滚动条 */
  ::-webkit-scrollbar{width:5px!important;height:5px!important;}
  ::-webkit-scrollbar-track{background:transparent!important;}
  ::-webkit-scrollbar-thumb{background:rgba(95,212,196,0.3)!important;border-radius:3px!important;}
  
  /* 文字颜色 */
  .muted{color:#7a8a8d!important;}
  .danger{color:#ff8b87!important;}
  
  /* 底部安全区域 */
  main{padding-bottom:40px!important;}
}</style>
<script>window.onerror=function(msg,url,line,col,error){
 var errDiv=document.createElement('div');
 errDiv.style.cssText='position:fixed;top:0;left:0;right:0;bottom:0;background:#1a0000;color:#ff6b6b;padding:30px;font-family:monospace;font-size:14px;z-index:99999;overflow:auto;white-space:pre-wrap;';
 errDiv.textContent='JAVASCRIPT ERROR\n\n消息: '+msg+'\n行: '+line+'\n列: '+col+'\nURL: '+url+'\n\n堆栈:\n'+(error?error.stack:'无堆栈');
 document.body.appendChild(errDiv);
 return false;
};
window.addEventListener('unhandledrejection',function(e){
 var errDiv=document.createElement('div');
 errDiv.style.cssText='position:fixed;top:0;left:0;right:0;bottom:0;background:#001a00;color:#6bff6b;padding:30px;font-family:monospace;font-size:14px;z-index:99999;overflow:auto;white-space:pre-wrap;';
 errDiv.textContent='PROMISE ERROR\n\n'+(e.reason?e.reason.message+'\n'+e.reason.stack:String(e.reason));
 document.body.appendChild(errDiv);
});

function esc(x){return String(x??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
const labels={id:'编号',name:'名称',cookie:'Cookie',status:'状态',points:'点数',created_at:'创建时间',host:'地址',port:'端口',username:'用户名',password:'密码',prompt:'提示词',duration:'时长',ratio:'比例',model:'模型',account_id:'账号',progress:'进度',result:'结果',quality:'目标画质',min_bytes:'目标大小',label:'名称',key:'密钥',enabled:'启用',action:'操作',detail:'详情',message:'内容',level:'级别',k:'设置项',v:'值',check_state:'检查状态',check_detail:'检查详情',last_checked:'最近检查',text_ok:'文字阶段',image_ok:'图片阶段'};
const originalRender=render;
render=async function(){
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===page));
 try{
 if(page==='generate')return gen();
 if(page==='import')return importPage();
 if(page==='settings')return settingsPage();
 if(page==='tasks')return tasksPage();
 if(page==='assets')return assetsPage();
 if(page==='node-canvas'||page==='node'){location.href='/static/node-canvas.html';return;}
 const table=page==='keys'?'api_keys':page;const data=await api(table);if(!Array.isArray(data))throw Error(data.error||'读取失败');
 C.innerHTML='<div class=\"panel\"><div class=\"toolbar\"><input id=\"filterText\" placeholder=\"搜索名称、任务或提示词\"><select id=\"filterStatus\"><option value=\"\">全部状态</option></select><button id=\"refresh\" onclick=\"render()\" style=\"padding:9px 14px;background:#2a4a5a;color:#7bd1be;border:1px solid #3a6a6a;border-radius:4px;cursor:pointer;font-size:13px\">↻ 刷新</button><button id=\"clearBtn\" onclick=\"clearFilters()\" style=\"padding:9px 14px;background:#4a2428;color:#ff9b98;border:1px solid #6a3438;border-radius:4px;cursor:pointer;font-size:13px\">✕ 清空</button>'+(['accounts','proxies','keys','settings'].includes(page)?'<button id=\"add\" class=\"primary\">新增 / 配置</button>':'')+'<button id=\"export\">导出 JSON</button></div><div id=\"listing\"></div></div>';
 const states=[...new Set(data.map(r=>r.status).filter(Boolean))];document.getElementById('filterStatus').innerHTML+=states.map(x=>'<option>'+esc(x)+'</option>').join('');
 const draw=()=>{const q=document.getElementById('filterText').value.toLowerCase(),status=document.getElementById('filterStatus').value;const filtered=data.filter(r=>JSON.stringify(r).toLowerCase().includes(q)&&(!status||r.status===status));const heads=data[0]?Object.keys(data[0]):[];document.getElementById('listing').innerHTML=filtered.length?'<table><thead><tr>'+heads.map(h=>'<th>'+esc(labels[h]||h)+'</th>').join('')+(['accounts','proxies','tasks','api_keys'].includes(table)?'<th>操作</th>':'')+'</tr></thead><tbody>'+filtered.map(r=>'<tr>'+heads.map(h=>'<td>'+esc(typeof r[h]==='object'?JSON.stringify(r[h]):r[h])+'</td>').join('')+(['accounts','proxies','tasks','api_keys'].includes(table)?'<td><button onclick=\'delRow(\\"'+table+'\\",\\"'+r.id+'\\")\'>删除</button></td>':'')+'</tr>').join('')+'</tbody></table>':'<div class=\"empty\">暂无数据</div>';};
 draw();
 document.getElementById('filterText').oninput=draw;
 document.getElementById('filterStatus').onchange=draw;
 document.getElementById('export').onclick=()=>{const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=table+'.json';a.click();};
 if(page==='accounts')document.getElementById('add').onclick=()=>modal.style.display='flex';
 if(page==='proxies')document.getElementById('add').onclick=()=>alert('代理配置在设置页');
 }catch(e){
 C.innerHTML='<div class=\"panel\">加载失败：'+esc(e.message)+'<br><br><button onclick=\"render()\">重试</button></div>';
 console.error('Render error:',e);
 }
};
addRow=function(){if(page==='accounts'){document.getElementById('modal').style.display='flex';return}
 const fields=page==='proxies'?{host:'代理主机',port:'端口',username:'用户名',password:'密码'}:page==='keys'?{label:'名称'}:{project_name:'项目名称',global_prompt:'全局提示词'};
 C.innerHTML='<form class="panel" id="editor"><h2>'+esc(T.textContent)+'</h2>'+Object.entries(fields).map(([k,v])=>'<p><label>'+esc(v)+'<br><input name="'+k+'" '+(k==='password'?'type="password"':'')+'></label></p>').join('')+'<button class="primary">保存</button></form>';
 document.getElementById('editor').onsubmit=async e=>{e.preventDefault();const x=Object.fromEntries(new FormData(e.target));if(page==='proxies'){x.port=Number(x.port);if(!x.host||x.port<1||x.port>65535){alert('填写有效地址及端口');return}}const r=await api(page,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)});if(r.error){alert(r.error);return}if(r.key)alert('保存密钥，仅本次显示：'+r.key);render()};
};
async function settingsPage(){
 const q=await api('queue'), t=await api('tasks'); const done=t.filter(x=>x.status==='completed').length; const used=Math.min(300,done);
 C.innerHTML='<div class="settings-wrap"><div class="usage-card"><div><span class="muted">24h 成片</span><b>'+used+'</b></div><div class="lease-box"><span>租约 <strong>'+Math.max(0,300-used)+'/300</strong></span><div class="lease-bars">'+Array.from({length:10},(_,i)=>'<i class="'+(i<Math.ceil((300-used)/30)?'on':'')+'"></i>').join('')+'</div><button onclick="render()">↻</button></div></div><div class="settings-card"><div class="settings-title"><div><h2>全局并发</h2><p>当前 '+q.workers+' 个 Worker · 服务端安全上限 20 个槽位</p></div><b id="concValue">'+q.workers+'</b></div><div class="setting-row"><label>并发数量 (1–20)</label><input id="conc" type="number" min="1" max="20" value="'+q.workers+'"></div><input id="concRange" type="range" min="1" max="20" value="'+q.workers+'"><div class="quick-row">'+[1,2,4,8,16,20].map(n=>'<button data-conc="'+n+'">'+n+'</button>').join('')+'</div><p class="muted">每个账号同时只运行 1 个任务；实际并发受账号、点数和供应商资源限制。</p><button class="save-conc primary" onclick="saveConcurrency()">▣ 保存并发</button></div><div class="settings-card"><h2>任务策略</h2><div class="strategy-grid"><label>默认画质<select id="defaultQuality"><option>720p</option><option>1080p</option></select></label><label>结果最小大小<input id="minBytes" type="number" value="30000000"> bytes</label><label>429 重试次数<input id="retryCount" type="number" min="0" max="5" value="3"></label></div></div></div>';
 const range=document.getElementById('concRange'),num=document.getElementById('conc'),value=document.getElementById('concValue');const sync=v=>{v=Math.max(1,Math.min(20,Number(v)||1));range.value=num.value=value.textContent=v};range.oninput=e=>sync(e.target.value);num.oninput=e=>sync(e.target.value);document.querySelectorAll('[data-conc]').forEach(b=>b.onclick=()=>sync(b.dataset.conc));
}
async function saveConcurrency(){const n=Math.max(1,Math.min(20,Number(document.getElementById('conc').value)||1));const r=await api('settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_concurrency:n,default_quality:document.getElementById('defaultQuality').value,min_bytes:document.getElementById('minBytes').value,retry_count:document.getElementById('retryCount').value})});alert(r.error||'并发设置已保存');render()}
let allTasks=[];let taskFilter='';let taskStatusFilter='';
async function tasksPage(){
  const [stats,tasks,q]=await Promise.all([api('stats'),api('tasks'),api('queue')]);
  allTasks=tasks;
  const taskCounts={running:tasks.filter(t=>t.status==="running").length,queued:tasks.filter(t=>t.status==="queued").length,completed:tasks.filter(t=>t.status==="completed").length,failed:tasks.filter(t=>t.status==="failed").length};
  C.innerHTML='<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:18px"><div style="background:rgba(28,32,33,0.85);border:1px solid rgba(48,59,62,0.5);border-radius:10px;padding:18px"><div style="font-size:13px;color:#8a9a9d;margin-bottom:8px">可用账号</div><div style="font-size:32px;font-weight:700;color:#7be2ad">'+stats.active_accounts+'</div><div style="font-size:11px;color:#6a7a7d;margin-top:4px">总共 '+stats.total_accounts+' 个</div></div><div style="background:rgba(28,32,33,0.85);border:1px solid rgba(48,59,62,0.5);border-radius:10px;padding:18px"><div style="font-size:13px;color:#8a9a9d;margin-bottom:8px">验活 / 冷却</div><div style="font-size:32px;font-weight:700;color:#ffc56d">'+stats.cooling_accounts+'</div><div style="font-size:11px;color:#6a7a7d;margin-top:4px">限流冷却中</div></div><div style="background:rgba(28,32,33,0.85);border:1px solid rgba(48,59,62,0.5);border-radius:10px;padding:18px"><div style="font-size:13px;color:#8a9a9d;margin-bottom:8px">失效 / 耗尽</div><div style="font-size:32px;font-weight:700;color:#ff9b98">'+stats.invalid_accounts+'</div><div style="font-size:11px;color:#6a7a7d;margin-top:4px">不可用账号</div></div><div style="background:rgba(28,32,33,0.85);border:1px solid rgba(48,59,62,0.5);border-radius:10px;padding:18px"><div style="font-size:13px;color:#8a9a9d;margin-bottom:8px">24h 成片</div><div style="font-size:32px;font-weight:700;color:#7bc8e2">'+stats.today_24h+'</div><div style="font-size:11px;color:#6a7a7d;margin-top:4px">运行中 '+stats.running_tasks+' 个</div></div><div style="background:rgba(28,42,38,0.9);border:1px solid rgba(58,106,74,0.6);border-radius:10px;padding:18px"><div style="font-size:13px;color:#8a9a9d;margin-bottom:8px">⚡ 正在生成</div><div style="font-size:32px;font-weight:700;color:#7be2ad">'+stats.running_tasks+'</div><div style="font-size:11px;color:#6a7a7d;margin-top:4px">并发 '+q.workers+' Worker</div></div><div style="background:rgba(42,28,30,0.9);border:1px solid rgba(106,52,56,0.6);border-radius:10px;padding:18px"><div style="font-size:13px;color:#8a9a9d;margin-bottom:8px">❌ 失败任务</div><div style="font-size:32px;font-weight:700;color:#ff9b98">'+stats.failed_tasks+'</div><div style="font-size:11px;color:#6a7a7d;margin-top:4px">可一键重试</div></div></div><div class="panel" style="padding:18px"><div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;align-items:center"><input placeholder="搜索任务 ID 或提示词" id="taskSearch" oninput="taskFilter=this.value;renderTaskTable()" style="flex:1;min-width:200px"><select id="taskStatusFilter" onchange="taskStatusFilter=this.value;renderTaskTable()"><option value="">全部状态</option><option value="completed">已完成</option><option value="running">运行中</option><option value="queued">排队中</option><option value="failed">失败</option></select><button class="primary" onclick="refreshTasks()" style="margin-right:6px">↻ 刷新</button><button onclick="retryAllFailed()" style="padding:9px 14px;background:#2a4a3a;color:#7be2ad;border:1px solid #3a6a4a;border-radius:4px;cursor:pointer;font-size:13px;margin-right:6px">🔄 一键重试失败</button><button onclick="clearTaskFilters()" style="padding:9px 14px;background:#4a2428;color:#ff9b98;border:1px solid #6a3438;border-radius:4px;cursor:pointer;font-size:13px">✕ 清空筛选</button><span id="taskAutoRefresh" style="margin-left:auto;font-size:11px;color:#6a7a7d">自动刷新: 开启</span></div><div id="taskTableContainer"></div></div>';
  renderTaskTable();
  startTaskAutoRefresh();
}

let taskAutoRefreshTimer = null;
function refreshTasks(){
  tasksPage();
  if(typeof toast==='function') toast('任务列表已刷新');
  else alert('任务列表已刷新');
}
function clearTaskFilters(){
  taskFilter='';
  taskStatusFilter='';
  var ft=document.getElementById('taskSearch');
  if(ft) ft.value='';
  var fs=document.getElementById('taskStatusFilter');
  if(fs) fs.value='';
  renderTaskTable();
  if(typeof toast==='function') toast('已清空筛选条件');
  else alert('已清空筛选条件');
}
function startTaskAutoRefresh(){
  if(taskAutoRefreshTimer){clearInterval(taskAutoRefreshTimer);}
  taskAutoRefreshTimer=setInterval(function(){
    if(page==='tasks'){
      api('tasks').then(function(tasks){
        allTasks=tasks;
        renderTaskTable();
      }).catch(function(){});
    }
  }, 5000);
}
function stopTaskAutoRefresh(){
  if(taskAutoRefreshTimer){clearInterval(taskAutoRefreshTimer);taskAutoRefreshTimer=null;}
}

function renderTaskTable(){
  const container=document.getElementById("taskTableContainer");
  if(!container)return;
  let filtered=allTasks;
  if(taskFilter){const q=taskFilter.toLowerCase();filtered=filtered.filter(t=>(t.id||"").toLowerCase().includes(q)||(t.prompt||"").toLowerCase().includes(q))}
  if(taskStatusFilter)filtered=filtered.filter(t=>t.status===taskStatusFilter);
  if(!filtered.length){container.innerHTML="<div style=\"text-align:center;padding:40px;color:#6a7a7d\">暂无任务记录</div>";return}
  const now=Date.now();
  const getDur=(c)=>{if(!c)return"";const t=new Date(c).getTime();if(isNaN(t))return"";const d=Math.floor((now-t)/1000);if(d<60)return d+"秒";if(d<3600)return Math.floor(d/60)+"分"+(d%60)+"秒";return Math.floor(d/3600)+"时"+Math.floor((d%3600)/60)+"分"};
  const rows=filtered.map(t=>{
    const sc={"completed":"background:#1a4a2a;color:#7be2ad","running":"background:#1a3a4a;color:#7bc8e2","queued":"background:#4a3a1a;color:#ffc56d","failed":"background:#4a2428;color:#ff9b98","awaiting_provider":"background:#4a3a1a;color:#ffc56d"}[t.status]||"background:#3a484b;color:#b8c9cc";
    const st={"completed":"已完成","running":"运行中","queued":"排队中","failed":"失败","awaiting_provider":"待接通"}[t.status]||t.status;
    const mn=t.model||"";
    const md=mn.includes("2.5")?"SD2.5 30s":mn.includes("2.0")?(t.duration==10?"SD2.0 10s":"SD2.0 15s"):mn;
    const ru=t.result&&t.result.includes("|")?"/output/"+t.result.split("|")[0].split("/").pop():(t.result&&t.result.startsWith("http")?t.result:"");
    const pg=t.progress||0;
    const pb=t.status==="running"?"<div style=\"width:100%;height:6px;background:#1a2a3a;border-radius:3px;margin-top:6px;overflow:hidden\"><div style=\"width:"+pg+"%;height:100%;background:linear-gradient(90deg,#078d78,#7be2ad);border-radius:3px;transition:width 0.5s\"></div></div>":"";
    const dur=getDur(t.created_at);
    const rb=(t.status==="failed"||t.status==="awaiting_provider")?"<button onclick=\"retryTask(\x27"+esc(t.id||"")+"\x27)\" style=\"padding:4px 10px;background:#4a3a1a;color:#ffc56d;border:1px solid #6a5a2a;border-radius:4px;cursor:pointer;font-size:11px;margin-right:4px\">重试</button>":"";
    const db="<button onclick=\"deleteTask(\x27"+esc(t.id||"")+"\x27)\" style=\"padding:4px 10px;background:#4a2428;color:#ff9b98;border:1px solid #6a3438;border-radius:4px;cursor:pointer;font-size:11px\">删除</button>";
    const ra=t.status==="completed"&&ru?"<div style=\"margin-top:6px;display:flex;gap:4px;flex-wrap:wrap\"><button onclick=\"playVideo(\x27"+ru+"\x27,\x27"+esc(t.id||"")+"\x27)\" style=\"padding:4px 10px;background:#1a4a3a;color:#7be2ad;border:1px solid #2a6a4a;border-radius:4px;cursor:pointer;font-size:11px\">播放</button><a href=\""+ru+"\" download=\"dola_"+esc(t.id||"")+"_"+md.replace(/\s/g,"_")+"_"+(t.duration||15)+"s.mp4\" style=\"padding:4px 10px;background:#1a3a4a;color:#7bc8e2;border:1px solid #2a5a6a;border-radius:4px;cursor:pointer;font-size:11px;text-decoration:none;display:inline-block\">下载</a>"+db+"</div>":"<div style=\"margin-top:6px;display:flex;gap:4px;flex-wrap:wrap\">"+rb+db+"</div>";
    const rt=t.result&&!ru?"<div style=\"font-size:11px;color:#ff9b98;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap\" title=\""+esc(t.result)+"\">"+esc(t.result.substring(0,30))+"</div>":"";
    return "<tr style=\"border-bottom:1px solid rgba(44,53,55,0.5)\"><td style=\"padding:12px 14px;vertical-align:top\"><div style=\"font-family:monospace;font-size:11px;color:#7bd1be;margin-bottom:4px\">"+esc(t.id||"")+"</div><div style=\"color:#b8c9cc;font-size:12px;line-height:1.4;max-width:280px;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical\">"+esc(t.prompt||"无提示词")+"</div></td><td style=\"padding:12px 14px;vertical-align:top\"><span style=\"display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:600;"+sc+"\">"+st+"</span>"+(t.progress?"<div style=\"font-size:10px;color:#6a7a7d;margin-top:4px\">"+pg+"%</div>":"")+pb+"</td><td style=\"padding:12px 14px;vertical-align:top;font-size:11px;color:#8a9a9d;line-height:1.5\"><b style=\"color:#b8c9cc;font-weight:500\">"+(t.duration||15)+"s</b> · "+(t.ratio||"9:16")+"<br>"+esc(md)+(dur?"<br><span style=\"color:#6a7a7d\">⏱ "+dur+"</span>":"")+"</td><td style=\"padding:12px 14px;vertical-align:top;font-size:12px;color:#b8c9cc\">"+(t.account_id?"<span style=\"color:#7bd1be\">#"+t.account_id+"</span>":"<span style=\"color:#ffc56d\">智能</span>")+"</td><td style=\"padding:12px 14px;vertical-align:top\">"+rt+ra+"</td><td style=\"padding:12px 14px;vertical-align:top;font-size:11px;color:#6a7a7d;white-space:nowrap\">"+esc((t.created_at||"").substring(0,16).replace("T"," "))+"</td></tr>";
  }).join("");
  container.innerHTML="<div style=\"overflow-x:auto\"><table class=\"task-table\" style=\"width:100%;border-collapse:collapse;min-width:800px\"><thead><tr style=\"background:rgba(20,35,42,0.8)\"><th style=\"padding:12px 14px;text-align:left;font-size:12px;color:#8a9a9d;border-bottom:1px solid #2b3639\">任务</th><th style=\"padding:12px 14px;text-align:left;font-size:12px;color:#8a9a9d;border-bottom:1px solid #2b3639;min-width:120px\">状态/进度</th><th style=\"padding:12px 14px;text-align:left;font-size:12px;color:#8a9a9d;border-bottom:1px solid #2b3639\">参数</th><th style=\"padding:12px 14px;text-align:left;font-size:12px;color:#8a9a9d;border-bottom:1px solid #2b3639\">账号</th><th style=\"padding:12px 14px;text-align:left;font-size:12px;color:#8a9a9d;border-bottom:1px solid #2b3639\">操作</th><th style=\"padding:12px 14px;text-align:left;font-size:12px;color:#8a9a9d;border-bottom:1px solid #2b3639\">创建时间</th></tr></thead><tbody>"+rows+"</tbody></table></div>";
}

async function retryTask(taskId){
    if(!confirm("确定要重试这个任务吗？"))return;
    await api("tasks/"+encodeURIComponent(taskId)+"/retry",{method:"POST"});
    renderTaskTable();
}
async function retryTask(taskId){
  if(!confirm("确定要重试这个任务吗？"))return;
  try{
    await api("tasks/"+encodeURIComponent(taskId)+"/retry",{method:"POST"});
    toast("任务已重新加入队列");
    if(page==="tasks")tasksPage();
  }catch(e){toast("重试失败: "+e.message)}
}
async function retryAllFailed(){
  const failedCount = (allTasks||[]).filter(t=>t.status==="failed").length;
  if(failedCount===0){alert("没有失败的任务");return;}
  if(!confirm("确定要重试所有 "+failedCount+" 个失败的任务吗？"))return;
  try{
    const r = await api("tasks/retry-all-failed",{method:"POST"});
    if(r.error){alert(r.error);return;}
    alert("已重新提交 "+r.retried+" 个失败任务！");
    if(page==="tasks")tasksPage();
  }catch(e){alert("重试失败: "+e.message)}
}

async function deleteTask(taskId){
  if(!confirm("确定要删除这个任务吗？此操作不可恢复。"))return;
  try{
    await api("tasks/"+encodeURIComponent(taskId),{method:"DELETE"});
    toast("任务已删除");
    if(page==="tasks")tasksPage();
  }catch(e){toast("删除失败: "+e.message)}
}


function playVideo(url,id){
  if(!url)return;
  var fullUrl=url.startsWith('http')?url:(window.location.origin+url);
  document.getElementById('videoModalTitle').textContent='任务 '+id+' - 视频播放';
  document.getElementById('videoPlayer').src=fullUrl;
  document.getElementById('videoModal').style.display='flex';
}

function closeVideoModal(){
  document.getElementById('videoModal').style.display='none';
  document.getElementById('videoPlayer').pause();
  document.getElementById('videoPlayer').src='';
}

doImport=async function(){const fd=new FormData();fd.append('cookies',document.getElementById('cookies').value);const f=document.getElementById('file').files[0];if(f)fd.append('file',f);try{const r=await api('import',{method:'POST',body:fd});if(r.error)throw Error(r.error);closeModal();alert('导入 '+r.count+' 条');render()}catch(e){alert(e.message)}};

function changeBg(){
  const now=new Date();
  const hour=now.getHours();
  const bgNum=(hour%15)+1;
  const bgUrl='/static/bg_images/bg_'+String(bgNum).padStart(2,'0')+'.jpg';
  document.body.style.backgroundImage='url('+bgUrl+')';
}
changeBg();
setInterval(changeBg,60000);

function updateLeaseStatus(){
  fetch('/api/proxy-status').then(function(r){return r.json()}).then(function(d){
    var el=document.getElementById('lease');
    if(!el)return;
    if(d.connected){
      el.innerHTML='台湾 '+d.ip+' · 50分钟';
      el.style.color='#7bd1be';
    }else{
      el.innerHTML='未连接';
      el.style.color='#ff9b98';
    }
  }).catch(function(){
    var el=document.getElementById('lease');
    if(el){el.innerHTML='未连接';el.style.color='#ff9b98'}
  });
}
updateLeaseStatus();setInterval(updateLeaseStatus,30000);

function clearFilters(){
  var ft=document.getElementById('filterText');
  if(ft) ft.value='';
  var fs=document.getElementById('filterStatus');
  if(fs) fs.value='';
  var search=document.getElementById('search');
  if(search) search.value='';
  render();
  toast('已清空筛选');
}
function filterTable(){
  var q=document.getElementById('search')?.value.toLowerCase()||'';
  var rows=document.querySelectorAll('tbody tr');
  rows.forEach(function(row){
    var text=row.textContent.toLowerCase();
    row.style.display=(!q||text.includes(q))?'':'none';
  });
}

nav();
render();
</script>
</body></html>'''
