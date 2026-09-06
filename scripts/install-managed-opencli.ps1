param(
    [Parameter(Mandatory = $true)]
    [string]$CentralApiUrl,
    [string]$ApiAuthToken = "",
    [string]$OhMyOpenCliRepo = "",
    [string]$OhMyOpenCliRoot = "$env:LOCALAPPDATA\opencli-admin\OhMyOpenCLI",
    [string]$NpmPrefix = "$env:LOCALAPPDATA\opencli-admin\npm",
    [string]$AdapterHome = $env:USERPROFILE,
    [switch]$ConfirmManagedPrefix
)

$ErrorActionPreference = "Stop"
$OpenCliVersion = "1.8.7"
$PiVersion = "0.83.0"
$OhMyOpenCliCommit = "73cc60c83586ef2c95469b3b70d6cfc80fa5bc53"
$CapabilitySourceCommit = "73cc60c83586ef2c95469b3b70d6cfc80fa5bc53"
$requestHeaders = @{}
if ($ApiAuthToken) {
    $requestHeaders = @{ Authorization = "Bearer $ApiAuthToken" }
}

function Invoke-Native {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Native command failed ($File), exit code $LASTEXITCODE"
    }
}

function Assert-UnlinkedPath {
    param([string]$Value)
    if (-not [IO.Path]::IsPathRooted($Value) -or $Value -match '[\r\n]') {
        throw "An absolute local path is required"
    }
    $current = [IO.Path]::GetFullPath($Value)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Managed paths must not traverse symlinks or junctions: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

$commands = @("node", "npm.cmd", "tar.exe")
if ($OhMyOpenCliRepo) { $commands += "git" }
foreach ($command in $commands) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command is required"
    }
}
Assert-UnlinkedPath $AdapterHome
Assert-UnlinkedPath $NpmPrefix
$AdapterHome = [IO.Path]::GetFullPath($AdapterHome)
$NpmPrefix = [IO.Path]::GetFullPath($NpmPrefix)
$defaultPrefix = [IO.Path]::GetFullPath("$env:LOCALAPPDATA\opencli-admin\npm")
if ($NpmPrefix -ne $defaultPrefix -and -not $ConfirmManagedPrefix) {
    throw "Use -ConfirmManagedPrefix only for a confirmed managed legacy npm target"
}
if (-not (Test-Path -LiteralPath $AdapterHome -PathType Container)) {
    throw "AdapterHome must be the existing HOME of the actual Agent/API execution user"
}
foreach ($layout in @("node_modules", "lib\node_modules")) {
    Assert-UnlinkedPath (Join-Path $NpmPrefix "$layout\@jackwener\opencli")
}
$env:HOME = $AdapterHome
$env:USERPROFILE = $AdapterHome
$env:NPM_CONFIG_PREFIX = $NpmPrefix
$env:NPM_CONFIG_CACHE = Join-Path $NpmPrefix ".npm-cache"
$node = (Get-Command node).Source
$npm = (Get-Command npm.cmd).Source
$tar = (Get-Command tar.exe).Source
$env:PATH = "$NpmPrefix;$(Split-Path $node);$env:PATH"
New-Item -ItemType Directory -Force -Path $NpmPrefix | Out-Null
$stage = Join-Path ([IO.Path]::GetTempPath()) ("opencli-adapters-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    $patchPath = Join-Path $stage "patch-opencli.js"
    $archivePath = Join-Path $stage "opencli-adapters.tar.gz"
    Invoke-WebRequest -UseBasicParsing `
        -Uri "$($CentralApiUrl.TrimEnd('/'))/api/v1/nodes/install/patch-opencli.js" `
        -Headers $requestHeaders -OutFile $patchPath
    Invoke-WebRequest -UseBasicParsing `
        -Uri "$($CentralApiUrl.TrimEnd('/'))/api/v1/nodes/install/opencli-adapters.tar.gz" `
        -Headers $requestHeaders -OutFile $archivePath
    $names = @(Invoke-Native $tar @("-tzf", $archivePath))
    $details = @(Invoke-Native $tar @("-tvzf", $archivePath))
    $fixed = @("scripts/install-opencli-adapters.mjs", "integrations/opencli/adapter-pack.json", "integrations/opencli/LICENSE.opencli")
    if ($names.Count -eq 0 -or ($names | Select-Object -Unique).Count -ne $names.Count `
        -or $details.Count -ne $names.Count) {
        throw "Invalid adapter archive"
    }
    foreach ($entry in $details) {
        if (-not $entry.StartsWith("-")) { throw "Adapter archive must contain only regular files" }
    }
    foreach ($name in $names) {
        if ($name -notin $fixed -and $name -cnotmatch '^integrations/opencli/(amazon|taobao|coupang|ebay)/[a-z0-9-]+\.js$') {
            throw "Unexpected adapter archive path"
        }
    }
    foreach ($name in $fixed) {
        if ($name -notin $names) { throw "Incomplete adapter archive" }
    }
    Invoke-Native $tar @("-xzf", $archivePath, "-C", $stage)
    $source = Join-Path $stage "integrations\opencli"
    $inventory = Get-Content -Raw -LiteralPath (Join-Path $source "adapter-pack.json") | ConvertFrom-Json
    if ($inventory.schemaVersion -ne 1 -or $inventory.opencliVersion -ne $OpenCliVersion) {
        throw "Invalid adapter inventory"
    }
    $expected = @($fixed) + @($inventory.files | ForEach-Object { "integrations/opencli/$_" })
    if (Compare-Object ($expected | Sort-Object -Unique) ($names | Sort-Object -Unique)) {
        throw "Incomplete adapter payload"
    }
    foreach ($name in $names) {
        if ((Get-Item -LiteralPath (Join-Path $stage $name)).Length -eq 0) { throw "Empty adapter payload" }
    }

    # Remove only the explicitly confirmed managed package, without lifecycle
    # scripts (upstream preuninstall stops daemons). Same-version install is not
    # a restoration: verify the polluted package directory really disappeared.
    Invoke-Native $npm @("uninstall", "-g", "--prefix", $NpmPrefix, "--ignore-scripts", "@jackwener/opencli")
    foreach ($layout in @("node_modules", "lib\node_modules")) {
        if (Test-Path -LiteralPath (Join-Path $NpmPrefix "$layout\@jackwener\opencli")) {
            throw "Official restore failed: old OpenCLI package remains"
        }
    }
    Invoke-Native $npm @("install", "-g", "--prefix", $NpmPrefix, "--ignore-scripts", "--registry=https://registry.npmjs.org", "@jackwener/opencli@$OpenCliVersion")
    Invoke-Native $npm @("install", "-g", "--prefix", $NpmPrefix, "@earendil-works/pi-coding-agent@$PiVersion")
    Invoke-Native $node @($patchPath, $NpmPrefix)
    Invoke-Native $node @((Join-Path $stage "scripts\install-opencli-adapters.mjs"), "--source", $source, "--prefix", $NpmPrefix, "--home", $AdapterHome)
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force
}

# Organization packs remain an explicit, separately pinned opt-in. Never remove
# or switch an existing user repository to make a managed runtime update succeed.
if ($OhMyOpenCliRepo) {
    Assert-UnlinkedPath $OhMyOpenCliRoot
    if (Test-Path -LiteralPath $OhMyOpenCliRoot) {
        $origin = (Invoke-Native git @("-C", $OhMyOpenCliRoot, "remote", "get-url", "origin") | Out-String).Trim()
        $revision = (Invoke-Native git @("-C", $OhMyOpenCliRoot, "rev-parse", "HEAD") | Out-String).Trim()
        $changes = (Invoke-Native git @("-C", $OhMyOpenCliRoot, "status", "--porcelain") | Out-String).Trim()
        if ($origin -ne $OhMyOpenCliRepo -or $revision -ne $OhMyOpenCliCommit -or $changes) {
            throw "Existing organization repository is not the clean requested pinned checkout; left unchanged"
        }
    } else {
        Invoke-Native git @("clone", $OhMyOpenCliRepo, $OhMyOpenCliRoot)
        Invoke-Native git @("-C", $OhMyOpenCliRoot, "checkout", "--detach", $OhMyOpenCliCommit)
    }
    Invoke-Native git @("-C", $OhMyOpenCliRoot, "merge-base", "--is-ancestor", $CapabilitySourceCommit, "HEAD")
    Push-Location $OhMyOpenCliRoot
    try {
        Invoke-Native $npm @("ci")
        Invoke-Native $npm @("run", "bootstrap")
    } finally {
        Pop-Location
    }
    [Environment]::SetEnvironmentVariable("OHMYOPENCLI_ROOT", $OhMyOpenCliRoot, "User")
}

# The Agent launched from this shell uses exactly the prefix and HOME installed
# above. Future shells receive the managed executable path without losing PATH.
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($NpmPrefix -notin ($userPath -split ';')) {
    [Environment]::SetEnvironmentVariable("PATH", "$NpmPrefix;$userPath", "User")
}
$env:OPENCLI_BIN = Join-Path $NpmPrefix "opencli.cmd"
if (-not (Test-Path -LiteralPath $env:OPENCLI_BIN)) { throw "Managed OpenCLI executable is unavailable" }
Write-Output "Managed OpenCLI 1.8.7 and adapters installed for $AdapterHome. Start the Agent with this HOME, managed PATH, and an explicit OPENCLI_BROWSER_PROFILE_KIND."
