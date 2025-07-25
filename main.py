import os
import redis
import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")

r = redis.from_url(REDIS_URL)

# دالة إرسال رسالة للتيليغرام
def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text}
    requests.post(url, data=data)

# المسار الرئيسي للبوت
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json()
    if "message" in data:
        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "")

        # فقط إذا كانت الرسالة تبدأ بـ "سجل"
        if text.lower().startswith("سجل "):
            coin = text.split(" ", 1)[1].strip()
            if coin:
                r.sadd("saved_coins", coin)
                send_message(f"تم تسجيل {coin}")
            else:
                send_message("اكتب اسم العملة بعد 'سجل'")
    return "OK"