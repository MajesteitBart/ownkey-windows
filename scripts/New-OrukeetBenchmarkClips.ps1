# Synthetic, local speech fixtures. Requires installed Zira and OneCore Frank.
# These clips measure runtime behavior; they are not a human speech accuracy test.
param([string]$OutputDirectory = "$PSScriptRoot\..\build\orukeet-clips")
$ErrorActionPreference = 'Stop'
$fixtureDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $fixtureDirectory | Out-Null
$speaker = New-Object -ComObject SAPI.SpVoice
$voices = @{
    en = 'HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_ZIRA_11.0'
    nl = 'HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\MSTTS_V110_nlNL_Frank'
}
$phrases = @{
    en = 'Today we are testing local speech recognition. The audio stays on this computer.'
    nl = 'Vandaag testen we lokale spraakherkenning. De audio blijft op deze computer.'
}
foreach ($language in @('en', 'nl')) {
    $token = New-Object -ComObject SAPI.SpObjectToken
    $token.SetId($voices[$language])
    $speaker.Voice = $token
    foreach ($length in @('short', 'long')) {
        $stream = New-Object -ComObject SAPI.SpFileStream
        $stream.Format.Type = 18 # 16 kHz, 16-bit, mono
        $stream.Open((Join-Path $fixtureDirectory "$language-$length.wav"), 3, $false)
        $speaker.AudioOutputStream = $stream
        $phrase = $phrases[$language]
        if ($length -eq 'long') { $phrase = (($phrase + ' ') * 6).Trim() }
        $speaker.Speak($phrase) | Out-Null
        $stream.Close()
    }
}
