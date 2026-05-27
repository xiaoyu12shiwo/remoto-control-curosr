# AGENTS.md

## Cursor Cloud specific instructions

### Project Overview

Cursor Remote Control is a Windows-targeted tool that allows users to remotely control Cursor IDE from a mobile phone browser over a local network. It consists of:

1. **Python Flask Web Server** (`remote_control/remote_control_server.py`) - Core backend serving a mobile web UI on port 5000
2. **VS Code/Cursor Extension** (`remote_control/cursor-remote-extension/`) - Extension that manages the Python server lifecycle

### Platform Limitation (Linux/Cloud)

The Python server uses Windows-specific APIs (`win32gui`, `win32ui`, `ctypes.windll`) for GUI automation. On Linux:
- The Flask web server starts and serves the UI correctly
- API endpoints (`/send`, `/capture`) return "未找到 Cursor 窗口" (Cursor window not found) since there's no Windows GUI
- Win32 stubs are installed in user site-packages (`usercustomize.py`, `win32gui.py`, `win32ui.py`, `win32clipboard.py`) to allow the server to import and start

### Running the Flask Server

```bash
python3 remote_control/remote_control_server.py
```

The server starts on `http://0.0.0.0:5000` by default. Override with env vars:
- `CURSOR_REMOTE_PORT` - port (default 5000)
- `CURSOR_REMOTE_HOST` - host (default 0.0.0.0)

### VS Code Extension Development

```bash
cd remote_control/cursor-remote-extension
npm install          # install dev dependencies
npm run bundle       # copy Python server files to server/ directory
npm run package      # bundle + create .vsix package
node --check extension.js  # syntax validation (cannot fully require without VS Code runtime)
```

### Testing Notes

- No automated test suite exists in this repository
- The Flask web UI can be tested by curling `http://127.0.0.1:5000` (serves the HTML page)
- The `/send` and `/capture` endpoints accept POST JSON requests and respond with JSON
- On Linux, these endpoints will always return `{"ok": false, "msg": "未找到 Cursor 窗口"}` since Win32 window automation is unavailable
