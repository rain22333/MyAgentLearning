#!/usr/bin/env python3
"""
s18_worktree_isolation/code.py — Worktree Isolation：各干各的，互不干扰

s17 中队友能自主认领任务了，但所有人都在同一个目录下工作。
Alice 改 config.py，Bob 也改 config.py——互相覆盖，分不清谁的改动。

s15-17 解决了"谁干什么"（任务系统）和"怎么通信"（消息总线），
但没解决"在哪干"的问题。

s18 的解法：每个任务绑一个 Git worktree（独立目录 + 独立分支）。
Alice 在 .worktrees/auth-refactor/ 下干活，
Bob 在 .worktrees/ui-login/ 下干活——互不干扰。

─────────────────────────────────────────────────────────
s18 新增四个核心机制：

① create_worktree — 为任务创建独立 Git 工作目录 + 分支
② bind_task_to_worktree — 将任务与 worktree 绑定（不改任务状态）
③ cwd 切换 — 队友认领绑了 worktree 的任务时，工具自动切到对应目录
④ 收尾：keep/remove — 完成后清理或保留分支

安全：name 校验防止路径穿越，有改动时拒绝删除

运行：cd s18_worktree_isolation && ..\.venv\Scripts\python.exe code.py
前提：当前目录是一个 git 仓库
"""
import os, sys, json, time, random, threading, subprocess, re
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
#  MessageBus + 协议（同 s15-s17）
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

@dataclass
class ProtocolState:
    request_id: str; type: str; sender: str; target: str
    status: str; payload: str
    created_at: float = field(default_factory=time.time)

pending_requests: dict[str, ProtocolState] = {}

def new_request_id(): return f"req_{random.randint(100000, 999999)}"

def match_response(response_type, request_id, approve):
    state = pending_requests.get(request_id)
    if not state or state.status != "pending": return
    if response_type != state.type + "_response": return
    state.status = "approved" if approve else "rejected"
    print(f"  \033[3{2 if approve else 1}m[协议] {state.type} {'✔' if approve else '✗'} ({request_id})\033[0m")

def consume_lead_inbox():
    msgs = BUS.read_inbox("lead")
    for msg in msgs:
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        if req_id and msg.get("type", "").endswith("_response"):
            match_response(msg["type"], req_id, meta.get("approve", False))
    return msgs


# ═══════════════════════════════════════════════════════════
#  任务系统（同 s17，新增 worktree 字段）
# ═══════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)
_task_counter = [0]

@dataclass
class Task:
    id: str; subject: str; description: str
    status: str; owner: str; blockedBy: list[str]
    worktree: str = ""  # ★ s18 新增

def _task_path(task_id): return TASKS_DIR / f"{task_id}.json"

def create_task(subject, description="", blockedBy=None):
    _task_counter[0] += 1
    task = Task(id=f"task_{_task_counter[0]:04d}", subject=subject,
                description=description, status="pending", owner="",
                blockedBy=blockedBy or [], worktree="")
    save_task(task)
    return task

def load_task(task_id):
    p = _task_path(task_id)
    if not p.exists(): return None
    return Task(**json.loads(p.read_text()))

def list_tasks():
    return [Task(**json.loads(p.read_text())) for p in sorted(TASKS_DIR.glob("task_*.json"))]

def save_task(task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2, ensure_ascii=False))

def can_start(task_id):
    task = load_task(task_id)
    if not task: return False
    for dep_id in task.blockedBy:
        dep = load_task(dep_id)
        if not dep or dep.status != "completed": return False
    return True

def claim_task(task_id, owner):
    task = load_task(task_id)
    if not task: return f"任务不存在"
    if task.status != "pending": return f"状态是 {task.status}"
    if task.owner: return f"已被 {task.owner} 认领"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy if not load_task(d) or load_task(d).status != "completed"]
        return f"被阻塞: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    return f"已认领 {task_id} ({task.subject})" + (f" [worktree: {task.worktree}]" if task.worktree else "")

def complete_task(task_id):
    task = load_task(task_id)
    if not task: return f"任务不存在"
    if task.status != "in_progress": return f"状态是 {task.status}"
    task.status = "completed"
    save_task(task)
    return f"已完成 {task_id} ({task.subject})"


# ═══════════════════════════════════════════════════════════
#  ★【s18 新概念 1/4】validate_worktree_name — 安全校验
# ═══════════════════════════════════════════════════════════
#
# 防止路径穿越攻击：拒绝 ".."、"/"、"\"

VALID_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

def validate_worktree_name(name: str) -> str | None:
    """校验 worktree 名称，合法返回 None，非法返回错误信息"""
    if not VALID_NAME.match(name):
        return f"名称不合法: '{name}'（只允许字母数字 ._- 最多64字符）"
    return None


# ═══════════════════════════════════════════════════════════
#  ★【s18 新概念 2/4】create_worktree — 创建独立工作目录
# ═══════════════════════════════════════════════════════════

WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_DIR.mkdir(exist_ok=True)

def _run_git(args: list[str]) -> tuple[bool, str]:
    """执行 git 命令，返回 (成功, 输出)"""
    try:
        r = subprocess.run(["git"] + args, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except Exception as e:
        return False, str(e)


def create_worktree(name: str, task_id: str = "") -> str:
    """创建一个独立的 git worktree。

    步骤:
    1. 校验名称
    2. git worktree add <path> -b wt/<name> HEAD
    3. 可选：绑定到任务
    """
    err = validate_worktree_name(name)
    if err:
        return f"错误: {err}"

    path = WORKTREES_DIR / name
    if path.exists():
        # 检查是否已经是 worktree
        ok, out = _run_git(["worktree", "list"])
        if ok and str(path) in out:
            return _do_bind(name, task_id, path)
        return f"错误: 目录已存在但不是 worktree: {path}"

    # 创建 worktree
    ok, out = _run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git 错误: {out}"

    log_event("create", name, task_id)
    return _do_bind(name, task_id, path)


def _do_bind(name, task_id, path):
    """绑定任务到 worktree"""
    result = f"Worktree '{name}' 已创建于 {path}"
    if task_id:
        bind_result = bind_task_to_worktree(task_id, name)
        result += f"\n{bind_result}"
    return result


# ═══════════════════════════════════════════════════════════
#  ★【s18 新概念 3/4】bind_task_to_worktree — 任务与目录绑定
# ═══════════════════════════════════════════════════════════
#
# 关键设计：绑定不修改任务状态。任务仍然是 pending，
# 队友空闲时自己认领带 worktree 的任务，认领后自动切换 cwd。

def bind_task_to_worktree(task_id: str, worktree_name: str) -> str:
    """将任务绑定到 worktree。不改变任务状态。"""
    task = load_task(task_id)
    if not task:
        return f"任务 {task_id} 不存在"
    task.worktree = worktree_name
    save_task(task)
    log_event("bind", worktree_name, task_id)
    print(f"  \033[36m[worktree] 任务 {task_id} 绑定到 {worktree_name}\033[0m")
    return f"任务 {task_id} 已绑定到 worktree '{worktree_name}'"


# ═══════════════════════════════════════════════════════════
#  ★【s18 新概念 4/4】收尾：keep vs remove
# ═══════════════════════════════════════════════════════════

def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """删除 worktree。

    安全措施：有未提交改动时默认拒绝，除非 discard_changes=true。
    """
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"Worktree '{name}' 不存在"

    if not discard_changes:
        # 检查是否有改动
        ok, out = _run_git(["-C", str(path), "status", "--porcelain"])
        if ok and out.strip():
            return (f"Worktree '{name}' 有未提交改动。"
                    f"使用 discard_changes=true 强制删除，或 keep_worktree 保留。")

    # 删除
    ok, out = _run_git(["worktree", "remove", str(path), "--force"])
    if not ok:
        return f"删除失败: {out}"

    _run_git(["branch", "-D", f"wt/{name}"])  # 清理分支（忽略错误）
    log_event("remove", name)
    return f"Worktree '{name}' 已删除"


def keep_worktree(name: str) -> str:
    """保留 worktree（分支仍存在，可供后续 review）"""
    log_event("keep", name)
    return f"Worktree '{name}' 已保留（分支: wt/{name}）"


def list_worktrees() -> str:
    ok, out = _run_git(["worktree", "list"])
    return out if ok else "无法列出 worktrees"


# 事件日志（审计用）
EVENTS_LOG = WORKTREES_DIR / "events.jsonl"

def log_event(action, name, task_id=""):
    try:
        entry = {"action": action, "name": name, "task_id": task_id,
                 "ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(EVENTS_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  空闲轮询 & 队友线程（同 s17，新增 worktree cwd 切换）
# ═══════════════════════════════════════════════════════════

IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

def scan_unclaimed_tasks():
    unclaimed = []
    for p in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(p.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed

def idle_poll(agent_name, messages, name, role):
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)

        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "正在关机。", "shutdown_response",
                             {"request_id": req_id, "approve": True})
                    print(f"  \033[35m[协议] {name} 空闲中同意关机 ({req_id})\033[0m")
                    return "shutdown"
                if msg.get("type") == "plan_approval_response":
                    meta = msg.get("metadata", {})
                    tag = "通过" if meta.get("approve") else "驳回"
                    messages.append({"role": "user", f"content": f"[计划已{tag}] {msg.get('content', '')}"})
                else:
                    messages.append({"role": "user", "content": f"<inbox>{json.dumps([msg], ensure_ascii=False)}</inbox>"})
            print(f"  \033[36m[idle] {name} 收到收件箱消息\033[0m")
            return "work"

        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]
            result = claim_task(task["id"], agent_name)
            if "已认领" in result or "Claimed" in result:
                wt_info = f" [worktree: {task.get('worktree', '')}]" if task.get("worktree") else ""
                messages.append({"role": "user",
                    "content": f"<auto-claimed>任务 {task['id']}: {task['subject']}{wt_info}</auto-claimed>"})
                print(f"  \033[32m[idle] {name} 自动认领: {task['subject']}{wt_info}\033[0m")
                return "work"
            print(f"  \033[33m[idle] {name} 认领失败: {result}\033[0m")

    print(f"  \033[31m[idle] {name} 超时 ({IDLE_TIMEOUT}s)\033[0m")
    return "timeout"


def spawn_teammate_thread(name, role, prompt):
    if name in active_teammates:
        return f"队友 '{name}' 已存在"

    system = (
        f"你是 '{name}'，一个 {role}。用工具完成任务。"
        f"可以 list_tasks / claim_task / complete_task。"
        f"注意：你的 bash/read/write 操作会在已绑定的 worktree 目录下执行。"
    )

    # ★ s18：队友维护当前 worktree 上下文
    wt_ctx = {"path": None}

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
        {"name": "list_tasks", "input_schema": {"type": "object", "properties": {}, "required": []}},
        {"name": "claim_task", "input_schema": {"type": "object",
            "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"name": "complete_task", "input_schema": {"type": "object",
            "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    ]

    def run():
        messages = [{"role": "user", "content": prompt}]

        while True:
            for _ in range(10):
                inbox = BUS.read_inbox(name)
                shutdown = False
                for msg in inbox:
                    meta = msg.get("metadata", {})
                    req_id = meta.get("request_id", "")
                    if msg.get("type") == "shutdown_request":
                        BUS.send(name, "lead", "正在关机。", "shutdown_response",
                                 {"request_id": req_id, "approve": True})
                        shutdown = True; break
                    elif msg.get("type") == "plan_approval_response":
                        tag = "通过" if meta.get("approve") else "驳回"
                        messages.append({"role": "user", "content": f"[计划已{tag}] {msg.get('content', '')}"})
                    else:
                        messages.append({"role": "user", "content": f"<inbox>{json.dumps(msg, ensure_ascii=False)}</inbox>"})
                if shutdown: break

                try:
                    response = client.messages.create(
                        model=MODEL, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=8000)
                except Exception as e:
                    print(f"  \033[31m[队友 {name}] 错误: {e}\033[0m")
                    break

                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use": break

                results = []
                for block in response.content:
                    if block.type != "tool_use": continue
                    print(f"\033[36m  [{name}] >>> {block.name}\033[0m")
                    output = _teammate_tool(name, block, wt_ctx)
                    print(f"\033[90m  [{name}] {str(output)[:150]}\033[0m")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                messages.append({"role": "user", "content": results})

            if shutdown: break
            idle_result = idle_poll(name, messages, name, role)
            if idle_result == "shutdown": break
            if idle_result == "timeout": break

        BUS.send(name, "lead", f"{name} 已关机。", "result")
        active_teammates.pop(name, None)
        print(f"  \033[32m[队友] {name} 已关机\033[0m")

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[队友] {name} 已启动（角色: {role}）\033[0m")
    return f"队友 '{name}' 已作为 {role} 启动"


def _teammate_tool(name, block, wt_ctx):
    """队友工具执行器。★ s18：认领任务时自动切换 cwd"""

    def _claim(task_id):
        result = claim_task(task_id, name)
        if "已认领" in result or "Claimed" in result:
            task = load_task(task_id)
            if task and task.worktree:
                wt_ctx["path"] = str(WORKTREES_DIR / task.worktree)
                print(f"  \033[36m  [{name}] cwd → {wt_ctx['path']}\033[0m")
        return result

    cwd = wt_ctx["path"]  # worktree 路径，None 表示用默认

    handlers = {
        "bash": lambda **kw: _safe_bash(kw.get("command", ""), cwd),
        "read_file": lambda **kw: _safe_read(kw.get("path", ""), cwd),
        "write_file": lambda **kw: _safe_write(kw.get("path", ""), kw.get("content", ""), cwd),
        "send_message": lambda **kw: (BUS.send(name, kw.get("to", "lead"),
                                                kw.get("content", "")), "已发送")[1],
        "submit_plan": lambda **kw: _submit_plan(name, kw.get("plan", "")),
        "list_tasks": lambda: _list_tasks_summary(),
        "claim_task": _claim,
        "complete_task": lambda **kw: complete_task(kw.get("task_id", "")),
    }
    h = handlers.get(block.name)
    return h(**block.input) if h else f"未知: {block.name}"


def _submit_plan(from_name, plan):
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead", status="pending", payload=plan)
    BUS.send(from_name, "lead", plan, "plan_approval_request", {"request_id": req_id})
    return f"计划已提交 ({req_id})"

def _list_tasks_summary():
    tasks = list_tasks()
    if not tasks: return "没有任务。"
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "◉", "completed": "✔"}.get(t.status, "?")
        owner = f" [{t.owner}]" if t.owner else ""
        wt = f" @{t.worktree}" if t.worktree else ""
        lines.append(f"  {icon} {t.id}: {t.subject} [{t.status}]{owner}{wt}")
    return "\n".join(lines)


# ★ s18：工具函数接受 cwd 参数（默认 WORKDIR）
def _safe_bash(cmd, cwd=None):
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd or WORKDIR,
                           capture_output=True, text=True, timeout=60)
        return (r.stdout + r.stderr).strip()[:10000] or "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误: 超时"
    except Exception as e:
        return f"错误: {e}"

def _safe_read(path, cwd=None):
    try:
        base = cwd or WORKDIR
        fp = (base / path).resolve()
        # 安全检查：必须在 WORKTREES_DIR 或 WORKDIR 下
        if not (fp.is_relative_to(WORKTREES_DIR) or fp.is_relative_to(WORKDIR)):
            return "路径越界"
        return fp.read_text(encoding="utf-8")[:5000]
    except Exception as e:
        return f"错误: {e}"

def _safe_write(path, content, cwd=None):
    try:
        base = cwd or WORKDIR
        fp = (base / path).resolve()
        if not (fp.is_relative_to(WORKTREES_DIR) or fp.is_relative_to(WORKDIR)):
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

def run_request_shutdown(teammate):
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown", sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "请关机。", "shutdown_request", {"request_id": req_id})
    return f"关机请求已发给 {teammate} (req: {req_id})"

def run_request_plan(teammate, task):
    BUS.send("lead", teammate, f"请提交计划: {task}"); return f"已要求 {teammate} 提交计划"

def run_review_plan(request_id, approve, feedback=""):
    state = pending_requests.get(request_id)
    if not state: return f"请求 {request_id} 不存在"
    if state.status != "pending": return f"请求 {request_id} 已经是 {state.status}"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender, feedback or ("通过" if approve else "驳回"),
             "plan_approval_response", {"request_id": request_id, "approve": approve})
    return f"计划已{'通过' if approve else '驳回'} ({request_id})"

def run_create_task(subject, description=""):
    task = create_task(subject, description)
    return f"任务已创建: {task.id} ({subject})"

def run_list_tasks_lead():
    tasks = list_tasks()
    if not tasks: return "没有任务。"
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "◉", "completed": "✔"}.get(t.status, "?")
        owner = f" [{t.owner}]" if t.owner else ""
        wt = f" @{t.worktree}" if t.worktree else ""
        lines.append(f"  {icon} {t.id}: {t.subject} [{t.status}]{owner}{wt}")
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
    {"name": "create_task", "input_schema": {"type": "object",
        "properties": {"subject": {"type": "string"}, "description": {"type": "string"}},
        "required": ["subject"]}},
    {"name": "list_tasks", "input_schema": {"type": "object", "properties": {}, "required": []}},
    # ★ s18 新增
    {"name": "create_worktree", "description": "为任务创建独立的 Git worktree 目录。",
     "input_schema": {"type": "object",
        "properties": {"name": {"type": "string"}, "task_id": {"type": "string"}},
        "required": ["name"]}},
    {"name": "list_worktrees", "description": "列出所有 worktrees。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "remove_worktree", "description": "删除 worktree。",
     "input_schema": {"type": "object",
        "properties": {"name": {"type": "string"}, "discard_changes": {"type": "boolean"}},
        "required": ["name"]}},
    {"name": "keep_worktree", "description": "保留 worktree（分支不删）。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

TOOL_HANDLERS = {
    **BASE_HANDLERS,
    "spawn_teammate": run_spawn_teammate, "send_message": run_send_message,
    "check_inbox": run_check_inbox, "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan, "review_plan": run_review_plan,
    "create_task": run_create_task, "list_tasks": run_list_tasks_lead,
    "create_worktree": create_worktree, "list_worktrees": list_worktrees,
    "remove_worktree": remove_worktree, "keep_worktree": keep_worktree,
}

register_default_hooks()

SYSTEM = (
    f"你是 {WORKDIR} 下的 Lead Agent。用中文回答。\n"
    "你可以:\n"
    "  1. create_task 创建任务\n"
    "  2. create_worktree 为任务创建隔离的工作目录\n"
    "  3. spawn_teammate 启动队友\n"
    "  4. list_worktrees / remove_worktree / keep_worktree 管理 worktrees\n"
)


def agent_loop(messages, context):
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
            if block.type != "tool_use": continue
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
    # 检查是否在 git 仓库中
    ok, _ = _run_git(["rev-parse", "--git-dir"])
    if not ok:
        print("⚠ 当前目录不是 git 仓库！")
        print("  请先执行: git init && git add -A && git commit -m \"init\"")
        print("  （如果是教学用，可以先初始化一个空仓库）")
        print()
        print("  =============================================")
        print("  将使用 模拟目录 模式（无 git worktree，仅目录隔离）")
        print("  =============================================")
        print()

    print("=" * 55)
    print("  s18: Worktree Isolation — 各干各的目录")
    print("=" * 55)
    print()
    print("  s17 vs s18:")
    print("    s17: 所有人共享 WORKDIR，互相覆盖")
    print("    s18: 每个任务绑独立 worktree 目录 + 分支")
    print()
    print("  LEADER 任务:")
    print("    1. create_task 创建 2 个任务")
    print("    2. create_worktree 为每个任务创建隔离目录")
    print("    3. spawn 2 个队友 → 观察它们在不同 worktree 下工作")
    print("    4. list_worktrees / remove_worktree 管理")
    print()
    print("  观察: .worktrees/ 目录 + .tasks/ 中的 worktree 字段")
    print()

    history = []
    context = {"workspace": str(WORKDIR)}

    while True:
        try:
            query = input("\033[36ms18 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！"); break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！"); break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)

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
