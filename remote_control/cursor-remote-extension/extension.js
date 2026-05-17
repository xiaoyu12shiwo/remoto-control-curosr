'use strict';

const vscode = require('vscode');
const cp = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

/** @type {import('child_process').ChildProcess | null} */
let serverProcess = null;
/** @type {vscode.StatusBarItem | undefined} */
let statusBarItem;
/** @type {vscode.OutputChannel | undefined} */
let outputChannel;
/** @type {string} */
let lastMobileUrl = '';

/**
 * @param {vscode.ExtensionContext} context
 */
function resolveServerDir(context) {
  const bundled = path.join(context.extensionPath, 'server');
  const bundledScript = path.join(bundled, 'remote_control_server.py');
  if (fs.existsSync(bundledScript)) {
    return bundled;
  }
  const parent = path.join(context.extensionPath, '..');
  const parentScript = path.join(parent, 'remote_control_server.py');
  if (fs.existsSync(parentScript)) {
    return parent;
  }
  return bundled;
}

function getConfig() {
  const cfg = vscode.workspace.getConfiguration('cursorRemote');
  return {
    pythonPath: (cfg.get('pythonPath') || '').trim(),
    port: cfg.get('port') || 5000,
    autoStart: cfg.get('autoStart') === true,
  };
}

function log(line) {
  const text = typeof line === 'string' ? line : String(line);
  outputChannel?.appendLine(text);
}

function getLocalIpv4() {
  const nets = os.networkInterfaces();
  const candidates = [];
  for (const name of Object.keys(nets)) {
    for (const net of nets[name] || []) {
      if (net.family === 'IPv4' && !net.internal) {
        candidates.push(net.address);
      }
    }
  }
  const lan = candidates.find((ip) => ip.startsWith('192.168.') || ip.startsWith('10.'));
  return lan || candidates[0] || '127.0.0.1';
}

function buildMobileUrl(port) {
  return `http://${getLocalIpv4()}:${port}`;
}

function updateStatusBar(running) {
  if (!statusBarItem) {
    return;
  }
  const { port } = getConfig();
  if (running) {
    lastMobileUrl = buildMobileUrl(port);
    statusBarItem.text = `$(radio-tower) Remote ${port}`;
    statusBarItem.tooltip = `远程控制已运行\n手机访问: ${lastMobileUrl}\n点击停止`;
    statusBarItem.backgroundColor = undefined;
  } else {
    statusBarItem.text = '$(circle-slash) Remote';
    statusBarItem.tooltip = '远程控制未运行\n点击启动';
    statusBarItem.backgroundColor = undefined;
  }
  statusBarItem.show();
}

/**
 * @returns {Promise<string>}
 */
function resolvePythonExecutable() {
  const { pythonPath } = getConfig();
  if (pythonPath) {
    return Promise.resolve(pythonPath);
  }

  const candidates = process.platform === 'win32'
    ? ['python', 'py']
    : ['python3', 'python'];

  return new Promise((resolve, reject) => {
    let index = 0;

    const tryNext = () => {
      if (index >= candidates.length) {
        reject(new Error('未找到 Python，请在设置中配置 cursorRemote.pythonPath'));
        return;
      }
      const cmd = candidates[index++];
      const args = cmd === 'py' ? ['-3', '--version'] : ['--version'];
      const child = cp.spawn(cmd, args, { shell: true, windowsHide: true });
      let stderr = '';
      child.stderr.on('data', (d) => { stderr += d.toString(); });
      child.on('error', () => tryNext());
      child.on('close', (code) => {
        if (code === 0) {
          resolve(cmd === 'py' ? 'py -3' : cmd);
        } else {
          tryNext();
        }
      });
    };

    tryNext();
  });
}

/**
 * @param {vscode.ExtensionContext} context
 */
async function startServer(context) {
  if (serverProcess) {
    vscode.window.showInformationMessage('远程控制服务已在运行');
    return;
  }

  const serverDir = resolveServerDir(context);
  const scriptPath = path.join(serverDir, 'remote_control_server.py');
  if (!fs.existsSync(scriptPath)) {
    vscode.window.showErrorMessage(
      `未找到 remote_control_server.py：${scriptPath}\n请先运行扩展目录下的 npm run bundle，或从含 Python 文件的 remote_control 目录安装。`
    );
    return;
  }

  let python;
  try {
    python = await resolvePythonExecutable();
  } catch (err) {
    vscode.window.showErrorMessage(err.message || String(err));
    return;
  }

  const { port } = getConfig();
  const pythonParts = python.split(/\s+/);
  const cmd = pythonParts[0];
  const baseArgs = pythonParts.slice(1);

  log(`启动: ${python} ${scriptPath}`);
  log(`工作目录: ${serverDir}`);
  log(`端口: ${port}`);

  serverProcess = cp.spawn(cmd, [...baseArgs, scriptPath], {
    cwd: serverDir,
    env: {
      ...process.env,
      CURSOR_REMOTE_PORT: String(port),
      CURSOR_REMOTE_HOST: '0.0.0.0',
    },
    shell: true,
    windowsHide: true,
  });

  serverProcess.stdout?.on('data', (data) => {
    log(data.toString().trimEnd());
  });
  serverProcess.stderr?.on('data', (data) => {
    log('[stderr] ' + data.toString().trimEnd());
  });

  serverProcess.on('error', (err) => {
    log(`进程错误: ${err.message}`);
    serverProcess = null;
    updateStatusBar(false);
    vscode.window.showErrorMessage(`启动失败: ${err.message}`);
  });

  serverProcess.on('close', (code) => {
    log(`服务已退出 (code=${code})`);
    serverProcess = null;
    updateStatusBar(false);
  });

  updateStatusBar(true);
  lastMobileUrl = buildMobileUrl(port);

  const msg = `远程控制已启动。手机浏览器访问：${lastMobileUrl}`;
  const copy = '复制地址';
  const open = '本机打开';
  vscode.window.showInformationMessage(msg, copy, open).then((choice) => {
    if (choice === copy) {
      vscode.env.clipboard.writeText(lastMobileUrl);
    } else if (choice === open) {
      vscode.env.openExternal(vscode.Uri.parse(lastMobileUrl));
    }
  });
}

function stopServer() {
  if (!serverProcess) {
    vscode.window.showInformationMessage('远程控制服务未在运行');
    return;
  }
  log('正在停止服务...');
  serverProcess.kill();
  serverProcess = null;
  updateStatusBar(false);
  vscode.window.showInformationMessage('远程控制服务已停止');
}

/**
 * @param {vscode.ExtensionContext} context
 */
function toggleServer(context) {
  if (serverProcess) {
    stopServer();
  } else {
    startServer(context);
  }
}

async function copyMobileUrl() {
  const { port } = getConfig();
  const url = serverProcess ? lastMobileUrl || buildMobileUrl(port) : buildMobileUrl(port);
  await vscode.env.clipboard.writeText(url);
  vscode.window.showInformationMessage(`已复制: ${url}`);
}

async function openMobileUrl() {
  const { port } = getConfig();
  const url = serverProcess ? lastMobileUrl || buildMobileUrl(port) : buildMobileUrl(port);
  await vscode.env.openExternal(vscode.Uri.parse(url));
}

function showLog() {
  outputChannel?.show(true);
}

/**
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
  outputChannel = vscode.window.createOutputChannel('Cursor Remote Control');
  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 200);
  statusBarItem.command = 'cursorRemote.toggleServer';

  context.subscriptions.push(
    outputChannel,
    statusBarItem,
    vscode.commands.registerCommand('cursorRemote.startServer', () => startServer(context)),
    vscode.commands.registerCommand('cursorRemote.stopServer', stopServer),
    vscode.commands.registerCommand('cursorRemote.toggleServer', () => toggleServer(context)),
    vscode.commands.registerCommand('cursorRemote.copyMobileUrl', copyMobileUrl),
    vscode.commands.registerCommand('cursorRemote.openMobileUrl', openMobileUrl),
    vscode.commands.registerCommand('cursorRemote.showLog', showLog),
  );

  updateStatusBar(false);

  if (getConfig().autoStart) {
    startServer(context).catch((err) => {
      log(`自动启动失败: ${err.message || err}`);
    });
  }
}

function deactivate() {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
}

module.exports = {
  activate,
  deactivate,
};
