import asyncio, time, os, requests
import re
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
    text: str | None = None

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

# --- Prompt moderation ---

PROMPT_BANNED_PATTERNS = [
    r"```",                   # fenced code
    r"`.+?`",                 # inline code
    r"\b(import|def|class)\b",
    r"\b(sudo|wget|curl|rm\s+-rf|powershell|cmd\.exe)\b",
    r"\b(http|https)://",
    r"<script", r"</script", r"<\?php",
    r"\b(select|drop|insert|update)\b\s"
]
NSFW_WORDS = {
    "nsfw","sex","porn","porno","pornography","nude","nudity","xxx","erotic","explicit","fetish"
}

def _normalize_leetspeak(s: str) -> str:
    # map common leet to letters
    table = str.maketrans({
        "0":"o", "1":"i", "!":"i", "|":"l", "3":"e", "4":"a",
        "5":"s", "7":"t", "8":"b", "9":"g", "$":"s", "@":"a"
    })
    return s.translate(table)

def _has_repeated_ngram(text: str) -> bool:
    t = re.sub(r"\s+", "", text or "")
    ngram_lengths = [2,3,4]
    for n in ngram_lengths:
        if len(t) < n * 4:
            continue
        # count occurrences of each n-gram
        counts = {}
        for i in range(0, len(t)-n+1):
            g = t[i:i+n]
            counts[g] = counts.get(g, 0) + 1
        # if any short n-gram repeats a lot, treat as smash
        if any(c >= max(4, len(t) // (n*3)) for c in counts.values()):
            return True
    return False

def _gibberish_reason(text: str) -> str | None:
    t = (text or "").strip()
    if len(t) < 8:
        return "too short"
    # Excessive repeated characters (e.g., aaaaaaa, !!!!!)
    if re.search(r"(.)\1{5,}", t):
        return "excessive repeated characters"
    # Character variety
    uniq = len(set(t))
    if len(t) > 20 and (uniq / len(t)) < 0.15:
        return "very low character variety"
    # Mostly numbers
    alnum = [c for c in t if c.isalnum()]
    if alnum:
        digits = sum(c.isdigit() for c in alnum)
        if (digits / len(alnum)) > 0.6:
            return "mostly numbers"
    # Too few real words
    words = re.findall(r"[A-Za-z]+", t)
    alpha_words = [w for w in words if len(w) >= 3]
    if len(alpha_words) < 3:
        return "too few meaningful words"
    # Very low vowel ratio (common in random strings)
    letters = [c for c in t.lower() if c.isalpha()]
    if len(letters) >= 6:
        vowels = sum(c in "aeiou" for c in letters)
        if (vowels / len(letters)) < 0.25:
            return "very low vowel ratio"
    # Non-alphanumeric noise
    noise = sum(1 for c in t if not c.isalnum() and not c.isspace() and c not in ".,!?'-")
    if (noise / max(1, len(t))) > 0.4:
        return "too much non-alphanumeric noise"
    if _has_repeated_ngram(t):
        return "repeated pattern gibberish"
    return None

def prompt_violation(text: str) -> str | None:
    t = (text or "").lower()
    t_norm = _normalize_leetspeak(t)
    for w in NSFW_WORDS:
        if w in t_norm:
            return "NSFW content not allowed"
    for pat in PROMPT_BANNED_PATTERNS:
        if re.search(pat, t_norm, re.IGNORECASE):
            return "code/unsafe content not allowed"
    g = _gibberish_reason(text)
    if g:
        return f"gibberish: {g}"
    return None

# --- Prompt cleanup for AI bridge ---

BAD_WORDS = {
    # mild profanity; extend as needed
    "fuck","shit","bitch","asshole","dick","cunt","bastard"
}

def _mask_words(text: str, words: set[str]) -> str:
    def repl(m):
        w = m.group(0)
        if len(w) <= 2:
            return "*" * len(w)
        return w[0] + ("*" * (len(w)-2)) + w[-1]
    for w in sorted(words | NSFW_WORDS, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(w)}\b", repl, text, flags=re.IGNORECASE)
    return text

def prepare_prompt_for_ai(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"`{1,3}", "", t)                 # remove backticks/code fences
    t = re.sub(r"https?://\S+", "", t)           # drop URLs
    t = re.sub(r"\s+", " ", t).strip()           # collapse whitespace
    t = re.sub(r"([!?.,])\1{1,}", r"\1", t)      # dedupe punctuation

    # Sentence-case first letter, keep casing of proper nouns as-is
    if t and t[0].isalpha():
        t = t[0].upper() + t[1:]

    # Ensure terminal punctuation if looks like a sentence
    if t and t[-1] not in ".!?":
        t += "."

    # Mask profanity to keep content safe for downstream AI
    t = _mask_words(t, BAD_WORDS)
    return t

@app.post("/submit")
def submit(req: SubmitPromptReq):
    global NEXT_ID
    if req.type == "prompt":
        text = (req.text or "").strip()
        if not text or len(text) > 500:
            return {"error": "Empty or too long prompt"}
        reason = prompt_violation(text)
        if reason:
            broadcast({"type":"auto_rejected_prompt","user": req.user, "text": text, "reason": reason})
            return {"error": f"Unsafe prompt: {reason}"}
        sid = NEXT_ID; NEXT_ID += 1
        SUBMISSIONS[sid] = {"id": sid, "user": req.user, "type": "prompt",
                            "text": text, "status": "queued", "votes": 0}
        broadcast({"type":"queued","item":SUBMISSIONS[sid]})
        return {"id": sid}

    elif req.type == "actions":
        # Auto-approve safe actions at submit-time using regex-based screening
        code = (req.code or "")
        lc = code.lower()
        banned_patterns = (
            r"(?<!\.)\bopen\(",
            r"\bimport\b",
            r"\bexec\(",
            r"\beval\(",
            r"__",
            r"\bos\.",
            r"\bsys\.",
            r"\bsubprocess\b",
        )
        import re as _re
        if any(_re.search(p, lc) for p in banned_patterns):
            return {"error": "Generated actions contain disallowed tokens"}

        sid = NEXT_ID; NEXT_ID += 1
        item = {"id": sid, "user": req.user, "type": "actions",
                "code": code, "status": "approved", "votes": 0}
        SUBMISSIONS[sid] = item
        # Immediately enqueue for runner
        try:
            APPROVED.put_nowait({"id": sid, "user": req.user, "code": code})
        except Exception:
            pass
        broadcast({"type": "auto_approved_actions", "id": sid})
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
    payload = {"type": req.type, "id": req.id}
    if req.text is not None:
        payload["text"] = req.text
    broadcast(payload)
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

# --- Winner marking + AI bridge enqueue ---

class WinnerReq(BaseModel):
    id: int = Field(..., description="Winning prompt id")

@app.post("/prompt/win")
async def prompt_win(req: WinnerReq):
    sub = SUBMISSIONS.get(req.id) or PROMPTS_HISTORY.get(req.id)
    if not sub or sub.get("type") != "prompt":
        raise HTTPException(status_code=404, detail="Winner not found")

    clean = prepare_prompt_for_ai(sub.get("text") or "")
    # Mark outcome and store cleaned text wherever the item lives
    if req.id in SUBMISSIONS:
        SUBMISSIONS[req.id]["outcome"] = "won"
        SUBMISSIONS[req.id]["clean_text"] = clean
    else:
        PROMPTS_HISTORY[req.id]["outcome"] = "won"
        PROMPTS_HISTORY[req.id]["clean_text"] = clean

    # Enqueue for orchestrator bridge
    await APPROVED_PROMPTS.put({"id": req.id, "user": sub.get("user"), "text": clean})
    broadcast({"type": "prompt_won", "id": req.id})
    return {"message": "Winner queued for AI bridge", "id": req.id}

# --- Cleaned prompt editing/rebuild ---

class CleanUpdateReq(BaseModel):
    id: int
    clean_text: str

@app.post("/prompt/clean/update")
def prompt_clean_update(req: CleanUpdateReq, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    sub = SUBMISSIONS.get(req.id) or PROMPTS_HISTORY.get(req.id)
    if not sub or sub.get("type") != "prompt":
        raise HTTPException(status_code=404, detail="Prompt not found")
    # Accept direct edits from moderator
    if req.id in SUBMISSIONS:
        SUBMISSIONS[req.id]["clean_text"] = (req.clean_text or "").strip()
    else:
        PROMPTS_HISTORY[req.id]["clean_text"] = (req.clean_text or "").strip()
    broadcast({"type": "prompt_clean_updated", "id": req.id})
    return {"ok": True}

class CleanRebuildReq(BaseModel):
    id: int

def _grammar_correct(text: str) -> str:
    url = os.getenv("GRAMMAR_API_URL")
    if not url:
        return text
    try:
        headers = {"Content-Type": "application/json"}
        key = os.getenv("GRAMMAR_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        r = requests.post(url, json={"text": text}, headers=headers, timeout=10)
        if r.ok:
            j = r.json()
            return j.get("text") or j.get("corrected") or text
    except Exception:
        pass
    return text

@app.post("/prompt/clean/rebuild")
def prompt_clean_rebuild(req: CleanRebuildReq, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    sub = SUBMISSIONS.get(req.id) or PROMPTS_HISTORY.get(req.id)
    if not sub or sub.get("type") != "prompt":
        raise HTTPException(status_code=404, detail="Prompt not found")
    original = sub.get("text") or ""
    corrected = _grammar_correct(original)
    rebuilt = prepare_prompt_for_ai(corrected)
    if req.id in SUBMISSIONS:
        SUBMISSIONS[req.id]["clean_text"] = rebuilt
    else:
        PROMPTS_HISTORY[req.id]["clean_text"] = rebuilt
    broadcast({"type": "prompt_clean_rebuilt", "id": req.id})
    return {"ok": True, "clean_text": rebuilt}

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

/* modal */
.modal{
  position:fixed; inset:0; display:none; align-items:center; justify-content:center;
  background: rgba(0,0,0,.45); z-index: 50;
}
.modal.show{ display:flex }
.modal-content{
  width: min(640px, 92vw);
  background: linear-gradient(180deg, #0f1c2f, #0b1524);
  border:1px solid rgba(124,156,251,.35);
  border-radius: 14px; padding: 14px; box-shadow: var(--glow);
}
.modal-content h4{ margin: 4px 2px 12px; font-size: 14px; color: #d6e1ff; }
.modal-section{ margin: 8px 0 10px; }
.modal-label{ font-size: 12px; color: var(--muted); margin-bottom: 6px }
.modal-text{
  white-space: pre-wrap; font-size: 13px; color: var(--text);
  background: rgba(255,255,255,.02);
  border: 1px solid rgba(124,156,251,.15);
  padding: 10px; border-radius: 10px;
}
.modal-actions{ display:flex; justify-content:flex-end; gap:8px; margin-top: 10px; }

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

      <section class="card">
        <h3>Winners (AI-ready)</h3>
        <div id="winners" class="list">
          <div class="dim">No winners yet.</div>
        </div>
      </section>
    </div>

    <div class="footer">Tip: prompts move to history after voting. Use reject button to remove unsafe prompts.</div>
  </div>

  <div id="toast" class="toast"></div>

  <div id="winnerModal" class="modal">
    <div class="modal-content">
      <h4>Winner #<span id="wm-id"></span></h4>
      <div class="modal-section">
        <div class="modal-label">Original</div>
        <div id="wm-original" class="modal-text"></div>
      </div>
      <div class="modal-section">
        <div class="modal-label">AI-ready</div>
        <textarea id="wm-clean" class="modal-text" style="width:100%; min-height: 110px;"></textarea>
      </div>
      <div class="modal-actions">
        <button class="btn" onclick="rebuildWinner()">Rebuild</button>
        <button class="btn ok" onclick="saveWinner()">Save</button>
        <button class="btn" onclick="closeWinner()">Close</button>
      </div>
    </div>
  </div>

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

function winnersRow(it){
  const el = document.createElement('div'); el.className='row';

  const id = document.createElement('div'); id.className='badge'; id.textContent = '#' + it.id;
  const kind = document.createElement('div'); kind.className='kind'; 
  kind.textContent = 'WINNER';
  kind.style.color = '#36d399';

  const meta = document.createElement('div'); meta.className='meta';
  const original = document.createElement('div'); original.textContent = it.text || '';
  const cleaned = document.createElement('div'); cleaned.textContent = 'AI-ready: ' + (it.clean_text || '(pending)');
  cleaned.style.color = '#a8b9ff'; cleaned.style.fontSize = '12px'; cleaned.style.opacity = .9;
  meta.appendChild(original); meta.appendChild(cleaned);

  const controls = document.createElement('div'); controls.className='controls';
  if(it.processed_at){
    const ts = document.createElement('div');
    const d = new Date(it.processed_at * 1000);
    ts.textContent = d.toLocaleTimeString();
    ts.style.fontSize = '11px'; ts.style.color = '#96a3b8'; ts.style.marginRight = '6px';
    controls.appendChild(ts);
  }
  const view = document.createElement('button'); view.className='btn sm'; view.textContent='View';
  view.onclick = (e)=>{ e.stopPropagation(); openWinner(it); };
  controls.appendChild(view);

  el.appendChild(id); el.appendChild(kind); el.appendChild(meta); el.appendChild(controls);
  el.onclick = ()=>openWinner(it);
  return el;
}

function openWinner(it){
  try{
    document.getElementById('wm-id').textContent = it.id ?? '';
    document.getElementById('wm-original').textContent = it.text || '';
    const c = document.getElementById('wm-clean');
    c.value = it.clean_text || '';
    document.getElementById('winnerModal').classList.add('show');
  }catch(e){ toast('Open error: '+e); }
}
function closeWinner(){
  document.getElementById('winnerModal').classList.remove('show');
}

async function saveWinner(){
  const id = +(document.getElementById('wm-id').textContent||0);
  const clean = document.getElementById('wm-clean').value || '';
  const k = getKey(); if(!k){ setKey(); return; }
  try{
    const r = await fetch(BASE + '/prompt/clean/update', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-ADMIN-KEY':k},
      body: JSON.stringify({id, clean_text: clean})
    });
    const j = await r.json();
    if(!r.ok){ throw new Error(j.detail || 'Save failed'); }
    toast('Saved');
    refresh(false);
  }catch(e){ toast('Save error: '+e); }
}

async function rebuildWinner(){
  const id = +(document.getElementById('wm-id').textContent||0);
  const k = getKey(); if(!k){ setKey(); return; }
  try{
    const r = await fetch(BASE + '/prompt/clean/rebuild', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-ADMIN-KEY':k},
      body: JSON.stringify({id})
    });
    const j = await r.json();
    if(!r.ok){ throw new Error(j.detail || 'Rebuild failed'); }
    document.getElementById('wm-clean').value = j.clean_text || '';
    toast('Rebuilt');
  }catch(e){ toast('Rebuild error: '+e); }
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
    const winners = historyItems.filter(i=>i.outcome==='won');

    const pList = document.getElementById('prompts');
    const hList = document.getElementById('history');
    const wList = document.getElementById('winners');
    pList.innerHTML = ''; hList.innerHTML = ''; wList.innerHTML = '';

    if(!prompts.length) pList.innerHTML = '<div class="dim">No queued prompts.</div>';
    if(!historyItems.length) hList.innerHTML = '<div class="dim">No processed prompts.</div>';
    if(!winners.length) wList.innerHTML = '<div class="dim">No winners yet.</div>';

    for(const it of prompts) pList.appendChild(row(it));
    for(const it of historyItems) hList.appendChild(historyRow(it));
    for(const it of winners) wList.appendChild(winnersRow(it));

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