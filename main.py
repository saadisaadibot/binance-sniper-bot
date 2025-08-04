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

# 📦 جلب رموز العملات المتاحة في Bitvavo
def get_bitvavo_symbols():
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets")
        data = res.json()
        return set(m["market"].split("-")[0].upper() for m in data if m["market"].endswith("-EUR"))
    except:
        return set()

BITVAVO_SYMBOLS = get_bitvavo_symbols()

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

# 🔁 جلب الترندات كل دقيقة
def update_trends_loop():
    while True:
        try:
            trends = set(get_coingecko_trends() + get_google_trends())
            filtered = [symbol for symbol in trends if symbol in BITVAVO_SYMBOLS]
            new_coins = []

            for symbol in filtered:
                key = f"{WATCH_KEY}:{symbol}"
                if not r.exists(key):
                    r.setex(key, 1800, "1")  # راقب نصف ساعة
                    r.sadd("watched_trend_coins", symbol)
                    new_coins.append(symbol)
                    threading.Thread(target=watch_price, args=(symbol,), daemon=True).start()

            if new_coins:
                send_message(f"📡 بدأ مراقبة: {' '.join(new_coins)}")

        except Exception as e:
            print("✖️ خطأ بجلب الترندات:", e)

        time.sleep(60)

# 👁️‍🗨️ راقب السعر
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

        # انفجار 1.5% خلال 1s
        if last_price and last_time:
            change = (price - last_price) / last_price * 100
            if change >= 1.5 and (now - last_time) <= 1:
                send_message(f"🚀 انفجار 1s: {symbol} ارتفع {change:.2f}%")

        # انفجار 2.5% خلال 5s
        if price_5s_ago and time_5s_ago:
            change = (price - price_5s_ago) / price_5s_ago * 100
            if change >= 2.5 and (now - time_5s_ago) <= 5:
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

# 🛰️ نقطة التشغيل
@app.route("/")
def home():
    return "Trend Sniper is alive ✅", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify(ok=True)

    text = data["message"].get("text", "").strip().lower()

    if text == "ابدأ":
        r.set("trendbot_running", "1")
        send_message("✅ بدأ العمل على مراقبة الترند.")
    elif text == "قف":
        r.set("trendbot_running", "0")
        send_message("🛑 تم إيقاف مراقبة الترند.")
    elif text == "الترند":
        coins = r.smembers("watched_trend_coins")
        if coins:
            msg = "👁️‍🗨️ العملات التي تتم مراقبتها الآن:\n" + " ".join(c.decode() for c in coins)
        else:
            msg = "🚫 لا توجد عملات قيد المراقبة حالياً."
        send_message(msg)
    elif text == "انسى كل شي":
        r.delete("watched_trend_coins")
        for key in r.scan_iter(f"{WATCH_KEY}:*"):
            r.delete(key)
        send_message("🧹 تم حذف كل العملات من Redis وقائمة المراقبة.")

    return jsonify(ok=True)

# 🚀 بدء التشغيل
if __name__ == "__main__":
    threading.Thread(target=update_trends_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)