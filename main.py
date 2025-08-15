# -*- coding: utf-8 -*-
import os, time, math, requests
from collections import deque, defaultdict
from threading import Thread
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# =========================
# ⚙️ إعدادات أساسية (عدوانية)
# =========================
SCAN_INTERVAL_SEC     = float(os.getenv("SCAN_INTERVAL_SEC", 2.0))  # مسح كل 2s
HISTORY_SEC           = 900     # نحتفظ بـ 15 دقيقة
FOLLOWUP_WINDOW_SEC   = 300     # تقييم الإشعار بعد 5 دقائق
ALERT_COOLDOWN_SEC    = 300     # كولداون 5 دقائق لكل عملة
EXPECTED_TARGET_MIN   = 2.0     # نركز على +2% كهدف
TG_BOT_TOKEN          = os.getenv("BOT_TOKEN")
TG_CHAT_ID            = os.getenv("CHAT_ID")
BASE_URL              = "https://api.bitvavo.com/v2"

# =========================
# 🧠 الحالة
# =========================
# تخزين تسلسل الأسعار (ts, price). نستخدمه لحساب سرعات/تسارع و"ميكروفوليوم" (تقلب).
prices = defaultdict(lambda: deque())               # symbol -> deque[(ts, price)]
# إحصاء تقلب دقيق كبديل لحجم لحظي (EWMA لكل عملة)
vol_ewma = defaultdict(lambda: 0.0)                 # symbol -> ewma(abs returns)
# آخر إشعار وتفاصيله
last_alert_ts = {}                                  # symbol -> ts
predictions = {}                                     # symbol -> {"time","expected","start_price","status"}
history_results = deque(maxlen=500)                  # [(ts, symbol, status, expected, actual)]
# عتبات متكيفة (تبدأ عدوانية)
thresholds = {
    "MIN_SPEED_PM": 0.6,      # حد أدنى سرعة (% لكل دقيقة) محسوبة من r30s*2
    "MIN_ACCEL_PM": 0.15,     # حد أدنى للتسارع (%/د دقيقة-على-دقيقة)
    "VOL_RATIO_MIN": 1.10,    # ميكروفوليوم حالي / خط أساس (EWMA)
    "EXPECTED_MIN": 2.0       # أقل قفزة متوقعة للإشعار
}
# أداء آخر فترة للتعلم الذاتي
learning_window = deque(maxlen=40)  # "hit" أو "miss"

# =========================
# 📡 مساعدات
# =========================
def send_message(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[NO_TG]", text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text})
    except Exception as e:
        print("TG error:", e)

def http_get_json(url, params=None, timeout=6):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

# =========================
# 📊 حسابات لحظية
# =========================
def pct_change(a, b):
    try:
        return (a - b) / b * 100.0 if b > 0 else 0.0
    except:
        return 0.0

def micro_volatility(symbol, now):
    """
    ميكروفوليوم = متوسط المطلق لعوائد 5s داخل آخر 60s.
    نستخدمه كبديل سريع لحجم لحظي.
    """
    dq = prices[symbol]
    if len(dq) < 3:
        return 0.0
    # نجمع تغيرات 5s تقريبية
    rets = []
    # نمشي على النقاط الحديثة ونقيس فرق متتالٍ
    prev_ts, prev_p = dq[0]
    for ts, p in list(dq):
        if ts <= prev_ts: 
            continue
        dr = abs(pct_change(p, prev_p))
        # نأخذ فقط آخر 60s
        if now - ts <= 60:
            rets.append(dr)
        prev_ts, prev_p = ts, p
    if not rets:
        return 0.0
    cur = sum(rets) / len(rets)  # متوسط المطلق
    # أحدث EWMA
    alpha = 0.25
    vol_ewma[symbol] = (1 - alpha) * vol_ewma[symbol] + alpha * cur if vol_ewma[symbol] else cur
    base = vol_ewma[symbol] if vol_ewma[symbol] else cur
    # نسبة الميكروفوليوم الحالي إلى خط الأساس (نسبيًا)
    return (cur / base) if base else 1.0

def window_price(symbol, lookback_sec, now):
    """
    يرجع سعر منذ lookback_sec (أقرب نقطة أقدم من/تساوي المطلوب).
    """
    dq = prices[symbol]
    ref = None
    for ts, pr in reversed(dq):
        if now - ts >= lookback_sec:
            ref = pr
            break
    return ref

def analyze(symbol):
    """
    يحسب:
      - r30s, r60s, r5m
      - speed_pm (%/د من r30s*2)
      - accel_pm (فرق السرعة الحالية - سرعة الدقيقة السابقة)
      - vol_ratio (ميكروفوليوم نسبي)
      - expected_jump (توقع +% خلال ~2-5 دقائق)
    """
    now = time.time()
    dq = prices[symbol]
    if len(dq) < 3:
        return None

    cur = dq[-1][1]
    p30 = window_price(symbol, 30, now)
    p60 = window_price(symbol, 60, now)
    p180 = window_price(symbol, 180, now)
    p300 = window_price(symbol, 300, now)

    r30s = pct_change(cur, p30) if p30 else 0.0
    r60s = pct_change(cur, p60) if p60 else 0.0
    r3m  = pct_change(cur, p180) if p180 else 0.0
    r5m  = pct_change(cur, p300) if p300 else 0.0

    # سرعة بالحاضر: لو عندي r30s = +0.9% → سرعة تقريبية 1.8%/د
    speed_pm_cur = r30s * 2.0
    # سرعة الدقيقة السابقة: نقرّبها من r60s لأنه يلتقط الدقيقة الماضية كلها
    speed_pm_prev = r60s  # تقريبية
    accel_pm = speed_pm_cur - speed_pm_prev  # تسارع

    vol_ratio = micro_volatility(symbol, now)  # >1 يعني نشاط أعلى من المعتاد

    # تقدير قفزة خلال 2–5 د: نمزج السرعة + التسارع + النشاط
    # صيغة بسيطة عدوانية:
    # توقع ≈ speed_now * (1 + accel/100) * clamp(vol_ratio, 0.9..2.5) * horizon_factor
    horizon_factor = 1.8  # نميل لتوقع ~دقيقتين إلى ثلاث (عدواني)
    vol_clamped = max(0.9, min(vol_ratio, 2.5))
    expected = speed_pm_cur * (1.0 + accel_pm / 100.0) * vol_clamped * horizon_factor

    return {
        "symbol": symbol,
        "cur": cur,
        "r30s": r30s,
        "r60s": r60s,
        "r3m": r3m,
        "r5m": r5m,
        "speed_pm": speed_pm_cur,
        "accel_pm": accel_pm,
        "vol_ratio": vol_ratio,
        "expected": expected
    }

# =========================
# 🔁 جامع الأسعار (سريع)
# =========================
def collector():
    while True:
        now = time.time()
        markets = http_get_json(f"{BASE_URL}/markets")
        if not markets:
            time.sleep(SCAN_INTERVAL_SEC)
            continue

        # نتابع فقط الأزواج EUR شغّالة
        symbols = [m.get("base") for m in markets if m.get("quote") == "EUR" and m.get("status") == "trading"]
        for base in symbols:
            ticker = http_get_json(f"{BASE_URL}/ticker/price", {"market": f"{base}-EUR"})
            if not ticker:
                continue
            try:
                price = float(ticker["price"])
            except:
                continue

            dq = prices[base]
            dq.append((now, price))
            # قصّ التاريخ
            cutoff = now - HISTORY_SEC
            while dq and dq[0][0] < cutoff:
                dq.popleft()

        time.sleep(SCAN_INTERVAL_SEC)

# =========================
# 🤖 متنبّه الإشعارات + التتبع 5 دقائق
# =========================
def predictor():
    while True:
        now = time.time()
        # تحديث نتائج الإشعارات التي انتهت فترة المتابعة لها
        for sym, pred in list(predictions.items()):
            if pred["status"] is None and now - pred["time"] >= FOLLOWUP_WINDOW_SEC:
                dq = prices.get(sym)
                if not dq:
                    continue
                cur = dq[-1][1]
                actual = pct_change(cur, pred["start_price"])
                status = "✅ أصابت" if actual >= pred["expected"] else "❌ خابت"
                pred["status"] = status
                history_results.append((now, sym, status, pred["expected"], actual))
                learning_window.append("hit" if "✅" in status else "miss")

        # تحليل وإرسال إشعارات جديدة
        for sym in list(prices.keys()):
            res = analyze(sym)
            if not res:
                continue

            # فلتر عدواني بسيط لمنع الشخبطة:
            if res["speed_pm"] < thresholds["MIN_SPEED_PM"]:
                continue
            if res["accel_pm"] < thresholds["MIN_ACCEL_PM"]:
                continue
            if res["vol_ratio"] < thresholds["VOL_RATIO_MIN"]:
                continue
            if res["expected"] < thresholds["EXPECTED_MIN"]:
                continue

            # كولداون
            if sym in last_alert_ts and now - last_alert_ts[sym] < ALERT_COOLDOWN_SEC:
                continue

            last_alert_ts[sym] = now
            predictions[sym] = {
                "time": now,
                "expected": res["expected"],
                "start_price": res["cur"],
                "status": None
            }

            msg = (
                f"🚀 توقع قفزة: {sym}\n"
                f"الهدف: {res['expected']:+.2f}% (≈ 2–5 د)\n"
                f"سرعة: {res['speed_pm']:+.2f}%/د | تسارع: {res['accel_pm']:+.2f}% | نشاط: ×{res['vol_ratio']:.2f}\n"
                f"r30s {res['r30s']:+.2f}% | r60s {res['r60s']:+.2f}% | r5m {res['r5m']:+.2f}%"
            )
            send_message(msg)

        time.sleep(2)

# =========================
# 🧪 تعلّم ذاتي سريع (كل دقيقة)
# =========================
def self_learning():
    """
    يراقب hit/miss في آخر نافذة ويعدّل العتبات:
      - إذا النجاح < 40% ومعنا ≥8 نتائج: يشدد الشروط (أقل عدوانية).
      - إذا النجاح > 70% ومعنا ≥8 نتائج: يخفف الشروط (أكثر عدوانية).
    """
    while True:
        time.sleep(60)
        total = len(learning_window)
        if total < 8:
            continue
        hits = sum(1 for x in learning_window if x == "hit")
        rate = hits / total

        # نسخ العتبات الحالية
        ms, ma, vr, ex = (thresholds["MIN_SPEED_PM"], thresholds["MIN_ACCEL_PM"],
                          thresholds["VOL_RATIO_MIN"], thresholds["EXPECTED_MIN"])

        if rate < 0.40:
            # شدّد قليلًا
            ms = min(ms + 0.10, 1.50)
            ma = min(ma + 0.05, 0.60)
            vr = min(vr + 0.10, 2.0)
            ex = min(ex + 0.2, 3.0)   # يمكن يرفع الهدف شوي لتقليل الإشارات
        elif rate > 0.70:
            # كن أكثر عدوانية
            ms = max(ms - 0.10, 0.30)
            ma = max(ma - 0.05, 0.05)
            vr = max(vr - 0.10, 1.0)
            ex = max(ex - 0.2, 1.6)   # لا ننزل أقل من 1.6% كي نبقى ضمن سياق 2%
        else:
            continue  # نطاق مقبول، لا تغيّر

        thresholds["MIN_SPEED_PM"] = round(ms, 2)
        thresholds["MIN_ACCEL_PM"] = round(ma, 2)
        thresholds["VOL_RATIO_MIN"] = round(vr, 2)
        thresholds["EXPECTED_MIN"]  = round(ex, 2)

# =========================
# 🌐 أوامر تيليجرام: الملخص/الضبط
# =========================
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    msg = data.get("message") or {}
    text = (msg.get("text") or "").strip().lower()

    if not text:
        return "ok", 200

    if text in {"الملخص", "/summary"}:
        if not history_results:
            send_message("📊 لا توجد نتائج بعد.")
        else:
            total = len(history_results)
            hits  = sum(1 for _,_,s,_,_ in history_results if "✅" in s)
            misses= total - hits
            rate  = (hits / total) * 100
            lines = [
                f"📊 الملخص (آخر {total} إشارة):",
                f"أصابت: {hits} | خابت: {misses} | نجاح: {rate:.1f}%",
                "",
                "آخر 12 نتيجة:"
            ]
            for ts, sym, status, exp, act in list(history_results)[-12:]:
                lines.append(f"{sym}: {status} | متوقعة {exp:+.2f}% | فعلية {act:+.2f}%")
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
            f"FOLLOWUP_WIN   = {FOLLOWUP_WINDOW_SEC//60}m",
        ]
        send_message("\n".join(lines))
        return "ok", 200

    return "ok", 200

# =========================
# 🌐 صحة
# =========================
@app.route("/", methods=["GET"])
def health():
    return "Aggressive Predictor is alive ✅", 200

# =========================
# 🚀 التشغيل
# =========================
def start_all():
    Thread(target=collector,    daemon=True).start()
    Thread(target=predictor,    daemon=True).start()
    Thread(target=self_learning,daemon=True).start()

if __name__ == "__main__":
    start_all()
    # للتشغيل على Gunicorn: web: gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))