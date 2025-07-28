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
TOTO_WEBHOOK = "https://totozaghnot-production.up.railway.app/webhook"

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
        # جلب رموز Bitvavo أولاً
        bitvavo_symbols = fetch_bitvavo_symbols()
        if not bitvavo_symbols:
            return []

        # جلب رموز Binance النشطة
        exchange_info = requests.get("https://api.binance.com/api/v3/exchangeInfo").json()
        binance_usdt_pairs = [
            s["symbol"] for s in exchange_info["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
        ]

        # مطابقة الرموز بين Bitvavo و Binance
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
                time.sleep(0.05)  # منع ضغط على Binance
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

        # شرط 1: 1.5% خلال ثانية
        if last_price and last_time:
            change = (price - last_price) / last_price * 100
            diff = now - last_time
            if change >= 1.5 and diff <= 1:
                msg = f"اشتري {coin} يا توتو sniper"
                send_message(msg)
                try:
                    requests.post(TOTO_WEBHOOK, json={"message": {"text": msg}})
                except:
                    pass

        # شرط 2: 2.5% خلال 5 ثواني
        if price_5s_ago and time_5s_ago:
            change = (price - price_5s_ago) / price_5s_ago * 100
            diff = now - time_5s_ago
            if change >= 2.5 and diff <= 5:
                msg = f"اشتري {coin} يا توتو sniper"
                send_message(msg)
                try:
                    requests.post(TOTO_WEBHOOK, json={"message": {"text": msg}})
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