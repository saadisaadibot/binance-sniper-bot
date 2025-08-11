# -*- coding: utf-8 -*-
import os, json, time, math, redis, threading, requests
from flask import Flask, request, jsonify
from websocket import WebSocketApp
from concurrent.futures import ThreadPoolExecutor
from collections import deque

# =========================
# 🔧 الإعدادات القابلة للتعديل
# =========================
MAX_TOP_COINS = 13             # عدد العملات المختارة من Bitvavo في كل دورة
SYMBOL_UPDATE_INTERVAL = 180   # كل كم ثانية نعيد جمع التوب من Bitvavo
WATCH_DURATION = 180           # نافذة المراقبة بالثواني (للقاع/التحليل)

# فلترة الرتبة/الإرسال
RANK_FILTER = 10               # الترتيب الأقصى للإرسال العام
RANK_MAX = 12                  # الترتيب الأقصى لإشارة score (أوسع قليلاً)
IMPROVEMENT_STEPS = 2          # أقل تحسّن بالترتيب خلال دقيقة
REQUIRE_LAST_1M_GREEN = True
RANK_CACHE_TTL = 15
ALERT_COOLDOWN_SEC = 600       # كولداون لكل (coin, tag)

# =========================
# 🧠 نظام النقاط قبل الانفجار (إشارة واحدة فقط)
# =========================
ENABLE_SCORE_SIGNAL = True
SCORE_THRESHOLD = 7            # العتبة
HOLD_SECONDS = 5               # لازم يستمر ≥ 5 ثوانٍ قبل الإرسال

# 1) اختراق نطاق
BREAKOUT_LOOKBACK_SEC = 90
BREAKOUT_PAD_PCT = 0.15

# 2) سَكويز → توسّع
SQUEEZE_LOOKBACK_SEC = 120
SQUEEZE_MAX_STD_PCT = 0.20
RECENT_STD_SEC = 15
PRIOR_STD_SEC = 45
EXPANSION_MIN_MULT = 1.3

# 3) ميل/تسارع
MIN_SLOPE_PCT_PER_SEC = 0.02   # ميل 5s
MIN_ACCEL = 0.01               # (ميل5s - ميل15s)

# 4) قيعان أعلى
HIGHER_LOWS_REQUIRED = 2
HL_MIN_DIFF_PCT = 0.20
HL_MIN_GAP_SEC = 10

# 5) حجم
VOL_SPIKE_MULT = 1.8           # حجم 1m ≥ 1.8x متوسط آخر 5

# فلاتر قتل الإشارات الوهمية
RETEST_MAX_DROP_PCT = 0.40     # لا رجوع >0.4% تحت خط الاختراق أول 10s
LONG_WICK_DROP_PCT = 0.70      # لا قفزة ثم هبوط >0.7% خلال 5s

# 🔑 متغيرات البيئة
REDIS_URL = os.getenv("REDIS_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "https://saadisaadibot-saqarxbo-production.up.railway.app/")
IS_RUNNING_KEY = "sniper_running"

app = Flask(__name__)
r = redis.from_url(REDIS_URL)

# =========================
# أدوات مساعدة
# =========================
def send_message(text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print("فشل إرسال الرسالة:", e)

def get_candle_change(market, interval):
    try:
        url = f"https://api.bitvavo.com/v2/{market}/candles?interval={interval}&limit=2"
        res = requests.get(url, timeout=6).json()
        if not isinstance(res, list) or len(res) < 2: return None
        o, c = float(res[-2][1]), float(res[-2][4])
        return ((c - o) / o) * 100
    except Exception as e:
        print(f"❌ get_candle_change({market},{interval}):", e); return None

def fetch_binance_symbols_cached():
    try:
        ck = "binance:exchangeInfo"; cached = r.get(ck)
        if cached: return json.loads(cached)
        info = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=8).json()
        r.setex(ck, 600, json.dumps(info)); return info
    except Exception as e:
        print("⚠️ exchangeInfo:", e); return {"symbols": []}

def prefer_pair(base, symbols):
    base = base.upper()
    cands = [s for s in symbols if s.get("baseAsset","").upper()==base and s.get("status")=="TRADING"]
    if not cands: return None
    for q in ("USDT","EUR","BTC"):
        for s in cands:
            if s.get("quoteAsset")==q: return s.get("symbol")
    return cands[0].get("symbol")

ALIASES = {}

def fetch_top_bitvavo_then_match_binance():
    try:
        r.delete("not_found_binance")
        markets_res = requests.get("https://api.bitvavo.com/v2/markets", timeout=8).json()
        markets = [m["market"] for m in markets_res if m.get("market","").endswith("-EUR")]

        def process(mkt):
            sym = mkt.replace("-EUR","").upper()
            ch5 = get_candle_change(mkt,"5m")
            return (sym, ch5)

        changes = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            for s, ch in ex.map(process, markets):
                if ch is not None: changes.append((s, ch))
        top_syms = [s for s,_ in sorted(changes, key=lambda x:x[1], reverse=True)[:MAX_TOP_COINS]]
        top_syms = list(dict.fromkeys(top_syms))

        info = fetch_binance_symbols_cached(); syms = info.get("symbols",[])
        matched, not_found = [], []
        for c in top_syms:
            base = ALIASES.get(c,c).upper()
            best = prefer_pair(base, syms)
            (matched.append(best) if best else not_found.append(c))
        if not_found: r.sadd("not_found_binance", *not_found)
        print(f"📊 Bitvavo top → Binance:", matched)
        return matched
    except Exception as e:
        print("❌ fetch_top:", e); return []

def get_rank_from_bitvavo(coin, *, force_refresh=False):
    try:
        ck = "rank_cache:all"; sorted_changes = None
        if not force_refresh:
            cached = r.get(ck)
            if cached: sorted_changes = json.loads(cached)
        if sorted_changes is None:
            markets_res = requests.get("https://api.bitvavo.com/v2/markets", timeout=8).json()
            markets = [m["market"] for m in markets_res if m.get("market","").endswith("-EUR")]
            arr = []
            for m in markets:
                s = m.replace("-EUR","").upper()
                ch5 = get_candle_change(m,"5m")
                if ch5 is not None: arr.append((s,ch5))
            sorted_changes = sorted(arr, key=lambda x:x[1], reverse=True)
            r.setex(ck, RANK_CACHE_TTL, json.dumps(sorted_changes))
        for i,(s,_) in enumerate(sorted_changes,1):
            if s == coin.upper(): return i
        return None
    except Exception as e:
        print("⚠️ get_rank:", e); return None

def is_last_1m_green(coin):
    try:
        url = f"https://api.bitvavo.com/v2/{coin.upper()}-EUR/candles?interval=1m&limit=2"
        res = requests.get(url, timeout=5).json()
        if isinstance(res,list) and len(res)>=2:
            o = float(res[-2][1]); c = float(res[-2][4]); return c>=o
    except Exception as e:
        print("⚠️ 1m green:", e)
    return True

def get_1m_volume(coin):
    try:
        url = f"https://api.bitvavo.com/v2/{coin.upper()}-EUR/candles?interval=1m&limit=7"
        res = requests.get(url, timeout=5).json()
        if not isinstance(res,list) or len(res)<6: return None,None
        last_vol = float(res[-2][5]); avg5 = sum(float(x[5]) for x in res[-7:-2]) / 5.0
        return last_vol, avg5
    except Exception:
        return None, None

# =========================
# إحصائيات من price_history
# =========================
def std_pct(vals):
    if not vals: return 0.0
    m = sum(vals)/len(vals)
    if m==0: return 0.0
    var = sum((v-m)**2 for v in vals)/len(vals)
    return (math.sqrt(var)/m)*100.0

def window_vals(history, secs):
    now = time.time()
    return [p for t,p in history if now - t <= secs]

def value_at(history, secs_back):
    now = time.time()
    cand = [(abs((now - t) - secs_back), p) for t,p in history]
    if not cand: return None
    return min(cand, key=lambda x:x[0])[1]

def highest_in(history, secs):
    vals = window_vals(history, secs)
    return max(vals) if vals else None

def lowest_in(history, secs):
    vals = window_vals(history, secs)
    return min(vals) if vals else None

def slope_pct_per_sec(history, secs):
    p_old = value_at(history, secs)
    if not p_old: return 0.0
    p_now = history[-1][1]
    return (((p_now - p_old)/p_old)*100.0)/secs

def accel(history):
    s5 = slope_pct_per_sec(history, 5)
    s15 = slope_pct_per_sec(history, 15)
    return s5 - s15, s5, s15

def count_higher_lows(history, lookback=120, min_gap=HL_MIN_GAP_SEC, min_diff_pct=HL_MIN_DIFF_PCT):
    now = time.time()
    # استخرج قيعان محلية بسيطة
    pts = [(t,p) for t,p in history if now - t <= lookback]
    if len(pts) < 5: return 0
    lows = []
    for i in range(1,len(pts)-1):
        if pts[i][1] < pts[i-1][1] and pts[i][1] < pts[i+1][1]:
            if not lows or (pts[i][0] - lows[-1][0] >= min_gap):
                lows.append(pts[i])
    # احسب قيعان أعلى متتالية بفارق نسبي
    cnt = 0
    for i in range(1,len(lows)):
        prev, cur = lows[i-1][1], lows[i][1]
        if (cur - prev)/prev*100.0 >= min_diff_pct:
            cnt += 1
    return cnt

# =========================
# الإشعار النهائي (مدروس)
# =========================
def notify_buy(coin, tag, change_text=None, *, allow_rank_max=False):
    key = f"buy_alert:{coin}:{tag}"
    last = r.get(key)
    if last and time.time() - float(last) < ALERT_COOLDOWN_SEC:
        return

    rank = get_rank_from_bitvavo(coin, force_refresh=True)
    max_rank = RANK_MAX if allow_rank_max else RANK_FILTER
    if not rank or rank > max_rank:
        print(f"⛔ {coin} خارج التوب {max_rank} (rank={rank})."); return

    # تحسّن أو دخول جديد
    prev_k, prev_ts_k = f"rank_prev:{coin}", f"rank_prev_ts:{coin}"
    prev = r.get(prev_k); prev = int(prev) if prev else None
    r.set(prev_k, rank); r.set(prev_ts_k, time.time())
    just_entered = (prev is None) or (prev > max_rank and rank <= max_rank)
    improved = (prev is not None) and ((prev - rank) >= IMPROVEMENT_STEPS)
    if not (just_entered or improved):
        print(f"⛔ {coin} دون تحسّن كافٍ (prev={prev} → now={rank})."); return

    if REQUIRE_LAST_1M_GREEN and not is_last_1m_green(coin):
        print(f"⛔ {coin} شمعة 1m ليست خضراء."); return

    # حجم 1m
    v_now, v_avg = get_1m_volume(coin)
    if not (v_now and v_avg and v_now >= VOL_SPIKE_MULT * v_avg):
        print(f"⛔ {coin} حجم غير كافٍ {v_now}<{VOL_SPIKE_MULT}×{v_avg}."); return

    r.set(key, time.time())
    msg = f"🚀 {coin} setup مدروس #top{rank}" if not change_text else f"🚀 {coin} {change_text} #top{rank}"
    send_message(msg)
    try:
        payload = {"message": {"text": f"اشتري {coin}"}}
        resp = requests.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        print(f"🛰️ صقر <= {payload} | {resp.status_code} {resp.text[:120]}")
    except Exception as e:
        print("❌ إرسال صقر:", e)

# =========================
# دورات جلب التوب + مراقبة
# =========================
def update_symbols_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1": time.sleep(5); continue
        print("🌀 دورة جديدة لجلب التوب...")
        top_symbols = fetch_top_bitvavo_then_match_binance()
        if not top_symbols:
            send_message("⚠️ لا عملات صالحة في هذه الدورة."); time.sleep(SYMBOL_UPDATE_INTERVAL); continue
        now = time.time()
        for s in top_symbols: r.hset("watchlist", s, now)
        print(f"📡 حدّثنا المراقبة: {len(top_symbols)} رمز.")
        cleanup_old_coins()
        time.sleep(SYMBOL_UPDATE_INTERVAL)

def cleanup_old_coins():
    now = time.time()
    for sym, ts in r.hgetall("watchlist").items():
        try:
            t = float(ts.decode())
            if now - t > 3000: r.hdel("watchlist", sym.decode())
        except: continue

# =========================
# المراقِب اللحظي (سكور صارم فقط)
# =========================
def watch_price(symbol):
    stream = f"{symbol.lower()}@trade"
    url = f"wss://stream.binance.com:9443/ws/{stream}"

    price_history = deque()
    state = {
        "breakout_price": None,
        "score_hold_start": None,   # متى تعدت النقاط العتبة
        "last_sent": 0.0
    }

    def reset_score():
        state["score_hold_start"] = None

    def on_message(ws, message):
        if r.get(IS_RUNNING_KEY) != b"1": ws.close(); return
        try:
            data = json.loads(message)
            price = float(data.get("p"))
        except Exception:
            return
        now = time.time()
        coin = symbol.replace("USDT","").replace("BTC","").replace("EUR","")

        # تحديث التاريخ
        price_history.append((now, price))
        while price_history and now - price_history[0][0] > WATCH_DURATION:
            price_history.popleft()
        if len(price_history) < 6: return

        # ============= نظام النقاط =============
        S = 0
        details = []

        # 1) اختراق نطاق
        hi = highest_in(price_history, BREAKOUT_LOOKBACK_SEC)
        breakout_ok = False
        if hi:
            br_level = hi * (1 + BREAKOUT_PAD_PCT/100.0)
            if price >= br_level:
                S += 2; details.append("BR")
                breakout_ok = True
                if state["breakout_price"] is None or br_level > state["breakout_price"]:
                    state["breakout_price"] = br_level

        # 2) سَكويز → توسّع
        sq_vals = window_vals(price_history, SQUEEZE_LOOKBACK_SEC)
        if sq_vals:
            sq_std = std_pct(sq_vals)
            rec_std = std_pct(window_vals(price_history, RECENT_STD_SEC))
            pri_std = std_pct(window_vals(price_history, PRIOR_STD_SEC))
            if sq_std <= SQUEEZE_MAX_STD_PCT and rec_std >= max(1e-9, pri_std)*EXPANSION_MIN_MULT:
                S += 2; details.append("SQ")

        # 3) ميل/تسارع
        a, s5, s15 = accel(price_history)
        if s5 >= MIN_SLOPE_PCT_PER_SEC:
            S += 1; details.append("S5")
        if a >= MIN_ACCEL:
            S += 1; details.append("ACC")

        # 4) قيعان أعلى
        if count_higher_lows(price_history, 120, HL_MIN_GAP_SEC, HL_MIN_DIFF_PCT) >= HIGHER_LOWS_REQUIRED:
            S += 1; details.append("HL")

        # 5/6) ترتيب + حجم (نؤجّل حتى شبه تأكيد محلي)
        if S >= 4:
            rank_now = get_rank_from_bitvavo(coin, force_refresh=True)
            v_now, v_avg = get_1m_volume(coin)
            if rank_now and rank_now <= RANK_MAX and v_now and v_avg and v_now >= VOL_SPIKE_MULT*v_avg:
                S += 2; details.append("R+V")
                # تحسّن بالترتيب
                prev = r.get(f"rank_prev:{coin}")
                prev = int(prev) if prev else None
                if prev is None or (prev - rank_now) >= IMPROVEMENT_STEPS:
                    S += 1; details.append("IMP")
                r.set(f"rank_prev:{coin}", rank_now)
                r.set(f"rank_prev_ts:{coin}", now)

        # الاستمرارية وفلترة القتل
        if S >= SCORE_THRESHOLD:
            if state["score_hold_start"] is None:
                state["score_hold_start"] = now
            hold_ok = (now - state["score_hold_start"]) >= HOLD_SECONDS
        else:
            reset_score(); hold_ok = False

        kill = False
        # لا رجوع قوي بعد الاختراق في أول 10s
        if breakout_ok and state["breakout_price"]:
            if (price < state["breakout_price"]*(1 - RETEST_MAX_DROP_PCT/100.0)) and (now - state["score_hold_start"] <= 10 if state["score_hold_start"] else False):
                kill = True
        # لا ويك طويل
        p5ago = value_at(price_history, 5)
        if p5ago and ((p5ago - price)/p5ago*100.0) >= LONG_WICK_DROP_PCT:
            kill = True

        # إرسال الإشارة
        if ENABLE_SCORE_SIGNAL and hold_ok and not kill:
            if now - state["last_sent"] > 2:  # منع تكرار لحظي
                change_txt = f"setup مدروس S={S} ({'+'.join(details)})"
                notify_buy(coin, "setup", change_txt, allow_rank_max=True)
                state["last_sent"] = now
                reset_score()
        # ========================================

    backoff = 1
    while True:
        if r.get(IS_RUNNING_KEY) != b"1": time.sleep(2); continue
        try:
            ws = WebSocketApp(url, on_message=on_message)
            ws.run_forever(ping_interval=20, ping_timeout=10)
            print(f"[{symbol}] اتصال أغلق. إعادة بعد {backoff}s")
            time.sleep(backoff); backoff = min(backoff*2, 30)
        except Exception as e:
            print(f"[{symbol}] WS خطأ:", e)
            time.sleep(backoff); backoff = min(backoff*2, 30)

def watcher_loop():
    watched = set()
    while True:
        if r.get(IS_RUNNING_KEY) != b"1": time.sleep(5); continue
        coins = r.hkeys("watchlist")
        symbols = {c.decode() for c in coins}
        for sym in symbols - watched:
            threading.Thread(target=watch_price, args=(sym,), daemon=True).start()
            watched.add(sym)
        time.sleep(1)

# =========================
# واجهات
# =========================
@app.route("/")
def home():
    return "🔥 Sniper (Score-only) is Live", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json() or {}
    txt = (data.get("message",{}).get("text") or "").strip().lower()
    if txt == "play":
        r.set(IS_RUNNING_KEY, "1"); send_message("✅ بدأ التشغيل (Score-only).")
    elif txt == "stop":
        r.set(IS_RUNNING_KEY, "0"); send_message("🛑 تم الإيقاف مؤقتاً.")
    elif txt == "العملات المفقودة":
        coins = r.smembers("not_found_binance")
        names = [c.decode() for c in coins]
        send_message("🚫 غير موجودة على Binance:\n" + ", ".join(names) if names else "✅ لا توجد عملات مفقودة.")
    elif txt == "السجل":
        coins = r.hkeys("watchlist")
        if coins:
            coin_list = [c.decode().replace("USDT","").replace("BTC","").replace("EUR","") for c in coins]
            msg = ""
            for i,s in enumerate(coin_list,1):
                msg += f"{i}. {s}   "
                if i%5==0: msg += "\n"
            send_message("📡 العملات المرصودة:\n"+msg.strip())
        else:
            send_message("🚫 لا توجد عملات قيد المراقبة.")
    elif txt == "reset":
        r.delete("watchlist"); send_message("🧹 مسحنا الذاكرة. سيبدأ الجمع بالدورة القادمة.")
    return jsonify(ok=True)

# =========================
# تشغيل
# =========================
if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    threading.Thread(target=update_symbols_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)