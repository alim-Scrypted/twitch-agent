Param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

# Move to repo root (parent of scripts directory)
Set-Location (Join-Path $PSScriptRoot "..")

# Resolve a working Python executable
$pyExe = $PythonExe
$pyArgs = @()
if (-not (Get-Command $pyExe -ErrorAction SilentlyContinue)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $pyExe = "py"
        $pyArgs = @("-3")
    } elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
        $pyExe = "python3"
    } else {
        Write-Error "Python not found in PATH. Please install Python 3.10+ and re-run this script."
        exit 1
    }
}

if (-not (Test-Path .venv)) {
    & $pyExe @pyArgs -m venv .venv
}

$pip = Join-Path ".\.venv\Scripts" "pip.exe"
& $pip install --upgrade pip

if (Test-Path .\requirements.txt) {
    & $pip install -r .\requirements.txt
} else {
    & $pip install fastapi uvicorn[standard] pydantic python-dotenv requests twitchio==2.7.0 websockets
}

if (-not (Test-Path .\.env)) {
    $envTemplateLines = @(
        "ADMIN_KEY=changeme",
        "BACKEND_BASE_URL=http://127.0.0.1:8000",
        "",
        "# Twitch bot",
        "TWITCH_CHANNEL=your_channel",
        "TWITCH_BOT_USERNAME=your_bot_username",
        "TWITCH_OAUTH_TOKEN=oauth:xxxxxxxx",
        "",
        "# Optional services",
        "GRAMMAR_API_URL=",
        "GRAMMAR_API_KEY=",
        "AI_API_URL=",
        "AI_API_KEY="
    )
    $envTemplate = ($envTemplateLines -join [Environment]::NewLine)
    $envTemplate | Set-Content -NoNewline -Path .\.env -Encoding UTF8
    Write-Host "Created .env with template values. Edit it before running."
} else {
    Write-Host '.env already exists - not overwriting.'
}

Write-Host 'Setup complete. To activate: .\.venv\Scripts\Activate.ps1'

