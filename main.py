import os
import json
import time
import redis
import threading
import requests
from flask import Flask, request, jsonify
from websocket import WebSocketApp
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
IS_RUNNING_KEY = "sniper_running"
SAQAR_WEBHOOK = "https://saadisaadibot-saqarxbo-production.up.railway.app/"

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

def fetch_change(sym, interval):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit=2"
        res = requests.get(url, timeout=3)
        data = res.json()
        if len(data) < 2:
            return None
        open_price = float(data[-2][1])
        close_price = float(data[-2][4])
        change = ((close_price - open_price) / open_price) * 100
        return (sym, change)
    except:
        return None

def fetch_binance_top_matched():
    try:
        bitvavo_symbols = fetch_bitvavo_symbols()
        if not bitvavo_symbols:
            return []

        exchange_info = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=5).json()
        binance_usdt_pairs = [
            s["symbol"] for s in exchange_info["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
        ]

        matched = [sym for sym in binance_usdt_pairs if sym.replace("USDT", "") in bitvavo_symbols]
        matched = matched[:100]  # نراقب فقط أول 100 عملة لتقليل الضغط

        all_changes = {}

        def collect_top(interval, count):
            local_changes = []
            with ThreadPoolExecutor(max_workers=20) as executor:
                results = executor.map(lambda sym: fetch_change(sym, interval), matched)
                for res in results:
                    if res:
                        local_changes.append(res)

            sorted_changes = sorted(local_changes, key=lambda x: x[1], reverse=True)
            for sym, change in sorted_changes[:count]:
                all_changes[sym] = change

        # ⏱️ جمع من كل فريم
        collect_top("15m", 10)
        collect_top("10m", 10)
        collect_top("5m", 10)

        # 🔄 نرتبهم حسب التغير النهائي بعد الدمج
        sorted_all = sorted(all_changes.items(), key=lambda x: x[1], reverse=True)
        return [s[0] for s in sorted_all]
    except Exception as e:
        print("فشل جلب البيانات:", e)
        return []

def update_symbols_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue

        top_symbols = fetch_binance_top_matched()
        now = time.time()
        count_added = 0

        if top_symbols:
            for sym in top_symbols:
                if not r.hexists("watchlist", sym):
                    r.hset("watchlist", sym, now)
                    count_added += 1

            symbols = [s.replace("USDT", "") for s in top_symbols]
            # send_message("📡 العملات المرصودة:\n" + " ".join([f"سجل {s}" for s in symbols]))
        else:
            send_message("🚫 لا توجد عملات قابلة للمراقبة حالياً.")

        # 🧹 حذف العملات التي انتهت مدة مراقبتها
        cleanup_old_coins()
        time.sleep(180)  # كل 3 دقائق

def cleanup_old_coins():
    now = time.time()
    for sym, ts in r.hgetall("watchlist").items():
        try:
            t = float(ts.decode())
            if now - t > 2400:  # 30 دقيقة
                r.hdel("watchlist", sym.decode())
        except:
            continue

def notify_buy(coin, tag):
    msg = f"🚀 انفجار {tag}: {coin} #{tag}"
    send_message(msg)

    try:
        payload = {"message": {"text": f"اشتري {coin}"}}
        resp = requests.post(SAQAR_WEBHOOK, json=payload)
        print(f"🛰️ إرسال إلى صقر: {payload}")
        print(f"🔁 رد صقر: {resp.status_code} - {resp.text}")
    except Exception as e:
        print("❌ فشل الإرسال إلى صقر:", e)

def watch_price(symbol):
    stream = f"{symbol.lower()}@trade"
    url = f"wss://stream.binance.com:9443/ws/{stream}"
    price_5s = price_10s = price_120s = None
    time_5s = time_10s = time_120s = None

    def on_message(ws, message):
        nonlocal price_5s, time_5s, price_10s, time_10s, price_120s, time_120s
        if r.get(IS_RUNNING_KEY) != b"1":
            ws.close()
            return

        data = json.loads(message)

        if "p" not in data:
            print(f"[{symbol}] ⚠️ لا يوجد المفتاح 'p' في الرسالة: {data}")
            return

        try:
            price = float(data["p"])
        except Exception as e:
            print(f"[{symbol}] خطأ في تحويل السعر: {e}")
            return

        now = time.time()
        coin = symbol.replace("USDT", "")

        # 💥 إشارات الانفجار
        if price_5s and now - time_5s <= 5 and (price - price_5s) / price_5s * 100 >= 1.6:
            notify_buy(coin, "5")
        if price_10s and now - time_10s <= 10 and (price - price_10s) / price_10s * 100 >= 2:
            notify_buy(coin, "10")
        if price_120s and now - time_120s <= 120 and (price - price_120s) / price_120s * 100 >= 3:
            notify_buy(coin, "120")

        # 🕒 تحديث الأسعار المرجعية
        if not time_5s or now - time_5s >= 5:
            price_5s = price
            time_5s = now
        if not time_10s or now - time_10s >= 10:
            price_10s = price
            time_10s = now
        if not time_120s or now - time_120s >= 120:
            price_120s = price
            time_120s = now

    def on_close(ws):
        time.sleep(2)
        threading.Thread(target=watch_price, args=(symbol,), daemon=True).start()

    def on_error(ws, error):
        print(f"[{symbol}] خطأ:", error)

    ws = WebSocketApp(url, on_message=on_message, on_close=on_close, on_error=on_error)
    ws.run_forever()

def watcher_loop():
    watched = set()
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue
        coins = r.hkeys("watchlist")
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

    elif text == "stop":
        r.set(IS_RUNNING_KEY, "0")
        send_message("🛑 تم إيقاف Sniper مؤقتًا.")

    elif text == "السجل":
        coins = r.hkeys("watchlist")
        if coins:
            coin_list = [c.decode().replace("USDT", "") for c in coins]
            formatted = ""
            for i, sym in enumerate(coin_list, 1):
                formatted += f"{i}. {sym}   "
                if i % 5 == 0:
                    formatted += "\n"
            send_message("📡 العملات المرصودة:\n" + formatted.strip())
        else:
            send_message("🚫 لا توجد عملات قيد المراقبة حالياً.")

    elif text == "reset":
        r.delete("watchlist")
        send_message("🧹 تم مسح الذاكرة. سيبدأ المراقبة من جديد بعد الدورة القادمة.")

    return jsonify(ok=True)

if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    threading.Thread(target=update_symbols_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)