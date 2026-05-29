#!/usr/bin/env python3
"""
s17_autonomous_agents/code.py — Autonomous Agents：自己看板，自己认领

s16 的队友能通信、能握手关机。但每个队友等 Lead 分配任务——
如果任务看板上有 10 个未认领任务，Lead 得手动 assign 10 次。这不 scalable。

s17 的解法：队友变成自主的。空闲时自己扫描任务看板，发现没人做的任务就认领，
做完再找下一个。Lead 只需要创建任务，队友自己发现、自己认领、自己完成。

─────────────────────────────────────────────────────────
s17 新增三个核心机制：

① scan_unclaimed_tasks — 扫描看板上可认领的任务
② idle_poll — 空闲时每 5 秒轮询收件箱 + 任务看板
③ WORK → IDLE 循环 — 队友不再干完就退出，而是进入空闲等待新任务

类比：s16 = 等老板派活的小时工，s17 = 自己看任务板的自由职业者

队友三阶段生命周期：
  WORK:   inbox → LLM → 工具 → 循环（干活）
  IDLE:   每5s轮询 inbox + 任务看板（找活干）
  SHUTDOWN: 收到关机请求 → 退出

运行：cd s17_autonomous_agents && ..\.venv\Scripts\python.exe code.py
"""
import os, sys, json, time, random, threading, subprocess
from pathlib import Path
from dataclasses import dataclass, field, asdict

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
MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


# ═══════════════════════════════════════════════════════════
#  MessageBus（同 s15/s16）
# ═══════════════════════════════════════════════════════════

class MessageBus:
    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type, "ts": time.time()}
        if metadata:
            msg["metadata"] = metadata
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in
                inbox.read_text(encoding="utf-8").splitlines() if line.strip()]
        inbox.unlink()
        return msgs


BUS = MessageBus()
active_teammates: dict[str, bool] = {}


# ═══════════════════════════════════════════════════════════
#  协议状态机（同 s16）
# ═══════════════════════════════════════════════════════════

@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    created_at: float = field(default_factory=time.time)

pending_requests: dict[str, ProtocolState] = {}

def new_request_id() -> str:
    return f"req_{random.randint(100000, 999999)}"

def match_response(response_type: str, request_id: str, approve: bool):
    state = pending_requests.get(request_id)
    if not state:
        return
    expected = state.type + "_response"
    if response_type != expected:
        return
    if state.status != "pending":
        return
    state.status = "approved" if approve else "rejected"
    print(f"  \033[3{2 if approve else 1}m[协议] {state.type} {'✓' if approve else '✗'} ({request_id})\033[0m")

def consume_lead_inbox() -> list[dict]:
    msgs = BUS.read_inbox("lead")
    for msg in msgs:
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        if req_id and msg.get("type", "").endswith("_response"):
            match_response(msg["type"], req_id, meta.get("approve", False))
    return msgs


# ═══════════════════════════════════════════════════════════
#  任务系统（最小实现，聚焦 s17 新概念）
# ═══════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)

_task_counter = [0]

@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str      # pending | in_progress | completed
    owner: str
    blockedBy: list[str]

def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"

def create_task(subject: str, description: str = "", blockedBy: list[str] | None = None) -> Task:
    _task_counter[0] += 1
    task = Task(
        id=f"task_{_task_counter[0]:04d}",
        subject=subject, description=description,
        status="pending", owner="",
        blockedBy=blockedBy or [])
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2, ensure_ascii=False))
    return task

def load_task(task_id: str) -> Task | None:
    p = _task_path(task_id)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    return Task(**d)

def list_tasks() -> list[Task]:
    tasks = []
    for p in sorted(TASKS_DIR.glob("task_*.json")):
        tasks.append(Task(**json.loads(p.read_text())))
    return tasks

def save_task(task: Task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2, ensure_ascii=False))

def can_start(task_id: str) -> bool:
    task = load_task(task_id)
    if not task:
        return False
    for dep_id in task.blockedBy:
        dep = load_task(dep_id)
        if not dep or dep.status != "completed":
            return False
    return True

def claim_task(task_id: str, owner: str) -> str:
    task = load_task(task_id)
    if not task:
        return f"任务 {task_id} 不存在"
    if task.status != "pending":
        return f"任务 {task_id} 当前状态 {task.status}"
    if task.owner:
        return f"任务 {task_id} 已被 {task.owner} 认领"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not load_task(d) or load_task(d).status != "completed"]
        return f"任务被阻塞: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    return f"已认领 {task_id} ({task.subject})"

def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if not task:
        return f"任务 {task_id} 不存在"
    if task.status != "in_progress":
        return f"任务 {task_id} 当前状态 {task.status}"
    task.status = "completed"
    save_task(task)
    return f"已完成 {task_id} ({task.subject})"


# ═══════════════════════════════════════════════════════════
#  ★【s17 新概念 1/3】scan_unclaimed_tasks — 扫描可认领任务
# ═══════════════════════════════════════════════════════════
#
# 三个条件：
#   ① status == "pending"（等待中）
#   ② owner 为空（没人认领）
#   ③ can_start()（所有 blockedBy 依赖已完成）

def scan_unclaimed_tasks() -> list[dict]:
    """扫描看板上所有可认领的任务"""
    unclaimed = []
    for p in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(p.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed


# ═══════════════════════════════════════════════════════════
#  ★【s17 新概念 2/3】idle_poll — 空闲轮询
# ═══════════════════════════════════════════════════════════
#
# 队友干完活后进入 IDLE 阶段，每 5 秒检查一次：
#   ① 收件箱（优先！可能有 shutdown_request）
#   ② 任务看板（有没有新任务可以认领）
#
# 优先级：收件箱 > 任务看板

IDLE_POLL_INTERVAL = 5   # 每 5 秒轮询一次
IDLE_TIMEOUT = 60         # 60 秒无新任务则超时退出

def idle_poll(agent_name: str, messages: list, name: str, role: str) -> str:
    """空闲轮询。返回 'work'（有新活）、'shutdown'（收到关机请求）、'timeout'（超时）"""
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)

        # ★ 优先检查收件箱（可能包含 shutdown_request 等协议消息）
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    # 在 IDLE 阶段直接处理关机，不等下一轮 WORK
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "正在关机。",
                             "shutdown_response",
                             {"request_id": req_id, "approve": True})
                    print(f"  \033[35m[协议] {name} 在空闲中同意关机 ({req_id})\033[0m")
                    return "shutdown"
                if msg.get("type") == "plan_approval_response":
                    meta = msg.get("metadata", {})
                    if meta.get("approve"):
                        messages.append({"role": "user", "content": "[计划已审批] 可以开始执行。"})
                    else:
                        messages.append({"role": "user",
                            "content": f"[计划被驳回] 反馈: {msg['content']}"})
                else:
                    # 普通消息注入上下文
                    messages.append({"role": "user",
                        "content": f"<inbox>{json.dumps([msg], ensure_ascii=False)}</inbox>"})
            print(f"  \033[36m[idle] {name} 发现收件箱消息，回到 WORK\033[0m")
            return "work"

        # ★ 然后扫描任务看板
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]  # 取第一个可认领的任务
            result = claim_task(task["id"], agent_name)
            if "已认领" in result or "Claimed" in result:
                messages.append({"role": "user",
                    "content": f"<auto-claimed>任务 {task['id']}: {task['subject']}</auto-claimed>"})
                print(f"  \033[32m[idle] {name} 自动认领: {task['subject']}\033[0m")
                return "work"
            print(f"  \033[33m[idle] {name} 认领失败: {result}\033[0m")

    print(f"  \033[31m[idle] {name} 超时 ({IDLE_TIMEOUT}s)，退出\033[0m")
    return "timeout"


# ═══════════════════════════════════════════════════════════
#  ★【s17 新概念 3/3】队友线程：WORK → IDLE 循环
# ═══════════════════════════════════════════════════════════
#
# s16 的队友：WORK → IDLE（只等 shutdown）
# s17 的队友：WORK → IDLE → 自动认领新任务 → WORK → IDLE → ...
#             循环直到收到 shutdown_request 或 IDLE 超时

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    if name in active_teammates:
        return f"队友 '{name}' 已存在"

    system = (
        f"你是 '{name}'，一个 {role}。"
        f"用工具完成任务。可以 list_tasks / claim_task / complete_task。"
        f"完成后用 send_message 发送结果给 'lead'。"
    )

    sub_tools = [
        {"name": "bash", "input_schema": {"type": "object",
            "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "input_schema": {"type": "object",
            "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "write_file", "input_schema": {"type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}},
        {"name": "send_message", "input_schema": {"type": "object",
            "properties": {"to": {"type": "string"}, "content": {"type": "string"}},
            "required": ["to", "content"]}},
        {"name": "submit_plan", "input_schema": {"type": "object",
            "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
        # ★ s17：队友可以自己看板、认领、完成
        {"name": "list_tasks", "input_schema": {"type": "object",
            "properties": {}, "required": []}},
        {"name": "claim_task", "input_schema": {"type": "object",
            "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"name": "complete_task", "input_schema": {"type": "object",
            "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    ]

    def run():
        messages = [{"role": "user", "content": prompt}]

        # ★ 外层循环: WORK → IDLE → WORK → IDLE → ...
        while True:
            # ── WORK 阶段 ──
            for _ in range(10):  # 最多 10 轮工具调用
                inbox = BUS.read_inbox(name)
                shutdown = False
                for msg in inbox:
                    meta = msg.get("metadata", {})
                    req_id = meta.get("request_id", "")
                    msg_type = msg.get("type", "")

                    if msg_type == "shutdown_request":
                        BUS.send(name, "lead", "正在关机。",
                                 "shutdown_response",
                                 {"request_id": req_id, "approve": True})
                        print(f"  \033[35m[协议] {name} 同意关机 ({req_id})\033[0m")
                        shutdown = True
                        break
                    elif msg_type == "plan_approval_response":
                        approve = meta.get("approve", False)
                        messages.append({"role": "user",
                            "content": f"[计划已{'通过' if approve else '驳回'}] {msg.get('content', '')}"})
                    else:
                        messages.append({"role": "user",
                            "content": f"<inbox>{json.dumps(msg, ensure_ascii=False)}</inbox>"})

                if shutdown:
                    break

                if messages and isinstance(messages[-1].get("content"), list):
                    # 上一轮是 assistant 的 tool_use 回复，不需要额外用户消息
                    pass
                elif inbox and not any(msg.get("type") == "plan_approval_response" for msg in inbox):
                    pass

                try:
                    response = client.messages.create(
                        model=MODEL, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=8000)
                except Exception as e:
                    print(f"  \033[31m[队友 {name}] 错误: {e}\033[0m")
                    break

                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason != "tool_use":
                    break  # LLM 没有调用工具 → 干完活了

                # 执行工具
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    print(f"\033[36m  [{name}] >>> {block.name}\033[0m")
                    output = _teammate_tool(name, block)
                    print(f"\033[90m  [{name}] {str(output)[:100]}\033[0m")
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id, "content": output})
                messages.append({"role": "user", "content": results})

            if shutdown:
                break

            # ── IDLE 阶段（s17 新增）──
            idle_result = idle_poll(name, messages, name, role)
            if idle_result == "shutdown":
                break
            if idle_result == "timeout":
                break
            # idle_result == "work" → 回到外层循环的 WORK 阶段

        # 关机
        BUS.send(name, "lead", f"{name} 已关机。", "result")
        active_teammates.pop(name, None)
        print(f"  \033[32m[队友] {name} 已关机\033[0m")

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[队友] {name} 已启动（角色: {role}）\033[0m")
    return f"队友 '{name}' 已作为 {role} 启动"


def _teammate_tool(name: str, block) -> str:
    handlers = {
        "bash": lambda **kw: _safe_bash(kw.get("command", "")),
        "read_file": lambda **kw: _safe_read(kw.get("path", "")),
        "write_file": lambda **kw: _safe_write(kw.get("path", ""), kw.get("content", "")),
        "send_message": lambda **kw: (BUS.send(name, kw.get("to", "lead"),
                                                kw.get("content", "")), "已发送")[1],
        "submit_plan": lambda **kw: _submit_plan(name, kw.get("plan", "")),
        "list_tasks": lambda: _run_list_tasks(),
        "claim_task": lambda **kw: claim_task(kw.get("task_id", ""), name),
        "complete_task": lambda **kw: complete_task(kw.get("task_id", "")),
    }
    h = handlers.get(block.name)
    return h(**block.input) if h else f"未知: {block.name}"


def _submit_plan(from_name: str, plan: str) -> str:
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead", status="pending", payload=plan)
    BUS.send(from_name, "lead", plan, "plan_approval_request", {"request_id": req_id})
    return f"计划已提交 ({req_id})"


def _run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "没有任务。"
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "◉", "completed": "✓"}.get(t.status, "?")
        owner = f" [{t.owner}]" if t.owner else ""
        deps = f" (依赖: {','.join(t.blockedBy)})" if t.blockedBy else ""
        lines.append(f"  {icon} {t.id}: {t.subject} [{t.status}]{owner}{deps}")
    return "\n".join(lines)


def _safe_bash(cmd: str) -> str:
    try:
        r = subprocess.run(cmd, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=60)
        return (r.stdout + r.stderr).strip()[:10000] or "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误: 超时"
    except Exception as e:
        return f"错误: {e}"


def _safe_read(path: str) -> str:
    try:
        fp = (WORKDIR / path).resolve()
        return fp.read_text(encoding="utf-8")[:5000] if fp.is_relative_to(WORKDIR) else "路径越界"
    except Exception as e:
        return f"错误: {e}"


def _safe_write(path: str, content: str) -> str:
    try:
        fp = (WORKDIR / path).resolve()
        if not fp.is_relative_to(WORKDIR):
            return "路径越界"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节到 {path}"
    except Exception as e:
        return f"错误: {e}"


# ═══════════════════════════════════════════════════════════
#  Lead 端工具
# ═══════════════════════════════════════════════════════════

def run_spawn_teammate(name, role, prompt): return spawn_teammate_thread(name, role, prompt)
def run_send_message(to, content): BUS.send("lead", to, content); return f"消息已发给 {to}"
def run_check_inbox():
    msgs = consume_lead_inbox()
    if not msgs: return "（收件箱为空）"
    return "\n".join(f"  [{m['from']}] [{m['type']}] {m['content'][:200]}" for m in msgs)

def run_request_shutdown(teammate: str) -> str:
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown", sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "请关机。", "shutdown_request", {"request_id": req_id})
    return f"关机请求已发给 {teammate} (req: {req_id})"

def run_request_plan(teammate, task): BUS.send("lead", teammate, f"请提交计划: {task}"); return f"已要求 {teammate} 提交计划"
def run_review_plan(request_id, approve, feedback=""):
    state = pending_requests.get(request_id)
    if not state: return f"请求 {request_id} 不存在"
    if state.status != "pending": return f"请求 {request_id} 已经是 {state.status}"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender, feedback or ("通过" if approve else "驳回"),
             "plan_approval_response", {"request_id": request_id, "approve": approve})
    return f"计划已{'通过' if approve else '驳回'} ({request_id})"

# ★ s17：Lead 可以创建任务，队友自己认领
def run_create_task(subject, description=""):
    task = create_task(subject, description)
    return f"任务已创建: {task.id} ({subject})"

def run_list_tasks_lead():
    tasks = list_tasks()
    if not tasks: return "没有任务。"
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "◉", "completed": "✓"}.get(t.status, "?")
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} [{t.status}]{owner}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  工具注册 & Agent 循环
# ═══════════════════════════════════════════════════════════

TOOLS = BASE_TOOLS + [
    {"name": "spawn_teammate", "input_schema": {"type": "object",
        "properties": {"name": {"type": "string"}, "role": {"type": "string"},
                       "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "send_message", "input_schema": {"type": "object",
        "properties": {"to": {"type": "string"}, "content": {"type": "string"}},
        "required": ["to", "content"]}},
    {"name": "check_inbox", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "request_shutdown", "input_schema": {"type": "object",
        "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "request_plan", "input_schema": {"type": "object",
        "properties": {"teammate": {"type": "string"}, "task": {"type": "string"}},
        "required": ["teammate", "task"]}},
    {"name": "review_plan", "input_schema": {"type": "object",
        "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"},
                       "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    # ★ s17：Lead 创建任务，队友自己认领
    {"name": "create_task", "description": "创建任务放入看板。队友空闲时会自动认领。",
     "input_schema": {"type": "object",
        "properties": {"subject": {"type": "string"}, "description": {"type": "string"}},
        "required": ["subject"]}},
    {"name": "list_tasks", "description": "查看任务看板。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]

TOOL_HANDLERS = {
    **BASE_HANDLERS,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message,
    "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan,
    "review_plan": run_review_plan,
    "create_task": run_create_task,
    "list_tasks": run_list_tasks_lead,
}

register_default_hooks()

SYSTEM = (
    f"你是 {WORKDIR} 下的 Lead Agent。用中文回答。\n"
    "你可以:\n"
    "  1. spawn_teammate 启动自主队友\n"
    "  2. create_task 创建任务放入看板，队友空闲时会自动认领\n"
    "  3. request_shutdown 让队友关机\n"
)


def agent_loop(messages: list, context: dict) -> dict:
    while True:
        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[错误] {e}"}]})
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
    print("  s17: Autonomous Agents — 自己看板，自己认领")
    print("=" * 55)
    print()
    print("  s16 vs s17:")
    print("    s16: 队友等 Lead 分配任务")
    print("    s17: 队友空闲时自己扫描任务看板，自动认领")
    print()
    print("  三阶段生命周期: WORK → IDLE → WORK → ...")
    print("    每 5 秒轮询收件箱 + 任务看板，60 秒超时")
    print()
    print("  试试:")
    print("    1. create_task 创建 3 个任务")
    print("    2. spawn 队友 bob")
    print("    3. 观察 bob 自动认领、完成、再自动认领下一个")
    print("    4. request_shutdown 让 bob 关机")
    print()
    print("  观察: .tasks/ 下的任务状态变化、IDLE 阶段的打印")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    context = {"workspace": str(WORKDIR)}

    while True:
        try:
            query = input("\033[36ms17 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)

        # 消费收件箱
        inbox_msgs = consume_lead_inbox()
        for msg in inbox_msgs:
            if msg.get("type") == "plan_approval_request":
                history.append({"role": "user",
                    "content": f"[计划审批] {msg['from']}: {msg['content']} "
                               f"(request_id: {msg['metadata']['request_id']})"})
                print(f"\n\033[33m[计划审批请求] {msg['from']}: {msg['content'][:80]}\033[0m")
            elif msg.get("type") == "result":
                history.append({"role": "user",
                    "content": f"[队友结果] {msg['from']}: {msg['content'][:200]}"})

        last = history[-1]
        if isinstance(last.get("content"), list):
            for block in last["content"]:
                if getattr(block, "type", None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")
        print()
