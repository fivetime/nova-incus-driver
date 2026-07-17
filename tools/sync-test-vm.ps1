param(
    [string]$HostName = "10.224.0.16",
    [string]$RemotePath = "/opt/openstack-incus-src",
    [string]$SdkPath = "C:\MyProjects\IaasProjects\Incus\incus-python-sdk",
    [string]$RemoteSdkPath = "/opt/incus-python-sdk-src",
    [string]$IdentityFile = "$env:USERPROFILE\.ssh\openstack-incus-vm_ed25519"
)

$ErrorActionPreference = "Stop"

$repository = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$msysRoot = Join-Path $env:USERPROFILE "scoop\apps\msys2\current"
$bash = Join-Path $msysRoot "usr\bin\bash.exe"

if (-not (Test-Path -LiteralPath $bash)) {
    throw "MSYS2 is not installed through Scoop: $bash"
}
if (-not (Test-Path -LiteralPath $IdentityFile)) {
    throw "Test VM SSH identity does not exist: $IdentityFile"
}

function Convert-ToMsysPath([string]$Path) {
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $drive = $resolved.Substring(0, 1).ToLowerInvariant()
    return "/$drive/" + $resolved.Substring(3).Replace("\", "/")
}

$source = Convert-ToMsysPath $repository
$sdkSource = Convert-ToMsysPath $SdkPath
$identity = Convert-ToMsysPath $IdentityFile

& ssh -i $IdentityFile -o BatchMode=yes -o StrictHostKeyChecking=no `
    "root@$HostName" "mkdir -p '$RemotePath' '$RemoteSdkPath'"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to prepare the remote test directory"
}

function Sync-Source([string]$Source, [string]$Destination) {
    $rsync = @(
        "rsync -az --delete",
        "--exclude=.git/",
        "--exclude=.tox/",
        "--exclude=.venv/",
        "--exclude=__pycache__/",
        "--exclude='*.pyc'",
        "-e 'ssh -i $identity -o StrictHostKeyChecking=no'",
        "'$Source/' 'root@$HostName`:$Destination/'"
    ) -join " "

    & $bash -lc $rsync
    if ($LASTEXITCODE -ne 0) {
        throw "rsync failed for $Destination with exit code $LASTEXITCODE"
    }
}

Sync-Source $source $RemotePath
Sync-Source $sdkSource $RemoteSdkPath

Write-Output "Synchronized authoritative local trees to root@$HostName"
