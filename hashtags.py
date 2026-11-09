"""5 viral TikTok hashtags for a video title and the market of the TikTok account, from Google's Gemini API
(free key from Google AI Studio). Standard library only."""
import json
import re
import urllib.error
import urllib.request

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"
# (model, Google Search): a free key gets no Google Search (429) and no 2.5 models (404) any more, so the models
# answer from what they know; the next one is tried when one is busy (503) or out of free requests (429)
ATTEMPTS = [("gemini-3.6-flash", False), ("gemini-3.8-flash", False), ("gemini-3.5-flash-lite", False)]
MARKETS = {"JP": "Japan", "US": "the United States", "KR": "South Korea", "VN": "Vietnam", "GB": "the United Kingdom",
           "DE": "Germany", "FR": "France", "TW": "Taiwan", "TH": "Thailand", "ID": "Indonesia", "PH": "the Philippines",
           "MY": "Malaysia", "SG": "Singapore", "CA": "Canada", "AU": "Australia", "BR": "Brazil", "MX": "Mexico"}
DEFAULT_PROMPT = (
    "I will send you a video title. Research and send me 5 hashtags for a TikTok video on that topic. "
    "The hashtags must be widely used and viral, not too niche. The video is for the {market} market: use the "
    "hashtags TikTok users in {market} actually use, in their language.\n"
    "Answer with only the 5 hashtags on one line, separated by spaces.\n\n"
    "Video title: {title}")


class HashtagError(Exception):
    pass


def _ask(key, model, prompt, search):
    body = {"model": model, "input": prompt}
    if search:
        body["tools"] = [{"type": "google_search"}]
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "x-goog-api-key": key, "Content-Type": "application/json", "Api-Revision": API_REVISION})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _texts(obj):
    """Every "text" string in the answer, in order: the reply is the last one."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "text" and isinstance(v, str):
                yield v
            else:
                yield from _texts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _texts(v)


def parse(text, n=5):
    tags = []
    for t in re.findall(r"#[^\s#,、，。！？!?]+", text or ""):
        t = t.rstrip(".:;)]」』")
        if len(t) > 1 and t.lower() not in (x.lower() for x in tags):
            tags.append(t)
    return tags[:n]


def generate(title, country, key, template="", log=None):
    """5 hashtags for `title` on a TikTok account in `country` (JP, US…)."""
    if not key:
        raise HashtagError("Add your free Gemini API key in the TikTok tab first.")
    if not (title or "").strip():
        raise HashtagError("Write the title first.")
    market = MARKETS.get((country or "").upper(), (country or "").upper() or "the worldwide")
    template = template or DEFAULT_PROMPT
    prompt = template.replace("{market}", market).replace("{country}", (country or "").upper())
    prompt = prompt.replace("{title}", title.strip()) if "{title}" in template else f"{prompt}\n\n{title.strip()}"
    last, codes = None, []
    for model, search in ATTEMPTS:
        try:
            answer = _ask(key, model, prompt, search)
        except urllib.error.HTTPError as ex:
            detail = ex.read().decode("utf-8", "replace")[:300]
            if ex.code in (400, 401, 403) and re.search(r"API.?key|PERMISSION|UNAUTHENTICATED", detail, re.I):
                raise HashtagError("The Gemini API key was refused - check it in the TikTok tab.") from None
            last = f"{model}: HTTP {ex.code} {detail}"
            codes.append(ex.code)
            if log:
                log(f"Hashtags: {last}")
            continue
        except (OSError, ValueError) as ex:
            last = f"{model}: {type(ex).__name__}"
            continue
        tags = parse(list(_texts(answer))[-1] if list(_texts(answer)) else "")
        if tags:
            if log:
                log(f"Hashtags from {model}" + (" with Google Search" if search else "") + f": {' '.join(tags)}")
            return tags
        last = f"{model}: no hashtags in the answer"
    if codes and all(c == 429 for c in codes):
        raise HashtagError("Gemini's free limit is used up for now - try again later.")
    if codes and all(c in (429, 503) for c in codes):
        raise HashtagError("Gemini is busy right now - try again in a minute.")
    raise HashtagError(f"Gemini did not give hashtags ({last}).")


def append(caption, tags):
    """The caption with these tags at the end, replacing hashtags already at its end."""
    base = re.sub(r"(\s*#[^\s#]+)+\s*$", "", caption or "").rstrip()
    return f"{base} {' '.join(tags)}".strip()
