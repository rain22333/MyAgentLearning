#!/usr/bin/env python3
"""
s01_agent_loop/code.py — Agent Loop 最小可运行教学版

核心概念：一个 while 循环，让 LLM 能持续调用工具直到完成任务。

    用户输入 → LLM → 调用了工具？→ 是 → 执行工具 → 结果喂回 LLM → 继续循环
                         → 否 → 输出最终回答，结束

整个 Claude Code 的核心就是这个不到 30 行的循环。
后面 s02~s20 的所有章节，都是在这个循环上叠加机制。
"""

import os
import subprocess

# ── 可选：让命令行支持更好的编辑体验 ──
try:
    import readline
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass  # Windows 上通常没有 readline，忽略即可

from anthropic import Anthropic
from dotenv import load_dotenv

# ── 第 1 层：初始化，连接 LLM ──
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"), override=True)  # 从根目录 .env 加载

# 如果用了第三方兼容接口，需要去掉 AUTH_TOKEN
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 系统提示词：告诉模型它是谁、能做什么
SYSTEM = (
    f"你是一个在 {os.getcwd()} 目录下工作的编码助手。"
    "你可以使用 shell 工具来执行命令（Windows PowerShell）。"
    "用中文回答。直接行动，不要废话。"
)

# ── 第 2 层：定义工具 ──
# 只给模型一个工具：shell（执行命令）
TOOLS = [{
    "name": "shell",
    "description": "执行一条 Shell 命令（Windows PowerShell/cmd）。用于读取文件、列出目录、运行脚本等。",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的命令，例如 dir、type xxx.py、python xxx.py",
            }
        },
        "required": ["command"],
    },
}]


# ── 第 3 层：工具执行函数 ──
def run_shell(command: str) -> str:
    """真正执行命令，返回 stdout + stderr"""
    # 简单的安全检查
    dangerous = [
        "rm -rf /", "sudo", "shutdown", "reboot", "> /dev/",
        "format", "del /f /s", "rd /s /q C:\\",
    ]
    if any(d.lower() in command.lower() for d in dangerous):
        return "Error: 危险命令已被拦截"

    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "Error: 命令超时 (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  第 4 层：核心 —— Agent Loop（不到 30 行）
# ═══════════════════════════════════════════════════════════
def agent_loop(messages: list):
    """
    核心循环：
    1. 把消息发给 LLM
    2. 如果 LLM 调用了工具 → 执行工具 → 把结果喂回去 → 回到步骤 1
    3. 如果 LLM 没调工具 → 结束（模型给出了最终回答）
    """
    while True:
        # ① 发送请求给 LLM
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        # ② 记录模型的回复
        messages.append({"role": "assistant", "content": response.content})

        # ③ 检查：模型有没有调用工具？
        if response.stop_reason != "tool_use":
            # 没调工具 → 任务完成，退出循环
            return

        # ④ 模型调了工具 → 逐个执行
        results = []
        for block in response.content:
            if block.type == "tool_use":
                command = block.input.get("command", "")
                print(f"\033[33m>>> 执行命令: {command}\033[0m")

                output = run_shell(command)
                # 只打印前 300 字预览
                print(output[:300])
                if len(output) > 300:
                    print("... (输出过长，已截断预览)")

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

        # ⑤ 把工具执行结果作为新消息追加 → 回到 ①
        messages.append({"role": "user", "content": results})


# ── 入口 ──
if __name__ == "__main__":
    print("=" * 50)
    print("  s01: Agent Loop — 最小可运行 Agent")
    print("=" * 50)
    print()
    print("  工作原理：")
    print("    你输入问题 → LLM 思考 → 调工具 → 执行 → 结果返回")
    print("    → LLM 继续思考 → ... → 最终回答")
    print()
    print("  试试这些 prompt：")
    print("    1. 列出当前目录下所有文件")
    print("    2. 创建一个 hello.py 文件，内容是打印 Hello World")
    print("    3. 然后运行它")
    print()
    print("  输入 q / exit / 空行 退出")
    print()

    history = []  # 对话历史

    while True:
        try:
            query = input("\033[36ms01 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break

        # 把用户输入加入历史
        history.append({"role": "user", "content": query})

        # 启动 Agent Loop
        agent_loop(history)

        # 打印模型的最终文本回答
        last_msg = history[-1]["content"]
        if isinstance(last_msg, list):
            for block in last_msg:
                if getattr(block, "type", None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")

        print()
