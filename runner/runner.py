import os, time, threading, requests, traceback
import re
import pyautogui as pag
from ast_allowlist import validate_snippet

BACKEND = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8000")

pag.FAILSAFE = True

class Agent:
    def move(self, x: int, y: int, duration:float=0.2):
        pag.moveTo(int(x), int(y), duration=max(0.0, float(duration)))
    def click(self, button:str="left", clicks:int=1, interval:float=0.1):
        pag.click(button=button, clicks=int(clicks), interval=max(0.0, float(interval)))
    def type(self, text:str, interval:float=0.03):
        pag.write(str(text), interval=max(0.0, float(interval)))
    def hotkey(self, *keys):
        pag.hotkey(*[str(k) for k in keys])
    def wait(self, seconds:float=0.5):
        time.sleep(min(max(0.0, float(seconds)), 2.0))
    # Safe primitives (no GUI launching)
    def log(self, text:str):
        print(str(text))
    def write_output(self, text:str):
        try:
            with open("agent_output.txt", "a", encoding="utf-8") as f:
                f.write(str(text) + "\n")
        except Exception:
            pass
    def broadcast(self, text:str):
        try:
            requests.post(f"{BACKEND}/event", json={"type":"runner_msg","id":0,"text":str(text)}, timeout=3)
        except Exception:
            pass

agent = Agent()

BANNED_PATTERNS = [
    r'(?<!\.)\bopen\(',   # bans plain open( ... ) not obj.open_(...)
    r'\bimport\b',
    r'\bexec\(',
    r'\beval\(',
    r'__',                # dunders
    r'\bos\.',            # os.*
    r'\bsys\.',           # sys.*
    r'\bsubprocess\b',
    r'\bopen_app\b',      # remove Notepad GUI behavior entirely
]

def looks_safe(code: str) -> bool:
    c = (code or "")
    lc = c.lower()
    return not any(re.search(p, lc) for p in BANNED_PATTERNS)

def execute_snippet(code: str, timeout_s: float = 5.0) -> bool:
    # Normalize smart quotes from mobile keyboards
    code = (code or "").replace("“", '"').replace("”", '"').replace("’", "'")

    # Precheck for banned tokens
    if not looks_safe(code):
        print("🚫 Banned token detected before execution.")
        return False

    # Compile first so SyntaxError is caught cleanly
    try:
        code_obj = compile(code, "<snippet>", "exec")
    except SyntaxError as e:
        print(f"❌ SyntaxError: {e}")
        return False

    success = {"ok": True}

    def run():
        try:
            exec(code_obj, {"agent": agent}, {})
        except Exception:
            print("❌ Exception while running snippet:")
            print(traceback.format_exc())
            success["ok"] = False

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        print("⏱️  Snippet timed out")
        return False
    return success["ok"]

def execute_snippet_subproc(code: str, timeout_s: float = 6.0) -> bool:
    try:
        validate_snippet(code)  # parses AST; rejects anything not agent.*
    except ValueError as e:
        print(f"🚫 Validation failed: {e}")
        return False
    # spawn worker_subproc.py and feed code via stdin; kill on timeout
    # (use the snippet I gave you earlier)
    return True  # Placeholder - implement subprocess execution

def poll_loop():
    print(f" Runner polling {BACKEND}/approved/next")
    while True:
        try:
            sub = requests.get(f"{BACKEND}/approved/next", timeout=10).json()
            if sub and sub.get("code"):
                sid, code, user = sub["id"], sub["code"], sub.get("user")
                print(f"\n EXECUTING #{sid} from {user}: {code}")
                ok = execute_snippet(code, timeout_s=5.0)
                if ok:
                    print(f"Finished #{sid}")
                else:
                    print(f"Failed #{sid}")
            time.sleep(0.5)
        except Exception as e:
            print("Runner error:", repr(e))

if __name__ == "__main__":
    poll_loop()