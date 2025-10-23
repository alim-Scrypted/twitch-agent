# twitch-agent

## Quickstart (Windows, PowerShell)

1) Setup environment and dependencies

```powershell
cd C:\twitch-agent
./scripts/setup.ps1      # creates .venv, installs deps, creates .env if missing
```

2) Edit `.env` in the repo root. Required:

- ADMIN_KEY=changeme
- BACKEND_BASE_URL=http://127.0.0.1:8000
- TWITCH_CHANNEL=your_channel
- TWITCH_BOT_USERNAME=your_bot_username
- TWITCH_OAUTH_TOKEN=oauth:xxxxxxxx

Optional:

- GRAMMAR_API_URL / GRAMMAR_API_KEY
- AI_API_URL / AI_API_KEY

3) Start services in separate terminals

Terminal A — Backend (FastAPI)

```powershell
./scripts/run-backend.ps1 -Port 8000 -AdminKey (Get-Content .env | Select-String -Pattern '^ADMIN_KEY=' | ForEach-Object { ($_ -split '=')[1] })
```

Open `http://127.0.0.1:8000/panel`, click “Set Admin Key”, and paste the same ADMIN_KEY.

Terminal B — Twitch Bot

```powershell
./scripts/run-bot.ps1
```

Terminal C — Orchestrator

```powershell
./scripts/run-orchestrator.ps1
```

Terminal D — Runner

```powershell
./scripts/run-runner.ps1
```

## How it works

- View moderation panel at `/panel`.
- Chat submits `!prompt your idea` in Twitch chat.
- After 5 queued prompts, bot runs a 15s poll (`!1..!5`).
- Winner is marked; orchestrator generates safe actions and submits.
- Runner executes approved `agent.*` actions and writes files under `runner/output`.

## Troubleshooting

- 401 on moderator actions: ensure ADMIN_KEY is set for backend and in the panel.
- Bot login error: verify `TWITCH_OAUTH_TOKEN` starts with `oauth:` and channel/user are correct.
- Port in use: change backend port in `run-backend.ps1` and update `BACKEND_BASE_URL`.