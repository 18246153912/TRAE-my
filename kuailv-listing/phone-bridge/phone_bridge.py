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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 5175
PHONE_DIR = "/sdcard/Pictures/KLPush"

# adb 定位：优先 PATH，其次同目录 platform-tools，再次 D:\tools\platform-tools
ADB_CANDIDATES = [
    "adb",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform-tools", "adb.exe"),
    r"D:\tools\platform-tools\adb.exe",
]

def adb_path():
    for cand in ADB_CANDIDATES:
        try:
            r = subprocess.run([cand, "version"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return cand
        except Exception:
            continue
    return None

def adb(*args, timeout=30):
    exe = adb_path()
    if not exe:
        raise RuntimeError("未找到 adb，请下载 platform-tools 解压到 phone-bridge/platform-tools 或 D:\\tools\\platform-tools")
    return subprocess.run([exe, *args], capture_output=True, timeout=timeout)

def device_info():
    try:
        r = adb("devices", timeout=10)
        out = r.stdout.decode("utf-8", "ignore")
        for line in out.splitlines()[1:]:
            line = line.strip()
            if line.endswith("\tdevice"):
                serial = line.split("\t")[0]
                model = serial
                try:
                    m = adb("-s", serial, "shell", "getprop", "ro.product.model", timeout=10)
                    model = m.stdout.decode("utf-8", "ignore").strip() or serial
                except Exception:
                    pass
                return {"connected": True, "serial": serial, "model": model}
        return {"connected": False, "reason": out.strip() or "无设备"}
    except RuntimeError as e:
        return {"connected": False, "reason": str(e)}
    except Exception as e:
        return {"connected": False, "reason": str(e)}

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

    def do_GET(self):
        if self.path.startswith("/api/device"):
            try:
                self._send(device_info())
            except Exception as e:
                self._send({"connected": False, "error": str(e)}, 500)
        else:
            self._send({"ok": True, "service": "kuailv-phone-bridge"})

    def do_POST(self):
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
        else:
            print(" 设备: 未连接（插上手机并开启USB调试后自动识别）")
    print(" 请保持本窗口开着；网页端点「检测设备」验证")
    print("=" * 56)
    try:
        HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
