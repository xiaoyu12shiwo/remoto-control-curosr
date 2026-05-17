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
    """查找 Cursor 主窗口"""
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
    """截取指定窗口的图像"""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = right - left
    height = bottom - top

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()

    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)

    result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)

    bmp_info = bitmap.GetInfo()
    bmp_str = bitmap.GetBitmapBits(True)
    img = np.frombuffer(bmp_str, dtype=np.uint8)
    img.shape = (height, width, 4)

    img_cv = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    mfc_dc.DeleteDC()
    save_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    win32gui.DeleteObject(bitmap.GetHandle())

    return img_cv, (left, top, width, height)


def find_input_box_by_template(window_img, window_offset, template_path=None):
    """通过模板匹配查找输入框"""
    if template_path:
        template = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if template is None:
            print(f"无法加载模板图片: {template_path}")
            return None
        result = cv2.matchTemplate(window_img, template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
        if max_val > 0.7:
            h, w = template.shape[:2]
            x, y = max_loc
            abs_x = window_offset[0] + x + w // 2
            abs_y = window_offset[1] + y + h // 2
            return {
                "type": "input_box",
                "confidence": max_val,
                "rect": (x, y, w, h),
                "center": (abs_x, abs_y),
            }
    return None


def find_input_box_by_contour(window_img, window_offset):
    """通过轮廓分析在窗口右下角查找输入框"""
    h, w = window_img.shape[:2]
    region_bottom = window_img[int(h * 0.75):, int(w * 0.5):]
    gray = cv2.cvtColor(region_bottom, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(blurred, 50, 150)

    contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []

    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cv2.contourArea(cnt)
        if area < 500:
            continue
        aspect_ratio = cw / max(ch, 1)
        if 3.0 < aspect_ratio < 30.0 and ch > 15 and cw > 100:
            abs_x = window_offset[0] + int(w * 0.5) + x + cw // 2
            abs_y = window_offset[1] + int(h * 0.75) + y + ch // 2
            candidates.append(
                {
                    "type": "input_box_contour",
                    "rect": (x, y, cw, ch),
                    "center": (abs_x, abs_y),
                    "area": area,
                    "aspect_ratio": aspect_ratio,
                }
            )

    candidates.sort(key=lambda c: c["area"], reverse=True)
    return candidates[:3]


def find_voice_button_by_color(window_img, window_offset):
    """通过颜色特征查找语音按钮（通常是麦克风图标）"""
    h, w = window_img.shape[:2]
    region_bottom_right = window_img[int(h * 0.85):, int(w * 0.85):]

    hsv = cv2.cvtColor(region_bottom_right, cv2.COLOR_BGR2HSV)

    lower_gray = np.array([0, 0, 120])
    upper_gray = np.array([180, 50, 220])
    mask = cv2.inRange(hsv, lower_gray, upper_gray)

    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []

    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cv2.contourArea(cnt)
        if area < 50:
            continue
        if abs(cw - ch) < max(cw, ch) * 0.5 and area > 100:
            abs_x = window_offset[0] + int(w * 0.85) + x + cw // 2
            abs_y = window_offset[1] + int(h * 0.85) + y + ch // 2
            candidates.append(
                {
                    "type": "voice_button_color",
                    "rect": (x, y, cw, ch),
                    "center": (abs_x, abs_y),
                    "area": area,
                }
            )

    candidates.sort(key=lambda c: c["area"], reverse=True)
    return candidates[:3]


def find_voice_button_by_icon_template(window_img, window_offset, template_path=None):
    """通过麦克风图标模板匹配查找语音按钮"""
    if template_path:
        template = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if template is None:
            return None
        h, w = window_img.shape[:2]
        region = window_img[int(h * 0.7):, int(w * 0.7):]
        result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
        if max_val > 0.6:
            th, tw = template.shape[:2]
            abs_x = window_offset[0] + int(w * 0.7) + max_loc[0] + tw // 2
            abs_y = window_offset[1] + int(h * 0.7) + max_loc[1] + th // 2
            return {
                "type": "voice_button_template",
                "confidence": max_val,
                "rect": (max_loc[0], max_loc[1], tw, th),
                "center": (abs_x, abs_y),
            }
    return None


def find_template_full(window_img, window_offset, template_path, label, threshold=0.7):
    """在全窗口范围内进行模板匹配"""
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
    """主扫描函数"""
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
    mic_tpl = os.path.join(script_dir, "mic_icon.png")

    results = {
        "window": {"hwnd": hwnd, "title": title, "rect": (wx, wy, ww, wh)},
        "input_box": None,
        "voice_button": None,
    }

    input_match = find_template_full(window_img, window_offset, input_tpl, "input_box", threshold=0.6)
    if input_match:
        r = input_match["rect"]
        cx = window_offset[0] + r[0] + r[2] // 4
        cy = window_offset[1] + r[1] + r[3] // 4
        input_match["center"] = (cx, cy)
        results["input_box"] = input_match
        print(f"  相对位置: {input_match['rect']}, 绝对中心: {input_match['center']}")

    mic_match = find_template_full(window_img, window_offset, mic_tpl, "mic_icon", threshold=0.6)
    if mic_match:
        r = mic_match["rect"]
        cx = window_offset[0] + r[0] + r[2] * 3 // 4
        cy = window_offset[1] + r[1] + r[3] * 3 // 4
        mic_match["center"] = (cx, cy)
        results["voice_button"] = mic_match
        print(f"  相对位置: {mic_match['rect']}, 绝对中心: {mic_match['center']}")

    annotated = window_img.copy()
    if results["input_box"]:
        r = results["input_box"]["rect"]
        cv2.rectangle(annotated, (r[0], r[1]), (r[0] + r[2], r[1] + r[3]), (0, 255, 0), 2)
        cv2.putText(annotated, "Input Box", (r[0], r[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    if results["voice_button"]:
        r = results["voice_button"]["rect"]
        cv2.rectangle(annotated, (r[0], r[1]), (r[0] + r[2], r[1] + r[3]), (0, 0, 255), 2)
        cv2.putText(annotated, "Voice Btn", (r[0], r[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    cv2.imwrite(os.path.join(script_dir, "cursor_ui_annotated.png"), annotated)
    print(f"\n标注图已保存: cursor_ui_annotated.png")

    return results


def click_input_box(results):
    """点击输入框"""
    if results and results["input_box"]:
        cx, cy = results["input_box"]["center"]
        pyautogui.click(cx, cy)
        print(f"已点击输入框: ({cx}, {cy})")
    else:
        print("未找到输入框")


def click_voice_button(results):
    """点击语音按钮"""
    if results and results["voice_button"]:
        cx, cy = results["voice_button"]["center"]
        pyautogui.click(cx, cy)
        print(f"已点击语音按钮: ({cx}, {cy})")
    else:
        print("未找到语音按钮")


def take_screenshot_for_templates():
    """截取 Cursor 窗口用于手动裁剪模板"""
    windows = find_cursor_window()
    if not windows:
        print("未找到 Cursor 窗口")
        return
    hwnd = windows[0][0]
    window_img, _ = capture_window(hwnd)
    cv2.imwrite("cursor_screenshot.png", window_img)
    print("截图已保存: cursor_screenshot.png")
    print("请裁剪输入框区域保存为 templates/input_box.png")
    print("请裁剪麦克风图标保存为 templates/mic_icon.png")


if __name__ == "__main__":

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "screenshot":
            take_screenshot_for_templates()
        elif cmd == "run":
            results = scan_cursor_ui()
            if results:
                print("\n--- 识别结果汇总 ---")
                print(f"输入框: {'已找到' if results['input_box'] else '未找到'}")
                print(f"语音按钮: {'已找到' if results['voice_button'] else '未找到'}")

                if results["input_box"]:
                    cx, cy = results["input_box"]["center"]
                    print(f"\n移动光标到输入框: ({cx}, {cy})")
                    mouse_move_to(cx, cy)
                    time.sleep(0.3)
                    mouse_click()

                print("等待 5 秒...")
                time.sleep(5)

                if results["voice_button"]:
                    cx, cy = results["voice_button"]["center"]
                    print(f"移动光标到语音按钮: ({cx}, {cy})")
                    mouse_move_to(cx, cy)
                else:
                    print("未找到语音按钮，重新截取窗口后重试...")
                    results2 = scan_cursor_ui()
                    if results2 and results2["voice_button"]:
                        cx, cy = results2["voice_button"]["center"]
                        print(f"移动光标到语音按钮: ({cx}, {cy})")
                        mouse_move_to(cx, cy)
                    else:
                        print("仍然未找到语音按钮")
        else:
            print(f"未知命令: {cmd}")
    else:
        results = scan_cursor_ui()
        if results:
            print("\n--- 识别结果汇总 ---")
            print(f"输入框: {'已找到' if results['input_box'] else '未找到'}")
            print(f"语音按钮: {'已找到' if results['voice_button'] else '未找到'}")
