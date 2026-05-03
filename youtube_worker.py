"""
youtube_worker.py – موتور پردازش یوتیوب بدون API Key
تمام عملیات جستجو، اطلاعات، دانلود تامنیل و دانلود ویدیو با روش‌های چندگانه (fallback) انجام می‌شود.
"""

import json
import os
import re
import time
from typing import List, Optional, Dict, Any

# ──────────────── بارگذاری تنظیمات و ابزارهای داخلی ────────────────
import settings
from utils import get_logger, download_file
import requests

_log = get_logger("youtube_worker")


# ──────────────────────────────────────────────────────────────────────────────
#                                 توابع کمکی داخلی
# ──────────────────────────────────────────────────────────────────────────────

def _parse_innertube_renderer(data: dict, limit: int = 10) -> List[Dict[str, Any]]:
    """
    تحلیل پاسخ innertube یا داده‌های ytInitialData برای استخراج videoRendererها.
    خروجی: لیستی از دیکشنری شامل video_id, title, thumbnail_url, duration
    """
    results = []
    # پیمایش مسیرهای احتمالی
    contents = []
    try:
        # مسیر استاندارد innertube search
        primary = (data.get("contents", {})
                       .get("twoColumnSearchResultsRenderer", {})
                       .get("primaryContents", {})
                       .get("sectionListRenderer", {})
                       .get("contents", []))
        for section in primary:
            items = (section.get("itemSectionRenderer", {})
                         .get("contents", []))
            contents.extend(items)
        if not contents:
            # richGridRenderer (برای innertube ورژن وب جدید)
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
            # thumbnail
            thumbs = video.get("thumbnail", {}).get("thumbnails", [])
            thumb_url = thumbs[0]["url"] if thumbs else None
            # duration
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


def _parse_yt_initial_data(html: str, limit: int) -> List[Dict[str, Any]]:
    """استخراج JSON از ytInitialData و استفاده از پارسر داخلی"""
    match = re.search(r"var ytInitialData\s*=\s*(\{.+?\});", html)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
        return _parse_innertube_renderer(data, limit)
    except Exception as e:
        _log.warning(f"خطا در استخراج ytInitialData: {e}")
        return []


# ──────────────────────────────────────────────────────────────────────────────
#                            جستجوی یوتیوب (Multi‑Method)
# ──────────────────────────────────────────────────────────────────────────────

def search_youtube(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    جستجوی ویدیو در یوتیوب با استفاده از روش‌های fallback.
    Args:
        query: عبارات جستجو
        limit: حداکثر تعداد نتایج
    Returns:
        لیست دیکشنری‌ها با کلیدهای video_id, title, thumbnail_url, duration
    """
    _log.info(f"شروع جستجو برای: {query}")
    for method in settings.SEARCH_METHODS:
        _log.info(f"تلاش با روش {method}")
        try:
            if method == "innertube":
                from innertube import InnerTube
                client = InnerTube("WEB")
                data = client.search(query)
                results = _parse_innertube_renderer(data, limit)
                if results:
                    return results

            elif method == "scrapetube":
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
                        })
                if results:
                    return results

            elif method == "py_yt_search":
                import py_yt_search
                s = py_yt_search.Search(query, limit=limit)
                results = []
                for video in s.videos():  # متد videos() لیست اشیاء Video
                    results.append({
                        "video_id": video.id,
                        "title": getattr(video, "title", None),
                        "thumbnail_url": video.thumbnails[0] if getattr(video, "thumbnails", None) and video.thumbnails else None,
                        "duration": getattr(video, "duration", None),
                    })
                if results:
                    return results

            elif method == "html_parse":
                from bs4 import BeautifulSoup
                url = f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}"
                resp = requests.get(url, headers={"User-Agent": settings.USER_AGENT}, timeout=settings.REQUEST_TIMEOUT)
                resp.raise_for_status()
                results = _parse_yt_initial_data(resp.text, limit)
                if results:
                    return results

        except Exception as e:
            _log.warning(f"روش {method} شکست خورد: {e}")

    _log.error("تمام روش‌های جستجو ناموفق بودند.")
    return []


# ──────────────────────────────────────────────────────────────────────────────
#                        دریافت اطلاعات کامل ویدیو (Multi‑Method)
# ──────────────────────────────────────────────────────────────────────────────

def get_video_info(video_id: str) -> Dict[str, Any]:
    """
    دریافت اطلاعات کامل ویدیو.
    Args:
        video_id: شناسه ویدیو
    Returns:
        دیکشنری شامل title, duration, view_count, thumbnail, author, formats (در صورت وجود)
    """
    _log.info(f"دریافت اطلاعات ویدیو: {video_id}")
    for method in settings.INFO_METHODS:
        _log.info(f"تلاش با روش {method}")
        try:
            if method == "yt_dlp":
                import yt_dlp
                url = f"https://www.youtube.com/watch?v={video_id}"
                ydl_opts = {
                    "quiet": True,
                    "skip_download": True,
                    "noplaylist": True,
                    "extract_flat": False,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        continue
                    duration = info.get("duration")  # ثانیه
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
                        "formats": info.get("formats", []),
                        "description": info.get("description"),
                    }

            elif method == "innertube":
                from innertube import InnerTube
                client = InnerTube("WEB")
                data = client.video(video_id)
                video = data.get("videoDetails", {})
                if not video:
                    continue
                micro = video.get("microformat", {}).get("playerMicroformatRenderer", {})
                thumbnails = video.get("thumbnail", {}).get("thumbnails", [])
                thumb_url = thumbnails[-1]["url"] if thumbnails else None
                duration_str = video.get("lengthSeconds")
                duration = int(duration_str) if duration_str else None
                return {
                    "title": video.get("title"),
                    "duration": duration,
                    "view_count": int(video.get("viewCount", 0)) if video.get("viewCount") else None,
                    "thumbnail": thumb_url,
                    "author": video.get("author"),
                    "formats": [],
                    "description": micro.get("description", {}).get("simpleText", ""),
                }

            elif method == "py_yt_search":
                import py_yt_search
                v = None
                try:
                    v = py_yt_search.Video.get(video_id)
                except TypeError:
                    # Video.get ممکن است async باشد – استفاده از روش جستجو
                    s = py_yt_search.Search(video_id, limit=1)
                    videos = s.videos()
                    if videos:
                        v = videos[0]
                if not v:
                    continue
                return {
                    "title": v.title,
                    "duration": getattr(v, "duration", None),
                    "view_count": getattr(v, "views", None),
                    "thumbnail": v.thumbnails[0] if hasattr(v, "thumbnails") and v.thumbnails else None,
                    "author": getattr(v, "author", None) or getattr(v, "channel", None),
                    "formats": [],
                    "description": getattr(v, "description", None),
                }

            elif method == "oembed":
                url = f"https://www.youtube.com/oembed?url=https://youtube.com/watch?v={video_id}&format=json"
                resp = requests.get(url, timeout=settings.REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                return {
                    "title": data.get("title"),
                    "duration": None,
                    "view_count": None,
                    "thumbnail": data.get("thumbnail_url"),
                    "author": data.get("author_name"),
                    "formats": [],
                    "description": None,
                }

        except Exception as e:
            _log.warning(f"روش {method} شکست خورد: {e}")

    _log.error(f"دریافت اطلاعات ویدیو {video_id} ناموفق ماند.")
    return {}


# ──────────────────────────────────────────────────────────────────────────────
#                           دانلود تامنیل (با چند کیفیت)
# ──────────────────────────────────────────────────────────────────────────────

def download_thumbnail(video_id: str, save_dir: str) -> Optional[str]:
    """
    دانلود تامنیل ویدیو با آزمایش کیفیت‌های مختلف.
    Args:
        video_id: شناسه ویدیو
        save_dir: پوشه ذخیره
    Returns:
        مسیر فایل ذخیره‌شده یا None
    """
    _log.info(f"دانلود تامنیل برای {video_id}")
    base_url = "https://img.youtube.com/vi/{}/{}"
    variants = ["maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg", "mqdefault.jpg"]
    headers = {"User-Agent": settings.USER_AGENT}
    for var in variants:
        url = base_url.format(video_id, var)
        try:
            head = requests.head(url, timeout=10, headers=headers)
            if head.status_code == 200:
                _log.info(f"تامنیل در {var} یافت شد.")
                path = download_file(url, save_dir, filename=f"{video_id}.jpg", timeout=20)
                if path:
                    return path
        except Exception as e:
            _log.warning(f"بررسی/دانلود تامنیل {var} خطا: {e}")
    _log.error(f"هیچ تامنیلی برای {video_id} پیدا نشد.")
    return None


# ──────────────────────────────────────────────────────────────────────────────
#                         دانلود ویدیو (Multi‑Method)
# ──────────────────────────────────────────────────────────────────────────────

def _find_downloaded_file(video_id: str, save_dir: str, ext: str = ".mp4") -> Optional[str]:
    """پیدا کردن فایل دانلود شده با پیشوند video_id در پوشه"""
    for fname in os.listdir(save_dir):
        if fname.startswith(video_id) and fname.endswith(ext):
            return os.path.join(save_dir, fname)
    return None


def download_video(video_id: str, save_dir: str) -> Optional[str]:
    """
    دانلود ویدیو با روش‌های fallback.
    Args:
        video_id: شناسه ویدیو
        save_dir: پوشه ذخیره
    Returns:
        مسیر فایل نهایی یا None
    """
    _log.info(f"شروع دانلود ویدیو: {video_id}")
    os.makedirs(save_dir, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"

    for method in settings.DOWNLOAD_METHODS:
        _log.info(f"تلاش دانلود با روش {method}")
        try:
            if method == "yt_dlp":
                import yt_dlp
                ydl_opts = {
                    "format": (f"bestvideo[height<={settings.MAX_VIDEO_HEIGHT}]+bestaudio"
                               f"/best[height<={settings.MAX_VIDEO_HEIGHT}]"),
                    "merge_output_format": settings.VIDEO_FORMAT,
                    "outtmpl": os.path.join(save_dir, f"{video_id}.%(ext)s"),
                    "quiet": True,
                    "noplaylist": True,
                    "ffmpeg_location": settings.FFMPEG_PATH,
                    "retries": 3,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                result = _find_downloaded_file(video_id, save_dir, ".mp4") or \
                         _find_downloaded_file(video_id, save_dir, ".webm") or \
                         _find_downloaded_file(video_id, save_dir, ".mkv")
                if result:
                    _log.info(f"yt-dlp دانلود شد: {result}")
                    return result

            elif method == "innertube_stream":
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
                    _log.warning("هیچ فرمت progressive با mp4/webm یافت نشد.")
                    continue

                # اصلاح URL اگر نسبی بود
                if not stream_url.startswith("https://"):
                    from urllib.parse import urljoin
                    stream_url = urljoin("https://www.youtube.com", stream_url)

                _log.info("دانلود با URL مستقیم از innertube")
                result = download_file(stream_url, save_dir, filename=f"{video_id}_stream.mp4", timeout=120)
                if result:
                    final_path = os.path.join(save_dir, f"{video_id}.mp4")
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    os.rename(result, final_path)
                    return final_path

            elif method == "pytube":
                from pytube import YouTube
                yt = YouTube(url)
                stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
                if stream:
                    stream.download(output_path=save_dir, filename=f"{video_id}.mp4")
                    final_path = os.path.join(save_dir, f"{video_id}.mp4")
                    if os.path.exists(final_path):
                        _log.info(f"pytube دانلود شد: {final_path}")
                        return final_path

        except Exception as e:
            _log.warning(f"روش دانلود {method} شکست خورد: {e}")

    _log.error(f"دانلود ویدیو {video_id} ناموفق بود.")
    return None
