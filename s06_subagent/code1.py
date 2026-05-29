#!/usr/bin/env python3
"""
s06_subagent/code.py — 子 Agent：大任务拆小，上下文隔离

给 Agent 一个 task 工具，让它能启动子 Agent。
子 Agent 拥有全新的 messages[]，中间过程不污染父 Agent 上下文。
完成后只回传最终结论。

工具实现和 Hook 系统已抽取到 common/，本章聚焦 spawn_subagent。
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
CURRENT_TODOS: list[dict] = []

SYSTEM = (
    f"你是一个在 {WORKDIR} 目录下工作的编码助手。"
    "对于复杂的子问题，使用 task 工具启动子 Agent 来处理。用中文回答，直接行动。"
)

# ★ s06：子 Agent 有自己的系统提示
SUB_SYSTEM = (
    f"你是一个在 {WORKDIR} 目录下工作的编码助手。"
    "完成交给你的任务，然后返回简洁的结论。不要进一步委托。用中文回答。"
)

# 子 Agent 的工具：5 个基础工具，没有 task（防递归）
SUB_TOOLS = BASE_TOOLS
SUB_HANDLERS = BASE_HANDLERS


# ── todo_write ──
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
    return f"已更新 {len(CURRENT_TODOS)} 个任务"


# ═══════════════════════════════════════════════════════════
#  ★【s06 新概念】spawn_subagent — 核心新增
# ═══════════════════════════════════════════════════════════

def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [b.text for b in content if hasattr(b, "text")]
        return "\n".join(texts) if texts else "(无文本输出)"
    return str(content)


def spawn_subagent(description: str) -> str:
    """
    启动子 Agent：
    - 全新 messages[]，只有一条用户消息
    - 独立 while 循环，最多 30 轮
    - 只返回最后一条文本结论，中间过程全部丢弃
    - 没有 task 工具（防递归）
    - 工具执行仍经过权限 hook
    """
    print(f"\n  🚀 [SUBAGENT] 启动子 Agent...")
    print(f"  📋 任务: {description[:100]}{'...' if len(description) > 100 else ''}")

    messages = [{"role": "user", "content": description}]

    for turn in range(30):
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM, messages=messages,
            tools=SUB_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            result = extract_text(messages[-1]["content"])
            print(f"  ✅ [SUBAGENT] 完成 ({turn + 1} 轮)")
            return result

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

            handler = SUB_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"未知工具: {block.name}"
            print(f"    └─ [SUB] {block.name}: {str(output)[:80]}")

            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": results})

    print(f"  ⚠️  [SUBAGENT] 达到上限 30 轮，强制返回")
    return "(子 Agent 达到上限，未完成)"


# 工具：6 基础 + todo_write + task
TOOLS = BASE_TOOLS + [
    {"name": "todo_write", "description": "创建和管理任务计划列表。",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "task", "description": "启动子 Agent 处理复杂子任务。子 Agent 有独立上下文，完成后只返回结论。",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string", "description": "子任务描述"}}, "required": ["description"]}},
]

TOOL_HANDLERS = {**BASE_HANDLERS, "todo_write": run_todo_write, "task": spawn_subagent}

register_default_hooks()

rounds_since_todo = 0


def agent_loop(messages: list):
    global rounds_since_todo
    while True:
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user", "content": "<reminder>请更新 todo_write。</reminder>"})
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
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"未知工具: {block.name}"

            trigger_hooks("PostToolUse", block, output)
            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("=" * 55)
    print("  s06: Subagent — 大任务拆小，上下文隔离")
    print("=" * 55)
    print()
    print("  新增 task 工具：启动子 Agent，全新上下文，只回传结论")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    while True:
        try:
            query = input("\033[36ms06 >>> \033[0m")
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
