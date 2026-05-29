#!/usr/bin/env python3
"""
s05_todo_write/code.py — 计划工具：先想清楚再动手

给 Agent 一个 todo_write 工具 —— 不做任何实际工作，只维护任务计划列表。
关键洞察：不增加执行能力，增加的是规划能力。

工具实现和 Hook 系统已抽取到 common/，本章聚焦 todo_write 和 nag reminder。
"""
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.tools import BASE_TOOLS, BASE_HANDLERS
from common.hooks import register_default_hooks, trigger_hooks

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

# ★ s05: TODO 列表存在进程内存中
CURRENT_TODOS: list[dict] = []

SYSTEM = (
    f"你是一个在 {WORKDIR} 目录下工作的编码助手。"
    "开始多步骤任务前，必须先使用 todo_write 工具制定计划。"
    "执行过程中及时更新每个步骤的状态。用中文回答，直接行动。"
)


# ═══════════════════════════════════════════════════════════
#  ★【s05 新概念】todo_write — 纯规划工具
# ═══════════════════════════════════════════════════════════
def run_todo_write(todos: list[dict]) -> str:
    global CURRENT_TODOS
    CURRENT_TODOS = todos

    icons = {"pending": "🔲", "in_progress": "🔄", "completed": "✅", "cancelled": "❌"}
    lines = ["\n  ┌─── 当前任务计划 ───"]
    for t in CURRENT_TODOS:
        icon = icons.get(t.get("status", "pending"), "❓")
        lines.append(f"  │ {icon} {t.get('content', '')}")
    lines.append("  └────────────────────\n")
    print("\n".join(lines))

    counts = {}
    for t in CURRENT_TODOS:
        s = t.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1
    parts = []
    for key, label in [("completed", "已完成"), ("in_progress", "进行中"), ("pending", "待处理")]:
        if counts.get(key):
            parts.append(f"{counts[key]} {label}")
    summary = "，".join(parts) if parts else "无任务"

    return f"任务计划已更新: 共 {len(CURRENT_TODOS)} 个任务 ({summary})"


# 在 5 个基础工具上追加 todo_write
TOOLS = BASE_TOOLS + [{
    "name": "todo_write",
    "description": "创建和管理任务计划列表。多步骤任务前先调用此工具制定计划。",
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "任务描述"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
}]

TOOL_HANDLERS = {**BASE_HANDLERS, "todo_write": run_todo_write}

register_default_hooks()


# ═══════════════════════════════════════════════════════════
#  agent_loop — s04 结构 + nag reminder
#
#  新增 1：每 3 轮没调 todo_write → 注入提醒
#  新增 2：调了 todo_write → 重置计数器
# ═══════════════════════════════════════════════════════════
rounds_since_todo = 0


def agent_loop(messages: list):
    global rounds_since_todo
    while True:
        # ★ s05 新增：nag reminder
        if rounds_since_todo >= 3 and messages:
            print("  ⏰ [NAG] 已 3 轮未更新计划，注入提醒...")
            messages.append({
                "role": "user",
                "content": "<reminder>你的任务计划需要更新了，请调用 todo_write。</reminder>",
            })
            rounds_since_todo = 0

        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": str(force)})
                continue
            return

        rounds_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": str(blocked),
                })
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"未知工具: {block.name}"

            trigger_hooks("PostToolUse", block, output)

            # ★ 调了 todo_write 就重置计数器
            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("=" * 55)
    print("  s05: TodoWrite — 先计划再执行")
    print("=" * 55)
    print()
    print("  todo_write 不增加执行能力，增加的是规划能力。")
    print("  连续 3 轮不更新计划 → 自动注入提醒。")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    while True:
        try:
            query = input("\033[36ms05 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        last_msg = history[-1]["content"]
        if isinstance(last_msg, list):
            for block in last_msg:
                if getattr(block, "type", None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")
        print()
