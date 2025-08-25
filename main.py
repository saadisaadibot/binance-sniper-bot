# -*- coding: utf-8 -*-
"""
Fast-Scalping Learner — Bitvavo EUR pairs
- يختار Top2 (5m) + Top2 (15m) كل دقيقة ← watchlist <= 4
- كل 3 ثواني يفحص "تهيؤ للقفزة" ويطلق شراء وهمي
- يراقب الصفقة 5 دقائق (قد تتمدد لـ 10 تلقائياً) بهدف +2%
- أي هبوط ≤ -2% قبل بلوغ +2% = فشل فوري
- يتعلم ويعدل العتبات بسرعة بعد كل صفقة
- أوامر تلغرام: /learn_on /learn_off /learn_status /learn_summary /clear_learn
"""

import os, time, json, math, traceback
from collections import deque, defaultdict
from threading import Thread, Lock, Event
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests
import redis

# ========= Boot =========
load_dotenv()
app = Flask(__name__)

# ========= إعدادات عامة =========
BASE_URL            = os.getenv("BITVAVO_URL", "https://api.bitvavo.com/v2")
HTTP_TIMEOUT        = float(os.getenv("HTTP_TIMEOUT", 6.0))
QUOTE               = os.getenv("QUOTE", "EUR")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r               = redis.from_url(REDIS_URL, decode_responses=True)
REDIS_TTL_SEC   = int(os.getenv("REDIS_TTL_SEC", 7200))

# سحب الأسعار العام (لتغذية Redis)
POLL_SEC            = int(os.getenv("POLL_SEC", 3))
MARKETS_REFRESH_SEC = int(os.getenv("MARKETS_REFRESH_SEC", 120))
EXPECTED_MIN        = int(os.getenv("EXPECTED_MIN", 80))
UNHEALTHY_THRESHOLD = int(os.getenv("UNHEALTHY_THRESHOLD", 6))

# ========= إعدادات التعلم (سكالب سريع) =========
LEARN_ENABLED        = os.getenv("LEARN_ENABLED", "1") == "1"
SELECT_EVERY_SEC     = 60                 # اختيار Top2/5m + Top2/15m كل دقيقة
FULL_RESET_EVERY_SEC = 15 * 60            # كل ربع ساعة إعادة اختيار كاملة
TICK_LEARN_SEC       = 3                  # فحص التهيؤ/المراقبة كل 3 ثواني

TP_PCT               = float(os.getenv("TP_PCT", "2.0"))     # هدف الربح +2%
FAIL_PCT             = float(os.getenv("FAIL_PCT", "-2.0"))  # فشل فوري -2%
VBUY_TIMEOUT_BASE    = 5 * 60            # 5 دقائق (ديناميكيًا قد يصبح 10)
VBUY_TIMEOUT_ALT     = 10 * 60           # 10 دقائق (تمديد تلقائي)

ORDERBOOK_DEPTH_LVL  = 10

# عتبات أولية قابلة للتكيّف (تُحفظ في Redis)
DEFAULT_PARAMS = {
    "r20s_thr":   0.30,   # تسارع 20s %
    "r60s_thr":   0.70,   # تسارع 60s %
    "spread_max": 0.40,   # سبريد أقصى %
    "ob_imb_min": 1.50,   # مجموع bids / asks
    "vol_z_min":  1.70    # حجم 1m/متوسط 5m
}

# ========= حالة =========
lock   = Lock()
started= Event()
learn_running = Event()
if LEARN_ENABLED:
    learn_running.set()

symbols_all = []            # bases المتاحة للتداول مقابل QUOTE
last_markets_refresh = 0

# تاريخ أسعار محلي خفيف + Redis ZSET
prices_local = defaultdict(lambda: deque(maxlen=128))
last_bulk_ts = 0
consecutive_http_fail = 0

watch_list = set()
_last_wl_reset = 0

# ========= مساعدات عامة =========
def send_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG_DISABLED] {text}"); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=HTTP_TIMEOUT
        )
    except Exception as e:
        print(f"[TG][ERR] {type(e).__name__}: {e}")

def http_get(url, params=None, timeout=HTTP_TIMEOUT):
    headers = {"User-Agent": "fast-learner/1.0"}
    delays = [0.2, 0.5, 1.0, 2.0]
    for i, d in enumerate(delays, 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers=headers)
            if resp.status_code in (429,) or resp.status_code >= 500:
                time.sleep(d); continue
            return resp
        except Exception as e:
            print(f"[HTTP][ERR] {url}: {type(e).__name__}: {e}")
            time.sleep(d)
    return None

def pct(now_p, old_p):
    try:
        return (now_p - old_p) / old_p * 100.0 if old_p else 0.0
    except Exception:
        return 0.0

# ========= Redis أسعار (ZSET لكل عملة) =========
def r_price_key(base): return f"fl:{QUOTE}:p:{base}"

def redis_store_price(base, ts, price):
    key = r_price_key(base)
    member = f"{int(ts)}:{price}"
    pipe = r.pipeline()
    pipe.zadd(key, {member: ts})
    pipe.zremrangebyscore(key, 0, ts - 3600)  # نحتفظ بساعة
    pipe.expire(key, REDIS_TTL_SEC)
    pipe.execute()

def redis_last_price(base):
    key = r_price_key(base)
    now_ts = int(time.time())
    rows = r.zrevrangebyscore(key, now_ts, 0, start=0, num=1)
    if not rows: return None
    try:
        return float(rows[0].split(":")[1])
    except Exception:
        return None

def redis_pct_change_seconds(base, seconds):
    key = r_price_key(base)
    now_ts = int(time.time())
    from_ts = now_ts - int(seconds)
    first = r.zrangebyscore(key, from_ts, now_ts, start=0, num=1)
    last  = r.zrevrangebyscore(key, now_ts, from_ts, start=0, num=1)
    cnt   = r.zcount(key, from_ts, now_ts)
    if cnt < 2 or not first or not last:
        return None
    try:
        p0 = float(first[0].split(":")[1]); p1 = float(last[0].split(":")[1])
        if p0 <= 0: return None
        return (p1 - p0)/p0*100.0
    except Exception:
        return None

# ========= Bitvavo =========
def refresh_markets(now=None):
    global symbols_all, last_markets_refresh
    now = now or time.time()
    if symbols_all and (now - last_markets_refresh) < MARKETS_REFRESH_SEC:
        return
    resp = http_get(f"{BASE_URL}/markets")
    if not resp or resp.status_code != 200:
        return
    try:
        data = resp.json()
        bases = []
        for m in data:
            if m.get("quote") == QUOTE and m.get("status") == "trading":
                base = m.get("base")
                if base and base.isalpha():
                    bases.append(base)
        with lock:
            symbols_all = bases
            last_markets_refresh = now
        print(f"[MARKETS] listed={len(bases)} for quote {QUOTE}")
    except Exception as e:
        print(f"[MARKETS][ERR] {type(e).__name__}: {e}")

def bulk_prices():
    """dict base->price"""
    resp = http_get(f"{BASE_URL}/ticker/price")
    out = {}
    if not resp or resp.status_code != 200:
        return out
    try:
        for row in resp.json():
            mk = row.get("market","")
            if mk.endswith(f"-{QUOTE}"):
                base = mk.split("-")[0]
                try:
                    out[base] = float(row["price"])
                except Exception:
                    pass
    except Exception as e:
        print(f"[BULK][ERR] {type(e).__name__}: {e}")
    return out

def get_candles_change(base, interval="5m", lookback=3):
    """تغير % بين إغلاق الآن ومتوسط إغلاقات آخر lookback شموع قبل الحالية."""
    url = f"{BASE_URL}/markets/{base}-{QUOTE}/candles"
    resp = http_get(url, params={"interval": interval})
    if not resp or resp.status_code != 200: return None
    try:
        rows = resp.json()[-(lookback+1):]
        closes = [float(x[4]) for x in rows]
        if len(closes) < lookback+1: return None
        now_c = closes[-1]; ref = sum(closes[:-1])/len(closes[:-1])
        if ref <= 0: return None
        return (now_c - ref)/ref*100.0
    except Exception:
        return None

def top2_for_interval(bases, interval):
    scored = []
    for b in bases:
        ch = get_candles_change(b, interval=interval, lookback=3)
        if ch is not None:
            scored.append((b, ch))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [b for (b, _) in scored[:2]]

def get_orderbook_and_spread(base):
    resp = http_get(f"{BASE_URL}/book", params={"market": f"{base}-{QUOTE}", "depth": ORDERBOOK_DEPTH_LVL})
    if not resp or resp.status_code != 200: return None
    try:
        data = resp.json()
        bids = data.get("bids", [])[:ORDERBOOK_DEPTH_LVL]
        asks = data.get("asks", [])[:ORDERBOOK_DEPTH_LVL]
        best_bid = float(bids[0][0]) if bids else None
        best_ask = float(asks[0][0]) if asks else None
        spread_pct = ((best_ask - best_bid)/best_bid*100.0) if (best_bid and best_ask and best_bid>0) else None
        # مجموع سيولة
        def sum_pa(rows):
            s = 0.0
            for row in rows:
                if len(row) >= 2:
                    p, q = float(row[0]), float(row[1])
                    s += p*q
            return s
        sum_bids = sum_pa(bids); sum_asks = sum_pa(asks)
        ob_imb = (sum_bids/sum_asks) if (sum_bids>0 and sum_asks>0) else None
        return {"best_bid": best_bid, "best_ask": best_ask, "spread_pct": spread_pct, "ob_imb": ob_imb}
    except Exception:
        return None

def vol_1m_vs_5m(base):
    resp = http_get(f"{BASE_URL}/markets/{base}-{QUOTE}/candles", params={"interval": "1m"})
    if not resp or resp.status_code != 200: return None
    try:
        rows = resp.json()[-6:]  # آخر 6 دقائق
        vols = [float(x[5]) for x in rows]
        if len(vols) < 2: return None
        v1 = vols[-1]; v5avg = sum(vols[:-1])/max(1, len(vols)-1)
        return (v1 / v5avg) if v5avg>0 else None
    except Exception:
        return None

# ========= سعر حالي =========
def get_last_price(base):
    p = redis_last_price(base)
    if p is not None:
        return p
    # fallback API
    mp = bulk_prices()
    return mp.get(base)

# ========= إدارة العتبات (تعلّم سريع) =========
def load_params():
    params = DEFAULT_PARAMS.copy()
    try:
        if r.exists("fl:params"):
            for k in DEFAULT_PARAMS.keys():
                v = r.hget("fl:params", k)
                if v is not None:
                    params[k] = float(v)
    except Exception:
        pass
    return params

def bump_param(k, delta, lo, hi):
    try:
        cur = float(r.hget("fl:params", k) or DEFAULT_PARAMS[k])
        new = max(lo, min(hi, cur + delta))
        r.hset("fl:params", k, new)
        return new
    except Exception:
        return None

def adapt_on_result(win: bool):
    # أرباح ⇒ رفع صرامة طفيفة (تقليل إشارات زائفة)
    step = 0.04 if win else -0.04
    bump_param("r20s_thr", step,   0.10, 0.80)
    bump_param("r60s_thr", step*1.2, 0.30, 2.00)
    bump_param("spread_max", -step*0.6, 0.12, 0.80)
    bump_param("ob_imb_min", step*0.8, 1.10, 3.50)
    bump_param("vol_z_min",  step*0.8, 1.10, 4.00)

# ========= اختيار قائمة المراقبة =========
def selector_worker():
    global _last_wl_reset
    while True:
        try:
            if not learn_running.is_set():
                time.sleep(1); continue

            refresh_markets()
            bases = list(symbols_all)
            if not bases:
                time.sleep(2); continue

            now = time.time()
            if (now - _last_wl_reset) >= FULL_RESET_EVERY_SEC:
                with lock:
                    watch_list.clear()
                _last_wl_reset = now

            top5  = top2_for_interval(bases, "5m")
            top15 = top2_for_interval(bases, "15m")

            # دمج (الأولوية لـ 15m ثم 5m) إلى حد 4
            ordered = top15 + [b for b in top5 if b not in top15]
            final = []
            for b in ordered:
                if b not in final:
                    final.append(b)
                if len(final) >= 4: break

            with lock:
                watch_list.clear()
                watch_list.update(final)

            print(f"[SELECT] watch={list(watch_list)} (5m={top5}, 15m={top15})")

        except Exception as e:
            print(f"[SELECT][ERR] {type(e).__name__}: {e}")
        time.sleep(SELECT_EVERY_SEC)

# ========= عامل سحب الأسعار العام (يغذي Redis + local) =========
def poller():
    global last_bulk_ts, consecutive_http_fail
    while True:
        try:
            refresh_markets()
            mp = bulk_prices()  # dict base->price
            now = time.time()

            if not mp:
                consecutive_http_fail += 1
                if consecutive_http_fail >= UNHEALTHY_THRESHOLD:
                    print("[HEALTH][DOWN] API unhealthy; cooling...")
                    time.sleep(min(60, POLL_SEC*5))
                time.sleep(POLL_SEC); continue

            if consecutive_http_fail >= UNHEALTHY_THRESHOLD:
                print("[HEALTH][UP] API restored")
            consecutive_http_fail = 0

            with lock:
                for base, price in mp.items():
                    if symbols_all and base not in symbols_all: continue
                    dq = prices_local[base]
                    dq.append((now, price))
                    redis_store_price(base, now, price)

            last_bulk_ts = now

        except Exception as e:
            print(f"[POLL][ERR] {type(e).__name__}: {e}")
            traceback.print_exc()
        time.sleep(POLL_SEC)

# ========= إدارة الصفقات الوهمية =========
def active_key(base): return f"fl:active:{base}"

def compute_dynamic_timeout():
    """يمدّد إلى 10 دقائق إذا اتضح أن الوصول لـ +2% يتأخر عادةً."""
    try:
        wins = []
        for raw in r.lrange("fl:trades", 0, 49):
            x = json.loads(raw)
            if x.get("win"):
                if x.get("dur_s"):
                    wins.append(x["dur_s"])
        if not wins: return VBUY_TIMEOUT_BASE
        avg = sum(wins)/len(wins)
        # لو متوسط زمن الفوز > 240s نسمح بتمديد 10 دقائق
        return VBUY_TIMEOUT_ALT if avg > 240 else VBUY_TIMEOUT_BASE
    except Exception:
        return VBUY_TIMEOUT_BASE

def launch_virtual_buy(base, entry_price, feats):
    key = active_key(base)
    if r.exists(key):  # لا نكرر على نفس العملة
        return False
    timeout_sec = compute_dynamic_timeout()
    payload = {
        "base": base,
        "entry_price": entry_price,
        "entry_ts": int(time.time()),
        "timeout_sec": timeout_sec,
        "min_pnl": 0.0,      # أدنى PnL شوهد
        "max_pnl": 0.0,      # أعلى PnL شوهد
        "feats": feats
    }
    try:
        r.hset(key, mapping={
            "base": base,
            "entry_price": entry_price,
            "entry_ts": payload["entry_ts"],
            "timeout_sec": timeout_sec,
            "min_pnl": 0.0,
            "max_pnl": 0.0,
            "feats": json.dumps(feats)
        })
        r.expire(key, timeout_sec + 900)
    except Exception as e:
        print(f"[VBUY][ERR] {type(e).__name__}: {e}")
        return False
    send_message(
        f"🤖 شراء وهمي {base} @ {entry_price:.8f} | "
        f"r20s={feats.get('r20s') and round(feats['r20s'],3)} "
        f"r60s={feats.get('r60s') and round(feats['r60s'],3)} "
        f"spr={feats.get('spread') and round(feats['spread'],3)} "
        f"imb={feats.get('ob_imb') and round(feats['ob_imb'],2)} "
        f"volZ={feats.get('vol_z') and round(feats['vol_z'],2)} "
        f"⏱ {timeout_sec//60}m"
    )
    return True

def log_and_adapt(base, entry_price, exit_price, reason, win_flag, dur_s, min_pnl, max_pnl):
    pnl_pct = (exit_price - entry_price)/entry_price*100.0 if entry_price else 0.0
    rec = {
        "t": int(time.time()), "base": base, "pnl_pct": round(pnl_pct, 3),
        "dur_s": int(dur_s) if dur_s is not None else None,
        "reason": reason, "win": bool(win_flag),
        "min_pnl": round(min_pnl,3), "max_pnl": round(max_pnl,3)
    }
    try:
        r.lpush("fl:trades", json.dumps(rec))
        r.ltrim("fl:trades", 0, 499)
        # coin stats
        hk = f"fl:coin:{base}:stats"
        if win_flag:
            r.hincrby(hk, "wins", 1)
        else:
            r.hincrby(hk, "losses", 1)
        r.hset(hk, "last_seen", int(time.time()))
        if win_flag and dur_s:
            old = r.hget(hk, "avg_time_to_2")
            new = (0.7*float(old) + 0.3*dur_s) if old else float(dur_s)
            r.hset(hk, "avg_time_to_2", new)
    except Exception as e:
        print(f"[LOG][ERR] {type(e).__name__}: {e}")

    adapt_on_result(win_flag)

    emoji = "✅" if win_flag else "❌"
    send_message(
        f"{emoji} {base} {('ربح' if win_flag else 'خسر')} {pnl_pct:+.2f}% خلال {dur_s or '?'}s "
        f"| سبب: {reason} | min={min_pnl:+.2f}% max={max_pnl:+.2f}%"
    )

    # ملخص آخر 10
    try:
        items = [json.loads(x) for x in r.lrange("fl:trades", 0, 9)]
        if items:
            wins = sum(1 for x in items if x.get("win"))
            losses = len(items) - wins
            durs = [x["dur_s"] for x in items if x.get("win") and x.get("dur_s")]
            avg_dur_win = int(sum(durs)/len(durs)) if durs else None
            send_message(f"📊 ملخص (آخر 10): {wins} ✅ / {losses} ❌"
                         + (f" | ⏱ متوسط بلوغ +{TP_PCT:.1f}% ≈ {avg_dur_win}s" if avg_dur_win else ""))
    except Exception:
        pass

def close_virtual_trade(base, exit_price, reason, win_flag):
    key = active_key(base)
    if not r.exists(key): return
    entry_price = float(r.hget(key, "entry_price") or 0)
    entry_ts    = int(r.hget(key, "entry_ts") or 0)
    min_pnl     = float(r.hget(key, "min_pnl") or 0.0)
    max_pnl     = float(r.hget(key, "max_pnl") or 0.0)
    dur_s       = int(time.time()) - entry_ts if entry_ts else None

    log_and_adapt(base, entry_price, exit_price, reason, win_flag, dur_s, min_pnl, max_pnl)
    try: r.delete(key)
    except Exception: pass

# ========= كاشف “تهيؤ للقفزة” =========
def readiness_and_maybe_launch(base):
    params = load_params()
    # تسارع قصير باستخدام Redis
    r20s = redis_pct_change_seconds(base, 20)   # ~ 20s
    r60s = redis_pct_change_seconds(base, 60)   # ~ 60s
    ob   = get_orderbook_and_spread(base) or {}
    volz = vol_1m_vs_5m(base)
    price= get_last_price(base)

    if price is None: return

    checks = []
    if r20s is not None: checks.append(r20s >= params["r20s_thr"])
    if r60s is not None: checks.append(r60s >= params["r60s_thr"])
    if ob.get("spread_pct") is not None: checks.append(ob["spread_pct"] <= params["spread_max"])
    if ob.get("ob_imb") is not None:     checks.append(ob["ob_imb"] >= params["ob_imb_min"])
    if volz is not None:                 checks.append(volz >= params["vol_z_min"])

    if checks and all(checks):
        feats = {
            "r20s": r20s, "r60s": r60s,
            "spread": ob.get("spread_pct"), "ob_imb": ob.get("ob_imb"),
            "vol_z": volz
        }
        launch_virtual_buy(base, price, feats)

# ========= عامل التعلم/المراقبة =========
def learner_worker():
    last_tick = 0
    while True:
        try:
            if not learn_running.is_set():
                time.sleep(1); continue

            now = time.time()
            if (now - last_tick) < TICK_LEARN_SEC:
                time.sleep(0.2); continue
            last_tick = now

            # محاولات إطلاق
            wl = list(watch_list)
            for b in wl:
                readiness_and_maybe_launch(b)

            # مراقبة الصفقات النشطة
            for key in r.scan_iter("fl:active:*", count=200):
                base = key.split(":")[-1]
                entry_price = float(r.hget(key, "entry_price") or 0)
                entry_ts    = int(r.hget(key, "entry_ts") or 0)
                timeout_sec = int(r.hget(key, "timeout_sec") or VBUY_TIMEOUT_BASE)
                min_pnl     = float(r.hget(key, "min_pnl") or 0.0)
                max_pnl     = float(r.hget(key, "max_pnl") or 0.0)
                if not entry_price or not entry_ts:
                    r.delete(key); continue

                price = get_last_price(base)
                if price is None: continue

                pnl = (price - entry_price)/entry_price*100.0

                # حدث الإحصاء اللحظي min/max
                new_min = min(min_pnl, pnl) if min_pnl or min_pnl == 0 else pnl
                new_max = max(max_pnl, pnl) if max_pnl or max_pnl == 0 else pnl
                r.hset(key, mapping={"min_pnl": new_min, "max_pnl": new_max})

                # قاعدة النجاح/الفشل:
                # 1) أول ما يلمس -2% → فشل فوري
                if pnl <= FAIL_PCT:
                    close_virtual_trade(base, price, f"FAIL {FAIL_PCT:.1f}% touch", win_flag=False)
                    continue
                # 2) إذا وصل +2% قبل ملامسة -2% → نجاح
                if pnl >= TP_PCT:
                    close_virtual_trade(base, price, f"TP +{TP_PCT:.1f}%", win_flag=True)
                    continue
                # 3) انتهاء الوقت → فشل (timeout)
                if (int(time.time()) - entry_ts) >= timeout_sec:
                    close_virtual_trade(base, price, "timeout", win_flag=False)
                    continue

        except Exception as e:
            print(f"[LEARN][ERR] {type(e).__name__}: {e}")
        time.sleep(0.2)

# ========= مسح مفاتيح التعلم فقط =========
def clear_learn_keys():
    total = 0
    for pat in ["fl:params", "fl:active:*", "fl:trades", "fl:coin:*", f"fl:{QUOTE}:p:*"]:
        for k in r.scan_iter(pat, count=1000):
            try: r.unlink(k); total += 1
            except Exception:
                try: r.delete(k); total += 1
                except Exception: pass
    return total

# ========= Web =========
@app.get("/")
def health():
    return f"Fast-Scalping Learner (quote={QUOTE}) ✅", 200

@app.get("/stats")
def stats():
    with lock:
        wl = list(watch_list)
    p = load_params()
    return jsonify({
        "watch_list": wl,
        "params": p,
        "last_bulk_age": (time.time()-last_bulk_ts) if last_bulk_ts else None,
        "tick_sec": TICK_LEARN_SEC,
        "tp_pct": TP_PCT, "fail_pct": FAIL_PCT
    }), 200

# ========= تلغرام Webhook =========
@app.post("/webhook")
def telegram_webhook():
    data = request.json or {}; msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return "ok", 200

    if text in {"ابدأ التعلم", "/learn_on"}:
        learn_running.set()
        send_message("🟢 تم تشغيل التعلم.")
        return "ok", 200

    if text in {"أوقف التعلم", "/learn_off"}:
        learn_running.clear()
        send_message("🛑 تم إيقاف التعلم.")
        return "ok", 200

    if text in {"الضبط تعلم", "/learn_status"}:
        p = load_params()
        with lock: wl = list(watch_list)
        lines = [
            "⚙️ Learn-Params:",
            f"- r20s_thr={p['r20s_thr']:.3f}% | r60s_thr={p['r60s_thr']:.3f}%",
            f"- spread_max={p['spread_max']:.3f}% | ob_imb_min={p['ob_imb_min']:.2f} | vol_z_min={p['vol_z_min']:.2f}",
            f"- watch_list={wl}"
        ]
        send_message("\n".join(lines))
        return "ok", 200

    if text in {"ملخص التعلم", "/learn_summary"}:
        try:
            items = [json.loads(x) for x in r.lrange("fl:trades", 0, 9)]
            if not items:
                send_message("📊 لا يوجد سجل تعلم بعد.")
                return "ok", 200
            wins = sum(1 for x in items if x.get("win"))
            losses = len(items) - wins
            lines = [f"📊 آخر {len(items)}: {wins} ✅ / {losses} ❌"]
            for it in items[:6]:
                lines.append(f"- {it['base']} {it['pnl_pct']:+.2f}% خلال {it.get('dur_s','?')}s ({it.get('reason','')})")
            send_message("\n".join(lines))
        except Exception as e:
            send_message(f"ERR: {type(e).__name__}: {e}")
        return "ok", 200

    if text in {"مسح تعلم", "/clear_learn"}:
        n = clear_learn_keys()
        send_message(f"🧹 تم مسح {n} مفتاح/مفاتيح تخص التعلم.")
        return "ok", 200

    return "ok", 200

# ========= تشغيل العمّال =========
def start_workers_once():
    if started.is_set(): return
    with lock:
        if started.is_set(): return
        Thread(target=poller,           daemon=True).start()
        Thread(target=selector_worker,  daemon=True).start()
        Thread(target=learner_worker,   daemon=True).start()
        started.set()
        print("[BOOT] workers started")

start_workers_once()
# إذا تشغّل داخل بيئة WSGI (Railway) سيُستدعى تلقائياً
# لو أردت التشغيل محلياً أزل التعليق:
# if __name__ == "__main__":
#     app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))