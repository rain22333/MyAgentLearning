#!/usr/bin/env python3
"""
s10_system_prompt/code.py — System Prompt：运行时组装，不硬编码

从 s01 到 s09，SYSTEM 都是一行硬编码字符串：
  SYSTEM = "你是一个编码助手..."

三个问题：
  1. 能力越来越多，prompt 膨胀成一锅粥
  2. 改一处可能影响全局，不知道哪些该改哪些不该
  3. 即使当前对话用不到某些内容（比如没有记忆文件），也每轮都带着

s10 的解法：把 prompt 拆成独立的 section，运行时根据真实状态按需组装。

  核心思路：prompt 是组装出来的，不是写死的。

工具实现从 common/ 导入，本章聚焦三段式 prompt 组装 + 缓存机制。
"""
import os, sys, json
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
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


# ═══════════════════════════════════════════════════════════
#  ★【s10 新概念 1/3】PROMPT_SECTIONS：按主题拆分
#  每个 section 独立维护，互不影响
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": (
        "你是一个在项目目录中工作的编码助手。"
        "用中文回答，直接行动，不要废话。"
    ),
    "tools": (
        "你有以下工具：bash(执行命令)、read_file(读文件)、"
        "write_file(写文件)、edit_file(编辑文件)、glob(查找文件)。"
    ),
    "workspace": f"当前工作目录: {WORKDIR}",
    "memory": "",  # 动态填充：只在 .memory/MEMORY.md 存在时才加载
    "skills": "",  # 动态填充：只在 skills/ 目录非空时才加载
}


# ═══════════════════════════════════════════════════════════
#  ★【s10 新概念 2/3】assemble_system_prompt：按真实状态组装
#  不是"看到关键词就加载"——是"文件真的存在才加载"
# ═══════════════════════════════════════════════════════════

def assemble_system_prompt(context: dict) -> str:
    """
    根据 context 的真实状态，选择性拼接 section。
    
    始终加载：identity、tools、workspace（每轮都需要）
    按需加载：memory（MEMORY.md 存在时）、skills（skills/ 非空时）
    """
    sections = []

    # ── 始终加载 ──
    sections.append(PROMPT_SECTIONS["identity"])
    sections.append(PROMPT_SECTIONS["tools"])
    sections.append(PROMPT_SECTIONS["workspace"])

    # ── 按需加载：基于文件系统真实状态，不是关键词匹配 ──
    if context.get("has_skills"):
        skills = context.get("skills_catalog", "")
        sections.append(f"## 可用技能\n{skills}")

    if context.get("has_memories"):
        memories = context.get("memories_index", "")
        sections.append(f"## 记忆\n{memories}")

    return "\n\n".join(sections)


# ═══════════════════════════════════════════════════════════
#  ★【s10 新概念 3/3】get_system_prompt：缓存避免重复组装
#  context 没变 → 不重新拼字符串（进程内优化）
# ═══════════════════════════════════════════════════════════

_last_context_key: str | None = None
_last_prompt: str | None = None


def get_system_prompt(context: dict) -> str:
    """
    缓存 wrapper：
    - context 和上次一样 → 返回缓存的 prompt（打印 cache hit）
    - context 变了 → 重新组装（打印 assembled sections）
    
    用 json.dumps 做确定性比较，不用 Python 的 hash()
    （hash 有进程随机化，且无法处理嵌套 dict）
    """
    global _last_context_key, _last_prompt

    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        print("  ✅ [cache hit] system prompt 未变，复用缓存")
        return _last_prompt

    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    # 打印加载了哪些 section
    loaded = ["identity", "tools", "workspace"]
    if context.get("has_skills"): loaded.append("skills")
    if context.get("has_memories"): loaded.append("memory")
    print(f"  🔧 [assembled] sections: {', '.join(loaded)} "
          f"({len(_last_prompt)} 字符)")

    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  update_context：从文件系统真实状态推导 context
# ═══════════════════════════════════════════════════════════

def update_context() -> dict:
    """根据文件系统状态构建 context dict"""
    context = {
        "workspace": str(WORKDIR),
        "has_skills": False,
        "has_memories": False,
    }

    # 检查 skills 目录
    skills_dir = WORKDIR / "skills"
    if skills_dir.exists() and any(skills_dir.iterdir()):
        skill_names = []
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                raw = (d / "SKILL.md").read_text(encoding="utf-8")
                # 简单提取名称
                name = d.name
                if raw.startswith("---"):
                    for line in raw.split("---")[1].strip().splitlines():
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip().strip('"').strip("'")
                            break
                skill_names.append(f"- {name}")
        if skill_names:
            context["has_skills"] = True
            context["skills_catalog"] = "\n".join(skill_names)

    # 检查 .memory/MEMORY.md
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text(encoding="utf-8").strip()
        if content:
            context["has_memories"] = True
            context["memories_index"] = content

    return context


# ═══════════════════════════════════════════════════════════
#  工具和循环（从 common 继承，简化版）
# ═══════════════════════════════════════════════════════════

TOOLS = BASE_TOOLS
TOOL_HANDLERS = BASE_HANDLERS
register_default_hooks()


def agent_loop(messages: list, context: dict):
    system = get_system_prompt(context)

    while True:
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": str(force)})
                continue
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            print(f"\033[36m>>> [{block.name}]\033[0m")
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": str(blocked),
                })
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"未知工具: {block.name}"
            print(str(output)[:200])

            trigger_hooks("PostToolUse", block, output)
            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": results})

        # ★ 每轮工具执行后重新评估 context、重建 system prompt
        context = update_context()
        system = get_system_prompt(context)


# ── 入口 ──
if __name__ == "__main__":
    print("=" * 55)
    print("  s10: System Prompt — 运行时组装")
    print("=" * 55)
    print()
    print("  之前: SYSTEM = '硬编码的一大段字符串'")
    print("  现在: PROMPT_SECTIONS → assemble → cache")
    print()
    print("  始终加载: identity / tools / workspace")
    print("  按需加载: memory（.memory/MEMORY.md 存在时）")
    print("            skills（skills/ 目录非空时）")
    print()
    print("  试试：")
    print("    1. 观察启动时 loaded sections")
    print("    2. 创建 .memory/MEMORY.md → 下一轮 memory section 自动出现")
    print("    3. 删除它 → 下一轮 memory section 消失")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    context = update_context()
    while True:
        try:
            query = input("\033[36ms10 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！"); break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！"); break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context()
        last = history[-1]["content"]
        if isinstance(last, list):
            for b in last:
                if getattr(b, "type", None) == "text":
                    print(f"\n\033[32m{b.text}\033[0m")
        print()
