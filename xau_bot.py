import os
import time
import requests
import threading
from datetime import datetime, timedelta

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TWELVE_KEY = os.environ.get("TWELVE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
SYMBOL = "XAU/USD"
API_URL = "https://api.telegram.org/bot" + TG_TOKEN

ALERT_THRESHOLD = 80
SCAN_INTERVAL = 300

subscribers = set()
last_alert_sig = {}


# ── TELEGRAM ──────────────────────────────────────────────────────

def send(chat_id, text):
    try:
        requests.post(API_URL + "/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print("Send error: " + str(e))


def typing(chat_id):
    try:
        requests.post(API_URL + "/sendChatAction", json={
            "chat_id": chat_id,
            "action": "typing"
        }, timeout=5)
    except:
        pass


# ── TWELVE DATA ───────────────────────────────────────────────────

def get_quote():
    r = requests.get(
        "https://api.twelvedata.com/quote",
        params={"symbol": SYMBOL, "apikey": TWELVE_KEY},
        timeout=10
    )
    d = r.json()
    if d.get("status") == "error":
        raise Exception(d.get("message", "API error"))
    return {
        "price": float(d["close"]),
        "high": float(d["high"]),
        "low": float(d["low"]),
        "change": float(d["change"]),
        "pct": float(d["percent_change"])
    }


def get_history(interval="1h", bars=200):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": SYMBOL, "interval": interval, "outputsize": bars, "apikey": TWELVE_KEY},
        timeout=15
    )
    d = r.json()
    if d.get("status") == "error":
        raise Exception(d.get("message", "API error"))
    data = list(reversed(d.get("values", [])))
    closes = [float(b["close"]) for b in data]
    highs = [float(b["high"]) for b in data]
    lows = [float(b["low"]) for b in data]
    return closes, highs, lows


def get_dxy():
    try:
        r = requests.get(
            "https://api.twelvedata.com/quote",
            params={"symbol": "DX/Y", "apikey": TWELVE_KEY},
            timeout=10
        )
        d = r.json()
        if d.get("status") != "error":
            return float(d.get("close", 0))
    except:
        pass
    return None


# ── CALENDRIER ECONOMIQUE ─────────────────────────────────────────

def get_economic_events():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://api.twelvedata.com/economic_calendar",
            params={
                "start_date": today,
                "end_date": tomorrow,
                "importance": "high",
                "apikey": TWELVE_KEY
            },
            timeout=10
        )
        d = r.json()
        events = d.get("result", {}).get("list", [])
        gold_keywords = ["fed", "fomc", "cpi", "inflation", "ppi", "gdp", "nfp", "jobs", "unemployment", "rate", "dollar", "usd", "gold", "powell"]
        important = []
        for ev in events[:10]:
            name = ev.get("name", "").lower()
            country = ev.get("country", "").upper()
            if country == "US" or any(k in name for k in gold_keywords):
                important.append({
                    "name": ev.get("name", ""),
                    "time": ev.get("time", ""),
                    "country": country,
                    "importance": ev.get("importance", "")
                })
        return important[:5]
    except Exception as e:
        print("Calendar error: " + str(e))
        return []


# ── MATH ──────────────────────────────────────────────────────────

def last_n(a, n):
    return a[-min(len(a), n):]


def ema(a, n):
    if not a:
        return 0
    k = 2.0 / (n + 1)
    e = a[0]
    for v in a[1:]:
        e = v * k + e * (1 - k)
    return round(e, 2)


def calc_rsi(a, n=14):
    if len(a) < n + 1:
        return 50
    ch = [a[i+1] - a[i] for i in range(len(a)-1)]
    rc = ch[-n:]
    g = sum(x for x in rc if x > 0) / n
    l = abs(sum(x for x in rc if x < 0)) / n
    if l == 0:
        return 100
    return round(100 - (100 / (1 + g / l)), 2)


def calc_macd(a):
    if len(a) < 26:
        return {"macd": 0, "signal": 0, "hist": 0}
    m = round(ema(a, 12) - ema(a, 26), 3)
    sig = round(m * 0.85, 3)
    return {"macd": m, "signal": sig, "hist": round(m - sig, 3)}


def calc_stoch(cl, hi, lo, n=14):
    sh = last_n(hi, n)
    sl = last_n(lo, n)
    h = max(sh)
    l = min(sl)
    cur = cl[-1]
    k = 50 if h == l else round((cur - l) / (h - l) * 100, 2)
    return {"k": k, "d": round(k * 0.88 + 6, 2)}


def calc_bb(a, n=20):
    sl = last_n(a, n)
    m = sum(sl) / len(sl)
    sd = (sum((v - m) ** 2 for v in sl) / len(sl)) ** 0.5
    return {
        "upper": round(m + 2 * sd, 2),
        "mid": round(m, 2),
        "lower": round(m - 2 * sd, 2)
    }


def calc_atr(cl, hi, lo, n=14):
    if len(cl) < 2:
        return 10
    trs = [max(hi[i+1] - lo[i+1], abs(hi[i+1] - cl[i]), abs(lo[i+1] - cl[i])) for i in range(len(cl)-1)]
    return round(sum(last_n(trs, n)) / min(n, len(trs)), 2)


def calc_adx(cl, n=14):
    if len(cl) < n + 1:
        return {"adx": 20, "diP": 15, "diN": 15}
    diffs = [abs(cl[i+1] - cl[i]) for i in range(len(cl)-1)]
    avg_d = sum(last_n(diffs, n)) / n
    rng = max(last_n(cl, n)) - min(last_n(cl, n))
    adxv = min(75, max(10, (avg_d / (rng / n)) * 22))
    trend = cl[-1] - cl[-1-n]
    dip = min(50, adxv * 0.9) if trend > 0 else min(25, adxv * 0.4)
    din = min(50, adxv * 0.9) if trend < 0 else min(25, adxv * 0.4)
    return {"adx": round(adxv, 1), "diP": round(dip, 1), "diN": round(din, 1)}


def calc_cci(a, n=20):
    sl = last_n(a, n)
    m = sum(sl) / len(sl)
    md = sum(abs(v - m) for v in sl) / len(sl)
    return 0 if md == 0 else round((a[-1] - m) / (0.015 * md), 1)


def calc_wr(cl, hi, lo, n=14):
    h = max(last_n(hi, n))
    l = min(last_n(lo, n))
    return -50 if h == l else round(((h - cl[-1]) / (h - l)) * -100, 1)


def calc_psar(cl, hi, lo):
    if len(cl) < 10:
        return cl[-1]
    up = cl[-1] > cl[-5]
    return round(min(last_n(lo, 5)), 2) if up else round(max(last_n(hi, 5)), 2)


def calc_supres(cl):
    sl = sorted(last_n(cl, 60))
    return {
        "sup": round(sl[int(len(sl) * 0.1)], 2),
        "res": round(sl[int(len(sl) * 0.9)], 2)
    }


# ── INDICATEURS PAR TIMEFRAME ─────────────────────────────────────

def compute_indicators(closes, highs, lows):
    price = closes[-1]
    return {
        "price": price,
        "rsi": calc_rsi(closes),
        "macd": calc_macd(closes),
        "e20": ema(last_n(closes, 20), 20),
        "e50": ema(last_n(closes, 50), 50),
        "e200": ema(last_n(closes, 200), 200),
        "adx": calc_adx(closes),
        "bb": calc_bb(closes),
        "stoch": calc_stoch(closes, highs, lows),
        "cci": calc_cci(closes),
        "wr": calc_wr(closes, highs, lows),
        "atr": calc_atr(closes, highs, lows),
        "psar": calc_psar(closes, highs, lows),
        "res": calc_supres(closes),
    }


# ── SIGNAL ENGINE ─────────────────────────────────────────────────

def build_signal(price, ind):
    S = []

    def p(name, d, w, label):
        S.append({"name": name, "dir": d, "w": w, "label": label})

    rv = ind["rsi"]
    if rv < 28:
        p("RSI", "bull", 3, "RSI survente extreme")
    elif rv < 40:
        p("RSI", "bull", 2, "RSI survente")
    elif rv > 72:
        p("RSI", "bear", 3, "RSI surachat extreme")
    elif rv > 60:
        p("RSI", "bear", 2, "RSI surachat")
    else:
        p("RSI", "neut", 1, "RSI neutre")

    mh = ind["macd"]["hist"]
    mm = ind["macd"]["macd"]
    if mh > 0 and mm > 0:
        p("MACD", "bull", 2, "MACD haussier")
    elif mh > 0:
        p("MACD", "bull", 1, "MACD croise hausse")
    elif mh < 0 and mm < 0:
        p("MACD", "bear", 2, "MACD baissier")
    else:
        p("MACD", "bear", 1, "MACD croise baisse")

    e20, e50, e200 = ind["e20"], ind["e50"], ind["e200"]
    if e20 > e50 and e50 > e200:
        p("EMA", "bull", 3, "EMA 20>50>200 tendance haussiere")
    elif e20 > e50:
        p("EMA", "bull", 2, "EMA 20>50 haussier")
    elif e20 < e50 and e50 < e200:
        p("EMA", "bear", 3, "EMA 20<50<200 tendance baissiere")
    else:
        p("EMA", "bear", 2, "EMA 20<50 baissier")

    adxv = ind["adx"]["adx"]
    dip = ind["adx"]["diP"]
    din = ind["adx"]["diN"]
    if adxv > 30 and dip > din:
        p("ADX", "bull", 2, "Tendance haussiere forte ADX>30")
    elif adxv > 30:
        p("ADX", "bear", 2, "Tendance baissiere forte ADX>30")
    else:
        p("ADX", "neut", 1, "Pas de tendance forte")

    bbu = ind["bb"]["upper"]
    bbl = ind["bb"]["lower"]
    bbm = ind["bb"]["mid"]
    if price < bbl:
        p("BB", "bull", 2, "Prix sous bande basse BB")
    elif price > bbu:
        p("BB", "bear", 2, "Prix sur bande haute BB")
    elif price < bbm:
        p("BB", "bull", 1, "Prix sous moyenne BB")
    else:
        p("BB", "bear", 1, "Prix sur moyenne BB")

    sk = ind["stoch"]["k"]
    sd = ind["stoch"]["d"]
    if sk < 20 and sd < 20:
        p("STOCH", "bull", 2, "Stochastique survente")
    elif sk > 80 and sd > 80:
        p("STOCH", "bear", 2, "Stochastique surachat")
    elif sk > sd:
        p("STOCH", "bull", 1, "Stoch K>D haussier")
    else:
        p("STOCH", "neut", 1, "Stochastique neutre")

    cc = ind["cci"]
    if cc < -100:
        p("CCI", "bull", 2, "CCI survente")
    elif cc > 100:
        p("CCI", "bear", 2, "CCI surachat")
    else:
        p("CCI", "neut", 1, "CCI neutre")

    wrv = ind["wr"]
    if wrv < -80:
        p("WR", "bull", 2, "Williams survente")
    elif wrv > -20:
        p("WR", "bear", 2, "Williams surachat")
    else:
        p("WR", "neut", 1, "Williams neutre")

    rng = ind["res"]["res"] - ind["res"]["sup"]
    pos = (price - ind["res"]["sup"]) / (rng or 1)
    if pos < 0.15:
        p("SR", "bull", 2, "Proche support " + str(ind["res"]["sup"]))
    elif pos > 0.85:
        p("SR", "bear", 2, "Proche resistance " + str(ind["res"]["res"]))
    else:
        p("SR", "neut", 1, "Zone mediane support/resistance")

    if price > ind["psar"]:
        p("SAR", "bull", 1, "Prix au-dessus du SAR")
    else:
        p("SAR", "bear", 1, "Prix en-dessous du SAR")

    bW = sum(s["w"] for s in S if s["dir"] == "bull")
    rW = sum(s["w"] for s in S if s["dir"] == "bear")
    tot = bW + rW or 1
    ratio = bW / tot

    if ratio >= 0.62:
        sig, conf = "BUY", round(ratio * 100)
    elif ratio <= 0.38:
        sig, conf = "SELL", round((1 - ratio) * 100)
    else:
        sig, conf = "NEUTRE", round(max(ratio, 1 - ratio) * 100)

    a = ind["atr"]
    tp = sl = rr = None
    entry = price

    if sig == "BUY":
        tp = round(price + a * 2.5, 2)
        sl = round(price - a * 1.2, 2)
    if sig == "SELL":
        tp = round(price - a * 2.5, 2)
        sl = round(price + a * 1.2, 2)

    if tp and sl:
        g = abs(tp - price)
        r = abs(sl - price)
        rr = round(g / r, 2) if r > 0 else None

    dir_filter = "bull" if sig == "BUY" else "bear"
    reasons = [s["label"] for s in S if s["dir"] == dir_filter][:5]

    return {
        "sig": sig, "conf": conf, "entry": entry,
        "tp": tp, "sl": sl, "rr": rr,
        "bW": bW, "rW": rW, "reasons": reasons
    }


# ── MULTI TIMEFRAME ───────────────────────────────────────────────

def multi_timeframe_analysis():
    results = {}
    timeframes = [("1h", "H1"), ("4h", "H4"), ("1day", "D1")]
    for interval, label in timeframes:
        try:
            closes, highs, lows = get_history(interval, 200)
            ind = compute_indicators(closes, highs, lows)
            res = build_signal(ind["price"], ind)
            results[label] = {"signal": res["sig"], "conf": res["conf"], "ind": ind, "result": res}
        except Exception as e:
            print("MTF error " + label + ": " + str(e))
            results[label] = {"signal": "ERREUR", "conf": 0, "ind": None, "result": None}
    return results


def mtf_confluence(mtf):
    signals = [mtf[tf]["signal"] for tf in mtf if mtf[tf]["signal"] not in ["NEUTRE", "ERREUR"]]
    if not signals:
        return "NEUTRE", 0
    buys = signals.count("BUY")
    sells = signals.count("SELL")
    total = len(signals)
    if buys > sells:
        conf = round((buys / total) * 100)
        return "BUY", conf
    elif sells > buys:
        conf = round((sells / total) * 100)
        return "SELL", conf
    return "NEUTRE", 50


# ── ANALYSE COMPLETE ──────────────────────────────────────────────

def run_full_analysis():
    quote = get_quote()
    price = quote["price"]
    mtf = multi_timeframe_analysis()
    h1 = mtf.get("H1", {})
    ind = h1.get("ind") or {}
    result = h1.get("result") or {}
    confluence_sig, confluence_conf = mtf_confluence(mtf)

    if result and result["sig"] == confluence_sig:
        result["conf"] = min(99, round((result["conf"] + confluence_conf) / 2 + 5))

    events = get_economic_events()
    dxy = get_dxy()

    return price, quote, result, ind, mtf, events, dxy


# ── CLAUDE AI ─────────────────────────────────────────────────────

def get_ai_analysis(price, result, ind, quote, mtf, events, dxy):
    if not ANTHROPIC_KEY:
        return "Cle Anthropic manquante."

    today = datetime.now().strftime("%d/%m/%Y %H:%M")

    h1_sig = mtf.get("H1", {}).get("signal", "N/A")
    h4_sig = mtf.get("H4", {}).get("signal", "N/A")
    d1_sig = mtf.get("D1", {}).get("signal", "N/A")
    h1_conf = mtf.get("H1", {}).get("conf", 0)
    h4_conf = mtf.get("H4", {}).get("conf", 0)
    d1_conf = mtf.get("D1", {}).get("conf", 0)

    events_str = ""
    if events:
        for ev in events:
            events_str += ev.get("name", "") + " (" + ev.get("time", "") + ") | "
    else:
        events_str = "Aucun evenement majeur identifie"

    dxy_str = str(round(dxy, 2)) if dxy else "indisponible"

    prompt = (
        "Tu es un expert trader XAU/USD et analyste macro. Nous sommes le " + today + ".\n\n"
        "ANALYSE MULTI-TIMEFRAME:\n"
        "H1: " + h1_sig + " (" + str(h1_conf) + "%)\n"
        "H4: " + h4_sig + " (" + str(h4_conf) + "%)\n"
        "D1: " + d1_sig + " (" + str(d1_conf) + "%)\n\n"
        "DONNEES H1 EN TEMPS REEL:\n"
        "Prix: " + str(round(price, 2)) + " USD/oz\n"
        "Variation: " + str(round(quote["change"], 2)) + " (" + str(round(quote["pct"], 2)) + "%)\n"
        "Signal consolide: " + result.get("sig", "N/A") + " avec " + str(result.get("conf", 0)) + "% de fiabilite\n"
        "Entree: " + str(result.get("entry", "N/A")) + "\n"
        "TP: " + str(result.get("tp", "N/A")) + " | SL: " + str(result.get("sl", "N/A")) + " | RR: 1:" + str(result.get("rr", "N/A")) + "\n"
        "RSI: " + str(ind.get("rsi", "N/A")) + " | MACD hist: " + str(ind.get("macd", {}).get("hist", "N/A")) + "\n"
        "ADX: " + str(ind.get("adx", {}).get("adx", "N/A")) + "\n"
        "DXY Dollar Index: " + dxy_str + "\n\n"
        "EVENEMENTS ECONOMIQUES AUJOURD HUI ET DEMAIN:\n"
        "" + events_str + "\n\n"
        "Redige une analyse complete en 4 sections:\n\n"
        "1. MULTI-TIMEFRAME: Les 3 timeframes sont-ils alignes ? Que dit chacun ?\n"
        "2. CONTEXTE MACRO: Dollar (DXY " + dxy_str + "), Fed, inflation, geopolitique - impact sur l or\n"
        "3. NEWS A SURVEILLER: Commente les evenements economiques du calendrier ci-dessus\n"
        "4. CONSEIL FINAL: Valides-tu ce trade ? Entree precise, SL, TP, niveau de risque\n\n"
        "Sois direct et concis. En francais simple pour un trader debutant."
    )

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1200,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=30
    )
    d = r.json()
    return "".join(b.get("text", "") for b in d.get("content", []))


# ── RAPPORT QUOTIDIEN ─────────────────────────────────────────────

def get_daily_report_ai(price, quote, mtf, events, dxy):
    if not ANTHROPIC_KEY:
        return "Cle Anthropic manquante."

    today = datetime.now().strftime("%d/%m/%Y")
    h1_sig = mtf.get("H1", {}).get("signal", "N/A")
    h4_sig = mtf.get("H4", {}).get("signal", "N/A")
    d1_sig = mtf.get("D1", {}).get("signal", "N/A")

    events_str = ""
    if events:
        for ev in events:
            events_str += "- " + ev.get("name", "") + " a " + ev.get("time", "") + "\n"
    else:
        events_str = "Aucun evenement majeur aujourd hui\n"

    dxy_str = str(round(dxy, 2)) if dxy else "indisponible"

    prompt = (
        "Tu es un expert analyste XAU/USD. Nous sommes le " + today + " au matin.\n\n"
        "DONNEES DU JOUR:\n"
        "Prix or: " + str(round(price, 2)) + " USD/oz\n"
        "Variation: " + str(round(quote["change"], 2)) + " (" + str(round(quote["pct"], 2)) + "%)\n"
        "Signal H1: " + h1_sig + " | H4: " + h4_sig + " | D1: " + d1_sig + "\n"
        "DXY: " + dxy_str + "\n\n"
        "CALENDRIER ECONOMIQUE AUJOURD HUI:\n"
        "" + events_str + "\n"
        "Redige un rapport matinal structure en 5 sections:\n\n"
        "BONJOUR - RAPPORT XAU/USD DU " + today + "\n\n"
        "1. RESUME MARCHE: Situation actuelle de l or en 2 phrases\n"
        "2. BIAIS DU JOUR: Plutot haussier ou baissier aujourd hui et pourquoi\n"
        "3. NIVEAUX CLES: Support et resistance a surveiller aujourd hui\n"
        "4. AGENDA DU JOUR: Quels evenements economiques peuvent bouger l or\n"
        "5. STRATEGIE CONSEILEE: Que faire aujourd hui (attendre, acheter sur support, vendre sur resistance)\n\n"
        "En francais simple et direct."
    )

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=30
    )
    d = r.json()
    return "".join(b.get("text", "") for b in d.get("content", []))


# ── FORMATAGE MESSAGES ────────────────────────────────────────────

def format_mtf_line(mtf):
    lines = ""
    icons = {"BUY": "BUY", "SELL": "SELL", "NEUTRE": "NEUTRE", "ERREUR": "ERREUR"}
    for tf in ["H1", "H4", "D1"]:
        sig = mtf.get(tf, {}).get("signal", "N/A")
        conf = mtf.get(tf, {}).get("conf", 0)
        lines += tf + ": `" + sig + "` (" + str(conf) + "%) | "
    return lines.rstrip(" | ")


def format_analyse(price, quote, result, ind, mtf, events, ai_text):
    sig = result.get("sig", "N/A")
    conf = result.get("conf", 0)
    entry = result.get("entry", price)
    tp = result.get("tp")
    sl = result.get("sl")
    rr = result.get("rr")
    now = datetime.now().strftime("%H:%M - %d/%m/%Y")

    conf_label = "FIABLE" if conf >= 70 else ("MOYEN" if conf >= 55 else "FAIBLE")
    sig_label = "ACHAT" if sig == "BUY" else ("VENTE" if sig == "SELL" else "NEUTRE")
    chg = ("+" if quote["change"] >= 0 else "") + str(round(quote["change"], 2))
    pct = ("+" if quote["pct"] >= 0 else "") + str(round(quote["pct"], 2))

    reasons_text = "\n".join("  - " + r for r in result.get("reasons", []))
    rsi_label = "Surachat" if ind.get("rsi", 50) > 70 else ("Survente" if ind.get("rsi", 50) < 30 else "Neutre")

    confluence_sig, confluence_conf = mtf_confluence(mtf)
    aligned = "OUI - signal fort" if confluence_sig == sig and sig != "NEUTRE" else "NON - prudence"

    events_text = ""
    if events:
        for ev in events:
            events_text += "  - " + ev.get("name", "") + " (" + ev.get("time", "") + ")\n"
    else:
        events_text = "  Aucun evenement majeur\n"

    msg = (
        "*XAU/USD - SIGNAL PRO*\n"
        "`" + now + "`\n\n"
        "---\n"
        "*MULTI-TIMEFRAME*\n"
        "" + format_mtf_line(mtf) + "\n"
        "Confluence: `" + aligned + "`\n\n"
        "---\n"
        "*PRIX EN DIRECT*\n"
        "`" + str(round(price, 2)) + " USD/oz`\n"
        "" + chg + " USD (" + pct + "%)\n\n"
        "---\n"
        "*SIGNAL: " + sig_label + "*\n"
        "Fiabilite: *" + str(conf) + "%* [" + conf_label + "]\n\n"
        "Entree:      `" + str(round(entry, 2)) + "`\n"
        "Take Profit: `" + str(tp if tp else "---") + "`\n"
        "Stop Loss:   `" + str(sl if sl else "---") + "`\n"
        "Ratio R/R:   `1:" + str(rr if rr else "---") + "`\n\n"
        "---\n"
        "*RAISONS DU SIGNAL*\n"
        "" + reasons_text + "\n\n"
        "---\n"
        "*INDICATEURS H1*\n"
        "RSI:      `" + str(ind.get("rsi", "N/A")) + "` [" + rsi_label + "]\n"
        "MACD:     `" + str(ind.get("macd", {}).get("hist", "N/A")) + "`\n"
        "ADX:      `" + str(ind.get("adx", {}).get("adx", "N/A")) + "`\n"
        "Stoch:    `" + str(ind.get("stoch", {}).get("k", "N/A")) + "/" + str(ind.get("stoch", {}).get("d", "N/A")) + "`\n"
        "BB:       `" + str(ind.get("bb", {}).get("lower", "N/A")) + "` / `" + str(ind.get("bb", {}).get("upper", "N/A")) + "`\n"
        "ATR:      `" + str(ind.get("atr", "N/A")) + "`\n"
        "Support:  `" + str(ind.get("res", {}).get("sup", "N/A")) + "`\n"
        "Resist:   `" + str(ind.get("res", {}).get("res", "N/A")) + "`\n\n"
        "---\n"
        "*NEWS DU JOUR*\n"
        "" + events_text + "\n"
        "---\n"
        "*ANALYSE CLAUDE AI*\n\n"
        "" + ai_text + "\n\n"
        "---\n"
        "_Signaux indicatifs uniquement_\n"
        "_Pas un conseil financier_\n"
        "_Utilisez toujours un stop loss_"
    )
    return msg


def format_alert(price, quote, result, ind, mtf, events, ai_text):
    sig = result.get("sig", "N/A")
    conf = result.get("conf", 0)
    entry = result.get("entry", price)
    tp = result.get("tp")
    sl = result.get("sl")
    rr = result.get("rr")
    now = datetime.now().strftime("%H:%M - %d/%m/%Y")

    direction = "LONG (ACHAT)" if sig == "BUY" else "SHORT (VENTE)"
    gain = round(abs(tp - entry), 2) if tp else 0
    risk = round(abs(sl - entry), 2) if sl else 0

    events_text = ""
    if events:
        for ev in events:
            events_text += "  - " + ev.get("name", "") + " (" + ev.get("time", "") + ")\n"
    else:
        events_text = "  Aucun evenement majeur\n"

    msg = (
        "ALERTE TRADE XAU/USD\n"
        "`" + now + "`\n\n"
        "---\n"
        "*SIGNAL: " + direction + "*\n"
        "Fiabilite: *" + str(conf) + "%* [FIABLE]\n\n"
        "*MULTI-TIMEFRAME*\n"
        "" + format_mtf_line(mtf) + "\n\n"
        "---\n"
        "*TRADE A PRENDRE*\n"
        "Entree:      `" + str(round(entry, 2)) + "`\n"
        "Take Profit: `" + str(tp) + "`\n"
        "Stop Loss:   `" + str(sl) + "`\n"
        "Ratio R/R:   `1:" + str(rr) + "`\n"
        "Gain potentiel: `+" + str(gain) + " USD/oz`\n"
        "Risque max:     `-" + str(risk) + " USD/oz`\n\n"
        "---\n"
        "*PRIX ACTUEL*\n"
        "`" + str(round(price, 2)) + " USD/oz`\n\n"
        "---\n"
        "*NEWS A SURVEILLER*\n"
        "" + events_text + "\n"
        "---\n"
        "*ANALYSE CLAUDE AI*\n\n"
        "" + ai_text + "\n\n"
        "---\n"
        "_Signaux indicatifs uniquement_\n"
        "_Pas un conseil financier_\n"
        "_Utilisez toujours un stop loss_"
    )
    return msg


# ── COMMANDES BOT ─────────────────────────────────────────────────

def handle(update):
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "").strip().lower()

    if not chat_id:
        return

    if text in ["/start", "/aide", "/help"]:
        subscribers.add(chat_id)
        send(chat_id, (
            "*XAU/USD Signal Pro*\n\n"
            "Bienvenue ! Tu es abonne aux alertes automatiques.\n\n"
            "Commandes:\n\n"
            "/analyse - Analyse complete multi-timeframe\n"
            "/prix - Prix actuel XAU/USD\n"
            "/news - Evenements economiques du jour\n"
            "/rapport - Rapport marche complet\n"
            "/alertes - Activer les alertes auto (>80%)\n"
            "/stop - Desactiver les alertes\n"
            "/aide - Ce message\n\n"
            "Le bot scanne H1 + H4 + D1 toutes les 5 minutes.\n"
            "Alerte automatique quand fiabilite > 80%.\n"
            "Rapport quotidien envoye chaque matin a 8h.\n\n"
            "_Pas un conseil financier. Utilisez toujours un stop loss._"
        ))

    elif text == "/alertes":
        subscribers.add(chat_id)
        send(chat_id,
            "Alertes automatiques ACTIVEES.\n"
            "Signal > 80% fiabilite + confirmation multi-timeframe.\n"
            "Scan toutes les 5 minutes sur H1, H4 et D1."
        )

    elif text == "/stop":
        subscribers.discard(chat_id)
        send(chat_id, "Alertes automatiques DESACTIVEES.")

    elif text == "/prix":
        typing(chat_id)
        try:
            q = get_quote()
            chg = ("+" if q["change"] >= 0 else "") + str(round(q["change"], 2))
            pct = ("+" if q["pct"] >= 0 else "") + str(round(q["pct"], 2))
            send(chat_id,
                "*XAU/USD Prix Actuel*\n"
                "`" + str(round(q["price"], 2)) + " USD/oz`\n"
                "" + chg + " (" + pct + "%)"
            )
        except Exception as e:
            send(chat_id, "Erreur prix: " + str(e))

    elif text == "/news":
        typing(chat_id)
        try:
            events = get_economic_events()
            if events:
                msg_text = "*Evenements economiques importants*\n_(Impact sur l or)_\n\n"
                for ev in events:
                    msg_text += "- *" + ev.get("name", "") + "*\n"
                    msg_text += "  Heure: " + ev.get("time", "N/A") + "\n"
                    msg_text += "  Pays: " + ev.get("country", "N/A") + "\n\n"
            else:
                msg_text = "Aucun evenement economique majeur pour aujourd hui et demain."
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur news: " + str(e))

    elif text in ["/rapport", "/report"]:
        typing(chat_id)
        send(chat_id, "Preparation du rapport...\nAnalyse H1+H4+D1 + macro en cours.")
        try:
            price, quote, result, ind, mtf, events, dxy = run_full_analysis()
            ai_text = get_daily_report_ai(price, quote, mtf, events, dxy)
            send(chat_id, ai_text)
        except Exception as e:
            send(chat_id, "Erreur rapport: " + str(e))

    elif text in ["/analyse", "/signal", "/a"]:
        subscribers.add(chat_id)
        typing(chat_id)
        send(chat_id,
            "Analyse multi-timeframe en cours...\n"
            "H1 + H4 + D1 + calendrier economique\n"
            "Environ 20-30 secondes."
        )
        try:
            price, quote, result, ind, mtf, events, dxy = run_full_analysis()
            ai_text = get_ai_analysis(price, result, ind, quote, mtf, events, dxy)
            message = format_analyse(price, quote, result, ind, mtf, events, ai_text)
            send(chat_id, message)
        except Exception as e:
            send(chat_id, "Erreur analyse: " + str(e))

    else:
        send(chat_id,
            "Commande non reconnue.\n"
            "Tape /aide pour voir les commandes."
        )


# ── SCAN AUTOMATIQUE ──────────────────────────────────────────────

def auto_scan():
    print("Scan automatique lance...")
    while True:
        try:
            if subscribers:
                print("Scan... " + str(len(subscribers)) + " abonnes")
                price, quote, result, ind, mtf, events, dxy = run_full_analysis()
                sig = result.get("sig", "NEUTRE")
                conf = result.get("conf", 0)

                confluence_sig, confluence_conf = mtf_confluence(mtf)
                aligned = (confluence_sig == sig and sig != "NEUTRE")

                if sig != "NEUTRE" and conf >= ALERT_THRESHOLD and aligned:
                    last_sig = last_alert_sig.get("last", "")
                    last_conf = last_alert_sig.get("conf", 0)

                    if sig != last_sig or abs(conf - last_conf) >= 5:
                        print("ALERTE! Signal=" + sig + " Conf=" + str(conf) + "% Aligne=" + str(aligned))
                        ai_text = get_ai_analysis(price, result, ind, quote, mtf, events, dxy)
                        message = format_alert(price, quote, result, ind, mtf, events, ai_text)

                        for chat_id in list(subscribers):
                            send(chat_id, message)

                        last_alert_sig["last"] = sig
                        last_alert_sig["conf"] = conf
                else:
                    print("Signal=" + sig + " Conf=" + str(conf) + "% Aligne=" + str(aligned) + " - pas d alerte")
            else:
                print("Aucun abonne")

        except Exception as e:
            print("Erreur scan: " + str(e))

        time.sleep(SCAN_INTERVAL)


# ── RAPPORT QUOTIDIEN A 8H ────────────────────────────────────────

def daily_report_scheduler():
    print("Planificateur rapport quotidien demarre...")
    last_report_day = None
    while True:
        try:
            now = datetime.now()
            current_day = now.date()
            if now.hour == 8 and now.minute < 5 and last_report_day != current_day:
                if subscribers:
                    print("Envoi rapport quotidien...")
                    price, quote, result, ind, mtf, events, dxy = run_full_analysis()
                    ai_text = get_daily_report_ai(price, quote, mtf, events, dxy)
                    for chat_id in list(subscribers):
                        send(chat_id, ai_text)
                    last_report_day = current_day
                    print("Rapport quotidien envoye a " + str(len(subscribers)) + " abonnes")
        except Exception as e:
            print("Erreur rapport quotidien: " + str(e))
        time.sleep(60)


# ── MAIN ──────────────────────────────────────────────────────────

def main():
    print("Bot XAU/USD Signal Pro v4 demarre...")
    print("Seuil alerte: " + str(ALERT_THRESHOLD) + "%")
    print("Intervalle scan: " + str(SCAN_INTERVAL) + "s")
    print("Rapport quotidien: 8h00")

    scan_thread = threading.Thread(target=auto_scan, daemon=True)
    scan_thread.start()

    report_thread = threading.Thread(target=daily_report_scheduler, daemon=True)
    report_thread.start()

    offset = 0
    while True:
        try:
            r = requests.get(
                API_URL + "/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35
            )
            updates = r.json().get("result", [])
            for u in updates:
                offset = u["update_id"] + 1
                handle(u)
        except Exception as e:
            print("Erreur polling: " + str(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
