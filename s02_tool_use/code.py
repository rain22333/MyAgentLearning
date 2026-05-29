#!/usr/bin/env python3
"""
s02_tool_use/code.py — 多工具 + 分发表

s01 只有一个 bash 工具：
  模型想读文件 → 得拼 cat xxx.py
  模型想写文件 → 得拼 echo "..." > xxx.py
  模型想找文件 → 得拼 find . -name "*.py"
  又丑又容易出错，还浪费 token 做"翻译"

s02 给 5 个专用工具：bash / read_file / write_file / edit_file / glob
  加一个工具 = 定义 TOOLS 条目 + 注册 TOOL_HANDLERS 映射
  核心循环 agent_loop 一毛一样，只改了一行：
    s01: output = run_bash(block.input["command"])
    s02: output = TOOL_HANDLERS[block.name](**block.input)
"""

import os
import subprocess
from pathlib import Path

try:
    import readline
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"), override=True)  # 从根目录 .env 加载
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"你是一个在 {WORKDIR} 目录下工作的编码助手。可以使用多种工具。用中文回答，直接行动。"

# ═══════════════════════════════════════════════════════════
#  【来自 s01】bash 工具实现（不变）
# ═══════════════════════════════════════════════════════════
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/",
                 "format", "del /f /s", "rd /s /q C:\\"]
    if any(d.lower() in command.lower() for d in dangerous):
        return "Error: 危险命令已被拦截"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "Error: 命令超时 (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  【NEW in s02】safe_path：防止路径穿越攻击
#  比如模型传 ../../etc/passwd，会被拦截
# ═══════════════════════════════════════════════════════════
def safe_path(p: str) -> Path:
    """将相对路径解析为工作目录下的绝对路径，防止越权访问"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径越界: {p}")
    return path


# ═══════════════════════════════════════════════════════════
#  【NEW in s02】4 个新工具的实现
# ═══════════════════════════════════════════════════════════
def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容，可选限制行数"""
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入内容到文件（自动创建父目录）"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节到 {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """在文件中精确替换一段文本（只替换第一次出现）"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: 在 {path} 中找不到指定文本"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已编辑 {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    """按通配符模式查找文件，如 *.py、**/*.md"""
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(无匹配)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  【NEW in s02】工具定义：从 1 个扩展到 5 个
# ═══════════════════════════════════════════════════════════
TOOLS = [
    {
        "name": "bash",
        "description": "执行 Shell 命令（Windows PowerShell/cmd）。",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要执行的命令"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取文件内容，可以指定行数限制。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "limit": {"type": "integer", "description": "最多读取行数（可选）"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "将内容写入文件（覆盖写入，自动创建目录）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的内容"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "在文件中精确替换一段文本（只替换首次出现）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "old_text": {"type": "string", "description": "要被替换的原文本"},
                "new_text": {"type": "string", "description": "替换后的新文本"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "按通配符查找文件，如 *.py、**/*.md。",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "通配符模式"}},
            "required": ["pattern"],
        },
    },
]

# ═══════════════════════════════════════════════════════════
#  【NEW in s02】工具分发表 — s02 的核心创新
#  加新工具 = 在这加一行映射即可，循环不用改
# ═══════════════════════════════════════════════════════════
TOOL_HANDLERS = {
    "bash":       run_bash,
    "read_file":  run_read,
    "write_file": run_write,
    "edit_file":  run_edit,
    "glob":       run_glob,
}


# ═══════════════════════════════════════════════════════════
#  agent_loop — 和 s01 结构完全一致！
#  唯一变化：工具执行从硬编码 run_bash 变成查表分发
#
#  s01: output = run_bash(block.input["command"])
#  s02: handler = TOOL_HANDLERS[block.name]
#        output = handler(**block.input)
# ═══════════════════════════════════════════════════════════
def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_name = block.name
                args = dict(block.input)
                print(f"\033[33m>>> [{tool_name}] {args}\033[0m")

                # ★ 这一行是 s02 相对于 s01 的唯一变化 ★
                handler = TOOL_HANDLERS.get(tool_name)
                output = handler(**args) if handler else f"未知工具: {tool_name}"

                print(str(output)[:300])
                if len(str(output)) > 300:
                    print("... (输出过长，已截断)")

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

        messages.append({"role": "user", "content": results})


# ── 入口 ──
if __name__ == "__main__":
    print("=" * 55)
    print("  s02: Tool Use — 从 1 个工具到 5 个工具")
    print("=" * 55)
    print()
    print("  新增工具：read_file / write_file / edit_file / glob")
    print("  核心机制：TOOL_HANDLERS 分发表（加工具 = 加一行）")
    print("  循环不变：agent_loop 和 s01 结构完全相同")
    print()
    print("  试试这些 prompt：")
    print("    1. 读一下 code.py 的前 20 行")
    print("    2. 创建一个 test.txt，内容是 Hello s02!")
    print("    3. 把 test.txt 里的 Hello 改成 Hi，然后读出来确认")
    print("    4. 找出所有 .py 文件")
    print()
    print("  输入 q / exit / 空行 退出")
    print()

    history = []
    while True:
        try:
            query = input("\033[36ms02 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        last_msg = history[-1]["content"]
        if isinstance(last_msg, list):
            for block in last_msg:
                if getattr(block, "type", None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")
        print()
