# Run the plot-designer container with Docker Desktop on Windows.
#
#   $b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
#   $env:DESIGNER_TOKEN = -join ($b | % { $_.ToString('x2') })          # 48 hex chars from a CSPRNG
#   powershell -File C:\Users\James\Desktop\paperplot\designer\docker\run.ps1 `
#       -PaperDir C:\path\to\paper -StateDir C:\Users\James\designer_state -Publish 127.0.0.1:8776 `
#       -Data @{ "C:\Users\James\Desktop\paperplot\data" = "/data/paperplot" }
#
# Windows notes:
#  * Windows paths cannot exist inside a Linux container, so data dirs are mounted at a LINUX path (-Data maps
#    Windows dir -> Linux path) and must be registered in the designer under that Linux path (/data/...), not as
#    C:\... . Datasets registered by the Windows-native designer will show as missing inside the container.
#  * NTFS junctions / .lnk shortcuts do not cross into Linux containers: mount the junction's TARGET directory.
#  * Mount a CLONE you are happy for the container to write into; the designer writes figure PDFs/PNGs, edited TeX and
#    LaTeX build artefacts into -PaperDir. Bind mounts from Windows drives are slow for LaTeX; a clone inside WSL
#    (\\wsl$\...) is much faster.
#  * git in the container sees the repo's files as-is; if the clone uses core.autocrlf=true, run git operations from
#    one side only (Windows or container) to avoid spurious "modified" files.
param(
    [Parameter(Mandatory = $true)][string]$PaperDir,
    [Parameter(Mandatory = $true)][string]$StateDir,
    [hashtable]$Data = @{},                 # @{ "C:\host\dir" = "/data/name" } mounted read-only
    [string]$GitCredentials = "",           # e.g. "$env:USERPROFILE\.git-credentials" (mounted read-only)
    [string]$Image = "paperplot-designer:latest",
    [string]$Name = "paperplot-designer",
    [string]$Publish = "127.0.0.1:8776"     # loopback only (8766 is the Windows-native designer)
)
$ErrorActionPreference = "Stop"

if (-not $env:DESIGNER_TOKEN) { throw "set `$env:DESIGNER_TOKEN first (the container binds 0.0.0.0 and needs a token)" }
if (-not (Test-Path $PaperDir -PathType Container)) { throw "PaperDir $PaperDir does not exist" }
New-Item -ItemType Directory -Force $StateDir | Out-Null

$args_ = @("run", "--name", $Name, "-d", "--init", "--restart", "unless-stopped",
           "-p", "${Publish}:8766",
           "-v", "$((Resolve-Path $PaperDir).Path):/paper",
           "-v", "$((Resolve-Path $StateDir).Path):/state",
           "-e", "DESIGNER_TOKEN",
           "-e", "DESIGNER_GIT_NAME", "-e", "DESIGNER_GIT_EMAIL", "-e", "DESIGNER_READONLY", "-e", "DESIGNER_BASE_PATH")
$roots = @()
foreach ($k in $Data.Keys) {
    $linux = [string]$Data[$k]
    if (-not $linux.StartsWith("/")) { throw "data mount target $linux must be an absolute Linux path" }
    $item = Get-Item $k
    if ($item.LinkType -eq "Junction" -or $item.LinkType -eq "SymbolicLink") {
        Write-Warning "$k is a $($item.LinkType); mounting its target $($item.Target) instead"
        $k = [string]$item.Target
    }
    $args_ += @("-v", "$((Resolve-Path $k).Path):${linux}:ro")
    $roots += $linux
}
# the only places datasets may be registered from (inside the container: the Linux mount targets)
$args_ += @("-e", "DESIGNER_DATA_ROOTS=$($roots -join ':')")
if ($GitCredentials) { $args_ += @("-v", "$((Resolve-Path $GitCredentials).Path):/run/designer/git-credentials:ro") }
$args_ += $Image

& docker @args_
if ($LASTEXITCODE -ne 0) { throw "docker run failed ($LASTEXITCODE)" }
Write-Host "started $Name on http://$Publish/  (sign in at http://$Publish/login with DESIGNER_TOKEN)"
Write-Host "logs: docker logs -f $Name    stop: docker rm -f $Name"
