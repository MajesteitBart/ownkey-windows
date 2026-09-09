# Signing pipeline report, 2026-09-02

## Scope and baseline

Work started on `main` at
`69923eacf2654db978b390955bcfa12e634e5de5`. The existing README, build batch
files, PyInstaller specification, Tauri configuration, and Inno Setup source
were reviewed before editing.

The existing public-looking `v0.3.0` installer and its packaged executables had
no Authenticode signatures. This machine had only self-signed development
code-signing certificates, so it could not produce evidence of a qualified
public release. No artifact was published or uploaded.

The pre-existing untracked file `docs/2026-09-02-signing-brief.md` was left
unchanged and is not part of this implementation.

## Changed files

- `.gitignore`: ignores the separate development and public-release output
  directories.
- `README.md`: documents the unsigned contributor path, signed release entry
  point, and checks.
- `build.bat`: makes the backend-only build explicitly unsigned development
  output and propagates failures.
- `build-installer.bat`: delegates to the unsigned development mode and prints
  a prominent non-release warning.
- `build-release.ps1`: provides the signing-enforced public entry point.
- `docs/USER_INSTALLATION.md`: records the unsigned `v0.3.0` status and explains
  the SmartScreen distinction.
- `docs/WINDOWS_RELEASE_SIGNING.md`: documents setup, policy, build,
  verification, Inno integration, CI boundaries, and local testing.
- `docs/reports/2026-09-02-signing-pipeline-report.md`: records this work and its
  evidence.
- `installer/Ownkey.iss`: separates output names and enables the named signing
  tool plus signed uninstallers only for release mode.
- `overlay-ui/package.json` and `overlay-ui/README.md`: add and document a
  binary-only Tauri release build, avoiding unused MSI and NSIS bundles.
- `scripts/Build-WindowsInstaller.ps1`: builds isolated candidates, signs every
  packaged `.exe`, captures Inno-generated signed images, verifies them, and
  promotes output only after success.
- `scripts/Invoke-InnoSigning.ps1`: gives Inno Setup a checked signing command
  and retains audit copies of its installer and generated uninstaller images.
- `scripts/Ownkey.Signing.psm1`: implements selector validation, certificate
  policy, SHA-256/RFC3161 signing, trust checks, and Authenticode verification.
- `scripts/Verify-WindowsRelease.ps1`: independently checks artifact presence,
  exact executable inventory, signatures, timestamps, publisher consistency,
  Windows trust, and captured uninstaller evidence.
- `tests/SigningPipeline.Tests.ps1`: tests parsing and the principal failure
  policies without Pester.
- `tests/SigningPipeline.LocalTest.ps1`: runs the explicitly marked local-only
  signing integration and proves that its output cannot qualify as public.

## Commands and actual results

### Repository and tool baseline

```powershell
git rev-parse HEAD
git branch --show-current
py --version
py -m PyInstaller --version
pnpm --version
rustc --version
```

Result: commit `69923eacf2654db978b390955bcfa12e634e5de5` on `main`; Python
3.14.2; PyInstaller 6.19.0; pnpm 10.30.3; and rustc 1.93.0. Inno Setup 6 and
Windows SDK SignTool 10.0.26100.0 were found in their standard installation
locations. PSScriptAnalyzer was not installed, so the repository test parses
every changed PowerShell script through PowerShell's language parser instead.

### Unsigned development build

```powershell
cmd.exe /d /c .\build-installer.bat -SkipDependencyInstall
```

Result: exit 0. The batch file built the PyInstaller directory bundle, Tauri
binary, and Inno installer. It produced:

| File | Bytes | SHA-256 | Authenticode |
|---|---:|---|---|
| `dist\Ownkey\Ownkey.exe` | 8,341,363 | `DC3805D5877783A626A825BCEA96DA88A7E4EF6872F0BD8BBE97B9E781041D5F` | `NotSigned` |
| `overlay-ui\src-tauri\target\release\ownkey-overlay.exe` | 8,790,016 | `17471F379807D940D46EABD36E0038B87404972207D755AB78F9DCEA0AD7344C` | `NotSigned` |
| `dist-installer-dev\Ownkey-Setup-0.3.0-UNSIGNED-DEV.exe` | 31,502,093 | `57C19C20676FD43294B40F2110B055DB3034A3A28F11E5057572E7A5308DF017` | `NotSigned` |

`dist-installer-dev\UNSIGNED-DEVELOPMENT-BUILD.txt` also states that the output
is not qualified for a public release. No `dist-release` directory was made.

### Automated checks

```powershell
py -m unittest discover -s tests -v

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\tests\SigningPipeline.Tests.ps1

Set-Location .\overlay-ui
pnpm lint
```

Results:

- Python: 10 tests passed, exit 0.
- Signing pipeline: 13 checks passed, exit 0. These included missing and
  ambiguous selectors; unsafe timestamp URLs; self-signed, expired,
  private-keyless, wrong-EKU, and untrusted-chain certificates; Inno mode
  separation; signing switches; secret-input boundaries; and rejection of an
  incomplete release fixture.
- Frontend lint: passed, exit 0.

### Local-only signing integration

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\tests\SigningPipeline.LocalTest.ps1
```

Result: exit 0. The test created a unique one-hour, non-exportable self-signed
certificate in `Cert:\CurrentUser\My`, signed and RFC3161-timestamped five
executables, and captured one Inno-generated uninstaller image. It reported:

```text
Self-signed local signing integration test passed.
Public release eligible: False
Public policy rejection: The selected certificate is self-signed and cannot qualify a public release.
Cryptographically checked self-signed executables: 5
Windows default trust policy valid: False
Captured generated uninstallers: 1
Public verifier exit code: 1
```

The test required embedded signer and timestamp certificates, rejected hash
mismatches, and accepted only the expected untrusted-root failure from Windows
policy. It deleted the test artifact directory and certificate in `finally`.
This is local cryptographic test evidence, not public-release proof.

### Public-release safe failure

The real entry point was invoked with a current self-signed development
code-signing certificate selected dynamically from `Cert:\CurrentUser\My`.
The machine-specific thumbprint was neither printed in this report nor added to
source.

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\build-release.ps1 `
  -CertificateThumbprint $developmentCertificate.Thumbprint `
  -CertificateStoreLocation CurrentUser `
  -SkipDependencyInstall
```

Result: the release process returned exit 1 during preflight with
`The selected certificate is self-signed and cannot qualify a public release.`
There were zero `build\release-operation-*` directories before and after the
probe, and `dist-release` did not exist. The unsuitable certificate state
therefore failed closed without compiling or emitting release-qualified output.

### Final hygiene checks

```powershell
git diff --check
Get-ChildItem .\build -Directory |
  Where-Object Name -Like 'signing-local-test-*'
Get-ChildItem Cert:\CurrentUser\My |
  Where-Object Subject -Like '*Ownkey Signing Pipeline*'
Test-Path .\dist-release
```

Result: `git diff --check` returned exit 0. No local-test build directories or
temporary test certificates remained, and `Test-Path .\dist-release` returned
`False`.

## Release properties now enforced

- The certificate is selected from a Windows `My` store by a non-secret SHA-1
  thumbprint or exact subject. No PFX path, password, provider key, or
  machine-specific selector is committed.
- Public preflight rejects a missing or ambiguous certificate, inaccessible
  private key, invalid validity period, CA certificate, missing Code Signing
  EKU, incompatible Key Usage, SHA-1 certificate signature, self-signed
  certificate, untrusted or revocation-invalid chain, and a root outside the
  Windows third-party `AuthRoot` store.
- Payload executables, the final installer, and Inno's generated uninstaller
  image are signed with SHA-256 and timestamped through a configurable RFC3161
  URL.
- A public candidate is not promoted until a separate PowerShell process has
  verified exact inventory, `Valid` Authenticode status, timestamps, one signer
  identity, Windows default authentication policy, and public certificate
  policy. The promoted directory is checked again.
- Unsigned development output has a separate directory, an `UNSIGNED-DEV`
  filename, a marker file, and no Inno signing-tool configuration.

## Remaining blocker and exact next step

The remaining blocker is a CA-issued public code-signing certificate and its
private key. The signer must satisfy the documented policy and chain to a root
in the Windows third-party root program. The certificates currently installed
on this machine cannot do that.

After a certificate provider or protected CI provisioning step installs the
publisher certificate and accessible private key into a Windows `My` store,
select it without writing its value into the repository and run:

```powershell
$matches = @(Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
  Where-Object { $_.HasPrivateKey -and $_.Subject -eq '<publisher subject>' })
if ($matches.Count -ne 1) {
  throw "Expected one publisher certificate, found $($matches.Count)."
}
$certificate = $matches[0]

.\build-release.ps1 `
  -CertificateThumbprint $certificate.Thumbprint `
  -CertificateStoreLocation CurrentUser

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\scripts\Verify-WindowsRelease.ps1 `
  -ArtifactsDirectory .\dist-release `
  -ExpectedVersion 0.3.0 `
  -ExpectedCertificateThumbprint $certificate.Thumbprint
```

Run those commands from a clean tracked checkout. Successful verification is
evidence of signed files and a Windows-trusted chain. It does not promise that
SmartScreen will suppress warnings; publisher or artifact reputation and the
applicable certificate policy remain separate factors.
