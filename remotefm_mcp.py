#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RemoteFM MCP stdio server.

把 RemoteFM 的 HTTP API 包装成 MCP tools。MCP 客户端（Claude Desktop、Cursor、
Codex 等）启动本进程后，就可以直接列出/读写/上传/下载文件、执行命令、管理进程和用户。

环境变量：
  REMOTEFM_URL       RemoteFM 地址，默认 https://127.0.0.1:8880
  REMOTEFM_USER      登录用户名，默认 admin
  REMOTEFM_PASS      登录密码，默认 admin123
  REMOTEFM_VERIFY    TLS 证书校验，默认 0（本地自签证书需要关闭）
  REMOTEFM_MCP_READONLY 设为 1 时只允许只读工具
"""
from __future__ import annotations

import base64
import ftplib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SERVER_NAME = 'remotefm'
SERVER_VERSION = '1.0.0'
CHUNK_SIZE = 8 * 1024 * 1024


class RFError(Exception):
    pass


def _env_bool(name, default=False):
    v = os.environ.get(name, '')
    if v == '':
        return default
    return v.strip().lower() not in ('0', 'false', 'no', 'off')


class RFClient:
    def __init__(self):
        self.url = (os.environ.get('REMOTEFM_URL') or 'https://127.0.0.1:8880').rstrip('/')
        self.user = os.environ.get('REMOTEFM_USER') or 'admin'
        self.password = os.environ.get('REMOTEFM_PASS') or 'admin123'
        self.verify = _env_bool('REMOTEFM_VERIFY', False)
        self.timeout = float(os.environ.get('REMOTEFM_TIMEOUT') or 60)
        self.session = requests.Session()
        self.session.verify = self.verify
        self._logged = False

    def _login(self):
        self._logged = False
        try:
            r = self.session.post(
                self.url + '/login',
                data={'username': self.user, 'password': self.password},
                timeout=self.timeout,
                allow_redirects=True,
            )
            me = self.session.get(self.url + '/api/me', timeout=self.timeout)
            if me.status_code == 200:
                try:
                    data = me.json()
                except ValueError:
                    data = {}
                if data.get('success'):
                    self._logged = True
                    return
            raise RFError(f'RemoteFM 登录失败（HTTP {r.status_code}/{me.status_code}）')
        except requests.RequestException as e:
            raise RFError(f'无法连接 RemoteFM {self.url}: {e}')

    def _ensure_login(self):
        if not self._logged:
            self._login()

    def request(self, method, path, *, params=None, json_body=None, data=None,
                files=None, stream=False, read_timeout=None):
        self._ensure_login()
        url = self.url + path
        timeout = (10, read_timeout if read_timeout else self.timeout) if stream else self.timeout
        r = None
        for attempt in range(2):
            try:
                r = self.session.request(method, url, params=params, json=json_body,
                                         data=data, files=files, timeout=timeout,
                                         stream=stream)
            except requests.RequestException as e:
                raise RFError(f'HTTP 请求失败: {e}')
            expired = r.status_code in (401, 403)
            login_html = (r.status_code == 200 and path.startswith('/api/')
                          and 'text/html' in (r.headers.get('Content-Type') or '').lower())
            if (expired or login_html) and attempt == 0:
                # 服务重启或会话过期时重新登录一次
                self._logged = False
                self._ensure_login()
                continue
            break
        if r.status_code >= 400:
            text = r.text[:800] if not stream else ''
            raise RFError(f'RemoteFM HTTP {r.status_code}: {text or r.reason}')
        return r

    def api(self, method, path, **kwargs):
        r = self.request(method, path, **kwargs)
        try:
            return r.json()
        except ValueError:
            raise RFError(f'RemoteFM 返回了非 JSON 内容: {r.text[:300]}')

    def download_to(self, path, local_path, as_zip=False):
        endpoint = '/download_zip/' if as_zip else '/download/'
        url_path = endpoint + quote(path.lstrip('/'), safe='/')
        r = self.request('GET', url_path, stream=True)
        lp = Path(local_path)
        if lp.exists() and lp.is_dir():
            name = path.rstrip('/').split('/')[-1] or 'download.zip'
            lp = lp / name
        lp.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        size = 0
        try:
            with open(lp, 'wb') as f:
                for chunk in r.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
        finally:
            r.close()
        return {'local_path': str(lp), 'size': size, 'sha256': h.hexdigest()}

    def upload_file(self, local_path=None, content=None, remote_dir='', remote_name=''):
        if local_path:
            lp = Path(local_path)
            if not lp.is_file():
                raise RFError(f'本地文件不存在: {local_path}')
            name = remote_name or lp.name
            size = lp.stat().st_size
            fh = open(lp, 'rb')
        elif content is not None:
            blob = base64.b64decode(content)
            name = remote_name or f'upload_{int(time.time())}.bin'
            size = len(blob)
            fh = None
        else:
            raise RFError('必须提供 local_path 或 content_base64')
        uid = uuid.uuid4().hex
        total = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
        try:
            for i in range(total):
                chunk = fh.read(CHUNK_SIZE) if fh else blob[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
                if not chunk:
                    break
                d = self.api('POST', '/upload_chunk', data={
                    'file_uid': uid, 'chunk_index': str(i), 'total_chunks': str(total),
                    'file_name': name, 'current_path': remote_dir,
                }, files={'chunk': (name, chunk, 'application/octet-stream')})
                if not d.get('success'):
                    raise RFError(f'分片 {i + 1}/{total} 上传失败: {d.get("message") or d}')
            m = self.api('POST', '/merge_chunks', json_body={
                'file_uid': uid, 'file_name': name, 'total_chunks': total,
                'current_path': remote_dir,
            })
            if not m.get('success'):
                raise RFError(f'合并文件失败: {m.get("message") or m}')
            return {'success': True, 'name': name, 'size': size,
                    'remote_dir': remote_dir, 'chunks': total}
        finally:
            if fh:
                fh.close()

    def stream_job(self, job_id, timeout=300):
        r = self.request('GET', f'/api/terminal/stream/{job_id}', stream=True,
                         read_timeout=timeout)
        event = ''
        output = []
        code = None
        done = False
        try:
            for raw in r.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                if raw.startswith('event: '):
                    event = raw[7:]
                elif raw.startswith('data: '):
                    data = raw[6:]
                    if event == 'done':
                        done = True
                        break
                    if event == 'message' and data:
                        try:
                            msg = json.loads(data)
                        except ValueError:
                            continue
                        typ = msg.get('type')
                        text = msg.get('content') or ''
                        if typ in ('output', 'error', 'exit', 'cmd'):
                            output.append(text)
                        if typ == 'exit':
                            m = re.search(r'退出码[：:]\s*(-?\d+)', text)
                            if m:
                                code = int(m.group(1))
                elif raw == '':
                    event = ''
        except requests.RequestException as e:
            raise RFError(f'读取命令输出失败: {e}')
        finally:
            r.close()
        text = ''.join(output)
        if not done:
            raise RFError('命令输出流提前结束')
        return {'output': text[-100000:], 'exit_code': code, 'job_id': job_id}


CLIENT = RFClient()
READONLY = _env_bool('REMOTEFM_MCP_READONLY', False)
WRITE_TOOLS = {
    'set_root', 'write_text_file', 'create_folder', 'rename_path', 'delete_paths',
    'copy_or_move', 'compress_paths', 'extract_archives', 'upload_file', 'fetch_url_to_remote',
    'ftp_upload',
    'terminal_exec', 'terminal_run', 'terminal_kill', 'terminal_session_create',
    'terminal_session_close', 'terminal_session_clear', 'process_kill',
    'user_create', 'user_update', 'user_delete', 'change_password', 'token_action',
}


def _ok_text(data):
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    failed = isinstance(data, dict) and data.get('success') is False
    return {'content': [{'type': 'text', 'text': text}], 'isError': failed}


def _err(exc):
    return {'content': [{'type': 'text', 'text': f'ERROR: {exc}'}], 'isError': True}


def _list_directory(a):
    return CLIENT.api('GET', '/api/list', params={
        'path': a.get('path', ''),
        'keyword': a.get('keyword', ''),
        'sort_by': a.get('sort_by', 'name'),
        'order': a.get('order', 'asc'),
    })


def _read_text(a):
    d = CLIENT.api('GET', '/api/read_text', params={'path': a['path']})
    limit = max(1, min(int(a.get('max_chars') or 200000), 2000000))
    if d.get('success') and len(d.get('content') or '') > limit:
        d['content'] = d['content'][:limit]
        d['truncated'] = True
    return d


def _write_text(a):
    return CLIENT.api('POST', '/api/write_text', json_body={
        'path': a['path'], 'content': a['content'],
        'create_parents': bool(a.get('create_parents', False)),
    })


def _root_get(a):
    return CLIENT.api('GET', '/api/get_root')


def _root_set(a):
    return CLIENT.api('POST', '/api/set_root', json_body={'path': a.get('path', '')})


def _mkdir(a):
    return CLIENT.api('POST', '/api/mkdir', json_body={
        'path': a.get('path', ''), 'name': a['name'],
    })


def _rename(a):
    return CLIENT.api('POST', '/api/rename', json_body={
        'path': a['path'], 'name': a['new_name'],
    })


def _delete(a):
    return CLIENT.api('POST', '/api/delete', json_body={'paths': a['paths']})


def _copy_move(a):
    task = CLIENT.api('POST', '/api/batch_async', json_body={
        'paths': a['paths'], 'target': a['target'], 'operation': a['operation'],
    })
    if not task.get('success'):
        return task
    tid = task['task_id']
    deadline = time.time() + max(5, min(float(a.get('timeout_seconds') or 300), 1800))
    while time.time() < deadline:
        st = CLIENT.api('GET', f'/api/batch_status/{tid}')
        if not st.get('success') or st.get('status') in ('completed', 'failed'):
            st['task_id'] = tid
            return st
        time.sleep(0.5)
    raise RFError(f'任务 {tid} 超时，仍在后台运行')


def _compress(a):
    return CLIENT.api('POST', '/api/compress', json_body={
        'paths': a['paths'], 'name': a['name'], 'current': a.get('current', ''),
    })


def _extract(a):
    return CLIENT.api('POST', '/api/extract', json_body={
        'paths': a['paths'], 'target': a.get('target', ''),
    })


def _download(a):
    return CLIENT.download_to(a['path'], a['local_path'], bool(a.get('as_zip', False)))


def _download_multi(a):
    r = CLIENT.request('POST', '/download_zip_multi',
                       data={'paths': json.dumps(a['paths'])}, stream=True)
    lp = Path(a['local_path'])
    if lp.exists() and lp.is_dir():
        lp = lp / 'RemoteFM-selection.zip'
    lp.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    try:
        with open(lp, 'wb') as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk); h.update(chunk); size += len(chunk)
    finally:
        r.close()
    return {'local_path': str(lp), 'size': size, 'sha256': h.hexdigest()}


def _upload(a):
    return CLIENT.upload_file(
        local_path=a.get('local_path'),
        content=a.get('content_base64'),
        remote_dir=a.get('remote_dir', ''),
        remote_name=a.get('remote_name', ''),
    )


def _fetch_url(a):
    url = a['url']
    name = a.get('remote_name') or unquote(os.path.basename(urlparse(url).path)) or 'download.bin'
    fd, tmp = tempfile.mkstemp(prefix='remotefm-mcp-', suffix='.part')
    os.close(fd)
    try:
        r = requests.get(url, stream=True, timeout=(10, max(10, float(a.get('timeout_seconds') or 120))))
        r.raise_for_status()
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
        r.close()
        out = CLIENT.upload_file(local_path=tmp, remote_dir=a.get('remote_dir', ''),
                                 remote_name=name)
        out['source_url'] = url
        return out
    except requests.RequestException as e:
        raise RFError(f'下载 URL 失败: {e}')
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _me(a):
    d = CLIENT.api('GET', '/api/me')
    d['ftp'] = {}
    try:
        d['ftp'] = CLIENT.api('GET', '/api/ftp/info')
    except RFError:
        pass
    return d


def _ftp_info(a):
    return CLIENT.api('GET', '/api/ftp/info')


def _ftp_connect():
    info = CLIENT.api('GET', '/api/ftp/info')
    if not info.get('success'):
        raise RFError(info.get('error') or 'FTP 信息不可用')
    f = ftplib.FTP()
    f.connect(info.get('host') or '127.0.0.1', int(info.get('port') or 2121), timeout=30)
    f.login(info.get('user') or '', info.get('pass') or '')
    f.set_pasv(True)
    return f


def _ftp_upload(a):
    p = Path(a['local_path'])
    if not p.is_file(): raise RFError(f'本地文件不存在: {p}')
    name = a.get('remote_name') or p.name
    f = _ftp_connect()
    try:
        d = (a.get('remote_dir') or '').strip('/')
        if d: f.cwd(d)
        with open(p, 'rb') as fh:
            f.storbinary('STOR ' + name, fh, blocksize=1024 * 1024)
        return {'success': True, 'remote_dir': d, 'name': name, 'size': p.stat().st_size}
    finally:
        try: f.quit()
        except Exception:
            try: f.close()
            except Exception: pass


def _ftp_download(a):
    remote = a['remote_path'].lstrip('/')
    lp = Path(a['local_path'])
    if lp.exists() and lp.is_dir(): lp = lp / (remote.split('/')[-1] or 'download.bin')
    lp.parent.mkdir(parents=True, exist_ok=True)
    f = _ftp_connect()
    n = 0; h = hashlib.sha256()
    try:
        # 流式写入本地文件，避免把大文件读进内存
        with open(lp, 'wb') as out:
            def cb(data):
                nonlocal n
                out.write(data); h.update(data); n += len(data)
            f.retrbinary('RETR ' + remote, cb, blocksize=1024 * 1024)
        return {'success': True, 'local_path': str(lp), 'size': n, 'sha256': h.hexdigest()}
    finally:
        try: f.quit()
        except Exception:
            try: f.close()
            except Exception: pass


def _process_list(a):
    d = CLIENT.api('GET', '/api/process/list', params={'force': '1' if a.get('force') else ''})
    items = d.get('processes') or []
    kw = (a.get('filter') or '').lower()
    if kw:
        items = [p for p in items if kw in str(p.get('name', '')).lower()
                 or kw in str(p.get('pid', '')) or kw in str(p.get('command', '')).lower()]
    limit = max(1, min(int(a.get('limit') or 200), 1000))
    d['total'] = len(items)
    d['processes'] = items[:limit]
    return d


def _process_kill(a):
    return CLIENT.api('POST', '/api/process/kill', json_body={'pid': a['pid']})


def _terminal_exec(a):
    return CLIENT.api('POST', '/api/terminal/exec_sync', json_body={
        'command': a['command'], 'cwd': a.get('cwd', ''),
        'timeout': a.get('timeout_seconds', 30),
    })


def _terminal_platform(a):
    return CLIENT.api('GET', '/api/terminal/platform')


def _terminal_sessions(a):
    return CLIENT.api('GET', '/api/terminal/sessions')


def _terminal_create(a):
    return CLIENT.api('POST', '/api/terminal/sessions', json_body={'cwd': a.get('cwd', '')})


def _terminal_close(a):
    return CLIENT.api('DELETE', f"/api/terminal/sessions/{a['session_id']}")


def _terminal_clear(a):
    return CLIENT.api('POST', f"/api/terminal/sessions/{a['session_id']}/clear")


def _terminal_run(a):
    sid = a.get('session_id') or ''
    created = False
    if not sid:
        made = CLIENT.api('POST', '/api/terminal/sessions', json_body={'cwd': a.get('cwd', '')})
        sid = made['session']['id']
        created = True
    d = CLIENT.api('POST', '/api/terminal/exec', json_body={
        'session_id': sid, 'command': a['command'], 'cwd': a.get('cwd', ''),
    })
    if not d.get('success'):
        return d
    timeout = max(1, min(int(a.get('timeout_seconds') or 300), 900))
    try:
        out = CLIENT.stream_job(d['job_id'], timeout=timeout)
    except Exception:
        try: CLIENT.api('POST', f"/api/terminal/kill/{d['job_id']}")
        except Exception: pass
        raise
    out['session_id'] = sid
    out['created_session'] = created
    return out


def _terminal_kill(a):
    return CLIENT.api('POST', f"/api/terminal/kill/{a['job_id']}")


def _terminal_history(a):
    return CLIENT.api('GET', '/api/terminal/history', params={'limit': a.get('limit', 100)})


def _users_list(a):
    return CLIENT.api('GET', '/api/users')


def _user_create(a):
    return CLIENT.api('POST', '/api/users', json_body=a)


def _user_update(a):
    return CLIENT.api('PUT', '/api/users', json_body=a)


def _user_delete(a):
    return CLIENT.api('DELETE', '/api/users', json_body={'name': a['name']})


def _change_password(a):
    return CLIENT.api('POST', '/api/passwd', json_body=a)


def _token_status(a):
    d = CLIENT.api('GET', '/api/terminal/token')
    if not a.get('show_token') and d.get('token'):
        d['token'] = d['token'][:8] + '...' + d['token'][-4:]
    return d


def _token_action(a):
    d = CLIENT.api('POST', '/api/terminal/token', json_body={'action': a['action']})
    if not a.get('show_token') and d.get('token'):
        d['token'] = d['token'][:8] + '...' + d['token'][-4:]
    return d


def _schema(props, required=None):
    return {'type': 'object', 'properties': props,
            'required': required or [], 'additionalProperties': False}


STR = {'type': 'string'}
STR_ARR = {'type': 'array', 'items': {'type': 'string'}}
BOOL = {'type': 'boolean'}
INT = {'type': 'integer'}

TOOLS = [
    {'name': 'system_info', 'description': '查看 RemoteFM 当前登录用户、根目录和 FTP 状态。',
     'inputSchema': _schema({}), 'fn': _me},
    {'name': 'get_root', 'description': '读取当前会话的根目录。', 'inputSchema': _schema({}), 'fn': _root_get},
    {'name': 'set_root', 'description': '切换当前登录会话的根目录（超级用户）。path 留空表示整台计算机。',
     'inputSchema': _schema({'path': STR}, []), 'fn': _root_set},
    {'name': 'list_directory',
     'description': '列出目录。path 为相对当前根目录的路径；keyword 可搜索。',
     'inputSchema': _schema({'path': STR, 'keyword': STR,
                             'sort_by': {'type': 'string', 'enum': ['name', 'mtime', 'size']},
                             'order': {'type': 'string', 'enum': ['asc', 'desc']}}), 'fn': _list_directory},
    {'name': 'read_text_file', 'description': '读取文本文件内容（最大 8MB，可限制返回字符数）。',
     'inputSchema': _schema({'path': STR, 'max_chars': INT}, ['path']), 'fn': _read_text},
    {'name': 'write_text_file', 'description': '新建或覆盖 UTF-8 文本文件。',
     'inputSchema': _schema({'path': STR, 'content': STR, 'create_parents': BOOL}, ['path', 'content']), 'fn': _write_text},
    {'name': 'create_folder', 'description': '在 path 目录下创建文件夹。',
     'inputSchema': _schema({'path': STR, 'name': STR}, ['name']), 'fn': _mkdir},
    {'name': 'rename_path', 'description': '重命名文件或文件夹。',
     'inputSchema': _schema({'path': STR, 'new_name': STR}, ['path', 'new_name']), 'fn': _rename},
    {'name': 'delete_paths', 'description': '删除文件或文件夹（危险）。',
     'inputSchema': _schema({'paths': STR_ARR}, ['paths']), 'fn': _delete},
    {'name': 'copy_or_move', 'description': '复制或移动多个文件/文件夹到目标目录。',
     'inputSchema': _schema({'paths': STR_ARR, 'target': STR,
                             'operation': {'type': 'string', 'enum': ['copy', 'move']},
                             'timeout_seconds': INT}, ['paths', 'target', 'operation']), 'fn': _copy_move},
    {'name': 'compress_paths', 'description': '把文件/文件夹压缩成 ZIP。',
     'inputSchema': _schema({'paths': STR_ARR, 'name': STR, 'current': STR}, ['paths', 'name']), 'fn': _compress},
    {'name': 'extract_archives', 'description': '解压 ZIP 或 7Z 到目标目录。',
     'inputSchema': _schema({'paths': STR_ARR, 'target': STR}, ['paths']), 'fn': _extract},
    {'name': 'download_file', 'description': '把服务器文件下载到 MCP 主机本地；文件夹请设置 as_zip=true。',
     'inputSchema': _schema({'path': STR, 'local_path': STR, 'as_zip': BOOL}, ['path', 'local_path']), 'fn': _download},
    {'name': 'download_paths', 'description': '把多个文件/文件夹打包成一个 ZIP 下载到 MCP 主机。',
     'inputSchema': _schema({'paths': STR_ARR, 'local_path': STR}, ['paths', 'local_path']), 'fn': _download_multi},
    {'name': 'upload_file', 'description': '从 MCP 主机上传文件，或传 content_base64 上传内存内容。',
     'inputSchema': _schema({'local_path': STR, 'content_base64': STR,
                             'remote_dir': STR, 'remote_name': STR}), 'fn': _upload},
    {'name': 'fetch_url_to_remote', 'description': '由 MCP 主机下载 URL，再上传到 RemoteFM 指定目录。',
     'inputSchema': _schema({'url': STR, 'remote_dir': STR, 'remote_name': STR,
                             'timeout_seconds': INT}, ['url']), 'fn': _fetch_url},
    {'name': 'terminal_exec', 'description': '同步执行一条远程命令（受白名单限制，默认 30 秒）。',
     'inputSchema': _schema({'command': STR, 'cwd': STR, 'timeout_seconds': INT}, ['command']), 'fn': _terminal_exec},
    {'name': 'terminal_platform', 'description': '读取终端平台、shell 和允许的命令。',
     'inputSchema': _schema({}), 'fn': _terminal_platform},
    {'name': 'terminal_sessions', 'description': '列出服务端终端标签及其运行状态。',
     'inputSchema': _schema({}), 'fn': _terminal_sessions},
    {'name': 'terminal_session_create', 'description': '新建一个终端标签。',
     'inputSchema': _schema({'cwd': STR}), 'fn': _terminal_create},
    {'name': 'terminal_run', 'description': '在终端标签中异步执行命令并等待输出；不传 session_id 会自动建标签。',
     'inputSchema': _schema({'command': STR, 'session_id': STR, 'cwd': STR,
                             'timeout_seconds': INT}, ['command']), 'fn': _terminal_run},
    {'name': 'terminal_kill', 'description': '终止某个终端 job。',
     'inputSchema': _schema({'job_id': STR}, ['job_id']), 'fn': _terminal_kill},
    {'name': 'terminal_session_close', 'description': '关闭终端标签并强制结束其中命令。',
     'inputSchema': _schema({'session_id': STR}, ['session_id']), 'fn': _terminal_close},
    {'name': 'terminal_session_clear', 'description': '清空终端标签的输出历史。',
     'inputSchema': _schema({'session_id': STR}, ['session_id']), 'fn': _terminal_clear},
    {'name': 'terminal_history', 'description': '读取远程命令历史。',
     'inputSchema': _schema({'limit': INT}), 'fn': _terminal_history},
    {'name': 'process_list', 'description': '列出进程；可用 filter 过滤名称/PID/命令行。',
     'inputSchema': _schema({'force': BOOL, 'filter': STR, 'limit': INT}), 'fn': _process_list},
    {'name': 'process_kill', 'description': '结束进程（危险）。',
     'inputSchema': _schema({'pid': INT}, ['pid']), 'fn': _process_kill},
    {'name': 'ftp_info', 'description': '读取 FTP 连接信息。', 'inputSchema': _schema({}), 'fn': _ftp_info},
    {'name': 'ftp_upload', 'description': '通过 FTP 从 MCP 主机上传文件（FTP 根目录由 RemoteFM 配置决定）。',
     'inputSchema': _schema({'local_path': STR, 'remote_dir': STR, 'remote_name': STR}, ['local_path']), 'fn': _ftp_upload},
    {'name': 'ftp_download', 'description': '通过 FTP 下载文件到 MCP 主机。',
     'inputSchema': _schema({'remote_path': STR, 'local_path': STR}, ['remote_path', 'local_path']), 'fn': _ftp_download},
    {'name': 'user_list', 'description': '列出账号与目录范围。', 'inputSchema': _schema({}), 'fn': _users_list},
    {'name': 'user_create', 'description': '创建普通用户；可传 root 或 roots。',
     'inputSchema': _schema({'name': STR, 'password': STR, 'root': STR, 'roots': STR_ARR},
                            ['name', 'password']), 'fn': _user_create},
    {'name': 'user_update', 'description': '修改普通用户名、密码或目录范围。',
     'inputSchema': _schema({'name': STR, 'new_name': STR, 'password': STR,
                             'root': STR, 'roots': STR_ARR}, ['name']), 'fn': _user_update},
    {'name': 'user_delete', 'description': '删除普通用户。',
     'inputSchema': _schema({'name': STR}, ['name']), 'fn': _user_delete},
    {'name': 'change_password', 'description': '修改密码；修改超级用户需提供 old_password。',
     'inputSchema': _schema({'name': STR, 'old_password': STR, 'new_password': STR},
                            ['new_password']), 'fn': _change_password},
    {'name': 'token_status', 'description': '查看远程命令 Token 状态；show_token=true 才返回明文。',
     'inputSchema': _schema({'show_token': BOOL}), 'fn': _token_status},
    {'name': 'token_action', 'description': '启用、停用或刷新远程命令 Token。',
     'inputSchema': _schema({'action': {'type': 'string', 'enum': ['enable', 'disable', 'refresh']},
                             'show_token': BOOL}, ['action']), 'fn': _token_action},
]
TOOL_MAP = {t['name']: t for t in TOOLS}


def _rpc_error(msg_id, code, message):
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': code, 'message': message}}


def _handle(msg):
    method = msg.get('method')
    msg_id = msg.get('id')
    params = msg.get('params') or {}
    if method == 'initialize':
        pv = params.get('protocolVersion') or '2024-11-05'
        return {'jsonrpc': '2.0', 'id': msg_id, 'result': {
            'protocolVersion': pv,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
        }}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': msg_id, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': msg_id, 'result': {'tools': [
            {k: t[k] for k in ('name', 'description', 'inputSchema')} for t in TOOLS
        ]}}
    if method == 'tools/call':
        name = params.get('name')
        args = params.get('arguments') or {}
        tool = TOOL_MAP.get(name)
        if not tool:
            return _rpc_error(msg_id, -32602, f'未知工具: {name}')
        if READONLY and name in WRITE_TOOLS:
            return {'jsonrpc': '2.0', 'id': msg_id,
                    'result': _err('REMOTEFM_MCP_READONLY=1，写操作已禁用')}
        try:
            result = tool['fn'](args)
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': _ok_text(result)}
        except Exception as e:
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': _err(e)}
    if method in ('notifications/initialized', 'notifications/cancelled',
                  'notifications/roots/list_changed'):
        return None
    if method in ('shutdown',):
        return {'jsonrpc': '2.0', 'id': msg_id, 'result': None}
    if method == 'exit':
        raise SystemExit(0)
    if msg_id is None:
        return None
    return _rpc_error(msg_id, -32601, f'未知方法: {method}')


def main():
    print(f'[remotefm-mcp] stdio server, target={CLIENT.url} user={CLIENT.user}'
          + (' [READONLY]' if READONLY else ''), file=sys.stderr, flush=True)
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode('utf-8'))
        except Exception as e:
            sys.stdout.buffer.write(json.dumps(_rpc_error(None, -32700, f'JSON 解析失败: {e}'),
                                                ensure_ascii=False).encode('utf-8') + b'\n')
            sys.stdout.buffer.flush()
            continue
        try:
            resp = _handle(msg)
        except SystemExit:
            break
        except Exception as e:
            resp = _rpc_error(msg.get('id'), -32603, str(e))
        if resp is not None:
            sys.stdout.buffer.write(json.dumps(resp, ensure_ascii=False,
                                                separators=(',', ':')).encode('utf-8') + b'\n')
            sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
