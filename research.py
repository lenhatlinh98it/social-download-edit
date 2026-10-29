"""Niche & channel research for TikTok re-posting.

1. Hot niches: for each candidate niche, the market's most-viewed Shorts of the last 14 days (YouTube Data API)
   are scored by view velocity, how many of their channels are new, and how many break 100K views.
2. Promising channels: Shorts channels found in a niche, kept when created in the last 24 months, 5K-500K
   subscribers and >= 70 % Shorts, then ranked by growth (views per day, recent views, upload pace).
3. TikTok check: links from YouTube (About page + descriptions), handle variants, user search by name, video
   search by title; every candidate is opened (bio link back to YouTube, avatar hash, matching videos) and the
   channel gets a verdict: exists / likely / suspect / none. Runs in the Chrome Portable "Research" profile.
"""
import difflib
import json
import math
import os
import re
import statistics
import subprocess
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
NICHE_CACHE_HOURS = 24
AVATAR_MATCH = 6  # dHash bits (of 64): the same picture re-encoded is 0-6, unrelated pictures ~25-35
AVATAR_SIMILAR = 10
COVER_MATCH = 12

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


def clean_title(t):
    t = re.sub(r"#\S+", " ", t or "")
    t = re.sub(r"[|｜/].*$", "", t)
    return re.sub(r"\s+", " ", t).strip()[:60]


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def dhash(ffmpeg, data, crop_vertical=False):
    """64-bit difference hash of an image (ffmpeg does the decoding, so no imaging library is needed)."""
    vf = ("crop=ih*9/16:ih," if crop_vertical else "") + "scale=9:8:flags=area,format=gray"
    try:
        r = subprocess.run([ffmpeg, "-v", "error", "-i", "pipe:0", "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
                           input=data, capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    px = r.stdout
    if len(px) != 72:
        return None
    return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])


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

    def search_shorts(self, q, market, days, order="viewCount", max_results=50):
        m = MARKETS[market]
        after = (_now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        d = self.call("search", cost=100, part="snippet", type="video", videoDuration="short", order=order,
                      regionCode=m["region"], relevanceLanguage=m["lang"], q=q, publishedAfter=after,
                      maxResults=max_results)
        return [it["id"]["videoId"] for it in d.get("items", []) if it.get("id", {}).get("videoId")]

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
    def find_channels(self, market, keywords):
        ids = []
        for i, q in enumerate(keywords):
            self._check_cancel()
            self.progress("channels", f"Searching Shorts for “{q}”…", 0.05 + 0.1 * i)
            ids += self.yt.search_shorts(q, market, CHANNEL_WINDOW_DAYS)
            ids += self.yt.search_shorts(q, market, CHANNEL_WINDOW_DAYS, order="relevance")
        vids = self.yt.videos(list(dict.fromkeys(ids)))
        chans = self.yt.channels([v["channelId"] for v in vids])
        now = _now()
        region, script = MARKETS[market]["region"], MARKET_SCRIPTS.get(MARKETS[market]["lang"])
        stage1 = [c for c in chans.values() if (now - _parse_time(c["created"])).days <= MAX_AGE_DAYS
                  and c["subs"] is not None and MIN_SUBS <= c["subs"] <= MAX_SUBS
                  and (not c["country"] or c["country"] == region)]
        self.log("info", f"{len(chans)} channels found, {len(stage1)} are ≤24 months old with {MIN_SUBS:,}-{MAX_SUBS:,} "
                         f"subscribers and not registered in another country")

        def enrich(c):
            tail = c["id"][2:]
            shorts, longs = self.yt.playlist_total("UUSH" + tail), self.yt.playlist_total("UULF" + tail)
            c["shorts"], c["longs"] = shorts, longs
            c["shortsShare"] = round(shorts / (shorts + longs), 2) if shorts + longs else 0
            if c["shortsShare"] < MIN_SHORTS_SHARE:
                return c
            recent = self.yt.videos(self.yt.playlist_ids("UUSH" + tail, 20))
            c["recent"] = recent
            if script and recent and sum(bool(script.search(v["title"])) for v in recent) / len(recent) < MIN_SCRIPT_SHARE:
                c["offMarket"] = True
                return c
            days = max((now - _parse_time(c["created"])).days, 1)
            last30 = [v for v in recent if (now - _parse_time(v["published"])).days <= 30]
            c["ageDays"] = days
            c["viewsPerDay"] = round(c["views"] / days)
            c["subsPerDay"] = round(c["subs"] / days, 1)
            c["avgRecentViews"] = round(statistics.mean(v["views"] for v in recent)) if recent else 0
            c["uploads30"] = len(last30)
            c["lastUpload"] = recent[0]["published"][:10] if recent else ""
            c["score"] = round(math.log10(1 + c["viewsPerDay"]) * 10 + math.log10(1 + c["avgRecentViews"]) * 8
                               + min(c["uploads30"], 30) / 3 + math.log10(1 + c["subsPerDay"]) * 5, 1)
            return c

        out = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for i, c in enumerate(pool.map(enrich, stage1)):
                self.progress("channels", f"Checking channels {i + 1}/{len(stage1)}", 0.3 + 0.2 * (i + 1) / max(len(stage1), 1))
                if c.get("shortsShare", 0) >= MIN_SHORTS_SHARE and c.get("uploads30") and not c.get("offMarket"):
                    out.append(c)
        out.sort(key=lambda c: c["score"], reverse=True)
        off = sum(1 for c in stage1 if c.get("offMarket"))
        self.log("info", f"{len(out)} promising Shorts channels (≥{int(MIN_SHORTS_SHARE * 100)} % Shorts, active in the last 30 days"
                         + (f"; {off} dropped for titles not in the market's language" if off else "") + ")")
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
        c["recent"] = self.yt.videos(self.yt.playlist_ids("UUSH" + cid[2:], 20) or self.yt.playlist_ids("UU" + cid[2:], 20))
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
            try:
                checker = _TikTokChecker(page, self)
                free = 0
                for i, c in enumerate(channels):
                    self._check_cancel()
                    self.progress("tiktok", f"TikTok check {i + 1}/{len(channels)}: {c['title']}", 0.5 + 0.5 * i / len(channels))
                    try:
                        c["tiktok"] = checker.check(c)
                    except Cancelled:
                        raise
                    except Exception as ex:  # noqa: BLE001 - one channel failing must not stop the run
                        c["tiktok"] = {"verdict": "error", "error": f"{type(ex).__name__}: {ex}", "candidates": []}
                    self.log("info" if c["tiktok"]["verdict"] != "error" else "warning",
                             f"{c['title']}: TikTok {c['tiktok']['verdict']}",
                             "\n".join(f"{x['verdict']} {x['url']} — {'; '.join(x['evidence'])}" for x in c["tiktok"].get("candidates", [])))
                    free += c["tiktok"]["verdict"] == "none"
                    if stop_after and free >= stop_after:
                        break
            finally:
                page.close()
                browser.close()
        return channels


VERDICT_ORDER = {"exists": 0, "likely": 1, "suspect": 2, "reup": 3, "none": 4, "error": 5}


class _TikTokChecker:
    def __init__(self, page, research):
        self.page = page
        self.r = research
        self.api = []
        page.on("response", self._on_response)
        self._last = 0.0
        self._hashes = {}

    def _on_response(self, resp):
        if "tiktok.com/api/" in resp.url and any(k in resp.url for k in ("/search/user/full", "/search/item/full", "/post/item_list")):
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
        out = []
        if h:
            out += [h, h.replace("-", "_"), re.sub(r"[-_.]", "", h)]
        title = norm_name(c.get("title"))
        if re.fullmatch(r"[a-z0-9]{3,24}", title or ""):
            out.append(title)
        return [v for v in dict.fromkeys(out) if re.fullmatch(r"[a-z0-9_.]{2,24}", v)][:4]

    def _search_users(self, q):
        out = []
        if not self._goto(f"https://www.tiktok.com/search/user?q={urllib.parse.quote(q)}"):
            return out
        for url, body in self.api:
            if "/search/user/full" in url and body:
                for it in body.get("user_list") or []:
                    ui = it.get("user_info") or {}
                    if ui.get("unique_id"):
                        out.append({"handle": ui["unique_id"], "nickname": ui.get("nickname", ""),
                                    "followers": ui.get("follower_count", 0),
                                    "avatar": ((ui.get("avatar_thumb") or {}).get("url_list") or [""])[0]})
        return out[:10]

    def _search_videos(self, q):
        out = []
        if not self._goto(f"https://www.tiktok.com/search/video?q={urllib.parse.quote(q)}"):
            return out
        for url, body in self.api:
            if "/search/item/full" in url and body:
                for it in body.get("item_list") or []:
                    a, v = it.get("author") or {}, it.get("video") or {}
                    if a.get("uniqueId"):
                        out.append({"handle": a["uniqueId"], "nickname": a.get("nickname", ""), "desc": it.get("desc", ""),
                                    "id": it.get("id", ""), "seconds": v.get("duration", 0),
                                    "cover": v.get("cover") or v.get("originCover") or ""})
        return out[:12]

    def _same_video(self, yt_video, yt_thumb, secs, desc, cover_url):
        """A TikTok video is a copy of a YouTube Short: near-same length and the same title or cover frame."""
        if abs(yt_video["seconds"] - secs) > 8:
            return False
        if abs(yt_video["seconds"] - secs) <= 1 and similarity(clean_title(yt_video["title"]), clean_title(desc)) >= 0.55:
            return True
        return hamming(self._hash_url(cover_url, via_page=True), yt_thumb) <= COVER_MATCH

    # -- evaluation
    def _inspect(self, handle, c, yt_avatar, yt_thumbs):
        if not self._goto(f"https://www.tiktok.com/@{handle}", settle=4500):
            return None
        u = self._user_detail()
        if not u:
            return None
        ev, score = [], 0
        name_sim = max(similarity(c["title"], u["nickname"]), similarity(c["title"], u["handle"]),
                       similarity(c.get("handle", ""), u["handle"]), similarity(c.get("handle", ""), u["nickname"]))
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
        matches = 0
        for it in self._items()[:16]:
            v = it.get("video") or {}
            if any(self._same_video(yv, th, v.get("duration", 0), it.get("desc", ""), v.get("cover") or v.get("originCover"))
                   for yv, th in zip(c.get("recent", []), yt_thumbs)):
                matches += 1
        if matches:
            ev.append(f"{matches} recent video(s) are copies of this channel's Shorts")
            score += min(matches, 5) * 15
        return {"handle": u["handle"], "url": f"https://www.tiktok.com/@{u['handle']}", "nickname": u["nickname"],
                "followers": u["followers"], "videos": u["videos"], "nameSim": round(name_sim, 2), "avatarDistance": av,
                "videoMatches": matches, "reverseLink": reverse, "score": score, "evidence": ev}

    def check(self, c):
        found_links = self._links_from_youtube(c)
        yt_avatar = self._hash_url(c.get("avatar"))
        recent = c.get("recent", [])[:20]
        yt_thumbs = [self._hash_url(v.get("thumb"), crop=True) for v in recent]
        cands = {}  # handle -> why it is opened
        lookalikes = []  # name-only matches, listed but not opened

        def add(handle, why):
            h = (handle or "").lower()
            if h and h not in cands:
                cands[h] = why

        for h in found_links:
            add(h, "linked from YouTube")
        for h in self._handle_variants(c):
            add(h, "same handle as YouTube")

        names = [c["title"]]
        short_title = re.sub(r"[【\[(（].*?[】\])）]", "", c["title"]).strip()
        if short_title and short_title != c["title"]:
            names.append(short_title)
        seen_users = set()
        for q in names[:2]:
            self.r._check_cancel()
            for u in self._search_users(q):
                h = u["handle"].lower()
                sim = max(similarity(q, u["nickname"]), similarity(q, u["handle"]))
                if h in seen_users or sim < 0.5:
                    continue
                seen_users.add(h)
                av = hamming(self._hash_url(u["avatar"], via_page=True), yt_avatar)
                if av <= AVATAR_SIMILAR:
                    add(h, f"user search “{q}” + similar avatar")
                elif sim >= 0.8:
                    lookalikes.append({"handle": u["handle"], "url": f"https://www.tiktok.com/@{u['handle']}",
                                       "nickname": u["nickname"], "followers": u["followers"], "nameSim": round(sim, 2),
                                       "evidence": [f"name {int(sim * 100)}% similar, different avatar"]})

        # copies: same length and title/cover; possible copies: a top-5 hit for the exact title with nearly the same length
        reposts, possible = {}, {}  # handle -> [video urls]
        for yv, th in sorted(zip(recent, yt_thumbs), key=lambda p: p[0]["views"], reverse=True)[:3]:
            q = clean_title(yv["title"])
            if len(q) < 6:
                continue
            self.r._check_cancel()
            for rank, hit in enumerate(self._search_videos(q)):
                link = f"https://www.tiktok.com/@{hit['handle']}/video/{hit['id']}"
                if self._same_video(yv, th, hit["seconds"], hit["desc"], hit["cover"]):
                    reposts.setdefault(hit["handle"].lower(), []).append(link)
                elif rank < 5 and yv["seconds"] >= 20 and abs(yv["seconds"] - hit["seconds"]) <= 8:
                    possible.setdefault(hit["handle"].lower(), []).append(link)
        for h in reposts:
            add(h, "posted a copy of a Short")
        for h in list(possible)[:4]:
            add(h, "possible copy (top result for a Short's title, same length)")

        results = []
        for h, why in list(cands.items())[:10]:
            self.r._check_cancel()
            info = self._inspect(h, c, yt_avatar, yt_thumbs)
            if not info:
                continue
            info["why"] = why
            if why == "linked from YouTube":
                info["evidence"].insert(0, "YouTube channel links to this TikTok")
                info["score"] += 100
            if h in reposts:
                info["videoMatches"] = max(info["videoMatches"], len(reposts[h]))
                info["evidence"].append(f"found by video search: {len(reposts[h])} copied Short(s)")
                info["examples"] = reposts[h][:3]
                info["score"] += 15 * len(reposts[h])
            elif h in possible:
                info["possibleCopies"] = len(possible[h])
                info["evidence"].append(f"possible copy: top result for a Short's title with the same length ({len(possible[h])})")
                info["examples"] = possible[h][:3]
            results.append(info)

        verdict = "none"
        for r in results:
            linked = r["why"] == "linked from YouTube" or r["reverseLink"]
            same_av, near_av = r["avatarDistance"] <= AVATAR_MATCH, r["avatarDistance"] <= AVATAR_SIMILAR
            if linked or (same_av and (r["nameSim"] >= 0.6 or r["videoMatches"] >= 1)):
                r["verdict"] = "exists"
            elif same_av or (near_av and (r["nameSim"] >= 0.6 or r["videoMatches"] >= 1)):
                r["verdict"] = "likely"
            elif near_av or (r["nameSim"] >= 0.8 and r["videoMatches"] >= 1):
                r["verdict"] = "suspect"
            elif r["videoMatches"] >= 1 or r.get("possibleCopies"):
                r["verdict"] = "reup"
                r["evidence"].append("re-uploads this channel's videos (different name and avatar: probably not the owner)"
                                     if r["videoMatches"] else "may re-upload this channel's videos — check the example links")
            else:
                r["verdict"] = "none"
            if VERDICT_ORDER[r["verdict"]] < VERDICT_ORDER[verdict]:
                verdict = r["verdict"]
        results = [r for r in results if r["verdict"] != "none"]
        results.sort(key=lambda r: (VERDICT_ORDER[r["verdict"]], -r["score"]))
        lookalikes = [x for x in lookalikes if x["handle"].lower() not in {r["handle"].lower() for r in results}]
        return {"verdict": verdict, "candidates": results[:6], "lookalikes": lookalikes[:5],
                "checked": len(cands), "youtubeLinks": found_links}
