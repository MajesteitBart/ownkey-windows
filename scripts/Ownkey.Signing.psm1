Set-StrictMode -Version 3.0

$script:CodeSigningEkuOid = '1.3.6.1.5.5.7.3.3'

function ConvertTo-OwnkeyNormalizedThumbprint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Thumbprint
    )

    $normalized = ($Thumbprint -replace '[\s:-]', '').ToUpperInvariant()
    if ($normalized.Length -ne 40 -or $normalized -notmatch '^[0-9A-F]{40}$') {
        throw 'A certificate thumbprint must contain exactly 40 hexadecimal SHA-1 characters.'
    }

    return $normalized
}

function Assert-OwnkeyTimestampUrl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$TimestampUrl
    )

    $uri = $null
    if (-not [Uri]::TryCreate($TimestampUrl, [UriKind]::Absolute, [ref]$uri)) {
        throw "The timestamp URL is not an absolute URI: $TimestampUrl"
    }

    if ($uri.Scheme -notin @('http', 'https')) {
        throw 'The timestamp URL must use HTTP or HTTPS.'
    }

    if (-not [string]::IsNullOrEmpty($uri.UserInfo)) {
        throw 'The timestamp URL must not contain credentials.'
    }

    if (-not [string]::IsNullOrEmpty($uri.Fragment)) {
        throw 'The timestamp URL must not contain a fragment.'
    }

    if ($TimestampUrl.IndexOfAny(@([char]'"', [char]'$', [char]13, [char]10)) -ge 0) {
        throw 'The timestamp URL contains characters that cannot be passed safely to Inno Setup.'
    }

    return $uri.AbsoluteUri
}

function Resolve-OwnkeySignTool {
    [CmdletBinding()]
    param(
        [string]$SignToolPath
    )

    if (-not [string]::IsNullOrWhiteSpace($SignToolPath)) {
        $resolved = [IO.Path]::GetFullPath($SignToolPath)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "SignTool was not found at the configured path: $resolved"
        }
        return $resolved
    }

    $command = Get-Command 'signtool.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        return $command.Source
    }

    $kitRoots = New-Object System.Collections.Generic.List[string]
    try {
        $registryRoot = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots' -ErrorAction Stop).KitsRoot10
        if (-not [string]::IsNullOrWhiteSpace($registryRoot)) {
            $kitRoots.Add($registryRoot)
        }
    }
    catch {
        # The registry key is absent on machines without the Windows SDK.
    }

    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
        $kitRoots.Add((Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10'))
    }

    $candidates = foreach ($kitRoot in $kitRoots | Select-Object -Unique) {
        $binRoot = Join-Path $kitRoot 'bin'
        if (Test-Path -LiteralPath $binRoot -PathType Container) {
            Get-ChildItem -LiteralPath $binRoot -Recurse -File -Filter 'signtool.exe' -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match '[\\/]x64[\\/]signtool\.exe$' }
        }
    }

    $selected = $candidates | Sort-Object FullName -Descending | Select-Object -First 1
    if ($null -eq $selected) {
        throw 'SignTool.exe was not found. Install a Windows SDK or pass -SignToolPath.'
    }

    return $selected.FullName
}

function Resolve-OwnkeySigningCertificate {
    [CmdletBinding()]
    param(
        [string]$CertificateThumbprint,

        [string]$CertificateSubject,

        [ValidateSet('CurrentUser', 'LocalMachine')]
        [string]$CertificateStoreLocation = 'CurrentUser'
    )

    $hasThumbprint = -not [string]::IsNullOrWhiteSpace($CertificateThumbprint)
    $hasSubject = -not [string]::IsNullOrWhiteSpace($CertificateSubject)
    if ($hasThumbprint -eq $hasSubject) {
        throw 'Specify exactly one of -CertificateThumbprint or -CertificateSubject.'
    }

    $storePath = "Cert:\$CertificateStoreLocation\My"
    if (-not (Test-Path -LiteralPath $storePath)) {
        throw "The certificate store does not exist: $storePath"
    }

    $certificates = @(Get-ChildItem -LiteralPath $storePath -ErrorAction Stop)
    if ($hasThumbprint) {
        $normalized = ConvertTo-OwnkeyNormalizedThumbprint -Thumbprint $CertificateThumbprint
        $matches = @($certificates | Where-Object {
            -not [string]::IsNullOrWhiteSpace($_.Thumbprint) -and
            (($_.Thumbprint -replace '\s', '').ToUpperInvariant() -eq $normalized)
        })
        $selectorDescription = "thumbprint $normalized"
    }
    else {
        $matches = @($certificates | Where-Object { $_.Subject -ieq $CertificateSubject })
        $selectorDescription = "subject '$CertificateSubject'"
    }

    if ($matches.Count -eq 0) {
        throw "No certificate matching $selectorDescription was found in $storePath."
    }
    if ($matches.Count -gt 1) {
        throw "Certificate selector $selectorDescription is ambiguous in $storePath. Use a SHA-1 thumbprint."
    }

    return [pscustomobject]@{
        Certificate   = $matches[0]
        StoreLocation = $CertificateStoreLocation
        StoreName     = 'My'
    }
}

function Get-OwnkeyInnoSignToolDefinition {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$PowerShellPath,

        [Parameter(Mandatory = $true)]
        [string]$SigningScriptPath
    )

    $resolvedPowerShellPath = [IO.Path]::GetFullPath($PowerShellPath)
    $resolvedSigningScriptPath = [IO.Path]::GetFullPath($SigningScriptPath)
    foreach ($path in @($resolvedPowerShellPath, $resolvedSigningScriptPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "The Inno signing wrapper dependency was not found: $path"
        }
        if ($path.IndexOfAny(@([char]'"', [char]'$')) -ge 0) {
            throw 'An Inno signing wrapper path contains characters that Inno Setup cannot quote safely.'
        }
    }

    return '$q' + $resolvedPowerShellPath + '$q' +
        ' -NoProfile -NonInteractive -ExecutionPolicy Bypass' +
        ' -File $q' + $resolvedSigningScriptPath + '$q' +
        ' -Path $f'
}

function Test-OwnkeyCertificateSelfSigned {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [Security.Cryptography.X509Certificates.X509Certificate2]$Certificate
    )

    $subjectName = [Convert]::ToBase64String($Certificate.SubjectName.RawData)
    $issuerName = [Convert]::ToBase64String($Certificate.IssuerName.RawData)
    return $subjectName -ceq $issuerName
}

function Assert-OwnkeyCertificateForPublicRelease {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [Security.Cryptography.X509Certificates.X509Certificate2]$Certificate,

        [bool]$RequirePrivateKey = $true,

        [bool]$RequireCurrentValidity = $true,

        [datetime]$VerificationTime = (Get-Date)
    )

    if ($RequirePrivateKey -and -not $Certificate.HasPrivateKey) {
        throw 'The selected certificate does not have an accessible private key.'
    }

    if ($RequireCurrentValidity) {
        if ($VerificationTime -lt $Certificate.NotBefore) {
            throw "The selected certificate is not valid before $($Certificate.NotBefore.ToString('o'))."
        }
        if ($VerificationTime -gt $Certificate.NotAfter) {
            throw "The selected certificate expired at $($Certificate.NotAfter.ToString('o'))."
        }
    }

    $basicConstraints = $Certificate.Extensions |
        Where-Object { $_.Oid.Value -eq '2.5.29.19' } |
        Select-Object -First 1
    if ($null -ne $basicConstraints -and $basicConstraints.CertificateAuthority) {
        throw 'The selected certificate is a certificate-authority certificate, not an end-entity code-signing certificate.'
    }

    $ekuExtension = $Certificate.Extensions |
        Where-Object { $_.Oid.Value -eq '2.5.29.37' } |
        Select-Object -First 1
    if ($null -eq $ekuExtension) {
        throw 'The selected certificate has no Enhanced Key Usage extension. Public releases require the Code Signing EKU.'
    }

    $ekuOids = @($ekuExtension.EnhancedKeyUsages | ForEach-Object { $_.Value })
    if ($script:CodeSigningEkuOid -notin $ekuOids) {
        throw "The selected certificate does not permit code signing (EKU $script:CodeSigningEkuOid)."
    }

    $keyUsageExtension = $Certificate.Extensions |
        Where-Object { $_.Oid.Value -eq '2.5.29.15' } |
        Select-Object -First 1
    if ($null -ne $keyUsageExtension) {
        $digitalSignature = [Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature
        if (($keyUsageExtension.KeyUsages -band $digitalSignature) -eq 0) {
            throw 'The selected certificate key usage does not permit digital signatures.'
        }
    }

    if ($Certificate.SignatureAlgorithm.FriendlyName -match 'sha1' -or
        $Certificate.SignatureAlgorithm.Value -eq '1.2.840.113549.1.1.5') {
        throw 'The selected certificate itself uses a deprecated SHA-1 signature algorithm.'
    }

    if (Test-OwnkeyCertificateSelfSigned -Certificate $Certificate) {
        throw 'The selected certificate is self-signed and cannot qualify a public release.'
    }

    $chain = New-Object Security.Cryptography.X509Certificates.X509Chain
    try {
        $chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::Online
        $chain.ChainPolicy.RevocationFlag = [Security.Cryptography.X509Certificates.X509RevocationFlag]::ExcludeRoot
        if ($RequireCurrentValidity) {
            $chain.ChainPolicy.VerificationFlags = [Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
        }
        else {
            $chain.ChainPolicy.VerificationFlags = [Security.Cryptography.X509Certificates.X509VerificationFlags]::IgnoreNotTimeValid
        }
        $chain.ChainPolicy.VerificationTime = $VerificationTime
        $chain.ChainPolicy.UrlRetrievalTimeout = [TimeSpan]::FromSeconds(30)

        if (-not $chain.Build($Certificate)) {
            $failures = @($chain.ChainStatus | ForEach-Object {
                $information = $_.StatusInformation.Trim()
                if ([string]::IsNullOrWhiteSpace($information)) {
                    $_.Status.ToString()
                }
                else {
                    "$($_.Status): $information"
                }
            })
            if ($failures.Count -eq 0) {
                $failures = @('Windows could not build a trusted certificate chain.')
            }
            throw "The selected certificate does not build a trusted, revocation-checked public chain: $($failures -join '; ')"
        }

        $chainRoot = $chain.ChainElements[$chain.ChainElements.Count - 1].Certificate
        $chainRootThumbprint = ConvertTo-OwnkeyNormalizedThumbprint -Thumbprint $chainRoot.Thumbprint
        $authRootMatch = @(foreach ($authRootStore in @('Cert:\CurrentUser\AuthRoot', 'Cert:\LocalMachine\AuthRoot')) {
            if (Test-Path -LiteralPath $authRootStore) {
                Get-ChildItem -LiteralPath $authRootStore -ErrorAction SilentlyContinue | Where-Object {
                    -not [string]::IsNullOrWhiteSpace($_.Thumbprint) -and
                    (($_.Thumbprint -replace '\s', '').ToUpperInvariant() -eq $chainRootThumbprint)
                }
            }
        })
        if ($authRootMatch.Count -eq 0) {
            throw "The certificate chain ends at a locally trusted root that is not in Windows' third-party AuthRoot store: $($chainRoot.Subject)"
        }
    }
    finally {
        $chain.Dispose()
    }

    return [pscustomobject]@{
        Subject    = $Certificate.Subject
        Thumbprint = (ConvertTo-OwnkeyNormalizedThumbprint -Thumbprint $Certificate.Thumbprint)
        NotBefore  = $Certificate.NotBefore
        NotAfter   = $Certificate.NotAfter
    }
}

function Invoke-OwnkeySignFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [Security.Cryptography.X509Certificates.X509Certificate2]$Certificate,

        [Parameter(Mandatory = $true)]
        [ValidateSet('CurrentUser', 'LocalMachine')]
        [string]$CertificateStoreLocation,

        [Parameter(Mandatory = $true)]
        [string]$TimestampUrl,

        [Parameter(Mandatory = $true)]
        [string]$SignToolPath
    )

    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
        throw "Cannot sign a missing file: $resolvedPath"
    }

    $normalizedTimestampUrl = Assert-OwnkeyTimestampUrl -TimestampUrl $TimestampUrl
    $thumbprint = ConvertTo-OwnkeyNormalizedThumbprint -Thumbprint $Certificate.Thumbprint
    $arguments = @(
        'sign',
        '/sha1', $thumbprint,
        '/s', 'My',
        '/fd', 'SHA256',
        '/tr', $normalizedTimestampUrl,
        '/td', 'SHA256',
        '/d', 'Ownkey for Windows',
        '/du', 'https://ownkey.bvdm.ai',
        '/v'
    )
    if ($CertificateStoreLocation -eq 'LocalMachine') {
        $arguments += '/sm'
    }
    $arguments += $resolvedPath

    & $SignToolPath @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool failed with exit code $LASTEXITCODE while signing $resolvedPath"
    }
}

function Get-OwnkeyAuthenticodeRecord {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$SignToolPath,

        [string]$ExpectedCertificateThumbprint
    )

    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
        throw "Required release artifact is missing: $resolvedPath"
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $resolvedPath
    if ($signature.Status -ne [Management.Automation.SignatureStatus]::Valid) {
        throw "Authenticode status for '$resolvedPath' is $($signature.Status): $($signature.StatusMessage)"
    }
    if ($null -eq $signature.SignerCertificate) {
        throw "No Authenticode signer certificate was returned for '$resolvedPath'."
    }
    if ($null -eq $signature.TimeStamperCertificate) {
        throw "The Authenticode signature on '$resolvedPath' has no timestamp."
    }

    $signerThumbprint = ConvertTo-OwnkeyNormalizedThumbprint -Thumbprint $signature.SignerCertificate.Thumbprint
    if (-not [string]::IsNullOrWhiteSpace($ExpectedCertificateThumbprint)) {
        $expected = ConvertTo-OwnkeyNormalizedThumbprint -Thumbprint $ExpectedCertificateThumbprint
        if ($signerThumbprint -cne $expected) {
            throw "'$resolvedPath' was signed by thumbprint $signerThumbprint, expected $expected."
        }
    }

    & $SignToolPath verify /q /pa /all /tw $resolvedPath
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool verification failed with exit code $LASTEXITCODE for '$resolvedPath'."
    }

    return [pscustomobject]@{
        Path                      = $resolvedPath
        Status                    = $signature.Status.ToString()
        SignerSubject             = $signature.SignerCertificate.Subject
        SignerThumbprint          = $signerThumbprint
        SignerNotBefore           = $signature.SignerCertificate.NotBefore.ToUniversalTime().ToString('o')
        SignerNotAfter            = $signature.SignerCertificate.NotAfter.ToUniversalTime().ToString('o')
        TimestampSubject          = $signature.TimeStamperCertificate.Subject
        TimestampThumbprint       = $signature.TimeStamperCertificate.Thumbprint
        TimestampCertificateUntil = $signature.TimeStamperCertificate.NotAfter.ToUniversalTime().ToString('o')
        Sha256                    = (Get-FileHash -LiteralPath $resolvedPath -Algorithm SHA256).Hash
    }
}

function Assert-OwnkeyPathUnderRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $rootPrefix = $fullRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate on '$fullPath' because it is not below '$fullRoot'."
    }

    return $fullPath
}

Export-ModuleMember -Function @(
    'Assert-OwnkeyCertificateForPublicRelease',
    'Assert-OwnkeyPathUnderRoot',
    'Assert-OwnkeyTimestampUrl',
    'ConvertTo-OwnkeyNormalizedThumbprint',
    'Get-OwnkeyInnoSignToolDefinition',
    'Get-OwnkeyAuthenticodeRecord',
    'Invoke-OwnkeySignFile',
    'Resolve-OwnkeySigningCertificate',
    'Resolve-OwnkeySignTool',
    'Test-OwnkeyCertificateSelfSigned'
)
