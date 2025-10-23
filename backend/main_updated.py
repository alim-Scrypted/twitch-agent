import asyncio, time, re, os, requests
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

class WinnerReq(BaseModel):
    id: int = Field(..., description="Winning prompt id")

class CleanUpdateReq(BaseModel):
    id: int
    clean_text: str

# --- Helper functions ---

def prepare_prompt_for_ai(text: str) -> str:
    """Clean and prepare prompt text for AI processing"""
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
    # (simplified version - in real implementation would use a proper filter)
    profanity = ["damn", "hell", "crap", "suck", "stupid"]
    for word in profanity:
        t = re.sub(f"\\b{word}\\b", word[0] + "*" * (len(word) - 1), t, flags=re.IGNORECASE)

    return t

def clean_with_grammar_api(text: str) -> str:
    """Clean text using external grammar API if available"""
    url = os.getenv("GRAMMAR_API_URL")
    key = os.getenv("GRAMMAR_API_KEY")

    if not url or not key:
        return prepare_prompt_for_ai(text)

    try:
        headers = {"Authorization": f"Bearer {key}"}
        r = requests.post(url, json={"text": text}, headers=headers, timeout=10)
        if r.ok:
            j = r.json()
            return j.get("text") or j.get("corrected") or prepare_prompt_for_ai(text)
    except Exception:
        pass

    return prepare_prompt_for_ai(text)

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
async def submit(req: SubmitPromptReq):
    global NEXT_ID
    if req.type == "prompt":
        text = (req.text or "").strip()
        if not text or len(text) > 500:
            return {"error": "Empty or too long prompt"}
        sid = NEXT_ID; NEXT_ID += 1
        SUBMISSIONS[sid] = {"id": sid, "user": req.user, "type": "prompt",
                            "text": text, "status": "queued", "votes": 0, "timestamp": time.time()}
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
                            "code": code, "status": "approved", "votes": 0, "timestamp": time.time()}
        broadcast({"type":"queued","item":SUBMISSIONS[sid]})
        # Auto-approve actions and broadcast the event
        broadcast({"type": "auto_approved_actions", "id": sid})
        # Enqueue for runner execution
        await APPROVED.put({"id": sid, "user": req.user, "code": code})
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
            prompt["timestamp"] = prompt.get("timestamp", time.time())
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

# --- Missing endpoints for full system functionality ---

@app.post("/prompt/win")
async def prompt_win(req: WinnerReq):
    """Mark a prompt as winner and queue for AI processing"""
    sub = SUBMISSIONS.get(req.id) or PROMPTS_HISTORY.get(req.id)
    if not sub or sub.get("type") != "prompt":
        raise HTTPException(status_code=404, detail="Prompt not found")

    clean = clean_with_grammar_api(sub.get("text") or "")
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

@app.post("/prompt/clean/update")
def prompt_clean_update(req: CleanUpdateReq, x_admin_key: str = Header(None)):
    """Update cleaned prompt text (admin only)"""
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
    return {"message": "Clean text updated", "id": req.id}

@app.post("/prompt/clean/rebuild")
def prompt_clean_rebuild(req: WinnerReq, x_admin_key: str = Header(None)):
    """Rebuild cleaned prompt from original (admin only)"""
    require_admin(x_admin_key)
    sub = SUBMISSIONS.get(req.id) or PROMPTS_HISTORY.get(req.id)
    if not sub or sub.get("type") != "prompt":
        raise HTTPException(status_code=404, detail="Prompt not found")

    clean = clean_with_grammar_api(sub.get("text") or "")
    if req.id in SUBMISSIONS:
        SUBMISSIONS[req.id]["clean_text"] = clean
    else:
        PROMPTS_HISTORY[req.id]["clean_text"] = clean

    broadcast({"type": "prompt_clean_rebuilt", "id": req.id})
    return {"message": "Clean text rebuilt", "id": req.id}

@app.post("/poll/start")
def poll_start(options: list[dict]):
    """Start a new poll (called by bot)"""
    poll_data = {
        "options": options,
        "startTime": time.time(),
        "votes": {i: 0 for i in range(len(options))}
    }
    broadcast({"type": "poll_started", "options": options, "startTime": poll_data["startTime"]})
    return {"message": "Poll started"}

@app.post("/poll/end")
def poll_end(winner: dict = None):
    """End poll and announce winner (called by bot)"""
    poll_data = {"winner": winner} if winner else {}
    broadcast({"type": "poll_ended", **poll_data})
    return {"message": "Poll ended"}

@app.post("/auto_approved_actions")
def auto_approved_actions(submission_id: int):
    """Mark actions as auto-approved (called by orchestrator)"""
    if submission_id in SUBMISSIONS:
        SUBMISSIONS[submission_id]["status"] = "approved"
        broadcast({"type": "auto_approved_actions", "id": submission_id})
        return {"message": f"Actions #{submission_id} auto-approved"}
    return {"error": "Submission not found"}

@app.post("/finished")
def finished(submission_id: int):
    """Mark action execution as finished (called by runner)"""
    if submission_id in SUBMISSIONS:
        SUBMISSIONS[submission_id]["status"] = "completed"
        broadcast({"type": "finished", "id": submission_id})
        return {"message": f"Action #{submission_id} completed"}
    return {"error": "Submission not found"}

# --- Dashboard API endpoints ---

@app.get("/dashboard/stats")
def dashboard_stats():
    """Get dashboard statistics"""
    total_prompts = len([p for p in SUBMISSIONS.values() if p["type"] == "prompt"]) + len(PROMPTS_HISTORY)
    winners = len([p for p in PROMPTS_HISTORY.values() if p.get("outcome") == "won"])
    actions_executed = winners  # Simplified - in real implementation would track actual executions
    contributors = len(set([p.get("user") for p in list(SUBMISSIONS.values()) + list(PROMPTS_HISTORY.values()) if p.get("user")]))

    # Calculate uptime (simplified)
    import time
    start_time = getattr(app.state, 'start_time', time.time())
    if not hasattr(app.state, 'start_time'):
        app.state.start_time = time.time()
    uptime_seconds = time.time() - start_time
    uptime_str = f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m"

    return {
        "total_prompts": total_prompts,
        "winners": winners,
        "actions_executed": actions_executed,
        "contributors": contributors,
        "uptime": uptime_str,
        "queued_prompts": len([p for p in SUBMISSIONS.values() if p["type"] == "prompt"]),
        "processing_prompts": len([p for p in SUBMISSIONS.values() if p.get("status") == "processing"])
    }

@app.get("/dashboard/capabilities")
def dashboard_capabilities():
    """Get agent capabilities tree"""
    capabilities = []

    # Extract capabilities from winning prompts
    winners = [p for p in PROMPTS_HISTORY.values() if p.get("outcome") == "won"]

    # Group by capability type
    capability_groups = {}
    for winner in winners:
        text = (winner.get("text") or "").lower()
        if "click" in text or "mouse" in text:
            cap_type = "Mouse Control"
        elif "type" in text or "text" in text or "keyboard" in text:
            cap_type = "Text Input"
        elif "file" in text or "write" in text or "read" in text:
            cap_type = "File I/O"
        elif "wait" in text or "delay" in text or "time" in text:
            cap_type = "System Control"
        else:
            cap_type = "General Actions"

        if cap_type not in capability_groups:
            capability_groups[cap_type] = []
        capability_groups[cap_type].append(winner)

    # Build capability tree
    for cap_type, prompts in capability_groups.items():
        emoji = {
            "Mouse Control": "ðŸ–±ï¸",
            "Text Input": "âŒ¨ï¸",
            "File I/O": "ðŸ“",
            "System Control": "ðŸ”§",
            "General Actions": "âš¡"
        }.get(cap_type, "âš¡")

        capabilities.append({
            "type": cap_type,
            "emoji": emoji,
            "count": len(prompts),
            "prompts": [{"id": p["id"], "text": p["text"], "user": p["user"]} for p in prompts[-5:]]  # Last 5
        })

    return {"capabilities": capabilities}

@app.get("/dashboard/contributors")
def dashboard_contributors():
    """Get top contributors leaderboard"""
    contributor_stats = {}

    # Count from all submissions and history
    all_items = list(SUBMISSIONS.values()) + list(PROMPTS_HISTORY.values())

    for item in all_items:
        user = item.get("user")
        if not user:
            continue

        if user not in contributor_stats:
            contributor_stats[user] = {"prompts": 0, "wins": 0, "votes": 0}

        contributor_stats[user]["prompts"] += 1

        if item.get("type") == "prompt":
            # Count votes for this prompt
            votes = VOTES.get(item["id"], set())
            contributor_stats[user]["votes"] += len(votes)

        if item.get("outcome") == "won":
            contributor_stats[user]["wins"] += 1

    # Sort by wins, then prompts, then votes
    sorted_contributors = [
        {
            "user": user,
            "prompts": stats["prompts"],
            "wins": stats["wins"],
            "votes": stats["votes"],
            "score": (stats["wins"] * 10) + (stats["prompts"] * 2) + (stats["votes"] * 0.1)
        }
        for user, stats in contributor_stats.items()
    ]

    sorted_contributors.sort(key=lambda x: x["score"], reverse=True)

    return {
        "contributors": sorted_contributors[:20],  # Top 20
        "total_contributors": len(contributor_stats)
    }

@app.get("/dashboard/timeline")
def dashboard_timeline():
    """Get evolution timeline data"""
    timeline_events = []

    # Add prompt submission events
    for item in list(SUBMISSIONS.values()) + list(PROMPTS_HISTORY.values()):
        if item["type"] == "prompt":
            event_type = "prompt_submitted"
            status = "voting"
            if item.get("status") == "processed":
                status = "processed"
            elif item.get("outcome") == "won":
                status = "won"

            timeline_events.append({
                "id": item["id"],
                "timestamp": item.get("timestamp", item.get("processed_at", time.time())),
                "type": event_type,
                "user": item["user"],
                "text": item["text"],
                "status": status,
                "votes": len(VOTES.get(item["id"], set())) if item["id"] in SUBMISSIONS else 0
            })

    # Add action generation events
    for item in SUBMISSIONS.values():
        if item["type"] == "actions" and item.get("status") == "approved":
            timeline_events.append({
                "id": item["id"],
                "timestamp": time.time(),  # Would need proper timestamp
                "type": "action_generated",
                "user": item["user"],
                "code": item["code"],
                "status": "generated"
            })

    # Sort by timestamp
    timeline_events.sort(key=lambda x: x["timestamp"], reverse=True)

    return {"timeline": timeline_events[:50]}  # Last 50 events

@app.get("/dashboard/current")
def dashboard_current():
    """Get current processing status"""
    current_processing = None

    # Check for prompts being processed (winners)
    for item in PROMPTS_HISTORY.values():
        if item.get("outcome") == "won" and not item.get("clean_text"):
            current_processing = {
                "type": "cleaning",
                "prompt_id": item["id"],
                "user": item["user"],
                "text": item["text"],
                "stage": "cleaning"
            }
            break

    # Check for actions being generated
    if not current_processing:
        for item in SUBMISSIONS.values():
            if item["type"] == "actions" and item.get("status") == "approved":
                current_processing = {
                    "type": "ai_processing",
                    "action_id": item["id"],
                    "user": item["user"],
                    "code": item["code"],
                    "stage": "ai_processing"
                }
                break

    return {"current": current_processing}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ðŸŽ¯ AI Agent Formation Dashboard</title>
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
  --success:#10b981;
  --info:#3b82f6;
  --warning:#f59e0b;
}

* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; padding: 0; }
body {
  background:
  radial-gradient(1200px 1200px at 80% -20%, rgba(124,156,251,.15), transparent 60%),
  radial-gradient(900px 900px at -10% 110%, rgba(56,208,255,.12), transparent 60%),
  var(--bg);
  color: var(--text);
  font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  overflow-x: hidden;
}

.dashboard {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

/* Header */
.header {
  background: linear-gradient(to bottom right, rgba(13,22,38,.8), rgba(9,14,24,.8));
  border-bottom: 1px solid rgba(124,156,251,.2);
  padding: 16px 24px;
  backdrop-filter: blur(8px);
}

.header-content {
  max-width: 1400px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.header-title {
  display: flex;
  align-items: center;
  gap: 12px;
}

.header-title h1 {
  margin: 0;
  font-size: 24px;
  font-weight: 700;
  background: linear-gradient(135deg, var(--brand), var(--accent));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.header-stats {
  display: flex;
  gap: 24px;
  align-items: center;
}

.stat {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: var(--muted);
}

.stat-value {
  font-weight: 700;
  color: var(--text);
}

.stat-icon {
  width: 16px;
  height: 16px;
  opacity: 0.7;
}

.version {
  font-size: 12px;
  color: var(--muted);
  font-weight: 500;
}

/* Main Content */
.main-content {
  flex: 1;
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px;
  width: 100%;
}

.content-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 24px;
  height: calc(100vh - 200px);
}

/* Panel Styles */
.panel {
  background: linear-gradient(to bottom right, rgba(13,22,38,.7), rgba(9,14,24,.7));
  border: 1px solid rgba(124,156,251,.2);
  border-radius: 18px;
  padding: 20px;
  box-shadow: var(--glow);
  backdrop-filter: blur(8px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid rgba(124,156,251,.15);
}

.panel-title {
  font-size: 16px;
  font-weight: 600;
  color: #d6e1ff;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-icon {
  width: 20px;
  height: 20px;
  opacity: 0.8;
}

.panel-content {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* Live Prompts Panel */
.live-prompts .prompt-item {
  background: linear-gradient(180deg, rgba(17,26,43,.7), rgba(12,18,31,.6));
  border: 1px solid rgba(124,156,251,.18);
  border-radius: 12px;
  padding: 12px;
  display: flex;
  align-items: flex-start;
  gap: 12px;
}

.prompt-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--brand), var(--accent));
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 14px;
  color: white;
  flex-shrink: 0;
}

.prompt-content {
  flex: 1;
  min-width: 0;
}

.prompt-text {
  font-size: 14px;
  color: var(--text);
  line-height: 1.4;
  margin-bottom: 4px;
  word-break: break-word;
}

.prompt-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--muted);
}

.prompt-user {
  font-weight: 600;
  color: var(--accent);
}

.prompt-time {
  opacity: 0.7;
}

/* Voting Panel */
.voting-panel .poll-item {
  background: linear-gradient(180deg, rgba(17,26,43,.7), rgba(12,18,31,.6));
  border: 1px solid rgba(124,156,251,.18);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 12px;
}

.poll-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.poll-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.poll-timer {
  background: var(--warn);
  color: white;
  padding: 4px 8px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
}

.poll-options {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.poll-option {
  background: rgba(255,255,255,.05);
  border: 1px solid rgba(124,156,251,.2);
  border-radius: 8px;
  padding: 8px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.poll-option:hover {
  border-color: var(--brand);
  background: rgba(124,156,251,.1);
}

.poll-option.active {
  border-color: var(--ok);
  background: rgba(54,211,153,.1);
}

.poll-votes {
  font-size: 12px;
  color: var(--muted);
  font-weight: 600;
}

.poll-winner {
  background: linear-gradient(135deg, rgba(54,211,153,.2), rgba(16,185,129,.1));
  border-color: var(--ok);
}

/* Evolution Timeline */
.evolution-timeline .timeline-item {
  background: linear-gradient(180deg, rgba(17,26,43,.7), rgba(12,18,31,.6));
  border: 1px solid rgba(124,156,251,.18);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 12px;
  position: relative;
}

.timeline-item::before {
  content: '';
  position: absolute;
  left: -8px;
  top: 20px;
  width: 12px;
  height: 12px;
  background: var(--brand);
  border-radius: 50%;
  border: 2px solid var(--bg);
}

.timeline-item.processing::before {
  background: var(--warn);
  animation: pulse 2s infinite;
}

.timeline-item.completed::before {
  background: var(--ok);
}

.timeline-item.failed::before {
  background: var(--danger);
}

.timeline-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.timeline-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.timeline-status {
  padding: 4px 8px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
}

.timeline-status.processing {
  background: var(--warn);
  color: white;
}

.timeline-status.completed {
  background: var(--ok);
  color: white;
}

.timeline-status.failed {
  background: var(--danger);
  color: white;
}

.timeline-content {
  font-size: 13px;
  color: var(--muted);
  line-height: 1.4;
}

.timeline-meta {
  font-size: 11px;
  color: var(--muted);
  margin-top: 8px;
  opacity: 0.8;
}

/* Capability Tree */
.capability-tree {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}

.capability-item {
  padding: 8px 0;
  border-left: 2px solid rgba(124,156,251,.3);
  margin-left: 12px;
  padding-left: 16px;
  position: relative;
}

.capability-item::before {
  content: '';
  position: absolute;
  left: -6px;
  top: 16px;
  width: 8px;
  height: 8px;
  background: var(--brand);
  border-radius: 50%;
}

.capability-item.root {
  border-left: none;
  margin-left: 0;
  padding-left: 0;
}

.capability-item.root::before {
  display: none;
}

.capability-name {
  font-weight: 600;
  color: var(--text);
  font-size: 14px;
}

.capability-desc {
  font-size: 12px;
  color: var(--muted);
  margin-left: 16px;
  line-height: 1.3;
}

.capability-badge {
  display: inline-block;
  background: rgba(124,156,251,.2);
  color: var(--brand);
  padding: 2px 6px;
  border-radius: 6px;
  font-size: 10px;
  font-weight: 600;
  margin-left: 8px;
}

/* Contributors Panel */
.contributors-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.contributor-item {
  background: linear-gradient(180deg, rgba(17,26,43,.7), rgba(12,18,31,.6));
  border: 1px solid rgba(124,156,251,.18);
  border-radius: 12px;
  padding: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
}

.contributor-rank {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--brand), var(--accent));
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 14px;
  color: white;
  flex-shrink: 0;
}

.contributor-info {
  flex: 1;
}

.contributor-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 2px;
}

.contributor-stats {
  font-size: 12px;
  color: var(--muted);
}

.contributor-badge {
  background: rgba(54,211,153,.2);
  color: var(--ok);
  padding: 2px 6px;
  border-radius: 6px;
  font-size: 10px;
  font-weight: 600;
}

/* Chat Panel */
.chat-messages {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
}

.chat-message {
  background: linear-gradient(180deg, rgba(17,26,43,.7), rgba(12,18,31,.6));
  border: 1px solid rgba(124,156,251,.18);
  border-radius: 12px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.4;
}

.chat-user {
  font-weight: 600;
  color: var(--accent);
  margin-right: 8px;
}

.chat-text {
  color: var(--text);
}

.chat-input-area {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid rgba(124,156,251,.15);
}

.chat-input {
  width: 100%;
  background: rgba(255,255,255,.05);
  border: 1px solid rgba(124,156,251,.2);
  border-radius: 8px;
  padding: 8px 12px;
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
}

.chat-input:focus {
  outline: none;
  border-color: var(--brand);
  box-shadow: 0 0 0 2px rgba(124,156,251,.2);
}

/* Animations */
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.fade-in {
  animation: fadeIn 0.3s ease-out;
}

/* Responsive Design */
@media (max-width: 1200px) {
  .content-grid {
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  .header-stats {
    gap: 16px;
  }
}

@media (max-width: 768px) {
  .content-grid {
    grid-template-columns: 1fr;
    gap: 16px;
  }

  .header-content {
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }

  .header-stats {
    flex-wrap: wrap;
    gap: 12px;
  }

  .main-content {
    padding: 16px;
  }
}

/* Scrollbar Styling */
.panel-content::-webkit-scrollbar {
  width: 6px;
}

.panel-content::-webkit-scrollbar-track {
  background: rgba(255,255,255,.05);
  border-radius: 3px;
}

.panel-content::-webkit-scrollbar-thumb {
  background: rgba(124,156,251,.3);
  border-radius: 3px;
}

.panel-content::-webkit-scrollbar-thumb:hover {
  background: rgba(124,156,251,.5);
}
</style>
</head>
<body>
  <div class="dashboard">
    <!-- Header -->
    <header class="header">
      <div class="header-content">
        <div class="header-title">
          <div style="width: 32px; height: 32px; background: linear-gradient(135deg, var(--brand), var(--accent)); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 18px; font-weight: 700;">ðŸŽ¯</div>
          <div>
            <h1>AI Agent Formation Dashboard</h1>
            <div class="version">v1.0.0</div>
      </div>
      </div>
        <div class="header-stats">
          <div class="stat">
            <span class="stat-icon">ðŸ“Š</span>
            <span class="stat-value" id="stat-prompts">0</span>
            <span>prompts</span>
    </div>
          <div class="stat">
            <span class="stat-icon">ðŸ†</span>
            <span class="stat-value" id="stat-winners">0</span>
            <span>winners</span>
          </div>
          <div class="stat">
            <span class="stat-icon">âš¡</span>
            <span class="stat-value" id="stat-actions">0</span>
            <span>actions</span>
          </div>
          <div class="stat">
            <span class="stat-icon">â±ï¸</span>
            <span class="stat-value" id="stat-uptime">0m</span>
          </div>
          <div class="stat">
            <span class="stat-icon">ðŸ‘¥</span>
            <span class="stat-value" id="stat-contributors">0</span>
            <span>contributors</span>
          </div>
          <div class="stat" id="connection-status">
            <span class="stat-icon">ðŸ”´</span>
            <span>Disconnected</span>
          </div>
        </div>
      </div>
    </header>

    <!-- Main Content -->
    <main class="main-content">
      <div class="content-grid">
        <!-- Left Column -->
        <div style="display: flex; flex-direction: column; gap: 24px;">
          <!-- Live Prompts -->
          <div class="panel live-prompts">
            <div class="panel-header">
              <h3 class="panel-title">
                <span class="panel-icon">ðŸŒ±</span>
                Raw Prompts
              </h3>
              <div style="font-size: 12px; color: var(--muted);">Live Feed</div>
        </div>
            <div class="panel-content" id="prompts-list">
              <div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">
                No prompts yet...
              </div>
            </div>
          </div>

          <!-- Community Voting -->
          <div class="panel voting-panel">
            <div class="panel-header">
              <h3 class="panel-title">
                <span class="panel-icon">ðŸ—³ï¸</span>
                Community Voting
              </h3>
              <div style="font-size: 12px; color: var(--muted);">Live Polls</div>
        </div>
            <div class="panel-content" id="voting-panel">
              <div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">
                No active polls
              </div>
            </div>
          </div>
    </div>

        <!-- Center Column -->
        <div style="display: flex; flex-direction: column; gap: 24px;">
          <!-- Agent Evolution -->
          <div class="panel evolution-timeline">
            <div class="panel-header">
              <h3 class="panel-title">
                <span class="panel-icon">ðŸ¤–</span>
                Agent Evolution
              </h3>
              <div style="font-size: 12px; color: var(--muted);">Formation Timeline</div>
            </div>
            <div class="panel-content" id="evolution-timeline">
              <div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">
                No evolution events yet...
              </div>
            </div>
  </div>

          <!-- Current Processing -->
          <div class="panel" style="border-color: var(--warn);">
            <div class="panel-header">
              <h3 class="panel-title">
                <span class="panel-icon">âš¡</span>
                Current Processing
              </h3>
              <div style="font-size: 12px; color: var(--muted);">Live Status</div>
            </div>
            <div class="panel-content" id="current-processing">
              <div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">
                No current processing
              </div>
            </div>
          </div>
        </div>

        <!-- Right Column -->
        <div style="display: flex; flex-direction: column; gap: 24px;">
          <!-- Capability Growth -->
          <div class="panel capability-tree">
            <div class="panel-header">
              <h3 class="panel-title">
                <span class="panel-icon">ðŸ“ˆ</span>
                Capability Growth
              </h3>
              <div style="font-size: 12px; color: var(--muted);">Current Abilities</div>
            </div>
            <div class="panel-content" id="capability-tree">
              <div class="capability-item root">
                <div class="capability-name">ðŸŽ¯ Current AI Agent Capabilities</div>
              </div>
              <div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px; margin-top: 16px;">
                No capabilities yet...
              </div>
            </div>
          </div>

          <!-- Top Contributors -->
          <div class="panel">
            <div class="panel-header">
              <h3 class="panel-title">
                <span class="panel-icon">ðŸ†</span>
                Top Contributors
              </h3>
              <div style="font-size: 12px; color: var(--muted);">Leaderboard</div>
            </div>
            <div class="panel-content" id="contributors-list">
              <div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">
                No contributors yet...
              </div>
            </div>
          </div>

          <!-- Community Chat -->
          <div class="panel">
            <div class="panel-header">
              <h3 class="panel-title">
                <span class="panel-icon">ðŸ’¬</span>
                Community Chat
              </h3>
              <div style="font-size: 12px; color: var(--muted);">Recent Messages</div>
            </div>
            <div class="panel-content">
              <div class="chat-messages" id="chat-messages">
                <div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">
                  No messages yet...
                </div>
              </div>
              <div class="chat-input-area">
                <input type="text" class="chat-input" id="chat-input" placeholder="Type a message..." maxlength="200">
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>

<script>
    const BASE_URL = window.location.origin;
    const wsUrl = (window.location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + window.location.host + '/ws';
    let ws = null;
    let dashboardData = {
      prompts: [],
      history: [],
      votes: {},
      currentPoll: null,
      processing: null,
      capabilities: [],
      contributors: [],
      chat: []
    };

    // Initialize WebSocket connection
    function initWebSocket() {
      ws = new WebSocket(wsUrl);

      ws.onopen = function(event) {
        console.log('WebSocket connected');
        updateConnectionStatus(true);
      };

      ws.onmessage = function(event) {
        try {
          const data = JSON.parse(event.data);
          handleWebSocketMessage(data);
        } catch (e) {
          console.error('Error parsing WebSocket message:', e);
        }
      };

      ws.onclose = function(event) {
        console.log('WebSocket disconnected');
        updateConnectionStatus(false);
        // Reconnect after 3 seconds
        setTimeout(initWebSocket, 3000);
      };

      ws.onerror = function(error) {
        console.error('WebSocket error:', error);
        updateConnectionStatus(false);
      };
    }

    function updateConnectionStatus(connected) {
      const statusElement = document.getElementById('connection-status');
      if (statusElement) {
        statusElement.style.color = connected ? 'var(--ok)' : 'var(--danger)';
        statusElement.textContent = connected ? 'ðŸŸ¢ Connected' : 'ðŸ”´ Disconnected';
      }
    }

    function handleWebSocketMessage(data) {
      console.log('Received:', data);

      switch (data.type) {
        case 'queued':
          if (data.item.type === 'prompt') {
            addPrompt(data.item);
          }
          break;
        case 'vote':
          updateVotes(data.id, data.votes);
          break;
        case 'prompt_won':
          handlePromptWon(data.id);
          break;
        case 'prompt_clean_updated':
          updatePromptClean(data.id);
          break;
        case 'auto_approved_actions':
          handleActionApproved(data.id);
          break;
        case 'finished':
          handleActionFinished(data.id);
          break;
        case 'prompts_moved_to_history':
          refreshData();
          break;
        case 'poll_started':
          handlePollStarted(data);
          break;
        case 'poll_ended':
          handlePollEnded(data);
          break;
        default:
          console.log('Unhandled message type:', data.type);
      }
    }

    // Data management functions
    async function refreshData() {
      try {
        const [statsRes, timelineRes, capabilitiesRes, contributorsRes, currentRes] = await Promise.all([
          fetch(BASE_URL + '/dashboard/stats'),
          fetch(BASE_URL + '/dashboard/timeline'),
          fetch(BASE_URL + '/dashboard/capabilities'),
          fetch(BASE_URL + '/dashboard/contributors'),
          fetch(BASE_URL + '/dashboard/current')
        ]);

        const statsData = await statsRes.json();
        const timelineData = await timelineRes.json();
        const capabilitiesData = await capabilitiesRes.json();
        const contributorsData = await contributorsRes.json();
        const currentData = await currentRes.json();

        // Update dashboard data
        dashboardData.stats = statsData;
        dashboardData.timeline = timelineData.timeline || [];
        dashboardData.capabilities = capabilitiesData.capabilities || [];
        dashboardData.contributors = contributorsData.contributors || [];
        dashboardData.current = currentData.current;

        // Also fetch basic data for prompts display
        const [queueRes, historyRes] = await Promise.all([
          fetch(BASE_URL + '/queue'),
          fetch(BASE_URL + '/history')
        ]);

        const queueData = await queueRes.json();
        const historyData = await historyRes.json();

        dashboardData.prompts = queueData.filter(item => item.type === 'prompt');
        dashboardData.history = historyData;

        updatePromptsList();
        updateStats();
        updateEvolutionTimeline();
        updateCapabilities();
        updateContributors();
        updateCurrentProcessing();
      } catch (error) {
        console.error('Error refreshing data:', error);
      }
    }

    function updateStats() {
      if (dashboardData.stats) {
        document.getElementById('stat-prompts').textContent = dashboardData.stats.total_prompts;
        document.getElementById('stat-winners').textContent = dashboardData.stats.winners;
        document.getElementById('stat-contributors').textContent = dashboardData.stats.contributors;
        document.getElementById('stat-actions').textContent = dashboardData.stats.actions_executed;
        document.getElementById('stat-uptime').textContent = dashboardData.stats.uptime;
      } else {
        // Fallback to old calculation
        const totalPrompts = dashboardData.prompts.length + dashboardData.history.length;
        const winners = dashboardData.history.filter(item => item.outcome === 'won').length;
        const contributors = new Set([
          ...dashboardData.prompts.map(p => p.user),
          ...dashboardData.history.map(h => h.user)
        ]).size;

        document.getElementById('stat-prompts').textContent = totalPrompts;
        document.getElementById('stat-winners').textContent = winners;
        document.getElementById('stat-contributors').textContent = contributors;
        document.getElementById('stat-actions').textContent = winners;
        document.getElementById('stat-uptime').textContent = '0m';
      }
    }

    function addPrompt(prompt) {
      dashboardData.prompts.unshift(prompt);
      updatePromptsList();
      updateStats();

      // Add to chat as well
      addChatMessage('system', `ðŸ“ ${prompt.user} submitted: "${prompt.text}"`);
    }

    function updatePromptsList() {
      const container = document.getElementById('prompts-list');
      container.innerHTML = '';

      if (dashboardData.prompts.length === 0) {
        container.innerHTML = '<div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">No prompts yet...</div>';
        return;
      }

      dashboardData.prompts.slice(0, 10).forEach(prompt => {
        const item = createPromptElement(prompt);
        container.appendChild(item);
      });
    }

    function createPromptElement(prompt) {
      const item = document.createElement('div');
      item.className = 'prompt-item fade-in';

      const timeAgo = formatTimeAgo(Date.now() / 1000);

      item.innerHTML = `
        <div class="prompt-avatar">${prompt.user.charAt(0).toUpperCase()}</div>
        <div class="prompt-content">
          <div class="prompt-text">${escapeHtml(prompt.text)}</div>
          <div class="prompt-meta">
            <span class="prompt-user">@${prompt.user}</span>
            <span class="prompt-time">${timeAgo}</span>
          </div>
        </div>
      `;

      return item;
    }

    function updateEvolutionTimeline() {
      const container = document.getElementById('evolution-timeline');
      container.innerHTML = '';

      // Use timeline data from API if available, otherwise fallback to history
      const timelineEvents = dashboardData.timeline.length > 0 ? dashboardData.timeline : dashboardData.history.slice(-10).reverse();

      if (timelineEvents.length === 0) {
        container.innerHTML = '<div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">No evolution events yet...</div>';
        return;
      }

      timelineEvents.forEach(item => {
        const timelineItem = createTimelineElement(item);
        container.appendChild(timelineItem);
      });
    }

    function createTimelineElement(item) {
      const itemEl = document.createElement('div');
      itemEl.className = `timeline-item ${item.status || 'pending'} fade-in`;

      let statusText = 'Pending';
      let statusClass = '';
      let title = '';
      let content = '';
      let meta = '';

      // Handle different timeline event types
      if (item.type === 'prompt_submitted') {
        title = `"${item.text}"`;
        content = `by @${item.user}`;
        meta = formatTimeAgo(item.timestamp || Date.now() / 1000);

        if (item.status === 'won') {
          statusText = 'Winner';
          statusClass = 'completed';
        } else if (item.status === 'processed') {
          statusText = 'Processed';
          statusClass = 'completed';
        } else {
          statusText = 'Voting';
          statusClass = 'processing';
        }
      } else if (item.type === 'action_generated') {
        title = `Action #${item.id}`;
        content = `Generated: ${item.code}`;
        meta = formatTimeAgo(item.timestamp || Date.now() / 1000);
        statusText = 'Generated';
        statusClass = 'completed';
      } else {
        // Fallback for old format
        title = item.text || item.code || 'Unknown';
        content = item.user ? `by @${item.user}` : '';
        meta = formatTimeAgo(item.processed_at || Date.now() / 1000);

        if (item.outcome === 'won') {
          statusText = 'Winner';
          statusClass = 'completed';
        } else if (item.status === 'processed') {
          statusText = 'Processed';
          statusClass = 'completed';
        } else if (item.status === 'rejected') {
          statusText = 'Rejected';
          statusClass = 'failed';
        }
      }

      itemEl.innerHTML = `
        <div class="timeline-header">
          <div class="timeline-title">${escapeHtml(title)}</div>
          <div class="timeline-status ${statusClass}">${statusText}</div>
        </div>
        <div class="timeline-content">${content}</div>
        <div class="timeline-meta">${meta}</div>
      `;

      return itemEl;
    }

    function updateCapabilities() {
      const container = document.getElementById('capability-tree');
      const existingCapabilities = container.querySelector('.capability-item.root + div');

      if (existingCapabilities) {
        existingCapabilities.remove();
      }

      const capabilitiesDiv = document.createElement('div');

      // Use API capabilities if available, otherwise fallback to extracted capabilities
      const capabilities = dashboardData.capabilities.length > 0 ? dashboardData.capabilities : extractCapabilitiesFromHistory();

      if (capabilities.length === 0) {
        capabilitiesDiv.innerHTML = '<div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px; margin-top: 16px;">No capabilities yet...</div>';
      } else {
        capabilities.forEach((capability, index) => {
          const capabilityItem = document.createElement('div');
          capabilityItem.className = 'capability-item';

          const emoji = capability.emoji || getCapabilityEmoji(capability.type);
          const name = capability.type || capability.name;
          const count = capability.count || 1;

          capabilityItem.innerHTML = `
            <div class="capability-name">
              ${emoji} ${name}
              <span class="capability-badge">${count}</span>
            </div>
            <div class="capability-desc">Capabilities extracted from winning prompts</div>
          `;

          capabilitiesDiv.appendChild(capabilityItem);
        });
      }

      container.appendChild(capabilitiesDiv);
    }

    function extractCapabilitiesFromHistory() {
      const winners = dashboardData.history.filter(item => item.outcome === 'won');
      const capabilities = [];

      winners.forEach((winner, index) => {
        const capabilityName = extractCapabilityName(winner.text || '');
        const emoji = getCapabilityEmoji(capabilityName);

        capabilities.push({
          type: capabilityName,
          emoji: emoji,
          count: 1,
          prompts: [{id: winner.id, text: winner.text, user: winner.user}]
        });
      });

      return capabilities;
    }

    function updateCurrentProcessing() {
      const container = document.getElementById('current-processing');
      container.innerHTML = '';

      if (dashboardData.current) {
        const current = dashboardData.current;
        const item = document.createElement('div');
        item.className = 'timeline-item processing fade-in';

        let title = '';
        let content = '';

        if (current.stage === 'cleaning') {
          title = `"${current.text}"`;
          content = `ðŸ§¹ Cleaning: "${current.text}"<br>ðŸ”„ AI Processing: Preparing prompt for AI bridge...`;
        } else if (current.stage === 'ai_processing') {
          title = `Action #${current.action_id}`;
          content = `ðŸ”„ AI Processing: Generating actions from prompt...<br>âš¡ Generated: ${current.code}`;
        }

        item.innerHTML = `
          <div class="timeline-header">
            <div class="timeline-title">${title}</div>
            <div class="timeline-status processing">Processing</div>
          </div>
          <div class="timeline-content">${content}</div>
          <div class="timeline-meta">Live â€¢ In Progress</div>
        `;

        container.appendChild(item);
      } else {
        container.innerHTML = '<div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">No current processing</div>';
      }
    }

    function updateContributors() {
      const container = document.getElementById('contributors-list');
      container.innerHTML = '';

      // Use API contributors if available, otherwise fallback to calculation
      const contributors = dashboardData.contributors.length > 0 ? dashboardData.contributors : calculateContributorsFallback();

      if (contributors.length === 0) {
        container.innerHTML = '<div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">No contributors yet...</div>';
        return;
      }

      contributors.forEach((contributor, index) => {
        const item = document.createElement('div');
        item.className = 'contributor-item fade-in';

        const rank = index + 1;
        const rankEmoji = rank === 1 ? 'ðŸ¥‡' : rank === 2 ? 'ðŸ¥ˆ' : rank === 3 ? 'ðŸ¥‰' : 'ðŸ…';

        item.innerHTML = `
          <div class="contributor-rank">${rank}</div>
          <div class="contributor-info">
            <div class="contributor-name">${rankEmoji} @${contributor.user}</div>
            <div class="contributor-stats">
              ${contributor.wins || 0} wins, ${contributor.prompts || 0} prompts
            </div>
          </div>
          <div class="contributor-badge">Top ${Math.min(rank, 3)}</div>
        `;

        container.appendChild(item);
      });
    }

    function calculateContributorsFallback() {
      const contributorStats = {};

      [...dashboardData.prompts, ...dashboardData.history].forEach(item => {
        if (!contributorStats[item.user]) {
          contributorStats[item.user] = { prompts: 0, wins: 0, votes: 0 };
        }
        contributorStats[item.user].prompts++;

        if (item.outcome === 'won') {
          contributorStats[item.user].wins++;
        }
      });

      return Object.entries(contributorStats)
        .map(([user, stats]) => ({ user, ...stats }))
        .sort((a, b) => b.wins - a.wins || b.prompts - a.prompts)
        .slice(0, 10);
    }

    function addChatMessage(user, text) {
      const container = document.getElementById('chat-messages');
      const messageDiv = document.createElement('div');
      messageDiv.className = 'chat-message fade-in';

      messageDiv.innerHTML = `
        <span class="chat-user">@${user}:</span>
        <span class="chat-text">${escapeHtml(text)}</span>
      `;

      container.appendChild(messageDiv);

      // Keep only last 20 messages
      while (container.children.length > 20) {
        container.removeChild(container.firstChild);
      }

      // Auto scroll to bottom
      container.scrollTop = container.scrollHeight;
    }

    function updateVotes(promptId, votes) {
      // Update voting display if there's an active poll
      const votingPanel = document.getElementById('voting-panel');
      if (votingPanel && dashboardData.currentPoll) {
        // This would need to be implemented based on the actual voting system
        console.log('Votes updated:', promptId, votes);
      }
    }

    function handlePromptWon(promptId) {
      console.log('Prompt won:', promptId);
      refreshData();

      // Find the winning prompt and show in chat
      const winner = [...dashboardData.prompts, ...dashboardData.history].find(p => p.id === promptId);
      if (winner) {
        addChatMessage('system', `ðŸ† WINNER: @${winner.user} - "${winner.text}"`);
      }
    }

    function handleActionApproved(actionId) {
      addChatMessage('system', `âœ… Action #${actionId} approved and queued for execution`);
    }

    function handleActionFinished(actionId) {
      addChatMessage('system', `ðŸŽ¯ Action #${actionId} executed successfully`);
    }

    function handlePollStarted(data) {
      console.log('Poll started:', data);
      dashboardData.currentPoll = data;
      updateVotingPanel();

      addChatMessage('system', `ðŸ—³ï¸ NEW POLL STARTED! Vote with !1, !2, !3, !4, or !5`);
      data.options.forEach((option, index) => {
        addChatMessage('system', `${index + 1}) ${option.user}: ${option.text}`);
      });
      addChatMessage('system', `â° You have 15 seconds to vote!`);
    }

    function handlePollEnded(data) {
      console.log('Poll ended:', data);
      dashboardData.currentPoll = null;
      updateVotingPanel();

      if (data.winner) {
        addChatMessage('system', `ðŸ† POLL WINNER: @${data.winner.user} - "${data.winner.text}"`);
      }
    }

    function updateVotingPanel() {
      const container = document.getElementById('voting-panel');
      container.innerHTML = '';

      if (dashboardData.currentPoll) {
        const poll = dashboardData.currentPoll;
        const pollItem = document.createElement('div');
        pollItem.className = 'poll-item fade-in';

        const timeLeft = Math.max(0, 15 - Math.floor((Date.now() - poll.startTime) / 1000));

        pollItem.innerHTML = `
          <div class="poll-header">
            <div class="poll-title">Live Poll in Progress</div>
            <div class="poll-timer">${timeLeft}s</div>
          </div>
          <div class="poll-options">
            ${poll.options.map((option, index) => `
              <div class="poll-option" data-index="${index}">
                <div style="font-weight: 600; color: var(--text);">${index + 1}</div>
                <div style="flex: 1;">
                  <div style="font-size: 12px; color: var(--accent);">@${option.user}</div>
                  <div style="font-size: 11px; color: var(--muted);">${option.text}</div>
                </div>
                <div class="poll-votes" id="votes-${index}">0</div>
              </div>
            `).join('')}
          </div>
        `;

        container.appendChild(pollItem);

        // Update votes every second
        if (poll.votes) {
          Object.entries(poll.votes).forEach(([index, count]) => {
            const votesEl = document.getElementById(`votes-${index}`);
            if (votesEl) votesEl.textContent = count;
          });
        }
      } else {
        container.innerHTML = '<div style="color: var(--muted); font-style: italic; text-align: center; padding: 20px;">No active polls</div>';
      }
    }

    // Utility functions
    function formatTimeAgo(timestamp) {
      const now = Date.now() / 1000;
      const diff = now - timestamp;

      if (diff < 60) return 'just now';
      if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
      if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
      return `${Math.floor(diff / 86400)}d ago`;
    }

    function escapeHtml(text) {
      const div = document.createElement('div');
      div.textContent = text;
      return div.innerHTML;
    }

    function extractCapabilityName(text) {
      // Simple extraction - could be improved with NLP
      text = text.toLowerCase();
      if (text.includes('click') || text.includes('mouse')) return 'Mouse Control';
      if (text.includes('type') || text.includes('text') || text.includes('keyboard')) return 'Text Input';
      if (text.includes('file') || text.includes('write') || text.includes('read')) return 'File I/O';
      if (text.includes('wait') || text.includes('delay') || text.includes('time')) return 'System Control';
      return 'General Action';
    }

    function getCapabilityEmoji(name) {
      switch (name) {
        case 'Mouse Control': return 'ðŸ–±ï¸';
        case 'Text Input': return 'âŒ¨ï¸';
        case 'File I/O': return 'ðŸ“';
        case 'System Control': return 'ðŸ”§';
        default: return 'âš¡';
      }
    }

    // Chat input handling
    function handleChatSubmit() {
      const input = document.getElementById('chat-input');
      const message = input.value.trim();

      if (message) {
        addChatMessage('you', message);
        input.value = '';

        // In a real implementation, this would send to backend
        console.log('Chat message:', message);
      }
    }

    document.getElementById('chat-input').addEventListener('keypress', function(e) {
      if (e.key === 'Enter') {
        handleChatSubmit();
      }
    });

    // Initialize dashboard
    function init() {
      console.log('Initializing dashboard...');
      initWebSocket();
      refreshData();

      // Refresh data every 5 seconds
      setInterval(refreshData, 5000);
    }

    // Demo functions for testing
    function demoPoll() {
      const demoOptions = [
        { user: 'alice', text: 'Make it click buttons' },
        { user: 'bob', text: 'Type some text' },
        { user: 'charlie', text: 'Open calculator' },
        { user: 'diana', text: 'Take a screenshot' },
        { user: 'eve', text: 'Move the mouse smoothly' }
      ];

      const pollData = {
        options: demoOptions,
        startTime: Date.now(),
        votes: { 0: 0, 1: 0, 2: 0, 3: 0, 4: 0 }
      };

      handlePollStarted(pollData);

      // Simulate votes over time
      let voteCount = 0;
      const voteInterval = setInterval(() => {
        if (voteCount < 20) {
          const randomOption = Math.floor(Math.random() * 5);
          pollData.votes[randomOption] = (pollData.votes[randomOption] || 0) + 1;
          updateVotingPanel();
          voteCount++;
        } else {
          clearInterval(voteInterval);
          // End poll after 15 seconds
          setTimeout(() => {
            const maxVotes = Math.max(...Object.values(pollData.votes));
            const winnerIndex = Object.keys(pollData.votes).find(key => pollData.votes[key] === maxVotes);
            const winner = demoOptions[winnerIndex];

            handlePollEnded({ winner });
          }, 2000);
        }
      }, 500);
    }

    // Add demo button (uncomment to enable)
    // document.addEventListener('DOMContentLoaded', () => {
    //   setTimeout(demoPoll, 2000); // Start demo after 2 seconds
    // });

    // Start when page loads
    document.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>
    """


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_updated:app", host="0.0.0.0", port=8000, reload=True)
