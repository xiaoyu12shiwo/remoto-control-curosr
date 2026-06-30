import base64
import ctypes
import io
import json
import os
import re
import sys
import threading
import time

if getattr(sys, "frozen", False):
    sys.path.insert(0, getattr(sys, "_MEIPASS", ""))

from app_paths import bundle_dir, exe_dir
from load_local_env import load_local_env_file

_chat_baseline = None          # list[str]，发送前快照（与采集使用相同滚动逻辑）
_chat_baseline_set = set()
_chat_user_message = None      # 本次发送的用户文本，用于锚定增量起点
_chat_lock = threading.Lock()

_cursor_window_index = None    # None=自动；int=固定使用第 N 个窗口（0 起，全量列表）
_cursor_target_hwnd = None     # 优先：用户指定的窗口 HWND
_window_lock = threading.Lock()

import cv2
import numpy as np
import uiautomation as ua
import win32gui
import win32ui
from flask import Flask, request, render_template_string, jsonify
from PIL import Image as PILImage

try:
    import cursor_sdk_backend as _sdk_backend
except ImportError:
    _sdk_backend = None

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()


def _ensure_com_initialized():
    """Flask 工作线程中调用 UIA 前需初始化 COM。"""
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass


def _safe_print(*args, **kwargs):
    """控制台为 GBK 时避免打印 Unicode 导致请求崩溃。"""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(enc, errors="replace").decode(enc, errors="replace") + "\n")


def mouse_move_to(x, y):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def mouse_click():
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)


MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


def mouse_wheel(notches=1):
    """滚轮：正数向上滚（查看更早的聊天内容）。"""
    delta = int(notches * WHEEL_DELTA)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)


def focus_chat_panel(chat_region_screen):
    """点击聊天列，确保滚轮作用于 Agent 面板。"""
    if not chat_region_screen:
        return
    x1, y1, x2, y2 = chat_region_screen
    cx = (x1 + x2) // 2
    cy = y1 + max(40, (y2 - y1) // 4)
    mouse_move_to(cx, cy)
    time.sleep(0.05)
    mouse_click()
    time.sleep(0.15)


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


def _enum_cursor_windows():
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
    result.sort(key=lambda item: item[1].lower())
    return result


def _exclude_title_patterns():
    patterns = []
    raw = os.environ.get("CURSOR_REMOTE_EXCLUDE", "").strip()
    if raw:
        patterns.extend(p.strip().lower() for p in raw.split(",") if p.strip())
    if os.environ.get("CURSOR_REMOTE_EXCLUDE_SELF", "1").lower() not in ("0", "false", "no"):
        script_dir = exe_dir()
        workspace = os.path.basename(os.path.dirname(script_dir)).lower()
        if workspace:
            patterns.append(workspace)
        patterns.append("remote_control_server")
    return list(dict.fromkeys(patterns))


def _title_is_excluded(title, patterns=None):
    patterns = patterns or _exclude_title_patterns()
    tl = (title or "").lower()
    return any(p in tl for p in patterns)


def find_all_cursor_windows():
    return _enum_cursor_windows()


def find_cursor_window(include_excluded=False):
    """返回 Cursor 窗口列表；默认排除本服务所在工作区窗口。"""
    all_windows = _enum_cursor_windows()
    if include_excluded:
        return all_windows
    patterns = _exclude_title_patterns()
    return [w for w in all_windows if not _title_is_excluded(w[1], patterns)]


def get_cursor_window_index():
    """返回固定窗口下标（0 起，全量列表），None 表示未按序号固定。"""
    with _window_lock:
        if _cursor_window_index is not None:
            return _cursor_window_index
    return None


def get_cursor_target_hwnd():
    with _window_lock:
        return _cursor_target_hwnd


def set_cursor_window_target(hwnd=None, index_1based=None, auto=False):
    """设置目标 Cursor。hwnd 优先；auto=True 恢复自动（排除本服务窗口后择优）。"""
    global _cursor_window_index, _cursor_target_hwnd
    with _window_lock:
        if auto or (hwnd is None and index_1based is None):
            _cursor_window_index = None
            _cursor_target_hwnd = None
            _clear_persisted_target()
            print("[server] Cursor 窗口选择: 自动")
            return
        if hwnd is not None:
            _cursor_target_hwnd = int(hwnd)
            _cursor_window_index = None
            title = ""
            for h, t, _ in find_all_cursor_windows():
                if h == _cursor_target_hwnd:
                    title = t
                    break
            _save_persisted_target(_cursor_target_hwnd, title)
            print(f"[server] Cursor 窗口选择: 固定 HWND={_cursor_target_hwnd} '{title[:60]}'")
            return
        _cursor_window_index = max(0, int(index_1based) - 1)
        _cursor_target_hwnd = None
        _clear_persisted_target()
        print(f"[server] Cursor 窗口选择: 固定第 {index_1based} 个 (index={_cursor_window_index})")


def set_cursor_window_index(index_1based=None, auto=False):
    set_cursor_window_target(index_1based=index_1based, auto=auto)


def _target_persist_path():
    return os.path.join(exe_dir(), ".cursor_target.json")


def _load_persisted_target():
    path = _target_persist_path()
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        hwnd = data.get("hwnd")
        if hwnd is not None:
            global _cursor_target_hwnd, _cursor_window_index
            with _window_lock:
                _cursor_target_hwnd = int(hwnd)
                _cursor_window_index = None
            print(f"[server] 已恢复上次选择 HWND={hwnd}: {data.get('title', '')[:60]}")
    except Exception as exc:
        print(f"[server] 读取窗口选择缓存失败: {exc}")


def _save_persisted_target(hwnd, title):
    try:
        with open(_target_persist_path(), "w", encoding="utf-8") as f:
            json.dump({"hwnd": int(hwnd), "title": title}, f, ensure_ascii=False)
    except Exception as exc:
        print(f"[server] 保存窗口选择失败: {exc}")


def _clear_persisted_target():
    path = _target_persist_path()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _init_window_index_from_env():
    raw = os.environ.get("CURSOR_REMOTE_WINDOW_INDEX", "").strip()
    if not raw:
        return
    try:
        n = int(raw)
        if n >= 1:
            set_cursor_window_index(n)
    except ValueError:
        print(f"[server] 无效 CURSOR_REMOTE_WINDOW_INDEX: {raw!r}")


def pick_cursor_hwnd():
    """选择要控制的 Cursor 窗口。固定 HWND > 固定序号 > 标题匹配 > 自动（排除本服务窗口）。"""
    all_windows = find_all_cursor_windows()
    if not all_windows:
        return None

    target_hwnd = get_cursor_target_hwnd()
    if target_hwnd is not None:
        for item in all_windows:
            if item[0] == target_hwnd:
                print(f"[server] 使用指定 HWND={target_hwnd}: '{item[1]}'")
                return item
        print(f"[server] 指定 HWND={target_hwnd} 已关闭，改自动选择")

    idx = get_cursor_window_index()
    if idx is not None:
        if idx < len(all_windows):
            hwnd, title, class_name = all_windows[idx]
            print(f"[server] 使用指定序号 [{idx + 1}/{len(all_windows)}]: '{title}'")
            return hwnd, title, class_name
        print(f"[server] 指定窗口第 {idx + 1} 个不存在 (仅 {len(all_windows)} 个)，改自动选择")

    target_title = os.environ.get("CURSOR_REMOTE_TARGET_TITLE", "").strip().lower()
    if target_title:
        for item in all_windows:
            if target_title in item[1].lower():
                print(f"[server] 标题匹配 '{target_title}': '{item[1]}'")
                return item

    candidates = find_cursor_window(include_excluded=False)
    if not candidates:
        print("[server] 排除后无候选，使用全部 Cursor 窗口")
        candidates = all_windows

    best = None
    best_score = -1
    for hwnd, title, class_name in candidates:
        panel = find_chat_panel_uia(hwnd)
        score = panel["input_width"] if panel else 0
        if score > best_score:
            best_score = score
            best = (hwnd, title, class_name)
    picked = best or candidates[0]
    print(f"[server] 自动选择 Cursor: '{picked[1]}'")
    return picked


def get_target_window_info():
    """返回当前将使用的 Cursor 窗口信息（不触发 UIA 扫描）。"""
    picked = pick_cursor_hwnd()
    if not picked:
        return None
    hwnd, title, _ = picked
    return {
        "hwnd": hwnd,
        "title": title,
        "excluded": _title_is_excluded(title),
    }


def find_chat_panel_uia(hwnd):
    """通过 UI Automation 定位 Cursor Agent 聊天输入框与聊天列区域。"""
    _ensure_com_initialized()
    try:
        root = ua.ControlFromHandle(hwnd)
        if root is None:
            return None
    except Exception as exc:
        print(f"[server] UIA 聊天面板: {exc}")
        return None

    wleft, wtop, wright, wbottom = win32gui.GetWindowRect(hwnd)
    ww, wh = wright - wleft, wbottom - wtop
    bottom_thresh = wtop + int(wh * 0.72)
    right_thresh = wleft + int(ww * 0.35)

    edit_candidates = []
    panel_top = None
    chat_hints = ("new agent", "show chat history", "chat history", "replace agent")

    def walk(ctrl, depth=0):
        nonlocal panel_top
        if depth > 55:
            return
        try:
            ctype = ctrl.ControlTypeName
            rect = ctrl.BoundingRectangle
            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
            name = (ctrl.Name or "").strip()
            nlower = name.lower()

            if ctype == "ButtonControl" and any(h in nlower for h in chat_hints):
                if right > right_thresh:
                    panel_top = top if panel_top is None else min(panel_top, top)

            if ctype == "EditControl" and bottom >= bottom_thresh and left >= right_thresh:
                width = right - left
                height = bottom - top
                if width >= 180 and 18 <= height <= 140:
                    edit_candidates.append((width, bottom, left, top, right))
        except Exception:
            pass
        try:
            child = ctrl.GetFirstChildControl()
            while child:
                walk(child, depth + 1)
                child = child.GetNextSiblingControl()
        except Exception:
            pass

    walk(root)
    if not edit_candidates:
        return None

    edit_candidates.sort(key=lambda item: (-item[0], -item[1]))
    width, _bottom, left, top, right = edit_candidates[0]

    rel_x = left - wleft
    rel_y = top - wtop
    rel_w = right - left
    rel_h = _bottom - top
    col_margin = 80
    chat_top = panel_top - 20 if panel_top else wtop + int(wh * 0.10)

    return {
        "input_rect": (rel_x, rel_y, rel_w, rel_h),
        "input_center": (left + rel_w // 4, top + rel_h // 2),
        "chat_region_screen": (left - col_margin, chat_top, right + col_margin, top - 8),
        "input_width": width,
    }


def _screen_rect_from_window_rect(hwnd, rect):
    """窗口内相对矩形 (x,y,w,h) -> 屏幕 (x,y,w,h)。"""
    if not rect:
        return None
    ix, iy, iw, ih = rect
    wleft, wtop, _, _ = win32gui.GetWindowRect(hwnd)
    return (wleft + ix, wtop + iy, iw, ih)


def _region_from_input_screen(input_rect_screen):
    """由输入框屏幕坐标推导聊天列过滤区域。"""
    if not input_rect_screen:
        return None
    ix, iy, iw, ih = input_rect_screen
    margin = 80
    return (ix - margin, iy - int(ih * 80), ix + iw + margin, iy)


def _log_lines(tag, lines, limit=40):
    """调试：打印行列表摘要。"""
    lines = lines or []
    _safe_print(f"[diff-log] {tag}: {len(lines)} 行")
    for i, ln in enumerate(lines[:limit]):
        _safe_print(f"[diff-log]   [{i}] {ln!r}")
    if len(lines) > limit:
        _safe_print(f"[diff-log]   ... 还有 {len(lines) - limit} 行")


def _prepare_baseline_for_send(baseline, user_message):
    """发送前去掉视口末尾「上一轮助手回复」，避免差量只剩 token 数字。"""
    if not baseline or not user_message:
        return list(baseline or [])
    um = user_message.strip()
    if not um or len(um) > 30:
        return list(baseline)
    out = list(baseline)
    while out and _is_ui_metric_line(out[-1]):
        out.pop()
    if not out:
        return out
    last = out[-1].strip()
    if len(last) > len(um) + 6 and (um in last or last.startswith(um[:2])):
        _safe_print(f"[diff-log] baseline 去掉末尾旧助手行: {last!r}")
        out.pop()
    return out


def _novel_lines_after_baseline(baseline, lines):
    """current 中不在 baseline 集合里、且非 metrics 的行（保持顺序）。"""
    bset = set(baseline or [])
    novel = [
        ln for ln in (lines or [])
        if ln not in bset and not _is_ui_metric_line(ln)
    ]
    return _strip_trailing_metric_lines(novel)


def snapshot_chat_baseline(
    hwnd, input_rect_screen=None, chat_region_screen=None, user_message=None,
):
    """发送前快照基线（仅当前视口文本，不滚动、不截屏）。"""
    global _chat_baseline, _chat_baseline_set, _chat_user_message
    lines, _ = extract_response_text(
        hwnd, input_rect_screen=input_rect_screen, chat_region_screen=chat_region_screen,
    )
    um = (user_message or "").strip() or None
    prepared = _prepare_baseline_for_send(lines or [], um)
    with _chat_lock:
        _chat_baseline = prepared
        _chat_baseline_set = set(_chat_baseline)
        _chat_user_message = um
    print(
        f"[server] 发送前基线: {len(_chat_baseline)} 行 (原始 {len(lines or [])} 行)"
        + (f", 用户消息={_chat_user_message!r}" if _chat_user_message else "")
    )
    _log_lines("baseline_snapshot", _chat_baseline)


def clear_chat_baseline():
    """清空基线，不操作 Cursor 窗口。"""
    global _chat_baseline, _chat_baseline_set, _chat_user_message
    with _chat_lock:
        _chat_baseline = []
        _chat_baseline_set = set()
        _chat_user_message = None
    print("[server] 聊天基线已清空")


def _line_looks_like_user_message(line, user_message):
    """用户气泡行：全等或仅比用户输入略长（标点），不能是助手长回复。"""
    um = (user_message or "").strip()
    s = (line or "").strip()
    if not um or not s:
        return False
    if s == um:
        return True
    # 助手回复可能以相同问候开头但明显更长，例如 你好 -> 你好！有什么可以帮你的？
    if um in s and len(s) <= len(um) + 4:
        return True
    return False


def _strip_trailing_metric_lines(lines):
    out = list(lines or [])
    while out and _is_ui_metric_line(out[-1]):
        out.pop()
    return out


def _strip_leading_user_message(text, user_message):
    """前缀差量结果里去掉紧跟的用户气泡行。"""
    if not text or not user_message:
        return text
    um = user_message.strip()
    if not um:
        return text
    lines = text.splitlines()
    if lines and lines[0].strip() == um:
        return "\n".join(lines[1:])
    return text


def _has_meaningful_text(text):
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return any(not _is_ui_metric_line(ln) for ln in lines)


def _join_lines_filtered(lines):
    return "\n".join(_strip_trailing_metric_lines(lines))


def _tail_after_baseline_prefix(baseline, lines):
    """发送前基线等于 current 前缀时，直接取尾部（最稳定）。"""
    if not lines:
        return None
    bl = len(baseline or [])
    if bl == 0:
        joined = _join_lines_filtered(lines)
        return joined if joined else None
    if len(lines) >= bl and lines[:bl] == baseline:
        new_lines = _strip_trailing_metric_lines(lines[bl:])
        joined = "\n".join(new_lines) if new_lines else ""
        return joined if joined else ""
    return None


def _find_user_message_index(lines, user_message):
    """在聊天行中找到用户刚发送的消息（取最后一次匹配）。"""
    if not user_message or not lines:
        return -1
    um = user_message.strip()
    if not um:
        return -1

    last_idx = -1
    for i, ln in enumerate(lines):
        if ln.strip() == um:
            last_idx = i

    if last_idx >= 0:
        return last_idx

    for i, ln in enumerate(lines):
        if _line_looks_like_user_message(ln, um):
            last_idx = i

    if last_idx >= 0:
        return last_idx

    first_line = um.splitlines()[0].strip()
    if len(first_line) >= 4:
        for i, ln in enumerate(lines):
            s = ln.strip()
            if _line_looks_like_user_message(ln, first_line):
                last_idx = i
            elif first_line in s and len(s) <= len(first_line) + 4:
                last_idx = i
    return last_idx


def _subsequence_end_index(baseline, lines):
    """baseline 作为有序子序列嵌入 lines 时，返回子序列之后的起始下标。"""
    if not baseline:
        return 0
    bi = 0
    end = 0
    for li, ln in enumerate(lines):
        if bi < len(baseline) and ln == baseline[bi]:
            end = li + 1
            bi += 1
    if bi == len(baseline):
        return end
    return -1


def _joined_text_suffix(baseline, lines):
    """整段拼接后做前缀差量（应对行边界变化）。"""
    if not baseline:
        return None
    bj = "\n".join(baseline)
    cj = "\n".join(lines)
    if not bj or not cj.startswith(bj):
        return None
    suffix = cj[len(bj):].lstrip("\n")
    return suffix if suffix else None


def _anchor_after_last_baseline_line(baseline, lines):
    """找 baseline 各行在 lines 中最后一次出现的位置之后。"""
    if not baseline:
        return 0
    baseline_set = set(baseline)
    last = -1
    for i, ln in enumerate(lines):
        if ln in baseline_set:
            last = i
    return last + 1 if last >= 0 else 0


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
    picked = pick_cursor_hwnd()
    if not picked:
        return None, "未找到 Cursor 窗口"

    hwnd, title, class_name = picked
    print(f"[server] 找到窗口: hwnd={hwnd}, title='{title}'")

    uia_panel = find_chat_panel_uia(hwnd)
    input_rect = None
    input_center = None
    send_center = None
    chat_region_screen = None

    if uia_panel:
        input_rect = uia_panel["input_rect"]
        input_center = uia_panel["input_center"]
        chat_region_screen = uia_panel["chat_region_screen"]
        print(f"[server] UIA 输入框: rect={input_rect}, center={input_center}")

    window_img = None
    window_offset = None

    if not input_center:
        window_img, (wx, wy, ww, wh) = capture_window(hwnd)
        window_offset = (wx, wy)
        print(f"[server] 窗口位置: ({wx}, {wy}), 大小: {ww}x{wh}")

        res_dir = bundle_dir()
        input_tpl = os.path.join(res_dir, "input_box.png")
        input_match = find_template_full(
            window_img, window_offset, input_tpl, "input_box", threshold=0.55,
        )
        if input_match:
            r = input_match["rect"]
            input_rect = r
            input_center = (
                window_offset[0] + r[0] + r[2] // 4,
                window_offset[1] + r[1] + r[3] // 4,
            )
            print(f"[server] 模板 输入框中心: {input_center}")

        send_tpl = os.path.join(res_dir, "send_icon.png")
        send_match = find_template_full(
            window_img, window_offset, send_tpl, "send_icon", threshold=0.6,
        )
        if send_match and input_center:
            r = send_match["rect"]
            candidate = (
                window_offset[0] + r[0] + r[2] * 3 // 4,
                window_offset[1] + r[1] + r[3] * 3 // 4,
            )
            dx = abs(candidate[0] - input_center[0])
            dy = abs(candidate[1] - input_center[1])
            if dx < 500 and dy < 200:
                send_center = candidate
                print(f"[server] 发送按钮中心: {send_center}")
            else:
                print(f"[server] 忽略误匹配的发送按钮 {candidate} (距输入框过远)")

    if not input_center:
        return None, "未找到聊天输入框（请打开 Agent 面板并确保聊天框可见）"

    if chat_region_screen is None and input_rect:
        chat_region_screen = _region_from_input_screen(
            _screen_rect_from_window_rect(hwnd, input_rect),
        )

    return {
        "input_center": input_center,
        "send_center": send_center,
        "input_rect": input_rect,
        "chat_region_screen": chat_region_screen,
        "hwnd": hwnd,
        "title": title,
    }, None


TEXT_TYPES = {
    "TextControl", "HyperlinkControl", "ButtonControl", "ListItemControl",
    "EditControl", "DocumentControl", "DataItemControl", "GroupControl",
    "HeaderControl", "HeaderItemControl", "TreeItemControl",
}

_NOISE_LOWER = {
    "new agent (ctrl+n) [alt] replace agent",
    "show chat history",
    "more actions",
    "more actions...",
    "file", "undo all", "keep all", "review",
    "plan, build, / for skills, @ for context",
    "agent", "auto",
    "agents window",
    "toggle agents (ctrl+alt+j)",
    "open cursor settings",
    "close (ctrl+f4)",
    "thought",
}
_NOISE_RE = re.compile(
    r"^(~[/\\]|for\s+\d+\s*s$|copy\b|retry\b|apply\b|reject\b)", re.IGNORECASE,
)
_FILENAME_RE = re.compile(
    r"^[\w.\-]+\.(cpp|py|h|hpp|md|txt|yaml|yml|json|sh|js|ts|launch|xml|csv|urdf|srdf)$"
)
_METRIC_LINE_RE = re.compile(r"^\d{1,4}$")


def _is_ui_metric_line(t):
    """Cursor 聊天区 token/耗时等纯数字行。"""
    return bool(_METRIC_LINE_RE.match((t or "").strip()))


def _is_noise(t):
    if t.lower() in _NOISE_LOWER:
        return True
    if _NOISE_RE.match(t):
        return True
    if _FILENAME_RE.match(t):
        return True
    return False


def _rect_tuple(ctrl):
    try:
        r = ctrl.BoundingRectangle
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def _ctrl_texts(ctrl):
    """返回 (ControlTypeName, [该控件可见文本...])。"""
    try:
        ctype = ctrl.ControlTypeName
    except Exception:
        ctype = ""
    name = ""
    try:
        name = (ctrl.Name or "").strip()
    except Exception:
        name = ""
    value = ""
    if ctype in ("EditControl", "DocumentControl"):
        try:
            value = (ctrl.Value or "").strip()
        except Exception:
            value = ""
    texts = []
    if name:
        texts.append(name)
    if value:
        texts.append(value)
    return ctype, texts


def _collect_chat_lines(root, region):
    """遍历 UIA 树，收集聊天区域内的文本，按视觉顺序返回去重后的 list[str]。

    region = (y_top, y_limit, col_x1, col_x2)，任一可为 None。
    策略：
      - 用元素中心点过滤区域；
      - 收集每个文本控件的 Name/Value；
      - 按屏幕坐标 (top, left) 排序，保证阅读顺序且跨调用稳定；
      - 子串去重：丢弃「作为其它文本严格子串」的碎片，消除父子控件拼接重复。
    """
    y_top, y_limit, col_x1, col_x2 = region

    def in_region(rt):
        if not rt:
            return False
        l, t, r, b = rt
        if r <= l or b <= t:
            return False
        cx = (l + r) // 2
        cy = (t + b) // 2
        if y_top is not None and cy < y_top:
            return False
        if y_limit is not None and cy >= y_limit:
            return False
        if col_x1 is not None and (cx < col_x1 or cx > col_x2):
            return False
        return True

    raw = []  # (top, left, text)

    def walk(ctrl, depth=0):
        if depth > 60:
            return
        ctype, texts = _ctrl_texts(ctrl)
        rt = _rect_tuple(ctrl)
        if ctype in TEXT_TYPES and in_region(rt):
            for t in texts:
                if t and len(t) >= 1 and not _is_noise(t):
                    raw.append((rt[1], rt[0], t))
        try:
            child = ctrl.GetFirstChildControl()
        except Exception:
            child = None
        while child is not None:
            walk(child, depth + 1)
            try:
                child = child.GetNextSiblingControl()
            except Exception:
                child = None

    try:
        walk(root)
    except Exception:
        return []

    raw.sort(key=lambda e: (e[0], e[1]))

    all_texts = [e[2] for e in raw]
    out = []
    seen = set()
    for _, _, t in raw:
        if t in seen:
            continue
        # 丢弃严格子串（父控件的完整拼接串已包含这些子片段）
        if any(t != o and len(o) > len(t) and t in o for o in all_texts):
            continue
        seen.add(t)
        out.append(t)
    return out


def extract_response_text(hwnd, input_rect_screen=None, chat_region_screen=None):
    """通过 UI Automation 遍历 Cursor 窗口的辅助功能树，提取聊天回复区域的文本内容。

    返回 (lines, err)：成功时 err 为 None。
    实际收集逻辑见 _collect_chat_lines（区域过滤 + 按坐标排序 + 子串去重）。
    """
    _ensure_com_initialized()
    try:
        root = ua.ControlFromHandle(hwnd)
        if root is None:
            return None, "无法获取窗口的 UI Automation 对象"
    except Exception as e:
        return None, f"UIA 初始化失败: {e}"

    y_top = y_limit = col_x1 = col_x2 = None
    if chat_region_screen:
        col_x1, y_top, col_x2, y_limit = chat_region_screen
    elif input_rect_screen:
        ix, iy, iw, ih = input_rect_screen
        col_margin = 80
        col_x1 = ix - col_margin
        col_x2 = ix + iw + col_margin
        y_top = iy - int(ih * 80)
        y_limit = iy

    lines = _collect_chat_lines(root, (y_top, y_limit, col_x1, col_x2))
    if not lines:
        return None, "未提取到任何文本（可能 Cursor 辅助功能未启用，可尝试 --force-renderer-accessibility）"
    return lines, None


def _text_scroll_enabled():
    """回复完成后向上滚动合并全文（仅文本，不额外截图）。默认关闭，易误读历史。"""
    v = os.environ.get("CURSOR_REMOTE_TEXT_SCROLL", "0").lower()
    return v not in ("0", "false", "no")


def _merge_scroll_batches(all_batches):
    """多次向上滚动采集的批次按阅读顺序合并（越往后滚动的批次内容越靠上）。"""
    merged = []
    seen = set()
    for batch in reversed(all_batches):
        for ln in batch:
            if ln not in seen:
                seen.add(ln)
                merged.append(ln)
    return merged


def extract_response_text_full(
    hwnd, input_rect_screen=None, chat_region_screen=None, max_scrolls=20,
):
    """读取聊天文本；默认滚动合并全文，CURSOR_REMOTE_TEXT_SCROLL=0 则仅当前视口。"""
    if _text_scroll_enabled():
        return _extract_response_text_scrolling(
            hwnd, input_rect_screen, chat_region_screen, max_scrolls=max_scrolls,
        )
    return extract_response_text(
        hwnd, input_rect_screen=input_rect_screen, chat_region_screen=chat_region_screen,
    )


def _extract_response_text_scrolling(
    hwnd, input_rect_screen=None, chat_region_screen=None, max_scrolls=None,
):
    """向上滚动聊天区，合并各视口 UIA 行（结束后滚回底部）。"""
    if max_scrolls is None:
        max_scrolls = int(os.environ.get("CURSOR_REMOTE_TEXT_MAX_SCROLLS", "35"))
    focus_chat_panel(chat_region_screen)
    all_batches = []
    seen = set()
    stagnant = 0
    last_lines = None
    last_err = None
    scrolls_done = 0

    for scroll_i in range(max_scrolls):
        lines, err = extract_response_text(
            hwnd, input_rect_screen=input_rect_screen, chat_region_screen=chat_region_screen,
        )
        last_err = err
        last_lines = lines
        if lines:
            if any(ln not in seen for ln in lines):
                all_batches.append(list(lines))
                for ln in lines:
                    seen.add(ln)
                stagnant = 0
            else:
                stagnant += 1
        else:
            stagnant += 1

        if stagnant >= 2 and scroll_i > 0:
            break

        if scroll_i < max_scrolls - 1:
            mouse_wheel(3)
            scrolls_done += 1
            time.sleep(0.35)

    if scrolls_done:
        for _ in range(scrolls_done):
            mouse_wheel(-3)
            time.sleep(0.12)

    merged = _merge_scroll_batches(all_batches)
    if merged:
        print(
            f"[server] 滚动采集: {scrolls_done} 次, {len(all_batches)} 批, "
            f"共 {len(merged)} 行"
        )
        return merged, None
    return last_lines, last_err


def _content_signature(lines):
    if not lines:
        return (0, 0, "")
    tail = "\n".join(lines[-4:])
    return (len(lines), sum(len(x) for x in lines), tail)


def _wait_for_response_text(
    hwnd, input_rect_screen, chat_region_screen, wait_seconds=8,
):
    """轮询聊天文本，内容连续稳定或达到上限后返回。"""
    min_wait = max(int(wait_seconds), 5)
    max_total = min_wait + 50
    poll_interval = 1.0
    stable_needed = 2.5

    start = time.time()
    deadline = start + max_total
    lines = []
    last_sig = None
    stable_since = None

    while time.time() < deadline:
        new_lines, terr = extract_response_text(
            hwnd,
            input_rect_screen=input_rect_screen,
            chat_region_screen=chat_region_screen,
        )
        new_lines = new_lines or []
        if terr and not new_lines:
            print(f"[server] 轮询提示: {terr}")

        sig = _content_signature(new_lines)
        now = time.time()
        elapsed = now - start

        if sig != last_sig:
            lines = new_lines
            last_sig = sig
            stable_since = None
        elif stable_since is None:
            stable_since = now

        if elapsed >= min_wait and stable_since is not None:
            if now - stable_since >= stable_needed:
                print(
                    f"[server] 回复稳定 {stable_needed}s, 耗时 {elapsed:.1f}s, "
                    f"{len(lines)} 行"
                )
                break

        if elapsed >= min_wait + 8 and sig[0] == 0:
            print(f"[server] 未读到文本，{elapsed:.1f}s 后结束等待")
            break

        time.sleep(poll_interval)

    if time.time() >= deadline:
        print(f"[server] 等待达到上限 {max_total}s, 返回当前结果")
    return lines


def crop_chat_from_window(window_img, wx, wy, chat_region_screen):
    h, w = window_img.shape[:2]
    if not chat_region_screen:
        return window_img[:int(h * 0.7), int(w * 0.45):]
    x1, y1, x2, y2 = chat_region_screen
    crop_x1 = max(0, x1 - wx)
    crop_y1 = max(0, y1 - wy)
    crop_x2 = min(w, x2 - wx)
    crop_y2 = min(h, y2 - wy)
    if crop_y2 <= crop_y1 or crop_x2 <= crop_x1:
        return window_img[:int(h * 0.7), int(w * 0.45):]
    return window_img[crop_y1:crop_y2, crop_x1:crop_x2]


def encode_image_b64(img_bgr, quality=72):
    _, buf = cv2.imencode('.jpg', img_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf).decode('utf-8')


def capture_chat_image(hwnd, chat_region_screen):
    """截取聊天列当前可见区域（单张）。"""
    window_img, (wx, wy, _, _) = capture_window(hwnd)
    crop = crop_chat_from_window(window_img, wx, wy, chat_region_screen)
    return encode_image_b64(crop)


def init_chat_baseline():
    """从当前视口读取基线（不滚动、不截屏）。仅 /reset-baseline 等显式调用时使用。"""
    global _chat_baseline, _chat_baseline_set, _chat_user_message
    try:
        picked = pick_cursor_hwnd()
        if not picked:
            print("[server] 基线初始化：未找到 Cursor 窗口")
            return
        hwnd = picked[0]
        panel = find_chat_panel_uia(hwnd)
        if not panel:
            print("[server] 基线初始化：未找到聊天输入框")
            return
        input_rect_screen = _screen_rect_from_window_rect(hwnd, panel.get("input_rect"))
        chat_region_screen = panel.get("chat_region_screen")
        lines, terr = extract_response_text(
            hwnd, input_rect_screen=input_rect_screen, chat_region_screen=chat_region_screen,
        )
        with _chat_lock:
            _chat_baseline = lines or []
            _chat_baseline_set = set(_chat_baseline)
            _chat_user_message = None
        print(f"[server] 基线初始化完成，基线行数: {len(_chat_baseline)}")
        if terr:
            print(f"[server] 基线初始化提示: {terr}")
    except Exception as e:
        print(f"[server] 基线初始化失败: {e}")


def diff_against_baseline(lines):
    """计算相对发送前基线的新增内容（本次用户消息之后的 AI 回复）。

    优先级：
      1. 用户消息锚点 — 取用户气泡之后的行（排除误匹配助手长回复）
      2. 前缀尾部 — baseline 为 current 前缀时取 lines[len(baseline):]
      3. 拼接前缀 — 行边界变化时长回复更稳
      4. 有序子序列 — baseline 完整嵌入 current 时取尾部
      5. 末锚点 — 兜底
      6. 新增行集合 — 仅视口碎片时最后手段
      7. 行前缀 LCP — 兜底
    """
    if not lines:
        return ""

    with _chat_lock:
        baseline = list(_chat_baseline) if _chat_baseline else []
        user_msg = _chat_user_message

    _log_lines("diff_current", lines)
    _log_lines("diff_baseline", baseline)
    _safe_print(f"[diff-log] user_message={user_msg!r}")

    # 1) 用户消息锚点
    if user_msg:
        uidx = _find_user_message_index(lines, user_msg)
        _safe_print(f"[diff-log] user_anchor_index={uidx}")
        if uidx >= 0:
            anchor_line = lines[uidx] if uidx < len(lines) else ""
            _safe_print(f"[diff-log] user_anchor_line={anchor_line!r}")
            new_lines = _strip_trailing_metric_lines(lines[uidx + 1:])
            joined = "\n".join(new_lines) if new_lines else ""
            if _has_meaningful_text(joined):
                _safe_print(f"[diff-log] method=user_anchor rows={len(new_lines)}")
                return joined
            _safe_print("[diff-log] user_anchor 结果无有效文本，尝试其它策略")

    # 2) 前缀尾部（发送前后行序不变时最准）
    prefix_tail = _tail_after_baseline_prefix(baseline, lines)
    if prefix_tail is not None and _has_meaningful_text(prefix_tail):
        prefix_tail = _strip_leading_user_message(prefix_tail, user_msg)
        _safe_print(f"[diff-log] method=prefix_tail len={len(prefix_tail)} text={prefix_tail[:120]!r}")
        return prefix_tail
    _safe_print(f"[diff-log] prefix_tail={prefix_tail!r}")

    # 3) 拼接前缀（行边界变化时长回复更稳）
    suffix = _joined_text_suffix(baseline, lines)
    if suffix and _has_meaningful_text(suffix):
        suffix = _strip_leading_user_message(suffix, user_msg)
        suffix = "\n".join(_strip_trailing_metric_lines(suffix.splitlines()))
        _safe_print(f"[diff-log] method=joined_prefix len={len(suffix)}")
        return suffix

    if not baseline:
        joined = _join_lines_filtered(lines)
        _safe_print(f"[diff-log] method=no_baseline_all len={len(joined)}")
        return joined

    # 4) 有序子序列
    end = _subsequence_end_index(baseline, lines)
    _safe_print(f"[diff-log] subsequence_end={end}")
    if end > 0:
        new_lines = _strip_trailing_metric_lines(lines[end:])
        joined = "\n".join(new_lines) if new_lines else ""
        if _has_meaningful_text(joined):
            joined = _strip_leading_user_message(joined, user_msg)
            _safe_print(f"[diff-log] method=subsequence rows={len(new_lines)}")
            return joined

    # 5) 末锚点
    anchor = _anchor_after_last_baseline_line(baseline, lines)
    _safe_print(f"[diff-log] last_anchor={anchor}")
    if anchor < len(lines):
        new_lines = _strip_trailing_metric_lines(lines[anchor:])
        joined = "\n".join(new_lines) if new_lines else ""
        if _has_meaningful_text(joined):
            _safe_print(f"[diff-log] method=last_anchor rows={len(new_lines)}")
            return joined

    # 6) 新增行（视口碎片兜底，顺序可能不完整）
    novel = _novel_lines_after_baseline(baseline, lines)
    if novel:
        joined = "\n".join(novel)
        _safe_print(f"[diff-log] method=novel_lines rows={len(novel)} text={joined[:120]!r}")
        return joined

    # 7) 行前缀 LCP
    n = min(len(baseline), len(lines))
    i = 0
    while i < n and baseline[i] == lines[i]:
        i += 1
    new_lines = _strip_trailing_metric_lines(lines[i:])
    new_lines = [ln for ln in new_lines if not _is_ui_metric_line(ln)]
    joined = "\n".join(new_lines) if new_lines else ""
    joined = _strip_leading_user_message(joined, user_msg)
    _safe_print(f"[diff-log] method=lcp @行{i} rows={len(new_lines)} result={joined[:120]!r}")
    return joined


def capture_response(hwnd, input_rect, wait_seconds=8, chat_region_screen=None):
    input_rect_screen = _screen_rect_from_window_rect(hwnd, input_rect)
    if chat_region_screen is None:
        chat_region_screen = _region_from_input_screen(input_rect_screen)

    time.sleep(1.5)

    lines = _wait_for_response_text(
        hwnd, input_rect_screen, chat_region_screen, wait_seconds=wait_seconds,
    )

    if _text_scroll_enabled():
        merged, terr = _extract_response_text_scrolling(
            hwnd,
            input_rect_screen=input_rect_screen,
            chat_region_screen=chat_region_screen,
        )
        if merged:
            lines = merged
        if terr and not merged:
            print(f"[server] 滚动采集提示: {terr}")

    resp_text = diff_against_baseline(lines)

    img_b64 = None
    if os.environ.get("CURSOR_REMOTE_SCREENSHOT", "1").lower() not in ("0", "false", "no"):
        try:
            img_b64 = capture_chat_image(hwnd, chat_region_screen)
        except Exception as exc:
            print(f"[server] 截图失败: {exc}")

    print(f"[server] 增量回复长度: {len(resp_text)} 字符, {len(lines or [])} 行")
    if resp_text:
        preview = resp_text if len(resp_text) <= 2000 else resp_text[:2000] + "\n...(截断打印)"
        _safe_print("---------- 增量回复开始 ----------")
        _safe_print(preview)
        _safe_print("---------- 增量回复结束 ----------")
    return img_b64, resp_text


def get_remote_backend():
    """sdk = 官方 Cursor SDK；uia = 模拟点击 + UIA 读聊天区。"""
    raw = os.environ.get("CURSOR_REMOTE_BACKEND", "").strip().lower()
    if raw in ("sdk", "uia"):
        return raw
    if _sdk_backend and _sdk_backend.api_key_configured():
        return "sdk"
    return "uia"


def send_to_cursor_sdk(text, wait_seconds=8):
    del wait_seconds  # SDK 自行等待 Agent 完成
    if _sdk_backend is None:
        return False, "未安装 cursor-sdk（pip install cursor-sdk）", None, None, None, None
    cwd = _sdk_backend.workspace_cwd()
    mode = _sdk_backend.sdk_mode()
    print(f"[server] SDK 模式发送 ({mode}), cwd={cwd}")
    reply, err = _sdk_backend.send_prompt(text)
    label = f"Cursor SDK ({cwd})"
    if err:
        return False, err, None, None, None, label
    return True, "已通过 SDK 获取回复", None, reply, None, label


def send_to_cursor(text, wait_seconds=8, image_data=None):
    if get_remote_backend() == "sdk":
        if image_data:
            return False, "SDK 模式暂不支持图片", None, None, None, "Cursor SDK"
        return send_to_cursor_sdk(text, wait_seconds=wait_seconds)

    result, err = scan_cursor_ui()
    if err:
        return False, err, None, None, None, None

    if not result["input_center"]:
        return False, "未找到聊天输入框", None, None, None, None

    hwnd = result["hwnd"]
    target_title = result.get("title") or ""
    input_rect_screen = _screen_rect_from_window_rect(hwnd, result.get("input_rect"))
    chat_region_screen = result.get("chat_region_screen")
    snapshot_chat_baseline(
        hwnd, input_rect_screen, chat_region_screen, user_message=text,
    )

    cx, cy = result["input_center"]
    mouse_move_to(cx, cy)
    time.sleep(0.3)
    mouse_click()
    time.sleep(0.3)

    if image_data:
        uploads_dir = os.path.join(exe_dir(), "uploads")
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
    try:
        img_b64, resp_text = capture_response(
            hwnd, result["input_rect"], wait_seconds, chat_region_screen=chat_region_screen,
        )
    except Exception as exc:
        print(f"[server] 获取响应失败: {exc}")
        return False, f"获取响应失败: {exc}", None, None, hwnd, target_title
    return True, "已发送", img_b64, resp_text, hwnd, target_title


def capture_cursor_response_only(wait_seconds=0):
    """仅重新截取 Cursor 响应区域，不发送消息（用于首次等待不够时再读一次）。"""
    result, err = scan_cursor_ui()
    if err:
        return False, err, None, None, None, None
    print(f"[server] 仅截取响应，等待 {wait_seconds} 秒...")
    img_b64, resp_text = capture_response(
        result["hwnd"],
        result["input_rect"],
        wait_seconds,
        chat_region_screen=result.get("chat_region_screen"),
    )
    return True, "已获取返回", img_b64, resp_text, result["hwnd"], result.get("title")


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
    max-height: none;
    display: flex;
    flex-direction: column;
    gap: 8px;
    background: #16213e;
    border-radius: 8px;
    border: 1px solid #0f3460;
    padding: 8px;
    overflow: auto;
    max-height: 55vh;
}
.response-img {
    max-width: 100%;
    width: auto;
    height: auto;
    object-fit: contain;
    border-radius: 4px;
    border: 1px solid #0f3460;
    cursor: pointer;
    display: block;
}
.response-img-label {
    font-size: 11px;
    color: #888;
    margin-top: 4px;
}
.response-text-wrap {
    max-height: 55vh;
    overflow: auto;
    background: #0f1a2e;
    border: 1px solid #0f3460;
    border-radius: 8px;
    margin-bottom: 6px;
}
.response-text {
    white-space: pre-wrap;
    word-break: break-word;
    font-family: "Consolas", "Menlo", monospace;
    font-size: 13px;
    line-height: 1.5;
    color: #d6e6ff;
    padding: 10px;
    margin: 0;
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
.window-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 8px;
    flex-wrap: wrap;
}
.window-row label {
    font-size: 13px;
    color: #aaa;
    white-space: nowrap;
}
.window-row select {
    flex: 1;
    min-width: 120px;
    padding: 8px;
    background: #16213e;
    color: #eee;
    border: 1px solid #0f3460;
    border-radius: 6px;
    font-size: 13px;
}
.btn-refresh {
    flex: 0 0 auto;
    padding: 8px 12px;
    font-size: 13px;
    background: #2a2a4e;
    color: #a8c8ff;
    border: 1px solid #0f3460;
    border-radius: 6px;
    cursor: pointer;
}
.settings-panel {
    background: #16213e;
    border: 1px solid #0f3460;
    border-radius: 8px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.settings-panel h3 {
    font-size: 0.85em;
    color: #888;
    margin: 0;
}
.settings-panel select,
.settings-panel input {
    width: 100%;
    padding: 8px;
    background: #0f1a30;
    color: #eee;
    border: 1px solid #0f3460;
    border-radius: 6px;
    font-size: 13px;
}
.settings-hint {
    font-size: 12px;
    color: #7a8aa0;
    line-height: 1.5;
}
.btn-save-config {
    padding: 8px 12px;
    font-size: 13px;
    background: #1a3a2e;
    color: #2ecc71;
    border: 1px solid #1a5a3e;
    border-radius: 6px;
    cursor: pointer;
    align-self: flex-start;
}
</style>
</head>
<body>
<h1>Cursor Remote Control</h1>
<div class="container">
    <div class="settings-panel">
        <h3>连接模式</h3>
        <select id="backendMode" onchange="onBackendModeChange()">
            <option value="sdk-local">本地 SDK（分析本机目录）</option>
            <option value="sdk-cloud">云端 SDK（Cursor 云 Agent）</option>
            <option value="uia">控制 IDE 窗口（SSH 远程推荐）</option>
        </select>
        <div id="sdkCwdRow">
            <label for="sdkCwd" style="font-size:12px;color:#aaa">SDK 工作目录</label>
            <input id="sdkCwd" type="text" placeholder="本机: d:\project  远程机: /home/user/project">
            <button type="button" class="btn-save-config" onclick="saveConfig()">保存目录</button>
        </div>
        <div class="settings-hint" id="modeHint"></div>
    </div>
    <div class="window-row" id="windowRow">
        <label for="cursorWindow">目标 Cursor:</label>
        <select id="cursorWindow" onchange="onWindowChange()"></select>
        <button type="button" class="btn-refresh" onclick="loadWindows()">刷新</button>
    </div>
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
        <div class="response-text-wrap" id="responseTextWrap" style="display:none;width:100%">
            <pre class="response-text" id="responseText"></pre>
        </div>
        <div class="response-img-wrap" id="responseImages"></div>
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
var WAIT_SECONDS = 20;
var SERVER_MAX_EXTRA = 50;
var currentPhotoBase64 = null;

var MODE_HINTS = {
    'sdk-local': '本地 SDK：Agent 在你电脑上跑，读取下方「工作目录」里的代码。适合本机项目。',
    'sdk-cloud': '云端 SDK：在 Cursor 云端 VM 运行，需开启 Usage-based pricing。不读你 SSH 里的文件。',
    'uia': '控制 IDE：模拟操作你眼前的 Cursor 窗口。开发 SSH 远程时选标题含 [SSH: IP] 的窗口。'
};

function backendModeValue() {
    return document.getElementById('backendMode').value;
}

function onBackendModeChange() {
    var mode = backendModeValue();
    document.getElementById('modeHint').textContent = MODE_HINTS[mode] || '';
    var isUia = mode === 'uia';
    document.getElementById('windowRow').style.display = isUia ? 'flex' : 'none';
    document.getElementById('sdkCwdRow').style.display = isUia ? 'none' : 'flex';
    document.getElementById('sdkCwdRow').style.flexDirection = 'column';
    document.getElementById('sdkCwdRow').style.gap = '6px';
    saveConfig(true);
}

function loadConfig() {
    fetch('/config')
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (!d.ok) return;
        var sel = document.getElementById('backendMode');
        if (d.backend === 'uia') {
            sel.value = 'uia';
        } else if (d.sdk_mode === 'cloud') {
            sel.value = 'sdk-cloud';
        } else {
            sel.value = 'sdk-local';
        }
        if (d.sdk_cwd) {
            document.getElementById('sdkCwd').value = d.sdk_cwd;
        }
        onBackendModeChange();
        if (d.backend !== 'uia') {
            document.getElementById('modeHint').textContent +=
                ' 当前目录: ' + (d.sdk_cwd || '(未设置)');
        }
    })
    .catch(function() {});
}

function saveConfig(silent) {
    var mode = backendModeValue();
    var body = { sdk_cwd: document.getElementById('sdkCwd').value.trim() };
    if (mode === 'uia') {
        body.backend = 'uia';
    } else {
        body.backend = 'sdk';
        body.sdk_mode = mode === 'sdk-cloud' ? 'cloud' : 'local';
    }
    return fetch('/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (!silent && d.ok) {
            setStatus(d.msg || '配置已保存', 'ok');
        }
        return d;
    });
}

function loadWindows() {
    fetch('/windows')
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (!d.ok) return;
        var sel = document.getElementById('cursorWindow');
        sel.innerHTML = '';
        var auto = document.createElement('option');
        auto.value = '0';
        auto.textContent = '自动（排除本服务窗口）';
        sel.appendChild(auto);
        (d.windows || []).forEach(function(w) {
            var opt = document.createElement('option');
            opt.value = String(w.hwnd);
            var short = w.title.length > 48 ? w.title.substring(0, 48) + '…' : w.title;
            var tag = w.excluded ? ' [已排除]' : '';
            opt.textContent = w.index + '. ' + short + tag;
            if (w.excluded) {
                opt.style.color = '#888';
            }
            sel.appendChild(opt);
        });
        var needSync = false;
        if (d.selected_hwnd) {
            sel.value = String(d.selected_hwnd);
        } else {
            var first = (d.windows || []).find(function(w) { return !w.excluded; });
            if (first) {
                sel.value = String(first.hwnd);
                needSync = true;
            } else {
                sel.value = '0';
            }
        }
        if (needSync) onWindowChange();
    })
    .catch(function() {});
}

function onWindowChange() {
    var sel = document.getElementById('cursorWindow');
    var v = sel.value;
    var body = v === '0' ? {auto: true} : {hwnd: parseInt(v, 10)};
    fetch('/select-window', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.ok) {
            setStatus(d.msg, 'ok');
        }
    });
}

loadWindows();
loadConfig();

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
    setStatus('正在发送，等待 Cursor 响应（约 ' + (WAIT_SECONDS + SERVER_MAX_EXTRA) + ' 秒内）...', 'loading');

    document.getElementById('responseArea').style.display = 'none';
    document.getElementById('waitBar').className = 'wait-bar active';
    startProgress(WAIT_SECONDS + SERVER_MAX_EXTRA);

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
            var tip = d.msg;
            if (d.target_title) {
                tip += ' → ' + d.target_title.substring(0, 40);
            }
            if (!d.text && !d.image) {
                tip += '（无文本/截图，可点「获取返回」）';
            }
            setStatus(tip, d.text || d.image ? 'ok' : 'err');
            addHistory(msg, currentPhotoBase64);
            document.getElementById('msg').value = '';
            removePhoto();
            if (d.image || (d.images && d.images.length)) {
                showResponseImages(d.images, d.image);
                document.getElementById('responseArea').style.display = 'flex';
            }
            showResponseText(d.text, d.text_len);
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
            if (d.ok && (d.image || (d.images && d.images.length))) {
                setStatus(d.msg, 'ok');
                showResponseImages(d.images, d.image);
                document.getElementById('responseArea').style.display = 'flex';
            } else if (d.ok) {
                setStatus(d.msg || '未得到图片', 'err');
            } else {
                setStatus('失败: ' + d.msg, 'err');
            }
            showResponseText(d.text, d.text_len);
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

function showResponseImages(imageList, primary) {
    var wrap = document.getElementById('responseImages');
    wrap.innerHTML = '';
    var imgs = (imageList && imageList.length) ? imageList : (primary ? [primary] : []);
    if (!imgs.length) return;
    imgs.forEach(function(b64, idx) {
        if (imgs.length > 1) {
            var label = document.createElement('div');
            label.className = 'response-img-label';
            label.textContent = '截图 ' + (idx + 1) + ' / ' + imgs.length
                + (idx === 0 ? '（最新）' : '（向上滚动）');
            wrap.appendChild(label);
        }
        var img = document.createElement('img');
        img.className = 'response-img';
        img.src = 'data:image/jpeg;base64,' + b64;
        img.alt = 'response ' + (idx + 1);
        img.onclick = function() { openFull(this); };
        wrap.appendChild(img);
    });
}

function showResponseText(text, textLen) {
    var wrap = document.getElementById('responseTextWrap');
    var el = document.getElementById('responseText');
    if (text) {
        el.textContent = text;
        wrap.style.display = 'block';
        document.getElementById('responseArea').style.display = 'flex';
        var header = document.querySelector('#responseArea h3');
        if (header) {
            var n = textLen || text.length;
            header.textContent = 'Cursor 响应 (' + n + ' 字)';
        }
    } else {
        wrap.style.display = 'none';
        el.textContent = '';
        var header2 = document.querySelector('#responseArea h3');
        if (header2) header2.textContent = 'Cursor 响应';
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

    try:
        ok, msg, img_b64, resp_text, target_hwnd, target_title = send_to_cursor(
            text, wait_seconds=wait, image_data=image_data,
        )
    except Exception as exc:
        print(f"[server] /send 异常: {exc}")
        return jsonify({"ok": False, "msg": f"服务异常: {exc}"})

    resp = {"ok": ok, "msg": msg, "backend": get_remote_backend()}
    if target_hwnd:
        resp["target_hwnd"] = target_hwnd
    if target_title:
        resp["target_title"] = target_title
    if img_b64:
        resp["image"] = img_b64
    if resp_text:
        resp["text"] = resp_text
        resp["text_len"] = len(resp_text)
    elif ok:
        resp["msg"] = msg + "（未读到文本，可点「获取返回」重试）"
    return jsonify(resp)


@app.route('/capture', methods=['POST'])
def handle_capture():
    data = request.get_json(silent=True) or {}
    try:
        wait = int(data.get('wait', 0))
    except (TypeError, ValueError):
        wait = 0
    ok, msg, img_b64, resp_text, target_hwnd, target_title = capture_cursor_response_only(
        wait_seconds=max(0, wait),
    )
    resp = {"ok": ok, "msg": msg}
    if target_hwnd:
        resp["target_hwnd"] = target_hwnd
    if target_title:
        resp["target_title"] = target_title
    if img_b64:
        resp["image"] = img_b64
    if resp_text:
        resp["text"] = resp_text
        resp["text_len"] = len(resp_text)
    return jsonify(resp)


@app.route('/target', methods=['GET'])
def handle_target():
    info = get_target_window_info()
    if not info:
        return jsonify({"ok": False, "msg": "未找到 Cursor 窗口"})
    return jsonify({"ok": True, **info})


@app.route('/backend', methods=['GET'])
def handle_backend():
    backend = get_remote_backend()
    payload = {
        "ok": True,
        "backend": backend,
        "api_key_set": bool(_sdk_backend and _sdk_backend.api_key_configured()),
    }
    if _sdk_backend:
        payload["sdk_cwd"] = _sdk_backend.workspace_cwd()
        payload["sdk_model"] = _sdk_backend.model_name()
        payload["sdk_mode"] = _sdk_backend.sdk_mode()
    return jsonify(payload)


def _config_payload():
    payload = {
        "ok": True,
        "backend": get_remote_backend(),
        "api_key_set": bool(_sdk_backend and _sdk_backend.api_key_configured()),
    }
    if _sdk_backend:
        payload["sdk_cwd"] = _sdk_backend.workspace_cwd()
        payload["sdk_mode"] = _sdk_backend.sdk_mode()
        payload["sdk_model"] = _sdk_backend.model_name()
    return payload


@app.route('/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'GET':
        return jsonify(_config_payload())

    data = request.get_json(force=True) or {}
    changed = []
    if "backend" in data:
        b = str(data["backend"]).strip().lower()
        if b in ("sdk", "uia"):
            os.environ["CURSOR_REMOTE_BACKEND"] = b
            changed.append("backend")
    if "sdk_mode" in data:
        m = str(data["sdk_mode"]).strip().lower()
        if m in ("local", "cloud"):
            os.environ["CURSOR_SDK_MODE"] = m
            changed.append("sdk_mode")
    if "sdk_cwd" in data:
        cwd = str(data["sdk_cwd"]).strip()
        if cwd:
            os.environ["CURSOR_SDK_CWD"] = cwd
            changed.append("sdk_cwd")
    if changed and _sdk_backend and any(x in changed for x in ("sdk_mode", "sdk_cwd")):
        _sdk_backend.reset_agent()
    out = _config_payload()
    out["msg"] = "配置已保存" if changed else "无变更"
    out["changed"] = changed
    return jsonify(out)


@app.route('/sdk/reset', methods=['POST'])
def handle_sdk_reset():
    if _sdk_backend is None:
        return jsonify({"ok": False, "msg": "未安装 cursor-sdk"})
    _sdk_backend.reset_agent()
    return jsonify({"ok": True, "msg": "SDK 多轮会话已重置"})


@app.route('/windows', methods=['GET'])
def handle_list_windows():
    all_windows = find_all_cursor_windows()
    patterns = _exclude_title_patterns()
    idx = get_cursor_window_index()
    selected_hwnd = get_cursor_target_hwnd()
    items = [
        {
            "index": i + 1,
            "hwnd": hwnd,
            "title": title,
            "excluded": _title_is_excluded(title, patterns),
        }
        for i, (hwnd, title, _) in enumerate(all_windows)
    ]
    return jsonify({
        "ok": True,
        "count": len(items),
        "windows": items,
        "selected_index": (idx + 1) if idx is not None else None,
        "selected_hwnd": selected_hwnd,
        "exclude_patterns": patterns,
        "mode": "fixed" if (idx is not None or selected_hwnd is not None) else "auto",
    })


@app.route('/select-window', methods=['POST'])
def handle_select_window():
    data = request.get_json(force=True) or {}
    if data.get("auto"):
        set_cursor_window_target(auto=True)
        clear_chat_baseline()
        return jsonify({"ok": True, "msg": "已切换为自动选择（排除本服务窗口）"})

    hwnd = data.get("hwnd")
    if hwnd is not None:
        try:
            hwnd = int(hwnd)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "msg": "hwnd 无效"})
        all_windows = find_all_cursor_windows()
        match = next((w for w in all_windows if w[0] == hwnd), None)
        if not match:
            return jsonify({"ok": False, "msg": f"未找到 HWND={hwnd} 的 Cursor 窗口"})
        set_cursor_window_target(hwnd=hwnd)
        clear_chat_baseline()
        return jsonify({
            "ok": True,
            "msg": f"已选择: {match[1][:60]}",
            "hwnd": hwnd,
            "title": match[1],
        })

    try:
        index = int(data.get("index", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "index 无效"})
    if index < 1:
        return jsonify({"ok": False, "msg": "index 从 1 开始"})
    all_windows = find_all_cursor_windows()
    if index > len(all_windows):
        return jsonify({
            "ok": False,
            "msg": f"只有 {len(all_windows)} 个 Cursor 窗口，无法选择第 {index} 个",
        })
    hwnd, title, _ = all_windows[index - 1]
    set_cursor_window_target(hwnd=hwnd)
    clear_chat_baseline()
    return jsonify({
        "ok": True,
        "msg": f"已选择第 {index} 个: {title[:60]}",
        "index": index,
        "hwnd": hwnd,
        "title": title,
    })


@app.route('/debug-diff', methods=['GET'])
def handle_debug_diff():
    """调试：查看基线、当前 UIA 行、差量结果（不发送消息）。"""
    global _chat_user_message
    simulate_user = request.args.get("user", "").strip()
    result, err = scan_cursor_ui()
    if err:
        return jsonify({"ok": False, "msg": err})
    hwnd = result["hwnd"]
    input_rect_screen = _screen_rect_from_window_rect(hwnd, result.get("input_rect"))
    chat_region_screen = result.get("chat_region_screen")
    lines, terr = extract_response_text(
        hwnd, input_rect_screen=input_rect_screen, chat_region_screen=chat_region_screen,
    )
    if simulate_user:
        with _chat_lock:
            _chat_user_message = simulate_user
    with _chat_lock:
        baseline = list(_chat_baseline or [])
        user_msg = _chat_user_message
    diff_text = diff_against_baseline(lines or [])
    return jsonify({
        "ok": True,
        "terr": terr,
        "target_title": result.get("title"),
        "user_message": user_msg,
        "simulate_user": simulate_user or None,
        "baseline_len": len(baseline),
        "baseline_lines": baseline,
        "current_len": len(lines or []),
        "current_lines": lines or [],
        "diff_text": diff_text,
        "diff_len": len(diff_text or ""),
        "prefix_match": (
            len(lines or []) >= len(baseline)
            and (lines or [])[:len(baseline)] == baseline
        ),
    })


@app.route('/test-text', methods=['GET'])
def handle_test_text():
    picked = pick_cursor_hwnd()
    if not picked:
        return jsonify({"ok": False, "msg": "未找到 Cursor 窗口"})
    hwnd = picked[0]

    result, err = scan_cursor_ui()
    input_rect_screen = None
    chat_region_screen = None
    if not err and result:
        input_rect_screen = _screen_rect_from_window_rect(hwnd, result.get("input_rect"))
        chat_region_screen = result.get("chat_region_screen")

    use_full = request.args.get("full", "").lower() in ("1", "true", "yes")
    if use_full:
        lines, terr = extract_response_text_full(
            hwnd, input_rect_screen=input_rect_screen, chat_region_screen=chat_region_screen,
        )
    else:
        lines, terr = extract_response_text(
            hwnd, input_rect_screen=input_rect_screen, chat_region_screen=chat_region_screen,
        )
    if terr:
        print(f"[test-text] 错误: {terr}")
        return jsonify({"ok": False, "msg": terr, "len": 0})
    text = "\n".join(lines) if lines else ""
    _safe_print(f"[test-text] 文本长度: {len(text)}")
    _safe_print("---------- test-text 提取开始 ----------")
    _safe_print(text)
    _safe_print("---------- test-text 提取结束 ----------")
    return jsonify({"ok": True, "text": text, "len": len(text)})


@app.route('/reset-baseline', methods=['GET', 'POST'])
def handle_reset_baseline():
    init_chat_baseline()
    return jsonify({"ok": True, "baseline_lines": len(_chat_baseline or [])})


def _free_port(port):
    """启动前释放端口上占用的旧进程（避免多实例残留）。"""
    if os.environ.get("CURSOR_REMOTE_NO_KILL_PORT", "").lower() in ("1", "true", "yes"):
        return
    try:
        import subprocess
        ps = (
            f"Get-NetTCPConnection -LocalPort {int(port)} -State Listen "
            f"-ErrorAction SilentlyContinue | "
            f"Select-Object -ExpandProperty OwningProcess -Unique"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        my_pid = os.getpid()
        killed = []
        for line in (proc.stdout or "").splitlines():
            pid_s = line.strip()
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            if pid == my_pid:
                continue
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
            killed.append(pid)
        if killed:
            print(f"[server] 已关闭占用端口 {port} 的旧进程: {killed}")
            time.sleep(1)
    except Exception as exc:
        print(f"[server] 释放端口 {port} 失败: {exc}")


if __name__ == '__main__':
    loaded = load_local_env_file()
    if loaded:
        print(f"[server] 已加载配置: {loaded}")
    else:
        print("[server] 未找到 local_env.env / local_env.ps1（可将 local_env.example.env 复制为 local_env.env）")

    host = os.environ.get("CURSOR_REMOTE_HOST", "0.0.0.0")
    port = int(os.environ.get("CURSOR_REMOTE_PORT", "5002"))
    uploads_dir = os.path.join(exe_dir(), "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    _free_port(port)
    print(f"服务启动: http://{host}:{port}")
    print(f"手机访问: http://<本机IP>:{port}")
    backend = get_remote_backend()
    print(f"[server] 控制后端: {backend}")
    if backend == "sdk" and _sdk_backend:
        print(
            f"[server] SDK mode={_sdk_backend.sdk_mode()} "
            f"cwd={_sdk_backend.workspace_cwd()} model={_sdk_backend.model_name()}"
        )
    elif backend == "uia":
        print("[server] UIA 模式：设置 CURSOR_API_KEY 可切换为官方 SDK")
    _load_persisted_target()
    _init_window_index_from_env()
    app.run(host=host, port=port, debug=False)
