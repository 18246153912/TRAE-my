#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快驴餐厨上架系统 · 新客户开通工具

双击运行：输入 服务器IP / SSH密码 / 域名 → 连接后自动判断：
  · 服务器未部署过 → 全新安装：
  0. 环境预检（发行版 / Python 版本 / 端口占用 / 磁盘内存 / 重复部署检测），不合格直接中止
  1. 安装系统环境（python3-venv / nginx / certbot，自动适配 apt / dnf / yum）
  2. 上传后端与页面文件
  3. 创建虚拟环境、安装 flask + gunicorn
  4. 生成该客户专属运营授权码（kl_xxxx）
  5. 配置 systemd 后台服务 + nginx 反向代理（可选 HTTPS 证书）
  6. 冒烟验证，输出交付信息（可一键复制 / 保存 txt）
  · 服务器已部署过 → 自动进入升级模式（打补丁）：
  只上传更新文件并重启服务，商品数据 / 图片 / 厂家账号 / 运营授权码全部不动，
  完成后展示客户当前数据概况（已推送链接数量 / 待审核数量 / 厂家数量）。
  想给哪台服务器打补丁，就连接哪台。

依赖：pip install paramiko
"""

import datetime
import os
import queue
import secrets
import shlex
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def read_local_version():
    """从本机 server/app.py 读取当前代码版本号"""
    import re
    try:
        with open(os.path.join(BASE_DIR, 'server', 'app.py'), encoding='utf-8') as f:
            m = re.search(r"APP_VERSION\s*=\s*'([^']+)'", f.read())
            return m.group(1) if m else '未知'
    except OSError:
        return '未知'


CODE_VERSION = read_local_version()
LOCAL_FILES = [
    ('server/app.py', 'server/app.py'),
    ('prototype/index.html', 'prototype/index.html'),
    ('prototype/vendor.html', 'prototype/vendor.html'),
]
# 前端框架本地化（不依赖外部 CDN，客户内网也能正常显示）
LIB_DIR = os.path.join('prototype', 'lib')
PIP_MIRROR = 'https://pypi.tuna.tsinghua.edu.cn/simple'

UNIT_TMPL = """[Unit]
Description=Kuailv Listing System
After=network-online.target
Wants=network-online.target

[Service]
User={user}
WorkingDirectory={app_dir}/server
Environment=KL_ADMIN_TOKEN={token}
ExecStart={app_dir}/venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

NGX_TMPL = """server {{
    listen 80;
    server_name {server_name};
    client_max_body_size 20m;

    location / {{
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""


def deploy(cfg, log):
    """执行完整部署，返回交付信息文本。异常向上抛。"""
    import paramiko  # 延迟导入，界面先起来

    host = cfg['host']
    port = cfg['port']
    user = cfg['user']
    password = cfg['password']
    domain = cfg['domain']
    want_https = cfg['https'] and bool(domain)
    if cfg['https'] and not domain:
        log('未填写域名，无法申请证书，按 HTTP 部署')

    server_name = domain or host
    scheme = 'https' if want_https else 'http'

    log('连接 {0}@{1}:{2} ...'.format(user, host, port))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=port, username=user, password=password,
                timeout=20, banner_timeout=20, auth_timeout=20)
    log('SSH 连接成功')
    log('本机代码版本: v' + CODE_VERSION)

    def run(cmd, timeout=300):
        _, out, err = ssh.exec_command(cmd, timeout=timeout)
        code = out.channel.recv_exit_status()
        return code, out.read().decode('utf-8', 'replace'), err.read().decode('utf-8', 'replace')

    def sudo(cmd, timeout=600, check=True):
        stdin, out, err = ssh.exec_command('sudo -S bash -c ' + shlex.quote(cmd), timeout=timeout)
        stdin.write(password + '\n')
        stdin.flush()
        stdin.channel.shutdown_write()
        code = out.channel.recv_exit_status()
        o = out.read().decode('utf-8', 'replace')
        e = err.read().decode('utf-8', 'replace')
        if check and code != 0:
            raise RuntimeError('命令失败(退出码 {0}): {1}\n{2}{3}'.format(code, cmd, o, e))
        return code, o, e

    def write_remote(path, content):
        tmp = home + '/.kuailv_tmp_' + secrets.token_hex(4)
        sftp = ssh.open_sftp()
        with sftp.open(tmp, 'w') as f:
            f.write(content)
        sftp.close()
        sudo('mv {0} {1} && chown root:root {1}'.format(tmp, shlex.quote(path)))

    try:
        _, home, _ = run('echo $HOME')
        home = home.strip() or ('/root' if user == 'root' else '/home/' + user)
        app_dir = home + '/kuailv'
        log('部署目录: ' + app_dir)

        # ---------- 环境预检：不合格直接中止，避免装一半留烂摊子 ----------
        log('环境预检 ...')
        _, os_id, _ = run('. /etc/os-release 2>/dev/null; echo $ID')
        os_id = os_id.strip().lower() or '未知'
        _, pm, _ = run('command -v apt-get || command -v dnf || command -v yum')
        pm = pm.strip().split('/')[-1]
        if pm not in ('apt-get', 'dnf', 'yum'):
            raise RuntimeError('无法识别的系统（{0}）：未找到 apt-get / dnf / yum，'
                               '目前仅支持 Ubuntu / Debian / CentOS / Rocky / Alma 等主流发行版'.format(os_id))
        log('  ✓ 系统发行版: {0}（包管理器 {1}）'.format(os_id, pm))

        _, pyv, _ = run('command -v python3 >/dev/null 2>&1'
                        ' && python3 -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])"'
                        ' || echo 0')
        pyv = (pyv.strip().splitlines() or ['0'])[-1]
        if not pyv.isdigit():
            pyv = '0'
        if pyv == '0':
            log('  ✓ Python3: 未安装，稍后由包管理器自动安装')
        elif int(pyv) < 308:
            raise RuntimeError('服务器自带 Python 版本过低（3.{0}），本系统要求 ≥ 3.8，'
                               '请更换更高版本的系统镜像（如 Ubuntu 22.04）'.format(int(pyv) % 100))
        else:
            log('  ✓ Python3: 3.{0}'.format(int(pyv) % 100))

        _, ports_out, _ = run("ss -ltn 2>/dev/null | awk 'NR>1{print $4}' | grep -oE '[0-9]+$' | sort -un")
        busy = set(p for p in ports_out.split() if p.isdigit())
        _, kuailv_st, _ = run('systemctl is-active kuailv 2>/dev/null')
        _, nginx_st, _ = run('systemctl is-active nginx 2>/dev/null')
        if '8000' in busy and kuailv_st.strip() != 'active':
            raise RuntimeError('端口 8000 已被其他程序占用，无法部署；请释放该端口或更换服务器')
        if '80' in busy and nginx_st.strip() != 'active':
            raise RuntimeError('端口 80 已被其他程序占用，nginx 无法启动；请先停止占用该端口的程序')
        log('  ✓ 端口 80 / 8000: 可用')

        _, disk, _ = run("df -m / | awk 'NR==2{print $4}'")
        if disk.strip().isdigit():
            if int(disk.strip()) < 500:
                raise RuntimeError('磁盘剩余空间不足（{0} MB），至少需要 500 MB'.format(disk.strip()))
            log('  ✓ 磁盘剩余: {0} MB'.format(disk.strip()))
        _, mem, _ = run("free -m 2>/dev/null | awk '/^Mem:/{print $2}'")
        if mem.strip().isdigit():
            log('  ✓ 内存: {0} MB'.format(mem.strip()))

        _, prev, _ = run('test -f /etc/systemd/system/kuailv.service && echo yes || echo no')
        if 'yes' in prev:
            # ===== 已部署过：自动进入升级模式（只传文件 + 重启，数据/授权码不动） =====
            import re as _re
            _, rv, _ = run('grep APP_VERSION {0}/server/app.py | head -1'.format(app_dir))
            m = _re.search(r"'([^']+)'", rv)
            remote_ver = m.group(1) if m else ''
            if remote_ver == CODE_VERSION:
                log('  ! 检测到本机已部署本系统，且版本相同（v{0}）→ 进入【升级模式】重刷一遍文件'.format(remote_ver))
            elif remote_ver:
                log('  ! 检测到本机已部署本系统（v{0}）→ 进入【升级模式】，将升级到 v{1}'.format(remote_ver, CODE_VERSION))
                if remote_ver > CODE_VERSION:
                    log('  ⚠ 警告：服务器版本比本机代码还新！请确认本机代码是最新版，避免把客户系统回滚')
            else:
                log('  ! 检测到本机已部署本系统（旧版无版本号）→ 进入【升级模式】，升级到 v' + CODE_VERSION)
            log('[1/3] 上传更新文件 ...')
            sftp = ssh.open_sftp()
            upload_files = list(LOCAL_FILES)
            lib_abs = os.path.join(BASE_DIR, LIB_DIR)
            if os.path.isdir(lib_abs):
                for fn in sorted(os.listdir(lib_abs)):
                    upload_files.append((LIB_DIR.replace(os.sep, '/') + '/' + fn, 'prototype/lib/' + fn))
            for local_rel, remote_rel in upload_files:
                local_path = os.path.join(BASE_DIR, local_rel.replace('/', os.sep))
                sftp.put(local_path, app_dir + '/' + remote_rel)
                log('  已更新 ' + remote_rel)
            sftp.close()

            log('[2/3] 重启服务 ...')
            sudo('systemctl restart kuailv')
            time.sleep(2)
            _, o, _ = run('curl -s -o /dev/null -w "%{{http_code}}" http://127.0.0.1:8000/api/health')
            if o.strip() != '200':
                raise RuntimeError('重启后健康检查未通过，返回: ' + o.strip())
            log('  服务已重载，运行正常')

            log('[3/3] 读取客户数据概况 ...')
            stat_script = (
                'import sqlite3, json\n'
                'c = sqlite3.connect({0!r})\n'
                'rows = c.execute("SELECT status, data FROM items").fetchall()\n'
                'links = 0\n'
                'for s, d in rows:\n'
                '    try: links += len(json.loads(d).get("listings") or [])\n'
                '    except Exception: pass\n'
                'pend = sum(1 for s, d in rows if s == "pending")\n'
                'sup = c.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]\n'
                'print(links, pend, sup)\n'
            ).format(app_dir + '/server/data.db')
            code, o, e = run('{0}/venv/bin/python -c {1}'.format(app_dir, shlex.quote(stat_script)))
            parts = o.split()
            if code == 0 and len(parts) >= 3:
                links_n, pend_n, sup_n = parts[0], parts[1], parts[2]
            else:
                links_n = pend_n = sup_n = '读取失败'
            _, tok, _ = run("grep -oE 'KL_ADMIN_TOKEN=[^ ]+' /etc/systemd/system/kuailv.service | head -1 | cut -d= -f2")
            old_token = tok.strip() or '（可在服务器 /etc/systemd/system/kuailv.service 中查看）'
            _, sch, _ = run("grep -q 'listen 443' /etc/nginx/sites-available/kuailv 2>/dev/null && echo https || echo http")
            upg_base = '{0}://{1}'.format(sch.strip() or 'http', server_name)

            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            return '\n'.join([
                '========== 快驴餐厨上架系统 · 升级补丁已完成 ==========',
                '升级时间：' + ts,
                '服务器：' + host,
                '系统版本：v' + CODE_VERSION + ('（升级前 v' + remote_ver + '）' if remote_ver else ''),
                '',
                '本次为补丁升级：仅更新程序与页面文件并重启服务。',
                '商品数据、已传图片、厂家账号、运营授权码均保持不变。',
                '',
                '当前数据概况：',
                '    已推送链接数量：' + links_n,
                '    待审核数量：' + pend_n,
                '    厂家数量：' + sup_n,
                '',
                '运营端地址：' + upg_base + '/',
                '运营授权码（与升级前一致）：' + old_token,
                '厂家录入链接：' + upg_base + '/vendor/',
            ])
        # ===== 未部署过：全新安装 =====

        log('[1/6] 安装系统依赖（nginx / python3' + (' / certbot' if want_https else '') + '）...')
        if pm == 'apt-get':
            pkgs = 'python3-venv python3-pip nginx curl'
            if want_https:
                pkgs += ' certbot python3-certbot-nginx'
            sudo('export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y ' + pkgs,
                 timeout=900)
        else:
            sudo(pm + ' install -y python3 python3-pip nginx curl', timeout=900)
            if want_https:
                code, o, e = sudo(pm + ' install -y certbot python3-certbot-nginx', timeout=600, check=False)
                if code != 0:
                    log('  证书工具安装失败（可能需先启用 EPEL 源），本次按 HTTP 交付')
                    want_https = False
                    scheme = 'http'

        log('[2/6] 上传系统文件 ...')
        run('mkdir -p {0}/server {0}/prototype/lib {0}/server/uploads'.format(app_dir))
        sftp = ssh.open_sftp()
        upload_files = list(LOCAL_FILES)
        lib_abs = os.path.join(BASE_DIR, LIB_DIR)
        if os.path.isdir(lib_abs):
            for fn in sorted(os.listdir(lib_abs)):
                upload_files.append((LIB_DIR.replace(os.sep, '/') + '/' + fn, 'prototype/lib/' + fn))
        for local_rel, remote_rel in upload_files:
            local_path = os.path.join(BASE_DIR, local_rel.replace('/', os.sep))
            sftp.put(local_path, app_dir + '/' + remote_rel)
            log('  已上传 ' + remote_rel)
        sftp.close()

        log('[3/6] 创建 Python 虚拟环境并安装依赖 ...')
        _, o, _ = run('test -x {0}/venv/bin/python && echo yes || echo no'.format(app_dir))
        if 'yes' not in o:
            code, o, e = run('python3 -m venv {0}/venv'.format(app_dir), timeout=300)
            if code != 0:
                raise RuntimeError('创建虚拟环境失败:\n' + o + e)
        code, o, e = run('{0}/venv/bin/pip install -q -i {1} flask gunicorn'.format(app_dir, PIP_MIRROR),
                         timeout=900)
        if code != 0:
            raise RuntimeError('pip 安装失败:\n' + o + e)

        token = 'kl_' + secrets.token_hex(24)
        log('[4/6] 已生成该客户专属运营授权码: ' + token)

        log('[5/6] 配置后台服务（systemd）+ Nginx 反向代理 ...')
        write_remote('/etc/systemd/system/kuailv.service',
                     UNIT_TMPL.format(user=user, app_dir=app_dir, token=token))
        sudo('systemctl daemon-reload && systemctl enable kuailv && systemctl restart kuailv')

        write_remote('/etc/nginx/sites-available/kuailv', NGX_TMPL.format(server_name=server_name))
        sudo('ln -sf /etc/nginx/sites-available/kuailv /etc/nginx/sites-enabled/kuailv'
             ' && rm -f /etc/nginx/sites-enabled/default && nginx -t && systemctl reload nginx')

        if want_https:
            log('[6/6] 申请 HTTPS 证书（certbot）...')
            code, o, e = sudo('certbot --nginx --non-interactive --agree-tos'
                              ' --register-unsafely-without-email --redirect -d ' + shlex.quote(domain),
                              timeout=600, check=False)
            if code != 0:
                log('  证书申请失败（请确认域名已解析到本服务器、安全组放行 80/443），已回退为 HTTP')
                tail = (o + e).strip().splitlines()
                if tail:
                    log('  certbot: ' + tail[-1][:200])
                scheme = 'http'
            else:
                log('  HTTPS 证书已生效')
        else:
            log('[6/6] 未启用 HTTPS，按 HTTP 交付')

        log('冒烟验证 ...')
        # gunicorn 重启后需要数秒就绪，重试最多 30 秒
        ok_health = False
        for _ in range(10):
            time.sleep(3)
            _, o, _ = run('curl -s -o /dev/null -w "%{{http_code}}" http://127.0.0.1:8000/api/health')
            if o.strip() == '200':
                ok_health = True
                break
        if not ok_health:
            raise RuntimeError('后端服务未正常启动，健康检查返回: ' + o.strip())
        port_no = 443 if scheme == 'https' else 80
        base = '{0}://{1}'.format(scheme, server_name)
        _, o, _ = run('curl -sk -o /dev/null -w "%{{http_code}}" -H "X-KL-Token: {0}"'
                      ' --resolve {1}:{2}:127.0.0.1 {3}/api/bootstrap'.format(token, server_name, port_no, base))
        if o.strip() != '200':
            raise RuntimeError('经 Nginx 访问异常，返回: ' + o.strip())
        log('服务运行正常')

        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines = [
            '========== 快驴餐厨上架系统 · 客户交付信息 ==========',
            '部署时间：' + ts,
            '服务器：' + host,
            '系统版本：v' + CODE_VERSION,
            '',
            '运营端地址（桌面软件首次打开时填写的“服务器地址”）：',
            '    ' + base + '/',
            '运营授权码（桌面软件里填写的“运营授权码”）：',
            '    ' + token,
            '厂家录入链接（发给厂家，手机/电脑浏览器直接打开）：',
            '    ' + base + '/vendor/',
            '演示厂家账号：jinwuyuan / 123456 ，kanglida / 123456',
            '',
            '注意事项：',
            '1. 云服务器安全组/防火墙需放行端口 80' + (' 和 443' if scheme == 'https' else '（如后续启用 HTTPS 再放开 443）'),
            '2. 运营授权码等同后台钥匙，请妥善保管，泄露后需重新部署更换',
            '3. 后续系统更新时，再次运行本工具连接这台服务器即可：自动进入升级模式，只更新文件，数据与授权码不变',
        ]
        if not domain:
            lines.append('4. 当前为纯 IP HTTP 部署，桌面软件里服务器地址请完整填写 http://' + host + '/')
        return '\n'.join(lines)
    finally:
        ssh.close()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('快驴餐厨上架系统 · 新客户开通工具（本机代码 v{0}）'.format(CODE_VERSION))
        self.geometry('720x780')
        self.minsize(640, 700)
        self.q = queue.Queue()

        # ===== 输入区 =====
        frm = ttk.LabelFrame(self, text='服务器信息', padding=10)
        frm.pack(fill='x', padx=12, pady=(12, 6))
        frm.columnconfigure(1, weight=1)
        frm.columnconfigure(3, weight=1)

        self.var_host = tk.StringVar()
        self.var_port = tk.StringVar(value='22')
        self.var_user = tk.StringVar(value='ubuntu')
        self.var_pwd = tk.StringVar()
        self.var_domain = tk.StringVar()
        self.var_https = tk.BooleanVar(value=True)

        ttk.Label(frm, text='服务器 IP *').grid(row=0, column=0, sticky='e', padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.var_host).grid(row=0, column=1, sticky='we', padx=4, pady=4)
        ttk.Label(frm, text='SSH 端口').grid(row=0, column=2, sticky='e', padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.var_port, width=8).grid(row=0, column=3, sticky='w', padx=4, pady=4)

        ttk.Label(frm, text='SSH 用户名').grid(row=1, column=0, sticky='e', padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.var_user).grid(row=1, column=1, sticky='we', padx=4, pady=4)
        ttk.Label(frm, text='SSH 密码 *').grid(row=1, column=2, sticky='e', padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.var_pwd, show='*').grid(row=1, column=3, sticky='we', padx=4, pady=4)

        ttk.Label(frm, text='域名').grid(row=2, column=0, sticky='e', padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.var_domain).grid(row=2, column=1, sticky='we', padx=4, pady=4)
        ttk.Checkbutton(frm, text='申请 HTTPS 证书（域名需已解析到服务器）',
                        variable=self.var_https).grid(row=2, column=2, columnspan=2, sticky='w', padx=4)

        self.btn = ttk.Button(frm, text='开始部署', command=self.start)
        self.btn.grid(row=3, column=0, columnspan=4, pady=(8, 2))

        # ===== 日志区 =====
        frm_log = ttk.LabelFrame(self, text='部署日志', padding=6)
        frm_log.pack(fill='both', expand=True, padx=12, pady=6)
        self.txt_log = scrolledtext.ScrolledText(frm_log, height=16, state='disabled',
                                                 font=('Consolas', 9))
        self.txt_log.pack(fill='both', expand=True)

        # ===== 交付信息区 =====
        frm_out = ttk.LabelFrame(self, text='交付信息（部署完成后生成）', padding=6)
        frm_out.pack(fill='both', padx=12, pady=(6, 12))
        self.txt_out = scrolledtext.ScrolledText(frm_out, height=11, font=('Microsoft YaHei UI', 9))
        self.txt_out.pack(fill='both', expand=True)
        bar = ttk.Frame(frm_out)
        bar.pack(fill='x', pady=(6, 0))
        self.btn_copy = ttk.Button(bar, text='复制交付信息', command=self.copy_out, state='disabled')
        self.btn_copy.pack(side='left')
        self.btn_save = ttk.Button(bar, text='保存为文件…', command=self.save_out, state='disabled')
        self.btn_save.pack(side='left', padx=8)

        missing = [r for r, _ in LOCAL_FILES
                   if not os.path.isfile(os.path.join(BASE_DIR, r.replace('/', os.sep)))]
        if missing:
            messagebox.showerror('缺少文件', '以下系统文件未找到，无法部署：\n' + '\n'.join(missing)
                                 + '\n\n请确认本工具放在 kuailv-listing 项目目录内。')
            self.btn.config(state='disabled')

        self.after(100, self._poll)

    # ---- 日志线程安全追加 ----
    def log(self, msg):
        self.q.put(('log', msg))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'log':
                    self.txt_log.config(state='normal')
                    self.txt_log.insert('end', payload + '\n')
                    self.txt_log.see('end')
                    self.txt_log.config(state='disabled')
                elif kind == 'done':
                    self.txt_out.delete('1.0', 'end')
                    self.txt_out.insert('end', payload)
                    self.btn_copy.config(state='normal')
                    self.btn_save.config(state='normal')
                    self.btn.config(state='normal', text='开始部署')
                    self.log('===== 部署完成 =====')
                    messagebox.showinfo('完成', '执行完成，交付信息已生成。')
                elif kind == 'fail':
                    self.btn.config(state='normal', text='开始部署')
                    self.log('===== 部署失败 =====')
                    self.log(payload)
                    messagebox.showerror('部署失败', payload.splitlines()[-1][:300] if payload else '未知错误')
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---- 部署流程 ----
    def start(self):
        host = self.var_host.get().strip()
        password = self.var_pwd.get()
        if not host or not password:
            messagebox.showwarning('缺少信息', '请填写服务器 IP 和 SSH 密码')
            return
        try:
            port = int(self.var_port.get().strip() or '22')
        except ValueError:
            messagebox.showwarning('端口错误', 'SSH 端口必须是数字')
            return
        domain = self.var_domain.get().strip().lower()
        domain = domain.replace('https://', '').replace('http://', '').strip('/')

        cfg = {
            'host': host,
            'port': port,
            'user': self.var_user.get().strip() or 'ubuntu',
            'password': password,
            'domain': domain,
            'https': self.var_https.get(),
        }
        self._domain_for_save = domain or host
        self.btn.config(state='disabled', text='部署中…')
        self.txt_out.delete('1.0', 'end')
        self.btn_copy.config(state='disabled')
        self.btn_save.config(state='disabled')
        self.txt_log.config(state='normal')
        self.txt_log.delete('1.0', 'end')
        self.txt_log.config(state='disabled')
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _worker(self, cfg):
        try:
            delivery = deploy(cfg, self.log)
            self.q.put(('done', delivery))
        except Exception:
            self.q.put(('fail', traceback.format_exc()))

    # ---- 交付信息操作 ----
    def copy_out(self):
        text = self.txt_out.get('1.0', 'end').strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            messagebox.showinfo('已复制', '交付信息已复制到剪贴板')

    def save_out(self):
        text = self.txt_out.get('1.0', 'end').strip()
        if not text:
            return
        path = filedialog.asksaveasfilename(
            defaultextension='.txt',
            initialfile='交付信息-{0}.txt'.format(self._domain_for_save),
            filetypes=[('文本文件', '*.txt')],
        )
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text + '\n')
            messagebox.showinfo('已保存', '已保存到:\n' + path)


if __name__ == '__main__':
    try:
        import paramiko  # noqa: F401
    except ImportError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('缺少依赖', '未安装 paramiko，请先执行:\n\n    pip install paramiko')
        raise SystemExit(1)
    App().mainloop()
