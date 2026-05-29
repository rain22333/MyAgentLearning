#!/usr/bin/env python3
"""
s20_comprehensive/code.py — Comprehensive Agent：全部机制，归于一个循环

s01～s19 每章只加一个机制，适合学习。
s20 是终点章：把前 19 章的机制全部放回同一个 while True 循环里。

架构全景图：

  用户输入
    ├─[s04] UserPromptSubmit hooks
    ├─[s14] cron queue → [Scheduled] 注入
    ├─[s13] background notifications → <task_notification>
    ├─[s08] context compact
    ├─[s10] system prompt（含 [s07] skills + [s09] memory + [s19] MCP）
    ├─[s11] LLM 调用 + error recovery
    ├─ stop_reason != "tool_use"? → [s04] Stop hooks → 返回
    └─ 工具执行轮:
        ├─[s04] PreToolUse hooks + [s03] permission
        ├─ dispatch: [s02]内置 [s19]MCP [s13]bg [s05]todo [s06]sub [s07]skill [s15]team
        ├─[s04] PostToolUse hooks
        └─ tool_result → 下一轮

运行：cd s20_comprehensive && ..\.venv\Scripts\python.exe code.py
"""
import os, sys, json, time, random, threading, subprocess, re
from pathlib import Path
from dataclasses import dataclass, field, asdict

sys.path.insert(0, str(Path(__file__).parent.parent))
from common.tools import BASE_TOOLS, BASE_HANDLERS
from common.hooks import register_default_hooks, trigger_hooks

try: import readline; readline.parse_and_bind("set bind-tty-special-chars off")
except ImportError: pass

from anthropic import Anthropic
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"), override=True)
if os.getenv("ANTHROPIC_BASE_URL"): os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
MAILBOX_DIR = WORKDIR / ".mailboxes"; MAILBOX_DIR.mkdir(exist_ok=True)
MEMORY_DIR = WORKDIR / ".memory"; MEMORY_DIR.mkdir(exist_ok=True)
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
SKILLS_DIR = WORKDIR / ".skills"; SKILLS_DIR.mkdir(exist_ok=True)
TASKS_DIR = WORKDIR / ".tasks"; TASKS_DIR.mkdir(exist_ok=True)
WORKTREES_DIR = WORKDIR / ".worktrees"; WORKTREES_DIR.mkdir(exist_ok=True)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
_tool_call_counter = [0]

# === [s05] todo_write ===
_todo_items: list[dict] = []
TODO_NAG_EVERY = 3

def run_todo_write(todos):  # noqa
    global _todo_items
    _todo_items = todos; _tool_call_counter[0] = 0
    return "ok\n" + "\n".join(f"  {'Y' if t.get('done') else '-'} {t.get('task','')}" for t in todos)

# === [s06] subagent ===
def spawn_subagent(prompt, tools_whitelist=None):
    allowed = tools_whitelist or ["bash","read_file","write_file"]
    subs = [t for t in BASE_TOOLS if t["name"] in allowed]
    subh = {k:v for k,v in BASE_HANDLERS.items() if k in allowed}
    msgs = [{"role":"user","content":prompt}]
    for _ in range(5):
        try:
            r = client.messages.create(model=MODEL, system="Do, dont explain.", messages=msgs, tools=subs, max_tokens=4000)
        except: break
        msgs.append({"role":"assistant","content":r.content})
        if r.stop_reason != "tool_use": break
        res = []
        for b in r.content:
            if b.type != "tool_use": continue
            h = subh.get(b.name); out = h(**b.input) if h else "?"
            res.append({"type":"tool_result","tool_use_id":b.id,"content":out})
        msgs.append({"role":"user","content":res})
    for m in reversed(msgs):
        if m["role"]=="assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if getattr(b,"type",None)=="text": return f"[sub] {b.text[:500]}"
    return "[sub] no result"

# === [s07] skill_loading ===
_loaded_skills = {}
def _scan_skills():
    c = {}
    for d in SKILLS_DIR.iterdir():
        if d.is_dir() and (d/"SKILL.md").exists():
            l = (d/"SKILL.md").read_text(encoding="utf-8").splitlines()[0]
            c[d.name] = l.strip("#").strip() or d.name
    return c

def run_load_skill(name):
    if name in _loaded_skills: return f"[loaded] {name}"
    sm = SKILLS_DIR / name / "SKILL.md"
    if not sm.exists(): return f"skill {name} not found. avlb: {','.join(_scan_skills())}"
    _loaded_skills[name] = sm.read_text(encoding="utf-8")[:3000]
    return _loaded_skills[name]

# === [s08] context_compact ===
TRB = 5000; HL = 30
def compact_context(msgs):
    for m in msgs:
        if m["role"]=="user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type")=="tool_result" and isinstance(b.get("content"),str) and len(b["content"])>TRB:
                    b["content"] = b["content"][:TRB] + f"\n... ({len(b['content'])-TRB}c truncated)"
    if len(msgs) > HL:
        for m in msgs[:-10]:
            if m["role"]=="user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if b.get("type")=="tool_result" and isinstance(b.get("content"),str) and len(b["content"])>200:
                        b["content"] = f"[cached {b['content'][:100]}...]"
        head = msgs[:5]; tail = msgs[-(HL-5):]
        msgs[:] = head + [{"role":"user","content":f"[... {len(msgs)-HL} msgs omitted ...]"}] + tail
    return msgs

# === [s09] memory ===
def _load_memory():
    return MEMORY_INDEX.read_text(encoding="utf-8").strip() if MEMORY_INDEX.exists() else ""

def run_save_memory(entry):
    e = _load_memory(); ne = f"- {time.strftime('%Y-%m-%d')}: {entry}"
    MEMORY_INDEX.write_text((e+"\n"+ne).strip() if e else ne, encoding="utf-8")
    return f"saved: {entry}"

# === [s10] system_prompt ===
def assemble_system_prompt():
    s = [f"You are coding agent in {WORKDIR}. Use Chinese.",
         f"tools: bash read_file write_file todo_write task load_skill compact "
         f"create_task list_tasks claim_task complete_task schedule_cron "
         f"spawn_teammate send_message check_inbox request_shutdown request_plan review_plan "
         f"create_worktree remove_worktree connect_mcp save_memory",
         f"skills: {','.join(f'{k}' for k in _scan_skills()) or 'none'}"]
    m = _load_memory()
    if m: s.append(f"memory:\n{m}")
    if mcp_clients: s.append(f"MCP: {','.join(mcp_clients)}")
    return "\n\n".join(s)

# === [s11] error_recovery ===
MAX_RETRIES = 3; MAX_TOK = 8000
def call_llm(msgs, sys_p, tools):
    ct = MAX_TOK; retries = 0
    while retries <= MAX_RETRIES:
        try:
            return client.messages.create(model=MODEL, system=sys_p, messages=msgs[-50:], tools=tools, max_tokens=ct)
        except Exception as e:
            es = str(e)
            if "max_tokens" in es.lower() and ct < 32000: ct *= 2; continue
            if "prompt" in es.lower() or "context" in es.lower(): compact_context(msgs); ct = MAX_TOK; continue
            retries += 1
            if retries > MAX_RETRIES: raise
            time.sleep(2 ** retries)
    return None

# === [s12] task_system ===
_tc = [0]
@dataclass
class Task: id:str; subject:str; description:str; status:str; owner:str; blockedBy:list; worktree:str=""
def _tp(id): return TASKS_DIR/f"{id}.json"
def ct(subject, desc="", bb=None):
    _tc[0]+=1; t=Task(id=f"t{_tc[0]:04d}", subject=subject, description=desc, status="pending", owner="", blockedBy=bb or [])
    _tp(t.id).write_text(json.dumps(asdict(t),indent=2,ensure_ascii=False)); return t
def lt(id): p=_tp(id); return Task(**json.loads(p.read_text())) if p.exists() else None
def lts(): return [Task(**json.loads(p.read_text())) for p in sorted(TASKS_DIR.glob("t*.json"))]
def st(t): _tp(t.id).write_text(json.dumps(asdict(t),indent=2,ensure_ascii=False))
def cs(id):
    t=lt(id)
    if not t: return False
    return all((lt(d) and lt(d).status=="completed") for d in t.blockedBy)
def clm(id, owner):
    t=lt(id)
    if not t: return "not found"
    if t.status!="pending": return f"status:{t.status}"
    if t.owner: return f"owned:{t.owner}"
    if not cs(id): return f"blocked:{[d for d in t.blockedBy if not lt(d) or lt(d).status!='completed']}"
    t.owner=owner; t.status="in_progress"; st(t)
    return f"claimed {id} ({t.subject})"
def comp(id):
    t=lt(id)
    if not t: return "not found"
    if t.status!="in_progress": return f"status:{t.status}"
    t.status="completed"; st(t)
    return f"done {id} ({t.subject})"


# === [s13] background + [s14] cron ===
_bg={}; _bgr={}; _bgl=threading.Lock(); _bgc=[0]
_cj={}; _cq=[]; _cl=threading.Lock(); _lf={}

def s_bg(c,h,a):
    _bgc[0]+=1; bid=f"bg{_bgc[0]:04d}"
    def w(): r=h(**a); 
    with _bgl: _bg[bid]="done"; _bgr[bid]=r
    with _bgl: _bg[bid]="running"
    threading.Thread(target=w,daemon=True).start()
    return bid
def c_bg():
    with _bgl: ready=[b for b,s in _bg.items() if s=="done"]
    ns=[]
    for b in ready:
        with _bgl: _bg.pop(b,None); out=_bgr.pop(b,"")
        ns.append(f"<task_notification><id>{b}</id><status>done</status><summary>{str(out)[:200]}</summary></task_notification>")
    return ns

def _cfm(f,v):
    if f=="*": return True
    if "/" in f:
        bs,st=f.split("/"); bs=0 if bs=="*" else int(bs); st=int(st)
        return v>=bs and (v-bs)%st==0
    if "," in f: return any(int(x.strip())==v for x in f.split(","))
    if "-" in f: lo,hi=f.split("-"); return int(lo)<=v<=int(hi)
    return int(f)==v

def cron_matches(expr,now):
    fs=expr.strip().split()
    if len(fs)!=5: return False
    vs=[now.tm_min,now.tm_hour,now.tm_mday,now.tm_mon,(now.tm_wday+1)%7]
    return all(_cfm(f,v) for f,v in zip(fs,vs))

def cron_loop():
    while True:
        time.sleep(1); now=time.localtime()
        mm=f"{now.tm_year}-{now.tm_mon:02d}-{now.tm_mday:02d} {now.tm_hour:02d}:{now.tm_min:02d}"
        with _cl:
            for jid,job in list(_cj.items()):
                try:
                    if cron_matches(job["cron"],now) and _lf.get(jid)!=mm:
                        _cq.append(job); _lf[jid]=mm
                except: pass

threading.Thread(target=cron_loop,daemon=True).start()

# === [s15][s16][s17] MessageBus + protocol + teams ===
class MB:
    def send(self,frm,to,content,mt="message",meta=None):
        msg={"from":frm,"to":to,"content":content,"type":mt,"ts":time.time()}
        if meta: msg["metadata"]=meta
        with open(MAILBOX_DIR/f"{to}.jsonl","a",encoding="utf-8") as f:
            f.write(json.dumps(msg,ensure_ascii=False)+"\n")
    def read_inbox(self,agent):
        ib=MAILBOX_DIR/f"{agent}.jsonl"
        if not ib.exists(): return []
        ms=[json.loads(l) for l in ib.read_text(encoding="utf-8").splitlines() if l.strip()]
        ib.unlink(); return ms

BUS=MB(); a_team={}; p_req={}
@dataclass
class Proto: rid:str; ptype:str; sender:str; target:str; status:str; payload:str
def nr(): return f"r{random.randint(100000,999999)}"

def stt(name,role,prompt):
    if name in a_team: return f"exists"
    sys_p=f"You are {name}, a {role}. Use tools. submit_plan for risky work."
    wtc={"path":None}
    def run():
        msgs=[{"role":"user","content":prompt}]
        st2=[{"name":"bash","input_schema":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}},
             {"name":"read_file","input_schema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
             {"name":"write_file","input_schema":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}},
             {"name":"send_message","input_schema":{"type":"object","properties":{"to":{"type":"string"},"content":{"type":"string"}},"required":["to","content"]}},
             {"name":"submit_plan","input_schema":{"type":"object","properties":{"plan":{"type":"string"}},"required":["plan"]}},
             {"name":"list_tasks","input_schema":{"type":"object","properties":{},"required":[]}},
             {"name":"claim_task","input_schema":{"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]}},
             {"name":"complete_task","input_schema":{"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]}}]
        for _ in range(15):
            ib=BUS.read_inbox(name); sd=False
            for m in ib:
                mt=m.get("type",""); meta=m.get("metadata",{}); rid=meta.get("request_id","")
                if mt=="shutdown_request":
                    BUS.send(name,"lead","ok","shutdown_response",{"request_id":rid,"approve":True}); sd=True; break
                elif mt=="plan_approval_response":
                    msgs.append({"role":"user","content":f"[plan {'ok' if meta.get('approve') else 'rejected'}]"})
                else: msgs.append({"role":"user","content":json.dumps(m,ensure_ascii=False)})
            if sd: break
            try: r=client.messages.create(model=MODEL,system=sys_p,messages=msgs[-20:],tools=st2,max_tokens=4000)
            except: break
            msgs.append({"role":"assistant","content":r.content})
            if r.stop_reason!="tool_use": break
            res=[]
            for b in r.content:
                if b.type!="tool_use": continue
                out=_tt(name,b,wtc); res.append({"type":"tool_result","tool_use_id":b.id,"content":out})
            msgs.append({"role":"user","content":res})
        BUS.send(name,"lead",f"{name} done.","result"); a_team.pop(name,None)
    a_team[name]=True; threading.Thread(target=run,daemon=True).start()
    return f"spawned {name} ({role})"

def _tt(name,block,wtc):
    cwd=wtc.get("path")
    h={"bash":lambda **kw: _tb(cwd,kw),"read_file":lambda **kw: _trd(cwd,kw),
       "write_file":lambda **kw: _twr(cwd,kw),
       "send_message":lambda **kw: (BUS.send(name,kw.get("to","lead"),kw.get("content","")),"sent")[1],
       "submit_plan":lambda **kw: _spl(name,kw.get("plan","")),
       "list_tasks":lambda: _ftl(),"claim_task":lambda **kw: _cwt(kw.get("task_id",""),name,wtc),
       "complete_task":lambda **kw: comp(kw.get("task_id",""))}
    f=h.get(block.name); return f(**block.input) if f else f"?{block.name}"

def _tb(cwd,kw):
    try:
        r=subprocess.run(kw.get("command",""),shell=True,cwd=cwd or WORKDIR,capture_output=True,text=True,timeout=60)
        return (r.stdout+r.stderr).strip()[:5000] or "(no output)"
    except: return "err"
def _trd(cwd,kw):
    try:
        fp=((cwd or WORKDIR)/kw.get("path","")).resolve()
        return fp.read_text(encoding="utf-8")[:3000] if fp.exists() else "not found"
    except: return "err"
def _twr(cwd,kw):
    try:
        fp=((cwd or WORKDIR)/kw.get("path","")).resolve(); fp.parent.mkdir(parents=True,exist_ok=True)
        fp.write_text(kw.get("content",""),encoding="utf-8"); return f"wrote {len(kw.get('content',''))}b"
    except: return "err"
def _spl(frm,plan):
    rid=nr(); p_req[rid]=Proto(rid,"plan_approval",frm,"lead","pending",plan)
    BUS.send(frm,"lead",plan,"plan_approval_request",{"request_id":rid}); return f"plan submitted ({rid})"
def _ftl():
    ts=lts()
    if not ts: return "no tasks"
    ic={"pending":"-","in_progress":">","completed":"+"}
    return "\n".join(f"  {ic.get(t.status,'?')} {t.id}: {t.subject} [{t.status}]"+(" ["+t.owner+"]" if t.owner else "") for t in ts)
def _cwt(tid,owner,wtc):
    r=clm(tid,owner)
    if "claimed" in r:
        t=lt(tid)
        if t and t.worktree: wtc["path"]=str(WORKTREES_DIR/t.worktree)
    return r


# === [s18] worktree ===
VN=re.compile(r"^[A-Za-z0-9._-]{1,64}$")
def cw(name,task_id=""):
    if not VN.match(name): return f"bad name: {name}"
    p=WORKTREES_DIR/name
    if p.exists(): return f"exists: {p}"
    try:
        r=subprocess.run(["git","worktree","add",str(p),"-b",f"wt/{name}","HEAD"],cwd=WORKDIR,capture_output=True,text=True,timeout=30)
        if r.returncode!=0: return f"git err: {r.stderr.strip()}"
    except Exception as e: return f"err: {e}"
    if task_id:
        t=lt(task_id)
        if t: t.worktree=name; st(t)
    return f"created {name} @ {p}"
def lws():
    try:
        r=subprocess.run(["git","worktree","list"],cwd=WORKDIR,capture_output=True,text=True,timeout=10)
        return r.stdout.strip()
    except Exception as e: return str(e)
def rw(name,dc=False):
    p=WORKTREES_DIR/name
    if not p.exists(): return "not found"
    if not dc:
        try:
            r=subprocess.run(["git","-C",str(p),"status","--porcelain"],capture_output=True,text=True,timeout=10)
            if r.stdout.strip(): return "has changes, use discard_changes=true"
        except: pass
    try:
        subprocess.run(["git","worktree","remove",str(p),"--force"],cwd=WORKDIR,capture_output=True,timeout=30)
        subprocess.run(["git","branch","-D",f"wt/{name}"],cwd=WORKDIR,capture_output=True,timeout=10)
    except Exception as e: return f"err: {e}"
    return f"removed {name}"

# === [s19] MCP ===
class MCPC:
    def __init__(self,name): self.name=name; self._t=[]; self._h={}
    def reg(self,td,hd): self._t=td; self._h=hd
    def gt(self): return self._t
    def ct(self,tn,args):
        h=self._h.get(tn); return h(**args) if h else f"MCP err: no {tn}"

def _mk_jira():
    m=MCPC("jira")
    _d=[{"id":"P-1","title":"login bug","status":"in progress"},{"id":"P-2","title":"export","status":"todo"}]
    m.reg([
        {"name":"search","description":"search issues (readOnly)","input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
        {"name":"create","description":"create issue (destructive)","input_schema":{"type":"object","properties":{"title":{"type":"string"},"desc":{"type":"string"}},"required":["title"]}},
    ],{
        "search":lambda query: "\n".join(f"  {i['id']}: {i['title']} [{i['status']}]" for i in _d if query.lower() in i["title"].lower()) or "not found",
        "create":lambda title,desc="": f"created {title}",
    })
    return m

MOCK_S={"jira":_mk_jira}; mcp_clients={}; _NC=re.compile(r'[^a-zA-Z0-9_-]')
def nm(name): return _NC.sub('_',name)

# ============================================================
# s20 COMPREHENSIVE AGENT LOOP
# All 19 mechanisms annotated by [sXX] markers
# ============================================================

def comprehensive_agent_loop(messages: list):
    """s20: all mechanisms in one loop"""

    while True:
        # === PHASE A: Pre-LLM Preparation ===

        # [s05] todo nag: every N tool calls, inject reminder
        _tool_call_counter[0] += 1
        if _tool_call_counter[0] % TODO_NAG_EVERY == 0:
            pt = [t for t in _todo_items if not t.get("done")]
            if pt:
                nag = "Plan:\n" + "\n".join(f"  {'Y' if t.get('done') else '-'} {t.get('task','')}" for t in _todo_items)
                messages.append({"role":"user","content":f"[plan reminder]\n{nag}"})

        # [s14] cron queue injection
        with _cl: fired = list(_cq); _cq.clear()
        for j in fired: messages.append({"role":"user","content":f"[Scheduled] {j['prompt']}"})

        # [s13] background notifications
        bns = c_bg()
        if bns: messages.append({"role":"user","content":bns})

        # [s08] context compact
        compact_context(messages)

        # [s10] system prompt: [s07] skills + [s09] memory + [s19] MCP
        system = assemble_system_prompt()

        # [s02][s19] assemble tool pool
        tools = list(BASE_TOOLS)
        handlers = dict(BASE_HANDLERS)

        # Register comprehensive tools
        _common_tools = [
            {"name":"todo_write","input_schema":{"type":"object","properties":{"todos":{"type":"array","items":{"type":"object"}}},"required":["todos"]}},
            {"name":"task","input_schema":{"type":"object","properties":{"prompt":{"type":"string"},"tools_whitelist":{"type":"array","items":{"type":"string"}}},"required":["prompt"]}},
            {"name":"load_skill","input_schema":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}},
            {"name":"compact","input_schema":{"type":"object","properties":{},"required":[]}},
            {"name":"create_task","input_schema":{"type":"object","properties":{"subject":{"type":"string"},"description":{"type":"string"},"blockedBy":{"type":"array","items":{"type":"string"}}},"required":["subject"]}},
            {"name":"list_tasks","input_schema":{"type":"object","properties":{},"required":[]}},
            {"name":"claim_task","input_schema":{"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]}},
            {"name":"complete_task","input_schema":{"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]}},
            {"name":"schedule_cron","description":"cron: m h d M w","input_schema":{"type":"object","properties":{"cron":{"type":"string"},"prompt":{"type":"string"},"recurring":{"type":"boolean"}},"required":["cron","prompt"]}},
            {"name":"spawn_teammate","input_schema":{"type":"object","properties":{"name":{"type":"string"},"role":{"type":"string"},"prompt":{"type":"string"}},"required":["name","role","prompt"]}},
            {"name":"send_message","input_schema":{"type":"object","properties":{"to":{"type":"string"},"content":{"type":"string"}},"required":["to","content"]}},
            {"name":"check_inbox","input_schema":{"type":"object","properties":{},"required":[]}},
            {"name":"request_shutdown","input_schema":{"type":"object","properties":{"teammate":{"type":"string"}},"required":["teammate"]}},
            {"name":"request_plan","input_schema":{"type":"object","properties":{"teammate":{"type":"string"},"task":{"type":"string"}},"required":["teammate","task"]}},
            {"name":"review_plan","input_schema":{"type":"object","properties":{"request_id":{"type":"string"},"approve":{"type":"boolean"},"feedback":{"type":"string"}},"required":["request_id","approve"]}},
            {"name":"create_worktree","input_schema":{"type":"object","properties":{"name":{"type":"string"},"task_id":{"type":"string"}},"required":["name"]}},
            {"name":"list_worktrees","input_schema":{"type":"object","properties":{},"required":[]}},
            {"name":"remove_worktree","input_schema":{"type":"object","properties":{"name":{"type":"string"},"discard_changes":{"type":"boolean"}},"required":["name"]}},
            {"name":"connect_mcp","description":"connect MCP server. avlb: jira","input_schema":{"type":"object","properties":{"server_name":{"type":"string"}},"required":["server_name"]}},
            {"name":"save_memory","input_schema":{"type":"object","properties":{"entry":{"type":"string"}},"required":["entry"]}},
        ]
        tools.extend(_common_tools)
        handlers.update({
            "todo_write":lambda todos: run_todo_write(todos),
            "task":lambda prompt,tools_whitelist=None: spawn_subagent(prompt,tools_whitelist),
            "load_skill":lambda name: run_load_skill(name),
            "compact":lambda: (compact_context(messages),"compacted")[1],
            "create_task":lambda subject,description="",blockedBy=None: f"created: {ct(subject,description,blockedBy).id}",
            "list_tasks":lambda: _ftl(),
            "claim_task":lambda task_id: clm(task_id,"lead"),
            "complete_task":lambda task_id: comp(task_id),
            "schedule_cron":lambda cron,prompt,recurring=True: (lambda j={"cron":cron,"prompt":prompt,"recurring":recurring}: (_cj.update({f"c{random.randint(0,999999):06d}":j}),f"scheduled: {cron}"))(),
            "spawn_teammate":lambda name,role,prompt: stt(name,role,prompt),
            "send_message":lambda to,content: (BUS.send("lead",to,content),f"sent to {to}")[1],
            "check_inbox":lambda: "\n".join(f"  [{m['from']}] {m['content'][:200]}" for m in BUS.read_inbox("lead")) or "empty",
            "request_shutdown":lambda teammate: (lambda rid=nr(): (p_req.update({rid:Proto(rid,"shutdown","lead",teammate,"pending","")}), BUS.send("lead",teammate,"pls shutdown","shutdown_request",{"request_id":rid}), f"sent ({rid})")[2])(),
            "request_plan":lambda teammate,task: (BUS.send("lead",teammate,f"pls submit plan: {task}"),f"asked {teammate}")[1],
            "review_plan":lambda request_id,approve,feedback="": (lambda s=p_req.get(request_id): (setattr(s,"status","approved" if approve else "rejected"), BUS.send("lead",s.sender,feedback or ("ok" if approve else "no"),"plan_approval_response",{"request_id":request_id,"approve":approve}), f"{'Y' if approve else 'N'}")[2] if s and s.status=="pending" else "bad state")(),
            "create_worktree":cw,"list_worktrees":lws,"remove_worktree":rw,
            "connect_mcp":lambda server_name: (lambda f=MOCK_S.get(server_name): (lambda m=f(): (mcp_clients.update({server_name:m}), f"connected {server_name}: {[t['name'] for t in m.gt()]}")[1])() if f else f"unknown. avlb: {','.join(MOCK_S)}")(),
            "save_memory":lambda entry: run_save_memory(entry),
        })

        # [s19] inject MCP tools
        for sn,mp in mcp_clients.items():
            ss=nm(sn)
            for td in mp.gt():
                st=nm(td["name"]); pfx=f"mcp__{ss}__{st}"
                nd=td.copy(); nd["name"]=pfx; nd["description"]=f"[MCP:{sn}] {td.get('description','')}"
                tools.append(nd)
                def mh(c=mp,t=td["name"]):
                    def f(**kw): return c.ct(t,kw)
                    return f
                handlers[pfx]=mh()

        # === PHASE B: LLM Call [s11] ===
        try:
            response = call_llm(messages, system, tools)
        except Exception as e:
            messages.append({"role":"assistant","content":[{"type":"text","text":f"[Error] {e}"}]})
            return

        messages.append({"role":"assistant","content":response.content})

        # === PHASE C: Stop Check ===
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role":"user","content":str(force)})
                continue
            return

        # === PHASE D: Tool Execution ===
        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            print(f"\033[36m>>> [{block.name}]\033[0m")

            # [s04] PreToolUse hooks + [s03] permission
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type":"tool_result","tool_use_id":block.id,"content":f"[blocked] {blocked}"})
                continue

            h = handlers.get(block.name)
            if h:
                # [s13] background dispatch
                if block.name=="bash" and any(kw in block.input.get("command","").lower() for kw in ["install","build","test","deploy"]):
                    bid=s_bg(block.input.get("command",""),h,block.input)
                    results.append({"type":"tool_result","tool_use_id":block.id,"content":f"[bg:{bid}]"})
                    print(f"  bg:{bid}")
                else:
                    output = h(**block.input)
                    print(str(output)[:300])
                    results.append({"type":"tool_result","tool_use_id":block.id,"content":output})
            else:
                output = f"unknown: {block.name}"
                results.append({"type":"tool_result","tool_use_id":block.id,"content":output})

            # [s04] PostToolUse hooks
            trigger_hooks("PostToolUse", block, results[-1]["content"] if results else "")

        messages.append({"role":"user","content":results})

        # [s15] teammate inbox
        ib=BUS.read_inbox("lead")
        for m in ib:
            mt=m.get("type","")
            if mt=="plan_approval_request":
                messages.append({"role":"user","content":f"[plan review] {m['from']}: {m['content']} (id:{m['metadata']['request_id']})"})
                print(f"\n\033[33m[plan request] {m['from']}\033[0m")
            elif mt=="result":
                messages.append({"role":"user","content":f"[teammate done] {m['from']}: {m['content'][:300]}"})


# === MAIN ===
if __name__ == "__main__":
    register_default_hooks()

    print("=" * 55)
    print("  s20: Comprehensive Agent")
    print("  All 19 mechanisms in one while True loop")
    print("=" * 55)
    print()
    print("  A: [s05]nag [s14]cron [s13]bg [s08]compact [s10]prompt")
    print("  B: [s11]LLM+recovery")
    print("  C: [s04]Stop hooks")
    print("  D: [s04]Pre [s03]perm [s02]dispatch [s19]MCP [s13]bg")
    print("     [s05]todo [s06]sub [s07]skill [s15]team [s16]proto")
    print("     [s17]auto [s18]wt [s04]Post [s15]inbox")
    print()
    print("  Try anything. q/exit/empty to quit.")
    print()

    history = []
    while True:
        try:
            query = input("\033[36ms20 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\nbye!"); break
        if query.strip().lower() in ("q","exit",""):
            print("bye!"); break

        trigger_hooks("UserPromptSubmit", query)
        history.append({"role":"user","content":query})
        comprehensive_agent_loop(history)

        last = history[-1]
        if isinstance(last.get("content"), list):
            for block in last["content"]:
                if getattr(block,"type",None) == "text":
                    print(f"\n\033[32m{block.text}\033[0m")
        print()
