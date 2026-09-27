param(
    [Parameter(Mandatory = $true)][string]$Text,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Voice = 'Microsoft Zira Desktop',
    [int]$Rate = -1
)

# One line of narration to one WAV. `SetOutputToWaveFile` is the method that exists on this
# API; `SetWaveFile` does not, and calling it fails at runtime rather than at parse time.
# Rate -1 is the measured choice: the default reads as rushed on review pacing, and anything
# slower starts to sound like a warning message.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SelectVoice($Voice)
    $synth.Rate = $Rate
    $synth.Volume = 100
    $parent = Split-Path -Parent $Out
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $synth.SetOutputToWaveFile($Out)
    $synth.Speak($Text)
    $synth.Dispose()
}
finally {
    if (Test-Path $Out) {
        Write-Output ("{0}`t{1}" -f $Out, (Get-Item $Out).Length)
    }
    else {
        Write-Error "no wave written to $Out"
        exit 1
    }
}
