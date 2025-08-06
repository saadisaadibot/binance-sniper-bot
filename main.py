import os
import json
import time
import redis
import threading
import requests
from flask import Flask, request, jsonify
from websocket import WebSocketApp
from concurrent.futures import ThreadPoolExecutor
from collections import deque

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

        # ✅ تعديل المطابقة ليتوافق مع Bitvavo بغض النظر عن الحروف
        matched = [sym for sym in binance_usdt_pairs if sym.replace("USDT", "").upper() in bitvavo_symbols]
        matched = matched[:100]

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

        # ✅ رفع عدد العملات المختارة من كل فريم لـ 25 بدل 10
        collect_top("15m", 50)
        collect_top("10m", 20)
        collect_top("5m", 20)

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
        else:
            send_message("🚫 لا توجد عملات قابلة للمراقبة حالياً.")

        cleanup_old_coins()
        time.sleep(180)

def cleanup_old_coins():
    now = time.time()
    for sym, ts in r.hgetall("watchlist").items():
        try:
            t = float(ts.decode())
            if now - t > 2400:
                r.hdel("watchlist", sym.decode())
        except:
            continue

def notify_buy(coin, tag, change=None):
    key = f"buy_alert:{coin}:{tag}"
    last_time = r.get(key)

    if last_time and time.time() - float(last_time) < 30:
        print(f"⛔ تجاهل الإشعار المكرر لـ {coin} #{tag}")
        return

    r.set(key, time.time())

    msg = f"🚀 انفجار {tag}"
    if change:
        msg += f" (+{change})"
    msg += f": {coin} #{tag}"
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

    watch_duration = 240       # عدد الثواني للمراقبة (قابلة للتعديل)
    required_change = 2.1      # النسبة المطلوبة للإشعار (قابلة للتعديل)

    price_history = deque()  # 🧠 قائمة لحفظ الأسعار مع الزمن

    def on_message(ws, message):
        if r.get(IS_RUNNING_KEY) != b"1":
            ws.close()
            return

        data = json.loads(message)
        if "p" not in data:
            return

        try:
            price = float(data["p"])
        except:
            return

        now = time.time()
        coin = symbol.replace("USDT", "")

        # 🕓 أضف السعر الحالي مع التوقيت إلى القائمة
        price_history.append((now, price))

        # 🧹 حذف الأسعار الأقدم من مدة المراقبة
        while price_history and now - price_history[0][0] > watch_duration:
            price_history.popleft()

        # ✅ شرط الانفجار مقارنةً بأقل سعر خلال آخر 3 دقائق
        if len(price_history) > 1:
            min_price = min(p[1] for p in price_history)
            change = ((price - min_price) / min_price) * 100
            if change >= required_change:
                change_str = f"{change:.2f}%"
                notify_buy(coin, f"{watch_duration}s", change_str)

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