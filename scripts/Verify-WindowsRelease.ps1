[CmdletBinding()]
param(
    [string]$ArtifactsDirectory = (Join-Path ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))) 'dist-release'),

    [string]$ExpectedVersion,

    [string]$ExpectedCertificateThumbprint,

    [string]$ExpectedCertificateSubject,

    [string]$SignToolPath,

    [string]$ReportPath
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Ownkey.Signing.psm1') -Force

function Get-RelativeArtifactPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $safePath = Assert-OwnkeyPathUnderRoot -Path $Path -Root $Root
    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    return $safePath.Substring($fullRoot.Length + 1).Replace('\', '/')
}

function Write-Utf8Json {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $json = $Value | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
}

try {
    $artifactsRoot = [IO.Path]::GetFullPath($ArtifactsDirectory)
    if (-not (Test-Path -LiteralPath $artifactsRoot -PathType Container)) {
        throw "The release artifact directory does not exist: $artifactsRoot"
    }

    $metadataPath = Join-Path $artifactsRoot 'release-metadata.json'
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
        throw "Release metadata is missing: $metadataPath"
    }
    $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
    if ([int]$metadata.schemaVersion -ne 1) {
        throw "Unsupported release metadata schema: $($metadata.schemaVersion)"
    }
    if ([string]$metadata.mode -cne 'PublicRelease') {
        throw "Release metadata mode must be PublicRelease, found '$($metadata.mode)'."
    }
    if ([string]$metadata.product -cne 'Ownkey for Windows' -or
        [string]$metadata.timestampProtocol -cne 'RFC3161' -or
        [string]$metadata.timestampDigest -cne 'SHA256' -or
        [bool]$metadata.trackedWorktreeDirty) {
        throw 'Release metadata does not describe a clean Ownkey RFC3161/SHA-256 public build.'
    }
    if ([string]$metadata.gitCommit -notmatch '^[0-9a-fA-F]{40}$') {
        throw 'Release metadata does not contain a full Git commit ID.'
    }

    if ([string]::IsNullOrWhiteSpace($ExpectedVersion)) {
        $ExpectedVersion = [string]$metadata.version
    }
    elseif ([string]$metadata.version -cne $ExpectedVersion) {
        throw "Release metadata version '$($metadata.version)' does not match expected version '$ExpectedVersion'."
    }
    if ($ExpectedVersion -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
        throw "The expected release version has an unsupported format: $ExpectedVersion"
    }

    if ([string]::IsNullOrWhiteSpace($ExpectedCertificateThumbprint)) {
        $ExpectedCertificateThumbprint = [string]$metadata.signerThumbprint
    }
    $ExpectedCertificateThumbprint = ConvertTo-OwnkeyNormalizedThumbprint -Thumbprint $ExpectedCertificateThumbprint
    if ((ConvertTo-OwnkeyNormalizedThumbprint -Thumbprint ([string]$metadata.signerThumbprint)) -cne $ExpectedCertificateThumbprint) {
        throw 'Release metadata names a different signer thumbprint than the verifier expects.'
    }

    if ([string]::IsNullOrWhiteSpace($ExpectedCertificateSubject)) {
        $ExpectedCertificateSubject = [string]$metadata.publisherSubject
    }
    elseif ([string]$metadata.publisherSubject -cne $ExpectedCertificateSubject) {
        throw 'Release metadata names a different publisher subject than the verifier expects.'
    }

    $resolvedSignToolPath = Resolve-OwnkeySignTool -SignToolPath $SignToolPath
    $installerPath = Join-Path $artifactsRoot "Ownkey-Setup-$ExpectedVersion.exe"
    $backendPath = Join-Path $artifactsRoot 'payload\backend\Ownkey.exe'
    $overlayPath = Join-Path $artifactsRoot 'payload\ownkey-overlay.exe'
    $innoCaptureRoot = Join-Path $artifactsRoot 'signed-inno-images'

    foreach ($requiredPath in @($installerPath, $backendPath, $overlayPath)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Required release executable is missing: $requiredPath"
        }
    }
    if (-not (Test-Path -LiteralPath $innoCaptureRoot -PathType Container)) {
        throw "The signed Inno image capture directory is missing: $innoCaptureRoot"
    }

    $capturedInnoImages = @(Get-ChildItem -LiteralPath $innoCaptureRoot -File -Filter '*.exe')
    if ($capturedInnoImages.Count -lt 2) {
        throw 'The release does not contain captured installer and generated-uninstaller signing images.'
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
    if ($matchingInstallerCaptures.Count -eq 0) {
        throw 'No captured Inno signing image matches the final installer.'
    }
    if ($generatedUninstallerCaptures.Count -eq 0) {
        throw 'No distinct generated-uninstaller signing image was captured.'
    }
    foreach ($capturedImage in $capturedInnoImages) {
        $capturedHash = (Get-FileHash -LiteralPath $capturedImage.FullName -Algorithm SHA256).Hash
        $captureRecordPath = Join-Path $innoCaptureRoot ("inno-$capturedHash.json")
        if (-not (Test-Path -LiteralPath $captureRecordPath -PathType Leaf)) {
            throw "Capture metadata is missing for '$($capturedImage.FullName)'."
        }
        $captureRecord = Get-Content -LiteralPath $captureRecordPath -Raw | ConvertFrom-Json
        if ([string]$captureRecord.signingPolicy -cne 'PublicRelease' -or
            [string]$captureRecord.sha256 -cne $capturedHash -or
            [string]$captureRecord.capturedFileName -cne $capturedImage.Name -or
            (ConvertTo-OwnkeyNormalizedThumbprint -Thumbprint ([string]$captureRecord.signerThumbprint)) -cne $ExpectedCertificateThumbprint -or
            [string]$captureRecord.signerSubject -cne $ExpectedCertificateSubject) {
            throw "Capture metadata does not match '$($capturedImage.FullName)'."
        }
    }

    $allExecutables = @(Get-ChildItem -LiteralPath $artifactsRoot -Recurse -File -Filter '*.exe' | Sort-Object FullName)
    if ($allExecutables.Count -lt 5) {
        throw 'The release must contain the installer, backend, overlay, captured installer, and captured uninstaller.'
    }

    $ambiguousNames = @($allExecutables | Where-Object { $_.Name -match '(?i)(unsigned|self[-_ ]?signed|local[-_ ]?test)' })
    if ($ambiguousNames.Count -gt 0) {
        throw "Public-release output contains a development or test filename: $($ambiguousNames[0].FullName)"
    }

    $actualRelativePaths = @($allExecutables | ForEach-Object {
        Get-RelativeArtifactPath -Path $_.FullName -Root $artifactsRoot
    } | Sort-Object)
    $metadataRelativePaths = @($metadata.expectedExecutableFiles | ForEach-Object { ([string]$_).Replace('\', '/') } | Sort-Object)
    $inventoryDifferences = @(Compare-Object -ReferenceObject $metadataRelativePaths -DifferenceObject $actualRelativePaths)
    if ($metadataRelativePaths.Count -ne $actualRelativePaths.Count -or
        $inventoryDifferences.Count -ne 0) {
        throw 'The executable inventory does not match release-metadata.json.'
    }

    $records = foreach ($executable in $allExecutables) {
        $record = Get-OwnkeyAuthenticodeRecord `
            -Path $executable.FullName `
            -SignToolPath $resolvedSignToolPath `
            -ExpectedCertificateThumbprint $ExpectedCertificateThumbprint
        if ($record.SignerSubject -cne $ExpectedCertificateSubject) {
            throw "Publisher subject mismatch on '$($executable.FullName)': '$($record.SignerSubject)'"
        }

        [pscustomobject]@{
            RelativePath              = Get-RelativeArtifactPath -Path $record.Path -Root $artifactsRoot
            Sha256                    = $record.Sha256
            AuthenticodeStatus        = $record.Status
            SignerSubject             = $record.SignerSubject
            SignerThumbprint          = $record.SignerThumbprint
            SignerNotBefore           = $record.SignerNotBefore
            SignerNotAfter            = $record.SignerNotAfter
            TimestampSubject          = $record.TimestampSubject
            TimestampThumbprint       = $record.TimestampThumbprint
            TimestampCertificateUntil = $record.TimestampCertificateUntil
        }
    }

    $subjects = @($records.SignerSubject | Sort-Object -Unique)
    $thumbprints = @($records.SignerThumbprint | Sort-Object -Unique)
    if ($subjects.Count -ne 1 -or $thumbprints.Count -ne 1) {
        throw 'Release executables do not have one consistent publisher identity and signing certificate.'
    }

    $releaseSigner = (Get-AuthenticodeSignature -LiteralPath $installerPath).SignerCertificate
    Assert-OwnkeyCertificateForPublicRelease `
        -Certificate $releaseSigner `
        -RequirePrivateKey:$false `
        -RequireCurrentValidity:$false | Out-Null

    $verificationReport = [ordered]@{
        schemaVersion        = 1
        result               = 'Valid'
        verifiedAtUtc        = (Get-Date).ToUniversalTime().ToString('o')
        version              = $ExpectedVersion
        publisherSubject     = $subjects[0]
        signerThumbprint     = $thumbprints[0]
        timestampRequired    = $true
        authenticodePolicy   = 'Windows Default Authentication (/pa)'
        executableCount      = $records.Count
        generatedUninstallerCaptureCount = $generatedUninstallerCaptures.Count
        artifacts            = @($records)
    }

    if (-not [string]::IsNullOrWhiteSpace($ReportPath)) {
        $resolvedReportPath = [IO.Path]::GetFullPath($ReportPath)
        Write-Utf8Json -Value $verificationReport -Path $resolvedReportPath
    }

    Write-Host "Verified $($records.Count) public-release executables." -ForegroundColor Green
    Write-Host "Publisher: $($subjects[0])"
    Write-Host "Certificate: $($thumbprints[0])"
    $records | Select-Object RelativePath, AuthenticodeStatus, Sha256 | Format-Table -AutoSize
    return
}
catch {
    throw "Release verification failed: $($_.Exception.Message)"
}
