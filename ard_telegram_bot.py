"""
Ард-ын Telegram сувгуудын шинэ постыг таны группт автоматаар оруулдаг бот.

Хэрхэн ажилладаг вэ:
  Ард-ын нээлттэй сувгуудын вэб хуудсыг (https://t.me/s/<суваг>) хэдэн минут
  тутамд шалгаж, шинэ пост гарсан бол текст болон линкийг нь таны группт илгээнэ.

Суулгах:
  pip install requests beautifulsoup4

Ажиллуулах:
  1) Chat ID-гаа олох:   python ard_telegram_bot.py --get-chat-id
  2) Ботоо ажиллуулах:   python ard_telegram_bot.py
"""

import json
import logging
import os
import sys
import time

import requests
from bs4 import BeautifulSoup

# ============ ТОХИРГОО ============
# Токеноо кодонд шууд бичихийн оронд орчны хувьсагчаар (environment variable)
# өгөх нь аюулгүй. Хүсвэл "" доторх утгыг өөрөө бөглөж болно.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")  # жишээ: -1001234567890

# Дагах сувгууд (t.me/ дараах нэр). Хүссэнээрээ нэмж, хасаж болно.
CHANNELS = ["ardappofficial", "ardcoin"]

CHECK_INTERVAL = 300  # хэдэн секунд тутамд шалгах вэ (300 = 5 минут)
STATE_FILE = "ard_bot_state.json"  # аль постыг илгээснийг санах файл
MAX_TEXT = 3500  # Telegram мессежийн хязгаар 4096 тэмдэгт
# ==================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ard_bot")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ArdForwardBot/1.0)"}
API = "https://api.telegram.org/bot{token}/{method}"


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


def parse_posts(html_text):
    """Сувгийн вэб хуудаснаас (пост_дугаар, текст) жагсаалт гаргана."""
    soup = BeautifulSoup(html_text, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            post_id = int(msg["data-post"].split("/")[-1])
        except (ValueError, KeyError):
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        posts.append((post_id, text))
    posts.sort()
    return posts


def fetch_posts(channel):
    url = f"https://t.me/s/{channel}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_posts(r.text)


def send_message(text):
    """Группт мессеж илгээнэ. Хэт олон хүсэлт (429) ирвэл хүлээгээд дахин оролдоно."""
    for _ in range(3):
        r = requests.post(
            API.format(token=BOT_TOKEN, method="sendMessage"),
            json={"chat_id": CHAT_ID, "text": text},
            timeout=30,
        )
        data = r.json()
        if data.get("ok"):
            return True
        retry = data.get("parameters", {}).get("retry_after")
        if r.status_code == 429 and retry:
            time.sleep(retry + 1)
            continue
        log.error("Илгээж чадсангүй: %s", data)
        return False
    return False


def format_post(channel, post_id, text):
    link = f"https://t.me/{channel}/{post_id}"
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rstrip() + "…"
    body = text if text else "(зураг/видео пост)"
    return f"📢 @{channel}\n\n{body}\n\n🔗 {link}"


def check_channel(channel, state):
    posts = fetch_posts(channel)
    if not posts:
        log.warning("%s: пост олдсонгүй", channel)
        return

    last_seen = state.get(channel)
    newest = posts[-1][0]

    # Анх удаа ажиллахад хуучин постуудаар группээ дүүргэхгүйн тулд
    # зөвхөн хамгийн сүүлийн дугаарыг тэмдэглээд өнгөрнө.
    if last_seen is None:
        state[channel] = newest
        save_state(state)
        log.info("%s: эхлүүллээ (сүүлийн пост #%s). Үүнээс хойших шинэ постыг илгээнэ.",
                 channel, newest)
        return

    for post_id, text in posts:
        if post_id <= last_seen:
            continue
        if send_message(format_post(channel, post_id, text)):
            log.info("%s: #%s илгээлээ", channel, post_id)
            state[channel] = post_id
            save_state(state)
            time.sleep(2)  # группт дараалан хэт хурдан бичихгүйн тулд
        else:
            break  # дараагийн шалгалтаар дахин оролдоно


def get_chat_id():
    """Бот нэмсэн группынхаа Chat ID-г олоход туслана."""
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
        print("Чат олдсонгүй. Ботоо группт нэмээд группт /start гэж бичээд дахин ажиллуулна уу.")
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

    # --once: нэг удаа шалгаад дуусна (GitHub Actions дээр ажиллуулахад)
    if "--once" in sys.argv:
        for channel in CHANNELS:
            try:
                check_channel(channel, state)
            except Exception as e:
                log.error("%s: алдаа гарлаа: %s", channel, e)
        return

    log.info("Бот ажиллаж эхэллээ. Сувгууд: %s", ", ".join(CHANNELS))
    while True:
        for channel in CHANNELS:
            try:
                check_channel(channel, state)
            except Exception as e:  # сүлжээний алдаа гарсан ч бот зогсохгүй
                log.error("%s: алдаа гарлаа: %s", channel, e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
