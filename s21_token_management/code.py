#!/usr/bin/env python3
"""
s21_token_management/code.py — Token Management

当前代码的问题：
  TRB = 5000     ← 5000字符 = 多少token？英文~1250，中文~2500，差两倍
  HL = 30        ← 30条消息 = 多少token？1000还是100000？不知道
  messages[-20:] ← 同上
  content[:5000] ← 粗暴截断

s21 用 token 估算替代字符数统计，用预算管理替代消息条数截断。
"""
import re, json, time

def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。中文 ~2 chars/token，英文 ~4 chars/token。"""
    if not text: return 0
    chinese = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    total = len(text)
    non_chinese = total - chinese
    est = (chinese / 2.0 + non_chinese / 4.0) * 1.1
    return max(1, int(est))

def count_message_tokens(msg: dict) -> int:
    """计算单条消息的 token 数"""
    t = 4
    content = msg.get("content", "")
    if isinstance(content, str):
        t += estimate_tokens(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                tp = block.get("type", "")
                if tp == "text":
                    t += estimate_tokens(block.get("text", ""))
                elif tp == "tool_use":
                    t += estimate_tokens(block.get("name", ""))
                    t += estimate_tokens(json.dumps(block.get("input", {}), ensure_ascii=False))
                    t += 8
                elif tp == "tool_result":
                    c = block.get("content", "")
                    t += estimate_tokens(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
                    t += 4
    return t

def count_messages_tokens(messages: list) -> int:
    return sum(count_message_tokens(m) for m in messages)

def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """截断文本到约 max_tokens，在换行处自然截断"""
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    ratio = max_tokens / estimate_tokens(text)
    target = int(len(text) * ratio * 0.9)
    if target <= 0:
        target = min(len(text), max_tokens * 2)
    truncated = text[:target]
    last_nl = truncated.rfind("\n")
    if last_nl > target * 0.5:
        truncated = truncated[:last_nl]
    return truncated

class ContextBudget:
    """上下文 token 预算管理器"""
    def __init__(self, total=128_000, output_reserve=8_000):
        self.total = total
        self.output_reserve = output_reserve
        self.system_tokens = 0

    @property
    def available(self) -> int:
        return self.total - self.output_reserve - self.system_tokens

    def set_system(self, text: str):
        self.system_tokens = estimate_tokens(text)

    def report(self, messages: list) -> str:
        used = count_messages_tokens(messages)
        pct = used / self.available * 100 if self.available > 0 else 0
        return f"Token: {used:,}/{self.available:,} ({pct:.0f}%)"

def fit_messages(messages: list, budget: ContextBudget, tail_keep: int = 10) -> list:
    """将 messages 适配到 token 预算内。优先保留尾部，裁剪旧内容的 tool_result。"""
    if not messages or count_messages_tokens(messages) <= budget.available:
        return messages

    head_count = max(0, len(messages) - tail_keep)

    for i in range(head_count):
        msg = messages[i]
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, str) and estimate_tokens(text) > 500:
                        block["content"] = truncate_to_tokens(text, 200) + " [cut]"
        if count_messages_tokens(messages) <= budget.available:
            return messages

    for i in range(head_count):
        msg = messages[i]
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            msg["content"] = [
                {"type": "tool_result", "tool_use_id": b.get("tool_use_id", ""),
                 "content": "[cut: old result]"}
                if b.get("type") == "tool_result" else b
                for b in msg["content"]
            ]
        if count_messages_tokens(messages) <= budget.available:
            return messages

    while count_messages_tokens(messages) > budget.available and len(messages) > tail_keep + 2:
        del messages[2]

    return messages


if __name__ == "__main__":
    print("=" * 55)
    print("  s21: Token Management")
    print("=" * 55)
    print()

    print("--- Demo 1: chars vs tokens ---")
    for label, text in [
        ("English", "The quick brown fox jumps. " * 20),
        ("Chinese", "敏捷的棕色狐狸跳过了。 " * 20),
        ("Code",    "def hello(): return 42\n" * 20),
    ]:
        chars = len(text)
        tokens = estimate_tokens(text)
        print(f"  {label}: {chars:,} chars ~ {tokens:,} tokens (ratio {chars/tokens:.1f}:1)")
    print("  -> Same chars, Chinese uses ~2x tokens of English")
    print()

    print("--- Demo 2: ContextBudget ---")
    budget = ContextBudget(total=128_000, output_reserve=8_000)
    budget.set_system("You are a coding agent. Use Chinese.")
    msgs = [
        {"role": "user", "content": "Write a web server"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "OK, creating server.py"},
            {"type": "tool_use", "name": "write_file", "id": "1",
             "input": {"path": "server.py", "content": "print('hello')"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "1",
             "content": "Wrote server.py"}
        ]},
    ]
    print(f"  {budget.report(msgs)}")
    print()

    print("--- Demo 3: fit_messages with 50 fake rounds ---")
    many = []
    for i in range(50):
        many.append({"role": "user", "content": f"Q{i+1}: question {i+1}"})
        many.append({"role": "assistant", "content": [
            {"type": "text", "text": f"A{i+1}: answer {i+1}."},
            {"type": "tool_use", "name": "read_file", "id": f"r{i}",
             "input": {"path": f"file_{i}.py"}},
        ]})
        many.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"r{i}",
             "content": f"file_{i}.py contents:\n" + "x" * 500}
        ]})

    print(f"  Original: {len(many)} msgs, {count_messages_tokens(many):,} tokens")
    print(f"  Budget:   {budget.available:,} available")

    old = many[-30:]
    old_t = count_messages_tokens(old)
    print(f"  Old [-30:]:   {len(old)} msgs, {old_t:,} tokens ({"OVER" if old_t > budget.available else "ok"})")

    fit = fit_messages(many, budget, tail_keep=10)
    fit_t = count_messages_tokens(fit)
    print(f"  New fit:      {len(fit)} msgs, {fit_t:,} tokens ({"OVER" if fit_t > budget.available else "ok"})")
    print(f"  Retention:    {len(fit)}/{len(many)} msgs")
    print()

    print("--- Demo 4: truncate_to_tokens ---")
    long = "第一段内容：这是一段较长的文本。" * 30
    cut = truncate_to_tokens(long, 50)
    print(f"  Original: {len(long)} chars ~ {estimate_tokens(long)} tokens")
    print(f"  Cut:      {len(cut)} chars ~ {estimate_tokens(cut)} tokens")
    print(f"  Tail: ...{cut[-40:]}")
    print()
    print("=" * 55)
    print("  Summary:")
    print("    Old: chars + msg count -> magic numbers")
    print("    New: tokens + budget -> reliable across languages")
    print("=" * 55)
