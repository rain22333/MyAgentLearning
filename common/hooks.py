"""common/hooks.py — Hook 系统（s04 引入，s04~s20 复用）"""
from typing import Any, Callable

HOOKS: dict[str, list[Callable]] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}


def register_hook(event: str, callback: Callable):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args: Any) -> Any | None:
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ── 默认 Hook 实现 ──

DENY_LIST = [
    "rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=",
    "format", "del /f /s C:\\", "rd /s /q C:\\",
]


def permission_hook(block) -> str | None:
    """PreToolUse: 拒绝列表检查"""
    if block.name == "bash":
        cmd = block.input.get("command", "")
        for p in DENY_LIST:
            if p.lower() in cmd.lower():
                print(f"\n  🚫 [HOOK] 已拦截: '{p}'")
                return "权限被拒绝"
    return None


def log_hook(block) -> None:
    """PreToolUse: 工具调用日志"""
    print(f"  📝 [LOG] {block.name}")
    return None


def context_inject_hook(query: str) -> None:
    """UserPromptSubmit: 输入日志（可扩展为注入上下文）"""
    return None


def summary_hook(messages: list) -> None:
    """Stop: 打印本轮工具调用统计"""
    tool_count = sum(
        1 for m in messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    print(f"  📊 [HOOK] Stop: 本轮 {tool_count} 次工具调用")
    return None


def register_default_hooks():
    """注册所有默认 hook"""
    register_hook("UserPromptSubmit", context_inject_hook)
    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", log_hook)
    register_hook("Stop", summary_hook)
