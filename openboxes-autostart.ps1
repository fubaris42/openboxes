<#
.SYNOPSIS
  Start OpenBoxes automatically on Windows (podman machine + podman compose).
.DESCRIPTION
  Keep this script next to compose.yaml. Run it as the user who owns the podman machine.
.EXAMPLE
  .\openboxes-autostart.ps1             # install: register a logon task, start now, apply seed.sql
  .\openboxes-autostart.ps1 seed        # (re)apply seed.sql; safe to repeat
  .\openboxes-autostart.ps1 uninstall   # remove the task (containers keep running)
  .\openboxes-autostart.ps1 start       # what the task runs
#>
param([ValidateSet('install', 'uninstall', 'start', 'seed')][string]$Action = 'install')

$Root = $PSScriptRoot
$TaskName = 'OpenBoxes'
$LogName = 'autostart.log'
Set-Location $Root

function Write-Log([string]$Message) {
    Add-Content -Path $LogName -Value "[$(Get-Date -Format s)] $Message"
}

# Native stderr redirection is unreliable in Windows PowerShell 5.1, so let cmd.exe do it.
function Invoke-Logged([string]$Command) {
    Write-Log "> $Command"
    cmd /c "$Command >> $LogName 2>&1"
    return $LASTEXITCODE
}

function New-Secret {
    -join ((48..57 + 65..90 + 97..122) | Get-Random -Count 28 | ForEach-Object { [char]$_ })
}

function New-EnvFile {
    if (Test-Path .env) { return }
    @"
OB_VERSION=v0.9.8-hotfix1
OB_PORT=8081
# To serve the office LAN: OB_BIND=0.0.0.0 and OB_HOST=<this machine's hostname or IP>
#OB_BIND=0.0.0.0
#OB_HOST=localhost
DB_PASSWORD=$(New-Secret)
DB_ROOT_PASSWORD=$(New-Secret)
"@ | Out-File -Encoding ascii .env
    Write-Host 'Created .env with new database passwords.'
}

function Start-Stack {
    $state = cmd /c 'podman machine inspect --format {{.State}} 2>nul'
    if ($LASTEXITCODE -ne 0) {
        Write-Log 'No podman machine found; run "podman machine init" first.'
        return 1
    }
    if ($state -ne 'running') { Invoke-Logged 'podman machine start' | Out-Null }

    # the machine can report running before the API answers
    foreach ($i in 1..30) {
        cmd /c 'podman info >nul 2>&1'
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 5
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Log 'podman is not responding; giving up.'
        return 1
    }
    return (Invoke-Logged 'podman compose up -d')
}

function Invoke-Seed {
    if (-not (Test-Path seed.sql)) {
        Write-Host 'No seed.sql next to this script; nothing to seed.'
        return 0
    }
    Write-Host 'Waiting for OpenBoxes to finish booting (the first boot can take several minutes)...'
    $health = ''
    foreach ($i in 1..180) {
        $health = cmd /c 'podman inspect -f {{.State.Health.Status}} openboxes-app 2>nul'
        if ($health -eq 'healthy') { break }
        Start-Sleep -Seconds 10
    }
    if ($health -ne 'healthy') {
        Write-Log 'openboxes-app did not become healthy; seed skipped.'
        return 1
    }
    if ((Invoke-Logged 'podman cp seed.sql openboxes-db:/tmp/seed.sql') -ne 0) { return 1 }
    $rc = Invoke-Logged 'podman exec openboxes-db sh -c "MYSQL_PWD=$MARIADB_PASSWORD mariadb -uopenboxes openboxes < /tmp/seed.sql"'
    if ($rc -ne 0) { return $rc }
    # the app caches locations, categories and products, so restart it to pick up the new rows
    return (Invoke-Logged 'podman restart openboxes-app')
}

function Install-Autostart {
    if (-not (Get-Command podman -ErrorAction SilentlyContinue)) {
        throw 'podman not found. Install it (winget install RedHat.Podman), reopen the terminal and rerun.'
    }
    if (-not (Get-Command podman-compose, docker-compose -ErrorAction SilentlyContinue)) {
        throw 'No compose provider found. Install one (pip install podman-compose) and rerun.'
    }
    cmd /c 'podman machine inspect >nul 2>&1'
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Creating podman machine...'
        podman machine init
        if ($LASTEXITCODE -ne 0) { throw 'podman machine init failed.' }
    }
    New-EnvFile

    $user = "$env:USERDOMAIN\$env:USERNAME"
    $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $Root `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" start"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger `
        -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
    Write-Host "Registered scheduled task '$TaskName' (runs at logon of $user)."

    Write-Host 'Starting OpenBoxes (the first run pulls ~1 GB of images)...'
    if ((Start-Stack) -ne 0) { throw "Start failed, see $Root\$LogName" }
    if ((Invoke-Seed) -ne 0) { throw "Seeding failed, see $Root\$LogName" }
    $port = 8081
    $match = Select-String -Path .env -Pattern '^OB_PORT=(\d+)' | Select-Object -First 1
    if ($match) { $port = $match.Matches[0].Groups[1].Value }
    Write-Host "OpenBoxes is starting: http://localhost:$port/openboxes/ (allow a few minutes on first boot)"
}

switch ($Action) {
    'install' { Install-Autostart }
    'uninstall' {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Removed scheduled task '$TaskName'. Containers were left as they are."
    }
    'start' { exit (Start-Stack) }
    'seed' {
        if ((Invoke-Seed) -ne 0) { throw "Seeding failed, see $Root\$LogName" }
        Write-Host 'Seed applied; OpenBoxes is restarting.'
    }
}
