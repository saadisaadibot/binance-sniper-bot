import os
import time
import requests
import redis
from threading import Thread

# إعداد Redis
r = redis.from_url(os.getenv("REDIS_URL"))
WEBHOOK_URL = "https://totozaghnot-production.up.railway.app"
HEADERS = {'Content-Type': 'application/json'}

def get_binance_prices():
    url = "https://api.binance.com/api/v3/ticker/price"
    res = requests.get(url).json()
    return {x['symbol']: float(x['price']) for x in res if x['symbol'].endswith("USDT")}

def get_bitvavo_prices():
    url = "https://api.bitvavo.com/v2/ticker/price"
    res = requests.get(url).json()
    return {x['market'].replace("-EUR", ""): float(x['price']) for x in res if x['market'].endswith("-EUR")}

def get_top_50_binance(binance_prices):
    changes = []
    url = "https://api.binance.com/api/v3/ticker/24hr"
    data = requests.get(url).json()
    for item in data:
        if item["symbol"].endswith("USDT"):
            change = float(item.get("priceChangePercent", 0))
            changes.append((item["symbol"].replace("USDT", ""), change))
    top_50 = sorted(changes, key=lambda x: x[1], reverse=True)[:50]
    return [x[0] for x in top_50]

def watch_top_coin(coin, delay_minutes=2):
    print(f"🎯 أفضل عملة: {coin} 🔍 بدأ التركيز...")
    start = time.time()
    while time.time() - start < delay_minutes * 60:
        binance_price = get_binance_prices().get(coin + "USDT")
        bitvavo_price = get_bitvavo_prices().get(coin)
        if binance_price and bitvavo_price and bitvavo_price > 0:
            diff = ((binance_price - bitvavo_price) / bitvavo_price) * 100
            print(f"[{coin}] Diff: {diff:.2f}%")
            if diff >= 3:
                payload = {"text": f"اشتري {coin} يا توتو sniper"}
                requests.post(WEBHOOK_URL, json=payload, headers=HEADERS)
                print("🚀 أُرسِل إشعار الشراء")
                return
        time.sleep(2)
    print("🐰 هرب الأرنب")

def sniper_loop():
    while True:
        r.flushdb()
        print("🚀 جاري فحص أفضل 50 عملة...")
        binance_prices = get_binance_prices()
        bitvavo_prices = get_bitvavo_prices()
        top_50 = get_top_50_binance(binance_prices)

        valid_candidates = []
        for coin in top_50:
            b_price = binance_prices.get(coin + "USDT")
            bv_price = bitvavo_prices.get(coin)
            if b_price and bv_price and bv_price > 0:
                diff = ((b_price - bv_price) / bv_price) * 100
                valid_candidates.append((coin, diff))
                print(f"[{coin}] Diff: {diff:.2f}%")

        if not valid_candidates:
            print("❌ لا يوجد عملات صالحة للمقارنة")
            time.sleep(600)
            continue

        best = max(valid_candidates, key=lambda x: x[1])
        best_coin = best[0]
        best_diff = best[1]

        print(f"🎯 أفضل عملة: {best_coin} {best_diff:.2f}%")
        watch_top_coin(best_coin)
        time.sleep(2)

# تشغيل البوت في ثريد منفصل
Thread(target=sniper_loop).start()