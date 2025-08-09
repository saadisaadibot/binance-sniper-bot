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

# =========================
# 🔧 الإعدادات القابلة للتعديل
# =========================
MAX_TOP_COINS = 13            # عدد العملات المختارة من Bitvavo في كل دورة
WATCH_DURATION = 180          # مدة نافذة المراقبة بالثواني (لتحديد القاع المحلي)
RANK_FILTER = 10              # إرسال الإشعارات فقط للعملات ضمن Top {X} على Bitvavo (5m)
SYMBOL_UPDATE_INTERVAL = 180  # الزمن بين كل دورة لجمع العملات (ثانية)

# 📈 نمط 1% + 1% المتتالي
STEP_PCT = 1.3                # كل خطوة = 1%
STEP_GAP_SECONDS = 2          # أقل فرق زمني بين الخطوتين (ثوانٍ)
MAX_WAIT_AFTER_FIRST = 60     # ⏳ المدة القصوى لانتظار +1% الثانية بعد الأولى (ثواني)

# 🧠 فلترة لحظية عند الإشعار
IMPROVEMENT_STEPS = 3         # كم مرتبة لازم تتحسن بالتوب ليمر الإشعار إن لم يكن دخول جديد
REQUIRE_LAST_1M_GREEN = True  # تأكيد أن آخر شمعة 1m خضراء قبل الإرسال (اختياري لكن مفيد)
RANK_CACHE_TTL = 15           # كاش ترتيب 5m بالثواني للاستعلامات العامة (نتجاوز الكاش وقت الإرسال)

# 🔑 متغيرات البيئة
REDIS_URL = os.getenv("REDIS_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
# فضّل ضبطه من .env (إن كان عندك على الروت خلي المتغير = الرابط الأساسي، وإن عندك مسار /webhook حطه كامل)
SAQAR_WEBHOOK = os.getenv(
    "SAQAR_WEBHOOK",
    "https://saadisaadibot-saqarxbo-production.up.railway.app/"
)
IS_RUNNING_KEY = "sniper_running"
# =========================

app = Flask(__name__)
r = redis.from_url(REDIS_URL)

def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=5
        )
    except Exception as e:
        print("فشل إرسال الرسالة:", e)

def get_candle_change(market, interval):
    try:
        url = f"https://api.bitvavo.com/v2/{market}/candles?interval={interval}&limit=2"
        res = requests.get(url, timeout=5)
        data = res.json()
        if not isinstance(data, list) or len(data) < 2:
            return None
        open_price = float(data[-2][1])
        close_price = float(data[-2][4])
        return ((close_price - open_price) / open_price) * 100
    except Exception as e:
        print(f"❌ خطأ في get_candle_change لـ {market}: {e}")
        return None

# =========================
# 🧭 مطابقة Binance + كاش
# =========================
def fetch_binance_symbols_cached():
    """ExchangeInfo من Binance مع كاش 10 دقائق لتخفيف الضغط"""
    try:
        cache_key = "binance:exchangeInfo"
        cached = r.get(cache_key)
        if cached:
            return json.loads(cached)

        info = requests.get(
            "https://api.binance.com/api/v3/exchangeInfo", timeout=8
        ).json()
        # نخزن كل الشيء كما هو
        r.setex(cache_key, 600, json.dumps(info))
        return info
    except Exception as e:
        print("⚠️ خطأ في fetch_binance_symbols_cached:", e)
        return {"symbols": []}

def prefer_pair(base, symbols):
    """نفضّل USDT ثم EUR ثم BTC للـ baseAsset المعطى"""
    base = base.upper()
    candidates = [s for s in symbols if s.get("baseAsset", "").upper() == base and s.get("status") == "TRADING"]
    if not candidates:
        return None
    for quote in ("USDT", "EUR", "BTC"):
        for s in candidates:
            if s.get("quoteAsset") == quote:
                return s.get("symbol")
    # fallback: أول واحد متاح
    return candidates[0].get("symbol")

# aliases لبعض الحالات اللي بتختلف أسماؤها بين المنصتين (أضف عند الحاجة)
ALIASES = {
    # "ICNT": "ICNT",  # مثال توضيحي — ضيف التحويلات الفعلية عند اكتشافها
}

def fetch_top_bitvavo_then_match_binance():
    """نجمع Top من Bitvavo (5m)، بعدها نطابق لـ Binance بأفضل زوج"""
    try:
        r.delete("not_found_binance")
        markets_res = requests.get("https://api.bitvavo.com/v2/markets", timeout=8).json()
        markets = [m["market"] for m in markets_res if m.get("market", "").endswith("-EUR")]

        def process(market):
            symbol = market.replace("-EUR", "").upper()
            ch5 = get_candle_change(market, "5m")
            return (symbol, ch5)

        changes_5m = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            for sym, ch5 in ex.map(process, markets):
                if ch5 is not None:
                    changes_5m.append((sym, ch5))

        top_symbols = [s for s, _ in sorted(changes_5m, key=lambda x: x[1], reverse=True)[:MAX_TOP_COINS]]
        top_symbols = list(dict.fromkeys(top_symbols))  # إزالة تكرارات احتياطًا

        info = fetch_binance_symbols_cached()
        symbols = info.get("symbols", [])

        matched, not_found = [], []
        for coin in top_symbols:
            base = ALIASES.get(coin, coin).upper()
            best = prefer_pair(base, symbols)
            if best:
                matched.append(best)
            else:
                not_found.append(coin)

        if not_found:
            r.sadd("not_found_binance", *not_found)

        print(f"📊 Bitvavo top: {top_symbols} → Binance matched: {matched}")
        return matched

    except Exception as e:
        print("❌ خطأ في fetch_top_bitvavo_then_match_binance:", e)
        return []

# --- كاش خفيف لترتيب Bitvavo (نتجاوزه وقت الإرسال) ---
def get_rank_from_bitvavo(coin_symbol, *, force_refresh=False):
    try:
        cache_key = "rank_cache:all"
        sorted_changes = None

        if not force_refresh:
            cached = r.get(cache_key)
            if cached:
                sorted_changes = json.loads(cached)

        if sorted_changes is None:
            markets_res = requests.get("https://api.bitvavo.com/v2/markets", timeout=8).json()
            markets = [m["market"] for m in markets_res if m.get("market", "").endswith("-EUR")]

            changes = []
            for market in markets:
                symbol = market.replace("-EUR", "").upper()
                ch5 = get_candle_change(market, "5m")
                if ch5 is not None:
                    changes.append((symbol, ch5))

            sorted_changes = sorted(changes, key=lambda x: x[1], reverse=True)
            r.setex(cache_key, RANK_CACHE_TTL, json.dumps(sorted_changes))

        for i, (symbol, _) in enumerate(sorted_changes, 1):
            if symbol == coin_symbol.upper():
                return i
        return None
    except Exception as e:
        print(f"⚠️ خطأ في get_rank_from_bitvavo: {e}")
        return None

def is_last_1m_green(coin_symbol):
    try:
        market = f"{coin_symbol.upper()}-EUR"
        url = f"https://api.bitvavo.com/v2/{market}/candles?interval=1m&limit=2"
        res = requests.get(url, timeout=5).json()
        if isinstance(res, list) and len(res) >= 2:
            o = float(res[-2][1]); c = float(res[-2][4])
            return c >= o
    except Exception as e:
        print("⚠️ فشل التحقق من شمعة 1m:", e)
    return True  # لا نمنع الإشعار بسبب خطأ شبكي

def notify_buy(coin, tag, change=None):
    key = f"buy_alert:{coin}:{tag}"
    last_time = r.get(key)
    if last_time and time.time() - float(last_time) < 900:
        # كولداون 15 دقيقة لنفس (coin, tag)
        return

    # 🚦 حساب الترتيب الآن بلا كاش (فلترة حديثة حقيقية)
    rank = get_rank_from_bitvavo(coin, force_refresh=True)
    if not rank or rank > RANK_FILTER:
        print(f"⛔ تجاهل الإشعار لأن {coin} خارج التوب {RANK_FILTER} حالياً (rank={rank}).")
        return

    # ✅ دخل التوب مؤخرًا أو تحسّن بوضوح
    prev_key, prev_ts_key = f"rank_prev:{coin}", f"rank_prev_ts:{coin}"
    prev = r.get(prev_key)
    prev = int(prev) if prev else None
    now_ts = time.time()
    r.set(prev_key, rank)
    r.set(prev_ts_key, now_ts)

    just_entered = (prev is None) or (prev > RANK_FILTER and rank <= RANK_FILTER)
    improved     = (prev is not None) and ((prev - rank) >= IMPROVEMENT_STEPS)

    if not (just_entered or improved):
        print(f"⛔ {coin}: داخل التوب سابقًا بدون تحسّن كافٍ (prev={prev} → now={rank}).")
        return

    # (اختياري) تأكيد الشمعة 1m خضراء
    if REQUIRE_LAST_1M_GREEN and (not is_last_1m_green(coin)):
        print(f"⛔ {coin}: آخر شمعة 1m ليست خضراء — تجاهل الإشعار.")
        return

    # كل الشروط تمام → فعّل الكولداون وأرسل
    r.set(key, time.time())

    msg = f"🚀 {coin} انفجرت بـ {change}  #top{rank}" if change else f"🚀 انفجار {tag}: {coin} #top{rank}"
    send_message(msg)

    try:
        payload = {"message": {"text": f"اشتري {coin}"}}
        resp = requests.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        print(f"🛰️ إرسال إلى صقر: {payload}")
        print(f"🔁 رد صقر: {resp.status_code} - {resp.text}")
    except Exception as e:
        print("❌ فشل الإرسال إلى صقر:", e)

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
            # نحذف بعد ~50 دقيقة (يمكن تعديلها)
            if now - t > 3000:
                r.hdel("watchlist", sym.decode())
        except:
            continue

# =========================
# 🔌 WebSocket watcher بباك-اوف
# =========================
def watch_price(symbol):
    stream = f"{symbol.lower()}@trade"
    url = f"wss://stream.binance.com:9443/ws/{stream}"

    price_history = deque()
    state = {
        "base_price": None,        # القاع المحلي ضمن نافذة WATCH_DURATION
        "first_hit_time": None,    # زمن تحقق +1% الأولى
        "first_hit_price": None    # السعر عند تحقق +1% الأولى
    }

    def reset_first_step():
        state["first_hit_time"] = None
        state["first_hit_price"] = None

    def reset_all(base_to=None):
        state["base_price"] = base_to
        state["first_hit_time"] = None
        state["first_hit_price"] = None

    def on_message(ws, message):
        if r.get(IS_RUNNING_KEY) != b"1":
            ws.close()
            return

        try:
            data = json.loads(message)
        except Exception:
            return

        if "p" not in data:
            return

        try:
            price = float(data["p"])
        except Exception:
            return

        now = time.time()
        coin = symbol.replace("USDT", "").replace("BTC", "").replace("EUR", "")

        # تاريخ الأسعار ضمن النافذة
        price_history.append((now, price))
        while price_history and now - price_history[0][0] > WATCH_DURATION:
            price_history.popleft()

        if len(price_history) < 2:
            return

        # حدّث القاع المحلي ضمن النافذة
        window_min_price = min(p for _, p in price_history)

        # إذا لم تكن لدينا قاعدة، أو القاع انخفض → نعيد الضبط على القاع الجديد
        if state["base_price"] is None or window_min_price < state["base_price"]:
            reset_all(base_to=window_min_price)

        # (جديد) إلغاء التريغر الأول إذا تأخرت +1% الثانية أكثر من المهلة القصوى
        if state["first_hit_time"] and (now - state["first_hit_time"]) > MAX_WAIT_AFTER_FIRST:
            reset_first_step()

        # عتبة الخطوة الأولى (+1% على القاع المحلي)
        first_threshold = state["base_price"] * (1 + STEP_PCT / 100.0)

        # 1) التريغر الأول: السعر يصل +1% من القاع
        if state["first_hit_time"] is None:
            if price >= first_threshold:
                state["first_hit_time"] = now
                state["first_hit_price"] = price
            return

        # 2) التريغر الثاني: +1% إضافية فوق سعر الخطوة الأولى وبفاصل زمني أدنى
        second_threshold = state["first_hit_price"] * (1 + STEP_PCT / 100.0)
        time_gap_ok = (now - state["first_hit_time"]) >= STEP_GAP_SECONDS

        if time_gap_ok and price >= second_threshold:
            duration = int(now - state["first_hit_time"])
            total_change = ((price - state["base_price"]) / state["base_price"]) * 100.0
            change_str = f"{total_change:.2f}% خلال {duration} ثانية"

            # إشعار بشرط 1+1 المتتالي (مع فلترة لحظية جوّا notify_buy)
            notify_buy(coin, f"{WATCH_DURATION}s", change_str)

            # صفّر الحالة بالكامل لالتقاط فرص جديدة لاحقًا
            reset_all(base_to=None)
            return

        # (اختياري) إذا هبط السعر كثيرًا بعد الخطوة الأولى نلغيها لمنع تريغرات وهمية
        # if state["first_hit_time"] and price <= state["first_hit_price"] * 0.995:
        #     reset_first_step()

    backoff = 1
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(2)
            continue
        try:
            ws = WebSocketApp(
                url,
                on_message=on_message
            )
            # ترسل Ping تلقائيًا وتحافظ على الاتصال
            ws.run_forever(ping_interval=20, ping_timeout=10)
            print(f"[{symbol}] اتصال مُغلق. إعادة المحاولة بعد {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)  # backoff بسيط
        except Exception as e:
            print(f"[{symbol}] خطأ في ws: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

def watcher_loop():
    watched = set()
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(5)
            continue

        coins = r.hkeys("watchlist")
        symbols = {c.decode() for c in coins}
        # شغّل ووتشر لكل رمز جديد فقط
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
    # على Railway ما منستخدم app.run عادة، بس خليها محليًا
    app.run(host="0.0.0.0", port=8080)