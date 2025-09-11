import sqlite3, os
DB = os.path.join(os.path.dirname(__file__), "agent.db")

def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init():
    with conn() as cx:
        cx.executescript("""
        CREATE TABLE IF NOT EXISTS submissions(
          id INTEGER PRIMARY KEY,
          user TEXT, type TEXT, text TEXT, code TEXT,
          status TEXT, votes INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS votes(
          submission_id INTEGER, user TEXT, PRIMARY KEY(submission_id, user)
        );
        """)
