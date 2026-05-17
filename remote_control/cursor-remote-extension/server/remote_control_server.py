import base64
import ctypes
import io
import json
import os
import sys
import threading
import time

import cv2
import numpy as np
import win32gui
import win32ui
from flask import Flask, request, render_template_string, jsonify
from PIL import Image as PILImage

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()


def mouse_move_to(x, y):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def mouse_click():
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)


def type_text(text):
    import pywinauto.keyboard as kb
    kb.send_keys(text)


def paste_from_clipboard():
    import pywinauto.keyboard as kb
    kb.send_keys('^v')


def copy_image_to_clipboard(image_path):
    from PIL import Image as PILImg
    import win32clipboard

    img = PILImg.open(image_path)
    if img.mode != 'RGB':
        img = img.convert('RGB')

    output = io.BytesIO()
    img.save(output, 'BMP')
    data = output.getvalue()[14:]
    output.close()

    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    win32clipboard.CloseClipboard()


def find_cursor_window():
    result = []

    def enum_callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            className = win32gui.GetClassName(hwnd)
            title_lower = title.lower()
            class_lower = className.lower()
            if "cursor" in title_lower and " - cursor" in title_lower:
                if "chrome_widgetwin_1" in class_lower:
                    result.append((hwnd, title, className))

    win32gui.EnumWindows(enum_callback, None)
    return result


def capture_window(hwnd):
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = right - left
    height = bottom - top

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()

    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)

    ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)

    bmp_str = bitmap.GetBitmapBits(True)
    img = np.frombuffer(bmp_str, dtype=np.uint8)
    img.shape = (height, width, 4)
    img_cv = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    mfc_dc.DeleteDC()
    save_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    win32gui.DeleteObject(bitmap.GetHandle())

    return img_cv, (left, top, width, height)


def find_template_full(window_img, window_offset, template_path, label, threshold=0.6):
    template = cv2.imread(template_path, cv2.IMREAD_COLOR)
    if template is None:
        print(f"无法加载模板图片: {template_path}")
        return None
    th, tw = template.shape[:2]
    h, w = window_img.shape[:2]
    if th > h or tw > w:
        print(f"模板 {label} 尺寸({tw}x{th})大于窗口({w}x{h})")
        return None
    result = cv2.matchTemplate(window_img, template, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
    print(f"[{label}] 最佳匹配置信度: {max_val:.4f}, 阈值: {threshold}")
    if max_val >= threshold:
        x, y = max_loc
        abs_x = window_offset[0] + x + tw // 2
        abs_y = window_offset[1] + y + th // 2
        return {
            "type": label,
            "confidence": max_val,
            "rect": (x, y, tw, th),
            "center": (abs_x, abs_y),
        }
    return None


def scan_cursor_ui():
    windows = find_cursor_window()
    if not windows:
        return None, "未找到 Cursor 窗口"

    hwnd, title, class_name = windows[0]
    print(f"[server] 找到窗口: hwnd={hwnd}, title='{title}'")

    window_img, (wx, wy, ww, wh) = capture_window(hwnd)
    window_offset = (wx, wy)
    print(f"[server] 窗口位置: ({wx}, {wy}), 大小: {ww}x{wh}")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_tpl = os.path.join(script_dir, "input_box.png")
    send_tpl = os.path.join(script_dir, "send_icon.png")

    input_match = find_template_full(window_img, window_offset, input_tpl, "input_box", threshold=0.6)
    input_center = None
    input_rect = None
    if input_match:
        r = input_match["rect"]
        input_rect = r
        input_center = (window_offset[0] + r[0] + r[2] // 4,
                        window_offset[1] + r[1] + r[3] // 4)
        print(f"[server] 输入框中心: {input_center}")

    send_match = find_template_full(window_img, window_offset, send_tpl, "send_icon", threshold=0.6)
    send_center = None
    if send_match:
        r = send_match["rect"]
        send_center = (window_offset[0] + r[0] + r[2] * 3 // 4,
                       window_offset[1] + r[1] + r[3] * 3 // 4)
        print(f"[server] 发送按钮中心: {send_center}")

    return {
        "input_center": input_center,
        "send_center": send_center,
        "input_rect": input_rect,
        "hwnd": hwnd,
    }, None


def capture_response(hwnd, input_rect, wait_seconds=8):
    time.sleep(wait_seconds)
    window_img, (wx, wy, ww, wh) = capture_window(hwnd)
    h, w = window_img.shape[:2]

    if input_rect:
        ix, iy, iw, ih = input_rect
        margin = 20
        crop_x1 = max(0, ix - margin)
        crop_y1 = max(0, iy - int(h * 0.6))
        crop_y2 = iy - margin
        crop_x2 = min(w, ix + iw + margin)
        response_img = window_img[crop_y1:crop_y2, crop_x1:crop_x2]
    else:
        response_img = window_img[:int(h * 0.7), :]

    encode_param = [cv2.IMWRITE_JPEG_QUALITY, 80]
    _, buf = cv2.imencode('.jpg', response_img, encode_param)
    img_b64 = base64.b64encode(buf).decode('utf-8')
    return img_b64


def send_to_cursor(text, wait_seconds=8, image_data=None):
    result, err = scan_cursor_ui()
    if err:
        return False, err, None

    if not result["input_center"]:
        return False, "未找到输入框", None

    cx, cy = result["input_center"]
    mouse_move_to(cx, cy)
    time.sleep(0.3)
    mouse_click()
    time.sleep(0.3)

    if image_data:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        uploads_dir = os.path.join(script_dir, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        img_path = os.path.join(uploads_dir, f"photo_{ts}.png")

        img_bytes = base64.b64decode(image_data)
        img_arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img_cv = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
        cv2.imwrite(img_path, img_cv)
        print(f"[server] 图片已保存: {img_path}")

        copy_image_to_clipboard(img_path)
        time.sleep(0.3)
        paste_from_clipboard()
        time.sleep(0.5)

    if text:
        type_text(text)
        time.sleep(0.3)

    if result["send_center"]:
        sx, sy = result["send_center"]
        mouse_move_to(sx, sy)
        time.sleep(0.3)
        mouse_click()
    else:
        import pywinauto.keyboard as kb
        kb.send_keys("{ENTER}")

    print(f"[server] 等待 {wait_seconds} 秒获取响应...")
    img_b64 = capture_response(result["hwnd"], result["input_rect"], wait_seconds)
    return True, "已发送", img_b64


def capture_cursor_response_only(wait_seconds=0):
    """仅重新截取 Cursor 响应区域，不发送消息（用于首次等待不够时再读一次）。"""
    result, err = scan_cursor_ui()
    if err:
        return False, err, None
    print(f"[server] 仅截取响应，等待 {wait_seconds} 秒...")
    img_b64 = capture_response(result["hwnd"], result["input_rect"], wait_seconds)
    return True, "已获取返回", img_b64


HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Cursor Remote</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #1a1a2e;
    color: #eee;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 16px;
}
h1 {
    font-size: 1.4em;
    margin-bottom: 16px;
    color: #00d4ff;
}
.container {
    width: 100%;
    max-width: 600px;
    display: flex;
    flex-direction: column;
    gap: 10px;
}
textarea {
    width: 100%;
    height: 120px;
    background: #16213e;
    color: #eee;
    border: 1px solid #0f3460;
    border-radius: 8px;
    padding: 12px;
    font-size: 16px;
    resize: vertical;
    outline: none;
    transition: border-color 0.2s;
}
textarea:focus {
    border-color: #00d4ff;
}
.photo-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.photo-row {
    display: flex;
    gap: 8px;
    align-items: center;
}
.photo-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 10px 16px;
    background: #1a3a2e;
    color: #2ecc71;
    border: 1px solid #1a5a3e;
    border-radius: 8px;
    cursor: pointer;
    font-size: 14px;
    flex: 1;
    justify-content: center;
}
.photo-btn:hover { background: #1a5a3e; }
.photo-btn .icon { font-size: 18px; }
.photo-preview {
    position: relative;
    display: none;
}
.photo-preview img {
    width: 100%;
    max-height: 120px;
    object-fit: contain;
    border-radius: 8px;
    border: 1px solid #0f3460;
}
.photo-remove {
    position: absolute;
    top: 4px;
    right: 4px;
    background: rgba(231,76,60,0.85);
    color: #fff;
    border: none;
    border-radius: 50%;
    width: 28px;
    height: 28px;
    font-size: 16px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
}
.btn-row {
    display: flex;
    gap: 10px;
}
.btn-row-secondary {
    margin-top: -4px;
}
button {
    flex: 1;
    padding: 14px;
    font-size: 16px;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    transition: background 0.2s, transform 0.1s;
}
button:active {
    transform: scale(0.97);
}
.btn-send {
    background: #0f3460;
    color: #00d4ff;
    font-weight: bold;
}
.btn-send:hover { background: #1a4a8a; }
.btn-send:disabled {
    background: #333;
    color: #666;
    cursor: not-allowed;
    transform: none;
}
.btn-clear {
    background: #2a1a1a;
    color: #e74c3c;
    flex: 0.4;
}
.btn-clear:hover { background: #3a2a2a; }
.btn-fetch {
    flex: 1;
    padding: 10px 14px;
    font-size: 14px;
    background: #2a2a4e;
    color: #a8c8ff;
    border: 1px solid #0f3460;
    border-radius: 8px;
    cursor: pointer;
    transition: background 0.2s, transform 0.1s;
}
.btn-fetch:hover { background: #32325e; }
.btn-fetch:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    transform: none;
}
.status {
    padding: 10px;
    border-radius: 6px;
    text-align: center;
    font-size: 14px;
    min-height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
}
.status.ok { background: #1a3a1a; color: #2ecc71; }
.status.err { background: #3a1a1a; color: #e74c3c; }
.status.idle { background: #16213e; color: #888; }
.status.loading { background: #1a2a3a; color: #00d4ff; }
.response-area {
    display: none;
    flex-direction: column;
    gap: 6px;
    align-items: center;
}
.response-header {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
}
.response-area h3 {
    font-size: 0.9em;
    color: #888;
    margin: 0;
}
.response-img-wrap {
    max-width: 100%;
    max-height: 200px;
    display: flex;
    justify-content: center;
    align-items: center;
    background: #16213e;
    border-radius: 8px;
    border: 1px solid #0f3460;
    padding: 4px;
    overflow: hidden;
}
.response-img {
    max-width: 100%;
    max-height: 192px;
    width: auto;
    height: auto;
    object-fit: contain;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    display: block;
}
.response-img:active {
    opacity: 0.8;
}
.history {
    margin-top: 10px;
}
.history h3 {
    font-size: 0.9em;
    color: #888;
    margin-bottom: 6px;
}
.history-item {
    background: #16213e;
    padding: 8px 12px;
    border-radius: 4px;
    margin-bottom: 4px;
    font-size: 13px;
    word-break: break-all;
    cursor: pointer;
}
.history-item:hover { background: #1a2a4e; }
.history-thumb {
    max-height: 40px;
    border-radius: 4px;
    vertical-align: middle;
    margin-right: 8px;
}
.wait-bar {
    display: none;
    height: 4px;
    background: #16213e;
    border-radius: 2px;
    overflow: hidden;
}
.wait-bar-inner {
    height: 100%;
    width: 0%;
    background: #00d4ff;
    border-radius: 2px;
    transition: width 0.5s linear;
}
.wait-bar.active {
    display: block;
}
</style>
</head>
<body>
<h1>Cursor Remote Control</h1>
<div class="container">
    <textarea id="msg" placeholder="输入要发送的内容..."></textarea>

    <div class="btn-row">
        <button class="btn-send" id="sendBtn" onclick="doSend()">发送</button>
        <button class="btn-clear" onclick="clearAll()">清空</button>
    </div>
    <div class="wait-bar" id="waitBar"><div class="wait-bar-inner" id="waitBarInner"></div></div>
    <div class="status idle" id="status">就绪</div>
    <div class="btn-row btn-row-secondary">
        <button type="button" class="btn-fetch" id="fetchBtn" onclick="fetchResponse()">获取返回</button>
    </div>
    <div class="response-area" id="responseArea">
        <div class="response-header">
            <h3>Cursor 响应</h3>
        </div>
        <div class="response-img-wrap">
            <img class="response-img" id="responseImg" src="" alt="response" onclick="openFull(this)">
        </div>
    </div>
    <div class="history">
        <h3>历史记录 <span style="font-size:0.8em;color:#666">(点击可复用)</span></h3>
        <div id="historyList"></div>
    </div>

    <div class="photo-section">
        <div class="photo-row">
            <label class="photo-btn">
                <span class="icon">&#128247;</span> 拍照
                <input type="file" accept="image/*" capture="environment" id="cameraInput" style="display:none" onchange="handlePhoto(this)">
            </label>
            <label class="photo-btn">
                <span class="icon">&#128444;</span> 选图
                <input type="file" accept="image/*" id="fileInput" style="display:none" onchange="handlePhoto(this)">
            </label>
        </div>
        <div class="photo-preview" id="photoPreview">
            <img id="previewImg" src="" alt="preview">
            <button class="photo-remove" onclick="removePhoto()">&#10005;</button>
        </div>
    </div>
</div>
<script>
var WAIT_SECONDS = 8;
var currentPhotoBase64 = null;

document.getElementById('msg').addEventListener('keydown', function(e) {
    if (e.ctrlKey && e.key === 'Enter') {
        e.preventDefault();
        doSend();
    }
});

function handlePhoto(input) {
    var file = input.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function(e) {
        var dataUrl = e.target.result;
        document.getElementById('previewImg').src = dataUrl;
        document.getElementById('photoPreview').style.display = 'block';
        var base64 = dataUrl.split(',')[1];
        currentPhotoBase64 = base64;
    };
    reader.readAsDataURL(file);
}

function removePhoto() {
    currentPhotoBase64 = null;
    document.getElementById('photoPreview').style.display = 'none';
    document.getElementById('previewImg').src = '';
    document.getElementById('cameraInput').value = '';
    document.getElementById('fileInput').value = '';
}

function clearAll() {
    document.getElementById('msg').value = '';
    removePhoto();
}

function doSend() {
    var msg = document.getElementById('msg').value.trim();
    if (!msg && !currentPhotoBase64) {
        setStatus('请输入内容或选择图片', 'err');
        return;
    }
    var btn = document.getElementById('sendBtn');
    btn.disabled = true;
    btn.textContent = '发送中...';
    setStatus('正在发送，等待 Cursor 响应...', 'loading');

    document.getElementById('responseArea').style.display = 'none';
    document.getElementById('waitBar').className = 'wait-bar active';
    startProgress(WAIT_SECONDS);

    var payload = {
        text: msg || '',
        wait: WAIT_SECONDS,
        image: currentPhotoBase64 || null
    };

    fetch('/send', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        stopProgress();
        if (d.ok) {
            setStatus(d.msg, 'ok');
            addHistory(msg, currentPhotoBase64);
            document.getElementById('msg').value = '';
            removePhoto();
            if (d.image) {
                document.getElementById('responseImg').src = 'data:image/jpeg;base64,' + d.image;
                document.getElementById('responseArea').style.display = 'flex';
            }
        } else {
            setStatus('失败: ' + d.msg, 'err');
        }
    })
    .catch(function(e) {
        stopProgress();
        setStatus('网络错误: ' + e, 'err');
    })
    .finally(function() {
        btn.disabled = false;
        btn.textContent = '发送';
    });
}

function fetchResponse() {
    var btn = document.getElementById('fetchBtn');
    btn.disabled = true;
    setStatus('正在截取 Cursor 响应...', 'loading');

    fetch('/capture', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ wait: 0 })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.ok && d.image) {
            setStatus(d.msg, 'ok');
            document.getElementById('responseImg').src = 'data:image/jpeg;base64,' + d.image;
            document.getElementById('responseArea').style.display = 'flex';
        } else if (d.ok) {
            setStatus(d.msg || '未得到图片', 'err');
        } else {
            setStatus('失败: ' + d.msg, 'err');
        }
    })
    .catch(function(e) {
        setStatus('网络错误: ' + e, 'err');
    })
    .finally(function() {
        btn.disabled = false;
    });
}

var progressTimer = null;
function startProgress(seconds) {
    var bar = document.getElementById('waitBarInner');
    bar.style.width = '0%';
    var elapsed = 0;
    var interval = 200;
    progressTimer = setInterval(function() {
        elapsed += interval;
        var pct = Math.min((elapsed / (seconds * 1000)) * 100, 99);
        bar.style.width = pct + '%';
    }, interval);
}

function stopProgress() {
    if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
    document.getElementById('waitBarInner').style.width = '100%';
    setTimeout(function() {
        document.getElementById('waitBar').className = 'wait-bar';
    }, 500);
}

function setStatus(msg, cls) {
    var el = document.getElementById('status');
    el.textContent = msg;
    el.className = 'status ' + cls;
}

function addHistory(text, photoBase64) {
    var list = document.getElementById('historyList');
    var item = document.createElement('div');
    item.className = 'history-item';
    var now = new Date();
    var t = now.getHours().toString().padStart(2,'0') + ':' +
            now.getMinutes().toString().padStart(2,'0') + ':' +
            now.getSeconds().toString().padStart(2,'0');
    var html = '';
    if (photoBase64) {
        html += '<img class="history-thumb" src="data:image/jpeg;base64,' + photoBase64.substring(0, 100) + '...">';
    }
    html += '[' + t + '] ' + (text || '[图片]');
    item.innerHTML = html;
    item.onclick = function() {
        if (text) document.getElementById('msg').value = text;
    };
    list.insertBefore(item, list.firstChild);
    while (list.children.length > 20) {
        list.removeChild(list.lastChild);
    }
}

function openFull(img) {
    var w = window.open('');
    w.document.write('<html><head><title>Cursor Response</title><style>body{margin:0;background:#111;display:flex;align-items:center;justify-content:center;min-height:100vh;}img{max-width:100%;}</style></head><body><img src="' + img.src + '"></body></html>');
}
</script>
</body>
</html>
"""

app = Flask(__name__)


@app.route('/')
def index():
    return render_template_string(HTML_PAGE)


@app.route('/send', methods=['POST'])
def handle_send():
    data = request.get_json(force=True)
    text = data.get('text', '')
    wait = data.get('wait', 8)
    image_data = data.get('image', None)

    if not text and not image_data:
        return jsonify({"ok": False, "msg": "内容和图片都为空"})

    ok, msg, img_b64 = send_to_cursor(text, wait_seconds=wait, image_data=image_data)
    resp = {"ok": ok, "msg": msg}
    if img_b64:
        resp["image"] = img_b64
    return jsonify(resp)


@app.route('/capture', methods=['POST'])
def handle_capture():
    data = request.get_json(silent=True) or {}
    try:
        wait = int(data.get('wait', 0))
    except (TypeError, ValueError):
        wait = 0
    ok, msg, img_b64 = capture_cursor_response_only(wait_seconds=max(0, wait))
    resp = {"ok": ok, "msg": msg}
    if img_b64:
        resp["image"] = img_b64
    return jsonify(resp)


if __name__ == '__main__':
    host = os.environ.get("CURSOR_REMOTE_HOST", "0.0.0.0")
    port = int(os.environ.get("CURSOR_REMOTE_PORT", "5000"))
    script_dir = os.path.dirname(os.path.abspath(__file__))
    uploads_dir = os.path.join(script_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    print(f"服务启动: http://{host}:{port}")
    print(f"手机访问: http://<本机IP>:{port}")
    app.run(host=host, port=port, debug=False)
