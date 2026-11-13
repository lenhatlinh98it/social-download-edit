"""Drive Google's creative web apps in the user's Chrome Portable profile (signed in to Google) for Story video:
Flow (pictures with Nano Banana, a character sheet as reference), AI Studio (Gemini TTS narration) and
Flow Music (a background track). Same connection as the TikTok posting: Chrome is started with a debugging port
and driven over CDP, at a person's pace, one account.

The web apps change: every step raises LabsError with what it was doing, and a screenshot is kept for a fix.
"""
import difflib
import os
import re
import shutil
import tempfile
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

import tiktok

FLOW_URL = "https://labs.google/fx/tools/flow"
TTS_URL = "https://aistudio.google.com/generate-speech"
MUSIC_URL = "https://www.flowmusic.app/"


class LabsError(Exception):
    pass


class Cancelled(Exception):
    pass


class Session:
    """One Chrome window of a profile, connected over CDP. Use as a context manager."""

    def __init__(self, root, profile_dir, shots_dir=None, log=lambda _m: None, should_cancel=lambda: False):
        self.root, self.profile_dir, self.shots_dir = root, profile_dir, shots_dir
        self.log, self.should_cancel = log, should_cancel
        self._pw = self.browser = self.page = None
        self._project_url = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        try:
            self.browser, self.page = tiktok._open_profile_page(self._pw, self.root, self.profile_dir)
        except Exception:
            self._pw.stop()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.page and not self.page.is_closed():
                self.page.close()
        except PlaywrightError:
            pass
        try:
            self.browser.close()  # only the CDP connection: Chrome stays open for the next run
        finally:
            self._pw.stop()

    # ---------- helpers ----------
    def check(self):
        if self.should_cancel():
            raise Cancelled()

    def snap(self, step):
        """A screenshot for a later fix ("" when it cannot be taken)."""
        if not (self.shots_dir and self.page):
            return ""
        try:
            os.makedirs(self.shots_dir, exist_ok=True)
            shot = os.path.join(self.shots_dir, f"{time.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^a-z]+', '-', step.lower())[:40]}.png")
            self.page.screenshot(path=shot)
            return shot
        except PlaywrightError:
            return ""

    def fail(self, step, ex=None):
        shot = self.snap(step)
        detail = f" ({type(ex).__name__}: {str(ex).splitlines()[0][:160]})" if ex else ""
        return LabsError(f"{step} failed{detail}" + (f" | Screenshot: {shot}" if shot else ""))

    def icon_button(self, icon, scope=None):
        """A button showing a Material icon (its ligature text does not change with the UI language)."""
        return (scope or self.page).locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(icon)}\s*$"))

    def _type_into(self, loc, text):
        """Fill a text field; some fields are custom elements that only take key presses."""
        try:
            loc.fill(text, timeout=8000)
            return
        except PlaywrightError:
            pass
        loc.click()
        inner = loc.locator("input, textarea, [contenteditable=true]")
        if inner.count():
            try:
                inner.first.fill(text, timeout=5000)
                return
            except PlaywrightError:
                pass
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        self.page.keyboard.insert_text(text)

    def _close_side_panel(self):
        """Close a settings panel on the right (its X at the top right), else press Escape."""
        width, height = self.page.evaluate("[innerWidth, innerHeight]")
        for b in self.icon_button("close").all():
            bb = b.bounding_box()
            if bb and bb["x"] > width * 0.55 and bb["y"] < height * 0.2 and b.is_visible():
                b.click()
                self.wait(600)
                return
        self.page.keyboard.press("Escape")
        self.wait(400)

    def wait(self, ms):
        end = time.time() + ms / 1000
        while time.time() < end:
            self.check()
            self.page.wait_for_timeout(min(500, max(1, int((end - time.time()) * 1000))))

    CONSENT = re.compile(r"privacy|quyền riêng tư|terms|điều khoản|consent|đồng ý|agree|quyền sử dụng|rights to use|"
                         r"chính sách|policy", re.I)

    def _consent_dialog(self):
        dlg = self.page.locator("[role=dialog]:visible, .cdk-overlay-pane:visible, mat-dialog-container:visible").last
        if dlg.count():
            try:
                if self.CONSENT.search(dlg.inner_text(timeout=2000) or ""):
                    return dlg
            except PlaywrightError:
                pass
        return None

    def wait_for_consent(self, timeout=300):
        """Google asks the user to accept something (terms, rights to an uploaded picture): that is the user's
        to click, never the app's. Bring the window up, say so, and wait for the dialog to go."""
        if not self._consent_dialog():
            return
        self.page.bring_to_front()
        self.log("Google asks you to confirm something in the Chrome window (terms / rights to the picture). "
                 "Read it and click its button there yourself - Story video waits for you.")
        end = time.time() + timeout
        while self._consent_dialog():
            self.check()
            if time.time() > end:
                raise LabsError("Google's confirmation dialog is still open in Chrome - answer it, then render again.")
            self.wait(1000)
        self.log("Thanks - going on")

    def dismiss_intros(self):
        """Close "what's new" dialogs; terms / consent dialogs wait for the user."""
        for _ in range(3):
            dlg = self.page.locator("[role=dialog]:visible, .cdk-overlay-pane:visible").last
            if not dlg.count():
                return
            if self._consent_dialog():
                self.wait_for_consent()
                continue
            btn =dlg.get_by_role("button", name=re.compile(r"^(Bắt đầu|Get started|Start|Đóng|Close|OK|Got it|Đã hiểu)$", re.I))
            if btn.count():
                btn.first.click()
            else:
                self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(600)

    def download(self, click, dst, timeout=120000):
        """Run `click` and save the file it downloads to `dst`."""
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        problem = ""
        for attempt in range(2):
            # an empty first file can come before the real one (Flow prepares 2K pictures after the click):
            # then wait for the next download instead of clicking again
            with self.page.expect_download(timeout=timeout if attempt == 0 else 90000) as info:
                if attempt == 0:
                    click()
            dl = info.value
            problem = dl.failure() or ""  # waits for the download to finish
            if not problem:
                dl.save_as(dst)
                if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                    return dst
                problem = "empty file"
            if os.path.isfile(dst):
                os.remove(dst)
        raise LabsError(f"The download of {os.path.basename(dst)} failed ({problem}).")

    # ---------- Flow: pictures ----------
    def flow_open(self, project_url=None):
        """Open a Flow project (a new one when there is none yet); returns its URL."""
        self.log("Opening Google Flow…")
        try:
            # in a narrow window Flow shows a bare grid of pictures; wide, each result lists its prompt and its
            # buttons (download, reuse prompt), which is what the steps below rely on
            self.page.set_viewport_size({"width": 1920, "height": 1080})
            self.page.goto(project_url or FLOW_URL, wait_until="domcontentloaded", timeout=60000)
            if "accounts.google.com" in self.page.url:
                raise LabsError("This Chrome profile is not signed in to Google. Sign in to labs.google/flow in it once.")
            if not project_url or "/project/" not in self.page.url:
                new = self.page.get_by_role("button", name=re.compile(r"Dự án mới|New project", re.I))
                new.first.wait_for(timeout=60000)
                new.first.click()
                self.page.wait_for_url(re.compile(r"/project/"), timeout=60000)
            self.page.locator("div[role=textbox].ProseMirror").first.wait_for(timeout=60000)
            self.wait(1500)
            self.dismiss_intros()
            self._direct_mode()
            self._project_url = self.page.url.split("?")[0]
            return self._project_url
        except LabsError:
            raise
        except (PlaywrightError, PlaywrightTimeout) as ex:
            raise self.fail("Opening Flow", ex) from None

    def _settings_button(self):
        return self.page.locator("button.settings-trigger-button, button[class*=settings-trigger]").first

    def _direct_mode(self):
        """Flow can open a project with its Agent chat on the right instead of the prompt bar with the model
        settings (it did for a new account): close the chat and turn Agent mode off until the settings show."""
        for _ in range(4):
            if self._settings_button().is_visible():
                return
            chip = self.page.locator("button.agent-mode-chip")
            if chip.count() and chip.first.is_visible() and chip.first.get_attribute("aria-pressed") == "true":
                chip.first.click()
                self.wait(1000)
                continue
            width, height = self.page.evaluate("[innerWidth, innerHeight]")
            closed = False
            for b in self.icon_button("close").all():  # the chat panel's own close button, top right
                bb = b.bounding_box()
                if bb and bb["x"] > width * 0.55 and bb["y"] < height * 0.25 and b.is_visible():
                    b.click()
                    closed = True
                    break
            if not closed:
                self.page.keyboard.press("Escape")
            self.wait(1200)
        if not self._settings_button().is_visible():
            raise self.fail("Switching Flow from Agent mode to the picture prompt bar")

    def _prompt_bar(self):
        """The prompt bar at the bottom (the one with the model settings), not the Agent chat's box."""
        boxes = self.page.locator("div[role=textbox].ProseMirror")
        bar = self._settings_button().bounding_box() if self._settings_button().count() else None
        if bar:
            for b in boxes.all():
                bb = b.bounding_box()
                if bb and abs(bb["y"] - bar["y"]) < 160 and bb["x"] < bar["x"]:
                    return b
        return boxes.last

    def flow_settings(self, ratio="9:16"):
        """Pictures, `ratio`, one per prompt."""
        try:
            self._settings_button().click()
            pane = self.page.locator(".cdk-overlay-pane:visible").last
            pane.locator("button[role=radio]").filter(has_text=re.compile(r"^(image\s*)?(Hình ảnh|Image)", re.I)).first.click()
            pane.locator("button[role=radio]").filter(has_text=re.compile(rf"{re.escape(ratio)}$")).first.click()
            pane.locator("button[role=radio]").filter(has_text=re.compile(r"^x1$")).first.click()
            self.page.keyboard.press("Escape")
            self.wait(400)
        except (PlaywrightError, PlaywrightTimeout) as ex:
            raise self.fail("Choosing the picture settings in Flow", ex) from None

    @staticmethod
    def _ref_name(path):
        """The file name Flow will show for this reference: its folder (the character) and its own name, so two
        characters' sheets ("sheet.jpg" each) never get mixed up in the project's uploads."""
        folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
        return re.sub(r"[^\w.-]+", "_", f"{folder}_{os.path.basename(path)}")

    _UPLOAD_BTN = re.compile(r"Tải nội dung.*lên|Upload", re.I)

    def _picker(self):
        """Open the reference picker and return it - or None when it did not open (then nothing else on the
        page is clicked: the project's own "Uploads" link and picture tiles look alike and open the big viewer)."""
        p = self.page
        self._add_button().click()
        pane = p.locator(".cdk-overlay-pane, [role=dialog]").filter(has=p.get_by_role("button", name=self._UPLOAD_BTN))
        for _ in range(10):
            self.wait(400)
            if pane.count() and pane.last.is_visible():
                return pane.last
        p.keyboard.press("Escape")
        return None

    def _pick_uploaded(self, name):
        """Attach a picture uploaded earlier to this project (picker > Uploads > the file): no upload, no wait."""
        pane = self._picker()
        if pane is None:
            return False
        # search the file by name over every kind ("Uploads" alone lags behind a fresh upload)
        every = pane.get_by_role("tab").filter(has_text=re.compile(r"^\s*dashboard", re.I))
        if every.count():
            every.first.click()
        search = pane.locator("input[placeholder*='Tìm' i], input[placeholder*='Search' i]")
        if search.count():
            search.first.fill(name)
        item = pane.get_by_role("option").filter(has_text=name)  # the file name, wherever it sits in the item
        for _ in range(8):  # a picture uploaded a moment ago can take a few seconds to be listed
            self.wait(700)
            if item.count():
                break
        if not item.count():
            self.page.keyboard.press("Escape")
            self.wait(400)
            return False
        item.first.click()
        # picking it may attach it right away and close the picker; else it waits for "Add to prompt"
        add = pane.get_by_role("button", name=re.compile(r"Thêm vào câu lệnh|Add to prompt", re.I))
        for _ in range(10):
            self.wait(400)
            if self.page.evaluate(self._BAR_REFS_JS):
                return True
            if add.count() and add.first.is_visible() and add.first.is_enabled():
                add.first.click()
                self.wait(600)
                return True
        return bool(self.page.evaluate(self._BAR_REFS_JS))

    _BAR_REFS_JS = r"""() =>
        // pictures attached to the prompt bar (they stay there after a prompt is sent)
        // (the chip elements themselves have no box: measure the picture inside)
        [...document.querySelectorAll('.ingredient-bar-container img, flow-ingredient-chip img')]
            .filter(c => c.getBoundingClientRect().width > 0).length"""

    def _back_to_project(self):
        """Leave a picture's big view (/edit/) for the project grid - with Flow's own back arrow when it is
        there (no reload: the reference attached to the prompt bar stays), else by opening the project again."""
        back = self.icon_button("arrow_back")
        if "/edit/" in self.page.url and back.count() and back.first.is_visible():
            back.first.click()
            try:
                self.page.wait_for_url(lambda u: "/edit/" not in u, timeout=15000)
                self._prompt_bar().wait_for(timeout=30000)
                self.wait(800)
                return
            except (PlaywrightError, PlaywrightTimeout):
                pass
        if "/edit/" in self.page.url and self._project_url:
            self.log("Flow opened a picture - going back to the project")
            self.page.goto(self._project_url, wait_until="domcontentloaded", timeout=60000)
            self._prompt_bar().wait_for(timeout=60000)
            self.wait(1000)
        elif "/edit/" in self.page.url:
            self.page.go_back(wait_until="domcontentloaded")
            self.wait(1000)

    def flow_add_reference(self, path):
        """Attach a picture to the prompt as a reference (keeps the character the same). It is uploaded to the
        project once; after that it is only picked again from the project's uploads."""
        name = self._ref_name(path)
        try:
            self._back_to_project()
            if self.page.evaluate(self._BAR_REFS_JS):
                return  # still attached from the last prompt
            if self._pick_uploaded(name):
                return
            # first time in this project: upload a copy named after the character
            ref = os.path.join(tempfile.gettempdir(), "grabber-story-refs", name)
            os.makedirs(os.path.dirname(ref), exist_ok=True)
            shutil.copyfile(path, ref)
            pane = self._picker()
            if pane is None:
                raise LabsError("Flow's picture picker did not open (the + button next to the prompt).")
            up = pane.get_by_role("button", name=self._UPLOAD_BTN).first
            up.wait_for(timeout=20000)
            with self.page.expect_file_chooser(timeout=20000) as fc:
                up.click()
            fc.value.set_files(ref)
            self.log(f"Flow: uploaded the reference {name} (once for this project)")
            self.wait(1500)
            self.wait_for_consent()  # Flow asks once to confirm the rights to an uploaded picture
            add = self.page.get_by_role("button", name=re.compile(r"Thêm vào câu lệnh|Add to prompt", re.I)).first
            add.wait_for(timeout=120000)
            self.page.wait_for_function("b => !b.disabled", arg=add.element_handle(), timeout=120000)  # upload done
            add.click()
            self.wait(800)
        except (PlaywrightError, PlaywrightTimeout) as ex:
            raise self.fail("Adding the reference picture in Flow", ex) from None

    def _add_button(self):
        named = self.page.locator("button[aria-label='Thêm thành phần vào ô nhập câu lệnh'], "
                                  "button[aria-label*='ingredient' i][aria-label*='prompt' i]")
        if named.count() and named.first.is_visible():
            return named.first
        box = self._prompt_bar().bounding_box()
        for b in self.icon_button("add").all():
            bb = b.bounding_box()
            if bb and box and bb["y"] > box["y"] - 10 and abs(bb["x"] - box["x"]) < 120:
                return b
        return self.icon_button("add").last

    def flow_submit(self, prompt):
        """Type a prompt and start it (the references already attached stay for it)."""
        try:
            self._back_to_project()
            box = self._prompt_bar()
            box.click()
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
            box.type(prompt, delay=2)
            self.icon_button("arrow_forward").last.click()
            self.wait(2500)
        except (PlaywrightError, PlaywrightTimeout) as ex:
            raise self.fail("Sending a prompt to Flow", ex) from None

    @staticmethod
    def _norm(text):
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    _PROGRESS_JS = r"""() => {
        // progress badges ("37%") on pictures still being drawn, outside the prompt bar
        let n = 0;
        for (const e of document.querySelectorAll('div, span')) {
            if (e.children.length) continue;
            if (!/^\s*\d{1,3}\s*%\s*$/.test(e.textContent || '')) continue;
            const r = e.getBoundingClientRect();
            if (r.width && r.y < innerHeight - 170) n++;
        }
        return n;
    }"""

    _NEWEST_TILE_JS = r"""() => {
        // the top-left picture of the project grid (newest first), in either Flow layout
        const tiles = [...document.querySelectorAll('img')].filter(i => {
            const r = i.getBoundingClientRect();
            return r.width > 60 && r.height > 60 && r.x > 200 && r.x < innerWidth * 0.7 && r.y > 50 && r.y < innerHeight - 170
                && !i.closest('flow-batch-info');
        }).sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y || a.getBoundingClientRect().x - b.getBoundingClientRect().x);
        if (!tiles.length) return null;
        const r = tiles[0].getBoundingClientRect();
        return [r.x + r.width / 2, r.y + r.height / 2];
    }"""

    _NEXT_THUMB_JS = r"""() => {
        const b = [...document.querySelectorAll('flow-navigation-rail-thumbnail button.thumbnail-button')];
        const i = b.findIndex(x => x.classList.contains('active'));
        if (i < 0 || i + 1 >= b.length) return false;
        b[i + 1].scrollIntoView({block: 'nearest', inline: 'center'});
        b[i + 1].click();
        return true;
    }"""

    def flow_wait_idle(self, timeout=600):
        """Wait until no picture of the project is still being drawn."""
        end = time.time() + timeout
        self.wait(8000)
        calm = 0
        while time.time() < end:
            self.check()
            self._back_to_project()  # a finished picture may have been opened big on its own
            body = self.page.inner_text("body")
            if re.search(r"hoạt động bất thường|unusual activity", body, re.I):
                raise LabsError("Google Flow refused the pictures: it reports “unusual activity” on this account "
                                "(it noticed the pictures are being made automatically). Story video does not try to get "
                                "around that - make the pictures in Flow yourself, or wait before trying again.")
            if re.search(r"hết tín dụng|out of credits|used all your credits|hạn mức", body, re.I):
                raise LabsError("Google Flow says this account has used its free generations for now. Wait until it "
                                "refreshes (Flow shows when), or use another Google profile, then render again - "
                                "the finished pictures are kept.")
            calm = calm + 1 if not self.page.evaluate(self._PROGRESS_JS) else 0
            if calm >= 2:
                return
            self.wait(3000)
        self.log("Flow is still drawing after 10 minutes - taking what is finished")

    def _download_current(self, dst, hires):
        """On a picture's page: save it (2K, or the 1K original when 2K does not come)."""
        sizes = [r"^2K", r"^1K"] if hires else [r"^1K"]
        last = None
        for size in sizes:
            try:
                self.icon_button("download").first.click()
                item = self.page.get_by_role("menuitem", name=re.compile(size)).first
                item.wait_for(timeout=10000)
                return self.download(lambda: item.click(), dst, timeout=180000)
            except (PlaywrightError, PlaywrightTimeout, LabsError) as ex:
                last = ex
                self.page.keyboard.press("Escape")
                self.log(f"Flow: {size[1:]} download did not come, trying the next size")
        raise self.fail("Downloading a picture from Flow", last)

    def flow_collect(self, jobs, hires=True, on_picture=lambda *_: None):
        """Open the newest picture, then walk the strip of thumbnails (newest first): each picture page shows its
        prompt, which tells which job it belongs to. Returns the prompts that were saved."""
        want = {self._norm(p): (p, dst) for p, dst in jobs}
        got = set()
        try:
            spot = self.page.evaluate(self._NEWEST_TILE_JS)
            if not spot:
                return got
            self.page.mouse.click(*spot)
            self.page.wait_for_url(re.compile(r"/edit/"), timeout=30000)
            self.wait(1500)
            limit = 3 * len(jobs) + 4  # the new pictures, with the uploaded references between them
            for idx in range(limit):
                self.check()
                if len(got) == len(want):
                    break
                if idx:
                    # the strip is a virtual list (it drops thumbnails as it scrolls): step from the active one
                    before = self.page.url
                    if not self.page.evaluate(self._NEXT_THUMB_JS):
                        break
                    self.page.wait_for_function("u => location.href !== u", arg=before, timeout=15000)
                    self.wait(1200)
                body = self._norm(self.page.inner_text("body"))
                key = next((k for k in want if k not in got and k in body), None)
                if key is None:  # Flow may show the prompt a little changed: most of it in one piece is enough
                    for k in want:
                        if k in got:
                            continue
                        m = difflib.SequenceMatcher(None, k, body, autojunk=False).find_longest_match(0, len(k), 0, len(body))
                        if m.size >= 0.6 * len(k):
                            key = k
                            break
                if key is None:
                    continue
                prompt, dst = want[key]
                self._download_current(dst, hires)
                got.add(key)
                on_picture(prompt, dst)
                self.log(f"Flow: saved {os.path.basename(dst)}")
        except LabsError:
            raise
        except (PlaywrightError, PlaywrightTimeout) as ex:
            raise self.fail("Collecting the pictures in Flow", ex) from None
        return {want[k][0] for k in got}

    def flow_pictures(self, jobs, refs, project_url=None, ratio="9:16", hires=True, on_picture=lambda *_: None, batch=6):
        """`jobs`: [(prompt, dst)]. Sends prompts in batches (the reference pictures are uploaded once, then kept
        attached or picked again from the project's uploads), waits, saves the pictures. Returns the project URL and the
        jobs that failed (the caller may try them once more)."""
        url = self.flow_open(project_url)
        self.flow_settings(ratio)
        failed = []
        for k in range(0, len(jobs), batch):
            part = jobs[k:k + batch]
            for i, (prompt, _) in enumerate(part):
                self.check()
                for r in refs:
                    self.flow_add_reference(r)
                self.flow_submit(prompt)
                self.log(f"Flow: picture {k + i + 1}/{len(jobs)} started")
            self.flow_wait_idle()
            saved = self.flow_collect(part, hires, on_picture)
            failed += [j for j in part if j[0] not in saved]
            self._back_to_project()  # Flow's back arrow: no reload, the reference stays attached
            if "/project/" not in self.page.url or "/edit/" in self.page.url:
                self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                self._prompt_bar().wait_for(timeout=60000)
                self.wait(1500)
            self._direct_mode()
        return url, failed

    # ---------- AI Studio: narration ----------
    def tts(self, text, dst, voice="Knox", style=""):
        """Narrate `text` with a Gemini TTS voice in AI Studio and save the WAV to `dst`."""
        self.log(f"AI Studio: narrating {len(text.split())} words with {voice}…")
        p = self.page
        try:
            p.goto(TTS_URL, wait_until="domcontentloaded", timeout=60000)
            if "accounts.google.com" in p.url:
                raise LabsError("This Chrome profile is not signed in to Google. Sign in to aistudio.google.com in it once.")
            # AI Studio shows two versions of this page (accounts differ): find things by what they say
            box = p.get_by_role("textbox", name=re.compile(r"Speech block text", re.I)).or_(
                p.locator("textarea[placeholder*='for tags' i], [contenteditable=true][data-placeholder*='for tags' i]"))
            if not box.count():
                p.get_by_role("button", name=re.compile(r"Create new dialog", re.I)).first.click()
            box.first.wait_for(timeout=60000)
            self.dismiss_intros()
            # voice (and, on the newer panel, the "Audio Profile" persona)
            p.get_by_role("button", name=re.compile(r"^Speaker 1", re.I)).first.click()
            # the placeholder also sits on a custom wrapper (ms-input-field): take the real input inside
            search = p.locator("input[placeholder*='voices' i], textarea[placeholder*='voices' i]").or_(
                p.locator("[placeholder*='voices' i]")).first
            search.wait_for(timeout=20000)
            self._type_into(search, voice)
            self.wait(1500)
            pick = (p.get_by_role("button", name=re.compile(rf"^{re.escape(voice)}\b", re.I))
                    .or_(p.get_by_role("option", name=re.compile(rf"^{re.escape(voice)}\b", re.I)))
                    .or_(p.get_by_text(voice, exact=True)))
            visible = [c for c in pick.all() if c.is_visible()]
            if visible:
                visible[0].click()
                self.wait(600)
            else:
                # this account's voice list differs: say which voices it has, keep its default voice and go on
                self._type_into(search, "")
                self.wait(1500)
                shot = self.snap("ai-studio-voices")
                names = self.page.evaluate(r"""() => {
                    const skip = /^(Voice|Voices|Style|Pace|Accent|Audio|Profile|Speaker|Settings|Director|Search|Close|Male|Female|Masculine|Feminine|Neutral|Language|Gender|Pitch|Run|Create|Delete|Save|Cancel)$/i;
                    const seen = new Set();
                    for (const e of document.querySelectorAll('button, [role=option], li, mat-list-item')) {
                        const r = e.getBoundingClientRect();
                        if (!r.width || r.x < innerWidth * 0.5) continue;
                        const first = (e.innerText || '').trim().split('\n')[0].trim();
                        if (/^[A-Z][a-z]{1,11}$/.test(first) && !skip.test(first)) seen.add(first);
                    }
                    return [...seen].slice(0, 40);
                }""")
                self.log(f"AI Studio has no voice “{voice}” for this account - reading with its default voice. "
                         f"Voices it lists: {', '.join(names) or 'none found'}" + (f" | Screenshot: {shot}" if shot else ""))
            persona = p.locator("input[placeholder*='voice persona' i], textarea[placeholder*='voice persona' i]").or_(
                p.locator("[placeholder*='voice persona' i]"))
            if style and persona.count() and persona.first.is_visible():
                self._type_into(persona.first, style)
                style = ""
            self._close_side_panel()
            # style on the older page: a "Style" menu next to the speaker
            if style:
                btn = p.get_by_role("button", name=re.compile(r"^Style\b", re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    st = p.locator("[placeholder*='voice style' i]").first
                    st.wait_for(timeout=10000)
                    self._type_into(st, style)
                    p.keyboard.press("Escape")
            self._type_into(box.first, text)
            p.get_by_role("button", name=re.compile(r"^Run\b", re.I)).last.click()
            dl = self.icon_button("download").or_(p.get_by_role("button", name=re.compile(r"download", re.I))).last
            dl.wait_for(timeout=300000)
            self.wait(1500)
            pause = self.icon_button("pause")
            if pause.count():
                pause.first.click()  # it starts playing; keep the computer quiet
            return self.download(lambda: dl.click(), dst)
        except LabsError:
            raise
        except (PlaywrightError, PlaywrightTimeout) as ex:
            raise self.fail("Narrating in AI Studio", ex) from None

    # ---------- Flow Music: background track ----------
    def music(self, description, dst):
        """Make an instrumental track in Flow Music and save it as WAV to `dst`."""
        self.log("Flow Music: making the background track…")
        p = self.page
        try:
            p.goto(MUSIC_URL, wait_until="domcontentloaded", timeout=60000)
            if p.get_by_role("button", name=re.compile(r"^Log in$", re.I)).count():
                raise LabsError("This Chrome profile is not signed in to Flow Music. Open flowmusic.app in it and "
                                "sign in once (and accept its privacy notice).")
            self.wait(2500)
            if p.get_by_role("button", name=re.compile(r"^Log in$", re.I)).count():  # shown once the page settles
                raise LabsError("This Chrome profile is not signed in to Flow Music. Open flowmusic.app in it and "
                                "sign in once (and accept its privacy notice).")
            self.wait_for_consent()
            box = p.locator("textarea").or_(p.get_by_role("textbox")).first
            box.wait_for(timeout=60000)
            self.dismiss_intros()
            old_url = p.url
            self._type_into(box, description)
            send = p.get_by_role("button", name=re.compile(r"^(Send|Submit|Create|Generate)", re.I))
            if send.count() and send.last.is_visible() and send.last.is_enabled():
                send.last.click()
            else:
                p.keyboard.press("Enter")
            self.log("Flow Music: request sent, waiting for the track (1-3 minutes)…")
            # the request opens a new session; the track is ready when the session lists it ("Open details for
            # <title>") and nothing says "Generating" any more. (The home page and the player bar already show
            # older tracks, so counting buttons does not work.)
            title = self._music_wait(old_url)
            pause = p.get_by_role("button", name=re.compile(r"^Pause", re.I))
            if pause.count() and pause.first.is_visible():
                pause.first.click()  # it starts playing; keep the computer quiet
            return self._music_download(title, dst)
        except LabsError:
            raise
        except (PlaywrightError, PlaywrightTimeout) as ex:
            raise self.fail("Making music in Flow Music", ex) from None

    def _music_wait(self, old_url, timeout=360):
        p = self.page
        start = time.time()
        logged = 0
        while True:
            self.check()
            waited = time.time() - start
            if waited > timeout:
                raise self.fail(f"Waiting for Flow Music's track ({timeout // 60} minutes)")
            if "/session/" in p.url and p.url != old_url:
                details = p.get_by_role("button", name=re.compile(r"^Open details for ", re.I))
                busy = re.search(r"Generating|Đang tạo", p.inner_text("body"), re.I)
                if details.count() and not busy:
                    self.wait(2000)
                    return re.sub(r"^Open details for ", "", details.first.get_attribute("aria-label") or "", flags=re.I)
            elif waited > 30 and logged < 1:
                logged = 1
                self.log("Flow Music: no new session yet - the request may not have been sent")
            if waited > 5 and int(waited) % 30 < 5:
                self.log(f"Flow Music: still making the track… ({int(waited)} s)")
            self.wait(5000)

    def _music_download(self, title, dst):
        """Open the menu of the track `title` and save it as WAV."""
        p = self.page
        try:
            more = p.get_by_role("button", name=f"More options for {title}", exact=True)
            (more.first if more.count() else p.get_by_role("button", name=re.compile(r"^More options for ", re.I)).first).click()
            p.get_by_text(re.compile(r"^Download$", re.I)).first.hover()
            wav = p.get_by_text(re.compile(r"^WAV$", re.I)).first
            wav.wait_for(timeout=10000)
            return self.download(lambda: wav.click(), dst, timeout=300000)
        except LabsError:
            raise
        except (PlaywrightError, PlaywrightTimeout) as ex:
            raise self.fail("Making music in Flow Music", ex) from None
