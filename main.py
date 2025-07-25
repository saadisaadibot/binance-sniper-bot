import os
import redis
import requests
from flask import Flask, request

app = Flask(__name__)

# جلب المتغيرات من البيئة
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")

# الاتصال بـ Redis
r = redis.from_url(REDIS_URL)

# إرسال رسالة تيليغرام (اختياري)
def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("💥 Error sending message:", e)

@app.route("/", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json()
        print("🔥 Received update:", data)

        message = data.get("message", {})
        text = message.get("text", "").strip()
        chat_id = message.get("chat", {}).get("id")

        if text.lower().startswith("سجل"):
            parts = text.split(" ", 1)
            if len(parts) == 2:
                coin = parts[1].strip().upper()
                r.sadd("saved_coins", coin)
                send_message(f"✔️ تم تسجيل {coin}")
            else:
                send_message("⚠️ اكتب اسم العملة بعد 'سجل'")
        return "ok"
    except Exception as e:
        print("❌ ERROR:", e)
        return "error", 500

@app.route("/", methods=["GET"])
def home():
    return "Bot is running."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)