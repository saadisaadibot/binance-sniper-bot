import os
import json
import time
import redis
import threading
from flask import Flask, request
from websocket import WebSocketApp

# إعداد المتغيرات
app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))
chat_id = os.getenv("CHAT_ID")
bot_token = os.getenv("BOT_TOKEN")

# دالة إرسال رسالة تيليغرام
def send_message(text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, data=data)
    except:
        pass

# دالة WebSocket لمراقبة الأسعار
def watch_price(symbol):
    stream = f"{symbol.lower()}@ticker"
    url = f"wss://stream.binance.com:9443/ws/{stream}"

    def on_message(ws, message):
        data = json.loads(message)
        price = float(data['c'])
        print(f"[{symbol}] السعر الحالي: {price}")
        # يمكن إضافة منطق الإشعار هنا إذا السعر ارتفع بسرعة

    def on_error(ws, error):
        print(f"[{symbol}] خطأ: {error}")

    def on_close(ws):
        print(f"[{symbol}] تم الإغلاق")

    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

# دالة لفحص العملات المسجلة وتشغيل WebSocket عليها
def watcher_loop():
    watched = set()
    while True:
        coins = r.smembers("coins")
        symbols = {coin.decode('utf-8') for coin in coins}
        new_symbols = symbols - watched
        for symbol in new_symbols:
            threading.Thread(target=watch_price, args=(symbol,)).start()
            watched.add(symbol)
        time.sleep(5)

# نقطة بداية البوت
@app.route('/')
def home():
    return "Bot Running"

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    message = data.get("message", {}).get("text", "").lower()
    if message.startswith("سجل"):
        tokens = message.split()[1:]
        added = []
        for token in tokens:
            full = f"{token.upper()}USDT"
            r.sadd("coins", full)
            added.append(full)
        return f"✅ تم تسجيل: {' - '.join(added)}", 200
    elif message == "احذف الكل":
        deleted = r.smembers("coins")
        r.delete("coins")
        names = [x.decode() for x in deleted]
        return f"🗑️ تم حذف الكل: {', '.join(names)}", 200
    return "تم", 200

# بدء الخيوط
if __name__ == '__main__':
    threading.Thread(target=watcher_loop).start()
    app.run(host="0.0.0.0", port=8080)