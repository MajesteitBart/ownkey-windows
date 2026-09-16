# Ownkey Windows signing-enforced release pipeline

Work in the existing clean checkout `E:\Development\voicekey-windows` on current `main` at `69923eacf2654db978b390955bcfa12e634e5de5`. Do not create a new worktree. Read the repository README and existing build/installer sources before editing.

## Outcome

Make the Windows release build signing-enforced and independently auditable, while remaining usable for ordinary unsigned local development builds.

The public `v0.3.0` installer and packaged executables are currently unsigned. The machine has only self-signed development certificates. Do not treat those as public trust and do not publish or upload anything.

## Required implementation

1. Add a release build mode that signs every executable that will ship, including the packaged Python backend, Tauri overlay, and final Inno Setup installer. Cover the generated uninstaller or any other executable payload where technically applicable and explain any Inno Setup signing-tool integration needed.
2. Accept certificate selection through a non-secret stable selector such as a SHA-1 thumbprint or configurable subject. Do not embed passwords, PFX paths, secret values, or machine-specific certificate identifiers in committed source.
3. Timestamp signatures through a configurable RFC3161 timestamp URL, with a safe documented default if appropriate.
4. Fail the release build if the signing certificate is missing, expired, unsuitable for code signing, self-signed/untrusted where public-release mode requires trust, or if any required output does not verify as `Valid` after signing.
5. Preserve a clearly named unsigned development build path so contributors can build without a certificate. It must not be easy to mistake that output for a release-qualified installer.
6. Add an independent PowerShell verification command/script that returns nonzero unless all expected release artifacts exist and have valid Authenticode signatures, consistent publisher identity, and a timestamp where expected.
7. Update README/release documentation with exact setup, build, verification, CI/release-secret boundaries, and the distinction between cryptographic validity, Windows trust-chain validity, and SmartScreen reputation. State plainly that signing does not guarantee no warning until reputation or a suitable certificate policy establishes it.
8. Add or improve automated tests/static checks for the scripts and failure modes. Exercise the build path as far as possible on this machine. You may use a self-signed certificate only for an explicitly marked local-test mode; never present it as release proof.

## Verification

Run the relevant repository tests and script checks. Build unsigned development artifacts if useful. Exercise release mode and prove it fails safely with the current unsuitable certificate state rather than emitting a release-qualified installer. If a local-test signing mode is implemented, prove its outputs verify cryptographically while the report still identifies the chain as unsuitable for public release.

Create `docs/reports/2026-09-02-signing-pipeline-report.md` with changed files, commands, actual output summary, remaining blockers, and exact next step for a publicly trusted publisher certificate.

## Boundaries

- Do not purchase or enroll for a certificate.
- Do not use passwords, provider/API keys, or browser account credentials.
- Do not publish, push, create a GitHub release, modify the live site, install into production paths, or contact anyone.
- Preserve unrelated work.
- Commit the completed change locally on the current branch only if all repository checks pass. Do not push.
