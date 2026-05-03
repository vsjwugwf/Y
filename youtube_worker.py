"""
youtube_worker.py – موتور قدرتمند یوتیوب با ۲۰+ متد و fallback هوشمند
تمامی عملیات جستجو، دریافت اطلاعات و دانلود ویدیو با چندین روش مستقل انجام می‌شود.
هر تابع عمومی خروجی خود را به همراه نام متد موفق برمی‌گرداند.
"""

import json
import os
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import requests

import settings
from utils import get_logger, download_file

_log = get_logger("youtube_worker")

# ──────────────────────────── ابزارهای کمکی داخلی ────────────────────────────

def _parse_innertube_renderer(data: dict, limit: int = 10) -> List[Dict[str, Any]]:
    """
    تحلیل پاسخ innertube یا ytInitialData برای استخراج videoRendererها.
    خروجی: لیستی از دیکشنری شامل video_id, title, thumbnail_url, duration
    """
    results = []
    contents = []
    try:
        primary = (data.get("contents", {})
                       .get("twoColumnSearchResultsRenderer", {})
                       .get("primaryContents", {})
                       .get("sectionListRenderer", {})
                       .get("contents", []))
        for section in primary:
            items = section.get("itemSectionRenderer", {}).get("contents", [])
            contents.extend(items)
        if not contents:
            rich = (data.get("contents", {})
                        .get("twoColumnSearchResultsRenderer", {})
                        .get("primaryContents", {})
                        .get("richGridRenderer", {})
                        .get("contents", []))
            for item in rich:
                video = item.get("richItemRenderer", {}).get("content", {}).get("videoRenderer")
                if video:
                    contents.append({"videoRenderer": video})

        for item in contents:
            if len(results) >= limit:
                break
            video = item.get("videoRenderer")
            if not video:
                continue
            video_id = video.get("videoId")
            if not video_id:
                continue
            title = "".join(run.get("text", "") for run in video.get("title", {}).get("runs", []))
            thumbs = video.get("thumbnail", {}).get("thumbnails", [])
            thumb_url = thumbs[0]["url"] if thumbs else None
            duration_text = video.get("lengthText", {}).get("simpleText", "")
            results.append({
                "video_id": video_id,
                "title": title or None,
                "thumbnail_url": thumb_url,
                "duration": duration_text,
            })
    except Exception as e:
        _log.warning(f"خطا در تجزیه renderer: {e}")
    return results


def _extract_video_id_from_url(url: str) -> Optional[str]:
    """استخراج شناسه ویدیو از URLهای رایج یوتیوب"""
    pattern = r'(?:v=|/)([0-9A-Za-z_-]{11})(?:[?&/#]|$)'
    parsed = requests.utils.urlparse(url)
    if parsed.netloc in ('youtu.be', 'www.youtu.be'):
        vid = parsed.path.lstrip('/')
        if re.match(r'^[0-9A-Za-z_-]{11}$', vid):
            return vid
    match = re.search(pattern, url)
    return match.group(1) if match else None


def _find_downloaded_file(video_id: str, save_dir: str, ext: str = ".mp4") -> Optional[str]:
    """پیدا کردن فایل دانلود شده با پیشوند video_id در پوشه"""
    for fname in os.listdir(save_dir):
        if fname.startswith(video_id) and fname.endswith(ext):
            return os.path.join(save_dir, fname)
    return None


# ──────────────────────── موتور fallback عمومی ──────────────────────────────

def run_with_fallback(
    chain: List[str],
    operation_func: Callable[[str, Dict[str, Any]], Any],
    start_method: Optional[str] = None,
    **kwargs
) -> Tuple[Any, Optional[str]]:
    """
    اجرای یک عملیات روی زنجیره‌ای از متدها به ترتیب، تا اولین موفقیت.
    Args:
        chain: لیست نام متدهای مجاز.
        operation_func: تابعی با امضای (method_name, kwargs) که نتیجه یا None برمی‌گرداند.
        start_method: در صورت مشخص بودن، زنجیره از این متد به بعد ادامه پیدا می‌کند.
        kwargs: آرگومان‌های اضافی به operation_func داده می‌شود.
    Returns:
        (نتیجه, نام متد موفق) یا (None, None) در صورت شکست کامل.
    """
    # اگر start_method داده شده، ایندکس آن را پیدا کن و زنجیره را از آن ببُر
    if start_method:
        try:
            idx = chain.index(start_method)
            chain = chain[idx:]
        except ValueError:
            _log.warning(f"start_method={start_method} در زنجیره نیست، از اول شروع می‌شود.")

    for method in chain:
        _log.info(f"تلاش با متد: {method}")
        try:
            result = operation_func(method, kwargs)
            if result is not None:
                _log.info(f"متد {method} موفق بود.")
                return result, method
        except Exception as e:
            _log.warning(f"متد {method} شکست خورد: {e}")
    _log.error("تمام متدهای زنجیره ناموفق بودند.")
    return None, None


# ──────────────────────────── متدهای جستجو ────────────────────────────────

def _search_piped(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    try:
        import requests as req
        url = "https://pipedapi.kavin.rocks/search"
        params = {"q": query, "filter": "videos"}
        resp = req.get(url, params=params, timeout=15,
                       headers={"User-Agent": settings.USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        results = []
        for item in items:
            if item.get("type") != "video":
                continue
            vid = item.get("videoId")
            title = item.get("title")
            duration = item.get("duration")  # ثانیه
            thumb = item.get("thumbnail")
            # fallback thumbnail
            if not thumb:
                thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
            uploader = item.get("uploaderName")
            results.append({
                "video_id": vid,
                "title": title,
                "duration": duration,
                "thumbnail_url": thumb,
                "uploader": uploader,
            })
            if len(results) >= limit:
                break
        return results if results else None
    except Exception:
        return None


def _search_newpipe(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    # تلاش برای استفاده از کتابخانهٔ newpipe
    try:
        # ممکن است کتابخانه به نام newpipe_extractor باشد
        from newpipe_extractor import search
        videos = search(query)
        results = []
        for v in videos[:limit]:
            results.append({
                "video_id": v.get("id"),
                "title": v.get("title"),
                "duration": v.get("duration"),
                "thumbnail_url": v.get("thumbnail"),
                "uploader": v.get("uploader"),
            })
        return results if results else None
    except ImportError:
        # fallback با subprocess به یک اسکریپت خارجی
        pass
    except Exception:
        return None

    # Fallback subprocess (اختیاری، در صورتی که newpipe-extractor-cli نصب باشد)
    try:
        cmd = ["npx", "-y", "newpipe-extractor-cli", "search", query, "--limit", str(limit)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        results = []
        for item in data.get("items", [])[:limit]:
            results.append({
                "video_id": item.get("id"),
                "title": item.get("title"),
                "duration": item.get("duration"),
                "thumbnail_url": item.get("thumbnail"),
                "uploader": item.get("uploader"),
            })
        return results if results else None
    except Exception:
        return None


def _search_innertube2(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    try:
        from innertube import InnerTube
        client = InnerTube("WEB")
        data = client.search(query)
        # استفاده از پارسر مشترک
        results = _parse_innertube_renderer(data, limit)
        # افزودن uploader در صورت وجود (معمولاً خالی است)
        for r in results:
            r.setdefault("uploader", None)
        return results if results else None
    except Exception:
        return None


def _search_scrapetube(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    try:
        import scrapetube
        videos = scrapetube.get_search(query, limit=limit)
        results = []
        for v in videos:
            vid = v.get("videoId") if isinstance(v, dict) else getattr(v, "videoId", None)
            if vid:
                results.append({
                    "video_id": vid,
                    "title": None,
                    "thumbnail_url": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
                    "duration": None,
                    "uploader": None,
                })
        return results if results else None
    except Exception:
        return None


def _search_youtube_search_python(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    try:
        from youtubesearchpython import VideosSearch
        search = VideosSearch(query, limit=limit)
        data = search.result()["result"]
        results = []
        for item in data:
            vid = item.get("id")
            results.append({
                "video_id": vid,
                "title": item.get("title"),
                "duration": item.get("duration"),
                "thumbnail_url": item.get("thumbnails", [{}])[0].get("url"),
                "uploader": item.get("channel", {}).get("name"),
            })
        return results if results else None
    except ImportError:
        # برخی نصب‌ها نام کتابخانه متفاوت است
        try:
            from youtube_search import YoutubeSearch
            search = YoutubeSearch(query, max_results=limit)
            results = []
            for v in search.to_dict():
                vid = v.get("id")
                results.append({
                    "video_id": vid,
                    "title": v.get("title"),
                    "duration": v.get("duration"),
                    "thumbnail_url": v.get("thumbnails", [None])[0] if v.get("thumbnails") else None,
                    "uploader": v.get("channel"),
                })
            return results if results else None
        except Exception:
            return None
    except Exception:
        return None


def _search_py_yt_search(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    try:
        import py_yt_search
        s = py_yt_search.Search(query, limit=limit)
        videos = s.videos()
        results = []
        for v in videos[:limit]:
            results.append({
                "video_id": v.id,
                "title": v.title,
                "duration": getattr(v, "duration", None),
                "thumbnail_url": v.thumbnails[0] if getattr(v, "thumbnails", None) else None,
                "uploader": getattr(v, "author", None) or getattr(v, "channel", None),
            })
        return results if results else None
    except Exception:
        return None


def _search_duckduckgo(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    try:
        from duckduckgo_search import DDGS
        ddgs = DDGS()
        results = []
        for r in ddgs.videos(query, max_results=limit):
            vid = _extract_video_id_from_url(r.get("content", ""))
            if not vid:
                continue
            results.append({
                "video_id": vid,
                "title": r.get("title"),
                "duration": None,  # DuckDuckGo duration معمولاً رشته‌ای است
                "thumbnail_url": r.get("image"),
                "uploader": r.get("uploader"),
            })
        return results if results else None
    except Exception:
        return None


def _search_html_parse(query: str, limit: int) -> Optional[List[Dict[str, Any]]]:
    try:
        from bs4 import BeautifulSoup
        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}"
        resp = requests.get(url, headers={"User-Agent": settings.USER_AGENT}, timeout=15)
        resp.raise_for_status()
        match = re.search(r"var ytInitialData\s*=\s*(\{.+?\});", resp.text)
        if not match:
            return None
        data = json.loads(match.group(1))
        results = _parse_innertube_renderer(data, limit)
        for r in results:
            r.setdefault("uploader", None)
        return results if results else None
    except Exception:
        return None


# ──────────────────────── متدهای دریافت اطلاعات ────────────────────────────

def _info_piped(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        import requests as req
        url = f"https://pipedapi.kavin.rocks/video/{video_id}"
        resp = req.get(url, timeout=15, headers={"User-Agent": settings.USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        return {
            "title": data.get("title"),
            "duration": data.get("duration"),
            "view_count": data.get("views"),
            "thumbnail": data.get("thumbnail"),
            "author": data.get("uploaderName"),
            "description": data.get("description"),
        }
    except Exception:
        return None


def _info_newpipe(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        from newpipe_extractor import get_video_info
        info = get_video_info(video_id)
        return {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "view_count": info.get("view_count"),
            "thumbnail": info.get("thumbnail"),
            "author": info.get("uploader"),
            "description": info.get("description"),
        }
    except ImportError:
        # fallback subprocess
        try:
            cmd = ["npx", "-y", "newpipe-extractor-cli", "video", video_id]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                return None
            data = json.loads(proc.stdout)
            return {
                "title": data.get("title"),
                "duration": data.get("duration"),
                "view_count": data.get("view_count"),
                "thumbnail": data.get("thumbnail"),
                "author": data.get("uploader"),
                "description": data.get("description"),
            }
        except Exception:
            return None
    except Exception:
        return None


def _info_innertube2(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        from innertube import InnerTube
        client = InnerTube("WEB")
        data = client.video(video_id)
        video = data.get("videoDetails", {})
        if not video:
            return None
        micro = video.get("microformat", {}).get("playerMicroformatRenderer", {})
        thumbnails = video.get("thumbnail", {}).get("thumbnails", [])
        thumb_url = thumbnails[-1]["url"] if thumbnails else None
        duration = int(video.get("lengthSeconds", 0)) if video.get("lengthSeconds") else None
        return {
            "title": video.get("title"),
            "duration": duration,
            "view_count": int(video.get("viewCount", 0)) if video.get("viewCount") else None,
            "thumbnail": thumb_url,
            "author": video.get("author"),
            "description": micro.get("description", {}).get("simpleText", ""),
        }
    except Exception:
        return None


def _info_oembed(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        url = f"https://www.youtube.com/oembed?url=https://youtube.com/watch?v={video_id}&format=json"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            "title": data.get("title"),
            "duration": None,
            "view_count": None,
            "thumbnail": data.get("thumbnail_url"),
            "author": data.get("author_name"),
            "description": None,
        }
    except Exception:
        return None


def _info_yt_dlp(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        import yt_dlp
        url = f"https://www.youtube.com/watch?v={video_id}"
        ydl_opts = {"quiet": True, "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None
            duration = info.get("duration")
            try:
                duration = int(duration) if duration else None
            except (ValueError, TypeError):
                duration = None
            return {
                "title": info.get("title"),
                "duration": duration,
                "view_count": info.get("view_count"),
                "thumbnail": info.get("thumbnail"),
                "author": info.get("uploader") or info.get("channel"),
                "description": info.get("description"),
            }
    except Exception:
        return None


def _info_py_yt_search(video_id: str) -> Optional[Dict[str, Any]]:
    try:
        import py_yt_search
        v = None
        try:
            v = py_yt_search.Video.get(video_id)
        except TypeError:
            s = py_yt_search.Search(video_id, limit=1)
            videos = s.videos()
            if videos:
                v = videos[0]
        if not v:
            return None
        return {
            "title": v.title,
            "duration": getattr(v, "duration", None),
            "view_count": getattr(v, "views", None),
            "thumbnail": v.thumbnails[0] if getattr(v, "thumbnails", None) else None,
            "author": getattr(v, "author", None) or getattr(v, "channel", None),
            "description": getattr(v, "description", None),
        }
    except Exception:
        return None


# ──────────────────────── متدهای دانلود ویدیو ──────────────────────────────

def _download_cobalt(video_id: str, save_dir: str) -> Optional[str]:
    # روش اول: استفاده از کتابخانه pybalt
    try:
        import pybalt
        return pybalt.download(video_id, output_dir=save_dir)
    except ImportError:
        pass
    except Exception:
        return None

    # روش دوم: درخواست مستقیم به Cobalt API
    try:
        url = "https://api.cobalt.tools/api/json"
        payload = {
            "url": f"https://youtube.com/watch?v={video_id}",
            "vCodec": "h264",
            "aFormat": "mp3",
        }
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        stream_url = data.get("url") or data.get("streamUrl")
        if not stream_url:
            return None
        # گاهی cobalt خروجی مستقیم می‌دهد، در غیر این صورت redirect است
        if isinstance(stream_url, dict):
            stream_url = stream_url.get("url")
        if not stream_url:
            return None
        return download_file(stream_url, save_dir, filename=f"{video_id}.mp4", timeout=120)
    except Exception:
        return None


def _download_yt_dlp_pot(video_id: str, save_dir: str) -> Optional[str]:
    # تلاش برای دریافت PO Token از سرور محلی
    try:
        token_resp = requests.post(
            "http://localhost:4416/token",
            json={"url": f"https://www.youtube.com/watch?v={video_id}"},
            timeout=5,
        )
        token_resp.raise_for_status()
        pot_token = token_resp.json().get("token")
    except Exception:
        # بدون توکن نمی‌توان ادامه داد
        return None

    try:
        import yt_dlp
        ydl_opts = {
            "format": f"bestvideo[height<={settings.MAX_VIDEO_HEIGHT}]+bestaudio/best[height<={settings.MAX_VIDEO_HEIGHT}]",
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(save_dir, f"{video_id}.%(ext)s"),
            "quiet": True,
            "noplaylist": True,
            "ffmpeg_location": settings.FFMPEG_PATH,
            "extractor_args": {"youtubetab": {"pot": pot_token}},
            "retries": 3,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        # یافتن فایل
        result = _find_downloaded_file(video_id, save_dir, ".mp4") or \
                 _find_downloaded_file(video_id, save_dir, ".webm") or \
                 _find_downloaded_file(video_id, save_dir, ".mkv")
        return result
    except Exception:
        return None


def _download_newpipe(video_id: str, save_dir: str) -> Optional[str]:
    # استخراج لینک استریم با newpipe_extractor (در صورت موجود بودن)
    try:
        from newpipe_extractor import get_stream_url
        stream_url = get_stream_url(video_id)
        if not stream_url:
            return None
        return download_file(stream_url, save_dir, filename=f"{video_id}.mp4", timeout=120)
    except ImportError:
        # fallback subprocess (اختیاری)
        try:
            cmd = ["npx", "-y", "newpipe-extractor-cli", "stream", video_id, "--format", "mp4"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                return None
            url = proc.stdout.strip()
            if url:
                return download_file(url, save_dir, filename=f"{video_id}.mp4", timeout=120)
        except Exception:
            return None
    except Exception:
        return None


def _download_innertube2_stream(video_id: str, save_dir: str) -> Optional[str]:
    try:
        from innertube import InnerTube
        client = InnerTube("WEB")
        data = client.video(video_id)
        streaming = data.get("streamingData", {})
        formats = streaming.get("formats", [])  # progressive

        stream_url = None
        for f in formats:
            mime = f.get("mimeType", "")
            if "video/mp4" in mime:
                stream_url = f.get("url")
                break
        if not stream_url:
            for f in formats:
                mime = f.get("mimeType", "")
                if "video/webm" in mime:
                    stream_url = f.get("url")
                    break
        if not stream_url:
            return None

        if not stream_url.startswith("https://"):
            from urllib.parse import urljoin
            stream_url = urljoin("https://www.youtube.com", stream_url)

        return download_file(stream_url, save_dir, filename=f"{video_id}.mp4", timeout=120)
    except Exception:
        return None


def _download_piped_stream(video_id: str, save_dir: str) -> Optional[str]:
    # Piped ممکن است streamURL ندهد، سعی می‌کنیم
    try:
        url = f"https://pipedapi.kavin.rocks/video/{video_id}"
        resp = requests.get(url, timeout=15, headers={"User-Agent": settings.USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        # برخی نسخه‌های Piped videoStreams دارند
        streams = data.get("videoStreams", [])
        if not streams:
            return None
        # انتخاب بهترین کیفیت (آخرین عنصر معمولاً بالاترین کیفیت)
        stream = streams[-1]
        stream_url = stream.get("url")
        if not stream_url:
            return None
        return download_file(stream_url, save_dir, filename=f"{video_id}.mp4", timeout=120)
    except Exception:
        return None


def _download_pytube(video_id: str, save_dir: str) -> Optional[str]:
    try:
        # تلاش با pytubefix (فورک پایدارتر) در اولویت
        try:
            from pytubefix import YouTube
        except ImportError:
            from pytube import YouTube
        yt = YouTube(f"https://www.youtube.com/watch?v={video_id}")
        stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
        if stream:
            stream.download(output_path=save_dir, filename=f"{video_id}.mp4")
            final_path = os.path.join(save_dir, f"{video_id}.mp4")
            if os.path.exists(final_path):
                return final_path
    except Exception:
        return None
    return None


# ──────────────────────── دانلود تامنیل (بدون تغییر عمده) ──────────────────

def download_thumbnail(video_id: str, save_dir: str) -> Tuple[Optional[str], str]:
    """
    دانلود تامنیل با کیفیت‌های مختلف.
    Returns:
        (مسیر فایل, نام متد استفاده شده - اینجا "direct")
    """
    base_url = "https://img.youtube.com/vi/{}/{}"
    variants = ["maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg", "mqdefault.jpg"]
    headers = {"User-Agent": settings.USER_AGENT}
    for var in variants:
        url = base_url.format(video_id, var)
        try:
            head = requests.head(url, timeout=10, headers=headers)
            if head.status_code == 200:
                path = download_file(url, save_dir, filename=f"{video_id}.jpg", timeout=20)
                if path:
                    return path, "direct"
        except Exception:
            continue
    return None, "direct"


# ──────────────────────── توابع اصلی عمومی ─────────────────────────────────

def search_youtube(
    query: str,
    limit: int = 10,
    chain: Optional[List[str]] = None,
    start_method: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    جستجوی یوتیوب با زنجیره‌ای از متدها.
    Args:
        query: عبارت جستجو
        limit: حداکثر تعداد نتایج
        chain: زنجیرهٔ متدها (پیش‌فرض settings.DEFAULT_SEARCH_CHAIN)
        start_method: نام متدی که جستجو از آن آغاز شود (برای /next)
    Returns:
        (لیست نتایج, نام متد موفق) یا ([], None)
    """
    if chain is None:
        chain = settings.DEFAULT_SEARCH_CHAIN[:]

    # تعریف تابع داخلی که یک متد خاص را اجرا می‌کند
    def _search_op(method_name: str, kwargs: dict) -> Optional[List[Dict[str, Any]]]:
        q = kwargs["query"]
        lim = kwargs["limit"]
        if method_name == "piped":
            return _search_piped(q, lim)
        elif method_name == "newpipe":
            return _search_newpipe(q, lim)
        elif method_name == "innertube2":
            return _search_innertube2(q, lim)
        elif method_name == "scrapetube":
            return _search_scrapetube(q, lim)
        elif method_name == "youtube_search_python":
            return _search_youtube_search_python(q, lim)
        elif method_name == "py_yt_search":
            return _search_py_yt_search(q, lim)
        elif method_name == "duckduckgo":
            return _search_duckduckgo(q, lim)
        elif method_name == "html_parse":
            return _search_html_parse(q, lim)
        else:
            _log.warning(f"متد جستجوی ناشناخته: {method_name}")
            return None

    results, used_method = run_with_fallback(
        chain, _search_op, start_method, query=query, limit=limit
    )
    if results is None:
        return [], used_method
    return results, used_method


def get_video_info(
    video_id: str,
    chain: Optional[List[str]] = None,
    start_method: Optional[str] = None
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    دریافت اطلاعات ویدیو با زنجیره‌ای از متدها.
    Returns:
        (دیکشنری اطلاعات, نام متد موفق) یا ({}, None)
    """
    if chain is None:
        chain = settings.DEFAULT_INFO_CHAIN[:]

    def _info_op(method_name: str, kwargs: dict) -> Optional[Dict[str, Any]]:
        vid = kwargs["video_id"]
        if method_name == "piped":
            return _info_piped(vid)
        elif method_name == "newpipe":
            return _info_newpipe(vid)
        elif method_name == "innertube2":
            return _info_innertube2(vid)
        elif method_name == "oembed":
            return _info_oembed(vid)
        elif method_name == "yt_dlp":
            return _info_yt_dlp(vid)
        elif method_name == "py_yt_search":
            return _info_py_yt_search(vid)
        else:
            _log.warning(f"متد اطلاعات ناشناخته: {method_name}")
            return None

    info, used_method = run_with_fallback(
        chain, _info_op, start_method, video_id=video_id
    )
    if info is None:
        return {}, used_method
    return info, used_method


def download_video(
    video_id: str,
    save_dir: str,
    chain: Optional[List[str]] = None,
    start_method: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """
    دانلود ویدیو با زنجیره‌ای از متدها.
    Returns:
        (مسیر فایل دانلود شده, نام متد موفق) یا (None, None)
    """
    os.makedirs(save_dir, exist_ok=True)
    if chain is None:
        chain = settings.DEFAULT_DOWNLOAD_CHAIN[:]

    def _download_op(method_name: str, kwargs: dict) -> Optional[str]:
        vid = kwargs["video_id"]
        directory = kwargs["save_dir"]
        if method_name == "cobalt":
            return _download_cobalt(vid, directory)
        elif method_name == "yt_dlp_pot":
            return _download_yt_dlp_pot(vid, directory)
        elif method_name == "newpipe":
            return _download_newpipe(vid, directory)
        elif method_name == "innertube2_stream":
            return _download_innertube2_stream(vid, directory)
        elif method_name == "piped_stream":
            return _download_piped_stream(vid, directory)
        elif method_name == "pytube":
            return _download_pytube(vid, directory)
        else:
            _log.warning(f"متد دانلود ناشناخته: {method_name}")
            return None

    path, used_method = run_with_fallback(
        chain, _download_op, start_method, video_id=video_id, save_dir=save_dir
    )
    return path, used_method
