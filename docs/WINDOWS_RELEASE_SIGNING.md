# Windows release signing

Ownkey has two Windows packaging paths:

| Path | Command | Output | Publishable |
|---|---|---|---:|
| Unsigned development | `build-installer.bat` | `dist-installer-dev\Ownkey-Setup-<version>-UNSIGNED-DEV.exe` | No |
| Public release | `build-release.ps1` | `dist-release\Ownkey-Setup-<version>.exe` plus audit files | Only after the built-in verifier passes |

The public `v0.3.0` assets that predate this pipeline are unsigned. This change
does not retroactively sign an existing GitHub release.

## Required tools

Run release builds on Windows with:

- Windows PowerShell 5.1 or newer
- Python 3.11+ and the `py` launcher
- Node.js and pnpm
- Rust with the MSVC toolchain and Visual Studio Build Tools
- Inno Setup 6
- A Windows SDK containing `signtool.exe`

The scripts discover standard Inno Setup and Windows SDK locations. Use
`-IsccPath` or `-SignToolPath` only when the tools are installed elsewhere.

## Certificate policy

The public-release certificate must:

- be in `Cert:\CurrentUser\My` or `Cert:\LocalMachine\My`;
- have an accessible private key;
- be within its validity period at build time;
- be an end-entity certificate with the Code Signing EKU
  `1.3.6.1.5.5.7.3.3`;
- permit digital signatures when a Key Usage extension is present;
- use a certificate signature algorithm other than SHA-1;
- not be self-signed; and
- build a Windows-trusted chain with online revocation checking and no ignored
  chain errors whose root is present in Windows' third-party `AuthRoot` store.

The `AuthRoot` check keeps a private or enterprise root that was added only to
one build machine from qualifying a public release. A public certificate
provider must chain to the Windows third-party root program on a normally
configured release machine.

The build accepts one stable, non-secret selector. A SHA-1 certificate
thumbprint is preferred. "SHA-1" here describes the certificate-store selector,
not the artifact signature algorithm. Artifacts and RFC3161 timestamps use
SHA-256.

List candidate certificates without exposing private-key material:

```powershell
Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
  Select-Object Subject, Thumbprint, NotBefore, NotAfter, HasPrivateKey
```

For a machine-store certificate, replace `CurrentUser` with `LocalMachine`.
The exact subject selector is also supported, but the build fails if more than
one certificate has that subject. A thumbprint avoids renewal ambiguity.

## Build a public release

Select by thumbprint:

```powershell
$thumbprint = '<40-hex-character-certificate-thumbprint>'

.\build-release.ps1 `
  -CertificateThumbprint $thumbprint `
  -CertificateStoreLocation CurrentUser
```

Or select by exact distinguished subject:

```powershell
.\build-release.ps1 `
  -CertificateSubject 'CN=Publisher Name, O=Publisher Organization, C=NL' `
  -CertificateStoreLocation CurrentUser
```

The default timestamp URL is DigiCert's published RFC3161 endpoint,
`http://timestamp.digicert.com`. SignTool uses `/tr` with `/td SHA256`. To use
another RFC3161 service:

```powershell
.\build-release.ps1 `
  -CertificateThumbprint $thumbprint `
  -TimestampUrl 'https://timestamp-provider.example/rfc3161'
```

The URL may use HTTP or HTTPS and must not contain credentials or a fragment.
An RFC3161 response is signed by the timestamp authority. Organizations may
still require an HTTPS endpoint or an allowlisted timestamp authority.

Use `-Force` to replace an existing local `dist-release` directory. The script
does not publish or upload anything.

## What the release builder enforces

The public path performs certificate and tool preflight before compiling code.
It also requires a full Git commit ID and no tracked worktree changes. It then:

1. Builds PyInstaller into a unique directory below `build\`.
2. Builds the Tauri overlay.
3. Copies release payloads into an isolated candidate and discovers every
   `.exe` below `payload\`. This catches future helper executables as well as
   `Ownkey.exe` and `ownkey-overlay.exe`.
4. Signs each discovered executable with SHA-256 and an RFC3161 timestamp, then
   requires PowerShell Authenticode status `Valid` and a successful
   `signtool verify /pa /all /tw` result.
5. Compiles Inno Setup with its named `SignTool` and `SignedUninstaller=yes`.
   The signing wrapper keeps audit copies of the signed installer image and the
   generated uninstaller image before Inno embeds or renames them.
6. Runs `Verify-WindowsRelease.ps1` in a separate PowerShell process.
7. Copies the candidate to `dist-release` and verifies the copied files again.

Candidate files stay below `build\` until all checks pass. A failed build
removes its candidate. If post-copy verification fails, the new
`dist-release` directory is removed too.

Successful output has this shape:

```text
dist-release\
  Ownkey-Setup-<version>.exe
  payload\
    backend\
      Ownkey.exe
      ...
    ownkey-overlay.exe
  signed-inno-images\
    inno-installer-<sha256>.exe
    inno-uninstaller-<sha256>.exe
    inno-<sha256>.json
  release-metadata.json
  verification-report.json
```

The payload directory contains the exact signed executables supplied to Inno
Setup. The signed Inno images are byte-for-byte audit copies made by the signing
wrapper. `release-metadata.json` records the Git commit, publisher, certificate
thumbprint, timestamp configuration, and complete executable inventory.
`verification-report.json` records each verified file's SHA-256 hash and signer
details.

## Inno Setup integration

`installer\Ownkey.iss` enables these directives only when the release script
defines `ReleaseSigning`:

```ini
SignTool=ownkey_release_sha256
SignedUninstaller=yes
```

The build supplies `ownkey_release_sha256` through ISCC's `/S` argument. Inno
calls `scripts\Invoke-InnoSigning.ps1` for each image. The wrapper selects the
already validated certificate from the Windows certificate store, calls
SignTool with `/fd SHA256`, `/tr`, and `/td SHA256`, verifies the result, and
copies it into `signed-inno-images`. The process environment carries the
thumbprint, store location, timestamp URL, SignTool path, capture directory,
and signing policy. These values are non-secret and are restored after ISCC
returns. No certificate identifier, key path, or secret is committed to the
Inno source.

`SignedUninstaller=yes` makes Inno Setup sign `unins???.exe` and any temporary
self-copies. When an automatic SignTool is configured, Inno uses a temporary
uninstaller image rather than retaining the `SignedUninstallerDir` cache used by
manual signing. The wrapper captures that temporary signed image before it is
embedded. The ordinary development compile sets `SignedUninstaller=no` and has
no named SignTool.

## Independent verification

Run the verifier in a new PowerShell process so its process exit code can be
checked directly:

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\scripts\Verify-WindowsRelease.ps1 `
  -ArtifactsDirectory .\dist-release `
  -ExpectedVersion 0.4.0 `
  -ExpectedCertificateThumbprint $thumbprint

if ($LASTEXITCODE -ne 0) {
  throw "Ownkey release verification failed with exit code $LASTEXITCODE"
}
```

The verifier returns nonzero unless:

- metadata identifies `PublicRelease` mode;
- the expected installer, backend, and overlay exist;
- captured Inno images include one exact copy of the final installer and at
  least one distinct generated-uninstaller image;
- metadata's executable inventory exactly matches every `.exe` found below the
  artifact directory;
- every executable has Authenticode status `Valid`;
- SignTool validates every signature under the Windows default authentication
  policy;
- every signature has a timestamp;
- every executable has the same publisher subject and signer thumbprint; and
- optional expected subject or thumbprint arguments match.

Build preflight requires the signer certificate to be valid at the current
time. Later verification permits that certificate to have expired only when
Windows and SignTool accept the RFC3161 timestamp under Authenticode policy.
This preserves the point of timestamping without weakening build-time checks.

To pin both identity fields, also pass
`-ExpectedCertificateSubject '<exact distinguished subject>'`. The verifier
does not need the private key.

## CI and secret boundaries

The repository scripts accept certificate-store selectors only. They do not
accept a PFX path, certificate password, provider API key, or hardware-token
PIN.

A release job should separate secret provisioning from this repository:

1. Use an ephemeral Windows runner.
2. In a protected CI step, provision the certificate and private key into the
   chosen Windows `My` store. A certificate provider, KSP, or hardware-backed
   service may require its own secret material.
3. Store the certificate thumbprint and timestamp URL as ordinary CI variables.
4. Run `build-release.ps1`.
5. Run the independent verification command again before an upload job can read
   `dist-release`.
6. Remove provisioned key material and destroy the runner according to the
   provider's instructions.

If a CI system imports a PFX, keep its bytes and password in that system's
protected secret store. Perform the import outside these scripts, do not echo
the import command, and do not place the PFX or password in the checkout, build
arguments, logs, metadata, caches, or artifacts. Prefer a non-exportable or
hardware-backed private key when the certificate provider supports it.

The pipeline deliberately has no self-signed release mode. The static tests
create uniquely named, non-exportable self-signed certificates only to prove
that public preflight rejects them. Tests remove those certificates in a
`finally` block.

An optional integration test exercises SignTool, RFC3161 timestamping, and the
Inno wrapper with an unmistakably named self-signed local certificate:

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\tests\SigningPipeline.LocalTest.ps1
```

It first records that public policy rejects the one-hour certificate. For each
file, it requires a matching embedded signer, an RFC3161 timestamp, no
Authenticode hash mismatch, and a SignTool result whose only failure is the
self-signed trust chain. Its JSON report records cryptographic integrity as
verified and public trust-chain suitability as false. It also proves that the
public verifier rejects `LocalSigningTest` metadata. The test does not add the
certificate to a trust store and never executes the generated installer. It
removes the private-key certificate in `finally`. Output is deleted unless
`-KeepArtifacts` is passed, and any kept path and filename contains
`SELF-SIGNED-LOCAL-TEST`.

## What Windows warnings mean

Three checks answer different questions:

- **Cryptographic validity:** The file has not changed since the holder of the
  private key signed it, and the timestamp is cryptographically present.
- **Windows trust-chain validity:** Windows can build the signer and timestamp
  chains to roots it trusts under the selected policy, including revocation and
  validity checks. A self-signed development certificate can produce a sound
  cryptographic signature while failing this check.
- **SmartScreen reputation:** Microsoft evaluates reputation separately from
  Authenticode validity. A correctly signed new release may still show a
  SmartScreen warning until the publisher or artifact develops reputation, or
  until the applicable certificate and organization policy treats it as
  trusted.

Signing does not guarantee a warning-free install. It establishes publisher
identity and tamper evidence. Windows trust policy and SmartScreen reputation
still determine what the user sees.

## Checks for contributors

Run both test suites:

```powershell
py -m unittest discover -s tests

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\tests\SigningPipeline.Tests.ps1

# Optional end-to-end self-signed integration test
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\tests\SigningPipeline.LocalTest.ps1
```

The PowerShell suite checks syntax, selectors, timestamp URL restrictions,
self-signed, expired, missing-private-key and wrong-EKU certificate failures,
an untrusted non-self-signed chain, Inno mode separation, signing switches,
secret-input boundaries, and verifier failure on an incomplete artifact set.

## Primary references

- [Microsoft SignTool documentation](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)
- [Microsoft Authenticode timestamping documentation](https://learn.microsoft.com/en-us/windows/win32/seccrypto/time-stamping-authenticode-signatures)
- [Inno Setup SignTool directive](https://jrsoftware.org/ishelp/topic_setup_signtool.htm)
- [Inno Setup SignedUninstaller directive](https://jrsoftware.org/ishelp/topic_setup_signeduninstaller.htm)
- [Inno Setup command-line compiler](https://jrsoftware.org/ishelp/topic_compilercmdline.htm)
- [DigiCert RFC3161 timestamp service](https://knowledge.digicert.com/general-information/rfc3161-compliant-time-stamp-authority-server)
