[CmdletBinding()]
param(
    [string]$TimestampUrl = 'http://timestamp.digicert.com',

    [string]$SignToolPath,

    [string]$IsccPath,

    [switch]$KeepArtifacts
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = Join-Path $repoRoot 'build'
$probeRoot = Join-Path $buildRoot ("signing-local-test-" + [guid]::NewGuid().ToString('N'))
$probeRoot = [IO.Path]::GetFullPath($probeRoot)
$certificate = $null

Import-Module (Join-Path $repoRoot 'scripts\Ownkey.Signing.psm1') -Force

function Resolve-LocalTestIscc {
    param([string]$ConfiguredPath)

    if (-not [string]::IsNullOrWhiteSpace($ConfiguredPath)) {
        $resolved = [IO.Path]::GetFullPath($ConfiguredPath)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "ISCC.exe was not found at '$resolved'."
        }
        return $resolved
    }

    $command = Get-Command 'ISCC.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        return $command.Source
    }

    $defaultPath = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
    if (Test-Path -LiteralPath $defaultPath -PathType Leaf) {
        return $defaultPath
    }
    throw 'ISCC.exe was not found. Install Inno Setup 6 or pass -IsccPath.'
}

function Invoke-LocalTestCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$Description
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Write-LocalTestJson {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $json = $Value | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
}

try {
    Assert-OwnkeyPathUnderRoot -Path $probeRoot -Root $buildRoot | Out-Null
    $backendSource = Join-Path $repoRoot 'dist\Ownkey'
    $overlaySource = Join-Path $repoRoot 'overlay-ui\src-tauri\target\release\ownkey-overlay.exe'
    if (-not (Test-Path -LiteralPath (Join-Path $backendSource 'Ownkey.exe') -PathType Leaf) -or
        -not (Test-Path -LiteralPath $overlaySource -PathType Leaf)) {
        throw 'Unsigned development payloads are missing. Run build-installer.bat first.'
    }

    $installerContent = Get-Content -LiteralPath (Join-Path $repoRoot 'installer\Ownkey.iss') -Raw
    $versionMatch = [regex]::Match($installerContent, '(?m)^#define\s+MyAppVersion\s+"([^"]+)"\s*$')
    if (-not $versionMatch.Success) {
        throw 'Could not read MyAppVersion from installer\Ownkey.iss.'
    }
    $version = $versionMatch.Groups[1].Value

    $payloadRoot = Join-Path $probeRoot 'SELF-SIGNED-LOCAL-TEST-payload'
    $backendPayload = Join-Path $payloadRoot 'backend'
    $overlayPayload = Join-Path $payloadRoot 'ownkey-overlay.exe'
    $innoCaptureRoot = Join-Path $probeRoot 'SELF-SIGNED-LOCAL-TEST-inno-images'
    New-Item -ItemType Directory -Path $probeRoot, $payloadRoot, $innoCaptureRoot -Force | Out-Null
    Copy-Item -LiteralPath $backendSource -Destination $backendPayload -Recurse
    Copy-Item -LiteralPath $overlaySource -Destination $overlayPayload

    $subject = "CN=Ownkey Signing Pipeline Local Test $([guid]::NewGuid().ToString('N'))"
    $certificate = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $subject `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -KeyExportPolicy NonExportable `
        -NotBefore (Get-Date).AddMinutes(-5) `
        -NotAfter (Get-Date).AddHours(1)

    $publicReleaseRejection = $null
    try {
        Assert-OwnkeyCertificateForPublicRelease -Certificate $certificate | Out-Null
    }
    catch {
        $publicReleaseRejection = $_.Exception.Message
    }
    if ($publicReleaseRejection -notmatch 'self-signed') {
        throw "Public certificate policy did not reject the test certificate as self-signed: $publicReleaseRejection"
    }

    $resolvedSignTool = Resolve-OwnkeySignTool -SignToolPath $SignToolPath
    $resolvedTimestampUrl = Assert-OwnkeyTimestampUrl -TimestampUrl $TimestampUrl
    $payloadExecutables = @(Get-ChildItem -LiteralPath $payloadRoot -Recurse -File -Filter '*.exe')
    if ($payloadExecutables.Count -lt 2) {
        throw 'The local test payload does not contain both Ownkey executables.'
    }

    foreach ($file in $payloadExecutables) {
        Invoke-OwnkeySignFile `
            -Path $file.FullName `
            -Certificate $certificate `
            -CertificateStoreLocation CurrentUser `
            -TimestampUrl $resolvedTimestampUrl `
            -SignToolPath $resolvedSignTool
    }

    $innoDefinition = Get-OwnkeyInnoSignToolDefinition `
        -PowerShellPath (Join-Path $PSHOME 'powershell.exe') `
        -SigningScriptPath (Join-Path $repoRoot 'scripts\Invoke-InnoSigning.ps1')
    $resolvedIscc = Resolve-LocalTestIscc -ConfiguredPath $IsccPath
    $installerBaseName = "Ownkey-Setup-$version-SELF-SIGNED-LOCAL-TEST"
    $installerPath = Join-Path $probeRoot ($installerBaseName + '.exe')
    $innoArguments = @(
        '/DReleaseSigning=1',
        "/DBackendSourceDir=$backendPayload",
        "/DOverlaySourceFile=$overlayPayload",
        "/O$probeRoot",
        "/F$installerBaseName",
        "/Sownkey_release_sha256=$innoDefinition",
        '/Qp',
        (Join-Path $repoRoot 'installer\Ownkey.iss')
    )
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
        [Environment]::SetEnvironmentVariable('OWNKEY_SIGN_CERT_THUMBPRINT', $certificate.Thumbprint, 'Process')
        [Environment]::SetEnvironmentVariable('OWNKEY_SIGN_CERT_STORE_LOCATION', 'CurrentUser', 'Process')
        [Environment]::SetEnvironmentVariable('OWNKEY_SIGN_TIMESTAMP_URL', $resolvedTimestampUrl, 'Process')
        [Environment]::SetEnvironmentVariable('OWNKEY_SIGNTOOL_PATH', $resolvedSignTool, 'Process')
        [Environment]::SetEnvironmentVariable('OWNKEY_INNO_SIGN_CAPTURE_DIR', $innoCaptureRoot, 'Process')
        [Environment]::SetEnvironmentVariable('OWNKEY_INNO_SIGNING_POLICY', 'SelfSignedLocalTest', 'Process')
        Invoke-LocalTestCommand -FilePath $resolvedIscc -ArgumentList $innoArguments -Description 'Self-signed local Inno integration test'
    }
    finally {
        foreach ($name in $innoEnvironmentNames) {
            [Environment]::SetEnvironmentVariable($name, $previousInnoEnvironment[$name], 'Process')
        }
    }

    $capturedInnoImages = @(Get-ChildItem -LiteralPath $innoCaptureRoot -File -Filter '*.exe')
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf) -or $capturedInnoImages.Count -lt 2) {
        throw 'Inno Setup did not produce the installer plus captured installer and uninstaller signing images.'
    }
    $installerHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash
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

    $allExecutables = @(Get-ChildItem -LiteralPath $probeRoot -Recurse -File -Filter '*.exe' | Sort-Object FullName)
    $verifiedRecords = foreach ($file in $allExecutables) {
        $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
        if ($signature.Status -in @(
                [Management.Automation.SignatureStatus]::NotSigned,
                [Management.Automation.SignatureStatus]::HashMismatch,
                [Management.Automation.SignatureStatus]::NotSupportedFileFormat,
                [Management.Automation.SignatureStatus]::Incompatible
            )) {
            throw "The local-test signature on '$($file.FullName)' is structurally invalid: $($signature.Status)"
        }
        if ($null -eq $signature.SignerCertificate -or
            $signature.SignerCertificate.Thumbprint -cne $certificate.Thumbprint -or
            $null -eq $signature.TimeStamperCertificate) {
            throw "The local-test signature or timestamp is missing on '$($file.FullName)'."
        }

        $previousErrorPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $signToolOutput = @(& $resolvedSignTool verify /pa /all /tw /v $file.FullName 2>&1 | ForEach-Object { $_.ToString() })
            $signToolExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorPreference
        }
        $signToolText = $signToolOutput -join [Environment]::NewLine
        $trustFailureOnly = $signToolExitCode -eq 0 -or $signToolText -match '(?i)(0x800B0109|CERT_E_UNTRUSTEDROOT|root\s+certificate\s+which\s+is\s+not\s+trusted|basiscertificaat[\s\S]+niet[\s\S]+vertrouwd)'
        if (-not $trustFailureOnly) {
            throw "SignTool failed for a reason other than the expected self-signed trust chain on '$($file.FullName)': $signToolText"
        }

        [pscustomobject]@{
            File                        = $file.FullName.Substring($probeRoot.Length + 1).Replace('\', '/')
            AuthenticodeStatus          = $signature.Status.ToString()
            SignToolExitCode            = $signToolExitCode
            TrustFailureOnly            = $trustFailureOnly
            Sha256                      = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            SignerSubject               = $signature.SignerCertificate.Subject
            TimestampSubject            = $signature.TimeStamperCertificate.Subject
        }
    }

    $publisherSubjects = @($allExecutables | ForEach-Object {
        (Get-AuthenticodeSignature -LiteralPath $_.FullName).SignerCertificate.Subject
    } | Sort-Object -Unique)
    if ($publisherSubjects.Count -ne 1) {
        throw 'The local-test executables do not have one consistent publisher subject.'
    }

    $localMetadata = [ordered]@{
        schemaVersion           = 1
        mode                    = 'LocalSigningTest'
        version                 = $version
        publisherSubject        = $certificate.Subject
        signerThumbprint        = $certificate.Thumbprint
        expectedExecutableFiles = @($verifiedRecords.File)
    }
    Write-LocalTestJson -Value $localMetadata -Path (Join-Path $probeRoot 'release-metadata.json')

    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & (Join-Path $PSHOME 'powershell.exe') `
            -NoProfile `
            -NonInteractive `
            -ExecutionPolicy Bypass `
            -File (Join-Path $repoRoot 'scripts\Verify-WindowsRelease.ps1') `
            -ArtifactsDirectory $probeRoot *> $null
        $publicVerifierExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorPreference
    }
    if ($publicVerifierExitCode -eq 0) {
        throw 'The public verifier accepted self-signed local-test output.'
    }

    $windowsDefaultPolicyValid = @($verifiedRecords | Where-Object { $_.SignToolExitCode -ne 0 }).Count -eq 0
    $report = [ordered]@{
        schemaVersion                 = 1
        mode                          = 'SELF-SIGNED-LOCAL-TEST'
        publicReleaseEligible         = $false
        publicReleaseRejection        = $publicReleaseRejection
        publicVerifierExitCode        = $publicVerifierExitCode
        cryptographicIntegrityVerified = $true
        windowsDefaultPolicyValid     = $windowsDefaultPolicyValid
        publicTrustChainSuitable      = $false
        verificationBasis             = 'Authenticode hash was not HashMismatch; signer and RFC3161 timestamp were present; SignTool either succeeded under local policy or reported only the self-signed trust-chain failure.'
        executableCount               = $verifiedRecords.Count
        payloadExecutableCount        = $payloadExecutables.Count
        generatedUninstallerCaptureCount = $generatedUninstallerCaptures.Count
        publisherIdentityCount        = $publisherSubjects.Count
        artifacts                     = @($verifiedRecords)
    }
    Write-LocalTestJson -Value $report -Path (Join-Path $probeRoot 'SELF-SIGNED-LOCAL-TEST-report.json')

    Write-Host 'Self-signed local signing integration test passed.' -ForegroundColor Green
    Write-Host "Public release eligible: $($report.publicReleaseEligible)"
    Write-Host "Public policy rejection: $publicReleaseRejection"
    Write-Host "Cryptographically checked self-signed executables: $($verifiedRecords.Count)"
    Write-Host "Windows default trust policy valid: $($report.windowsDefaultPolicyValid)"
    Write-Host "Captured generated uninstallers: $($generatedUninstallerCaptures.Count)"
    Write-Host "Public verifier exit code: $publicVerifierExitCode"
    if ($KeepArtifacts) {
        Write-Host "Clearly marked local-test artifacts kept at: $probeRoot" -ForegroundColor Yellow
    }
}
finally {
    if ($null -ne $certificate) {
        $privateCertificatePath = "Cert:\CurrentUser\My\$($certificate.Thumbprint)"
        if (Test-Path -LiteralPath $privateCertificatePath) {
            Remove-Item -LiteralPath $privateCertificatePath -Force
        }
    }

    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $probeRoot)) {
        $safeProbeRoot = Assert-OwnkeyPathUnderRoot -Path $probeRoot -Root $buildRoot
        Remove-Item -LiteralPath $safeProbeRoot -Recurse -Force
    }
}
