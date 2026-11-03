"""
Ард-ын Telegram сувгуудын шинэ постыг (текст, зураг, видео) таны группт
автоматаар оруулдаг бот.

Хэрхэн ажилладаг вэ:
  Ард-ын нээлттэй сувгуудын вэб хуудсыг (https://t.me/s/<суваг>) шалгаж,
  шинэ пост гарсан бол текст, зураг, линкийг нь таны группт илгээнэ.

Суулгах:
  pip install requests beautifulsoup4

Ажиллуулах:
  Chat ID олох:        python ard_telegram_bot.py --get-chat-id
  Нэг удаа шалгах:     python ard_telegram_bot.py --once   (GitHub Actions)
  Байнга ажиллуулах:   python ard_telegram_bot.py
"""

import json
import logging
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

# ============ ТОХИРГОО ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# Дагах сувгууд (t.me/ дараах нэр). Хүссэнээрээ нэмж, хасаж болно.
CHANNELS = ["ardappofficial", "ardcoin"]

CHECK_INTERVAL = 300  # байнга ажиллах горимд хэдэн секунд тутамд шалгах
STATE_FILE = "ard_bot_state.json"  # аль постыг илгээснийг санах файл
MAX_TEXT = 3500      # энгийн мессежийн хязгаар (Telegram: 4096)
MAX_CAPTION = 1024   # зурагны тайлбарын хязгаар (Telegram: 1024)
MAX_MEDIA = 10       # нэг удаа илгээх зураг/видеоны дээд тоо (Telegram: 10)
DEDUP_KEEP = 300     # давхардал шалгахад сүүлийн хэдэн постыг санах вэ
MAX_FILE_MB = 49     # ботоор илгээх файлын хэмжээний хязгаар (Telegram: 50MB)
# ==================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ard_bot")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ArdForwardBot/1.2)"}
API = "https://api.telegram.org/bot{token}/{method}"
BG_URL = re.compile(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)")


# ---------- Төлөв хадгалах ----------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ---------- Сувгаас унших ----------
def parse_posts(html_text):
    """Сувгийн вэб хуудаснаас (пост_дугаар, текст, [(төрөл, линк), ...]) гаргана."""
    soup = BeautifulSoup(html_text, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            post_id = int(msg["data-post"].split("/")[-1])
        except (ValueError, KeyError):
            continue

        text = ""
        text_el = msg.select_one(".tgme_widget_message_text")
        if text_el:
            # <br>-ийг шинэ мөр болгоод, эможи бүрийг тусдаа мөрөнд гаргахгүй
            for br in text_el.find_all("br"):
                br.replace_with("\n")
            text = text_el.get_text().strip()

        media, seen = [], set()
        for el in msg.select("a.tgme_widget_message_photo_wrap, video.tgme_widget_message_video"):
            if el.name == "video":
                url, kind = el.get("src"), "video"
            else:
                m = BG_URL.search(el.get("style", ""))
                url, kind = (m.group(1) if m else None), "photo"
            if url and url not in seen:
                seen.add(url)
                media.append((kind, url))

        posts.append((post_id, text, media))
    posts.sort(key=lambda p: p[0])
    return posts


def fetch_posts(channel):
    r = requests.get(f"https://t.me/s/{channel}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_posts(r.text)


def download(url):
    r = requests.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()
    if len(r.content) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"{MAX_FILE_MB}MB-аас том файл")
    return r.content


# ---------- Telegram руу илгээх ----------
def tg_call(method, data, files=None):
    """Telegram API дуудна. Хэт олон хүсэлт (429) ирвэл хүлээгээд дахин оролдоно."""
    for _ in range(3):
        r = requests.post(API.format(token=BOT_TOKEN, method=method),
                          data=data, files=files, timeout=120)
        try:
            res = r.json()
        except ValueError:
            res = {"ok": False, "description": r.text[:200]}
        if res.get("ok"):
            return True
        retry = res.get("parameters", {}).get("retry_after")
        if r.status_code == 429 and retry:
            time.sleep(retry + 1)
            continue
        log.error("%s амжилтгүй: %s", method, res)
        return False
    return False


def format_post(channel, post_id, text):
    link = f"https://t.me/{channel}/{post_id}"
    parts = [f"📢 @{channel}"]
    if text:
        parts.append(text)
    parts.append(f"🔗 {link}")
    return "\n\n".join(parts)


def send_text(text):
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rstrip() + "…"
    return tg_call("sendMessage", {"chat_id": CHAT_ID, "text": text})


def send_post(channel, post_id, text, media_items):
    message = format_post(channel, post_id, text)

    files_ok = []  # [(төрөл, байт), ...]
    for kind, url in media_items[:MAX_MEDIA]:
        try:
            files_ok.append((kind, download(url)))
        except Exception as e:
            log.warning("%s татаж чадсангүй (%s): %s", kind, url, e)

    # Зураг/видеогүй пост → энгийн текст
    if not files_ok:
        return send_text(message)

    caption_fits = len(message) <= MAX_CAPTION
    ext = {"photo": "jpg", "video": "mp4"}

    if len(files_ok) == 1:
        kind, blob = files_ok[0]
        data = {"chat_id": CHAT_ID}
        if caption_fits:
            data["caption"] = message
        if kind == "video":
            data["supports_streaming"] = "true"
        method = "sendVideo" if kind == "video" else "sendPhoto"
        ok = tg_call(method, data, files={kind: (f"file.{ext[kind]}", blob)})
    else:
        media, files = [], {}
        for i, (kind, blob) in enumerate(files_ok):
            item = {"type": kind, "media": f"attach://m{i}"}
            if i == 0 and caption_fits:
                item["caption"] = message
            media.append(item)
            files[f"m{i}"] = (f"m{i}.{ext[kind]}", blob)
        ok = tg_call("sendMediaGroup",
                     {"chat_id": CHAT_ID, "media": json.dumps(media, ensure_ascii=False)},
                     files=files)

    if not ok:
        return False

    # Текст урт бол зураг/видеоны дараа тусад нь илгээнэ
    if not caption_fits:
        time.sleep(1)
        if not send_text(message):
            log.warning("Медиа явсан ч текст явсангүй (#%s)", post_id)
    return True


# ---------- Давхардал шалгах ----------
def text_key(text):
    """Постын текстээс линк, эможи, тэмдэгтийг хасаад харьцуулах түлхүүр гаргана."""
    t = re.sub(r"https?://\S+|t\.me/\S+|@\w+", " ", text.lower())
    t = "".join(ch for ch in t if ch.isalnum())
    return t[:300] if len(t) >= 20 else ""  # хэт богино текстийг харьцуулахгүй


# ---------- Гол логик ----------
def check_channel(channel, state):
    posts = fetch_posts(channel)
    if not posts:
        log.warning("%s: пост олдсонгүй", channel)
        return

    last_seen = state.get(channel)
    newest = posts[-1][0]

    # Анх удаа: хуучин постоор группээ дүүргэхгүйн тулд зөвхөн тэмдэглэнэ
    if last_seen is None:
        state[channel] = newest
        save_state(state)
        log.info("%s: эхлүүллээ (сүүлийн пост #%s)", channel, newest)
        return

    seen = state.setdefault("_seen", [])
    for post_id, text, media in posts:
        if post_id <= last_seen:
            continue
        key = text_key(text)
        if key and key in seen:
            log.info("%s: #%s давхардсан тул алгаслаа", channel, post_id)
            state[channel] = post_id
            save_state(state)
            continue
        if send_post(channel, post_id, text, media):
            log.info("%s: #%s илгээлээ (%d зураг/видео)", channel, post_id, len(media))
            state[channel] = post_id
            if key:
                seen.append(key)
                del seen[:-DEDUP_KEEP]
            save_state(state)
            time.sleep(2)
        else:
            break  # дараагийн шалгалтаар дахин оролдоно


def run_all(state):
    for channel in CHANNELS:
        try:
            check_channel(channel, state)
        except Exception as e:
            log.error("%s: алдаа гарлаа: %s", channel, e)


def get_chat_id():
    r = requests.get(API.format(token=BOT_TOKEN, method="getUpdates"), timeout=30)
    data = r.json()
    if not data.get("ok"):
        print("Алдаа:", data)
        return
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("my_chat_member") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name")
    if not chats:
        print("Чат олдсонгүй. Ботоо группт нэмээд /start гэж бичээд дахин ажиллуулна уу.")
        return
    print("Олдсон чатууд:")
    for cid, title in chats.items():
        print(f"  {cid}  ->  {title}")


def main():
    if not BOT_TOKEN:
        sys.exit("BOT_TOKEN тохируулаагүй байна.")
    if "--get-chat-id" in sys.argv:
        get_chat_id()
        return
    if not CHAT_ID:
        sys.exit("CHAT_ID тохируулаагүй байна. Эхлээд --get-chat-id ажиллуулна уу.")

    state = load_state()
    if "--once" in sys.argv:
        run_all(state)
        return

    log.info("Бот ажиллаж эхэллээ. Сувгууд: %s", ", ".join(CHANNELS))
    while True:
        run_all(state)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
