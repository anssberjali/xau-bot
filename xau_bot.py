import os
import time
import requests
import threading
from datetime import datetime, timedelta
import json

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TWELVE_KEY = os.environ.get("TWELVE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
SYMBOL = "XAU/USD"
API_URL = "https://api.telegram.org/bot" + TG_TOKEN

ALERT_THRESHOLD = 80
SCAN_INTERVAL = 300
MIN_ALERT_DELAY = 1800

subscribers = set()
last_alert_time = {}
last_alert_sig = {}
last_signal_data = {}


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
        "open": float(d["open"]),
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
    volumes = [float(b.get("volume", 0)) for b in data]
    return closes, highs, lows, volumes


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
        gold_keywords = ["fed", "fomc", "cpi", "inflation", "ppi", "gdp", "nfp", "jobs",
                        "unemployment", "rate", "dollar", "usd", "gold", "powell", "treasury"]
        important = []
        for ev in events[:15]:
            name = ev.get("name", "").lower()
            country = ev.get("country", "").upper()
            if country == "US" or any(k in name for k in gold_keywords):
                important.append({
                    "name": ev.get("name", ""),
                    "time": ev.get("time", ""),
                    "country": country,
                    "importance": ev.get("importance", ""),
                    "forecast": ev.get("forecast", "N/A"),
                    "previous": ev.get("previous", "N/A")
                })
        return important[:6]
    except Exception as e:
        print("Calendar error: " + str(e))
        return []


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
        "lower": round(m - 2 * sd, 2),
        "width": round(4 * sd, 2)
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


def calc_supres(cl, hi, lo):
    # Support/resistance sur les pivots réels
    sl = sorted(last_n(cl, 60))
    sup = round(sl[int(len(sl) * 0.1)], 2)
    res = round(sl[int(len(sl) * 0.9)], 2)
    # Pivot point du jour
    if hi and lo and cl:
        pivot = round((hi[-1] + lo[-1] + cl[-1]) / 3, 2)
        r1 = round(2 * pivot - lo[-1], 2)
        s1 = round(2 * pivot - hi[-1], 2)
        r2 = round(pivot + (hi[-1] - lo[-1]), 2)
        s2 = round(pivot - (hi[-1] - lo[-1]), 2)
    else:
        pivot = r1 = r2 = s1 = s2 = 0
    return {
        "sup": sup, "res": res,
        "pivot": pivot, "r1": r1, "r2": r2, "s1": s1, "s2": s2
    }


def calc_momentum(cl, n=10):
    if len(cl) < n + 1:
        return 0
    return round(((cl[-1] - cl[-1-n]) / cl[-1-n]) * 100, 3)


def candle_strength(opens, closes, highs, lows, n=3):
    if len(closes) < n:
        return "neutre"
    bull_count = sum(1 for i in range(-n, 0) if closes[i] > opens[i])
    if bull_count >= 2:
        return "haussiere"
    elif bull_count <= 1:
        return "baissiere"
    return "neutre"


def compute_indicators(closes, highs, lows, volumes=None, opens=None):
    price = closes[-1]
    atr = calc_atr(closes, highs, lows)
    sr = calc_supres(closes, highs, lows)
    bb = calc_bb(closes)
    op = opens if opens else closes
    return {
        "price": price,
        "rsi": calc_rsi(closes),
        "macd": calc_macd(closes),
        "e20": ema(last_n(closes, 20), 20),
        "e50": ema(last_n(closes, 50), 50),
        "e200": ema(last_n(closes, 200), 200),
        "adx": calc_adx(closes),
        "bb": bb,
        "stoch": calc_stoch(closes, highs, lows),
        "cci": calc_cci(closes),
        "wr": calc_wr(closes, highs, lows),
        "atr": atr,
        "psar": calc_psar(closes, highs, lows),
        "res": sr,
        "momentum": calc_momentum(closes),
        "candles": candle_strength(op, closes, highs, lows),
        "high24": round(max(last_n(highs, 24)), 2),
        "low24": round(min(last_n(lows, 24)), 2),
        "spread_bb": bb["width"],
    }


def build_signal(price, ind):
    S = []

    def p(name, d, w, label):
        S.append({"name": name, "dir": d, "w": w, "label": label})

    rv = ind["rsi"]
    if rv < 28:
        p("RSI", "bull", 3, "RSI survente extreme (" + str(rv) + ")")
    elif rv < 40:
        p("RSI", "bull", 2, "RSI survente (" + str(rv) + ")")
    elif rv > 72:
        p("RSI", "bear", 3, "RSI surachat extreme (" + str(rv) + ")")
    elif rv > 60:
        p("RSI", "bear", 2, "RSI surachat (" + str(rv) + ")")
    else:
        p("RSI", "neut", 1, "RSI neutre (" + str(rv) + ")")

    mh = ind["macd"]["hist"]
    mm = ind["macd"]["macd"]
    if mh > 0 and mm > 0:
        p("MACD", "bull", 2, "MACD haussier (hist+" + str(mh) + ")")
    elif mh > 0:
        p("MACD", "bull", 1, "MACD croise hausse")
    elif mh < 0 and mm < 0:
        p("MACD", "bear", 2, "MACD baissier (hist" + str(mh) + ")")
    else:
        p("MACD", "bear", 1, "MACD croise baisse")

    e20, e50, e200 = ind["e20"], ind["e50"], ind["e200"]
    if e20 > e50 and e50 > e200:
        p("EMA", "bull", 3, "EMA 20>50>200 tendance haussiere forte")
    elif e20 > e50:
        p("EMA", "bull", 2, "EMA 20>50 haussier")
    elif e20 < e50 and e50 < e200:
        p("EMA", "bear", 3, "EMA 20<50<200 tendance baissiere forte")
    else:
        p("EMA", "bear", 2, "EMA 20<50 baissier")

    adxv = ind["adx"]["adx"]
    dip = ind["adx"]["diP"]
    din = ind["adx"]["diN"]
    if adxv > 30 and dip > din:
        p("ADX", "bull", 2, "Tendance haussiere forte (ADX=" + str(adxv) + ")")
    elif adxv > 30:
        p("ADX", "bear", 2, "Tendance baissiere forte (ADX=" + str(adxv) + ")")
    else:
        p("ADX", "neut", 1, "Marche sans tendance (ADX=" + str(adxv) + ")")

    bbu = ind["bb"]["upper"]
    bbl = ind["bb"]["lower"]
    bbm = ind["bb"]["mid"]
    if price < bbl:
        p("BB", "bull", 2, "Prix sous bande basse (" + str(bbl) + ")")
    elif price > bbu:
        p("BB", "bear", 2, "Prix sur bande haute (" + str(bbu) + ")")
    elif price < bbm:
        p("BB", "bull", 1, "Prix sous moyenne BB (" + str(bbm) + ")")
    else:
        p("BB", "bear", 1, "Prix sur moyenne BB (" + str(bbm) + ")")

    sk = ind["stoch"]["k"]
    sd = ind["stoch"]["d"]
    if sk < 20 and sd < 20:
        p("STOCH", "bull", 2, "Stochastique survente (" + str(sk) + ")")
    elif sk > 80 and sd > 80:
        p("STOCH", "bear", 2, "Stochastique surachat (" + str(sk) + ")")
    elif sk > sd:
        p("STOCH", "bull", 1, "Stoch K>D haussier")
    else:
        p("STOCH", "neut", 1, "Stochastique neutre")

    cc = ind["cci"]
    if cc < -100:
        p("CCI", "bull", 2, "CCI survente (" + str(cc) + ")")
    elif cc > 100:
        p("CCI", "bear", 2, "CCI surachat (" + str(cc) + ")")
    else:
        p("CCI", "neut", 1, "CCI neutre")

    wrv = ind["wr"]
    if wrv < -80:
        p("WR", "bull", 2, "Williams survente (" + str(wrv) + ")")
    elif wrv > -20:
        p("WR", "bear", 2, "Williams surachat (" + str(wrv) + ")")
    else:
        p("WR", "neut", 1, "Williams neutre")

    rng = ind["res"]["res"] - ind["res"]["sup"]
    pos = (price - ind["res"]["sup"]) / (rng or 1)
    if pos < 0.15:
        p("SR", "bull", 2, "Proche support " + str(ind["res"]["sup"]))
    elif pos > 0.85:
        p("SR", "bear", 2, "Proche resistance " + str(ind["res"]["res"]))
    else:
        p("SR", "neut", 1, "Zone mediane S/R")

    if price > ind["psar"]:
        p("SAR", "bull", 1, "Prix > SAR (" + str(ind["psar"]) + ")")
    else:
        p("SAR", "bear", 1, "Prix < SAR (" + str(ind["psar"]) + ")")

    mom = ind["momentum"]
    if mom > 0.3:
        p("MOM", "bull", 1, "Momentum haussier (+" + str(mom) + "%)")
    elif mom < -0.3:
        p("MOM", "bear", 1, "Momentum baissier (" + str(mom) + "%)")
    else:
        p("MOM", "neut", 1, "Momentum plat")

    candles = ind["candles"]
    if candles == "haussiere":
        p("BOUGIES", "bull", 1, "Structure bougies haussiere")
    elif candles == "baissiere":
        p("BOUGIES", "bear", 1, "Structure bougies baissiere")

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
    tp1 = tp2 = tp3 = sl = rr = None
    entry = price
    entry_zone_low = entry_zone_high = price

    if sig == "BUY":
        entry_zone_low = round(price - a * 0.3, 2)
        entry_zone_high = round(price + a * 0.2, 2)
        tp1 = round(price + a * 1.5, 2)
        tp2 = round(price + a * 2.5, 2)
        tp3 = round(price + a * 4.0, 2)
        sl = round(price - a * 1.2, 2)

    if sig == "SELL":
        entry_zone_low = round(price - a * 0.2, 2)
        entry_zone_high = round(price + a * 0.3, 2)
        tp1 = round(price - a * 1.5, 2)
        tp2 = round(price - a * 2.5, 2)
        tp3 = round(price - a * 4.0, 2)
        sl = round(price + a * 1.2, 2)

    if tp2 and sl:
        g = abs(tp2 - price)
        r = abs(sl - price)
        rr = round(g / r, 2) if r > 0 else None

    dir_filter = "bull" if sig == "BUY" else "bear"
    reasons = [s["label"] for s in S if s["dir"] == dir_filter][:6]

    return {
        "sig": sig, "conf": conf, "entry": entry,
        "entry_zone_low": entry_zone_low, "entry_zone_high": entry_zone_high,
        "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "rr": rr,
        "bW": bW, "rW": rW, "reasons": reasons, "signals": S
    }


def multi_timeframe_analysis():
    results = {}
    timeframes = [("1h", "H1"), ("4h", "H4"), ("1day", "D1")]
    for interval, label in timeframes:
        try:
            closes, highs, lows, volumes = get_history(interval, 200)
            ind = compute_indicators(closes, highs, lows, volumes)
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
        return "BUY", round((buys / total) * 100)
    elif sells > buys:
        return "SELL", round((sells / total) * 100)
    return "NEUTRE", 50


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


def claude_validate_signal(price, result, ind, quote, mtf, events, dxy):
    if not ANTHROPIC_KEY:
        return True, "Cle Anthropic manquante - signal envoye sans validation AI."

    today = datetime.now().strftime("%d/%m/%Y %H:%M")
    sig = result.get("sig", "NEUTRE")
    conf = result.get("conf", 0)

    h1_sig = mtf.get("H1", {}).get("signal", "N/A")
    h4_sig = mtf.get("H4", {}).get("signal", "N/A")
    d1_sig = mtf.get("D1", {}).get("signal", "N/A")
    h1_conf = mtf.get("H1", {}).get("conf", 0)
    h4_conf = mtf.get("H4", {}).get("conf", 0)
    d1_conf = mtf.get("D1", {}).get("conf", 0)

    events_str = ""
    news_risk = "FAIBLE"
    if events:
        for ev in events:
            events_str += ev.get("name", "") + " a " + ev.get("time", "") + " | "
            ev_time = ev.get("time", "")
            try:
                ev_hour = int(ev_time.split(":")[0]) if ":" in ev_time else -1
                now_hour = datetime.now().hour
                if abs(ev_hour - now_hour) <= 2:
                    news_risk = "ELEVE"
            except:
                pass
    else:
        events_str = "Aucun evenement majeur"

    dxy_str = str(round(dxy, 2)) if dxy else "indisponible"

    prompt = (
        "Tu es un expert trader XAU/USD senior avec 15 ans d experience. Nous sommes le " + today + ".\n\n"
        "SIGNAL A VALIDER: " + sig + " avec " + str(conf) + "% de fiabilite technique\n\n"
        "ANALYSE MULTI-TIMEFRAME:\n"
        "H1: " + h1_sig + " (" + str(h1_conf) + "%) | H4: " + h4_sig + " (" + str(h4_conf) + "%) | D1: " + d1_sig + " (" + str(d1_conf) + "%)\n\n"
        "DONNEES TECHNIQUES H1:\n"
        "Prix: " + str(round(price, 2)) + " | Variation: " + str(round(quote["change"], 2)) + " (" + str(round(quote["pct"], 2)) + "%)\n"
        "Zone entree: " + str(result.get("entry_zone_low", "N/A")) + " - " + str(result.get("entry_zone_high", "N/A")) + "\n"
        "TP1: " + str(result.get("tp1", "N/A")) + " | TP2: " + str(result.get("tp2", "N/A")) + " | TP3: " + str(result.get("tp3", "N/A")) + "\n"
        "SL: " + str(result.get("sl", "N/A")) + " | Ratio R/R: 1:" + str(result.get("rr", "N/A")) + "\n"
        "RSI: " + str(ind.get("rsi", "N/A")) + " | MACD hist: " + str(ind.get("macd", {}).get("hist", "N/A")) + "\n"
        "EMA 20/50/200: " + str(ind.get("e20", "N/A")) + "/" + str(ind.get("e50", "N/A")) + "/" + str(ind.get("e200", "N/A")) + "\n"
        "ADX: " + str(ind.get("adx", {}).get("adx", "N/A")) + " DI+: " + str(ind.get("adx", {}).get("diP", "N/A")) + " DI-: " + str(ind.get("adx", {}).get("diN", "N/A")) + "\n"
        "Stoch: " + str(ind.get("stoch", {}).get("k", "N/A")) + "/" + str(ind.get("stoch", {}).get("d", "N/A")) + "\n"
        "CCI: " + str(ind.get("cci", "N/A")) + " | Williams: " + str(ind.get("wr", "N/A")) + "\n"
        "ATR: " + str(ind.get("atr", "N/A")) + " | SAR: " + str(ind.get("psar", "N/A")) + "\n"
        "Momentum: " + str(ind.get("momentum", "N/A")) + "% | Bougies: " + str(ind.get("candles", "N/A")) + "\n"
        "Pivot: " + str(ind.get("res", {}).get("pivot", "N/A")) + " | R1: " + str(ind.get("res", {}).get("r1", "N/A")) + " | S1: " + str(ind.get("res", {}).get("s1", "N/A")) + "\n"
        "High 24h: " + str(ind.get("high24", "N/A")) + " | Low 24h: " + str(ind.get("low24", "N/A")) + "\n"
        "DXY: " + dxy_str + " | Risque news: " + news_risk + "\n\n"
        "EVENEMENTS ECONOMIQUES: " + events_str + "\n\n"
        "ROLE: Valide ou invalide ce signal " + sig + " en suivant ces criteres STRICTS.\n\n"
        "Reponds EXACTEMENT dans ce format:\n\n"
        "VALIDATION: OUI\n"
        "ou\n"
        "VALIDATION: NON\n\n"
        "RAISON: (1-2 phrases courtes et directes)\n\n"
        "ANALYSE: (contexte marche, indicateurs cles, conseil precis sur l entree)\n\n"
        "RISQUE: FAIBLE ou MOYEN ou ELEVE\n\n"
        "LOT_CONSEILLE: (suggestion de taille de position en % du capital, ex: 1% du capital)\n\n"
        "Criteres OUI: 2/3 timeframes alignes, RSI coherent, ADX>20, RR>=1.5, pas de news dans 2h\n"
        "Criteres NON: timeframes contradictoires, RSI divergent, ADX<15, news imminente, RR<1.5\n\n"
        "Sois strict, honnete, en francais simple."
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
            "max_tokens": 700,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=30
    )
    d = r.json()
    response = "".join(b.get("text", "") for b in d.get("content", []))

    validated = "VALIDATION: OUI" in response.upper()

    raison = ""
    analyse = ""
    risque = "MOYEN"
    lot = "1% du capital"

    lines = response.split("\n")
    capture_analyse = False
    for line in lines:
        line_up = line.upper().strip()
        if line_up.startswith("RAISON:"):
            raison = line[7:].strip()
            capture_analyse = False
        elif line_up.startswith("ANALYSE:"):
            analyse = line[8:].strip()
            capture_analyse = True
        elif line_up.startswith("RISQUE:"):
            risque = line[7:].strip()
            capture_analyse = False
        elif line_up.startswith("LOT_CONSEILLE:"):
            lot = line[14:].strip()
            capture_analyse = False
        elif capture_analyse and line.strip() and not line_up.startswith("RISQUE") and not line_up.startswith("LOT"):
            analyse += " " + line.strip()

    return validated, raison.strip(), analyse.strip(), risque.strip(), lot.strip()


def format_mtf_line(mtf):
    lines = ""
    for tf in ["H1", "H4", "D1"]:
        sig = mtf.get(tf, {}).get("signal", "N/A")
        conf = mtf.get(tf, {}).get("conf", 0)
        icon = "BUY" if sig == "BUY" else ("SELL" if sig == "SELL" else "---")
        lines += tf + ": `" + icon + "` " + str(conf) + "% | "
    return lines.rstrip(" | ")


def format_precise_alert(price, quote, result, ind, mtf, events, raison, analyse, risque, lot, validated):
    sig = result.get("sig", "N/A")
    conf = result.get("conf", 0)
    entry = result.get("entry", price)
    entry_low = result.get("entry_zone_low", entry)
    entry_high = result.get("entry_zone_high", entry)
    tp1 = result.get("tp1")
    tp2 = result.get("tp2")
    tp3 = result.get("tp3")
    sl = result.get("sl")
    rr = result.get("rr")
    now = datetime.now().strftime("%H:%M - %d/%m/%Y")

    if sig == "BUY":
        direction = "ACHAT (LONG)"
        action = "ACHETER"
    else:
        direction = "VENTE (SHORT)"
        action = "VENDRE"

    gain1 = round(abs(tp1 - entry), 2) if tp1 else 0
    gain2 = round(abs(tp2 - entry), 2) if tp2 else 0
    gain3 = round(abs(tp3 - entry), 2) if tp3 else 0
    risk = round(abs(sl - entry), 2) if sl else 0

    risque_icon = "FAIBLE" if risque == "FAIBLE" else ("MOYEN" if risque == "MOYEN" else "ELEVE")

    events_text = ""
    if events:
        for ev in events[:3]:
            events_text += "  - " + ev.get("name", "") + " (" + ev.get("time", "") + ")\n"
    else:
        events_text = "  Aucun evenement majeur\n"

    msg = (
        "ALERTE TRADE XAU/USD\n"
        "`" + now + "`\n\n"
        "---\n"
        "*" + action + " XAU/USD*\n"
        "Signal: *" + direction + "*\n"
        "Fiabilite: *" + str(conf) + "%*\n"
        "Validation AI: *OUI - TRADE VALIDE*\n"
        "Risque: *" + risque_icon + "*\n\n"
        "---\n"
        "*PLAN DE TRADE PRECIS*\n\n"
        "Zone d entree: `" + str(entry_low) + " - " + str(entry_high) + "`\n"
        "Prix actuel:   `" + str(round(price, 2)) + "`\n\n"
        "SL:  `" + str(sl) + "` (risque -" + str(risk) + ")\n"
        "TP1: `" + str(tp1) + "` (+" + str(gain1) + ") - Securiser 30%\n"
        "TP2: `" + str(tp2) + "` (+" + str(gain2) + ") - Objectif principal\n"
        "TP3: `" + str(tp3) + "` (+" + str(gain3) + ") - Objectif max\n\n"
        "Ratio R/R: `1:" + str(rr) + "`\n"
        "Taille position: `" + lot + "`\n\n"
        "---\n"
        "*MULTI-TIMEFRAME*\n"
        "" + format_mtf_line(mtf) + "\n\n"
        "---\n"
        "*NIVEAUX CLES*\n"
        "High 24h: `" + str(ind.get("high24", "N/A")) + "`\n"
        "Low 24h:  `" + str(ind.get("low24", "N/A")) + "`\n"
        "Pivot:    `" + str(ind.get("res", {}).get("pivot", "N/A")) + "`\n"
        "R1:       `" + str(ind.get("res", {}).get("r1", "N/A")) + "`\n"
        "S1:       `" + str(ind.get("res", {}).get("s1", "N/A")) + "`\n"
        "ATR:      `" + str(ind.get("atr", "N/A")) + "` (volatilite)\n\n"
        "---\n"
        "*NEWS A SURVEILLER*\n"
        "" + events_text + "\n"
        "---\n"
        "*POURQUOI CE TRADE*\n"
        "" + raison + "\n\n"
        "*ANALYSE DETAILLEE*\n"
        "" + analyse + "\n\n"
        "---\n"
        "*COMMENT PASSER L ORDRE SUR MT4*\n"
        "1. Ouvrir MT4 -> Nouvel ordre\n"
        "2. Symbole: XAUUSD\n"
        "3. Type: " + ("Achat au marche" if sig == "BUY" else "Vente au marche") + "\n"
        "4. SL: " + str(sl) + "\n"
        "5. TP: " + str(tp2) + " (TP2 recommande)\n"
        "6. Volume: selon " + lot + "\n\n"
        "---\n"
        "_Signaux indicatifs uniquement_\n"
        "_Pas un conseil financier_\n"
        "_Utilisez toujours un stop loss_"
    )
    return msg


def format_analyse_complete(price, quote, result, ind, mtf, events, validated, raison, analyse, risque, lot):
    sig = result.get("sig", "N/A")
    conf = result.get("conf", 0)
    entry = result.get("entry", price)
    entry_low = result.get("entry_zone_low", entry)
    entry_high = result.get("entry_zone_high", entry)
    tp1 = result.get("tp1")
    tp2 = result.get("tp2")
    tp3 = result.get("tp3")
    sl = result.get("sl")
    rr = result.get("rr")
    now = datetime.now().strftime("%H:%M - %d/%m/%Y")

    conf_label = "FIABLE" if conf >= 70 else ("MOYEN" if conf >= 55 else "FAIBLE")
    sig_label = "ACHAT" if sig == "BUY" else ("VENTE" if sig == "SELL" else "NEUTRE")
    chg = ("+" if quote["change"] >= 0 else "") + str(round(quote["change"], 2))
    pct = ("+" if quote["pct"] >= 0 else "") + str(round(quote["pct"], 2))
    reasons_text = "\n".join("  - " + r for r in result.get("reasons", []))
    rsi_label = "Surachat" if ind.get("rsi", 50) > 70 else ("Survente" if ind.get("rsi", 50) < 30 else "Neutre")
    confluence_sig, _ = mtf_confluence(mtf)
    aligned = "OUI" if confluence_sig == sig and sig != "NEUTRE" else "NON"
    ai_verdict = "OUI - VALIDE" if validated else "NON - REJETE"

    events_text = ""
    if events:
        for ev in events:
            events_text += "  - " + ev.get("name", "") + " (" + ev.get("time", "") + ")\n"
    else:
        events_text = "  Aucun evenement majeur\n"

    tp1_str = str(tp1) if tp1 else "---"
    tp2_str = str(tp2) if tp2 else "---"
    tp3_str = str(tp3) if tp3 else "---"
    sl_str = str(sl) if sl else "---"

    msg = (
        "*XAU/USD - ANALYSE COMPLETE*\n"
        "`" + now + "`\n\n"
        "---\n"
        "*MULTI-TIMEFRAME*\n"
        "" + format_mtf_line(mtf) + "\n"
        "Confluence: `" + aligned + "`\n\n"
        "---\n"
        "*PRIX*\n"
        "`" + str(round(price, 2)) + " USD/oz` " + chg + " (" + pct + "%)\n"
        "High 24h: `" + str(ind.get("high24", "N/A")) + "` | Low 24h: `" + str(ind.get("low24", "N/A")) + "`\n\n"
        "---\n"
        "*SIGNAL: " + sig_label + "*\n"
        "Fiabilite: *" + str(conf) + "%* [" + conf_label + "]\n"
        "Validation AI: *" + ai_verdict + "*\n"
        "Risque: *" + risque + "*\n\n"
        "Zone entree: `" + str(entry_low) + " - " + str(entry_high) + "`\n"
        "SL:  `" + sl_str + "`\n"
        "TP1: `" + tp1_str + "` (securiser 30%)\n"
        "TP2: `" + tp2_str + "` (objectif principal)\n"
        "TP3: `" + tp3_str + "` (objectif max)\n"
        "R/R: `1:" + str(rr if rr else "---") + "`\n"
        "Position: `" + lot + "`\n\n"
        "---\n"
        "*RAISONS DU SIGNAL*\n"
        "" + reasons_text + "\n\n"
        "---\n"
        "*INDICATEURS H1*\n"
        "RSI:   `" + str(ind.get("rsi", "N/A")) + "` [" + rsi_label + "]\n"
        "MACD:  `" + str(ind.get("macd", {}).get("hist", "N/A")) + "`\n"
        "ADX:   `" + str(ind.get("adx", {}).get("adx", "N/A")) + "`\n"
        "Stoch: `" + str(ind.get("stoch", {}).get("k", "N/A")) + "/" + str(ind.get("stoch", {}).get("d", "N/A")) + "`\n"
        "ATR:   `" + str(ind.get("atr", "N/A")) + "`\n"
        "Pivot: `" + str(ind.get("res", {}).get("pivot", "N/A")) + "` R1:`" + str(ind.get("res", {}).get("r1", "N/A")) + "` S1:`" + str(ind.get("res", {}).get("s1", "N/A")) + "`\n\n"
        "---\n"
        "*NEWS DU JOUR*\n"
        "" + events_text + "\n"
        "---\n"
        "*AVIS CLAUDE AI*\n"
        "" + raison + "\n\n"
        "" + analyse + "\n\n"
        "---\n"
        "_Signaux indicatifs - Pas un conseil financier - SL obligatoire_"
    )
    return msg


def get_daily_report_ai(price, quote, mtf, events, dxy, ind):
    if not ANTHROPIC_KEY:
        return "Cle Anthropic manquante."

    today = datetime.now().strftime("%d/%m/%Y")
    h1_sig = mtf.get("H1", {}).get("signal", "N/A")
    h4_sig = mtf.get("H4", {}).get("signal", "N/A")
    d1_sig = mtf.get("D1", {}).get("signal", "N/A")

    events_str = ""
    if events:
        for ev in events:
            events_str += "- " + ev.get("name", "") + " a " + ev.get("time", "") + " (prev: " + ev.get("forecast", "N/A") + ")\n"
    else:
        events_str = "Aucun evenement majeur\n"

    dxy_str = str(round(dxy, 2)) if dxy else "indisponible"

    prompt = (
        "Tu es un expert analyste XAU/USD. Nous sommes le " + today + " au matin.\n\n"
        "DONNEES MARCHE:\n"
        "Prix: " + str(round(price, 2)) + " | Variation: " + str(round(quote["change"], 2)) + " (" + str(round(quote["pct"], 2)) + "%)\n"
        "High 24h: " + str(ind.get("high24", "N/A")) + " | Low 24h: " + str(ind.get("low24", "N/A")) + "\n"
        "ATR: " + str(ind.get("atr", "N/A")) + " (volatilite attendue par bougie)\n"
        "Signal H1: " + h1_sig + " | H4: " + h4_sig + " | D1: " + d1_sig + "\n"
        "DXY: " + dxy_str + "\n"
        "Pivot: " + str(ind.get("res", {}).get("pivot", "N/A")) + " | R1: " + str(ind.get("res", {}).get("r1", "N/A")) + " | S1: " + str(ind.get("res", {}).get("s1", "N/A")) + "\n\n"
        "CALENDRIER ECONOMIQUE AUJOURD HUI:\n"
        "" + events_str + "\n"
        "Redige un rapport matinal precis:\n\n"
        "RAPPORT XAU/USD - " + today + "\n\n"
        "1. SITUATION: Etat du marche en 2 phrases\n"
        "2. BIAIS: Haussier ou baissier et pourquoi\n"
        "3. NIVEAUX: Support et resistance cles a surveiller\n"
        "4. AGENDA: Impact des news prevues aujourd hui\n"
        "5. STRATEGIE: Quoi faire aujourd hui avec zones d entree precises\n\n"
        "Sois precis avec des niveaux de prix. En francais simple."
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


def handle(update):
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "").strip().lower()

    if not chat_id:
        return

    if text in ["/start", "/aide", "/help"]:
        subscribers.add(chat_id)
        send(chat_id, (
            "*XAU/USD Signal Pro v6*\n\n"
            "Alertes automatiques activees.\n\n"
            "Commandes:\n\n"
            "/analyse - Analyse complete + validation AI\n"
            "/prix - Prix actuel\n"
            "/news - Evenements economiques\n"
            "/rapport - Rapport marche du jour\n"
            "/niveaux - Supports et resistances\n"
            "/alertes - Activer alertes auto\n"
            "/stop - Desactiver alertes\n\n"
            "Fonctionnement:\n"
            "Indicateurs => Signal => Claude AI valide\n"
            "=> Alerte avec TP1/TP2/TP3 + SL precis\n"
            "=> Comment passer l ordre sur MT4\n\n"
            "Scan H1+H4+D1 toutes les 5 min\n"
            "Rapport automatique a 8h chaque matin\n\n"
            "_Pas un conseil financier. SL obligatoire._"
        ))

    elif text == "/alertes":
        subscribers.add(chat_id)
        send(chat_id, "Alertes ACTIVEES. Signal>80% + validation Claude AI.")

    elif text == "/stop":
        subscribers.discard(chat_id)
        send(chat_id, "Alertes DESACTIVEES.")

    elif text == "/prix":
        typing(chat_id)
        try:
            q = get_quote()
            chg = ("+" if q["change"] >= 0 else "") + str(round(q["change"], 2))
            pct = ("+" if q["pct"] >= 0 else "") + str(round(q["pct"], 2))
            send(chat_id, "*XAU/USD*\n`" + str(round(q["price"], 2)) + " USD/oz`\n" + chg + " (" + pct + "%)")
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text == "/niveaux":
        typing(chat_id)
        try:
            closes, highs, lows, _ = get_history("1h", 50)
            ind = compute_indicators(closes, highs, lows)
            sr = ind["res"]
            send(chat_id,
                "*Niveaux XAU/USD*\n\n"
                "Pivot: `" + str(sr.get("pivot", "N/A")) + "`\n"
                "R2: `" + str(sr.get("r2", "N/A")) + "`\n"
                "R1: `" + str(sr.get("r1", "N/A")) + "`\n"
                "S1: `" + str(sr.get("s1", "N/A")) + "`\n"
                "S2: `" + str(sr.get("s2", "N/A")) + "`\n\n"
                "Resistance principale: `" + str(sr.get("res", "N/A")) + "`\n"
                "Support principal:     `" + str(sr.get("sup", "N/A")) + "`\n"
                "High 24h: `" + str(ind.get("high24", "N/A")) + "`\n"
                "Low 24h:  `" + str(ind.get("low24", "N/A")) + "`\n"
                "ATR:      `" + str(ind.get("atr", "N/A")) + "` (mouvement moyen/bougie)"
            )
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text == "/news":
        typing(chat_id)
        try:
            events = get_economic_events()
            if events:
                msg_text = "*News importantes pour l or*\n\n"
                for ev in events:
                    msg_text += "*" + ev.get("name", "") + "*\n"
                    msg_text += "Heure: " + ev.get("time", "N/A") + "\n"
                    msg_text += "Prevision: " + ev.get("forecast", "N/A") + "\n"
                    msg_text += "Precedent: " + ev.get("previous", "N/A") + "\n\n"
            else:
                msg_text = "Aucun evenement majeur aujourd hui."
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text in ["/rapport", "/report"]:
        typing(chat_id)
        send(chat_id, "Preparation rapport... 20-30 secondes.")
        try:
            price, quote, result, ind, mtf, events, dxy = run_full_analysis()
            ai_text = get_daily_report_ai(price, quote, mtf, events, dxy, ind)
            send(chat_id, ai_text)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text in ["/analyse", "/signal", "/a"]:
        subscribers.add(chat_id)
        typing(chat_id)
        send(chat_id, "Analyse H1+H4+D1 + validation Claude AI...\n20-30 secondes.")
        try:
            price, quote, result, ind, mtf, events, dxy = run_full_analysis()
            validated, raison, analyse, risque, lot = claude_validate_signal(price, result, ind, quote, mtf, events, dxy)
            message = format_analyse_complete(price, quote, result, ind, mtf, events, validated, raison, analyse, risque, lot)
            send(chat_id, message)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    else:
        send(chat_id, "Tape /aide pour les commandes.")


def auto_scan():
    print("Scan automatique lance...")
    while True:
        try:
            if subscribers:
                print("Scan... " + str(len(subscribers)) + " abonnes")
                price, quote, result, ind, mtf, events, dxy = run_full_analysis()
                sig = result.get("sig", "NEUTRE")
                conf = result.get("conf", 0)
                confluence_sig, _ = mtf_confluence(mtf)
                aligned = (confluence_sig == sig and sig != "NEUTRE")

                if sig != "NEUTRE" and conf >= ALERT_THRESHOLD and aligned:
                    now_ts = time.time()
                    last_time = last_alert_time.get(sig, 0)
                    last_sig = last_alert_sig.get("last", "")
                    time_ok = (now_ts - last_time) >= MIN_ALERT_DELAY
                    sig_changed = (sig != last_sig)

                    if time_ok or sig_changed:
                        print("Validation Claude AI...")
                        validated, raison, analyse, risque, lot = claude_validate_signal(price, result, ind, quote, mtf, events, dxy)

                        if validated:
                            print("ALERTE VALIDEE: " + sig + " " + str(conf) + "%")
                            message = format_precise_alert(price, quote, result, ind, mtf, events, raison, analyse, risque, lot, validated)
                            for chat_id in list(subscribers):
                                send(chat_id, message)
                            last_alert_time[sig] = now_ts
                            last_alert_sig["last"] = sig
                            last_signal_data["signal"] = sig
                            last_signal_data["entry"] = result.get("entry")
                            last_signal_data["sl"] = result.get("sl")
                            last_signal_data["tp1"] = result.get("tp1")
                            last_signal_data["tp2"] = result.get("tp2")
                            last_signal_data["tp3"] = result.get("tp3")
                            last_signal_data["time"] = datetime.now().isoformat()
                        else:
                            print("REJETE par Claude: " + sig + " " + str(conf) + "%")
                    else:
                        mins = round((MIN_ALERT_DELAY - (now_ts - last_time)) / 60)
                        print("Delai: encore " + str(mins) + " min")
                else:
                    print("Signal=" + sig + " Conf=" + str(conf) + "% Aligne=" + str(aligned))

        except Exception as e:
            print("Erreur scan: " + str(e))

        time.sleep(SCAN_INTERVAL)


def daily_report_scheduler():
    print("Rapport quotidien demarre...")
    last_report_day = None
    while True:
        try:
            now = datetime.now()
            if now.hour == 8 and now.minute < 5 and last_report_day != now.date():
                if subscribers:
                    price, quote, result, ind, mtf, events, dxy = run_full_analysis()
                    ai_text = get_daily_report_ai(price, quote, mtf, events, dxy, ind)
                    for chat_id in list(subscribers):
                        send(chat_id, ai_text)
                    last_report_day = now.date()
                    print("Rapport envoye")
        except Exception as e:
            print("Erreur rapport: " + str(e))
        time.sleep(60)


def main():
    print("XAU/USD Signal Pro v6")
    print("Seuil: " + str(ALERT_THRESHOLD) + "% | Delai: " + str(MIN_ALERT_DELAY//60) + "min | Scan: " + str(SCAN_INTERVAL) + "s")

    threading.Thread(target=auto_scan, daemon=True).start()
    threading.Thread(target=daily_report_scheduler, daemon=True).start()

    offset = 0
    while True:
        try:
            r = requests.get(API_URL + "/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                handle(u)
        except Exception as e:
            print("Erreur: " + str(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
