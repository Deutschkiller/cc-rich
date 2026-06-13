const { app, BrowserWindow } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

const ROOT = path.resolve(__dirname, '..');
const RESOURCES = app.isPackaged ? process.resourcesPath : ROOT;
const HOST = process.env.HOST || '127.0.0.1';

let mainWindow = null;
let pyProc = null;
let baseUrl = null;

function startPythonServer() {
  const serverPath = path.join(RESOURCES, 'server.py');
  const python = process.env.PYTHON || 'python3';

  const dataDir = app.isPackaged
    ? path.join(app.getPath('userData'), 'data')
    : ROOT;

  pyProc = spawn(python, [serverPath], {
    cwd: ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: '1', HOST, CCRICH_DATA: dataDir },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  pyProc.stdout.on('data', (data) => {
    const text = data.toString().trim();
    console.log(`[py] ${text}`);
    // 从服务端启动日志解析实际 URL（处理端口回退）
    const m = text.match(/url:\s*(http:\/\/[\d.:]+)/);
    if (m && !baseUrl) baseUrl = m[1];
  });
  pyProc.stderr.on('data', (data) => {
    console.error(`[py:err] ${data.toString().trim()}`);
  });
  pyProc.on('error', (err) => {
    console.error('启动 Python 服务失败:', err.message);
  });
  pyProc.on('exit', (code) => {
    console.log(`Python 服务退出, code=${code}`);
    pyProc = null;
  });
}

function waitForServer(retries = 40, interval = 500) {
  return new Promise((resolve, reject) => {
    let count = 0;
    const check = () => {
      const url = baseUrl || `http://${HOST}:3000`;
      http.get(`${url}/api/status`, (res) => {
        if (res.statusCode === 200) {
          resolve(url);
        } else if (++count < retries) {
          setTimeout(check, interval);
        } else {
          reject(new Error('服务器启动超时'));
        }
      }).on('error', () => {
        if (++count < retries) {
          setTimeout(check, interval);
        } else {
          reject(new Error('服务器启动超时'));
        }
      });
    };
    check();
  });
}

function createWindow(url) {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 800,
    minHeight: 500,
    title: 'CC Rich',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  mainWindow.loadURL(url);
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function killPythonServer() {
  if (pyProc) {
    pyProc.kill('SIGTERM');
    pyProc = null;
  }
}

app.whenReady().then(async () => {
  startPythonServer();
  try {
    const url = await waitForServer();
    createWindow(url);
  } catch (e) {
    console.error(e.message);
    app.quit();
    return;
  }
});

app.on('window-all-closed', () => {
  killPythonServer();
  app.quit();
});

app.on('before-quit', () => {
  killPythonServer();
});

app.on('will-quit', () => {
  killPythonServer();
});
