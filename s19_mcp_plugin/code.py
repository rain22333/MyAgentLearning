#!/usr/bin/env python3
"""
s19_mcp_plugin/code.py — MCP Tools：外接工具，标准协议

s01 到 s18，Agent 的所有工具都是手写的——bash、read、write、task、worktree。
每个工具的输入验证、执行逻辑、错误处理，都是你一行行写的。

现在你有 3 个外部服务想接入：Jira API、部署系统、Notion 知识库。
你不想为每个服务重写一整套工具代码。

需要一个标准协议——外部服务只要实现它，Agent 就能直接调用，
不管服务用什么语言写的。

MCP（Model Context Protocol）就是这个标准。

─────────────────────────────────────────────────────────
s19 新增四个核心机制：

① MCPClient — 封装外部服务的发现 + 调用
② tools/list + tools/call — MCP 的两个核心方法
③ connect_mcp — 连接外部服务，发现它的工具
④ assemble_tool_pool — 内置工具 + MCP 工具 → 统一工具池
   mcp__server__tool 命名规范防止名称冲突

类比：USB 协议——任何设备只要支持 USB，电脑就能用。
      MCP 就是工具的 USB 协议。

运行：cd s19_mcp_plugin && ..\.venv\Scripts\python.exe code.py
"""
import os, sys, json, time, random, threading, subprocess, re
from pathlib import Path
from dataclasses import dataclass, field, asdict

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
MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


# ═══════════════════════════════════════════════════════════
#  ★【s19 新概念 1/4】MCPClient — 外部服务的客户端
# ═══════════════════════════════════════════════════════════
#
# MCP 定义了两个核心方法：
#   tools/list  → 发现这个服务提供了哪些工具
#   tools/call  → 调用某个工具
#
# 教学版用 Python dict 模拟，真实版通过 stdio JSON-RPC 与子进程通信。

class MCPClient:
    """MCP 客户端：封装一个外部服务。

    真实 MCP 中这是通过 stdio/SSE/HTTP 与子进程通信的。
    教学版直接用 Python 函数模拟 server 的工具实现。
    """

    def __init__(self, name: str):
        self.name = name
        self._tools: list[dict] = []      # tools/list 的结果
        self._handlers: dict = {}          # 工具名 → 处理函数

    def register(self, tool_defs: list[dict], handlers: dict):
        """模拟 tools/list 发现过程。
        真实版：发送 JSON-RPC tools/list 请求，server 返回工具列表。
        """
        self._tools = tool_defs
        self._handlers = handlers

    def get_tools(self) -> list[dict]:
        """获取此 server 提供的所有工具（类似 tools/list）"""
        return self._tools

    def call_tool(self, tool_name: str, args: dict) -> str:
        """调用某个工具（类似 tools/call）。
        真实版：发送 JSON-RPC tools/call 请求，server 执行并返回结果。
        """
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP 错误: '{self.name}' 没有工具 '{tool_name}'"
        try:
            return handler(**args)
        except Exception as e:
            return f"MCP 错误: {e}"


# ═══════════════════════════════════════════════════════════
#  ★【s19 新概念 2/4】Mock Servers — 模拟外部服务
# ═══════════════════════════════════════════════════════════
#
# 真实场景中，这些是外部进程或 HTTP 服务。
# 教学版直接用 Python 函数模拟。

def _create_jira_server() -> MCPClient:
    """模拟 Jira 服务"""
    mcp = MCPClient("jira")
    tool_defs = [
        {"name": "search_issues",
         "description": "搜索 Jira issues（readOnly）",
         "input_schema": {
             "type": "object",
             "properties": {
                 "query": {"type": "string", "description": "搜索关键词"},
                 "project": {"type": "string", "description": "项目名，可选"}
             },
             "required": ["query"]
         }},
        {"name": "create_issue",
         "description": "创建 Jira issue（destructive）",
         "input_schema": {
             "type": "object",
             "properties": {
                 "title": {"type": "string"},
                 "description": {"type": "string"},
                 "priority": {"type": "string", "enum": ["low", "medium", "high"]}
             },
             "required": ["title", "description"]
         }},
    ]
    # 模拟数据
    _fake_db = [
        {"id": "PROJ-1", "title": "修复登录页面样式", "status": "进行中", "priority": "high"},
        {"id": "PROJ-2", "title": "添加用户导出功能", "status": "待办", "priority": "medium"},
        {"id": "PROJ-3", "title": "性能优化：首页加载速度", "status": "进行中", "priority": "low"},
    ]

    def search_issues(query, project=None):
        results = [i for i in _fake_db if query.lower() in i["title"].lower()]
        if project:
            results = [i for i in results if i.get("project") == project]
        if not results:
            return f"未找到匹配 '{query}' 的 issues。"
        return "\n".join(f"  {i['id']}: {i['title']} [{i['status']}] ({i['priority']})" for i in results)

    def create_issue(title, description, priority="medium"):
        new_id = f"PROJ-{len(_fake_db) + 1}"
        _fake_db.append({"id": new_id, "title": title, "status": "待办", "priority": priority})
        return f"已创建 {new_id}: {title}"

    mcp.register(tool_defs, {"search_issues": search_issues, "create_issue": create_issue})
    return mcp


def _create_deploy_server() -> MCPClient:
    """模拟部署系统"""
    mcp = MCPClient("deploy-bot")
    tool_defs = [
        {"name": "deploy_status",
         "description": "查看部署状态（readOnly）",
         "input_schema": {
             "type": "object",
             "properties": {"env": {"type": "string", "enum": ["staging", "production"]}},
             "required": ["env"]
         }},
        {"name": "trigger_deploy",
         "description": "触发部署（destructive）",
         "input_schema": {
             "type": "object",
             "properties": {
                 "env": {"type": "string", "enum": ["staging", "production"]},
                 "version": {"type": "string"}
             },
             "required": ["env", "version"]
         }},
        {"name": "tail_logs",
         "description": "查看最近日志（readOnly）",
         "input_schema": {
             "type": "object",
             "properties": {
                 "env": {"type": "string"},
                 "lines": {"type": "integer", "default": 20}
             },
             "required": ["env"]
         }},
    ]

    def deploy_status(env):
        return f"  {env}: ● 运行中\n  最后部署: 2026-05-27 09:00\n  版本: v2.4.1\n  实例数: 3"

    def trigger_deploy(env, version):
        print(f"  \033[33m[MCP:deploy] 正在部署 {version} 到 {env}...\033[0m")
        return f"部署已触发: {version} → {env}（预计 3 分钟完成）"

    def tail_logs(env, lines=20):
        logs = [
            "[09:00:01] INFO  Request GET /api/users 200 45ms",
            "[09:00:02] INFO  Request POST /api/auth 201 120ms",
            "[09:00:03] WARN  Rate limit approaching: 850/1000",
            "[09:00:04] INFO  Cache hit ratio: 94.2%",
            f"[09:00:05] INFO  Health check: {env} OK",
        ]
        return "\n".join(logs)

    mcp.register(tool_defs, {"deploy_status": deploy_status,
                              "trigger_deploy": trigger_deploy,
                              "tail_logs": tail_logs})
    return mcp


def _create_notion_server() -> MCPClient:
    """模拟 Notion 知识库服务"""
    mcp = MCPClient("notion-kb")
    tool_defs = [
        {"name": "search_docs",
         "description": "搜索知识库文档（readOnly）",
         "input_schema": {
             "type": "object",
             "properties": {"query": {"type": "string"}},
             "required": ["query"]
         }},
        {"name": "create_page",
         "description": "创建知识库页面（destructive）",
         "input_schema": {
             "type": "object",
             "properties": {
                 "title": {"type": "string"},
                 "content": {"type": "string"},
                 "parent_page": {"type": "string"}
             },
             "required": ["title", "content"]
         }},
    ]

    _kb = [
        {"title": "新人入职指南", "content": "步骤：1. 安装开发环境 2. 克隆代码 3. 运行测试"},
        {"title": "部署流程", "content": "staging: git push → CI自动部署。production: 需要审批 + 发布窗口"},
        {"title": "API 设计规范", "content": "RESTful, 版本号在 URL, 错误码用 RFC 7807"},
    ]

    def search_docs(query):
        results = [d for d in _kb if query.lower() in d["title"].lower() or query.lower() in d["content"].lower()]
        if not results:
            return f"未找到相关文档: '{query}'"
        return "\n---\n".join(f"**{d['title']}**\n{d['content']}" for d in results)

    def create_page(title, content, parent_page=""):
        _kb.append({"title": title, "content": content})
        parent_info = f"（子页面: {parent_page}）" if parent_page else ""
        return f"页面已创建: {title} {parent_info}"

    mcp.register(tool_defs, {"search_docs": search_docs, "create_page": create_page})
    return mcp


# 可用的模拟服务
MOCK_SERVERS = {
    "jira": _create_jira_server,
    "deploy-bot": _create_deploy_server,
    "notion-kb": _create_notion_server,
}

# 已连接的 MCP 客户端
mcp_clients: dict[str, MCPClient] = {}


# ═══════════════════════════════════════════════════════════
#  ★【s19 新概念 3/4】connect_mcp — 连接外部服务
# ═══════════════════════════════════════════════════════════
#
# 连接 → 获取 tools/list → 注册到工具池。
# 真实版：启动子进程，通过 stdio JSON-RPC 握手。

def connect_mcp(server_name: str) -> str:
    """连接到 MCP 服务器，发现它的工具。"""
    if server_name in mcp_clients:
        return f"MCP 服务 '{server_name}' 已连接"

    factory = MOCK_SERVERS.get(server_name)
    if not factory:
        names = ", ".join(MOCK_SERVERS.keys())
        return f"未知服务 '{server_name}'。可用: {names}"

    mcp = factory()
    mcp_clients[server_name] = mcp

    tool_names = [t["name"] for t in mcp.get_tools()]
    print(f"  \033[36m[MCP] 已连接到 '{server_name}'，发现 {len(tool_names)} 个工具\033[0m")
    return (f"已连接 '{server_name}'。可用工具: "
            f"{', '.join(f'mcp__{server_name}__{n}' for n in tool_names)}")


# ═══════════════════════════════════════════════════════════
#  ★【s19 新概念 4/4】assemble_tool_pool — 组装统一工具池
# ═══════════════════════════════════════════════════════════
#
# 把内置工具和 MCP 工具合并成一个池子。
# MCP 工具命名规范: mcp__<server>__<tool>
#
# 为什么加前缀？不同 server 可能有同名工具（如都有 "search"）
# 前缀避免冲突。

# 名称规范化：只允许 [a-zA-Z0-9_-]
_NAME_CLEAN = re.compile(r'[^a-zA-Z0-9_-]')

def normalize_mcp_name(name: str) -> str:
    """规范化名称，替换特殊字符为 _"""
    return _NAME_CLEAN.sub('_', name)


def assemble_tool_pool() -> tuple[list[dict], dict]:
    """组装统一工具池：内置工具 + 所有 MCP 工具。

    返回: (工具定义列表, 处理器字典)
    """
    tools = list(BASE_TOOLS)    # 内置工具
    handlers = dict(BASE_HANDLERS)

    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.get_tools():
            safe_tool = normalize_mcp_name(tool_def["name"])
            prefixed = f"mcp__{safe_server}__{safe_tool}"

            # 复制工具定义，加上前缀
            new_def = tool_def.copy()
            new_def["name"] = prefixed
            new_def["description"] = f"[MCP:{server_name}] {tool_def.get('description', '')}"

            tools.append(new_def)

            # 创建闭包，捕获当前的 mcp_client 和 tool_name
            def make_handler(c=mcp_client, t=tool_def["name"]):
                def handler(**kwargs):
                    print(f"  \033[35m[MCP] {c.name}:{t}()\033[0m")
                    return c.call_tool(t, kwargs)
                return handler

            handlers[prefixed] = make_handler()

    return tools, handlers


# ═══════════════════════════════════════════════════════════
#  Agent 循环（聚焦 MCP 概念）
# ═══════════════════════════════════════════════════════════

register_default_hooks()

SYSTEM = (
    f"你是 {WORKDIR} 下的 Lead Agent。用中文回答。\n"
    "你可以通过 connect_mcp 连接外部服务，然后使用它们的工具。\n"
    "可用服务: jira（项目管理）、deploy-bot（部署）、notion-kb（知识库）\n"
    "连接后工具名格式: mcp__<服务名>__<工具名>\n"
)


def agent_loop(messages: list, context: dict) -> dict:
    """Agent 循环。★ 每轮动态组装工具池（含 MCP 工具）。"""

    # ★ s19 关键：每轮重新组装工具池
    # 因为 connect_mcp 可能在上一轮被调用，新增了 MCP 工具
    TOOLS, TOOL_HANDLERS = assemble_tool_pool()

    # 添加 connect_mcp / disconnect_mcp（管理工具）
    extra_tools = [
        {"name": "connect_mcp",
         "description": "连接 MCP 外部服务。可用: jira, deploy-bot, notion-kb",
         "input_schema": {"type": "object",
                          "properties": {"server_name": {"type": "string"}},
                          "required": ["server_name"]}},
        {"name": "list_mcp_connections",
         "description": "列出所有已连接的 MCP 服务及其工具。",
         "input_schema": {"type": "object", "properties": {}, "required": []}},
    ]
    all_tools = TOOLS + extra_tools
    all_handlers = {
        **TOOL_HANDLERS,
        "connect_mcp": lambda server_name: connect_mcp(server_name),
        "list_mcp_connections": list_connections,
    }

    while True:
        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM, messages=messages,
                tools=all_tools, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[错误] {e}"}]})
            return context

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": str(force)})
                continue
            return context

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m>>> [{block.name}]\033[0m")
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue
            h = all_handlers.get(block.name)
            output = h(**block.input) if h else f"未知工具: {block.name}"
            print(str(output)[:400])
            trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})
        messages.append({"role": "user", "content": results})
        return context


def list_connections() -> str:
    if not mcp_clients:
        return "没有已连接的 MCP 服务。用 connect_mcp 连接。"
    lines = []
    for name, mcp in mcp_clients.items():
        tools = [f"mcp__{name}__{t['name']}" for t in mcp.get_tools()]
        lines.append(f"  {name}: {', '.join(tools)}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  s19: MCP Plugin — 外接工具，标准协议")
    print("=" * 55)
    print()
    print("  核心概念:")
    print("    MCPClient:    封装外部服务的发现 + 调用")
    print("    tools/list:   发现这个服务提供了哪些工具")
    print("    tools/call:   调用某个工具")
    print("    connect_mcp:  连接外部服务 → 工具加入池")
    print()
    print("  命名规范:  mcp__<服务名>__<工具名>")
    print("    例如: mcp__jira__search_issues")
    print()
    print("  可用服务: jira, deploy-bot, notion-kb")
    print()
    print("  试试:")
    print("    1. connect_mcp jira")
    print("    2. 问: 搜索和'登录'相关的 issues")
    print("    3. connect_mcp deploy-bot → 查部署状态")
    print("    4. connect_mcp notion-kb → 查部署文档")
    print("    5. list_mcp_connections 查看所有连接")
    print()
    print("  输入 q / exit / 空行 退出\n")

    history = []
    context = {"workspace": str(WORKDIR)}

    while True:
        try:
            query = input("\033[36ms19 >>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            print("再见！")
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)

        last = history[-1]
        if isinstance(last.get("content"), list):
            for block in last["content"]:
                if getattr(block, "type", None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")
        print()
