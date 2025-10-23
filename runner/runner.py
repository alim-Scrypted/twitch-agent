import os, time, json, traceback, re, sys
from pathlib import Path
import requests
from dotenv import load_dotenv
from ast_allowlist import validate_snippet

load_dotenv()
BACKEND = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8000")

OUT_DIR = Path(__file__).resolve().parent / "output"
OUT_DIR.mkdir(exist_ok=True)

class Agent:
    def log(self, msg: str):
        print(f"[agent] {msg}")
    def write_output(self, filename: str, content: str):
        name = "".join(ch for ch in filename if ch.isalnum() or ch in ("-","_","."))
        p = OUT_DIR / name
        p.write_text(str(content), encoding="utf-8")
        print(f"[agent] wrote {p.name}")
    def broadcast(self, backend: str, event: str, sid: int | None = None):
        try:
            requests.post(backend + "/event", json={"type": event, "id": sid or -1}, timeout=2)
        except Exception:
            pass

agent = Agent()

BANNED_PATTERNS = [
    r'(?<!\.)\bopen\(',
    r'\bimport\b',
    r'\bexec\(',
    r'\beval\(',
    r'__',
    r'\bos\.',
    r'\bsys\.',
    r'\bsubprocess\b'
]

def looks_safe(code: str) -> bool:
    lc = (code or "").lower()
    return not any(re.search(p, lc) for p in BANNED_PATTERNS)

def execute_snippet(code: str, sid: int, timeout_s: float = 6.0) -> bool:
    if not looks_safe(code):
        print("🚫 Banned token detected before execution.")
        return False
    try:
        compiled = compile(code, "<snippet>", "exec")
    except SyntaxError as e:
        print(f"❌ SyntaxError: {e}")
        return False
    ok = {"v": True}
    def run():
        try:
            exec(compiled, {"agent": agent}, {})
        except Exception:
            print("❌ Exception:\n" + traceback.format_exc())
            ok["v"] = False
    import threading
    t = threading.Thread(target=run, daemon=True); t.start(); t.join(timeout_s)
    if t.is_alive():
        print("⏱️  Timed out")
        return False
    return ok["v"]

def execute_snippet_subproc(code: str, timeout_s: float = 6.0) -> bool:
    try:
        validate_snippet(code)  # parses AST; rejects anything not agent.*
    except ValueError as e:
        print(f"🚫 Validation failed: {e}")
        return False

    import subprocess
    process = None
    try:
        # Spawn worker_subproc.py and feed code via stdin
        process = subprocess.Popen(
            [sys.executable, "worker_subproc.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=Path(__file__).resolve().parent
        )

        # Send code to subprocess via stdin and close it
        stdout, stderr = process.communicate(code, timeout=timeout_s)

        if process.returncode == 0:
            print(f"✅ Subprocess execution successful")
            if stdout:
                print(f"Output: {stdout}")
            return True
        else:
            print(f"❌ Subprocess execution failed (exit code {process.returncode})")
            if stderr:
                print(f"Error: {stderr}")
            return False

    except subprocess.TimeoutExpired:
        print(f"⏱️ Subprocess timed out after {timeout_s}s")
        if process:
            process.kill()
        return False
    except Exception as e:
        print(f"❌ Subprocess execution error: {e}")
        return False

def poll_loop():
    print(f" Runner polling {BACKEND}/approved/next")
    while True:
        try:
            sub = requests.get(f"{BACKEND}/approved/next", timeout=10).json()
            if sub and sub.get("code"):
                sid, code, user = sub["id"], sub["code"], sub.get("user")
                print(f"\n EXECUTING #{sid} from {user}: {code}")
                ok = execute_snippet_subproc(code, 6.0)
                if ok:
                    print(f"✅ FINISHED #{sid}")
                    agent.broadcast(BACKEND, "finished", sid)
                else:
                    print(f"❌ FAILED #{sid}")
            time.sleep(0.5)
        except Exception as e:
            print("Runner error:", repr(e))

if __name__ == "__main__":
    poll_loop()