import os
import redis
import requests
from flask import Flask, request

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_message(text):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={
        "chat_id": CHAT_ID,
        "text": text
    })

@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    msg = data.get("message", {}).get("text", "").lower()
    chat_id = str(data.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(CHAT_ID): return "ok"

    # تسجيل العملات
    if msg.startswith("سجل "):
        parts = msg.replace("سجل ", "").split()
        added = []
        for coin in parts:
            key = f"watch:{coin.upper()}USDT"
            r.set(key, "1")
            added.append(coin.upper())
        send_message(f"✅ تم تسجيل: {' - '.join(added)}")

    # حذف الكل
    elif msg.strip() == "احذف الكل":
        deleted = []
        for key in r.scan_iter("watch:*"):
            r.delete(key)
            deleted.append(key.decode().split(":")[1])
        send_message(f"🗑️ تم حذف الكل:\n{', '.join(deleted)}" if deleted else "❌ لا يوجد شيء لحذفه")

    # حذف جماعي أو فردي
    elif msg.startswith("احذف "):
        parts = msg.replace("احذف ", "").split()
        deleted = []
        for coin in parts:
            key = f"watch:{coin.upper()}USDT"
            if r.exists(key):
                r.delete(key)
                deleted.append(coin.upper())
        send_message(f"🗑️ تم حذف: {' - '.join(deleted)}" if deleted else "❌ لم يتم العثور على العملات")

    # عرض محتويات Redis
    elif msg.strip() == "شو سجلت":
        coins = [k.decode().split(":")[1] for k in r.scan_iter("watch:*")]
        send_message("📌 العملات المسجلة:\n" + "\n".join(coins) if coins else "📭 لا توجد عملات مسجلة.")

    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))