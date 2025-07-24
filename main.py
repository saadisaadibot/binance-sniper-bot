import os
import requests
from flask import Flask, request

app = Flask(__name__)

TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

@app.route("/", methods=["GET"])
def home():
    return "Bot is running!"

@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json()

    # استخرج الشات آي دي والرسالة
    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    # رد بسيط لاختبار البوت
    if text == "/start":
        send_message(chat_id, "أهلا! البوت شغّال تمام ✅")
    elif text == "ايدي":
        send_message(chat_id, f"الشات آي دي تبعك هو: `{chat_id}`", parse_mode="Markdown")

    return {"ok": True}

def send_message(chat_id, text, parse_mode=None):
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    requests.post(f"{TELEGRAM_API}/sendMessage", data=payload)

if __name__ == "__main__":
    app.run(debug=True)
