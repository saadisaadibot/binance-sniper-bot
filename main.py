import os
import time
import json
import redis
import threading
import requests
from flask import Flask, request
from websocket import WebSocketApp

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = "https://totozaghnot-production.up.railway.app"
IS_RUNNING_KEY = "sniper_running"

def send_buy_signal(coin):
    payload = {"message": f"اشتري {coin} يا توتو sniper"}
    try:
        requests.post(WEBHOOK_URL, json=payload)
    except Exception as e:
        print(f"فشل إرسال الإشعار لتوتو: {e}")

def fetch_bitvavo_price(symbol):
    try:
        url = f"https://api.bitvavo.com/v2/ticker/price?market={symbol}"
        res = requests.get(url)
        price = float(res.json()["price"])
        if price < 0.01: return None  # تجاهل الأسعار الصغيرة جداً
        return price
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
        return float(res.json()["price"])
    except:
        return None

def monitor_top_coin(top_coin):
    coin = top_coin.replace("USDT", "")
    symbol_bv = f"{coin}-EUR"
    eur_usd = get_eur_usd_rate()

    print(f"🎯 ببدأ التركيز على: {coin}")
    best_diff = 0

    for _ in range(120):  # دقيقتين مراقبة
        if r.get(IS_RUNNING_KEY) != b"1": return
        binance_price = fetch_binance_price(top_coin)
        bitvavo_price = fetch_bitvavo_price(symbol_bv)
        if not binance_price or not bitvavo_price:
            time.sleep(1)
            continue

        bitvavo_usd = bitvavo_price * eur_usd
        diff = ((binance_price - bitvavo_usd) / bitvavo_usd) * 100

        if diff > 50 or diff < 0:  # تجاهل الفروقات الوهمية
            time.sleep(1)
            continue

        print(f"[{coin}] Diff: {diff:.2f}%")
        if diff > 3.5:
            send_buy_signal(coin)
            return
        time.sleep(1)

    print("🐇 هرب الأرنب!")
    time.sleep(1)

def scan_top_50_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue

        try:
            r.delete("coins")
            res = requests.get("https://api.binance.com/api/v3/ticker/24hr")
            coins = sorted(res.json(), key=lambda x: float(x["priceChangePercent"]), reverse=True)
            top50 = [c["symbol"] for c in coins if c["symbol"].endswith("USDT") and not c["symbol"].endswith("BUSD")][:50]
            for coin in top50:
                r.sadd("coins", coin)

            print("🚀 جارٍ فحص أفضل 50 عملة...")

            # تحليل الفروقات السريعة
            best = None
            best_diff = 0
            eur_usd = get_eur_usd_rate()
            for coin in top50:
                coin_name = coin.replace("USDT", "")
                bv_symbol = f"{coin_name}-EUR"

                binance_price = fetch_binance_price(coin)
                bitvavo_price = fetch_bitvavo_price(bv_symbol)
                if not binance_price or not bitvavo_price:
                    continue

                bitvavo_usd = bitvavo_price * eur_usd
                diff = ((binance_price - bitvavo_usd) / bitvavo_usd) * 100

                if diff > 50 or diff < 0: continue  # فلترة القيم الوهمية

                if diff > best_diff:
                    best_diff = diff
                    best = coin

                print(f"[{coin_name}] Diff: {diff:.2f}%")

            if best:
                print(f"🎯 أفضل عملة: {best.replace('USDT','')} {best_diff:.2f}%")
                monitor_top_coin(best)
        except Exception as e:
            print("حدث خطأ في حلقة المراقبة:", e)

@app.route("/")
def home():
    return "Sniper Smart Mode™ جاهز", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    text = data.get("message", {}).get("text", "").lower()
    if text == "play":
        r.set(IS_RUNNING_KEY, "1")
        send_message("✅ بدأ سنايبر الذكي.")
    elif text == "stop":
        r.set(IS_RUNNING_KEY, "0")
        send_message("🛑 تم إيقاف سنايبر.")
    elif text == "السجل":
        coins = r.smembers("coins")
        text = "📡 العملات المرصودة:\n" + "\n".join(c.decode().replace("USDT", "") for c in coins)
        send_message(text)
    return {"ok": True}

def send_message(text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": os.getenv("CHAT_ID"), "text": text})
    except:
        pass

if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    r.delete("coins")
    threading.Thread(target=scan_top_50_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)