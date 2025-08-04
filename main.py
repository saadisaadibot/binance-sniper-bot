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

SAQAR_WEBHOOK = "https://saadisaadibot-saqarxbo-production.up.railway.app/webhook"

def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}
        )
    except Exception as e:
        print("فشل إرسال الرسالة:", e)

def fetch_bitvavo_symbols():
    try:
        res = requests.get("https://api.bitvavo.com/v2/markets")
        data = res.json()
        return set(m["market"].replace("-EUR", "").upper() for m in data if m["market"].endswith("-EUR"))
    except:
        return set()

def fetch_binance_top_matched():
    try:
        bitvavo_symbols = fetch_bitvavo_symbols()
        if not bitvavo_symbols:
            return []

        exchange_info = requests.get("https://api.binance.com/api/v3/exchangeInfo").json()
        binance_usdt_pairs = [
            s["symbol"] for s in exchange_info["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
        ]

        matched = [sym for sym in binance_usdt_pairs if sym.replace("USDT", "") in bitvavo_symbols]

        top_changes = []
        for sym in matched:
            try:
                url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=15m&limit=2"
                data = requests.get(url).json()
                if len(data) < 2:
                    continue
                open_price = float(data[-2][1])
                close_price = float(data[-2][4])
                change = ((close_price - open_price) / open_price) * 100
                top_changes.append((sym, change))
                time.sleep(0.05)
            except:
                continue

        sorted_top = sorted(top_changes, key=lambda x: x[1], reverse=True)
        return [s[0] for s in sorted_top[:50]]
    except Exception as e:
        print("فشل جلب البيانات:", e)
        return []

def update_symbols_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue

        r.delete("coins")
        top_symbols = fetch_binance_top_matched()
        if top_symbols:
            for sym in top_symbols:
                r.sadd("coins", sym)
            send_message("📡 العملات المرصودة:\n" + " ".join([f"سجل {s.replace('USDT','')}" for s in top_symbols]))
        else:
            send_message("🚫 لا توجد عملات قابلة للمراقبة حالياً.")

        time.sleep(600)

def notify_buy(coin, tag):
    msg = f"🚀 انفجار {tag}: {coin} #{tag}"
    send_message(msg)
    try:
        requests.post(SAQAR_WEBHOOK, json={"message": {"text": f"اشتري {coin}"}})
    except:
        pass

def watch_price(symbol):
    stream = f"{symbol.lower()}@ticker"
    url = f"wss://stream.binance.com:9443/ws/{stream}"
    last_price = None
    last_time = None
    price_5s = None
    time_5s = None
    price_10s = None
    time_10s = None
    price_60s = None
    time_60s = None

    def on_message(ws, message):
        nonlocal last_price, last_time, price_5s, time_5s, price_10s, time_10s, price_60s, time_60s
        if r.get(IS_RUNNING_KEY) != b"1":
            ws.close()
            return

        data = json.loads(message)
        price = float(data["c"])
        now = time.time()
        coin = symbol.replace("USDT", "")

        # ⏱️ انفجار 2% خلال 5 ثواني
        if price_5s and time_5s:
            change = (price - price_5s) / price_5s * 100
            if change >= 2 and now - time_5s <= 5:
                notify_buy(coin, "5")

        # ⏱️ انفجار 3% خلال 10 ثواني
        if price_10s and time_10s:
            change = (price - price_10s) / price_10s * 100
            if change >= 3 and now - time_10s <= 10:
                notify_buy(coin, "10")

        # ⏱️ انفجار 4% خلال دقيقة
        if price_60s and time_60s:
            change = (price - price_60s) / price_60s * 100
            if change >= 4 and now - time_60s <= 60:
                notify_buy(coin, "60")

        last_price = price
        last_time = now

        if not time_5s or now - time_5s >= 5:
            price_5s = price
            time_5s = now

        if not time_10s or now - time_10s >= 10:
            price_10s = price
            time_10s = now

        if not time_60s or now - time_60s >= 60:
            price_60s = price
            time_60s = now

    def on_error(ws, error):
        print(f"[{symbol}] خطأ:", error)

    def on_close(ws):
        print(f"[{symbol}] تم الإغلاق - إعادة التشغيل...")
        time.sleep(2)
        threading.Thread(target=watch_price, args=(symbol,), daemon=True).start()

    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

def watcher_loop():
    watched = set()
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue
        coins = r.smembers("coins")
        symbols = {c.decode() for c in coins}
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
        coins = r.smembers("coins")
        coin_list = [c.decode().replace("USDT", "") for c in coins]
        if coin_list:
            send_message("📡 العملات المرصودة:\n" + " ".join([f"سجل {m}" for m in coin_list]))
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