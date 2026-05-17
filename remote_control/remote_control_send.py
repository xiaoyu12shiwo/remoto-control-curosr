import ctypes
import os
import sys
import time

import cv2
import numpy as np
import win32gui
import win32ui
import win32con
from PIL import Image

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()


def mouse_move_to(x, y):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def mouse_click():
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)


def find_cursor_window():
    result = []

    def enum_callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            className = win32gui.GetClassName(hwnd)
            if "cursor" in title.lower() or "cursor" in className.lower():
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


def find_template_full(window_img, window_offset, template_path, label, threshold=0.7):
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
        print("未找到 Cursor 窗口，请确保 Cursor IDE 已打开")
        return None

    hwnd, title, class_name = windows[0]
    print(f"找到 Cursor 窗口: hwnd={hwnd}, title='{title}', class='{class_name}'")

    window_img, (wx, wy, ww, wh) = capture_window(hwnd)
    window_offset = (wx, wy)
    print(f"窗口位置: ({wx}, {wy}), 大小: {ww}x{wh}")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_tpl = os.path.join(script_dir, "input_box.png")
    send_tpl = os.path.join(script_dir, "send_icon.png")

    results = {
        "window": {"hwnd": hwnd, "title": title, "rect": (wx, wy, ww, wh)},
        "input_box": None,
        "send_button": None,
    }

    input_match = find_template_full(window_img, window_offset, input_tpl, "input_box", threshold=0.6)
    if input_match:
        r = input_match["rect"]
        cx = window_offset[0] + r[0] + r[2] // 4
        cy = window_offset[1] + r[1] + r[3] // 4
        input_match["center"] = (cx, cy)
        results["input_box"] = input_match
        print(f"  相对位置: {input_match['rect']}, 绝对中心: {input_match['center']}")

    send_match = find_template_full(window_img, window_offset, send_tpl, "send_icon", threshold=0.6)
    if send_match:
        r = send_match["rect"]
        cx = window_offset[0] + r[0] + r[2] * 3 // 4
        cy = window_offset[1] + r[1] + r[3] * 3 // 4
        send_match["center"] = (cx, cy)
        results["send_button"] = send_match
        print(f"  相对位置: {send_match['rect']}, 绝对中心: {send_match['center']}")

    annotated = window_img.copy()
    if results["input_box"]:
        r = results["input_box"]["rect"]
        cv2.rectangle(annotated, (r[0], r[1]), (r[0] + r[2], r[1] + r[3]), (0, 255, 0), 2)
        cv2.putText(annotated, "Input Box", (r[0], r[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    if results["send_button"]:
        r = results["send_button"]["rect"]
        cv2.rectangle(annotated, (r[0], r[1]), (r[0] + r[2], r[1] + r[3]), (0, 0, 255), 2)
        cv2.putText(annotated, "Send Btn", (r[0], r[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    cv2.imwrite(os.path.join(script_dir, "cursor_ui_annotated_send.png"), annotated)
    print(f"\n标注图已保存: cursor_ui_annotated_send.png")

    return results


if __name__ == "__main__":

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "screenshot":
            windows = find_cursor_window()
            if not windows:
                print("未找到 Cursor 窗口")
            else:
                hwnd = windows[0][0]
                window_img, _ = capture_window(hwnd)
                script_dir = os.path.dirname(os.path.abspath(__file__))
                cv2.imwrite(os.path.join(script_dir, "cursor_screenshot.png"), window_img)
                print("截图已保存: cursor_screenshot.png")
        elif cmd == "run":
            results = scan_cursor_ui()
            if results:
                print("\n--- 识别结果汇总 ---")
                print(f"输入框: {'已找到' if results['input_box'] else '未找到'}")
                print(f"发送按钮: {'已找到' if results['send_button'] else '未找到'}")

                if results["input_box"]:
                    cx, cy = results["input_box"]["center"]
                    print(f"\n移动光标到输入框: ({cx}, {cy})")
                    mouse_move_to(cx, cy)
                    time.sleep(0.3)
                    mouse_click()

                print("等待 5 秒...")
                time.sleep(5)

                if results["send_button"]:
                    cx, cy = results["send_button"]["center"]
                    print(f"移动光标到发送按钮: ({cx}, {cy})")
                    mouse_move_to(cx, cy)
                else:
                    print("未找到发送按钮，重新截取窗口后重试...")
                    results2 = scan_cursor_ui()
                    if results2 and results2["send_button"]:
                        cx, cy = results2["send_button"]["center"]
                        print(f"移动光标到发送按钮: ({cx}, {cy})")
                        mouse_move_to(cx, cy)
                    else:
                        print("仍然未找到发送按钮")
        else:
            print(f"未知命令: {cmd}")
    else:
        results = scan_cursor_ui()
        if results:
            print("\n--- 识别结果汇总 ---")
            print(f"输入框: {'已找到' if results['input_box'] else '未找到'}")
            print(f"发送按钮: {'已找到' if results['send_button'] else '未找到'}")
