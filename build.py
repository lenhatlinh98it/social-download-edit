"""Build dist/Grabber/Grabber.exe with PyInstaller and put a shortcut on the Desktop.

Usage:  python -m pip install pyinstaller
        python build.py              # build + Desktop shortcut
        python build.py --no-shortcut
"""
import os
import subprocess
import sys

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "Grabber"
EXE = os.path.join(ROOT, "dist", NAME, f"{NAME}.exe")


def build():
    extra = []
    if os.path.isfile(os.path.join(ROOT, "bin", "ffmpeg.exe")):  # portable ffmpeg from setup.ps1
        extra = ["--add-binary", f"{os.path.join(ROOT, 'bin')}{os.pathsep}bin"]
    PyInstaller.__main__.run([
        *extra,
        os.path.join(ROOT, "app.py"),
        "--name", NAME,
        "--windowed",
        "--noconfirm",
        "--clean",
        "--icon", os.path.join(ROOT, "assets", "icon.ico"),
        "--add-data", f"{os.path.join(ROOT, 'web')}{os.pathsep}web",
        "--add-data", f"{os.path.join(ROOT, 'assets')}{os.pathsep}assets",
        "--collect-all", "playwright",  # python package + bundled Node driver
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build"),
        "--specpath", os.path.join(ROOT, "build"),
    ])


def desktop_shortcut():
    script = (
        "$desktop = [Environment]::GetFolderPath('Desktop');"
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop '{NAME}.lnk'));"
        f"$s.TargetPath = '{EXE}';"
        f"$s.WorkingDirectory = '{os.path.dirname(EXE)}';"
        f"$s.IconLocation = '{EXE},0';"
        "$s.Description = 'Shorts & Reels Downloader';"
        "$s.Save();"
        "Write-Output (Join-Path $desktop '" + NAME + ".lnk')"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=True)
    print("Shortcut:", out.stdout.strip())


if __name__ == "__main__":
    build()
    print("Built:", EXE)
    if "--no-shortcut" not in sys.argv:
        desktop_shortcut()
