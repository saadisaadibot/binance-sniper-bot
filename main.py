import os
import json
import time
import redis
import threading
import requests
from flask import Flask, request
from websocket import WebSocketApp

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
IS_RUNNING_KEY = "sniper_running"
TOTO_WEBHOOK = "https://totozaghnot-production.up.railway.app/webhook"

# ========== إرسال إشعار إلى توتو ==========
def send_toto(coin):
    try:
        requests.post(TOTO_WEBHOOK, json={"message": f"اشتري {coin} يا توتو sniper"})
    except Exception as e:
        print("فشل إرسال إلى توتو:", e)

# ========== إرسال رسالة تيليغرام ==========
def send_message(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": text}
        requests.post(url, data=data)
    except Exception as e:
        print("فشل إرسال رسالة:", e)

# ========== جلب سعر اليورو مقابل الدولار ==========
def get_usd_rate():
    try:
        res = requests.get("https://api.exchangerate.host/latest?base=EUR&symbols=USD")
        return res.json()["rates"]["USD"]
    except:
        return 1.1  # افتراضي

# ========== جلب أعلى 50 عملة من Bitvavo ==========
def get_top_50_symbols():
    try:
        res = requests.get("https://api.bitvavo.com/v2/ticker/price")
        prices = res.json()
        changes = {}
        for item in prices:
            symbol = item["market"]
            if not symbol.endswith("-EUR"):
                continue
            try:
                ticker = requests.get(f"https://api.bitvavo.com/v2/{symbol}/ticker/24h").json()
                change = float(ticker.get("priceChangePercentage", 0))
                changes[symbol] = change
            except:
                continue
        top = sorted(changes.items(), key=lambda x: x[1], reverse=True)
        return [s[0] for s in top[:50]]
    except:
        return []

# ========== جلب أسعار Bitvavo ==========
def fetch_bitvavo_prices():
    try:
        res = requests.get("https://api.bitvavo.com/v2/ticker/price").json()
        return {i["market"].replace("-EUR", ""): float(i["price"]) for i in res if i["market"].endswith("-EUR")}
    except:
        return {}

# ========== مراقبة فرق السعر عبر WebSocket ==========
def watch_best(symbol):
    coin = symbol.replace("USDT", "")
    eur_usd = get_usd_rate()
    print(f"🎯 متابعة {symbol} لمدة دقيقتين...")

    def on_message(ws, message):
        try:
            data = json.loads(message)
            price_binance = float(data['c'])
            price_bitvavo = fetch_bitvavo_prices().get(coin)
            if not price_bitvavo or price_bitvavo < 0.01:
                return
            price_bitvavo_usd = price_bitvavo * eur_usd
            diff = (price_binance - price_bitvavo_usd) / price_bitvavo_usd * 100
            if 0 < diff < 50 and diff >= 3:
                print(f"🚀 فرق {diff:.2f}% - {coin}")
                send_toto(coin)
                ws.close()
        except Exception as e:
            print("❌ خطأ في on_message:", e)

    def on_close(ws, code, msg):
        print(f"🔴 WebSocket Closed for {symbol} (code={code}, msg={msg})")

    url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@ticker"
    ws = WebSocketApp(url, on_message=on_message, on_close=on_close)
    thread = threading.Thread(target=ws.run_forever)
    thread.start()
    time.sleep(120)  # دقيقتين
    ws.close()

# ========== بدء دورة المراقبة ==========
def sniper_loop():
    while True:
        try:
            r.set(IS_RUNNING_KEY, "1")
            top_symbols = get_top_50_symbols()
            binance_symbols = [s.replace("-EUR", "USDT") for s in top_symbols if s.replace("-EUR", "USDT").isupper()]
            prices = fetch_bitvavo_prices()
            eur_usd = get_usd_rate()
            best = None
            best_diff = -100

            for b_symbol in binance_symbols:
                coin = b_symbol.replace("USDT", "")
                if coin not in prices:
                    continue
                try:
                    res = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={b_symbol}")
                    price_binance = float(res.json()["price"])
                    price_bitvavo = prices[coin] * eur_usd
                    diff = (price_binance - price_bitvavo) / price_bitvavo * 100
                    if 0 < diff < 50 and diff > best_diff:
                        best_diff = diff
                        best = b_symbol
                except:
                    continue

            if best:
                send_message(f"🎯 أفضل فرق سعري: {best} ({best_diff:.2f}%)")
                watch_best(best)
                send_message("🐰 هرب الأرنب..")
            else:
                send_message("❌ لم يتم العثور على عملة مناسبة.")
        except Exception as e:
            print("خطأ في sniper_loop:", e)

        time.sleep(600)  # كل 10 دقائق

# ========== تشغيل التطبيق ==========
if __name__ == "__main__":
    threading.Thread(target=sniper_loop).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))