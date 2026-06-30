"""通过 Cursor API 发送消息并读取结构化回复（不依赖 UIA 抓屏）。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

API_BASE = "https://api.cursor.com"

_agent = None
_cloud_agent_id: str | None = None
_lock = threading.Lock()

_TERMINAL = frozenset({"FINISHED", "ERROR", "FAILED", "CANCELLED", "CANCELED"})


def api_key_configured() -> bool:
    return bool((os.environ.get("CURSOR_API_KEY") or "").strip())


def workspace_cwd() -> str:
    return (os.environ.get("CURSOR_SDK_CWD") or os.getcwd()).strip()


def model_name() -> str:
    return (os.environ.get("CURSOR_SDK_MODEL") or "composer-2.5").strip()


def one_shot_enabled() -> bool:
    return os.environ.get("CURSOR_SDK_ONE_SHOT", "").lower() in ("1", "true", "yes")


def sdk_mode() -> str:
    raw = os.environ.get("CURSOR_SDK_MODE", "").strip().lower()
    if raw in ("cloud", "local"):
        return raw
    return "local"


def _api_key() -> str:
    return (os.environ.get("CURSOR_API_KEY") or "").strip()


def _parse_api_error(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:500]
    err = data.get("error") or data
    if isinstance(err, dict):
        code = err.get("code", "")
        msg = err.get("message", str(err))
        if code == "usage_limit_exceeded":
            return (
                f"{msg} "
                "请在 Cursor 设置中开启 Usage-based pricing 并设置 Spend Limit："
                "https://cursor.com/dashboard?tab=settings"
            )
        return f"{code}: {msg}" if code else msg
    return str(err)


def _api_json(method: str, path: str, body: dict | None = None, timeout: float = 120):
    key = _api_key()
    url = API_BASE + path
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return None, _parse_api_error(raw)
    except Exception as exc:
        return None, str(exc)


def _wait_cloud_run(agent_id: str, run_id: str) -> tuple[str | None, str | None]:
    poll = max(float(os.environ.get("CURSOR_SDK_POLL_SEC", "2")), 0.5)
    deadline = time.time() + max(int(os.environ.get("CURSOR_SDK_TIMEOUT", "300")), 30)
    while time.time() < deadline:
        data, err = _api_json("GET", f"/v1/agents/{agent_id}/runs/{run_id}", timeout=60)
        if err:
            return None, err
        status = str(data.get("status", "")).upper()
        if status in _TERMINAL:
            if status != "FINISHED":
                return None, f"Cloud Agent 结束状态: {status}"
            reply = (data.get("result") or "").strip()
            return (reply, None) if reply else (None, "未返回文本")
        time.sleep(poll)
    return None, "等待 Cloud Agent 回复超时"


def _send_prompt_cloud(text: str) -> tuple[str | None, str | None]:
    global _cloud_agent_id
    body = {
        "prompt": {"text": text},
        "model": {"id": model_name()},
    }
    with _lock:
        if one_shot_enabled() or not _cloud_agent_id:
            data, err = _api_json("POST", "/v1/agents", body)
            if err:
                return None, err
            agent_id = (data.get("agent") or {}).get("id")
            run_id = (data.get("run") or {}).get("id")
            if not one_shot_enabled() and agent_id:
                _cloud_agent_id = agent_id
        else:
            agent_id = _cloud_agent_id
            data, err = _api_json("POST", f"/v1/agents/{agent_id}/runs", body)
            if err:
                return None, err
            run_id = (data.get("run") or data or {}).get("id")
        if not agent_id or not run_id:
            return None, f"Cloud API 响应缺少 agent/run id: {data!r}"
    return _wait_cloud_run(agent_id, run_id)


def _bridge_candidate(path: str) -> str | None:
    """返回可用的 bridge 启动器路径（兼容 PyInstaller 误打包为子目录）。"""
    if not path:
        return None
    if os.path.isfile(path):
        return os.path.abspath(path)
    base = os.path.basename(path)
    nested = os.path.join(path, base)
    if os.path.isfile(nested):
        return os.path.abspath(nested)
    return None


def _ensure_bridge_env() -> None:
    """设置 CURSOR_SDK_BRIDGE_BIN，避免打包后找不到 cursor-sdk-bridge。"""
    found = _bridge_candidate(os.environ.get("CURSOR_SDK_BRIDGE_BIN", "").strip())
    if found:
        os.environ["CURSOR_SDK_BRIDGE_BIN"] = found
        return

    names = ("cursor-sdk-bridge.cmd", "cursor-sdk-bridge.exe", "cursor-sdk-bridge")
    bases: list[str] = []

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            bases.append(meipass)
            bases.append(os.path.join(meipass, "cursor_sdk"))

    try:
        import cursor_sdk

        bases.append(os.path.dirname(cursor_sdk.__file__))
    except ImportError:
        pass

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    bases.append(exe_dir)
    bases.append(os.path.join(exe_dir, "_internal"))
    bases.append(os.path.join(exe_dir, "_internal", "cursor_sdk"))

    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        for name in names:
            for rel in (
                os.path.join("_vendor", "bridge", "bin", name),
                os.path.join("cursor_sdk", "_vendor", "bridge", "bin", name),
            ):
                found = _bridge_candidate(os.path.join(base, rel))
                if found:
                    os.environ["CURSOR_SDK_BRIDGE_BIN"] = found
                    return


def _patch_os_blocking_for_py310():
    if hasattr(os, "get_blocking"):
        return
    if os.name == "nt":
        os.get_blocking = lambda _fd: True  # type: ignore[attr-defined]
        os.set_blocking = lambda _fd, _blocking: None  # type: ignore[attr-defined]


def _patch_cursor_sdk_windows_bridge():
    """Windows 上 os.read(pipe_fd) 会失败，改用 stderr.readline() 读 bridge 发现行。"""
    if os.name != "nt":
        return
    import cursor_sdk._bridge as bridge_mod
    from cursor_sdk.errors import CursorSDKError

    if getattr(bridge_mod, "_read_discovery_patched", False):
        return

    def _read_discovery_win(process, timeout: float):
        if process.stderr is None:
            raise CursorSDKError("Bridge process stderr is unavailable")
        deadline = time.monotonic() + timeout
        stderr_lines: list[str] = []
        while time.monotonic() < deadline:
            line = process.stderr.readline()
            if line:
                stderr_lines.append(line)
                discovery = bridge_mod.parse_discovery_line(line)
                if discovery is not None:
                    return discovery
            exit_code = process.poll()
            if exit_code is not None and not line:
                raise CursorSDKError(
                    f"Bridge exited before discovery with status {exit_code}: "
                    + "".join(stderr_lines)
                )
            if not line:
                time.sleep(0.05)
        raise CursorSDKError("Timed out waiting for bridge discovery")

    bridge_mod._read_discovery = _read_discovery_win  # type: ignore[assignment]
    bridge_mod._read_discovery_patched = True


def _send_prompt_local(text: str) -> tuple[str | None, str | None]:
    _ensure_bridge_env()
    _patch_os_blocking_for_py310()
    _patch_cursor_sdk_windows_bridge()
    from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions

    key = _api_key()
    prompt = text.strip()
    opts = AgentOptions(
        api_key=key,
        model=model_name(),
        local=LocalAgentOptions(cwd=workspace_cwd()),
    )
    try:
        if one_shot_enabled():
            result = Agent.prompt(prompt, opts)
            if str(getattr(result, "status", "")).lower() == "error":
                return None, f"Agent 运行失败: {getattr(result, 'id', result.status)}"
            reply = (result.result or "").strip()
            return (reply, None) if reply else (None, "未返回文本")

        with _lock:
            global _agent
            if _agent is None:
                _agent = Agent.create(
                    api_key=key,
                    model=model_name(),
                    local=LocalAgentOptions(cwd=workspace_cwd()),
                )
            run = _agent.send(prompt)
            reply = (run.text() or "").strip()
            return (reply, None) if reply else (None, "未返回文本")
    except CursorAgentError as err:
        return None, f"SDK 启动失败: {err.message}"
    except Exception as err:
        return None, f"SDK 错误: {err}"


def send_prompt(text: str) -> tuple[str | None, str | None]:
    """返回 (reply_text, error_message)。成功时 error 为 None。"""
    if not _api_key():
        return None, "未设置 CURSOR_API_KEY（Cursor 控制台 → Integrations 创建）"
    prompt = (text or "").strip()
    if not prompt:
        return None, "消息为空"
    if sdk_mode() == "cloud":
        return _send_prompt_cloud(prompt)
    return _send_prompt_local(prompt)


def reset_agent():
    """清空多轮会话。"""
    global _agent, _cloud_agent_id
    with _lock:
        if _agent is not None:
            try:
                _agent.close()
            except Exception:
                pass
            _agent = None
        _cloud_agent_id = None
