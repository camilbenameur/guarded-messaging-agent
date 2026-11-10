<#
.SYNOPSIS
    Generates voice-note fixtures for `tests/fixtures/scenario.json`.

.DESCRIPTION
    Uses the Windows System.Speech TTS engine to synthesize four short
    phrases (2 French via "Microsoft Hortense Desktop", 2 English via
    "Microsoft Zira Desktop") to WAV on the host, then shells out to
    `docker compose run --rm ingest ffmpeg` to convert each WAV to
    OGG/Opus at 24 kbps (Telegram voice notes are OGG/Opus). Ground-truth transcripts are saved
    alongside as .txt for the STT fixtures.

    Run once from the repo root (host, not in a container):
        powershell -File scripts/make_voice_fixtures.ps1

    Requires: Docker Desktop running (the `ingest` image's ffmpeg does the
    conversion) and the Hortense (fr-FR) / Zira (en-US) voices installed.
    Verify installed voices with:
        Add-Type -AssemblyName System.Speech
        (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() |
            ForEach-Object { $_.VoiceInfo.Name }

    If a required voice is missing, this script falls back to a synthetic
    tone WAV (clearly not speech) so the pipeline still has a fixture to
    exercise, and leaves a TODO marker file next to it — see the
    `New-ToneWav` fallback below.
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$audioDir = Join-Path $RepoRoot "tests\fixtures\audio"
New-Item -ItemType Directory -Force -Path $audioDir | Out-Null

Add-Type -AssemblyName System.Speech

# Fallback used only if a required voice isn't installed on this machine.
function New-ToneWav {
    param(
        [string]$Path,
        [int]$DurationMs = 1200,
        [int]$SampleRate = 16000,
        [double]$Frequency = 440.0
    )
    $numSamples = [int]($SampleRate * $DurationMs / 1000)
    $bytesPerSample = 2
    $dataSize = $numSamples * $bytesPerSample

    $stream = [System.IO.File]::Create($Path)
    $writer = New-Object System.IO.BinaryWriter($stream)
    try {
        $writer.Write([char[]]"RIFF")
        $writer.Write([int32](36 + $dataSize))
        $writer.Write([char[]]"WAVE")
        $writer.Write([char[]]"fmt ")
        $writer.Write([int32]16)
        $writer.Write([int16]1)                                # PCM
        $writer.Write([int16]1)                                # mono
        $writer.Write([int32]$SampleRate)
        $writer.Write([int32]($SampleRate * $bytesPerSample))  # byte rate
        $writer.Write([int16]$bytesPerSample)                  # block align
        $writer.Write([int16]16)                               # bits/sample
        $writer.Write([char[]]"data")
        $writer.Write([int32]$dataSize)
        for ($i = 0; $i -lt $numSamples; $i++) {
            $t = $i / $SampleRate
            $sample = [int16]([math]::Sin(2 * [math]::PI * $Frequency * $t) * 8000)
            $writer.Write($sample)
        }
        $writer.Flush()
    }
    finally {
        $writer.Close()
        $stream.Close()
    }
}

$phrases = @(
    @{ Name = "fr_meeting"; Voice = "Microsoft Hortense Desktop"; Text = "On se voit demain à 15h pour le rendez-vous." }
    @{ Name = "fr_dossier"; Voice = "Microsoft Hortense Desktop"; Text = "Le dossier a été envoyé au client ce matin." }
    @{ Name = "en_report";  Voice = "Microsoft Zira Desktop";     Text = "Can you send me the report before Friday morning?" }
    @{ Name = "en_meeting"; Voice = "Microsoft Zira Desktop";     Text = "The meeting has been moved to three PM." }
)

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$installedVoices = $synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }

$generated = @()

foreach ($phrase in $phrases) {
    $wavPath = Join-Path $audioDir "$($phrase.Name).wav"
    $txtPath = Join-Path $audioDir "$($phrase.Name).txt"

    if ($installedVoices -contains $phrase.Voice) {
        $synth.SelectVoice($phrase.Voice)
        $synth.SetOutputToWaveFile($wavPath)
        $synth.Speak($phrase.Text)
        $synth.SetOutputToNull()
        Write-Host "TTS OK   : $wavPath ($($phrase.Voice))"
    }
    else {
        Write-Warning "Voice '$($phrase.Voice)' not installed; falling back to a tone placeholder for $($phrase.Name)."
        New-ToneWav -Path $wavPath
        $todoPath = Join-Path $audioDir "$($phrase.Name).TODO.txt"
        $todoText = "TODO: real TTS voice '$($phrase.Voice)' was unavailable on the generating host; this fixture is a synthetic tone, not speech. Regenerate with a machine that has the voice installed."
        [System.IO.File]::WriteAllText($todoPath, $todoText, (New-Object System.Text.UTF8Encoding($false)))
    }

    # Write without a BOM (plain UTF-8) -- Set-Content -Encoding utf8 on
    # Windows PowerShell 5.1 always prepends one, which is unwanted noise in
    # a ground-truth transcript file.
    [System.IO.File]::WriteAllText($txtPath, $phrase.Text, (New-Object System.Text.UTF8Encoding($false)))
    $generated += $phrase.Name
}

$synth.Dispose()

Write-Host ""
Write-Host "Converting WAV -> OGG/Opus (24 kbps) via docker compose run --rm ingest ffmpeg ..."

foreach ($name in $generated) {
    $wavRelPath = "tests/fixtures/audio/$name.wav"
    $oggRelPath = "tests/fixtures/audio/$name.ogg"
    $wavPath = Join-Path $audioDir "$name.wav"

    docker compose run --rm -v "${RepoRoot}:/workspace" -w /workspace ingest ffmpeg -y -i $wavRelPath -ac 1 -c:a libopus -b:a 24k $oggRelPath

    if ($LASTEXITCODE -ne 0) {
        throw "ffmpeg failed converting $wavRelPath"
    }

    Remove-Item $wavPath
    Write-Host "wrote $oggRelPath (removed intermediate wav)"
}

Write-Host ""
Write-Host "Done. Commit the resulting tests/fixtures/audio/*.ogg + *.txt files."
