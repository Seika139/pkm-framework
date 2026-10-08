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

function ConvertTo-XmlText([string] $Value) {
    return [System.Security.SecurityElement]::Escape($Value)
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
        $taskXml = @"
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Hourly commit and Git sync for approved PKM Storage paths.</Description></RegistrationInfo>
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
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Output "PKM scheduled sync is not installed for this user."
            break
        }
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        Write-Output "Task: $taskName"
        Write-Output "State: $($task.State)"
        Write-Output "Last run: $($info.LastRunTime)"
        Write-Output "Next run: $($info.NextRunTime)"
        Write-Output "Last result: $($info.LastTaskResult)"
    }

    "uninstall" {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Output "PKM scheduled sync is not installed for this user."
            break
        }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "Removed scheduled task: $taskName"
    }
}
