#!/usr/bin/env python3
"""
s13_background_tasks/code.py — 后台任务：慢操作丢后台，Agent 不干等

s12 的问题：pip install 要跑 10 分钟，npm build 要跑 3 分钟。
bash 工具一执行，Agent 就干等着——等 10 分钟什么也不做。
LLM 按 token 计费，空转就是浪费。

s13 的解法：慢操作丢到后台线程，Agent 继续跑循环。
后台完成后，结果以通知形式注入下一轮对话。

  类比：洗衣机。衣服丢进去，按启动，然后去干别的。
  你不会站在洗衣机前面盯着它转 30 分钟。

  同步模式 (s12):              后台模式 (s13):
  Agent → bash("pip install")   Agent → bash("pip install") [后台]
        │ 干等 10 分钟...              │ Agent 继续处理其他任务
        └→ 拿到结果                    │ 后台线程跑 pip install
                                       └→ 完成后注入通知

工具实现从 common/ 导入，本章聚焦线程调度 + 通知注入。
"""
import os, sys, json, time, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.tools import BASE_TOOLS, BASE_HANDLERS, run_bash
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


# ═══════════════════════════════════════════════════════════
#  ★【s13 新概念】后台任务系统
# ═══════════════════════════════════════════════════════════

_bg_counter = 0
background_tasks: dict[str, dict] = {}     # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}    # bg_id → output
background_lock = threading.Lock()


# ── 判断是否需要后台执行 ──
def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """启发式判断：命令是否可能耗时超过 30 秒"""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = [
        "install", "build", "test", "deploy", "compile",
        "docker build", "pip install", "npm install",
        "cargo build", "pytest", "make",
    ]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """
    模型显式请求优先 → 否则用关键词启发式兜底。
    模型可以传 run_in_background=True 主动要求后台执行。
    """
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, dict(tool_input))


# ── 在后台线程中执行工具 ──
def execute_tool(block) -> str:
    """同步执行工具（前台和后台都用这个）"""
    handler = TOOL_HANDLERS.get(block.name)
    if not handler:
        return f"未知工具: {block.name}"
    return handler(**block.input)


# ── 启动后台任务 ──
def start_background_task(block) -> str:
    """在 daemon 线程中执行工具，立即返回 bg_id"""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"

    cmd = block.input.get("command", str(block.input)[:60])

    def worker():
        result = execute_tool(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"  🔄 [后台] 已派发 {bg_id}: {cmd[:50]}")
    return bg_id


# ── 收集已完成的后台结果 ──
def collect_background_results() -> list[str]:
    """收集已完成的后台任务，格式化为通知"""
    with background_lock:
        ready = [bid for bid, t in background_tasks.items()
                 if t["status"] == "completed"]

    notifications = []
    for bg_id in ready:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "(无输出)")

        summary = output[:300] if len(output) > 300 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>"
        )
        print(f"  ✅ [后台完成] {bg_id}: {task['command'][:50]}")

    return notifications


# ═══════════════════════════════════════════════════════════
#  工具定义：bash 增加 run_in_background 参数
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "执行 Shell 命令。慢操作用 run_in_background=true 丢后台。",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string"},
         "run_in_background": {"type": "boolean", "description": "设为 true 在后台执行，不阻塞"}
     }, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "精确替换文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "按通配符查找。",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

TOOL_HANDLERS = BASE_HANDLERS
register_default_hooks()

SYSTEM = (
    f"你是 {WORKDIR} 下的编码助手。用中文回答。\n"
    "慢操作（install/build/test/deploy）用 run_in_background=true 丢后台，"
    "Agent 不等待，继续处理其他任务。"
)


# ═══════════════════════════════════════════════════════════
#  agent_loop — 后台 vs 前台的分岔口
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    while True:
        # ★ 在调用 LLM 之前，先收集已完成的后台通知
        bg_notifications = collect_background_results()
        if bg_notifications:
            notification_text = "\n\n".join(bg_notifications)
            messages.append({"role": "user", "content": notification_text})
            print(f"  📨 [注入] {len(bg_notifications)} 条后台通知")

        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM, messages=messages,
                tools=TOOLS, max_tokens=8000,
            )
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[错误] {e}"}
            ]})
            return

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force: messages.append({"role": "user", "content": str(force)}); continue
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            print(f"\033[36m>>> [{block.name}]\033[0m")

            # ★ s13 核心：慢操作走后台，快操作走前台
            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block)
                # 立即返回占位符，不阻塞
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": (
                        f"[后台任务 {bg_id} 已启动]\n"
                        f"命令: {block.input.get('command', '')}\n"
                        f"完成后结果会自动通知。"
                    ),
                })
            else:
                # 快操作：同步执行
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)})
                    continue
                h = TOOL_HANDLERS.get(block.name)
                output = h(**block.input) if h else f"未知: {block.name}"
                print(str(output)[:200])
                trigger_hooks("PostToolUse", block, output)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("=" * 55)
    print("  s13: Background Tasks — 慢操作丢后台")
    print("=" * 55)
    print()
    print("  同步: Agent 干等慢命令结束")
    print("  后台: daemon 线程执行，Agent 继续处理")
    print()
    print("  判断标准:")
    print("    1. 模型显式传 run_in_background=true")
    print("    2. 关键词兜底（install/build/test/deploy...）")
    print()
    print("  试试：")
    print("    1. 用 run_in_background=true 执行 pip list")
    print("    2. 同时列出当前目录所有 .py 文件")
    print("    → Agent 不会等 pip list 完成")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    while True:
        try:
            query = input("\033[36ms13 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！"); break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！"); break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        last = history[-1]["content"]
        if isinstance(last, list):
            for b in last:
                if getattr(b, "type", None) == "text":
                    print(f"\n\033[32m{b.text}\033[0m")
        print()
