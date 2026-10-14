param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("install", "status", "uninstall")]
    [string] $Action,

    [Parameter(Mandatory = $false)]
    [string] $StorageRoot,

    [Parameter(Mandatory = $false)]
    [string] $MisePath,

    [Parameter(Mandatory = $false)]
    [string] $PowerShellPath
)

$ErrorActionPreference = "Stop"
$taskName = "PKM Storage Git Sync - $([System.Environment]::UserName)"
$legacyDescription = "Hourly commit and Git sync for approved PKM Storage paths."

function ConvertTo-XmlText([string] $Value) {
    return [System.Security.SecurityElement]::Escape($Value)
}

function Get-StorageDescription([string] $Storage, [string] $UserSid) {
    return "Hourly commit and Git sync for Storage '$Storage' (SID $UserSid)."
}

function Test-FullyQualifiedWindowsPath([string] $Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }
    if ($Value -notmatch '^(?:[A-Za-z]:[\\/].*|\\\\[^\\/]+[\\/][^\\/]+(?:[\\/].*)?)$') {
        return $false
    }
    try {
        [void][System.IO.Path]::GetFullPath($Value)
        return $true
    }
    catch {
        return $false
    }
}

function Assert-TaskOwnership($Task, [string] $Storage, [string] $UserSid) {
    $expectedDescription = Get-StorageDescription $Storage $UserSid
    if (
        $Task.Description -ine $expectedDescription -and
        $Task.Description -cne $legacyDescription
    ) {
        throw "Task '$taskName' is not an owned PKM Storage sync task; leaving it unchanged."
    }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1 -or $actions[0].CimClass.CimClassName -ne "MSFT_TaskExecAction") {
        throw "Task '$taskName' does not have the expected single executable action; leaving it unchanged."
    }

    $action = $actions[0]
    $expectedStorage = [System.IO.Path]::GetFullPath($Storage)
    if (
        -not (Test-FullyQualifiedWindowsPath $action.Execute) -or
        $action.Execute -notmatch '(?i)\\(powershell|pwsh)\.exe$' -or
        [System.IO.Path]::GetFullPath($action.WorkingDirectory) -ine $expectedStorage
    ) {
        throw "Task '$taskName' has a different executable or working directory; leaving it unchanged."
    }

    $argumentMatch = [regex]::Match(
        $action.Arguments,
        '^-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ([A-Za-z0-9+/=]+)$'
    )
    if (-not $argumentMatch.Success) {
        throw "Task '$taskName' has unexpected arguments; leaving it unchanged."
    }
    try {
        $runner = [System.Text.Encoding]::Unicode.GetString(
            [Convert]::FromBase64String($argumentMatch.Groups[1].Value)
        )
        $lines = [System.Text.RegularExpressions.Regex]::Split($runner, "`n")
        if ($lines.Count -ne 6) {
            throw "runner line count mismatch"
        }

        $pathMatch = [regex]::Match(
            $lines[0],
            '^\$env:PATH = \[System\.Text\.Encoding\]::UTF8\.GetString\(\[Convert\]::FromBase64String\(''([A-Za-z0-9+/=]*)''\)\)$'
        )
        $storageMatch = [regex]::Match(
            $lines[1],
            '^\$storage = \[System\.Text\.Encoding\]::UTF8\.GetString\(\[Convert\]::FromBase64String\(''([A-Za-z0-9+/=]+)''\)\)$'
        )
        $miseMatch = [regex]::Match(
            $lines[2],
            '^\$mise = \[System\.Text\.Encoding\]::UTF8\.GetString\(\[Convert\]::FromBase64String\(''([A-Za-z0-9+/=]+)''\)\)$'
        )
        if (
            -not $pathMatch.Success -or
            -not $storageMatch.Success -or
            -not $miseMatch.Success -or
            $lines[3] -cne 'Set-Location -LiteralPath $storage' -or
            $lines[4] -cne '& $mise run sync' -or
            $lines[5] -cne 'exit $LASTEXITCODE'
        ) {
            throw "runner content mismatch"
        }
        $environmentPath = [System.Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($pathMatch.Groups[1].Value)
        )
        $taskStorage = [System.Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($storageMatch.Groups[1].Value)
        )
        $misePath = [System.Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($miseMatch.Groups[1].Value)
        )
        if (
            $environmentPath.Contains("`r") -or $environmentPath.Contains("`n") -or
            [System.IO.Path]::GetFullPath($taskStorage) -ine $expectedStorage -or
            -not (Test-FullyQualifiedWindowsPath $misePath) -or
            $misePath.Contains("`r") -or $misePath.Contains("`n")
        ) {
            throw "runner context mismatch"
        }
    }
    catch {
        throw "Task '$taskName' does not contain the expected Storage sync runner; leaving it unchanged."
    }

    if (
        $Task.Principal.UserId -ine $UserSid -or
        $Task.Principal.LogonType -notin @("Interactive", "InteractiveToken") -or
        $Task.Principal.RunLevel -ne "Limited"
    ) {
        throw "Task '$taskName' has a different user or privilege level; leaving it unchanged."
    }
}

switch ($Action) {
    "install" {
        if (
            [string]::IsNullOrWhiteSpace($StorageRoot) -or
            [string]::IsNullOrWhiteSpace($MisePath) -or
            [string]::IsNullOrWhiteSpace($PowerShellPath)
        ) {
            throw "StorageRoot, MisePath, and PowerShellPath are required for install."
        }

        $storage = [System.IO.Path]::GetFullPath($StorageRoot)
        $mise = [System.IO.Path]::GetFullPath($MisePath)
        $powerShell = [System.IO.Path]::GetFullPath($PowerShellPath)
        $start = (Get-Date).AddMinutes(1).ToString("yyyy-MM-dd'T'HH:mm:ss")
        $userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -ne $existingTask) {
            Assert-TaskOwnership $existingTask $storage $userSid
        }
        $environmentBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($env:PATH))
        $storageBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($storage))
        $miseBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($mise))
        $runner = "`$env:PATH = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$environmentBase64'))`n"
        $runner += "`$storage = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$storageBase64'))`n"
        $runner += "`$mise = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$miseBase64'))`n"
        $runner += "Set-Location -LiteralPath `$storage`n"
        $runner += "& `$mise run sync`n"
        $runner += "exit `$LASTEXITCODE"
        $encodedRunner = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($runner))
        $taskArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encodedRunner"
        $description = ConvertTo-XmlText (Get-StorageDescription $storage $userSid)
        $taskXml = @"
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>$description</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>$start</StartBoundary>
      <Repetition><Interval>PT1H</Interval><Duration>P1D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>$userSid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowHardTerminate>true</AllowHardTerminate>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(ConvertTo-XmlText $powerShell)</Command>
      <Arguments>$(ConvertTo-XmlText $taskArguments)</Arguments>
      <WorkingDirectory>$(ConvertTo-XmlText $storage)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
        Register-ScheduledTask -TaskName $taskName -Xml $taskXml -Force | Out-Null
        Write-Output "Installed hourly Task Scheduler job for the current user: $taskName"
    }

    "status" {
        if ([string]::IsNullOrWhiteSpace($StorageRoot)) {
            throw "StorageRoot is required for status."
        }
        $storage = [System.IO.Path]::GetFullPath($StorageRoot)
        $userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Output "PKM scheduled sync is not installed for this user."
            break
        }
        Assert-TaskOwnership $task $storage $userSid
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        Write-Output "Task: $taskName"
        Write-Output "State: $($task.State)"
        Write-Output "Last run: $($info.LastRunTime)"
        Write-Output "Next run: $($info.NextRunTime)"
        Write-Output "Last result: $($info.LastTaskResult)"
    }

    "uninstall" {
        if ([string]::IsNullOrWhiteSpace($StorageRoot)) {
            throw "StorageRoot is required for uninstall."
        }
        $storage = [System.IO.Path]::GetFullPath($StorageRoot)
        $userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Output "PKM scheduled sync is not installed for this user."
            break
        }
        Assert-TaskOwnership $task $storage $userSid
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "Removed scheduled task: $taskName"
    }
}
