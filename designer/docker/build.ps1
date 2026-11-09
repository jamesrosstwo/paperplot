# Build the plot-designer image with Docker Desktop on Windows. Context = the paperplot folder.
#
#   powershell -File C:\Users\James\Desktop\paperplot\designer\docker\build.ps1
#   powershell -File ...\build.ps1 -Image paperplot-designer:test -- --no-cache
#
# UID/GID only matter for Linux bind mounts; files written through Docker Desktop's Windows-drive mounts are owned by
# your Windows user regardless, so the 1000/1000 defaults are fine on Windows.
param(
    [string]$Image = "paperplot-designer:latest",
    [int]$Uid = 1000,
    [int]$Gid = 1000,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Extra = @()
)
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$context = (Resolve-Path (Join-Path $here "..\..")).Path
Write-Host "building $Image from $context (UID=$Uid GID=$Gid)"
$sw = [Diagnostics.Stopwatch]::StartNew()
& docker build -f (Join-Path $here "Dockerfile") --build-arg "UID=$Uid" --build-arg "GID=$Gid" -t $Image @Extra $context
if ($LASTEXITCODE -ne 0) { throw "docker build failed ($LASTEXITCODE)" }
Write-Host ("built {0} in {1:n0} s" -f $Image, $sw.Elapsed.TotalSeconds)
& docker image ls $Image
