[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Ownkey.Signing.psm1') -Force

function Get-RequiredEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Required Inno signing environment variable is missing: $Name"
    }
    return $value
}

function Assert-LocalTestCertificate {
    param(
        [Parameter(Mandatory = $true)]
        [Security.Cryptography.X509Certificates.X509Certificate2]$Certificate
    )

    if (-not $Certificate.HasPrivateKey) {
        throw 'The local-test certificate has no accessible private key.'
    }
    $now = Get-Date
    if ($now -lt $Certificate.NotBefore -or $now -gt $Certificate.NotAfter) {
        throw 'The local-test certificate is outside its validity period.'
    }
    if (-not (Test-OwnkeyCertificateSelfSigned -Certificate $Certificate)) {
        throw 'Local-test signing policy accepts only an explicitly self-signed test certificate.'
    }
    $eku = $Certificate.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' } | Select-Object -First 1
    $ekuOids = if ($null -eq $eku) { @() } else { @($eku.EnhancedKeyUsages | ForEach-Object { $_.Value }) }
    if ('1.3.6.1.5.5.7.3.3' -notin $ekuOids) {
        throw 'The local-test certificate does not have the Code Signing EKU.'
    }
}

$thumbprint = Get-RequiredEnvironmentValue -Name 'OWNKEY_SIGN_CERT_THUMBPRINT'
$storeLocation = Get-RequiredEnvironmentValue -Name 'OWNKEY_SIGN_CERT_STORE_LOCATION'
$timestampUrl = Get-RequiredEnvironmentValue -Name 'OWNKEY_SIGN_TIMESTAMP_URL'
$signToolPath = Get-RequiredEnvironmentValue -Name 'OWNKEY_SIGNTOOL_PATH'
$captureDirectory = Get-RequiredEnvironmentValue -Name 'OWNKEY_INNO_SIGN_CAPTURE_DIR'
$signingPolicy = Get-RequiredEnvironmentValue -Name 'OWNKEY_INNO_SIGNING_POLICY'

if ($storeLocation -notin @('CurrentUser', 'LocalMachine')) {
    throw "Unsupported certificate store location: $storeLocation"
}
if ($signingPolicy -notin @('PublicRelease', 'SelfSignedLocalTest')) {
    throw "Unsupported Inno signing policy: $signingPolicy"
}

$selection = Resolve-OwnkeySigningCertificate `
    -CertificateThumbprint $thumbprint `
    -CertificateStoreLocation $storeLocation
$certificate = $selection.Certificate
if ($signingPolicy -eq 'PublicRelease') {
    Assert-OwnkeyCertificateForPublicRelease -Certificate $certificate | Out-Null
}
else {
    Assert-LocalTestCertificate -Certificate $certificate
}

$resolvedSignTool = Resolve-OwnkeySignTool -SignToolPath $signToolPath
$resolvedTimestampUrl = Assert-OwnkeyTimestampUrl -TimestampUrl $timestampUrl
$resolvedPath = [IO.Path]::GetFullPath($Path)
Invoke-OwnkeySignFile `
    -Path $resolvedPath `
    -Certificate $certificate `
    -CertificateStoreLocation $storeLocation `
    -TimestampUrl $resolvedTimestampUrl `
    -SignToolPath $resolvedSignTool

if ($signingPolicy -eq 'PublicRelease') {
    Get-OwnkeyAuthenticodeRecord `
        -Path $resolvedPath `
        -SignToolPath $resolvedSignTool `
        -ExpectedCertificateThumbprint $certificate.Thumbprint | Out-Null
}
else {
    $signature = Get-AuthenticodeSignature -LiteralPath $resolvedPath
    if ($signature.Status -in @(
            [Management.Automation.SignatureStatus]::NotSigned,
            [Management.Automation.SignatureStatus]::HashMismatch,
            [Management.Automation.SignatureStatus]::NotSupportedFileFormat,
            [Management.Automation.SignatureStatus]::Incompatible
        ) -or
        $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.Thumbprint -cne $certificate.Thumbprint -or
        $null -eq $signature.TimeStamperCertificate) {
        throw "The Inno local-test signature or timestamp did not validate structurally: $resolvedPath"
    }
}

$resolvedCaptureDirectory = [IO.Path]::GetFullPath($captureDirectory)
New-Item -ItemType Directory -Path $resolvedCaptureDirectory -Force | Out-Null
$hash = (Get-FileHash -LiteralPath $resolvedPath -Algorithm SHA256).Hash
$versionInfo = [Diagnostics.FileVersionInfo]::GetVersionInfo($resolvedPath)
$description = [string]$versionInfo.FileDescription
if ($description -match '(?i)uninstall') {
    $kind = 'uninstaller'
}
elseif ($description -match '(?i)(setup|installer)') {
    $kind = 'installer'
}
else {
    $kind = 'generated-image'
}

$captureName = "inno-$kind-$hash.exe"
$capturePath = Join-Path $resolvedCaptureDirectory $captureName
Copy-Item -LiteralPath $resolvedPath -Destination $capturePath -Force

$captureRecord = [ordered]@{
    schemaVersion       = 1
    signingPolicy       = $signingPolicy
    sourceFileName      = [IO.Path]::GetFileName($resolvedPath)
    fileDescription     = $description
    capturedFileName    = $captureName
    sha256              = $hash
    signerSubject       = $certificate.Subject
    signerThumbprint    = $certificate.Thumbprint
    timestampUrl        = $resolvedTimestampUrl
    capturedAtUtc       = (Get-Date).ToUniversalTime().ToString('o')
}
$recordPath = Join-Path $resolvedCaptureDirectory ("inno-$hash.json")
$json = $captureRecord | ConvertTo-Json -Depth 4
[IO.File]::WriteAllText($recordPath, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
