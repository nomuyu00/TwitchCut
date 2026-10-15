#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$WithAsr,
    [string]$Python = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$setupDir = Join-Path $repo '.setup'
$venv = Join-Path $repo '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$helper = Join-Path $repo 'scripts\setup_env.py'
$logStarted = $false

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

function Test-Python([string]$Exe, [string[]]$Prefix = @()) {
    try {
        $probe = & $Exe @Prefix -c "import sys, struct; print(sys.executable); sys.exit(0 if sys.version_info[:2] in ((3,11),(3,12)) and struct.calcsize('P')==8 else 1)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe) { return ([string]($probe | Select-Object -Last 1)).Trim() }
    } catch { }
    return $null
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'This launcher supports Windows 10/11 x64. See README.md for other environments.' }
    New-Item -ItemType Directory -Path $setupDir -Force | Out-Null
    $temporaryDir = Join-Path $setupDir 'temp'
    New-Item -ItemType Directory -Path $temporaryDir -Force | Out-Null
    # Set this before venv/ensurepip: sandboxed Windows hosts may have a read-only
    # redirected system temp directory. All setup scratch files stay in the repo.
    $env:TEMP = $temporaryDir
    $env:TMP = $temporaryDir
    $logPath = Join-Path $setupDir ('setup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $PID + '.log')
    Start-Transcript -LiteralPath $logPath | Out-Null
    $logStarted = $true
    Write-Host 'TwitchCut setup: local tools, CPU support, no login or video download.'
    Write-Host "Log: $logPath"

    if (Test-Path -LiteralPath $venv) {
        $resolvedPython = Test-Python $venvPython
        if (-not $resolvedPython) { throw 'Existing .venv is not a working 64-bit Python 3.11/3.12 environment. It was preserved; ask Codex to inspect it.' }
    } else {
        $basePython = $null
        if ($Python) {
            if (-not [IO.Path]::IsPathRooted($Python)) { throw '-Python must be an absolute path to a Python interpreter.' }
            $basePython = Test-Python $Python
            if (-not $basePython) { throw '-Python must point to a working 64-bit Python 3.11 or 3.12 interpreter.' }
        } else {
            $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
            if ($pyLauncher) {
                $basePython = Test-Python $pyLauncher.Source @('-3.12')
                if (-not $basePython) { $basePython = Test-Python $pyLauncher.Source @('-3.11') }
            }
            if (-not $basePython) {
                $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
                if ($pythonCommand -and $pythonCommand.Source -notmatch '\\WindowsApps\\') {
                    $basePython = Test-Python $pythonCommand.Source
                }
            }
        }
        if (-not $basePython) {
            throw 'Python 3.11/3.12 x64 was not found. Ask Codex to locate its bundled Python, then rerun with -Python <absolute path>. Or install Python from https://www.python.org/downloads/windows/ and rerun. No global installation was attempted.'
        }
        Invoke-Checked $basePython @('-m', 'venv', $venv)
    }

    # Never activate a shell or install into a global interpreter.
    $env:PIP_CACHE_DIR = Join-Path $setupDir 'pip-cache'
    $env:HF_HOME = Join-Path $setupDir 'huggingface'
    $env:DOTNET_BUNDLE_EXTRACT_BASE_DIR = Join-Path $setupDir 'dotnet'
    $env:PYTHONUTF8 = '1'
    $requirements = Join-Path $repo 'requirements.txt'
    if ($WithAsr) { $requirements = Join-Path $repo 'requirements-asr.txt' }
    Invoke-Checked $venvPython @('-m', 'pip', '--isolated', '--disable-pip-version-check', 'install', '--cache-dir', $env:PIP_CACHE_DIR, '--only-binary=:all:', '--index-url', 'https://pypi.org/simple', '-r', $requirements)
    Invoke-Checked $venvPython @('-m', 'pip', '--isolated', '--disable-pip-version-check', 'check')
    Invoke-Checked $venvPython @($helper, 'install-ffmpeg')

    # The installer checks the fixed release hash and probes its executable.
    $td = Get-Command TwitchDownloaderCLI.exe -ErrorAction SilentlyContinue
    if (-not $td) {
        $toolsDir = Join-Path $repo '.tools'
        New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
        & (Join-Path $repo 'scripts\install_twitchdownloader.ps1') -Workspace $toolsDir
        if ($LASTEXITCODE -ne 0) { throw "TwitchDownloaderCLI installation failed (exit $LASTEXITCODE)." }
    }
    Invoke-Checked $venvPython @($helper, 'init')
    $doctorArgs = @($helper, 'doctor')
    if ($WithAsr) { $doctorArgs += '--require-asr' }
    Invoke-Checked $venvPython $doctorArgs
    Write-Host ''
    Write-Host 'Base environment is ready. Existing recipient settings were preserved.'
    Write-Host 'Next: ask Codex to finish your Twitch / YouTube profile and check the browser connection.'
    if (-not $WithAsr) { Write-Host 'Local transcription is optional at setup; install it with setup.cmd -WithAsr before production.' }
    exit 0
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
} finally {
    if ($logStarted) { Stop-Transcript | Out-Null }
}
