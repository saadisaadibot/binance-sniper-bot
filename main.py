import os
import json
import time
import redis
import requests
from binance.websocket.spot.websocket_client import SpotWebsocketClient as Client

# إعدادات البيئة
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
r = redis.from_url(REDIS_URL)

# دالة إرسال رسالة تيليغرام
def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text}
        )
    except Exception as e:
        print("Telegram Error:", e)

# حفظ الأسعار السابقة
last_prices = {}

# المعالجة عند وصول بيانات
def handle_message(msg):
    try:
        symbol = msg["s"]
        price = float(msg["c"])

        if symbol not in last_prices:
            last_prices[symbol] = price
            return

        old_price = last_prices[symbol]
        change_percent = ((price - old_price) / old_price) * 100

        if change_percent >= 2:
            coin = symbol.replace("USDT", "")
            send_message(f"🚀 انفجار {coin}: ارتفعت 2% خلال ثانية")
            print(f"تم إرسال إشعار لـ {symbol} ✅")

        last_prices[symbol] = price

    except Exception as e:
        print("handle_message Error:", e)

# تشغيل WebSocket
def start_ws():
    symbols = r.smembers("binance_pairs")
    if not symbols:
        print("❌ لا توجد عملات مسجلة للمراقبة.")
        send_message("❌ لا توجد عملات مسجلة للمراقبة.")
        return

    stream_list = [f"{s.decode().lower()}@ticker" for s in symbols]
    print("✅ الاشتراك في:", stream_list)

    ws = Client()
    ws.start()
    ws.aggregate_subscribe(stream_list, handle_message)

# تشغيل السكربت
if __name__ == "__main__":
    send_message("✅ تم تشغيل مراقبة بينانس اللحظية 🚀")
    start_ws()