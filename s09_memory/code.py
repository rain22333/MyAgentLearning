#!/usr/bin/env python3
"""
s09_memory/code.py — Memory 系统：压缩会丢细节，要有一层不丢的

s08 的问题：压缩管线可以腾出上下文空间，但会丢失细节。
"用 tab 缩进不用空格" 可能被简化成 "用户有代码风格偏好"。
而且新开一个会话，连摘要都没了。

s09 的解法：在上下文之外建立一层持久化存储 —— Memory 系统。

  存储: .memory/ 目录，每个记忆一个 .md 文件
  加载: 索引注入 SYSTEM（始终可见）+ 相关内容按需注入当前消息
  提取: 每轮结束后，LLM 分析对话，自动提取新记忆
  整理: 定期合并去重，防止文件膨胀

核心思想：重要的东西别放 messages 里等着被裁 —— 主动存到不会被压缩的地方。

工具实现和 Hook 系统从 common/ 导入，本章聚焦 Memory 的四项操作。
"""
import os, sys, json, time, re
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
MEMORY_DIR = WORKDIR / ".memory"; MEMORY_DIR.mkdir(exist_ok=True)
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
CURRENT_TODOS: list[dict] = []


# ═══════════════════════════════════════════════════════════
#  ★【s09 新概念 1/4】存储：Markdown 文件 + 索引
# ═══════════════════════════════════════════════════════════

# 四种记忆类型
MEMORY_TYPES = ["user", "feedback", "project", "reference"]


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


def write_memory_file(name: str, mem_type: str, description: str, body: str):
    """写入单个记忆文件，带 YAML frontmatter"""
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8"
    )
    _rebuild_index()
    print(f"  💾 [MEMORY] 保存: {name} → {filename}")
    return filepath


def _rebuild_index():
    """重建 MEMORY.md 索引：每个记忆文件一行链接"""
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md": continue
        raw = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def read_memory_index() -> str:
    """读取索引（每轮注入 SYSTEM prompt）"""
    if not MEMORY_INDEX.exists(): return ""
    return MEMORY_INDEX.read_text(encoding="utf-8").strip()


def read_memory_file(filename: str) -> str | None:
    path = MEMORY_DIR / filename
    return path.read_text(encoding="utf-8") if path.exists() else None


def list_memory_files() -> list[dict]:
    """列出所有记忆文件的元数据"""
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md": continue
        raw = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename": f.name, "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type", "user"), "body": body,
        })
    return result


# ═══════════════════════════════════════════════════════════
#  ★【s09 新概念 2/4】加载：两条路径
#     路径①: 索引注入 SYSTEM（始终可见，便宜）
#     路径②: 相关内容按需注入当前消息（LLM 选择，5 条上限）
# ═══════════════════════════════════════════════════════════

def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """
    LLM side-query：根据最近的对话内容，从记忆目录中选出相关的文件。
    返回文件名列表，最多 max_items 个。
    不确定就不选，宁愿漏掉也不要乱塞。
    """
    files = list_memory_files()
    if not files: return []

    # 收集最近对话作为上下文
    recent = []
    for m in messages[-6:]:
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("content", "")[:200] if isinstance(b, dict) else ""
                for b in content if isinstance(b, dict)
            )
        recent.append(f"[{m.get('role','?')}] {str(content)[:500]}")

    # 构建记忆目录
    catalog = "\n".join(
        f"{i}: {f['name']} — {f['description']}"
        for i, f in enumerate(files)
    )

    try:
        response = client.messages.create(
            model=MODEL,
            system="你是一个记忆选择器。根据对话内容选择相关的记忆。只返回 JSON 数组，不解释。",
            messages=[{"role": "user", "content": (
                f"最近的对话：\n{chr(10).join(recent)}\n\n"
                f"记忆目录：\n{catalog}\n\n"
                "选出与当前对话相关的记忆编号，用 JSON 数组返回，如 [0, 3]。不确定就不选。最多 5 个。"
            )}],
            max_tokens=200,
        )
        text = ""
        for b in response.content:
            if hasattr(b, "text"): text += b.text
        indices = json.loads(re.search(r'\[.*?\]', text).group() or "[]")
        return [files[i]["filename"] for i in indices if 0 <= i < len(files)]
    except Exception:
        # LLM 选择失败 → 退化为关键词匹配
        recent_text = " ".join(str(m.get("content", "")) for m in messages[-3:]).lower()
        selected = []
        for f in files:
            if f["name"].lower() in recent_text or f["description"].lower() in recent_text:
                selected.append(f["filename"])
        return selected[:max_items]


def load_memories(messages: list) -> str:
    """
    加载记忆内容，注入到当前 user turn 前面。
    路径①: 索引已通过 build_system() 注入 SYSTEM prompt
    路径②: 本函数做按需加载，读选中文件的完整内容
    """
    selected = select_relevant_memories(messages, max_items=5)
    if not selected: return ""

    parts = ["## 📋 相关记忆\n"]
    for filename in selected:
        content = read_memory_file(filename)
        if content:
            parts.append(f"### {filename}\n{content}\n")
            print(f"  📖 [MEMORY] 注入: {filename}")

    return "\n".join(parts) + "\n---\n"


# ═══════════════════════════════════════════════════════════
#  ★【s09 新概念 3/4】提取：每轮结束后自动分析对话
#     用 LLM 判断是否有值得保存的信息，有就写入文件
# ═══════════════════════════════════════════════════════════

def extract_memories(pre_compress_messages: list):
    """
    每轮对话结束后触发。
    用 LLM 分析完整的原始对话（压缩前），提取新的记忆。
    
    关键：使用 pre_compress_messages（压缩前的完整对话），
    而不是压缩后的 messages。这样即使压缩丢了细节，提取时还能看到。
    """
    if not pre_compress_messages or len(pre_compress_messages) < 4:
        return

    # 构建对话文本
    history = []
    for m in pre_compress_messages[-20:]:  # 只看最近 20 条
        content = m.get("content", "")
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    parts.append(f"[工具结果: {str(b.get('content',''))[:200]}]")
                elif hasattr(b, "text"):
                    parts.append(b.text[:300])
            content = "\n".join(parts)
        history.append(f"[{m.get('role','?')}] {str(content)[:800]}")

    prompt = (
        "分析以下对话，提取值得长期保存的信息。\n\n"
        f"对话：\n{chr(10).join(history)}\n\n"
        "如果有值得保存的信息，用以下 JSON 格式返回：\n"
        '{"memories":[{"name":"记忆名称","type":"user|feedback|project|reference",'
        '"description":"一句话描述","body":"详细内容（含 Why 和 How）"}]\n\n'
        "记忆类型说明：\n"
        "- user: 用户偏好（如代码风格、工具选择）\n"
        "- feedback: 行为反馈（如不要 mock 数据库）\n"
        "- project: 项目事实（如当前阶段、架构决策）\n"
        "- reference: 引用信息（如 bug 在哪、文档在哪）\n\n"
        "如果没有值得保存的信息，返回 {\"memories\":[]}。"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            system="你是记忆提取器。只返回 JSON。不确定就不提取。",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )
        text = ""
        for b in response.content:
            if hasattr(b, "text"): text += b.text

        # 提取 JSON
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match: return
        data = json.loads(match.group())
        for mem in data.get("memories", []):
            name = mem.get("name", "")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if name and body:
                write_memory_file(name, mem_type, desc, body)
    except Exception as e:
        pass  # 提取失败不阻塞主流程


# ═══════════════════════════════════════════════════════════
#  ★【s09 新概念 4/4】整理（Dream）：定期合并去重
#     记忆文件多了会膨胀，定期让 LLM 合并相似内容
# ═══════════════════════════════════════════════════════════

def consolidate_memories():
    """当记忆文件超过 10 个时，触发合并且去重"""
    files = list_memory_files()
    if len(files) < 10:
        return

    print(f"  🌙 [DREAM] 记忆文件已达 {len(files)} 个，触发整理...")

    # 收集所有记忆
    all_text = "\n\n---\n\n".join(
        f"## {f['name']} ({f['type']})\n{f['body']}" for f in files
    )

    try:
        response = client.messages.create(
            model=MODEL,
            system="你是记忆整理器。合并重复和相似的内容，输出更精简的记忆列表。只返回 JSON。",
            messages=[{"role": "user", "content": (
                "整理以下记忆，合并重复和相似的，保持信息不丢失。\n\n"
                f"{all_text[:30000]}\n\n"
                "返回 JSON: {\"memories\":[{\"name\":\"...\",\"type\":\"...\","
                "\"description\":\"...\",\"body\":\"...\"}]}"
            )}],
            max_tokens=4000,
        )
        text = ""
        for b in response.content:
            if hasattr(b, "text"): text += b.text

        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match: return
        data = json.loads(match.group())

        # 清空旧记忆，写入合并后的
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in data.get("memories", []):
            if mem.get("name") and mem.get("body"):
                write_memory_file(
                    mem["name"], mem.get("type", "user"),
                    mem.get("description", ""), mem["body"]
                )

        print(f"  ✅ [DREAM] 整理完成: {len(files)} → {len(data.get('memories',[]))} 个记忆")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  继承：技能加载 + todo_write + 子 Agent + 压缩管线
# ═══════════════════════════════════════════════════════════

# ── 技能 ──
SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    if not SKILLS_DIR.exists(): return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir(): continue
        m = d / "SKILL.md"
        if m.exists():
            raw = m.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(raw)
            SKILL_REGISTRY[meta.get("name", d.name)] = {
                "name": meta.get("name", d.name),
                "description": meta.get("description", ""),
                "content": raw,
            }

_scan_skills()

def load_skill(name: str) -> str:
    s = SKILL_REGISTRY.get(name)
    return s["content"] if s else f"未找到: {name}"


def build_system() -> str:
    """★ s09: SYSTEM prompt 包含技能目录 + 记忆索引"""
    skills = "\n".join(
        f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values()
    ) if SKILL_REGISTRY else "(无技能)"

    mem_index = read_memory_index()
    mem_section = f"## 记忆\n{mem_index}\n" if mem_index else ""

    return (
        f"你是 {WORKDIR} 下的编码助手。\n\n"
        f"## 技能\n{skills}\n\n"
        f"{mem_section}\n"
        "用中文回答。需要时使用 load_skill / write_memory / read_memory。"
    )


# ── todo_write ──
def run_todo_write(todos: list[dict]) -> str:
    global CURRENT_TODOS
    CURRENT_TODOS = todos
    icons = {"pending":"🔲","in_progress":"🔄","completed":"✅","cancelled":"❌"}
    lines = ["\n  ┌─── 当前任务计划 ───"]
    for t in CURRENT_TODOS:
        lines.append(f"  │ {icons.get(t.get('status','pending'),'❓')} {t.get('content','')}")
    lines.append("  └────────────────────\n")
    print("\n".join(lines))
    return f"已更新 {len(CURRENT_TODOS)} 个任务"


# ── 子 Agent ──
SUB_SYSTEM = f"你是 {WORKDIR} 下的编码助手。完成任务后返回结论。用中文。"

def extract_text(c) -> str:
    if isinstance(c, str): return c
    if isinstance(c, list):
        return "\n".join(b.text for b in c if hasattr(b,"text")) or "(无文本)"
    return str(c)

def spawn_subagent(desc: str) -> str:
    print(f"\n  🚀 [SUB] {desc[:80]}...")
    msgs = [{"role":"user","content":desc}]
    for t in range(30):
        r = client.messages.create(model=MODEL,system=SUB_SYSTEM,messages=msgs,tools=BASE_TOOLS,max_tokens=8000)
        msgs.append({"role":"assistant","content":r.content})
        if r.stop_reason != "tool_use":
            print(f"  ✅ [SUB] 完成({t+1}轮)"); return extract_text(msgs[-1]["content"])
        res = []
        for b in r.content:
            if b.type != "tool_use": continue
            blocked = trigger_hooks("PreToolUse", b)
            if blocked: res.append({"type":"tool_result","tool_use_id":b.id,"content":str(blocked)}); continue
            h = BASE_HANDLERS.get(b.name)
            res.append({"type":"tool_result","tool_use_id":b.id,"content":h(**b.input) if h else f"未知:{b.name}"})
        msgs.append({"role":"user","content":res})
    return "(子Agent达上限)"


# ── 压缩管线（s08 继承）──
CONTEXT_LIMIT = 150_000; MAX_MESSAGES = 50; KEEP_RECENT_RESULTS = 3; TOOL_RESULT_BUDGET = 200_000

def estimate_size(msgs): return sum(len(str(m)) for m in msgs)

def tool_result_budget(msgs):
    if not msgs: return msgs
    last = msgs[-1]
    if not isinstance(last.get("content"), list): return msgs
    blocks = [(i,b) for i,b in enumerate(last["content"]) if isinstance(b,dict) and b.get("type")=="tool_result"]
    total = sum(len(str(b.get("content",""))) for _,b in blocks)
    if total <= TOOL_RESULT_BUDGET: return msgs
    for idx,block in sorted(blocks,key=lambda p:len(str(p[1].get("content",""))),reverse=True):
        content = str(block.get("content",""))
        if len(content) <= 2000: continue
        TOOL_RESULTS_DIR.mkdir(parents=True,exist_ok=True)
        fname = TOOL_RESULTS_DIR / f"{block.get('tool_use_id','unknown')}.json"
        fname.write_text(content,encoding="utf-8")
        block["content"] = f"<persisted-output path='{fname}'>\n{content[:2000]}\n...(共{len(content):,}字符)</persisted-output>"
        if sum(len(str(b.get("content",""))) for _,b in blocks) <= TOOL_RESULT_BUDGET: break
    return msgs

def snip_compact(msgs):
    if len(msgs) <= MAX_MESSAGES: return msgs
    keep_head, keep_tail = 3, MAX_MESSAGES - 3
    snipped = len(msgs) - keep_head - keep_tail
    print(f"  ✂️  [L1 snip] {len(msgs)}→{MAX_MESSAGES}")
    return msgs[:keep_head] + [{"role":"user","content":f"[已裁剪中间{snipped}条消息]"}] + msgs[-keep_tail:]

def micro_compact(msgs):
    tr = [(mi,bi,b) for mi,m in enumerate(msgs) for bi,b in enumerate(m.get("content",[]) or [])
          if isinstance(m.get("content"),list) and isinstance(b,dict) and b.get("type")=="tool_result"]
    if len(tr) <= KEEP_RECENT_RESULTS: return msgs
    for mi,bi,block in tr[:-KEEP_RECENT_RESULTS]:
        if len(str(block.get("content",""))) > 120:
            block["content"] = "[旧工具结果已压缩。]"
    print(f"  🗜️  [L2 micro] {len(tr)}结果→保留{KEEP_RECENT_RESULTS}")
    return msgs

def compact_history(msgs):
    history = []
    for m in msgs:
        c = m.get("content","")
        if isinstance(c,list):
            c = "\n".join(f"[{b.get('type','?')}] {str(b.get('content',''))[:300]}" for b in c if isinstance(b,dict))
        history.append(f"[{m.get('role','?')}] {str(c)[:1000]}")
    prompt = ("压缩对话为结构化摘要（中文）：\n1.任务目标 2.已完成 3.当前状态 4.关键发现 5.下一步\n\n"
              f"{chr(10).join(history)[:80000]}")
    print("  🧠 [L4 compact] ...")
    try:
        r = client.messages.create(model=MODEL,system="压缩器。只输出摘要，不调工具。",
                                   messages=[{"role":"user","content":prompt}],max_tokens=4000)
        summary = extract_text(r.content)
    except: summary = "[压缩失败]"
    TRANSCRIPT_DIR.mkdir(parents=True,exist_ok=True)
    p = TRANSCRIPT_DIR / f"compact_{time.strftime('%Y%m%d_%H%M%S')}.json"
    p.write_text(json.dumps(msgs,ensure_ascii=False,default=str),encoding="utf-8")
    return [{"role":"user","content":f"[上下文已压缩]\n\n{summary}\n\n原始对话: {p}"}]

def reactive_compact(msgs):
    if len(msgs) <= 10: return msgs
    return msgs[:2] + [{"role":"user","content":f"[紧急压缩:{len(msgs)}→10条]"}] + msgs[-8:]


# ═══════════════════════════════════════════════════════════
#  工具定义
# ═══════════════════════════════════════════════════════════
TOOLS = BASE_TOOLS + [
    {"name":"todo_write","description":"创建和管理任务计划。",
     "input_schema":{"type":"object","properties":{"todos":{"type":"array","items":{"type":"object","properties":{"content":{"type":"string"},"status":{"type":"string","enum":["pending","in_progress","completed","cancelled"]}},"required":["content","status"]}}},"required":["todos"]}},
    {"name":"task","description":"启动子Agent。",
     "input_schema":{"type":"object","properties":{"description":{"type":"string"}},"required":["description"]}},
    {"name":"load_skill","description":"加载技能完整指南。",
     "input_schema":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}},
    {"name":"compact","description":"请求压缩上下文。",
     "input_schema":{"type":"object","properties":{"reason":{"type":"string"}},"required":[]}},
    # ★ s09 新增：write_memory / read_memory
    {"name":"write_memory","description":"写入一条持久记忆。记忆会跨会话保存，不会被压缩清除。",
     "input_schema":{"type":"object","properties":{"name":{"type":"string"},"type":{"type":"string","enum":["user","feedback","project","reference"]},"description":{"type":"string"},"body":{"type":"string"}},"required":["name","type","description","body"]}},
    {"name":"read_memory","description":"读取记忆文件的完整内容。",
     "input_schema":{"type":"object","properties":{"filename":{"type":"string"}},"required":["filename"]}},
]

TOOL_HANDLERS = {
    **BASE_HANDLERS,
    "todo_write": run_todo_write, "task": spawn_subagent,
    "load_skill": load_skill,
    "write_memory": lambda **kw: str(write_memory_file(kw["name"],kw.get("type","user"),kw.get("description",""),kw.get("body",""))),
    "read_memory": lambda filename: read_memory_file(filename) or f"未找到: {filename}",
}

register_default_hooks()
rounds_since_todo = 0; MAX_REACTIVE = 1


def agent_loop(messages: list):
    global rounds_since_todo
    reactive_retries = 0

    # ★ s09: 加载相关记忆，注入到当前 user turn
    memories_content = load_memories(messages)

    while True:
        if rounds_since_todo >= 3 and messages:
            messages.append({"role":"user","content":"<reminder>请更新 todo_write。</reminder>"})
            rounds_since_todo = 0

        system = build_system()  # ★ s09: 每次重建，包含最新记忆索引

        # ★ 保存压缩前快照（用于准确提取记忆）
        pre_compress = [m.copy() if isinstance(m,dict) else {"role":m.get("role",""),"content":str(m.get("content",""))} for m in messages]

        # 压缩管线（s08 继承）
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)
        if estimate_size(messages) > CONTEXT_LIMIT:
            messages[:] = compact_history(messages)

        # ★ 将相关记忆注入到当前 user turn 前面
        request_messages = messages
        if memories_content and messages:
            last_user = next((i for i in range(len(messages)-1,-1,-1) if messages[i].get("role")=="user"), None)
            if last_user is not None:
                request_messages = messages.copy()
                request_messages[last_user] = {
                    **messages[last_user],
                    "content": memories_content + "\n" + str(messages[last_user].get("content","")),
                }

        try:
            response = client.messages.create(model=MODEL,system=system,messages=request_messages,tools=TOOLS,max_tokens=8000)
            reactive_retries = 0
        except Exception as e:
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE:
                messages[:] = reactive_compact(messages); reactive_retries += 1; continue
            raise

        messages.append({"role":"assistant","content":response.content})
        if response.stop_reason != "tool_use":
            # ★ s09: 从压缩前的完整对话中提取记忆
            extract_memories(pre_compress)
            consolidate_memories()
            force = trigger_hooks("Stop", messages)
            if force: messages.append({"role":"user","content":str(force)}); continue
            return

        rounds_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            if block.name == "compact":
                messages[:] = compact_history(messages)
                results.append({"type":"tool_result","tool_use_id":block.id,"content":"[已压缩。]"})
                messages.append({"role":"user","content":results}); break
            blocked = trigger_hooks("PreToolUse", block)
            if blocked: results.append({"type":"tool_result","tool_use_id":block.id,"content":str(blocked)}); continue
            h = TOOL_HANDLERS.get(block.name)
            output = h(**block.input) if h else f"未知:{block.name}"
            trigger_hooks("PostToolUse", block, output)
            if block.name == "todo_write": rounds_since_todo = 0
            results.append({"type":"tool_result","tool_use_id":block.id,"content":output})
        else:
            messages.append({"role":"user","content":results}); continue
        continue


if __name__ == "__main__":
    print("=" * 55)
    print("  s09: Memory — 跨会话持久记忆")
    print("=" * 55)
    print()
    print("  四项操作：")
    print("    存储 → .memory/ 目录，Markdown + YAML frontmatter")
    print("    加载 → 索引注入 SYSTEM + 相关内容按需注入")
    print("    提取 → 每轮结束后 LLM 自动分析对话")
    print("    整理 → 记忆 > 10 个时自动合并去重")
    print()
    print("  记忆类型：user(偏好) / feedback(反馈) / project(进展) / reference(引用)")
    print()
    print("  试试这些 prompt：")
    print("    1. 记住：我喜欢用 4 空格缩进，不用 tab")
    print("    2. 我是一个做电商系统的项目，整体架构是微服务")
    print("    3. 现在列出所有记忆")  
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    while True:
        try:
            query = input("\033[36ms09 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！"); break
        if query.strip().lower() in ("q","exit",""): print("再见！"); break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role":"user","content":query})
        agent_loop(history)
        last = history[-1]["content"]
        if isinstance(last, list):
            for b in last:
                if getattr(b, "type", None) == "text":
                    print(f"\n\033[32m{b.text}\033[0m")
        print()
