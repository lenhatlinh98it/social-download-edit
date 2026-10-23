# Grabber setup - run via setup.bat (double-click). Safe to run again: it repairs / updates.
#  1. Python 3.10-3.13 (installs Python 3.12 for the current user via winget if missing)
#  2. .venv + pinned packages from requirements.txt
#  3. Chromium for Playwright (Facebook / Douyin)
#  4. Portable ffmpeg (LGPL build) into .\bin
#  5. Microsoft Edge WebView2 runtime (UI), if missing
#  6. "Grabber" shortcut on the Desktop
param([switch]$NoShortcut)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
$Root = $PSScriptRoot
Set-Location $Root

$FfmpegUrl = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip'
$WebView2Url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
$WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    $msg" -ForegroundColor Green }

function Run([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "'$exe $($arguments -join ' ')' failed (exit code $LASTEXITCODE)" }
}

function Get-PythonVersion([string]$exe, [string[]]$prefix) {
    try {
        $v = & $exe @prefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match '^3\.(\d+)$') { return [int]$Matches[1] }
    } catch { }
    return $null
}

function Find-Python {
    # py launcher first, then python on PATH (skips the Microsoft Store stub, which prints no version)
    $candidates = @(
        @{ exe = 'py'; prefix = @('-3') },
        @{ exe = 'python'; prefix = @() },
        @{ exe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"; prefix = @() }
    )
    foreach ($c in $candidates) {
        $minor = Get-PythonVersion $c.exe $c.prefix
        if ($minor -ge 10 -and $minor -le 13) { return $c }
    }
    return $null
}

try {
    # ---------- 1. Python ----------
    Step 'Checking Python'
    $py = Find-Python
    if (-not $py) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw 'Python 3.10-3.13 not found and winget is unavailable. Install Python 3.12 from https://www.python.org/downloads/ and run setup again.'
        }
        Write-Host '    Installing Python 3.12 (current user)...'
        Run 'winget' @('install', '-e', '--id', 'Python.Python.3.12', '--scope', 'user',
                       '--accept-package-agreements', '--accept-source-agreements', '--silent')
        $py = Find-Python
        if (-not $py) { throw 'Python was installed but could not be found. Close this window and run setup again.' }
    }
    Ok "Using $($py.exe) $($py.prefix) (Python 3.$(Get-PythonVersion $py.exe $py.prefix))"

    # ---------- 2. Virtual environment + packages ----------
    Step 'Installing Python packages into .venv'
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) { Run $py.exe ($py.prefix + @('-m', 'venv', '.venv')) }
    Run $venvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--upgrade', 'pip')
    Run $venvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', 'requirements.txt')
    Ok 'Packages installed'

    # ---------- 3. Chromium for Playwright ----------
    Step 'Installing Chromium for Facebook / Douyin (first time ~150 MB)'
    Run $venvPy @('-m', 'playwright', 'install', 'chromium')
    Ok 'Chromium ready'

    # ---------- 4. ffmpeg ----------
    Step 'Checking ffmpeg'
    $bin = Join-Path $Root 'bin'
    if (Test-Path (Join-Path $bin 'ffmpeg.exe')) {
        Ok 'ffmpeg already in .\bin'
    } else {
        Write-Host '    Downloading portable ffmpeg (LGPL build, ~180 MB)...'
        $zip = Join-Path $env:TEMP 'grabber-ffmpeg.zip'
        $unz = Join-Path $env:TEMP 'grabber-ffmpeg'
        Invoke-WebRequest -Uri $FfmpegUrl -OutFile $zip -UseBasicParsing
        if (Test-Path $unz) { Remove-Item $unz -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $unz
        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        foreach ($name in 'ffmpeg.exe', 'ffprobe.exe') {
            $file = Get-ChildItem -Path $unz -Recurse -Filter $name | Select-Object -First 1
            if (-not $file) { throw "$name not found in the ffmpeg archive" }
            Copy-Item $file.FullName -Destination $bin -Force
        }
        Remove-Item $zip, $unz -Recurse -Force
        Ok 'ffmpeg installed into .\bin'
    }

    # ---------- 5. WebView2 runtime ----------
    Step 'Checking Microsoft Edge WebView2 runtime'
    $hasWebView2 = $false
    foreach ($hive in "HKLM:\SOFTWARE\WOW6432Node\$WebView2Key", "HKLM:\SOFTWARE\$WebView2Key", "HKCU:\SOFTWARE\$WebView2Key") {
        $pv = (Get-ItemProperty -Path $hive -Name pv -ErrorAction SilentlyContinue).pv
        if ($pv -and $pv -ne '0.0.0.0') { $hasWebView2 = $true }
    }
    if ($hasWebView2) {
        Ok 'WebView2 already installed'
    } else {
        Write-Host '    Installing WebView2 runtime...'
        $wv = Join-Path $env:TEMP 'MicrosoftEdgeWebview2Setup.exe'
        Invoke-WebRequest -Uri $WebView2Url -OutFile $wv -UseBasicParsing
        Start-Process -FilePath $wv -ArgumentList '/silent', '/install' -Wait
        Ok 'WebView2 installed'
    }

    # ---------- 6. Desktop shortcut ----------
    if (-not $NoShortcut) {
        Step 'Creating Desktop shortcut'
        $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Grabber.lnk'
        $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
        $s.TargetPath = Join-Path $Root '.venv\Scripts\pythonw.exe'
        $s.Arguments = '"' + (Join-Path $Root 'app.py') + '"'
        $s.WorkingDirectory = $Root
        $s.IconLocation = (Join-Path $Root 'assets\icon.ico') + ',0'
        $s.Description = 'Shorts & Reels Downloader'
        $s.Save()
        Ok $lnk
    }

    Write-Host "`nSetup complete. Open 'Grabber' from your Desktop (or run.bat)." -ForegroundColor Green
    exit 0
} catch {
    Write-Host "`nSetup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
