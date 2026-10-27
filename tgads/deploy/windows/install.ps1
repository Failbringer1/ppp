<#
TGAds: install or update on a Windows server, next to other sites behind Caddy.

Run in PowerShell as Administrator (Windows PowerShell 5.1 or PowerShell 7):

  [Net.ServicePointManager]::SecurityProtocol = 'Tls12'
  & ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/Failbringer1/ppp/claude/telegram-channel-ads-platform-8qn7b8/tgads/deploy/windows/install.ps1'))) -ApiId 123 -ApiHash abc

What it does (everything lives in C:\Apps\TGAds, other apps are not touched):
  * private Python + dependencies, headless Chromium for TGStat, Claude Code CLI;
  * .env with a generated panel password (readable only by admins and the service);
  * scheduled task "TGAds" (LocalService, starts with Windows, restarts on failure)
    serving the panel on 127.0.0.1 only;
  * adds ONE site block for <ip-with-dashes>.sslip.io to the running Caddy's
    Caddyfile (backup first, `caddy validate`, rollback on error, then reload);
  * with -RunnerRepo owner/private-repo: registers a GitHub Actions self-hosted
    runner (service, SYSTEM, label "tgads-ops") so the server can be managed by
    pushing to that private repository. Never use a public repository for this.
Re-running it updates the code and dependencies; data, .env and Caddy block stay.
-Unattended skips every question (used when the runner updates TGAds itself).
#>
param(
    [string]$ApiId = '',
    [string]$ApiHash = '',
    [string]$Dir = 'C:\Apps\TGAds',
    [string]$Repo = 'https://github.com/Failbringer1/ppp.git',
    [string]$Branch = 'claude/telegram-channel-ads-platform-8qn7b8',
    [string]$HostName = '',
    [int]$Port = 8790,
    [switch]$NoCaddy,
    [switch]$NoBrowser,
    [switch]$NoClaude,
    [string]$RunnerRepo = '',
    [string]$RunnerToken = '',
    [string]$RunnerLabel = 'tgads-ops',
    [switch]$Unattended
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$TaskName = 'TGAds'
$PythonVersions = @('3.12.10', '3.12.9', '3.12.8')
$PlaywrightVersion = '1.55.0'
$LocalServiceSid = '*S-1-5-19'

function Step([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }

function Invoke-Native {
    # Native tools write progress to stderr; under 'Stop' Windows PowerShell would treat that as an error.
    param([string]$Exe, [string[]]$Arguments, [switch]$Quiet)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Quiet) { & $Exe @Arguments *> $null }
        else { & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" } }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
}

function Get-NativeOutput([string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { return (& $Exe @Arguments 2>$null | Out-String).Trim() } finally { $ErrorActionPreference = $old }
}

function Get-Status([string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 15 -MaximumRedirection 0
        return [int]$r.StatusCode
    } catch {
        $resp = $_.Exception.Response
        if ($resp) { return [int]$resp.StatusCode }
        return 0
    }
}

function Wait-Status([string]$Url, [int[]]$Ok, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $code = Get-Status $Url
        if ($Ok -contains $code) { return $code }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    return $code
}

$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Запустите PowerShell от имени администратора'
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Не найден git. Установите Git for Windows (https://git-scm.com) и запустите установку ещё раз.'
}

$Src = Join-Path $Dir 'src'
$App = Join-Path $Src 'tgads'
$Py = Join-Path $Dir 'python\python.exe'
$EnvFile = Join-Path $Dir '.env'
$Claude = Join-Path $Dir 'tools\claude.exe'
$Browsers = Join-Path $Dir 'browsers'
New-Item -ItemType Directory -Force -Path $Dir, "$Dir\data", "$Dir\logs", "$Dir\tools" | Out-Null

# ---------------------------------------------------------------- runner for Claude (first: if anything below fails,
# Claude can still reach the server and finish the job)
$runnerOk = $false
if ($RunnerRepo) {
    try {
        Step "Доступ для Claude: раннер GitHub Actions ($RunnerRepo, метка $RunnerLabel)"
        $RunnerDir = 'C:\actions-runner-claude'
        $existing = Get-Service -Name 'actions.runner.*' -ErrorAction SilentlyContinue |
            Where-Object { (Test-Path (Join-Path $RunnerDir '.runner')) }
        if ($existing) {
            Write-Host "    раннер уже установлен: $($existing.Name -join ', ') ($($existing.Status -join ', '))"
            $runnerOk = $true
        } else {
            if (-not $RunnerToken) {
                $gh = (Get-Command gh -ErrorAction SilentlyContinue | Select-Object -First 1).Source
                if (-not $gh) {
                    $gh = @('C:\Apps\SpreadEdge\tools\gh\bin\gh.exe', 'C:\Program Files\GitHub CLI\gh.exe') |
                        Where-Object { Test-Path $_ } | Select-Object -First 1
                }
                if ($gh) {
                    $RunnerToken = Get-NativeOutput $gh @('api', '-X', 'POST', "repos/$RunnerRepo/actions/runners/registration-token", '--jq', '.token')
                }
            }
            if (-not $RunnerToken -and -not $Unattended) {
                Write-Host "    Откройте https://github.com/$RunnerRepo/settings/actions/runners/new и скопируйте значение после --token в команде config"
                $RunnerToken = (Read-Host 'Токен раннера').Trim()
            }
            if (-not $RunnerToken) {
                Write-Warning 'Нет токена регистрации раннера — доступ для Claude не настроен.'
            } else {
                $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/actions/runner/releases/latest' -Headers @{ 'User-Agent' = 'tgads-installer' }
                $ver = $rel.tag_name.TrimStart('v')
                $zip = Join-Path $env:TEMP 'actions-runner.zip'
                Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/actions/runner/releases/download/v$ver/actions-runner-win-x64-$ver.zip" -OutFile $zip
                New-Item -ItemType Directory -Force -Path $RunnerDir | Out-Null
                Expand-Archive -Path $zip -DestinationPath $RunnerDir -Force
                Remove-Item -Force $zip
                Push-Location $RunnerDir
                try {
                    $code = Invoke-Native (Join-Path $RunnerDir 'config.cmd') @('--unattended', '--url', "https://github.com/$RunnerRepo",
                        '--token', $RunnerToken, '--name', "$env:COMPUTERNAME-claude", '--labels', $RunnerLabel,
                        '--runasservice', '--windowslogonaccount', 'NT AUTHORITY\SYSTEM', '--replace')
                } finally { Pop-Location }
                if ($code -eq 0) { $runnerOk = $true; Write-Host "    раннер $ver установлен как служба" }
                else { Write-Warning "Регистрация раннера не удалась (код $code)" }
            }
        }
    } catch {
        Write-Warning "Раннер не настроен: $($_.Exception.Message). Продолжаю установку TGAds."
    }
}

# ---------------------------------------------------------------- stop old copy
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Step 'Останавливаю работающую копию'
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $Py } | Stop-Process -Force
}

# ---------------------------------------------------------------- code
Step 'Код'
$safe = 'safe.directory=' + ($Src -replace '\\', '/')
if ((Invoke-Native git @('ls-remote', '--exit-code', '--heads', $Repo, $Branch) -Quiet) -ne 0) { $Branch = 'main' }
if (Test-Path (Join-Path $Src '.git')) {
    if ((Invoke-Native git @('-c', $safe, '-C', $Src, 'fetch', '-q', '--depth', '1', 'origin', $Branch)) -ne 0) { throw 'git fetch не удался' }
    if ((Invoke-Native git @('-c', $safe, '-C', $Src, 'checkout', '-q', '-f', '-B', $Branch, 'FETCH_HEAD')) -ne 0) { throw 'git checkout не удался' }
} else {
    if ((Invoke-Native git @('clone', '-q', '--depth', '1', '-b', $Branch, $Repo, $Src)) -ne 0) { throw 'git clone не удался' }
}
Invoke-Native git @('-c', $safe, '-C', $Src, 'log', '--oneline', '-1') | Out-Null

# ---------------------------------------------------------------- python
if (-not (Test-Path $Py)) {
    Step 'Python (отдельный, только для TGAds)'
    $zip = Join-Path $env:TEMP 'tgads-python.zip'
    $got = $false
    foreach ($v in $PythonVersions) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "https://www.nuget.org/api/v2/package/python/$v" -OutFile $zip
            $got = $true
            Write-Host "    Python $v"
            break
        } catch { Write-Host "    Python $v недоступен: $($_.Exception.Message)" }
    }
    if (-not $got) { throw 'Не удалось скачать Python с nuget.org' }
    $tmp = Join-Path $env:TEMP 'tgads-python'
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    Copy-Item -Recurse -Force (Join-Path $tmp 'tools') (Join-Path $Dir 'python')
    Remove-Item -Recurse -Force $tmp, $zip
    if (-not (Test-Path $Py)) { throw "Python не найден в $Py после распаковки" }
}

Step 'Зависимости Python'
Invoke-Native $Py @('-m', 'ensurepip', '--upgrade') -Quiet | Out-Null
if ((Invoke-Native $Py @('-m', 'pip', 'install', '-q', '--upgrade', 'pip')) -ne 0) { throw 'pip не обновился' }
if ((Invoke-Native $Py @('-m', 'pip', 'install', '-q', '-r', (Join-Path $App 'requirements.txt'))) -ne 0) { throw 'Зависимости не установились' }

if (-not $NoBrowser) {
    Step 'Браузер для TGStat (Chromium)'
    $env:PLAYWRIGHT_BROWSERS_PATH = $Browsers
    if ((Invoke-Native $Py @('-m', 'pip', 'install', '-q', "playwright==$PlaywrightVersion")) -eq 0) {
        if ((Invoke-Native $Py @('-m', 'playwright', 'install', 'chromium')) -ne 0) { Write-Warning 'Chromium не скачался: TGStat будет работать без браузерного режима' }
    } else { Write-Warning 'playwright не установился: TGStat будет работать без браузерного режима' }
}

# ---------------------------------------------------------------- claude code
if (-not $NoClaude -and -not (Test-Path $Claude)) {
    Step 'Claude Code (ИИ по подписке, без API-ключа)'
    $userClaude = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (-not (Test-Path $userClaude)) {
        # separate process: the official installer may call exit
        Invoke-Native powershell.exe @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
            "[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm https://claude.ai/install.ps1 | iex") | Out-Null
    }
    if (Test-Path $userClaude) { Copy-Item -Force $userClaude $Claude }
    else { Write-Warning 'Claude Code не установился: ИИ будет в ручном режиме (копировать промпт в чат)' }
}

# ---------------------------------------------------------------- .env
function Read-EnvValue([string]$Name) {
    if (-not (Test-Path $EnvFile)) { return $null }
    $line = Get-Content $EnvFile | Where-Object { $_ -like "$Name=*" } | Select-Object -First 1
    if ($line) { return $line.Substring($Name.Length + 1) }
    return $null
}

if (-not $HostName) { $HostName = Read-EnvValue 'PUBLIC_HOST' }
if (-not $HostName) {
    try { $ip = (Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 15).ToString().Trim() } catch { $ip = '' }
    if (-not $ip -and -not $Unattended) { $ip = Read-Host 'Публичный IP сервера' }
    if (-not $ip) { throw 'Не удалось узнать публичный IP: передайте -HostName' }
    $HostName = ($ip -replace '\.', '-') + '.sslip.io'
}

if (Test-Path $EnvFile) {
    $Port = [int](Read-EnvValue 'TGADS_PORT')
    $Password = Read-EnvValue 'TGADS_PANEL_PASSWORD'
} else {
    Step 'Настройки (.env)'
    if ((-not $ApiId -or -not $ApiHash) -and $Unattended) { throw 'Первая установка: нужны -ApiId и -ApiHash' }
    if (-not $ApiId) { $ApiId = Read-Host 'Telegram api_id' }
    if (-not $ApiHash) { $ApiHash = Read-Host 'Telegram api_hash' }
    while (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { $Port++ }
    $chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $Password = -join (1..16 | ForEach-Object { $chars | Get-Random })
    $lines = @(
        "TGADS_TG_API_ID=$ApiId",
        "TGADS_TG_API_HASH=$ApiHash",
        'TGADS_PANEL_USER=admin',
        "TGADS_PANEL_PASSWORD=$Password",
        'TGADS_HOST=127.0.0.1',
        "TGADS_PORT=$Port",
        "TGADS_DATA_DIR=$Dir\data",
        "TGADS_LOG_FILE=$Dir\logs\tgads.log",
        'TGADS_AI_MODE=auto',
        "TGADS_CLAUDE_BIN=$Claude",
        "TGADS_CLAUDE_CONFIG_DIR=$Dir\data\claude",
        "PUBLIC_HOST=$HostName"
    )
    [IO.File]::WriteAllLines($EnvFile, $lines)  # UTF-8 without BOM
}

# ---------------------------------------------------------------- runner + task
Step 'Задача Windows «TGAds» (автозапуск, перезапуск при сбое)'
$runCmd = @"
@echo off
set PYTHONPATH=$App
set PYTHONUTF8=1
set PLAYWRIGHT_BROWSERS_PATH=$Browsers
cd /d "$Dir"
"$Py" -m tgads 2>> "$Dir\logs\crash.log"
"@
[IO.File]::WriteAllText((Join-Path $Dir 'run.cmd'), $runCmd)

# the service account may change files under C:\Apps\TGAds; the secrets file is for admins and the service only
Invoke-Native icacls.exe @($Dir, '/grant', "${LocalServiceSid}:(OI)(CI)M", '/T', '/C', '/Q') -Quiet | Out-Null
Invoke-Native icacls.exe @($EnvFile, '/inheritance:r', '/grant:r', '*S-1-5-32-544:F', '*S-1-5-18:F', "${LocalServiceSid}:R") -Quiet | Out-Null

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$Dir\run.cmd`"" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\LOCALSERVICE' -LogonType ServiceAccount -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$local = "http://127.0.0.1:$Port/"
$code = Wait-Status $local @(200, 401) 120
if (@(200, 401) -notcontains $code) {
    Write-Warning "Панель не ответила на $local (код $code). Логи: $Dir\logs\tgads.log и $Dir\logs\crash.log"
} else { Write-Host "    панель работает на $local" }

# ---------------------------------------------------------------- caddy
$publicUrl = "https://$HostName/"
$caddyBlock = "$HostName {`r`n`tencode gzip`r`n`treverse_proxy 127.0.0.1:$Port`r`n}"
$caddyOk = $false
if (-not $NoCaddy) {
    Step "Caddy: адрес $publicUrl"
    $proc = Get-CimInstance Win32_Process -Filter "Name='caddy.exe'" | Select-Object -First 1
    if (-not $proc) {
        Write-Warning 'Caddy не запущен — HTTPS-адрес не настроен.'
    } else {
        $caddyExe = $proc.ExecutablePath
        $cfg = $null
        if ($proc.CommandLine -match '--config[= ]+"([^"]+)"') { $cfg = $Matches[1] }
        elseif ($proc.CommandLine -match '--config[= ]+(\S+)') { $cfg = $Matches[1] }
        if (-not $cfg) { $cfg = 'Caddyfile' }
        if (-not [IO.Path]::IsPathRooted($cfg)) { $cfg = Join-Path (Split-Path $caddyExe) $cfg }
        if (-not (Test-Path $cfg) -or $cfg -like '*.json') {
            Write-Warning "Не нашёл Caddyfile ($cfg). Добавьте в конфиг Caddy вручную:`n$caddyBlock"
        } elseif ((Get-Content -Raw $cfg) -match [regex]::Escape($HostName)) {
            Write-Host '    блок TGAds уже есть в Caddyfile'
            $caddyOk = $true
        } else {
            $backup = "$cfg.bak-tgads-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
            Copy-Item $cfg $backup
            $bytes = [IO.File]::ReadAllBytes($cfg)
            $enc = New-Object Text.UTF8Encoding($false)
            if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) { $enc = [Text.Encoding]::Unicode }
            [IO.File]::AppendAllText($cfg, "`r`n`r`n# TGAds panel (added by tgads installer)`r`n$caddyBlock`r`n", $enc)
            if ((Invoke-Native $caddyExe @('validate', '--config', $cfg, '--adapter', 'caddyfile')) -ne 0) {
                Copy-Item -Force $backup $cfg
                Write-Warning "Caddy не принял конфиг — Caddyfile восстановлен из $backup. Добавьте вручную:`n$caddyBlock"
            } elseif ((Invoke-Native $caddyExe @('reload', '--config', $cfg, '--adapter', 'caddyfile')) -eq 0) {
                $caddyOk = $true
            } else {
                $svc = Get-CimInstance Win32_Service | Where-Object { $_.ProcessId -eq $proc.ProcessId } | Select-Object -First 1
                if ($svc) {
                    Write-Host "    reload не прошёл, перезапускаю службу $($svc.Name) (пара секунд)"
                    Restart-Service -Name $svc.Name
                    $caddyOk = $true
                } else {
                    Copy-Item -Force $backup $cfg
                    Write-Warning "Не удалось перезагрузить Caddy — Caddyfile восстановлен из $backup."
                }
            }
        }
    }
    if ($caddyOk) {
        Write-Host '    жду HTTPS-сертификат (до 2 минут)'
        $code = Wait-Status $publicUrl @(200, 401) 150
        if (@(200, 401) -notcontains $code) { Write-Warning "$publicUrl пока не отвечает (код $code). Подождите пару минут, Caddy получает сертификат." }
    }
}

# ---------------------------------------------------------------- done
Write-Host ''
Write-Host 'Готово.' -ForegroundColor Green
if ($caddyOk) { Write-Host "  Панель:  $publicUrl" } else { Write-Host "  Панель (только на сервере): $local" }
Write-Host '  Логин:   admin'
Write-Host "  Пароль:  $Password   (хранится в $EnvFile)"
Write-Host "  Логи:    $Dir\logs\tgads.log"
if ($RunnerRepo) { Write-Host ("  Доступ для Claude: " + $(if ($runnerOk) { "есть (раннер $RunnerLabel в $RunnerRepo)" } else { 'не настроен' })) }
Write-Host ''

if (-not $Unattended -and -not $NoClaude -and (Test-Path $Claude) -and -not (Read-EnvValue 'TGADS_CLAUDE_OAUTH_TOKEN')) {
    Write-Host 'Чтобы ИИ работал без API-ключа, нужен токен подписки Claude (делается один раз).'
    $ans = Read-Host 'Получить токен сейчас? [Y/n]'
    if ($ans -notmatch '^[nNнН]') {
        Write-Host 'Откроется вход в Claude: перейдите по ссылке, войдите и скопируйте выданный токен (sk-ant-oat...).'
        & $Claude setup-token
        $token = (Read-Host 'Вставьте токен сюда').Trim()
        if ($token) {
            [IO.File]::AppendAllText($EnvFile, "TGADS_CLAUDE_OAUTH_TOKEN=$token`r`n", (New-Object Text.UTF8Encoding($false)))
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $Py } | Stop-Process -Force
            Start-ScheduledTask -TaskName $TaskName
            Write-Host '    токен сохранён, панель перезапущена'
        }
    } else {
        Write-Host "Позже: выполните '$Claude setup-token' и вставьте токен в панели: Настройки -> Claude."
    }
}
