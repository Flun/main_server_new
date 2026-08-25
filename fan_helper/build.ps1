$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$localDotnet = Join-Path $repoRoot '.tools\dotnet\dotnet.exe'
$dotnet = if (Test-Path -LiteralPath $localDotnet) { $localDotnet } else { 'dotnet' }
try {
    $sdkList = & $dotnet --list-sdks 2>$null
} catch {
    $sdkList = @()
}
if (-not ($sdkList | Select-String '^10\.')) {
    $toolsDir = Join-Path $repoRoot '.tools'
    $installScript = Join-Path $toolsDir 'dotnet-install.ps1'
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    Invoke-WebRequest -UseBasicParsing 'https://dot.net/v1/dotnet-install.ps1' -OutFile $installScript
    & $installScript -Channel '10.0' -InstallDir (Split-Path -Parent $localDotnet) -NoPath
    $dotnet = $localDotnet
}
& $dotnet publish (Join-Path $PSScriptRoot 'MainServer.FanHelper.csproj') -c Release --self-contained true
$publishDir = Join-Path $PSScriptRoot 'bin\Release\net10.0-windows\win-x64\publish'
$distDir = Join-Path $PSScriptRoot 'dist'
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
Copy-Item -LiteralPath (Join-Path $publishDir 'MainServer.FanHelper.exe') -Destination (Join-Path $distDir 'MainServer.FanHelper.exe') -Force
