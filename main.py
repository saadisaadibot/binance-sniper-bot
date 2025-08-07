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
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "https://saadisaadibot-saqarxbo-production.up.railway.app/")
HISTORY_SECONDS = 1800  # 30 دقيقة
ALERT_EXPIRE = 60       # لا تكرار إشعارات للعملة خلال دقيقة

def send_message(text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        print("فشل إرسال الرسالة:", e)

def fetch_symbols():
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets")
        data = res.json()
        return [m["market"].replace("-EUR", "") for m in data if m["market"].endswith("-EUR")]
    except:
        return []

def fetch_prices():
    try:
        res = requests.get("https://api.bitvavo.com/v2/ticker/price")
        return {item["market"]: float(item["price"]) for item in res.json() if item["market"].endswith("-EUR")}
    except Exception as e:
        print("فشل جلب الأسعار:", e)
        return {}

def store_price(symbol, price):
    key = f"prices:{symbol}"
    ts = int(time.time())
    r.zadd(key, {price: ts})
    r.zremrangebyscore(key, 0, ts - HISTORY_SECONDS)

def get_old_price(symbol, seconds_ago):
    key = f"prices:{symbol}"
    target_ts = int(time.time()) - seconds_ago
    results = r.zrangebyscore(key, target_ts - 2, target_ts + 2, withscores=True)
    if results:
        return float(results[0][0])
    return None

def alert(symbol, tag, percent):
    last_key = f"alerted:{symbol}"
    if r.exists(last_key):
        print(f"⛔ تجاهل الإشعار المكرر لـ {symbol} #{tag}")
        return
    r.setex(last_key, ALERT_EXPIRE, "1")
    message = f"🚀 انفجار {tag}: {symbol} +{percent:.2f}%"
    send_message(message)
    try:
        requests.post(SAQAR_WEBHOOK, json={"text": f"اشتري {symbol}"})
    except Exception as e:
        print("فشل الإرسال إلى صقر:", e)

def analyze_symbol(symbol):
    current = get_old_price(symbol, 0)
    if not current:
        return
    changes = {
        "5s": (5, 2),
        "10s": (10, 3),
        "60s": (60, 4),
        "180s": (180, 6),
        "300s": (300, 7),
    }
    for tag, (sec, threshold) in changes.items():
        old = get_old_price(symbol, sec)
        if not old:
            continue
        diff = ((current - old) / old) * 100
        if diff >= threshold:
            alert(symbol, tag, diff)
            break

def store_loop():
    while True:
        prices = fetch_prices()
        for market, price in prices.items():
            symbol = market.replace("-EUR", "")
            store_price(symbol, price)
        print("✅ تم تخزين الأسعار:", datetime.now().strftime("%H:%M:%S"))
        time.sleep(5)

def analyze_loop():
    while True:
        symbols = fetch_symbols()
        for symbol in symbols:
            threading.Thread(target=analyze_symbol, args=(symbol,)).start()
        time.sleep(30)

@app.route("/")
def home():
    return "صياد الصيادين جاهز ✅"

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json
    if "message" not in data:
        return "", 200
    text = data["message"].get("text", "")
    if "السجل" in text:
        return jsonify(get_summary()), 200
    return "", 200

def get_summary():
    now = int(time.time())
    top_5_5m = []
    top_5_10m = []

    symbols = fetch_symbols()
    for symbol in symbols:
        now_price = get_old_price(symbol, 0)
        old_5m = get_old_price(symbol, 300)
        old_10m = get_old_price(symbol, 600)
        if now_price and old_5m:
            diff5 = ((now_price - old_5m) / old_5m) * 100
            top_5_5m.append((symbol, diff5))
        if now_price and old_10m:
            diff10 = ((now_price - old_10m) / old_10m) * 100
            top_5_10m.append((symbol, diff10))

    top_5_5m = sorted(top_5_5m, key=lambda x: x[1], reverse=True)[:5]
    top_5_10m = sorted(top_5_10m, key=lambda x: x[1], reverse=True)[:5]

    text = "📈 أقوى العملات خلال 5 دقائق:\n"
    for s, p in top_5_5m:
        text += f"{s}: {p:.2f}%\n"

    text += "\n📈 أقوى العملات خلال 10 دقائق:\n"
    for s, p in top_5_10m:
        text += f"{s}: {p:.2f}%\n"

    send_message(text)
    return {"ok": True}

if __name__ == "__main__":
    t1 = threading.Thread(target=store_loop)
    t2 = threading.Thread(target=analyze_loop)
    t1.start()
    t2.start()
    app.run(host="0.0.0.0", port=5000)