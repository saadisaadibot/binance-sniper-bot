import os
import json
import requests
from flask import Flask, request
from pytrends.request import TrendReq

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# إعداد Google Trends
pytrends = TrendReq(hl='en-US', tz=360)

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("فشل إرسال:", e)

def get_google_trending():
    try:
        trending = pytrends.trending_searches(pn="global")
        keywords = [k.strip().upper() for k in trending[0].tolist()[:20]]
        return keywords
    except Exception as e:
        print("مشكلة Google Trends:", e)
        return []

def get_coingecko_trending():
    try:
        res = requests.get("https://api.coingecko.com/api/v3/search/trending")
        coins = res.json()["coins"]
        names = [c["item"]["name"].upper() for c in coins]
        return names
    except:
        return []

def get_trending_coins():
    google_keywords = get_google_trending()
    coingecko_coins = get_coingecko_trending()

    all_combined = list(set(google_keywords + coingecko_coins))
    return all_combined

@app.route("/")
def home():
    return "🚀 TrendBot is alive!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return "no message", 200

    text = data["message"].get("text", "").lower()

    if "ابدأ" in text or "start" in text:
        send_message("🤖 بوت التريند جاهز! أرسل 'الترند' لعرض العملات.")
    elif "الترند" in text or "trend" in text:
        trending = get_trending_coins()
        if trending:
            msg = "🔥 العملات الرائجة الآن:\n" + "\n".join([f"• {c}" for c in trending])
        else:
            msg = "🚫 لم يتم العثور على ترندات حالياً."
        send_message(msg)

    return "ok", 200

if __name__ == "__main__":
    # ضبط Webhook تلقائيًا
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
        requests.post(url, data={"url": WEBHOOK_URL})
    except:
        print("فشل تعيين الويب هوك")

    app.run(host="0.0.0.0", port=8080)