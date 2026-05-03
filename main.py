"""
main.py – رابط کاربری پنل شیشه‌ای ربات YouTube Bale Bot
مدیریت کامل API بله، صف هوشمند، کیبوردهای inline پیشرفته، پشتیبانی از /next و دانلود دسته‌جمعی
"""

import os
import re
import sys
import time
import threading
import uuid
import json
import queue
from typing import Any, Dict, List, Optional, Tuple

import requests

import settings
from utils import get_logger, load_json, save_json, split_file_binary, extract_video_id
import youtube_worker

_log = get_logger("main")

# ──────────────── ثابت‌های محلی ────────────────
MAX_SEND_SIZE = 20 * 1024 * 1024           # ۲۰ مگابایت (برای تقسیم فایل)
RESULTS_PER_PAGE = settings.UI_SETTINGS.get("result_page_size", 5)
LAST_SEARCH_FILE = os.path.join(settings.DATA_DIR, "last_search.json")
METHOD_STATE_FILE = os.path.join(settings.DATA_DIR, "method_state.json")


# ──────────────── توابع API بله ────────────────

def send_message(chat_id: int, text: str, reply_markup: Optional[Dict] = None) -> Optional[dict]:
    """ارسال پیام متنی به کاربر (با پشتیبانی از inline_keyboard)"""
    url = f"{settings.API_BASE}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)  # بله رد می‌کند که به صورت رشته JSON باشد
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


def send_document(chat_id: int, file_path: str, caption: str = "") -> Optional[dict]:
    """ارسال فایل به کاربر (با تکه‌تکه کردن در صورت نیاز)"""
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
        _log.info(f"فایل {file_path} بزرگتر از ۲۰ مگابایت است. تکه‌تکه می‌شود.")
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
            send_document(chat_id, part_path, part_caption)
            try:
                os.remove(part_path)
            except OSError:
                _log.warning(f"حذف تکه {part_path} ممکن نشد.")

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


def get_updates(offset: int, timeout: int) -> Optional[dict]:
    """دریافت پیام‌های جدید (long polling)"""
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


def edit_reply_markup(chat_id: int, message_id: int, reply_markup: Dict) -> Optional[dict]:
    """ویرایش کیبورد یک پیام (بدون تغییر متن)"""
    url = f"{settings.API_BASE}/editMessageReplyMarkup"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps(reply_markup),
    }
    try:
        resp = requests.post(url, json=payload, timeout=settings.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            _log.error(f"خطا در editMessageReplyMarkup: {data}")
            return None
        return data
    except Exception as e:
        _log.error(f"استثنا در editMessageReplyMarkup: {e}")
        return None


def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: Dict) -> Optional[dict]:
    """ویرایش متن و کیبورد یک پیام"""
    url = f"{settings.API_BASE}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": json.dumps(reply_markup),
    }
    try:
        resp = requests.post(url, json=payload, timeout=settings.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            _log.error(f"خطا در editMessageText: {data}")
            return None
        return data
    except Exception as e:
        _log.error(f"استثنا در editMessageText: {e}")
        return None


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """پاسخ به درخواست callback برای جلوگیری از تایمر بارگذاری"""
    url = f"{settings.API_BASE}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=settings.REQUEST_TIMEOUT)
    except Exception:
        pass  # خطای اینجا اهمیتی ندارد


# ──────────────── مدیریت کاربر ادمین ────────────────

def get_admin_chat_id() -> int:
    """برگرداندن شناسهٔ کاربر ادمین (ذخیره در فایل)"""
    if os.path.exists(settings.ADMIN_FILE):
        data = load_json(settings.ADMIN_FILE, {})
        return data.get("admin_chat_id", settings.DEFAULT_ADMIN_CHAT_ID)
    save_json(settings.ADMIN_FILE, {"admin_chat_id": settings.DEFAULT_ADMIN_CHAT_ID})
    return settings.DEFAULT_ADMIN_CHAT_ID


def is_admin(chat_id: int) -> bool:
    """بررسی اینکه کاربر مجاز است یا خیر"""
    return chat_id == get_admin_chat_id()


# ──────────────── مدیریت حالت (State) ────────────────

def load_last_search() -> List[Dict[str, Any]]:
    """بارگذاری آخرین نتایج جستجو (لیست کامل اشیاء ویدیو)"""
    data = load_json(LAST_SEARCH_FILE, {})
    return data.get("results", [])


def save_last_search(results: List[Dict[str, Any]]) -> None:
    """ذخیرهٔ نتایج جستجو برای صفحه‌بندی و دانلود دسته‌جمعی"""
    save_json(LAST_SEARCH_FILE, {"results": results})


def load_method_state() -> Dict[str, Any]:
    """بارگذاری آخرین وضعیت متدها و پارامترهای استفاده شده"""
    return load_json(METHOD_STATE_FILE, {
        "command": None,
        "params": {},
        "last_method": None,
    })


def save_method_state(command: str, params: Dict[str, Any], method: Optional[str] = None) -> None:
    """ذخیرهٔ وضعیت برای پشتیبانی از /next"""
    state = {
        "command": command,
        "params": params,
        "last_method": method,
    }
    save_json(METHOD_STATE_FILE, state)


# ──────────────── سیستم صف و کارگر ────────────────

task_queue = queue.Queue()
queue_lock = threading.Lock()


def enqueue_job(job: Dict[str, Any]) -> None:
    """اضافه کردن یک وظیفه به صف (هم حافظه و هم فایل)"""
    job.setdefault("job_id", uuid.uuid4().hex[:8])
    job.setdefault("created_at", time.time())

    with queue_lock:
        jobs = load_json(settings.QUEUE_FILE, [])
        jobs.append(job)
        save_json(settings.QUEUE_FILE, jobs)

    task_queue.put(job)
    _log.info(f"وظیفه {job['job_id']} ({job.get('command')}) در صف قرار گرفت.")


def worker_loop() -> None:
    """کارگر پردازش صف - اجرا در یک ترد جداگانه"""
    _log.info("کارگر صف آغاز به کار کرد.")

    # بارگذاری وظایف قبلی از فایل (در صورت restart)
    with queue_lock:
        pending = load_json(settings.QUEUE_FILE, [])
        for job in pending:
            task_queue.put(job)
        _log.info(f"{len(pending)} وظیفه از فایل صف بارگذاری شد.")

    while True:
        job = task_queue.get()
        command = job.get("command")
        params = job.get("params", {})
        chat_id = job.get("chat_id")
        job_id = job.get("job_id", "unknown")
        _log.info(f"شروع پردازش {job_id} | فرمان: {command}")

        # ساختن پوشهٔ موقت برای این وظیفه
        job_folder = os.path.join(settings.DOWNLOADS_DIR, job_id)
        os.makedirs(job_folder, exist_ok=True)

        try:
            if command == "search":
                query = params.get("query", "")
                limit = params.get("limit", 10)
                chain = params.get("chain", settings.DEFAULT_SEARCH_CHAIN[:])
                start_method = params.get("start_method")

                results, method_used = youtube_worker.search_youtube(query, limit, chain, start_method)
                if not results:
                    send_message(chat_id, "🔎 نتیجه‌ای پیدا نشد.")
                else:
                    # ذخیره نتایج کامل برای صفحه‌بندی
                    save_last_search(results)
                    save_method_state("search", params, method_used)

                    total_pages = (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
                    # فقط صفحهٔ اول را ارسال کن
                    first_page = results[:RESULTS_PER_PAGE]
                    caption = f"🔍 روش: {method_used}\nصفحه ۱ از {total_pages}"
                    keyboard = build_search_keyboard(first_page, page=0, total_pages=total_pages, method_used=method_used)
                    send_message(chat_id, caption, reply_markup=keyboard)

            elif command == "info":
                video_id = params.get("video_id")
                chain = params.get("chain", settings.DEFAULT_INFO_CHAIN[:])
                start_method = params.get("start_method")

                info, method_used = youtube_worker.get_video_info(video_id, chain, start_method)
                if not info or not info.get("title"):
                    send_message(chat_id, "❌ اطلاعات ویدیو دریافت نشد.")
                else:
                    save_method_state("info", params, method_used)

                    # متن اطلاعات
                    text = (
                        f"📹 {info.get('title')}\n"
                        f"👤 {info.get('author', 'نامشخص')}\n"
                        f"👁 {info.get('view_count', '?')}\n"
                        f"⏱ {info.get('duration', '?')} ثانیه\n"
                        f"🧩 روش: {method_used}"
                    )
                    # تامنیل در صورت وجود و فعال بودن تنظیم
                    if settings.UI_SETTINGS.get("show_thumbnails") and info.get("thumbnail"):
                        # دانلود تامنیل و ارسال
                        thumb_path, _ = youtube_worker.download_thumbnail(video_id, job_folder)
                        if thumb_path:
                            send_document(chat_id, thumb_path, caption="🖼 تامنیل")
                            try:
                                os.remove(thumb_path)
                            except OSError:
                                pass

                    keyboard = build_info_keyboard(video_id, method_used)
                    send_message(chat_id, text, reply_markup=keyboard)

            elif command == "thumb":
                video_id = params.get("video_id")
                thumb_path, method = youtube_worker.download_thumbnail(video_id, job_folder)
                if thumb_path:
                    send_document(chat_id, thumb_path, caption=f"🖼 تامنیل (روش: {method})")
                    os.remove(thumb_path)
                else:
                    send_message(chat_id, "❌ تامنیل پیدا نشد.")

            elif command == "download":
                video_id = params.get("video_id")
                chain = params.get("chain", settings.DEFAULT_DOWNLOAD_CHAIN[:])
                start_method = params.get("start_method")

                file_path, method_used = youtube_worker.download_video(video_id, job_folder, chain, start_method)
                if file_path:
                    save_method_state("download", params, method_used)
                    send_document(chat_id, file_path, caption=f"🎥 ویدیو (روش: {method_used})")
                    # فایل اصلی احتمالاً توسط send_document مدیریت می‌شود (اگر تکه‌تکه شد پاک می‌شود)
                    # اگر مستقیماً ارسال شد و هنوز وجود دارد، پاکش کن
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except OSError:
                            pass
                    keyboard = build_download_keyboard(video_id, method_used)
                    send_message(chat_id, f"✅ دانلود با روش «{method_used}» موفق بود.", reply_markup=keyboard)
                else:
                    send_message(chat_id, "❌ دانلود ناموفق - همه روش‌ها شکست خوردند.")

            elif command == "batch_download":
                results = load_last_search()
                if not results:
                    send_message(chat_id, "⚠️ ابتدا یک جستجو انجام دهید.")
                else:
                    total = len(results)
                    for idx, video in enumerate(results, 1):
                        job_params = {
                            "command": "download",
                            "params": {"video_id": video["video_id"]},
                            "chat_id": chat_id,
                        }
                        enqueue_job(job_params)
                    send_message(chat_id, f"📥 دانلود {total} ویدیو به صف اضافه شد.")

            else:
                send_message(chat_id, "⚠️ فرمان ناشناخته در صف.")

        except Exception as e:
            _log.error(f"خطا در پردازش وظیفه {job_id}: {e}")
            send_message(chat_id, f"⛔ خطایی رخ داد: {str(e)[:100]}")
        finally:
            # پاک‌سازی پوشهٔ موقت
            try:
                if os.path.exists(job_folder) and not os.listdir(job_folder):
                    os.rmdir(job_folder)
            except Exception:
                pass

            # حذف از فایل صف
            with queue_lock:
                jobs = load_json(settings.QUEUE_FILE, [])
                jobs = [j for j in jobs if j.get("job_id") != job_id]
                save_json(settings.QUEUE_FILE, jobs)

            task_queue.task_done()
            _log.info(f"پایان وظیفه {job_id}")


# ──────────────── ساخت inline keyboard (پنل شیشه‌ای) ────────────────

def build_search_keyboard(results: List[Dict[str, Any]], page: int, total_pages: int, method_used: str) -> Dict:
    """
    ساخت کیبورد برای نتایج جستجو.
    results: لیست ویدیوهای صفحه‌ی جاری.
    """
    keyboard = []

    # ردیف‌های ویدیوها: هر ردیف سه دکمه (📋 اطلاعات, 🖼 تامنیل, 📥 دانلود)
    for video in results:
        vid = video.get("video_id")
        title = video.get("title", "بی‌نام")[:30]
        # اطلاعات
        btn_info = {"text": f"📋 {title}", "callback_data": f"info|{vid}|{method_used}"}
        # تامنیل
        btn_thumb = {"text": "🖼", "callback_data": f"thumb|{vid}|{method_used}"}
        # دانلود
        btn_dl = {"text": "📥", "callback_data": f"dl|{vid}|{method_used}"}
        keyboard.append([btn_info, btn_thumb, btn_dl])

    # ردیف ناوبری (◀️ ▶️)
    nav_buttons = []
    if page > 0:
        nav_buttons.append({"text": "◀️", "callback_data": f"search_page|{page-1}"})
    if page < total_pages - 1:
        nav_buttons.append({"text": "▶️", "callback_data": f"search_page|{page+1}"})
    if nav_buttons:
        keyboard.append(nav_buttons)

    # ردیف کنترلی
    control_buttons = [
        {"text": "🔁 متد بعدی", "callback_data": f"next_search|{method_used}"},
        {"text": "📥 دانلود همه", "callback_data": "batch_dl"},
    ]
    keyboard.append(control_buttons)

    return {"inline_keyboard": keyboard}


def build_info_keyboard(video_id: str, method_used: str) -> Dict:
    """کیبورد برای اطلاعات ویدیو"""
    keyboard = [
        [
            {"text": "📥 دانلود", "callback_data": f"dl|{video_id}|{method_used}"},
            {"text": "🖼 تامنیل", "callback_data": f"thumb|{video_id}|{method_used}"},
        ],
        [
            {"text": "🔁 متد بعدی", "callback_data": f"next_info|{method_used}"},
        ],
    ]
    return {"inline_keyboard": keyboard}


def build_download_keyboard(video_id: str, method_used: str) -> Dict:
    """کیبورد پس از دانلود (فقط دکمهٔ متد بعدی)"""
    keyboard = [
        [
            {"text": "🔁 متد بعدی", "callback_data": f"next_dl|{method_used}"},
        ],
    ]
    return {"inline_keyboard": keyboard}


# ──────────────── مدیریت callback‌ها ────────────────

def handle_callback(chat_id: int, callback_data: str, message_id: int, callback_query_id: str) -> None:
    """پردازش callback_query های inline keyboard"""
    if not is_admin(chat_id):
        answer_callback_query(callback_query_id, "⛔ دسترسی ندارید.")
        return

    answer_callback_query(callback_query_id)  # پاسخ خالی برای جلوگیری از تایمر

    parts = callback_data.split("|")
    action = parts[0]

    if action in ("info", "thumb", "dl"):
        video_id = parts[1]
        method = parts[2] if len(parts) > 2 else None
        if action == "info":
            job_params = {"command": "info", "params": {"video_id": video_id, "start_method": method}, "chat_id": chat_id}
        elif action == "thumb":
            job_params = {"command": "thumb", "params": {"video_id": video_id}, "chat_id": chat_id}
        else:  # dl
            job_params = {"command": "download", "params": {"video_id": video_id, "start_method": method}, "chat_id": chat_id}
        enqueue_job(job_params)

    elif action in ("next_search", "next_info", "next_dl"):
        # دریافت وضعیت فعلی
        state = load_method_state()
        if not state.get("command") or not state.get("last_method"):
            send_message(chat_id, "⚠️ دستور قبلی‌ای برای ادامه وجود ندارد.")
            return

        # تعیین نوع و زنجیره
        command_type = state["command"]
        last_method = state["last_method"]
        params = state.get("params", {})
        if command_type == "search":
            chain = settings.DEFAULT_SEARCH_CHAIN[:]
        elif command_type == "info":
            chain = settings.DEFAULT_INFO_CHAIN[:]
        elif command_type == "download":
            chain = settings.DEFAULT_DOWNLOAD_CHAIN[:]
        else:
            return

        try:
            idx = chain.index(last_method)
            next_idx = idx + 1
        except ValueError:
            send_message(chat_id, "⚠️ متد فعلی در زنجیره یافت نشد.")
            return

        if next_idx >= len(chain):
            send_message(chat_id, "🏁 به انتهای زنجیره رسیدید، همهٔ متدها امتحان شدند.")
            return

        next_method = chain[next_idx]
        # ایجاد job جدید با start_method = next_method
        new_params = params.copy()
        new_params["start_method"] = next_method
        job = {"command": command_type, "params": new_params, "chat_id": chat_id}
        enqueue_job(job)

    elif action == "batch_dl":
        results = load_last_search()
        if not results:
            send_message(chat_id, "⚠️ ابتدا یک جستجو انجام دهید.")
            return
        for video in results:
            job = {"command": "download", "params": {"video_id": video["video_id"]}, "chat_id": chat_id}
            enqueue_job(job)
        send_message(chat_id, f"📥 دانلود {len(results)} ویدیو به صف اضافه شد.")

    elif action == "search_page":
        page = int(parts[1])
        results = load_last_search()
        if not results:
            send_message(chat_id, "⚠️ نتایج جستجو یافت نشد.")
            return
        total_pages = (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
        start = page * RESULTS_PER_PAGE
        end = start + RESULTS_PER_PAGE
        page_results = results[start:end]

        # خواندن متد استفاده‌شده (از state یا نگهداری در داده callback؟)
        # آخرین متد از state می‌آید
        state = load_method_state()
        method_used = state.get("last_method", "نامشخص")

        caption = f"🔍 روش: {method_used}\nصفحه {page+1} از {total_pages}"
        keyboard = build_search_keyboard(page_results, page, total_pages, method_used)
        edit_message_text(chat_id, message_id, caption, keyboard)


# ──────────────── مدیریت پیام‌های متنی ────────────────

def handle_message(chat_id: int, text: str) -> None:
    """تحلیل و پاسخ به پیام‌های کاربر"""
    if not is_admin(chat_id):
        send_message(chat_id, "⛔ دسترسی ندارید.")
        return

    text = text.strip()
    _log.info(f"پیام از {chat_id}: {text}")

    if text.startswith("/start"):
        send_message(chat_id, "👋 سلام ادمین! ربات YouTube Bale با ۲۰+ متد آماده است.\n/help را ببینید.")

    elif text.startswith("/help"):
        help_text = (
            "🤖 **راهنمای ربات**\n"
            "/search <عبارت> - جستجوی یوتیوب\n"
            "/info <شناسه|لینک> - اطلاعات ویدیو\n"
            "/thumb <شناسه> - دانلود تامنیل\n"
            "/download <شناسه> - دانلود ویدیو\n"
            "/next - ادامه با متد بعدی\n"
            "/batchdownload - دانلود همهٔ نتایج آخرین جستجو\n"
            "/log - دریافت فایل لاگ\n"
            "/help - این راهنما"
        )
        send_message(chat_id, help_text)

    elif text.startswith("/log"):
        if os.path.exists(settings.LOG_FILE):
            send_document(chat_id, settings.LOG_FILE, caption="📄 فایل لاگ")
        else:
            send_message(chat_id, "📭 فایل لاگ وجود ندارد.")

    elif text.startswith("/search"):
        query = text[7:].strip()
        if not query:
            send_message(chat_id, "❗ مثال: /search آموزش پایتون")
            return
        job = {
            "command": "search",
            "params": {"query": query, "limit": 10},
            "chat_id": chat_id
        }
        enqueue_job(job)
        send_message(chat_id, f"🔎 جستجوی «{query}» در صف قرار گرفت.")

    elif text.startswith("/info"):
        arg = text[5:].strip()
        if not arg:
            send_message(chat_id, "❗ مثال: /info dQw4w9WgXcQ")
            return
        video_id = arg
        if arg.startswith("http"):
            extracted = extract_video_id(arg)
            if extracted:
                video_id = extracted
            else:
                send_message(chat_id, "❗ لینک نامعتبر است.")
                return
        job = {
            "command": "info",
            "params": {"video_id": video_id},
            "chat_id": chat_id
        }
        enqueue_job(job)
        send_message(chat_id, f"📋 اطلاعات {video_id} در صف قرار گرفت.")

    elif text.startswith("/thumb"):
        video_id = text[6:].strip()
        if not video_id:
            send_message(chat_id, "❗ مثال: /thumb dQw4w9WgXcQ")
            return
        job = {
            "command": "thumb",
            "params": {"video_id": video_id},
            "chat_id": chat_id
        }
        enqueue_job(job)
        send_message(chat_id, f"🖼 دانلود تامنیل {video_id} در صف.")

    elif text.startswith("/download"):
        video_id = text[9:].strip()
        if not video_id:
            send_message(chat_id, "❗ مثال: /download dQw4w9WgXcQ")
            return
        job = {
            "command": "download",
            "params": {"video_id": video_id},
            "chat_id": chat_id
        }
        enqueue_job(job)
        send_message(chat_id, f"📥 دانلود {video_id} در صف.")

    elif text.startswith("/next"):
        state = load_method_state()
        if not state.get("command") or not state.get("last_method"):
            send_message(chat_id, "⚠️ دستور قبلی‌ای برای ادامه وجود ندارد.")
            return

        cmd = state["command"]
        last_method = state["last_method"]
        params = state.get("params", {})
        if cmd == "search":
            chain = settings.DEFAULT_SEARCH_CHAIN[:]
        elif cmd == "info":
            chain = settings.DEFAULT_INFO_CHAIN[:]
        elif cmd == "download":
            chain = settings.DEFAULT_DOWNLOAD_CHAIN[:]
        else:
            send_message(chat_id, "⚠️ فرمان نامعتبر.")
            return

        try:
            idx = chain.index(last_method)
            next_idx = idx + 1
        except ValueError:
            send_message(chat_id, "⚠️ متد فعلی در زنجیره نیست.")
            return

        if next_idx >= len(chain):
            send_message(chat_id, "🏁 همه متدها امتحان شدند.")
            return

        next_method = chain[next_idx]
        new_params = params.copy()
        new_params["start_method"] = next_method
        job = {"command": cmd, "params": new_params, "chat_id": chat_id}
        enqueue_job(job)

    elif text.startswith("/batchdownload"):
        results = load_last_search()
        if not results:
            send_message(chat_id, "⚠️ ابتدا یک جستجو انجام دهید.")
            return
        for video in results:
            job = {"command": "download", "params": {"video_id": video["video_id"]}, "chat_id": chat_id}
            enqueue_job(job)
        send_message(chat_id, f"📥 دانلود {len(results)} ویدیو به صف اضافه شد.")

    else:
        send_message(chat_id, "⚠️ دستور نامعتبر. /help را ببینید.")


# ──────────────── حلقهٔ اصلی ربات ────────────────

def main() -> None:
    """راه‌اندازی و اجرای اصلی ربات"""
    _log.info("ربات YouTube Bale Bot (نسخه ۲) آغاز به کار کرد.")

    # اطمینان از وجود پوشه‌ها
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    os.makedirs(settings.DOWNLOADS_DIR, exist_ok=True)

    # راه‌اندازی کارگر صف
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
                # پردازش پیام‌های متنی
                if "message" in update and "text" in update["message"]:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg["text"]
                    threading.Thread(target=handle_message, args=(chat_id, text), daemon=True).start()

                # پردازش callback query
                if "callback_query" in update:
                    cq = update["callback_query"]
                    chat_id = cq["message"]["chat"]["id"]
                    message_id = cq["message"]["message_id"]
                    callback_data = cq.get("data", "")
                    callback_query_id = cq["id"]
                    # اجرای مدیریت callback در همان ترد اصلی (یا در ترد جدا)
                    threading.Thread(target=handle_callback,
                                     args=(chat_id, callback_data, message_id, callback_query_id),
                                     daemon=True).start()

                offset = update["update_id"] + 1

        except Exception as e:
            _log.error(f"خطا در حلقه اصلی: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
