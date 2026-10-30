# Grabber

Windows desktop app for listing and batch-downloading short-form video from YouTube, Facebook, Douyin, and Instagram, plus a built-in editor for crop, color, text, music, batch presets, and merge.

Requires **Windows 10/11 (64-bit)** and an internet connection.

## Supported sources

| Platform | What to paste | Notes |
|---|---|---|
| **YouTube Shorts** | Channel URL, e.g. `https://www.youtube.com/@channel/shorts` | Uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| **Facebook Reels** | Page URL, e.g. `https://www.facebook.com/page/reels` | Sign in to Facebook inside the app |
| **Douyin** | User URL, e.g. `https://www.douyin.com/user/MS4wLjABAAAA...` | Guests see ~20 latest videos; sign in for the full list |
| **Instagram Reels** | Profile URL, e.g. `https://www.instagram.com/username/reels/` | Guests see ~12 latest reels; sign in for the full list. Saved in 1080p when available |

Filenames follow the video title or caption. Videos already downloaded (or marked **Add to downloaded**) are hidden on the next fetch.

## Editor

- **Edit** — aspect-ratio or free crop, zoom (including motion zoom), rotate/flip, trim, color + built-in filters, speed, volume, background music, fade, text + logo overlays, strip metadata; undo/redo and keyboard shortcuts (see the keyboard button on the Editor bar).
- **Batch** — save an edit as a preset and apply it to many videos via a queue.
- **Merge** — combine clips into one video (blurred background, letterbox, or crop; crossfade).
- Drag-and-drop video, music, or a logo image onto the window.
- Sources above 4K (e.g. 4320×5760 AV1) are exported scaled down to fit 4K, since H.264 encoders stop there.
- When the app is minimized, finished downloads and exports show a Windows toast.

## Post to TikTok

Post a downloaded video to one of your TikTok channels, each logged in inside its own profile of **Google Chrome Portable** (with its own VPN extension).

1. Open the **TikTok** tab and choose your `GoogleChromePortable` folder — its profiles are listed (from `Data\profile\Local State`).
2. For each profile, set the country its VPN must show (guessed from names like `TT-JP-Leather`), or *No VPN check*.
3. In **Downloaded**, press the ✈ button on a video: pick the profile, edit the caption, optionally **Schedule** (today up to 30 days ahead, 5-minute steps, at least 15 minutes from now) and post.

Grabber starts Chrome Portable with a local remote-debugging port, opens the profile, waits until the exit IP is in the chosen country, then fills TikTok Studio's upload page and presses Post (or stops before Post if *Stop before Post* is ticked). Chrome stays open for the next post. **Close every Chrome Portable window before the first post** — a Chrome started without the debugging port cannot be controlled. Posts are logged in `tiktok_posts.json`.

Automating uploads may be against TikTok's terms; you are responsible for the accounts you use.

## Research (niches → channels → TikTok check)

Finds promising YouTube Shorts channels in a hot niche that are **not on TikTok yet**. Needs a free YouTube Data API v3 key (Google Cloud Console → enable *YouTube Data API v3* → Credentials → Create API key): paste it in the box at the top of the **Research** tab — it is checked with one API call and kept in the app settings. Click the *API today* chip to change it later. (A `YOUTUBE_API_KEY=` line in a git-ignored `.env` file next to the app also works.)

1. Pick a market (US / UK / DE / JP / KR) and press **Research niches**: 10 niches from a built-in list of 40 are scored by the view velocity of their most-viewed Shorts of the last 14 days, the share of channels younger than 24 months and the number of 100K+ Shorts (~1,000 API units; scores are kept 24 h, so pressing again scores the next 10). Or type your own niche.
2. **Find channels**: Shorts channels from the niche, kept when created in the last 24 months, 5K–500K subscribers, ≥ 70 % Shorts and active in the last 30 days, ranked by views/day, recent views and upload pace.
3. Each candidate is checked on TikTok (in the Chrome Portable **Research** profile, no login needed): TikTok links on the YouTube About page and in descriptions, handle variants, a user search by name (avatar compared by perceptual hash), a video search by Short titles (copies / possible re-uploads), and each TikTok account found is opened (bio link back to YouTube, avatar, matching videos).
4. The top 5 channels with no TikTok account are shown, plus every suspect / re-uploader / look-alike account with its evidence and links to verify by hand.

**Check on TikTok** runs step 3 for a single YouTube channel link. If TikTok shows a captcha, solve it in the Chrome window; the run waits.

## Editions and license activation

`edition.py` picks the build: **owner** (branch `main`: every feature, never locked, plus a **Keys** tab that makes license keys) or **commercial** (branch `commercial`: no Research tab, no key generator, locked until activated). New features land on `main`; merge `main` into `commercial` to ship them.

The commercial app is locked until it is activated with a key made for that computer:

1. The buyer opens Grabber and sends you the **Machine ID** shown on the activation screen (with the payment).
2. You make the key in the **Keys** tab of the owner app (or `tools\keygen.bat` / `python tools/keygen.py issue <MACHINE-ID> --note "name"`): the key is printed, copied to the clipboard and listed in `%USERPROFILE%\.grabber-keygen\issued.csv`.
3. The buyer pastes the key → activated. The key only works on that machine (Ed25519-signed Machine ID + issue date), so sharing it does nothing. Lifetime by default; `--days N` makes a key that expires.

The signing key is `%USERPROFILE%\.grabber-keygen\private.key` — **outside the repo, back it up and never share it**. `python tools/keygen.py init` creates it once and prints the public key embedded in `license.py`. `tools/` is not bundled into the exe. A reinstall of Windows changes the Machine ID: issue a new key.

## Logs

The **Logs** tab records fetches, downloads, exports and every TikTok posting step (with error details) in `logs.jsonl`, filterable by source or errors only.

## Install (new machine)

1. Clone this repository.
2. Double-click **`setup.bat`**. It will:
   - install Python 3.12 via winget if Python 3.10–3.13 is missing
   - create `.venv` and install `requirements.txt`
   - install Chromium for Facebook / Douyin / Instagram
   - download a portable ffmpeg (LGPL) into `bin\`
   - install the Microsoft Edge WebView2 runtime if needed
   - create a **Grabber** shortcut on the Desktop
3. Open **Grabber** from the Desktop (or run `run.bat`).

Re-run `setup.bat` anytime to repair or update.

## User data

Stored under `%USERPROFILE%\.yt-shorts-downloader\` (not in this repo):

`settings.json`, `saved.json`, `downloaded.json`, `tiktok_posts.json`, `logs.jsonl`, Facebook / Douyin / Instagram login sessions, and the in-app yt-dlp engine update.

## Build a `.exe` (optional)

```bat
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python build.py
```

Output is `dist\Grabber\` (includes ffmpeg if `bin\` is present). The Desktop shortcut then points at the exe.

```bat
.venv\Scripts\python build.py --no-shortcut
```

## Layout

| Path | Role |
|---|---|
| `app.py` | Backend and pywebview window |
| `editor.py` | ffmpeg pipeline (crop, color, text/logo, music, presets, merge) |
| `web/` | UI (`editor.js` = Edit, `batch.js` = Batch/Merge) |
| `facebook.py` / `douyin.py` / `instagram.py` | Listing videos with Playwright |
| `tiktok.py` | Posting through Chrome Portable profiles (CDP) |
| `research.py` | Niche scoring, Shorts channel search and TikTok existence check |
| `license.py` / `ed25519.py` | Machine ID and license key check (pure-Python Ed25519) |
| `edition.py` | owner / commercial build switch |
| `keygen.py`, `tools/keygen.py` | Seller-only license key generator (Keys tab + command line) |
| `engine.py` | Update yt-dlp from PyPI from inside the app |
| `build.py` | Package the `.exe` with PyInstaller |
| `tools/make_icon.py` | Regenerate the icon (needs Pillow) |

## Disclaimer

Use this tool only with content you own or have permission to download. Respect YouTube, Facebook, Douyin, and Instagram terms of service and applicable copyright law. This project is provided as-is for personal use.
