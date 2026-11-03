"""
Ард-ын Telegram сувгуудын шинэ постыг (текст + зураг) таны группт
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
MAX_PHOTOS = 10      # нэг удаа илгээх зургийн дээд тоо (Telegram: 10)
# ==================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ard_bot")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ArdForwardBot/1.1)"}
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
    """Сувгийн вэб хуудаснаас (пост_дугаар, текст, [зургийн_линкүүд]) гаргана."""
    soup = BeautifulSoup(html_text, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            post_id = int(msg["data-post"].split("/")[-1])
        except (ValueError, KeyError):
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""

        photos = []
        for a in msg.select("a.tgme_widget_message_photo_wrap"):
            m = BG_URL.search(a.get("style", ""))
            if m and m.group(1) not in photos:
                photos.append(m.group(1))

        posts.append((post_id, text, photos))
    posts.sort(key=lambda p: p[0])
    return posts


def fetch_posts(channel):
    r = requests.get(f"https://t.me/s/{channel}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_posts(r.text)


def download(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
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


def send_post(channel, post_id, text, photo_urls):
    message = format_post(channel, post_id, text)

    images = []
    for url in photo_urls[:MAX_PHOTOS]:
        try:
            images.append(download(url))
        except Exception as e:
            log.warning("Зураг татаж чадсангүй (%s): %s", url, e)

    # Зураггүй пост → энгийн текст
    if not images:
        return send_text(message)

    caption_fits = len(message) <= MAX_CAPTION

    if len(images) == 1:
        data = {"chat_id": CHAT_ID}
        if caption_fits:
            data["caption"] = message
        ok = tg_call("sendPhoto", data, files={"photo": ("photo.jpg", images[0])})
    else:
        media, files = [], {}
        for i, img in enumerate(images):
            item = {"type": "photo", "media": f"attach://p{i}"}
            if i == 0 and caption_fits:
                item["caption"] = message
            media.append(item)
            files[f"p{i}"] = (f"p{i}.jpg", img)
        ok = tg_call("sendMediaGroup",
                     {"chat_id": CHAT_ID, "media": json.dumps(media, ensure_ascii=False)},
                     files=files)

    if not ok:
        return False

    # Текст урт бол зургийн дараа тусад нь илгээнэ
    if not caption_fits:
        time.sleep(1)
        if not send_text(message):
            log.warning("Зураг явсан ч текст явсангүй (#%s)", post_id)
    return True


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

    for post_id, text, photos in posts:
        if post_id <= last_seen:
            continue
        if send_post(channel, post_id, text, photos):
            log.info("%s: #%s илгээлээ (%d зураг)", channel, post_id, len(photos))
            state[channel] = post_id
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
