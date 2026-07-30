# tg_send.ps1 -- ручна/скриптова відправка в Telegram (docs/telegram-notify.md §6.3).
# Використання:  .\scripts\tg_send.ps1 "текст" [шлях_до_фото]
# Секрети: .secrets/telegram.env (TG_BOT_TOKEN, TG_CHAT_ID). Токен живе лише
# у змінній процесу; URL з токеном НІКОЛИ не друкується (жодного -Verbose/echo).
# Exit code: 0 -- відправлено, 1 -- збій/нема конфігу.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Text,
    [Parameter(Position = 1)][string]$PhotoPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)  # project/scripts -> repo root
$envFile = Join-Path $repoRoot '.secrets\telegram.env'

if (-not (Test-Path $envFile)) {
    Write-Error "нема $envFile — міст не налаштовано"
    exit 1
}

# Парсер KEY=VALUE (без сторонніх модулів); значення не друкуються.
$kv = @{}
foreach ($line in Get-Content $envFile -Encoding UTF8) {
    $line = $line.Trim()
    if ($line -eq '' -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
    if ($line.StartsWith('export ')) { $line = $line.Substring(7) }
    $idx = $line.IndexOf('=')
    $kv[$line.Substring(0, $idx).Trim()] = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
}

$token = $kv['TG_BOT_TOKEN']
$chatId = $kv['TG_CHAT_ID']
if (-not $token -or -not $chatId) {
    Write-Error 'TG_BOT_TOKEN/TG_CHAT_ID порожні — запусти bootstrap'
    exit 1
}

try {
    if ($PhotoPath) {
        if (-not (Test-Path $PhotoPath)) { Write-Error "нема файла: $PhotoPath"; exit 1 }
        $form = Get-Command Invoke-RestMethod | Select-Object -ExpandProperty Parameters
        if ($form.ContainsKey('Form')) {
            # PowerShell 7+: multipart через -Form
            $uri = "https://api.telegram.org/bot$token/sendPhoto"
            $null = Invoke-RestMethod -Uri $uri -Method Post -TimeoutSec 10 -Verbose:$false -Form @{
                chat_id = $chatId
                caption = $Text.Substring(0, [Math]::Min(1024, $Text.Length))
                photo   = Get-Item $PhotoPath
            }
        }
        else {
            # PowerShell 5.1 без -Form -> фолбек на python-модуль
            Push-Location $repoRoot
            try {
                & "$repoRoot\project\.venv\Scripts\python.exe" -m bot2048.tgnotify send $Text $PhotoPath --root $repoRoot
                if ($LASTEXITCODE -ne 0) { exit 1 }
            }
            finally { Pop-Location }
        }
    }
    else {
        $uri = "https://api.telegram.org/bot$token/sendMessage"
        $null = Invoke-RestMethod -Uri $uri -Method Post -TimeoutSec 10 -Verbose:$false -Body @{
            chat_id = $chatId
            text    = $Text.Substring(0, [Math]::Min(4096, $Text.Length))
        }
    }
    Write-Host 'відправлено'
    exit 0
}
catch {
    # Помилка БЕЗ URL (в $_ може бути повний URI з токеном — не друкуємо його).
    Write-Error ("збій відправки: " + $_.Exception.GetType().Name)
    exit 1
}
