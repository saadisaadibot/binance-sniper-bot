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

def fetch_bitvavo_price(symbol):
    try:
        url = f"https://api.bitvavo.com/v2/ticker/price?market={symbol}"
        res = requests.get(url)
        return float(res.json()["price"]) if res.status_code == 200 else None
    except:
        return None

def get_eur_usd_rate():
    try:
        res = requests.get("https://api.exchangerate.host/latest?base=EUR&symbols=USD")
        return float(res.json()["rates"]["USD"])
    except:
        return 1.08

def watch_price(symbol):
    stream = f"{symbol.lower()}@ticker"
    url = f"wss://stream.binance.com:9443/ws/{stream}"

    def on_message(ws, message):
        if r.get(IS_RUNNING_KEY) != b"1":
            ws.close()
            return
        data = json.loads(message)
        price = float(data['c'])  # Binance price in USDT
        coin = symbol.replace("USDT", "")
        bitvavo_symbol = f"{coin}-EUR"
        bitvavo_price = fetch_bitvavo_price(bitvavo_symbol)
        if not bitvavo_price:
            return
        eur_usd = get_eur_usd_rate()
        bitvavo_usd = bitvavo_price * eur_usd
        if price >= bitvavo_usd * 1.05:  # ✅ فرق 5%
            send_message(f"اشتري {coin} يا توتو sniper")

    def on_error(ws, error):
        print(f"[{symbol}] خطأ:", error)

    def on_close(ws):
        print(f"[{symbol}] تم الإغلاق")

    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

def fetch_bitvavo_top_symbols():
    try:
        url = "https://api.bitvavo.com/v2/ticker/24h"
        data = requests.get(url).json()
        eur_coins = [d for d in data if d.get("market", "").endswith("-EUR")]
        sorted_coins = sorted(eur_coins, key=lambda x: float(x.get("priceChangePercentage", 0)), reverse=True)
        return [coin["market"].replace("-EUR", "") for coin in sorted_coins[:20]]
    except:
        return []

def fetch_binance_symbols():
    try:
        res = requests.get("https://api.binance.com/api/v3/exchangeInfo")
        return set(s['symbol'] for s in res.json()['symbols'])
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
    return "🔥 Arbitrage Sniper Mode is Live", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify(success=True)
    text = data["message"].get("text", "").strip().lower()
    if text == "play":
        r.set(IS_RUNNING_KEY, "1")
        send_message("✅ Sniper بدأ التشغيل.")
    elif text == "stop":
        r.set(IS_RUNNING_KEY, "0")
        send_message("🛑 Sniper تم إيقافه مؤقتًا.")
    elif text == "السجل":
        coins = r.smembers("coins")
        coin_list = [c.decode().replace("USDT", "") for c in coins]
        send_message("📡 العملات المرصودة:\n" + "\n".join(coin_list)) if coin_list else send_message("🚫 لا توجد عملات.")
    return jsonify(ok=True)

if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    threading.Thread(target=update_symbols_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)