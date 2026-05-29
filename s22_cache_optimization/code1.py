#!/usr/bin/env python3
"""
s22_cache_optimization/code.py — Prompt Caching：让 API 少算 90%

每次 agent loop 调用 LLM，messages 列表里只有最后 1-2 条是新的。
前面的 system prompt + tools + 历史消息和上一轮完全一样，
但默认每次都要从头算 attention —— 这是巨大的浪费。

DeepSeek（兼容 Anthropic API）支持 prompt caching。
你在消息中标记"缓存断点"，服务端自动复用之前的结果。

─────────────────────────────────────────────────────────
核心概念：

① 缓存是怎么工作的
   - 你在 content block 里加 cache_control 标记
   - 服务端把标记之前的内容缓存（至少 1024 token 才能命中）
   - 下次调用如果前缀相同，直接复用，只算新增部分

② 缓存放哪里 — Agent 循环的最佳位置
   [system prompt] ← cache_control 放这里 → [system] 不变，永远命中
   [tools defs]    ← cache_control 放这里 → [tools] 不变，永远命中
   [msg1] [msg2] [msg3]                   → 旧消息
   [msg4] [msg5]                           → 最近的消息（可能变）
   ← 缓存断点放在 msg3 后面

③ 成本影响
   - 缓存命中: token 价格 = 原价 × 10%（写入时原价）
   - 一个 100K token 的上下文，如果 95K 命中缓存
     → 费用从 100K×单价 降到 (5K×单价 + 95K×0.1×单价) ≈ 14.5K×单价
     → 省 85%

④ 你需要做的
   不是实现缓存，而是"配合"缓存 —— 在正确的位置标记断点

运行: cd s22_cache_optimization && ..\.venv\Scripts\python.exe code.py
"""
import json, time

# ═══════════════════════════════════════════════════════════
#  ★ 概念 1/3: 缓存断点标记
# ═══════════════════════════════════════════════════════════

def with_cache_control(block: dict) -> dict:
    """给 content block 加上缓存断点标记。
    服务端会缓存这个 block 及之前的所有内容。

    DeepSeek 兼容 Anthropic 的 cache_control 格式。
    """
    return {**block, "cache_control": {"type": "ephemeral"}}


# ═══════════════════════════════════════════════════════════
#  ★ 概念 2/3: Agent 循环中的缓存策略
# ═══════════════════════════════════════════════════════════

def build_messages_with_cache(
    system_prompt: str,
    tools: list[dict],
    history: list[dict],
    cache_recent: int = 2,
) -> dict:
    """
    构建带缓存标记的 API 请求参数。

    缓存策略:
    - system prompt:    在末尾 block 加 cache_control → 每次都命中
    - tools:            在最后一个 tool 定义加 cache_control → 每次都命中
    - history:          最后 cache_recent 条不缓存（它们可能变）
                       其余的在第 N-cache_recent 条处加断点

    返回: {"system": ..., "messages": ..., "tools": ...}
    """

    # ── System prompt ──
    # system 是一个字符串列表。加在最后一个 block。
    system_blocks = [{"type": "text", "text": system_prompt}]
    system_blocks[-1] = with_cache_control(system_blocks[-1])

    # ── Tools ──
    # 工具定义作为 system prompt 的一部分发送。
    # 加在最后一个 tool。
    tools_text = json.dumps(tools, ensure_ascii=False)
    system_blocks.append({"type": "text", "text": f"<tools>{tools_text}</tools>"})
    system_blocks[-1] = with_cache_control(system_blocks[-1])

    # ── Messages ──
    messages = []
    total = len(history)

    for i, msg in enumerate(history):
        content = msg.get("content", "")

        # 最后 cache_recent 条不标记缓存（它们最可能变）
        should_cache = (i < total - cache_recent)

        if should_cache and i == total - cache_recent - 1:
            # ★ 缓存断点: 倒数第 cache_recent+1 条
            # 前面所有消息 + 这条 → 缓存
            # 后面 cache_recent 条 → 每次重新计算
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
                content[-1] = with_cache_control(content[-1])
                messages.append({"role": msg["role"], "content": content})
            elif isinstance(content, list):
                content = [dict(b) for b in content]
                if content:
                    content[-1] = with_cache_control(content[-1])
                messages.append({"role": msg["role"], "content": content})
        else:
            messages.append(msg)

    return {
        "system": system_blocks,
        "messages": messages,
        "tools": tools,
    }


# ═══════════════════════════════════════════════════════════
# ★ 概念 3/3: 成本模拟 + 对比
# ═══════════════════════════════════════════════════════════

def simulate_cost(history_len: int, turns: int):
    """模拟 N 轮对话的缓存命中率和成本"""
    print(f"  模拟: {history_len} 条历史 + {turns} 轮对话")
    print()

    # 假设: system+tools = 2000 tokens, 每条消息平均 500 tokens
    SYSTEM_TOKENS = 2000
    MSG_TOKENS = 500

    total_input = 0
    cached_tokens = 0

    for turn in range(1, turns + 1):
        msg_count = history_len + turn
        input_tokens = SYSTEM_TOKENS + msg_count * MSG_TOKENS

        # 缓存命中: 最后 2 条之前的全部（system+tools + 历史末2条之前）
        if msg_count > 2:
            cached = SYSTEM_TOKENS + (msg_count - 2) * MSG_TOKENS
        else:
            cached = 0

        total_input += input_tokens
        cached_tokens += cached

        print(f"    Turn {turn:2d}: {input_tokens:>6,} tokens in, "
              f"{cached:>6,} cached ({cached/input_tokens*100:3.0f}%)")

    # 费用计算 (假设 1M tokens = $1)
    PRICE_PER_M = 1.0
    CACHE_PRICE_PER_M = 0.1  # 缓存 token 10% 价格

    no_cache_cost = total_input / 1_000_000 * PRICE_PER_M
    with_cache_cost = (
        (total_input - cached_tokens) / 1_000_000 * PRICE_PER_M
        + cached_tokens / 1_000_000 * CACHE_PRICE_PER_M
    )

    print()
    print(f"  无缓存:  {no_cache_cost*100:.1f} 美分")
    print(f"  有缓存:  {with_cache_cost*100:.1f} 美分")
    print(f"  节省:    {(no_cache_cost - with_cache_cost)*100:.1f} 美分 "
          f"({(1-with_cache_cost/no_cache_cost)*100:.0f}%)")


# ═══════════════════════════════════════════════════════════
# Demo
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  s22: Prompt Caching — 缓存优化")
    print("=" * 55)
    print()

    # ── 演示 1: 缓存断点标记 ──
    print("─ 1. 缓存断点如何标记 ─")
    print()

    block = {"type": "text", "text": "我是要被缓存的内容"}
    cached = with_cache_control(block)
    print("  普通 block:")
    print(f"    {json.dumps(block, ensure_ascii=False)}")
    print("  带缓存标记:")
    print(f"    {json.dumps(cached, ensure_ascii=False)}")
    print("  → cache_control: ephemeral 告诉服务端缓存之前的全部")
    print()

    # ── 演示 2: 消息结构 ──
    print("─ 2. Agent 循环中的缓存位置 ─")
    print()

    system = "你是编码助手。用中文回答。"
    tools = [
        {"name": "read_file", "description": "读文件", "input_schema": {"type": "object"}},
        {"name": "write_file", "description": "写文件", "input_schema": {"type": "object"}},
    ]
    history = [
        {"role": "user", "content": "帮我写个 server.py"},
        {"role": "assistant", "content": "好的。我来创建。"},
        {"role": "user", "content": "再加一个路由"},
    ]

    result = build_messages_with_cache(system, tools, history, cache_recent=2)

    print("  System blocks:")
    for i, b in enumerate(result["system"]):
        cc = " [CACHE BREAK]" if "cache_control" in b else ""
        print(f"    [{i}] {b['type']}: {b.get('text','')[:40]}...{cc}")

    print(f"\n  Messages ({len(result['messages'])} 条):")
    for i, msg in enumerate(result["messages"]):
        content = msg["content"]
        cc = ""
        if isinstance(content, list) and content:
            cc = " [CACHE BREAK]" if "cache_control" in content[-1] else ""
        elif isinstance(content, str):
            cc = ""
        print(f"    [{i}] {msg['role']}: ...{cc}")

    print("\n  断点位置:")
    print("    system prompt 末尾    → system 永远命中")
    print("    tools 末尾            → tools 永远命中")
    print("    历史倒数第3条         → 前面全部命中")

    print()
    print("─ 3. 成本模拟 ─")
    print()
    simulate_cost(history_len=10, turns=20)

    print()
    print("─ 4. 什么时候需要关心缓存？ ─")
    print()
    print("  不需要:  偶尔用、上下文短（<5K tokens）、延迟不敏感")
    print("  需要:    Agent 长时间运行、上下文大（>50K）、高频调用")
    print("           → 缓存可以让延迟降 50-80%，费用降 80-90%")
    print()
    print("  你需要做的:")
    print("    不是实现缓存（服务端做），而是配合缓存")
    print("    在正确的位置标记 cache_control")
    print()
    print("  DeepSeek 缓存限制:")
    print("    最少 1024 tokens 才能命中")
    print("    最多 4 个缓存断点")
    print("    缓存 TTL: 5 分钟（不活跃后失效）")
    print()
    print("=" * 55)

