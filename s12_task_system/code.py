#!/usr/bin/env python3
"""
s12_task_system/code.py — Task 系统：有依赖的、持久化的任务图

s05 的 TodoWrite 是"当前任务的执行清单"，存在内存里，关了就没。
s12 的 Task 系统是"可恢复的任务图"——每个任务是磁盘上的 JSON 文件，
任务之间有 blockedBy 依赖关系，跨会话保留。

  TodoWrite (s05)              Task System (s12)
  ─────────────────            ──────────────────
  内存中的列表                  .tasks/{id}.json 文件
  平铺无依赖                   blockedBy 依赖图
  当前会话有效                  跨会话持久化
  无认领/分工                   owner 字段 + claim 机制

核心类比：盖房子不能先盖屋顶再打地基。Task 系统确保依赖关系被遵守。

工具实现从 common/ 导入，本章聚焦 Task 数据结构 + 5 个任务工具。
"""
import os, sys, json, time, random
from pathlib import Path
from dataclasses import dataclass, asdict

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


# ═══════════════════════════════════════════════════════════
#  ★【s12 新概念】Task 数据模型 + 持久化
# ═══════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    id: str
    subject: str           # 简短标题
    description: str       # 详细描述
    status: str            # pending | in_progress | completed
    owner: str | None      # 认领者（多 Agent 场景用）
    blockedBy: list[str]   # 依赖的任务 ID 列表


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def save_task(task: Task):
    _task_path(task.id).write_text(
        json.dumps(asdict(task), indent=2, ensure_ascii=False), encoding="utf-8")


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text(encoding="utf-8")))


# ═══════════════════════════════════════════════════════════
#  ★ 5 个任务工具
# ═══════════════════════════════════════════════════════════

# ── 工具 1：创建任务 ──
def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> str:
    """创建新任务，保存到 .tasks/{id}.json"""
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    deps = f" (依赖: {task.blockedBy})" if task.blockedBy else ""
    return f"已创建: [{task.id}] {task.subject}{deps}"


# ── 工具 2：列出所有任务 ──
def list_tasks() -> str:
    """列出所有任务及其状态、依赖关系"""
    tasks = [Task(**json.loads(p.read_text(encoding="utf-8")))
             for p in sorted(TASKS_DIR.glob("task_*.json"))]
    if not tasks:
        return "(无任务)"

    lines = ["## 任务列表\n"]
    for t in tasks:
        icon = {"pending": "🔲", "in_progress": "🔄", "completed": "✅"}.get(t.status, "❓")
        deps = f" ← 依赖: {t.blockedBy}" if t.blockedBy else ""
        owner_info = f" ({t.owner})" if t.owner else ""
        lines.append(f"{icon} [{t.id}] {t.subject}{owner_info}{deps}")
    return "\n".join(lines)


# ── 工具 3：查看任务详情 ──
def get_task(task_id: str) -> str:
    """查看单个任务的完整 JSON"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2, ensure_ascii=False)


# ── 依赖检查 ──
def can_start(task_id: str) -> bool:
    """检查 blockedBy 中的依赖是否全部 completed"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False  # 缺失的依赖 = 阻塞
        if load_task(dep_id).status != "completed":
            return False
    return True


# ── 工具 4：认领任务 ──
def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领一个 pending 任务 → 设为 owner、状态改 in_progress"""
    task = load_task(task_id)

    if task.status != "pending":
        return f"无法认领: {task_id} 当前状态是 {task.status}"

    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"被阻塞，依赖未完成: {deps}"

    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  🎯 [claim] {task.subject} → in_progress ({owner})")
    return f"已认领: [{task.id}] {task.subject}"


# ── 工具 5：完成任务 ──
def complete_task(task_id: str) -> str:
    """完成一个 in_progress 任务 → completed，并报告下游谁被解锁了"""
    task = load_task(task_id)

    if task.status != "in_progress":
        return f"无法完成: {task_id} 当前状态是 {task.status}"

    task.status = "completed"
    save_task(task)

    # 检查哪些任务因为这个完成而被解锁
    all_tasks = [Task(**json.loads(p.read_text(encoding="utf-8")))
                 for p in sorted(TASKS_DIR.glob("task_*.json"))]
    unblocked = [t.subject for t in all_tasks
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]

    msg = f"已完成: [{task.id}] {task.subject}"
    if unblocked:
        msg += f"\n🔓 已解锁: {', '.join(unblocked)}"
        print(f"  🔓 [unblocked] {', '.join(unblocked)}")

    return msg


# ═══════════════════════════════════════════════════════════
#  SYSTEM prompt（s10 继承）
# ═══════════════════════════════════════════════════════════

def build_system() -> str:
    return (
        "你是编码助手。用中文回答。\n\n"
        "任务工具有: create_task(创建)、list_tasks(列表)、get_task(详情)、"
        "claim_task(认领)、complete_task(完成)。\n"
        "任务之间可以有 blockedBy 依赖——先完成被依赖的任务。\n"
        f"工作目录: {WORKDIR}"
    )


SYSTEM = build_system()


# ═══════════════════════════════════════════════════════════
#  工具和循环
# ═══════════════════════════════════════════════════════════

TOOLS = BASE_TOOLS + [
    {"name": "create_task", "description": "创建新任务。可选 blockedBy 声明依赖其他任务。",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}, "blockedBy": {"type": "array", "items": {"type": "string"}}}, "required": ["subject"]}},
    {"name": "list_tasks", "description": "列出所有任务及状态。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_task", "description": "查看任务详情。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "claim_task", "description": "认领一个 pending 任务（依赖已完成的前提下）。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "complete_task", "description": "完成一个 in_progress 任务，自动报告下游解锁。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
]

TOOL_HANDLERS = {
    **BASE_HANDLERS,
    "create_task": create_task, "list_tasks": list_tasks,
    "get_task": get_task, "claim_task": claim_task,
    "complete_task": complete_task,
}

register_default_hooks()


def agent_loop(messages: list):
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
            return

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force: messages.append({"role": "user", "content": str(force)}); continue
            return

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
            print(str(output)[:300])
            trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("=" * 55)
    print("  s12: Task System — 带依赖的持久化任务图")
    print("=" * 55)
    print()
    print("  s05 TodoWrite: 内存列表，平铺，当前会话")
    print("  s12 Task System: 磁盘文件，有向无环图，跨会话")
    print()
    print("  5 个工具: create / list / get / claim / complete")
    print("  依赖规则: blockedBy 全部 completed 才能 claim")
    print("  完成时自动报告下游哪些任务被解锁")
    print()
    print("  试试：")
    print("    1. 创建任务: 建数据库 → 写API(依赖建库) → 写测试(依赖API)")
    print("    2. 列出所有任务，观察依赖关系")
    print("    3. 逐个认领并完成，观察解锁链")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    while True:
        try:
            query = input("\033[36ms12 >>> \033[0m")
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
