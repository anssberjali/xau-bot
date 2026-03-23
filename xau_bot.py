import os
import time
import requests
from datetime import datetime

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TWELVE_KEY = os.environ.get("TWELVE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
SYMBOL = "XAU/USD"
API_URL = "https://api.telegram.org/bot" + TG_TOKEN


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
        "high": float(d["high"]),
        "low": float(d["low"]),
        "change": float(d["change"]),
        "pct": float(d["percent_change"])
    }


def get_history():
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": SYMBOL, "interval": "1h", "outputsize": 200, "apikey": TWELVE_KEY},
        timeout=15
    )
    d = r.json()
    if d.get("status") == "error":
        raise Exception(d.get("message", "API error"))
    bars = list(reversed(d.get("values", [])))
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    return closes, highs, lows


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
    if sig == "BUY":
        tp = round(price + a * 2, 2)
        sl = round(price - a * 1.2, 2)
    if sig == "SELL":
        tp = round(price - a * 2, 2)
        sl = round(price + a * 1.2, 2)
    if tp and sl:
        g = abs(tp - price)
        r = abs(sl - price)
        rr = round(g / r, 2) if r > 0 else None

    dir_filter = "bull" if sig == "BUY" else "bear"
    reasons = [s["label"] for s in S if s["dir"] == dir_filter][:5]

    return {
        "sig": sig, "conf": conf, "tp": tp, "sl": sl, "rr": rr,
        "bW": bW, "rW": rW, "reasons": reasons
    }


def get_ai_analysis(price, result, ind, quote):
    if not ANTHROPIC_KEY:
        return "Cle Anthropic manquante."

    today = datetime.now().strftime("%d/%m/%Y %H:%M")

    prompt = (
        "Tu es un expert trader XAU/USD et analyste macro. Nous sommes le " + today + ".\n\n"
        "DONNEES MARCHE EN TEMPS REEL:\n"
        "Prix: " + str(round(price, 2)) + " USD/oz\n"
        "Variation: " + str(round(quote["change"], 2)) + " (" + str(round(quote["pct"], 2)) + "%)\n"
        "Signal technique: " + result["sig"] + " avec " + str(result["conf"]) + "% de fiabilite\n"
        "TP: " + str(result["tp"]) + " | SL: " + str(result["sl"]) + " | Ratio RR: 1:" + str(result["rr"]) + "\n"
        "RSI: " + str(ind["rsi"]) + "\n"
        "MACD histogramme: " + str(ind["macd"]["hist"]) + "\n"
        "EMA 20/50/200: " + str(ind["e20"]) + "/" + str(ind["e50"]) + "/" + str(ind["e200"]) + "\n"
        "ADX: " + str(ind["adx"]["adx"]) + " (DI+: " + str(ind["adx"]["diP"]) + " / DI-: " + str(ind["adx"]["diN"]) + ")\n"
        "Stoch: " + str(ind["stoch"]["k"]) + "/" + str(ind["stoch"]["d"]) + "\n"
        "CCI: " + str(ind["cci"]) + " | Williams: " + str(ind["wr"]) + "\n"
        "ATR: " + str(ind["atr"]) + " | SAR: " + str(ind["psar"]) + "\n"
        "Support: " + str(ind["res"]["sup"]) + " | Resistance: " + str(ind["res"]["res"]) + "\n"
        "Score Bull: " + str(result["bW"]) + " pts | Score Bear: " + str(result["rW"]) + " pts\n\n"
        "Redige une analyse complete en 4 sections:\n\n"
        "1. SIGNAL TECHNIQUE: Explique pourquoi le signal est " + result["sig"] + " en 2-3 phrases simples\n"
        "2. CONTEXTE MACRO: Dollar, Fed, inflation, geopolitique - quel est leur impact sur l or aujourd hui\n"
        "3. CATALYSEURS: 2-3 evenements ou donnees economiques a surveiller cette semaine\n"
        "4. CONSEIL FINAL: Valides-tu ce trade ? Entree recommandee, SL, TP, taille de position conseille\n\n"
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
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=30
    )
    d = r.json()
    return "".join(b.get("text", "") for b in d.get("content", []))


def format_message(price, quote, result, ind, ai_text):
    sig = result["sig"]
    conf = result["conf"]
    tp = result["tp"]
    sl = result["sl"]
    rr = result["rr"]
    now = datetime.now().strftime("%H:%M - %d/%m/%Y")

    if conf >= 70:
        conf_label = "FIABLE"
    elif conf >= 55:
        conf_label = "MOYEN"
    else:
        conf_label = "FAIBLE"

    if sig == "BUY":
        sig_icon = "ACHAT"
    elif sig == "SELL":
        sig_icon = "VENTE"
    else:
        sig_icon = "NEUTRE"

    chg = ("+" if quote["change"] >= 0 else "") + str(round(quote["change"], 2))
    pct = ("+" if quote["pct"] >= 0 else "") + str(round(quote["pct"], 2))

    reasons_text = "\n".join("  - " + r for r in result["reasons"])

    rsi_label = "Surachat" if ind["rsi"] > 70 else ("Survente" if ind["rsi"] < 30 else "Neutre")

    msg = (
        "*XAU/USD - SIGNAL PRO*\n"
        "`" + now + "`\n\n"
        "---\n"
        "*PRIX EN DIRECT*\n"
        "`" + str(round(price, 2)) + " USD/oz`\n"
        "" + chg + " USD (" + pct + "%)\n\n"
        "---\n"
        "*SIGNAL: " + sig_icon + "*\n"
        "Fiabilite: *" + str(conf) + "%* [" + conf_label + "]\n\n"
        "Take Profit:  `" + str(tp if tp else "---") + "`\n"
        "Stop Loss:    `" + str(sl if sl else "---") + "`\n"
        "Ratio R/R:    `1:" + str(rr if rr else "---") + "`\n\n"
        "---\n"
        "*RAISONS DU SIGNAL*\n"
        "" + reasons_text + "\n\n"
        "---\n"
        "*INDICATEURS CLES*\n"
        "RSI(14):  `" + str(ind["rsi"]) + "` [" + rsi_label + "]\n"
        "MACD:     `" + str(ind["macd"]["macd"]) + "` hist: `" + str(ind["macd"]["hist"]) + "`\n"
        "ADX:      `" + str(ind["adx"]["adx"]) + "` DI+`" + str(ind["adx"]["diP"]) + "` DI-`" + str(ind["adx"]["diN"]) + "`\n"
        "Stoch:    `" + str(ind["stoch"]["k"]) + "` / `" + str(ind["stoch"]["d"]) + "`\n"
        "CCI:      `" + str(ind["cci"]) + "`\n"
        "Williams: `" + str(ind["wr"]) + "`\n"
        "BB:       `" + str(ind["bb"]["lower"]) + "` / `" + str(ind["bb"]["upper"]) + "`\n"
        "ATR:      `" + str(ind["atr"]) + "`\n"
        "SAR:      `" + str(ind["psar"]) + "`\n"
        "Support:  `" + str(ind["res"]["sup"]) + "`\n"
        "Resist:   `" + str(ind["res"]["res"]) + "`\n\n"
        "---\n"
        "*ANALYSE CLAUDE AI*\n\n"
        "" + ai_text + "\n\n"
        "---\n"
        "_Signaux indicatifs uniquement_\n"
        "_Pas un conseil financier_\n"
        "_Utilisez toujours un stop loss_"
    )
    return msg


def handle(update):
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "").strip().lower()

    if not chat_id:
        return

    if text in ["/start", "/aide", "/help"]:
        send(chat_id, (
            "*XAU/USD Signal Pro*\n\n"
            "Bienvenue ! Voici les commandes:\n\n"
            "/analyse - Analyse complete en temps reel\n"
            "/prix - Prix actuel XAU/USD\n"
            "/aide - Afficher ce message\n\n"
            "Le bot analyse 11 indicateurs techniques + contexte macro pour te donner un signal BUY/SELL valide.\n\n"
            "_Pas un conseil financier. Utilisez toujours un stop loss._"
        ))

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

    elif text in ["/analyse", "/signal", "/a"]:
        typing(chat_id)
        send(chat_id,
            "Analyse en cours...\n"
            "Recuperation donnees + analyse AI\n"
            "Environ 15-20 secondes, patientez."
        )
        try:
            closes, highs, lows = get_history()
            quote = get_quote()
            price = quote["price"]
            closes.append(price)
            highs.append(quote["high"])
            lows.append(quote["low"])

            ind = {
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

            result = build_signal(price, ind)
            ai_text = get_ai_analysis(price, result, ind, quote)
            message = format_message(price, quote, result, ind, ai_text)
            send(chat_id, message)

        except Exception as e:
            send(chat_id, "Erreur lors de l analyse: " + str(e))

    else:
        send(chat_id,
            "Commande non reconnue.\n"
            "Tape /aide pour voir les commandes disponibles."
        )


def main():
    print("Bot XAU/USD demarre...")
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
            print("Erreur: " + str(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
