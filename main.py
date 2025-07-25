import os
import json
import time
import redis
import threading
import requests
from flask import Flask, request, jsonify
from websocket import WebSocketApp

app = Flask(__name__)

# إعداد المتغيرات من env
r = redis.from_url(os.getenv("REDIS_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# إرسال رسالة إلى تيليغرام
def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("فشل إرسال الرسالة:", e)

# مراقبة السعر عبر # مراقبة السعر عبر WebSocket
def watch_price(symbol):
    stream = f"{symbol.lower()}@ticker"
    url = f"wss://stream.binance.com:9443/ws/{stream}"

    last_price = None
    last_time = None

    def on_message(ws, message):
        nonlocal last_price, last_time
        data = json.loads(message)
        price = float(data['c'])
        now = time.time()

        print(f"[{symbol}] السعر الحالي: {price}")

        if last_price is not None and last_time is not None:
            price_change = (price - last_price) / last_price * 100
            time_diff = now - last_time

            if price_change >= 2 and time_diff <= 1:
                send_message(f"🚀 انفجار سريع في {symbol} 📈\nالسعر ارتفع {price_change:.2f}% خلال ثانية!")

        last_price = price
        last_time = now

    def on_error(ws, error):
        print(f"[{symbol}] خطأ:", error)

    def on_close(ws):
        print(f"[{symbol}] تم الإغلاق")

    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

# تشغيل مراقبة العملات من Redis
def watcher_loop():
    watched = set()
    while True:
        coins = r.smembers("coins")
        symbols = {coin.decode() for coin in coins}
        new_symbols = symbols - watched
        for symbol in new_symbols:
            threading.Thread(target=watch_price, args=(symbol,)).start()
            watched.add(symbol)
        time.sleep(5)

# الصفحة الرئيسية
@app.route('/')
def home():
    return "✅ البوت شغّال تمام"

# الراوت الخاص بالويبهوك
@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    data = request.get_json()

    if not data or "message" not in data:
        return jsonify(success=True)

    message = data["message"]
    text = message.get("text", "").strip().lower()
    user_id = message["chat"]["id"]

    if text.startswith("سجل"):
        tokens = text.split()[1:]
        added = []
        for token in tokens:
            full = f"{token.upper()}USDT"
            r.sadd("coins", full)
            added.append(full)
        reply = f"✅ تم تسجيل: {' - '.join(added)}"
        send_message(reply)
        return jsonify(ok=True)

    elif text == "احذف الكل":
        deleted = r.smembers("coins")
        r.delete("coins")
        names = [x.decode() for x in deleted]
        reply = f"🗑️ تم حذف الكل: {', '.join(names)}"
        send_message(reply)
        return jsonify(ok=True)

    elif text == "شو سجلت":
        saved = r.smembers("coins")
        if saved:
            names = [x.decode() for x in saved]
            reply = f"📌 العملات المسجلة:\n" + "\n".join(names)
        else:
            reply = "📡 لا توجد عملات مسجلة."
        send_message(reply)
        return jsonify(ok=True)

    return jsonify(success=True)

# تشغيل البوت
if __name__ == '__main__':
    threading.Thread(target=watcher_loop).start()
    app.run(host="0.0.0.0", port=8080)