#!/usr/bin/env python3
"""快驴餐盒上架系统 · 后端服务

职责：
- 厂家账号（一厂一账号）落库
- 商品资料 / 审核结果 / 上架记录 落库，多端（手机、电脑、平板）看到同一份数据
- 图片上传存储，前端只保存图片 URL
- 页面托管：运营端 index.html 仅桌面软件（自动携带 X-KL-Token 头）可访问，
  浏览器直接打开一律跳转到厂家录入页 /vendor

安全模型：
- 运营端接口要求请求头 X-KL-Token = 环境变量 KL_ADMIN_TOKEN（只内置在桌面软件里）
- 厂家登录后下发 X-Vendor-Token（vendor_sessions 表，多 worker 共享），
  厂家只能提交商品、查看本厂商品
- 本机直连（127.0.0.1，本地开发）免 token
"""

import datetime
import json
import os
import sqlite3
import uuid

from flask import Flask, jsonify, redirect, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data.db')
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
PROTO_DIR = os.path.abspath(os.path.join(BASE_DIR, '..', 'prototype'))

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'webp'}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# 系统版本号：每次发版（改代码交付客户）时递增，部署/升级工具据此判断是否发错版本
APP_VERSION = '3.0'

# 运营端访问令牌：桌面软件在请求头自动携带；服务器通过环境变量 KL_ADMIN_TOKEN 配置
ADMIN_TOKEN = os.environ.get('KL_ADMIN_TOKEN', '')
VENDOR_SESSION_DAYS = 30

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES

# 演示账号（首次建库时写入；运营可在库里直接增改）
SEED_SUPPLIERS = [
    ('s1', 'jinwuyuan', '123456', '金五缘塑料制品有限公司', '张经理', '138****8888'),
    ('s2', 'kanglida', '123456', '康利达新材料科技', '李总', '139****6666'),
]


def now_iso():
    return datetime.datetime.now().isoformat(timespec='seconds')


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS suppliers (
                id          TEXT PRIMARY KEY,
                account     TEXT NOT NULL UNIQUE,
                password    TEXT NOT NULL,
                name        TEXT NOT NULL,
                contact     TEXT DEFAULT '',
                phone       TEXT DEFAULT '',
                remark      TEXT DEFAULT '',
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
                id          TEXT PRIMARY KEY,
                supplier_id TEXT,
                status      TEXT NOT NULL,
                spu_name    TEXT DEFAULT '',
                brand       TEXT DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                data        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at DESC);
            CREATE TABLE IF NOT EXISTS vendor_sessions (
                token       TEXT PRIMARY KEY,
                supplier_id TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            """
        )
        if conn.execute('SELECT COUNT(*) c FROM suppliers').fetchone()['c'] == 0:
            ts = now_iso()
            for sid, account, pwd, name, contact, phone in SEED_SUPPLIERS:
                conn.execute(
                    'INSERT INTO suppliers (id, account, password, name, contact, phone, remark, created_at)'
                    ' VALUES (?,?,?,?,?,?,?,?)',
                    (sid, account, generate_password_hash(pwd), name, contact, phone, '', ts),
                )
        # 老库补 remark 列
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(suppliers)')}
        if 'remark' not in cols:
            conn.execute("ALTER TABLE suppliers ADD COLUMN remark TEXT DEFAULT ''")


def row_to_supplier(r):
    return {
        'id': r['id'],
        'account': r['account'],
        'name': r['name'],
        'contact': r['contact'],
        'phone': r['phone'],
        'remark': r['remark'] or '',
    }


def public_url(path):
    """拼接可跨端访问的绝对地址；Nginx 反代下由 ProxyFix 还原 https 与域名"""
    return request.host_url.rstrip('/') + path


# ==================== 鉴权 ====================

def client_ip():
    # nginx 反代会用 $remote_addr 覆盖客户端伪造的 X-Real-IP，可信；
    # 本地直连 flask 开发时没有该头，退回 remote_addr
    return request.headers.get('X-Real-IP') or request.remote_addr or ''


def is_local():
    return client_ip() in ('127.0.0.1', '::1')


def is_admin():
    if is_local():
        return True
    return bool(ADMIN_TOKEN) and request.headers.get('X-KL-Token', '') == ADMIN_TOKEN


def forbid():
    return jsonify(ok=False, error='无访问权限'), 403


def operator_name():
    """审核人标识：桌面软件注入 X-KL-Operator 头（首次启动时填写，存本机）"""
    return (request.headers.get('X-KL-Operator') or '').strip()[:32] or '未署名'


def current_vendor():
    tok = request.headers.get('X-Vendor-Token', '').strip()
    if not tok:
        return None
    with connect() as conn:
        return conn.execute(
            'SELECT s.* FROM vendor_sessions vs JOIN suppliers s ON s.id = vs.supplier_id'
            ' WHERE vs.token = ?', (tok,),
        ).fetchone()


@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-KL-Token, X-Vendor-Token, X-KL-Operator'
    resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,PATCH,DELETE,OPTIONS'
    return resp


@app.get('/api/health')
def health():
    return jsonify(ok=True, time=now_iso())


@app.post('/api/login')
def login():
    body = request.get_json(silent=True) or {}
    account = (body.get('account') or '').strip()
    password = (body.get('password') or '').strip()
    if not account or not password:
        return jsonify(ok=False, error='请输入账号和密码'), 400
    with connect() as conn:
        r = conn.execute(
            'SELECT * FROM suppliers WHERE lower(account) = lower(?)', (account,)
        ).fetchone()
    if not r:
        return jsonify(ok=False, error='账号或密码错误'), 401
    stored = r['password'] or ''
    # 兼容历史明文密码，登录成功后自动升级为哈希
    if stored.startswith(('pbkdf2:', 'scrypt:')):
        ok = check_password_hash(stored, password)
    else:
        ok = stored == password
        if ok:
            with connect() as conn:
                conn.execute(
                    'UPDATE suppliers SET password = ? WHERE id = ?',
                    (generate_password_hash(password), r['id']),
                )
    if not ok:
        return jsonify(ok=False, error='账号或密码错误'), 401
    # 签发厂家会话 token（落库，gunicorn 多 worker 共享）
    token = uuid.uuid4().hex + uuid.uuid4().hex
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=VENDOR_SESSION_DAYS)).isoformat(timespec='seconds')
    with connect() as conn:
        conn.execute('DELETE FROM vendor_sessions WHERE created_at < ?', (cutoff,))
        conn.execute(
            'INSERT INTO vendor_sessions (token, supplier_id, created_at) VALUES (?,?,?)',
            (token, r['id'], now_iso()),
        )
    return jsonify(ok=True, supplier=row_to_supplier(r), token=token)


@app.get('/api/my/items')
def my_items():
    """厂家端：查看本厂提交过的全部商品"""
    v = current_vendor()
    if not v:
        return jsonify(ok=False, error='登录已失效，请重新登录'), 401
    with connect() as conn:
        rows = conn.execute(
            'SELECT data FROM items WHERE supplier_id = ? ORDER BY updated_at DESC', (v['id'],)
        ).fetchall()
    items = []
    for r in rows:
        try:
            items.append(json.loads(r['data']))
        except (TypeError, ValueError):
            continue
    return jsonify(ok=True, supplier=row_to_supplier(v), items=items)


# ==================== 厂家账号管理（仅运营端） ====================

def supplier_taken(conn, account, exclude_id=''):
    r = conn.execute(
        'SELECT id FROM suppliers WHERE lower(account) = lower(?) AND id <> ?',
        (account, exclude_id),
    ).fetchone()
    return r is not None


@app.get('/api/suppliers')
def list_suppliers():
    if not is_admin():
        return forbid()
    with connect() as conn:
        rows = conn.execute('SELECT * FROM suppliers ORDER BY created_at, id').fetchall()
    return jsonify(ok=True, suppliers=[row_to_supplier(r) for r in rows])


@app.post('/api/suppliers')
def create_supplier():
    if not is_admin():
        return forbid()
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    account = (body.get('account') or '').strip()
    password = (body.get('password') or '').strip()
    remark = (body.get('remark') or '').strip()
    contact = (body.get('contact') or '').strip()
    phone = (body.get('phone') or '').strip()
    if not name or not account or not password:
        return jsonify(ok=False, error='厂家名称、账号、密码必填'), 400
    if len(password) < 4:
        return jsonify(ok=False, error='密码至少 4 位'), 400
    with connect() as conn:
        if supplier_taken(conn, account):
            return jsonify(ok=False, error='该账号已被占用'), 409
        sid = 's' + uuid.uuid4().hex[:8]
        ts = now_iso()
        conn.execute(
            'INSERT INTO suppliers (id, account, password, name, contact, phone, remark, created_at)'
            ' VALUES (?,?,?,?,?,?,?,?)',
            (sid, account, generate_password_hash(password), name, contact, phone, remark, ts),
        )
        r = conn.execute('SELECT * FROM suppliers WHERE id = ?', (sid,)).fetchone()
    return jsonify(ok=True, supplier=row_to_supplier(r))


@app.patch('/api/suppliers/<sid>')
def update_supplier(sid):
    if not is_admin():
        return forbid()
    body = request.get_json(silent=True) or {}
    with connect() as conn:
        r = conn.execute('SELECT * FROM suppliers WHERE id = ?', (sid,)).fetchone()
        if not r:
            return jsonify(ok=False, error='厂家不存在'), 404
        name = (body.get('name') or r['name']).strip()
        account = (body.get('account') or r['account']).strip()
        remark = body['remark'].strip() if isinstance(body.get('remark'), str) else (r['remark'] or '')
        contact = body['contact'].strip() if isinstance(body.get('contact'), str) else (r['contact'] or '')
        phone = body['phone'].strip() if isinstance(body.get('phone'), str) else (r['phone'] or '')
        if not name or not account:
            return jsonify(ok=False, error='厂家名称、账号不能为空'), 400
        if supplier_taken(conn, account, exclude_id=sid):
            return jsonify(ok=False, error='该账号已被占用'), 409
        password = (body.get('password') or '').strip()
        if password:
            if len(password) < 4:
                return jsonify(ok=False, error='密码至少 4 位'), 400
            conn.execute('UPDATE suppliers SET name=?, account=?, contact=?, phone=?, remark=?, password=? WHERE id=?',
                         (name, account, contact, phone, remark, generate_password_hash(password), sid))
        else:
            conn.execute('UPDATE suppliers SET name=?, account=?, contact=?, phone=?, remark=? WHERE id=?',
                         (name, account, contact, phone, remark, sid))
        r = conn.execute('SELECT * FROM suppliers WHERE id = ?', (sid,)).fetchone()
    return jsonify(ok=True, supplier=row_to_supplier(r))


@app.delete('/api/suppliers/<sid>')
def delete_supplier(sid):
    if not is_admin():
        return forbid()
    with connect() as conn:
        conn.execute('DELETE FROM vendor_sessions WHERE supplier_id = ?', (sid,))
        conn.execute('DELETE FROM suppliers WHERE id = ?', (sid,))
    return jsonify(ok=True)


@app.get('/api/bootstrap')
def bootstrap():
    if not is_admin():
        return forbid()
    with connect() as conn:
        suppliers = [row_to_supplier(r) for r in conn.execute('SELECT * FROM suppliers ORDER BY id')]
        rows = conn.execute('SELECT data FROM items ORDER BY updated_at DESC').fetchall()
    items = []
    for r in rows:
        try:
            items.append(json.loads(r['data']))
        except (TypeError, ValueError):
            continue
    return jsonify(ok=True, suppliers=suppliers, items=items, serverTime=now_iso())


@app.post('/api/items')
def upsert_item():
    vendor = current_vendor()
    admin = is_admin()
    if not admin and not vendor:
        return forbid()
    body = request.get_json(silent=True) or {}
    item = body.get('item') if isinstance(body.get('item'), dict) else body
    if not isinstance(item, dict) or not item.get('id'):
        return jsonify(ok=False, error='缺少商品 id'), 400
    item_id = str(item['id'])
    with connect() as conn:
        old = conn.execute('SELECT supplier_id, status, data FROM items WHERE id = ?', (item_id,)).fetchone()
        if vendor and not admin:
            # 厂家只能写本厂商品；不能改别人的，也不能自己改变审核状态
            if old and old['supplier_id'] != vendor['id']:
                return forbid()
            item['supplierId'] = vendor['id']
            item['supplierName'] = vendor['name']
            supplier_id = vendor['id']
            status = old['status'] if old else 'pending'
            item['status'] = status
        else:
            supplier_id = item.get('supplierId') or ''
            status = item.get('status') or 'pending'
            # 多人审核冲突保护：已审过的商品，另一台电脑持旧页面提交相反结论时拒绝
            if old and old['status'] in ('ready', 'rejected') \
                    and status in ('ready', 'rejected') and status != old['status']:
                prev_who = '其他审核员'
                try:
                    prev_log = json.loads(old['data']).get('reviewLog') or []
                    if prev_log:
                        prev_who = prev_log[-1].get('operator') or prev_who
                except (TypeError, ValueError):
                    pass
                return jsonify(ok=False, error='该商品已由 %s %s，请刷新页面获取最新状态'
                               % (prev_who, '审核通过' if old['status'] == 'ready' else '驳回')), 409
            # 审核留痕：谁在何时通过/驳回了该商品
            if status in ('ready', 'rejected') and (not old or old['status'] != status):
                entry = {
                    'time': now_iso(),
                    'operator': operator_name(),
                    'action': 'approve' if status == 'ready' else 'reject',
                }
                if status == 'rejected' and item.get('rejectReason'):
                    entry['note'] = str(item['rejectReason'])[:200]
                item.setdefault('reviewLog', []).append(entry)
                item['reviewer'] = entry['operator']
        created = str(item.get('createdAt') or now_iso())
        updated = now_iso()
        item['updatedAt'] = updated
        data = json.dumps(item, ensure_ascii=False)
        if old:
            conn.execute(
                'UPDATE items SET supplier_id=?, status=?, spu_name=?, brand=?, updated_at=?, data=? WHERE id=?',
                (supplier_id, status, item.get('spuName', ''), item.get('brand', ''),
                 updated, data, item_id),
            )
        else:
            conn.execute(
                'INSERT INTO items (id, supplier_id, status, spu_name, brand, created_at, updated_at, data)'
                ' VALUES (?,?,?,?,?,?,?,?)',
                (item_id, supplier_id, status, item.get('spuName', ''), item.get('brand', ''),
                 created, updated, data),
            )
    return jsonify(ok=True, item=item)


@app.delete('/api/items/<item_id>')
def delete_item(item_id):
    if not is_admin():
        return forbid()
    with connect() as conn:
        conn.execute('DELETE FROM items WHERE id = ?', (item_id,))
    return jsonify(ok=True)


@app.post('/api/items/<item_id>/listings')
def add_listing(item_id):
    if not is_admin():
        return forbid()
    body = request.get_json(silent=True) or {}
    shop = (body.get('shop') or '').strip()
    if not shop:
        return jsonify(ok=False, error='缺少店铺'), 400
    with connect() as conn:
        r = conn.execute('SELECT data FROM items WHERE id = ?', (item_id,)).fetchone()
        if not r:
            return jsonify(ok=False, error='商品不存在'), 404
        item = json.loads(r['data'])
        item.setdefault('listings', []).append({'shop': shop, 'time': now_iso()})
        item['updatedAt'] = now_iso()
        conn.execute('UPDATE items SET data=?, updated_at=? WHERE id=?',
                     (json.dumps(item, ensure_ascii=False), item['updatedAt'], item_id))
    return jsonify(ok=True, item=item)


@app.post('/api/upload')
def upload():
    if not is_admin() and not current_vendor():
        return forbid()
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify(ok=False, error='没有收到文件'), 400
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in ALLOWED_EXT:
        return jsonify(ok=False, error='仅支持 png/jpg/jpeg/webp'), 400
    name = uuid.uuid4().hex + '.' + ('jpg' if ext == 'jpeg' else ext)
    f.save(os.path.join(UPLOAD_DIR, name))
    return jsonify(ok=True, url=public_url('/uploads/' + name))


@app.post('/api/reset')
def reset():
    if not is_admin():
        return forbid()
    with connect() as conn:
        conn.execute('DELETE FROM items')
    return jsonify(ok=True)


@app.get('/uploads/<path:name>')
def uploaded(name):
    return send_from_directory(UPLOAD_DIR, name, max_age=604800)


# ==================== 页面路由 ====================
# 运营端 index.html：仅桌面软件（带 X-KL-Token）可访问；浏览器打开一律跳厂家录入页

@app.get('/')
def index():
    if is_admin():
        return send_from_directory(PROTO_DIR, 'index.html')
    return redirect('/vendor')


@app.get('/vendor')
def vendor_page():
    return send_from_directory(PROTO_DIR, 'vendor.html')


@app.get('/<path:filename>')
def proto_static(filename):
    if filename == 'index.html' and not is_admin():
        return redirect('/vendor')
    if filename in ('vendor', 'vendor.html'):
        return send_from_directory(PROTO_DIR, 'vendor.html')
    return send_from_directory(PROTO_DIR, filename)


init_db()

if __name__ == '__main__':
    app.run(host=os.environ.get('HOST', '127.0.0.1'),
            port=int(os.environ.get('PORT', '8000')),
            debug=False, threaded=True)
