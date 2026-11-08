# -*- coding: utf-8 -*-
"""
快驴上架系统 · 手机桥接服务（阶段一半自动）
职责：网页(localhost:5173) <-> adb <-> 安卓真机
接口：
  GET  /api/device  检测设备连接状态
  POST /api/push    接收图片(base64 dataURL)，清空手机 KLPush 目录后按 KL{code}-NN 命名推送并触发媒体扫描
运行：python phone_bridge.py （或双击 start_bridge.bat），监听 127.0.0.1:5175
"""
import base64
import io
import json
import os
import threading
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 5175
PHONE_DIR = "/sdcard/Pictures/KLPush"

# adb 定位：优先内置 platform-tools（避免用到模拟器等旧版 adb），其次 PATH，再次 D:\tools\platform-tools
ADB_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform-tools", "adb.exe"),
    "adb",
    r"D:\tools\platform-tools\adb.exe",
]

_ADB_CACHE = None

def adb_path():
    """定位 adb 可执行文件（结果缓存，避免每次都跑 adb version 拖慢响应）"""
    global _ADB_CACHE
    if _ADB_CACHE:
        return _ADB_CACHE
    for cand in ADB_CANDIDATES:
        try:
            r = subprocess.run([cand, "version"], capture_output=True, timeout=10)
            if r.returncode == 0:
                _ADB_CACHE = cand
                return cand
        except Exception:
            continue
    return None

def adb(*args, timeout=30):
    exe = adb_path()
    if not exe:
        raise RuntimeError("未找到 adb，请下载 platform-tools 解压到 phone-bridge/platform-tools 或 D:\\tools\\platform-tools")
    return subprocess.run([exe, *args], capture_output=True, timeout=timeout)

def restart_adb():
    """重启 adb 服务，用于重新触发手机授权弹窗、修复离线状态"""
    exe = adb_path()
    if not exe:
        raise RuntimeError("未找到 adb")
    subprocess.run([exe, "kill-server"], capture_output=True, timeout=20)
    time.sleep(1)
    subprocess.run([exe, "start-server"], capture_output=True, timeout=20)
    time.sleep(1)
    return device_info(_auto_heal=False)


_HEAL_LOCK = threading.Lock()
_HEAL_LAST = 0.0

def _auto_heal_adb():
    """设备列表为空/离线时自动重启一次 adb server 自愈（60s 冷却，避免打断直播）"""
    global _HEAL_LAST
    with _HEAL_LOCK:
        if time.time() - _HEAL_LAST < 60:
            return False
        _HEAL_LAST = time.time()
    try:
        exe = adb_path()
        if exe:
            subprocess.run([exe, "kill-server"], capture_output=True, timeout=15)
            time.sleep(1)
            subprocess.run([exe, "start-server"], capture_output=True, timeout=15)
            time.sleep(1.5)
            return True
    except Exception:
        pass
    return False

def device_info(_auto_heal=True):
    """返回 state: ready / unauthorized / offline / none / no_adb
    _auto_heal=True 时，检测不到设备会自动重启 adb server 重试一次"""
    try:
        r = adb("devices", "-l", timeout=10)
    except RuntimeError as e:
        return {"connected": False, "state": "no_adb", "reason": str(e),
                "hint": "未找到 adb，请确认 phone-bridge/platform-tools/adb.exe 存在"}
    except Exception as e:
        return {"connected": False, "state": "error", "reason": str(e), "hint": "adb 执行异常，尝试重启桥接服务"}

    out = r.stdout.decode("utf-8", "ignore")
    lines = [l.strip() for l in out.splitlines()[1:] if l.strip()]
    if not lines:
        if _auto_heal and _auto_heal_adb():
            return device_info(_auto_heal=False)
        return {"connected": False, "state": "none", "reason": "未检测到设备",
                "hint": "① 手机保持亮屏解锁状态 ② 重新插拔数据线，手机下拉通知栏把 USB 连接方式选为「传输文件」"
                        "③ 弹出「允许 USB 调试」时勾选「一律允许」④ 仍不行则换 USB 口/数据线，或重启手机"}

    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "unauthorized":
            return {"connected": False, "state": "unauthorized", "serial": serial,
                    "reason": "手机已插入，但尚未授权 USB 调试",
                    "hint": "解锁手机屏幕，点「允许 USB 调试」并勾选「一律允许」；"
                            "若没有弹窗，到开发者选项点「撤销 USB 调试授权」，再重新插拔数据线"}
        if state == "offline":
            if _auto_heal and _auto_heal_adb():
                return device_info(_auto_heal=False)
            return {"connected": False, "state": "offline", "serial": serial,
                    "reason": "设备离线",
                    "hint": "重新插拔数据线，或点「重新触发授权」重启 adb 服务"}
        if state == "device":
            model = serial
            for p in parts[2:]:
                if p.startswith("model:"):
                    model = p.split(":", 1)[1] or serial
            try:
                m = adb("-s", serial, "shell", "getprop", "ro.product.model", timeout=10)
                model = m.stdout.decode("utf-8", "ignore").strip() or model
            except Exception:
                pass
            return {"connected": True, "state": "ready", "serial": serial, "model": model,
                    "reason": "设备已连接", "hint": ""}

    return {"connected": False, "state": "none", "reason": "未检测到可用设备",
            "hint": "重新插拔数据线后再点检测"}


# ---------- 屏幕画面（后台常驻抓屏，请求直接拿最新帧，避免排队造成卡顿） ----------
_screen_cache = {"ts": 0.0, "jpg": b"", "w": 0, "h": 0, "dw": 0, "dh": 0, "err": ""}
_screen_lock = threading.Lock()
_screen_cfg = {"w": 400, "q": 55, "wake": False, "last_req": 0.0}
_screen_thread = None
_size_cache = {"w": 0, "h": 0}

def _capture_frame(max_width, quality, wake=False):
    if wake:
        try:
            adb("shell", "input", "keyevent", "224", timeout=10)  # KEYCODE_WAKEUP
        except Exception:
            pass
    raw = adb("exec-out", "screencap", "-p", timeout=20).stdout
    if not raw:
        # adb 服务刚重启时偶发空返回，重试一次
        time.sleep(0.3)
        raw = adb("exec-out", "screencap", "-p", timeout=20).stdout
    if not raw:
        raise RuntimeError("抓屏失败，请确认设备已授权并点亮屏幕")
    from PIL import Image
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    ow, oh = img.size
    if ow > max_width:
        img = img.resize((max_width, int(oh * max_width / ow)), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return {"ts": time.time(), "jpg": buf.getvalue(), "w": ow, "h": oh,
            "dw": img.width, "dh": img.height}

def _screen_worker():
    while True:
        cfg = dict(_screen_cfg)
        # 没人看画面时（直播模式会用 screenrecord，两者同时跑会抢手机性能）就暂停抓屏
        if time.time() - cfg.get("last_req", 0) > 8:
            time.sleep(0.5)
            continue
        wake = cfg.get("wake", False)
        if wake:
            _screen_cfg["wake"] = False
        try:
            res = _capture_frame(cfg["w"], cfg["q"], wake)
            with _screen_lock:
                _screen_cache.update(res)
                _screen_cache["err"] = ""
        except Exception as e:
            with _screen_lock:
                _screen_cache["err"] = str(e)
            time.sleep(1.0)

def ensure_screen_thread():
    global _screen_thread
    if _screen_thread is None or not _screen_thread.is_alive():
        _screen_thread = threading.Thread(target=_screen_worker, daemon=True)
        _screen_thread.start()

def latest_screen(max_width=400, quality=55, wake=False):
    _screen_cfg["w"] = max_width
    _screen_cfg["q"] = quality
    _screen_cfg["last_req"] = time.time()
    if wake:
        _screen_cfg["wake"] = True
    ensure_screen_thread()
    # 首帧最多等 3 秒
    for _ in range(30):
        with _screen_lock:
            if _screen_cache["jpg"] and time.time() - _screen_cache["ts"] < 5:
                return dict(_screen_cache)
            err = _screen_cache["err"]
        if err:
            raise RuntimeError(err)
        time.sleep(0.1)
    with _screen_lock:
        if _screen_cache["jpg"]:
            return dict(_screen_cache)
        raise RuntimeError(_screen_cache["err"] or "抓屏超时，请点亮手机屏幕后重试")

def screencap_jpeg(max_width=400, quality=55, wake=False):
    return latest_screen(max_width, quality, wake)

def device_size():
    """读取手机真实分辨率（缓存），直播模式用来换算点击坐标，不触发抓屏"""
    if _size_cache["w"]:
        return dict(_size_cache)
    out = adb("shell", "wm", "size", timeout=15).stdout.decode("utf-8", "ignore")
    m = re.search(r"Override size:\s*(\d+)x(\d+)", out) or re.search(r"Physical size:\s*(\d+)x(\d+)", out)
    if not m:
        raise RuntimeError("无法读取手机分辨率")
    _size_cache["w"] = int(m.group(1))
    _size_cache["h"] = int(m.group(2))
    return dict(_size_cache)

def screen_state():
    """返回锁屏/亮屏/电量状态（screenrecord 在锁屏状态下无法工作）"""
    res = {"locked": False, "screen_on": True, "battery": -1}
    try:
        win = adb("shell", "dumpsys", "window", timeout=20).stdout.decode("utf-8", "ignore")
        res["locked"] = "mDreamingLockscreen=true" in win
    except Exception:
        pass
    try:
        pw = adb("shell", "dumpsys", "power", timeout=20).stdout.decode("utf-8", "ignore")
        res["screen_on"] = "mWakefulness=Awake" in pw
    except Exception:
        pass
    try:
        bt = adb("shell", "dumpsys", "battery", timeout=20).stdout.decode("utf-8", "ignore")
        m = re.search(r"level:\s*(\d+)", bt)
        if m:
            res["battery"] = int(m.group(1))
    except Exception:
        pass
    return res

# ---------- 挂机暗屏（插线时手机黑屏不发烫，平台仍可操作；拔线自动恢复） ----------
WATCH_PATH = "/data/local/tmp/kl_watch.sh"
RESTORE_PATH = "/data/local/tmp/kl_restore.sh"
_dim_state = {"on": False}

def _sget(ns, key):
    try:
        return adb("shell", "settings", "get", ns, key, timeout=15).stdout.decode("utf-8", "ignore").strip()
    except Exception:
        return ""

def _sput(ns, key, val):
    adb("shell", "settings", "put", ns, key, str(val), timeout=15)

def _push_text(path, content):
    tmp = tempfile.mkdtemp(prefix="klsh_")
    local = os.path.join(tmp, os.path.basename(path))
    with open(local, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(content)
    try:
        adb("push", local, path, timeout=30)
        adb("shell", "chmod", "755", path, timeout=15)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _usb_probe():
    """找手机上能反映 USB 插拔的 sysfs 节点"""
    for p in ("/sys/class/power_supply/usb/online",
              "/sys/class/power_supply/Usb/online",
              "/sys/class/power_supply/usb_present/online"):
        try:
            out = adb("shell", "cat", p, timeout=10).stdout.decode("utf-8", "ignore").strip()
        except Exception:
            continue
        if out in ("0", "1"):
            return p
    return ""

def screen_dim(on=True):
    """开/关「挂机暗屏」。真正关掉显示后安卓会丢弃触摸事件、也无法录屏，
    所以这里用「亮度归零 + 保持唤醒」实现看起来熄屏但操作照常；并在手机上驻留一个
    守护脚本，拔掉数据线后自动把亮度恢复成原样（此时已无法通过 adb 下发命令）。"""
    if on:
        orig = {
            "b": _sget("system", "screen_brightness"),
            "m": _sget("system", "screen_brightness_mode"),
            "s": _sget("global", "stay_on_while_plugged_in"),
            "t": _sget("system", "screen_off_timeout"),
        }
        lines = []
        for ns, key, val in (("system", "screen_brightness", orig["b"]),
                             ("system", "screen_brightness_mode", orig["m"]),
                             ("global", "stay_on_while_plugged_in", orig["s"]),
                             ("system", "screen_off_timeout", orig["t"])):
            if val and val != "null":
                lines.append("settings put %s %s %s" % (ns, key, val))
        _push_text(RESTORE_PATH, "#!/system/bin/sh\n" + "\n".join(lines) + "\n")
        probe = _usb_probe()
        if probe:
            cond = 'v=$(cat %s 2>/dev/null); [ "$v" = "0" ]' % probe
        else:
            cond = 'dumpsys battery | grep -q "USB powered: false"'
        watch = (
            "#!/system/bin/sh\n"
            "R=%s\n"
            "i=0\n"
            "while [ -f $R ]; do\n"
            "  if %s; then\n"
            "    sh $R; rm -f $R; break\n"
            "  fi\n"
            "  i=$((i+1)); [ $i -gt 3600 ] && break\n"
            "  sleep 2\n"
            "done\n"
        ) % (RESTORE_PATH, cond)
        _push_text(WATCH_PATH, watch)
        # 保持唤醒 + 亮度归零（看起来就是黑屏）
        _sput("global", "stay_on_while_plugged_in", "3")
        _sput("system", "screen_brightness_mode", "0")
        _sput("system", "screen_brightness", "0")
        adb("shell", "nohup sh %s >/dev/null 2>&1 &" % WATCH_PATH, timeout=15)
        _dim_state["on"] = True
    else:
        try:
            adb("shell", "sh", RESTORE_PATH, timeout=15)
        except Exception:
            pass
        try:
            adb("shell", "rm", "-f", RESTORE_PATH, timeout=15)
            adb("shell", "rm", "-f", WATCH_PATH, timeout=15)
        except Exception:
            pass
        _dim_state["on"] = False
    return dict(_dim_state)

def dim_status():
    """返回暗屏是否开启（以手机上的恢复脚本是否存在为准）"""
    try:
        out = adb("shell", "ls", RESTORE_PATH, timeout=10).stdout.decode("utf-8", "ignore")
        _dim_state["on"] = RESTORE_PATH in out
    except Exception:
        pass
    return dict(_dim_state)

# ---------- 输入控制 ----------
ADB_IME = "com.android.adbkeyboard/.AdbIME"
ADB_PKG = "com.android.adbkeyboard"

def adb_input(*args, timeout=20):
    return adb("shell", "input", *args, timeout=timeout)

def tap(x, y):
    adb_input("tap", str(int(x)), str(int(y)))

def swipe(x1, y1, x2, y2, duration=300):
    adb_input("swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration)))

def key(code):
    adb_input("keyevent", str(code))

_orig_ime = None

def ensure_adb_ime():
    """确保 ADBKeyboard 已启用并选中，用于发送中文文本（记录原输入法，发送后还原）"""
    global _orig_ime
    installed = adb("shell", "pm", "list", "packages", ADB_PKG, timeout=15)
    if ADB_PKG.encode() not in installed.stdout:
        # 自动安装内置的 ADBKeyboard.apk（中文输入组件），省去客户手动装
        apk = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ADBKeyboard.apk")
        if not os.path.isfile(apk):
            raise RuntimeError("手机未安装 ADBKeyboard 且未找到内置安装包 ADBKeyboard.apk")
        r = adb("install", "-r", apk, timeout=60)
        if b"Success" not in r.stdout + r.stderr:
            raise RuntimeError("ADBKeyboard 自动安装失败：" + r.stdout.decode("utf-8", "ignore").strip()
                               + "；请手动把 ADBKeyboard.apk 装到手机")
        time.sleep(0.5)
    adb("shell", "ime", "enable", ADB_IME, timeout=15)
    cur = adb("shell", "settings", "get", "secure", "default_input_method", timeout=15)
    cur_text = cur.stdout.decode("utf-8", "ignore").strip()
    if ADB_IME not in cur_text:
        if cur_text and cur_text != "null":
            _orig_ime = cur_text
        adb("shell", "ime", "set", ADB_IME, timeout=15)
        time.sleep(0.4)

def restore_ime():
    """还原用户原来的输入法，避免手机上一直没有软键盘"""
    if not _orig_ime:
        return
    try:
        adb("shell", "ime", "set", _orig_ime, timeout=15)
    except Exception:
        pass

def input_text(text):
    """向手机当前焦点输入框写入文本（支持中文，走 ADBKeyboard 广播 B64 协议）"""
    ensure_adb_ime()
    # ADB_INPUT_B64 的 msg 必须是「整段文本」的 base64，不能拆 base64 串；
    # 文本过长时按原文分段，每段单独 base64 后依次广播。
    for i in range(0, max(len(text), 1), 500):
        part = text[i:i + 500]
        if not part:
            continue
        b64 = base64.b64encode(part.encode("utf-8")).decode("ascii")
        adb("shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64, timeout=20)
        time.sleep(0.08)
    time.sleep(0.2)
    restore_ime()
    return len(text)

def clear_text():
    ensure_adb_ime()
    adb("shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT", timeout=20)
    time.sleep(0.1)
    restore_ime()

def push_images(code, files):
    """files: [{name, data(dataURL)}] -> 命名 KL{code}-01.{ext}"""
    if not code or not re.match(r"^[A-Z0-9]{1,12}$", code):
        raise ValueError("code 不合法")
    # 1. 清空手机旧图，防止相册排序错乱
    adb("shell", "rm", "-rf", PHONE_DIR, timeout=20)
    adb("shell", "mkdir", "-p", PHONE_DIR, timeout=20)
    # 2. 逐张写临时文件并 push
    count = 0
    tmpdir = tempfile.mkdtemp(prefix="klpush_")
    try:
        for i, f in enumerate(files, start=1):
            m = re.match(r"^data:image\/(\w+);base64,(.+)$", f.get("data", ""), re.S)
            if not m:
                continue
            ext = "jpg" if m.group(1).lower() in ("jpeg", "jpg") else m.group(1).lower()
            ext = {"png": "png"}.get(ext, "jpg")
            target = f"KL{code}-{i:02d}.{ext}"
            local = os.path.join(tmpdir, target)
            with open(local, "wb") as fp:
                fp.write(base64.b64decode(m.group(2)))
            adb("push", local, f"{PHONE_DIR}/{target}", timeout=60)
            # 3. 触发媒体扫描让相册立即刷新
            adb("shell", "am", "broadcast", "-a",
                "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d", f"file://{PHONE_DIR}/{target}", timeout=20)
            count += 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    # 4. 兜底：整体再扫一次目录
    try:
        adb("shell", "cmd", "content", "call", "scan_volume", "--uri", "external_primary", timeout=20)
    except Exception:
        pass
    return count

# ---------- 直播流（scrcpy-server 内核） ----------
# 架构：单一「上游」常驻读取手机 H264 裸流，再广播给所有浏览器连接。
# 浏览器断线/重连不会重启 scrcpy-server，也不会互相顶掉，从根上消除画面周期黑闪。

class _StreamHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = []
        self._thread = None
        self._running = False
        self._upstream = None
        # NAL 缓存：新连接的浏览器需要先补 SPS/PPS + 当前 GOP，否则会一直黑屏等关键帧
        self._nal_buf = b""
        self._sps = b""
        self._pps = b""
        self._gop = []          # 从最近一个 IDR 起的完整 NAL 序列
        self._gop_bytes = 0

    # ---- 订阅管理 ----
    def subscribe(self):
        q = queue.Queue(maxsize=240)
        with self._lock:
            # 先把「SPS/PPS + 当前 GOP」塞进队列，保证新连接立刻有可解码关键帧
            if self._gop:
                try:
                    q.put_nowait(b"".join(self._gop))
                except queue.Full:
                    pass
            self._subs.append(q)
            if self._thread is None or not self._thread.is_alive():
                self._running = True
                self._thread = threading.Thread(target=self._run, daemon=True, name="stream-hub")
                self._thread.start()
        return q

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    # ---- NAL 缓存（供新订阅者补帧） ----
    _MAX_GOP_BYTES = 16 * 1024 * 1024

    def _cache_nals(self, chunk):
        """把裸流按起始码切成 NAL，缓存 SPS/PPS 与最近一个 GOP（IDR 起）"""
        data = self._nal_buf + chunk
        n = len(data)
        starts = []
        i = 0
        while i + 3 < n:
            if data[i:i + 3] == b"\x00\x00\x01":
                starts.append(i)
                i += 3
            elif data[i:i + 4] == b"\x00\x00\x00\x01":
                starts.append(i)
                i += 4
            else:
                i += 1
        if not starts:
            # 没有起始码，仅保留极短尾巴防止丢字节
            self._nal_buf = data[-4:] if n > 4 else data
            return
        # 最后一个 NAL 可能还没收完整，留到下次处理
        self._nal_buf = data[starts[-1]:]
        with self._lock:
            for k in range(len(starts) - 1):
                s, e = starts[k], starts[k + 1]
                nal = data[s:e]
                t = nal[3] & 0x1f if nal[:3] == b"\x00\x00\x01" else (nal[4] & 0x1f if len(nal) > 4 else 0)
                if t == 7:
                    self._sps = nal
                elif t == 8:
                    self._pps = nal
                if t == 5:
                    head = self._sps + self._pps + nal
                    self._gop = [head]
                    self._gop_bytes = len(head)
                elif self._gop:
                    if self._gop_bytes + len(nal) > self._MAX_GOP_BYTES:
                        self._gop = []      # 超限就放弃回放，等下一个关键帧
                        self._gop_bytes = 0
                    else:
                        self._gop.append(nal)
                        self._gop_bytes += len(nal)

    def _broadcast(self, data):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                # 丢掉最旧帧，保证实时性而不是无限堆积
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    # ---- 上游连接 ----
    def _reset_cache(self):
        with self._lock:
            self._nal_buf = b""
            self._sps = b""
            self._pps = b""
            self._gop = []
            self._gop_bytes = 0

    def _start_scrcpy(self):
        self._reset_cache()   # 新 server 的 SPS/PPS 可能不同，旧缓存作废
        exe = adb_path()
        if not exe:
            raise RuntimeError("未找到 adb")
        subprocess.run([exe, "shell", "pkill", "-f", "scrcpy.Server"], capture_output=True, timeout=10)
        time.sleep(0.3)  # 等旧 server 释放 @scrcpy
        subprocess.run([exe, "forward", "--remove", "tcp:27183"], capture_output=True, timeout=10)
        subprocess.run([exe, "forward", "tcp:27183", "localabstract:scrcpy"], capture_output=True, timeout=10)
        # nohup 驻留启动，server 脱离 adb 会话存活
        subprocess.run([exe, "shell",
            "CLASSPATH=/data/local/tmp/scrcpy-server.jar nohup app_process / "
            "com.genymobile.scrcpy.Server 2.7 scid=-1 log_level=error audio=false "
            "control=false max_size=1088 video_bit_rate=8000000 max_fps=30 "
            "tunnel_forward=true send_frame_meta=false send_device_meta=false "
            "send_codec_meta=false stay_awake=true cleanup=false > /data/local/tmp/scrcpy.log 2>&1 &"],
            capture_output=True, timeout=15)
        return exe

    def _connect_upstream(self, exe):
        import socket as _sock
        for i in range(80):
            with self._lock:
                if not self._running:
                    return None
            if i > 0 and i % 16 == 0:
                alive = subprocess.run([exe, "shell", "pgrep", "-f", "scrcpy.Server"],
                                       capture_output=True, timeout=10)
                if not alive.stdout.strip():
                    self._start_scrcpy()  # server 死了，自愈重拉
            try:
                c = _sock.create_connection(("127.0.0.1", 27183), timeout=2)
                c.settimeout(2)
                if c.recv(1):   # 占位字节，丢掉；之后就是纯 H264 裸流
                    c.settimeout(None)
                    return c
                c.close()
            except OSError:
                pass
            time.sleep(0.3)
        return None

    def _teardown_server(self):
        try:
            exe = adb_path()
            if exe:
                subprocess.run([exe, "shell", "pkill", "-f", "scrcpy.Server"], capture_output=True, timeout=10)
                subprocess.run([exe, "forward", "--remove", "tcp:27183"], capture_output=True, timeout=10)
        except Exception:
            pass

    def _run(self):
        """常驻线程：有观看者才拉起上游，无人观看就断开；浏览器连接本身不受影响。"""
        up = None
        try:
            while self._running:
                with self._lock:
                    has_client = bool(self._subs)
                if not has_client:
                    # 没人看：断开上游并停掉 server，省手机性能与电量
                    if up:
                        try:
                            up.close()
                        except Exception:
                            pass
                        up = None
                        with self._lock:
                            self._upstream = None
                        self._teardown_server()
                    time.sleep(0.5)
                    continue
                if up is None:
                    try:
                        exe = self._start_scrcpy()
                        up = self._connect_upstream(exe)
                    except Exception:
                        up = None
                    if up is None:
                        time.sleep(1.0)
                        continue
                    with self._lock:
                        self._upstream = up
                try:
                    chunk = up.recv(32768)
                except OSError:
                    chunk = b""
                if not chunk:
                    # 上游断了：丢弃旧连接，稍后自动重拉（浏览器看到的只是短暂停格）
                    try:
                        up.close()
                    except Exception:
                        pass
                    up = None
                    with self._lock:
                        self._upstream = None
                    self._reset_cache()
                    time.sleep(0.3)
                    continue
                try:
                    self._cache_nals(chunk)
                except Exception:
                    pass
                self._broadcast(chunk)
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
            if up:
                try:
                    up.close()
                except Exception:
                    pass
                with self._lock:
                    self._upstream = None

    def stop(self):
        with self._lock:
            self._running = False
            up = self._upstream
            self._upstream = None
        if up:
            try:
                up.close()
            except Exception:
                pass
        self._teardown_server()


_stream_hub = _StreamHub()

class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send({"ok": True})

    def stream_video(self):
        """把手机屏幕以 H264 流转发给浏览器。
        上游由 _StreamHub 常驻维护并广播，浏览器重连/多开都不会重启 server，避免周期黑闪。"""
        exe = adb_path()
        if not exe:
            self._send({"ok": False, "error": "未找到 adb"}, 500)
            return
        st = screen_state()
        if not st["screen_on"]:
            try:
                adb("shell", "input", "keyevent", "224", timeout=10)  # 唤醒
            except Exception:
                pass
            time.sleep(1)
            st = screen_state()
        if st["locked"]:
            self._send({"ok": False, "error": "手机处于锁屏状态，直播无法启动，请先在手机上解锁"}, 409)
            return
        # 确保 scrcpy-server 已推送到手机
        jar_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrcpy-server.jar")
        if os.path.exists(jar_local):
            r = adb("shell", "ls", "/data/local/tmp/scrcpy-server.jar", timeout=10)
            if b"No such file" in (r.stdout + r.stderr):
                adb("push", jar_local, "/data/local/tmp/scrcpy-server.jar", timeout=60)

        q = _stream_hub.subscribe()
        try:
            first = q.get(timeout=6)   # 等上游首帧，避免浏览器空等一个永远没数据的响应
        except queue.Empty:
            _stream_hub.unsubscribe(q)
            try:
                self._send({"ok": False, "error": "直播启动超时，请确认手机解锁并亮屏后点重试"}, 500)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                pass
            return

        self.send_response(200)
        self.send_header("Content-Type", "video/h264")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(first)
            self.wfile.flush()
            while True:
                try:
                    chunk = q.get(timeout=15)
                except queue.Empty:
                    continue
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        finally:
            _stream_hub.unsubscribe(q)

    def do_GET(self):
        if self.path.startswith("/api/stream"):
            self.stream_video()
            return
        if self.path.startswith("/api/size"):
            try:
                s = device_size()
                self._send({"ok": True, "w": s["w"], "h": s["h"]})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return
        if self.path.startswith("/api/state"):
            try:
                s = screen_state()
                s["ok"] = True
                self._send(s)
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return
        if self.path.startswith("/api/screen/dim"):
            try:
                s = dim_status()
                s["ok"] = True
                self._send(s)
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return
        if self.path.startswith("/api/screen"):
            try:
                w = 400
                q = 55
                wake = False
                if "?" in self.path:
                    for kv in self.path.split("?", 1)[1].split("&"):
                        if kv.startswith("w="):
                            w = max(200, min(1080, int(kv[2:])))
                        elif kv.startswith("q="):
                            q = max(30, min(90, int(kv[2:])))
                        elif kv.startswith("wake="):
                            wake = kv[5:] in ("1", "true")
                sc = screencap_jpeg(w, q, wake)
                body = sc["jpg"]
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Screen-W", str(sc["w"]))
                self.send_header("X-Screen-H", str(sc["h"]))
                self.send_header("X-Frame-Ts", "%.3f" % sc["ts"])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return
        if self.path.startswith("/api/device"):
            try:
                self._send(device_info())
            except Exception as e:
                self._send({"connected": False, "error": str(e)}, 500)
        else:
            self._send({"ok": True, "service": "kuailv-phone-bridge"})

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _need_device(self):
        info = device_info()
        if not info.get("connected"):
            self._send({"ok": False, "error": "手机未连接：" + info.get("reason", "")}, 400)
            return False
        return True

    def do_POST(self):
        if self.path.startswith("/api/device/restart"):
            try:
                self._send(restart_adb())
            except Exception as e:
                self._send({"connected": False, "state": "error", "reason": str(e), "hint": ""}, 500)
            return

        if self.path.startswith("/api/screen/dim"):
            if not self._need_device():
                return
            try:
                p = self._json_body()
                st = screen_dim(bool(p.get("on", True)))
                self._send({"ok": True, "dim": st["on"]})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return

        if self.path.startswith("/api/tap"):
            if not self._need_device():
                return
            try:
                p = self._json_body()
                tap(p["x"], p["y"])
                self._send({"ok": True})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return

        if self.path.startswith("/api/swipe"):
            if not self._need_device():
                return
            try:
                p = self._json_body()
                swipe(p["x1"], p["y1"], p["x2"], p["y2"], p.get("duration", 300))
                self._send({"ok": True})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return

        if self.path.startswith("/api/key"):
            if not self._need_device():
                return
            try:
                p = self._json_body()
                key(p["code"])
                self._send({"ok": True})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return

        if self.path.startswith("/api/text/clear"):
            if not self._need_device():
                return
            try:
                clear_text()
                self._send({"ok": True})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return

        if self.path.startswith("/api/text"):
            if not self._need_device():
                return
            try:
                p = self._json_body()
                n = input_text(p.get("text", ""))
                self._send({"ok": True, "count": n})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
            return

        if not self.path.startswith("/api/push"):
            self._send({"ok": False, "error": "unknown endpoint"}, 404)
            return
        try:
            info = device_info()
            if not info.get("connected"):
                self._send({"ok": False, "error": "手机未连接：" + info.get("reason", "")}, 400)
                return
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            count = push_images(payload.get("code", ""), payload.get("files", []))
            self._send({"ok": True, "count": count})
        except ValueError as e:
            self._send({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            self._send({"ok": False, "error": str(e)}, 500)

    def log_message(self, fmt, *args):
        print("[bridge]", fmt % args)

if __name__ == "__main__":
    exe = adb_path()
    print("=" * 56)
    print(" 快驴手机桥接服务已启动: http://127.0.0.1:%d" % PORT)
    print(" adb: %s" % (exe or "未找到！请先安装 platform-tools"))
    if exe:
        info = device_info()
        if info["connected"]:
            print(" 设备: %s (%s) 已连接 ✓" % (info["model"], info["serial"]))
            # 插着 USB 时保持屏幕常亮，避免自动上架过程中手机息屏导致画面/直播中断
            try:
                adb("shell", "svc", "power", "stayon", "usb", timeout=15)
                # 系统息屏超时调到最大，防止"隔一会黑一下"（HONOR 默认 30 秒）
                adb("shell", "settings", "put", "system", "screen_off_timeout", "2147483647", timeout=15)
                print(" 已设置：USB 连接时屏幕保持常亮")
            except Exception:
                pass
            # 上次异常退出（直接关掉服务）可能残留「暗屏」状态，启动时先恢复
            try:
                out = adb("shell", "ls", RESTORE_PATH, timeout=10).stdout.decode("utf-8", "ignore")
                if RESTORE_PATH in out:
                    adb("shell", "sh", RESTORE_PATH, timeout=15)
                    adb("shell", "rm", "-f", RESTORE_PATH, timeout=10)
                    print(" 已恢复上次遗留的暗屏设置")
            except Exception:
                pass
        else:
            print(" 设备: 未连接（插上手机并开启USB调试后自动识别）")
    print(" 请保持本窗口开着；网页端点「检测设备」验证")
    print("=" * 56)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
