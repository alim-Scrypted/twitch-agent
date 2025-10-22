# runner/worker_subproc.py
import sys, time, traceback
import pyautogui as pag

pag.FAILSAFE = True

class Agent:
    def move(self, x:int, y:int, duration:float=0.2): pag.moveTo(int(x), int(y), duration=max(0.0, float(duration)))
    def click(self, button:str="left", clicks:int=1, interval:float=0.1): pag.click(button=button, clicks=int(clicks), interval=max(0.0, float(interval)))
    def type(self, text:str, interval:float=0.03): pag.write(str(text), interval=max(0.0, float(interval)))
    def hotkey(self, *keys): pag.hotkey(*[str(k) for k in keys])
    def wait(self, seconds:float=0.5): time.sleep(min(max(0.0, float(seconds)), 2.0))
    # Safe primitives only (no GUI app launching)
    def log(self, text:str):
        print(str(text))
    def write_output(self, text:str):
        try:
            with open("agent_output.txt", "a", encoding="utf-8") as f:
                f.write(str(text)+"\n")
        except Exception:
            pass

agent = Agent()

def main():
    # code arrives via stdin to avoid shell quoting pitfalls
    code = sys.stdin.read()
    code = code.replace("“", '"').replace("”", '"').replace("’", "'")
    try:
        compiled = compile(code, "<snippet>", "exec")
        exec(compiled, {"agent": agent}, {})
    except Exception:
        print("TRACEBACK-BEGIN")
        print(traceback.format_exc())
        print("TRACEBACK-END")
        sys.exit(1)

if __name__ == "__main__":
    main()
