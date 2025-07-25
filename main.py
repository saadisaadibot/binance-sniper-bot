import os
import redis
import requests
import time
import json
import threading
from flask import Flask, request
from binance.websocket.spot.websocket_client import SpotWebsocketClient as WebSocketClient

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")
r = redis.from_url(REDIS_URL)

tracked_prices = {}

def send_message(text):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": text})

def monitor_price(symbol):
    def handle_message(msg):
        symbol = msg['s']
        price = float(msg['c'])
        now = time.time()

        if symbol in tracked_prices:
            prev_price, prev_time = tracked_prices[symbol]
            if now - prev_time <= 1:
                change = (price - prev_price) / prev_price * 100
                if change >= 2:
                    send_message(f"🚀 ارتفاع مفاجئ: {symbol} صعدت {change:.2f}% خلال ثانية!")
        tracked_prices[symbol] = (price, now)

    ws = WebSocketClient()
    ws.start()
    ws.kline(symbol=symbol.lower(), interval="1s", callback=handle_message)

def start_monitoring():
    coins = r.smembers("coins")
    for coin in coins:
        coin = coin.decode()
        threading.Thread(target=monitor_price, args=(coin,), daemon=True).start()

@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    msg = data.get("message", {}).get("text", "")
    if not msg: return "no text"

    msg = msg.lower()
    if msg.startswith("سجل"):
        tokens = msg.split()[1:]
        added = []
        for t in tokens:
            t = t.upper() + "USDT"
            r.sadd("coins", t)
            added.append(t)
        send_message(f"✅ تم تسجيل: {' - '.join(added)}")
        return "added"

    if msg.startswith("احذف الكل"):
        deleted = r.smembers("coins")
        r.delete("coins")
        names = [d.decode() for d in deleted]
        send_message(f"🗑️ تم حذف الكل:\n{', '.join(names)}")
        return "deleted all"

    if msg.startswith("احذف"):
        coin = msg.split()[1].upper() + "USDT"
        r.srem("coins", coin)
        send_message(f"🗑️ تم حذف: {coin}")
        return "deleted one"

    if msg.startswith("شو سجلت"):
        coins = r.smembers("coins")
        if not coins:
            send_message("📭 لا توجد عملات مسجلة.")
        else:
            c = [x.decode() for x in coins]
            send_message("🔖 العملات المسجلة:\n" + "\n".join(c))
        return "listed"

    return "ok"

if __name__ == "__main__":
    send_message("✅ البوت اشتغل ويبدأ مراقبة العملات!")
    threading.Thread(target=start_monitoring, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)