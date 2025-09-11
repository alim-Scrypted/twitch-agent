import os, time, requests

BACKEND = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8000")

def synthesize_actions(prompt: str) -> str:
    p = prompt.lower()
    # Simple mappings; replace with an LLM later
    if "notepad" in p or "note" in p:
        text = prompt.replace('"', "'")[:120]
        return 'agent.open_app("notepad"); agent.wait(0.5); agent.type("' + text + '")'
    if "calc" in p or "calculator" in p:
        return 'agent.open_app("calc"); agent.wait(0.5)'
    # Default: open Notepad and type the prompt text
    text = prompt.replace('"', "'")[:120]
    return 'agent.open_app("notepad"); agent.wait(0.4); agent.type("' + text + '")'

def main():
    print("🧠 Orchestrator polling for approved prompts...")
    while True:
        try:
            # Drain all currently approved prompts; keep only the newest (last)
            latest = None
            while True:
                sub = requests.get(f"{BACKEND}/approved/prompt/next", timeout=10).json()
                if not sub or not sub.get("text"):
                    break
                latest = sub
            if latest:
                pid, text, user = latest["id"], latest["text"], latest.get("user")
                print(f"\n🪄 Generating actions for prompt #{pid} from {user}: {text}")
                actions = synthesize_actions(text)
                r = requests.post(
                    f"{BACKEND}/submit",
                    json={"user": "orchestrator", "type": "actions", "code": actions},
                    timeout=5
                )
                if r.ok:
                    aid = r.json().get("id")
                    print(f"➡️  Submitted actions as item #{aid}. Approve to execute.")
                else:
                    print("❌ Failed to submit actions:", r.text)
            time.sleep(0.5)
        except Exception as e:
            print("orchestrator error:", repr(e))
            time.sleep(1.0)


if __name__ == "__main__":
    main()
