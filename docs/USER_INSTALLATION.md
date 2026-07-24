# Install Ownkey on Windows

This guide is for people who want to install and use Ownkey on Windows 10 or
11.

## What you need

- Windows 10 or 11 on a compatible 64-bit PC
- A microphone allowed under **Windows Settings → Privacy & security →
  Microphone**
- An API key for a supported transcription provider: OpenAI, Google Gemini, or
  Mistral

Rewriting can use OpenAI, Anthropic, Google Gemini, Mistral, or Ollama.

## Install the app

1. Open the
   [latest GitHub Release](https://github.com/MajesteitBart/ownkey-windows/releases/latest).
2. Download `Ownkey-Setup-<version>.exe` from **Assets**.
3. Run the installer and follow the setup wizard.
4. Leave **Launch Ownkey** selected on the final page, or open Ownkey from the
   Start menu or desktop shortcut.
5. Right-click the Ownkey tray icon and choose **Settings**.
6. Configure the **Audio** provider and, optionally, a different **Rewriting**
   provider. Enter each API key, refresh the model list, choose a model, and
   click **Save**.

The installer is currently not code-signed. Windows SmartScreen may therefore
show an **Unknown publisher** warning. Verify that the installer came from the
official `MajesteitBart/ownkey-windows` GitHub release before continuing.

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

Treat this file as sensitive. Ownkey sends audio—and selected text when
rewriting—directly to the provider you configure. There is no Ownkey account,
relay server, or telemetry service in the middle.

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
