# Start srsTwin dashboard on localhost (Windows)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = $null
foreach ($c in @("python", "python3", "py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
        & $c -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $c; break }
    }
}
if (-not $py) { throw "Python 3 not found" }

$mode = if ($args.Count -gt 0) { $args[0] } else { "direct" }
$extra = @()
if ($args -contains "--pull") { $extra += "--pull" }

& $py serve_dashboard.py --mode $mode @extra
