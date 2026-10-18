param([Parameter(Mandatory=$true)][string]$Output)
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speaker.SetOutputToWaveFile([System.IO.Path]::GetFullPath($Output))
$text = ConvertFrom-Json '"\u8fd9\u662f\u4e00\u6bb5\u79bb\u7ebf\u8bed\u97f3\u6d4b\u8bd5\u3002\u4eca\u5929\u5904\u7406\u62a5\u9500\u8bb0\u5f55\u3002"'
$speaker.Speak($text)
$speaker.Dispose()
