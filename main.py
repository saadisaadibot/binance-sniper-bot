import os
import time
import json
import redis
import requests
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_URL = "https://totozaghnot-production.up.railway.app"

IS_RUNNING_KEY = "sniper_running"
TARGET_SYMBOL_KEY = "sniper_top_symbol"
NOTIFIED_KEY = "sniper_notified"

def send_message(text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": text})
    except:
        pass

def send_to_toto(coin):
    try:
        requests.post(WEBHOOK_URL, json={"text": f"اشتري {coin} يا توتو sniper"})
    except:
        pass

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

def fetch_binance_price(symbol):
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        res = requests.get(url)
        return float(res.json()["price"]) if res.status_code == 200 else None
    except:
        return None

def fetch_binance_symbols():
    try:
        res = requests.get("https://api.binance.com/api/v3/exchangeInfo")
        return set(s['symbol'] for s in res.json()['symbols'])
    except:
        return set()

def fetch_bitvavo_top_symbols():
    try:
        res = requests.get("https://api.bitvavo.com/v2/ticker/24h")
        data = res.json()
        eur = [x for x in data if x.get("market", "").endswith("-EUR")]
        sorted_data = sorted(eur, key=lambda x: float(x.get("priceChangePercentage", 0)), reverse=True)
        return [x["market"].replace("-EUR", "") for x in sorted_data[:50]]
    except:
        return []

def get_price_diff(coin, eur_usd):
    binance_price = fetch_binance_price(f"{coin}USDT")
    bitvavo_price = fetch_bitvavo_price(f"{coin}-EUR")
    if not binance_price or not bitvavo_price:
        return None, None, None
    bitvavo_usd = bitvavo_price * eur_usd
    diff = ((binance_price - bitvavo_usd) / bitvavo_usd) * 100
    return binance_price, bitvavo_usd, diff

def monitor_top_symbol(symbol):
    eur_usd = get_eur_usd_rate()
    r.set(NOTIFIED_KEY, "0")
    start = time.time()

    while time.time() - start < 120:  # دقيقتين
        if r.get(IS_RUNNING_KEY) != b"1":
            return
        binance, bitvavo, diff = get_price_diff(symbol, eur_usd)
        if not diff: continue
        print(f"[{symbol}] Diff: {diff:.2f}%")

        if diff >= 3 and r.get(NOTIFIED_KEY) != b"1":
            send_to_toto(symbol)
            r.set(NOTIFIED_KEY, "1")
        time.sleep(1)

    if r.get(NOTIFIED_KEY) != b"1":
        send_message("🐇 هرب الأرنب")
    r.set(TARGET_SYMBOL_KEY, "")  # لنبدأ من جديد

def sniper_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(3)
            continue

        if r.get(TARGET_SYMBOL_KEY):
            time.sleep(1)
            continue

        r.delete("sniper_cache")
        eur_usd = get_eur_usd_rate()
        top50 = fetch_bitvavo_top_symbols()
        available = fetch_binance_symbols()

        candidates = []
        for coin in top50:
            if f"{coin}USDT" not in available:
                continue
            b_price, v_price, diff = get_price_diff(coin, eur_usd)
            if diff is not None:
                candidates.append((coin, diff))

        if not candidates:
            time.sleep(10)
            continue

        best = max(candidates, key=lambda x: x[1])
        r.set(TARGET_SYMBOL_KEY, best[0])
        print(f"🎯 أفضل عملة: {best[0]} بفارق {best[1]:.2f}%")
        threading.Thread(target=monitor_top_symbol, args=(best[0],), daemon=True).start()
        time.sleep(30)

@app.route("/")
def home():
    return "🚀 Sniper Precision Mode Active", 200

@app.route("/webhook", methods=["POST"])
def telegram():
    data = request.get_json()
    text = data.get("message", {}).get("text", "").strip().lower()
    if text == "play":
        r.set(IS_RUNNING_KEY, "1")
        r.set(TARGET_SYMBOL_KEY, "")
        send_message("✅ Sniper بدأ التشغيل.")
    elif text == "stop":
        r.set(IS_RUNNING_KEY, "0")
        send_message("🛑 Sniper تم إيقافه.")
    return jsonify(ok=True)

if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    r.set(TARGET_SYMBOL_KEY, "")
    r.delete("sniper_cache")
    threading.Thread(target=sniper_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)