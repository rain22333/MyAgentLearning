#!/usr/bin/env python3
"""
s11_error_recovery/code.py — 错误恢复：错误不是结束，是重试的开始

s10 的问题：Agent 碰到 API 错误（429限流、529过载、输出截断）直接崩溃。
生产环境中 API 错误是常态，不是 bug。

s11 的解法：LLM 调用包在 try/except 里，根据错误类型走不同的恢复路径。

  三条恢复路径：
  
  路径1（输出截断）: max_tokens 用完 → 先升级8K→64K重试（不追加截断输出）
                    → 64K还不够 → 续写提示（最多3次）
  
  路径2（上下文超限）: prompt_too_long → 应急压缩 → 重试（1次）
  
  路径3（临时故障）: 429/529 → 指数退避+抖动 → 重试（最多10次）
                    → 连续3次529 → 切换到备用模型

工具实现从 common/ 导入，本章聚焦 try/except + 分类恢复。
"""
import os, sys, time, random, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.tools import BASE_TOOLS, BASE_HANDLERS
from common.hooks import register_default_hooks, trigger_hooks

try:
    import readline
    readline.parse_and_bind("set bind-tty-special-chars off")
except ImportError:
    pass

from anthropic import Anthropic, APIStatusError
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"), override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
PRIMARY_MODEL = os.environ["MODEL_ID"]
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# ═══════════════════════════════════════════════════════════
#  ★【s11 新概念】常量 + 恢复状态追踪
# ═══════════════════════════════════════════════════════════

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 64000
MAX_RECOVERY_RETRIES = 3     # 续写最多 3 次
MAX_RETRIES = 10             # 退避重试最多 10 次
BASE_DELAY_MS = 500          # 退避基数
MAX_CONSECUTIVE_529 = 3      # 连续 529 后切换模型

CONTINUATION_PROMPT = (
    "输出 token 已达上限。请直接从刚才中断的地方继续——不要道歉，不要复述。"
    "把剩余工作拆成更小的部分。"
)


class RecoveryState:
    """追踪本轮对话中的所有恢复状态"""
    def __init__(self):
        self.has_escalated = False              # 是否已升级过 token
        self.recovery_count = 0                  # 续写次数
        self.has_attempted_reactive_compact = False  # 是否已应急压缩
        self.consecutive_529 = 0                 # 连续 529 计数
        self.current_model = PRIMARY_MODEL       # 当前使用的模型


# ═══════════════════════════════════════════════════════════
#  ★ 路径3：指数退避 + 抖动
# ═══════════════════════════════════════════════════════════

def with_retry(fn, state: RecoveryState, max_retries: int = MAX_RETRIES):
    """
    临时故障（429/529）的指数退避重试。
    
    退避公式：delay = min(500 × 2^(attempt-1), 32000) + random(0~25%)
    
    try 1: 500ms    + 0-125ms
    try 2: 1000ms   + 0-250ms
    try 4: 4000ms   + 0-1000ms
    try 7+: 32000ms + 0-8000ms  (上限)
    """
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except APIStatusError as e:
            if e.status_code == 429:
                # 限流：退避重试
                delay = min(BASE_DELAY_MS * (2 ** (attempt - 1)), 32000)
                jitter = random.uniform(0, delay * 0.25)
                total_delay = (delay + jitter) / 1000
                print(f"  ⏳ [429] 限流，第{attempt}次重试，等待 {total_delay:.1f}s...")
                time.sleep(total_delay)

            elif e.status_code == 529:
                # 过载：退避重试，连续多次则切换模型
                state.consecutive_529 += 1
                if FALLBACK_MODEL and state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    print(f"  🔄 [529] 连续{state.consecutive_529}次过载，切换到备用模型: {FALLBACK_MODEL}")
                    state.current_model = FALLBACK_MODEL
                    state.consecutive_529 = 0
                delay = min(BASE_DELAY_MS * (2 ** (attempt - 1)), 32000)
                jitter = random.uniform(0, delay * 0.25)
                total_delay = (delay + jitter) / 1000
                print(f"  ⏳ [529] 过载，第{attempt}次重试，等待 {total_delay:.1f}s...")
                time.sleep(total_delay)

            else:
                raise  # 其他 API 错误不重试，直接抛出

    raise RuntimeError(f"退避重试 {max_retries} 次后仍失败")


# ═══════════════════════════════════════════════════════════
#  ★ 路径2 辅助：应急压缩
# ═══════════════════════════════════════════════════════════

def reactive_compact(messages: list) -> list:
    """API 报 prompt_too_long 时，激进裁剪消息"""
    if len(messages) <= 10:
        return messages

    # 保存完整对话
    transcript_dir = WORKDIR / ".transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = transcript_dir / f"reactive_{ts}.json"
    path.write_text(json.dumps(messages, ensure_ascii=False, default=str), encoding="utf-8")

    print(f"  🆘 [reactive compact] {len(messages)}条 → 10条，原文保存到 {path}")
    keep_head = 2
    return messages[:keep_head] + [
        {"role": "user", "content": f"[应急压缩：{len(messages)-10} 条消息已移除]"}
    ] + messages[-8:]


def is_prompt_too_long_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "prompt_too_long" in msg or "too many tokens" in msg


# ═══════════════════════════════════════════════════════════
#  Prompt 组装（s10 继承）
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": "你是编码助手。用中文回答，直接行动。",
    "tools":    "工具有：bash、read_file、write_file、edit_file、glob。",
    "workspace": f"工作目录: {WORKDIR}",
}

_last_context_key = None
_last_prompt = None


def assemble_system_prompt(context: dict) -> str:
    sections = [
        PROMPT_SECTIONS["identity"],
        PROMPT_SECTIONS["tools"],
        PROMPT_SECTIONS["workspace"],
    ]
    return "\n\n".join(sections)


def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  工具和循环
# ═══════════════════════════════════════════════════════════

TOOLS = BASE_TOOLS
TOOL_HANDLERS = BASE_HANDLERS
register_default_hooks()


def agent_loop(messages: list, context: dict):
    system = get_system_prompt(context)
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # ═══════════════════════════════════════════════════
        #  LLM 调用（包在 try/except 里）
        # ═══════════════════════════════════════════════════
        try:
            response = with_retry(
                lambda mt=max_tokens, mdl=state.current_model:
                    client.messages.create(
                        model=mdl, system=system, messages=messages,
                        tools=TOOLS, max_tokens=mt,
                    ),
                state,
            )
        except Exception as e:
            # ── 路径2：上下文超限 ──
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    print("  🆘 [prompt_too_long] 触发应急压缩...")
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                print("  ❌ 压缩后仍超限，无法继续")
                messages.append({"role": "assistant", "content": [
                    {"type": "text", "text": "[错误] 上下文过大，无法继续。"}
                ]})
                return

            # ── 不可恢复的错误 ──
            name = type(e).__name__
            print(f"  ❌ [{name}] {str(e)[:100]}")
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[错误] {name}"}
            ]})
            return

        # ═══════════════════════════════════════════════════
        #  路径1：max_tokens 截断
        # ═══════════════════════════════════════════════════
        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                # 第一次：升级 token 上限，重试同一请求
                # 关键：不追加截断输出到 messages！
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  ⚠️  [max_tokens] 升级 {DEFAULT_MAX_TOKENS}→{ESCALATED_MAX_TOKENS}")
                continue  # messages 不变，重新请求

            # 升级过还是截断 → 保存截断输出 + 续写提示
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"  📝 [max_tokens] 续写 {state.recovery_count}/{MAX_RECOVERY_RETRIES}")
                continue
            print("  ❌ [max_tokens] 续写次数用尽")
            return

        # 正常：追加回复
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": str(force)})
                continue
            return

        # 工具执行
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
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
        system = get_system_prompt(context)


if __name__ == "__main__":
    print("=" * 55)
    print("  s11: Error Recovery — 三条恢复路径")
    print("=" * 55)
    print()
    print("  路径1: max_tokens 截断 → 升级8K→64K → 续写(×3)")
    print("  路径2: prompt_too_long → 应急压缩 → 重试")
    print("  路径3: 429/529 临时故障 → 指数退避+抖动 → 切换模型")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    context = {"workspace": str(WORKDIR)}
    while True:
        try:
            query = input("\033[36ms11 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！"); break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！"); break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        last = history[-1]["content"]
        if isinstance(last, list):
            for b in last:
                if getattr(b, "type", None) == "text":
                    print(f"\n\033[32m{b.text}\033[0m")
        print()
