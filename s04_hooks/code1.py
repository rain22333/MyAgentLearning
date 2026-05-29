#!/usr/bin/env python3
"""
s04_hooks/code.py — Hook 系统：扩展逻辑挂在循环外面

s03 的权限检查直接写在循环里。每加一个新功能（日志、通知...）都要改循环。
s04 把扩展逻辑"拔"出来，挂到 Hook 系统上。循环只调 trigger_hooks()。

工具实现和 Hook 系统已抽取到 common/，本章聚焦 Hook 的使用方式和 4 个事件。
"""
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.tools import BASE_TOOLS, BASE_HANDLERS
from common.hooks import HOOKS, register_hook, trigger_hooks, register_default_hooks, DENY_LIST

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

SYSTEM = f"你是一个在 {WORKDIR} 目录下工作的编码助手。用中文回答，直接行动。"

TOOLS = BASE_TOOLS
TOOL_HANDLERS = BASE_HANDLERS


# ═══════════════════════════════════════════════════════════
#  ★【s04 新概念】Hook 系统
#
#  四个事件覆盖完整生命周期：
#    UserPromptSubmit  ← 用户输入后、LLM 调用前
#    PreToolUse        ← 工具执行前（s03 的权限检查移到这里）
#    PostToolUse       ← 工具执行后（新增）
#    Stop              ← 循环退出前（新增）
#
#  核心机制：register_hook("事件", 回调) → trigger_hooks("事件", 参数)
#  返回规则：第一个返回非 None 的回调会短路，后续不再执行
# ═══════════════════════════════════════════════════════════

# ★ s04：注册一个额外的 PostToolUse hook（大输出提醒）
def large_output_hook(block, output: str) -> None:
    if len(output) > 10000:
        print(f"  ⚠️  [HOOK] 大输出: {block.name} 返回了 {len(output):,} 字符")
    return None


# 注册默认 hooks（权限 + 日志 + 统计）
register_default_hooks()
# 再追加 s04 新增的 hook
register_hook("PostToolUse", large_output_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — s03 vs s04 的唯一区别
#
#  s03: if not check_permission(block): continue
#  s04: blocked = trigger_hooks("PreToolUse", block)
#        if blocked: continue
#        ...
#        trigger_hooks("PostToolUse", block, output)   ← 新增
#
#  循环不再知道"权限检查"的存在，只知道"触发 PreToolUse 事件"
# ═══════════════════════════════════════════════════════════
def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # ★ Stop hook：循环退出前触发（s04 新增）
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": str(force)})
                continue
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            print(f"\033[36m>>> [{block.name}]\033[0m")

            # ★ s04：不再调 check_permission，改调 hook 系统
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": str(blocked),
                })
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"未知工具: {block.name}"
            print(str(output)[:300])

            # ★ s04 新增：工具执行后触发 PostToolUse hook
            trigger_hooks("PostToolUse", block, output)

            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("=" * 55)
    print("  s04: Hooks — 扩展逻辑挂在循环外面")
    print("=" * 55)
    print()
    print("  s03: if not check_permission(block): ...")
    print("  s04: blocked = trigger_hooks('PreToolUse', block)")
    print("         if blocked: ...")
    print()
    print("  四个事件：UserPromptSubmit / PreToolUse / PostToolUse / Stop")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    while True:
        try:
            query = input("\033[36ms04 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break

        # ★ UserPromptSubmit hook
        trigger_hooks("UserPromptSubmit", query)

        history.append({"role": "user", "content": query})
        agent_loop(history)
        last_msg = history[-1]["content"]
        if isinstance(last_msg, list):
            for block in last_msg:
                if getattr(block, "type", None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")
        print()
