import os, time, requests

BACKEND = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8000")

def synthesize_actions(prompt: str) -> str:
    p = prompt.lower()
    # Safe default: just log and write output; no GUI app launching
    text = prompt.replace('"', "'")[:200]
    return 'agent.log("Prompt received"); agent.write_output("' + text + '")'

def call_ai_for_actions(prompt: str) -> str:
    url = os.getenv("AI_API_URL")
    key = os.getenv("AI_API_KEY")
    if url:
        try:
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            r = requests.post(url, json={"prompt": prompt}, headers=headers, timeout=20)
            if r.ok:
                j = r.json()
                return j.get("actions") or j.get("code") or j.get("text") or synthesize_actions(prompt)
            else:
                print("AI API error:", r.status_code, r.text[:200])
        except Exception as e:
            print("AI API exception:", repr(e))
    return synthesize_actions(prompt)

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
                actions = call_ai_for_actions(text)
                # Ensure actions do not reference GUI apps; prefer safe primitives
                # If upstream returns unsafe verbs, fall back to synthesize_actions
                if any(x in (actions or "").lower() for x in ["open_app", "notepad", "calc", "subprocess", "import", "exec(", "eval("]):
                    actions = synthesize_actions(text)
                r = requests.post(
                    f"{BACKEND}/submit",
                    json={"user": "orchestrator", "type": "actions", "code": actions},
                    timeout=5
                )
                if r.ok:
                    aid = r.json().get("id")
                    print(f"➡️  Submitted actions as item #{aid}. Auto-approved if safe.")
                else:
                    print("❌ Failed to submit actions:", r.text)
            time.sleep(0.5)
        except Exception as e:
            print("orchestrator error:", repr(e))
            time.sleep(1.0)


if __name__ == "__main__":
    main()
