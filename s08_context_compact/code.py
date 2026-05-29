#!/usr/bin/env python3
"""
s08_context_compact/code.py — 上下文压缩：便宜的先跑，贵的后跑

问题：Agent 跑了 80 轮对话，读了 30 个文件，跑了 20 条命令。
messages 列表堆满了中间过程的 tool_result，上下文窗口满了 → API 拒绝。

解法：四层压缩管线，核心原则是"便宜的先跑，贵的后跑"

  messages → [L3 budget: 大结果落盘] → [L1 snip: 裁中间消息]
          → [L2 micro: 旧结果占位符] → [token 还超?] → [L4 compact: LLM 摘要]
                                                     → [还超?] → [应急裁减]

  L1/L2/L3: 0 次 API 调用（免费）
  L4:       1 次 API 调用（昂贵，最后手段）

工具实现和 Hook 系统从 common/ 导入，本章聚焦四层压缩管线。
"""
import os, sys, json, time
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
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
CURRENT_TODOS: list[dict] = []

# ── s07 继承：技能加载 ──
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"): return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3: return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()

SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    if not SKILLS_DIR.exists(): return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir(): continue
        m = d / "SKILL.md"
        if m.exists():
            raw = m.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

_scan_skills()

def list_skills() -> str:
    if not SKILL_REGISTRY: return "(无可用技能)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

def load_skill(name: str) -> str:
    skill = SKILL_REGISTRY.get(name)
    return skill["content"] if skill else f"技能未找到: {name}"

def build_system() -> str:
    catalog = list_skills()
    return (f"你是 {WORKDIR} 下的编码助手。\n## 可用技能\n{catalog}\n"
            "需要完整指南时用 load_skill 加载。用中文回答，直接行动。")

SYSTEM = build_system()
SUB_SYSTEM = f"你是 {WORKDIR} 下的编码助手。完成任务后返回结论。用中文。"

# ── todo_write ──
def run_todo_write(todos: list[dict]) -> str:
    global CURRENT_TODOS
    CURRENT_TODOS = todos
    icons = {"pending": "🔲", "in_progress": "🔄", "completed": "✅", "cancelled": "❌"}
    lines = ["\n  ┌─── 当前任务计划 ───"]
    for t in CURRENT_TODOS:
        lines.append(f"  │ {icons.get(t.get('status','pending'),'❓')} {t.get('content','')}")
    lines.append("  └────────────────────\n")
    print("\n".join(lines))
    return f"已更新 {len(CURRENT_TODOS)} 个任务"

# ── 子 Agent ──
SUB_TOOLS, SUB_HANDLERS = BASE_TOOLS, BASE_HANDLERS

def extract_text(c) -> str:
    if isinstance(c, str): return c
    if isinstance(c, list):
        return "\n".join(b.text for b in c if hasattr(b, "text")) or "(无文本)"
    return str(c)

def spawn_subagent(desc: str) -> str:
    print(f"\n  🚀 [SUB] {desc[:80]}...")
    msgs = [{"role": "user", "content": desc}]
    for turn in range(30):
        r = client.messages.create(model=MODEL, system=SUB_SYSTEM, messages=msgs, tools=SUB_TOOLS, max_tokens=8000)
        msgs.append({"role": "assistant", "content": r.content})
        if r.stop_reason != "tool_use":
            print(f"  ✅ [SUB] 完成 ({turn+1}轮)")
            return extract_text(msgs[-1]["content"])
        res = []
        for b in r.content:
            if b.type != "tool_use": continue
            blocked = trigger_hooks("PreToolUse", b)
            if blocked: res.append({"type":"tool_result","tool_use_id":b.id,"content":str(blocked)}); continue
            h = SUB_HANDLERS.get(b.name)
            res.append({"type":"tool_result","tool_use_id":b.id,"content":h(**b.input) if h else f"未知:{b.name}"})
        msgs.append({"role": "user", "content": res})
    return "(子Agent达上限)"


# ═══════════════════════════════════════════════════════════
#  ★【s08 新概念】四层压缩管线
#  核心原则：便宜的先跑（0 API），贵的后跑（1 API）
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 150_000      # 字符数阈值（近似 token）
MAX_MESSAGES = 50            # L1: 消息数上限
KEEP_RECENT_RESULTS = 3      # L2: 保留最近 N 个完整结果
TOOL_RESULT_BUDGET = 200_000 # L3: 单轮结果总大小上限


# ── 辅助：估算消息列表的字符数 ──
def estimate_size(messages: list) -> int:
    return sum(len(str(m)) for m in messages)


# ═══════════════════════════════════════════════════════════
#  L3: tool_result_budget — 大结果落盘
#  问题：一次读了 5 个大文件，tool_result 加起来 500KB
#  解法：超过阈值的结果写入磁盘，上下文只留预览 + 标记
# ═══════════════════════════════════════════════════════════
def tool_result_budget(messages: list) -> list:
    if not messages:
        return messages

    last = messages[-1]
    if not isinstance(last.get("content"), list):
        return messages

    # 统计最后一条 user 消息里所有 tool_result 的总大小
    blocks = [(i, b) for i, b in enumerate(last["content"])
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)

    if total <= TOOL_RESULT_BUDGET:
        return messages

    # 按大小排序，从最大的开始落盘
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for idx, block in ranked:
        content = str(block.get("content", ""))
        if len(content) <= 2000:  # 小结果不用落盘
            continue

        # 落盘到文件
        TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts_id = block.get("tool_use_id", "unknown")
        fname = TOOL_RESULTS_DIR / f"{ts_id}.json"
        fname.write_text(content, encoding="utf-8")

        # 上下文只留预览
        preview = content[:2000]
        block["content"] = (
            f"<persisted-output path='{fname}'>\n"
            f"{preview}\n"
            f"... (完整内容已落盘，共 {len(content):,} 字符)"
            f"</persisted-output>"
        )

        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
        if total <= TOOL_RESULT_BUDGET:
            break

    return messages


# ═══════════════════════════════════════════════════════════
#  L1: snip_compact — 裁掉中间旧消息
#  问题：80 轮对话 → 160 条消息，最早的已经无关
#  解法：保留头 3 条（初始上下文）+ 尾 47 条（当前工作）
# ═══════════════════════════════════════════════════════════
def snip_compact(messages: list) -> list:
    if len(messages) <= MAX_MESSAGES:
        return messages

    keep_head = 3
    keep_tail = MAX_MESSAGES - keep_head
    snipped = len(messages) - keep_head - keep_tail

    placeholder = {
        "role": "user",
        "content": f"[已裁剪中间 {snipped} 条消息 —— 这些是早期的对话过程]"
    }

    print(f"  ✂️  [L1 snip] 消息 {len(messages)}→{MAX_MESSAGES}，裁剪 {snipped} 条")
    return messages[:keep_head] + [placeholder] + messages[-keep_tail:]


# ═══════════════════════════════════════════════════════════
#  L2: micro_compact — 旧工具结果替换为占位符
#  问题：裁掉了整条消息，但剩下的消息里 tool_result 仍在累积
#  解法：只保留最近 3 个 tool_result 的完整内容，更旧的换占位符
# ═══════════════════════════════════════════════════════════
def micro_compact(messages: list) -> list:
    # 收集所有 tool_result block 的位置
    tool_results = []
    for mi, m in enumerate(messages):
        content = m.get("content", [])
        if isinstance(content, list):
            for bi, b in enumerate(content):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tool_results.append((mi, bi, b))

    if len(tool_results) <= KEEP_RECENT_RESULTS:
        return messages

    # 旧的结果替换为占位符
    for mi, bi, block in tool_results[:-KEEP_RECENT_RESULTS]:
        content = str(block.get("content", ""))
        if len(content) > 120:
            block["content"] = "[旧工具结果已压缩。如需完整内容请重新执行。]"

    print(f"  🗜️  [L2 micro] {len(tool_results)} 个结果 → 保留最近 {KEEP_RECENT_RESULTS} 个完整")
    return messages


# ═══════════════════════════════════════════════════════════
#  L4: compact_history — LLM 全量摘要（1 次 API 调用）
#  当 L1-L3 处理完仍超阈值时，调用 LLM 做压缩摘要
#  这是最昂贵的手段，作为最后一道防线
# ═══════════════════════════════════════════════════════════
def compact_history(messages: list) -> list:
    """
    让 LLM 把整个对话历史压缩成结构化的摘要。
    要求返回 5 类信息：任务目标、已完成工作、当前状态、关键发现、下一步。
    """
    history_text = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    parts.append(f"[tool_result: {str(b.get('content',''))[:200]}]")
                elif hasattr(b, "text"):
                    parts.append(b.text[:500])
            content = "\n".join(parts)
        history_text.append(f"[{role}] {str(content)[:1000]}")

    full = "\n\n".join(history_text)

    summary_prompt = (
        "请将以下对话历史压缩成结构化的摘要。"
        "必须包含以下 5 个部分（用中文）：\n"
        "1. 任务目标：用户最初要完成什么\n"
        "2. 已完成工作：已做过的关键操作\n"
        "3. 当前状态：目前进展到哪一步\n"
        "4. 关键发现：重要的文件路径、错误信息、决策\n"
        "5. 下一步：接下来应该做什么\n\n"
        f"对话历史：\n{full[:80000]}"
    )

    print("  🧠 [L4 compact] 调用 LLM 压缩全量历史...")
    try:
        summary_r = client.messages.create(
            model=MODEL,
            system="你是上下文压缩器。只输出结构化摘要，不调用任何工具。",
            messages=[{"role": "user", "content": summary_prompt}],
            max_tokens=4000,
        )
        summary = extract_text(summary_r.content)
    except Exception:
        summary = "[压缩失败，保留原始上下文]"

    # 保存原始对话到转录文件（紧急回退用）
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    transcript_path = TRANSCRIPT_DIR / f"compact_{ts}.json"
    transcript_path.write_text(json.dumps(messages, ensure_ascii=False, default=str), encoding="utf-8")

    print(f"  💾 原始对话已保存到 {transcript_path}")

    # 返回压缩后的消息列表：系统提示 + 摘要
    return [
        {"role": "user", "content": f"[上下文已压缩]\n\n{summary}\n\n---\n原始对话已保存到 {transcript_path}，如需查阅完整历史请读取该文件。"}
    ]


# ═══════════════════════════════════════════════════════════
#  应急：reactive_compact — API 返回 prompt_too_long 时
#  L1-L4 都处理完，API 仍报错 → 激进裁减
# ═══════════════════════════════════════════════════════════
def reactive_compact(messages: list) -> list:
    """API 报 prompt_too_long 时的最后手段：聚缩为 10 条消息"""
    print("  🆘 [EMERGENCY] 激进压缩，仅保留 10 条消息")
    if len(messages) <= 10:
        return messages
    keep_head = 2
    return messages[:keep_head] + [
        {"role": "user", "content": f"[紧急压缩：原始 {len(messages)} 条消息，已裁剪 {len(messages) - 10} 条]"}
    ] + messages[-8:]


# ═══════════════════════════════════════════════════════════
#  工具定义
# ═══════════════════════════════════════════════════════════
TOOLS = BASE_TOOLS + [
    {"name":"todo_write","description":"创建和管理任务计划。",
     "input_schema":{"type":"object","properties":{"todos":{"type":"array","items":{"type":"object","properties":{"content":{"type":"string"},"status":{"type":"string","enum":["pending","in_progress","completed","cancelled"]}},"required":["content","status"]}}},"required":["todos"]}},
    {"name":"task","description":"启动子Agent处理复杂子任务。",
     "input_schema":{"type":"object","properties":{"description":{"type":"string"}},"required":["description"]}},
    {"name":"load_skill","description":"加载技能的完整指南。",
     "input_schema":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}},
    # ★ s08 新增：compact 工具
    {"name":"compact","description":"当对话历史过长时，主动请求压缩上下文。调用后对话历史会被 LLM 摘要替代。",
     "input_schema":{"type":"object","properties":{"reason":{"type":"string","description":"请求压缩的原因"}},"required":[]}},
]

TOOL_HANDLERS = {**BASE_HANDLERS, "todo_write":run_todo_write, "task":spawn_subagent,
                  "load_skill":load_skill, "compact":lambda **kw: "compact 工具已触发"}

register_default_hooks()

rounds_since_todo = 0
MAX_REACTIVE = 1


def agent_loop(messages: list):
    global rounds_since_todo
    reactive_retries = 0

    while True:
        if rounds_since_todo >= 3 and messages:
            messages.append({"role":"user","content":"<reminder>请更新 todo_write。</reminder>"})
            rounds_since_todo = 0

        # ★ s08：每次 LLM 调用前运行压缩管线（0 API 调用的先跑）
        before = estimate_size(messages)
        messages[:] = tool_result_budget(messages)    # L3: 大结果落盘
        messages[:] = snip_compact(messages)           # L1: 裁中间消息
        messages[:] = micro_compact(messages)          # L2: 旧结果占位
        after = estimate_size(messages)
        if after < before:
            print(f"  📦 [压缩] {before:,} → {after:,} 字符 (节省 {(1-after/before)*100:.0f}%)")

        # L4: token 仍超阈值 → LLM 全量摘要
        if estimate_size(messages) > CONTEXT_LIMIT:
            print(f"  ⚠️  上下文仍超限 ({estimate_size(messages):,} > {CONTEXT_LIMIT:,})，触发 L4 压缩...")
            messages[:] = compact_history(messages)

        try:
            response = client.messages.create(model=MODEL, system=SYSTEM, messages=messages, tools=TOOLS, max_tokens=8000)
            reactive_retries = 0
        except Exception as e:
            err = str(e).lower()
            if ("prompt_too_long" in err or "too many tokens" in err) and reactive_retries < MAX_REACTIVE:
                print("  🆘 API 仍报 prompt_too_long，触发应急压缩...")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise

        messages.append({"role":"assistant","content":response.content})
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role":"user","content":str(force)})
                continue
            return

        rounds_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use": continue

            # ★ s08：compact 工具触发时，立即执行压缩
            if block.name == "compact":
                print("  🔧 模型主动请求 compact...")
                messages[:] = compact_history(messages)
                results.append({"type":"tool_result","tool_use_id":block.id,
                                "content":"[已压缩。对话历史已被 LLM 摘要替代。]"})
                messages.append({"role":"user","content":results})
                break

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type":"tool_result","tool_use_id":block.id,"content":str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"未知工具:{block.name}"
            trigger_hooks("PostToolUse", block, output)
            if block.name == "todo_write": rounds_since_todo = 0
            results.append({"type":"tool_result","tool_use_id":block.id,"content":output})
        else:
            messages.append({"role":"user","content":results})
            continue
        continue


if __name__ == "__main__":
    print("=" * 55)
    print("  s08: Context Compact — 四层压缩管线")
    print("=" * 55)
    print()
    print("  核心原则：便宜的先跑，贵的后跑")
    print()
    print("  L3 budget  → 大工具结果落盘（0 API）")
    print("  L1 snip    → 裁掉中间旧消息（0 API）")
    print("  L2 micro   → 旧结果替换占位符（0 API）")
    print("  L4 compact → LLM 全量摘要   （1 API，最后手段）")
    print("  应急        → API 报错时激进裁减")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    while True:
        try:
            query = input("\033[36ms08 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role":"user","content":query})
        agent_loop(history)
        last = history[-1]["content"]
        if isinstance(last, list):
            for b in last:
                if getattr(b, "type", None) == "text":
                    print(f"\n\033[32m{b.text}\033[0m")
        print()
