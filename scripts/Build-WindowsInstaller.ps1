[CmdletBinding()]
param(
    [ValidateSet('DevelopmentUnsigned', 'PublicRelease')]
    [string]$Mode = 'DevelopmentUnsigned',

    [string]$CertificateThumbprint,

    [string]$CertificateSubject,

    [ValidateSet('CurrentUser', 'LocalMachine')]
    [string]$CertificateStoreLocation = 'CurrentUser',

    [string]$TimestampUrl = 'http://timestamp.digicert.com',

    [string]$SignToolPath,

    [string]$IsccPath,

    [switch]$SkipDependencyInstall,

    [switch]$Force
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$modulePath = Join-Path $PSScriptRoot 'Ownkey.Signing.psm1'
$installerScript = Join-Path $repoRoot 'installer\Ownkey.iss'
$verifyScript = Join-Path $PSScriptRoot 'Verify-WindowsRelease.ps1'
$innoSigningScript = Join-Path $PSScriptRoot 'Invoke-InnoSigning.ps1'

Import-Module $modulePath -Force

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

function Resolve-IsccPath {
    param([string]$ConfiguredPath)

    if (-not [string]::IsNullOrWhiteSpace($ConfiguredPath)) {
        $resolved = [IO.Path]::GetFullPath($ConfiguredPath)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "ISCC.exe was not found at the configured path: $resolved"
        }
        return $resolved
    }

    $command = Get-Command 'ISCC.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        return $command.Source
    }

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    }
    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $candidates += (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }

    throw 'ISCC.exe was not found. Install Inno Setup 6 or pass -IsccPath.'
}

function Get-OwnkeyVersion {
    $content = Get-Content -LiteralPath $installerScript -Raw
    $match = [regex]::Match($content, '(?m)^#define\s+MyAppVersion\s+"([^"]+)"\s*$')
    if (-not $match.Success) {
        throw 'Could not read MyAppVersion from installer\Ownkey.iss.'
    }
    return $match.Groups[1].Value
}

function Assert-VersionConsistency {
    param([Parameter(Mandatory = $true)][string]$Version)

    $packageJson = Get-Content -LiteralPath (Join-Path $repoRoot 'overlay-ui\package.json') -Raw | ConvertFrom-Json
    $tauriJson = Get-Content -LiteralPath (Join-Path $repoRoot 'overlay-ui\src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
    $cargoContent = Get-Content -LiteralPath (Join-Path $repoRoot 'overlay-ui\src-tauri\Cargo.toml') -Raw
    $cargoMatch = [regex]::Match($cargoContent, '(?ms)^\[package\].*?^version\s*=\s*"([^"]+)"')
    if (-not $cargoMatch.Success) {
        throw 'Could not read the package version from overlay-ui\src-tauri\Cargo.toml.'
    }

    $versions = [ordered]@{
        'installer\Ownkey.iss'                 = $Version
        'overlay-ui\package.json'              = [string]$packageJson.version
        'overlay-ui\src-tauri\tauri.conf.json' = [string]$tauriJson.version
        'overlay-ui\src-tauri\Cargo.toml'       = $cargoMatch.Groups[1].Value
    }
    $mismatches = @($versions.GetEnumerator() | Where-Object { $_.Value -cne $Version })
    if ($mismatches.Count -gt 0) {
        $details = $mismatches | ForEach-Object { "$($_.Key)=$($_.Value)" }
        throw "Version metadata is inconsistent with $Version`: $($details -join ', ')"
    }
}

function Invoke-BackendBuild {
    param(
        [Parameter(Mandatory = $true)][string]$DistPath,
        [Parameter(Mandatory = $true)][string]$WorkPath
    )

    $python = Get-Command 'py.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $python) {
        throw "The Python launcher 'py.exe' was not found."
    }

    if (-not $SkipDependencyInstall) {
        Write-Host '[backend] Installing PyInstaller...'
        Invoke-CheckedCommand -FilePath $python.Source -ArgumentList @(
            '-m', 'pip', 'install', '--disable-pip-version-check', '-q', 'pyinstaller'
        ) -Description 'PyInstaller dependency installation'
    }

    Write-Host '[backend] Building the PyInstaller directory bundle...'
    Invoke-CheckedCommand -FilePath $python.Source -ArgumentList @(
        '-m', 'PyInstaller',
        '--noconfirm',
        '--clean',
        '--distpath', $DistPath,
        '--workpath', $WorkPath,
        $installerSpec
    ) -Description 'PyInstaller build'
}

function Invoke-OverlayBuild {
    $pnpm = Get-Command 'pnpm.cmd' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $pnpm) {
        $pnpm = Get-Command 'pnpm' -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($null -eq $pnpm) {
        throw "The 'pnpm' command was not found."
    }

    Push-Location (Join-Path $repoRoot 'overlay-ui')
    try {
        if (-not $SkipDependencyInstall) {
            Write-Host '[overlay] Installing locked Node.js dependencies...'
            Invoke-CheckedCommand -FilePath $pnpm.Source -ArgumentList @('install', '--frozen-lockfile') -Description 'pnpm install'
        }

        Write-Host '[overlay] Building the Tauri release executable without standalone Tauri installers...'
        Invoke-CheckedCommand -FilePath $pnpm.Source -ArgumentList @('build:binary') -Description 'Tauri overlay binary build'
    }
    finally {
        Pop-Location
    }
}

function Write-Utf8Json {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $json = $Value | ConvertTo-Json -Depth 8
    $utf8WithoutBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $utf8WithoutBom)
}

function Remove-TemporaryBuildDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (Test-Path -LiteralPath $Path) {
        $safePath = Assert-OwnkeyPathUnderRoot -Path $Path -Root (Join-Path $repoRoot 'build')
        Remove-Item -LiteralPath $safePath -Recurse -Force
    }
}

$installerSpec = Join-Path $repoRoot 'Ownkey.spec'
$version = Get-OwnkeyVersion
Assert-VersionConsistency -Version $version
$resolvedIsccPath = Resolve-IsccPath -ConfiguredPath $IsccPath
$overlayBuildOutput = Join-Path $repoRoot 'overlay-ui\src-tauri\target\release\ownkey-overlay.exe'

Push-Location $repoRoot
try {
    if ($Mode -eq 'DevelopmentUnsigned') {
        if (-not [string]::IsNullOrWhiteSpace($CertificateThumbprint) -or
            -not [string]::IsNullOrWhiteSpace($CertificateSubject)) {
            throw 'Certificate selectors are not accepted in DevelopmentUnsigned mode. Use build-release.ps1 for public signing.'
        }

        $backendDistRoot = Join-Path $repoRoot 'dist'
        $backendWorkRoot = Join-Path $repoRoot 'build'
        $backendSource = Join-Path $backendDistRoot 'Ownkey'
        $developmentOutput = Join-Path $repoRoot 'dist-installer-dev'
        New-Item -ItemType Directory -Path $developmentOutput -Force | Out-Null

        Write-Host 'Building an UNSIGNED DEVELOPMENT installer.' -ForegroundColor Yellow
        Invoke-BackendBuild -DistPath $backendDistRoot -WorkPath $backendWorkRoot
        Invoke-OverlayBuild

        $backendExecutable = Join-Path $backendSource 'Ownkey.exe'
        if (-not (Test-Path -LiteralPath $backendExecutable -PathType Leaf)) {
            throw "The backend executable was not produced: $backendExecutable"
        }
        if (-not (Test-Path -LiteralPath $overlayBuildOutput -PathType Leaf)) {
            throw "The overlay executable was not produced: $overlayBuildOutput"
        }

        $developmentBaseName = "Ownkey-Setup-$version-UNSIGNED-DEV"
        $innoArguments = @(
            "/DBackendSourceDir=$backendSource",
            "/DOverlaySourceFile=$overlayBuildOutput",
            "/O$developmentOutput",
            "/F$developmentBaseName",
            '/Qp',
            $installerScript
        )
        Write-Host '[installer] Compiling without a SignTool...'
        Invoke-CheckedCommand -FilePath $resolvedIsccPath -ArgumentList $innoArguments -Description 'Unsigned Inno Setup build'

        $developmentInstaller = Join-Path $developmentOutput ($developmentBaseName + '.exe')
        if (-not (Test-Path -LiteralPath $developmentInstaller -PathType Leaf)) {
            throw "The unsigned development installer was not produced: $developmentInstaller"
        }

        $noticePath = Join-Path $developmentOutput 'UNSIGNED-DEVELOPMENT-BUILD.txt'
        $notice = @(
            'This directory contains unsigned development output.',
            'It is not qualified for a public Ownkey release.',
            'Use build-release.ps1 with a publicly trusted code-signing certificate for release output.'
        ) -join [Environment]::NewLine
        [IO.File]::WriteAllText($noticePath, $notice + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))

        Write-Host ''
        Write-Host 'Unsigned development installer created:' -ForegroundColor Yellow
        Write-Host "  $developmentInstaller"
        return
    }

    Write-Host '[preflight] Validating public-release signing inputs...'
    $normalizedTimestampUrl = Assert-OwnkeyTimestampUrl -TimestampUrl $TimestampUrl
    $certificateSelection = Resolve-OwnkeySigningCertificate `
        -CertificateThumbprint $CertificateThumbprint `
        -CertificateSubject $CertificateSubject `
        -CertificateStoreLocation $CertificateStoreLocation
    $certificate = $certificateSelection.Certificate
    $certificateSummary = Assert-OwnkeyCertificateForPublicRelease -Certificate $certificate
    $resolvedSignToolPath = Resolve-OwnkeySignTool -SignToolPath $SignToolPath
    $gitCommit = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $gitCommit -notmatch '^[0-9a-fA-F]{40}$') {
        throw 'Could not read a full Git commit ID for release metadata.'
    }
    $trackedChanges = @(& git status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not read the tracked Git status for release preflight.'
    }
    if ($trackedChanges.Count -gt 0) {
        throw 'Public-release builds require a clean tracked worktree. Commit or revert tracked changes first.'
    }

    $releaseOutput = Join-Path $repoRoot 'dist-release'
    if ((Test-Path -LiteralPath $releaseOutput) -and -not $Force) {
        throw "Release output already exists at '$releaseOutput'. Remove it or rerun with -Force."
    }

    $releaseOperationRoot = Join-Path (Join-Path $repoRoot 'build') ("release-operation-" + [guid]::NewGuid().ToString('N'))
    $releaseOperationRoot = Assert-OwnkeyPathUnderRoot -Path $releaseOperationRoot -Root (Join-Path $repoRoot 'build')
    $candidateRoot = Join-Path $releaseOperationRoot 'candidate'
    $promoted = $false
    try {
        $unsignedBackendDist = Join-Path $releaseOperationRoot 'unsigned-backend'
        $backendWorkRoot = Join-Path $releaseOperationRoot 'pyinstaller-work'
        $payloadRoot = Join-Path $candidateRoot 'payload'
        $backendPayload = Join-Path $payloadRoot 'backend'
        $overlayPayload = Join-Path $payloadRoot 'ownkey-overlay.exe'
        $innoCaptureRoot = Join-Path $candidateRoot 'signed-inno-images'
        New-Item -ItemType Directory -Path $candidateRoot, $payloadRoot, $innoCaptureRoot -Force | Out-Null

        Invoke-BackendBuild -DistPath $unsignedBackendDist -WorkPath $backendWorkRoot
        Invoke-OverlayBuild

        $unsignedBackendSource = Join-Path $unsignedBackendDist 'Ownkey'
        if (-not (Test-Path -LiteralPath (Join-Path $unsignedBackendSource 'Ownkey.exe') -PathType Leaf)) {
            throw 'PyInstaller did not produce Ownkey.exe in the isolated release candidate.'
        }
        if (-not (Test-Path -LiteralPath $overlayBuildOutput -PathType Leaf)) {
            throw "The overlay executable was not produced: $overlayBuildOutput"
        }

        Copy-Item -LiteralPath $unsignedBackendSource -Destination $backendPayload -Recurse
        Copy-Item -LiteralPath $overlayBuildOutput -Destination $overlayPayload

        $payloadExecutables = @(Get-ChildItem -LiteralPath $payloadRoot -Recurse -File -Filter '*.exe')
        if ($payloadExecutables.Count -lt 2) {
            throw "Expected at least the backend and overlay executables in '$payloadRoot'."
        }

        foreach ($executable in $payloadExecutables) {
            Write-Host "[sign] $($executable.FullName)"
            Invoke-OwnkeySignFile `
                -Path $executable.FullName `
                -Certificate $certificate `
                -CertificateStoreLocation $CertificateStoreLocation `
                -TimestampUrl $normalizedTimestampUrl `
                -SignToolPath $resolvedSignToolPath
            Get-OwnkeyAuthenticodeRecord `
                -Path $executable.FullName `
                -SignToolPath $resolvedSignToolPath `
                -ExpectedCertificateThumbprint $certificateSummary.Thumbprint | Out-Null
        }

        $releaseBaseName = "Ownkey-Setup-$version"
        $innoSignTool = Get-OwnkeyInnoSignToolDefinition `
            -PowerShellPath (Join-Path $PSHOME 'powershell.exe') `
            -SigningScriptPath $innoSigningScript
        $innoArguments = @(
            '/DReleaseSigning=1',
            "/DBackendSourceDir=$backendPayload",
            "/DOverlaySourceFile=$overlayPayload",
            "/O$candidateRoot",
            "/F$releaseBaseName",
            "/Sownkey_release_sha256=$innoSignTool",
            '/Qp',
            $installerScript
        )

        Write-Host '[installer] Compiling with signed installer and signed uninstaller enforcement...'
        $innoEnvironmentNames = @(
            'OWNKEY_SIGN_CERT_THUMBPRINT',
            'OWNKEY_SIGN_CERT_STORE_LOCATION',
            'OWNKEY_SIGN_TIMESTAMP_URL',
            'OWNKEY_SIGNTOOL_PATH',
            'OWNKEY_INNO_SIGN_CAPTURE_DIR',
            'OWNKEY_INNO_SIGNING_POLICY'
        )
        $previousInnoEnvironment = @{}
        foreach ($name in $innoEnvironmentNames) {
            $previousInnoEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        }
        try {
            [Environment]::SetEnvironmentVariable('OWNKEY_SIGN_CERT_THUMBPRINT', $certificateSummary.Thumbprint, 'Process')
            [Environment]::SetEnvironmentVariable('OWNKEY_SIGN_CERT_STORE_LOCATION', $CertificateStoreLocation, 'Process')
            [Environment]::SetEnvironmentVariable('OWNKEY_SIGN_TIMESTAMP_URL', $normalizedTimestampUrl, 'Process')
            [Environment]::SetEnvironmentVariable('OWNKEY_SIGNTOOL_PATH', $resolvedSignToolPath, 'Process')
            [Environment]::SetEnvironmentVariable('OWNKEY_INNO_SIGN_CAPTURE_DIR', $innoCaptureRoot, 'Process')
            [Environment]::SetEnvironmentVariable('OWNKEY_INNO_SIGNING_POLICY', 'PublicRelease', 'Process')
            Invoke-CheckedCommand -FilePath $resolvedIsccPath -ArgumentList $innoArguments -Description 'Signed Inno Setup build'
        }
        finally {
            foreach ($name in $innoEnvironmentNames) {
                [Environment]::SetEnvironmentVariable($name, $previousInnoEnvironment[$name], 'Process')
            }
        }

        $releaseInstaller = Join-Path $candidateRoot ($releaseBaseName + '.exe')
        if (-not (Test-Path -LiteralPath $releaseInstaller -PathType Leaf)) {
            throw "The signed installer was not produced: $releaseInstaller"
        }

        $capturedInnoImages = @(Get-ChildItem -LiteralPath $innoCaptureRoot -File -Filter '*.exe')
        if ($capturedInnoImages.Count -lt 2) {
            throw 'The Inno signing wrapper did not capture both installer and generated-uninstaller images.'
        }
        $installerHash = (Get-FileHash -LiteralPath $releaseInstaller -Algorithm SHA256).Hash
        $matchingInstallerCaptures = @($capturedInnoImages | Where-Object {
            $_.Name -like 'inno-installer-*.exe' -and
            (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash -ceq $installerHash
        })
        $generatedUninstallerCaptures = @($capturedInnoImages | Where-Object {
            $_.Name -like 'inno-uninstaller-*.exe' -and
            (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash -cne $installerHash
        })
        if ($matchingInstallerCaptures.Count -eq 0 -or $generatedUninstallerCaptures.Count -eq 0) {
            throw 'Captured Inno images do not include the final installer and a distinct generated uninstaller.'
        }

        $candidateExecutables = @(Get-ChildItem -LiteralPath $candidateRoot -Recurse -File -Filter '*.exe')
        $relativeExecutables = $candidateExecutables.FullName |
            ForEach-Object { $_.Substring($candidateRoot.Length + 1).Replace('\', '/') } |
            Sort-Object
        $metadata = [ordered]@{
            schemaVersion          = 1
            mode                   = 'PublicRelease'
            product                = 'Ownkey for Windows'
            version                = $version
            gitCommit              = $gitCommit
            trackedWorktreeDirty   = $false
            builtAtUtc             = (Get-Date).ToUniversalTime().ToString('o')
            publisherSubject       = $certificateSummary.Subject
            signerThumbprint       = $certificateSummary.Thumbprint
            certificateStore       = "$CertificateStoreLocation\My"
            timestampProtocol      = 'RFC3161'
            timestampDigest        = 'SHA256'
            timestampUrl           = $normalizedTimestampUrl
            expectedExecutableFiles = $relativeExecutables
        }
        Write-Utf8Json -Value $metadata -Path (Join-Path $candidateRoot 'release-metadata.json')

        $candidateReport = Join-Path $candidateRoot 'verification-report.json'
        $verifyArguments = @(
            '-NoProfile',
            '-NonInteractive',
            '-ExecutionPolicy', 'Bypass',
            '-File', $verifyScript,
            '-ArtifactsDirectory', $candidateRoot,
            '-ExpectedVersion', $version,
            '-ExpectedCertificateThumbprint', $certificateSummary.Thumbprint,
            '-ExpectedCertificateSubject', $certificateSummary.Subject,
            '-SignToolPath', $resolvedSignToolPath,
            '-ReportPath', $candidateReport
        )
        Write-Host '[verify] Running the independent verifier against the candidate...'
        Invoke-CheckedCommand -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $verifyArguments -Description 'Independent release verification'

        if (Test-Path -LiteralPath $releaseOutput) {
            if (-not $Force) {
                throw "Release output appeared during the build at '$releaseOutput'; refusing to replace it without -Force."
            }
            $safeReleaseOutput = Assert-OwnkeyPathUnderRoot -Path $releaseOutput -Root $repoRoot
            Remove-Item -LiteralPath $safeReleaseOutput -Recurse -Force
        }
        $promoted = $true
        Copy-Item -LiteralPath $candidateRoot -Destination $releaseOutput -Recurse

        $finalReport = Join-Path $releaseOutput 'verification-report.json'
        $finalVerifyArguments = @(
            '-NoProfile',
            '-NonInteractive',
            '-ExecutionPolicy', 'Bypass',
            '-File', $verifyScript,
            '-ArtifactsDirectory', $releaseOutput,
            '-ExpectedVersion', $version,
            '-ExpectedCertificateThumbprint', $certificateSummary.Thumbprint,
            '-ExpectedCertificateSubject', $certificateSummary.Subject,
            '-SignToolPath', $resolvedSignToolPath,
            '-ReportPath', $finalReport
        )
        Write-Host '[verify] Rechecking the promoted release directory...'
        Invoke-CheckedCommand -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $finalVerifyArguments -Description 'Promoted release verification'

        Write-Host ''
        Write-Host 'Public-release artifacts created and verified:' -ForegroundColor Green
        Write-Host "  $releaseOutput"
    }
    catch {
        if ($promoted -and (Test-Path -LiteralPath $releaseOutput)) {
            $safeReleaseOutput = Assert-OwnkeyPathUnderRoot -Path $releaseOutput -Root $repoRoot
            Remove-Item -LiteralPath $safeReleaseOutput -Recurse -Force
        }
        throw
    }
    finally {
        Remove-TemporaryBuildDirectory -Path $releaseOperationRoot
    }
}
catch {
    throw
}
finally {
    Pop-Location
}
