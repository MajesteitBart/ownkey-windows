# Install Ownkey on Windows

This guide is for people who want to install and use Ownkey on Windows 10 or
11.

## What you need

- Windows 10 or 11 on a compatible 64-bit PC
- A microphone allowed under **Windows Settings → Privacy & security →
  Microphone**
- For transcription, either the local Orukeet model (a one-time 487 MB
  download, no API key) or an API key for OpenAI, Google Gemini, or Mistral

Rewriting is optional and can use OpenAI, Anthropic, Google Gemini, Mistral,
OpenRouter, a custom OpenAI-compatible server, or local Ollama.

### For local transcription

Local (Orukeet) runs on a 64-bit Intel or AMD CPU. No dedicated graphics card,
Python installation, or CUDA setup is needed.

- Budget **2 GB of available RAM** for short dictation, in addition to Windows
  and other apps. Our test process peaked at about 1.3 GB; longer recordings
  can use more. This is guidance, not a tested minimum.
- Allow **1.3 GB of free disk space** for downloading and extracting the model,
  in addition to the app. The download is **487 MB** and the installed model
  occupies **672 MB**.
- Internet access is needed to download the model. After setup, local
  transcription works offline without an API key.

In our CPU test on an Intel Core i5-13600KF, 6-7 seconds of synthetic English
or Dutch speech decoded in less than half a second after model loading.
Loading itself took about two seconds. These are recognition timings, not the
full recording-to-paste delay; performance varies with your PC and recording.
See [the measurements](ORUKEET_VALIDATION.md#v050-release-copy-measurements).

For fully offline dictation, turn rewriting off or use local Ollama. Cloud
rewriting still sends the transcript to the selected provider.

## Install the app

1. Open the
   [latest GitHub Release](https://github.com/MajesteitBart/ownkey-windows/releases/latest).
2. Download `Ownkey-Setup-<version>.exe` from **Assets**.
3. Run the installer and follow the setup wizard.
4. Leave **Launch Ownkey** selected on the final page, or open Ownkey from the
   Start menu or desktop shortcut.
5. Right-click the Ownkey tray icon and choose **Settings**.
6. On the **Transcription** page choose **Local (Orukeet)** and click
   **Download**. Wait until the status reads **Installed · Not active** before
   you continue; saving while the download is still running fails. Or pick a
   cloud provider, enter its API key, click **Refresh**, and choose a
   transcription model.
7. Optionally set up a **Rewriting** provider: enter the key, refresh the model
   list, and choose a model. For **Custom (OpenAI-compatible)**, first enter
   the server's full request URL as the endpoint; a key is needed only when
   that server requires one. Ollama defaults to the local server.
8. On the **Dictionary** page add your name and the terms you use, so they are
   typed the way you spell them.
9. Click **Save**. Settings and dictionary entries are only stored when you
   save.

The public `v0.3.0`, `v0.5.0`, and `v0.6.0` installers are unsigned, so Windows
may show an **Unknown publisher** warning. Before continuing, check that the
installer came from the official `MajesteitBart/ownkey-windows` GitHub release
and that its SHA-256 hash matches the `.sha256` file attached to that release:

```powershell
Get-FileHash .\Ownkey-Setup-0.6.0.exe -Algorithm SHA256
```

Future release-qualified installers must pass the repository's Authenticode
verification script and identify one consistent publisher for the installer and
packaged executables. A valid signature does not guarantee that SmartScreen
will show no warning. SmartScreen reputation is separate from cryptographic
signature and Windows certificate-chain validation.

## First use

1. Focus a text field in any application.
2. Hold `Right Alt`, speak, and release the key.
3. Wait for Ownkey to transcribe and insert the text.

To rewrite selected text, highlight it, hold `Right Ctrl`, speak an instruction,
and release the key.

## Configuration and API keys

Ownkey stores its settings and API keys locally in:

```text
%APPDATA%\Ownkey\config.json
```

Treat this file as sensitive. With a cloud transcription provider, Ownkey sends
audio directly to that provider. With Local (Orukeet), audio stays on your PC.
Selected text goes to the rewrite provider only when you use rewriting. There
is no Ownkey account, relay server, or telemetry service in the middle.

## Troubleshooting

- **The hotkey does not work in an elevated app:** run Ownkey as Administrator.
- **No audio is captured:** check the Windows microphone privacy setting.
- **Text is not inserted:** disable paste mode in Ownkey Settings so text is
  typed key by key.
- **A provider or model fails:** verify the activity-specific key and endpoint,
  then use **Refresh** to confirm that the provider returns models.

## Run from source

Developers can run Ownkey without installing it:

```powershell
git clone https://github.com/MajesteitBart/ownkey-windows.git
cd ownkey-windows
py -m pip install -r requirements.txt
py ownkey.py
```

## Uninstall

Open **Windows Settings → Apps → Installed apps**, find **Ownkey**, and choose
**Uninstall**.
