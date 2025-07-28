import os, json, time, redis, threading, requests
from flask import Flask, request
from python_bitvavo_api.bitvavo import Bitvavo

app = Flask(__name__)
r = redis.from_url(os.getenv("REDIS_URL"))

bitvavo = Bitvavo({
    'APIKEY': os.getenv("BITVAVO_API_KEY"),
    'APISECRET': os.getenv("BITVAVO_API_SECRET"),
    'RESTURL': 'https://api.bitvavo.com/v2',
    'WSURL': 'wss://ws.bitvavo.com/v2/'
})

BOT_TOKEN = os.getenv("BOT_TOKEN")
TOUTO_CHAT_ID = os.getenv("CHAT_ID")
TOTO_WEBHOOK = "https://totozaghnot-production.up.railway.app/webhook"

SNIPER_MODE = {"active": False}
SNIPER_LAST_ALERT = {}

# ========== أدوات أساسية ==========
def send_message(text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={
            "chat_id": TOUTO_CHAT_ID,
            "text": text
        })
    except:
        pass

def send_to_toto(symbol, mode):
    try:
        base = symbol.split("-")[0]
        requests.post(TOTO_WEBHOOK, json={"message": {"text": f"اشتري {base} يا توتو {mode}"}})
    except Exception as e:
        print(f"[Webhook Error] {e}")

# ========== Ridder Score (آخر 15 دقيقة فقط) ==========
def ridder_score(symbol):
    try:
        candles = bitvavo.candles(symbol, '1m', {'limit': 15})
        if len(candles) < 3:
            return 0

        total_volume = sum([float(c[5]) for c in candles])
        if total_volume < 10000:
            return 0

        recent = candles[-3:]
        change = (float(recent[-1][4]) - float(recent[0][1])) / float(recent[0][1]) * 100
        avg_range = sum([abs(float(c[2]) - float(c[3])) for c in recent]) / 3
        avg_volume = sum([float(c[5]) for c in recent]) / 3

        return change * avg_range * avg_volume
    except:
        return 0

# ========== الفلتر الذكي ==========
def smart_filter():
    while True:
        for key in list(r.scan_iter("ridder:*")):
            symbol = key.decode().split(":")[1]
            try:
                data = json.loads(r.get(key))
                if data.get("notified"):
                    continue

                candles = bitvavo.candles(symbol, '1m', {'limit': 5})
                if len(candles) < 5:
                    continue

                prices = [float(c[4]) for c in candles]
                volumes = [float(c[5]) for c in candles]
                avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)
                last_volume = volumes[-1]
                price_jump = prices[-1] / prices[0]

                if last_volume > avg_volume * 2.5 and price_jump > 1.018:
                    data["notified"] = True
                    r.set(key, json.dumps(data))
                    send_message(f"🚀 اشترِ {symbol} يا توتو Ridder")
                    send_to_toto(symbol, "Ridder")

                elif SNIPER_MODE["active"]:
                    if last_volume > avg_volume * 1.5 and price_jump > 1.007:
                        now = time.time()
                        last = SNIPER_LAST_ALERT.get(symbol, 0)
                        if now - last > 180:
                            SNIPER_LAST_ALERT[symbol] = now
                            send_message(f"👀 انفجار صغير محتمل: {symbol}")
                            try:
                                base = symbol.split("-")[0]
                                requests.post("https://alnemsbot-production.up.railway.app/webhook", json={
                                    "message": {"text": f"اشتري {base} يا نمس"}
                                })
                            except Exception as e:
                                print(f"[Webhook to Nems Failed] {e}")

            except Exception as e:
                print(f"[Smart Filter Error] {e}")
        time.sleep(1)

# ========== Ridder Loop (تحديث Top 50 فقط) ==========
def run_ridder_loop():
    while True:
        try:
            markets = bitvavo.markets()
            symbols = [m['market'] for m in markets if m['quote'] == 'EUR']
            scored = [(s, ridder_score(s)) for s in symbols]
            top = sorted(scored, key=lambda x: x[1], reverse=True)[:50]

            existing_keys = set(k.decode().split(":")[1] for k in r.scan_iter("ridder:*"))
            new_symbols = set([s for s, _ in top])

            to_add = new_symbols - existing_keys
            for symbol in to_add:
                r.set(f"ridder:{symbol}", json.dumps({"start": time.time(), "notified": False}))
                print(f"[+] تمت إضافة عملة جديدة للمراقبة: {symbol}")

        except Exception as e:
            print(f"[Ridder Error] {e}")
        time.sleep(90)

# ========== تنظيف العملات بعد 15 دقيقة ==========
def cleanup_expired():
    while True:
        for key in r.scan_iter("ridder:*"):
            try:
                data = json.loads(r.get(key))
                if time.time() - data["start"] > 900:
                    r.delete(key)
            except:
                continue
        time.sleep(60)

# ========== Webhook تلغرام ==========
@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    msg = data.get("message", {}).get("text", "").lower()
    chat_id = str(data.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != str(TOUTO_CHAT_ID): return "ok"

    if msg == "شو عم تعمل":
        ridder = [k.decode().split(":")[1] for k in r.scan_iter("ridder:*")]
        reply = "🚨 Ridder:\n" + "\n".join(ridder) if ridder else "🚨 لا عملات في Ridder"
        send_message(reply)

    elif msg == "افتح الجدار":
        SNIPER_MODE["active"] = True
        send_message("✅ تم تفعيل Sniper Mode! أي حركة غير مؤكدة سيتم إرسالها لك مباشرة.")

    elif msg == "اغلق الجدار":
        SNIPER_MODE["active"] = False
        send_message("🔕 تم إغلاق Sniper Mode. توقفت إشعارات الحركات غير المؤكدة.")

    return "ok"

# ========== التشغيل ==========
if __name__ == "__main__":
    for key in r.scan_iter("*"): r.delete(key)
    threading.Thread(target=run_ridder_loop).start()
    threading.Thread(target=smart_filter).start()
    threading.Thread(target=cleanup_expired).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))