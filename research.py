"""Niche & channel research for TikTok re-posting.

1. Hot niches: for each candidate niche, the market's most-viewed Shorts of the last 14 days (YouTube Data API)
   are scored by view velocity, how many of their channels are new, and how many break 100K views.
2. Promising channels: found by the niche's keywords, its most used hashtags and a channel-name search; kept
   when created in the last 36 months, 2K-1M subscribers, >= 60 % Shorts and active in the last 45 days, then
   ranked by growth (views per day, recent views, upload pace).
3. TikTok check: links from YouTube (About page + descriptions), handle variants, user search by name, video
   search by the titles of the channel's most viewed and latest Shorts (re-uploads keep the title but trim the
   clip and change the cover, so borderline hits are confirmed by comparing video frames); every candidate is
   opened (bio link back to YouTube, avatar hash, matching videos) and the channel gets a verdict:
   exists / likely / suspect / reup / none. Runs in the Chrome Portable "Research" profile.
"""
import difflib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import tiktok
from playwright.sync_api import Error as PlaywrightError

API = "https://www.googleapis.com/youtube/v3/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
RESEARCH_PROFILE = "Research"
DAILY_QUOTA = 10000
MAX_AGE_DAYS = 730
MIN_SUBS, MAX_SUBS = 5000, 500000
MIN_SHORTS_SHARE = 0.7
MAX_SHORT_SECONDS = 180
NICHE_WINDOW_DAYS = 14
CHANNEL_WINDOW_DAYS = 30
DISCOVERY_DAYS = 90  # relevance searches look further back: they are for finding channels, not hot videos
CHANNEL_MAX_AGE_DAYS = 1095  # promising channels: created in the last 36 months
CHANNEL_MIN_SUBS, CHANNEL_MAX_SUBS = 2000, 1_000_000
CHANNEL_MIN_SHORTS_SHARE = 0.6
ACTIVE_DAYS = 45
# long-video channels (for Smart cut): mostly long uploads, any age, a slower upload pace is fine
LONG_MIN_SHARE = 0.6  # of the channel's uploads that are long videos (not Shorts)
LONG_ACTIVE_DAYS = 60
LONG_RECENT = 10  # latest long videos read per channel (5 are shown)
HASHTAG_QUERIES = 3  # extra searches with the niche's most used hashtags
# hashtags too generic to define a niche
GENERIC_TAGS = set("shorts short ytshorts youtubeshorts shortsfeed shortvideo shortsvideo viral viralshorts viralvideo "
                   "fyp foryou foryoupage trending trend explore reels tiktok satisfying asmr funny new video "
                   "subscribe like usa us uk".split())
NICHE_CACHE_HOURS = 24
AVATAR_MATCH = 6  # dHash bits (of 64): the same picture re-encoded is 0-6, unrelated pictures ~25-35
AVATAR_SIMILAR = 10
COVER_MATCH = 12
FRAME_MATCH = 8  # dHash bits between two frames of the same footage (re-encoded, trimmed: 0-8; other footage ~20)
MIN_FRAME_OVERLAP = 0.5  # of a TikTok clip's frames found in the Short = a copy
FRAME_CHECKS = 6  # title hits per channel confirmed by downloading both videos
PROFILE_FRAME_CHECKS = 3  # same-name / same-title accounts per channel whose latest videos are compared by frames
TITLE_SEARCHES = 6  # Shorts whose titles are searched on TikTok (most viewed first, then the latest)

MARKETS = {
    "US": {"region": "US", "lang": "en"},
    "UK": {"region": "GB", "lang": "en"},
    "DE": {"region": "DE", "lang": "de"},
    "JP": {"region": "JP", "lang": "ja"},
    "KR": {"region": "KR", "lang": "ko"},
}

# id, display name, keywords per language (first one is the main query)
NICHES = [
    ("recipes", "Quick recipes", {"en": ["easy recipe", "quick dinner"], "de": ["rezept", "schnelles rezept"], "ja": ["レシピ", "簡単レシピ"], "ko": ["레시피", "간단 요리"]}),
    ("street_food", "Street food", {"en": ["street food"], "de": ["streetfood"], "ja": ["屋台グルメ", "ストリートフード"], "ko": ["길거리 음식"]}),
    ("baking", "Baking & desserts", {"en": ["baking", "dessert recipe"], "de": ["backen", "kuchen rezept"], "ja": ["お菓子作り", "スイーツ"], "ko": ["베이킹", "디저트"]}),
    ("food_tour", "Restaurant & food tour", {"en": ["food review", "restaurant"], "de": ["restaurant test", "essen gehen"], "ja": ["グルメ", "飲食店"], "ko": ["맛집", "먹방"]}),
    ("diet_meals", "Diet & healthy meals", {"en": ["healthy meal", "weight loss recipe"], "de": ["abnehmen rezept", "gesund essen"], "ja": ["ダイエットレシピ", "ヘルシー"], "ko": ["다이어트 식단", "건강식"]}),
    ("car_mods", "Car modification", {"en": ["car modification", "car tuning"], "de": ["auto tuning", "umbau auto"], "ja": ["車 カスタム", "改造車"], "ko": ["자동차 튜닝", "차 개조"]}),
    ("car_detailing", "Car detailing", {"en": ["car detailing", "car cleaning"], "de": ["auto aufbereitung", "autopflege"], "ja": ["洗車", "車 磨き"], "ko": ["디테일링", "세차"]}),
    ("supercars", "Supercars", {"en": ["supercar"], "de": ["supersportwagen"], "ja": ["スーパーカー"], "ko": ["슈퍼카"]}),
    ("leather", "Leather crafting", {"en": ["leather craft", "leather wallet"], "de": ["leder handarbeit", "lederarbeit"], "ja": ["レザークラフト", "革小物"], "ko": ["가죽공예"]}),
    ("woodworking", "Woodworking", {"en": ["woodworking"], "de": ["holzarbeiten", "tischler"], "ja": ["木工", "DIY 木工"], "ko": ["목공"]}),
    ("printing_3d", "3D printing", {"en": ["3d printing", "3d printed"], "de": ["3d druck"], "ja": ["3Dプリンター"], "ko": ["3D프린터"]}),
    ("home_hacks", "DIY home hacks", {"en": ["life hacks", "diy home"], "de": ["life hacks", "haushaltstipps"], "ja": ["ライフハック", "便利グッズ"], "ko": ["생활 꿀팁", "살림 꿀팁"]}),
    ("restoration", "Restoration", {"en": ["restoration", "restoring old"], "de": ["restaurierung"], "ja": ["レストア", "修復"], "ko": ["복원", "리스토어"]}),
    ("satisfying", "Satisfying & ASMR", {"en": ["satisfying", "asmr"], "de": ["satisfying", "asmr deutsch"], "ja": ["ASMR", "気持ちいい動画"], "ko": ["ASMR", "힐링 영상"]}),
    ("cleaning", "Deep cleaning", {"en": ["deep cleaning", "pressure washing"], "de": ["reinigung", "hochdruckreiniger"], "ja": ["掃除", "大掃除"], "ko": ["청소", "청소 루틴"]}),
    ("pets", "Cute pets", {"en": ["cute cat", "funny dog"], "de": ["katze lustig", "hund lustig"], "ja": ["猫", "犬 かわいい"], "ko": ["고양이", "강아지"]}),
    ("wildlife", "Wildlife & animal facts", {"en": ["wild animals", "animal facts"], "de": ["tiere wildnis", "tierfakten"], "ja": ["野生動物", "動物の雑学"], "ko": ["야생동물", "동물 상식"]}),
    ("history", "History facts", {"en": ["history facts", "history"], "de": ["geschichte", "geschichte fakten"], "ja": ["歴史", "歴史雑学"], "ko": ["역사", "역사 이야기"]}),
    ("space", "Space & astronomy", {"en": ["space facts", "universe"], "de": ["weltall", "universum"], "ja": ["宇宙", "宇宙の謎"], "ko": ["우주", "우주 미스터리"]}),
    ("science", "Science experiments", {"en": ["science experiment"], "de": ["experiment", "wissenschaft"], "ja": ["実験", "科学実験"], "ko": ["과학 실험"]}),
    ("gadgets", "Tech gadgets", {"en": ["tech gadgets", "gadget"], "de": ["technik gadgets", "gadgets"], "ja": ["ガジェット", "便利ガジェット"], "ko": ["전자기기", "가젯"]}),
    ("phone_tips", "Smartphone tips", {"en": ["iphone tips", "android tips"], "de": ["iphone tipps", "handy tricks"], "ja": ["iPhone 便利機能", "スマホ 裏技"], "ko": ["아이폰 꿀팁", "스마트폰 꿀팁"]}),
    ("gaming", "Gaming clips", {"en": ["gaming", "minecraft"], "de": ["gaming deutsch", "minecraft"], "ja": ["ゲーム実況", "マイクラ"], "ko": ["게임", "마인크래프트"]}),
    ("fitness", "Fitness workouts", {"en": ["workout", "home workout"], "de": ["workout", "fitness übungen"], "ja": ["筋トレ", "宅トレ"], "ko": ["홈트", "운동"]}),
    ("skincare", "Skincare", {"en": ["skincare", "skin care routine"], "de": ["hautpflege", "skincare"], "ja": ["スキンケア", "美容"], "ko": ["스킨케어", "피부관리"]}),
    ("makeup", "Makeup", {"en": ["makeup tutorial", "makeup"], "de": ["make up tutorial", "schminken"], "ja": ["メイク", "メイク動画"], "ko": ["메이크업", "화장법"]}),
    ("fashion", "Fashion outfits", {"en": ["outfit ideas", "fashion"], "de": ["outfit ideen", "mode"], "ja": ["コーデ", "ファッション"], "ko": ["코디", "패션"]}),
    ("nails", "Nail art", {"en": ["nail art"], "de": ["nageldesign"], "ja": ["ネイル", "セルフネイル"], "ko": ["네일아트", "셀프네일"]}),
    ("hair", "Hair styling", {"en": ["hairstyle", "hair tutorial"], "de": ["frisuren", "haare stylen"], "ja": ["ヘアアレンジ", "髪型"], "ko": ["헤어스타일", "머리 묶는법"]}),
    ("travel", "Travel spots", {"en": ["travel", "places to visit"], "de": ["reise", "reiseziele"], "ja": ["旅行", "観光スポット"], "ko": ["여행", "여행지 추천"]}),
    ("house_tours", "House tours & real estate", {"en": ["house tour", "real estate"], "de": ["haus tour", "immobilien"], "ja": ["ルームツアー", "不動産"], "ko": ["랜선집들이", "부동산"]}),
    ("interior", "Room makeover & interior", {"en": ["room makeover", "interior design"], "de": ["zimmer makeover", "einrichtung"], "ja": ["部屋づくり", "インテリア"], "ko": ["인테리어", "방꾸미기"]}),
    ("gardening", "Gardening & plants", {"en": ["gardening", "plants"], "de": ["garten", "pflanzen"], "ja": ["ガーデニング", "観葉植物"], "ko": ["가드닝", "식물"]}),
    ("fishing", "Fishing", {"en": ["fishing"], "de": ["angeln"], "ja": ["釣り"], "ko": ["낚시"]}),
    ("camping", "Camping & bushcraft", {"en": ["camping", "bushcraft"], "de": ["camping", "bushcraft"], "ja": ["キャンプ", "ソロキャンプ"], "ko": ["캠핑", "차박"]}),
    ("finance", "Money & finance tips", {"en": ["money tips", "personal finance"], "de": ["geld sparen", "finanzen"], "ja": ["節約", "お金の知識"], "ko": ["재테크", "돈 모으는 법"]}),
    ("language", "Language learning", {"en": ["learn english", "language learning"], "de": ["deutsch lernen", "englisch lernen"], "ja": ["英会話", "英語 勉強"], "ko": ["영어 공부", "일본어 공부"]}),
    ("toys", "Toys & unboxing", {"en": ["toy unboxing", "unboxing"], "de": ["spielzeug", "unboxing"], "ja": ["おもちゃ", "開封"], "ko": ["장난감", "언박싱"]}),
    ("magic", "Magic tricks", {"en": ["magic trick", "illusion"], "de": ["zaubertrick", "illusion"], "ja": ["マジック", "手品"], "ko": ["마술", "마술 트릭"]}),
    ("football", "Football skills", {"en": ["football skills", "soccer skills"], "de": ["fußball skills", "fussball"], "ja": ["サッカー", "サッカー 技"], "ko": ["축구", "축구 기술"]}),
]
NICHE_BY_ID = {n[0]: n for n in NICHES}
# a JP / KR channel must title most of its Shorts in that script (region filters of the API are loose)
MARKET_SCRIPTS = {"ja": re.compile("[぀-ヿ一-鿿]"), "ko": re.compile("[가-힯]")}
MIN_SCRIPT_SHARE = 0.5
MIN_LANG_SHARE = 0.5   # of a channel's recent Shorts in the market's language
MIN_TOPIC_SHARE = 0.4  # of a channel's recent Shorts about the niche
# small word lists to tell apart the languages that show up in Latin-script searches (titles rarely set a language)
LANG_WORDS = {
    "en": set("the and to of in is this how my with for you your it on what why best when from this that".split()),
    "de": set("der die das und ist mit ich nicht wie ein eine zu auf für den dem wird so auto".split()),
    "ms": set("yang dan ini untuk dengan tidak saya kereta cara di ke dari ada boleh mobil cuci bersih jadi".split()),
    "es": set("el la los las que y en un una por para con cómo del lo coche limpieza".split()),
    "pt": set("o os as que e em um uma para com como não do da carro limpeza".split()),
    "fr": set("le la les des et un une pour avec comment est du voiture nettoyage".split()),
    "hi": set("hai ka ki ke ko se aur kya kaise mein yeh wala bhai hamare hamari aapko aap toh zaroor nahi "
              "karein dekhne milega bhi".split()),
}
# Latin-script markets: Shorts aimed at another market (South Asian prices / places, other scripts in titles)
FOREIGN_MARKERS = re.compile(r"₹|\blakh\b|\bcrore\b|\bindian?\b|\bpakistan\w*|\bbangladesh\w*"
                             r"|[Ѐ-ӿ؀-ۿऀ-෿฀-๿぀-ヿ一-鿿가-힯]",
                             re.I)
MAX_FOREIGN_SHARE = 0.25  # of a channel's recent Shorts
MIN_DISTINCT_TITLES = 0.5  # spam channels paste one clickbait title on every Short, whatever the video shows
TOPIC_STOPWORDS = set("the and for with how best easy quick tips video videos shorts ideas".split())


class ResearchError(Exception):
    pass


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- helpers
def load_api_key(*dirs):
    """YOUTUBE_API_KEY from the environment or a .env file in one of `dirs`."""
    if os.environ.get("YOUTUBE_API_KEY"):
        return os.environ["YOUTUBE_API_KEY"].strip()
    for d in dirs:
        path = os.path.join(d, ".env")
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.partition("=")
                    if k.strip() == "YOUTUBE_API_KEY" and v.strip():
                        return v.strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _now():
    return datetime.now(timezone.utc)


def _parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_seconds(d):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    days, h, mi, s = (int(x or 0) for x in m.groups())
    return days * 86400 + h * 3600 + mi * 60 + s


def norm_name(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = re.sub(r"[【\[(（].*?[】\])）]", " ", s)  # bracketed taglines: グルメ大学【JAPAN】
    return re.sub(r"[\W_]+", "", s)


def similarity(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return max(0.8, difflib.SequenceMatcher(None, a, b).ratio())
    return difflib.SequenceMatcher(None, a, b).ratio()


def guess_lang(text):
    """Best language of a Latin-script title by common words, or "" when unsure."""
    words = set(re.findall(r"[a-zäöüßáéíóúñçãõâêôàèùì]+", (text or "").casefold()))
    hits = {lang: len(words & vocab) for lang, vocab in LANG_WORDS.items()}
    best = max(hits, key=hits.get)
    return best if hits[best] and list(hits.values()).count(hits[best]) == 1 else ""


def language_ok(recent, lang):
    """Most recent Shorts are in `lang`: by YouTube's language tag, else script (ja/ko), else common words."""
    if not recent:
        return True
    codes = [v["lang"] for v in recent if v.get("lang")]
    if len(codes) >= 3:
        return sum(c == lang for c in codes) / len(codes) >= MIN_LANG_SHARE
    script = MARKET_SCRIPTS.get(lang)
    if script:
        return sum(bool(script.search(v["title"])) for v in recent) / len(recent) >= MIN_SCRIPT_SHARE
    guesses = [g for g in (guess_lang(f"{v['title']} {v['description'][:200]}") for v in recent) if g]
    return not guesses or sum(g == lang for g in guesses) / len(guesses) >= MIN_LANG_SHARE


def foreign_share(recent):
    """Share of recent Shorts whose title / description / tags point at another market."""
    if not recent:
        return 0.0
    hits = sum(bool(FOREIGN_MARKERS.search(f"{v['title']} {' '.join(v.get('tags', []))} {v['description'][:300]}"))
               for v in recent)
    return hits / len(recent)


def distinct_titles(recent):
    """Share of distinct titles among recent Shorts (invisible characters and case ignored)."""
    if not recent:
        return 1.0
    titles = {re.sub(r"[\s​-‏⁠﻿]+", " ", v["title"]).strip().casefold() for v in recent}
    return len(titles) / len(recent)


def niche_hashtags(vids, keywords):
    """Hashtags most channels of the niche put on their Shorts (brand tags used by one channel and generic ones skipped)."""
    by_tag = {}
    for v in vids:
        for t in re.findall(r"#(\w{3,30})", f"{v['title']} {v['description'][:500]}"):
            t = unicodedata.normalize("NFKC", t).casefold()
            if t not in GENERIC_TAGS and not t.isdigit():
                by_tag.setdefault(t, set()).add(v["channelId"])
    used = {re.sub(r"\W+", "", k.casefold()) for k in keywords}
    tags = [(len(chs), t) for t, chs in by_tag.items() if len(chs) >= 2 and len(t) >= 5]  # #car, #auto: too broad
    # the keyword itself as a tag (#carcare) still finds other Shorts, so it stays; most shared first
    return [t for _, t in sorted(tags, key=lambda x: (-x[0], x[1] not in used))]


def topic_pattern(keywords, phrases=()):
    """Regex matching any meaningful word of the niche keywords (word prefixes, so clean ~ cleaning / cleaner).
    `phrases` (a niche typed by the user) only match as a whole, plus their long words: "car care" must not let
    every video about a car in."""
    parts = set()
    for k in phrases:
        words = re.findall(r"[a-z0-9äöüß]+", unicodedata.normalize("NFKC", k or "").casefold())
        if len(words) > 1 and all(w.isascii() for w in words):
            parts.add(r"\b" + r"\W*".join(re.escape(w) for w in words))
            parts.update(r"\b" + re.escape(w[:5]) for w in words if len(w) >= 5 and w not in TOPIC_STOPWORDS)
        else:
            keywords = [*keywords, k]
    for k in keywords:
        k = unicodedata.normalize("NFKC", k or "").casefold().strip()
        if not k:
            continue
        if not k.isascii():
            parts.update(re.escape(w) for w in k.split())  # レシピ, 세차: plain substrings
            continue
        for w in re.findall(r"[a-z0-9äöüß]+", k):
            if len(w) < 3 or w in TOPIC_STOPWORDS:
                continue
            # short words whole (car, cars - not cardboard), longer ones by prefix (clean ~ cleaning / cleaner)
            parts.add(r"\b" + re.escape(w) + r"s?\b" if len(w) <= 4 else r"\b" + re.escape(w[:5]))
    return re.compile("|".join(sorted(parts)), re.I) if parts else None


def topic_share(recent, pattern):
    if not pattern or not recent:
        return 1.0
    hits = sum(bool(pattern.search(f"{v['title']} {' '.join(v.get('tags', []))} {v['description'][:300]}")) for v in recent)
    return hits / len(recent)


def clean_title(t):
    t = re.sub(r"#\S+", " ", t or "")
    t = re.sub(r"[|｜/].*$", "", t)
    return re.sub(r"\s+", " ", t).strip()[:60]


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


HASH_W, HASH_H = 72, 128  # gray thumbnail the hashes are taken from (9:16, like a Short)


def _dhash_px(px):
    """dHash of a HASH_W x HASH_H gray frame after trimming black borders (re-uploads often letterbox the clip)."""
    w, h = HASH_W, HASH_H
    rows = [sum(px[y * w:(y + 1) * w]) / w for y in range(h)]
    cols = [sum(px[y * w + x] for y in range(0, h, 2)) / (h / 2) for x in range(w)]
    ys = [y for y in range(h) if rows[y] > 18]
    xs = [x for x in range(w) if cols[x] > 18]
    x0, y0, x1, y1 = (min(xs), min(ys), max(xs) + 1, max(ys) + 1) if ys and xs else (0, 0, w, h)
    grid = []
    for r in range(8):
        ya, yb = y0 + (y1 - y0) * r // 8, y0 + (y1 - y0) * (r + 1) // 8
        for c in range(9):
            xa, xb = x0 + (x1 - x0) * c // 9, x0 + (x1 - x0) * (c + 1) // 9
            cells = [px[y * w + x] for y in range(ya, max(yb, ya + 1)) for x in range(xa, max(xb, xa + 1))]
            grid.append(sum(cells) / len(cells))
    return sum(1 << i for i in range(64) if grid[(i // 8) * 9 + i % 8] > grid[(i // 8) * 9 + i % 8 + 1])


def _gray_frames(ffmpeg, args, data=None, timeout=20):
    try:
        r = subprocess.run([ffmpeg, "-v", "error", *args, "-f", "rawvideo", "pipe:1"], input=data, capture_output=True,
                           timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return []
    n = HASH_W * HASH_H
    return [r.stdout[i:i + n] for i in range(0, len(r.stdout) - n + 1, n)]


def dhash(ffmpeg, data, crop_vertical=False):
    """64-bit difference hash of an image (ffmpeg does the decoding, so no imaging library is needed)."""
    vf = ("crop=ih*9/16:ih," if crop_vertical else "") + f"scale={HASH_W}:{HASH_H}:flags=area,format=gray"
    frames = _gray_frames(ffmpeg, ["-i", "pipe:0", "-vf", vf, "-frames:v", "1"], data)
    return _dhash_px(frames[0]) if frames else None


def frame_hashes(ffmpeg, path, fps=1):
    """One dHash per second of a video file."""
    vf = f"fps={fps},scale={HASH_W}:{HASH_H}:flags=area,format=gray"
    return [_dhash_px(px) for px in _gray_frames(ffmpeg, ["-i", path, "-vf", vf], timeout=180)]


def frames_overlap(a, b):
    """Share of the frames of `a` that also appear (within FRAME_MATCH bits) in `b`."""
    if not a or not b:
        return 0.0
    return sum(min(hamming(x, y) for y in b) <= FRAME_MATCH for x in a) / len(a)


def hamming(a, b):
    return 64 if a is None or b is None else bin(a ^ b).count("1")


# ---------------------------------------------------------------- YouTube Data API
class YouTube:
    def __init__(self, key, usage):
        if not key:
            raise ResearchError("No YouTube API key: put YOUTUBE_API_KEY=... in a .env file next to the app.")
        self.key = key
        self.usage = usage  # callable(units)

    def call(self, endpoint, cost=1, **params):
        params["key"] = self.key
        url = API + endpoint + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    self.usage(cost)
                    return json.load(r)
            except urllib.error.HTTPError as ex:
                try:
                    msg = json.load(ex).get("error", {}).get("message", "")
                except ValueError:
                    msg = str(ex)
                if ex.code == 403 and "quota" in msg.lower():
                    raise ResearchError("YouTube API daily quota is used up — try again tomorrow.") from None
                if ex.code >= 500 and attempt < 2:
                    time.sleep(2)
                    continue
                raise ResearchError(f"YouTube API {endpoint}: {re.sub('<[^>]+>', '', msg) or ex}") from None
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(2)

    def search_shorts(self, q, market, days, order="viewCount", max_results=50, duration="short"):
        """`duration`: "short" (< 4 min), "medium" (4-20 min) or "long" (> 20 min)."""
        m = MARKETS[market]
        after = (_now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        d = self.call("search", cost=100, part="snippet", type="video", videoDuration=duration, order=order,
                      regionCode=m["region"], relevanceLanguage=m["lang"], q=q, publishedAfter=after,
                      maxResults=max_results)
        return [it["id"]["videoId"] for it in d.get("items", []) if it.get("id", {}).get("videoId")]

    def search_channels(self, q, market, max_results=50):
        """Channels whose name / description match `q` (catches channels the video searches miss)."""
        m = MARKETS[market]
        d = self.call("search", cost=100, part="snippet", type="channel", q=q, regionCode=m["region"],
                      relevanceLanguage=m["lang"], maxResults=max_results)
        return [it["id"]["channelId"] for it in d.get("items", []) if it.get("id", {}).get("channelId")]

    def popular_shorts(self, channel_id, n=6, pages=4):
        """The channel's `n` most viewed Shorts among its latest 50 x `pages` (what re-uploaders copy first)."""
        ids, token = [], None
        for _ in range(pages):
            params = {"part": "contentDetails", "playlistId": "UUSH" + channel_id[2:], "maxResults": 50}
            if token:
                params["pageToken"] = token
            try:
                d = self.call("playlistItems", **params)
            except ResearchError:
                break
            ids += [it["contentDetails"]["videoId"] for it in d.get("items", [])]
            token = d.get("nextPageToken")
            if not token:
                break
        return sorted(self.videos(ids), key=lambda v: v["views"], reverse=True)[:n]

    def videos(self, ids):
        out = []
        for i in range(0, len(ids), 50):
            d = self.call("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
            for it in d.get("items", []):
                sn, st = it["snippet"], it.get("statistics", {})
                out.append({
                    "id": it["id"], "title": sn.get("title", ""), "description": sn.get("description", ""),
                    "channelId": sn["channelId"], "channelTitle": sn.get("channelTitle", ""),
                    "published": sn["publishedAt"], "views": int(st.get("viewCount", 0)),
                    "seconds": iso_seconds(it["contentDetails"].get("duration")),
                    "thumb": (sn.get("thumbnails", {}).get("high") or sn.get("thumbnails", {}).get("default") or {}).get("url", ""),
                    "lang": (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").split("-")[0].lower(),
                    "tags": sn.get("tags", [])[:30],
                })
        return out

    def channels(self, ids):
        out = {}
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), 50):
            d = self.call("channels", part="snippet,statistics", id=",".join(ids[i:i + 50]))
            for it in d.get("items", []):
                sn, st = it["snippet"], it.get("statistics", {})
                out[it["id"]] = {
                    "id": it["id"], "title": sn.get("title", ""), "handle": sn.get("customUrl", ""),
                    "description": sn.get("description", ""), "created": sn["publishedAt"],
                    "country": sn.get("country", ""),
                    "avatar": (sn.get("thumbnails", {}).get("medium") or sn.get("thumbnails", {}).get("default") or {}).get("url", ""),
                    "subs": None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0)),
                    "views": int(st.get("viewCount", 0)), "videos": int(st.get("videoCount", 0)),
                }
        return out

    def playlist_total(self, playlist_id):
        try:
            d = self.call("playlistItems", part="contentDetails", playlistId=playlist_id, maxResults=1)
        except ResearchError:
            return 0  # a channel with no Shorts / no long videos has no such playlist
        return d.get("pageInfo", {}).get("totalResults", 0)

    def playlist_ids(self, playlist_id, n=20):
        try:
            d = self.call("playlistItems", part="contentDetails", playlistId=playlist_id, maxResults=n)
        except ResearchError:
            return []
        return [it["contentDetails"]["videoId"] for it in d.get("items", [])]


# ---------------------------------------------------------------- research engine
class Research:
    def __init__(self, key, data_dir, ffmpeg, chrome_root, log=lambda level, msg, detail="": None,
                 progress=lambda stage, text, frac=None: None, should_cancel=lambda: False):
        self.cache_file = os.path.join(data_dir, "research_cache.json")
        self._lock = threading.Lock()
        self.cache = self._load()
        self.yt = YouTube(key, self._use_quota)
        self.ffmpeg = ffmpeg
        self.chrome_root = chrome_root
        self.log = log
        self.progress = progress
        self.should_cancel = should_cancel

    # ---- cache & quota
    def _load(self):
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False)
        os.replace(tmp, self.cache_file)

    def _use_quota(self, units):
        # quota resets at midnight Pacific time; counting per local day is close enough for a gauge
        with self._lock:
            day = time.strftime("%Y-%m-%d")
            q = self.cache.setdefault("quota", {})
            if q.get("day") != day:
                q.clear()
                q["day"] = day
            q["used"] = q.get("used", 0) + units

    def quota(self):
        q = self.cache.get("quota", {})
        return q.get("used", 0) if q.get("day") == time.strftime("%Y-%m-%d") else 0

    def _check_cancel(self):
        if self.should_cancel():
            raise Cancelled()

    # ---- 1. niches
    def niche_results(self, market):
        fresh = _now() - timedelta(hours=NICHE_CACHE_HOURS)
        rows = [r for r in self.cache.get("niches", {}).get(market, {}).values() if _parse_time(r["at"]) > fresh]
        return sorted(rows, key=lambda r: r["score"], reverse=True)

    def research_niches(self, market, count=10):
        """Score `count` niches not scored in the last 24 h; returns every fresh score for the market."""
        done = {r["id"] for r in self.niche_results(market)}
        todo = [n for n in NICHES if n[0] not in done][:count]
        if not todo:
            self.progress("niches", "All niches were scored in the last 24 h", 1)
        for i, (nid, name, kw) in enumerate(todo):
            self._check_cancel()
            self.progress("niches", f"Scoring niche {i + 1}/{len(todo)}: {name}", i / len(todo))
            self.score_niche(market, nid, name, kw[MARKETS[market]["lang"]][0])
        self.progress("niches", "Niches scored", 1)
        return self.niche_results(market)

    def score_niche(self, market, nid, name, keyword):
        ids = self.yt.search_shorts(keyword, market, NICHE_WINDOW_DAYS)
        vids = [v for v in self.yt.videos(ids) if 0 < v["seconds"] <= MAX_SHORT_SECONDS]
        chans = self.yt.channels([v["channelId"] for v in vids])
        now = _now()
        vpd = [v["views"] / max((now - _parse_time(v["published"])).total_seconds() / 86400, 0.5) for v in vids]
        new = [c for c in chans.values() if (now - _parse_time(c["created"])).days <= MAX_AGE_DAYS]
        med_vpd = statistics.median(vpd) if vpd else 0
        hits = sum(v["views"] >= 100_000 for v in vids)
        new_share = len(new) / len(chans) if chans else 0
        score = round(math.log10(1 + med_vpd) * 10 + new_share * 30 + min(hits, 25), 1)
        top = sorted(vids, key=lambda v: v["views"], reverse=True)[:3]
        row = {"id": nid, "name": name, "keyword": keyword, "market": market, "score": score,
               "medianViewsPerDay": round(med_vpd), "hits": hits, "shorts": len(vids), "channels": len(chans),
               "newChannels": len(new), "newShare": round(new_share, 2), "at": now.isoformat(),
               "examples": [{"id": v["id"], "title": v["title"], "views": v["views"], "channel": v["channelTitle"]} for v in top]}
        with self._lock:
            self.cache.setdefault("niches", {}).setdefault(market, {})[nid] = row
            self._save()
        self.log("info", f"Niche {name} [{market}] score {score}: median {round(med_vpd):,} views/day, "
                         f"{hits} shorts ≥100K, {len(new)}/{len(chans)} new channels")
        return row

    # ---- 2. channels
    def find_channels(self, market, keywords, topic_words=(), long=False):
        """`topic_words`: words that define a catalog niche (keywords + its name); channels must be mostly about them.
        Without them (a niche the user typed) the keywords only count as whole phrases.
        `long`: channels that mostly upload long videos (to cut with Smart cut) instead of Shorts channels."""
        kind = "long videos" if long else "Shorts"
        ids = []
        for i, q in enumerate(keywords):
            self._check_cancel()
            self.progress("channels", f"Searching {kind} for “{q}”…", 0.05 + 0.05 * i)
            if long:  # long channels upload less often: look back further, 4-20 min and 20+ min videos
                ids += self.yt.search_shorts(q, market, DISCOVERY_DAYS, duration="medium")
                ids += self.yt.search_shorts(q, market, DISCOVERY_DAYS, duration="long")
                ids += self.yt.search_shorts(q, market, DISCOVERY_DAYS, order="relevance", duration="long")
            else:
                ids += self.yt.search_shorts(q, market, CHANNEL_WINDOW_DAYS)
                ids += self.yt.search_shorts(q, market, DISCOVERY_DAYS, order="relevance")
        vids = self.yt.videos(list(dict.fromkeys(ids)))
        niche_tags = niche_hashtags(vids, keywords)
        tags = [] if long else niche_tags[:HASHTAG_QUERIES]  # long videos rarely carry hashtags: save the quota
        # the niche's own hashtags (#cardetailing) also define its topic: titles rarely repeat the typed keyword
        if topic_words:
            topic = topic_pattern([*keywords, *topic_words, *niche_tags[:6]])
        else:
            topic = topic_pattern(niche_tags[:6], phrases=keywords)
        tag_ids = []
        for i, tag in enumerate(tags):
            self._check_cancel()
            self.progress("channels", f"Searching Shorts for #{tag}…", 0.15 + 0.05 * i)
            tag_ids += self.yt.search_shorts("#" + tag, market, CHANNEL_WINDOW_DAYS)
            tag_ids += self.yt.search_shorts("#" + tag, market, DISCOVERY_DAYS, order="relevance")
        seen = {v["id"] for v in vids}
        vids += self.yt.videos([i for i in dict.fromkeys(tag_ids) if i not in seen])
        self._check_cancel()
        self.progress("channels", f"Searching channels named like “{keywords[0]}”…", 0.25)
        named = self.yt.search_channels(keywords[0], market)
        chans = self.yt.channels([v["channelId"] for v in vids] + named)
        self.log("info", f"Searched {len(keywords)} keyword(s), hashtags {', '.join('#' + t for t in tags) or '—'} "
                         f"and channel names: {len(vids)} {kind}, {len(chans)} channels")
        now = _now()
        region, lang = MARKETS[market]["region"], MARKETS[market]["lang"]
        stage1 = [c for c in chans.values() if (long or (now - _parse_time(c["created"])).days <= CHANNEL_MAX_AGE_DAYS)
                  and c["subs"] is not None and CHANNEL_MIN_SUBS <= c["subs"] <= CHANNEL_MAX_SUBS
                  and (not c["country"] or c["country"] == region)]
        self.log("info", f"{len(stage1)} channels " + ("" if long else f"are ≤{CHANNEL_MAX_AGE_DAYS // 30} months old ")
                 + f"with {CHANNEL_MIN_SUBS:,}-{CHANNEL_MAX_SUBS:,} subscribers and not registered in another country")
        share_ok = (lambda c: c.get("longShare", 0) >= LONG_MIN_SHARE) if long else \
            (lambda c: c.get("shortsShare", 0) >= CHANNEL_MIN_SHORTS_SHARE)

        def enrich(c):
            tail = c["id"][2:]
            shorts, longs = self.yt.playlist_total("UUSH" + tail), self.yt.playlist_total("UULF" + tail)
            c["shorts"], c["longs"] = shorts, longs
            c["shortsShare"] = round(shorts / (shorts + longs), 2) if shorts + longs else 0
            if long:
                c["format"] = "long"
                c["longShare"] = round(longs / (shorts + longs), 2) if shorts + longs else 0
            if not share_ok(c):
                return c
            recent = self.yt.videos(self.yt.playlist_ids(("UULF" if long else "UUSH") + tail, LONG_RECENT if long else 20))
            c["recent"] = recent
            if distinct_titles(recent) < MIN_DISTINCT_TITLES:
                c["offMarket"] = "spam"
                return c
            channel_lang = guess_lang(c["description"][:500])
            if not language_ok(recent, lang) or (channel_lang and channel_lang != lang):
                c["offMarket"] = "language"
                return c
            if lang not in MARKET_SCRIPTS and foreign_share(recent) >= MAX_FOREIGN_SHARE:
                c["offMarket"] = "language"
                return c
            c["topicShare"] = round(topic_share(recent, topic), 2)
            if c["topicShare"] < MIN_TOPIC_SHARE:
                c["offMarket"] = "topic"
                return c
            days = max((now - _parse_time(c["created"])).days, 1)
            last30 = [v for v in recent if (now - _parse_time(v["published"])).days <= 30]
            c["ageDays"] = days
            c["viewsPerDay"] = round(c["views"] / days)
            c["subsPerDay"] = round(c["subs"] / days, 1)
            c["avgRecentViews"] = round(statistics.mean(v["views"] for v in recent)) if recent else 0
            c["uploads30"] = len(last30)
            c["lastUpload"] = recent[0]["published"][:10] if recent else ""
            c["active"] = bool(recent) and (now - _parse_time(recent[0]["published"])).days <= (LONG_ACTIVE_DAYS if long else ACTIVE_DAYS)
            c["score"] = round(math.log10(1 + c["viewsPerDay"]) * 10 + math.log10(1 + c["avgRecentViews"]) * 8
                               + min(c["uploads30"], 30) / 3 + math.log10(1 + c["subsPerDay"]) * 5, 1)
            return c

        out = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for i, c in enumerate(pool.map(enrich, stage1)):
                self.progress("channels", f"Checking channels {i + 1}/{len(stage1)}", 0.3 + 0.2 * (i + 1) / max(len(stage1), 1))
                if share_ok(c) and c.get("active") and not c.get("offMarket"):
                    out.append(c)
        out.sort(key=lambda c: c["score"], reverse=True)
        off_lang = sum(1 for c in stage1 if c.get("offMarket") == "language")
        off_topic = sum(1 for c in stage1 if c.get("offMarket") == "topic")
        spam = sum(1 for c in stage1 if c.get("offMarket") == "spam")
        rule = (f"≥{int(LONG_MIN_SHARE * 100)} % long videos, active in the last {LONG_ACTIVE_DAYS} days" if long else
                f"≥{int(CHANNEL_MIN_SHORTS_SHARE * 100)} % Shorts, active in the last {ACTIVE_DAYS} days")
        self.log("info", f"{len(out)} promising {'long-video' if long else 'Shorts'} channels ({rule}"
                         f"; dropped {off_lang} not in the market's language or aimed at another market, "
                         f"{off_topic} mostly about other topics and {spam} reusing one title on most Shorts)")
        return out

    def channel_by_url(self, url):
        """Resolve a YouTube channel URL (@handle, /channel/UC...) into the same dict find_channels returns."""
        m = re.search(r"youtube\.com/(?:channel/(UC[\w-]{22})|(@[^/?#]+))", url or "")
        if not m:
            raise ResearchError("Use a YouTube channel link (…/@handle or …/channel/UC…).")
        if m.group(1):
            cid = m.group(1)
        else:
            d = self.yt.call("channels", part="id", forHandle=urllib.parse.unquote(m.group(2)))
            if not d.get("items"):
                raise ResearchError("YouTube channel not found.")
            cid = d["items"][0]["id"]
        c = self.yt.channels([cid])[cid]
        tail = cid[2:]
        shorts, longs = self.yt.playlist_total("UUSH" + tail), self.yt.playlist_total("UULF" + tail)
        if longs > shorts:  # a long-video channel: checked against its long videos, not Shorts
            c["format"], c["longShare"] = "long", round(longs / (shorts + longs), 2)
            c["recent"] = self.yt.videos(self.yt.playlist_ids("UULF" + tail, LONG_RECENT))
        else:
            c["recent"] = self.yt.videos(self.yt.playlist_ids("UUSH" + tail, 20) or self.yt.playlist_ids("UU" + tail, 20))
        return c

    # ---- 3. TikTok check
    def about_links(self, channel):
        """External links from the channel's About page (the Data API does not expose them)."""
        try:
            html = http_get(f"https://www.youtube.com/channel/{channel['id']}/about").decode("utf-8", "replace")
        except OSError:
            return []
        m = re.search(r"var ytInitialData = (\{.*?\});</script>", html)
        text = urllib.parse.unquote(m.group(1)) if m else ""
        return sorted(set(re.findall(r"https?://[^\s\"\\<>]+", text.replace("\\u0026", "&"))))

    def check_tiktok(self, channels, stop_after=None):
        """Check channels one by one in one Chrome window; stops early once `stop_after` have verdict 'none'."""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, page = tiktok._open_profile_page(p, self.chrome_root, RESEARCH_PROFILE)
            checker = _TikTokChecker(page, self)
            try:
                free, warned = 0, False
                for i, c in enumerate(channels):
                    self._check_cancel()
                    self.progress("tiktok", f"TikTok check {i + 1}/{len(channels)}: {c['title']}", 0.5 + 0.5 * i / len(channels))
                    try:
                        c.setdefault("popular", [] if c.get("format") == "long" else self.yt.popular_shorts(c["id"]))
                        c["tiktok"] = checker.check(c)
                    except Cancelled:
                        raise
                    except Exception as ex:  # noqa: BLE001 - one channel failing must not stop the run
                        c["tiktok"] = {"verdict": "error", "error": f"{type(ex).__name__}: {ex}", "candidates": []}
                    self.log("info" if c["tiktok"]["verdict"] != "error" else "warning",
                             f"{c['title']}: TikTok {c['tiktok']['verdict']}",
                             "\n".join(f"{x['verdict']} {x['url']} — {'; '.join(x['evidence'])}" for x in c["tiktok"].get("candidates", [])))
                    if c["tiktok"].get("blocked") and not warned:
                        warned = True
                        self.log("warning", f"TikTok refused some searches ({c['tiktok']['blocked']}): channels with no "
                                            "account found are marked unverified, not “not on TikTok”. Log in to TikTok "
                                            "in the Chrome Portable “Research” profile so searches work.")
                    free += c["tiktok"]["verdict"] == "none"
                    if stop_after and free >= stop_after:
                        break
            finally:
                checker.close()
                page.close()
                browser.close()
        return channels


# the visible box titled "Log in to TikTok" (any language) and the centre of the small button in its top right corner
LOGIN_POPUP_X_JS = """() => {
  const title = [...document.querySelectorAll('h1, h2, div, span')].find((e) => {
    const t = (e.childElementCount ? '' : e.textContent || '').trim();
    return t.length < 40 && /log in to tiktok|tiktokにログイン|登录 ?tiktok|tiktok에 로그인|đăng nhập vào tiktok/i.test(t)
      && e.getBoundingClientRect().width > 0;
  });
  if (!title) return null;
  for (let box = title.parentElement, up = 0; box && up < 8; box = box.parentElement, up++) {
    const r = box.getBoundingClientRect();
    if (r.width < 300 || r.width > window.innerWidth * 0.9) continue;  // the popup card, not the whole page
    const close = [...box.querySelectorAll('button, [role="button"], div, span')].map((e) => [e, e.getBoundingClientRect()])
      .filter(([e, b]) => b.width >= 16 && b.width <= 56 && b.height >= 16 && b.height <= 56 && e.querySelector('svg')
        && b.right >= r.right - 70 && b.top <= r.top + 70)
      .sort((a, b) => a[1].width * a[1].height - b[1].width * b[1].height)[0];
    if (close) return { x: close[1].left + close[1].width / 2, y: close[1].top + close[1].height / 2 };
  }
  return null;
}"""

VERDICT_ORDER = {"exists": 0, "likely": 1, "suspect": 2, "reup": 3, "unverified": 4, "none": 5, "error": 6}


class _TikTokChecker:
    def __init__(self, page, research):
        self.page = page
        self.r = research
        self.api = []
        page.on("response", self._on_response)
        self._last = 0.0
        self._hashes = {}
        self._tmp = tempfile.mkdtemp(prefix="grabber-research-")
        self._yt_frame_cache = {}
        self.long = False  # checking a long-video channel: no lengths or frames to compare
        self.search_blocked = {}  # search endpoint -> TikTok's refusal ("2483 log in first")
        self.searches = 0
        self._search_cache = {}

    def close(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _on_response(self, resp):
        if "tiktok.com/api/" in resp.url and any(k in resp.url for k in ("/search/general/full", "/post/item_list")):
            try:
                self.api.append((resp.url, resp.json()))
            except Exception:  # noqa: BLE001 - empty / blocked body
                self.api.append((resp.url, None))

    def _goto(self, url, settle=4000):
        wait = 1.5 - (time.time() - self._last)  # gentle pacing between TikTok page loads
        if wait > 0:
            time.sleep(wait)
        self.api.clear()
        self._last = time.time()
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightError:
            return False  # e.g. HTTP 404 for a handle that does not exist
        self.page.wait_for_timeout(settle)
        self._wait_captcha()
        self._close_login_popup()  # a guest gets the log in popup over pages now and then
        return True

    def _wait_captcha(self):
        def blocked():
            return self.page.evaluate("""() => !!document.querySelector('#captcha-verify-container, .captcha-verify-container, [class*="captcha_verify"], #tiktok-verify-ele')
                || /Drag the slider|Verify to continue|スライダー|슬라이더/.test(document.body.innerText.slice(0, 4000))""")
        if not blocked():
            return
        self.r.progress("tiktok", "TikTok shows a captcha — solve it in the Chrome window (Research profile)…", None)
        self.r.log("warning", "TikTok captcha: waiting for it to be solved in Chrome")
        self.page.bring_to_front()
        deadline = time.time() + 300
        while time.time() < deadline:
            self.r._check_cancel()
            self.page.wait_for_timeout(2000)
            if not blocked():
                self.page.wait_for_timeout(2500)
                return
        raise ResearchError("TikTok captcha was not solved within 5 minutes.")

    def _user_detail(self):
        raw = self.page.evaluate("() => (document.querySelector('#__UNIVERSAL_DATA_FOR_REHYDRATION__') || {}).textContent || ''")
        if not raw:
            return None
        d = json.loads(raw).get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
        if d.get("statusCode") != 0:
            return None
        info = d.get("userInfo", {})
        u, st = info.get("user", {}), info.get("stats", {})
        return {"handle": u.get("uniqueId", ""), "nickname": u.get("nickname", ""), "bio": u.get("signature", ""),
                "bioLink": (u.get("bioLink") or {}).get("link", ""), "avatar": u.get("avatarLarger") or u.get("avatarMedium", ""),
                "followers": st.get("followerCount", 0), "videos": st.get("videoCount", 0)}

    def _items(self):
        items = []
        for url, body in self.api:
            if "/post/item_list" in url and body:
                items += body.get("itemList") or []
        return items

    def _hash_url(self, url, crop=False, via_page=False):
        if not url:
            return None
        if (url, crop) in self._hashes:
            return self._hashes[(url, crop)]
        try:
            data = self.page.request.get(url, timeout=20000).body() if via_page else http_get(url, 20)
        except Exception:  # noqa: BLE001
            data = b""
        h = dhash(self.r.ffmpeg, data, crop) if data else None
        self._hashes[(url, crop)] = h
        return h

    # -- candidate sources
    def _links_from_youtube(self, c):
        texts = [c.get("description", "")] + [v.get("description", "") for v in c.get("recent", [])[:20]]
        links = self.r.about_links(c)
        handles = []
        for t in links + texts:
            handles += re.findall(r"tiktok\.com/@([\w.]+)", t, re.I)
            handles += re.findall(r"tiktok\s*[:：@-]?\s*@([\w.]{2,24})", t, re.I)
        return list(dict.fromkeys(h.rstrip(".").lower() for h in handles))

    def _handle_variants(self, c):
        h = (c.get("handle") or "").lstrip("@").lower()
        h = re.sub(r"-[a-z0-9]{3}$", "", h)  # auto handles like @acperformance-h4p
        # only the exact handle is opened directly: changed spellings are found by searching the handle (see check)
        out = [h] if h else []
        raw = unicodedata.normalize("NFKC", c.get("title") or "").casefold().strip()
        if re.fullmatch(r"[a-z0-9_.]{3,24}", raw):  # a title that is itself a handle: ena_kazokugohan
            out.append(raw)
        title = norm_name(c.get("title"))
        if re.fullmatch(r"[a-z0-9]{3,24}", title or ""):
            out.append(title)
        return [v for v in dict.fromkeys(out) if re.fullmatch(r"[a-z0-9_.]{2,24}", v)][:6]

    def _mentioned_handles(self, c):
        """@handles written in the channel description or the Shorts' descriptions: creators mostly use the same
        handle on Instagram and TikTok ("お仕事のご依頼はInstagramのDMまで @yuu_meshi_recipe_")."""
        texts = [c.get("description", "")] + [v.get("description", "") for v in [*c.get("recent", [])[:20], *c.get("popular", [])]]
        found = []
        for t in texts:
            found += re.findall(r"(?<![\w@])@([A-Za-z0-9_.]{3,24})", t)
        return list(dict.fromkeys(h.rstrip(".").lower() for h in found))[:5]

    def _blocked(self, endpoint):
        """TikTok answers a search it won't serve to this session (e.g. "log in first", status 2483) with an empty
        list and a non-zero status_code: remember it, so an empty result is not read as "nothing on TikTok"."""
        for url, body in self.api:
            if endpoint in url and body and body.get("status_code"):
                self.search_blocked[endpoint] = f"{body.get('status_code')} {body.get('status_msg') or ''}".strip()
                return True
        return False

    def _search(self, q):
        """Search TikTok the way a visitor does: type into the search box and press Enter. Opening
        /search/user?q=… or /search/video?q=… directly is refused for a guest ("log in first", status 2483),
        while the box's general search answers with videos (and their authors) plus a few matching users.
        Returns {"users": [...], "videos": [...]}; one search serves both lists, cached per query."""
        if q in self._search_cache:
            return self._search_cache[q]
        res = {"users": [], "videos": []}
        wait = 1.5 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        for attempt in range(2):
            try:
                self._close_login_popup()
                box = self.page.locator('[data-e2e="search-user-input"]:visible').first
                if "tiktok.com" not in self.page.url or not box.count() or attempt:
                    self.page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=60000)
                    self.page.wait_for_timeout(3000)
                    self._wait_captcha()
                    self._close_login_popup()
                    box = self.page.locator('[data-e2e="search-user-input"]:visible').first
                    if not box.count():
                        self.page.locator('button[data-e2e="nav-search"]:visible').first.click(timeout=10000)
                        self.page.wait_for_timeout(800)
                        box = self.page.locator('[data-e2e="search-user-input"]:visible').first
                box.fill(q, timeout=10000)
                self._close_login_popup()  # it can pop up while the query is typed and swallow the Enter
                self.api.clear()
                with self.page.expect_response(lambda r: "/api/search/general/full" in r.url, timeout=30000):
                    box.press("Enter")
                self.page.wait_for_timeout(1500)
                self._wait_captcha()
                break
            except PlaywrightError as ex:  # TimeoutError is a PlaywrightError too; a login popup may cover the box
                self._close_login_popup()
                if attempt:
                    self.search_blocked["general"] = f"search box failed: {str(ex).splitlines()[0][:80]}"
                    return res
        self.searches += 1
        if self._blocked("/search/general/full"):
            return res
        seen = set()
        for url, body in self.api:
            if "/search/general/full" not in url or not body:
                continue
            for d in body.get("data") or []:
                for it in d.get("user_list") or []:
                    ui = it.get("user_info") or {}
                    if ui.get("unique_id") and ui["unique_id"].lower() not in seen:
                        seen.add(ui["unique_id"].lower())
                        res["users"].append({"handle": ui["unique_id"], "nickname": ui.get("nickname", ""),
                                             "followers": ui.get("follower_count", 0),
                                             "avatar": ((ui.get("avatar_thumb") or {}).get("url_list") or [""])[0]})
                it = d.get("item")
                if not it:
                    continue
                a, v = it.get("author") or {}, it.get("video") or {}
                if not a.get("uniqueId"):
                    continue
                res["videos"].append({"handle": a["uniqueId"], "nickname": a.get("nickname", ""), "desc": it.get("desc", ""),
                                      "id": it.get("id", ""), "seconds": v.get("duration", 0),
                                      "cover": v.get("cover") or v.get("originCover") or "",
                                      "play": v.get("playAddr") or v.get("downloadAddr") or ""})
                if a["uniqueId"].lower() not in seen:  # the authors of matching videos are user hits too
                    seen.add(a["uniqueId"].lower())
                    res["users"].append({"handle": a["uniqueId"], "nickname": a.get("nickname", ""),
                                         "followers": (it.get("authorStats") or {}).get("followerCount", 0),
                                         "avatar": a.get("avatarThumb") or a.get("avatarMedium") or ""})
        res["users"], res["videos"] = res["users"][:12], res["videos"][:12]
        self._search_cache[q] = res
        return res

    def _close_login_popup(self):
        """A guest sometimes gets the log in / sign up popup over the page: close it with its X (or Escape)."""
        close = self.page.locator('[data-e2e="modal-close-inner-button"]:visible, [role="dialog"] [aria-label="Close"]:visible, '
                                  '[role="dialog"] [aria-label="閉じる"]:visible, [role="dialog"] [aria-label="关闭"]:visible')
        try:
            if close.count():
                close.first.click(timeout=5000)
                self.page.wait_for_timeout(600)
                return
            # the "Log in to TikTok" box whatever its markup: click the round X at its top right corner
            spot = self.page.evaluate(LOGIN_POPUP_X_JS)
            if spot:
                self.page.mouse.click(spot["x"], spot["y"])
                self.page.wait_for_timeout(600)
            elif self.page.locator('[role="dialog"]:visible').count():
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(600)
        except PlaywrightError:
            pass

    def _search_users(self, q):
        return self._search(q)["users"]

    def _search_videos(self, q):
        return self._search(q)["videos"]

    def _same_video(self, yt_video, yt_thumb, secs, desc, cover_url):
        """How a TikTok video matches a YouTube Short: "cover" (near-same length and the same cover frame: a copy),
        "title" (the same title, about the same length: re-uploads trim a few seconds, but short titles such as a
        dish name are shared by many creators, so it needs the frames to confirm) or None."""
        # a clip cut from a long video is much shorter than it: the lengths only count for Shorts
        gap = 0 if self.long else abs(yt_video["seconds"] - secs)
        if gap <= 8 and hamming(self._hash_url(cover_url, via_page=True), yt_thumb) <= COVER_MATCH:
            return "cover"
        if gap <= 12 and similarity(clean_title(yt_video["title"]), clean_title(desc)) >= 0.6:
            return "title"
        return None

    def _download(self, url, name, referer=None):
        path = os.path.join(self._tmp, name)
        if not os.path.exists(path):
            try:
                body = self.page.request.get(url, headers={"Referer": referer} if referer else None, timeout=90000).body()
            except Exception:  # noqa: BLE001 - expired link / blocked
                return None
            with open(path, "wb") as f:
                f.write(body)
        return path

    def _yt_frames(self, yv):
        if yv["id"] not in self._yt_frame_cache:
            import yt_dlp  # the app activates its own yt-dlp engine before this runs
            out = os.path.join(self._tmp, f"yt_{yv['id']}.%(ext)s")
            try:
                with yt_dlp.YoutubeDL({"format": "bv*[height<=360]/b[height<=360]/w", "outtmpl": out, "quiet": True,
                                       "no_warnings": True, "noprogress": True}) as y:
                    path = y.prepare_filename(y.extract_info(f"https://www.youtube.com/shorts/{yv['id']}"))
            except Exception:  # noqa: BLE001 - age gate, removed video, network
                path = None
            self._yt_frame_cache[yv["id"]] = frame_hashes(self.r.ffmpeg, path) if path and os.path.exists(path) else []
        return self._yt_frame_cache[yv["id"]]

    def _profile_frame_matches(self, refs, n_tiktok=4, n_youtube=6):
        """How many of the opened profile's latest videos show footage of the channel's Shorts (most viewed first)."""
        if self.long:  # downloading whole long videos to compare frames would take far too long
            return 0
        yt = [h for yv in sorted(refs, key=lambda v: v["views"], reverse=True)[:n_youtube] for h in self._yt_frames(yv)]
        if not yt:
            return 0
        matches = 0
        for it in self._items()[:n_tiktok]:
            self.r._check_cancel()
            v = it.get("video") or {}
            url = v.get("playAddr") or v.get("downloadAddr")
            path = self._download(url, f"tt_{it.get('id')}.mp4", referer="https://www.tiktok.com/") if url else None
            if path and frames_overlap(frame_hashes(self.r.ffmpeg, path), yt) >= MIN_FRAME_OVERLAP:
                matches += 1
        return matches

    def _frames_match(self, yv, hit):
        """Download both videos and compare their frames: settles hits whose title / length only roughly agree."""
        if not hit.get("play"):
            return False
        tt = self._download(hit["play"], f"tt_{hit['id']}.mp4", referer="https://www.tiktok.com/")
        if not tt:
            return False
        return frames_overlap(frame_hashes(self.r.ffmpeg, tt), self._yt_frames(yv)) >= MIN_FRAME_OVERLAP

    # -- evaluation
    def _inspect(self, handle, c, yt_avatar, refs, yt_thumbs):
        if not self._goto(f"https://www.tiktok.com/@{handle}", settle=4500):
            return None
        u = self._user_detail()
        if not u and "/@" in self.page.url:
            # the profile sometimes comes back without its data (popup, slow load): skipping it here once let the
            # channel's own account go unnoticed, so close the popup and load it again before giving up
            self._close_login_popup()
            if self._goto(f"https://www.tiktok.com/@{handle}", settle=6000):
                u = self._user_detail()
        if not u:
            return None
        ev, score = [], 0
        name_sim = max(similarity(c["title"], u["nickname"]), similarity(c["title"], u["handle"]),
                       similarity(c.get("handle", ""), u["handle"]), similarity(c.get("handle", ""), u["nickname"]))
        # the display name alone: handles are what we guessed from the channel name, so a handle match proves little
        # (@acperformance is someone else's account)
        nick_sim = max(similarity(c["title"], u["nickname"]), similarity(c.get("handle", ""), u["nickname"]))
        if name_sim >= 0.6:
            ev.append(f"name {int(name_sim * 100)}% similar")
            score += int(name_sim * 30)
        bio = f"{u['bio']} {u['bioLink']}".lower()
        yt_handle = (c.get("handle") or "").lstrip("@").lower()
        reverse = c["id"].lower() in bio or bool(yt_handle and yt_handle in bio and "youtu" in bio)
        if reverse:
            ev.append("TikTok bio links back to this YouTube channel")
            score += 100
        av = hamming(self._hash_url(u["avatar"], via_page=True), yt_avatar)
        if av <= AVATAR_MATCH:
            ev.append(f"same avatar (hash distance {av})")
            score += 45
        elif av <= AVATAR_SIMILAR:
            ev.append(f"similar avatar (hash distance {av})")
            score += 15
        matches = titles = 0
        for it in self._items()[:16]:
            v = it.get("video") or {}
            levels = {self._same_video(yv, th, v.get("duration", 0), it.get("desc", ""), v.get("cover") or v.get("originCover"))
                      for yv, th in zip(refs, yt_thumbs)}
            if "cover" in levels:
                matches += 1
            elif "title" in levels:
                titles += 1
        if matches:
            ev.append(f"{matches} recent video(s) are copies of this channel's {'long videos' if self.long else 'Shorts'}")
            score += min(matches, 5) * 15
        return {"handle": u["handle"], "url": f"https://www.tiktok.com/@{u['handle']}", "nickname": u["nickname"],
                "followers": u["followers"], "videos": u["videos"], "nameSim": round(name_sim, 2), "nickSim": round(nick_sim, 2), "avatarDistance": av,
                "videoMatches": matches, "titleMatches": titles, "reverseLink": reverse, "score": score, "evidence": ev}

    def check(self, c):
        self.search_blocked = {}
        self.long = c.get("format") == "long"
        found_links = self._links_from_youtube(c)
        yt_avatar = self._hash_url(c.get("avatar"))
        # the latest Shorts plus the most viewed ones of all time: re-uploaders copy the hits, often months later
        refs = list({v["id"]: v for v in [*c.get("recent", [])[:20], *c.get("popular", [])]}.values())
        yt_thumbs = [self._hash_url(v.get("thumb"), crop=True) for v in refs]
        cands = {}  # handle -> why it is opened
        lookalikes = []  # name-only matches, listed but not opened

        def add(handle, why):
            h = (handle or "").lower()
            if h and h not in cands:
                cands[h] = why

        for h in found_links:
            add(h, "linked from YouTube")
        for h in self._mentioned_handles(c):
            add(h, "handle written on YouTube")
        for h in self._handle_variants(c):
            add(h, "same handle as YouTube")

        # search by the YouTube handle first, then the channel name: typed into TikTok's search box (without the @),
        # the handle also finds accounts that changed a few characters (adventureviper2 -> adventure_viper), which
        # opening tiktok.com/@<guess> misses; a Japanese handle like @えなごはん is also what people search
        yt_handle = re.sub(r"-[a-z0-9]{3}$", "", (c.get("handle") or "").lstrip("@"))
        names = [yt_handle] if yt_handle else []
        for n in (c["title"], re.sub(r"[【\[(（].*?[】\])）]", "", c["title"]).strip()):
            if n and norm_name(n) not in {norm_name(x) for x in names}:
                names.append(n)
        seen_users = set()
        same_name = 0
        for q in names[:3]:
            self.r._check_cancel()
            for u in self._search_users(q):
                h = u["handle"].lower()
                sim = max(similarity(n, u["nickname"]) for n in names) if names else 0
                sim = max(sim, max(similarity(n, u["handle"]) for n in names))
                if h in seen_users or sim < 0.5:
                    continue
                seen_users.add(h)
                av = hamming(self._hash_url(u["avatar"], via_page=True), yt_avatar)
                if av <= AVATAR_SIMILAR:
                    add(h, f"user search “{q}” + similar avatar")
                elif sim >= 0.8 and same_name < 3:  # same name, other avatar: open it and compare the videos
                    same_name += 1
                    add(h, f"user search “{q}”: same name")
                elif sim >= 0.8:
                    lookalikes.append({"handle": u["handle"], "url": f"https://www.tiktok.com/@{u['handle']}",
                                       "nickname": u["nickname"], "followers": u["followers"], "nameSim": round(sim, 2),
                                       "evidence": [f"name {int(sim * 100)}% similar, different avatar"]})

        # copies: same title / cover / frames; possible copies: a top-5 hit for the exact title with nearly the same length
        reposts, possible = {}, {}  # handle -> [video urls]
        thumb_of = {v["id"]: th for v, th in zip(refs, yt_thumbs)}
        popular = [v for v in c.get("popular", []) if v["id"] in thumb_of]
        latest = sorted(c.get("recent", [])[:20], key=lambda v: v["views"], reverse=True)
        searched = list({v["id"]: v for v in [*popular, *latest]}.values())[:TITLE_SEARCHES]
        frame_checks = 0 if self.long else FRAME_CHECKS
        for yv in searched:
            th = thumb_of[yv["id"]]
            q = clean_title(yv["title"])
            if len(q) < 6:
                continue
            self.r._check_cancel()
            for rank, hit in enumerate(self._search_videos(q)):
                link =f"https://www.tiktok.com/@{hit['handle']}/video/{hit['id']}"
                gap = abs(yv["seconds"] - hit["seconds"])
                level = self._same_video(yv, th, hit["seconds"], hit["desc"], hit["cover"])
                if level == "cover":
                    reposts.setdefault(hit["handle"].lower(), []).append(link)
                elif level == "title" or (rank < 5 and gap <= 20 and similarity(q, clean_title(hit["desc"])) >= 0.35):
                    if frame_checks:  # a shared title is not proof: compare the footage
                        frame_checks -= 1
                        if self._frames_match(yv, hit):
                            reposts.setdefault(hit["handle"].lower(), []).append(link)
                            continue
                    if level == "title" or (rank < 5 and yv["seconds"] >= 20 and gap <= 8):
                        possible.setdefault(hit["handle"].lower(), []).append(link)
                elif rank < 5 and yv["seconds"] >= 20 and gap <= 8:
                    possible.setdefault(hit["handle"].lower(), []).append(link)
        for h in reposts:
            add(h, "posted a copy of a long video" if self.long else "posted a copy of a Short")
        for h in list(possible)[:4]:
            add(h, "possible copy (same title as a long video)" if self.long else
                "possible copy (top result for a Short's title, same length)")

        results = []
        frame_checks = PROFILE_FRAME_CHECKS
        for h, why in list(cands.items())[:12]:
            self.r._check_cancel()
            info = self._inspect(h, c, yt_avatar, refs, yt_thumbs)
            if not info:
                continue
            info["why"] = why
            if why == "linked from YouTube":
                info["evidence"].insert(0, "YouTube channel links to this TikTok")
                info["score"] += 100
            if why == "handle written on YouTube":
                info["evidence"].insert(0, "this handle is written on the YouTube channel")
                info["score"] += 40
            # same name (or a handle the channel writes) but nothing else in common yet: compare the actual footage
            if (not info["videoMatches"] and frame_checks and info["avatarDistance"] > AVATAR_MATCH
                    and (info["nickSim"] >= 0.8 or why == "handle written on YouTube" or info["titleMatches"])):
                frame_checks -= 1
                n = self._profile_frame_matches(refs)
                if n:
                    info["videoMatches"] = n
                    info["frameMatches"] = n
                    info["evidence"].append(f"{n} of its latest videos show the same footage as this channel's Shorts")
                    info["score"] += 30 * n
            if h in reposts:
                info["videoMatches"] = max(info["videoMatches"], len(reposts[h]))
                info["evidence"].append(f"found by video search: {len(reposts[h])} copied "
                                        + ("long video(s)" if self.long else "Short(s)"))
                info["examples"] = reposts[h][:3]
                info["score"] += 15 * len(reposts[h])
            elif h in possible:
                info["possibleCopies"] = len(possible[h])
                info["evidence"].append(f"possible copy: same title as a long video ({len(possible[h])})" if self.long else
                                        f"possible copy: top result for a Short's title with the same length ({len(possible[h])})")
                info["examples"] = possible[h][:3]
            results.append(info)

        verdict = "none"
        for r in results:
            linked = r["why"] == "linked from YouTube" or r["reverseLink"]
            written = r["why"] == "handle written on YouTube"
            same_av, near_av = r["avatarDistance"] <= AVATAR_MATCH, r["avatarDistance"] <= AVATAR_SIMILAR
            if (linked or (same_av and (r["nameSim"] >= 0.6 or r["videoMatches"] >= 1))
                    or (r.get("frameMatches") and (r["nickSim"] >= 0.8 or written))
                    or (written and (r["nickSim"] >= 0.6 or near_av or r["videoMatches"] >= 1))):
                r["verdict"] = "exists"
            elif (same_av or (near_av and (r["nameSim"] >= 0.6 or r["videoMatches"] >= 1))
                  or (r["nickSim"] >= 0.8 and r["videoMatches"] >= 3)):  # same name posting many of the Shorts
                r["verdict"] = "likely"
            elif (near_av or (r["nickSim"] >= 0.8 and r["videoMatches"] >= 1)
                  or (r["nickSim"] >= 0.95 and r["followers"] >= 1000)):  # the very same display name, a real account
                r["verdict"] = "suspect"
                if r["nickSim"] >= 0.95 and not (near_av or r["videoMatches"]):
                    r["evidence"].append("same name with a real following, nothing else in common — check it is the same creator")
            elif r["videoMatches"] >= 1 or r.get("possibleCopies", 0) >= 2:
                r["verdict"] = "reup"
                r["evidence"].append("re-uploads this channel's videos (different name and avatar: probably not the owner)"
                                     if r["videoMatches"] else "may re-upload this channel's videos — check the example links")
            else:
                r["verdict"] = "none"
                if r.get("possibleCopies"):  # one unconfirmed hit: listed for a manual look, not a verdict
                    lookalikes.append({"handle": r["handle"], "url": r["url"], "nickname": r["nickname"],
                                       "followers": r["followers"], "nameSim": r["nameSim"], "examples": r.get("examples", []),
                                       "evidence": ["one video with a similar title and length — not confirmed by the footage"]})
            if VERDICT_ORDER[r["verdict"]] < VERDICT_ORDER[verdict]:
                verdict = r["verdict"]
        results = [r for r in results if r["verdict"] != "none"]
        results.sort(key=lambda r: (VERDICT_ORDER[r["verdict"]], -r["score"]))
        lookalikes = [x for x in lookalikes if x["handle"].lower() not in {r["handle"].lower() for r in results}]
        out = {"verdict": verdict, "candidates": results[:6], "lookalikes": lookalikes[:5],
               "checked": len(cands), "youtubeLinks": found_links}
        if self.search_blocked:
            # nothing found, but TikTok refused some searches: "none" would be a guess
            out["blocked"] = "; ".join(f"TikTok search: {v}" for v in dict.fromkeys(self.search_blocked.values()))
            if verdict == "none":
                out["verdict"] = "unverified"
        return out
