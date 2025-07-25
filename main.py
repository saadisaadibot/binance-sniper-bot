import os
import redis
from flask import Flask, request
import requests

app = Flask(__name__)

# تحميل المتغيرات من بيئة Railway
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")

r = redis.from_url(REDIS_URL)

# إرسال رسالة إلى تيليغرام
def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text}
    requests.post(url, data=data)

# استقبال رسائل من تيليغرام عبر الويب هوك
@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    print("📨 وصلت رسالة:", data)

    try:
        message = data["message"]["text"]
        chat_id = data["message"]["chat"]["id"]

        if message.startswith("سجل "):
            coin = message.split("سجل ")[1].strip()
            r.sadd("test_saved_coins", coin)
            send_message(f"✅ تم تسجيل: {coin}")
        else:
            send_message("🤖 أرسل أمر بصيغة: سجل ADA")

    except Exception as e:
        print("خطأ في المعالجة:", e)

    return "OK", 200