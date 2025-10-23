Param(
    [string]$Channel = "",
    [string]$BotUsername = "",
    [string]$OAuthToken = ""
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if ($Channel) { $env:TWITCH_CHANNEL = $Channel }
if ($BotUsername) { $env:TWITCH_BOT_USERNAME = $BotUsername }
if ($OAuthToken) { $env:TWITCH_OAUTH_TOKEN = $OAuthToken }

$python = ".\.venv-bot\Scripts\python.exe"
& $python -m bot.main

