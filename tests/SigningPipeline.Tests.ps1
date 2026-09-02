[CmdletBinding()]
param()

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$modulePath = Join-Path $repoRoot 'scripts\Ownkey.Signing.psm1'
$createdCertificateThumbprints = New-Object System.Collections.Generic.List[string]
$failures = New-Object System.Collections.Generic.List[string]

Import-Module $modulePath -Force

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Assert-ThrowsLike {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Pattern
    )

    $caught = $null
    try {
        & $Action
    }
    catch {
        $caught = $_
    }
    if ($null -eq $caught) {
        throw "Expected an exception matching '$Pattern', but no exception was thrown."
    }
    if ($caught.Exception.Message -notmatch $Pattern) {
        throw "Exception did not match '$Pattern': $($caught.Exception.Message)"
    }
}

function Invoke-Test {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    try {
        & $Action
        Write-Host "PASS $Name" -ForegroundColor Green
    }
    catch {
        $failures.Add("$Name`: $($_.Exception.Message)")
        Write-Host "FAIL $Name`: $($_.Exception.Message)" -ForegroundColor Red
    }
}

function New-TemporaryCertificate {
    param(
        [Parameter(Mandatory = $true)][string]$Purpose,
        [ValidateSet('CodeSigning', 'ServerAuthentication')]
        [string]$Usage = 'CodeSigning',
        [datetime]$NotBefore = (Get-Date).AddMinutes(-5),
        [datetime]$NotAfter = (Get-Date).AddDays(1)
    )

    $subject = "CN=OwnkeySigningPipelineTest-$Purpose-$([guid]::NewGuid().ToString('N'))"
    if ($Usage -eq 'CodeSigning') {
        $certificate = New-SelfSignedCertificate `
            -Type CodeSigningCert `
            -Subject $subject `
            -CertStoreLocation 'Cert:\CurrentUser\My' `
            -KeyExportPolicy NonExportable `
            -NotBefore $NotBefore `
            -NotAfter $NotAfter
    }
    else {
        $certificate = New-SelfSignedCertificate `
            -Type Custom `
            -Subject $subject `
            -CertStoreLocation 'Cert:\CurrentUser\My' `
            -KeyExportPolicy NonExportable `
            -KeyUsage DigitalSignature `
            -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.1') `
            -NotBefore $NotBefore `
            -NotAfter $NotAfter
    }
    $createdCertificateThumbprints.Add($certificate.Thumbprint)
    return $certificate
}

try {
    Invoke-Test 'PowerShell files parse without errors' {
        $files = @(
            Get-ChildItem -LiteralPath (Join-Path $repoRoot 'scripts') -File |
                Where-Object { $_.Extension -in @('.ps1', '.psm1') }
            Get-ChildItem -LiteralPath (Join-Path $repoRoot 'tests') -File -Filter '*.ps1'
            Get-Item -LiteralPath (Join-Path $repoRoot 'build-release.ps1')
        )
        foreach ($file in $files) {
            $tokens = $null
            $parseErrors = $null
            [Management.Automation.Language.Parser]::ParseFile(
                $file.FullName,
                [ref]$tokens,
                [ref]$parseErrors
            ) | Out-Null
            Assert-True -Condition ($parseErrors.Count -eq 0) -Message "$($file.Name) has parser errors."
        }
    }

    Invoke-Test 'Thumbprints are normalized and malformed selectors fail' {
        $value = ConvertTo-OwnkeyNormalizedThumbprint 'aa bb cc dd ee ff 00 11 22 33 44 55 66 77 88 99 aa bb cc dd'
        Assert-True -Condition ($value -ceq 'AABBCCDDEEFF00112233445566778899AABBCCDD') -Message 'Thumbprint normalization changed the value.'
        Assert-ThrowsLike -Action { ConvertTo-OwnkeyNormalizedThumbprint '1234' } -Pattern '40 hexadecimal'
        Assert-ThrowsLike -Action { ConvertTo-OwnkeyNormalizedThumbprint 'ZZ0000000000000000000000000000000000000000' } -Pattern '40 hexadecimal'
    }

    Invoke-Test 'Timestamp URL validation rejects unsafe inputs' {
        $url = Assert-OwnkeyTimestampUrl 'http://timestamp.digicert.com'
        Assert-True -Condition ($url -ceq 'http://timestamp.digicert.com/') -Message 'The RFC3161 URL was not normalized as expected.'
        Assert-ThrowsLike -Action { Assert-OwnkeyTimestampUrl 'file:///c:/timestamp' } -Pattern 'HTTP or HTTPS'
        Assert-ThrowsLike -Action { Assert-OwnkeyTimestampUrl 'https://user:secret@example.test/' } -Pattern 'credentials'
        Assert-ThrowsLike -Action { Assert-OwnkeyTimestampUrl 'https://example.test/$f' } -Pattern 'cannot be passed safely'
    }

    Invoke-Test 'Certificate selection requires exactly one stable selector' {
        Assert-ThrowsLike -Action { Resolve-OwnkeySigningCertificate } -Pattern 'exactly one'
        Assert-ThrowsLike -Action {
            Resolve-OwnkeySigningCertificate `
                -CertificateThumbprint '0000000000000000000000000000000000000000' `
                -CertificateSubject 'CN=Nobody'
        } -Pattern 'exactly one'
        Assert-ThrowsLike -Action {
            Resolve-OwnkeySigningCertificate -CertificateThumbprint '0000000000000000000000000000000000000000'
        } -Pattern 'No certificate matching'
    }

    $selfSigned = New-TemporaryCertificate -Purpose 'SelfSigned'
    Invoke-Test 'Public release rejects a self-signed code-signing certificate' {
        Assert-ThrowsLike -Action {
            Assert-OwnkeyCertificateForPublicRelease -Certificate $selfSigned
        } -Pattern 'self-signed'
    }

    Invoke-Test 'Public release rejects a certificate without a private key' {
        $publicOnly = New-Object Security.Cryptography.X509Certificates.X509Certificate2 -ArgumentList @(, $selfSigned.RawData)
        try {
            Assert-ThrowsLike -Action {
                Assert-OwnkeyCertificateForPublicRelease -Certificate $publicOnly
            } -Pattern 'private key'
        }
        finally {
            $publicOnly.Dispose()
        }
    }

    $expired = New-TemporaryCertificate `
        -Purpose 'Expired' `
        -NotBefore (Get-Date).AddDays(-3) `
        -NotAfter (Get-Date).AddDays(-2)
    Invoke-Test 'Public release rejects an expired certificate' {
        Assert-ThrowsLike -Action {
            Assert-OwnkeyCertificateForPublicRelease -Certificate $expired
        } -Pattern 'expired'
    }

    $wrongEku = New-TemporaryCertificate -Purpose 'WrongEku' -Usage ServerAuthentication
    Invoke-Test 'Public release rejects a certificate without the Code Signing EKU' {
        Assert-ThrowsLike -Action {
            Assert-OwnkeyCertificateForPublicRelease -Certificate $wrongEku
        } -Pattern 'does not permit code signing'
    }

    $untrustedTag = [guid]::NewGuid().ToString('N')
    $untrustedRoot = New-SelfSignedCertificate `
        -Type Custom `
        -Subject "CN=Ownkey Signing Pipeline Untrusted Root $untrustedTag" `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -KeyExportPolicy NonExportable `
        -KeyUsage CertSign, CRLSign `
        -TextExtension @('2.5.29.19={critical}{text}ca=1&pathlength=1') `
        -NotAfter (Get-Date).AddDays(2)
    $createdCertificateThumbprints.Add($untrustedRoot.Thumbprint)
    $untrustedLeaf = New-SelfSignedCertificate `
        -Type Custom `
        -Subject "CN=Ownkey Signing Pipeline Untrusted Leaf $untrustedTag" `
        -Signer $untrustedRoot `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -KeyExportPolicy NonExportable `
        -KeyUsage DigitalSignature `
        -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3') `
        -NotAfter (Get-Date).AddDays(1)
    $createdCertificateThumbprints.Add($untrustedLeaf.Thumbprint)
    Invoke-Test 'Public release rejects a non-self-signed certificate with an untrusted chain' {
        Assert-ThrowsLike -Action {
            Assert-OwnkeyCertificateForPublicRelease -Certificate $untrustedLeaf
        } -Pattern 'does not build a trusted'
    }

    Invoke-Test 'Inno Setup has separate signed-release and unsigned-development modes' {
        $inno = Get-Content -LiteralPath (Join-Path $repoRoot 'installer\Ownkey.iss') -Raw
        Assert-True -Condition ($inno -match '#ifdef ReleaseSigning') -Message 'ReleaseSigning conditional is missing.'
        Assert-True -Condition ($inno -match '(?m)^SignTool=ownkey_release_sha256\r?$') -Message 'The release SignTool directive is missing.'
        Assert-True -Condition ($inno -match '(?m)^SignedUninstaller=yes\r?$') -Message 'SignedUninstaller=yes is missing.'
        Assert-True -Condition ($inno -match '(?m)^SignedUninstaller=no\r?$') -Message 'The unsigned mode does not disable signed uninstallers.'
        Assert-True -Condition ($inno -match 'UNSIGNED-DEV') -Message 'The default output name is not visibly marked as unsigned development output.'
    }

    Invoke-Test 'Signing commands require SHA-256 and RFC3161 timestamping' {
        $signingModule = Get-Content -LiteralPath (Join-Path $repoRoot 'scripts\Ownkey.Signing.psm1') -Raw
        Assert-True -Condition ($signingModule -match "'/fd', 'SHA256'") -Message 'Direct signing does not pin the SHA-256 file digest.'
        Assert-True -Condition ($signingModule -match "'/tr'") -Message 'Direct signing does not use the RFC3161 /tr switch.'
        Assert-True -Condition ($signingModule -match "'/td', 'SHA256'") -Message 'Direct signing does not pin the SHA-256 timestamp digest.'
        $innoDefinition = Get-OwnkeyInnoSignToolDefinition `
            -PowerShellPath (Join-Path $PSHOME 'powershell.exe') `
            -SigningScriptPath (Join-Path $repoRoot 'scripts\Invoke-InnoSigning.ps1')
        Assert-True -Condition ($innoDefinition -match 'Invoke-InnoSigning\.ps1') -Message 'Inno does not call the checked signing wrapper.'
        Assert-True -Condition ($innoDefinition.EndsWith('-Path $f')) -Message 'The Inno SignTool command does not end with Inno''s quoted file placeholder.'
        $innoWrapper = Get-Content -LiteralPath (Join-Path $repoRoot 'scripts\Invoke-InnoSigning.ps1') -Raw
        Assert-True -Condition ($innoWrapper -match 'Invoke-OwnkeySignFile') -Message 'The Inno wrapper does not use the SHA-256/RFC3161 signing function.'
        Assert-True -Condition ($innoWrapper -match 'OWNKEY_INNO_SIGN_CAPTURE_DIR') -Message 'The Inno wrapper does not capture generated signing images.'
    }

    Invoke-Test 'Release scripts do not accept PFX files or passwords' {
        $releaseScripts = @(
            Get-Content -LiteralPath (Join-Path $repoRoot 'build-release.ps1') -Raw
            Get-Content -LiteralPath (Join-Path $repoRoot 'scripts\Build-WindowsInstaller.ps1') -Raw
            Get-Content -LiteralPath (Join-Path $repoRoot 'scripts\Ownkey.Signing.psm1') -Raw
        ) -join [Environment]::NewLine
        Assert-True -Condition ($releaseScripts -notmatch '(?i)(PfxPath|CertificatePassword|ConvertTo-SecureString)') -Message 'A release script exposes a PFX or password input.'
    }

    Invoke-Test 'Independent verifier fails when required release artifacts are absent' {
        $fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("ownkey-verifier-test-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $fixtureRoot | Out-Null
        try {
            $metadata = @{
                schemaVersion = 1
                mode = 'PublicRelease'
                version = '0.3.0'
                publisherSubject = 'CN=Fixture'
                signerThumbprint = '0000000000000000000000000000000000000000'
                expectedExecutableFiles = @()
            } | ConvertTo-Json
            [IO.File]::WriteAllText(
                (Join-Path $fixtureRoot 'release-metadata.json'),
                $metadata,
                (New-Object Text.UTF8Encoding($false))
            )

            $previousErrorPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                & (Join-Path $PSHOME 'powershell.exe') `
                    -NoProfile `
                    -NonInteractive `
                    -ExecutionPolicy Bypass `
                    -File (Join-Path $repoRoot 'scripts\Verify-WindowsRelease.ps1') `
                    -ArtifactsDirectory $fixtureRoot *> $null
                $verifierExitCode = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $previousErrorPreference
            }
            Assert-True -Condition ($verifierExitCode -ne 0) -Message 'The verifier accepted a release with missing executables.'
        }
        finally {
            if (Test-Path -LiteralPath $fixtureRoot) {
                Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
            }
        }
    }
}
finally {
    foreach ($thumbprint in $createdCertificateThumbprints) {
        $certificatePath = "Cert:\CurrentUser\My\$thumbprint"
        if (Test-Path -LiteralPath $certificatePath) {
            Remove-Item -LiteralPath $certificatePath -Force
        }
    }
}

if ($failures.Count -gt 0) {
    Write-Host ''
    $failures | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    exit 1
}

Write-Host ''
Write-Host 'All signing-pipeline checks passed.' -ForegroundColor Green
exit 0
