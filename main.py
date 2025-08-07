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
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        print("فشل إرسال الرسالة:", e)

def get_candle_change(market, interval):
    try:
        url = f"https://api.bitvavo.com/v2/{market}/candles?interval={interval}&limit=2"
        res = requests.get(url, timeout=3)
        data = res.json()
        if not isinstance(data, list) or len(data) < 2:
            print(f"⚠️ لا توجد شموع كافية لـ {market} ({interval}) - المحتوى:", data)
            return None
        open_price = float(data[-2][1])
        close_price = float(data[-2][4])
        change = ((close_price - open_price) / open_price) * 100
        return change
    except Exception as e:
        print(f"❌ خطأ في get_candle_change لـ {market}: {e}")
        return None

def fetch_top_bitvavo_then_match_binance():
    try:
        markets_res = requests.get("https://api.bitvavo.com/v2/markets", timeout=5).json()
        markets = [m["market"] for m in markets_res if m["market"].endswith("-EUR")]

        changes_15m, changes_10m, changes_5m = [], [], []

        def process(market):
            symbol = market.replace("-EUR", "").upper()
            ch15 = get_candle_change(market, "15m")
            ch10 = get_candle_change(market, "10m")
            ch5 = get_candle_change(market, "5m")
            return (symbol, ch15, ch10, ch5)

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = executor.map(process, markets)
            for sym, ch15, ch10, ch5 in results:
                if ch15 is not None:
                    changes_15m.append((sym, ch15))
                if ch10 is not None:
                    changes_10m.append((sym, ch10))
                if ch5 is not None:
                    changes_5m.append((sym, ch5))

        top15 = sorted(changes_15m, key=lambda x: x[1], reverse=True)[:5]
        top10 = sorted(changes_10m, key=lambda x: x[1], reverse=True)[:10]
        top5 = sorted(changes_5m, key=lambda x: x[1], reverse=True)[:15]
        combined = list({s for s, _ in top15 + top10 + top5})
        print(f"📊 العملات المختارة من Bitvavo: {len(combined)} → {combined}")

        exchange_info = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=5).json()
        binance_pairs = {s["symbol"] for s in exchange_info["symbols"] if s["status"] == "TRADING"}

        matched, not_found = [], []

        for coin in combined:
            found = False
            for quote in ["USDT", "BTC", "EUR"]:
                pair = f"{coin}{quote}"
                if pair in binance_pairs:
                    matched.append(pair)
                    found = True
                    break
            if not found:
                not_found.append(coin)

        if not_found:
            send_message("🚫 عملات غير موجودة على Binance:\n" + ", ".join(not_found))

        return matched

    except Exception as e:
        print("❌ خطأ في fetch_top_bitvavo_then_match_binance:", e)
        return []

def update_symbols_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue

        print("🌀 بدء دورة جديدة لجلب العملات...")
        top_symbols = fetch_top_bitvavo_then_match_binance()

        if not top_symbols:
            print("⚠️ لم يتم العثور على عملات قابلة للمراقبة.")
            send_message("⚠️ لم يتم العثور على عملات صالحة في هذه الدورة.")
            time.sleep(180)
            continue

        now = time.time()
        for sym in top_symbols:
            r.hset("watchlist", sym, now)
        print(f"📡 تم تحديث {len(top_symbols)} عملة في المراقبة.")
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
    if last_time and time.time() - float(last_time) < 240:
        print(f"⛔ تجاهل الإشعار المكرر لـ {coin} #{tag}")
        return
    r.set(key, time.time())

    if change:
        msg = f"🚀 {coin} انفجرت بـ {change}  #{tag}"
    else:
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
    watch_duration = 180
    required_change = 1.8
    price_history = deque()

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
        coin = symbol.replace("USDT", "").replace("BTC", "").replace("EUR", "")
        price_history.append((now, price))
        while price_history and now - price_history[0][0] > watch_duration:
            price_history.popleft()
        if len(price_history) > 1:
            min_price = min(p[1] for p in price_history)
            change = ((price - min_price) / min_price) * 100
            if change >= required_change:
                duration = int(now - price_history[0][0])
                change_str = f"{change:.2f}% خلال {duration} ثانية"
                notify_buy(coin, f"{watch_duration}s", change_str)

    def on_close(ws): time.sleep(2); threading.Thread(target=watch_price, args=(symbol,), daemon=True).start()
    def on_error(ws, error): print(f"[{symbol}] خطأ:", error)
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
def home(): return "🔥 Sniper Mode is Live", 200

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
            coin_list = [c.decode().replace("USDT", "").replace("BTC", "").replace("EUR", "") for c in coins]
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