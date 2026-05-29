#!/usr/bin/env python3
"""
s07_skill_loading/code.py — 技能加载：用到时才加载完整内容

s06 的问题：你有一堆项目规范（React 风格指南、SQL 规范、API 设计文档）。
最直接的做法全塞进 system prompt → 6500 行！
Agent 每次调 LLM 都带着这些文档，不管当前任务是否需要。99% 的内容无关。

s07 的解法：两级加载
  第 1 级（便宜，始终存在）：
    启动时扫描 skills/ 目录，把技能名称 + 一句话描述注入 SYSTEM prompt
    ~100 tokens/技能，每轮都带，但很轻量

  第 2 级（昂贵，按需加载）：
    Agent 调用 load_skill("code-review") → 完整 SKILL.md 内容
    通过 tool_result 注入对话，~2000 tokens/技能，用到才花

  类比：目录 vs 全文。你书架上有 50 本书的目录（第 1 级），
  需要哪本才拿下来翻开（第 2 级）。不会把 50 本书全摊在桌上。

工具实现和 Hook 系统从 common/ 导入，本章聚焦技能加载机制。
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
SKILLS_DIR = WORKDIR / "skills"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
CURRENT_TODOS: list[dict] = []


# ═══════════════════════════════════════════════════════════
#  ★【s07 新概念】两级技能加载系统
# ═══════════════════════════════════════════════════════════

# ── 前端内容解析 ──
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML frontmatter。返回 (元数据, 正文)。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            val = v.strip().strip('"').strip("'")
            meta[k.strip()] = val
    return meta, parts[2].strip()


# ── 第 1 级：启动时扫描 skills/ 目录，构建注册表 ──
SKILL_REGISTRY: dict[str, dict] = {}


def _scan_skills():
    """
    启动时一次执行：扫描 skills/ 目录下的每个子目录，
    解析每个 SKILL.md 的 frontmatter，存入 SKILL_REGISTRY。
    
    SKILL_REGISTRY 的作用：
    - 生成 SYSTEM prompt 中的技能目录（第 1 级加载）
    - load_skill 时直接从注册表取内容（避免路径遍历风险）
    """
    if not SKILLS_DIR.exists():
        print("  ⚠️  skills/ 目录不存在，跳过技能加载")
        return

    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if not manifest.exists():
            continue

        raw = manifest.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", d.name)
        desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
        SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

    print(f"  📚 已加载 {len(SKILL_REGISTRY)} 个技能: {', '.join(SKILL_REGISTRY.keys())}")


_scan_skills()  # 启动时立即执行


# ── 生成技能目录（注入 SYSTEM prompt）──
def list_skills() -> str:
    """列出所有技能的名称和一句话描述（第 1 级：不到 100 tokens/技能）"""
    if not SKILL_REGISTRY:
        return "(无可用技能)"
    return "\n".join(
        f"- **{s['name']}**: {s['description']}"
        for s in SKILL_REGISTRY.values()
    )


def build_system() -> str:
    """
    构建 SYSTEM prompt，注入技能目录。
    这是第 1 级加载：便宜、始终存在。
    """
    catalog = list_skills()
    return (
        f"你是一个在 {WORKDIR} 目录下工作的编码助手。\n\n"
        f"## 可用技能\n{catalog}\n\n"
        "当你需要某个技能的完整指南时，使用 load_skill 加载它。"
        "用中文回答，直接行动。"
    )


SYSTEM = build_system()


# ── 第 2 级：load_skill 工具 ──
def load_skill(name: str) -> str:
    """
    按需加载技能的完整内容（第 2 级：昂贵，只在需要时调用）。
    
    从 SKILL_REGISTRY 查找（不是从文件系统），
    避免路径遍历风险——Agent 只能通过技能名访问，
    不能指定任意文件路径。
    """
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        available = ", ".join(SKILL_REGISTRY.keys())
        return f"技能未找到: {name}。可用技能: {available}"
    print(f"  📖 [SKILL] 加载技能: {name} ({len(skill['content'])} 字符)")
    return skill["content"]


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


# ── 子 Agent ──
SUB_SYSTEM = f"你是 {WORKDIR} 下的编码助手。完成任务后返回简洁结论。不要委托。用中文回答。"

SUB_TOOLS = BASE_TOOLS
SUB_HANDLERS = BASE_HANDLERS


def extract_text(content) -> str:
    if isinstance(content, str): return content
    if isinstance(content, list):
        texts = [b.text for b in content if hasattr(b, "text")]
        return "\n".join(texts) if texts else "(无文本输出)"
    return str(content)


def spawn_subagent(description: str) -> str:
    print(f"\n  🚀 [SUBAGENT] {description[:80]}...")
    messages = [{"role": "user", "content": description}]
    for turn in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM, messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            print(f"  ✅ [SUBAGENT] 完成 ({turn + 1} 轮)")
            return extract_text(messages[-1]["content"])
        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)})
                continue
            handler = SUB_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"未知: {block.name}"
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
    return "(子 Agent 达到上限)"


# ═══════════════════════════════════════════════════════════
#  工具定义：基础 + todo_write + task + load_skill
# ═══════════════════════════════════════════════════════════
TOOLS = BASE_TOOLS + [
    {"name": "todo_write", "description": "创建和管理任务计划。",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "task", "description": "启动子 Agent 处理复杂子任务。",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    # ★ s07 新增：load_skill
    {"name": "load_skill", "description": "加载技能的完整指南内容。先查看 SYSTEM prompt 中的技能目录，按需加载。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "技能名称"}}, "required": ["name"]}},
]

TOOL_HANDLERS = {**BASE_HANDLERS, "todo_write": run_todo_write, "task": spawn_subagent, "load_skill": load_skill}

register_default_hooks()

rounds_since_todo = 0


def agent_loop(messages: list):
    global rounds_since_todo
    while True:
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user", "content": "<reminder>请更新 todo_write。</reminder>"})
            rounds_since_todo = 0

        response = client.messages.create(model=MODEL, system=SYSTEM, messages=messages, tools=TOOLS, max_tokens=8000)
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
            if block.type != "tool_use": continue
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)})
                continue
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"未知工具: {block.name}"
            trigger_hooks("PostToolUse", block, output)
            if block.name == "todo_write": rounds_since_todo = 0
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("=" * 55)
    print("  s07: Skill Loading — 两级按需加载")
    print("=" * 55)
    print()
    print("  第 1 级（便宜）：技能目录注入 SYSTEM prompt")
    print("    → Agent 每轮都能看到「有哪些技能可用」")
    print("  第 2 级（昂贵）：调用 load_skill 加载完整内容")
    print("    → 只有真正需要时才花 token 加载全文")
    print()
    print(f"  当前 skills/ 目录有: {', '.join(SKILL_REGISTRY.keys())}")
    print()
    print("  试试这些 prompt：")
    print("    1. 有哪些技能可用？")
    print("    2. 加载 python-expert 技能，然后创建一个符合规范的 Python 脚本")
    print("    3. 我需要做代码审查，先加载相关技能")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    while True:
        try:
            query = input("\033[36ms07 >>> \033[0m")
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
