const { app, BrowserWindow, ipcMain, shell } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const crypto = require('crypto');

let mainWindow;
let loginWindow;
let loginPoll;
let backend;
let backendBuffer = '';
const pending = new Map();
let loginCaptureInFlight = false;
let backendReadyData = null;
const projectRoot = path.resolve(__dirname, '..');

function sendToRenderer(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
}

function startBackend() {
  const dataDir = app.getPath('userData');
  if (app.isPackaged) {
    const bundled = path.join(process.resourcesPath, 'backend', 'electron_bridge.exe');
    backend = spawn(bundled, ['--data-dir', dataDir], { cwd: path.dirname(bundled), windowsHide: true });
  } else {
    const python = process.env.DOUYIN_PYTHON || 'python';
    backend = spawn(python, [path.join(projectRoot, 'electron_bridge.py'), '--data-dir', dataDir], {
      cwd: projectRoot, windowsHide: true,
    });
  }
  backend.stdout.on('data', (chunk) => {
    backendBuffer += chunk.toString();
    const lines = backendBuffer.split(/\r?\n/);
    backendBuffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.trim()) continue;
      try { handleBackendMessage(JSON.parse(line)); } catch (error) {
        sendToRenderer('backend-event', { event: 'log', message: `桥接消息解析失败：${error.message}` });
      }
    }
  });
  backend.stderr.on('data', (chunk) => sendToRenderer('backend-event', { event: 'log', message: chunk.toString().trim() }));
  backend.on('error', (error) => sendToRenderer('backend-event', { event: 'error', message: `Python 后端启动失败：${error.message}` }));
  backend.on('exit', (code) => { if (code !== 0) sendToRenderer('backend-event', { event: 'error', message: `Python 后端已退出（${code}）` }); });
}

function handleBackendMessage(message) {
  if (message.type === 'ready') { backendReadyData = { event: 'ready', dataDir: message.dataDir }; sendToRenderer('backend-event', backendReadyData); return; }
  if (message.type === 'event') { sendToRenderer('backend-event', message); return; }
  if (message.type === 'response' && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.ok) resolve(message.data); else reject(new Error(message.error || '操作失败'));
  }
}

function bridgeRequest(command, extra = {}) {
  return new Promise((resolve, reject) => {
    if (!backend || backend.killed) return reject(new Error('本地采集引擎尚未启动'));
    const id = crypto.randomUUID();
    pending.set(id, { resolve, reject });
    backend.stdin.write(`${JSON.stringify({ id, command, ...extra })}\n`);
  });
}

async function readLoginState() {
  if (!loginWindow || loginWindow.isDestroyed() || loginWindow.webContents.isDestroyed()) return null;
  const webState = await loginWindow.webContents.executeJavaScript(`({
    url: location.href,
    webProtect: localStorage.getItem('security-sdk/s_sdk_sign_data_key/web_protect') || '',
    keys: localStorage.getItem('security-sdk/s_sdk_crypt_sdk') || ''
  })`, true);
  const cookies = (await loginWindow.webContents.session.cookies.get({})).filter((cookie) => cookie.domain.endsWith('douyin.com'));
  const cookieMap = {};
  for (const cookie of cookies) cookieMap[cookie.name] = cookie.value;
  return { ...webState, cookies: cookieMap };
}

async function tryCaptureLogin() {
  if (loginCaptureInFlight || !loginWindow || loginWindow.isDestroyed()) return;
  loginCaptureInFlight = true;
  try {
    const state = await readLoginState();
    if (!state || !(state.cookies.sessionid || state.cookies.sessionid_ss)) return;
    if (!state.webProtect || !state.keys) return;
    const result = await bridgeRequest('login_state', state);
    sendToRenderer('auth-state', { authenticated: true, ...result });
    if (loginWindow && !loginWindow.isDestroyed()) loginWindow.close();
  } catch (error) {
    sendToRenderer('backend-event', { event: 'log', message: `登录状态暂未确认：${error.message}` });
  } finally { loginCaptureInFlight = false; }
}

function openLoginWindow() {
  if (loginWindow && !loginWindow.isDestroyed()) { loginWindow.focus(); return; }
  loginWindow = new BrowserWindow({
    width: 1120, height: 780, minWidth: 900, minHeight: 650, title: '抖音扫码登录', autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, partition: 'persist:douyin' },
  });
  loginWindow.loadURL('https://www.douyin.com/');
  loginWindow.webContents.on('did-finish-load', () => {
    loginWindow.webContents.executeJavaScript(`(() => {
      const nodes = Array.from(document.querySelectorAll('button, span, div, a'));
      const hit = nodes.find(n => ['登录', '登 录'].includes((n.textContent || '').trim()));
      if (hit) hit.click();
    })()`).catch(() => {});
    clearInterval(loginPoll);
    loginPoll = setInterval(tryCaptureLogin, 1800);
    tryCaptureLogin();
  });
  loginWindow.on('closed', () => { clearInterval(loginPoll); loginPoll = null; loginWindow = null; });
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1260, height: 850, minWidth: 1000, minHeight: 700, title: 'Douyin Spider', autoHideMenuBar: true,
    backgroundColor: '#111518', webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
  });
  mainWindow.webContents.on('did-finish-load', () => { if (backendReadyData) sendToRenderer('backend-event', backendReadyData); });
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

ipcMain.handle('open-login', () => openLoginWindow());
ipcMain.handle('get-auth-status', () => bridgeRequest('load_saved'));
ipcMain.handle('crawl', (_event, request) => bridgeRequest('crawl', request));
ipcMain.handle('open-output', (_event, folder) => shell.openPath(folder || app.getPath('userData')));
ipcMain.handle('get-app-info', () => ({ dataDir: app.getPath('userData'), packaged: app.isPackaged }));

app.whenReady().then(() => { startBackend(); createMainWindow(); });
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', () => { if (backend && !backend.killed) backend.kill(); });
