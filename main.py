import os
import json
import time
import redis
import threading
import requests
from flask import Flask, request, jsonify
from websocket import WebSocketApp
from pytrends.request import TrendReq

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
WATCH_KEY = "trends:watching"

# 🔥 إرسال رسالة تلغرام
def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}
        )
    except Exception as e:
        print("✖️ فشل إرسال الرسالة:", e)

# 🧠 ترندات Google
def get_google_trends():
    try:
        pytrends = TrendReq()
        df = pytrends.trending_searches(pn="global")
        return [x.strip().upper().replace(" ", "") for x in df[0].tolist()]
    except:
        return []

# 🧠 ترندات CoinGecko
def get_coingecko_trends():
    try:
        url = "https://api.coingecko.com/api/v3/search/trending"
        res = requests.get(url)
        data = res.json()
        coins = [c["item"]["symbol"].upper() for c in data.get("coins", [])]
        return coins
    except:
        return []

# 🔁 جلب ترندات كل دقيقة
def update_trends_loop():
    while True:
        try:
            all = set(get_coingecko_trends() + get_google_trends())
            new_coins = []

            for symbol in all:
                key = f"{WATCH_KEY}:{symbol}"
                if not r.exists(key):
                    r.setex(key, 1800, "1")  # راقب نصف ساعة
                    new_coins.append(symbol)
                    threading.Thread(target=watch_price, args=(symbol,), daemon=True).start()

            if new_coins:
                send_message(f"📡 بدأ مراقبة: {' '.join(new_coins)}")

        except Exception as e:
            print("✖️ خطأ بجلب الترندات:", e)

        time.sleep(60)

# 👁️‍🗨️ راقب السعر من Binance
def watch_price(symbol):
    stream = f"{symbol.lower()}usdt@ticker"
    url = f"wss://stream.binance.com:9443/ws/{stream}"

    last_price = None
    last_time = None
    price_5s_ago = None
    time_5s_ago = None

    def on_message(ws, message):
        nonlocal last_price, last_time, price_5s_ago, time_5s_ago
        data = json.loads(message)
        price = float(data.get("c", 0))
        now = time.time()

        # انفجار 1.5% خلال ثانية
        if last_price and last_time:
            change = (price - last_price) / last_price * 100
            diff = now - last_time
            if change >= 1.5 and diff <= 1:
                send_message(f"🚀 انفجار 1s: {symbol} ارتفع {change:.2f}%")

        # انفجار 2.5% خلال 5 ثواني
        if price_5s_ago and time_5s_ago:
            change = (price - price_5s_ago) / price_5s_ago * 100
            diff = now - time_5s_ago
            if change >= 2.5 and diff <= 5:
                send_message(f"🚀 انفجار 5s: {symbol} ارتفع {change:.2f}%")

        last_price = price
        last_time = now

        if not time_5s_ago or (now - time_5s_ago) >= 5:
            price_5s_ago = price
            time_5s_ago = now

    def on_error(ws, error):
        print(f"[{symbol}] WebSocket Error:", error)

    def on_close(ws):
        print(f"[{symbol}] WebSocket Closed")

    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

# 🛰️ نقطة التأكد من التشغيل
@app.route("/")
def home():
    return "Trend Sniper is alive ✅", 200

# 🚀 بدء التشغيل
if __name__ == "__main__":
    threading.Thread(target=update_trends_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)