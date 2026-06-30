"""PyInstaller 打包后路径：可写目录（exe 旁）与资源目录（bundle）。"""

from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> str:
    """exe 所在目录（配置、uploads、.cursor_target.json）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def bundle_dir() -> str:
    """打包内资源目录（模板图等）。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", exe_dir())
    return os.path.dirname(os.path.abspath(__file__))
