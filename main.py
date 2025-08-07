import os
import time
import redis
import json
import requests
import threading
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "")
HISTORY_SECONDS = 1800  # 30 دقيقة
ALERT_EXPIRE = 60       # لا إشعارات مكررة خلال 60 ثانية

# إرسال رسالة لتلغرام
def send_message(text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        print("❌ فشل إرسال الرسالة:", e)

# جلب جميع العملات من Bitvavo
def fetch_symbols():
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets")
        data = res.json()
        return [m["market"].replace("-EUR", "") for m in data if m["market"].endswith("-EUR")]
    except:
        return []

# جلب أسعار العملات
def fetch_prices():
    try:
        res = requests.get("https://api.bitvavo.com/v2/ticker/price")
        return {item["market"].replace("-EUR", ""): float(item["price"]) for item in res.json() if item["market"].endswith("-EUR")}
    except:
        return {}

# تخزين السعر الحالي في Redis
def store_prices(prices):
    now = time.time()
    for symbol, price in prices.items():
        key = f"prices:{symbol}"
        r.zadd(key, {price: now})
        r.zremrangebyscore(key, 0, now - HISTORY_SECONDS)

# تحليل السعر ومقارنة التغير
def analyze_price_movements(prices):
    now = time.time()
    for symbol, price_now in prices.items():
        key = f"prices:{symbol}"
        old_5s = r.zrangebyscore(key, now - 5, now, start=0, num=1, withscores=False)
        old_10s = r.zrangebyscore(key, now - 10, now, start=0, num=1, withscores=False)
        old_60s = r.zrangebyscore(key, now - 60, now, start=0, num=1, withscores=False)
        old_180s = r.zrangebyscore(key, now - 180, now, start=0, num=1, withscores=False)

        def calc_change(old):
            if old:
                return ((price_now - float(old[0])) / float(old[0])) * 100
            return 0

        changes = {
            "5s": calc_change(old_5s),
            "10s": calc_change(old_10s),
            "60s": calc_change(old_60s),
            "180s": calc_change(old_180s)
        }

        for tag, change in changes.items():
            if change >= 2:
                last_alert = r.get(f"alerted:{symbol}")
                if last_alert and time.time() - float(last_alert) < ALERT_EXPIRE:
                    return
                r.set(f"alerted:{symbol}", time.time())
                send_message(f"🚀 انفجار {symbol} خلال {tag}: +{change:.2f}%")
                if SAQAR_WEBHOOK:
                    try:
                        requests.post(SAQAR_WEBHOOK, json={"message": f"اشتري {symbol}"})
                    except:
                        print("⚠️ فشل إرسال لـ صقر")
                break

# تشغيل الدورة الدائمة
def run_sniper():
    while True:
        symbols = fetch_symbols()
        prices = fetch_prices()
        prices = {s: p for s, p in prices.items() if s in symbols}
        store_prices(prices)
        analyze_price_movements(prices)
        print(f"⏱️ دورة تمّت على {len(prices)} عملة")
        time.sleep(5)

# 🧠 Webhook لتلقي أوامر تلغرام (مثلاً /السجل)
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        data = request.json
        if "message" not in data:
            return "ok", 200
        msg = data["message"]
        text = msg.get("text", "").lower()
        if "السجل" in text:
            summary = []
            for key in r.scan_iter("prices:*"):
                symbol = key.decode().split(":")[1]
                recent_prices = r.zrange(key, -10, -1)
                if len(recent_prices) >= 2:
                    first = float(recent_prices[0])
                    last = float(recent_prices[-1])
                    change = ((last - first) / first) * 100
                    summary.append((symbol, change))
            top = sorted(summary, key=lambda x: x[1], reverse=True)[:5]
            text = "\n".join([f"{s}: +{c:.2f}%" for s, c in top]) or "لا نتائج"
            send_message("📈 أفضل العملات:\n" + text)
        return "ok", 200
    except Exception as e:
        print("خطأ في Webhook:", e)
        return "error", 500

# ✅ بدء التشغيل بالخيط
threading.Thread(target=run_sniper, daemon=True).start()