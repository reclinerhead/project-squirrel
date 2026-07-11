<#
Launch the Merle processes that live on bluejay: one Windows Terminal window,
one tab per process (daemon, mcc). Same commands as the Quick start in
TechnicalGuide.md.

The broker (Mosquitto) and the narrator (Marlin) run on pearl (192.168.1.64,
always-on Ubuntu) and are not started from here.

Usage:
  .\start-merle.ps1              # live camera
  .\start-merle.ps1 -Synthetic   # camera-free world (MERLE_SOURCE=synthetic)

Ctrl+C in a tab stops that process; closing the window stops everything.
#>
param(
    [switch]$Synthetic,
    # Internal: run a single process inside a tab (set by the launcher, not by hand)
    [ValidateSet('daemon', 'mcc')]
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
            & .\.venv\Scripts\python.exe -m uvicorn merle_daemon:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 3
        }
        'mcc' {
            pnpm --dir mcc dev
        }
    }
    return
}

$self = Join-Path $root 'start-merle.ps1'
$wtArgs = @('-w', 'merle')
foreach ($tab in 'daemon', 'mcc') {
    $wtArgs += @('nt', '--title', $tab, '-d', $root,
                 'powershell', '-NoExit', '-File', $self, '-Role', $tab)
    if ($Synthetic -and $tab -eq 'daemon') { $wtArgs += '-Synthetic' }
    $wtArgs += ';'
}
& wt @wtArgs

Write-Host 'Merle station launching: daemon, mcc. (Broker + narrator run on pearl.)'
Write-Host 'Dashboard: http://localhost:3000'
