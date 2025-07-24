from flask import Flask, request
import os
import redis
import requests

app = Flask(__name__)

# إعداد مفاتيح البيئة
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")

# الاتصال بـ Redis
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# دالة إرسال رسالة إلى تيليغرام
def send_message(text):
    if BOT_TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": text}
        requests.post(url, data=data)

@app.route('/', methods=['POST'])
def telegram_webhook():
    data = request.json
    message = data['message']['text']
    chat_id = data['message']['chat']['id']

    if message.startswith("سجل "):
        code = message.replace("سجل ", "").strip()
        r.rpush("codes", code)
        print(f"✅ تم تسجيل الكود: {code}")
        send_message(f"✅ تم حفظ الكود: {code}")

    return "ok"

if __name__ == '__main__':
    app.run()