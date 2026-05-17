# Cursor Remote Control 使用手册

## 功能简介

通过手机浏览器远程控制 Cursor IDE，实现：
- 向 Cursor 输入框发送文本指令
- 自动点击发送按钮
- 截取 Cursor 响应结果并返回到手机页面

## 环境要求

- Windows 系统
- Python 3.10+（已加入系统 PATH，或可用 `py -3.10`）
- Cursor IDE 已打开并运行
- 手机与电脑在同一局域网

## 安装 Python 依赖

```bash
pip install opencv-python numpy pywin32 Pillow flask pywinauto
```

若访问 `pypi.org` 出现 SSL 错误，可使用国内镜像（已配置可省略 `-i` 参数）：

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn opencv-python numpy pywin32 Pillow flask pywinauto
```

永久配置镜像（可选，执行一次即可）：

```bash
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn
```

验证依赖是否安装成功：

```bash
python -c "import cv2, flask, win32gui; print('依赖OK')"
```

## 文件说明

```
remote_control/
├── cursor-remote-extension/     # Cursor 扩展（插件）
│   ├── extension.js
│   ├── package.json
│   ├── server/                  # 打包后的 Python 资源（npm run bundle 生成）
│   └── cursor-remote-control-0.1.0.vsix   # 可安装的扩展包
├── remote_control_server.py     # Web 服务端（主程序）
├── remote_control.py            # 命令行版（mic_icon 语音按钮）
├── remote_control_send.py       # 命令行版（send_icon 发送按钮）
├── input_box.png                # 输入框模板图片
├── send_icon.png                # 发送按钮模板图片
├── mic_icon.png                 # 语音按钮模板图片
└── remote_control_使用手册.md    # 本文件
```

## 模板图片准备

如果模板图片不匹配，需要重新截取：

1. 启动远程服务后，浏览器访问 `http://<电脑IP>:5000`
2. 或运行 `python remote_control.py screenshot` 生成 `cursor_screenshot.png`
3. 用图片编辑工具从截图中裁剪以下区域，保存到 `remote_control/` 目录：
   - **input_box.png** — Cursor 右下角的输入框区域
   - **send_icon.png** — 输入框旁的发送按钮图标
   - **mic_icon.png** — 输入框旁的语音按钮图标

若使用插件且已打包过扩展，修改模板后需重新执行 `npm run bundle` 再 `npm run package`，或直接使用上级 `remote_control/` 目录中的模板（从文件夹安装时会自动读取）。

---

## 使用方法

### 方式一：Cursor 插件（推荐）

插件在 Cursor 内一键启停 Python 服务，无需每次手动打开 CMD。

#### 1. 安装插件

**方式 A：安装 VSIX 包（推荐，适合拷贝到其他电脑）**

1. 确认存在文件：`remote_control/cursor-remote-extension/cursor-remote-control-0.1.0.vsix`  
   若没有，在该目录执行：
   ```bash
   npm run bundle
   npm run package
   ```
2. 打开 Cursor → **扩展**（`Ctrl+Shift+X`）→ 右上角 `...` → **Install from VSIX...**
3. 选择 `cursor-remote-control-0.1.0.vsix`
4. 按提示 **Reload Window** 重载窗口

**方式 B：从文件夹安装（开发调试）**

1. `Ctrl+Shift+P` → **Extensions: Install from Location...**（扩展：从位置安装）
2. 选择目录：`remote_control/cursor-remote-extension`
3. 重载窗口

安装成功后，扩展列表中应显示 **Cursor Remote Control**，右下角状态栏会出现 **Remote** 图标。

#### 2. 启动服务

1. **先打开 Cursor 编辑器窗口**（要被控制的窗口）
2. 点击状态栏 **Remote**，或 `Ctrl+Shift+P` 执行 **Cursor Remote: 启动服务**
3. 弹出提示中的地址即为手机访问地址，例如：`http://192.168.1.129:5000`
4. 需要再次复制地址时：**Cursor Remote: 复制手机访问地址**
5. 本机测试可在浏览器打开：`http://127.0.0.1:5000`

查看运行日志：`Ctrl+Shift+P` → **Cursor Remote: 查看日志**  
正常时应看到类似：

```
服务启动: http://0.0.0.0:5000
 * Running on http://192.168.x.x:5000
```

停止服务：点击状态栏 **Remote** 再次切换，或执行 **Cursor Remote: 停止服务**。

#### 3. 插件设置（可选）

打开 Cursor 设置，搜索 `cursorRemote`：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `cursorRemote.pythonPath` | Python 可执行文件完整路径；留空则自动检测 `python` / `py -3` | 空 |
| `cursorRemote.port` | Web 服务端口 | 5000 |
| `cursorRemote.autoStart` | 打开 Cursor 时自动启动远程服务 | false |

若 CMD 中 `python` 不可用，可填写例如：

```
C:\Users\你的用户名\AppData\Local\Programs\Python\Python310\python.exe
```

#### 4. 插件命令一览

| 命令 | 作用 |
|------|------|
| Cursor Remote: 启动服务 | 启动 Python Web 服务 |
| Cursor Remote: 停止服务 | 停止服务 |
| Cursor Remote: 切换服务 | 点击状态栏 Remote 时启停切换 |
| Cursor Remote: 复制手机访问地址 | 复制 `http://局域网IP:端口` |
| Cursor Remote: 在浏览器打开 | 本机浏览器打开控制页 |
| Cursor Remote: 查看日志 | 打开输出面板查看服务日志 |

#### 5. 手机访问与发送指令

1. 手机与电脑连接 **同一 WiFi**
2. 用手机浏览器打开插件提示的地址（或复制的地址）
3. 在页面输入框输入内容，点击 **发送**（或 Ctrl+Enter）
4. 等待进度条结束，页面显示 Cursor 响应截图；点击截图可全屏查看
5. 发送成功的记录会出现在 **历史记录**，点击可重新填入输入框

#### 6. 如何确认插件有效

| 步骤 | 预期结果 |
|------|----------|
| 扩展已启用，状态栏有 Remote | 插件加载成功 |
| 启动服务后日志有 `Running on` | Python 服务正常 |
| 本机打开 `http://127.0.0.1:5000` | Web 页面正常 |
| 手机打开局域网地址 | 远程访问正常 |
| 发送后 Cursor 有输入/回复，页面有截图 | 完整功能有效 |

---

### 方式二：手动运行 Python（不用插件）

**1. 启动服务**

```bash
python remote_control/remote_control_server.py
```

或使用 Python 启动器：

```bash
py -3.10 remote_control/remote_control_server.py
```

输出示例：

```
服务启动: http://0.0.0.0:5000
手机访问: http://<本机IP>:5000
Running on http://192.168.1.129:5000
```

可通过环境变量改端口（插件启动时也会自动设置）：

```bash
set CURSOR_REMOTE_PORT=5000
python remote_control/remote_control_server.py
```

**2. 手机访问**

手机浏览器打开 `http://192.168.1.129:5000`（替换为你的电脑 IP）

**3. 发送指令**

与插件方式相同：输入内容 → 发送 → 等待截图 → 查看历史记录。

---

### 方式三：命令行版（调试模板用）

**仅识别（不操作鼠标）：**

```bash
python remote_control/remote_control.py
```

**识别 + 点击输入框 → 等5秒 → 移动到语音按钮：**

```bash
python remote_control/remote_control.py run
```

**截取窗口截图：**

```bash
python remote_control/remote_control.py screenshot
```

---

## 操作流程

Web 模式下（插件或手动启动），点击发送后的完整流程：

```
手机输入文本 → 发送请求到服务端
  → 服务端截图 Cursor 窗口
  → 模板匹配定位输入框
  → 点击输入框
  → 输入文本
  → 模板匹配定位发送按钮
  → 点击发送按钮
  → 等待 8 秒
  → 再次截图 Cursor 对话区域
  → 返回截图到手机页面
```

## 查看电脑 IP

```bash
ipconfig
```

找到与手机同一网段的 IPv4 地址，通常是 `192.168.x.x` 格式。也可使用插件命令 **复制手机访问地址** 自动获取。

## 常见问题

**Q: CMD 里找不到 python / pip**

Python 可能未加入 PATH。可：
1. 将 `Python310` 与 `Python310\Scripts` 加入用户环境变量 PATH
2. 使用 `py -3.10` 和 `py -3.10 -m pip install ...`
3. 在插件设置中填写 `cursorRemote.pythonPath` 为完整 `python.exe` 路径

关闭 Windows **应用执行别名** 中的 python.exe / python3.exe 占位项，避免指向微软商店。

**Q: pip 安装报 SSL / pypi.org 错误**

使用上文「安装 Python 依赖」中的清华镜像命令，或配置 `pip.ini` 默认镜像源。

**Q: 插件已安装但状态栏没有 Remote**

重载窗口（Reload Window）；在扩展列表确认 **Cursor Remote Control** 已启用。

**Q: 插件启动服务失败**

1. 打开 **Cursor Remote: 查看日志** 查看报错
2. 确认 Python 依赖已安装
3. 在设置中指定 `cursorRemote.pythonPath`
4. 确认 `remote_control_server.py` 存在（从文件夹安装时用上级目录；VSIX 安装时用扩展内 `server/`）

**Q: 提示"未找到 Cursor 窗口"**

确保 Cursor IDE 已打开，且窗口标题以 ` - Cursor` 结尾。如果打开了多个窗口，程序会匹配第一个找到的。

**Q: 提示"未找到输入框"**

1. 确认 `input_box.png` 模板与当前 Cursor 界面匹配
2. Cursor 窗口大小变化可能导致识别不准，重新截取模板图片
3. 检查日志中的置信度数值，低于 0.6 说明模板不匹配

**Q: 鼠标点击位置偏移**

可能是 DPI 缩放问题。程序已设置 DPI 感知，如仍有偏移，可调整代码中的偏移量：
- 输入框：`r[2] // 4`（左上 1/4 处）
- 发送按钮：`r[2] * 3 // 4`（右下 1/4 处）

**Q: 手机无法访问**

1. 确认手机和电脑在同一局域网
2. 检查 Windows 防火墙是否放行 5000 端口（或你设置的端口）
3. 管理员 CMD 执行：
   ```bash
   netsh advfirewall firewall add rule name="CursorRemote" dir=in action=allow protocol=TCP localport=5000
   ```

**Q: 响应截图空白或不完整**

默认等待 8 秒后截图，如果 Cursor 响应较慢，可在发送请求时增加等待时间（修改前端 `WAIT_SECONDS` 变量）。

**Q: 修改代码或模板后插件仍用旧文件**

- **从文件夹安装**：自动使用 `remote_control/` 上级目录，保存即生效，需重启服务
- **VSIX 安装**：需重新 `npm run bundle` → `npm run package` 并安装新 VSIX，或改用手动 Python 方式
