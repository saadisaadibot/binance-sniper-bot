import os
import redis
import requests
from flask import Flask, request

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")
r = redis.from_url(REDIS_URL)

@app.route("/", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return "ignored"

    message = data["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.startswith("سجل "):
        content = text.replace("سجل", "").strip()
        if content:
            r.sadd("test_saves", content.upper())
            send_message(f"تم حفظ {content.upper()} ✅", chat_id)
        else:
            send_message("⚠️ لم يتم تحديد محتوى", chat_id)

    return "ok"

def send_message(text, chat_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, data=payload)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)