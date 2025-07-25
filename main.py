import os
import json
import time
import redis
import requests
import threading
from flask import Flask, request, jsonify
from websocket import WebSocketApp

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
IS_RUNNING_KEY = "sniper_running"
TOUTO_WEBHOOK = "https://totozaghnot-production.up.railway.app"

def send_toto(coin):
    try:
        requests.post(TOUTO_WEBHOOK, json={"message": f"اشتري {coin} يا توتو sniper"})
    except:
        pass

def send_message(text):
    chat_id = os.getenv("CHAT_ID")
    token = os.getenv("BOT_TOKEN")
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": text})

def get_usd_rate():
    try:
        res = requests.get("https://api.exchangerate.host/latest?base=EUR&symbols=USD")
        return float(res.json()["rates"]["USD"])
    except:
        return 1.08

def fetch_bitvavo_prices():
    try:
        url = "https://api.bitvavo.com/v2/ticker/price"
        data = requests.get(url).json()
        return {item['market'].replace("-EUR", ""): float(item['price']) for item in data if item['market'].endswith("-EUR")}
    except:
        return {}

def fetch_binance_symbols():
    try:
        res = requests.get("https://api.binance.com/api/v3/exchangeInfo")
        return set(s['symbol'] for s in res.json()['symbols'])
    except:
        return set()

def fetch_binance_top():
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        res = requests.get(url).json()
        sorted_data = sorted(res, key=lambda x: float(x['priceChangePercent']), reverse=True)
        return [d['symbol'] for d in sorted_data if d['symbol'].endswith("USDT")][:50]
    except:
        return []

def find_best_arbitrage():
    eur_usd = get_usd_rate()
    bitvavo = fetch_bitvavo_prices()
    top = fetch_binance_top()
    binance_symbols = fetch_binance_symbols()

    best = None
    best_diff = 0

    for symbol in top:
        coin = symbol.replace("USDT", "")
        if symbol not in binance_symbols or coin not in bitvavo:
            continue
        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
            res = requests.get(url).json()
            price_binance = float(res['price'])
            price_bitvavo_usd = bitvavo[coin] * eur_usd
            diff = (price_binance - price_bitvavo_usd) / price_bitvavo_usd * 100
            if 0 < diff > best_diff:
                best = symbol
                best_diff = diff
        except:
            continue
    return best

def watch_best(symbol):
    coin = symbol.replace("USDT", "")
    bitvavo_symbol = f"{coin}-EUR"
    eur_usd = get_usd_rate()
    print(f"🎯 متابعة {symbol} لمدة دقيقتين...")

    def on_message(ws, message):
        try:
            data = json.loads(message)
            price_binance = float(data['c'])
            price_bitvavo = fetch_bitvavo_prices().get(coin)
            if not price_bitvavo:
                return
            price_bitvavo_usd = price_bitvavo * eur_usd
            diff = (price_binance - price_bitvavo_usd) / price_bitvavo_usd * 100
            if diff >= 3:
                send_toto(coin)
                ws.close()
        except:
            pass

    def on_close(ws): print("📴 المراقبة انتهت.")
    def on_error(ws, err): print("❌ WebSocket Error:", err)

    url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@ticker"
    ws = WebSocketApp(url, on_message=on_message, on_close=on_close, on_error=on_error)

    def run():
        ws.run_forever()

    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()
    time.sleep(120)
    if thread.is_alive():
        send_message("🐰 هرب الأرنب")
        try: ws.close()
        except: pass

def sniper_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue
        best = find_best_arbitrage()
        if best:
            watch_best(best)
        time.sleep(1)

@app.route("/")
def home():
    return "🚀 Sniper PRO V3 Live", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    text = data.get("message", {}).get("text", "").lower()
    if "play" in text:
        r.set(IS_RUNNING_KEY, "1")
        send_message("✅ بدأ Sniper PRO.")
    elif "stop" in text:
        r.set(IS_RUNNING_KEY, "0")
        send_message("🛑 تم إيقاف Sniper PRO.")
    return jsonify(ok=True)

if __name__ == "__main__":
    r.flushall()
    r.set(IS_RUNNING_KEY, "1")
    threading.Thread(target=sniper_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)