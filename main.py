"""
main.py - حلقهٔ اصلی ربات YouTube Bale Bot
مدیریت اتصال به API بله، کنترل کاربر ادمین، صف وظایف و کارگر پردازش
"""

import json
import os
import sys
import time
import threading
import uuid
import requests
import queue

import settings
from utils import get_logger, load_json, save_json, split_file_binary
import youtube_worker

_log = get_logger("main")

# ──────────────── ثابت‌های محلی ────────────────
MAX_SEND_SIZE = 20 * 1024 * 1024  # ۲۰ مگابایت (برای تکه‌تکه کردن فایل‌های خروجی)

# ──────────────── ارتباط با API بله ────────────────

def send_message(chat_id: int, text: str) -> dict or None:
    """
    ارسال پیام متنی به کاربر.
    Args:
        chat_id: شناسه کاربر
        text: متن پیام
    Returns:
        دیکشنری پاسخ API یا None در صورت خطا
    """
    url = f"{settings.API_BASE}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=settings.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            _log.error(f"خطا در sendMessage: {data}")
            return None
        return data
    except Exception as e:
        _log.error(f"استثنا در sendMessage: {e}")
        return None


def send_document(chat_id: int, file_path: str, caption: str = "") -> dict or None:
    """
    ارسال فایل به کاربر (در صورت نیاز تکه‌تکه می‌کند).
    Args:
        chat_id: شناسه کاربر
        file_path: مسیر فایل
        caption: توضیح فایل
    Returns:
        دیکشنری خلاصه یا None
    """
    if not os.path.exists(file_path):
        _log.error(f"فایل برای ارسال یافت نشد: {file_path}")
        return None

    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        _log.error(f"خطا در خواندن حجم فایل: {file_path}")
        return None

    # اگر حجم فایل بزرگتر از حد مجاز باشد، تکه‌تکه کن
    if file_size > MAX_SEND_SIZE:
        _log.info(f"فایل {file_path} بزرگتر از حد مجاز است. تکه‌تکه می‌شود.")
        prefix = os.path.splitext(os.path.basename(file_path))[0]
        ext = os.path.splitext(file_path)[1]
        parts = split_file_binary(file_path, prefix, ext)
        if not parts:
            _log.error("تقسیم فایل موفق نبود.")
            return None

        total_parts = len(parts)
        for idx, part_path in enumerate(parts, 1):
            part_name = os.path.basename(part_path)
            part_caption = f"{caption} (بخش {idx}/{total_parts})" if caption else f"بخش {idx}/{total_parts}"
            # ارسال هر تکه (بازگشتی – ولی تکه‌ها زیر حد هستند و مستقیم ارسال می‌شوند)
            send_document(chat_id, part_path, part_caption)
            # حذف تکه بعد از ارسال
            try:
                os.remove(part_path)
            except OSError:
                _log.warning(f"حذف تکه {part_path} ممکن نشد.")

        # حذف فایل اصلی بعد از ارسال موفق همه تکه‌ها
        try:
            os.remove(file_path)
        except OSError:
            _log.warning(f"حذف فایل اصلی {file_path} ممکن نشد.")
        return {"ok": True, "sent_parts": total_parts}

    # ── ارسال مستقیم فایل (حجم کمتر از MAX_SEND_SIZE) ──
    url = f"{settings.API_BASE}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f)}
            data = {"chat_id": chat_id, "caption": caption}
            resp = requests.post(url, files=files, data=data, timeout=settings.REQUEST_TIMEOUT * 2)
            resp.raise_for_status()
            resp_data = resp.json()
            if not resp_data.get("ok"):
                _log.error(f"خطا در sendDocument: {resp_data}")
                return None
            return resp_data
    except Exception as e:
        _log.error(f"استثنا در sendDocument: {e}")
        return None


def get_updates(offset: int, timeout: int) -> dict:
    """
    دریافت پیام‌های جدید از سرور بله (long polling).
    Args:
        offset: شناسه آخرین پیام دریافت شده + 1
        timeout: تایم‌اوت انتظار
    Returns:
        دیکشنری پاسخ یا یک دیکشنری خالی پیش‌فرض در صورت خطا
    """
    url = f"{settings.API_BASE}/getUpdates"
    payload = {"offset": offset, "timeout": timeout}
    try:
        resp = requests.post(url, json=payload, timeout=timeout + 10)
        if resp.status_code != 200:
            _log.error(f"getUpdates status={resp.status_code}")
            return {"ok": True, "result": []}
        data = resp.json()
        if not data.get("ok"):
            _log.error(f"getUpdates خطا: {data}")
            return {"ok": True, "result": []}
        return data
    except Exception as e:
        _log.warning(f"خطا در getUpdates: {e}")
        return {"ok": True, "result": []}


# ──────────────── مدیریت کاربر ادمین ────────────────

def get_admin_chat_id() -> int:
    """
    برگرداندن chat_id ادمین. در صورت نبودن فایل، پیش‌فرض را ذخیره می‌کند.
    """
    if os.path.exists(settings.ADMIN_FILE):
        data = load_json(settings.ADMIN_FILE, {})
        return data.get("admin_chat_id", settings.DEFAULT_ADMIN_CHAT_ID)
    # ایجاد فایل admin.json با مقدار پیش‌فرض
    save_json(settings.ADMIN_FILE, {"admin_chat_id": settings.DEFAULT_ADMIN_CHAT_ID})
    return settings.DEFAULT_ADMIN_CHAT_ID


def is_admin(chat_id: int) -> bool:
    """بررسی مجاز بودن کاربر"""
    return chat_id == get_admin_chat_id()


# ──────────────── سیستم صف و کارگر ────────────────

task_queue = queue.Queue()  # صف درون‌حافظه‌ای
queue_lock = threading.Lock()


def enqueue_job(job: dict) -> None:
    """
    اضافه کردن یک وظیفه به صف (هم در حافظه و هم در فایل).
    """
    # تنظیم شناسه و زمان در صورت نیاز
    job.setdefault("job_id", uuid.uuid4().hex[:8])
    job.setdefault("created_at", time.time())

    with queue_lock:
        jobs = load_json(settings.QUEUE_FILE, [])
        jobs.append(job)
        save_json(settings.QUEUE_FILE, jobs)

    task_queue.put(job)
    _log.info(f"وظیفه {job['job_id']} به صف اضافه شد: {job.get('command')}")


def worker_loop() -> None:
    """
    کارگر پردازش صف، اجرا در یک ترد جداگانه.
    وظایف را از صف برداشته و عملیات یوتیوب را اجرا می‌کند، سپس نتیجه را به کاربر ارسال می‌کند.
    """
    _log.info("کارگر شروع به کار کرد.")

    # بارگذاری وظایف قبلی از فایل به حافظه (در صورت وجود)
    with queue_lock:
        pending = load_json(settings.QUEUE_FILE, [])
        for job in pending:
            task_queue.put(job)
        _log.info(f"{len(pending)} وظیفه از فایل بارگذاری شد.")

    while True:
        job = task_queue.get()
        command = job.get("command")
        params = job.get("params", {})
        chat_id = job.get("chat_id")
        job_id = job.get("job_id", "unknown")
        _log.info(f"شروع پردازش وظیفه {job_id} (دستور: {command})")

        # ساختن پوشهٔ موقت یکتا برای این کار (برای فایل‌های خروجی)
        job_folder = os.path.join(settings.DOWNLOADS_DIR, job_id)
        os.makedirs(job_folder, exist_ok=True)

        try:
            if command == "search":
                query = params.get("query", "")
                limit = params.get("limit", 10)
                results = youtube_worker.search_youtube(query, limit)
                if not results:
                    send_message(chat_id, "🔎 نتیجه‌ای یافت نشد.")
                else:
                    # ساخت پیام خلاصه نتایج
                    lines = [f"نتایج جستجو برای: {query}"]
                    for r in results[:limit]:
                        title = r.get("title") or "بی‌نام"
                        vid = r.get("video_id")
                        dur = r.get("duration") or "?"
                        lines.append(f"▫️ {title}\n   🆔 {vid} | ⏱ {dur}")
                    send_message(chat_id, "\n".join(lines))

            elif command == "info":
                video_id = params.get("video_id")
                info = youtube_worker.get_video_info(video_id)
                if not info or not info.get("title"):
                    send_message(chat_id, "❌ اطلاعات ویدیو دریافت نشد.")
                else:
                    # ساخت پیام متنی
                    msg = (
                        f"📹 {info.get('title')}\n"
                        f"👤 {info.get('author') or 'نامشخص'}\n"
                        f"👁 {info.get('view_count') or '?'} بازدید\n"
                        f"⏱ {info.get('duration')} ثانیه\n"
                        f"🖼 {info.get('thumbnail') or 'بدون تامنیل'}"
                    )
                    send_message(chat_id, msg)

                    # اگر تامنیل موجود بود، دانلود و ارسال کن
                    thumb_url = info.get("thumbnail")
                    if thumb_url:
                        # دانلود تامنیل با worker
                        thumb_path = youtube_worker.download_thumbnail(video_id, job_folder)
                        if thumb_path:
                            send_document(chat_id, thumb_path, caption="🖼 تامنیل ویدیو")
                            # پاک‌سازی تامنیل (فایل اصلی توسط send_document در صورت نیاز حذف می‌شود)
                            # send_document خودش فایل را حذف نمی‌کند، ما در اینجا بعد از ارسال پاک می‌کنیم
                            try:
                                os.remove(thumb_path)
                            except OSError:
                                pass
                        else:
                            _log.warning("دانلود تامنیل برای info شکست خورد.")

            elif command == "thumb":
                video_id = params.get("video_id")
                thumb_path = youtube_worker.download_thumbnail(video_id, job_folder)
                if thumb_path:
                    send_document(chat_id, thumb_path, caption=f"🖼 تامنیل {video_id}")
                    # پاک‌سازی (send_document فایل را حذف نکرده)
                    try:
                        os.remove(thumb_path)
                    except OSError:
                        pass
                else:
                    send_message(chat_id, "❌ تامنیل پیدا نشد.")

            elif command == "download":
                video_id = params.get("video_id")
                video_path = youtube_worker.download_video(video_id, job_folder)
                if video_path:
                    send_document(chat_id, video_path, caption=f"🎥 ویدیو {video_id}")
                    # send_document خودش فایل اصلی و تکه‌ها را در صورت نیاز پاک می‌کند
                    # اما اگر مستقیماً ارسال شده (بدون تکه‌تکه) باید پاک کنیم
                    if os.path.exists(video_path):
                        try:
                            os.remove(video_path)
                        except OSError:
                            _log.warning(f"حذف فایل ویدیو {video_path} ممکن نشد.")
                else:
                    send_message(chat_id, "❌ دانلود ویدیو ناموفق بود.")

            else:
                send_message(chat_id, "⚠️ دستور ناشناخته در صف.")

        except Exception as e:
            _log.error(f"خطا در پردازش وظیفه {job_id}: {e}")
            send_message(chat_id, f"⛔ خطایی در پردازش درخواست رخ داد: {str(e)[:100]}")
        finally:
            # پاک‌سازی پوشهٔ موقت (اگر خالی بود)
            try:
                if os.path.exists(job_folder) and not os.listdir(job_folder):
                    os.rmdir(job_folder)
            except Exception:
                pass

            # حذف job از فایل صف
            with queue_lock:
                jobs = load_json(settings.QUEUE_FILE, [])
                jobs = [j for j in jobs if j.get("job_id") != job_id]
                save_json(settings.QUEUE_FILE, jobs)

            task_queue.task_done()
            _log.info(f"پایان وظیفه {job_id}")


# ──────────────── پردازش پیام‌های دریافتی ────────────────

def handle_message(chat_id: int, text: str) -> None:
    """
    تحلیل و پاسخ به یک پیام متنی از کاربر.
    """
    if not is_admin(chat_id):
        send_message(chat_id, "⛔ دسترسی ندارید.")
        return

    text = text.strip()
    _log.info(f"پیام از {chat_id}: {text}")

    if text.startswith("/start"):
        send_message(chat_id, "👋 سلام ادمین! ربات YouTube Bale آمادهٔ خدمت است.\n"
                              "دستور /help را برای راهنما بفرستید.")

    elif text.startswith("/log"):
        if os.path.exists(settings.LOG_FILE):
            send_document(chat_id, settings.LOG_FILE, caption="📄 فایل لاگ")
        else:
            send_message(chat_id, "📭 فایل لاگ وجود ندارد.")

    elif text.startswith("/search"):
        query = text[7:].strip()
        if not query:
            send_message(chat_id, "❗ لطفاً عبارت جستجو را مشخص کنید. مثال: /search آموزش پایتون")
            return
        job = {
            "command": "search",
            "params": {"query": query, "limit": 10},
            "chat_id": chat_id
        }
        enqueue_job(job)
        send_message(chat_id, f"🔎 جستجوی «{query}» در صف قرار گرفت.")

    elif text.startswith("/info"):
        video_id = text[5:].strip()
        if not video_id:
            youtube_url = text[5:].strip()
            if youtube_url.startswith("http"):
                from utils import extract_video_id
                video_id = extract_video_id(youtube_url)
                if not video_id:
                    send_message(chat_id, "❗ لینک یوتیوب نامعتبر است.")
                    return
            else:
                send_message(chat_id, "❗ لطفاً شناسه یا لینک ویدیو را مشخص کنید. مثال: /info dQw4w9WgXcQ")
                return
        job = {
            "command": "info",
            "params": {"video_id": video_id},
            "chat_id": chat_id
        }
        enqueue_job(job)
        send_message(chat_id, f"📋 دریافت اطلاعات ویدیو {video_id} در صف قرار گرفت.")

    elif text.startswith("/thumb"):
        video_id = text[6:].strip()
        if not video_id:
            send_message(chat_id, "❗ لطفاً شناسه ویدیو را مشخص کنید. مثال: /thumb dQw4w9WgXcQ")
            return
        job = {
            "command": "thumb",
            "params": {"video_id": video_id},
            "chat_id": chat_id
        }
        enqueue_job(job)
        send_message(chat_id, f"🖼 دانلود تامنیل {video_id} در صف قرار گرفت.")

    elif text.startswith("/download"):
        video_id = text[9:].strip()
        if not video_id:
            send_message(chat_id, "❗ لطفاً شناسه ویدیو را مشخص کنید. مثال: /download dQw4w9WgXcQ")
            return
        job = {
            "command": "download",
            "params": {"video_id": video_id},
            "chat_id": chat_id
        }
        enqueue_job(job)
        send_message(chat_id, f"📥 دانلود ویدیو {video_id} در صف قرار گرفت.")

    elif text.startswith("/help"):
        help_text = (
            "🤖 دستورات ربات:\n"
            "/start - شروع\n"
            "/help - این راهنما\n"
            "/log - دریافت فایل لاگ\n"
            "/search <عبارت> - جستجوی یوتیوب\n"
            "/info <شناسه|لینک> - اطلاعات ویدیو\n"
            "/thumb <شناسه> - دانلود تامنیل\n"
            "/download <شناسه> - دانلود ویدیو"
        )
        send_message(chat_id, help_text)

    else:
        send_message(chat_id, "⚠️ دستور نامعتبر. /help را ببینید.")


# ──────────────── حلقهٔ اصلی ربات ────────────────

def main() -> None:
    """
    نقطه شروع ربات: ایجاد پوشه‌ها، شروع کارگر، دریافت پیام‌ها و پاسخ‌دهی.
    """
    _log.info("ربات YouTube Bale Bot شروع به کار کرد.")

    # اطمینان از وجود پوشه‌های ضروری
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    os.makedirs(settings.DOWNLOADS_DIR, exist_ok=True)

    # راه‌اندازی ترد کارگر
    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()

    offset = 0
    while True:
        try:
            resp = get_updates(offset, settings.LONG_POLL_TIMEOUT)
            if resp is None or not resp.get("ok"):
                time.sleep(2)
                continue

            for update in resp.get("result", []):
                if "message" in update and "text" in update["message"]:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg["text"]
                    # پاسخ‌گویی در یک ترد جداگانه برای جلوگیری از کندی
                    threading.Thread(target=handle_message, args=(chat_id, text), daemon=True).start()

                offset = update["update_id"] + 1

        except Exception as e:
            _log.error(f"خطا در حلقه اصلی: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
