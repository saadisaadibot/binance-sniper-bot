import os
import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
RAILWAY_URL = os.getenv("RAILWAY_URL")  # مثل: https://web-production-1234.up.railway.app

# 1. عند التشغيل: ربط الويب هوك
def set_webhook():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={RAILWAY_URL}"
    response = requests.get(url)
    print("Webhook status:", response.json())

# 2. إرسال رسالة للتيليغرام
def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text}
    response = requests.post(url, data=data)
    print("Send message status:", response.json())

# 3. نقطة الاستقبال
@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    try:
        message = data["message"]["text"]
        sender = data["message"]["chat"]["id"]

        if message.lower().startswith("سجل "):
            coin = message.split(" ")[1].upper()
            print(f"📥 تم طلب تسجيل العملة: {coin}")
            send_message(f"📝 تم تسجيل العملة {coin}")
        else:
            print("📩 أمر غير معروف:", message)

    except Exception as e:
        print("🚨 خطأ:", e)
        send_message(f"🚨 حصل خطأ: {e}")

    return "ok", 200

# 4. تشغيل الخادم و ربط الويب هوك
if __name__ == "__main__":
    set_webhook()
    send_message("✅ البوت اشتغل ورابط الويب هوك جاهز")
    app.run(host="0.0.0.0", port=8080)