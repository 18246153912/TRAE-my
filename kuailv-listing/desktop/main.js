// 餐厨产品录入系统 · 运营管理端（Electron 纯前端壳）
// 通用安装包：首次启动填写「服务器地址 + 运营授权码 + 审核人姓名」，
// 保存在本机 userData/config.json；之后自动携带令牌访问该客户的专属服务器。
// 每个客户部署独立服务器+域名+独立数据库+专属授权码，数据互相隔离。
const { app, BrowserWindow, shell, Menu, session, ipcMain, net } = require('electron');
const fs = require('fs');
const path = require('path');

function configFile() {
  return path.join(app.getPath('userData'), 'config.json');
}

function loadConfig() {
  try {
    const cfg = JSON.parse(fs.readFileSync(configFile(), 'utf8'));
    if (cfg && cfg.serverUrl && cfg.token) return cfg;
  } catch (e) { /* 无配置或已损坏 */ }
  return null;
}

function saveConfig(cfg) {
  fs.writeFileSync(configFile(), JSON.stringify(cfg, null, 2), 'utf8');
}

function normalizeUrl(u) {
  u = String(u || '').trim();
  if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
  return u.replace(/\/+$/, '') + '/';
}

// 对配置的服务器域名自动注入访问令牌与审核人标识（浏览器无此头，直接打开无效）
function injectHeaders(cfg) {
  const host = new URL(cfg.serverUrl).hostname;
  session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) => {
    try {
      if (new URL(details.url).hostname === host) {
        details.requestHeaders['X-KL-Token'] = cfg.token;
        if (cfg.operator) details.requestHeaders['X-KL-Operator'] = cfg.operator;
      }
    } catch (e) { /* 忽略异常 URL */ }
    callback({ requestHeaders: details.requestHeaders });
  });
}

function createMainWindow(cfg) {
  const host = new URL(cfg.serverUrl).hostname;
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    title: '餐厨产品录入系统',
    autoHideMenuBar: true,
    backgroundColor: '#f5f7fa',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  win.loadURL(cfg.serverUrl);

  // 外部链接一律交给系统浏览器，应用窗口内只操作系统本身
  win.webContents.setWindowOpenHandler(({ url }) => {
    try {
      if (new URL(url).hostname === host) return { action: 'allow' };
    } catch (e) { /* 忽略异常 URL */ }
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

function openConfigWindow() {
  const win = new BrowserWindow({
    width: 480,
    height: 600,
    resizable: false,
    title: '连接服务器',
    autoHideMenuBar: true,
    backgroundColor: '#f5f7fa',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  win.loadFile('config.html');
}

ipcMain.handle('config:load', () => loadConfig());

ipcMain.handle('config:save', async (event, form) => {
  const serverUrl = normalizeUrl(form && form.serverUrl);
  const token = String((form && form.token) || '').trim();
  const operator = String((form && form.operator) || '').trim();
  if (!serverUrl || !token) return { ok: false, error: '请填写服务器地址和运营授权码' };
  // 先带令牌验证服务器，通了才保存
  try {
    const resp = await net.fetch(serverUrl + 'api/bootstrap', { headers: { 'X-KL-Token': token } });
    if (resp.status === 403) return { ok: false, error: '授权码不正确，请与系统管理员核对' };
    if (!resp.ok) return { ok: false, error: '服务器返回异常（HTTP ' + resp.status + '）' };
  } catch (e) {
    return { ok: false, error: '连不上服务器，请检查地址是否正确、网络是否正常' };
  }
  const cfg = { serverUrl, token, operator };
  saveConfig(cfg);
  injectHeaders(cfg);
  createMainWindow(cfg);
  const senderWin = BrowserWindow.fromWebContents(event.sender);
  if (senderWin) senderWin.close();
  return { ok: true };
});

function start() {
  const cfg = loadConfig();
  if (cfg) {
    injectHeaders(cfg);
    createMainWindow(cfg);
  } else {
    openConfigWindow();
  }
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: '选项',
      submenu: [
        { label: '重新配置服务器', accelerator: 'CmdOrCtrl+Shift+S', click: openConfigWindow },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    },
  ]));
  start();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) start();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
