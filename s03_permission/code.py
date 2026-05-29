#!/usr/bin/env python3
"""
s03_permission/code.py — 权限系统：三道闸门

在 s02 基础上，工具执行前插入三道闸门。
现在工具实现从 common/ 导入，聚焦本章新概念。
"""
import os, sys
from pathlib import Path

# 将根目录加入 path，支持 from common import
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.tools import BASE_TOOLS, BASE_HANDLERS, safe_path
from common.hooks import register_default_hooks

try:
    import readline
    readline.parse_and_bind("set bind-tty-special-chars off")
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"), override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = (
    f"你是一个在 {WORKDIR} 目录下工作的编码助手。"
    "所有破坏性操作需要用户审批。用中文回答，直接行动。"
)

# s03 使用 5 个基础工具（和 s02 一样）
TOOLS = BASE_TOOLS
TOOL_HANDLERS = BASE_HANDLERS


# ═══════════════════════════════════════════════════════════
#  ★【s03 新概念】三道闸门权限管线
# ═══════════════════════════════════════════════════════════

# 闸门 1：硬拒绝列表
DENY_LIST = [
    "rm -rf /", "sudo", "shutdown", "reboot",
    "mkfs", "dd if=", "> /dev/sda",
    "format", "del /f /s C:\\", "rd /s /q C:\\",
]

DESTRUCTIVE = ["del ", "rm ", "rmdir", "format"]


def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern.lower() in command.lower():
            return f"已拦截: '{pattern}' 在拒绝列表中"
    return None


# 闸门 2：规则匹配
PERMISSION_RULES = [
    {
        "tools": ["write_file", "edit_file"],
        "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
        "message": "在工作区外写入文件",
    },
    {
        "tools": ["bash"],
        "check": lambda args: any(
            kw in args.get("command", "").lower() for kw in DESTRUCTIVE
        ),
        "message": "潜在破坏性命令",
    },
]


def check_rules(tool_name: str, args: dict) -> str | None:
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# 闸门 3：用户审批
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    print(f"\n  ⚠️  {reason}")
    print(f"     工具: {tool_name}")
    for k, v in args.items():
        print(f"     {k}: {str(v)[:100]}")
    choice = input("     允许执行? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


# 三道闸门串联
def check_permission(block) -> bool:
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"\n  🚫 {reason}")
            return False
    reason = check_rules(block.name, dict(block.input))
    if reason:
        decision = ask_user(block.name, dict(block.input), reason)
        if decision == "deny":
            print("  ❌ 已拒绝\n")
            return False
        print("  ✅ 已允许\n")
    return True


# ═══════════════════════════════════════════════════════════
#  agent_loop — s02 结构 + 权限检查
#
#  s02: output = TOOL_HANDLERS[block.name](**block.input)
#  s03: if not check_permission(block): continue
#        output = TOOL_HANDLERS[block.name](**block.input)
# ═══════════════════════════════════════════════════════════
def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            print(f"\033[36m>>> [{block.name}]\033[0m")

            # ★ s03 唯一新增：权限检查
            if not check_permission(block):
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": "权限被拒绝。",
                })
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"未知工具: {block.name}"
            print(str(output)[:300])
            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("=" * 55)
    print("  s03: Permission — 三道闸门权限系统")
    print("=" * 55)
    print()
    print("  闸门1 → 硬拒绝列表（永远禁止）")
    print("  闸门2 → 规则匹配（写工作区外？破坏性命令？）")
    print("  闸门3 → 用户审批（暂停等待确认）")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    while True:
        try:
            query = input("\033[36ms03 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        last_msg = history[-1]["content"]
        if isinstance(last_msg, list):
            for block in last_msg:
                if getattr(block, "type", None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")
        print()
