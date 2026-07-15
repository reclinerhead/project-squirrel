<#
Launch the Merle process that lives on bluejay -- the perception daemon -- in
its own Windows Terminal tab. Same command as the Quick start in
TechnicalGuide.md.

The broker (Mosquitto), the narrator (Marlin), and the dashboard (MCC, :3000)
all run on pearl (192.168.1.64, always-on Ubuntu) and are not started from here.

Usage:
  .\start-merle.ps1              # live camera
  .\start-merle.ps1 -Synthetic   # camera-free world (MERLE_SOURCE=synthetic)

Ctrl+C in the tab stops the daemon; closing the window stops it too.
#>
param(
    [switch]$Synthetic,
    # Internal: run the daemon inside the tab (set by the launcher, not by hand)
    [ValidateSet('daemon')]
    [string]$Role
)

$root = $PSScriptRoot

if ($Role) {
    Set-Location $root
    switch ($Role) {
        'daemon' {
            # bus.py refuses to guess the broker address; pearl unless overridden
            if (-not $env:MERLE_MQTT) { $env:MERLE_MQTT = '192.168.1.64:1883' }
            if ($Synthetic) {
                $env:MERLE_SOURCE = 'synthetic'
            } elseif (-not $env:MERLE_RTSP_PASS) {
                Write-Warning 'MERLE_RTSP_PASS is not set - the camera will not open.'
            }
            # --host 0.0.0.0: pearl's dashboard proxies to this daemon across
            # the LAN (loopback-only was the one-box era). The bounded shutdown
            # exists because that dashboard runs 24/7 and always holds an MJPEG
            # /stream connection, which never completes -- without the timeout,
            # Ctrl+C waits on it forever (and a second Ctrl+C is ignored).
            # --no-access-log: that same 24/7 dashboard polls /state about twice
            # a second, and each poll printed a line, burying the event prints
            # this console exists to show. Startup/error lines are unaffected.
            & .\.venv\Scripts\python.exe -m uvicorn vision.merle_daemon:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 3 --no-access-log
        }
    }
    return
}

$self = Join-Path $root 'start-merle.ps1'
$wtArgs = @('-w', 'merle', 'nt', '--title', 'daemon', '-d', $root,
            'powershell', '-NoExit', '-File', $self, '-Role', 'daemon')
if ($Synthetic) { $wtArgs += '-Synthetic' }
& wt @wtArgs

Write-Host 'Merle daemon launching. (Broker, narrator + dashboard run on pearl.)'
Write-Host 'Dashboard: http://pearl:3000'
