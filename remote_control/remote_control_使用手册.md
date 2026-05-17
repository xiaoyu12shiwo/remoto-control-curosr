# Cursor Remote Control 使用手册

## 功能简介

通过手机浏览器远程控制 Cursor IDE，实现：
- 向 Cursor 输入框发送文本指令
- 自动点击发送按钮
- 截取 Cursor 响应结果并返回到手机页面

## 环境要求

- Windows 系统
- Python 3.x
- Cursor IDE 已打开并运行
- 手机与电脑在同一局域网

## 安装依赖

```bash
pip install opencv-python numpy pywin32 Pillow flask pywinauto
```

## 文件说明

```
remote_control/
├── remote_control_server.py   # Web 服务端（主程序）
├── remote_control.py          # 命令行版（mic_icon 语音按钮）
├── remote_control_send.py     # 命令行版（send_icon 发送按钮）
├── input_box.png              # 输入框模板图片
├── send_icon.png              # 发送按钮模板图片
├── mic_icon.png               # 语音按钮模板图片
└── remote_control_使用手册.md  # 本文件
```

## 模板图片准备

如果模板图片不匹配，需要重新截取：

1. 运行 `python remote_control_server.py`，浏览器访问 `http://<电脑IP>:5000`
2. 或运行 `python remote_control.py screenshot` 生成 `cursor_screenshot.png`
3. 用图片编辑工具从截图中裁剪以下区域，保存到 `remote_control/` 目录：
   - **input_box.png** — Cursor 右下角的输入框区域
   - **send_icon.png** — 输入框旁的发送按钮图标
   - **mic_icon.png** — 输入框旁的语音按钮图标

## 使用方法

### 方式一：Web 远程控制（推荐）

**1. 启动服务**

在电脑上运行：

```bash
python remote_control/remote_control_server.py
```

输出示例：
```
服务启动: http://0.0.0.0:5000
手机访问: http://<本机IP>:5000
Running on http://192.168.1.129:5000
```

**2. 手机访问**

手机浏览器打开 `http://192.168.1.129:5000`（替换为你的电脑 IP）

**3. 发送指令**

- 在文本框输入内容
- 点击"发送"按钮（或 Ctrl+Enter）
- 等待进度条走完，页面会显示 Cursor 的响应截图
- 点击截图可全屏查看

**4. 历史记录**

- 发送成功的内容会记录在"历史记录"区域
- 点击历史记录可自动填入输入框

### 方式二：命令行版

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

## 操作流程

Web 模式下，点击发送后的完整流程：

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

找到与手机同一网段的 IPv4 地址，通常是 `192.168.x.x` 格式。

## 常见问题

**Q: 提示"未找到 Cursor 窗口"**

确保 Cursor IDE 已打开，且窗口标题以 " - Cursor" 结尾。如果打开了多个窗口，程序会匹配第一个找到的。

**Q: 提示"未找到输入框"**

1. 确认 `input_box.png` 模板与当前 Cursor 界面匹配
2. Cursor 窗口大小变化可能导致识别不准，重新截取模板图片
3. 检查终端输出的置信度数值，如果低于 0.6 说明模板不匹配

**Q: 鼠标点击位置偏移**

可能是 DPI 缩放问题。程序已设置 DPI 感知，如仍有偏移，可调整代码中的偏移量：
- 输入框：`r[2] // 4`（左上 1/4 处）
- 发送按钮：`r[2] * 3 // 4`（右下 1/4 处）

**Q: 手机无法访问**

1. 确认手机和电脑在同一局域网
2. 检查 Windows 防火墙是否放行 5000 端口
3. 可用以下命令开放端口：
   ```bash
   netsh advfirewall firewall add rule name="CursorRemote" dir=in action=allow protocol=TCP localport=5000
   ```

**Q: 响应截图空白或不完整**

默认等待 8 秒后截图，如果 Cursor 响应较慢，可在发送请求时增加等待时间（修改前端 `WAIT_SECONDS` 变量）。
