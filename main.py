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

# 🔧 الإعدادات القابلة للتعديل
MAX_TOP_COINS = 13        # عدد العملات المختارة من Bitvavo في كل دورة
WATCH_DURATION = 130      # مدة مراقبة السعر بالثواني
REQUIRED_CHANGE = 1.3     # نسبة الانفجار السعري المطلوبة (%)
RANK_FILTER = 13          # إرسال الإشعارات فقط للعملات ضمن Top {X}
SYMBOL_UPDATE_INTERVAL = 180  # الزمن بين كل دورة لجمع العملات (ثانية)
REDIS_URL = os.getenv("REDIS_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = "https://saadisaadibot-saqarxbo-production.up.railway.app/"
IS_RUNNING_KEY = "sniper_running"

app = Flask(__name__)
r = redis.from_url(REDIS_URL)

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
        return ((close_price - open_price) / open_price) * 100
    except Exception as e:
        print(f"❌ خطأ في get_candle_change لـ {market}: {e}")
        return None

def fetch_top_bitvavo_then_match_binance():
    try:
        r.delete("not_found_binance")
        markets_res = requests.get("https://api.bitvavo.com/v2/markets", timeout=5).json()
        markets = [m["market"] for m in markets_res if m["market"].endswith("-EUR")]

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

        top_symbols = sorted(changes_5m, key=lambda x: x[1], reverse=True)[:MAX_TOP_COINS]
        combined = list({s for s, _ in top_symbols})
        print(f"📊 العملات المختارة من Bitvavo (5m): {len(combined)} → {combined}")

        exchange_info = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=5).json()
        binance_pairs = {s["symbol"] for s in exchange_info["symbols"] if s["status"] == "TRADING"}
        symbol_map = {s["baseAsset"].upper(): s["symbol"] for s in exchange_info["symbols"] if s["status"] == "TRADING"}

        matched, not_found = [], []
        for coin in combined:
            coin_upper = coin.upper()
            if coin_upper in symbol_map:
                matched.append(symbol_map[coin_upper])
            else:
                possible_matches = [s for s in binance_pairs if s.startswith(coin_upper)]
                if possible_matches:
                    matched.append(possible_matches[0])
                else:
                    not_found.append(coin)

        if not_found:
            # send_message("🚫 عملات غير موجودة على Binance:\n" + ", ".join(not_found))
            r.sadd("not_found_binance", *not_found)

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
            send_message("⚠️ لم يتم العثور على عملات صالحة في هذه الدورة.")
            time.sleep(SYMBOL_UPDATE_INTERVAL)
            continue

        now = time.time()
        for sym in top_symbols:
            r.hset("watchlist", sym, now)

        print(f"📡 تم تحديث {len(top_symbols)} عملة في المراقبة.")
        cleanup_old_coins()
        time.sleep(SYMBOL_UPDATE_INTERVAL)

def cleanup_old_coins():
    now = time.time()
    for sym, ts in r.hgetall("watchlist").items():
        try:
            t = float(ts.decode())
            if now - t > 3000:
                r.hdel("watchlist", sym.decode())
        except:
            continue

def get_rank_from_bitvavo(coin_symbol):
    try:
        markets_res = requests.get("https://api.bitvavo.com/v2/markets", timeout=5).json()
        markets = [m["market"] for m in markets_res if m["market"].endswith("-EUR")]

        changes = []
        for market in markets:
            symbol = market.replace("-EUR", "").upper()
            ch5 = get_candle_change(market, "5m")
            if ch5 is not None:
                changes.append((symbol, ch5))

        sorted_changes = sorted(changes, key=lambda x: x[1], reverse=True)
        for i, (symbol, _) in enumerate(sorted_changes, 1):
            if symbol == coin_symbol.upper():
                return i
        return None
    except Exception as e:
        print(f"⚠️ خطأ في get_rank_from_bitvavo: {e}")
        return None

def notify_buy(coin, tag, change=None):
    key = f"buy_alert:{coin}:{tag}"
    last_time = r.get(key)
    if last_time and time.time() - float(last_time) < 900:
        # حذف هذه الطباعة لا يغيّر أي شيء وظيفي
        return
    r.set(key, time.time())

    rank = get_rank_from_bitvavo(coin)
    if not rank or rank > RANK_FILTER:
        print(f"⛔ تجاهل الإشعار لأن {coin} ترتيبها خارج التوب {RANK_FILTER} أو غير معروف.")
        return

    msg = f"🚀 {coin} انفجرت بـ {change}  #top{rank}" if change else f"🚀 انفجار {tag}: {coin} #top{rank}"
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
        while price_history and now - price_history[0][0] > WATCH_DURATION:
            price_history.popleft()

        if len(price_history) > 1:
            min_price = min(p[1] for p in price_history)
            change = ((price - min_price) / min_price) * 100
            if change >= REQUIRED_CHANGE:
                duration = int(now - price_history[0][0])
                change_str = f"{change:.2f}% خلال {duration} ثانية"
                notify_buy(coin, f"{WATCH_DURATION}s", change_str)

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
    elif text == "العملات المفقودة":
        coins = r.smembers("not_found_binance")
        if coins:
            names = [c.decode() for c in coins]
            send_message("🚫 عملات غير موجودة على Binance:\n" + ", ".join(names))
        else:
            send_message("✅ لا توجد عملات مفقودة حالياً.")
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