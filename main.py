# -*- coding: utf-8 -*-
import os, json, time, math, redis, threading, requests, statistics
from flask import Flask, request, jsonify
from websocket import WebSocketApp
from concurrent.futures import ThreadPoolExecutor
from collections import deque

# =========================
# 🔧 الإعدادات القابلة للتعديل
# =========================
MAX_TOP_COINS = int(os.getenv("MAX_TOP_COINS", 13))        # عدد العملات المختارة من Bitvavo في كل دورة
SYMBOL_UPDATE_INTERVAL = int(os.getenv("SYMBOL_UPDATE_INTERVAL", 180))
WATCH_DURATION = int(os.getenv("WATCH_DURATION", 180))     # نافذة المراقبة (ثوانٍ)

# فلترة الرتبة/الإرسال
RANK_FILTER = int(os.getenv("RANK_FILTER", 10))            # أقصى ترتيب مسموح للإرسال
RANK_MAX = int(os.getenv("RANK_MAX", 12))                  # أقصى ترتيب عند allow_rank_max
IMPROVEMENT_STEPS = int(os.getenv("IMPROVEMENT_STEPS", 2)) # أقل تحسّن بالترتيب
REQUIRE_LAST_1M_GREEN = os.getenv("REQUIRE_LAST_1M_GREEN", "1") == "1"
RANK_CACHE_TTL = int(os.getenv("RANK_CACHE_TTL", 15))
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", 600))     # كولداون (coin, tag)
GLOBAL_BUDGET_WINDOW = int(os.getenv("GLOBAL_BUDGET_WINDOW", 600)) # نافذة ميزانية الإشارات
GLOBAL_BUDGET_MAX = int(os.getenv("GLOBAL_BUDGET_MAX", 6))         # حد أقصى لإشارات 10 دقائق

# =========================
# 🧠 نظام النقاط (قابل للتكيّف)
# =========================
ENABLE_SCORE_SIGNAL = os.getenv("ENABLE_SCORE_SIGNAL", "1") == "1"
BASE_SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", 7))
BASE_HOLD_SECONDS = float(os.getenv("HOLD_SECONDS", 5))

# 1) اختراق نطاق
BREAKOUT_LOOKBACK_SEC = int(os.getenv("BREAKOUT_LOOKBACK_SEC", 90))
BREAKOUT_PAD_PCT = float(os.getenv("BREAKOUT_PAD_PCT", 0.15))

# 2) سَكويز → توسّع
SQUEEZE_LOOKBACK_SEC = int(os.getenv("SQUEEZE_LOOKBACK_SEC", 120))
SQUEEZE_MAX_STD_PCT = float(os.getenv("SQUEEZE_MAX_STD_PCT", 0.20))
RECENT_STD_SEC = int(os.getenv("RECENT_STD_SEC", 15))
PRIOR_STD_SEC = int(os.getenv("PRIOR_STD_SEC", 45))
EXPANSION_MIN_MULT = float(os.getenv("EXPANSION_MIN_MULT", 1.3))

# 3) ميل/تسارع + Anti blow-off
MIN_SLOPE_PCT_PER_SEC = float(os.getenv("MIN_SLOPE_PCT_PER_SEC", 0.02))  # ميل 5s
MIN_ACCEL = float(os.getenv("MIN_ACCEL", 0.01))                           # (ميل5s - ميل15s)
MAX_BLOWOFF_SLOPE = float(os.getenv("MAX_BLOWOFF_SLOPE", 0.6))            # حد ميل 5s لتجنّب blow-off

# 4) قيعان أعلى
HIGHER_LOWS_REQUIRED = int(os.getenv("HIGHER_LOWS_REQUIRED", 2))
HL_MIN_DIFF_PCT = float(os.getenv("HL_MIN_DIFF_PCT", 0.20))
HL_MIN_GAP_SEC = int(os.getenv("HL_MIN_GAP_SEC", 10))

# 5) حجم
BASE_VOL_SPIKE_MULT = float(os.getenv("VOL_SPIKE_MULT", 1.8))  # أساس 1m ≥ x متوسط آخر 5

# فلاتر قتل الإشارة
RETEST_MAX_DROP_PCT = float(os.getenv("RETEST_MAX_DROP_PCT", 0.40))   # لا رجوع >0.4% تحت الاختراق أول 10s
LONG_WICK_DROP_PCT = float(os.getenv("LONG_WICK_DROP_PCT", 0.70))     # لا قفزة ثم هبوط >0.7% خلال 5s
POST_SEND_MAX_DD_PCT = float(os.getenv("POST_SEND_MAX_DD_PCT", 0.9))  # دروداون خلال 10s بعد الإرسال ⇒ حظر مؤقت

# 🔑 متغيرات البيئة
REDIS_URL = os.getenv("REDIS_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SAQAR_WEBHOOK = os.getenv("SAQAR_WEBHOOK", "https://saadisaadibot-saqarxbo-production.up.railway.app/")
IS_RUNNING_KEY = "sniper_running"

app = Flask(__name__)
r = redis.from_url(REDIS_URL)

# مفاتيح داخلية
GLOBAL_BUDGET_KEY = "alerts:global_times"           # ZSET timestamps
BINANCE_INFO_CACHE = "binance:exchangeInfo"
RANK_CACHE_ALL = "rank_cache:all"
FAIL_BLACKLIST_PREFIX = "failblk:"                   # failblk:{coin}
ACTIVE_WS_SET_KEY = "ws:active_set"                  # آخر مجموعة رموز فعلية في WS

# =========================
# أدوات مساعدة
# =========================
def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}, timeout=5
        )
    except Exception as e:
        print("فشل إرسال الرسالة:", e)

def _get(url, timeout=8):
    return requests.get(url, timeout=timeout).json()

def get_candle_change(market, interval):
    try:
        res = _get(f"https://api.bitvavo.com/v2/{market}/candles?interval={interval}&limit=3")
        if not isinstance(res, list) or len(res) < 2: return None
        o, c = float(res[-2][1]), float(res[-2][4])
        return ((c - o) / o) * 100.0
    except Exception as e:
        print(f"❌ get_candle_change({market},{interval}):", e); return None

def fetch_binance_symbols_cached():
    try:
        cached = r.get(BINANCE_INFO_CACHE)
        if cached: return json.loads(cached)
        info = _get("https://api.binance.com/api/v3/exchangeInfo", timeout=8)
        r.setex(BINANCE_INFO_CACHE, 600, json.dumps(info))
        return info
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

ALIASES = {}  # إن أردت إصلاح أسماء

def bitvavo_markets_changes():
    """يُعيد (sorted_arr, median, p75) حيث sorted_arr = [(SYMBOL, ch5m), ...]"""
    try:
        markets_res = _get("https://api.bitvavo.com/v2/markets", timeout=8)
        markets = [m["market"] for m in markets_res if m.get("market","").endswith("-EUR")]
        arr = []
        for m in markets:
            ch5 = get_candle_change(m, "5m")
            if ch5 is not None:
                arr.append((m.replace("-EUR","").upper(), ch5))
        if not arr:
            return [], 0.0, 0.0
        changes = [c for _, c in arr]
        med = statistics.median(changes)
        # p75 آمن حتى لو العناصر قليلة
        if len(changes) >= 4:
            p75 = statistics.quantiles(changes, n=4)[2]
        else:
            # تقريب p75 بسيط لما القائمة قصيرة
            p75 = sorted(changes)[int(len(changes)*0.75) - 1] if len(changes) > 1 else changes[0]
        arr.sort(key=lambda x:x[1], reverse=True)
        return arr, med, p75
    except Exception as e:
        print("❌ bitvavo_markets_changes:", e)
        return [], 0.0, 0.0
def filter_binance_tradables(candidates):
    info = fetch_binance_symbols_cached()
    by_name = {s["symbol"]: s for s in info.get("symbols", [])}
    ok = []
    for sym in candidates:
        s = by_name.get(sym)
        if not s or s.get("status") != "TRADING":
            continue
        name = s.get("symbol","")
        if any(name.endswith(x) for x in ("UPUSDT","DOWNUSDT","BULLUSDT","BEARUSDT")):
            continue
        ok.append(sym)  # لا نعقّد الـNOTIONAL هنا
    return ok

def fetch_top_bitvavo_then_match_binance():
    try:
        r.delete("not_found_binance")
        sorted_changes, med, p75 = bitvavo_markets_changes()
        if sorted_changes:
            r.setex("market:breadth:med", 60, str(med))
            r.setex("market:breadth:p75", 60, str(p75))
        top_syms = [s for s,_ in sorted_changes[:MAX_TOP_COINS]]
        top_syms = list(dict.fromkeys(top_syms))
        info = fetch_binance_symbols_cached(); syms = info.get("symbols",[])
        matched, not_found = [], []
        for c in top_syms:
            base = ALIASES.get(c, c).upper()
            best = prefer_pair(base, syms)
            (matched.append(best) if best else not_found.append(c))
        if not_found: r.sadd("not_found_binance", *not_found)
        matched = filter_binance_tradables(matched)
        print(f"📊 Bitvavo top → Binance tradables:", matched)
        return matched
    except Exception as e:
        print("❌ fetch_top:", e); return []

def get_rank_from_bitvavo(coin, *, force_refresh=False):
    try:
        sorted_changes = None
        if not force_refresh:
            cached = r.get(RANK_CACHE_ALL)
            if cached:
                sorted_changes = json.loads(cached)
        if sorted_changes is None:
            # كمل احتياطاً، بس لا تستدعي كثيراً
            sorted_changes, _, _ = bitvavo_markets_changes()
            r.setex(RANK_CACHE_ALL, RANK_CACHE_TTL, json.dumps(sorted_changes))
        for i,(s,_) in enumerate(sorted_changes,1):
            if s == coin.upper(): return i
        return None
    except Exception as e:
        print("⚠️ get_rank:", e); return None

def is_last_1m_green(coin):
    try:
        res = _get(f"https://api.bitvavo.com/v2/{coin.upper()}-EUR/candles?interval=1m&limit=2", timeout=5)
        if isinstance(res,list) and len(res)>=2:
            o = float(res[-2][1]); c = float(res[-2][4]); return c>=o
    except Exception as e:
        print("⚠️ 1m green:", e)
    return True

def get_1m_volume(coin):
    key = f"v1m:{coin.upper()}"
    cached = r.get(key)
    if cached:
        try: 
            x = json.loads(cached); return x["last"], x["avg5"]
        except: 
            pass
    try:
        res = _get(f"https://api.bitvavo.com/v2/{coin.upper()}-EUR/candles?interval=1m&limit=7", timeout=5)
        if not isinstance(res,list) or len(res)<6: return None, None
        last_vol = float(res[-2][5]); avg5 = sum(float(x[5]) for x in res[-7:-2]) / 5.0
        r.setex(key, 8, json.dumps({"last": last_vol, "avg5": avg5}))
        return last_vol, avg5
    except Exception:
        return None, None
# =========================
# حسابات من price_history
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
    pts = [(t,p) for t,p in history if now - t <= lookback]
    if len(pts) < 5: return 0
    lows = []
    for i in range(1,len(pts)-1):
        if pts[i-1][1] > pts[i][1] < pts[i+1][1]:
            if not lows or (pts[i][0] - lows[-1][0] >= min_gap):
                lows.append(pts[i])
    cnt = 0
    for i in range(1,len(lows)):
        prev, cur = lows[i-1][1], lows[i][1]
        if (cur - prev)/prev*100.0 >= min_diff_pct:
            cnt += 1
    return cnt

# =========================
# ميزانية الإشارات + بلاك ليست
# =========================
def global_budget_ok():
    now = time.time()
    r.zremrangebyscore(GLOBAL_BUDGET_KEY, 0, now - GLOBAL_BUDGET_WINDOW)
    cnt = r.zcard(GLOBAL_BUDGET_KEY)
    if cnt >= GLOBAL_BUDGET_MAX:
        return False
    r.zadd(GLOBAL_BUDGET_KEY, {str(now): now})
    return True
def fail_blacklisted(coin):
    return r.ttl(f"{FAIL_BLACKLIST_PREFIX}{coin}") > 0

def fail_blacklist(coin, seconds):
    r.setex(f"{FAIL_BLACKLIST_PREFIX}{coin}", seconds, "1")

# =========================
# الإشعار النهائي
# =========================
def notify_buy(coin, tag, change_text=None, *, allow_rank_max=False):
    # كولداون لكل عملة/وسم
    key = f"buy_alert:{coin}:{tag}"
    last = r.get(key)
    if last and time.time() - float(last) < ALERT_COOLDOWN_SEC:
        return
    if not global_budget_ok():
        print("⛔ تجاوزنا ميزانية الإشارات العالمية مؤقتًا."); return
    if fail_blacklisted(coin):
        print(f"⛔ {coin} محظورة مؤقتًا بعد دروداون سابق."); return

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

    v_now, v_avg = get_1m_volume(coin)
    # عتبة حجم تكيفية حسب حرارة السوق
    med = float(r.get("market:breadth:med") or "0")
    p75 = float(r.get("market:breadth:p75") or "0")
    vol_mult = BASE_VOL_SPIKE_MULT - (0.2 if p75 >= 1.0 else 0.0) + (0.2 if p75 <= 0.1 else 0.0)
    if not (v_now and v_avg and v_now >= vol_mult * v_avg):
        print(f"⛔ {coin} حجم غير كافٍ {v_now}<{vol_mult:.2f}×{v_avg}."); return

    r.set(key, time.time())
    msg = f"🚀 {coin} setup مدروس #top{rank}" if not change_text else f"🚀 {coin} {change_text} #top{rank}"
    send_message(msg)
    try:
        payload = {"message": {"text": f"اشتري {coin}"}}
        resp = requests.post(SAQAR_WEBHOOK, json=payload, timeout=8)
        print(f"🛰️ صقر <= {payload} | {resp.status_code} {resp.text[:120]}")
        # مراقبة دروداون ما بعد الإرسال (حظر مؤقت عند اللزوم)
        r.setex(f"postsend:watch:{coin}", 12, "1")
    except Exception as e:
        print("❌ إرسال صقر:", e)

# =========================
# WebSocket مدمج لعدة رموز
# =========================
def start_combined_ws(symbols, gen):
    if not symbols:
        return
    stream = "/".join([f"{s.lower()}@trade" for s in symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={stream}"

    states = { s: {"price_history": deque(), "last_sent": 0.0, "last_send_price": None,
                   "breakout_price": None, "score_hold_start": None}
               for s in symbols }

    def reset_score(st):
        st["score_hold_start"] = None

    def on_message(ws, message):
        # أغلق إذا GEN تغيّر
        cur_gen = int(r.get("ws:gen") or b"0")
        if cur_gen != gen or r.get(IS_RUNNING_KEY) != b"1":
            ws.close(); return
        try:
            payload = json.loads(message)
            stream_name = payload.get("stream","")
            data = payload.get("data",{})
            price = float(data.get("p"))
            symbol = stream_name.split("@")[0].upper()
        except Exception:
            return

        st = states.get(symbol)
        if not st: return
        now = time.time()
        coin = symbol.replace("USDT","").replace("BTC","").replace("EUR","")

        # تحديث التاريخ
        ph = st["price_history"]
        ph.append((now, price))
        while ph and now - ph[0][0] > WATCH_DURATION:
            ph.popleft()
        if len(ph) < 6: return

        # حرارة السوق لتكييف العتبات
        p75 = float(r.get("market:breadth:p75") or "0")
        SCORE_THRESHOLD = BASE_SCORE_THRESHOLD - (1.0 if p75 >= 1.5 else 0.0) + (0.5 if p75 <= 0.1 else 0.0)
        HOLD_SECONDS = BASE_HOLD_SECONDS + (1.0 if p75 <= 0.0 else 0.0)

        # ============= نظام النقاط =============
        S = 0
        details = []

        # 1) اختراق نطاق
        hi = highest_in(ph, BREAKOUT_LOOKBACK_SEC)
        breakout_ok = False
        if hi:
            br_level = hi * (1 + BREAKOUT_PAD_PCT/100.0)
            if price >= br_level:
                S += 2; details.append("BR")
                breakout_ok = True
                if st["breakout_price"] is None or br_level > st["breakout_price"]:
                    st["breakout_price"] = br_level

        # 2) سَكويز → توسّع
        sq_vals = window_vals(ph, SQUEEZE_LOOKBACK_SEC)
        if sq_vals:
            sq_std = std_pct(sq_vals)
            rec_std = std_pct(window_vals(ph, RECENT_STD_SEC))
            pri_std = std_pct(window_vals(ph, PRIOR_STD_SEC))
            if sq_std <= SQUEEZE_MAX_STD_PCT and rec_std >= max(1e-9, pri_std)*EXPANSION_MIN_MULT:
                S += 2; details.append("SQ")

        # 3) ميل/تسارع + منع blow-off
        a = slope_pct_per_sec(ph, 5) - slope_pct_per_sec(ph, 15)
        s5 = slope_pct_per_sec(ph, 5)
        if s5 >= MIN_SLOPE_PCT_PER_SEC:
            S += 1; details.append("S5")
        if a >= MIN_ACCEL:
            S += 1; details.append("ACC")
        if s5 > MAX_BLOWOFF_SLOPE:
            reset_score(st); return

        # 4) قيعان أعلى
        if count_higher_lows(ph, 120, HL_MIN_GAP_SEC, HL_MIN_DIFF_PCT) >= HIGHER_LOWS_REQUIRED:
            S += 1; details.append("HL")

        # 5/6) ترتيب + حجم (نؤجّل حتى شبه تأكيد)
        if S >= (SCORE_THRESHOLD - 2):
            rank_now = get_rank_from_bitvavo(coin, force_refresh=True)
            v_now, v_avg = get_1m_volume(coin)
            vol_mult = BASE_VOL_SPIKE_MULT - (0.2 if p75 >= 1.0 else 0.0) + (0.2 if p75 <= 0.1 else 0.0)
            if rank_now and rank_now <= RANK_MAX and v_now and v_avg and v_now >= vol_mult*v_avg:
                S += 2; details.append("R+V")
                prev = r.get(f"rank_prev:{coin}")
                prev = int(prev) if prev else None
                if prev is None or (prev - rank_now) >= IMPROVEMENT_STEPS:
                    S += 1; details.append("IMP")
                r.set(f"rank_prev:{coin}", rank_now)
                r.set(f"rank_prev_ts:{coin}", now)

        # الاستمرارية + فلترة القتل
        if S >= SCORE_THRESHOLD:
            if st["score_hold_start"] is None:
                st["score_hold_start"] = now
            hold_ok = (now - st["score_hold_start"]) >= HOLD_SECONDS
        else:
            reset_score(st); hold_ok = False

        kill = False
        # لا رجوع قوي بعد الاختراق بأول 10s
        if breakout_ok and st["breakout_price"] and st["score_hold_start"]:
            if (price < st["breakout_price"]*(1 - RETEST_MAX_DROP_PCT/100.0)) and (now - st["score_hold_start"] <= 10):
                kill = True
        # لا ويك طويل
        p5ago = value_at(ph, 5)
        if p5ago and ((p5ago - price)/p5ago*100.0) >= LONG_WICK_DROP_PCT:
            kill = True

        # مراقبة دروداون ما بعد إرسال سابق
        if r.get(f"postsend:watch:{coin}"):
            base_price = st.get("last_send_price")
            if base_price:
                dd = (base_price - price)/base_price*100.0
                if dd >= POST_SEND_MAX_DD_PCT:
                    fail_blacklist(coin, ALERT_COOLDOWN_SEC)  # حظر مؤقت
                    r.delete(f"postsend:watch:{coin}")

        # إرسال الإشارة
        if ENABLE_SCORE_SIGNAL and hold_ok and not kill:
            if time.time() - st["last_sent"] > 2:
                change_txt = f"setup مدروس S={S} ({'+'.join(details)})"
                st["last_send_price"] = price
                notify_buy(coin, "setup", change_txt, allow_rank_max=True)
                st["last_sent"] = time.time()
                reset_score(st)
        # ========================================

    backoff = 1
    while True:
        if r.get(IS_RUNNING_KEY) != b"1":
            time.sleep(2); continue
        try:
            ws = WebSocketApp(url, on_message=on_message)
            ws.run_forever(ping_interval=20, ping_timeout=10)
            print(f"[WS] اتصال أغلق. إعادة بعد {backoff}s")
            time.sleep(backoff); backoff = min(backoff*2, 30)
        except Exception as e:
            print(f"[WS] خطأ:", e)
            time.sleep(backoff); backoff = min(backoff*2, 30)

# =========================
# دورات جلب التوب + مراقبة
# =========================
def update_symbols_loop():
    while True:
        if r.get(IS_RUNNING_KEY) != b"1": time.sleep(5); continue
        print("🌀 دورة جديدة لجلب التوب...")
        # --- جلب الرتب مرة لكل دورة وتخزينها ---
        sorted_changes, med, p75 = bitvavo_markets_changes()
        if sorted_changes:
            r.setex(RANK_CACHE_ALL, RANK_CACHE_TTL, json.dumps(sorted_changes))
            r.setex("market:breadth:med", 60, str(med))
            r.setex("market:breadth:p75", 60, str(p75))
        # ---------------------------------------
        top_symbols = fetch_top_bitvavo_then_match_binance()  # يستخدم نفس الدالة كما هي
        if not top_symbols:
            send_message("⚠️ لا عملات صالحة في هذه الدورة.")
            time.sleep(SYMBOL_UPDATE_INTERVAL); continue
        now = time.time()
        for s in top_symbols: r.hset("watchlist", s, now)
        print(f"📡 حدّثنا المراقبة: {len(top_symbols)} رمز.")
        cleanup_old_coins()
        # إدارة GEN للـWS
        coins = r.hkeys("watchlist")
        symbols = sorted({c.decode() for c in coins})
        active = json.loads(r.get(ACTIVE_WS_SET_KEY) or "[]")
        if symbols and symbols != active:
            r.set(ACTIVE_WS_SET_KEY, json.dumps(symbols))
            # زِيادة GEN لإجبار القديم على الإغلاق
            gen = int(r.get("ws:gen") or b"0") + 1
            r.set("ws:gen", gen)
            threading.Thread(target=start_combined_ws, args=(symbols, gen), daemon=True).start()
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

def watcher_loop():
    """يحرّك WS أول مرة عند التشغيل."""
    booted = False
    while True:
        if r.get(IS_RUNNING_KEY) != b"1": time.sleep(5); continue
        if not booted:
            coins = r.hkeys("watchlist")
            symbols = sorted({c.decode() for c in coins})
            if symbols:
                r.set(ACTIVE_WS_SET_KEY, json.dumps(symbols))
                threading.Thread(target=start_combined_ws, args=(symbols,), daemon=True).start()
                booted = True
        time.sleep(2)

# =========================
# واجهات
# =========================
@app.route("/")
def home():
    return "🔥 Sniper (Adaptive Score + Combined Binance WS) is Live", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json() or {}
    txt = (data.get("message",{}).get("text") or "").strip().lower()
    if txt == "play":
        r.set(IS_RUNNING_KEY, "1"); send_message("✅ بدأ التشغيل.")
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
            for i,s in enumerate(coin_list,1): msg += f"{i}. {s}   " + ("\n" if i%5==0 else "")
            send_message("📡 العملات المرصودة:\n"+msg.strip())
        else:
            send_message("🚫 لا توجد عملات قيد المراقبة.")
    elif txt == "reset":
        r.delete("watchlist"); r.delete(RANK_CACHE_ALL); r.delete(GLOBAL_BUDGET_KEY)
        for k in r.scan_iter("postsend:watch:*"): r.delete(k)
        r.delete(ACTIVE_WS_SET_KEY)
        send_message("🧹 مسحنا الذاكرة. ستُحدَّث القوائم بالدورة القادمة.")
    return jsonify(ok=True)

# =========================
# تشغيل
# =========================
if __name__ == "__main__":
    r.set(IS_RUNNING_KEY, "1")
    threading.Thread(target=update_symbols_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)