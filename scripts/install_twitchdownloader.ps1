#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Workspace = (Join-Path (Split-Path -Parent $PSScriptRoot) '.tools')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$releaseVersion = '1.56.5'
$archiveName = 'TwitchDownloaderCLI-1.56.5-Windows-x64.zip'
$archiveUrl = 'https://github.com/lay295/TwitchDownloader/releases/download/1.56.5/TwitchDownloaderCLI-1.56.5-Windows-x64.zip'
$archiveHash = '8b1b0695f2b1b6bf0d2535fab4b84032951cded8cf4078dfdf4d58e391c813a0'
$stage = $null
$installerLock = $null

function Assert-PlainDirectory([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing non-directory or reparse-point directory: $Path"
        }
    }
}

function Invoke-CliProbe([string]$Exe, [string]$Arguments) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $Exe
    $start.Arguments = $Arguments
    $start.WorkingDirectory = [IO.Path]::GetDirectoryName($Exe)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw "Could not start CLI probe: $Arguments" }
        $outTask = $process.StandardOutput.ReadToEndAsync()
        $errTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill()
            throw "CLI probe timed out: $Arguments"
        }
        $stdout = $outTask.GetAwaiter().GetResult()
        $stderr = $errTask.GetAwaiter().GetResult()
        [pscustomobject]@{ arguments = $Arguments; exit_code = $process.ExitCode; stdout = $stdout; stderr = $stderr }
    }
    finally { $process.Dispose() }
}

function Test-Cli([string]$Exe) {
    if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { throw "CLI executable missing: $Exe" }
    if ((Get-Item -LiteralPath $Exe -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing reparse-point executable: $Exe"
    }
    $version = [Diagnostics.FileVersionInfo]::GetVersionInfo($Exe).FileVersion
    if ($version -notmatch '^1\.56\.5(?:\.0)?$') {
        throw "Existing or extracted executable version '$version' is not pinned $releaseVersion. It has been preserved."
    }
    $checks = @(
        @{ args = '--version'; required = @('1.56.5') },
        @{ args = 'help'; required = @('videodownload', 'chatdownload', 'info') },
        @{ args = 'videodownload --help'; required = @('--id', '--output', '--threads', '--collision', '--ffmpeg-path') },
        @{ args = 'chatdownload --help'; required = @('--id', '--output', '--compression', '--collision') },
        @{ args = 'info --help'; required = @('--id', '--format', '--oauth') }
    )
    $probes = @()
    foreach ($check in $checks) {
        $probe = Invoke-CliProbe -Exe $Exe -Arguments $check.args
        # CommandLineParser prints help/version and intentionally exits with 1.
        if ($probe.exit_code -notin @(0, 1)) { throw "CLI probe failed ($($probe.exit_code)): $($check.args)" }
        $combined = $probe.stdout + "`n" + $probe.stderr
        foreach ($required in $check.required) {
            if ($combined.IndexOf($required, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
                throw "Unexpected CLI help/version output: '$required' absent in '$($check.args)'."
            }
        }
        $probes += $probe
    }
    [pscustomobject]@{ file_version = $version; sha256 = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant(); probes = $probes }
}

function Expand-VerifiedZip([string]$ZipPath, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Directory]::CreateDirectory($Destination) | Out-Null
    $prefix = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        if ($zip.Entries.Count -gt 10000) { throw 'Unexpected ZIP entry count.' }
        [long]$expandedBytes = 0
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            $isDirectory = $name.EndsWith('/')
            $trimmed = $name.TrimEnd('/')
            if (-not $trimmed -or $name.StartsWith('/') -or $name.Contains(':') -or [IO.Path]::IsPathRooted($name)) {
                throw "Unsafe ZIP path: $name"
            }
            foreach ($part in $trimmed.Split('/')) {
                if (-not $part -or $part -in @('.', '..') -or $part.EndsWith('.') -or $part.EndsWith(' ') -or
                    $part.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or
                    $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
                    throw "Unsafe ZIP path component: $name"
                }
            }
            $unixType = ($entry.ExternalAttributes -shr 16) -band 61440
            if ($unixType -notin @(0, 16384, 32768)) { throw "ZIP link/special file rejected: $name" }
            $target = [IO.Path]::GetFullPath([IO.Path]::Combine($Destination, $trimmed.Replace('/', '\')))
            if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or -not $seen.Add($target)) {
                throw "ZIP traversal or duplicate path rejected: $name"
            }
            $expandedBytes += $entry.Length
            if ($entry.Length -gt 1GB -or $expandedBytes -gt 2GB) { throw 'Unexpected expanded ZIP size.' }
            if ($isDirectory) { [IO.Directory]::CreateDirectory($target) | Out-Null; continue }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
            $inputStream = $entry.Open()
            $outputStream = $null
            try {
                $outputStream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $inputStream.CopyTo($outputStream)
            }
            finally {
                if ($null -ne $outputStream) { $outputStream.Dispose() }
                $inputStream.Dispose()
            }
        }
    }
    finally { $zip.Dispose() }
}

try {
    $workspaceRoot = [IO.Path]::GetFullPath($Workspace).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $workspaceRoot -PathType Container)) { throw "Workspace does not exist: $workspaceRoot" }
    Assert-PlainDirectory $workspaceRoot
    $downloads = Join-Path $workspaceRoot 'downloads'
    $destination = Join-Path $workspaceRoot 'TwitchDownloaderCLI'
    $exe = Join-Path $destination 'TwitchDownloaderCLI.exe'
    Assert-PlainDirectory $downloads
    Assert-PlainDirectory $destination
    [IO.Directory]::CreateDirectory($downloads) | Out-Null
    # Persistent lock file, released by closing its handle; never delete another run's lock.
    $installerLock = [IO.File]::Open((Join-Path $downloads 'TwitchDownloaderCLI.install.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)

    if (Test-Path -LiteralPath $destination) {
        $existingManifest = Join-Path $destination 'install-manifest.json'
        if (Test-Path -LiteralPath $existingManifest -PathType Leaf) {
            $installed = Get-Content -LiteralPath $existingManifest -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($installed.version -ne $releaseVersion -or $installed.archive_sha256 -ne $archiveHash -or
                (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ine $installed.executable_sha256) {
                throw 'Existing install manifest or executable hash differs from the pinned installation. Existing files preserved.'
            }
        }
        $verification = Test-Cli $exe
        [pscustomobject]@{ status = 'already_installed'; executable = $exe; version = $verification.file_version; executable_sha256 = $verification.sha256; help_verified = $true; existing_files_preserved = $true; installer_manifest_present = (Test-Path -LiteralPath $existingManifest -PathType Leaf) } | ConvertTo-Json -Depth 4
        exit 0
    }

    $stage = Join-Path $downloads ('.tdcli-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    $cacheZip = Join-Path $downloads $archiveName
    $archive = Join-Path $stage $archiveName
    if ((Test-Path -LiteralPath $cacheZip -PathType Leaf) -and
        (Get-FileHash -LiteralPath $cacheZip -Algorithm SHA256).Hash -ieq $archiveHash) {
        $archive = $cacheZip
        Write-Host 'Using verified cached official CLI archive.'
    }
    else {
        Write-Host "Downloading pinned official CLI $releaseVersion to workspace staging."
        $oldProtocol = [Net.ServicePointManager]::SecurityProtocol
        try {
            [Net.ServicePointManager]::SecurityProtocol = $oldProtocol -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $archiveUrl -OutFile $archive -UseBasicParsing -TimeoutSec 180 -Headers @{ 'User-Agent' = 'twitch-archive-publisher-installer' }
        }
        finally { [Net.ServicePointManager]::SecurityProtocol = $oldProtocol }
    }
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $archiveHash) { throw "Official archive SHA256 mismatch. Expected $archiveHash; got $actualHash. No executable installed or run." }
    $payload = Join-Path $stage 'payload'
    Expand-VerifiedZip -ZipPath $archive -Destination $payload
    $verification = Test-Cli (Join-Path $payload 'TwitchDownloaderCLI.exe')
    $manifest = [ordered]@{
        version = $releaseVersion; source_url = $archiveUrl; archive_sha256 = $actualHash
        installed_at_utc = [DateTime]::UtcNow.ToString('o'); executable_sha256 = $verification.sha256
        help_probes = $verification.probes; gui_untouched = $true
    }
    [IO.File]::WriteAllText((Join-Path $payload 'install-manifest.json'), ($manifest | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $destination) { throw "Destination appeared during installation; refusing overwrite: $destination" }
    # Same-volume directory rename publishes the fully verified payload atomically.
    [IO.Directory]::Move($payload, $destination)
    if ($archive -ne $cacheZip -and -not (Test-Path -LiteralPath $cacheZip)) { [IO.File]::Move($archive, $cacheZip) }
    [pscustomobject]@{ status = 'installed'; executable = $exe; version = $verification.file_version; archive_sha256 = $actualHash; executable_sha256 = $verification.sha256; help_verified = $true; manifest = (Join-Path $destination 'install-manifest.json') } | ConvertTo-Json -Depth 4
    exit 0
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
finally {
    if ($null -ne $stage -and (Test-Path -LiteralPath $stage)) {
        # Recursive deletion is limited to this run's checked absolute staging path.
        $resolvedStage = [IO.Path]::GetFullPath($stage)
        $allowedPrefix = [IO.Path]::GetFullPath($downloads).TrimEnd('\') + '\'
        if ($resolvedStage.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($resolvedStage) -match '^\.tdcli-[0-9a-f]{32}$') {
            Remove-Item -LiteralPath $resolvedStage -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if ($null -ne $installerLock) { $installerLock.Dispose() }
}
