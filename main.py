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

# ⚙️ إعدادات قابلة للتعديل
BITVAVO_TOP_COUNT = 15
MONITOR_DURATION = 180  # مدة المراقبة لكل عملة (بالثواني)
TOP10_PATTERN = [(1.0, 60), (1.0, 60)]  # نمط top10
TOP1_PATTERN = [(2.0, 60), (1.0, 60), (2.0, 60)]  # نمط top1

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
            return None
        open_price = float(data[-2][1])
        close_price = float(data[-2][4])
        change = ((close_price - open_price) / open_price) * 100
        return change
    except Exception:
        return None

def fetch_top_bitvavo_then_match_binance():
    try:
        r.delete("not_found_binance")
        markets = [m["market"] for m in requests.get("https://api.bitvavo.com/v2/markets", timeout=5).json() if m["market"].endswith("-EUR")]

        changes_5m = []
        def process(market):
            symbol = market.replace("-EUR", "").upper()
            ch5 = get_candle_change(market, "5m")
            return (symbol, ch5)
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = executor.map(process, markets)
            for sym, ch5 in results:
                if ch5 is not None:
                    changes_5m.append((sym, ch5))

        top = sorted(changes_5m, key=lambda x: x[1], reverse=True)[:BITVAVO_TOP_COUNT]
        combined = list({s for s, _ in top})

        exchange_info = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=5).json()
        symbol_map = {s["baseAsset"].upper(): s["symbol"] for s in exchange_info["symbols"] if s["status"] == "TRADING"}
        binance_pairs = {s["symbol"] for s in exchange_info["symbols"] if s["status"] == "TRADING"}

        matched, not_found = [], []
        for coin in combined:
            if coin in symbol_map:
                matched.append(symbol_map[coin])
            elif any(pair.startswith(coin) for pair in binance_pairs):
                matched.append(next(pair for pair in binance_pairs if pair.startswith(coin)))
            else:
                not_found.append(coin)

        if not_found:
            send_message("🚫 عملات غير موجودة على Binance:\n" + ", ".join(not_found))
            r.sadd("not_found_binance", *not_found)

        return matched

    except Exception as e:
        print("❌ خطأ:", e)
        return []

def update_symbols_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue

        top_symbols = fetch_top_bitvavo_then_match_binance()
        if not top_symbols:
            send_message("⚠️ لم يتم العثور على عملات صالحة.")
            time.sleep(180)
            continue

        now = time.time()
        for sym in top_symbols:
            r.hset("watchlist", sym, now)

        cleanup_old_coins()
        time.sleep(180)

def cleanup_old_coins():
    now = time.time()
    for sym, ts in r.hgetall("watchlist").items():
        try:
            if now - float(ts.decode()) > 2400:
                r.hdel("watchlist", sym.decode())
        except:
            continue

def notify_buy(coin, level):
    key = f"alerted:{coin}:{level}"
    if r.get(key):
        return
    r.setex(key, 900, "1")

    msg = f"🚀 اشتري {coin} {level}"
    send_message(msg)
    try:
        requests.post(SAQAR_WEBHOOK, json={"message": {"text": msg}})
    except Exception as e:
        print("❌ فشل إرسال الإشعار:", e)

def check_pattern(moves, pattern):
    i = 0
    for percent, max_seconds in pattern:
        while i < len(moves):
            ch, dur = moves[i]
            if ch >= percent and dur <= max_seconds:
                i += 1
                break
            i += 1
        else:
            return False
    return True

def watch_price(symbol):
    stream = f"{symbol.lower()}@trade"
    url = f"wss://stream.binance.com:9443/ws/{stream}"
    price_history = deque()
    moves = []

    def on_message(ws, message):
        if r.get(IS_RUNNING_KEY) != b"1":
            ws.close()
            return
        data = json.loads(message)
        price = float(data.get("p", 0))
        now = time.time()
        coin = symbol.replace("USDT", "").replace("BTC", "").replace("EUR", "")
        price_history.append((now, price))
        while price_history and now - price_history[0][0] > MONITOR_DURATION:
            price_history.popleft()

        if len(price_history) > 3:
            min_time, min_price = min(price_history, key=lambda x: x[1])
            change = ((price - min_price) / min_price) * 100
            duration = now - min_time
            moves.append((change, duration))
            moves = moves[-10:]

            if check_pattern(moves, TOP1_PATTERN):
                notify_buy(coin, "top1")
                moves.clear()
            elif check_pattern(moves, TOP10_PATTERN):
                notify_buy(coin, "top10")
                moves.clear()

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
        symbols = {c.decode() for c in r.hkeys("watchlist")}
        for sym in symbols - watched:
            threading.Thread(target=watch_price, args=(sym,), daemon=True).start()
            watched.add(sym)
        time.sleep(1)

@app.route("/")
def home():
    return "🔥 Sniper Top1 & Top10 Mode is Active", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    text = data.get("message", {}).get("text", "").strip().lower()
    if text == "play":
        r.set(IS_RUNNING_KEY, "1")
        send_message("✅ بدأ التشغيل")
    elif text == "stop":
        r.set(IS_RUNNING_KEY, "0")
        send_message("🛑 تم الإيقاف")
    elif text == "reset":
        r.delete("watchlist")
        send_message("🧹 تم مسح الذاكرة")
    elif text == "السجل":
        coins = r.hkeys("watchlist")
        if coins:
            formatted = "\n".join(f"{i+1}. {c.decode()}" for i, c in enumerate(coins))
            send_message("📡 العملات:\n" + formatted)
        else:
            send_message("🚫 لا توجد عملات قيد المراقبة.")
    elif text == "العملات المفقودة":
        not_found = r.smembers("not_found_binance")
        if not_found:
            send_message("🚫:\n" + ", ".join(c.decode() for c in not_found))
        else:
            send_message("✅ لا عملات مفقودة حالياً.")
    return jsonify(ok=True)

if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    threading.Thread(target=update_symbols_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)