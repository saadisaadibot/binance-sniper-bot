# -*- coding: utf-8 -*-
import os, time, requests, traceback
from collections import deque, defaultdict
from threading import Thread
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ إعدادات
# =========================
BITVAVO_URL           = os.getenv("BITVAVO_URL", "https://api.bitvavo.com/v2")
HTTP_TIMEOUT          = float(os.getenv("HTTP_TIMEOUT", 6.0))
SCAN_INTERVAL_SEC     = float(os.getenv("SCAN_INTERVAL_SEC", 2.0))
MARKETS_REFRESH_SEC   = int(os.getenv("MARKETS_REFRESH_SEC", 60))
HISTORY_SEC           = int(os.getenv("HISTORY_SEC", 900))
FOLLOWUP_WINDOW_SEC   = int(os.getenv("FOLLOWUP_WINDOW_SEC", 300))
ALERT_COOLDOWN_SEC    = int(os.getenv("ALERT_COOLDOWN_SEC", 300))
PREDICT_LOOP_SLEEP    = float(os.getenv("PREDICT_LOOP_SLEEP", 2.0))
WARMUP_SEC            = int(os.getenv("WARMUP_SEC", 180))
DEBUG_LOG             = os.getenv("DEBUG_LOG", "0") == "1"
STATS_EVERY_SEC       = int(os.getenv("STATS_EVERY_SEC", 60))

TG_BOT_TOKEN          = os.getenv("BOT_TOKEN")
TG_CHAT_ID            = os.getenv("CHAT_ID")

def _env_float(name, default):
    try:
        v = os.getenv(name)
        return float(v) if v is not None else default
    except:
        return default

thresholds = {
    "MIN_SPEED_PM": _env_float("MIN_SPEED_PM", 0.40),
    "MIN_ACCEL_PM": _env_float("MIN_ACCEL_PM", 0.08),
    "VOL_RATIO_MIN": _env_float("VOL_RATIO_MIN", 1.00),
    "EXPECTED_MIN":  _env_float("EXPECTED_MIN", 1.80),
}

# =========================
# 🧠 الحالة
# =========================
prices          = defaultdict(lambda: deque())   # base -> deque[(ts, price)]
last_alert_ts   = {}
predictions     = {}
history_results = deque(maxlen=500)
learning_window = deque(maxlen=40)
_symbols_cache  = []
_last_markets   = 0

_started = False   # ← تشغيل مؤجل للخيوط

# =========================
# 🛠️ أدوات
# =========================
def send_message(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[NO_TG]", text); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=HTTP_TIMEOUT
        )
    except Exception as e:
        print("TG error:", e)

def http_get_json(url, params=None, timeout=HTTP_TIMEOUT):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def pct_change(a, b):
    try:
        return (a - b) / b * 100.0 if (b and b > 0) else 0.0
    except Exception:
        return 0.0

def window_price(base, lookback_sec, now):
    dq = prices[base]
    ref = None
    for ts, pr in reversed(dq):
        if now - ts >= lookback_sec:
            ref = pr; break
    return ref

def micro_volatility(base, now):
    dq = prices[base]
    if len(dq) < 5: return 1.0
    def avg_abs_ret(window_sec):
        rets, prev = [], None
        for ts, p in dq:
            if now - ts <= window_sec:
                if prev is not None: rets.append(abs(pct_change(p, prev)))
                prev = p
        return (sum(rets)/len(rets)) if rets else 0.0
    short = avg_abs_ret(30); long = avg_abs_ret(300)
    if long <= 0: return 1.0
    ratio = short/long
    return max(0.7, min(ratio, 3.0))

def analyze(base):
    now = time.time()
    dq = prices[base]
    if len(dq) < 5: return None
    cur  = dq[-1][1]
    p30  = window_price(base, 30,  now)
    p60  = window_price(base, 60,  now)
    p180 = window_price(base, 180, now)
    p300 = window_price(base, 300, now)
    if not (p30 and p60): return None

    r30s = pct_change(cur, p30)
    r60s = pct_change(cur, p60)
    r3m  = pct_change(cur, p180) if p180 else 0.0
    r5m  = pct_change(cur, p300) if p300 else 0.0

    speed_pm_cur  = r30s * 2.0
    speed_pm_prev = r60s
    accel_pm      = speed_pm_cur - speed_pm_prev
    vol_ratio     = micro_volatility(base, now)

    horizon_factor = 1.8
    vol_clamped    = max(0.9, min(vol_ratio, 2.5))
    expected       = speed_pm_cur * (1.0 + accel_pm/100.0) * vol_clamped * horizon_factor

    res = {"symbol": base, "cur": cur, "r30s": r30s, "r60s": r60s,
           "r3m": r3m, "r5m": r5m, "speed_pm": speed_pm_cur,
           "accel_pm": accel_pm, "vol_ratio": vol_ratio, "expected": expected}
    if DEBUG_LOG: print("[ANALYZE]", res)
    return res

# =========================
# 📡 مصادر الأسعار
# =========================
def refresh_markets(now):
    global _symbols_cache, _last_markets
    if (now - _last_markets) < MARKETS_REFRESH_SEC and _symbols_cache:
        return
    mk = http_get_json(f"{BITVAVO_URL}/markets")
    if not mk: return
    _symbols_cache = [m.get("base") for m in mk
                      if m.get("quote") == "EUR" and m.get("status") == "trading"]
    _last_markets = now
    if DEBUG_LOG: print(f"[MARKETS] tracking {len(_symbols_cache)} EUR markets")

def bulk_prices():
    data = http_get_json(f"{BITVAVO_URL}/ticker/price")
    out = {}
    if isinstance(data, list):
        for row in data:
            try:
                mk = row.get("market") or ""
                if mk.endswith("-EUR"):
                    base = mk.split("-")[0]
                    out[base] = float(row["price"])
            except Exception:
                continue
    return out

# =========================
# 🔁 جامع الأسعار
# =========================
def collector():
    last_stats, misses = 0, 0
    while True:
        try:
            now = time.time()
            refresh_markets(now)
            symbols = list(_symbols_cache)

            price_map = bulk_prices()
            used_bulk = bool(price_map)

            if not used_bulk and symbols:
                for base in symbols:
                    row = http_get_json(f"{BITVAVO_URL}/ticker/price", {"market": f"{base}-EUR"})
                    if not row: continue
                    try: price_map[base] = float(row["price"])
                    except Exception: continue

            for base, price in price_map.items():
                dq = prices[base]
                dq.append((now, price))
                cutoff = now - HISTORY_SEC
                while dq and dq[0][0] < cutoff: dq.popleft()

            if DEBUG_LOG and (now - last_stats) >= STATS_EVERY_SEC:
                last_stats = now
                filled = sum(1 for _, v in prices.items() if v)
                print(f"[COLLECT] {'bulk' if used_bulk else 'single'} | sym={len(symbols)} | with_data={filled} | misses={misses}")
                misses = 0

        except Exception as e:
            misses += 1
            if DEBUG_LOG:
                print("[COLLECT][ERR]", type(e).__name__, str(e))
                traceback.print_exc()

        time.sleep(SCAN_INTERVAL_SEC)

# =========================
# 🤖 متنبّه + متابعة
# =========================
def predictor():
    while True:
        try:
            now = time.time()
            # إغلاق المتابعات المنتهية
            for base, pred in list(predictions.items()):
                if pred["status"] is None and now - pred["time"] >= FOLLOWUP_WINDOW_SEC:
                    dq = prices.get(base)
                    if dq:
                        cur = dq[-1][1]
                        actual = pct_change(cur, pred["start_price"])
                        status = "✅ أصابت" if actual >= pred["expected"] else "❌ خابت"
                        pred["status"] = status
                        history_results.append((now, base, status, pred["expected"], actual))
                        learning_window.append("hit" if "✅" in status else "miss")

            # تحليلات جديدة
            for base, dq in list(prices.items()):
                if not dq or (dq[-1][0] - dq[0][0]) < WARMUP_SEC: continue
                res = analyze(base)
                if not res: continue

                if res["speed_pm"] < thresholds["MIN_SPEED_PM"]:   continue
                if res["accel_pm"] < thresholds["MIN_ACCEL_PM"]:   continue
                if res["vol_ratio"] < thresholds["VOL_RATIO_MIN"]: continue
                if res["expected"]  < thresholds["EXPECTED_MIN"]:  continue

                if base in last_alert_ts and now - last_alert_ts[base] < ALERT_COOLDOWN_SEC:
                    continue

                last_alert_ts[base] = now
                predictions[base] = {"time": now, "expected": res["expected"],
                                     "start_price": res["cur"], "status": None}

                msg = (
                    f"🚀 توقع قفزة: {base}\n"
                    f"الهدف: {res['expected']:+.2f}% (≈ 2–5 د)\n"
                    f"سرعة: {res['speed_pm']:+.2f}%/د | تسارع: {res['accel_pm']:+.2f}% | نشاط: ×{res['vol_ratio']:.2f}\n"
                    f"r30s {res['r30s']:+.2f}% | r60s {res['r60s']:+.2f}% | r5م {res['r5m']:+.2f}%"
                )
                send_message(msg)

        except Exception as e:
            if DEBUG_LOG:
                print("[PREDICT][ERR]", type(e).__name__, str(e))
                traceback.print_exc()

        time.sleep(PREDICT_LOOP_SLEEP)

# =========================
# 🧪 تعلّم ذاتي
# =========================
def self_learning():
    while True:
        try:
            time.sleep(60)
            total = len(learning_window)
            if total < 8: continue
            hits = sum(1 for x in learning_window if x == "hit")
            rate = hits / total

            ms = thresholds["MIN_SPEED_PM"]; ma = thresholds["MIN_ACCEL_PM"]
            vr = thresholds["VOL_RATIO_MIN"]; ex = thresholds["EXPECTED_MIN"]

            if rate < 0.40:
                ms = min(ms + 0.10, 1.50); ma = min(ma + 0.05, 0.60)
                vr = min(vr + 0.10, 2.0);  ex = min(ex + 0.20, 3.0)
            elif rate > 0.70:
                ms = max(ms - 0.10, 0.30); ma = max(ma - 0.05, 0.05)
                vr = max(vr - 0.10, 1.0);  ex = max(ex - 0.20, 1.60)
            else:
                continue

            thresholds["MIN_SPEED_PM"] = round(ms, 2)
            thresholds["MIN_ACCEL_PM"] = round(ma, 2)
            thresholds["VOL_RATIO_MIN"] = round(vr, 2)
            thresholds["EXPECTED_MIN"]  = round(ex, 2)
            if DEBUG_LOG: print("[ADAPT]", thresholds)
        except Exception as e:
            if DEBUG_LOG:
                print("[ADAPT][ERR]", type(e).__name__, str(e))
                traceback.print_exc()

# =========================
# 🏁 تشغيل مؤجّل للخيوط (Flask 3 compatible)
# =========================
def start_all():
    global _started
    if _started: return
    _started = True
    Thread(target=collector,    daemon=True).start()
    Thread(target=predictor,    daemon=True).start()
    Thread(target=self_learning,daemon=True).start()
    if DEBUG_LOG: print("[BOOT] threads started")

@app.before_request
def _ensure_threads():
    if not _started:
        try:
            start_all()
        except Exception as e:
            print("[BOOT][ERR]", type(e).__name__, str(e))

# =========================
# 🌐 Webhook & Health
# =========================
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()
    if not text: return "ok", 200

    if text in {"الملخص", "/summary"}:
        if not history_results:
            send_message("📊 لا توجد نتائج بعد.")
        else:
            total = len(history_results)
            hits  = sum(1 for *_, s, _, _ in history_results if "✅" in s)
            misses= total - hits
            rate  = (hits / total) * 100
            lines = [f"📊 الملخص (آخر {total} إشارة):",
                     f"أصابت: {hits} | خابت: {misses} | نجاح: {rate:.1f}%", "", "آخر 12 نتيجة:"]
            for ts, base, status, exp, act in list(history_results)[-12:]:
                lines.append(f"{base}: {status} | متوقعة {exp:+.2f}% | فعلية {act:+.2f}%")
            send_message("\n".join(lines))
        return "ok", 200

    if text in {"الضبط", "/status"}:
        t = thresholds
        lines = [
            "⚙️ الضبط الحالي (مُتكيّف):",
            f"MIN_SPEED_PM   = {t['MIN_SPEED_PM']:.2f} %/د",
            f"MIN_ACCEL_PM   = {t['MIN_ACCEL_PM']:.2f} %",
            f"VOL_RATIO_MIN  = ×{t['VOL_RATIO_MIN']:.2f}",
            f"EXPECTED_MIN   = {t['EXPECTED_MIN']:.2f} %",
            f"COOLDOWN       = {ALERT_COOLDOWN_SEC}s",
            f"WARMUP         = {WARMUP_SEC}s",
            f"FOLLOWUP_WIN   = {FOLLOWUP_WINDOW_SEC//60}m",
        ]
        send_message("\n".join(lines))
        return "ok", 200

    return "ok", 200

@app.route("/", methods=["GET"])
def health():
    return "Aggressive Predictor is alive ✅", 200

@app.route("/statusz", methods=["GET"])
def statusz():
    now = time.time()
    with_data  = sum(1 for _, v in prices.items() if v)
    open_preds = sum(1 for v in predictions.values() if v.get("status") is None)
    return jsonify({
        "markets_tracked": len(_symbols_cache),
        "symbols_with_data": with_data,
        "open_predictions": open_preds,
        "thresholds": thresholds,
        "cooldown_sec": ALERT_COOLDOWN_SEC,
        "warmup_sec": WARMUP_SEC,
        "scan_interval_sec": SCAN_INTERVAL_SEC,
        "predict_loop_sleep": PREDICT_LOOP_SLEEP,
        "last_markets_refresh_age": int(now - _last_markets) if _last_markets else None
    }), 200

# =========================
# 🖥️ تشغيل محلي فقط
# =========================
if __name__ == "__main__":
    start_all()  # محليًا
    app.run(host="0.0.0.0", port=int(os.getenv("PORT, 8080").split(",")[0]))