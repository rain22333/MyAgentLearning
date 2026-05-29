#!/usr/bin/env python3
"""
s16_team_protocols/code.py — Team Protocols：队友之间要有约定

s15 的队友能干活、能通信，但协调是松散的：Lead 发消息、队友回复，没有结构化协议。
两个场景暴露了问题：

场景1 — 关机：Lead 想让 Alice 关机。直接杀线程？Alice 写了一半的文件留在磁盘上。
         需要握手：Lead 发请求，Alice 确认收尾后关机。

场景2 — 计划审批：Bob 想重构认证模块，属于高风险操作。
         应该先让 Lead 看 Bob 的计划，审批通过后再动手。

这两个场景结构完全一样：一方发请求，另一方给回复，请求和回复通过请求ID关联。

─────────────────────────────────────────────────────────
s16 新增三个核心机制：

① ProtocolState — 请求状态追踪（pending → approved | rejected）
② dispatch_message — 按消息类型分发到不同处理器
③ match_response — 通过 request_id 关联回复与请求

类比：s15 = 微信群自由聊天，s16 = 加上了"审批流程"和"离职交接"

运行：cd s16_team_protocols && ..\.venv\Scripts\python.exe code.py
"""
import os, sys, json, time, random, threading, subprocess
from pathlib import Path
from dataclasses import dataclass, field

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
#  MessageBus（同 s15）
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
#  ★【s16 新概念 1/3】ProtocolState — 请求状态机
# ═══════════════════════════════════════════════════════════
#
# 每发起一个协议请求，就创建一条状态记录：
#   pending → approved | rejected
#
# request_id 是整个链路的关键：请求带着它出去，回复带着它回来。

@dataclass
class ProtocolState:
    request_id: str     # 唯一 ID，如 "req_004281"
    type: str           # "shutdown" | "plan_approval"
    sender: str         # 发起方
    target: str         # 接收方
    status: str         # pending | approved | rejected
    payload: str        # 计划文本或关机原因
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    return f"req_{random.randint(100000, 999999)}"


# ═══════════════════════════════════════════════════════════
#  ★【s16 新概念 2/3】match_response — 回复与请求关联
# ═══════════════════════════════════════════════════════════
#
# 回复消息到来时，通过 request_id 找到原始请求，更新状态。
# 还会校验回复类型是否匹配请求类型。

def match_response(response_type: str, request_id: str, approve: bool):
    """将回复关联到原始请求，并更新状态"""
    state = pending_requests.get(request_id)
    if not state:
        print(f"  \033[31m[协议] 未知 request_id: {request_id}\033[0m")
        return

    # ★ 类型校验：shutdown 请求必须对应 shutdown_response
    expected = state.type + "_response"
    if response_type != expected:
        print(f"  \033[31m[协议] 类型不匹配: 期望 {expected}, 收到 {response_type}\033[0m")
        return

    if state.status != "pending":
        print(f"  \033[33m[协议] {request_id} 已经是 {state.status}，忽略重复\033[0m")
        return

    state.status = "approved" if approve else "rejected"
    icon = "✓" if approve else "✗"
    color = "32" if approve else "31"
    print(f"  \033[{color}m[协议] {state.type} {icon} ({request_id})\033[0m")


# ── Lead 端：统一收件箱消费 ──

def consume_lead_inbox() -> list[dict]:
    """读取 Lead 收件箱。协议回复自动路由到 match_response。
    这样 check_inbox 工具和主循环都调用同一个函数，避免消息被未路由消费。"""
    msgs = BUS.read_inbox("lead")
    if not msgs:
        return []
    for msg in msgs:
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        msg_type = msg.get("type", "")
        if req_id and msg_type.endswith("_response"):
            approve = meta.get("approve", False)
            match_response(msg_type, req_id, approve)
    return msgs


# ═══════════════════════════════════════════════════════════
#  ★【s16 新概念 3/3】dispatch_message — 队友端消息分发
# ═══════════════════════════════════════════════════════════
#
# s15 中队友收到什么都当作文本注入。s16 中队友的收件箱里
# 不止有普通消息，还有协议消息（shutdown_request 等）。
# 需要按类型分发到不同处理器。

def dispatch_teammate_inbox(name: str, msgs: list[dict], messages: list) -> bool:
    """处理队友收件箱中的消息。返回 True 表示队友应停止。"""
    non_protocol = []

    for msg in msgs:
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")

        if msg_type == "shutdown_request":
            # ★ 关机协议：队友确认后退出
            BUS.send(name, "lead", "正在收尾，即将关机。",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            print(f"  \033[35m[协议] {name} 同意关机 ({req_id})\033[0m")
            return True  # 停止循环

        elif msg_type == "plan_approval_response":
            # ★ 计划审批：Lead 已审批，注入结果
            approve = meta.get("approve", False)
            feedback = msg.get("content", "")
            if approve:
                messages.append({"role": "user",
                    "content": f"[计划已审批] 可以开始执行。"})
            else:
                messages.append({"role": "user",
                    "content": f"[计划被驳回] 反馈: {feedback}"})

        else:
            non_protocol.append(msg)

    if non_protocol:
        messages.append({"role": "user",
            "content": f"<inbox>{json.dumps(non_protocol, ensure_ascii=False)}</inbox>"})

    return False  # 继续循环


# ═══════════════════════════════════════════════════════════
#  队友线程：idle loop 版（s16 改进）
# ═══════════════════════════════════════════════════════════
#
# s15 的队友最多 10 轮就退出，s16 改为 idle loop：
# LLM 停下来了 → 等收件箱有新消息 → 再继续
# 这样队友可以持续在线，直到收到 shutdown_request 才退出。

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    if name in active_teammates:
        return f"队友 '{name}' 已存在"

    system = (
        f"你是 '{name}'，一个 {role}。用工具完成任务。"
        f"如果需要执行高风险操作，先用 submit_plan 提交计划等待审批。"
        f"完成后用 send_message 发送结果给 'lead'。"
    )

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
        {"name": "send_message", "description": "发消息给其他 agent。",
         "input_schema": {"type": "object",
                          "properties": {"to": {"type": "string"},
                                         "content": {"type": "string"}},
                          "required": ["to", "content"]}},
        {"name": "submit_plan",  # ★ s16 新增：队友可以提交计划
         "description": "提交执行计划给 Lead 审批。审批通过后再执行。",
         "input_schema": {"type": "object",
                          "properties": {"plan": {"type": "string"}},
                          "required": ["plan"]}},
    ]

    def run():
        messages = [{"role": "user", "content": prompt}]

        while True:  # ★ idle loop：持续运行，直到被 shutdown
            # 1. 检查收件箱
            inbox = BUS.read_inbox(name)
            if inbox:
                should_stop = dispatch_teammate_inbox(name, inbox, messages)
                if should_stop:
                    break

            # 2. 调用 LLM
            try:
                response = client.messages.create(
                    model=MODEL, system=system, messages=messages[-20:],
                    tools=sub_tools, max_tokens=8000)
            except Exception as e:
                print(f"  \033[31m[队友 {name}] 错误: {e}\033[0m")
                break

            messages.append({"role": "assistant", "content": response.content})

            # 3. 如果没有工具调用 → 进入空闲等待
            if response.stop_reason != "tool_use":
                # ★ idle：等收件箱有新消息
                while True:
                    time.sleep(1)
                    inbox = BUS.read_inbox(name)
                    if not inbox:
                        continue
                    should_stop = dispatch_teammate_inbox(name, inbox, messages)
                    if should_stop:
                        return  # 退出整个 run()
                    break  # 有新消息，回到 LLM 循环
                continue

            # 4. 执行工具
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                print(f"\033[36m  [{name}] >>> {block.name}\033[0m")
                output = _teammate_tool(name, block)
                print(f"\033[90m  [{name}] {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
            messages.append({"role": "user", "content": results})

        # 关机收尾
        BUS.send(name, "lead", "已关机。", "result")
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
        "send_message": lambda **kw: (
            BUS.send(name, kw.get("to", "lead"), kw.get("content", "")), "已发送")[1],
        "submit_plan": lambda **kw: _submit_plan(name, kw.get("plan", "")),
    }
    h = handlers.get(block.name)
    return h(**block.input) if h else f"未知: {block.name}"


def _submit_plan(from_name: str, plan: str) -> str:
    """★ s16 新增：队友提交计划给 Lead 审批"""
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead",
        status="pending", payload=plan)
    BUS.send(from_name, "lead", plan, "plan_approval_request",
             {"request_id": req_id})
    print(f"  \033[35m[协议] {from_name} 提交计划 ({req_id})\033[0m")
    return f"计划已提交 ({req_id})，等待审批..."


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

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"消息已发给 {to}"


def run_check_inbox() -> str:
    msgs = consume_lead_inbox()  # ★ 统一消费，协议自动路由
    if not msgs:
        return "（收件箱为空）"
    lines = []
    for m in msgs:
        lines.append(f"  [{m['from']}] [{m['type']}] {m['content'][:200]}")
    return "\n".join(lines)


def run_request_shutdown(teammate: str) -> str:
    """★ s16 新增：Lead 请求队友关机（带握手）"""
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "请收尾后关机。", "shutdown_request",
             {"request_id": req_id})
    print(f"  \033[35m[协议] shutdown_request → {teammate} ({req_id})\033[0m")
    return f"关机请求已发给 {teammate} (req: {req_id})"


def run_request_plan(teammate: str, task: str) -> str:
    """★ s16 新增：Lead 要求队友先提交计划"""
    BUS.send("lead", teammate, f"请为以下任务提交计划: {task}", "message")
    return f"已要求 {teammate} 提交计划"


def run_review_plan(request_id: str, approve: bool, feedback: str = "") -> str:
    """★ s16 新增：Lead 审批队友的计划"""
    state = pending_requests.get(request_id)
    if not state:
        return f"请求 {request_id} 不存在"
    if state.status != "pending":
        return f"请求 {request_id} 已经是 {state.status}"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender,
             feedback or ("审批通过" if approve else "驳回"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    return f"计划已{'通过' if approve else '驳回'} ({request_id})"


# ═══════════════════════════════════════════════════════════
#  工具注册 & Agent 循环
# ═══════════════════════════════════════════════════════════

TOOLS = BASE_TOOLS + [
    {"name": "spawn_teammate",
     "description": "启动队友。队友在后台线程运行，支持 shutdown 握手和计划审批。",
     "input_schema": {"type": "object",
         "properties": {"name": {"type": "string"}, "role": {"type": "string"},
                        "prompt": {"type": "string"}},
         "required": ["name", "role", "prompt"]}},
    {"name": "send_message",
     "description": "发消息给队友。",
     "input_schema": {"type": "object",
         "properties": {"to": {"type": "string"}, "content": {"type": "string"}},
         "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "检查收件箱（协议消息自动路由）。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    # ★ s16 新增：
    {"name": "request_shutdown",
     "description": "请求队友关机（带握手确认）。",
     "input_schema": {"type": "object",
         "properties": {"teammate": {"type": "string"}},
         "required": ["teammate"]}},
    {"name": "request_plan",
     "description": "要求队友先提交执行计划。",
     "input_schema": {"type": "object",
         "properties": {"teammate": {"type": "string"}, "task": {"type": "string"}},
         "required": ["teammate", "task"]}},
    {"name": "review_plan",
     "description": "审批队友提交的计划（通过或驳回）。",
     "input_schema": {"type": "object",
         "properties": {"request_id": {"type": "string"},
                        "approve": {"type": "boolean"},
                        "feedback": {"type": "string"}},
         "required": ["request_id", "approve"]}},
]

TOOL_HANDLERS = {
    **BASE_HANDLERS,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message,
    "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan,
    "review_plan": run_review_plan,
}

register_default_hooks()

SYSTEM = (
    f"你是 {WORKDIR} 下的 Lead Agent。用中文回答。\n"
    "团队协议:\n"
    "  1. 用 request_shutdown 让队友优雅关机（不要直接杀线程）\n"
    "  2. 对高风险任务，先用 request_plan 要求队友提交计划\n"
    "  3. 收到队友的计划后，用 review_plan 审批\n"
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
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue
            h = TOOL_HANDLERS.get(block.name)
            output = h(**block.input) if h else f"未知: {block.name}"
            print(str(output)[:200])
            trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})
        messages.append({"role": "user", "content": results})
        return context


if __name__ == "__main__":
    print("=" * 55)
    print("  s16: Team Protocols — 结构化握手协议")
    print("=" * 55)
    print()
    print("  s15 vs s16:")
    print("    s15: 队友最多10轮自然退出，松散文本通信")
    print("    s16: idle loop + shutdown握手 + 计划审批")
    print()
    print("  两种协议:")
    print("    关机协议: request_shutdown → 队友确认 → 优雅退出")
    print("    审批协议: submit_plan → Lead review → approve/reject")
    print()
    print("  试试:")
    print("    1. 启动 bob 做后端开发，让他为 '重构认证模块' 先提交计划")
    print("    2. check_inbox 查看 bob 的计划")
    print("    3. review_plan 审批（通过或驳回）")
    print("    4. request_shutdown 让 bob 关机")
    print()
    print("  观察: pending_requests 状态变化、request_id 关联")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    context = {"workspace": str(WORKDIR)}

    while True:
        try:
            query = input("\033[36ms16 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)

        # ★ 每轮结束后统一消费收件箱（协议自动路由）
        inbox_msgs = consume_lead_inbox()
        for msg in inbox_msgs:
            if msg.get("type") == "plan_approval_request":
                # 队友提交的计划 → 注入 history 让 Lead 看到
                history.append({"role": "user",
                    "content": f"[计划审批请求] 来自 {msg['from']}: {msg['content']} "
                               f"(request_id: {msg['metadata']['request_id']})"})
                print(f"\n\033[33m[收到计划审批请求] {msg['from']}: {msg['content'][:80]}\033[0m")
            elif msg.get("type") == "result":
                history.append({"role": "user",
                    "content": f"[队友结果] {msg['from']}: {msg['content'][:200]}"})

        # 打印回复
        last = history[-1]
        if isinstance(last.get("content"), list):
            for block in last["content"]:
                if getattr(block, "type", None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")
        print()
