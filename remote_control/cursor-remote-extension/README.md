# Cursor Remote Control 扩展

在 Cursor 内一键启停现有的 Python 远程控制服务，手机浏览器访问同一局域网地址即可使用。

## 前置条件

1. Windows 系统，已安装 **Python 3**
2. Python 依赖：

```bash
pip install opencv-python numpy pywin32 Pillow flask pywinauto
```

3. `remote_control` 目录下准备好模板图：`input_box.png`、`send_icon.png` 等（见上级目录使用手册）

## 安装方式

### 方式 A：从文件夹安装（开发/本机最快）

1. 在 Cursor 按 `Ctrl+Shift+P`
2. 运行 **`Extensions: Install from Location...`**（扩展：从位置安装）
3. 选择本目录：

```
d:\pro\pythonspace\platformtest\remote_control\cursor-remote-extension
```

4. 按提示 **Reload Window** 重载窗口

> 未打包时，扩展会自动使用上级 `remote_control/` 里的 Python 脚本，无需先执行 bundle。

### 方式 B：安装 .vsix 包（可拷贝到其他电脑）

在本目录打开终端：

```bash
npm run bundle
npm run package
```

生成 `cursor-remote-control-0.1.0.vsix` 后：

1. Cursor → 扩展视图 → `...` → **Install from VSIX...**
2. 选择生成的 `.vsix` 文件

或命令行（若已配置 `cursor` 命令）：

```bash
cursor --install-extension cursor-remote-control-0.1.0.vsix
```

## 使用

1. 确保 **Cursor IDE 窗口已打开**
2. 点击右下角状态栏 **`Remote`**，或命令面板执行 **`Cursor Remote: 启动服务`**
3. 按提示用手机浏览器打开局域网地址（如 `http://192.168.x.x:5000`）
4. 命令 **`Cursor Remote: 复制手机访问地址`** 可再次复制 URL

## 设置

| 配置项 | 说明 |
|--------|------|
| `cursorRemote.pythonPath` | Python 路径，留空自动检测 |
| `cursorRemote.port` | 端口，默认 5000 |
| `cursorRemote.autoStart` | 启动 Cursor 时自动开启服务 |

## 命令

- `Cursor Remote: 启动服务`
- `Cursor Remote: 停止服务`
- `Cursor Remote: 复制手机访问地址`
- `Cursor Remote: 在浏览器打开`
- `Cursor Remote: 查看日志`
