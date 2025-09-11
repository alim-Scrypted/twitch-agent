import asyncio, time
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv



def require_admin(x_admin_key: str = Header(None)):
    from os import getenv
    if x_admin_key != getenv("ADMIN_KEY"):
        raise HTTPException(status_code=401, detail="Unauthorized")


app = FastAPI(title="Twitch Agent Backend")
load_dotenv()

# --- In-memory storage & queues ---

SUBMISSIONS: Dict[int, Dict] = {}
APPROVED = asyncio.Queue()            # NEW: actions/code approvals go here
APPROVED_PROMPTS = asyncio.Queue()    # prompts approvals go here
VOTES: Dict[int, set] = {}
PROMPTS_HISTORY: Dict[int, Dict] = {}  # Store processed prompts history
NEXT_ID = 1

# --- Models ---

class SubmitPromptReq(BaseModel):
    user: str
    type: str               # "prompt" or "actions"
    text: str | None = None # for prompts
    code: str | None = None # for generated actions

class VoteReq(BaseModel):
    user: str
    submission_id: int

class DecisionReq(BaseModel):
    submission_id: int
    approved: bool = True

class EventReq(BaseModel):
    type: str
    id: int

# --- WebSocket broadcast for overlay/bot ---

clients = set()

def broadcast(event: Dict):
    msg = {"ts": time.time(), **event}
    for ws in list(clients):
        try:
            asyncio.create_task(ws.send_json(msg))
        except Exception:
            pass

# --- Endpoints ---

@app.post("/submit")
def submit(req: SubmitPromptReq):
    global NEXT_ID
    if req.type == "prompt":
        text = (req.text or "").strip()
        if not text or len(text) > 500:
            return {"error": "Empty or too long prompt"}
        sid = NEXT_ID; NEXT_ID += 1
        SUBMISSIONS[sid] = {"id": sid, "user": req.user, "type": "prompt",
                            "text": text, "status": "queued", "votes": 0}
        broadcast({"type":"queued","item":SUBMISSIONS[sid]})
        return {"id": sid}

    elif req.type == "actions":
        code = (req.code or "")
        lowered = code.replace(" ","").lower()
        for bad in ("import","exec(","eval(","__","subprocess","os.","sys.","open("):
            if bad in lowered:
                return {"error":"Generated actions contain disallowed tokens"}
        sid = NEXT_ID; NEXT_ID += 1
        SUBMISSIONS[sid] = {"id": sid, "user": req.user, "type": "actions",
                            "code": code, "status": "queued", "votes": 0}
        broadcast({"type":"queued","item":SUBMISSIONS[sid]})
        return {"id": sid}

    else:
        return {"error": "Unknown submission type"}

@app.get("/queue")
def queue():
    return [v for v in SUBMISSIONS.values() if v["status"] == "queued"]

@app.get("/history")
def history():
    # Return processed prompts history (most recent first)
    return list(reversed(list(PROMPTS_HISTORY.values())))

@app.post("/vote")
def vote(req: VoteReq):
    if req.submission_id not in SUBMISSIONS or SUBMISSIONS[req.submission_id]["status"] != "queued":
        return {"message": "No such queued submission"}
    VOTES.setdefault(req.submission_id, set()).add(req.user)
    SUBMISSIONS[req.submission_id]["votes"] = len(VOTES[req.submission_id])
    broadcast({"type": "vote", "id": req.submission_id, "votes": len(VOTES[req.submission_id])})
    return {"message": f"Vote counted for #{req.submission_id}"}

@app.post("/decide")
async def decide(req: DecisionReq, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    sub = SUBMISSIONS.get(req.submission_id)
    if not sub or sub["status"] != "queued":
        return {"message": "Not queued"}

    # Only handle rejection for safety
    if req.approved:
        return {"message": "Approval not allowed - use voting system instead"}

    sub["status"] = "rejected"
    # Send mod rejection message to bot
    broadcast({"type": "mod_rejected", "id": sub["id"], "user": sub.get("user"), "text": sub.get("text")})
    return {"message": f"Rejected #{sub['id']}"}

@app.get("/approved/prompt/next")
async def approved_prompt_next():
    try:
        sub = await asyncio.wait_for(APPROVED_PROMPTS.get(), timeout=1.0)
        return sub
    except asyncio.TimeoutError:
        return {}

@app.get("/approved/next")
async def approved_next():
    # NEW: runner polls this to get next approved "actions" item
    try:
        sub = await asyncio.wait_for(APPROVED.get(), timeout=1.0)
        return sub
    except asyncio.TimeoutError:
        return {}

@app.post("/event")
def event(req: EventReq):
    # Optional: runner/bot can post status like {"type":"finished","id":123}
    broadcast({"type": req.type, "id": req.id})
    return {"ok": True}

@app.post("/move-to-history")
def move_to_history(prompt_ids: list[int]):
    """Move processed prompts to history (called by bot after voting)"""
    moved_count = 0
    for pid in prompt_ids:
        if pid in SUBMISSIONS and SUBMISSIONS[pid]["status"] == "queued":
            # Move to history with processed status
            prompt = SUBMISSIONS[pid].copy()
            prompt["status"] = "processed"
            prompt["processed_at"] = time.time()
            PROMPTS_HISTORY[pid] = prompt
            # Remove from active submissions
            del SUBMISSIONS[pid]
            if pid in VOTES:
                del VOTES[pid]
            moved_count += 1
    
    broadcast({"type": "prompts_moved_to_history", "count": moved_count})
    return {"message": f"Moved {moved_count} prompts to history"}

@app.websocket("/ws")
async def ws_overlay(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)

@app.post("/clear")
def clear(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    SUBMISSIONS.clear(); VOTES.clear(); PROMPTS_HISTORY.clear()
    while not APPROVED.empty(): APPROVED.get_nowait()
    while not APPROVED_PROMPTS.empty(): APPROVED_PROMPTS.get_nowait()
    broadcast({"type":"cleared"})
    return {"message":"Cleared all data"}


@app.get("/panel", response_class=HTMLResponse)
def panel():
    return """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Twitch Agent — Mod Panel</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#070b11;
  --card:#0b1220cc;
  --card-solid:#0b1220;
  --muted:#96a3b8;
  --text:#e8eef5;
  --brand:#7c9cfb;
  --accent:#38d0ff;
  --ok:#36d399;
  --warn:#ffb300;
  --danger:#ff5c7c;
  --ring: rgba(124,156,251,.35);
  --glow: 0 0 0 1px var(--ring), 0 0 24px rgba(56,208,255,.15), inset 0 0 24px rgba(124,156,251,.08);
}

*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; background:
  radial-gradient(1200px 1200px at 80% -20%, rgba(124,156,251,.15), transparent 60%),
  radial-gradient(900px 900px at -10% 110%, rgba(56,208,255,.12), transparent 60%),
  var(--bg);
  color:var(--text); font-family:Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
}

.container{max-width:1200px; margin:32px auto; padding:0 20px;}
.header{display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:18px}
.hgroup h1{font-size:22px; margin:0 0 4px; letter-spacing:.2px}
.hgroup p{margin:0; color:var(--muted); font-size:13px}

.toolbar{
  display:flex; flex-wrap:wrap; gap:8px; align-items:center;
}
.btn{
  appearance:none; border:1px solid transparent; background:linear-gradient(180deg, #111a2b, #0a1422);
  color:var(--text); padding:9px 12px; border-radius:12px; cursor:pointer; font-weight:600; font-size:13px;
  box-shadow: var(--glow);
  transition: transform .1s ease, box-shadow .2s ease, border-color .2s ease, background .2s ease;
}
.btn:hover{ transform: translateY(-1px); border-color:var(--ring) }
.btn:active{ transform: translateY(0) scale(.99) }
.btn.primary{ background:linear-gradient(180deg, #152345, #0f1c36); border-color: rgba(124,156,251,.4) }
.btn.accent{ background:linear-gradient(180deg, #0f2330, #0b1c27); border-color: rgba(56,208,255,.45) }
.btn.ok{ background:linear-gradient(180deg, #0d2a21, #0b1f1a); border-color: rgba(54,211,153,.45) }
.btn.danger{ background:linear-gradient(180deg, #2a0f18, #220b12); border-color: rgba(255,92,124,.45) }

.status{margin-left:8px; color:var(--muted); font-size:12px}

.grid{
  display:grid; gap:16px; margin-top:16px;
  grid-template-columns: repeat(12, 1fr);
}
.card{
  grid-column: span 6;
  background: linear-gradient( to bottom right, rgba(13,22,38,.7), rgba(9,14,24,.7));
  border: 1px solid rgba(124,156,251,.2);
  border-radius: 18px; padding: 14px 14px 8px;
  box-shadow: var(--glow);
  backdrop-filter: blur(8px);
}
.card h3{margin:2px 2px 10px; font-size:14px; letter-spacing:.3px; color:#d6e1ff}

.list{display:flex; flex-direction:column; gap:8px; min-height:64px}
.row{
  display:grid; grid-template-columns: auto 1fr auto auto; gap:10px; align-items:center;
  background: linear-gradient(180deg, rgba(17,26,43,.7), rgba(12,18,31,.6));
  border:1px solid rgba(124,156,251,.18); border-radius:12px; padding:10px 12px;
}
.badge{
  font-weight:700; color:#dbe5ff; background: linear-gradient(180deg, #203056, #182747);
  border:1px solid rgba(124,156,251,.45);
  padding:4px 8px; border-radius:10px; font-size:12px; min-width:48px; text-align:center;
}
.kind{ color:#a8b9ff; font-weight:600; font-size:12px; opacity:.9 }
.meta{
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:48vw;
  font-size:13px; color:#eaf2ff;
}
.meta.code{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace }
.dim{ color:var(--muted); font-size:13px; padding:8px 4px }

.controls{ display:flex; gap:8px }
.btn.sm{ padding:6px 10px; font-size:12px; border-radius:10px }
.btn.reject{ background:linear-gradient(180deg, #35151f, #240d14); border-color: rgba(255,92,124,.5) }

.footer{margin-top:14px; color:var(--muted); font-size:12px}

/* toast */
.toast{
  position:fixed; right:18px; bottom:18px; min-width:220px; max-width:50ch;
  background:linear-gradient(180deg, #0f1c2f, #0b1524);
  border:1px solid rgba(124,156,251,.35);
  color:var(--text); padding:10px 12px; border-radius:12px; box-shadow: var(--glow);
  opacity:0; transform: translateY(8px); transition: all .25s ease;
}
.toast.show{ opacity:1; transform: translateY(0) }

/* responsive */
@media (max-width: 980px){
  .card{ grid-column: span 12; }
  .meta{ max-width: 60vw }
}
</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="hgroup">
        <h1>Creator Agent — Moderator Panel</h1>
        <p>Monitor prompts and reject unsafe content. Processed prompts appear in history.</p>
      </div>
      <div class="toolbar">
        <button class="btn accent" onclick="setKey()">Set Admin Key</button>
        <button class="btn" onclick="refresh(true)">Refresh</button>
        <button class="btn primary" id="autoBtn" onclick="toggleAuto()">Auto: On</button>
        <button class="btn danger" onclick="clearAll()">Clear all</button>
        <span class="status" id="status"></span>
      </div>
    </div>

    <div class="grid">
      <section class="card">
        <h3>Queued Prompts</h3>
        <div id="prompts" class="list">
          <div class="dim">No queued prompts.</div>
        </div>
      </section>

      <section class="card">
        <h3>Queued Prompts History</h3>
        <div id="history" class="list">
          <div class="dim">No processed prompts.</div>
        </div>
      </section>
    </div>

    <div class="footer">Tip: prompts move to history after voting. Use reject button to remove unsafe prompts.</div>
  </div>

  <div id="toast" class="toast"></div>

<script>
const BASE = location.origin;
let auto = true, timer = null;

function getKey(){ return localStorage.getItem('ADMIN_KEY') || ''; }
function setKey(){
  const v = prompt('Enter ADMIN_KEY:', getKey());
  if(v!==null){ localStorage.setItem('ADMIN_KEY', v); toast('Admin key set'); }
}
function status(t){ document.getElementById('status').textContent = t ?? ''; }
function toast(t){
  const el = document.getElementById('toast');
  el.textContent = t; el.classList.add('show');
  setTimeout(()=> el.classList.remove('show'), 1800);
}

function toggleAuto(){
  auto = !auto;
  document.getElementById('autoBtn').textContent = 'Auto: ' + (auto ? 'On' : 'Off');
  if(auto){ schedule(); } else { clearTimeout(timer); }
}

function schedule(){
  clearTimeout(timer);
  if(auto) timer = setTimeout(()=>refresh(false), 1000);
}

function row(it){
  const el = document.createElement('div'); el.className='row';

  const id = document.createElement('div'); id.className='badge'; id.textContent = '#' + it.id;
  const kind = document.createElement('div'); kind.className='kind'; kind.textContent = it.type.toUpperCase();
  const meta = document.createElement('div'); meta.className = 'meta' + (it.type==='actions' ? ' code' : '');
  meta.textContent = (it.type==='prompt' ? (it.text || '') : (it.code || ''));

  const controls = document.createElement('div'); controls.className='controls';
  const no = document.createElement('button'); no.className='btn sm reject'; no.textContent='Reject';
  no.onclick = ()=>decide(it.id, false, el, no);

  controls.appendChild(no);
  el.appendChild(id); el.appendChild(kind); el.appendChild(meta); el.appendChild(controls);
  return el;
}

function historyRow(it){
  const el = document.createElement('div'); el.className='row';

  const id = document.createElement('div'); id.className='badge'; id.textContent = '#' + it.id;
  const kind = document.createElement('div'); kind.className='kind'; 
  kind.textContent = it.status.toUpperCase();
  kind.style.color = it.status === 'processed' ? '#36d399' : '#ff5c7c';
  
  const meta = document.createElement('div'); meta.className = 'meta';
  meta.textContent = it.text || '';
  
  const timestamp = document.createElement('div'); timestamp.className='controls';
  if(it.processed_at) {
    const date = new Date(it.processed_at * 1000);
    timestamp.textContent = date.toLocaleTimeString();
    timestamp.style.fontSize = '11px';
    timestamp.style.color = '#96a3b8';
  }

  el.appendChild(id); el.appendChild(kind); el.appendChild(meta); el.appendChild(timestamp);
  return el;
}

async function refresh(force){
  if(force) status('Loading…');
  try{
    const r = await fetch(BASE + '/queue');
    const items = await r.json();
    const prompts = items.filter(i=>i.type==='prompt');
    
    // Fetch history
    const historyR = await fetch(BASE + '/history');
    const historyItems = await historyR.json();

    const pList = document.getElementById('prompts');
    const hList = document.getElementById('history');
    pList.innerHTML = ''; hList.innerHTML = '';

    if(!prompts.length) pList.innerHTML = '<div class="dim">No queued prompts.</div>';
    if(!historyItems.length) hList.innerHTML = '<div class="dim">No processed prompts.</div>';

    for(const it of prompts) pList.appendChild(row(it));
    for(const it of historyItems) hList.appendChild(historyRow(it));

    status('');
  }catch(e){
    status('Load error'); toast('Load error: '+e);
  }finally{
    schedule();
  }
}

async function decide(id, approved, row, btn){
  const k = getKey(); if(!k){ setKey(); return; }
  const label = btn.textContent; btn.textContent='...'; btn.disabled=true;
  try{
    const r = await fetch(BASE + '/decide', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-ADMIN-KEY':k},
      body: JSON.stringify({submission_id:id, approved})
    });
    const j = await r.json();
    toast(j.message || (approved?'Approved':'Rejected'));
    row.remove();
  }catch(e){
    toast('Error: '+e);
  }finally{
    btn.textContent = label; btn.disabled=false;
  }
}

async function clearAll(){
  const k = getKey(); if(!k){ setKey(); return; }
  if(!confirm('Really clear all queues and history?')) return;
  try{
    const r = await fetch(BASE + '/clear', {method:'POST', headers:{'X-ADMIN-KEY':k}});
    const j = await r.json(); toast(j.message || 'Cleared'); refresh(true);
  }catch(e){ toast('Error: '+e); }
}

refresh(true);
</script>
</body>
</html>
    """

