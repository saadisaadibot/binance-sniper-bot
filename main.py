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
WALL_KEY = "sniper_wall_enabled"
TOTO_WEBHOOK = "https://totozaghnot-production.up.railway.app/webhook"
NEMS_WEBHOOK = "https://alnemsbot-production.up.railway.app/webhook"

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except:
        pass

def watch_price(symbol):
    stream = f"{symbol.lower()}@ticker"
    url = f"wss://stream.binance.com:9443/ws/{stream}"

    last_price = None
    last_time = None
    price_5s_ago = None
    time_5s_ago = None

    def on_message(ws, message):
        nonlocal last_price, last_time, price_5s_ago, time_5s_ago
        if r.get(IS_RUNNING_KEY) != b"1":
            ws.close()
            return

        data = json.loads(message)
        price = float(data['c'])
        now = time.time()
        coin = symbol.replace("USDT", "")

        # 🔥 إشعار تــوتــو
        if last_price and last_time:
            price_change = (price - last_price) / last_price * 100
            time_diff = now - last_time
            if price_change >= 1.5 and time_diff <= 1:
                msg = f"اشتري {coin} يا توتو sniper"
                send_message(msg)
                try:
                    requests.post(TOTO_WEBHOOK, json={"message": {"text": msg}})
                except:
                    pass

        if price_5s_ago and time_5s_ago:
            change_5s = (price - price_5s_ago) / price_5s_ago * 100
            diff_5s = now - time_5s_ago
            if change_5s >= 2.5 and diff_5s <= 5:
                msg = f"اشتري {coin} يا توتو sniper"
                send_message(msg)
                try:
                    requests.post(TOTO_WEBHOOK, json={"message": {"text": msg}})
                except:
                    pass

        # 👀 إشعار الجدار (نمس)
        if r.get(WALL_KEY) == b"1":
            if last_price and last_time:
                price_change = (price - last_price) / last_price * 100
                time_diff = now - last_time
                if price_change >= 1 and time_diff <= 1:
                    msg = f"اشتري {coin} يا نمس Sniper"
                    send_message(msg)
                    try:
                        requests.post(NEMS_WEBHOOK, json={"message": {"text": msg}})
                    except:
                        pass

            if price_5s_ago and time_5s_ago:
                change_5s = (price - price_5s_ago) / price_5s_ago * 100
                diff_5s = now - time_5s_ago
                if change_5s >= 1.8 and diff_5s <= 3.5:
                    msg = f"اشتري {coin} يا نمس Sniper"
                    send_message(msg)
                    try:
                        requests.post(NEMS_WEBHOOK, json={"message": {"text": msg}})
                    except:
                        pass

        last_price = price
        last_time = now
        if not time_5s_ago or (now - time_5s_ago) >= 5:
            price_5s_ago = price
            time_5s_ago = now

    def on_error(ws, error):
        print(f"[{symbol}] خطأ:", error)

    def on_close(ws):
        print(f"[{symbol}] تم الإغلاق - إعادة التشغيل...")
        time.sleep(2)
        threading.Thread(target=watch_price, args=(symbol,), daemon=True).start()

    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

def fetch_bitvavo_top_symbols():
    try:
        url = "https://api.bitvavo.com/v2/ticker/24h"
        res = requests.get(url)
        data = res.json()

        eur_coins = [
            d for d in data 
            if d.get("market", "").endswith("-EUR") and len(d.get("market", "")) > 5
        ]

        for coin in eur_coins:
            try:
                open_price = float(coin.get("open", "0"))
                last_price = float(coin.get("last", "0"))
                coin["customChange"] = ((last_price - open_price) / open_price) * 100 if open_price > 0 else -999
            except:
                coin["customChange"] = -999

        sorted_coins = sorted(eur_coins, key=lambda x: x["customChange"], reverse=True)
        return [coin["market"].replace("-EUR", "").upper() for coin in sorted_coins[:80]]
    except Exception as e:
        print("فشل جلب العملات من Bitvavo:", e)
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

        matched = []
        for c in bitvavo:
            for pair in [f"{c}USDT", f"{c}BTC", f"{c}ETH", f"{c}BNB"]:
                if pair in binance:
                    matched.append(pair)
                    break

        if matched:
            for sym in matched:
                r.sadd("coins", sym)
            send_message("📡 العملات المرصودة:\n" + " ".join([f"سجل {m.replace('USDT', '')}" for m in matched]))
        else:
            send_message("🚫 لا توجد عملات قابلة للمراقبة حالياً.")

        time.sleep(600)

def watcher_loop():
    watched = set()
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue
        symbols = {c.decode() for c in r.smembers("coins")}
        for sym in symbols - watched:
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
        coin_list = [c.decode().replace("USDT", "") for c in r.smembers("coins")]
        if coin_list:
            send_message("📡 العملات المرصودة:\n" + " ".join([f"سجل {m}" for m in coin_list]))
    elif text == "stop":
        r.set(IS_RUNNING_KEY, "0")
        send_message("🛑 تم إيقاف Sniper مؤقتًا.")
    elif text == "السجل":
        coin_list = [c.decode().replace("USDT", "") for c in r.smembers("coins")]
        send_message("📡 العملات المرصودة:\n" + ("\n".join(coin_list) if coin_list else "🚫 لا توجد عملات."))
    elif text == "افتح الجدار":
        r.set(WALL_KEY, "1")
        send_message("🟩 تم تفعيل جدار سنايبر.")
    elif text == "اغلق الجدار":
        r.set(WALL_KEY, "0")
        send_message("🟥 تم إغلاق جدار سنايبر.")

    return jsonify(ok=True)

if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    r.set(WALL_KEY, "0")
    threading.Thread(target=update_symbols_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)