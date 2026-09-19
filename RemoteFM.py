#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 willdomybest
"""
RemoteFM —— 单文件远程文件管理器（网页文件管理 + FTP + 网页终端）

安装：pip install flask requests pyftpdlib
运行：python RemoteFM.py
许可证：MIT，详见 LICENSE；第三方组件许可见 README
安全提示：本程序会开放文件读写、删除与命令执行能力，请勿直接暴露到公网，
         并务必修改默认账号密码（设置 SD_USER / SD_PASS 环境变量）。
"""
# /// script
# requires-python = ">=3.8"
# dependencies = ["flask>=2.0", "requests>=2.25", "pyftpdlib>=1.5.7"]
# ///
import os, sys, time, shutil, zipfile, json, uuid, threading, logging, tempfile, subprocess, signal, queue, re, shlex, socket, hashlib, platform as sysplat
from urllib.parse import urlparse, unquote
from flask import Flask, request, send_file, render_template_string, jsonify, Response, stream_with_context, abort, session, redirect
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler
import requests

# 输出被重定向到文件或管道时（例如 Windows 的 GBK 控制台），避免个别字符直接抛异常
try:
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')
except Exception:
    pass

try:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer
    from pyftpdlib.filesystems import AbstractedFS
    FTP_AVAILABLE = True
except ImportError:
    FTP_AVAILABLE = False

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

app.secret_key = os.environ.get('SD_SECRET', os.urandom(32).hex())


# ================= 运行参数（全部可用环境变量覆盖） =================
def _env_int(name, default):
    try:
        return int(os.environ.get(name, '') or default)
    except ValueError:
        return default


def _env_flag(name, default=True):
    v = os.environ.get(name)
    if v is None or v.strip() == '':
        return default
    return v.strip().lower() not in ('0', 'false', 'no', 'off')


HTTP_HOST = os.environ.get('SD_HOST', '0.0.0.0')
HTTP_PORT = _env_int('SD_PORT', 8880)

# ================= 默认根目录：Windows 为 D 盘，其他系统为用户主目录 =================
_root_env = os.environ.get('SD_ROOT', '').strip()
if _root_env:
    _default_root = os.path.abspath(_root_env)
elif os.name == 'nt':
    _default_root = os.path.abspath('D:/')
else:
    _default_root = os.path.expanduser('~')
_cfg = {'root': _default_root}

# ================= 用户与认证（超级用户 + 普通用户） =================
_auth = {
    'user': os.environ.get('SD_USER', 'admin'),
    'pass': os.environ.get('SD_PASS', 'admin123'),
}
_IS_DEFAULT_AUTH = (_auth['user'] == 'admin' and _auth['pass'] == 'admin123')


def _app_dir():
    """打包成 exe 时取 exe 所在目录，否则取脚本目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _pick_users_file():
    """用户存储位置：SD_USERS > 程序目录 > 用户主目录 > 临时目录。"""
    cands = []
    env = os.environ.get('SD_USERS', '').strip()
    if env:
        cands.append(os.path.abspath(env))
    cands.append(os.path.join(_app_dir(), 'users.json'))
    cands.append(os.path.join(os.path.expanduser('~'), '.remotefm', 'users.json'))
    for p in cands:
        try:
            os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
            with open(p, 'a', encoding='utf-8'):
                pass
            return p
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), 'remotefm-users.json')


USERS_FILE = _pick_users_file()
_users_lock = threading.Lock()
_users = {}          # name -> {'salt','hash','admin','root'}


def _pw_hash(pw, salt=None):
    salt = salt or os.urandom(8).hex()
    return salt, hashlib.sha256((salt + pw).encode('utf-8')).hexdigest()


def load_users():
    global _users
    try:
        with open(USERS_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    users = {}
    for u in (data.get('users') or []):
        n = (u.get('name') or '').strip()
        if not n or not u.get('salt') or not u.get('hash'):
            continue
        users[n] = {'salt': u['salt'], 'hash': u['hash'],
                    'admin': bool(u.get('admin')), 'root': u.get('root') or ''}
    _users = users


def save_users():
    with _users_lock:
        data = {'users': [dict(name=n, **v) for n, v in sorted(_users.items())]}
        try:
            with open(USERS_FILE + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(USERS_FILE + '.tmp', USERS_FILE)
            return True
        except OSError as e:
            logging.warning('保存用户信息失败: %s', e)
            return False


def _verify(name, pw):
    """已改过密码的账号走哈希校验，未改过的超级用户走 SD_PASS。"""
    rec = _users.get(name)
    if rec:
        return hashlib.sha256((rec['salt'] + pw).encode('utf-8')).hexdigest() == rec['hash']
    return name == _auth['user'] and pw == _auth['pass']


def current_user():
    """当前登录用户；后台线程/启动阶段没有会话时按超级用户处理。"""
    name = _auth['user']
    try:
        name = session.get('user') or name
    except RuntimeError:
        pass
    if name == _auth['user']:
        return {'name': name, 'admin': True, 'root': _cfg['root']}
    rec = _users.get(name)
    if rec:
        return {'name': name, 'admin': bool(rec.get('admin')),
                'root': rec['root'] if rec.get('root') else _cfg['root']}
    return {'name': _auth['user'], 'admin': True, 'root': _cfg['root']}


def is_admin():
    return current_user()['admin']


load_users()

# ================= FTP 服务器 =================
FTP_ENABLED = _env_flag('SD_FTP', True)
FTP_PORT = _env_int('SD_FTP_PORT', 2121)
_ftp_pasv_start = _env_int('SD_FTP_PASSIVE_START', 60000)
FTP_PASSIVE_PORTS = range(_ftp_pasv_start, _ftp_pasv_start + 50)

CHUNK_DIR = os.path.join(tempfile.gettempdir(), 'sc')
DL_DIR = os.path.join(tempfile.gettempdir(), 'sd')
for d in (CHUNK_DIR, DL_DIR, _cfg['root']): os.makedirs(d, exist_ok=True)

# ================= 上传参数 =================
PER_PAGE = 2000
CHUNK_SIZE = 32*1024*1024
app.config['MAX_CONTENT_LENGTH'] = CHUNK_SIZE + 8*1024*1024

IMG_E = {'jpg','jpeg','png','gif','webp','svg','bmp','ico','avif'}
VID_E = {'mp4','webm','ogg','ogv','mov','m4v','mkv','avi'}
TXT_E = {'txt','md','json','js','ts','jsx','tsx','py','html','htm','css','xml','csv','log','ini','conf','yaml','yml','sh','bat','cmd','ps1','java','c','cpp','h','hpp','go','rs','rb','php','sql','vue','svelte','toml','env','gitignore','dockerfile','makefile','srt','ass','vtt'}
TXT_MAX = 5*1024*1024
dl_tasks = {}

IS_WIN = os.name == 'nt'
PLAT = {'os':'windows' if IS_WIN else 'unix', 'name':'Windows' if IS_WIN else ('macOS' if sysplat.system()=='Darwin' else 'Linux'), 'shell':'cmd.exe' if IS_WIN else '/bin/sh'}
ENC = 'gbk' if IS_WIN else 'utf-8'

WIN_CMDS = {'dir','cd','chdir','md','mkdir','rd','rmdir','del','erase','copy','xcopy','robocopy','move','ren','rename','type','more','tree','attrib','fc','comp','find','findstr','sort','cls','ver','vol','date','time','whoami','hostname','systeminfo','tasklist','taskkill','set','path','echo','title','color','ping','ipconfig','netstat','nslookup','tracert','arp','net','netsh','chkdsk','diskpart','format','python','python3','py','pip','node','npm','yarn','git','java','javac','go','cargo','rustc','gcc','g++','cmake','make','curl','wget','ssh','scp','ftp','telnet','powershell','pwsh','cmd','wmic','reg','where','which','exit','help','7z','zip','unzip','tar','gzip'}
UNIX_CMDS = {'ls','cd','pwd','mkdir','rmdir','rm','cp','mv','ln','cat','head','tail','less','more','touch','file','stat','find','locate','which','whereis','tree','du','df','chmod','chown','chgrp','umask','wc','sort','uniq','cut','paste','tr','sed','awk','grep','egrep','diff','cmp','tar','gzip','gunzip','zip','unzip','bzip2','xz','clear','echo','env','export','unset','set','printenv','date','cal','uptime','uname','hostname','whoami','who','id','ps','top','htop','kill','killall','pkill','jobs','bg','fg','free','vmstat','iostat','lsof','strace','dmesg','sudo','su','exit','logout','history','alias','unalias','ping','ifconfig','ip','netstat','ss','nslookup','dig','host','traceroute','tracert','curl','wget','ssh','scp','rsync','ftp','sftp','nc','telnet','nmap','python','python3','pip','pip3','node','npm','yarn','pnpm','git','java','javac','go','cargo','rustc','gcc','g++','make','cmake','ninja','docker','kubectl','systemctl','service','crontab','at','nohup','screen','tmux','vi','vim','nano','emacs','man','info','help'}
CMDS = WIN_CMDS if IS_WIN else UNIX_CMDS

WIN_COMP = {'dir':['/a','/b','/s','/w','/p'],'cd':['..','\\','/d'],'del':['/f','/q','/s'],'copy':['/y','/v'],'xcopy':['/s','/e','/y','/i'],'robocopy':['/e','/mir','/mov','/move'],'tasklist':['/v','/svc','/fi','/fo'],'taskkill':['/f','/im','/pid','/t'],'ping':['-t','-n','-l'],'ipconfig':['/all','/release','/renew','/flushdns'],'netstat':['-a','-n','-o','-b','-ano'],'git':['status','log','diff','add','commit','push','pull','branch','checkout','merge','clone','init','remote','reset','stash','tag','fetch','rebase'],'python':['-m','-c','-u','-V'],'pip':['install','uninstall','list','show','freeze','upgrade'],'npm':['install','run','start','build','test','init'],'node':['-v','-e'],'curl':['-O','-L','-o','-X','-H','-d','-s','-k'],'wget':['-O','-c','-r'],'tar':['-xzf','-czf','-tzf'],'java':['-jar','-version','-cp'],'go':['run','build','test','mod','get'],'cargo':['build','run','test','check','new','init','add'],'powershell':['-Command','-File','-ExecutionPolicy']}
UNIX_COMP = {'ls':['-la','-lh','-l','-a','-R','-h'],'cd':['..','~','-'],'rm':['-rf','-r','-f','-i'],'cp':['-r','-a','-v','-u'],'mv':['-v','-u','-f'],'mkdir':['-p'],'chmod':['+x','755','644','777','-R'],'grep':['-r','-i','-n','-l','-v','-E'],'find':['-name','-type','-size','-mtime','-exec'],'sed':['-i','-e','-n'],'tar':['-xzf','-czf','-tzf','-xf'],'gzip':['-k','-d','-9'],'du':['-sh','-h','-a'],'df':['-h','-T'],'ps':['aux','-ef','-eo'],'kill':['-9','-15','-TERM'],'curl':['-O','-L','-o','-X','-H','-d','-s','-k'],'wget':['-O','-c','-r'],'git':['status','log','diff','add','commit','push','pull','branch','checkout','merge','clone','init','remote','reset','stash','tag','fetch','rebase'],'python':['-m','-c','-u','-V'],'python3':['-m','-c','-u','-V'],'pip':['install','uninstall','list','show','freeze','upgrade'],'docker':['ps','images','run','exec','build','logs','stop','start','rm','pull','push'],'systemctl':['start','stop','restart','status','enable','disable'],'kubectl':['get','describe','apply','delete','logs','exec'],'ssh':['-p','-i','-L','-R'],'scp':['-r','-P'],'rsync':['-avz','-av','--delete','--progress'],'ip':['addr','link','route'],'netstat':['-tuln','-an'],'ss':['-tuln','-an','-p']}
COMP = WIN_COMP if IS_WIN else UNIX_COMP

cmd_hist = []
HIST_MAX = 500
act_procs = {}
out_qs = {}
bg_tasks = {}
_proc_cache = {'data': None, 'ts': 0}


# ================= 认证 =================
def _check_auth(u, p):
    return bool(u) and _verify(u, p)


def _need_login():
    if request.path in ('/login', '/favicon.ico', '/favicon.svg'):
        return False
    if session.get('logged_in'):
        return False
    a = request.authorization
    if a and _check_auth(a.username, a.password):
        session['logged_in'] = True
        session['user'] = a.username
        return False
    return True


def _admin_only():
    """超级用户专属接口的统一拦截。"""
    if not is_admin():
        return jsonify({'success': False, 'error': '需要超级用户权限'}), 403
    return None


@app.before_request
def _require_login():
    if request.method == 'OPTIONS':
        return
    if _need_login():
        return redirect('/login')


# ================= 登录页 =================
ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
            '<path fill="#4a6cf7" transform="rotate(-20 32 32)" '
            'd="M37 6.49A26 26 0 1 0 37 57.51A26 26 0 0 1 37 6.49Z"/></svg>')


@app.route('/favicon.ico')
@app.route('/favicon.svg')
def favicon():
    return Response(ICON_SVG, mimetype='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})


LOGIN_PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>登录 - 因热爱而行动</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:linear-gradient(135deg,#4a6cf7,#6a4af7);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#fff;border-radius:12px;padding:36px 32px;width:100%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,.35);animation:pop .3s}
@keyframes pop{from{transform:scale(.95);opacity:0}to{transform:scale(1);opacity:1}}
.card h1{margin:0 0 6px;font-size:22px;color:#2c3e50;text-align:center}
.card .sub{color:#7a8299;text-align:center;font-size:13px;margin-bottom:24px}
.field{margin-bottom:16px}
.field label{display:block;font-size:13px;color:#4a5568;margin-bottom:6px;font-weight:500}
.field input{width:100%;padding:11px 12px;border:1px solid #dfe3eb;border-radius:6px;font-size:14px;outline:0;transition:.15s;font-family:inherit}
.field input:focus{border-color:#4a6cf7;box-shadow:0 0 0 3px rgba(74,108,247,.12)}
.btn{width:100%;padding:12px;background:#4a6cf7;color:#fff;border:0;border-radius:6px;font-size:15px;font-weight:500;cursor:pointer;transition:.15s;margin-top:4px;font-family:inherit}
.btn:hover{background:#3a5ce0}
.btn:active{transform:translateY(1px)}
.tip{background:#fff9e6;border:1px solid #ffe08a;padding:14px 16px;border-radius:8px;font-size:13px;color:#8a6d00;line-height:1.9;margin-top:22px}
.tip .t-title{font-weight:600;color:#664e00;display:block;margin-bottom:6px;font-size:13px}
.tip code{background:rgba(0,0,0,.08);padding:2px 7px;border-radius:4px;font-family:Consolas,Monaco,monospace;color:#664e00;font-size:12.5px;user-select:all}
.tip .row{margin:3px 0}
.error{background:#fdeceb;border:1px solid #fcc;color:#c0392b;padding:11px 14px;border-radius:6px;font-size:13px;margin-bottom:16px;text-align:center}
</style></head><body>
<div class="card">
  <h1>📁 文件管理器</h1>
  <p class="sub">请输入用户名和密码登录</p>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post" action="/login">
    <div class="field"><label>用户名</label><input name="username" value="{{ prefill_user }}" autofocus autocomplete="username"></div>
    <div class="field"><label>密码</label><input name="password" type="password" value="{{ prefill_pass }}" autocomplete="current-password"></div>
    <button class="btn" type="submit">登 录</button>
  </form>
  {% if is_default %}
  <div class="tip">
    <span class="t-title">💡 首次使用 · 默认账号</span>
    <div class="row">用户名：<code>{{ default_user }}</code></div>
    <div class="row">密　码：<code>{{ default_pass }}</code></div>
    <div class="row" style="margin-top:8px;font-size:12px;color:#a06000">登录后请尽快修改密码（编辑 <code>_auth</code> 或设置 <code>SD_USER</code>/<code>SD_PASS</code> 环境变量）</div>
  </div>
  {% endif %}
</div>
</body></html>'''


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect('/')
    if request.method == 'GET':
        return render_template_string(
            LOGIN_PAGE, error='',
            prefill_user=_auth['user'] if _IS_DEFAULT_AUTH else '',
            prefill_pass=_auth['pass'] if _IS_DEFAULT_AUTH else '',
            is_default=_IS_DEFAULT_AUTH,
            default_user=_auth['user'], default_pass=_auth['pass'],
        )
    u = (request.form.get('username') or '').strip()
    p = request.form.get('password') or ''
    if _check_auth(u, p):
        session['logged_in'] = True
        session['user'] = u
        session.permanent = True
        return redirect('/')
    return render_template_string(
        LOGIN_PAGE, error='❌ 用户名或密码错误',
        prefill_user=u, prefill_pass='',
        is_default=_IS_DEFAULT_AUTH,
        default_user=_auth['user'], default_pass=_auth['pass'],
    )


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


def get_root():
    """当前用户可见的根目录（普通用户为自己的根目录）。"""
    return current_user()['root']


def set_root(p):
    try:
        p = os.path.abspath(p)
        if not os.path.isdir(p): return False, '目录不存在'
        _cfg['root'] = p
        return True, p
    except Exception as e: return False, str(e)


def fnow(t): return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))


def merge_chunks(uid, name, total, target):
    try:
        with open(target, 'wb') as tf:
            for i in range(total):
                cp = os.path.join(CHUNK_DIR, f'{uid}_{i}')
                if not os.path.exists(cp): return False, f'分片 {i+1} 缺失'
                with open(cp, 'rb') as cf: shutil.copyfileobj(cf, tf, length=16*1024*1024)
                os.remove(cp)
        return True, '上传成功'
    except Exception as e: return False, f'合并失败: {e}'


def compress_files(paths, out):
    try:
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                ap = abspath(p)
                if not os.path.exists(ap): continue
                rp = os.path.relpath(ap, get_root())
                if os.path.isfile(ap): zf.write(ap, rp)
                else:
                    for r, d, fs in os.walk(ap):
                        zr = os.path.relpath(r, get_root())
                        zf.write(r, zr)
                        for f in fs:
                            zf.write(os.path.join(r, f), os.path.join(zr, f))
        return True, '压缩成功'
    except Exception as e: return False, f'压缩失败: {e}'


def extract_zip(zp, td):
    try:
        az, at = abspath(zp), abspath(td)
        if not os.path.exists(az): return False, 'ZIP不存在'
        if not zipfile.is_zipfile(az): return False, '不是有效ZIP'
        os.makedirs(at, exist_ok=True)
        with zipfile.ZipFile(az, 'r') as zf:
            for m in zf.infolist():
                mp = os.path.join(at, m.filename)
                if not mp.startswith(at): continue
                zf.extract(m, at)
        return True, '解压成功'
    except Exception as e: return False, f'解压失败: {e}'


def get_fname(url):
    try:
        f = os.path.basename(unquote(urlparse(url).path))
        return f if f and '.' in f else f'dl_{int(time.time())}.tmp'
    except: return f'dl_{int(time.time())}.tmp'


def dl_to_server(url, tid):
    try:
        dl_tasks[tid] = {'status':'downloading','progress':0,'message':'开始下载...','file_path':''}
        fname = get_fname(url)
        fp = os.path.join(DL_DIR, secure_filename(fname))
        base, ext = os.path.splitext(fp)
        i = 1
        while os.path.exists(fp): fp = f'{base}_{i}{ext}'; i += 1
        dl_tasks[tid]['message'] = f'正在下载: {fname}'
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()
        ts = int(r.headers.get('content-length', 0))
        ds = 0
        with open(fp, 'wb') as f:
            for c in r.iter_content(1024*1024):
                if c:
                    f.write(c); ds += len(c)
                    if ts > 0:
                        pg = int(ds/ts*100)
                        dl_tasks[tid]['progress'] = pg
                        dl_tasks[tid]['message'] = f'下载中: {pg}%'
        dl_tasks[tid].update({'status':'completed','progress':100,'message':'下载完成','file_path':fp})
    except Exception as e:
        dl_tasks[tid].update({'status':'failed','message':f'下载失败: {e}'})


def _inside(path, root):
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r.rstrip('\\/') + os.sep)


def abspath(rp, root=None):
    """把相对路径解析成绝对路径，并强制留在根目录内（含软链接逃逸检查）。"""
    ar = os.path.abspath(root or get_root())
    ap = os.path.abspath(os.path.join(ar, rp))
    if not _inside(ap, ar):
        return ar
    try:
        if os.path.exists(ap) and not _inside(os.path.realpath(ap), os.path.realpath(ar)):
            return ar
    except OSError:
        pass
    return ap


def list_items(cap, kw='', sb='name', od='asc'):
    items = []
    if not os.path.exists(cap): return items
    for n in os.listdir(cap):
        if kw and kw not in n: continue
        ap = os.path.join(cap, n)
        try: st = os.stat(ap)
        except OSError: continue
        isd = os.path.isdir(ap)
        sz = st.st_size if not isd else -1
        if sz < 0: ss = '-'
        elif sz < 1024: ss = f'{sz}B'
        elif sz < 1024*1024: ss = f'{sz/1024:.1f}KB'
        elif sz < 1024**3: ss = f'{sz/(1024*1024):.1f}MB'
        else: ss = f'{sz/(1024**3):.2f}GB'
        ext = n.rsplit('.', 1)[1].lower() if (not isd and '.' in n) else ''
        items.append({'name':n,'path':os.path.relpath(ap, get_root()).replace(os.sep,'/'),'abs':ap,'is_dir':isd,'size':sz,'size_str':ss,'ext':ext,'mtime':st.st_mtime,'mtime_str':fnow(st.st_mtime)})
    rev = (od == 'desc')
    if sb == 'name': items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()), reverse=rev)
    elif sb == 'size': items.sort(key=lambda x: x['size'], reverse=rev)
    elif sb == 'mtime': items.sort(key=lambda x: x['mtime'], reverse=rev)
    return items


def get_local_ip():
    try:
        host = request.host.split(':')[0]
    except Exception:
        host = '127.0.0.1'
    if host in ('localhost', '127.0.0.1', '::1'):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return host
    return host


def calc_size(path):
    try:
        if os.path.isfile(path): return os.path.getsize(path)
        total = 0
        for r, d, fs in os.walk(path):
            for f in fs:
                try: total += os.path.getsize(os.path.join(r, f))
                except: pass
        return total
    except: return 0


def copy_file_with_progress(src, dst, task_id, src_root_size, already_done):
    bs = 8 * 1024 * 1024
    copied_here = 0
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        while True:
            buf = fi.read(bs)
            if not buf: break
            fo.write(buf)
            copied_here += len(buf)
            done = already_done[0] + copied_here
            if src_root_size > 0:
                bg_tasks[task_id]['progress'] = int(done / src_root_size * 100)
            bg_tasks[task_id]['message'] = f'正在传输: {os.path.basename(src)} ({bg_tasks[task_id]["progress"]}%)'
    already_done[0] += copied_here
    try: shutil.copystat(src, dst)
    except: pass


def copy_tree_with_progress(src, dst, task_id, src_root_size, already_done):
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            copy_tree_with_progress(s, d, task_id, src_root_size, already_done)
        else:
            copy_file_with_progress(s, d, task_id, src_root_size, already_done)


def bg_batch_task(task_id, paths, target, operation, root=None):
    bg_tasks[task_id] = {
        'type': operation, 'status': 'running', 'progress': 0,
        'message': '正在计算大小…', 'total': len(paths), 'done': 0,
        'errors': [], 'src': paths, 'dst': target,
    }
    at = abspath(target, root)
    try:
        os.makedirs(at, exist_ok=True)
    except Exception as e:
        bg_tasks[task_id].update({'status':'failed','message':f'目标目录创建失败: {e}'})
        return

    total_size = 0
    items = []
    for p in paths:
        src = abspath(p, root)
        if not os.path.exists(src):
            bg_tasks[task_id]['errors'].append(f'{p}: 不存在')
            continue
        sz = calc_size(src)
        total_size += sz
        items.append((src, sz))

    bg_tasks[task_id]['message'] = f'共 {len(items)} 项，{total_size} 字节'
    already_done = [0]

    for src, sz in items:
        name = os.path.basename(src)
        dst = os.path.join(at, name)
        base, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(at, f'{base}_{i}{ext}')
            i += 1
        try:
            if operation == 'copy':
                if os.path.isdir(src):
                    copy_tree_with_progress(src, dst, task_id, total_size, already_done)
                else:
                    copy_file_with_progress(src, dst, task_id, total_size, already_done)
                bg_tasks[task_id]['done'] += 1
            else:
                try:
                    shutil.move(src, dst)
                    already_done[0] += sz
                    bg_tasks[task_id]['done'] += 1
                except Exception:
                    if os.path.isdir(src):
                        copy_tree_with_progress(src, dst, task_id, total_size, already_done)
                        shutil.rmtree(src, ignore_errors=True)
                    else:
                        copy_file_with_progress(src, dst, task_id, total_size, already_done)
                        os.remove(src)
                    bg_tasks[task_id]['done'] += 1
        except Exception as e:
            bg_tasks[task_id]['errors'].append(f'{name}: {e}')

    bg_tasks[task_id]['status'] = 'completed'
    bg_tasks[task_id]['progress'] = 100
    bg_tasks[task_id]['message'] = f'完成 {bg_tasks[task_id]["done"]}/{len(items)} 项'


if FTP_AVAILABLE:
    class DynamicFS(AbstractedFS):
        @property
        def root(self): return get_root()
        @root.setter
        def root(self, value): pass


def start_ftp_server():
    if not FTP_ENABLED:
        print('[FTP] 已通过 SD_FTP=0 关闭')
        return
    if not FTP_AVAILABLE:
        print('[FTP] pyftpdlib 未安装，跳过。安装: pip install pyftpdlib')
        return
    authorizer = DummyAuthorizer()
    authorizer.add_user(_auth['user'], _auth['pass'], get_root(), perm='elradfmwMT')

    class Handler(FTPHandler):
        filesystem = DynamicFS

    Handler.authorizer = authorizer
    Handler.banner = 'ShareDirectory FTP'
    Handler.passive_ports = FTP_PASSIVE_PORTS
    Handler.iobuffer = 1024 * 1024
    Handler.timeout = 600
    try:
        server = FTPServer((HTTP_HOST, FTP_PORT), Handler)
        server.max_cons = 100
        server.max_cons_per_ip = 20
        print(f'[FTP] 已启动: ftp://{HTTP_HOST}:{FTP_PORT}  账号: {_auth["user"]} / {_auth["pass"]}')
        server.serve_forever()
    except Exception as e:
        print(f'[FTP] 启动失败: {e}')


def _procs_win():
    ps = ('[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$ErrorActionPreference="SilentlyContinue";'
          '$cores=(Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors;if(-not $cores){$cores=1};'
          '$cpuMap=@{};Get-CimInstance Win32_PerfFormattedData_PerfProc_Process|ForEach-Object{try{$cpuMap[[int]$_.IDProcess]=[int]$_.PercentProcessorTime}catch{}};'
          '$processes=Get-CimInstance Win32_Process|ForEach-Object{$p=$_;$c=0;if($cpuMap.ContainsKey([int]$p.ProcessId)){$c=$cpuMap[[int]$p.ProcessId]};'
          '[PSCustomObject]@{pid=[int]$p.ProcessId;name=if($p.Name){$p.Name}else{""};cpu=[math]::Round($c/$cores,1);memory=[int64]$p.WorkingSetSize;command=if($p.CommandLine){$p.CommandLine}else{""}}};'
          '$os=Get-CimInstance Win32_OperatingSystem;$cs=Get-CimInstance Win32_ComputerSystem;'
          '$sys=[PSCustomObject]@{totalMemory=[int64]$os.TotalVisibleMemorySize*1024;freeMemory=[int64]$os.FreePhysicalMemory*1024;cpuCount=$cores;hostname=$cs.Name};'
          '[PSCustomObject]@{processes=$processes;system=$sys}|ConvertTo-Json -Compress -Depth 4')
    try:
        p = subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-Command',ps], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
        out = (p.stdout or '').strip()
        if not out: return None, (p.stderr or '').strip() or 'no output'
        data = json.loads(out)
        procs = data.get('processes') or []
        if isinstance(procs, dict): procs = [procs]
        cleaned = []
        for it in procs:
            try:
                cleaned.append({'pid':int(it.get('pid') or 0),'name':str(it.get('name') or ''),'cpu':round(float(it.get('cpu') or 0),1),'memory':int(it.get('memory') or 0),'command':str(it.get('command') or '')})
            except: pass
        si = data.get('system') or {}
        for k in ('totalMemory','freeMemory','cpuCount'):
            try: si[k] = int(si.get(k) or 0)
            except: si[k] = 0
        if not si['cpuCount']: si['cpuCount'] = os.cpu_count() or 1
        return {'processes':cleaned,'system':si}, None
    except Exception as e: return None, str(e)


def _procs_unix():
    try:
        p = subprocess.run(['ps','-eo','pid,pcpu,pmem,rss,comm,args','--no-headers'], capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            p = subprocess.run(['ps','-axo','pid,pcpu,pmem,rss,comm,args'], capture_output=True, text=True, timeout=10)
        procs = []
        for ln in (p.stdout or '').split('\n'):
            if not ln.strip(): continue
            parts = ln.strip().split(None, 5)
            if len(parts) < 5: continue
            try:
                pid, cpu, rss = int(parts[0]), float(parts[1]), int(parts[3])
            except: continue
            procs.append({'pid':pid,'name':parts[4],'cpu':round(cpu,1),'memory':rss*1024,'command':parts[5] if len(parts) > 5 else parts[4]})
        si = {'totalMemory':0,'freeMemory':0,'cpuCount':os.cpu_count() or 1,'hostname':sysplat.node()}
        try:
            with open('/proc/meminfo') as f:
                for ln in f:
                    if ln.startswith('MemTotal:'): si['totalMemory'] = int(ln.split()[1])*1024
                    elif ln.startswith('MemAvailable:'): si['freeMemory'] = int(ln.split()[1])*1024
        except: pass
        return {'processes':procs,'system':si}, None
    except Exception as e: return None, str(e)


def get_procs(force=False):
    now = time.time()
    if not force and _proc_cache['data'] and now - _proc_cache['ts'] < 1.0:
        return _proc_cache['data'], None
    d, err = _procs_win() if IS_WIN else _procs_unix()
    if d is not None:
        _proc_cache['data'] = d
        _proc_cache['ts'] = now
    return d, err


# ================= 前端 =================
# 注意：终端面板现在位于 .app 内部（.main 之后），不是 fixed，会挤压文件列表
TPL = r'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" type="image/svg+xml" href="/favicon.svg"><title>因热爱而行动</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}html,body{height:100%}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px;background:#f5f6fa;color:#2c3e50;overflow:hidden}
.app{display:flex;flex-direction:column;height:100vh}
.title-bar{background:linear-gradient(135deg,#4a6cf7,#6a4af7);color:#fff;padding:10px 20px;display:flex;justify-content:space-between;align-items:center;box-shadow:0 2px 8px rgba(0,0,0,.1);flex-shrink:0;gap:16px}
.title-bar h1{font-size:16px;font-weight:600;display:flex;align-items:center;gap:8px;flex-shrink:0}
.title-actions{display:flex;gap:8px;align-items:center}
.root-btn{display:flex;align-items:center;gap:6px;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.25);border-radius:6px;padding:5px 12px;color:#fff;font-size:12px;cursor:pointer;transition:.15s;max-width:60%;overflow:hidden}
.root-btn:hover{background:rgba(255,255,255,.25)}
.root-btn .label{opacity:.8;flex-shrink:0}
.root-btn .path{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:Consolas,monospace;flex:1}
.logout-btn{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.25);color:#fff;padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer;transition:.15s;text-decoration:none}
.logout-btn:hover{background:rgba(255,255,255,.25)}
.toolbar{background:#fff;border-bottom:1px solid #e6e8ec;padding:8px 16px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex-shrink:0}
.toolbar .group{display:flex;align-items:center;gap:4px}
.toolbar .sep{width:1px;height:22px;background:#e6e8ec;margin:0 4px}
.btn-icon{width:32px;height:32px;border:1px solid transparent;border-radius:6px;background:transparent;cursor:pointer;font-size:15px;color:#4a5568;display:flex;align-items:center;justify-content:center;transition:.15s}
.btn-icon:hover:not(:disabled){background:#eef1f8}.btn-icon:disabled{opacity:.35;cursor:not-allowed}
.btn{padding:6px 12px;border:1px solid #dfe3eb;border-radius:6px;background:#fff;cursor:pointer;font-size:13px;color:#2c3e50;display:inline-flex;align-items:center;gap:4px;transition:.15s;white-space:nowrap}
.btn:hover{background:#f5f7fb;border-color:#c8cede}
.btn.primary{background:#4a6cf7;border-color:#4a6cf7;color:#fff}
.btn.primary:hover{background:#3a5ce0}
.btn.active{background:#e8ecfb;border-color:#4a6cf7;color:#4a6cf7}
.breadcrumb{flex:1;min-width:200px;background:#f5f6fa;border-radius:6px;padding:6px 12px;font-size:13px;color:#4a5568;display:flex;align-items:center;gap:2px;overflow-x:auto;white-space:nowrap;border:1px solid transparent}
.breadcrumb::-webkit-scrollbar{height:0}
.breadcrumb .crumb{color:#4a6cf7;cursor:pointer;padding:2px 6px;border-radius:4px}
.breadcrumb .crumb:hover{background:#e8ecfb}
.breadcrumb .crumb.current{color:#2c3e50;font-weight:500;cursor:default}
.breadcrumb .crumb.current:hover{background:transparent}
.breadcrumb .sep{color:#a0a8b8;margin:0 2px}
.search-box{display:flex;align-items:center;border:1px solid #dfe3eb;border-radius:6px;padding:0 8px;background:#fff;height:32px;width:200px}
.search-box input{border:0;outline:0;flex:1;font-size:13px;padding:4px;background:transparent}
.main{flex:1;min-height:0;overflow:hidden;background:#fff;display:flex;flex-direction:row}
.content{flex:1;min-width:0;display:flex;flex-direction:column}
.sidebar{width:220px;flex-shrink:0;border-right:1px solid #e6e8ec;background:#fafbfc;display:flex;flex-direction:column;min-height:0}
.side-head{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;font-size:12px;color:#7a8299;border-bottom:1px solid #ebedf1;font-weight:600}
.side-btn{border:0;background:transparent;cursor:pointer;font-size:12px;color:#7a8299}
.tree{flex:1;overflow:auto;padding:6px 4px;font-size:13px}
.tree-node{white-space:nowrap}
.tree-row{display:flex;align-items:center;gap:4px;padding:3px 6px;border-radius:5px;cursor:pointer;color:#4a5568}
.tree-row:hover{background:#eef1f8}
.tree-row.active{background:#4a6cf7;color:#fff}
.tree-tog{width:14px;text-align:center;color:#9aa3b5;font-size:11px;user-select:none}
.tree-row.active .tree-tog{color:#fff}
.tree-label{overflow:hidden;text-overflow:ellipsis}
.tree-kids{margin-left:12px;border-left:1px dashed #e0e4ec;padding-left:2px}
.tree-empty{color:#b3b9c7;font-size:12px;padding:3px 8px}
.picker-box{height:300px;overflow:auto;border:1px solid #e6e8ec;border-radius:6px;padding:6px;background:#fafbfc}
.picker-path{font-family:Consolas,monospace;font-size:12px;color:#4a5568;background:#f4f6fa;border-radius:5px;padding:6px 8px;margin-bottom:10px;word-break:break-all}
.user-row{display:flex;align-items:center;gap:8px;padding:6px 8px;border-bottom:1px solid #f0f2f7;font-size:13px}
.user-row .uname{font-weight:600;color:#2c3e50;min-width:104px}
.user-row .uroot{color:#7a8299;font-family:Consolas,monospace;font-size:11.5px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.user-row button.mini{padding:2px 8px;font-size:12px;border:1px solid #dfe3eb;background:#fff;border-radius:4px;cursor:pointer;color:#4a5568}
.user-row button.mini.danger{color:#e74c3c;border-color:#fdd6d6}
.modal h3 .tag{font-size:11px;font-weight:400;color:#7a8299;margin-left:6px}
@media (max-width:760px){.sidebar{display:none}}
.file-area{flex:1;min-height:0;overflow:auto;position:relative}
.file-area.dragover::after{content:"松开鼠标上传文件到此处";position:absolute;inset:0;background:rgba(74,108,247,.1);border:3px dashed #4a6cf7;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:20px;color:#4a6cf7;font-weight:600;z-index:10;pointer-events:none}
table.file-table{width:100%;border-collapse:collapse}
table.file-table thead th{position:sticky;top:0;z-index:2;background:#fafbfc;font-size:12px;font-weight:600;color:#7a8299;text-align:left;padding:10px 14px;border-bottom:1px solid #ebedf1;user-select:none;white-space:nowrap}
table.file-table thead th.sortable{cursor:pointer}
table.file-table thead th.sortable:hover{background:#f0f2f7;color:#4a5568}
table.file-table thead th .sort-arrow{opacity:.5;margin-left:4px;font-size:10px}
table.file-table tbody tr{cursor:default;transition:background .1s}
table.file-table tbody tr:hover{background:#f8f9fc}
table.file-table tbody tr.selected{background:#e8ecfb}
table.file-table td{padding:8px 14px;font-size:13px;color:#4a5568;border-bottom:1px solid #f2f4f8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
table.file-table td.name-cell{display:flex;align-items:center;gap:8px;color:#2c3e50;user-select:none}
table.file-table .file-icon{font-size:18px;flex-shrink:0;width:22px;text-align:center}
table.file-table .row-actions{display:flex;gap:4px;opacity:0;transition:.15s}
table.file-table tbody tr:hover .row-actions{opacity:1}
table.file-table .row-actions button{padding:3px 8px;font-size:12px;border:1px solid #dfe3eb;background:#fff;border-radius:4px;cursor:pointer;color:#4a5568}
table.file-table .row-actions button:hover{background:#f5f7fb}
table.file-table .row-actions button.danger:hover{background:#fee;color:#e74c3c;border-color:#fcc}
.empty-state{padding:80px 20px;text-align:center;color:#a0a8b8}
.empty-state .icon{font-size:56px;opacity:.4;margin-bottom:12px}
.statusbar{height:28px;background:#fafbfc;border-top:1px solid #ebedf1;display:flex;align-items:center;justify-content:space-between;padding:0 16px;font-size:12px;color:#7a8299;flex-shrink:0}
.statusbar .right{display:flex;gap:16px}
.ctx-menu{position:fixed;background:#fff;border:1px solid #dfe3eb;border-radius:8px;box-shadow:0 8px 30px rgba(0,0,0,.12);padding:6px;min-width:180px;z-index:1000;display:none}
.ctx-menu.show{display:block}
.ctx-menu .item{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:5px;font-size:13px;color:#2c3e50;cursor:pointer;user-select:none}
.ctx-menu .item:hover{background:#f0f3fa}
.ctx-menu .item.danger{color:#e74c3c}
.ctx-menu .item.danger:hover{background:#fdeceb}
.ctx-menu .item .icon{width:16px;text-align:center;font-size:14px}
.ctx-menu .divider{height:1px;background:#ebedf1;margin:4px 0}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:2000;padding:20px}
.modal-bg.show{display:flex}
.modal{background:#fff;border-radius:10px;padding:24px;width:100%;max-width:520px;box-shadow:0 20px 60px rgba(0,0,0,.3);animation:pop .2s}
.modal.wide{max-width:640px}
@keyframes pop{from{transform:scale(.95);opacity:0}to{transform:scale(1);opacity:1}}
.modal h3{font-size:16px;margin-bottom:16px}
.modal .form-row{margin-bottom:14px}
.modal label{display:block;font-size:13px;color:#4a5568;margin-bottom:6px}
.modal input[type=text],.modal input[type=url]{width:100%;padding:9px 12px;border:1px solid #dfe3eb;border-radius:6px;font-size:14px;outline:0}
.modal input:focus{border-color:#4a6cf7;box-shadow:0 0 0 3px rgba(74,108,247,.1)}
.modal .hint{font-size:12px;color:#a0a8b8;margin-top:6px}
.modal .actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}
.modal .actions button{padding:8px 18px;border-radius:6px;border:1px solid #dfe3eb;background:#fff;cursor:pointer;font-size:14px}
.modal .actions button.primary{background:#4a6cf7;border-color:#4a6cf7;color:#fff}
.mode-cards{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
.mode-card{padding:20px 16px;border:2px solid #dfe3eb;border-radius:10px;cursor:pointer;text-align:center;transition:.15s;background:#fff}
.mode-card:hover{border-color:#4a6cf7;background:#f5f7ff;transform:translateY(-1px)}
.mode-card.default{border-color:#4a6cf7}
.mode-icon{font-size:36px;margin-bottom:8px}
.mode-title{font-weight:600;color:#2c3e50;margin-bottom:6px;font-size:14px}
.mode-desc{font-size:12px;color:#7a8299;line-height:1.5}
.mode-badge{display:inline-block;font-size:10px;color:#4a6cf7;background:#e8ecfb;padding:2px 8px;border-radius:8px;margin-top:6px}
.ftp-info-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 16px;margin-bottom:16px;background:#f8f9fc;padding:16px;border-radius:8px}
.ftp-info-grid>div{display:flex;flex-direction:column;gap:3px;min-width:0}
.ftp-info-grid label{font-size:11px;color:#a0a8b8;margin:0;font-weight:500;text-transform:uppercase;letter-spacing:.5px}
.ftp-info-grid span{font-family:Consolas,Monaco,monospace;color:#2c3e50;font-size:13px;word-break:break-all;user-select:all}
.ftp-link-box{background:#1e1e2e;color:#4af7a0;padding:12px 14px;border-radius:6px;font-family:Consolas,Monaco,monospace;font-size:12px;word-break:break-all;margin-bottom:16px;position:relative;user-select:all;line-height:1.6;cursor:pointer;transition:.15s}
.ftp-link-box:hover{background:#2a2a3e}
.ftp-link-box .lbl{color:#6a6a8a;font-size:11px;display:block;margin-bottom:4px;user-select:none}
.ftp-actions{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.ftp-actions button{flex:1;min-width:140px;padding:9px 14px;border:1px solid #dfe3eb;background:#fff;border-radius:6px;cursor:pointer;font-size:13px;color:#2c3e50;transition:.15s;display:inline-flex;align-items:center;justify-content:center;gap:6px}
.ftp-actions button:hover{background:#f5f7fb;border-color:#c8cede}
.ftp-actions button.primary{background:#4a6cf7;border-color:#4a6cf7;color:#fff}
.ftp-actions button.primary:hover{background:#3a5ce0}
.ftp-tip{background:#fff9e6;border:1px solid #ffe08a;padding:12px 16px;border-radius:6px;font-size:12.5px;color:#8a6d00;line-height:1.8}
.ftp-tip b{color:#664e00}
.preview-bg{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;flex-direction:column;z-index:4000}
.preview-bg.show{display:flex}
.preview-header{height:48px;display:flex;align-items:center;justify-content:space-between;padding:0 20px;background:rgba(0,0,0,.4);color:#fff;flex-shrink:0;backdrop-filter:blur(8px)}
.preview-title{font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.preview-actions{display:flex;gap:8px;flex-shrink:0}
.preview-actions button{padding:6px 14px;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.2);color:#fff;border-radius:6px;cursor:pointer;font-size:13px}
.preview-actions button:hover{background:rgba(255,255,255,.3)}
.preview-body{flex:1;display:flex;align-items:center;justify-content:center;overflow:auto;padding:20px}
.preview-body img,.preview-body video{max-width:100%;max-height:100%;border-radius:6px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
.preview-body .text-view{width:100%;height:100%;background:#1e1e2e;color:#e6e6e6;border-radius:6px;padding:20px;overflow:auto;font-family:Consolas,Monaco,monospace;font-size:13px;line-height:1.6;white-space:pre;tab-size:4}
.preview-error{color:#ff6b6b;font-size:14px;padding:20px;background:rgba(255,255,255,.05);border-radius:6px}
.upload-panel{position:fixed;right:20px;bottom:60px;width:360px;background:#fff;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.18);border:1px solid #dfe3eb;z-index:2500;display:none;overflow:hidden}
.upload-panel.show{display:block}
.upload-panel.collapsed{width:220px}
.upload-panel.collapsed .upload-body{display:none}
.upload-header{padding:12px 16px;background:linear-gradient(135deg,#4a6cf7,#6a4af7);color:#fff;display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none;gap:8px}
.upload-header .title{font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px;overflow:hidden;flex:1;min-width:0}
.upload-header .file-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:400;opacity:.9}
.upload-header .toggle{flex-shrink:0;font-size:12px;transition:transform .2s;width:16px;text-align:center}
.upload-panel.collapsed .upload-header .toggle{transform:rotate(-180deg)}
.upload-header .close-btn{flex-shrink:0;width:20px;height:20px;border-radius:4px;background:rgba(255,255,255,.15);border:0;color:#fff;cursor:pointer;display:none;align-items:center;justify-content:center;font-size:12px}
.upload-panel.done .upload-header .close-btn{display:flex}
.upload-body{padding:14px 16px 16px}
.progress-wrap{height:22px;background:#eef1f8;border-radius:11px;overflow:hidden;margin-bottom:12px;position:relative}
.progress-wrap .fill{height:100%;background:linear-gradient(90deg,#4a6cf7,#6a4af7);width:0%;transition:width .2s;display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:600}
.progress-wrap.bg-task .fill{background:linear-gradient(90deg,#27ae60,#16a085)}
.upload-stats{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;font-size:12px;color:#7a8299}
.upload-stats b{color:#4a6cf7;font-weight:600}
.upload-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px}
.upload-actions button{padding:6px 14px;border-radius:6px;border:1px solid #dfe3eb;background:#fff;cursor:pointer;font-size:12px}

/* ====== 终端面板（现在是 .app 的 flex 子元素，展开时挤压文件列表） ====== */
.terminal-panel{flex-shrink:0;height:380px;background:#1e1e2e;color:#e6e6e6;display:flex;flex-direction:column;border-top:1px solid #dfe3eb;transition:height .25s;font-family:Consolas,Monaco,monospace}
.terminal-panel.collapsed{height:34px}
.terminal-panel.collapsed .terminal-body{display:none}
.terminal-panel.collapsed .toggle-icon{transform:rotate(180deg)}
.terminal-header{height:34px;display:flex;align-items:center;justify-content:space-between;padding:0 14px;background:#252536;cursor:pointer;user-select:none;flex-shrink:0;border-bottom:1px solid #333}
.terminal-title{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:#b8b8d0;overflow:hidden}
.terminal-platform{font-size:11px;color:#4af7a0;font-weight:500;padding:2px 8px;background:rgba(74,247,160,.1);border-radius:4px;flex-shrink:0}
.terminal-cwd{font-size:11px;color:#6a6a8a;font-family:Consolas,monospace;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.terminal-header-actions{display:flex;align-items:center;gap:4px}
.term-btn{width:24px;height:24px;border:0;background:transparent;color:#888;cursor:pointer;border-radius:4px;font-size:13px;display:flex;align-items:center;justify-content:center}
.term-btn:hover{background:#3a3a4e;color:#ccc}
.toggle-icon{color:#888;font-size:11px;transition:transform .2s;margin-left:4px}
.terminal-body{flex:1;min-height:0;display:flex;overflow:hidden}
.terminal-history{width:200px;background:#222232;border-right:1px solid #333;display:flex;flex-direction:column;flex-shrink:0}
.history-header{padding:8px 12px;font-size:11px;color:#6a6a8a;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2a2a3a;display:flex;justify-content:space-between;align-items:center}
.history-clear{color:#6a6a8a;cursor:pointer;font-size:12px}
.history-clear:hover{color:#ff6b6b}
.history-list{flex:1;overflow-y:auto;padding:4px}
.history-item{padding:5px 8px;font-size:11px;color:#999;border-radius:4px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.history-item:hover{background:#2e2e42;color:#ccc}
.terminal-main{flex:1;display:flex;flex-direction:column;position:relative;overflow:hidden;min-height:0}
.terminal-output{flex:1;overflow-y:auto;padding:10px 14px;font-size:12.5px;line-height:1.5;white-space:pre-wrap;word-break:break-all;color:#d4d4e0}
.terminal-output .cmd-line{color:#4a9eff}
.terminal-output .error-line{color:#ff6b6b}
.terminal-output .exit-line{color:#6a6a8a;font-style:italic}
.terminal-output .info-line{color:#4af7a0}
.terminal-output .hint-line{color:#7a8299;padding-left:14px;margin:2px 0}
.terminal-output .hint-cmd{color:#4af7a0;background:rgba(74,247,160,.08);padding:1px 6px;border-radius:3px;font-family:Consolas,monospace;cursor:pointer}
.terminal-output .hint-cmd:hover{background:rgba(74,247,160,.2)}
.terminal-input-row{display:flex;align-items:center;padding:8px 14px;background:#252536;border-top:1px solid #333;gap:8px;flex-shrink:0}
.prompt{color:#4af7a0;font-weight:700;font-size:13px;flex-shrink:0}
.terminal-input{flex:1;background:transparent;border:0;outline:0;color:#e6e6e6;font-family:Consolas,Monaco,monospace;font-size:13px;caret-color:#4af7a0}
.terminal-input::placeholder{color:#4a4a6a}
.term-send-btn{padding:4px 14px;background:#4a6cf7;border:0;color:#fff;border-radius:5px;cursor:pointer;font-size:12px;flex-shrink:0}
.term-send-btn:disabled{opacity:.4;cursor:not-allowed}
.completion-popup{position:absolute;bottom:100%;left:14px;right:14px;background:#2a2a3e;border:1px solid #4a6cf7;border-radius:8px 8px 0 0;max-height:220px;overflow-y:auto;display:none;z-index:10;box-shadow:0 -4px 16px rgba(0,0,0,.3)}
.completion-popup.show{display:block}
.completion-list{padding:4px}
.completion-item{padding:6px 12px;font-size:12.5px;color:#ccc;cursor:pointer;border-radius:4px;display:flex;align-items:center;gap:8px}
.completion-item:hover,.completion-item.active{background:#3a3a5e;color:#fff}
.completion-item .cmd-icon{color:#4af7a0;font-size:11px}

.process-panel{position:fixed;inset:0;background:#f5f6fa;z-index:3500;display:none;flex-direction:column}
.process-panel.show{display:flex}
.process-topbar{background:linear-gradient(135deg,#4a6cf7,#6a4af7);color:#fff;padding:12px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;flex-shrink:0;box-shadow:0 2px 8px rgba(0,0,0,.1)}
.process-title{font-size:16px;font-weight:600;display:flex;align-items:center;gap:8px}
.process-stats{display:flex;gap:16px;font-size:13px;flex:1;flex-wrap:wrap}
.process-stats .stat{display:flex;align-items:center;gap:6px;background:rgba(255,255,255,.12);padding:4px 12px;border-radius:6px}
.process-stats .stat b{color:#ffd94a;font-weight:600;font-family:Consolas,monospace}
.process-search input{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.25);color:#fff;padding:6px 12px;border-radius:6px;width:260px;outline:none;font-size:13px}
.process-search input::placeholder{color:rgba(255,255,255,.55)}
.process-search input:focus{background:rgba(255,255,255,.25);border-color:rgba(255,255,255,.5)}
.process-refresh-toggle{display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer;user-select:none;padding:4px 10px;border-radius:6px;background:rgba(255,255,255,.12)}
.process-panel .btn{background:rgba(255,255,255,.15);border-color:rgba(255,255,255,.25);color:#fff}
.process-panel .btn:hover{background:rgba(255,255,255,.3)}
.process-body{flex:1;overflow:auto;background:#fff}
table.process-table{width:100%;border-collapse:collapse;font-size:13px}
table.process-table thead th{position:sticky;top:0;z-index:2;background:#fafbfc;color:#7a8299;font-size:12px;font-weight:600;text-align:left;padding:10px 14px;border-bottom:1px solid #ebedf1;cursor:pointer;user-select:none;white-space:nowrap}
table.process-table thead th:hover{background:#f0f2f7;color:#4a5568}
table.process-table thead th .sort-arrow{opacity:.5;margin-left:4px;font-size:10px}
table.process-table tbody tr:hover{background:#f8f9fc}
table.process-table td{padding:6px 14px;border-bottom:1px solid #f2f4f8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#4a5568}
table.process-table td.pid-cell{font-family:Consolas,monospace;color:#7a8299;width:80px}
table.process-table td.name-cell{font-weight:500;color:#2c3e50;width:220px}
table.process-table td.cpu-cell{width:90px}
table.process-table td.mem-cell{width:110px}
table.process-table td.cmd-cell{max-width:500px;color:#7a8299;font-family:Consolas,monospace;font-size:12px}
.cpu-badge,.mem-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;font-family:Consolas,monospace}
.cpu-low{background:#e8f7ee;color:#27ae60}
.cpu-mid{background:#fff6e0;color:#d68910}
.cpu-high{background:#fdeceb;color:#e74c3c}
.mem-badge{background:#eef1f8;color:#4a5568}
.process-actions{display:flex;gap:4px;opacity:0;transition:.15s}
table.process-table tbody tr:hover .process-actions{opacity:1}
.process-actions button{padding:3px 10px;font-size:12px;border:1px solid #dfe3eb;background:#fff;border-radius:4px;cursor:pointer;color:#4a5568}
.process-actions button:hover{background:#f5f7fb}
.process-actions button.danger:hover{background:#fee;color:#e74c3c;border-color:#fcc}
.process-actions button:disabled{opacity:.4;cursor:not-allowed}
.process-footer{height:28px;background:#fafbfc;border-top:1px solid #ebedf1;display:flex;align-items:center;justify-content:space-between;padding:0 16px;font-size:12px;color:#7a8299;flex-shrink:0}
.toast{position:fixed;top:20px;right:20px;padding:12px 20px;border-radius:8px;background:#2c3e50;color:#fff;font-size:13px;box-shadow:0 8px 30px rgba(0,0,0,.2);z-index:5000;animation:slideIn .25s;max-width:360px}
.toast.success{background:#27ae60}.toast.error{background:#e74c3c}
@keyframes slideIn{from{transform:translateX(20px);opacity:0}to{transform:translateX(0);opacity:1}}
</style></head><body>
<div class="app">
<div class="title-bar"><h1>📁 文件管理器</h1>
<div class="title-actions">
<button class="root-btn" onclick="showRootModal()" title="点击更改根目录"><span class="label">根目录</span><span class="path" id="rootPathDisplay">{{ROOT_DIR}}</span><span>✎</span></button>
<a class="logout-btn" href="/logout" title="退出登录">🚪 退出</a>
</div>
</div>
<div class="toolbar">
<div class="group">
<button class="btn-icon" id="btnBack" onclick="goBack()">←</button>
<button class="btn-icon" id="btnForward" onclick="goForward()">→</button>
<button class="btn-icon" id="btnUp" onclick="goUp()">↑</button>
<button class="btn-icon" onclick="refresh()">🔄</button>
</div><div class="sep"></div>
<div class="breadcrumb" id="breadcrumb"></div><div class="sep"></div>
<div class="search-box"><input id="searchInput" placeholder="搜索文件…" onkeydown="if(event.key==='Enter')doSearch()"></div>
<button class="btn primary" onclick="onUploadClick()">⬆ 上传</button>
<button class="btn" onclick="showMkdir()">＋ 新建文件夹</button>
<button class="btn" onclick="showRemoteDownload()">🌐 远程下载</button>
<button class="btn" id="btnTerminal" onclick="toggleTerminal()">💻 终端</button>
<button class="btn" id="btnProcess" onclick="openProcessPanel()">⚙️ 进程</button>
<button class="btn" id="btnUsers" onclick="openUserModal()">👤 <span id="userName">用户</span></button>
</div>
<div class="main">
<div class="sidebar" id="sidebar">
<div class="side-head"><span>📚 资源目录</span><button class="side-btn" onclick="treeInit()" title="刷新目录树">🔄</button></div>
<div class="tree" id="treeRoot"></div>
</div>
<div class="content"><div class="file-area" id="fileArea">
<table class="file-table"><thead><tr>
<th class="sortable" style="width:45%">名称 <span class="sort-arrow" id="sortName"></span></th>
<th class="sortable" style="width:180px">修改日期 <span class="sort-arrow" id="sortMtime"></span></th>
<th class="sortable" style="width:100px">大小 <span class="sort-arrow" id="sortSize"></span></th>
<th style="width:200px">操作</th>
</tr></thead><tbody id="fileList"></tbody></table>
<div class="empty-state" id="emptyState" style="display:none"><div class="icon">📭</div><div class="text">此文件夹为空</div></div>
</div>
<div class="statusbar"><span id="statusText">准备就绪</span><div class="right"><span id="selectionText"></span></div></div>
</div><!-- /.content -->
</div><!-- /.main -->

<!-- 终端面板：现在位于 .app 内部，是 flex 布局的一部分 -->
<div class="terminal-panel collapsed" id="terminalPanel">
<div class="terminal-header" onclick="toggleTerminal(event)"><span class="terminal-title"><span>💻</span><span>远程命令</span><span class="terminal-platform" id="terminalPlatform">—</span><span class="terminal-cwd" id="terminalCwdDisplay">—</span></span>
<div class="terminal-header-actions" onclick="event.stopPropagation()"><button class="term-btn" onclick="clearTerminal()">🗑️</button><button class="term-btn" onclick="killTerminalProcess()" id="btnKillProc" style="display:none">⏹</button><span class="toggle-icon">▲</span></div></div>
<div class="terminal-body">
<div class="terminal-history"><div class="history-header"><span>历史记录</span><span class="history-clear" onclick="clearHistory()">清空</span></div><div class="history-list" id="historyList"></div></div>
<div class="terminal-main"><div class="terminal-output" id="terminalOutput"></div>
<div class="completion-popup" id="completionPopup"><div class="completion-list" id="completionList"></div></div>
<div class="terminal-input-row"><span class="prompt" id="terminalPrompt">$</span><input id="terminalInput" class="terminal-input" placeholder="输入命令，按 Tab 补全..." autocomplete="off" spellcheck="false"><button class="term-send-btn" id="termSendBtn">发送</button></div>
</div></div></div>

</div><!-- /.app -->

<div class="ctx-menu" id="ctxMenu"></div>
<div class="preview-bg" id="previewModal"><div class="preview-header"><span class="preview-title" id="previewTitle"></span><div class="preview-actions"><button onclick="downloadFromPreview()">⬇ 下载</button><button class="close" onclick="closePreview()">✕ 关闭 (Esc)</button></div></div><div class="preview-body" id="previewBody"></div></div>
<div class="upload-panel" id="uploadPanel"><div class="upload-header" onclick="toggleUploadPanel(event)"><span class="title"><span id="uploadTitleIcon">⬆</span><span id="uploadTitleText">上传进度</span><span class="file-name" id="uploadFileName"></span></span><button class="close-btn" onclick="closeUploadPanel(event)">✕</button><span class="toggle">▲</span></div><div class="upload-body"><div class="progress-wrap" id="uploadProgressWrap"><div class="fill" id="uploadProgressFill"></div></div><div class="upload-stats"><div>已上传：<b id="uploadedSize">0B</b> / <b id="totalSize">0B</b></div><div>速度：<b id="uploadSpeed">0 KB/s</b></div><div>剩余时间：<b id="remainingTime">--</b></div><div>分片：<b id="currentChunk">0</b> / <b id="totalChunks">0</b></div></div><div class="upload-actions"><button id="btnCancelUpload" onclick="cancelUpload()">取消上传</button></div></div></div>
<div class="process-panel" id="processPanel">
<div class="process-topbar"><div class="process-title">⚙️ 进程管理</div>
<div class="process-stats" id="processStats">
<div class="stat">进程数 <b id="statProcCount">-</b></div>
<div class="stat">CPU <b id="statCpu">-</b></div>
<div class="stat">内存 <b id="statMem">-</b></div>
<div class="stat">主机 <b id="statHost">-</b></div>
</div>
<div class="process-search"><input id="processSearch" placeholder="搜索进程名 / PID / 命令行…" oninput="renderProcesses()"></div>
<label class="process-refresh-toggle"><input type="checkbox" id="processAutoRefresh" checked> 自动刷新</label>
<button class="btn" onclick="refreshProcesses(true)">🔄 刷新</button>
<button class="btn" onclick="closeProcessPanel()">✕ 关闭</button>
</div>
<div class="process-body"><table class="process-table"><thead><tr>
<th class="sortable" data-key="pid">PID <span class="sort-arrow" data-key="pid"></span></th>
<th class="sortable" data-key="name">名称 <span class="sort-arrow" data-key="name"></span></th>
<th class="sortable" data-key="cpu">CPU <span class="sort-arrow" data-key="cpu"></span></th>
<th class="sortable" data-key="memory">内存 <span class="sort-arrow" data-key="memory"></span></th>
<th>命令行</th><th style="width:220px">操作</th>
</tr></thead><tbody id="processList"></tbody></table></div>
<div class="process-footer"><span id="processFooterLeft">—</span><span id="processFooterRight"></span></div>
</div>
<div class="modal-bg" id="rootModal"><div class="modal"><h3>更改根目录</h3><div class="form-row"><label>目录路径（绝对路径）</label><input type="text" id="rootInput" placeholder="例如 D:/" onkeydown="if(event.key==='Enter')doSetRoot()"><div class="hint">切换后所有文件和操作将基于新目录进行</div></div><div class="actions"><button onclick="closeModal('rootModal')">取消</button><button class="primary" onclick="doSetRoot()">确定切换</button></div></div></div>
<div class="modal-bg" id="mkdirModal"><div class="modal"><h3>新建文件夹</h3><div class="form-row"><label>文件夹名称</label><input type="text" id="mkdirName" onkeydown="if(event.key==='Enter')doMkdir()"></div><div class="actions"><button onclick="closeModal('mkdirModal')">取消</button><button class="primary" onclick="doMkdir()">创建</button></div></div></div>
<div class="modal-bg" id="compressModal"><div class="modal"><h3>压缩为 ZIP</h3><div class="form-row"><label>压缩包名称</label><input type="text" id="compressName" placeholder="archive.zip"></div><div class="actions"><button onclick="closeModal('compressModal')">取消</button><button class="primary" onclick="doCompress()">开始压缩</button></div></div></div>
<div class="modal-bg" id="remoteModal"><div class="modal"><h3>远程文件下载</h3><div class="form-row"><label>文件 URL</label><input type="url" id="remoteUrl" placeholder="https://example.com/file.zip"></div><div class="progress-wrap" id="remoteProgressWrap" style="display:none;height:22px;border-radius:11px"><div class="fill" id="remoteProgressFill"></div></div><div id="remoteStatus" style="font-size:12px;color:#7a8299;margin-top:8px"></div><div class="actions"><button onclick="closeModal('remoteModal')">关闭</button><button onclick="downloadToLocal()">下载到本地</button><button class="primary" onclick="downloadToServer()">下载到服务器</button></div></div></div>

<div class="modal-bg" id="renameModal"><div class="modal"><h3>重命名</h3><div class="form-row"><label>新名称</label><input type="text" id="renameInput" onkeydown="if(event.key==='Enter')doRename()"><div class="hint">只改名称，不能输入路径分隔符</div></div><div class="actions"><button onclick="closeModal('renameModal')">取消</button><button class="primary" onclick="doRename()">确定</button></div></div></div>

<div class="modal-bg" id="pickerModal"><div class="modal" style="max-width:560px"><h3 id="pickerTitle">选择目标目录</h3><div class="picker-path" id="pickerPath">/</div><div class="picker-box" id="pickerTree"></div><div class="actions"><button onclick="closeModal('pickerModal')">取消</button><button class="primary" onclick="pickerOk()">选择此目录</button></div></div></div>

<div class="modal-bg" id="userModal"><div class="modal" style="max-width:620px"><h3>用户管理 <span class="tag" id="userTag"></span></h3>
<div id="userAdminBox">
<div class="form-row"><label>普通用户列表</label><div id="userList" style="max-height:180px;overflow:auto;border:1px solid #e6e8ec;border-radius:6px"></div><div class="hint">用户数据保存位置：<span id="userFile" style="font-family:Consolas,monospace"></span></div></div>
<div class="form-row"><label>添加普通用户</label>
<input type="text" id="newUserName" placeholder="用户名" style="margin-bottom:6px">
<input type="text" id="newUserPass" placeholder="密码（至少 4 位）" style="margin-bottom:6px">
<input type="text" id="newUserRoot" placeholder="根目录绝对路径（留空 = 在根目录下创建同名文件夹）" style="margin-bottom:6px">
<div style="display:flex;gap:8px"><button onclick="pickUserRoot()">📂 浏览目录</button><button class="primary" onclick="addUser()">添加用户</button></div>
<div class="hint">普通用户只能在自己根目录内浏览、上传、下载、复制、移动、删除，且看不到终端与进程。</div>
</div>
<div class="divider-line" style="height:1px;background:#eef1f8;margin:14px 0"></div>
</div>
<div class="form-row"><label>修改密码</label>
<input type="text" id="pwName" placeholder="用户名" style="margin-bottom:6px">
<input type="password" id="pwOld" placeholder="原密码（修改自己的密码时必填）" style="margin-bottom:6px">
<input type="password" id="pwNew" placeholder="新密码（至少 4 位）" style="margin-bottom:6px">
<button class="primary" onclick="doPasswd()">修改密码</button>
</div>
<div class="actions"><button onclick="closeModal('userModal')">关闭</button></div></div></div>

<div class="modal-bg" id="uploadModeModal">
  <div class="modal">
    <h3>选择上传方式</h3>
    <div class="mode-cards">
      <div class="mode-card default" onclick="chooseUploadMode('http')">
        <div class="mode-icon">📤</div>
        <div class="mode-title">HTTP 分片上传</div>
        <div class="mode-desc">浏览器直传，支持断点续传<br>32MB 分片，6 并发</div>
        <div class="mode-badge">默认</div>
      </div>
      <div class="mode-card" onclick="chooseUploadMode('ftp')">
        <div class="mode-icon">📁</div>
        <div class="mode-title">FTP 直传</div>
        <div class="mode-desc">复制链接到资源管理器<br>支持断点续传，速度更快</div>
        <div class="mode-badge">大文件推荐</div>
      </div>
    </div>
    <div class="form-row">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
        <input type="checkbox" id="rememberChoice"> 记住我的选择（下次不再询问）
      </label>
    </div>
    <div class="actions">
      <button onclick="closeModal('uploadModeModal')">取消</button>
      <button onclick="resetUploadPreference()">重置默认</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="ftpInfoModal">
  <div class="modal wide">
    <h3>📁 FTP 直传</h3>
    <div class="ftp-link-box" id="ftpLinkBox" onclick="copyFtpLink()">
      <span class="lbl">点此复制 → 粘贴到资源管理器地址栏</span>
      <span id="ftpLinkText">ftp://...</span>
    </div>
    <div class="ftp-info-grid">
      <div><label>服务器</label><span id="ftpHost">-</span></div>
      <div><label>端口</label><span id="ftpPort">-</span></div>
      <div><label>用户名</label><span id="ftpUser">-</span></div>
      <div><label>密码</label><span id="ftpPass">-</span></div>
      <div style="grid-column:1/-1"><label>当前目录</label><span id="ftpCwd">/</span></div>
    </div>
    <div class="ftp-actions">
      <button class="primary" onclick="copyFtpLink()">📋 复制 FTP 链接</button>
      <button onclick="copyFtpInfo()">📋 复制账号信息</button>
      <button onclick="copyFtpCmd()">📋 复制命令行</button>
    </div>
    <div class="ftp-tip">
      <b>📌 按场景推荐：</b><br>
      • <b>一般文件上传</b>：直接用 <b>Windows 资源管理器</b>（Win+E），把复制的 FTP 链接粘贴到地址栏，回车后拖拽文件即可，零安装最省事。<br>
      • <b>超大文件 / 需要断点续传</b>：推荐 <b>FileZilla</b> 或 <b>WinSCP</b>，支持断点续传、多线程并发、队列管理，断网后能自动恢复，是传输几十 GB 以上文件的首选。<br>
      <span style="color:#a06000">⚠️ FTP 为明文协议，仅建议在内网/可信网络下使用。</span>
    </div>
    <div class="actions">
      <button class="primary" onclick="closeModal('ftpInfoModal')">关闭</button>
    </div>
  </div>
</div>

<input type="file" id="fileInput" multiple style="display:none">
<script>
const CHUNK_SIZE={{chunk_size}},CONCURRENCY=6,MAX_RETRY=4;
const IMG_E=new Set(['jpg','jpeg','png','gif','webp','svg','bmp','ico','avif']);
const VID_E=new Set(['mp4','webm','ogg','ogv','mov','m4v','mkv','avi']);
const TXT_E=new Set(['txt','md','json','js','ts','jsx','tsx','py','html','htm','css','xml','csv','log','ini','conf','yaml','yml','sh','bat','cmd','ps1','java','c','cpp','h','hpp','go','rs','rb','php','sql','vue','svelte','toml','env','gitignore','dockerfile','makefile','srt','ass','vtt']);
let currentPath='',currentItems=[],selected=new Set(),lastIdx=-1,histStack=[''],histIdx=0,sortBy='name',order='asc',keyword='',currentRoot='',plat={os:'unix',name:'Linux',shell:'/bin/sh'};
let previewPath='',upFile=null,upUid='',upAbort=false,upBytes=0,speedHist=[],activeChunks=new Map();
let termCwd='',termHist=[],termHistIdx=-1,curJobId=null,evtSrc=null,compActive=false,compItems=[],compIdx=-1;
let procData=[],procSys={},procSortKey='cpu',procSortOrder='desc',procTimer=null,procKw='';
let ftpInfo={enabled:false,host:'',port:2121,user:'',pass:'',passive_ports:''};
let bgTaskTimer=null,bgTaskId=null;
const $=id=>document.getElementById(id);
function toast(msg,type=''){const el=document.createElement('div');el.className='toast '+type;el.textContent=msg;document.body.appendChild(el);setTimeout(()=>{el.style.opacity='0';el.style.transition='.3s'},2400);setTimeout(()=>el.remove(),2800)}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmtSize(b){if(b<0)return'-';if(b<1024)return b+'B';if(b<1048576)return(b/1024).toFixed(1)+'KB';if(b<1073741824)return(b/1048576).toFixed(1)+'MB';return(b/1073741824).toFixed(2)+'GB'}
function fmtTime(s){if(s<60)return s.toFixed(0)+'秒';if(s<3600)return(s/60).toFixed(0)+'分'+(s%60).toFixed(0)+'秒';return(s/3600).toFixed(1)+'小时'}
function fmtBytes(n){n=Number(n)||0;const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return n.toFixed(i===0?0:1)+' '+u[i]}
function fileIcon(it){if(it.is_dir)return'📁';const m={jpg:'🖼️',jpeg:'🖼️',png:'🖼️',gif:'🖼️',webp:'🖼️',svg:'🖼️',bmp:'🖼️',ico:'🖼️',avif:'🖼️',mp4:'🎬',avi:'🎬',mov:'🎬',mkv:'🎬',webm:'🎬',mp3:'🎵',wav:'🎵',flac:'🎵',zip:'🗜️',rar:'🗜️','7z':'🗜️',tar:'🗜️',gz:'🗜️',pdf:'📕',doc:'📘',docx:'📘',xls:'📗',xlsx:'📗',ppt:'📙',pptx:'📙',txt:'📄',md:'📄',json:'📄',js:'📜',ts:'📜',py:'🐍',html:'🌐',css:'🎨',exe:'⚙️',dll:'⚙️',bat:'⚙️',safetensors:'🧠',ckpt:'🧠',pt:'🧠',pth:'🧠'};return m[it.ext]||'📄'}
const isImg=it=>!it.is_dir&&IMG_E.has(it.ext),isVid=it=>!it.is_dir&&VID_E.has(it.ext),isTxt=it=>!it.is_dir&&TXT_E.has(it.ext),isPrev=it=>isImg(it)||isVid(it)||isTxt(it);
async function fretry(url,opt={},n=MAX_RETRY){for(let i=0;i<=n;i++){try{return await fetch(url,opt)}catch(e){if(i===n)throw e;await new Promise(r=>setTimeout(r,300*(i+1)))}}}
function pUrl(p){return'/preview/'+p.split('/').map(encodeURIComponent).join('/')}
async function openPreview(it){previewPath=it.path;$('previewTitle').textContent=it.name;const b=$('previewBody');b.innerHTML='<div style="color:#fff">加载中…</div>';$('previewModal').classList.add('show');
if(isImg(it)){const img=new Image();img.src=pUrl(it.path);img.onload=()=>{b.innerHTML='';b.appendChild(img)};img.onerror=()=>{b.innerHTML='<div class="preview-error">加载失败</div>'}}
else if(isVid(it)){const v=document.createElement('video');v.src=pUrl(it.path);v.controls=true;v.autoplay=true;v.preload='metadata';b.innerHTML='';b.appendChild(v)}
else if(isTxt(it)){try{const r=await fretry('/api/read_text?path='+encodeURIComponent(it.path));const d=await r.json();if(d.success){const pre=document.createElement('pre');pre.className='text-view';pre.textContent=d.content;b.innerHTML='';b.appendChild(pre)}else{b.innerHTML='<div class="preview-error">'+esc(d.error||'失败')+'</div>'}}catch(e){b.innerHTML='<div class="preview-error">'+esc(e.message)+'</div>'}}}
function closePreview(){$('previewModal').classList.remove('show');$('previewBody').innerHTML='';previewPath=''}
function downloadFromPreview(){if(previewPath)downloadFile(previewPath)}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('previewModal').classList.contains('show'))closePreview()});
$('previewModal').addEventListener('click',e=>{if(e.target.id==='previewModal'||e.target.classList.contains('preview-body'))closePreview()});
function showRootModal(){$('rootInput').value=currentRoot;$('rootModal').classList.add('show');setTimeout(()=>{$('rootInput').focus();$('rootInput').select()},50)}
async function doSetRoot(){const p=$('rootInput').value.trim();if(!p){toast('请输入路径','error');return}
try{const r=await fretry('/api/set_root',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p})});const d=await r.json();
if(d.success){currentRoot=d.root;$('rootPathDisplay').textContent=currentRoot;closeModal('rootModal');toast('已切换','success');histStack=[''];histIdx=0;currentPath='';load('')}else{toast(d.error||'失败','error')}}catch(e){toast(e.message,'error')}}
async function apiList(p,kw=keyword,sb=sortBy,od=order){const r=await fretry(`/api/list?path=${encodeURIComponent(p)}&keyword=${encodeURIComponent(kw)}&sort_by=${sb}&order=${od}`);if(!r.ok)throw new Error('加载失败');return r.json()}
async function loadInitRoot(){try{const r=await fretry('/api/get_root');const d=await r.json();currentRoot=d.root;$('rootPathDisplay').textContent=currentRoot}catch(e){}}
async function loadPlat(){try{const r=await fretry('/api/terminal/platform');const d=await r.json();if(d.success){plat=d;$('terminalPlatform').textContent=d.name;$('terminalPrompt').textContent=d.os==='windows'?'>':'$';$('terminalInput').placeholder=d.os==='windows'?'输入命令（如 dir、ipconfig），按 Tab 补全...':'输入命令（如 ls、pwd），按 Tab 补全...'}}catch(e){}}
async function loadFtpInfo(){try{const r=await fretry('/api/ftp/info');const d=await r.json();if(d.success)ftpInfo=d}catch(e){}}

function onUploadClick(){
    if(!ftpInfo.enabled){$('fileInput').click();return}
    const remembered=localStorage.getItem('uploadMode');
    if(remembered==='http'){$('fileInput').click();return}
    if(remembered==='ftp'){showFtpInfo();return}
    $('uploadModeModal').classList.add('show');
}
function chooseUploadMode(mode){
    if($('rememberChoice').checked)localStorage.setItem('uploadMode',mode);
    closeModal('uploadModeModal');
    if(mode==='http')$('fileInput').click();
    else showFtpInfo();
}
function resetUploadPreference(){localStorage.removeItem('uploadMode');toast('已重置','success')}
function showFtpInfo(){
    $('ftpHost').textContent=ftpInfo.host||'-';
    $('ftpPort').textContent=ftpInfo.port||2121;
    $('ftpUser').textContent=ftpInfo.user||'-';
    $('ftpPass').textContent=ftpInfo.pass||'-';
    $('ftpCwd').textContent='/'+(currentPath||'');
    const url=`ftp://${ftpInfo.user}:${ftpInfo.pass}@${ftpInfo.host}:${ftpInfo.port}/${currentPath||''}`;
    $('ftpLinkText').textContent=url;
    $('ftpInfoModal').classList.add('show');
}
function copyText(text,msg){
    const ta=document.createElement('textarea');
    ta.value=text;ta.style.position='fixed';ta.style.left='-9999px';ta.style.top='0';
    document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');toast(msg||'已复制','success')}
    catch(e){toast('复制失败','error')}
    document.body.removeChild(ta);
}
function copyFtpLink(){
    const url=`ftp://${ftpInfo.user}:${ftpInfo.pass}@${ftpInfo.host}:${ftpInfo.port}/${currentPath||''}`;
    copyText(url,'FTP 链接已复制 → 粘贴到资源管理器地址栏');
}
function copyFtpInfo(){
    const text=`服务器: ${ftpInfo.host}\n端口: ${ftpInfo.port}\n用户名: ${ftpInfo.user}\n密码: ${ftpInfo.pass}\n当前目录: /${currentPath||''}`;
    copyText(text,'已复制连接信息');
}
function copyFtpCmd(){
    const host=ftpInfo.host,port=ftpInfo.port,user=ftpInfo.user,pass=ftpInfo.pass;
    const dir='/'+(currentPath||'');
    const text=`# 上传单个文件\ncurl -T localfile.bin ftp://${user}:${pass}@${host}:${port}${dir}/\n\n# 上传目录（需 lftp）\nlftp -u ${user},${pass} -p ${port} ${host} -e "mirror -R ./localdir ${dir}; bye"`;
    copyText(text,'已复制命令');
}

async function load(path,push=true){try{const d=await apiList(path);currentPath=d.path||'';currentItems=d.items||[];selected.clear();lastIdx=-1;
if(push){histStack=histStack.slice(0,histIdx+1);if(histStack[histStack.length-1]!==currentPath){histStack.push(currentPath);histIdx=histStack.length-1}}
 render();updCrumb();updNav();updStatus();updTermCwd();treeReveal()}catch(e){toast('加载失败: '+e.message,'error')}}
function render(){const tb=$('fileList'),emp=$('emptyState');if(!currentItems.length){tb.innerHTML='';emp.style.display='block';return}emp.style.display='none';
let h='';currentItems.forEach((it,idx)=>{const sel=selected.has(it.path)?'selected':'';const prev=isPrev(it);
h+=`<tr class="${sel}" data-idx="${idx}" data-path="${esc(it.path)}"><td class="name-cell"><span class="file-icon">${fileIcon(it)}</span><span>${esc(it.name)}</span></td><td>${esc(it.mtime_str)}</td><td>${esc(it.size_str)}</td><td><div class="row-actions">${prev?`<button onclick="event.stopPropagation();openPreview(currentItems[${idx}])">查看</button>`:''}${it.is_dir?'':`<button onclick="event.stopPropagation();downloadFile('${esc(it.path)}')">下载</button>`}${it.ext==='zip'?`<button onclick="event.stopPropagation();extractOne('${esc(it.path)}')">解压</button>`:''}<button onclick="event.stopPropagation();showRename(currentItems[${idx}])">重命名</button><button class="danger" onclick="event.stopPropagation();deleteOne('${esc(it.path)}','${esc(it.name)}')">删除</button></div></td></tr>`});
tb.innerHTML=h;tb.querySelectorAll('tr').forEach(tr=>{tr.addEventListener('click',onRowClick);tr.addEventListener('dblclick',onRowDblClick);tr.addEventListener('contextmenu',onRowCtx)});
$('sortName').textContent=sortBy==='name'?(order==='asc'?'▲':'▼'):'';$('sortMtime').textContent=sortBy==='mtime'?(order==='asc'?'▲':'▼'):'';$('sortSize').textContent=sortBy==='size'?(order==='asc'?'▲':'▼'):''}
function updCrumb(){const bc=$('breadcrumb');let h=`<span class="crumb ${currentPath===''?'current':''}" onclick="navigate('')">🏠 根目录</span>`;const parts=currentPath.split('/').filter(Boolean);let acc='';
parts.forEach((p,i)=>{acc+=(acc?'/':'')+p;const last=i===parts.length-1;h+=`<span class="sep">›</span>`;h+=last?`<span class="crumb current">${esc(p)}</span>`:`<span class="crumb" onclick="navigate('${esc(acc)}')">${esc(p)}</span>`});bc.innerHTML=h;bc.scrollLeft=bc.scrollWidth}
function updNav(){$('btnBack').disabled=histIdx<=0;$('btnForward').disabled=histIdx>=histStack.length-1;$('btnUp').disabled=currentPath===''}
function updStatus(){$('statusText').textContent=`共 ${currentItems.length} 项`;$('selectionText').textContent=selected.size?`已选中 ${selected.size} 项`:''}
function navigate(p){load(p)}
function goBack(){if(histIdx>0){histIdx--;load(histStack[histIdx],false)}}
function goForward(){if(histIdx<histStack.length-1){histIdx++;load(histStack[histIdx],false)}}
function goUp(){if(!currentPath)return;navigate(currentPath.split('/').slice(0,-1).join('/'))}
function refresh(){load(currentPath,false)}
function doSearch(){keyword=$('searchInput').value.trim();load(currentPath,false)}
document.querySelectorAll('th.sortable').forEach((th,i)=>{th.addEventListener('click',()=>{const k=i===0?'name':i===1?'mtime':'size';if(sortBy===k)order=order==='asc'?'desc':'asc';else{sortBy=k;order='asc'}refresh()})});
function onRowClick(e){const tr=e.currentTarget,idx=+tr.dataset.idx,p=tr.dataset.path;
if(e.ctrlKey||e.metaKey){if(selected.has(p))selected.delete(p);else selected.add(p);lastIdx=idx}
else if(e.shiftKey&&lastIdx>=0){const[a,b]=[Math.min(lastIdx,idx),Math.max(lastIdx,idx)];for(let i=a;i<=b;i++)selected.add(currentItems[i].path)}
else{selected.clear();selected.add(p);lastIdx=idx}
render();updStatus()}
function onRowDblClick(e){const it=currentItems[+e.currentTarget.dataset.idx];if(it.is_dir)navigate(it.path);else if(isPrev(it))openPreview(it);else downloadFile(it.path)}
function onRowCtx(e){e.preventDefault();const p=e.currentTarget.dataset.path;if(!selected.has(p)){selected.clear();selected.add(p);render();updStatus()}showCtxMenu(e.clientX,e.clientY,Array.from(selected))}
function showCtxMenu(x,y,paths){const m=$('ctxMenu');const items=paths.map(p=>currentItems.find(i=>i.path===p)).filter(Boolean);
const allZip=items.length>0&&items.every(i=>!i.is_dir&&i.ext==='zip');const single=items.length===1;
let h='';if(single){const it=items[0];if(it.is_dir)h+=`<div class="item" onclick="ctxOpen()"><span class="icon">📂</span>打开</div>`;else{if(isPrev(it))h+=`<div class="item" onclick="ctxPreview()"><span class="icon">👁️</span>查看</div>`;h+=`<div class="item" onclick="ctxDownload()"><span class="icon">⬇️</span>下载</div>`}h+=`<div class="divider"></div>`}
h+=`<div class="item" onclick="ctxCompress()"><span class="icon">🗜️</span>压缩为 ZIP</div>`;
if(allZip)h+=`<div class="item" onclick="ctxExtract()"><span class="icon">📤</span>解压</div>`;
h+=`<div class="divider"></div><div class="item" onclick="ctxCopyMove('copy')"><span class="icon">📋</span>复制到…</div><div class="item" onclick="ctxCopyMove('move')"><span class="icon">✂️</span>移动到…</div>`+
   `<div class="item" onclick="ctxRename()"><span class="icon">✏️</span>重命名</div>`+
   `<div class="item" onclick="ctxCopyName()"><span class="icon">🔤</span>复制名称</div>`+
   `<div class="item" onclick="ctxCopyPath()"><span class="icon">📍</span>复制绝对路径</div>`+
   `<div class="divider"></div><div class="item danger" onclick="ctxDelete()"><span class="icon">🗑️</span>删除</div>`;
m.innerHTML=h;m.classList.add('show');const r=m.getBoundingClientRect();m.style.left=Math.min(x,innerWidth-r.width-8)+'px';m.style.top=Math.min(y,innerHeight-r.height-8)+'px'}
document.addEventListener('click',()=>$('ctxMenu').classList.remove('show'));
document.addEventListener('scroll',()=>$('ctxMenu').classList.remove('show'),true);
function ctxOpen(){const it=currentItems.find(i=>i.path===Array.from(selected)[0]);if(it&&it.is_dir)navigate(it.path)}
function ctxPreview(){const it=currentItems.find(i=>i.path===Array.from(selected)[0]);if(it&&isPrev(it))openPreview(it)}
function ctxDownload(){Array.from(selected).forEach(p=>downloadFile(p))}
function ctxDelete(){deleteMany(Array.from(selected))}
async function ctxCompress(){$('compressName').value='archive.zip';$('compressModal').classList.add('show');$('compressName').focus();$('compressName').select();window._cp=Array.from(selected)}
async function ctxExtract(){const ps=Array.from(selected);try{const r=await fretry('/api/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:ps,target:currentPath})});const d=await r.json();if(d.success){toast(`已解压 ${d.results.filter(x=>x.success).length}/${ps.length}`,'success');refresh()}else toast(d.error||'失败','error')}catch(e){toast(e.message,'error')}}
function ctxCopyMove(op){const ps=Array.from(selected);if(!ps.length)return;
 pickerOpen(op==='copy'?'选择复制到哪个目录':'选择移动到哪个目录',t=>startBgTask(ps,t,op),currentPath)}
function curOne(){return currentItems.find(i=>i.path===Array.from(selected)[0])}
function ctxRename(){const it=curOne();if(it)showRename(it)}
function ctxCopyName(){const it=curOne();if(it)copyText(it.name,'已复制名称')}
function ctxCopyPath(){const it=curOne();if(it)copyText(it.abs||it.path,'已复制绝对路径')}

async function startBgTask(paths,target,operation){
    try{
        const r=await fretry('/api/batch_async',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths,target,operation})});
        const d=await r.json();
        if(!d.success){toast(d.error||'启动失败','error');return}
        bgTaskId=d.task_id;
        showBgPanel(operation,paths.length);
        startBgPolling();
    }catch(e){toast(e.message,'error')}
}
function showBgPanel(operation,count){
    const p=$('uploadPanel');
    p.classList.remove('collapsed','done');
    p.classList.add('show');
    $('uploadTitleIcon').textContent=operation==='copy'?'📋':'✂️';
    $('uploadTitleText').textContent=operation==='copy'?'复制中':'移动中';
    $('uploadFileName').textContent=`— ${count} 项`;
    $('uploadProgressWrap').classList.add('bg-task');
    $('btnCancelUpload').style.display='none';
    $('uploadSpeed').textContent='--';
    $('remainingTime').textContent='--';
    $('currentChunk').textContent='--';
    $('totalChunks').textContent=count;
}
function startBgPolling(){
    if(bgTaskTimer)clearInterval(bgTaskTimer);
    bgTaskTimer=setInterval(async()=>{
        if(!bgTaskId)return;
        try{
            const r=await fetch(`/api/batch_status/${bgTaskId}`);
            const d=await r.json();
            if(!d.success)return;
            $('uploadProgressFill').style.width=(d.progress||0)+'%';
            $('uploadProgressFill').textContent=(d.progress||0)+'%';
            $('uploadedSize').textContent=d.message||'';
            $('totalSize').textContent=`${d.done||0}/${d.total||0} 项`;
            $('uploadFileName').textContent='— '+d.message;
            if(d.status==='completed'||d.status==='failed'){
                clearInterval(bgTaskTimer);bgTaskTimer=null;
                $('btnCancelUpload').style.display='none';
                $('uploadTitleIcon').textContent='✓';
                if(d.status==='completed'){
                    toast(`${d.type==='copy'?'复制':'移动'}完成 ${d.done}/${d.total} 项`,'success');
                    if(d.errors&&d.errors.length)toast(d.errors[0],'error');
                }else{
                    toast(d.message||'失败','error');
                }
                setTimeout(()=>{closeUploadPanel();refresh()},2000);
                bgTaskId=null;
            }
        }catch(e){}
    },500);
}

async function deleteOne(p,n){if(!confirm(`确定删除「${n}」？`))return;deleteMany([p])}
async function deleteMany(ps){try{const r=await fretry('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:ps})});const d=await r.json();if(d.success){if(d.deleted.length)toast(`已删除 ${d.deleted.length} 项`,'success');if(d.errors?.length)toast(d.errors[0],'error');refresh()}else toast(d.error||'失败','error')}catch(e){toast(e.message,'error')}}
function downloadFile(p){window.location.href='/download/'+encodeURIComponent(p)}
function showMkdir(){$('mkdirName').value='';$('mkdirModal').classList.add('show');$('mkdirName').focus()}
async function doMkdir(){const n=$('mkdirName').value.trim();if(!n){toast('请输入名称','error');return}
try{const r=await fretry('/api/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:currentPath,name:n})});const d=await r.json();if(d.success){toast('成功','success');closeModal('mkdirModal');refresh()}else toast(d.error||'失败','error')}catch(e){toast(e.message,'error')}}
async function doCompress(){const n=$('compressName').value.trim();if(!n){toast('请输入名称','error');return}const ps=window._cp||Array.from(selected);
try{const r=await fretry('/api/compress',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:ps,name:n,current:currentPath})});const d=await r.json();if(d.success){toast('成功','success');closeModal('compressModal');refresh()}else toast(d.message||'失败','error')}catch(e){toast(e.message,'error')}}
async function extractOne(p){try{const r=await fretry('/api/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:[p],target:currentPath})});const d=await r.json();if(d.success&&d.results[0].success){toast('成功','success');refresh()}else toast(d.results?.[0]?.message||'失败','error')}catch(e){toast(e.message,'error')}}
function closeModal(id){$(id).classList.remove('show')}
document.querySelectorAll('.modal-bg').forEach(m=>{m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('show')})});
const fileArea=$('fileArea');
['dragenter','dragover'].forEach(ev=>fileArea.addEventListener(ev,e=>{e.preventDefault();fileArea.classList.add('dragover')}));
['dragleave','drop'].forEach(ev=>fileArea.addEventListener(ev,e=>{e.preventDefault();fileArea.classList.remove('dragover')}));
fileArea.addEventListener('drop',e=>{if(e.dataTransfer.files.length)handleUpload(e.dataTransfer.files)});
$('fileInput').addEventListener('change',e=>{if(e.target.files.length){handleUpload(e.target.files);e.target.value=''}});
function showUpPanel(fn){const p=$('uploadPanel');p.classList.remove('collapsed','done');p.classList.add('show');$('uploadTitleIcon').textContent='⬆';$('uploadTitleText').textContent='上传进度';$('uploadFileName').textContent=fn?`— ${fn}`:'';$('btnCancelUpload').style.display='';$('uploadProgressWrap').classList.remove('bg-task')}
function toggleUploadPanel(e){if(e&&e.target.closest('.close-btn'))return;$('uploadPanel').classList.toggle('collapsed')}
function closeUploadPanel(e){if(e)e.stopPropagation();$('uploadPanel').classList.remove('show')}
function markUpDone(){$('uploadPanel').classList.add('done');$('uploadTitleIcon').textContent='✓';$('btnCancelUpload').style.display='none'}
async function handleUpload(files){for(const f of files)await upOne(f);refresh()}
async function upOne(f){upFile=f;upUid=genUid();upAbort=false;upBytes=0;speedHist=[];activeChunks.clear();showUpPanel(f.name);
$('uploadProgressFill').style.width='0%';$('uploadedSize').textContent='0B';$('totalSize').textContent=fmtSize(f.size);$('uploadSpeed').textContent='0 KB/s';$('remainingTime').textContent='--';$('currentChunk').textContent='0';$('totalChunks').textContent=Math.ceil(f.size/CHUNK_SIZE);
const total=Math.ceil(f.size/CHUNK_SIZE);let up=[];try{const r=await fretry(`/check_chunks?file_uid=${upUid}`);const d=await r.json();up=d.uploaded_chunks||[]}catch(e){}
upBytes=up.length*CHUNK_SIZE;if(upBytes>f.size)upBytes=f.size;updProg();
const pend=[];for(let i=0;i<total;i++)if(!up.includes(i))pend.push(i);
let fin=0,cur=0,failed=null;
async function worker(){while(true){if(upAbort)throw new Error('已取消');if(failed)throw failed;const idx=cur++;if(idx>=pend.length)return;const i=pend[idx];const s=i*CHUNK_SIZE,e=Math.min(s+CHUNK_SIZE,f.size);const blob=f.slice(s,e);
try{const res=await upChunkRetry(i,blob);if(!res.success)throw new Error(`分片 ${i+1} 失败: ${res.message}`);fin++}catch(err){failed=err;throw err}}}
try{const ws=[];for(let k=0;k<CONCURRENCY;k++)ws.push(worker());await Promise.all(ws);
const mr=await fretry('/merge_chunks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_uid:upUid,file_name:f.name,total_chunks:total,current_path:currentPath})});const md=await mr.json();
if(md.success){$('uploadProgressFill').style.width='100%';$('uploadProgressFill').textContent='100%';markUpDone();$('uploadFileName').textContent=`— ${f.name} 成功`;toast(`「${f.name}」成功`,'success');setTimeout(()=>{closeUploadPanel();refresh()},1500)}else throw new Error(md.message)}
catch(e){$('uploadFileName').textContent='— 上传失败';markUpDone();toast('失败: '+e.message,'error')}}
async function upChunkRetry(idx,blob){for(let a=0;a<=MAX_RETRY;a++){if(upAbort)return{success:false,message:'已取消'};const r=await upChunk(idx,blob);if(r.success)return r;if(a<MAX_RETRY)await new Promise(rs=>setTimeout(rs,800*(a+1)));else return r}}
function upChunk(idx,blob){return new Promise(res=>{if(upAbort){res({success:false,message:'已取消'});return}
const fd=new FormData();fd.append('file_uid',upUid);fd.append('chunk_index',idx);fd.append('total_chunks',Math.ceil(upFile.size/CHUNK_SIZE));fd.append('file_name',upFile.name);fd.append('current_path',currentPath);fd.append('chunk',blob);
$('currentChunk').textContent=idx+1;const xhr=new XMLHttpRequest();let ll=0,lt=Date.now();xhr.timeout=600000;
xhr.upload.onprogress=e=>{if(!e.lengthComputable)return;activeChunks.set(idx,e.loaded);const now=Date.now(),dt=(now-lt)/1000;
if(dt>=0.3){const sp=(e.loaded-ll)/dt/1024;speedHist.push(sp);if(speedHist.length>10)speedHist.shift();updProg();ll=e.loaded;lt=now}};
xhr.onload=()=>{activeChunks.delete(idx);if(xhr.status===200){try{res(JSON.parse(xhr.responseText))}catch(e){res({success:false,message:'解析失败'})}}else res({success:false,message:`HTTP ${xhr.status}`})};
xhr.onerror=()=>{activeChunks.delete(idx);res({success:false,message:'网络错误'})};
xhr.onabort=()=>{activeChunks.delete(idx);res({success:false,message:'已取消'})};
xhr.ontimeout=()=>{activeChunks.delete(idx);res({success:false,message:'超时'})};
xhr.open('POST','/upload_chunk');xhr.send(fd)})}
function updProg(){let at=0;activeChunks.forEach(v=>at+=v);const c=Math.min(upBytes+at,upFile.size);const pct=upFile.size?c/upFile.size*100:0;
$('uploadProgressFill').style.width=pct+'%';$('uploadProgressFill').textContent=pct.toFixed(1)+'%';$('uploadedSize').textContent=fmtSize(c);
if(speedHist.length){const avg=speedHist.reduce((a,b)=>a+b,0)/speedHist.length;$('uploadSpeed').textContent=avg.toFixed(0)+' KB/s';$('remainingTime').textContent=fmtTime((upFile.size-c)/(avg*1024))}}
function cancelUpload(){upAbort=true;markUpDone();$('uploadFileName').textContent='— 已取消'}
function genUid(){const t=Date.now(),r=Math.random().toString(36).substring(2,10);return btoa(t+'_'+r+'_'+upFile.name).replace(/[^a-zA-Z0-9]/g,'').substring(0,32)}
function showRemoteDownload(){$('remoteUrl').value='';$('remoteStatus').textContent='';$('remoteProgressWrap').style.display='none';$('remoteModal').classList.add('show')}
let remoteTimer=null;
async function downloadToServer(){const u=$('remoteUrl').value.trim();if(!u){toast('请输入URL','error');return}
$('remoteProgressWrap').style.display='block';$('remoteProgressFill').style.width='0%';$('remoteStatus').textContent='创建任务…';
try{const r=await fretry('/start_server_download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})});const d=await r.json();if(!d.success){toast(d.message,'error');return}
const tid=d.task_id;if(remoteTimer)clearInterval(remoteTimer);remoteTimer=setInterval(async()=>{try{const sr=await fetch(`/download_status?task_id=${tid}`);const sd=await sr.json();$('remoteStatus').textContent=sd.message;$('remoteProgressFill').style.width=sd.progress+'%';$('remoteProgressFill').textContent=sd.progress+'%';
if(sd.status==='completed'||sd.status==='failed'){clearInterval(remoteTimer);if(sd.status==='completed'){toast('下载完成','success');refresh()}}}catch(e){}},1000)}catch(e){$('remoteStatus').textContent='失败: '+e.message}}
function downloadToLocal(){const u=$('remoteUrl').value.trim();if(!u){toast('请输入URL','error');return}window.open(`/download_to_local?url=${encodeURIComponent(u)}`,'_blank')}

function toggleTerminal(e){
    if(e&&e.target.closest('.terminal-header-actions'))return;
    const p=$('terminalPanel');
    p.classList.toggle('collapsed');
    const open=!p.classList.contains('collapsed');
    $('btnTerminal').classList.toggle('active',open);
    if(open)setTimeout(()=>$('terminalInput').focus(),260);
}
function updTermCwd(){termCwd=currentPath;const sep=plat.os==='windows'?'\\':'/';$('terminalCwdDisplay').textContent=termCwd?currentRoot.replace(/\//g,sep)+sep+termCwd.replace(/\//g,sep):currentRoot.replace(/\//g,sep)}
function clearTerminal(){$('terminalOutput').innerHTML=''}
function tApp(text,cls=''){const o=$('terminalOutput');const d=document.createElement('div');d.className=cls;d.textContent=text;o.appendChild(d);o.scrollTop=o.scrollHeight}
function tAppRaw(t){const o=$('terminalOutput');o.appendChild(document.createTextNode(t));o.scrollTop=o.scrollHeight}
function hint(label,cmd){return `<b style="color:#b8b8d0">${label}</b> <span class="hint-cmd" onclick="useHintCmd(this)" data-cmd="${esc(cmd)}">${esc(cmd)}</span>`}
function useHintCmd(el){const c=el.getAttribute('data-cmd');$('terminalInput').value=c;$('terminalInput').focus();hideComp()}
async function sendTermCmd(){if(curJobId){toast('有命令执行中','error');return}
const inp=$('terminalInput'),cmd=inp.value.trim();if(!cmd)return;
const cc=plat.os==='windows'?['cls','clear']:['clear','cls'];if(cc.includes(cmd.toLowerCase())){clearTerminal();inp.value='';return}
inp.value='';hideComp();tApp(`${plat.os==='windows'?'>':'$'} ${cmd}`,'cmd-line');saveHist(cmd);
try{const r=await fretry('/api/terminal/exec',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd,cwd:termCwd})});const d=await r.json();
if(!d.success){tApp(d.error||'失败','error-line');return}
curJobId=d.job_id;$('btnKillProc').style.display='flex';$('termSendBtn').disabled=true;startOutStream(d.job_id)}catch(e){tApp('失败: '+e.message,'error-line')}}
function startOutStream(jid){if(evtSrc)evtSrc.close();evtSrc=new EventSource(`/api/terminal/stream/${jid}`);
evtSrc.addEventListener('message',e=>{try{const d=JSON.parse(e.data);if(d.type==='output')tAppRaw(d.content);else if(d.type==='error')tApp(d.content,'error-line');else if(d.type==='exit')tApp(d.content,'exit-line')}catch(e){}});
evtSrc.addEventListener('done',()=>{evtSrc.close();evtSrc=null;curJobId=null;$('btnKillProc').style.display='none';$('termSendBtn').disabled=false;$('terminalInput').focus()});
evtSrc.addEventListener('heartbeat',()=>{});evtSrc.onerror=()=>{if(evtSrc){evtSrc.close();evtSrc=null}curJobId=null;$('btnKillProc').style.display='none';$('termSendBtn').disabled=false}}
async function killTerminalProcess(){if(!curJobId)return;if(!confirm('确定终止当前命令？'))return;
try{await fretry(`/api/terminal/kill/${curJobId}`,{method:'POST'});tApp('[已终止]','exit-line')}catch(e){toast(e.message,'error')}}
function saveHist(c){termHist=termHist.filter(h=>h!==c);termHist.unshift(c);if(termHist.length>200)termHist.pop();termHistIdx=-1;renderHist()}
function renderHist(){$('historyList').innerHTML=termHist.slice(0,50).map(c=>`<div class="history-item" onclick="useHistCmd('${esc(c)}')" title="${esc(c)}">${esc(c)}</div>`).join('')}
function useHistCmd(c){$('terminalInput').value=c;$('terminalInput').focus();hideComp()}
async function clearHistory(){if(!confirm('清空历史？'))return;termHist=[];renderHist();try{await fretry('/api/terminal/clear_history',{method:'POST'})}catch(e){}}
async function loadHistSrv(){try{const r=await fretry('/api/terminal/history?limit=100');const d=await r.json();if(d.success){termHist=d.history.map(h=>h.command);renderHist()}}catch(e){}}
async function handleTab(){const inp=$('terminalInput'),cursor=inp.selectionStart,before=inp.value.substring(0,cursor);
try{const r=await fretry(`/api/terminal/completions?partial=${encodeURIComponent(before)}&cwd=${encodeURIComponent(termCwd)}`);const d=await r.json();
if(!d.success||!d.completions.length){insAtCursor(inp,'    ');return}
if(d.completions.length===1){applyComp(inp,d.completions[0]);hideComp();return}
showComp(d.completions)}catch(e){insAtCursor(inp,'    ')}}
function applyComp(inp,c){const cur=inp.selectionStart,before=inp.value.substring(0,cur),lastSp=before.lastIndexOf(' ');
let nt,np;if(lastSp===-1){nt=c+inp.value.substring(cur);np=c.length}else{const pre=inp.value.substring(0,lastSp+1),suf=inp.value.substring(cur);nt=pre+c+suf;np=pre.length+c.length}
inp.value=nt;inp.selectionStart=inp.selectionEnd=np}
function showComp(items){const p=$('completionPopup');compItems=items;compIdx=0;compActive=true;
$('completionList').innerHTML=items.map((it,i)=>{const d=it.endsWith('/')||it.endsWith('\\'),f=it.startsWith('-')||it.startsWith('/');const ic=d?'📁':(f?'⚙️':'📄');return`<div class="completion-item ${i===0?'active':''}" data-i="${i}" onclick="selComp(${i})"><span class="cmd-icon">${ic}</span><span>${esc(it)}</span></div>`}).join('');
p.classList.add('show')}
function selComp(i){applyComp($('terminalInput'),compItems[i]);hideComp();$('terminalInput').focus()}
function hideComp(){$('completionPopup').classList.remove('show');compActive=false;compItems=[];compIdx=-1}
function moveComp(d){if(!compActive||!compItems.length)return;compIdx=(compIdx+d+compItems.length)%compItems.length;
document.querySelectorAll('.completion-item').forEach((el,i)=>el.classList.toggle('active',i===compIdx));
const a=document.querySelector('.completion-item.active');if(a)a.scrollIntoView({block:'nearest'})}
function insAtCursor(inp,t){const s=inp.selectionStart,e=inp.selectionEnd,v=inp.value;inp.value=v.substring(0,s)+t+v.substring(e);inp.selectionStart=inp.selectionEnd=s+t.length}
function setupTermInput(){const inp=$('terminalInput');
inp.addEventListener('keydown',async e=>{
if(e.key==='Tab'){e.preventDefault();if(compActive)moveComp(1);else await handleTab();return}
if(e.key==='ArrowUp'||e.key==='ArrowDown'){if(compActive){e.preventDefault();moveComp(e.key==='ArrowUp'?-1:1);return}
e.preventDefault();if(!termHist.length)return;if(e.key==='ArrowUp')termHistIdx=Math.min(termHistIdx+1,termHist.length-1);else termHistIdx=Math.max(termHistIdx-1,-1);
inp.value=termHistIdx===-1?'':termHist[termHistIdx];return}
if(e.key==='Enter'){e.preventDefault();if(compActive){selComp(compIdx);return}sendTermCmd();return}
if(e.key==='Escape'){hideComp();return}
if(compActive&&!['Shift','Control','Alt'].includes(e.key)){setTimeout(()=>{if(compActive&&!inp.value.length)hideComp()},50)}});
inp.addEventListener('input',()=>{if(compActive){clearTimeout(inp._ct);inp._ct=setTimeout(()=>handleTab(),300)}});
inp.addEventListener('blur',()=>setTimeout(hideComp,200))}
async function initTerm(){await loadPlat();setupTermInput();loadHistSrv();updTermCwd();$('termSendBtn').addEventListener('click',sendTermCmd);
const w=plat.os==='windows';
tApp(`远程命令面板 [${plat.name}]`,'info-line');
if(w){
    const help=document.createElement('div');
    help.innerHTML=`
<div style="color:#ffd94a;font-weight:600;margin-top:8px">📦 压缩 / 解压</div>
<div class="hint-line">ZIP 压缩：&nbsp;${hint('', 'Compress-Archive -Path .\\folder -DestinationPath archive.zip')}</div>
<div class="hint-line">ZIP 解压：&nbsp;${hint('', 'Expand-Archive -Path archive.zip -DestinationPath .\\out')}</div>
<div class="hint-line">7z 压缩：&nbsp;${hint('', '7z a archive.7z .\\folder')}</div>
<div class="hint-line">7z 解压：&nbsp;${hint('', '7z x archive.7z -o.\\out')}</div>
<div class="hint-line">tar 打包：&nbsp;${hint('', 'tar -czf archive.tar.gz .\\folder')}</div>
<div class="hint-line">tar 解包：&nbsp;${hint('', 'tar -xzf archive.tar.gz')}</div>
<div style="color:#ffd94a;font-weight:600;margin-top:8px">🌐 端口 / 网络</div>
<div class="hint-line">查端口占用：&nbsp;${hint('', 'netstat -ano | findstr :{{ http_port }}')}</div>
<div class="hint-line">查全部监听：&nbsp;${hint('', 'netstat -ano | findstr LISTENING')}</div>
<div class="hint-line">按 PID 查进程：&nbsp;${hint('', 'tasklist | findstr 12345')}</div>
<div class="hint-line">查 IP 配置：&nbsp;${hint('', 'ipconfig /all')}</div>
<div class="hint-line">测试连通：&nbsp;${hint('', 'ping -n 4 8.8.8.8')}</div>
<div style="color:#ffd94a;font-weight:600;margin-top:8px">🗑️ 强制删除</div>
<div class="hint-line">强删文件：&nbsp;${hint('', 'del /f /q file.txt')}</div>
<div class="hint-line">强删目录：&nbsp;${hint('', 'rmdir /s /q .\\folder')}</div>
<div class="hint-line">递归强删：&nbsp;${hint('', 'del /f /s /q .\\folder\\*')}</div>
<div style="color:#ffd94a;font-weight:600;margin-top:8px">🔍 文件 / 目录</div>
<div class="hint-line">列目录：&nbsp;${hint('', 'dir /a /o:g')}</div>
<div class="hint-line">递归查找：&nbsp;${hint('', 'dir /s /b *.log')}</div>
<div class="hint-line">文本搜索：&nbsp;${hint('', 'findstr /s /i "keyword" *.txt')}</div>
<div style="color:#ffd94a;font-weight:600;margin-top:8px">⚙️ 进程</div>
<div class="hint-line">列进程：&nbsp;${hint('', 'tasklist')}</div>
<div class="hint-line">按名强杀：&nbsp;${hint('', 'taskkill /f /im notepad.exe')}</div>
<div class="hint-line">按 PID 强杀：&nbsp;${hint('', 'taskkill /f /pid 12345')}</div>
<div style="color:#7a8299;margin-top:10px;font-size:11px">💡 点击绿色命令可自动填入输入框 &nbsp;|&nbsp; Tab 补全 / ↑↓ 历史 / Enter 执行</div>`;
    $('terminalOutput').appendChild(help);
}else{
    const help=document.createElement('div');
    help.innerHTML=`
<div style="color:#ffd94a;font-weight:600;margin-top:8px">📦 压缩 / 解压</div>
<div class="hint-line">ZIP 压缩：&nbsp;${hint('', 'zip -r archive.zip folder/')}</div>
<div class="hint-line">ZIP 解压：&nbsp;${hint('', 'unzip archive.zip -d out/')}</div>
<div class="hint-line">7z 压缩：&nbsp;${hint('', '7z a archive.7z folder/')}</div>
<div class="hint-line">7z 解压：&nbsp;${hint('', '7z x archive.7z -oout/')}</div>
<div class="hint-line">tar 打包：&nbsp;${hint('', 'tar -czf archive.tar.gz folder/')}</div>
<div class="hint-line">tar 解包：&nbsp;${hint('', 'tar -xzf archive.tar.gz')}</div>
<div style="color:#ffd94a;font-weight:600;margin-top:8px">🌐 端口 / 网络</div>
<div class="hint-line">查端口：&nbsp;${hint('', 'ss -tulnp | grep {{ http_port }}')}</div>
<div class="hint-line">或：&nbsp;${hint('', 'netstat -tulnp | grep {{ http_port }}')}</div>
<div class="hint-line">查 IP：&nbsp;${hint('', 'ip addr show')}</div>
<div class="hint-line">测试连通：&nbsp;${hint('', 'ping -c 4 8.8.8.8')}</div>
<div style="color:#ffd94a;font-weight:600;margin-top:8px">🗑️ 强制删除</div>
<div class="hint-line">强删文件：&nbsp;${hint('', 'rm -f file.txt')}</div>
<div class="hint-line">强删目录：&nbsp;${hint('', 'rm -rf folder/')}</div>
<div style="color:#ffd94a;font-weight:600;margin-top:8px">🔍 文件 / 目录</div>
<div class="hint-line">列目录：&nbsp;${hint('', 'ls -lah')}</div>
<div class="hint-line">递归查找：&nbsp;${hint('', 'find . -name "*.log"')}</div>
<div class="hint-line">文本搜索：&nbsp;${hint('', 'grep -rn "keyword" .')}</div>
<div class="hint-line">磁盘占用：&nbsp;${hint('', 'df -h')}</div>
<div style="color:#ffd94a;font-weight:600;margin-top:8px">⚙️ 进程</div>
<div class="hint-line">列进程：&nbsp;${hint('', 'ps aux')}</div>
<div class="hint-line">强杀：&nbsp;${hint('', 'kill -9 12345')}</div>
<div style="color:#7a8299;margin-top:10px;font-size:11px">💡 点击绿色命令可自动填入输入框 &nbsp;|&nbsp; Tab 补全 / ↑↓ 历史 / Enter 执行</div>`;
    $('terminalOutput').appendChild(help);
}
$('terminalOutput').scrollTop=0;}

async function openProcessPanel(){$('processPanel').classList.add('show');$('btnProcess').classList.add('active');await refreshProcesses(true);startProcAuto()}
function closeProcessPanel(){$('processPanel').classList.remove('show');$('btnProcess').classList.remove('active');stopProcAuto()}
function startProcAuto(){stopProcAuto();if($('processAutoRefresh').checked)procTimer=setInterval(()=>{if($('processPanel').classList.contains('show'))refreshProcesses(false)},3000)}
function stopProcAuto(){if(procTimer){clearInterval(procTimer);procTimer=null}}
$('processAutoRefresh').addEventListener('change',()=>{if($('processAutoRefresh').checked)startProcAuto();else stopProcAuto()});
async function refreshProcesses(force){try{const r=await fretry('/api/process/list'+(force?'?force=1':''));const d=await r.json();
if(!d.success){toast(d.error||'获取失败','error');return}
procData=d.processes||[];procSys=d.system||{};
$('statProcCount').textContent=procData.length;
const totalCpu=procData.reduce((a,b)=>a+(b.cpu||0),0);
$('statCpu').textContent=totalCpu.toFixed(1)+'%';
$('statMem').textContent=procSys.totalMemory?`${fmtBytes(procSys.totalMemory-procSys.freeMemory)} / ${fmtBytes(procSys.totalMemory)}`:'-';
$('statHost').textContent=procSys.hostname||'-';
renderProcesses();updProcFooter()}catch(e){toast('获取失败: '+e.message,'error')}}
function renderProcesses(){const kw=($('processSearch').value||'').trim().toLowerCase();
let arr=procData.slice();
if(kw)arr=arr.filter(p=>(p.name||'').toLowerCase().includes(kw)||String(p.pid).includes(kw)||(p.command||'').toLowerCase().includes(kw));
const k=procSortKey,o=procSortOrder==='desc'?-1:1;
arr.sort((a,b)=>{const va=a[k]??0,vb=b[k]??0;if(typeof va==='string')return va.localeCompare(vb)*o;return(va-vb)*o});
document.querySelectorAll('.process-table .sort-arrow').forEach(el=>{el.textContent=el.dataset.key===k?(procSortOrder==='asc'?'▲':'▼'):''});
const tbody=$('processList');let h='';
arr.forEach(p=>{const cpu=p.cpu||0;const cpuCls=cpu<1?'cpu-low':cpu<10?'cpu-mid':'cpu-high';
const prot=isProtected(p.name);
h+=`<tr><td class="pid-cell">${p.pid}</td><td class="name-cell" title="${esc(p.name)}">${esc(p.name)}</td>
<td class="cpu-cell"><span class="cpu-badge ${cpuCls}">${cpu.toFixed(1)}%</span></td>
<td class="mem-cell"><span class="mem-badge">${fmtBytes(p.memory)}</span></td>
<td class="cmd-cell" title="${esc(p.command)}">${esc(p.command)||'-'}</td>
<td><div class="process-actions">
<button onclick="procDetail(${p.pid})">详情</button>
<button onclick="procCopyCmd(${p.pid}, this)">复制命令</button>
<button class="danger" onclick="procKill(${p.pid},'${esc(p.name)}', this)" ${prot?'disabled title="系统关键进程"':''}>结束</button>
</div></td></tr>`});
tbody.innerHTML=h||'<tr><td colspan="6" style="text-align:center;padding:40px;color:#a0a8b8">没有匹配的进程</td></tr>'}
function updProcFooter(){$('processFooterLeft').textContent=`共 ${procData.length} 个进程`;$('processFooterRight').textContent=procSys.cpuCount?`${procSys.cpuCount} 逻辑核心`:''}
function isProtected(name){name=(name||'').toLowerCase();const L={windows:['system','system idle process','registry','memory compression','smss.exe','csrss.exe','wininit.exe','services.exe','lsass.exe','winlogon.exe','fontdrvhost.exe','dwm.exe','sihost.exe','ctfmon.exe'],unix:['init','systemd','kernel_task','launchd','kthreadd','kworker']};const list=L[plat.os]||[];return list.some(x=>name===x||name.startsWith(x))}
document.querySelectorAll('.process-table th.sortable').forEach(th=>{th.addEventListener('click',()=>{const k=th.dataset.key;if(procSortKey===k)procSortOrder=procSortOrder==='asc'?'desc':'asc';else{procSortKey=k;procSortOrder='desc'}renderProcesses()})});
function procDetail(pid){const p=procData.find(x=>x.pid===pid);if(!p)return;alert(`PID: ${p.pid}\n名称: ${p.name}\nCPU: ${p.cpu}%\n内存: ${fmtBytes(p.memory)}\n命令行:\n${p.command||'-'}`)}
async function procCopyCmd(pid,btn){
    const p=procData.find(x=>x.pid===pid);if(!p){toast('进程不存在','error');return}
    const text=(p.command||'').trim();
    if(!text){toast('命令行内容为空，无可复制','error');return}
    const orig=btn?btn.textContent:'';
    if(btn){btn.disabled=true;btn.textContent='复制中…'}
    const autoBox=document.getElementById('processAutoRefresh');
    const wasAuto=autoBox&&autoBox.checked;
    if(wasAuto)stopProcAuto();
    try{
        let ok=false;
        if(navigator.clipboard&&window.isSecureContext){
            try{await navigator.clipboard.writeText(text);ok=true}catch(e){ok=false}
        }
        if(!ok){
            const ta=document.createElement('textarea');
            ta.value=text;ta.style.position='fixed';ta.style.left='-9999px';ta.style.top='0';
            ta.setAttribute('readonly','');
            document.body.appendChild(ta);ta.select();
            ta.setSelectionRange(0,ta.value.length);
            try{ok=document.execCommand('copy')}catch(e){ok=false}
            document.body.removeChild(ta);
        }
        if(ok)toast('已复制命令行','success');
        else toast('复制失败，请手动选择','error');
    }finally{
        if(btn){btn.disabled=false;btn.textContent=orig||'复制命令'}
        if(wasAuto)startProcAuto();
    }
}
async function procKill(pid,name,btn){
    if(!confirm(`确定结束进程「${name}」(PID: ${pid})？`))return;
    const orig=btn?btn.textContent:'';
    if(btn){btn.disabled=true;btn.textContent='结束中…'}
    const autoBox=document.getElementById('processAutoRefresh');
    const wasAuto=autoBox&&autoBox.checked;
    if(wasAuto)stopProcAuto();
    try{
        const r=await fretry('/api/process/kill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pid})});
        const d=await r.json();
        if(d.success){toast('已结束','success');setTimeout(()=>refreshProcesses(true),500)}
        else{toast(d.error||'失败','error');if(btn){btn.disabled=false;btn.textContent=orig||'结束'}}
    }catch(e){toast(e.message,'error');if(btn){btn.disabled=false;btn.textContent=orig||'结束'}}
    finally{if(wasAuto)startProcAuto()}
}

/* ================= 目录树 / 目录选择器 / 重命名 / 用户管理 ================= */
const treeIndex={};
async function treeFetch(path){const r=await fretry('/api/tree?path='+encodeURIComponent(path));const d=await r.json();return d.dirs||[]}
async function treeEnsure(kids,tog,path,factory){
  if(kids.dataset.loaded!=='1'){
    kids.innerHTML='<div class="tree-empty">加载中…</div>';
    let dirs=[];try{dirs=await treeFetch(path)}catch(e){}
    kids.innerHTML='';
    if(!dirs.length)kids.innerHTML='<div class="tree-empty">（无子目录）</div>';
    else dirs.forEach(d=>kids.appendChild(factory(d.path,d.name)));
    kids.dataset.loaded='1';
  }
  kids.style.display='';tog.textContent='▾';
}
function treeToggle(kids,tog,path,factory){
  if(kids.style.display==='none')treeEnsure(kids,tog,path,factory);
  else{kids.style.display='none';tog.textContent='▸'}
}
function treeNode(path,name){
  const wrap=document.createElement('div');wrap.className='tree-node';
  const row=document.createElement('div');row.className='tree-row';row.dataset.path=path;
  const tog=document.createElement('span');tog.className='tree-tog';tog.textContent='▸';
  const lbl=document.createElement('span');lbl.className='tree-label';lbl.textContent=name||'🏠 根目录';lbl.title=path||'/';
  const kids=document.createElement('div');kids.className='tree-kids';kids.style.display='none';
  row.appendChild(tog);row.appendChild(lbl);wrap.appendChild(row);wrap.appendChild(kids);
  tog.onclick=ev=>{ev.stopPropagation();treeToggle(kids,tog,path,treeNode)};
  row.onclick=()=>navigate(path);
  row.ondblclick=ev=>{ev.stopPropagation();treeToggle(kids,tog,path,treeNode)};
  treeIndex[path]={row:row,kids:kids,tog:tog};
  return wrap;
}
async function treeInit(){const box=$('treeRoot');if(!box)return;box.innerHTML='';box.appendChild(treeNode('','🏠 根目录'));
  const n=treeIndex[''];if(n)await treeEnsure(n.kids,n.tog,'',treeNode);treeMark()}
function treeMark(){document.querySelectorAll('#treeRoot .tree-row.active').forEach(e=>e.classList.remove('active'));
  const n=treeIndex[currentPath];if(n)n.row.classList.add('active')}
async function treeReveal(){const parts=currentPath.split('/').filter(Boolean);let cur='';
  for(const p of parts){const n=treeIndex[cur];if(!n)break;if(n.kids.dataset.loaded!=='1')await treeEnsure(n.kids,n.tog,cur,treeNode);cur=cur?cur+'/'+p:p}
  treeMark()}

let pickerCb=null,pickerTarget='',pickerIdx={};
function pickerNode(path,name){
  const wrap=document.createElement('div');wrap.className='tree-node';
  const row=document.createElement('div');row.className='tree-row';row.dataset.path=path;
  const tog=document.createElement('span');tog.className='tree-tog';tog.textContent='▸';
  const lbl=document.createElement('span');lbl.className='tree-label';lbl.textContent=name||'🏠 根目录';lbl.title=path||'/';
  const kids=document.createElement('div');kids.className='tree-kids';kids.style.display='none';
  row.appendChild(tog);row.appendChild(lbl);wrap.appendChild(row);wrap.appendChild(kids);
  tog.onclick=ev=>{ev.stopPropagation();treeToggle(kids,tog,path,pickerNode)};
  row.onclick=()=>pickerPick(path,row);
  row.ondblclick=ev=>{ev.stopPropagation();treeToggle(kids,tog,path,pickerNode)};
  pickerIdx[path]={row:row,kids:kids,tog:tog};
  return wrap;
}
function pickerPick(path,row){
  pickerTarget=path;$('pickerPath').textContent=path||'/';
  document.querySelectorAll('#pickerTree .tree-row.active').forEach(e=>e.classList.remove('active'));
  if(row)row.classList.add('active');
}
async function pickerOpen(title,cb,start){
  pickerCb=cb;pickerTarget=start||'';pickerIdx={};
  $('pickerTitle').textContent=title;$('pickerPath').textContent=pickerTarget||'/';
  const box=$('pickerTree');box.innerHTML='';box.appendChild(pickerNode('','🏠 根目录'));
  $('pickerModal').classList.add('show');
  const n=pickerIdx[''];if(n)await treeEnsure(n.kids,n.tog,'',pickerNode);
  if(pickerTarget&&pickerIdx[pickerTarget])pickerPick(pickerTarget,pickerIdx[pickerTarget].row);
}
function pickerOk(){closeModal('pickerModal');const cb=pickerCb;pickerCb=null;if(cb)cb(pickerTarget)}

let renameTarget=null;
function showRename(it){if(!it)return;renameTarget=it;$('renameInput').value=it.name;
  $('renameModal').classList.add('show');
  setTimeout(()=>{const i=$('renameInput');i.focus();const dot=it.is_dir?-1:it.name.lastIndexOf('.');
    i.setSelectionRange(0,dot>0?dot:it.name.length)},60)}
async function doRename(){
  const nn=$('renameInput').value.trim();
  if(!renameTarget||!nn){toast('请输入新名称','error');return}
  try{const r=await fretry('/api/rename',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:renameTarget.path,name:nn})});
    const d=await r.json();
    if(d.success){closeModal('renameModal');toast('已重命名为 '+nn,'success');
      const it=renameTarget;renameTarget=null;await load(currentPath,false);
      if(it.is_dir){treeInit()}}
    else toast(d.error||'重命名失败','error')
  }catch(e){toast(e.message,'error')}
}

let me={name:'',admin:true,root:''};
async function loadMe(){try{const r=await fretry('/api/me');const d=await r.json();
  if(d.success){me=d;const b=$('userName');if(b)b.textContent=d.name;const t=$('userTag');if(t)t.textContent=d.admin?'超级用户':'普通用户';applyRole()}}catch(e){}}
function applyRole(){
  if(me.admin)return;
  ['btnProcess','btnTerminal'].forEach(id=>{const b=$(id);if(b)b.style.display='none'});
  const rb=document.querySelector('.root-btn');if(rb)rb.style.display='none';
  ['terminalPanel','processPanel'].forEach(id=>{const e=$(id);if(e)e.style.display='none'});
}
function openUserModal(){$('userModal').classList.add('show');
  $('pwName').value=me.name;
  $('userAdminBox').style.display=me.admin?'':'none';
  if(me.admin)loadUsers();}
async function loadUsers(){try{const r=await fretry('/api/users');const d=await r.json();
  if(!d.success){toast(d.error||'加载失败','error');return}
  $('userFile').textContent=d.file||'';
  let h='';d.users.forEach(u=>{h+=`<div class="user-row"><span class="uname">${esc(u.name)}${u.admin?'（超级用户）':''}</span>`+
    `<span class="uroot" title="${esc(u.root||'')}">${esc(u.root||'')}</span>`+
    `<button class="mini" onclick="fillPw('${esc(u.name)}')">改密码</button>`+
    (u.admin?'':`<button class="mini danger" onclick="delUser('${esc(u.name)}')">删除</button>`)+`</div>`});
  $('userList').innerHTML=h||'<div class="tree-empty">暂无普通用户</div>';}catch(e){}}
function fillPw(n){$('pwName').value=n;$('pwOld').value='';$('pwNew').value='';$('pwNew').focus()}
async function addUser(){
  const n=$('newUserName').value.trim(),p=$('newUserPass').value,r=$('newUserRoot').value.trim();
  if(!n||!p){toast('用户名和密码不能为空','error');return}
  try{const rs=await fretry('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:n,password:p,root:r})});
    const d=await rs.json();
    if(d.success){toast('已添加用户 '+n,'success');$('newUserName').value='';$('newUserPass').value='';$('newUserRoot').value='';loadUsers()}
    else toast(d.error||'添加失败','error')}catch(e){toast(e.message,'error')}}
async function delUser(n){if(!confirm('确定删除用户 '+n+' ？该用户将无法再登录。'))return;
  try{const r=await fretry('/api/users',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});
    const d=await r.json();if(d.success){toast('已删除 '+n,'success');loadUsers()}else toast(d.error||'删除失败','error')}catch(e){toast(e.message,'error')}}
async function doPasswd(){
  const n=$('pwName').value.trim()||me.name,o=$('pwOld').value,np=$('pwNew').value;
  if(!np){toast('请输入新密码','error');return}
  try{const r=await fretry('/api/passwd',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:n,old_password:o,new_password:np})});
    const d=await r.json();
    if(d.success){toast('密码已修改','success');$('pwOld').value='';$('pwNew').value='';$('pwName').value=me.name}
    else toast(d.error||'修改失败','error')}catch(e){toast(e.message,'error')}}
function pickUserRoot(){pickerOpen('选择该用户的根目录',rel=>{
  $('newUserRoot').value=rel?(currentRoot.replace(/[\\/]+$/,'')+'/'+rel):currentRoot;},'')}

(async()=>{await loadMe();await loadInitRoot();loadFtpInfo();await treeInit();load('');if(me.admin)initTerm()})();
</script></body></html>'''


# ================= 路由 =================
@app.errorhandler(RequestEntityTooLarge)
def _413(e): return jsonify({'success':False,'message':'文件过大'}), 413


@app.route('/')
def idx(): return render_template_string(TPL, chunk_size=CHUNK_SIZE, ROOT_DIR=get_root(), http_port=HTTP_PORT)


@app.route('/api/ftp/info')
def api_ftp_info():
    r = _admin_only()
    if r: return r
    return jsonify({
        'success': True,
        'enabled': FTP_AVAILABLE,
        'host': get_local_ip(),
        'port': FTP_PORT,
        'user': _auth['user'],
        'pass': _auth['pass'],
        'passive_ports': f'{FTP_PASSIVE_PORTS.start}-{FTP_PASSIVE_PORTS.stop-1}',
    })


@app.route('/api/get_root')
def api_get_root(): return jsonify({'success':True,'root':get_root()})


@app.route('/api/set_root', methods=['POST'])
def api_set_root():
    r = _admin_only()
    if r: return r
    p = (request.get_json() or {}).get('path','').strip()
    if not p: return jsonify({'success':False,'error':'路径为空'})
    ok, r = set_root(p)
    return jsonify({'success':True,'root':r}) if ok else jsonify({'success':False,'error':r})


@app.route('/api/me')
def api_me():
    u = current_user()
    return jsonify({'success': True, 'name': u['name'], 'admin': u['admin'], 'root': u['root']})


@app.route('/api/rename', methods=['POST'])
def api_rename():
    d = request.get_json() or {}
    p, nn = d.get('path', ''), (d.get('name') or '').strip()
    if not p or not nn: return jsonify({'success': False, 'error': '参数缺失'})
    if '/' in nn or '\\' in nn or nn in ('.', '..'):
        return jsonify({'success': False, 'error': '名称不能包含路径分隔符'})
    root = os.path.abspath(get_root())
    src = abspath(p)
    if os.path.normcase(src) == os.path.normcase(root):
        return jsonify({'success': False, 'error': '不能重命名根目录'})
    if not os.path.exists(src):
        return jsonify({'success': False, 'error': '文件不存在'})
    dst = os.path.join(os.path.dirname(src), nn)
    if not _inside(dst, root):
        return jsonify({'success': False, 'error': '目标越出根目录'})
    if os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
        return jsonify({'success': False, 'error': '同名文件已存在'})
    try:
        os.rename(src, dst)
        return jsonify({'success': True, 'name': nn,
                        'path': os.path.relpath(dst, root).replace(os.sep, '/')})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/tree')
def api_tree():
    p = request.args.get('path', '')
    ap, root = abspath(p), os.path.abspath(get_root())
    if not os.path.isdir(ap):
        return jsonify({'success': False, 'error': '不是目录', 'dirs': []})
    dirs = []
    try:
        for n in sorted(os.listdir(ap), key=lambda s: s.lower()):
            fp = os.path.join(ap, n)
            try:
                if os.path.isdir(fp):
                    dirs.append({'name': n, 'path': os.path.relpath(fp, root).replace(os.sep, '/')})
            except OSError:
                continue
    except OSError as e:
        return jsonify({'success': False, 'error': str(e), 'dirs': []})
    return jsonify({'success': True, 'dirs': dirs[:500]})


@app.route('/api/users', methods=['GET', 'POST', 'DELETE'])
def api_users():
    r = _admin_only()
    if r: return r
    d = request.get_json(silent=True) or {}
    if request.method == 'GET':
        out = [{'name': _auth['user'], 'admin': True, 'root': _cfg['root']}]
        for n, rec in sorted(_users.items()):
            if n == _auth['user']: continue
            out.append({'name': n, 'admin': False, 'root': rec.get('root', '')})
        return jsonify({'success': True, 'users': out, 'file': USERS_FILE})
    name = (d.get('name') or '').strip()
    if request.method == 'DELETE':
        if not name or name == _auth['user']:
            return jsonify({'success': False, 'error': '不能删除超级用户'})
        if name not in _users:
            return jsonify({'success': False, 'error': '用户不存在'})
        _users.pop(name, None)
        return jsonify({'success': True}) if save_users() else jsonify({'success': False, 'error': '保存失败'})
    pw = d.get('password') or ''
    root = (d.get('root') or '').strip()
    if not name or not pw:
        return jsonify({'success': False, 'error': '用户名和密码不能为空'})
    if '/' in name or '\\' in name or name == _auth['user']:
        return jsonify({'success': False, 'error': '用户名不可用'})
    if len(pw) < 4:
        return jsonify({'success': False, 'error': '密码至少 4 位'})
    if root:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            return jsonify({'success': False, 'error': '根目录不存在'})
    else:
        root = os.path.join(_cfg['root'], name)
        try: os.makedirs(root, exist_ok=True)
        except OSError as e: return jsonify({'success': False, 'error': f'根目录创建失败: {e}'})
    salt, h = _pw_hash(pw)
    _users[name] = {'salt': salt, 'hash': h, 'admin': False, 'root': root}
    if not save_users():
        return jsonify({'success': False, 'error': '保存失败，请检查程序目录写入权限'})
    return jsonify({'success': True, 'root': root})


@app.route('/api/passwd', methods=['POST'])
def api_passwd():
    d = request.get_json() or {}
    me = current_user()
    name = (d.get('name') or me['name']).strip()
    old, new = d.get('old_password') or '', d.get('new_password') or ''
    if not new: return jsonify({'success': False, 'error': '新密码不能为空'})
    if len(new) < 4: return jsonify({'success': False, 'error': '新密码至少 4 位'})
    if name != me['name']:
        if not me['admin']:
            return jsonify({'success': False, 'error': '需要超级用户权限'})
    elif not _verify(me['name'], old):
        return jsonify({'success': False, 'error': '原密码不正确'})
    salt, h = _pw_hash(new)
    rec = _users.get(name) or {}
    _users[name] = {'salt': salt, 'hash': h,
                    'admin': bool(name == _auth['user'] or rec.get('admin')),
                    'root': rec.get('root', '')}
    if not save_users():
        return jsonify({'success': False, 'error': '保存失败，请检查程序目录写入权限'})
    return jsonify({'success': True})


@app.route('/api/list')
def api_list():
    p, kw = request.args.get('path',''), request.args.get('keyword','')
    sb, od = request.args.get('sort_by','name'), request.args.get('order','asc')
    ap = abspath(p)
    if not os.path.isdir(ap): return jsonify({'error':'不是目录','items':[]}), 404
    items = list_items(ap, kw, sb, od)[:PER_PAGE]
    return jsonify({'path':p, 'parent':os.path.dirname(p).replace(os.sep,'/') if p else '', 'items':items})


@app.route('/api/delete', methods=['POST'])
def api_del():
    paths = (request.get_json() or {}).get('paths', [])
    deleted, errors = [], []
    for p in paths:
        ap = abspath(p)
        try:
            if not os.path.exists(ap): errors.append(f'{p}: 不存在'); continue
            if ap == os.path.abspath(get_root()): errors.append('不能删根目录'); continue
            shutil.rmtree(ap) if os.path.isdir(ap) else os.remove(ap)
            deleted.append(p)
        except Exception as e: errors.append(f'{p}: {e}')
    return jsonify({'success':True,'deleted':deleted,'errors':errors})


@app.route('/api/mkdir', methods=['POST'])
def api_mkdir():
    d = request.get_json() or {}
    n = (d.get('name') or '').strip()
    if not n or '/' in n or '\\' in n: return jsonify({'success':False,'error':'名称非法'})
    t = os.path.join(abspath(d.get('path','')), n)
    if os.path.exists(t): return jsonify({'success':False,'error':'已存在'})
    try: os.makedirs(t); return jsonify({'success':True})
    except Exception as e: return jsonify({'success':False,'error':str(e)})


@app.route('/api/batch_async', methods=['POST'])
def api_batch_async():
    d = request.get_json() or {}
    paths, target, op = d.get('paths',[]), d.get('target',''), d.get('operation','copy')
    if not paths: return jsonify({'success':False,'error':'未选择'})
    tid = str(uuid.uuid4())[:12]
    t = threading.Thread(target=bg_batch_task,
                         args=(tid, paths, target, op, os.path.abspath(get_root())), daemon=True)
    t.start()
    return jsonify({'success':True,'task_id':tid})


@app.route('/api/batch_status/<tid>')
def api_batch_status(tid):
    if tid not in bg_tasks:
        return jsonify({'success':False,'error':'任务不存在'})
    t = bg_tasks[tid]
    return jsonify({
        'success': True, 'type': t['type'], 'status': t['status'],
        'progress': t['progress'], 'message': t['message'],
        'total': t['total'], 'done': t['done'], 'errors': t['errors'],
    })


@app.route('/api/batch', methods=['POST'])
def api_batch():
    d = request.get_json() or {}
    paths, target, op = d.get('paths',[]), d.get('target',''), d.get('operation','copy')
    if not paths: return jsonify({'success':False,'error':'未选择'})
    at = abspath(target)
    try: os.makedirs(at, exist_ok=True)
    except Exception as e: return jsonify({'success':False,'error':str(e)})
    cnt, errs = 0, []
    for p in paths:
        src = abspath(p)
        if not os.path.exists(src): errs.append(f'{p}: 不存在'); continue
        n = os.path.basename(src); dst = os.path.join(at, n)
        b, e = os.path.splitext(n); i = 1
        while os.path.exists(dst): dst = os.path.join(at, f'{b}_{i}{e}'); i += 1
        try:
            if op == 'copy':
                shutil.copytree(src, dst) if os.path.isdir(src) else shutil.copy2(src, dst)
            else: shutil.move(src, dst)
            cnt += 1
        except Exception as e: errs.append(f'{p}: {e}')
    return jsonify({'success':True,'count':cnt,'errors':errs})


@app.route('/api/compress', methods=['POST'])
def api_compress():
    d = request.get_json() or {}
    ps, n, cur = d.get('paths',[]), (d.get('name') or '').strip(), d.get('current','')
    if not ps or not n: return jsonify({'success':False,'error':'参数缺失'})
    if not n.endswith('.zip'): n += '.zip'
    ok, msg = compress_files(ps, os.path.join(abspath(cur), secure_filename(n)))
    return jsonify({'success':ok,'message':msg})


@app.route('/api/extract', methods=['POST'])
def api_extract():
    d = request.get_json() or {}
    ps, t = d.get('paths',[]), d.get('target','')
    if not ps: return jsonify({'success':False,'error':'未选择'})
    res = [{'path':p, 'success':ok, 'message':msg} for p in ps for ok, msg in [extract_zip(p, t)]]
    return jsonify({'success':True,'results':res})


@app.route('/preview/<path:path>')
def preview(path):
    ap = abspath(path)
    if not os.path.isfile(ap): abort(404)
    return send_file(ap, conditional=True)


@app.route('/api/read_text')
def api_read_text():
    p = request.args.get('path','')
    ap = abspath(p)
    if not os.path.isfile(ap): return jsonify({'success':False,'error':'文件不存在'})
    if os.path.getsize(ap) > TXT_MAX: return jsonify({'success':False,'error':'文件过大'})
    ext = os.path.splitext(ap)[1].lower().lstrip('.')
    if ext not in TXT_E: return jsonify({'success':False,'error':'不支持'})
    try:
        with open(ap, 'r', encoding='utf-8', errors='replace') as f: c = f.read()
        return jsonify({'success':True,'content':c,'ext':ext})
    except Exception as e: return jsonify({'success':False,'error':str(e)})


@app.route('/upload_chunk', methods=['POST'])
def upload_chunk():
    try:
        uid = request.form.get('file_uid')
        ci = int(request.form.get('chunk_index', 0))
        ch = request.files.get('chunk')
        if not uid or not ch:
            return jsonify({'success': False, 'message': '参数缺失'})
        cp = os.path.join(CHUNK_DIR, f'{uid}_{ci}')
        ch.save(cp)
        return jsonify({'success': True, 'message': f'分片 {ci} 上传成功'})
    except Exception as e:
        logging.exception('upload_chunk error')
        return jsonify({'success': False, 'message': str(e)})


@app.route('/check_chunks')
def check_chunks():
    uid = request.args.get('file_uid')
    if not uid: return jsonify({'success':False,'message':'缺少标识'})
    up = []
    if os.path.exists(CHUNK_DIR):
        for fn in os.listdir(CHUNK_DIR):
            if fn.startswith(f'{uid}_'):
                try: up.append(int(fn.split('_')[1]))
                except: pass
    return jsonify({'success':True,'uploaded_chunks':sorted(up)})


@app.route('/merge_chunks', methods=['POST'])
def api_merge():
    d = request.get_json()
    uid, name = d.get('file_uid'), d.get('file_name')
    total, cur = int(d.get('total_chunks',0)), d.get('current_path','')
    if not uid or not name: return jsonify({'success':False,'message':'参数缺失'})
    tp = os.path.join(abspath(cur), secure_filename(name))
    ok, msg = merge_chunks(uid, name, total, tp)
    return jsonify({'success':ok,'message':msg})


@app.route('/download/<path:path>')
def download(path):
    ap = abspath(path)
    if os.path.isfile(ap): return send_file(ap, as_attachment=True, conditional=True)
    return '文件不存在', 404


@app.route('/start_server_download', methods=['POST'])
def api_dl_server():
    u = ((request.get_json() or {}).get('url') or '').strip()
    if not u: return jsonify({'success':False,'message':'URL为空'})
    tid = str(uuid.uuid4())
    t = threading.Thread(target=dl_to_server, args=(u, tid)); t.daemon = True; t.start()
    return jsonify({'success':True,'task_id':tid,'message':'已启动'})


@app.route('/download_status')
def dl_status():
    tid = request.args.get('task_id','')
    if tid not in dl_tasks: return jsonify({'status':'not_found','progress':0,'message':'任务不存在','file_path':''})
    return jsonify(dl_tasks[tid])


@app.route('/download_to_local')
def dl_to_local():
    try:
        u = unquote(request.args.get('url','')).strip()
        if not u: return 'URL为空', 400
        fn = get_fname(u)
        r = requests.get(u, stream=True, timeout=30); r.raise_for_status()
        def gen():
            for c in r.iter_content(1024*1024):
                if c: yield c
        return Response(stream_with_context(gen()), headers={'Content-Disposition':f'attachment; filename="{secure_filename(fn)}"','Content-Type':r.headers.get('content-type','application/octet-stream')})
    except Exception as e: return f'失败: {e}', 500


# ================= 终端 API =================
@app.route('/api/terminal/platform')
def api_plat():
    return jsonify({'success':True,'os':PLAT['os'],'name':PLAT['name'],'shell':PLAT['shell'],'encoding':ENC,'commands':sorted(CMDS)})


@app.route('/api/terminal/exec', methods=['POST'])
def api_term_exec():
    r = _admin_only()
    if r: return r
    d = request.get_json() or {}
    cmd, cwd = (d.get('command') or '').strip(), d.get('cwd','')
    if not cmd: return jsonify({'success':False,'error':'命令为空'})
    try: parts = shlex.split(cmd, posix=(not IS_WIN))
    except ValueError: parts = cmd.split()
    if not parts: return jsonify({'success':False,'error':'无效命令'})
    base = parts[0].lower()
    if IS_WIN: base = base.replace('.exe','').replace('.bat','').replace('.cmd','')
    if base not in CMDS: return jsonify({'success':False,'error':f'命令 "{base}" 不在允许列表中'})
    for pat in [r'rm\s+(-rf?|--recursive)\s+/(\s|$)', r'rm\s+(-rf?|--recursive)\s+/\*', r'mkfs', r'dd\s+if=', r'>\s*/dev/sd', r'format\s+[a-z]:', r'del\s+/[sq]\s+[a-z]:']:
        if re.search(pat, cmd, re.IGNORECASE): return jsonify({'success':False,'error':'包含危险操作'})
    wd = abspath(cwd) if cwd else get_root()
    if not os.path.isdir(wd): wd = get_root()
    cmd_hist.append({'command':cmd,'cwd':cwd,'time':time.time()})
    if len(cmd_hist) > HIST_MAX: cmd_hist.pop(0)
    jid = str(uuid.uuid4())[:8]
    t = threading.Thread(target=_run_cmd, args=(jid, cmd, wd), daemon=True); t.start()
    return jsonify({'success':True,'job_id':jid,'command':cmd,'cwd':wd})


def _run_cmd(jid, cmd, cwd):
    q = queue.Queue(); out_qs[jid] = q
    q.put(('start', f'$ {cmd}\n'))
    try:
        kw = dict(shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE, encoding=ENC, errors='replace', bufsize=1)
        if IS_WIN: kw['creationflags'] = subprocess.CREATE_NO_WINDOW
        else: kw['preexec_fn'] = os.setsid
        proc = subprocess.Popen(cmd, **kw)
        act_procs[jid] = proc
        for ln in iter(proc.stdout.readline, ''):
            if not ln: break
            q.put(('output', ln))
        proc.wait()
        q.put(('exit', f'\n[退出码: {proc.returncode}]\n'))
    except Exception as e:
        q.put(('error', f'出错: {e}\n')); q.put(('exit', '\n[异常结束]\n'))
    finally:
        q.put(('done', '')); act_procs.pop(jid, None)
        def _cl():
            time.sleep(30); out_qs.pop(jid, None)
        threading.Thread(target=_cl, daemon=True).start()


@app.route('/api/terminal/stream/<jid>')
def api_term_stream(jid):
    r = _admin_only()
    if r: return r
    if jid not in out_qs: return jsonify({'error':'任务不存在'}), 404
    q = out_qs[jid]
    def gen():
        try:
            while True:
                try: mt, c = q.get(timeout=60)
                except queue.Empty: yield 'event: heartbeat\ndata: \n\n'; continue
                if mt == 'done': yield 'event: done\ndata: \n\n'; break
                yield f"event: message\ndata: {json.dumps({'type':mt,'content':c}, ensure_ascii=False)}\n\n"
        except GeneratorExit: pass
    return Response(stream_with_context(gen()), mimetype='text/event-stream', headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no','Connection':'keep-alive'})


@app.route('/api/terminal/kill/<jid>', methods=['POST'])
def api_term_kill(jid):
    r = _admin_only()
    if r: return r
    proc = act_procs.get(jid)
    if not proc: return jsonify({'success':False,'error':'进程不存在'})
    try:
        if IS_WIN: subprocess.run(['taskkill','/F','/T','/PID',str(proc.pid)], capture_output=True, timeout=10)
        else: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        return jsonify({'success':True,'message':'已终止'})
    except Exception as e: return jsonify({'success':False,'error':str(e)})


@app.route('/api/terminal/history')
def api_term_hist():
    r = _admin_only()
    if r: return r
    lim = int(request.args.get('limit', 50))
    return jsonify({'success':True,'history':cmd_hist[-lim:][::-1]})


@app.route('/api/terminal/completions')
def api_term_comp():
    r = _admin_only()
    if r: return r
    partial, cwd = request.args.get('partial','').strip(), request.args.get('cwd','')
    if not partial: return jsonify({'success':True,'completions':sorted(CMDS)})
    parts = partial.split(); comps = []
    if len(parts) <= 1:
        pre = parts[0] if parts else ''
        for c in sorted(CMDS):
            if c.startswith(pre.lower()): comps.append(c)
    else:
        cmd, last = parts[0].lower(), parts[-1]
        if cmd in COMP:
            for o in COMP[cmd]:
                if o.startswith(last): comps.append(o)
        if not comps:
            try:
                ac = abspath(cwd) if cwd else get_root()
                if os.path.isdir(ac):
                    for n in os.listdir(ac):
                        if n.lower().startswith(last.lower()):
                            full = os.path.join(cwd, n) if cwd else n
                            if os.path.isdir(os.path.join(ac, n)): full += '/'
                            comps.append(full)
            except: pass
    return jsonify({'success':True,'completions':comps[:50]})


@app.route('/api/terminal/clear_history', methods=['POST'])
def api_term_clhist():
    r = _admin_only()
    if r: return r
    cmd_hist.clear(); return jsonify({'success':True})


# ================= 进程 API =================
@app.route('/api/process/list')
def api_proc_list():
    r = _admin_only()
    if r: return r
    force = request.args.get('force') == '1'
    d, err = get_procs(force)
    if d is None: return jsonify({'success':False,'error':err or '获取失败','processes':[],'system':{}})
    return jsonify({'success':True,'processes':d.get('processes',[]),'system':d.get('system',{})})


@app.route('/api/process/kill', methods=['POST'])
def api_proc_kill():
    r = _admin_only()
    if r: return r
    pid = (request.get_json() or {}).get('pid')
    if not pid: return jsonify({'success':False,'error':'缺少 PID'})
    try:
        pid = int(pid)
        if IS_WIN:
            r = subprocess.run(['taskkill','/F','/T','/PID',str(pid)], capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode != 0:
                msg = r.stderr.decode('gbk','ignore').strip() or r.stdout.decode('gbk','ignore').strip()
                return jsonify({'success':False,'error':msg or '结束失败'})
        else:
            os.kill(pid, signal.SIGKILL)
        _proc_cache['data'] = None
        return jsonify({'success':True,'message':'已结束'})
    except Exception as e: return jsonify({'success':False,'error':str(e)})


# ================= 启动 =================
class LBServer(ThreadedWSGIServer):
    request_queue_size = 2048
    daemon_threads = True
    allow_reuse_address = True


class QH(WSGIRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_request(self, code='-', size='-'): pass
    def log_message(self, *a): pass


if __name__ == '__main__':
    if FTP_AVAILABLE and FTP_ENABLED:
        threading.Thread(target=start_ftp_server, daemon=True).start()

    print('=' * 60)
    print(f'根目录: {get_root()}')
    print(f'HTTP 访问:  http://{HTTP_HOST}:{HTTP_PORT}')
    print(f'登录账号:  {_auth["user"]} / {_auth["pass"]}')
    if _IS_DEFAULT_AUTH:
        print('[!] 正在使用默认密码 admin/admin123：能登录的人都能读写文件、执行命令，请尽快修改（设置 SD_USER / SD_PASS 环境变量）')
    if FTP_AVAILABLE and FTP_ENABLED:
        print(f'FTP  访问:  ftp://{HTTP_HOST}:{FTP_PORT}')
        print(f'FTP  账号:  {_auth["user"]} / {_auth["pass"]}   (与网页共用)')
        print(f'FTP  被动端口: {FTP_PASSIVE_PORTS.start}-{FTP_PASSIVE_PORTS.stop-1}')
    elif not FTP_ENABLED:
        print('FTP  未启用（SD_FTP=0）')
    else:
        print(f'FTP  未启用（安装: pip install pyftpdlib）')
    print(f'分片: {CHUNK_SIZE//(1024*1024)}MB  并发: 6')
    print(f'平台: {PLAT["name"]}  编码: {ENC}  命令: {len(CMDS)} 个')
    print('=' * 60)
    srv = LBServer(HTTP_HOST, HTTP_PORT, app, handler=QH)
    try: srv.serve_forever()
    except KeyboardInterrupt: print('\n已停止'); srv.server_close()
