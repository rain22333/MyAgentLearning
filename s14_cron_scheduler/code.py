#!/usr/bin/env python3
"""
s14_cron_scheduler/code.py — Cron Scheduler：按时执行，不用人手推

s13 让 Agent 能异步执行，但所有操作仍需要你手动触发。
"每天早上 9 点跑测试"、"每 30 分钟检查 CI"——这种周期任务不该每次人来推。

s14 的解法：给 Agent 装一个闹钟 —— Cron 调度器。

  四层架构：

  ① Scheduler（调度器）：daemon 线程，每秒检查一次 cron 表达式
  ② Queue（队列）：匹配的任务放入 cron_queue
  ③ Queue Processor（队列处理器）：发现队列非空 + Agent 空闲 → 自动触发
  ④ Consumer（消费者）：agent_loop 消费队列，注入到 messages

  类比：闹钟（调度器）到点响了 → 任务卡片掉进待办箱（队列）
  → 检查你没在忙（队列处理器）→ 把卡片递给你（消费）

工具实现从 common/ 导入，本章聚焦 cron 匹配 + 调度线程 + 队列解耦。
"""
import os, sys, json, time, threading
from pathlib import Path
from datetime import datetime
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
CRON_STORE = WORKDIR / ".scheduled_tasks.json"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


# ═══════════════════════════════════════════════════════════
#  ★【s14 新概念 1/4】CronJob 数据模型
# ═══════════════════════════════════════════════════════════

@dataclass
class CronJob:
    id: str
    cron: str          # "0 9 * * *" 五段式 cron 表达式
    prompt: str        # 触发时注入给 Agent 的消息
    recurring: bool    # True=周期性，False=一次性
    durable: bool      # True=写磁盘，跨会话保留


# 内存中的活跃任务
_cron_jobs: dict[str, CronJob] = {}
_cron_lock = threading.Lock()
_job_counter = 0


# ═══════════════════════════════════════════════════════════
#  ★【s14 新概念 2/4】Cron 表达式匹配
#  五段式: 分钟 小时 日 月 星期
#  DOM 和 DOW 同时约束时用 OR 语义
# ═══════════════════════════════════════════════════════════

def _cron_field_matches(field: str, value: int) -> bool:
    """单个 cron 字段匹配: *, */N, N, N-M, N,M"""
    if field == "*":
        return True
    for part in field.split(","):
        part = part.strip()
        if "/" in part:
            base, step = part.split("/")
            base = 0 if base == "*" else int(base)
            step = int(step)
            if value >= base and (value - base) % step == 0:
                return True
        elif "-" in part:
            lo, hi = part.split("-")
            if int(lo) <= value <= int(hi):
                return True
        else:
            if int(part) == value:
                return True
    return False


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """检查给定时间是否匹配 cron 表达式"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, (dt.weekday() + 1) % 7)  # Sunday=0

    if not (m and h and month_ok):
        return False

    # DOM 和 DOW: 都不约束 → True; 只有一个约束 → 用那个; 都约束 → OR
    dom_free = dom == "*"
    dow_free = dow == "*"
    if dom_free and dow_free: return True
    if dom_free: return dow_ok
    if dow_free: return dom_ok
    return dom_ok or dow_ok


# ═══════════════════════════════════════════════════════════
#  ★【s14 新概念 3/4】调度器 + 队列 + 处理器
# ═══════════════════════════════════════════════════════════

cron_queue: list[CronJob] = []
_queue_lock = threading.Lock()
agent_lock = threading.Lock()
_last_fired: dict[str, datetime] = {}  # 防止同一分钟内重复触发


def schedule_job(cron: str, prompt: str, recurring: bool = True, durable: bool = False) -> str:
    """注册一个 cron 任务"""
    global _job_counter
    _job_counter += 1
    job_id = f"cron_{_job_counter:04d}"

    job = CronJob(id=job_id, cron=cron, prompt=prompt,
                  recurring=recurring, durable=durable)

    with _cron_lock:
        _cron_jobs[job_id] = job

    if durable:
        _save_jobs()

    print(f"  ⏰ [CRON] 已注册 {job_id}: '{cron}' → {prompt[:50]}")
    return f"已注册定时任务 {job_id}: cron='{cron}', prompt='{prompt[:60]}...'"


def cancel_job(job_id: str) -> str:
    """取消一个 cron 任务"""
    with _cron_lock:
        if job_id in _cron_jobs:
            job = _cron_jobs.pop(job_id)
            _save_jobs()
            print(f"  ❌ [CRON] 已取消 {job_id}: {job.prompt[:50]}")
            return f"已取消: {job_id}"
    return f"未找到: {job_id}"


def list_cron_jobs() -> str:
    """列出所有活跃的 cron 任务"""
    with _cron_lock:
        if not _cron_jobs:
            return "(无定时任务)"
        lines = ["## 定时任务\n"]
        for j in _cron_jobs.values():
            recurring = "🔄" if j.recurring else "1️⃣"
            durable = "💾" if j.durable else ""
            lines.append(f"{recurring}{durable} [{j.id}] cron='{j.cron}' → {j.prompt[:60]}")
        return "\n".join(lines)


def _save_jobs():
    """持久化到磁盘"""
    with _cron_lock:
        durable_jobs = [asdict(j) for j in _cron_jobs.values() if j.durable]
    CRON_STORE.write_text(json.dumps(durable_jobs, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_jobs():
    """从磁盘恢复持久化任务"""
    if not CRON_STORE.exists():
        return
    global _job_counter
    try:
        data = json.loads(CRON_STORE.read_text(encoding="utf-8"))
        for d in data:
            _job_counter += 1
            job = CronJob(**d)
            _cron_jobs[job.id] = job
        print(f"  💾 [CRON] 从磁盘恢复 {len(data)} 个定时任务")
    except Exception:
        pass


# ── 调度器线程：每秒轮询 ──
def cron_scheduler_loop():
    """独立 daemon 线程，每秒检查所有 cron 任务"""
    print("  ⏰ [CRON] 调度器已启动（1s 轮询）")
    while True:
        time.sleep(1)
        now = datetime.now()
        time_key = now.strftime("%Y%m%d%H%M")

        with _cron_lock:
            jobs = list(_cron_jobs.items())

        for job_id, job in jobs:
            if cron_matches(job.cron, now):
                # 防止同一分钟重复触发
                if _last_fired.get(job_id) == time_key:
                    continue
                _last_fired[job_id] = time_key

                with _queue_lock:
                    cron_queue.append(job)
                print(f"  🔔 [CRON] 触发 {job_id}: {job.prompt[:50]}")

                # 非周期性任务：触发后自动删除
                if not job.recurring:
                    with _cron_lock:
                        _cron_jobs.pop(job_id, None)
                    _save_jobs()


def has_cron_queue() -> bool:
    with _queue_lock:
        return len(cron_queue) > 0


def consume_cron_queue() -> list[CronJob]:
    """消费队列中的所有任务"""
    with _queue_lock:
        jobs = cron_queue[:]
        cron_queue.clear()
    return jobs


# ── 队列处理器：Agent 空闲时自动交办 ──
def queue_processor_loop(history: list, context: dict):
    """后台线程，发现队列非空且 Agent 空闲时自动触发执行"""
    while True:
        time.sleep(0.3)
        if not has_cron_queue():
            continue
        if not agent_lock.acquire(blocking=False):
            continue
        try:
            if not has_cron_queue():
                continue
            print("\n  📬 [queue processor] 自动交付定时任务...")
            run_agent_turn(history, context, user_query=None)
        finally:
            agent_lock.release()


# ═══════════════════════════════════════════════════════════
#  ★【s14 新概念 4/4】工具和循环
# ═══════════════════════════════════════════════════════════

TOOLS = BASE_TOOLS + [
    {"name": "schedule_cron", "description": "注册定时任务。cron 格式: 分 时 日 月 星期。如 '0 9 * * *' 每天9点。",
     "input_schema": {"type": "object", "properties": {"cron": {"type": "string"}, "prompt": {"type": "string"}, "recurring": {"type": "boolean"}, "durable": {"type": "boolean"}}, "required": ["cron", "prompt"]}},
    {"name": "list_crons", "description": "列出所有定时任务。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "cancel_cron", "description": "取消定时任务。",
     "input_schema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}},
]

TOOL_HANDLERS = {
    **BASE_HANDLERS,
    "schedule_cron": schedule_job, "list_crons": list_cron_jobs,
    "cancel_cron": cancel_job,
}

register_default_hooks()

SYSTEM = (
    f"你是 {WORKDIR} 下的编码助手。用中文回答。\n"
    "可以用 schedule_cron 注册定时任务，cron 格式: 分 时 日 月 星期。"
)


def agent_loop(messages: list, context: dict) -> dict:
    while True:
        # 消费 cron 队列
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user", "content": f"[定时触发] {job.prompt}"})

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
            if force: messages.append({"role": "user", "content": str(force)}); continue
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
        context = {"workspace": str(WORKDIR)}
        return context  # 每轮返回，让锁机制管理


def run_agent_turn(history: list, context: dict, user_query: str | None = None):
    if user_query is not None:
        history.append({"role": "user", "content": user_query})
    context = agent_loop(history, context)
    # 打印最后回复
    if history:
        last = history[-1]
        if isinstance(last.get("content"), list):
            for b in last["content"]:
                if getattr(b, "type", None) == "text":
                    print(f"\n\033[32m{b.text}\033[0m")


if __name__ == "__main__":
    print("=" * 55)
    print("  s14: Cron Scheduler — 按时间表自动执行")
    print("=" * 55)
    print()
    print("  四层架构: Scheduler → Queue → Queue Processor → Consumer")
    print()
    print("  Cron 格式: 分 时 日 月 星期")
    print("    0 9 * * *    每天 9:00")
    print("    */1 * * * *  每分钟")
    print()
    print("  试试:")
    print("    1. schedule_cron: '*/1 * * * *' → '列出当前时间'")
    print("    2. list_crons 查看所有定时任务")
    print("    3. 等 1 分钟后观察自动触发")
    print()
    print("  输入 q / exit / 空行 退出\n")

    # 恢复持久化任务
    _load_jobs()

    # 启动调度器线程
    threading.Thread(target=cron_scheduler_loop, daemon=True).start()

    history = []
    context = {"workspace": str(WORKDIR)}

    # 启动队列处理器线程
    threading.Thread(target=queue_processor_loop, args=(history, context), daemon=True).start()

    while True:
        try:
            query = input("\033[36ms14 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！"); break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！"); break
        trigger_hooks("UserPromptSubmit", query)
        with agent_lock:
            run_agent_turn(history, context, query)
