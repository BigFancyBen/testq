# testq in the notification area.
#
# A tray icon, no window until you ask for one. Hover for a one-line summary,
# click to open the page, right-click for the rest. The icon itself is the
# status at a glance:
#
#   grey    nothing running
#   blue    running, queue empty
#   amber   something is waiting
#   purple  engines on the box that the queue did not start
#   red     the last job that finished did not pass
#
# Deliberately not an Electron app. Everything asked of it -- live in the
# overflow area, stay out of the way, open on click, summarise on hover -- is
# what the Windows tray API already does, and Windows ships the .NET assemblies
# used here. An Electron build would add a couple of hundred megabytes of Node
# and a second long-running process to a tool whose whole selling point is that
# it installs nothing. The page it opens is the same one either way.
#
#   powershell -ExecutionPolicy Bypass -File <testq install>/tray.ps1 [-Port 43117]
#
# or, more simply:  python <testq install>/testq.py tray
#
# -Auto is how the daemon launches it, and it is the usual way this runs: the
# icon appears when the box gets busy and takes itself away once the queue has
# been idle for -IdleExitSeconds, so there is nothing in the notification area
# on a quiet machine and nothing to remember to start on a busy one. Launched
# by hand -- `testq.py tray` -- it stays up until it is dismissed.

param(
  [int]$Port = 0,
  [int]$IntervalSeconds = 4,
  [switch]$Auto,
  [int]$IdleExitSeconds = 90
)

if ($Port -le 0) {
  if ($env:TESTQ_PORT) { $Port = [int]$env:TESTQ_PORT } else { $Port = 43117 }
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# One tray icon per port, however many times this gets launched.
# Name kept from when this lived in one project: a vendored copy still out
# there uses it, and two trays on one port is exactly what it prevents.
$mutex = New-Object System.Threading.Mutex($false, "Global\mfrs-testq-tray-$Port")
if (-not $mutex.WaitOne(0, $false)) {
  Write-Host "a testq tray icon for port $Port is already running"
  exit 0
}

$script:Base = "http://127.0.0.1:$Port"
$script:LastIconKey = ""
$script:LastFinished = 0
$script:Announced = $true
# Idle from the moment it starts, so an icon raised onto a box whose one job
# died in the same second still leaves on schedule instead of sitting there.
$script:IdleSince = Get-Date

# Icons are drawn rather than shipped, so there is no binary asset to keep in
# step with anything. 32x32 because that is what the overflow area asks for on
# a high-DPI display; Windows scales it down cleanly for the 16px slot.
function New-DotIcon([System.Drawing.Color]$Colour, [bool]$Ring) {
  $bmp = New-Object System.Drawing.Bitmap 32, 32
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = 'AntiAlias'
  $g.Clear([System.Drawing.Color]::Transparent)
  $brush = New-Object System.Drawing.SolidBrush $Colour
  $g.FillEllipse($brush, 4, 4, 24, 24)
  if ($Ring) {
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), 3
    $g.DrawEllipse($pen, 2, 2, 28, 28)
    $pen.Dispose()
  }
  $brush.Dispose(); $g.Dispose()
  $icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
  $bmp.Dispose()
  return $icon
}

$icons = @{
  idle      = New-DotIcon ([System.Drawing.Color]::FromArgb(110, 119, 129)) $false
  running   = New-DotIcon ([System.Drawing.Color]::FromArgb(88, 166, 255))  $false
  queued    = New-DotIcon ([System.Drawing.Color]::FromArgb(210, 153, 34))  $true
  unmanaged = New-DotIcon ([System.Drawing.Color]::FromArgb(163, 113, 247)) $true
  failed    = New-DotIcon ([System.Drawing.Color]::FromArgb(248, 81, 73))   $false
  down      = New-DotIcon ([System.Drawing.Color]::FromArgb(60, 66, 74))    $false
}

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = $icons.down
$notify.Text = "testq: starting"
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
function Add-Item($label, $action) {
  $item = $menu.Items.Add($label)
  $item.add_Click($action)
  return $item
}
Add-Item "Open the queue page" { Start-Process "$script:Base/" } | Out-Null
$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
$script:StatusItem = $menu.Items.Add("(no daemon)")
$script:StatusItem.Enabled = $false
$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
Add-Item "Hide this icon" {
  $notify.Visible = $false
  [System.Windows.Forms.Application]::Exit()
} | Out-Null
$notify.ContextMenuStrip = $menu

# Left click opens the page; that is the whole point of it being here.
$notify.add_MouseClick({
  if ($_.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
    Start-Process "$script:Base/"
  }
})

function Short([string]$s, [int]$n) {
  if ($s.Length -le $n) { return $s }
  return $s.Substring(0, $n - 1) + [char]0x2026
}

# An auto-raised icon exists for the duration of the work and no longer. The
# linger before it goes matters more than it looks: a run that has just failed
# leaves a red dot and a balloon, and both want time on screen to be read.
#
# A daemon that has gone away counts as idle -- the icon is a view onto a queue,
# and there is nothing to look at once there is no queue.
function Update-IdleExit([bool]$busy) {
  if (-not $Auto) { return }
  if ($busy) { $script:IdleSince = $null; return }
  if ($null -eq $script:IdleSince) { $script:IdleSince = Get-Date }
  if (((Get-Date) - $script:IdleSince).TotalSeconds -ge $IdleExitSeconds) {
    $notify.Visible = $false
    [System.Windows.Forms.Application]::Exit()
  }
}

function Update-Tray {
  try {
    $state = Invoke-RestMethod -Uri "$script:Base/state" -TimeoutSec 3
  } catch {
    $notify.Icon = $icons.down
    # NotifyIcon.Text throws above 63 characters, so every string here is
    # written to fit rather than trimmed at the last moment.
    $notify.Text = "testq: daemon not running"
    $script:StatusItem.Text = "daemon not running on port $Port"
    Update-IdleExit $false
    return
  }

  $running = @($state.running).Count
  $queued  = @($state.queued).Count
  $cpu     = $state.used.cpu
  $cap     = $state.capacity.cpu
  $unmanaged = $state.godot.unmanaged

  # A job that has just finished badly is worth one balloon, once.
  $newest = @($state.history)[0]
  if ($newest -and $newest.finished -gt $script:LastFinished) {
    $bad = ($newest.verdict -eq 'released' -and $newest.exit -ne 0)
    if ($script:LastFinished -gt 0 -and $bad) {
      $notify.BalloonTipTitle = "$($newest.script) $($newest.arg) failed"
      $notify.BalloonTipText  = "exit $($newest.exit) in $($newest.tree)"
      $notify.BalloonTipIcon  = [System.Windows.Forms.ToolTipIcon]::Warning
      $notify.ShowBalloonTip(6000)
    }
    $script:LastFinished = $newest.finished
    $script:RecentlyFailed = $bad
  }

  if ($running -eq 0 -and $queued -eq 0) {
    $tip = "testq: idle"
    $icon = if ($script:RecentlyFailed) { $icons.failed } else { $icons.idle }
  } else {
    $tip = "testq: $cpu/$cap cpu, $running running"
    if ($queued -gt 0) { $tip += ", $queued queued" }
    if ($running -gt 0) {
      $first = @($state.running)[0]
      $line = "$($first.script) $($first.arg)".Trim()
      $tip += "`n$(Short $line 40)"
    }
    $icon = if ($queued -gt 0) { $icons.queued } else { $icons.running }
  }
  if ($unmanaged -gt 0) {
    $icon = $icons.unmanaged
    $tip = "testq: $unmanaged engine(s) outside the queue"
  }

  $notify.Icon = $icon
  $notify.Text = Short $tip 62

  # The right-click menu carries the detail the tooltip has no room for.
  if ($running -gt 0 -or $queued -gt 0) {
    $lines = @()
    foreach ($r in @($state.running)) {
      $el = [int]$r.elapsed_s
      $lines += "running: $($r.script) $($r.arg) ($([int]($el/60))m$('{0:00}' -f ($el%60))s)"
    }
    foreach ($q in @($state.queued)) {
      $lines += "queued $($q.position): $($q.script) $($q.arg) -- $($q.blocked_on)"
    }
    $script:StatusItem.Text = ($lines -join "`n")
  } else {
    $script:StatusItem.Text = "idle -- $cap cpu, $($state.capacity.gpu) gpu free"
  }

  # Strays are not counted here on purpose, and the daemon does not count them
  # either: an editor somebody left open is an unmanaged engine for the rest of
  # the afternoon, and it would pin the icon up for all of it.
  Update-IdleExit ($running -gt 0 -or $queued -gt 0)
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $IntervalSeconds * 1000
$timer.add_Tick({ Update-Tray })
$timer.Start()
Update-Tray

# No form is ever shown; this is only here to pump messages for the icon.
[System.Windows.Forms.Application]::Run()

$timer.Stop()
$notify.Visible = $false
$notify.Dispose()
$mutex.ReleaseMutex()
