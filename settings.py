import os
import sys

# ──────────────── توکن و API ────────────────
BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN")
if not BOT_TOKEN:
    print("خطا: توکن ربات تنظیم نشده است. متغیر محیطی BALE_BOT_TOKEN را مقداردهی کنید.")
    sys.exit(1)

API_BASE = f"https://tapi.bale.ai/bot{BOT_TOKEN}"

# ──────────────── زمان و شبکه ────────────────
REQUEST_TIMEOUT = 30          # ثانیه، برای درخواست‌های HTTP عادی
LONG_POLL_TIMEOUT = 50        # ثانیه، تایم‌اوت getUpdates
ZIP_PART_SIZE = 49 * 1024 * 1024   # بایت، هر تکه برای آپلود فایل (حداکثر ۵۰ مگابایت بله)

# ──────────────── مسیرها ────────────────
DATA_DIR = "data"
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")       # فایل صف وظایف
ADMIN_FILE = os.path.join(DATA_DIR, "admin.json")       # ذخیره chat_id ادمین
LOG_FILE = "log.txt"                                    # فایل لاگ
DOWNLOADS_DIR = "downloads"                             # پوشه موقت دانلودها

# ──────────────── شناسه ادمین (پیش‌فرض) ────────────────
DEFAULT_ADMIN_CHAT_ID = 46829437   # در صورت نبودن admin.json، این شناسه استفاده می‌شود

# ──────────────── تنظیمات دانلود ویدیو ────────────────
MAX_VIDEO_HEIGHT = 1080    # حداکثر ارتفاع کیفیت
VIDEO_FORMAT = "mp4"       # فرمت خروجی نهایی

# ──────────────── User-Agent ────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ──────────────── کتابخانه‌های مورد نیاز (برای import پویا) ────────────────
REQUIRED_LIBS = [
    "requests",
    "yt_dlp",
    "innertube",
    "scrapetube",
    "py_yt_search",
    "pytube",
    "bs4",
]

# ──────────────── ترتیب fallback متدها برای هر عملیات ────────────────
SEARCH_METHODS = ["innertube", "scrapetube", "py_yt_search", "html_parse"]
INFO_METHODS = ["yt_dlp", "innertube", "py_yt_search", "oembed"]
DOWNLOAD_METHODS = ["yt_dlp", "innertube_stream", "pytube"]

# ──────────────── محدودیت حجم فایل ────────────────
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # بایت، ۲ گیگابایت (محدودیت گیت‌هاب)

# ──────────────── مسیر FFmpeg ────────────────
FFMPEG_PATH = "ffmpeg"   # فرض می‌شود ffmpeg در PATH سیستم موجود است
