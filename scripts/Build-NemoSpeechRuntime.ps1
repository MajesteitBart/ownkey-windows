<#
.SYNOPSIS
Builds the NeMo-Speech.cpp runtime that Ownkey loads for meeting speaker labels.

.DESCRIPTION
Nemotron 3 Diarization support landed in NeMo-Speech.cpp after its last
release, so Ownkey builds a pinned commit from source. The build is CPU only,
uses an AVX2/FMA/F16C baseline instead of this machine's instruction set, and
has no OpenMP runtime dependency. The DLLs, their licenses and a manifest go to
vendor\nemo-speech\windows-x64, which Ownkey.spec bundles as nemo_speech.

A staged runtime from the same commit is reused. Pass -Force to rebuild.
Requires Git, Python (py.exe) and Visual Studio 2022 with the C++ workload.
CMake and Ninja are installed into the source folder when missing.
#>
[CmdletBinding()]
param(
    [string]$SourceDir = (Join-Path $env:LOCALAPPDATA 'Ownkey\build\NeMo-Speech.cpp'),

    [int]$Jobs = [Environment]::ProcessorCount,

    [switch]$Force
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$repository = 'https://github.com/NVIDIA/NeMo-Speech.cpp.git'
# feat(diar): make Nemotron 3 Diarization the default diarizer (#52)
$commit = '97a15afa5caa9bce5baaa86c1184103877af4101'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$outputDir = Join-Path $repoRoot 'vendor\nemo-speech\windows-x64'
$manifestPath = Join-Path $outputDir 'runtime.json'
$buildDir = Join-Path $SourceDir 'build-ownkey-cpu'
$libraries = @('nemo_speech_asr_c.dll', 'nemo_speech_asr.dll', 'ggml.dll', 'ggml-base.dll', 'ggml-cpu.dll')
$licenses = @('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md')
# Seeded into the build cache before the first configure. MSVC derives FMA and
# F16C from AVX2, so those two stay unused there.
$portableFlags = [ordered]@{
    GGML_NATIVE = 'OFF'; GGML_OPENMP = 'OFF'; GGML_SSE42 = 'ON'; GGML_AVX = 'ON'; GGML_AVX2 = 'ON'
    GGML_BMI2 = 'ON'; GGML_FMA = 'ON'; GGML_F16C = 'ON'; GGML_AVX_VNNI = 'OFF'; GGML_AVX512 = 'OFF'
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

# Windows PowerShell turns a native command's redirected stderr into an error
# record, which stops the script under ErrorActionPreference Stop.
function Invoke-NativeProbe {
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$ArgumentList = @())

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $FilePath @ArgumentList 2>$null
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($output) }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-StagedRuntime {
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return $false
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.commit -ne $commit) {
        return $false
    }
    foreach ($name in $libraries) {
        $path = Join-Path $outputDir $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            return $false
        }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $manifest.files.$name) {
            return $false
        }
    }
    return $true
}

function Test-Tool {
    param([string]$Name, [version]$Minimum)

    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) {
        return $false
    }
    if ($null -eq $Minimum) {
        return $true
    }
    $output = (Invoke-NativeProbe -FilePath $command.Source -ArgumentList @('--version')).Output | Select-Object -First 1
    return ($output -match '(\d+\.\d+(\.\d+)?)') -and ([version]$Matches[1] -ge $Minimum)
}

if (-not $Force -and (Test-StagedRuntime)) {
    Write-Host "[nemo-speech] Using the staged runtime from $($commit.Substring(0, 8))."
    return
}

$python = Get-Command 'py.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $python) {
    throw "The Python launcher 'py.exe' was not found."
}

Write-Host "[nemo-speech] Preparing sources at $($commit.Substring(0, 8)) in $SourceDir"
if (-not (Test-Path -LiteralPath (Join-Path $SourceDir '.git'))) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $SourceDir) -Force | Out-Null
    Invoke-CheckedCommand -FilePath 'git' -ArgumentList @('clone', '--quiet', '--filter=blob:none', '--no-checkout', $repository, $SourceDir) -Description 'Cloning NeMo-Speech.cpp'
}
$probe = Invoke-NativeProbe -FilePath 'git' -ArgumentList @('-C', $SourceDir, 'rev-parse', '--verify', '--quiet', "$commit^{commit}")
if ($probe.ExitCode -ne 0) {
    Invoke-CheckedCommand -FilePath 'git' -ArgumentList @('-C', $SourceDir, 'fetch', '--quiet', 'origin', $commit) -Description 'Fetching the pinned commit'
}
Invoke-CheckedCommand -FilePath 'git' -ArgumentList @('-C', $SourceDir, 'checkout', '--quiet', '--detach', $commit) -Description 'Checking out the pinned commit'
Invoke-CheckedCommand -FilePath 'git' -ArgumentList @('-C', $SourceDir, 'submodule', 'update', '--init', 'ggml') -Description 'Fetching ggml'
# ASR builds read miniaudio from llama.cpp; a shallow copy is enough.
Invoke-CheckedCommand -FilePath 'git' -ArgumentList @('-C', $SourceDir, 'submodule', 'update', '--init', '--depth', '1', 'llama.cpp') -Description 'Fetching llama.cpp'

if (-not (Test-Tool 'cmake' ([version]'3.26')) -or -not (Test-Tool 'ninja')) {
    $tools = Join-Path $SourceDir '.tools'
    if (-not (Test-Path -LiteralPath (Join-Path $tools 'Scripts\cmake.exe'))) {
        Write-Host '[nemo-speech] Installing CMake and Ninja into the source folder...'
        Invoke-CheckedCommand -FilePath $python.Source -ArgumentList @('-m', 'venv', $tools) -Description 'Creating the tools environment'
        Invoke-CheckedCommand -FilePath (Join-Path $tools 'Scripts\python.exe') -ArgumentList @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', 'cmake', 'ninja') -Description 'Installing CMake and Ninja'
    }
    $env:Path = (Join-Path $tools 'Scripts') + ';' + $env:Path
}

if ($Force -and (Test-Path -LiteralPath $buildDir)) {
    Remove-Item -LiteralPath $buildDir -Recurse -Force
}
$cachePath = Join-Path $buildDir 'CMakeCache.txt'
if (-not (Test-Path -LiteralPath $cachePath)) {
    New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
    $seed = foreach ($flag in $portableFlags.GetEnumerator()) { "$($flag.Key):BOOL=$($flag.Value)" }
    Set-Content -LiteralPath $cachePath -Value $seed -Encoding ascii
}

Write-Host '[nemo-speech] Building the CPU runtime (ASR profile, which contains diarization)...'
# NeMo-Speech.cpp's driver targets Windows PowerShell, also when this script runs under pwsh.
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
Invoke-CheckedCommand -FilePath $windowsPowerShell -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', (Join-Path $SourceDir 'scripts\windows\build.ps1'),
    '-Backend', 'cpu', '-Profile', 'asr', '-BuildDir', $buildDir, '-Jobs', $Jobs
) -Description 'NeMo-Speech.cpp build'

$cache = Get-Content -LiteralPath $cachePath
foreach ($flag in $portableFlags.GetEnumerator()) {
    $line = $cache | Where-Object { $_ -match "^$($flag.Key):[A-Z]+=" } | Select-Object -First 1
    if ($null -eq $line -or $line -notmatch "=$($flag.Value)$") {
        throw "The runtime was not built portably ($($flag.Key)). Rerun with -Force."
    }
}

$bin = Join-Path $buildDir 'bin'
$staging = "$outputDir.staging"
if (Test-Path -LiteralPath $staging) {
    Remove-Item -LiteralPath $staging -Recurse -Force
}
New-Item -ItemType Directory -Path (Join-Path $staging 'licenses') -Force | Out-Null
$hashes = [ordered]@{}
foreach ($name in $libraries) {
    Copy-Item -LiteralPath (Join-Path $bin $name) -Destination $staging
    $hashes[$name] = (Get-FileHash -LiteralPath (Join-Path $staging $name) -Algorithm SHA256).Hash
}
foreach ($name in $licenses) {
    Copy-Item -LiteralPath (Join-Path $SourceDir $name) -Destination (Join-Path $staging 'licenses')
}
$manifest = [ordered]@{
    repository = $repository
    commit = $commit
    build = 'cpu-asr profile, portable AVX2 baseline, no OpenMP'
    flags = $portableFlags
    files = $hashes
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $staging 'runtime.json') -Encoding utf8

Invoke-CheckedCommand -FilePath $python.Source -ArgumentList @(
    '-c', 'import ctypes, sys; ctypes.CDLL(sys.argv[1]).nemo_speech_diar_create',
    (Join-Path $staging 'nemo_speech_asr_c.dll')
) -Description 'Loading the staged runtime'

if (Test-Path -LiteralPath $outputDir) {
    Remove-Item -LiteralPath $outputDir -Recurse -Force
}
New-Item -ItemType Directory -Path (Split-Path -Parent $outputDir) -Force | Out-Null
Move-Item -LiteralPath $staging -Destination $outputDir
Write-Host "[nemo-speech] Runtime staged in $outputDir"
