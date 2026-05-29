#!/usr/bin/env python3
"""
s15_agent_teams/code.py — Agent Teams：一个搞不定，组队来

s06 的子 Agent 是临时工，叫来干一件事就走了。
但有些任务需要能通信、能协作的队友。

s15 新增三个核心机制：

① MessageBus（消息总线）：基于文件的收件箱，队友间异步通信
② spawn_teammate_thread（启动队友线程）：队友跑在 daemon 线程里
③ Inbox 注入：队友消息自动注入 Lead 的 messages

类比：s06 = 叫个外卖（一次性），s15 = 拉个群聊（持续协作）

架构：
  Lead: messages → LLM → TOOLS ────→ loop
            ↑                       │
            └── inbox ← MessageBus ← teammate.send_message

  Teammate: inbox → LLM → bash/read/write/send → loop (max 10 turns)

运行：cd s15_agent_teams && ..\.venv\Scripts\python.exe code.py
"""
import os, sys, json, time, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ★ 导入公共模块
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
MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


# ═══════════════════════════════════════════════════════════
#  ★【s15 新概念 1/3】MessageBus — 基于文件的收件箱
# ═══════════════════════════════════════════════════════════
#
# 为什么不直接用内存队列？
# ① 跨线程天然可见（文件系统共享）
# ② 直观可调试（可以去看 .mailboxes/ 下的 jsonl 文件）
# ③ 真实 Claude Code 也用的文件收件箱
#
# 教学版用 read + unlink（消费式读取），无文件锁。
# 真实 CC 用 proper-lockfile 防并发写冲突。

class MessageBus:
    """文件消息总线。每个 agent 一个 .jsonl 收件箱。
    读操作是破坏性的：读完就删。"""

    def send(self, from_agent: str, to_agent: str,
             content: str, msg_type: str = "message"):
        """往对方收件箱追加一条 JSON 消息"""
        msg = {
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "ts": time.time()
        }
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        print(f"  \033[33m[bus] {from_agent} → {to_agent}: {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        """读取收件箱并清空（消费式）"""
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in
                inbox.read_text(encoding="utf-8").splitlines() if line.strip()]
        inbox.unlink()  # 读完删除
        return msgs


BUS = MessageBus()

# 活跃队友注册表
active_teammates: dict[str, bool] = {}


# ═══════════════════════════════════════════════════════════
#  ★【s15 新概念 2/3】spawn_teammate_thread — 启动队友线程
# ═══════════════════════════════════════════════════════════
#
# 每个队友是一个独立 daemon 线程，有自己的：
# - system prompt（角色描述）
# - messages 列表（独立上下文）
# - 简化工具集（bash / read_file / write_file / send_message）
# - 最多 10 轮循环
#
# 真实 CC 用 idle loop（等收件箱 → 干活 → 空闲通知 → 循环）
# 教学版简化：最多 10 轮，干完自动退出并发送 summary。

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """在后台线程中启动一个队友 Agent"""
    if name in active_teammates:
        return f"队友 '{name}' 已经存在"

    system = (
        f"你是 '{name}'，一个 {role}。"
        f"用工具完成任务。完成后用 send_message 发送结果给 'lead'。"
        f"保持简洁，直接干活。"
    )

    # 队友的简化工具集
    sub_tools = [
        {"name": "bash", "description": "运行 shell 命令。",
         "input_schema": {"type": "object",
                          "properties": {"command": {"type": "string"}},
                          "required": ["command"]}},
        {"name": "read_file", "description": "读文件。",
         "input_schema": {"type": "object",
                          "properties": {"path": {"type": "string"}},
                          "required": ["path"]}},
        {"name": "write_file", "description": "写文件。",
         "input_schema": {"type": "object",
                          "properties": {"path": {"type": "string"},
                                         "content": {"type": "string"}},
                          "required": ["path", "content"]}},
        {"name": "send_message",
         "description": "发消息给其他 agent（通常给 'lead'）。",
         "input_schema": {"type": "object",
                          "properties": {"to": {"type": "string"},
                                         "content": {"type": "string"}},
                          "required": ["to", "content"]}},
    ]

    def run():
        messages = [{"role": "user", "content": prompt}]

        for _ in range(10):  # 最多 10 轮
            # 1. 检查收件箱
            inbox = BUS.read_inbox(name)
            if inbox:
                messages.append({
                    "role": "user",
                    "content": f"<inbox>{json.dumps(inbox, ensure_ascii=False)}</inbox>"
                })

            # 2. 调用 LLM
            try:
                response = client.messages.create(
                    model=MODEL, system=system, messages=messages[-20:],
                    tools=sub_tools, max_tokens=8000)
            except Exception as e:
                print(f"  \033[31m[teammate {name}] error: {e}\033[0m")
                break

            messages.append({"role": "assistant", "content": response.content})

            # 3. 如果 LLM 没有调用工具 → 完成
            if response.stop_reason != "tool_use":
                break

            # 4. 执行工具
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                print(f"\033[36m  [{name}] >>> {block.name}\033[0m")
                output = _teammate_tool_handler(block, name)
                print(f"\033[90m  [{name}] {str(output)[:100]}\033[0m")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output
                })

            messages.append({"role": "user", "content": results})

        # 5. 完成后发 summary 给 Lead
        summary = "任务完成。"
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text[:300]
                        break
                else:
                    continue
                break

        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)
        print(f"  \033[32m[teammate] {name} 已退出\033[0m")

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} 已启动（角色: {role}）\033[0m")
    return f"队友 '{name}' 已作为 {role} 启动"


def _teammate_tool_handler(block, agent_name: str) -> str:
    """队友的工具执行器"""
    import subprocess

    handlers = {
        "bash": lambda **kw: _safe_bash(kw.get("command", "")),
        "read_file": lambda **kw: _safe_read(kw.get("path", "")),
        "write_file": lambda **kw: _safe_write(kw.get("path", ""), kw.get("content", "")),
        "send_message": lambda **kw: (
            BUS.send(agent_name, kw.get("to", "lead"), kw.get("content", "")),
            "已发送"
        )[1],
    }
    h = handlers.get(block.name)
    return h(**block.input) if h else f"未知工具: {block.name}"


def _safe_bash(command: str) -> str:
    import subprocess
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip()
        return out[:10000] or "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误: 超时 (60s)"
    except Exception as e:
        return f"错误: {e}"


def _safe_read(path: str) -> str:
    try:
        fp = (WORKDIR / path).resolve()
        if not fp.is_relative_to(WORKDIR):
            return "错误: 路径在工作区之外"
        return fp.read_text(encoding="utf-8")[:5000]
    except Exception as e:
        return f"错误: {e}"


def _safe_write(path: str, content: str) -> str:
    try:
        fp = (WORKDIR / path).resolve()
        if not fp.is_relative_to(WORKDIR):
            return "错误: 路径在工作区之外"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节到 {path}"
    except Exception as e:
        return f"错误: {e}"


# ═══════════════════════════════════════════════════════════
#  ★【s15 新概念 3/3】Lead 端工具 — 管理队友
# ═══════════════════════════════════════════════════════════

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"消息已发给 {to}"


def run_check_inbox() -> str:
    msgs = BUS.read_inbox("lead")
    if not msgs:
        return "（收件箱为空）"
    lines = []
    for m in msgs:
        lines.append(f"  [{m['from']}] [{m['type']}] {m['content'][:200]}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  工具注册 & Agent 循环
# ═══════════════════════════════════════════════════════════

TOOLS = BASE_TOOLS + [
    {"name": "spawn_teammate",
     "description": "启动一个队友 Agent。队友在后台线程运行，通过 send_message 通信。",
     "input_schema": {
         "type": "object",
         "properties": {
             "name": {"type": "string", "description": "队友名字，如 alice"},
             "role": {"type": "string", "description": "队友角色，如 后端开发"},
             "prompt": {"type": "string", "description": "派给队友的任务描述"},
         },
         "required": ["name", "role", "prompt"]
     }},
    {"name": "send_message",
     "description": "发消息给队友。",
     "input_schema": {
         "type": "object",
         "properties": {
             "to": {"type": "string"},
             "content": {"type": "string"}
         },
         "required": ["to", "content"]
     }},
    {"name": "check_inbox",
     "description": "检查 Lead 的收件箱，查看队友发来的消息。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]

TOOL_HANDLERS = {
    **BASE_HANDLERS,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message,
    "check_inbox": run_check_inbox,
}

register_default_hooks()

SYSTEM = (
    f"你是 {WORKDIR} 下的 Lead Agent。用中文回答。\n"
    "你可以调用 spawn_teammate 启动队友协助完成任务。\n"
    "队友是独立线程，可通过 send_message / check_inbox 通信。\n"
)


def agent_loop(messages: list, context: dict) -> dict:
    while True:
        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM, messages=messages,
                tools=TOOLS, max_tokens=8000,
            )
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[错误] {e}"}
            ]})
            return context

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": str(force)})
                continue
            return context

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m>>> [{block.name}]\033[0m")
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
        return context


if __name__ == "__main__":
    print("=" * 55)
    print("  s15: Agent Teams — 组队干活")
    print("=" * 55)
    print()
    print("  新概念: MessageBus + spawn_teammate + inbox 注入")
    print()
    print("  vs s06 (子Agent):")
    print("    s06: 一次性临时工，只回传结果")
    print("    s15: 多轮队友，可双向通信")
    print()
    print("  试试:")
    print("    1. 启动 alice 当后端开发，让她建一个 schema.sql")
    print("    2. check_inbox 查看 alice 发来的结果")
    print("    3. send_message 让 alice 补充字段")
    print()
    print("  观察:  .mailboxes/ 目录下的 jsonl 文件")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    context = {"workspace": str(WORKDIR)}

    while True:
        try:
            query = input("\033[36ms15 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)

        # ── ★ s15 关键：每轮结束后自动检查收件箱 ──
        inbox = BUS.read_inbox("lead")
        if inbox:
            inbox_text = "\n".join(
                f"来自 {m['from']}: {m['content'][:200]}" for m in inbox
            )
            history.append({"role": "user", "content": f"[收件箱]\n{inbox_text}"})
            print(f"\n\033[33m[收件箱: {len(inbox)} 条新消息已注入]\033[0m")

        # 打印 Lead 的回复
        last = history[-1]
        if isinstance(last.get("content"), list):
            for block in last["content"]:
                if getattr(block, "type", None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")
        print()
