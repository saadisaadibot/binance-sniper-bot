import os
import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_message(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": text}
        response = requests.post(url, data=data)
        print("رسالة تم إرسالها لتلغرام ✅", response.text)
    except Exception as e:
        print("خطأ أثناء الإرسال لتلغرام ❌:", str(e))

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print("📩 تم استلام Webhook:", data)
        return '', 200
    except Exception as e:
        print("خطأ أثناء معالجة Webhook:", str(e))
        return '', 500

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive!", 200

if __name__ == "__main__":
    try:
        send_message("✅ البوت اشتغل تمام 🙌")
        port = int(os.getenv("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    except Exception as e:
        print("🔥 البوت وقع بخطأ:", str(e))
        send_message(f"❌ خطأ أثناء تشغيل البوت:\n{str(e)}")