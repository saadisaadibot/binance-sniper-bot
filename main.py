import os
import json
import time
import redis
import threading
import requests
from flask import Flask, request, jsonify
from websocket import WebSocketApp

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
IS_RUNNING_KEY = "sniper_running"

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("فشل إرسال الرسالة:", e)

def watch_price(symbol):
    stream = f"{symbol.lower()}@ticker"
    url = f"wss://stream.binance.com:9443/ws/{stream}"

    last_price = None
    last_time = None

    def on_message(ws, message):
        nonlocal last_price, last_time
        if r.get(IS_RUNNING_KEY) != b"1":
            ws.close()
            return
        data = json.loads(message)
        price = float(data['c'])
        now = time.time()
        if last_price and last_time:
            price_change = (price - last_price) / last_price * 100
            time_diff = now - last_time
            if price_change >= 2 and time_diff <= 1:
                coin = symbol.replace("USDT", "")
                send_message(f"اشتري {coin} يا توتو sniper")
        last_price = price
        last_time = now

    def on_error(ws, error):
        print(f"[{symbol}] خطأ:", error)

    def on_close(ws):
        print(f"[{symbol}] تم الإغلاق")

    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

def fetch_bitvavo_top_symbols():
    try:
        url = "https://api.bitvavo.com/v2/ticker/24h"
        res = requests.get(url)
        data = res.json()
        eur_coins = [
            d for d in data
            if d.get("market", "").endswith("-EUR") and "priceChangePercentage" in d
        ]
        sorted_coins = sorted(eur_coins, key=lambda x: float(x["priceChangePercentage"]), reverse=True)
        return [coin["market"].replace("-EUR", "") for coin in sorted_coins[:20]]
    except Exception as e:
        print("فشل جلب العملات من Bitvavo:", e)
        return []

def fetch_binance_symbols():
    try:
        res = requests.get("https://api.binance.com/api/v3/exchangeInfo")
        data = res.json()
        return set(s['symbol'] for s in data['symbols'])
    except:
        return set()

def update_symbols_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue
        r.delete("coins")
        bitvavo = fetch_bitvavo_top_symbols()
        binance = fetch_binance_symbols()
        matched = [c for c in bitvavo if f"{c}USDT" in binance]
        for sym in matched:
            r.sadd("coins", f"{sym}USDT")
        send_message("📡 العملات المرصودة:\n" + " ".join([f"سجل {m}" for m in matched]))
        time.sleep(600)

def watcher_loop():
    watched = set()
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue
        coins = r.smembers("coins")
        symbols = {c.decode() for c in coins}
        new_symbols = symbols - watched
        for sym in new_symbols:
            threading.Thread(target=watch_price, args=(sym,), daemon=True).start()
            watched.add(sym)
        time.sleep(1)

@app.route("/")
def home():
    return "🔥 Sniper Mode is Live", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify(success=True)
    text = data["message"].get("text", "").strip().lower()
    if text == "play":
        r.set(IS_RUNNING_KEY, "1")
        send_message("✅ بدأ التشغيل Sniper.")
    elif text == "stop":
        r.set(IS_RUNNING_KEY, "0")
        send_message("🛑 تم إيقاف Sniper مؤقتًا.")
    elif text == "السجل":
        coins = r.smembers("coins")
        coin_list = [c.decode().replace("USDT", "") for c in coins]
        if coin_list:
            send_message("📡 العملات المرصودة:\n" + "\n".join(coin_list))
        else:
            send_message("🚫 لا توجد عملات قيد المراقبة حالياً.")
    return jsonify(ok=True)

if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    threading.Thread(target=update_symbols_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)