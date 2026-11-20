# -*- coding: utf-8 -*-
"""RemoteFM 远程桌面模块（内置，可选启用）

- 采集：DXGI Desktop Duplication（dxcam，原生 BGRA）；失败自动回退 GDI
- 差分：1/8 缩略图单通道 numpy 比较，按 64px 网格合并成矩形
- 传输：HTTP 二进制帧（RF01 头 + 多个矩形），flask 按 chunked 推送
- 调度：画面静止降频；没有观看者时完全不采集
- 输入：mouse_event / keybd_event

本模块只在第一次打开 /remote 时导入，不拖慢主程序启动。
"""

import ctypes
import io
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageGrab

MAGIC = b'RF01'
FRAME = __import__('struct').Struct('>4sIBB')       # magic, seq, flags, rect 数
RECT = __import__('struct').Struct('>HHHHI')         # x, y, w, h, jpeg 长度

user32 = ctypes.windll.user32
MOUSEEVENTF = {
    'move': 0x0001, 'abs': 0x8000, 'leftdown': 0x0002, 'leftup': 0x0004,
    'rightdown': 0x0008, 'rightup': 0x0010, 'wheel': 0x0800,
}
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1

# SendInput 结构（64 位下 dwExtraInfo 是 8 字节，必须按指针宽度声明，否则整个结构偏移错位）
_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', _ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', _ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('type', wintypes.DWORD), ('u', _INPUTUNION)]


def _send_unicode(scan):
    """按 UTF-16 码元发一个字符：KEYEVENTF_UNICODE 直接走字符消息，不受键盘布局影响。"""
    for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki.wScan = scan
        inp.ki.dwFlags = flags
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _type_text(text):
    for ch in text:
        if ch == '\r':
            continue
        if ch == '\n':
            _key_tap(0x0D)                       # 回车单独按 VK_RETURN，比发 \n 字符更通用
        elif ch == '\t':
            _key_tap(0x09)
        else:
            raw = ch.encode('utf-16-le')
            for i in range(0, len(raw), 2):      # emoji 等非 BMP 字符是两个码元
                _send_unicode(int.from_bytes(raw[i:i + 2], 'little'))



def _enable_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                   # noqa: BLE001
        try:
            user32.SetProcessDPIAware()
        except Exception:                               # noqa: BLE001
            pass


class Engine:
    """一块屏幕的采集/差分/编码，外加"最新帧"广播。"""

    def __init__(self, fps=10, quality=70, tile=64, idle_fps=5, auto_pause=True):
        self.cam = None
        self.backend = 'gdi'
        try:
            import dxcam
            self.cam = dxcam.create(output_idx=0, output_color='BGRA')
            self.backend = 'dxgi'
        except Exception:                               # noqa: BLE001
            self.cam = None
        self.interval = 1.0 / max(1, fps)
        self.idle_interval = 1.0 / max(1, idle_fps)
        self.quality = quality
        self.tile = max(16, int(tile))
        self.coarse = 8
        self.max_rects = 12
        self.threshold = 0.5
        self.auto_pause = auto_pause
        self.prev = None
        self.seq = 0
        self.packet = None
        self.full_pkt = None
        self.running = False
        self.paused = False
        self.force_full = True
        self.viewers = 0
        self.last_change = 0.0
        self.last_input = 0.0
        self.cond = threading.Condition()
        self.thread = None
        self.started_at = time.time()
        self.frames = 0
        self.bytes = 0
        self.size = None
        self.forced = []            # 点击/按键后强制重发的区域
        self.last_pos = None        # 最近一次光标位置，用于按键后定位重发区域
        self.cursor = None          # 光标所在分片（左上/右下），每帧必传
        self.cursor_prev = None     # 光标挪走前的分片，补发一帧清掉残留

    # -- 采集 --------------------------------------------------------------
    def _grab(self):
        if self.cam is not None:
            try:
                return self.cam.grab()
            except Exception:                           # noqa: BLE001
                self.cam = None
                self.backend = 'gdi'
        return np.asarray(ImageGrab.grab())

    def _jpeg(self, frame, box):
        x0, y0, x1, y1 = box
        if frame.shape[2] == 4:
            sub = np.ascontiguousarray(frame[y0:y1, x0:x1, 2::-1])
        else:
            sub = np.ascontiguousarray(frame[y0:y1, x0:x1])
        buf = io.BytesIO()
        Image.fromarray(sub).save(buf, format='JPEG', quality=self.quality, subsampling=2)
        return buf.getvalue()

    def _rects(self, frame, diff):
        t = time.perf_counter()
        h, w = frame.shape[:2]
        step = self.tile // self.coarse
        cols, rows = max(1, w // self.tile), max(1, h // self.tile)
        grid = diff[:rows * step, :cols * step].reshape(rows, step, cols, step).any(axis=(1, 3))
        runs = []
        for r in range(rows):
            row = grid[r]
            c = 0
            while c < cols:
                if not row[c]:
                    c += 1
                    continue
                c0 = c
                while c < cols and row[c]:
                    c += 1
                runs.append([c0, r, c, r + 1])
        merged = []
        for run in runs:
            if merged and merged[-1][0] == run[0] and merged[-1][2] == run[2] \
                    and merged[-1][3] == run[1]:
                merged[-1][3] = run[3]
            else:
                merged.append(run)
        if len(merged) > self.max_rects:
            merged = [[min(m[0] for m in merged), min(m[1] for m in merged),
                       max(m[2] for m in merged), max(m[3] for m in merged)]]
        boxes = [(c0 * self.tile, r0 * self.tile,
                  min(w, c1 * self.tile), min(h, r1 * self.tile))
                 for c0, r0, c1, r1 in merged]
        return boxes, (time.perf_counter() - t) * 1000

    def mark_dirty(self, x, y, size=320):
        """把某点附近强制纳入下一帧：光标、单个字符这类细小变化 1/8 采样很容易漏掉。"""
        try:
            x, y, size = int(x), int(y), max(64, int(size))
        except (TypeError, ValueError):
            return
        with self.cond:
            self.forced.append((x, y, size))
            if len(self.forced) > 6:                   # 太多就合成一个包围盒
                xs = [f[0] for f in self.forced]
                ys = [f[1] for f in self.forced]
                self.forced = [(min(xs), min(ys), max(f[2] for f in self.forced))]

    def mark_input(self, x, y):
        """记住输入落点，并把它所在的分片标成“每帧必传”。"""
        try:
            x, y = int(x), int(y)
        except (TypeError, ValueError):
            return
        self.last_pos = (x, y)
        tile = self.tile
        x0, y0 = max(0, x) // tile * tile, max(0, y) // tile * tile
        if self.size:
            w, h = self.size
            box = (x0, y0, min(w, x0 + tile), min(h, y0 + tile))
        else:
            box = (x0, y0, x0 + tile, y0 + tile)
        with self.cond:
            if box != self.cursor:
                self.cursor_prev = self.cursor
                self.cursor = box

    @staticmethod
    def _covers(boxes, box):
        """boxes 里是否已经有完全包住 box 的区域（有就不用重复发）。"""
        x0, y0, x1, y1 = box
        return any(a <= x0 and b <= y0 and c >= x1 and d >= y1 for a, b, c, d in boxes)

    def _tick(self):
        frame = self._grab()
        if frame is None:
            return None, None, 0.0
        h, w = frame.shape[:2]
        self.size = (w, h)
        small = frame[::self.coarse, ::self.coarse, 1]
        if self.prev is None or self.force_full:
            self.force_full = False
            self.prev = small.copy()
            box = (0, 0, w, h)
            return [(box, self._jpeg(frame, box))], 1, 1.0
        diff = small != self.prev
        self.prev = small.copy()
        with self.cond:
            forced, self.forced = self.forced, []
            must = [b for b in (self.cursor, self.cursor_prev) if b]
            self.cursor_prev = None
        if not diff.any() and not forced and not must:
            return None, None, 0.0
        boxes = self._rects(frame, diff)[0] if diff.any() else []
        for fx, fy, fs in forced:
            boxes.append((max(0, fx - fs // 2), max(0, fy - fs // 2),
                          min(w, fx + fs // 2), min(h, fy + fs // 2)))
        changed = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
        for box in must:                     # 光标分片必传：悬停高亮、焦点框不会漏
            if not self._covers(boxes, box):
                boxes.append(box)
        if not boxes:
            return None, None, 0.0
        # 只有 diff/点击区算“画面在变”；纯光标分片按空闲帧率发，不掉帧也不空转
        ratio = changed / (w * h)
        flags = 0
        if sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes) > self.threshold * w * h:
            boxes = [(0, 0, w, h)]
            flags = 1
        rects = [(b, self._jpeg(frame, b)) for b in boxes]
        return rects, flags, ratio

    def _loop(self):
        while self.running:
            if self.auto_pause and self.viewers == 0:
                self.paused = True
                time.sleep(0.5)
                continue
            if self.paused:
                self.paused = False
                self.force_full = True
            started = time.perf_counter()
            rects, flags, ratio = self._tick()
            now = time.time()
            if ratio and ratio >= 0.001:
                self.last_change = now
            with self.cond:
                if rects:
                    self.seq += 1
                    head = FRAME.pack(MAGIC, self.seq, flags, len(rects))
                    body = b''.join(
                        RECT.pack(b[0], b[1], b[2] - b[0], b[3] - b[1], len(data)) + data
                        for b, data in rects)
                    pkt = head + body
                    self.packet = pkt
                    if flags == 1:
                        self.full_pkt = pkt
                    self.frames += 1
                    self.bytes += len(pkt)
                    self.cond.notify_all()
            active = (now - max(self.last_change, self.last_input)) < 0.8
            left = (self.interval if active else self.idle_interval) - (time.perf_counter() - started)
            if left > 0:
                time.sleep(left)

    # -- 生命周期 ----------------------------------------------------------
    def start(self):
        if self.running:
            return
        _enable_dpi()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True, name='rf-remote')
        self.thread.start()

    def stop(self):
        self.running = False

    def add_viewer(self):
        with self.cond:
            self.viewers += 1
            self.force_full = True
            self.cond.notify_all()

    def remove_viewer(self):
        with self.cond:
            self.viewers = max(0, self.viewers - 1)

    def wake(self):
        self.last_input = time.time()

    def wait_packet(self, last, timeout=1.0):
        with self.cond:
            if self.seq != last and self.packet:
                return self.packet, self.seq
            self.cond.wait(timeout)
            return self.packet, self.seq

    def stats(self):
        alive = max(0.1, time.time() - self.started_at)
        return {'backend': self.backend, 'fps': round(self.frames / alive, 1),
                'bytes': self.bytes, 'viewers': self.viewers}


_engine = None
_engine_lock = threading.Lock()


def engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
            _engine.start()
        return _engine


def screen_size():
    """优先用采集到的真实帧尺寸（DXGI 走物理像素，GetSystemMetrics 可能被 DPI 虚拟化）。"""
    eng = engine()
    if eng.size:
        return eng.size
    w, h = _screen_size()
    return w, h


def info():
    eng = engine()
    w, h = screen_size()
    return {'w': w, 'h': h, 'backend': eng.backend}


def stream_iter(eng):
    """flask 生成器：把每一帧作为一个 chunk 推给浏览器，客户端断开时自动退出。"""
    eng.add_viewer()
    try:
        last = -1
        if eng.full_pkt:
            yield eng.full_pkt
        while True:
            pkt, seq = eng.wait_packet(last, timeout=1.0)
            if pkt is None or seq == last:
                continue
            last = seq
            yield pkt
    finally:
        eng.remove_viewer()


# --------------------------------------------------------------------------
# 输入注入
# --------------------------------------------------------------------------
def _screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _move(x, y):
    w, h = _screen_size()
    nx = int(int(x) * 65535 / max(1, w - 1))
    ny = int(int(y) * 65535 / max(1, h - 1))
    user32.mouse_event(MOUSEEVENTF['move'] | MOUSEEVENTF['abs'], nx, ny, 0, 0)


# 需要 EXTENDEDKEY 标志的键：方向键、Home/End/PgUp/PgDn、Insert/Delete、Win、右 Ctrl/Alt、小键盘除号、NumLock
EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
               0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}


def _key_event(vk, up=False):
    """按下/抬起一个 VK；方向键等必须带 EXTENDEDKEY，否则远程会把它当成小键盘键。"""
    if not 0 < vk < 256:
        return
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VK else 0)
    user32.keybd_event(vk, 0, flags, 0)


def _key_tap(vk):
    _key_event(vk)
    _key_event(vk, up=True)


def inject(msg):
    t = msg.get('t')
    if t == 'm':
        _move(msg.get('x', 0), msg.get('y', 0))
    elif t in ('ld', 'lu', 'rd', 'ru'):
        if 'x' in msg and 'y' in msg:      # 按下/抬起前先落点，避免快速点击用到旧坐标
            _move(msg['x'], msg['y'])
        user32.mouse_event(MOUSEEVENTF[{'ld': 'leftdown', 'lu': 'leftup',
                                        'rd': 'rightdown', 'ru': 'rightup'}[t]], 0, 0, 0, 0)
    elif t == 'w':
        user32.mouse_event(MOUSEEVENTF['wheel'], 0, 0, int(msg.get('d', 0)), 0)
    elif t in ('kd', 'ku'):
        _key_event(int(msg.get('vk', 0)), t == 'ku')
    elif t == 'tap':
        # 按下和抬起放在同一个请求里完成：浏览器并发 POST 不会把它拆开，
        # 修饰键也是先确认按下再敲键，Ctrl+C 之类的组合才稳。
        for m in msg.get('mods') or []:
            _key_event(int(m))
        if msg.get('mods'):
            time.sleep(0.015)
        vk = int(msg.get('vk', 0))
        _key_event(vk)
        time.sleep(0.015)
        _key_event(vk, up=True)
    elif t == 'rel':
        # 松开虚拟键盘上所有还按着的修饰键，避免留下“卡住”的 Ctrl/Alt
        for m in msg.get('mods') or []:
            _key_event(int(m), up=True)
    elif t == 'txt':
        # 手机端输入的文字：直接按 Unicode 字符注入到远程焦点窗口的光标处
        _type_text(str(msg.get('s') or '')[:2000])


# --------------------------------------------------------------------------
# 网页
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover, interactive-widget=resizes-content">
<title>桌面 · RemoteFM</title>
<style>
 html,body{margin:0;height:100%;overscroll-behavior:none;background:#eef1f7;font:13px/1.5 system-ui,"Segoe UI",sans-serif;color:#2c3e50}
 #bar,#mbar,#kbd{box-sizing:border-box}
 #bar{height:calc(46px + env(safe-area-inset-top,0px));display:flex;align-items:center;gap:14px;
      padding:env(safe-area-inset-top,0px) calc(14px + env(safe-area-inset-right,0px)) 0 calc(14px + env(safe-area-inset-left,0px));
      background:#fff;border-bottom:1px solid #e3e7ef}
 #bar .t{font-weight:600;display:flex;align-items:center;gap:7px}
 #bar .dot{width:8px;height:8px;border-radius:50%;background:#2ecc71}
 #bar .dot.off{background:#c9ced8}
 #bar .stat{color:#7a8299;font-size:12px}
 #bar .sp{flex:1}
 #bar button{border:1px solid #dfe3eb;background:#fff;border-radius:8px;padding:6px 10px;cursor:pointer;font:inherit;color:#2c3e50}
 #bar button:hover{border-color:#4a6cf7;color:#4a6cf7}
 #wrap{position:absolute;inset:calc(46px + env(safe-area-inset-top,0px)) 0 0;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#000}
 body.touch #wrap{inset:calc(46px + env(safe-area-inset-top,0px)) 0 calc(52px + env(safe-area-inset-bottom,0px))}
 body.touch.kbon #wrap{inset:calc(46px + env(safe-area-inset-top,0px)) 0 var(--kbdh,0px)}
 canvas{max-width:100%;max-height:100%;background:#000;cursor:crosshair;touch-action:none;-webkit-user-select:none;user-select:none}
 #msg{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);color:#7a8299}
 /* 手机端底部输入条：发送文字 / 远程退格 / 虚拟键盘 */
 #mbar{position:fixed;left:0;right:0;bottom:0;height:calc(52px + env(safe-area-inset-bottom,0px));display:none;align-items:center;gap:6px;
       padding:0 calc(8px + env(safe-area-inset-right,0px)) env(safe-area-inset-bottom,0px) calc(8px + env(safe-area-inset-left,0px));
       background:#fff;border-top:1px solid #e3e7ef;z-index:8;transition:transform .15s}
 body.touch #mbar{display:flex}
 body.touch.kbon #mbar{display:none}
 #mbar input{flex:1;min-width:0;height:38px;border:1px solid #dfe3eb;border-radius:9px;padding:0 10px;
             font:inherit;font-size:14px;color:#2c3e50;background:#fff}
 #mbar input:focus{outline:none;border-color:#4a6cf7}
 #mbar button{flex-shrink:0;height:38px;min-width:42px;padding:0 10px;border:1px solid #dfe3eb;border-radius:9px;
              background:#f6f8fc;color:#2c3e50;font:inherit;font-size:13px;cursor:pointer;touch-action:manipulation}
 #mbar button:active{background:#e8edf9}
 #mbar #msend{background:#4a6cf7;border-color:#4a6cf7;color:#fff}
 #mbar #mback{font-size:16px}
 #mbar #mkeys.on{background:#4a6cf7;border-color:#4a6cf7;color:#fff}
 /* 虚拟键盘（手机）：标准 60% 键位；Ctrl/Alt/Shift/Win 点一下锁定，再点松开，才能按住做组合键 */
 #kbd{position:fixed;left:0;right:0;bottom:0;display:none;
      padding:28px calc(4px + env(safe-area-inset-right,0px)) calc(6px + env(safe-area-inset-bottom,0px)) calc(4px + env(safe-area-inset-left,0px));
      background:#f2f5fb;border-top:1px solid #e3e7ef;z-index:7;transition:transform .15s}
 body.touch #kbd.show{display:block}
 #khint{position:absolute;left:calc(10px + env(safe-area-inset-left,0px));top:0;height:28px;line-height:28px;
        font-size:10px;color:#8b93a7;pointer-events:none}
 #kbd #kclose{position:absolute;top:3px;right:calc(6px + env(safe-area-inset-right,0px));width:22px;height:22px;
              padding:0;border-radius:50%;border:1px solid #dfe3eb;background:#fff;color:#7a8299;
              font-size:12px;line-height:20px;cursor:pointer;z-index:2}
 #kbd #kclose:active{background:#e8edf9}
 .krow{display:flex;gap:4px;margin-bottom:4px}
 .krow:last-child{margin-bottom:0}
 #kbd button{flex:1 1 0;min-width:0;height:38px;border:1px solid #dfe3eb;border-radius:6px;background:#fff;color:#2c3e50;
             font:inherit;font-size:11px;padding:0 1px;cursor:pointer;white-space:nowrap;touch-action:manipulation;
             overflow:hidden;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
 #kbd button:active{background:#e8edf9}
 #kbd .wide{font-size:10px}
 #kbd .mod.on,#kbd .cap.on{background:#4a6cf7;border-color:#4a6cf7;color:#fff}
 /* 横屏手机屏幕矮：键盘整体缩到约 2/3，给远程画面留空间 */
 @media (orientation:landscape) and (max-height:520px){
  #kbd{padding:22px calc(3px + env(safe-area-inset-right,0px)) calc(3px + env(safe-area-inset-bottom,0px)) calc(3px + env(safe-area-inset-left,0px))}
  #kbd button{height:22px;font-size:10px}
  #kbd .krow{gap:3px;margin-bottom:3px}
  #kbd #kclose{width:18px;height:18px;top:2px;font-size:10px;line-height:16px}
  #khint{display:none}
 }
 html:fullscreen #kbd{background:rgba(18,24,38,.92);border-top-color:rgba(255,255,255,.15)}
 html:fullscreen #kbd button{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.22);color:#fff}
 html:fullscreen #kbd button.on{background:#4a6cf7;border-color:#4a6cf7}
 html:fullscreen #kbd #kclose{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.22);color:#fff}
 /* 真全屏：隐藏顶栏，画布铺满整个屏幕 */
 html:fullscreen #bar{display:none}
 html:fullscreen #wrap{inset:0}
 html:fullscreen body.touch #wrap{inset:0 0 calc(52px + env(safe-area-inset-bottom,0px))}
 html:fullscreen body.touch.kbon #wrap{inset:0 0 var(--kbdh,0px)}
 html:fullscreen, html:fullscreen body{background:#000}
 #fsExit{position:fixed;top:12px;right:12px;z-index:9;display:none;padding:7px 13px;border:1px solid rgba(255,255,255,.3);
         border-radius:8px;background:rgba(18,24,38,.6);color:#fff;font:inherit;font-size:12px;cursor:pointer;opacity:0;transition:opacity .25s}
 #fsExit.show{opacity:1}
 #fsExit:hover{background:rgba(18,24,38,.85)}
</style></head><body>
<div id="bar">
  <span class="t"><span class="dot off" id="dot"></span>桌面</span>
  <span class="stat" id="stat">连接中…</span>
  <span class="sp"></span>
  <button id="fs">全屏</button>
</div>
<div id="wrap"><canvas id="cv" width="1280" height="720"></canvas><div id="msg">正在连接…</div></div>
<button id="fsExit" title="按 Esc 也可以退出全屏">退出全屏</button>
<div id="mbar">
  <input id="mtext" type="text" placeholder="输入信息发送到远端光标位置" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" enterkeyhint="send">
  <button id="mback" title="远程退格（删除一个字符）">⌫</button>
  <button id="msend">发送</button>
  <button id="mkeys" title="虚拟键盘：Ctrl/Alt/Shift/Win 组合键、方向键">⌨</button>
</div>
<div id="kbd">
  <span id="khint">Ctrl / Shift 点一下按住，再点松开</span>
  <button id="kclose" title="关闭键盘，回到输入栏">✕</button>
</div>
<script>
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const dot = document.getElementById('dot'), stat = document.getElementById('stat'), msg = document.getElementById('msg');
const mbar = document.getElementById('mbar'), kbd = document.getElementById('kbd');
let frames = 0, bytes = 0, last = performance.now();

/* 画面上不再画鼠标箭头：光标所在分片由服务端每帧必传，画布就是远端真实画面 */

function draw(blob, x, y, w, h, full) {
  return createImageBitmap(blob).then(bmp => {
    if (full) { ctx.clearRect(0, 0, cv.width, cv.height); x = 0; y = 0; }
    ctx.drawImage(bmp, x, y, w, h);
  });
}

async function main() {
  const info = await (await fetch('/remote/info')).json();
  cv.width = info.w; cv.height = info.h;
  msg.style.display = 'none';
  const res = await fetch('/remote/stream');
  if (!res.ok || !res.body) throw new Error('stream ' + res.status);
  const reader = res.body.getReader();
  let buf = new Uint8Array(0);
  dot.classList.remove('off');
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    const merged = new Uint8Array(buf.length + value.length);
    merged.set(buf); merged.set(value, buf.length); buf = merged;
    for (;;) {
      if (buf.length < 10) break;
      if (String.fromCharCode(buf[0], buf[1], buf[2], buf[3]) !== 'RF01') { buf = new Uint8Array(0); break; }
      const hv = new DataView(buf.buffer, buf.byteOffset, 10);
      const flags = hv.getUint8(8), count = hv.getUint8(9);
      let off = 10, ok = true; const parts = [];
      for (let i = 0; i < count; i++) {
        if (buf.length < off + 12) { ok = false; break; }
        const rv = new DataView(buf.buffer, buf.byteOffset + off, 12);
        const x = rv.getUint16(0), y = rv.getUint16(2), w = rv.getUint16(4),
              h = rv.getUint16(6), len = rv.getUint32(8);
        if (buf.length < off + 12 + len) { ok = false; break; }
        parts.push([x, y, w, h, buf.slice(off + 12, off + 12 + len)]);
        off += 12 + len;
      }
      if (!ok) break;
      buf = buf.slice(off);
      if (parts.length) frames++;
      const jobs = [];
      for (const [x, y, w, h, data] of parts) {
        jobs.push(draw(new Blob([data], {type: 'image/jpeg'}), x, y, w, h, flags === 1));
        bytes += data.length;
      }
      if (jobs.length) Promise.all(jobs);
    }
    const now = performance.now();
    if (now - last > 1000) {
      stat.textContent = (frames / ((now - last) / 1000)).toFixed(1) + ' fps · ' +
        (bytes * 8 / ((now - last) / 1000) / 1e6).toFixed(2) + ' Mbps';
      frames = 0; bytes = 0; last = now;
    }
  }
  dot.classList.add('off');
  stat.textContent = '连接已断开';
}

function pt(e) {
  const r = cv.getBoundingClientRect();
  return {x: Math.round((e.clientX - r.left) * cv.width / r.width),
          y: Math.round((e.clientY - r.top) * cv.height / r.height)};
}
let throttled = 0, tapAt = 0;
const isTouch = ('ontouchstart' in window) || navigator.maxTouchPoints > 0
                || matchMedia('(pointer: coarse)').matches;
if (isTouch) document.body.classList.add('touch');

/* 输入请求串行发送：键盘按下/抬起/敲键必须按顺序到达，否则组合键会被并发 POST 拆散 */
let qchain = Promise.resolve();
function send(o) {
  qchain = qchain.then(() => {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), 4000);
    return fetch('/remote/input', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(o), signal: ac.signal})
      .catch(() => {}).finally(() => clearTimeout(timer));
  });
  return qchain;
}
/* 鼠标移动不排队，避免拖慢键盘；坐标本身自带定位，不存在顺序问题 */
function sendNow(o) {
  fetch('/remote/input', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(o)}).catch(() => {});
}

/* 滚动：统一归一成 120 的整数倍；step 是“累积多少像素算一格”，鼠标和手指用不同手感 */
let wheelAcc = 0;
function wheelBy(delta, step) {
  wheelAcc += delta;
  if (Math.abs(wheelAcc) < step) return;
  const notches = Math.trunc(wheelAcc / step);
  wheelAcc -= notches * step;
  send({t: 'w', d: notches * 120});
}

cv.addEventListener('mousemove', e => {
  if (performance.now() - tapAt < 700) return;      // 触摸之后浏览器会补发鼠标事件，忽略
  const p = pt(e);
  const now = performance.now(); if (now - throttled < 33) return; throttled = now;
  sendNow(Object.assign({t: 'm'}, p));
});
cv.addEventListener('mousedown', e => {
  if (performance.now() - tapAt < 700) return;
  e.preventDefault();
  const p = pt(e);
  send(Object.assign({t: e.button === 2 ? 'rd' : 'ld'}, p));
});
cv.addEventListener('mouseup', e => {
  if (performance.now() - tapAt < 700) return;
  const p = pt(e);
  send(Object.assign({t: e.button === 2 ? 'ru' : 'lu'}, p));
});
cv.addEventListener('contextmenu', e => e.preventDefault());
cv.addEventListener('wheel', e => { e.preventDefault(); wheelBy(-e.deltaY, 100); }, {passive: false});
addEventListener('keydown', e => {
  if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;   // 手机输入框里的按键不转发
  e.preventDefault(); send({t: 'kd', vk: e.keyCode});
});
addEventListener('keyup', e => {
  if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
  e.preventDefault(); send({t: 'ku', vk: e.keyCode});
});

/* 手机手势：单指拖动 = 滚动；轻点 = 左键；长按 550ms = 右键 */
let tStart = null, tMoved = false, tTimer = 0, tClientY = 0;

function tpoint(t) {
  const r = cv.getBoundingClientRect();
  return {x: Math.round((t.clientX - r.left) * cv.width / r.width),
          y: Math.round((t.clientY - r.top) * cv.height / r.height)};
}

cv.addEventListener('touchstart', e => {
  tapAt = performance.now();
  if (e.touches.length !== 1) { clearTimeout(tTimer); return; }
  e.preventDefault();
  const p = tpoint(e.touches[0]);
  tStart = p; tMoved = false;
  tClientY = e.touches[0].clientY;
  sendNow(Object.assign({t: 'm'}, p));
  clearTimeout(tTimer);
  tTimer = setTimeout(() => {
    if (tMoved) return;
    tMoved = true;
    send(Object.assign({t: 'rd'}, tStart));
    send(Object.assign({t: 'ru'}, tStart));
  }, 550);
}, {passive: false});

cv.addEventListener('touchmove', e => {
  tapAt = performance.now();
  if (!tStart || e.touches.length !== 1) return;
  e.preventDefault();
  const p = tpoint(e.touches[0]);
  if (!tMoved && Math.abs(p.x - tStart.x) + Math.abs(p.y - tStart.y) > 12) {
    tMoved = true; clearTimeout(tTimer);
  }
  if (tMoved) {                       // 按手指在屏幕上的实际位移算，避免被缩放放大
    const cy = e.touches[0].clientY;
    wheelBy(cy - tClientY, 40);
    tClientY = cy;
  }
}, {passive: false});

cv.addEventListener('touchend', () => {
  tapAt = performance.now();
  clearTimeout(tTimer);
  if (tStart && !tMoved) {
    send(Object.assign({t: 'ld'}, tStart));
    setTimeout(() => send(Object.assign({t: 'lu'}, tStart)), 25);
  }
  tStart = null; tMoved = false;
});
cv.addEventListener('touchcancel', () => { clearTimeout(tTimer); tStart = null; tMoved = false; });

/* 手机底部输入条：发送文字到远程光标处；⌫ 远程退格 */
const mtext = document.getElementById('mtext');
function remoteBackspace() { send({t: 'tap', vk: 8}); }
function sendText() {
  const v = mtext.value;
  if (!v) return;
  send({t: 'txt', s: v});
  mtext.value = '';
}
document.getElementById('msend').onclick = sendText;
document.getElementById('mback').onclick = remoteBackspace;
mtext.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); sendText(); return; }
  if (e.key === 'Backspace' && mtext.value === '') { e.preventDefault(); remoteBackspace(); }
});

/* 虚拟键盘：标准 60% 键位。修饰键点一下“按住”，再点一下松开，
   这样 Ctrl+C / Ctrl+← / Alt+Tab 和真键盘一样是按住修饰键再敲其他键 */
const VK = {
  '1':49,'2':50,'3':51,'4':52,'5':53,'6':54,'7':55,'8':56,'9':57,'0':48,
  '-':189,'=':187,'[':219,']':221,'\\':220,';':186,"'":222,',':188,'.':190,'/':191,
  'q':81,'w':87,'e':69,'r':82,'t':84,'y':89,'u':85,'i':73,'o':79,'p':80,
  'a':65,'s':83,'d':68,'f':70,'g':71,'h':72,'j':74,'k':75,'l':76,
  'z':90,'x':88,'c':67,'v':86,'b':66,'n':78,'m':77
};
const SHIFTED = {'1':'!','2':'@','3':'#','4':'$','5':'%','6':'^','7':'&','8':'*','9':'(','0':')',
  '-':'_','=':'+','[':'{',']':'}','\\':'|',';':':',"'":'"',',':'<','.':'>','/':'?'};
const SPECIAL = {'⌫':8,'Tab':9,'⏎':13,'Shift':16,'Ctrl':17,'Alt':18,'Win':91,'空格':32,'Esc':27,
  'Del':46,'←':37,'↑':38,'↓':40,'→':39,'Home':36,'End':35,'PgUp':33,'PgDn':34,'Caps':20};
const LAYOUT = [
  [['1',1],['2',1],['3',1],['4',1],['5',1],['6',1],['7',1],['8',1],['9',1],['0',1],['-',1],['=',1],['⌫',1.4]],
  [['Tab',1.3],['q',1],['w',1],['e',1],['r',1],['t',1],['y',1],['u',1],['i',1],['o',1],['p',1],['[',1],[']',1]],
  [['Caps',1.3],['a',1],['s',1],['d',1],['f',1],['g',1],['h',1],['j',1],['k',1],['l',1],[';',1],["'",1],['⏎',1.5]],
  [['Shift',1.6],['z',1],['x',1],['c',1],['v',1],['b',1],['n',1],['m',1],[',',1],['.',1],['/',1],['Shift',1.6]],
  [['Ctrl',1.3],['Win',1.1],['Alt',1.1],['空格',4],['Alt',1.1],['Win',1.1],['Ctrl',1.3]],
  [['Esc',1.1],['Del',1.1],['←',1],['↑',1],['↓',1],['→',1],['Home',1.3],['End',1.3],['PgUp',1.3],['PgDn',1.3]]
];
const MOD_VK = new Set([16, 17, 18, 91]);
const mods = new Set();
let capsOn = false;

for (const row of LAYOUT) {
  const div = document.createElement('div');
  div.className = 'krow';
  for (const [label, grow] of row) {
    const b = document.createElement('button');
    b.textContent = label;
    if (grow !== 1) b.style.flexGrow = grow;
    if (label.length > 1) b.classList.add('wide');
    if (MOD_VK.has(SPECIAL[label])) {
      b.dataset.mod = SPECIAL[label];
      b.classList.add('mod');
    } else if (label === 'Caps') {
      b.classList.add('cap');
    } else if (VK[label] !== undefined) {
      b.dataset.ch = label;
    } else {
      b.dataset.vk = SPECIAL[label];
    }
    div.appendChild(b);
  }
  kbd.appendChild(div);
}

/* Shift/Caps 生效时键帽跟着变：字母大写、数字符号位显示 !@# */
function relabel() {
  const shift = mods.has(16), upper = shift || capsOn;
  kbd.querySelectorAll('button[data-ch]').forEach(b => {
    const ch = b.dataset.ch;
    if (shift && SHIFTED[ch]) b.textContent = SHIFTED[ch];
    else if (upper && ch >= 'a' && ch <= 'z') b.textContent = ch.toUpperCase();
    else b.textContent = ch;
  });
}
function syncMods() {
  kbd.querySelectorAll('.mod').forEach(b => b.classList.toggle('on', mods.has(+b.dataset.mod)));
  const cap = kbd.querySelector('.cap');
  if (cap) cap.classList.toggle('on', capsOn);
  relabel();
}
function toggleMod(vk) {
  if (mods.has(vk)) { mods.delete(vk); send({t: 'ku', vk}); }
  else { mods.add(vk); send({t: 'kd', vk}); }
  syncMods();
}
function releaseMods(beacon) {
  if (!mods.size) return;
  const list = [...mods];
  mods.clear(); syncMods();
  if (beacon && navigator.sendBeacon) {
    navigator.sendBeacon('/remote/input',
      new Blob([JSON.stringify({t: 'rel', mods: list})], {type: 'application/json'}));
  } else {
    send({t: 'rel', mods: list});
  }
}
kbd.addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b || !kbd.contains(b) || b.id === 'kclose') return;
  if (b.dataset.mod) { toggleMod(+b.dataset.mod); return; }
  if (b.classList.contains('cap')) {
    capsOn = !capsOn;
    send({t: 'tap', vk: 20});                        // 和真键盘一样，按一次切换 CapsLock
    syncMods();
    return;
  }
  const vk = b.dataset.ch ? VK[b.dataset.ch] : +b.dataset.vk;
  send({t: 'tap', vk, mods: [...mods]});            // 一个请求完成按下+抬起，不会乱序
});

const mkeys = document.getElementById('mkeys');
function setKbd(on) {
  kbd.classList.toggle('show', on);
  mkeys.classList.toggle('on', on);
  if (!on) releaseMods();
  syncKbdHeight();
}
mkeys.onclick = () => setKbd(!kbd.classList.contains('show'));
document.getElementById('kclose').onclick = () => setKbd(false);
/* 键盘展开时收起输入栏，画布区域让出键盘高度；收起后恢复输入栏 */
function syncKbdHeight() {
  const on = kbd.classList.contains('show');
  document.body.classList.toggle('kbon', on);
  document.documentElement.style.setProperty('--kbdh', (on ? kbd.offsetHeight : 0) + 'px');
}
if (window.ResizeObserver) new ResizeObserver(syncKbdHeight).observe(kbd);
/* 关页面/切到后台时把锁定的修饰键松开，避免远程 Ctrl 卡住 */
addEventListener('pagehide', () => releaseMods(true));
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') releaseMods(true);
});

/* 原生键盘弹出时，把底部栏整体顶到键盘上方，避免被遮挡 */
const vv = window.visualViewport;
function fitVp() {
  if (!vv) return;
  const hidden = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
  const tf = hidden > 0 ? `translateY(${-hidden}px)` : '';
  mbar.style.transform = tf;
  kbd.style.transform = tf;
}
if (vv) {
  vv.addEventListener('resize', fitVp);
  vv.addEventListener('scroll', fitVp);
}
mtext.addEventListener('focus', () => {
  if (window.innerHeight - (vv ? vv.height : window.innerHeight) > 120) {
    setKbd(false);
  }
});

/* 真全屏：顶栏隐藏、画布铺满；退出按钮 1.8 秒无操作后淡出 */
const fsExit = document.getElementById('fsExit');
let fsTimer = 0;
function toggleFs() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen({navigationUI: 'hide'}).catch(() => {});
}
document.getElementById('fs').onclick = toggleFs;
fsExit.onclick = () => document.exitFullscreen();
document.addEventListener('fullscreenchange', () => {
  const on = !!document.fullscreenElement;
  fsExit.style.display = on ? 'block' : 'none';
  fsExit.classList.remove('show');
});
document.addEventListener('mousemove', () => {
  if (!document.fullscreenElement) return;
  fsExit.classList.add('show');
  clearTimeout(fsTimer);
  fsTimer = setTimeout(() => fsExit.classList.remove('show'), 1800);
});

main().catch(e => { msg.textContent = '连接失败：' + e; dot.classList.add('off'); });
</script></body></html>"""
