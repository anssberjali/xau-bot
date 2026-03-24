import os
import time
import requests
import threading
import json
from datetime import datetime, timedelta
from math import sqrt

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TWELVE_KEY = os.environ.get("TWELVE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
FRED_KEY = os.environ.get("FRED_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SYMBOL = "XAU/USD"
API_URL = "https://api.telegram.org/bot" + TG_TOKEN

ALERT_THRESHOLD = 78
SCAN_INTERVAL = 900  # 15 minutes = 280 credits/jour (limite: 800)
MIN_ALERT_DELAY = 1800
TRADE_MONITOR_INTERVAL = 60

subscribers = set()
last_alert_time = {}
last_alert_sig = {}
active_trades = {}
user_capital = {}
signal_history = []
cached_cot = {"data": None, "time": 0}
cached_fear_greed = {"data": None, "time": 0}

# ── SUPABASE DATABASE ─────────────────────────────────────────────

def supabase_insert(table, data):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        requests.post(
            SUPABASE_URL + "/rest/v1/" + table,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": "Bearer " + SUPABASE_KEY,
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            },
            json=data,
            timeout=8
        )
        return True
    except Exception as e:
        print("Supabase insert error: " + str(e))
        return False


def supabase_select(table, params="select=*&order=created_at.desc&limit=100"):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    try:
        r = requests.get(
            SUPABASE_URL + "/rest/v1/" + table + "?" + params,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": "Bearer " + SUPABASE_KEY
            },
            timeout=8
        )
        return r.json()
    except Exception as e:
        print("Supabase select error: " + str(e))
        return []


def db_save_signal(sig, conf, entry, tp1, tp2, tp3, sl, rr, structure, session, validated):
    return supabase_insert("signals", {
        "signal": sig,
        "confidence": conf,
        "entry_price": round(entry, 2) if entry else None,
        "tp1": round(tp1, 2) if tp1 else None,
        "tp2": round(tp2, 2) if tp2 else None,
        "tp3": round(tp3, 2) if tp3 else None,
        "sl": round(sl, 2) if sl else None,
        "rr": rr,
        "structure": structure,
        "session": session,
        "validated": validated,
        "outcome": "OPEN"
    })


def db_get_performance():
    data = supabase_select("signals",
        "select=signal,confidence,outcome,created_at&order=created_at.desc&limit=500")
    if not data or not isinstance(data, list):
        return None
    from datetime import datetime, timedelta
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    weekly = [s for s in data if s.get("created_at", "") > week_ago]
    total = len(weekly)
    wins = sum(1 for s in weekly if s.get("outcome") == "WIN")
    losses = sum(1 for s in weekly if s.get("outcome") == "LOSS")
    open_t = sum(1 for s in weekly if s.get("outcome") == "OPEN")
    win_rate = round((wins / (wins + losses)) * 100, 1) if (wins + losses) > 0 else 0
    buys = [s for s in weekly if s.get("signal") == "BUY"]
    sells = [s for s in weekly if s.get("signal") == "SELL"]
    buy_wins = sum(1 for s in buys if s.get("outcome") == "WIN")
    sell_wins = sum(1 for s in sells if s.get("outcome") == "WIN")
    all_total = len(data)
    all_wins = sum(1 for s in data if s.get("outcome") == "WIN")
    all_losses = sum(1 for s in data if s.get("outcome") == "LOSS")
    all_rate = round((all_wins / (all_wins + all_losses)) * 100, 1) if (all_wins + all_losses) > 0 else 0
    return {
        "weekly_total": total, "weekly_wins": wins,
        "weekly_losses": losses, "weekly_open": open_t,
        "weekly_rate": win_rate,
        "buy_total": len(buys), "buy_wins": buy_wins,
        "buy_rate": round((buy_wins / len(buys)) * 100, 1) if buys else 0,
        "sell_total": len(sells), "sell_wins": sell_wins,
        "sell_rate": round((sell_wins / len(sells)) * 100, 1) if sells else 0,
        "all_total": all_total, "all_wins": all_wins,
        "all_losses": all_losses, "all_rate": all_rate
    }


def format_db_performance():
    perf = db_get_performance()
    if not perf:
        return "Aucune donnee en base. Les signaux seront sauvegardes apres la prochaine alerte."
    line1 = "*PERFORMANCE REELLE (Base de donnees)*"
    line2 = "*7 derniers jours:*"
    line3 = "Signaux: `" + str(perf["weekly_total"]) + "` | Wins: `" + str(perf["weekly_wins"]) + "` | Losses: `" + str(perf["weekly_losses"]) + "`"
    line4 = "Taux reussite: *" + str(perf["weekly_rate"]) + "%*"
    line5 = "*Historique complet:*"
    line6 = "Total: `" + str(perf["all_total"]) + "` signaux"
    line7 = "Taux global: *" + str(perf["all_rate"]) + "%*"
    line8 = "*Par direction:*"
    line9 = "BUY: `" + str(perf["buy_total"]) + "` -> *" + str(perf["buy_rate"]) + "%*"
    line10 = "SELL: `" + str(perf["sell_total"]) + "` -> *" + str(perf["sell_rate"]) + "%*"
    nl = chr(10)
    return (line1 + nl + nl + line2 + nl + line3 + nl + line4 + nl + nl + line5 + nl + line6 + nl + line7 + nl + nl + line8 + nl + line9 + nl + line10)
def send(chat_id, text):
    try:
        requests.post(API_URL + "/sendMessage", json={
            "chat_id": chat_id, "text": text, "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print("Send error: " + str(e))


def typing(chat_id):
    try:
        requests.post(API_URL + "/sendChatAction", json={
            "chat_id": chat_id, "action": "typing"
        }, timeout=5)
    except:
        pass


# ── SESSION ───────────────────────────────────────────────────────

def get_session_info():
    now_utc = datetime.utcnow()
    hour = now_utc.hour
    t = hour + now_utc.minute / 60.0
    if 13.0 <= t < 16.0:
        return {"session": "OVERLAP LONDRES/NY", "quality": "OPTIMALE", "active": True, "hour_utc": hour}
    elif 7.0 <= t < 16.0:
        return {"session": "LONDRES", "quality": "EXCELLENTE", "active": True, "hour_utc": hour}
    elif 16.0 <= t < 21.0:
        return {"session": "NEW YORK", "quality": "EXCELLENTE", "active": True, "hour_utc": hour}
    elif 21.0 <= t or t < 2.0:
        return {"session": "ASIATIQUE", "quality": "FAIBLE", "active": False, "hour_utc": hour}
    return {"session": "TRANSITION", "quality": "MOYENNE", "active": True, "hour_utc": hour}


# ── MARKET DATA ───────────────────────────────────────────────────

def get_quote():
    r = requests.get("https://api.twelvedata.com/quote",
        params={"symbol": SYMBOL, "apikey": TWELVE_KEY}, timeout=10)
    d = r.json()
    if d.get("status") == "error":
        msg = d.get("message", "API error")
        raise Exception(msg)
    return {
        "price": float(d["close"]), "open": float(d["open"]),
        "high": float(d["high"]), "low": float(d["low"]),
        "change": float(d["change"]), "pct": float(d["percent_change"])
    }


def get_history(interval="1h", bars=200):
    r = requests.get("https://api.twelvedata.com/time_series",
        params={"symbol": SYMBOL, "interval": interval,
                "outputsize": bars, "apikey": TWELVE_KEY}, timeout=15)
    d = r.json()
    if d.get("status") == "error":
        raise Exception(d.get("message", "API error"))
    data = list(reversed(d.get("values", [])))
    return (
        [float(b["close"]) for b in data],
        [float(b["high"]) for b in data],
        [float(b["low"]) for b in data],
        [float(b["open"]) for b in data],
        [b["datetime"] for b in data],
        [float(b.get("volume", 0)) for b in data]
    )


# Cache pour les correlations (valide 5 minutes)
_corr_cache = {"data": None, "time": 0}

def get_correlated_assets():
    now = time.time()
    if _corr_cache["data"] and (now - _corr_cache["time"]) < 300:
        return _corr_cache["data"]
    results = {}
    mapping = {
        "WTI/USD": "WTI", "SPY": "SPX", "TLT": "BONDS",
        "DX/Y": "DXY", "VIX": "VIX", "XAG/USD": "SILVER"
    }
    try:
        # Essai 1: appel batch pour tous les actifs
        symbols = "WTI/USD,SPY,TLT,DX/Y,VIX,XAG/USD"
        r = requests.get(
            "https://api.twelvedata.com/quote",
            params={"symbol": symbols, "apikey": TWELVE_KEY},
            timeout=10
        )
        d = r.json()
        if isinstance(d, dict):
            for sym, name in mapping.items():
                item = d.get(sym, {})
                if item and item.get("status") != "error" and item.get("close"):
                    try:
                        results[name] = {
                            "price": float(item["close"]),
                            "change": float(item.get("change", 0)),
                            "pct": float(item.get("percent_change", 0))
                        }
                    except:
                        pass
        print("Corr batch: " + str(len(results)) + " actifs recuperes")
    except Exception as e:
        print("Corr batch error: " + str(e))

    # Fallback: appels individuels pour les actifs manquants
    if len(results) < 3:
        print("Fallback correlations individuelles...")
        for sym, name in mapping.items():
            if name not in results:
                try:
                    r = requests.get(
                        "https://api.twelvedata.com/quote",
                        params={"symbol": sym, "apikey": TWELVE_KEY},
                        timeout=6
                    )
                    d = r.json()
                    if d.get("status") != "error" and d.get("close"):
                        results[name] = {
                            "price": float(d["close"]),
                            "change": float(d.get("change", 0)),
                            "pct": float(d.get("percent_change", 0))
                        }
                    time.sleep(8)
                except:
                    pass

    _corr_cache["data"] = results
    _corr_cache["time"] = now
    return results


def get_dxy():
    # Reutilise le cache des correlations si disponible
    corr = _corr_cache.get("data") or {}
    if "DXY" in corr:
        return corr["DXY"]["price"]
    try:
        r = requests.get("https://api.twelvedata.com/quote",
            params={"symbol": "DX/Y", "apikey": TWELVE_KEY}, timeout=8)
        d = r.json()
        if d.get("status") != "error":
            return float(d.get("close", 0))
    except:
        pass
    return None


_events_cache = {"data": None, "time": 0}

def get_economic_events():
    now = time.time()
    if _events_cache["data"] is not None and (now - _events_cache["time"]) < 1800:
        return _events_cache["data"]
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        r = requests.get("https://api.twelvedata.com/economic_calendar",
            params={"start_date": today, "end_date": tomorrow,
                   "importance": "high", "apikey": TWELVE_KEY}, timeout=10)
        d = r.json()
        events = d.get("result", {}).get("list", [])
        gold_keywords = ["fed", "fomc", "cpi", "inflation", "ppi", "gdp", "nfp",
                        "jobs", "unemployment", "rate", "dollar", "usd", "gold", "powell"]
        important = []
        for ev in events[:15]:
            name = ev.get("name", "").lower()
            country = ev.get("country", "").upper()
            if country == "US" or any(k in name for k in gold_keywords):
                important.append({
                    "name": ev.get("name", ""), "time": ev.get("time", ""),
                    "country": country, "forecast": ev.get("forecast", "N/A"),
                    "previous": ev.get("previous", "N/A")
                })
        _events_cache["data"] = important[:6]
        _events_cache["time"] = time.time()
        return important[:6]
    except Exception as e:
        print("Calendar error: " + str(e))
        return _events_cache["data"] if _events_cache["data"] else []


# ── FRED API — DONNEES MACRO OFFICIELLES ─────────────────────────

def get_fred_data():
    now = time.time()
    if cached_fred["data"] and (now - cached_fred["time"]) < 3600:
        return cached_fred["data"]

    result = {}
    key = FRED_KEY if FRED_KEY else "demo"

    series = {
        "FEDFUNDS": "Taux Fed",
        "CPIAUCSL": "CPI inflation",
        "PCEPI": "PCE inflation (prefere Fed)",
        "DGS10": "Taux US 10 ans",
        "DGS2": "Taux US 2 ans",
        "DTWEXBGS": "Dollar trade-weighted",
        "GOLDAMGBD228NLBM": "Prix or London Fix",
        "UNRATE": "Taux chomage",
    }

    for series_id, name in series.items():
        try:
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 2
                },
                timeout=8
            )
            d = r.json()
            obs = d.get("observations", [])
            if obs:
                latest = obs[0]
                previous = obs[1] if len(obs) > 1 else obs[0]
                val = latest.get("value", ".")
                prev_val = previous.get("value", ".")
                if val != ".":
                    result[series_id] = {
                        "name": name,
                        "value": val,
                        "previous": prev_val,
                        "date": latest.get("date", "")
                    }
        except:
            pass

    cached_fred["data"] = result
    cached_fred["time"] = now
    return result


def format_fred_data(fred):
    if not fred:
        return "Donnees FRED indisponibles (ajoutez FRED_KEY dans Railway)"
    text = ""
    for sid, data in fred.items():
        try:
            val = float(data["value"])
            prev = float(data["previous"]) if data["previous"] != "." else val
            chg = val - prev
            chg_str = ("+" if chg >= 0 else "") + str(round(chg, 3))
            text += data["name"] + ": `" + str(round(val, 3)) + "` (" + chg_str + ")\n"
        except:
            text += data["name"] + ": `" + data["value"] + "`\n"
    return text.strip()


def interpret_fred_for_gold(fred):
    signals = []
    interpretation = []

    if "FEDFUNDS" in fred and "DGS10" in fred:
        try:
            fed_rate = float(fred["FEDFUNDS"]["value"])
            t10 = float(fred["DGS10"]["value"])
            t2 = float(fred.get("DGS2", {}).get("value", t10))
            spread = t10 - t2
            if spread < 0:
                signals.append("bull")
                interpretation.append("Courbe inversee (spread 2-10y: " + str(round(spread, 2)) + "%) = recession anticipee = haussier or")
            if fed_rate > 4.5:
                signals.append("bear")
                interpretation.append("Taux Fed eleve (" + str(round(fed_rate, 2)) + "%) = dollar fort = pression baissiere or")
            elif fed_rate < 3.0:
                signals.append("bull")
                interpretation.append("Taux Fed bas (" + str(round(fed_rate, 2)) + "%) = dollar faible = soutien or")
        except:
            pass

    if "CPIAUCSL" in fred:
        try:
            cpi_val = float(fred["CPIAUCSL"]["value"])
            cpi_prev = float(fred["CPIAUCSL"]["previous"])
            if cpi_val > cpi_prev:
                signals.append("bull")
                interpretation.append("CPI en hausse = inflation accelere = demande or hausse")
            else:
                signals.append("bear")
                interpretation.append("CPI en baisse = desinflation = moins de demande or")
        except:
            pass

    bull = signals.count("bull")
    bear = signals.count("bear")
    if bull > bear:
        return "bull", interpretation
    elif bear > bull:
        return "bear", interpretation
    return "neut", interpretation


# ── COT REPORT (CFTC) ─────────────────────────────────────────────

def get_cot_report():
    now = time.time()
    if cached_cot["data"] and (now - cached_cot["time"]) < 86400:
        return cached_cot["data"]

    try:
        r = requests.get(
            "https://publicreporting.cftc.gov/api/odata/v1/MarketsandPrices/DisaggregatedFuturesOnlyReports",
            params={
                "$filter": "Market_and_Exchange_Names eq 'GOLD - COMMODITY EXCHANGE INC.' and strContains(Report_Date_as_MM_DD_YYYY, '" + str(datetime.now().year) + "')",
                "$orderby": "Report_Date_as_MM_DD_YYYY desc",
                "$top": "2",
                "$format": "json"
            },
            timeout=15
        )
        d = r.json()
        records = d.get("value", [])
        if records:
            latest = records[0]
            prod_longs = int(latest.get("Prod_Merc_Positions_Long_All", 0))
            prod_shorts = int(latest.get("Prod_Merc_Positions_Short_All", 0))
            mm_longs = int(latest.get("M_Money_Positions_Long_All", 0))
            mm_shorts = int(latest.get("M_Money_Positions_Short_All", 0))
            mm_net = mm_longs - mm_shorts
            mm_net_prev = 0
            if len(records) > 1:
                prev = records[1]
                mm_net_prev = int(prev.get("M_Money_Positions_Long_All", 0)) - int(prev.get("M_Money_Positions_Short_All", 0))
            mm_chg = mm_net - mm_net_prev
            date_str = latest.get("Report_Date_as_MM_DD_YYYY", "N/A")
            cot_data = {
                "date": date_str,
                "mm_longs": mm_longs,
                "mm_shorts": mm_shorts,
                "mm_net": mm_net,
                "mm_net_change": mm_chg,
                "prod_longs": prod_longs,
                "prod_shorts": prod_shorts,
                "signal": "bull" if mm_net > 0 and mm_chg > 0 else ("bear" if mm_net < 0 else "neut")
            }
            cached_cot["data"] = cot_data
            cached_cot["time"] = now
            return cot_data
    except Exception as e:
        print("COT error: " + str(e))
    return None


def format_cot(cot):
    if not cot:
        return "Donnees COT indisponibles"
    signal_txt = "HAUSSIER" if cot["signal"] == "bull" else ("BAISSIER" if cot["signal"] == "bear" else "NEUTRE")
    chg_str = ("+" if cot["mm_net_change"] >= 0 else "") + str(cot["mm_net_change"])
    return (
        "Date rapport: " + cot["date"] + "\n"
        "Money Managers LONG: `" + str(cot["mm_longs"]) + "`\n"
        "Money Managers SHORT: `" + str(cot["mm_shorts"]) + "`\n"
        "Positionnement NET: `" + str(cot["mm_net"]) + "` (" + chg_str + " vs semaine prev)\n"
        "Signal COT: *" + signal_txt + "*"
    )


# ── FEAR & GREED INDEX ────────────────────────────────────────────

def get_fear_greed():
    now = time.time()
    if cached_fear_greed["data"] and (now - cached_fear_greed["time"]) < 3600:
        return cached_fear_greed["data"]
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=2", timeout=8)
        d = r.json()
        data = d.get("data", [])
        if data:
            current = data[0]
            value = int(current.get("value", 50))
            classification = current.get("value_classification", "Neutral")
            signal = "bull" if value < 30 else ("bear" if value > 70 else "neut")
            result = {
                "value": value,
                "classification": classification,
                "signal": signal
            }
            cached_fear_greed["data"] = result
            cached_fear_greed["time"] = now
            return result
    except Exception as e:
        print("Fear & Greed error: " + str(e))
    return None


# ── FINNHUB NEWS ──────────────────────────────────────────────────

def get_real_news():
    try:
        news_items = []
        gold_keywords = ["gold", "xau", "fed", "federal reserve", "inflation", "cpi",
                        "dollar", "dxy", "interest rate", "powell", "fomc", "treasury",
                        "recession", "gdp", "nfp", "iran", "war", "china", "tariff", "oil", "silver"]
        for cat in ["general", "forex", "economy"]:
            try:
                r = requests.get("https://finnhub.io/api/v1/news",
                    params={"category": cat, "token": FINNHUB_KEY}, timeout=8)
                items = r.json()
                if isinstance(items, list):
                    for item in items[:30]:
                        headline = item.get("headline", "").lower()
                        summary = item.get("summary", "").lower()
                        if any(k in headline or k in summary for k in gold_keywords):
                            ts = item.get("datetime", 0)
                            if ts:
                                age_hours = (time.time() - ts) / 3600
                                if age_hours <= 24:
                                    news_items.append({
                                        "headline": item.get("headline", ""),
                                        "summary": item.get("summary", "")[:200],
                                        "source": item.get("source", ""),
                                        "age_hours": round(age_hours, 1)
                                    })
            except:
                continue
        news_items.sort(key=lambda x: x["age_hours"])
        seen = set()
        unique = []
        for item in news_items:
            h = item["headline"][:50]
            if h not in seen:
                seen.add(h)
                unique.append(item)
        return unique[:8]
    except Exception as e:
        print("News error: " + str(e))
        return []


def get_forex_sentiment():
    try:
        r = requests.get("https://finnhub.io/api/v1/news-sentiment",
            params={"symbol": "GLD", "token": FINNHUB_KEY}, timeout=8)
        d = r.json()
        if d and "sentiment" in d:
            score = d["sentiment"].get("bearishPercent", 0.5)
            if score > 0.6:
                return "BAISSIER", round(score * 100)
            elif score < 0.4:
                return "HAUSSIER", round((1 - score) * 100)
            return "NEUTRE", 50
    except:
        pass
    return None, None


def format_news_for_claude(news_items):
    if not news_items:
        return "Aucune actualite recente."
    text = ""
    for i, item in enumerate(news_items[:6]):
        text += str(i+1) + ". [" + item["source"] + " - " + str(item["age_hours"]) + "h]\n"
        text += item["headline"] + "\n"
        if item["summary"]:
            text += item["summary"][:150] + "\n"
        text += "\n"
    return text.strip()


# ── MATH INDICATORS ───────────────────────────────────────────────

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
    k = 50 if h == l else round((cl[-1] - l) / (h - l) * 100, 2)
    return {"k": k, "d": round(k * 0.88 + 6, 2)}


def calc_bb(a, n=20):
    sl = last_n(a, n)
    m = sum(sl) / len(sl)
    sd = (sum((v - m) ** 2 for v in sl) / len(sl)) ** 0.5
    return {"upper": round(m + 2 * sd, 2), "mid": round(m, 2), "lower": round(m - 2 * sd, 2)}


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
    sl = sorted(last_n(cl, 60))
    sup = round(sl[int(len(sl) * 0.1)], 2)
    res = round(sl[int(len(sl) * 0.9)], 2)
    pivot = round((hi[-1] + lo[-1] + cl[-1]) / 3, 2) if hi and lo and cl else 0
    r1 = round(2 * pivot - lo[-1], 2) if hi and lo else 0
    s1 = round(2 * pivot - hi[-1], 2) if hi and lo else 0
    r2 = round(pivot + (hi[-1] - lo[-1]), 2) if hi and lo else 0
    s2 = round(pivot - (hi[-1] - lo[-1]), 2) if hi and lo else 0
    return {"sup": sup, "res": res, "pivot": pivot, "r1": r1, "r2": r2, "s1": s1, "s2": s2}


def calc_momentum(cl, n=10):
    if len(cl) < n + 1:
        return 0
    return round(((cl[-1] - cl[-1-n]) / cl[-1-n]) * 100, 3)


# ── KAMA (Adaptive Moving Average) ───────────────────────────────

def calc_kama(cl, n=10, fast=2, slow=30):
    if len(cl) < n + 1:
        return cl[-1]
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    kama_val = cl[0]
    for i in range(1, len(cl)):
        if i < n:
            kama_val = cl[i]
            continue
        direction = abs(cl[i] - cl[i-n])
        volatility = sum(abs(cl[j] - cl[j-1]) for j in range(i-n+1, i+1))
        er = direction / volatility if volatility != 0 else 0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama_val = kama_val + sc * (cl[i] - kama_val)
    return round(kama_val, 2)


# ── SUPERTREND ────────────────────────────────────────────────────

def calc_supertrend(cl, hi, lo, period=10, multiplier=3.0):
    if len(cl) < period + 1:
        return {"value": cl[-1], "signal": "neut"}
    atr_val = calc_atr(cl, hi, lo, period)
    hl2 = (hi[-1] + lo[-1]) / 2
    upper = hl2 + multiplier * atr_val
    lower = hl2 - multiplier * atr_val
    if cl[-1] > upper:
        return {"value": round(lower, 2), "signal": "bull"}
    elif cl[-1] < lower:
        return {"value": round(upper, 2), "signal": "bear"}
    return {"value": round(hl2, 2), "signal": "neut"}


# ── ICHIMOKU ──────────────────────────────────────────────────────

def calc_ichimoku(cl, hi, lo):
    if len(cl) < 52:
        return None
    def donchian(h, l, n):
        return (max(h[-n:]) + min(l[-n:])) / 2
    tenkan = donchian(hi, lo, 9)
    kijun = donchian(hi, lo, 26)
    senkou_a = (tenkan + kijun) / 2
    senkou_b = donchian(hi, lo, 52)
    price = cl[-1]
    cloud_top = max(senkou_a, senkou_b)
    cloud_bot = min(senkou_a, senkou_b)
    if price > cloud_top:
        ichimoku_signal = "bull"
        ichimoku_label = "Prix AU-DESSUS du nuage"
    elif price < cloud_bot:
        ichimoku_signal = "bear"
        ichimoku_label = "Prix EN-DESSOUS du nuage"
    else:
        ichimoku_signal = "neut"
        ichimoku_label = "Prix DANS le nuage"
    tenkan_kijun = "bull" if tenkan > kijun else ("bear" if tenkan < kijun else "neut")
    return {
        "tenkan": round(tenkan, 2), "kijun": round(kijun, 2),
        "senkou_a": round(senkou_a, 2), "senkou_b": round(senkou_b, 2),
        "cloud_top": round(cloud_top, 2), "cloud_bot": round(cloud_bot, 2),
        "signal": ichimoku_signal, "label": ichimoku_label,
        "tk_signal": tenkan_kijun
    }


# ── RSI/MACD DIVERGENCES ──────────────────────────────────────────

def detect_divergences(cl, hi, lo, rsi_values=None):
    divergences = []
    if len(cl) < 30:
        return divergences
    lookback = 20
    recent_cl = cl[-lookback:]
    if rsi_values is None:
        rsi_series = []
        for i in range(14, len(cl)):
            rsi_series.append(calc_rsi(cl[:i+1]))
        rsi_recent = rsi_series[-lookback:] if len(rsi_series) >= lookback else rsi_series
    else:
        rsi_recent = rsi_values[-lookback:]
    if len(recent_cl) >= 10 and len(rsi_recent) >= 10:
        price_lows_idx = []
        for i in range(2, len(recent_cl) - 2):
            if recent_cl[i] == min(recent_cl[i-2:i+3]):
                price_lows_idx.append(i)
        if len(price_lows_idx) >= 2:
            i1, i2 = price_lows_idx[-2], price_lows_idx[-1]
            if i2 < len(rsi_recent) and i1 < len(rsi_recent):
                if recent_cl[i2] < recent_cl[i1] and rsi_recent[i2] > rsi_recent[i1]:
                    divergences.append({
                        "type": "DIVERGENCE HAUSSIERE RSI",
                        "dir": "bull",
                        "desc": "Prix fait un lower low mais RSI fait un higher low"
                    })
        price_highs_idx = []
        for i in range(2, len(recent_cl) - 2):
            if recent_cl[i] == max(recent_cl[i-2:i+3]):
                price_highs_idx.append(i)
        if len(price_highs_idx) >= 2:
            i1, i2 = price_highs_idx[-2], price_highs_idx[-1]
            if i2 < len(rsi_recent) and i1 < len(rsi_recent):
                if recent_cl[i2] > recent_cl[i1] and rsi_recent[i2] < rsi_recent[i1]:
                    divergences.append({
                        "type": "DIVERGENCE BAISSIERE RSI",
                        "dir": "bear",
                        "desc": "Prix fait un higher high mais RSI fait un lower high"
                    })
    return divergences


# ── ORDER BLOCKS ──────────────────────────────────────────────────

def detect_order_blocks(opens, highs, lows, closes, n=30):
    order_blocks = []
    if len(closes) < n:
        return order_blocks
    op = opens[-n:]
    hi = highs[-n:]
    lo = lows[-n:]
    cl = closes[-n:]
    for i in range(1, len(cl) - 2):
        body = abs(cl[i] - op[i])
        next_move = abs(cl[i+1] - cl[i])
        if next_move > body * 1.5:
            if cl[i+1] > cl[i]:
                order_blocks.append({
                    "type": "BULLISH ORDER BLOCK",
                    "dir": "bull",
                    "high": round(max(op[i], cl[i]), 2),
                    "low": round(min(op[i], cl[i]), 2),
                    "desc": "Zone institutionnelle haussiere"
                })
            elif cl[i+1] < cl[i]:
                order_blocks.append({
                    "type": "BEARISH ORDER BLOCK",
                    "dir": "bear",
                    "high": round(max(op[i], cl[i]), 2),
                    "low": round(min(op[i], cl[i]), 2),
                    "desc": "Zone institutionnelle baissiere"
                })
    return order_blocks[-3:]


def price_in_order_block(price, order_blocks):
    for ob in order_blocks:
        if ob["low"] <= price <= ob["high"]:
            return ob
    return None


# ── EQUAL HIGHS/LOWS (LIQUIDITE) ─────────────────────────────────

def detect_liquidity_zones(highs, lows, closes, tolerance=0.002):
    liquidity = []
    if len(highs) < 20:
        return liquidity
    recent_h = highs[-30:]
    recent_l = lows[-30:]
    price = closes[-1]
    sorted_h = sorted(set(round(h, 0) for h in recent_h))
    for i in range(len(sorted_h) - 1):
        if abs(sorted_h[i+1] - sorted_h[i]) / sorted_h[i] < tolerance:
            liq_level = round((sorted_h[i] + sorted_h[i+1]) / 2, 2)
            dist_pct = abs(price - liq_level) / price
            liquidity.append({
                "level": liq_level, "type": "EQUAL HIGHS",
                "dir": "bear", "dist_pct": round(dist_pct * 100, 2),
                "desc": "Zone de liquidite au-dessus"
            })
    sorted_l = sorted(set(round(l, 0) for l in recent_l))
    for i in range(len(sorted_l) - 1):
        if abs(sorted_l[i+1] - sorted_l[i]) / sorted_l[i] < tolerance:
            liq_level = round((sorted_l[i] + sorted_l[i+1]) / 2, 2)
            dist_pct = abs(price - liq_level) / price
            liquidity.append({
                "level": liq_level, "type": "EQUAL LOWS",
                "dir": "bull", "dist_pct": round(dist_pct * 100, 2),
                "desc": "Zone de liquidite en-dessous"
            })
    liquidity.sort(key=lambda x: x["dist_pct"])
    return liquidity[:4]


# ── FIBONACCI ─────────────────────────────────────────────────────

def calc_fibonacci(highs, lows, n=50):
    swing_high = max(highs[-n:])
    swing_low = min(lows[-n:])
    diff = swing_high - swing_low
    return {
        "swing_high": round(swing_high, 2), "swing_low": round(swing_low, 2),
        "fib_0": round(swing_high, 2), "fib_236": round(swing_high - diff * 0.236, 2),
        "fib_382": round(swing_high - diff * 0.382, 2), "fib_500": round(swing_high - diff * 0.500, 2),
        "fib_618": round(swing_high - diff * 0.618, 2), "fib_786": round(swing_high - diff * 0.786, 2),
        "fib_100": round(swing_low, 2)
    }


def find_nearest_fib(price, fib):
    levels = [("0%", fib["fib_0"]), ("23.6%", fib["fib_236"]), ("38.2%", fib["fib_382"]),
              ("50%", fib["fib_500"]), ("61.8%", fib["fib_618"]), ("78.6%", fib["fib_786"]),
              ("100%", fib["fib_100"])]
    nearest = min(levels, key=lambda x: abs(x[1] - price))
    distance = abs(nearest[1] - price)
    atr_est = abs(fib["swing_high"] - fib["swing_low"]) * 0.05
    return nearest[0], nearest[1], distance < atr_est


# ── MARKET STRUCTURE ──────────────────────────────────────────────

def detect_market_structure(highs, lows, closes, n=20):
    if len(closes) < n * 2:
        return "INDEFINI", [], []
    pivot_highs = []
    pivot_lows = []
    window = 5
    for i in range(window, len(highs) - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            pivot_highs.append((i, highs[i]))
        if lows[i] == min(lows[i-window:i+window+1]):
            pivot_lows.append((i, lows[i]))
    structure = "INDEFINI"
    if len(pivot_highs) >= 2 and len(pivot_lows) >= 2:
        last_highs = pivot_highs[-3:]
        last_lows = pivot_lows[-3:]
        if len(last_highs) >= 2 and len(last_lows) >= 2:
            hh = last_highs[-1][1] > last_highs[-2][1]
            hl = last_lows[-1][1] > last_lows[-2][1]
            lh = last_highs[-1][1] < last_highs[-2][1]
            ll = last_lows[-1][1] < last_lows[-2][1]
            if hh and hl: structure = "HAUSSIERE (HH+HL)"
            elif lh and ll: structure = "BAISSIERE (LH+LL)"
            elif hh and ll: structure = "MIXTE"
            else: structure = "RANGE"
    return structure, pivot_highs[-5:], pivot_lows[-5:]


# ── CANDLE PATTERNS ───────────────────────────────────────────────

def detect_candle_patterns(opens, highs, lows, closes):
    patterns = []
    n = len(closes)
    if n < 3:
        return patterns
    o1, h1, l1, c1 = opens[-1], highs[-1], lows[-1], closes[-1]
    o2, h2, l2, c2 = opens[-2], highs[-2], lows[-2], closes[-2]
    body1 = abs(c1 - o1)
    body2 = abs(c2 - o2)
    range1 = h1 - l1
    upper_shadow1 = h1 - max(o1, c1)
    lower_shadow1 = min(o1, c1) - l1
    bull1 = c1 > o1
    bear1 = c1 < o1
    bull2 = c2 > o2
    bear2 = c2 < o2
    if lower_shadow1 >= body1 * 2 and upper_shadow1 <= body1 * 0.3 and range1 > 0:
        patterns.append({"name": "MARTEAU", "dir": "bull", "strength": 2, "desc": "Retournement haussier"})
    if upper_shadow1 >= body1 * 2 and lower_shadow1 <= body1 * 0.3 and range1 > 0:
        patterns.append({"name": "ETOILE FILANTE", "dir": "bear", "strength": 2, "desc": "Retournement baissier"})
    if body1 <= range1 * 0.1 and range1 > 0:
        patterns.append({"name": "DOJI", "dir": "neut", "strength": 1, "desc": "Indecision"})
    if bear2 and bull1 and c1 > o2 and o1 < c2 and body1 > body2:
        patterns.append({"name": "ENGULFING HAUSSIER", "dir": "bull", "strength": 3, "desc": "Fort retournement haussier"})
    if bull2 and bear1 and c1 < o2 and o1 > c2 and body1 > body2:
        patterns.append({"name": "ENGULFING BAISSIER", "dir": "bear", "strength": 3, "desc": "Fort retournement baissier"})
    if n >= 3:
        o3, h3, l3, c3 = opens[-3], highs[-3], lows[-3], closes[-3]
        if bear2 and body1 > 0 and abs(c3 - o3) < abs(c2 - o2) * 0.5 and bull1 and c1 > (o2 + c2) / 2:
            patterns.append({"name": "MORNING STAR", "dir": "bull", "strength": 3, "desc": "Fort retournement haussier 3 bougies"})
        if bull2 and body1 > 0 and abs(c3 - o3) < abs(c2 - o2) * 0.5 and bear1 and c1 < (o2 + c2) / 2:
            patterns.append({"name": "EVENING STAR", "dir": "bear", "strength": 3, "desc": "Fort retournement baissier 3 bougies"})
    return patterns


# ── COMPUTE FULL INDICATORS ───────────────────────────────────────

def compute_indicators(closes, highs, lows, opens=None, volumes=None):
    price = closes[-1]
    op = opens if opens else closes

    patterns = detect_candle_patterns(op, highs, lows, closes)
    pat_score = sum(p["strength"] * (1 if p["dir"] == "bull" else -1 if p["dir"] == "bear" else 0) for p in patterns)
    pat_dir = "bull" if pat_score > 0 else ("bear" if pat_score < 0 else "neut")

    structure, ph, pl = detect_market_structure(highs, lows, closes)
    fib = calc_fibonacci(highs, lows)
    ichimoku = calc_ichimoku(closes, highs, lows)
    supertrend = calc_supertrend(closes, highs, lows)
    kama_val = calc_kama(closes)
    divergences = detect_divergences(closes, highs, lows)
    order_blocks = detect_order_blocks(op, highs, lows, closes)
    liquidity = detect_liquidity_zones(highs, lows, closes)

    return {
        "price": price,
        "rsi": calc_rsi(closes),
        "macd": calc_macd(closes),
        "e20": ema(last_n(closes, 20), 20),
        "e50": ema(last_n(closes, 50), 50),
        "e200": ema(last_n(closes, 200), 200),
        "kama": kama_val,
        "adx": calc_adx(closes),
        "bb": calc_bb(closes),
        "stoch": calc_stoch(closes, highs, lows),
        "cci": calc_cci(closes),
        "wr": calc_wr(closes, highs, lows),
        "atr": calc_atr(closes, highs, lows),
        "psar": calc_psar(closes, highs, lows),
        "res": calc_supres(closes, highs, lows),
        "momentum": calc_momentum(closes),
        "high24": round(max(last_n(highs, 24)), 2),
        "low24": round(min(last_n(lows, 24)), 2),
        "patterns": patterns,
        "pat_dir": pat_dir,
        "pat_score": abs(pat_score),
        "structure": structure,
        "fib": fib,
        "ichimoku": ichimoku,
        "supertrend": supertrend,
        "kama_signal": "bull" if price > kama_val else "bear",
        "divergences": divergences,
        "order_blocks": order_blocks,
        "liquidity": liquidity,
        "ob_current": price_in_order_block(price, order_blocks),
    }


# ── CORRELATION ANALYSIS ──────────────────────────────────────────

def analyze_correlations(corr):
    signals = []
    analysis = []
    if "DXY" in corr:
        pct = corr["DXY"]["pct"]
        if pct > 0.3: signals.append("bear"); analysis.append("Dollar fort (DXY +" + str(round(pct, 2)) + "%) = pression baissiere or")
        elif pct < -0.3: signals.append("bull"); analysis.append("Dollar faible (DXY " + str(round(pct, 2)) + "%) = soutien or")
    if "WTI" in corr:
        pct = corr["WTI"]["pct"]
        if pct > 1.0: signals.append("bull"); analysis.append("Petrole hausse = inflation = soutien or")
        elif pct < -1.0: signals.append("bear"); analysis.append("Petrole baisse = deflation = pression or")
    if "BONDS" in corr:
        pct = corr["BONDS"]["pct"]
        if pct > 0.3: signals.append("bull"); analysis.append("Obligations hausse = taux reels baisse = or haussier")
        elif pct < -0.3: signals.append("bear"); analysis.append("Obligations baisse = taux reels hausse = or baissier")
    if "SPX" in corr:
        pct = corr["SPX"]["pct"]
        if pct < -1.0: signals.append("bull"); analysis.append("Bourse baisse = fuite vers or")
        elif pct > 1.0: signals.append("bear"); analysis.append("Bourse hausse = risk-on = moins de demande or")
    if "VIX" in corr:
        vix = corr["VIX"]["price"]
        if vix > 25: signals.append("bull"); analysis.append("VIX eleve (" + str(round(vix, 1)) + ") = panique = refuge or")
    if "SILVER" in corr:
        pct = corr["SILVER"]["pct"]
        if pct > 0.5: signals.append("bull"); analysis.append("Argent hausse = metaux precieux haussiers")
        elif pct < -0.5: signals.append("bear"); analysis.append("Argent baisse = metaux precieux baissiers")
    bull = signals.count("bull")
    bear = signals.count("bear")
    if bull > bear: return "bull", analysis
    elif bear > bull: return "bear", analysis
    return "neut", analysis


def format_correlations(corr, analysis):
    if not corr:
        return "  Donnees indisponibles\n"
    text = ""
    for name, key in [("DXY", "DXY"), ("WTI", "WTI"), ("TLT", "BONDS"), ("S&P", "SPX"), ("VIX", "VIX"), ("Argent", "SILVER")]:
        if key in corr:
            pct = corr[key]["pct"]
            text += "  " + name + ": `" + str(round(corr[key]["price"], 2)) + "` (" + ("+" if pct >= 0 else "") + str(round(pct, 2)) + "%)\n"
    if analysis:
        text += "\n"
        for a in analysis[:2]:
            text += "  - " + a + "\n"
    return text


# ── SIGNAL ENGINE ─────────────────────────────────────────────────

def build_signal(price, ind, corr_signal="neut", struct_signal="neut", cot_signal="neut", fred_signal="neut", fg_signal="neut"):
    S = []
    def p(name, d, w, label):
        S.append({"name": name, "dir": d, "w": w, "label": label})

    rv = ind["rsi"]
    if rv < 28: p("RSI", "bull", 3, "RSI survente extreme (" + str(rv) + ")")
    elif rv < 40: p("RSI", "bull", 2, "RSI survente (" + str(rv) + ")")
    elif rv > 72: p("RSI", "bear", 3, "RSI surachat extreme (" + str(rv) + ")")
    elif rv > 60: p("RSI", "bear", 2, "RSI surachat (" + str(rv) + ")")
    else: p("RSI", "neut", 1, "RSI neutre (" + str(rv) + ")")

    mh = ind["macd"]["hist"]
    mm = ind["macd"]["macd"]
    if mh > 0 and mm > 0: p("MACD", "bull", 2, "MACD haussier")
    elif mh > 0: p("MACD", "bull", 1, "MACD croise hausse")
    elif mh < 0 and mm < 0: p("MACD", "bear", 2, "MACD baissier")
    else: p("MACD", "bear", 1, "MACD croise baisse")

    e20, e50, e200 = ind["e20"], ind["e50"], ind["e200"]
    if e20 > e50 and e50 > e200: p("EMA", "bull", 3, "EMA 20>50>200 tendance haussiere forte")
    elif e20 > e50: p("EMA", "bull", 2, "EMA 20>50 haussier")
    elif e20 < e50 and e50 < e200: p("EMA", "bear", 3, "EMA 20<50<200 tendance baissiere forte")
    else: p("EMA", "bear", 2, "EMA 20<50 baissier")

    if ind.get("kama_signal") == "bull": p("KAMA", "bull", 2, "Prix > KAMA (MA adaptative) haussier")
    elif ind.get("kama_signal") == "bear": p("KAMA", "bear", 2, "Prix < KAMA (MA adaptative) baissier")

    adxv = ind["adx"]["adx"]
    dip = ind["adx"]["diP"]
    din = ind["adx"]["diN"]
    if adxv > 30 and dip > din: p("ADX", "bull", 2, "Tendance haussiere forte (ADX=" + str(adxv) + ")")
    elif adxv > 30: p("ADX", "bear", 2, "Tendance baissiere forte (ADX=" + str(adxv) + ")")
    else: p("ADX", "neut", 1, "Marche sans tendance")

    bbu = ind["bb"]["upper"]
    bbl = ind["bb"]["lower"]
    bbm = ind["bb"]["mid"]
    if price < bbl: p("BB", "bull", 2, "Prix sous bande basse BB")
    elif price > bbu: p("BB", "bear", 2, "Prix sur bande haute BB")
    elif price < bbm: p("BB", "bull", 1, "Prix sous moyenne BB")
    else: p("BB", "bear", 1, "Prix sur moyenne BB")

    sk = ind["stoch"]["k"]
    sd_v = ind["stoch"]["d"]
    if sk < 20 and sd_v < 20: p("STOCH", "bull", 2, "Stochastique survente")
    elif sk > 80 and sd_v > 80: p("STOCH", "bear", 2, "Stochastique surachat")
    elif sk > sd_v: p("STOCH", "bull", 1, "Stoch K>D haussier")
    else: p("STOCH", "neut", 1, "Stochastique neutre")

    cc = ind["cci"]
    if cc < -100: p("CCI", "bull", 2, "CCI survente")
    elif cc > 100: p("CCI", "bear", 2, "CCI surachat")
    else: p("CCI", "neut", 1, "CCI neutre")

    wrv = ind["wr"]
    if wrv < -80: p("WR", "bull", 2, "Williams survente")
    elif wrv > -20: p("WR", "bear", 2, "Williams surachat")
    else: p("WR", "neut", 1, "Williams neutre")

    rng = ind["res"]["res"] - ind["res"]["sup"]
    pos = (price - ind["res"]["sup"]) / (rng or 1)
    if pos < 0.15: p("SR", "bull", 2, "Proche support " + str(ind["res"]["sup"]))
    elif pos > 0.85: p("SR", "bear", 2, "Proche resistance " + str(ind["res"]["res"]))
    else: p("SR", "neut", 1, "Zone mediane S/R")

    if price > ind["psar"]: p("SAR", "bull", 1, "Prix > SAR")
    else: p("SAR", "bear", 1, "Prix < SAR")

    mom = ind["momentum"]
    if mom > 0.3: p("MOM", "bull", 1, "Momentum haussier")
    elif mom < -0.3: p("MOM", "bear", 1, "Momentum baissier")

    ichi = ind.get("ichimoku")
    if ichi:
        if ichi["signal"] == "bull": p("ICHIMOKU", "bull", 3, "Ichimoku: prix au-dessus du nuage")
        elif ichi["signal"] == "bear": p("ICHIMOKU", "bear", 3, "Ichimoku: prix en-dessous du nuage")
        if ichi["tk_signal"] == "bull": p("ICHIMOKU_TK", "bull", 1, "Ichimoku: Tenkan > Kijun haussier")
        elif ichi["tk_signal"] == "bear": p("ICHIMOKU_TK", "bear", 1, "Ichimoku: Tenkan < Kijun baissier")

    st = ind.get("supertrend")
    if st:
        if st["signal"] == "bull": p("SUPERTREND", "bull", 2, "Supertrend: tendance haussiere confirmee")
        elif st["signal"] == "bear": p("SUPERTREND", "bear", 2, "Supertrend: tendance baissiere confirmee")

    pat_dir = ind.get("pat_dir", "neut")
    pat_score = ind.get("pat_score", 0)
    if pat_dir == "bull" and pat_score >= 2: p("PATTERNS", "bull", min(pat_score, 3), "Patterns bougies haussiers")
    elif pat_dir == "bear" and pat_score >= 2: p("PATTERNS", "bear", min(pat_score, 3), "Patterns bougies baissiers")

    for div in ind.get("divergences", []):
        if div["dir"] == "bull": p("DIV", "bull", 3, div["type"])
        elif div["dir"] == "bear": p("DIV", "bear", 3, div["type"])

    ob = ind.get("ob_current")
    if ob:
        if ob["dir"] == "bull": p("OB", "bull", 2, "Prix dans un Order Block haussier")
        elif ob["dir"] == "bear": p("OB", "bear", 2, "Prix dans un Order Block baissier")

    if corr_signal == "bull": p("CORR", "bull", 2, "Correlations favorables (Dollar, Petrole, Bourse)")
    elif corr_signal == "bear": p("CORR", "bear", 2, "Correlations defavorables")

    struct_sig_dir, _ = ("bull", 3) if "HAUSSIERE" in (ind.get("structure", "")) else (("bear", 3) if "BAISSIERE" in (ind.get("structure", "")) else ("neut", 0))
    if struct_sig_dir == "bull": p("STRUCT", "bull", 3, "Structure HH+HL haussiere")
    elif struct_sig_dir == "bear": p("STRUCT", "bear", 3, "Structure LH+LL baissiere")

    fib = ind.get("fib")
    if fib:
        nearest_name, nearest_val, is_near = find_nearest_fib(price, fib)
        if is_near and ("61.8" in nearest_name or "50" in nearest_name or "38.2" in nearest_name):
            p("FIB", "bull", 2, "Prix sur niveau Fibonacci cle " + nearest_name)

    if cot_signal == "bull": p("COT", "bull", 3, "COT: Hedge funds positionnement NET long or")
    elif cot_signal == "bear": p("COT", "bear", 3, "COT: Hedge funds positionnement NET short or")

    if fred_signal == "bull": p("FRED", "bull", 2, "Macro FRED: environnement favorable or")
    elif fred_signal == "bear": p("FRED", "bear", 2, "Macro FRED: environnement defavorable or")

    if fg_signal == "bull": p("FG", "bull", 2, "Fear & Greed: Extreme Fear = opportunite achat or")
    elif fg_signal == "bear": p("FG", "bear", 1, "Fear & Greed: Extreme Greed = prudence")

    bW = sum(s["w"] for s in S if s["dir"] == "bull")
    rW = sum(s["w"] for s in S if s["dir"] == "bear")
    tot = bW + rW or 1
    ratio = bW / tot

    if ratio >= 0.60: sig, conf = "BUY", round(ratio * 100)
    elif ratio <= 0.40: sig, conf = "SELL", round((1 - ratio) * 100)
    else: sig, conf = "NEUTRE", round(max(ratio, 1 - ratio) * 100)

    a = ind["atr"]
    tp1 = tp2 = tp3 = sl = rr = None
    entry = price
    entry_zone_low = entry_zone_high = price

    # TP INTRADAY: trades fermes dans la meme session (3-6h max)
    # TP1 = 0.5 ATR (~1h) | TP2 = 1.0 ATR (~2h) | TP3 = 1.5 ATR (~3-4h)
    # SL  = 0.6 ATR (serre mais raisonnable, R/R ~1.67)
    if sig == "BUY":
        entry_zone_low = round(price - a * 0.2, 2)
        entry_zone_high = round(price + a * 0.1, 2)
        tp1 = round(price + a * 0.5, 2)
        tp2 = round(price + a * 1.0, 2)
        tp3 = round(price + a * 1.5, 2)
        sl = round(price - a * 0.6, 2)
    if sig == "SELL":
        entry_zone_low = round(price - a * 0.1, 2)
        entry_zone_high = round(price + a * 0.2, 2)
        tp1 = round(price - a * 0.5, 2)
        tp2 = round(price - a * 1.0, 2)
        tp3 = round(price - a * 1.5, 2)
        sl = round(price + a * 0.6, 2)

    if tp2 and sl:
        g = abs(tp2 - price)
        r = abs(sl - price)
        rr = round(g / r, 2) if r > 0 else None
        # R/R minimum 1.5 pour valider le trade
        if rr and rr < 1.2:
            sig = "NEUTRE"  # Signal invalide si R/R trop faible

    dir_filter = "bull" if sig == "BUY" else "bear"
    reasons = [s["label"] for s in S if s["dir"] == dir_filter][:8]

    return {
        "sig": sig, "conf": conf, "entry": entry,
        "entry_zone_low": entry_zone_low, "entry_zone_high": entry_zone_high,
        "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "rr": rr,
        "bW": bW, "rW": rW, "reasons": reasons, "total_indicators": len(S)
    }


def multi_timeframe_analysis(corr_signal="neut", struct_signal="neut", cot_signal="neut", fred_signal="neut", fg_signal="neut", h1_data=None):
    results = {}
    timeframes = [("1h", "H1"), ("4h", "H4"), ("1day", "D1")]
    for i, (interval, label) in enumerate(timeframes):
        try:
            if label == "H1" and h1_data is not None:
                # Reutiliser les donnees H1 deja recuperees
                closes, highs, lows, opens, times, volumes = h1_data
            else:
                # Attendre entre les appels pour respecter la limite API
                if i > 0:
                    time.sleep(20)
                # Retry logic si rate limit
                last_err = None
                for attempt in range(3):
                    try:
                        closes, highs, lows, opens, times, volumes = get_history(interval, 200)
                        last_err = None
                        break
                    except Exception as retry_err:
                        last_err = retry_err
                        err_msg = str(retry_err).lower()
                        if "credits" in err_msg or "limit" in err_msg or "429" in err_msg:
                            wait_time = 30 * (attempt + 1)
                            print("Rate limit sur " + label + " tentative " + str(attempt+1) + " - attente " + str(wait_time) + "s...")
                            time.sleep(wait_time)
                        else:
                            raise retry_err
                if last_err is not None:
                    raise last_err
            ind = compute_indicators(closes, highs, lows, opens, volumes)
            res = build_signal(ind["price"], ind, corr_signal, struct_signal, cot_signal, fred_signal, fg_signal)
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
    if buys > sells: return "BUY", round((buys / total) * 100)
    elif sells > buys: return "SELL", round((sells / total) * 100)
    return "NEUTRE", 50


def run_full_analysis():
    # OPTIMISE: APIs lentes en parallele, APIs rapides en serie
    import concurrent.futures

    # Resultats des APIs lentes
    slow_results = {"cot": None, "fred": {}, "fg": None, "news": [], "sentiment": (None, None)}

    def fetch_cot():
        try:
            slow_results["cot"] = get_cot_report()
        except:
            pass

    def fetch_fred():
        try:
            slow_results["fred"] = get_fred_data()
        except:
            pass

    def fetch_fg():
        try:
            slow_results["fg"] = get_fear_greed()
        except:
            pass

    def fetch_news():
        try:
            slow_results["news"] = get_real_news()
            slow_results["sentiment"] = get_forex_sentiment()
        except:
            pass

    # Lancer les APIs lentes en parallele
    threads = [
        threading.Thread(target=fetch_cot),
        threading.Thread(target=fetch_fred),
        threading.Thread(target=fetch_fg),
        threading.Thread(target=fetch_news),
    ]
    for t in threads:
        t.daemon = True
        t.start()

    # Pendant ce temps, faire les appels rapides Twelve Data
    closes, highs, lows, opens, times, volumes = get_history("1h", 200)

    try:
        q_raw = requests.get("https://api.twelvedata.com/quote",
            params={"symbol": SYMBOL, "apikey": TWELVE_KEY}, timeout=10)
        q_data = q_raw.json()
        price = float(q_data["close"])
        quote = {
            "price": price, "open": float(q_data["open"]),
            "high": float(q_data["high"]), "low": float(q_data["low"]),
            "change": float(q_data["change"]), "pct": float(q_data["percent_change"])
        }
    except:
        price = closes[-1]
        quote = {"price": price, "open": opens[-1], "high": highs[-1],
                "low": lows[-1], "change": 0, "pct": 0}

    corr = get_correlated_assets()
    corr_signal, corr_analysis = analyze_correlations(corr)

    ind_h1 = compute_indicators(closes, highs, lows, opens, volumes)

    # Attendre les APIs lentes max 8 secondes
    for t in threads:
        t.join(timeout=8)

    # Recuperer les resultats
    cot = slow_results["cot"]
    fred = slow_results["fred"]
    fg = slow_results["fg"]
    news = slow_results["news"]
    sentiment, sent_score = slow_results["sentiment"]

    cot_signal = cot["signal"] if cot else "neut"
    fred_signal, fred_interpretation = interpret_fred_for_gold(fred) if fred else ("neut", [])
    fg_signal = fg["signal"] if fg else "neut"

    print("APIs: COT=" + ("OK" if cot else "N/A") +
          " FRED=" + ("OK" if fred else "N/A") +
          " FG=" + ("OK" if fg else "N/A") +
          " News=" + str(len(news)))
    structure = ind_h1["structure"]
    struct_sig = "bull" if "HAUSSIERE" in structure else ("bear" if "BAISSIERE" in structure else "neut")

    h1_data = (closes, highs, lows, opens, times, volumes)
    mtf = multi_timeframe_analysis(corr_signal, struct_sig, cot_signal, fred_signal, fg_signal, h1_data)
    h1 = mtf.get("H1", {})
    ind = h1.get("ind") or ind_h1
    result = h1.get("result") or {}

    confluence_sig, confluence_conf = mtf_confluence(mtf)
    if result and result["sig"] == confluence_sig:
        result["conf"] = min(99, round((result["conf"] + confluence_conf) / 2 + 5))

    events = get_economic_events()
    dxy = get_dxy()
    session = get_session_info()

    return (price, quote, result, ind, mtf, events, dxy, news, sentiment, sent_score,
            corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg)


# ── CAPITAL MANAGEMENT ────────────────────────────────────────────

def calc_lot_size(capital, risk_pct, entry, sl):
    risk_amount = capital * (risk_pct / 100)
    sl_distance = abs(entry - sl)
    if sl_distance == 0:
        return 0.01
    lot_size = risk_amount / (sl_distance * 100)
    return max(0.01, round(lot_size, 2))


def format_capital_plan(capital, risk_pct, entry, sl, tp1, tp2, tp3):
    if not capital or not sl:
        return ""
    lot = calc_lot_size(capital, risk_pct, entry, sl)
    risk_amount = capital * (risk_pct / 100)
    sl_dist = abs(entry - sl)
    tp1_profit = round(abs(tp1 - entry) * lot * 100, 2) if tp1 else 0
    tp2_profit = round(abs(tp2 - entry) * lot * 100, 2) if tp2 else 0
    tp3_profit = round(abs(tp3 - entry) * lot * 100, 2) if tp3 else 0
    return (
        "*GESTION DU CAPITAL*\n"
        "Capital: `" + str(capital) + "$` | Risque: `" + str(risk_pct) + "%` = `" + str(round(risk_amount, 2)) + "$`\n"
        "Lot size: `" + str(lot) + "`\n"
        "SL distance: `" + str(round(sl_dist, 2)) + " pts`\n"
        "Gain TP1: `+" + str(tp1_profit) + "$`\n"
        "Gain TP2: `+" + str(tp2_profit) + "$`\n"
        "Gain TP3: `+" + str(tp3_profit) + "$`\n"
    )


# ── TRADE MONITOR ─────────────────────────────────────────────────

def register_trade(chat_id, sig, entry, tp1, tp2, tp3, sl):
    active_trades[chat_id] = {
        "sig": sig, "entry": entry,
        "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl,
        "tp1_hit": False, "tp2_hit": False, "tp3_hit": False, "sl_warned": False,
        "time": datetime.now().isoformat()
    }


def check_trade_alerts(chat_id, trade, price):
    sig = trade["sig"]
    alerts = []
    if sig == "BUY":
        if not trade["tp1_hit"] and trade["tp1"] and price >= trade["tp1"]:
            trade["tp1_hit"] = True
            alerts.append("TP1 ATTEINT ! Securise 30%.\nPrix: `" + str(round(price, 2)) + "`\nDeplace SL a l entree.")
        if not trade["tp2_hit"] and trade["tp2"] and price >= trade["tp2"]:
            trade["tp2_hit"] = True
            alerts.append("TP2 ATTEINT ! Objectif principal.\nFerme 50% ou tout.")
        if not trade["tp3_hit"] and trade["tp3"] and price >= trade["tp3"]:
            trade["tp3_hit"] = True
            alerts.append("TP3 ATTEINT ! Objectif maximum.\nFerme la totalite.")
        if not trade["sl_warned"] and trade["sl"]:
            dist = price - trade["sl"]
            atr_est = abs(trade["entry"] - trade["sl"]) / 1.2
            if dist < atr_est * 0.3:
                trade["sl_warned"] = True
                alerts.append("ATTENTION SL PROCHE !\nPrix: `" + str(round(price, 2)) + "` | SL: `" + str(trade["sl"]) + "`")
    elif sig == "SELL":
        if not trade["tp1_hit"] and trade["tp1"] and price <= trade["tp1"]:
            trade["tp1_hit"] = True
            alerts.append("TP1 ATTEINT ! Securise 30%.\nDeplace SL a l entree.")
        if not trade["tp2_hit"] and trade["tp2"] and price <= trade["tp2"]:
            trade["tp2_hit"] = True
            alerts.append("TP2 ATTEINT ! Ferme 50% ou tout.")
        if not trade["tp3_hit"] and trade["tp3"] and price <= trade["tp3"]:
            trade["tp3_hit"] = True
            alerts.append("TP3 ATTEINT ! Ferme la totalite.")
        if not trade["sl_warned"] and trade["sl"]:
            dist = trade["sl"] - price
            atr_est = abs(trade["sl"] - trade["entry"]) / 1.2
            if dist < atr_est * 0.3:
                trade["sl_warned"] = True
                alerts.append("ATTENTION SL PROCHE !\nPrix: `" + str(round(price, 2)) + "` | SL: `" + str(trade["sl"]) + "`")
    return alerts


def trade_monitor():
    print("Suivi trades demarre...")
    while True:
        try:
            if active_trades:
                q = get_quote()
                price = q["price"]
                for chat_id in list(active_trades.keys()):
                    trade = active_trades[chat_id]
                    for alert in check_trade_alerts(chat_id, trade, price):
                        send(chat_id, "SUIVI TRADE XAU/USD\n\n" + alert)
                    if trade.get("tp3_hit"):
                        del active_trades[chat_id]
        except Exception as e:
            print("Trade monitor error: " + str(e))
        time.sleep(TRADE_MONITOR_INTERVAL)


# ── PERFORMANCE ───────────────────────────────────────────────────

def record_signal(sig, conf, entry, tp2, sl):
    signal_history.append({
        "sig": sig, "conf": conf, "entry": entry,
        "tp": tp2, "sl": sl, "time": datetime.now().isoformat(),
        "outcome": "OPEN"
    })
    if len(signal_history) > 200:
        signal_history.pop(0)


def format_weekly_performance():
    if not signal_history:
        return "Aucun signal enregistre."
    week_ago = datetime.now() - timedelta(days=7)
    weekly = [s for s in signal_history if datetime.fromisoformat(s["time"]) > week_ago]
    if not weekly:
        return "Aucun signal cette semaine."
    total = len(weekly)
    wins = sum(1 for s in weekly if s["outcome"] == "WIN")
    losses = sum(1 for s in weekly if s["outcome"] == "LOSS")
    open_t = sum(1 for s in weekly if s["outcome"] == "OPEN")
    win_rate = round((wins / (wins + losses)) * 100, 1) if (wins + losses) > 0 else 0
    buys = [s for s in weekly if s["sig"] == "BUY"]
    sells = [s for s in weekly if s["sig"] == "SELL"]
    return (
        "*RAPPORT PERFORMANCE HEBDOMADAIRE*\n\n"
        "Periode: 7 derniers jours\n"
        "Total signaux: *" + str(total) + "*\n"
        "Wins: *" + str(wins) + "* | Losses: *" + str(losses) + "* | Ouverts: *" + str(open_t) + "*\n"
        "Taux de reussite: *" + str(win_rate) + "%*\n\n"
        "BUY: " + str(len(buys)) + " signaux\n"
        "SELL: " + str(len(sells)) + " signaux"
    )


# ── CLAUDE AI VALIDATOR ───────────────────────────────────────────

def claude_validate_signal(price, result, ind, quote, mtf, events, dxy, news, sentiment, sent_score,
                            corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg):
    if not ANTHROPIC_KEY:
        return True, "Cle manquante", "Signal envoye sans validation.", "MOYEN", "1% du capital"

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
            try:
                ev_hour = int(ev.get("time", "99:00").split(":")[0])
                if abs(ev_hour - datetime.now().hour) <= 2:
                    news_risk = "ELEVE"
            except:
                pass
    else:
        events_str = "Aucun evenement majeur"

    dxy_str = str(round(dxy, 2)) if dxy else "indisponible"
    news_text = format_news_for_claude(news)
    sent_str = (sentiment + " " + str(sent_score) + "%") if sentiment else "indisponible"
    corr_str = "\n".join(("- " + a for a in corr_analysis)) if corr_analysis else "Neutre"

    patterns = ind.get("patterns", [])
    patterns_str = "\n".join(("- " + p["name"] + ": " + p["desc"] for p in patterns)) if patterns else "Aucun"

    divergences = ind.get("divergences", [])
    div_str = "\n".join(("- " + d["type"] + ": " + d["desc"] for d in divergences)) if divergences else "Aucune"

    ob = ind.get("ob_current")
    ob_str = ("Prix dans " + ob["type"] + " (" + str(ob["low"]) + "-" + str(ob["high"]) + ")") if ob else "Pas d order block actif"

    fib = ind.get("fib")
    fib_str = "indisponible"
    if fib:
        nearest_name, nearest_val, is_near = find_nearest_fib(price, fib)
        fib_str = "Fib " + nearest_name + " (" + str(nearest_val) + ")" + (" - NIVEAU CLE" if is_near else "")

    ichi = ind.get("ichimoku")
    ichi_str = (ichi["label"] + " | Tenkan/Kijun: " + ichi["tk_signal"]) if ichi else "indisponible"

    st = ind.get("supertrend")
    st_str = ("Supertrend " + st["signal"].upper() + " (" + str(st["value"]) + ")") if st else "indisponible"

    cot_str = format_cot(cot) if cot else "Donnees COT indisponibles"
    fred_str = format_fred_data(fred) if fred else "Donnees FRED indisponibles"
    fred_interp_str = "\n".join(("- " + i for i in fred_interpretation)) if fred_interpretation else "Neutre"
    fg_str = ("Fear & Greed: " + str(fg["value"]) + " [" + fg["classification"] + "]") if fg else "indisponible"

    liquidity = ind.get("liquidity", [])
    if liquidity:
        liq_str = "\n".join("- " + l["type"] + " @ " + str(l["level"]) + " (dist: " + str(l["dist_pct"]) + "%)" for l in liquidity[:3])
    else:
        liq_str = "Aucune zone detectee"

    prompt = (
        "Tu es un expert trader XAU/USD senior avec 20 ans d experience institutionnelle. Nous sommes le " + today + ".\n\n"
        "SIGNAL A VALIDER: " + sig + " avec " + str(conf) + "% de fiabilite (" + str(result.get("total_indicators", 0)) + " indicateurs analyses)\n\n"
        "=== SESSION ===\n"
        "Session: " + session["session"] + " | Qualite: " + session["quality"] + "\n\n"
        "=== ACTUALITES TEMPS REEL (FINNHUB) ===\n" + news_text + "\n\n"
        "=== DONNEES MACRO OFFICIELLES (FRED) ===\n" + fred_str + "\n"
        "Interpretation macro: " + fred_interp_str + "\n\n"
        "=== COT REPORT (CFTC) - Positionnement Hedge Funds ===\n" + cot_str + "\n\n"
        "=== FEAR & GREED INDEX ===\n" + fg_str + "\n\n"
        "=== SENTIMENT OR (GLD) ===\n" + sent_str + "\n\n"
        "=== STRUCTURE DE MARCHE ===\n"
        "Structure: " + structure + "\n\n"
        "=== ICHIMOKU ===\n" + ichi_str + "\n\n"
        "=== SUPERTREND ===\n" + st_str + "\n\n"
        "=== FIBONACCI ===\n" + fib_str + "\n\n"
        "=== DIVERGENCES RSI/MACD ===\n" + div_str + "\n\n"
        "=== ORDER BLOCKS INSTITUTIONNELS ===\n" + ob_str + "\n\n"
        "=== ZONES DE LIQUIDITE ===\n" + liq_str + "\n\n"
        "=== PATTERNS BOUGIES ===\n" + patterns_str + "\n\n"
        "=== CORRELATIONS ===\n" + corr_str + "\n\n"
        "=== MULTI-TIMEFRAME ===\n"
        "H1: " + h1_sig + " (" + str(h1_conf) + "%) | H4: " + h4_sig + " (" + str(h4_conf) + "%) | D1: " + d1_sig + " (" + str(d1_conf) + "%)\n\n"
        "=== TECHNIQUE H1 ===\n"
        "Prix: " + str(round(price, 2)) + " | " + str(round(quote["change"], 2)) + " (" + str(round(quote["pct"], 2)) + "%)\n"
        "Zone entree: " + str(result.get("entry_zone_low")) + " - " + str(result.get("entry_zone_high")) + "\n"
        "TP1/TP2/TP3: " + str(result.get("tp1")) + "/" + str(result.get("tp2")) + "/" + str(result.get("tp3")) + "\n"
        "SL: " + str(result.get("sl")) + " | R/R: 1:" + str(result.get("rr")) + "\n"
        "RSI: " + str(ind.get("rsi")) + " | MACD: " + str(ind.get("macd", {}).get("hist")) + "\n"
        "ADX: " + str(ind.get("adx", {}).get("adx")) + " | ATR: " + str(ind.get("atr")) + "\n"
        "KAMA: " + str(ind.get("kama")) + " (" + str(ind.get("kama_signal")) + ")\n"
        "High 24h: " + str(ind.get("high24")) + " | Low 24h: " + str(ind.get("low24")) + "\n"
        "DXY: " + dxy_str + " | Risque news: " + news_risk + "\n\n"
        "=== CALENDRIER ===\n" + events_str + "\n\n"
        "ANALYSE COMPLETE REQUISE:\n"
        "Tu as acces aux memes donnees qu un trader institutionnel.\n"
        "Analyse TOUT : macro FRED, COT institutionnel, Fear&Greed, structure, ichimoku, divergences, order blocks, news.\n"
        "Valide ou invalide le signal " + sig + ".\n\n"
        "Format EXACT:\n\n"
        "VALIDATION: OUI\nou\nVALIDATION: NON\n\n"
        "RAISON: (2-3 phrases: les facteurs les plus determinants de ta decision)\n\n"
        "ANALYSE: (5-6 phrases: macro, COT, structure, ichimoku, orderblocks, conseil precis)\n\n"
        "RISQUE: FAIBLE ou MOYEN ou ELEVE\n\n"
        "LOT_CONSEILLE: (% capital selon risque)\n\n"
        "Criteres OUI: 2/3 TF alignes, COT favorable, macro soutient, structure confirme, ichimoku confirme, session active, R/R>=1.5\n"
        "Criteres NON: COT contre le signal, macro adverse, news imminente majeure, structure contradictoire, divergence baissiere\n"
        "IMPORTANT: Ce sont des trades INTRADAY - le trade doit pouvoir etre ferme dans la meme session (3-6h max).\n"
        "Si le contexte suggere un mouvement lent ou range, dis NON.\n\n"
        "En francais, niveau institutionnel, precis."
    )

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
        json={"model": "claude-sonnet-4-20250514", "max_tokens": 1000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60
    )
    d = r.json()
    response = "".join(b.get("text", "") for b in d.get("content", []))

    if not response:
        print("Claude AI: reponse vide - signal envoye sans validation")
        return True, "Analyse AI indisponible.", "Signal technique valide sur indicateurs.", "MOYEN", "1% du capital"

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
        elif capture_analyse and line.strip():
            analyse += " " + line.strip()

    return validated, raison.strip(), analyse.strip(), risque.strip(), lot.strip()


def get_daily_report_ai(price, quote, mtf, events, dxy, ind, news, sentiment, sent_score,
                        corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg):
    if not ANTHROPIC_KEY:
        return "Cle Anthropic manquante."

    today = datetime.now().strftime("%d/%m/%Y")
    h1_sig = mtf.get("H1", {}).get("signal", "N/A")
    h4_sig = mtf.get("H4", {}).get("signal", "N/A")
    d1_sig = mtf.get("D1", {}).get("signal", "N/A")

    events_str = "\n".join("- " + ev.get("name", "") + " a " + ev.get("time", "") for ev in events) if events else "Aucun"
    dxy_str = str(round(dxy, 2)) if dxy else "indisponible"
    news_text = format_news_for_claude(news)
    sent_str = (sentiment + " " + str(sent_score) + "%") if sentiment else "indisponible"
    corr_str = "\n".join(("- " + a for a in corr_analysis)) if corr_analysis else "Neutre"
    cot_str = format_cot(cot) if cot else "Indisponible"
    fred_str = format_fred_data(fred) if fred else "Indisponible"
    fred_interp = "\n".join(("- " + i for i in fred_interpretation)) if fred_interpretation else "Neutre"
    fg_str = ("Fear & Greed: " + str(fg["value"]) + " [" + fg["classification"] + "]") if fg else "indisponible"
    ichi = ind.get("ichimoku")
    ichi_str = ichi["label"] if ichi else "indisponible"
    st = ind.get("supertrend")
    st_str = ("Supertrend " + st["signal"].upper()) if st else "indisponible"
    fib = ind.get("fib")
    fib_key = ""
    if fib:
        fib_key = "61.8%: " + str(fib["fib_618"]) + " | 50%: " + str(fib["fib_500"]) + " | 38.2%: " + str(fib["fib_382"])

    prompt = (
        "Tu es un expert analyste XAU/USD de niveau institutionnel. Nous sommes le " + today + " au matin.\n\n"
        "=== ACTUALITES TEMPS REEL ===\n" + news_text + "\n\n"
        "=== MACRO FRED ===\n" + fred_str + "\n"
        "Interpretation: " + fred_interp + "\n\n"
        "=== COT CFTC ===\n" + cot_str + "\n\n"
        "=== FEAR & GREED ===\n" + fg_str + "\n\n"
        "=== STRUCTURE + ICHIMOKU + SUPERTREND ===\n"
        "Structure: " + structure + " | Ichimoku: " + ichi_str + " | Supertrend: " + st_str + "\n\n"
        "=== FIBONACCI CLES ===\n" + fib_key + "\n\n"
        "=== CORRELATIONS ===\n" + corr_str + "\n\n"
        "=== DONNEES MARCHE ===\n"
        "Prix: " + str(round(price, 2)) + " | " + str(round(quote["change"], 2)) + " (" + str(round(quote["pct"], 2)) + "%)\n"
        "H24: " + str(ind.get("high24")) + " | L24: " + str(ind.get("low24")) + " | ATR: " + str(ind.get("atr")) + "\n"
        "Signal H1: " + h1_sig + " | H4: " + h4_sig + " | D1: " + d1_sig + "\n"
        "DXY: " + dxy_str + " | Sentiment: " + sent_str + "\n"
        "Pivot: " + str(ind.get("res", {}).get("pivot")) + " | R1: " + str(ind.get("res", {}).get("r1")) + " | S1: " + str(ind.get("res", {}).get("s1")) + "\n\n"
        "=== CALENDRIER ===\n" + events_str + "\n\n"
        "Redige un rapport matinal de niveau institutionnel:\n\n"
        "RAPPORT XAU/USD - " + today + "\n\n"
        "1. MACRO & COT: News, FRED, positionnement hedge funds\n"
        "2. STRUCTURE TECHNIQUE: Ichimoku, Supertrend, HH/HL\n"
        "3. NIVEAUX CLES: Fibonacci, pivots, order blocks\n"
        "4. BIAIS DU JOUR: Direction precise avec probabilite\n"
        "5. PLAN DE TRADING: Zones d entree, TP, SL precis\n\n"
        "Niveau institutionnel. En francais."
    )

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
        json={"model": "claude-sonnet-4-20250514", "max_tokens": 1500,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=40
    )
    d = r.json()
    return "".join(b.get("text", "") for b in d.get("content", []))


# ── FORMAT MESSAGES ───────────────────────────────────────────────

def format_mtf_line(mtf):
    lines = ""
    for tf in ["H1", "H4", "D1"]:
        sig = mtf.get(tf, {}).get("signal", "N/A")
        conf = mtf.get(tf, {}).get("conf", 0)
        lines += tf + ": `" + sig + "` " + str(conf) + "% | "
    return lines.rstrip(" | ")


def format_news_short(news):
    if not news:
        return "  Aucune news recente\n"
    return "".join("  [" + item["source"] + " -" + str(item["age_hours"]) + "h] " + item["headline"][:70] + "\n" for item in news[:4])


def format_precise_alert(price, quote, result, ind, mtf, events, news, corr, corr_analysis,
                         session, structure, cot, fg, raison, analyse, risque, lot, chat_id):
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
    direction = "ACHAT (LONG)" if sig == "BUY" else "VENTE (SHORT)"
    action_mt4 = "Achat au marche" if sig == "BUY" else "Vente au marche"
    gain1 = round(abs(tp1 - entry), 2) if tp1 else 0
    gain2 = round(abs(tp2 - entry), 2) if tp2 else 0
    gain3 = round(abs(tp3 - entry), 2) if tp3 else 0
    risk_pts = round(abs(sl - entry), 2) if sl else 0

    capital_section = ""
    if chat_id in user_capital and sl:
        cap = user_capital[chat_id]
        capital_section = "---\n" + format_capital_plan(cap["capital"], cap["risk_pct"], entry, sl, tp1, tp2, tp3) + "\n"

    fib = ind.get("fib")
    fib_line = ""
    if fib:
        nearest_name, nearest_val, is_near = find_nearest_fib(price, fib)
        fib_line = "Fib " + nearest_name + ": `" + str(nearest_val) + "`" + (" NIVEAU CLE" if is_near else "") + "\n"

    ichi = ind.get("ichimoku")
    ichi_line = (ichi["label"] if ichi else "") + "\n"

    st = ind.get("supertrend")
    st_line = ("Supertrend " + st["signal"].upper() + " (" + str(st["value"]) + ")\n") if st else ""

    cot_line = ""
    if cot:
        cot_signal_txt = "LONG" if cot["signal"] == "bull" else ("SHORT" if cot["signal"] == "bear" else "NEUTRE")
        cot_line = "COT: Hedge funds *" + cot_signal_txt + "* (net: " + str(cot["mm_net"]) + ")\n"

    fg_line = ("Fear&Greed: `" + str(fg["value"]) + "` [" + fg["classification"] + "]\n") if fg else ""

    patterns = ind.get("patterns", [])
    patterns_str = "".join(("  ▲ " if p["dir"] == "bull" else "  ▼ ") + p["name"] + "\n" for p in patterns[:3]) or "  Aucun pattern\n"

    divergences = ind.get("divergences", [])
    div_str = "".join(("  ◆ " + d["type"] + "\n" for d in divergences)) or ""

    ob = ind.get("ob_current")
    ob_line = ("Order Block: " + ob["type"] + " (" + str(ob["low"]) + "-" + str(ob["high"]) + ")\n") if ob else ""

    events_text = "".join("  - " + ev.get("name", "") + " (" + ev.get("time", "") + ")\n" for ev in events[:3]) or "  Aucun\n"

    return (
        "ALERTE TRADE XAU/USD v10\n"
        "`" + now + "`\n\n"
        "---\n"
        "*" + direction + "*\n"
        "Fiabilite: *" + str(conf) + "%* | Risque: *" + risque + "*\n"
        "Session: *" + session["session"] + "*\n"
        "Structure: *" + structure + "*\n"
        "Validation AI: *OUI - TRADE VALIDE*\n"
        "Type: *INTRADAY - Fermer avant fin de session*\n\n"
        "---\n"
        "*PLAN DE TRADE*\n\n"
        "Zone entree: `" + str(entry_low) + " - " + str(entry_high) + "`\n"
        "Prix actuel: `" + str(round(price, 2)) + "`\n\n"
        "SL:  `" + str(sl) + "` (-" + str(risk_pts) + ")\n"
        "TP1: `" + str(tp1) + "` (+" + str(gain1) + ") 30%\n"
        "TP2: `" + str(tp2) + "` (+" + str(gain2) + ") Objectif\n"
        "TP3: `" + str(tp3) + "` (+" + str(gain3) + ") Max\n"
        "R/R: `1:" + str(rr) + "`\n"
        "Position: `" + lot + "`\n\n"
        "" + capital_section +
        "---\n"
        "*MULTI-TIMEFRAME*\n"
        "" + format_mtf_line(mtf) + "\n\n"
        "---\n"
        "*ANALYSE INSTITUTIONNELLE*\n"
        "" + cot_line +
        "" + fg_line +
        "" + fib_line +
        "" + ichi_line +
        "" + st_line +
        "" + ob_line +
        "\n"
        "---\n"
        "*PATTERNS + DIVERGENCES*\n"
        "" + patterns_str +
        "" + div_str + "\n"
        "---\n"
        "*CORRELATIONS*\n"
        "" + format_correlations(corr, corr_analysis[:2]) + "\n"
        "---\n"
        "*ACTUALITES*\n"
        "" + format_news_short(news) + "\n"
        "---\n"
        "*NEWS ECONOMIQUES*\n"
        "" + events_text + "\n"
        "---\n"
        "*ANALYSE CLAUDE AI (Niveau Institutionnel)*\n"
        "" + raison + "\n\n"
        "" + analyse + "\n\n"
        "---\n"
        "*ORDRE MT4*\n"
        "Type: " + action_mt4 + " XAUUSD\n"
        "SL: " + str(sl) + " | TP: " + str(tp2) + "\n\n"
        "---\n"
        "_Suivi auto actif (alertes TP/SL)_\n"
        "_Signaux indicatifs - Pas un conseil financier_"
    )


def format_analyse_complete(price, quote, result, ind, mtf, events, news, sentiment,
                            corr, corr_analysis, session, structure, cot, fg,
                            validated, raison, analyse, risque, lot, chat_id):
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
    reasons_text = "\n".join(("  - " + r for r in result.get("reasons", [])))
    rsi_label = "Surachat" if ind.get("rsi", 50) > 70 else ("Survente" if ind.get("rsi", 50) < 30 else "Neutre")
    confluence_sig, _ = mtf_confluence(mtf)
    aligned = "OUI" if confluence_sig == sig and sig != "NEUTRE" else "NON"
    ai_verdict = "OUI - VALIDE" if validated else "NON - REJETE"
    sent_txt = sentiment if sentiment else "N/A"

    fib = ind.get("fib")
    fib_section = ""
    if fib:
        nearest_name, nearest_val, is_near = find_nearest_fib(price, fib)
        fib_section = (
            "---\n*FIBONACCI*\n"
            "61.8%: `" + str(fib["fib_618"]) + "` | 50%: `" + str(fib["fib_500"]) + "` | 38.2%: `" + str(fib["fib_382"]) + "`\n"
            "Niveau actuel: *Fib " + nearest_name + "* (" + str(nearest_val) + ")" + (" - CLE" if is_near else "") + "\n\n"
        )

    ichi = ind.get("ichimoku")
    ichi_str = ichi["label"] if ichi else "N/A"

    st = ind.get("supertrend")
    st_str = ("Supertrend " + st["signal"].upper() + " (" + str(st["value"]) + ")") if st else "N/A"

    kama = ind.get("kama")
    kama_sig = ind.get("kama_signal", "N/A")

    cot_line = ""
    if cot:
        cot_txt = "LONG" if cot["signal"] == "bull" else ("SHORT" if cot["signal"] == "bear" else "NEUTRE")
        cot_line = "COT: Hedge funds *" + cot_txt + "* (net: " + str(cot["mm_net"]) + ")\n"

    fg_line = ("Fear&Greed: `" + str(fg["value"]) + "` [" + fg["classification"] + "]\n") if fg else ""

    divergences = ind.get("divergences", [])
    div_str = "".join(("  ◆ " + d["type"] + "\n" for d in divergences)) or "  Aucune\n"

    ob = ind.get("ob_current")
    ob_line = ("Order Block: " + ob["type"] + "\n") if ob else "  Aucun order block actif\n"

    patterns = ind.get("patterns", [])
    patterns_str = "".join(("  ▲ " if p["dir"] == "bull" else ("  ▼ " if p["dir"] == "bear" else "  ◆ ")) + p["name"] + " - " + p["desc"] + "\n" for p in patterns[:3]) or "  Aucun\n"

    events_text = "".join("  - " + ev.get("name", "") + " (" + ev.get("time", "") + ")\n" for ev in events) or "  Aucun\n"

    capital_section = ""
    if chat_id in user_capital and sl:
        cap = user_capital[chat_id]
        capital_section = "---\n" + format_capital_plan(cap["capital"], cap["risk_pct"], entry, sl, tp1, tp2, tp3) + "\n"

    return (
        "*XAU/USD - SIGNAL PRO v10*\n"
        "`" + now + "`\n\n"
        "---\n"
        "*SESSION: " + session["session"] + "* | *Structure: " + structure + "*\n\n"
        "---\n"
        "*MULTI-TIMEFRAME*\n"
        "" + format_mtf_line(mtf) + "\n"
        "Confluence: `" + aligned + "`\n\n"
        "---\n"
        "*PRIX*\n"
        "`" + str(round(price, 2)) + "` " + chg + " (" + pct + "%)\n"
        "H24: `" + str(ind.get("high24")) + "` L24: `" + str(ind.get("low24")) + "`\n\n"
        "---\n"
        "*SIGNAL: " + sig_label + "*\n"
        "Fiabilite: *" + str(conf) + "%* [" + conf_label + "]\n"
        "Nb indicateurs: `" + str(result.get("total_indicators", 0)) + "`\n"
        "Validation AI: *" + ai_verdict + "*\n"
        "Risque: *" + risque + "* | Sentiment: *" + sent_txt + "*\n\n"
        "Zone entree: `" + str(entry_low) + " - " + str(entry_high) + "`\n"
        "SL:  `" + str(sl if sl else "---") + "`\n"
        "TP1: `" + str(tp1 if tp1 else "---") + "` (30%)\n"
        "TP2: `" + str(tp2 if tp2 else "---") + "` (objectif)\n"
        "TP3: `" + str(tp3 if tp3 else "---") + "` (max)\n"
        "R/R: `1:" + str(rr if rr else "---") + "`\n"
        "Position: `" + lot + "`\n\n"
        "" + capital_section +
        "---\n"
        "*ANALYSE INSTITUTIONNELLE*\n"
        "" + cot_line +
        "" + fg_line +
        "Ichimoku: " + ichi_str + "\n"
        "Supertrend: " + st_str + "\n"
        "KAMA: `" + str(kama) + "` (" + kama_sig + ")\n\n"
        "" + fib_section +
        "---\n"
        "*DIVERGENCES*\n" + div_str + "\n"
        "---\n"
        "*ORDER BLOCKS*\n" + ob_line + "\n"
        "---\n"
        "*PATTERNS BOUGIES*\n" + patterns_str + "\n"
        "---\n"
        "*CORRELATIONS*\n" + format_correlations(corr, corr_analysis[:3]) + "\n"
        "---\n"
        "*RAISONS (Top 8)*\n" + reasons_text + "\n\n"
        "---\n"
        "*INDICATEURS H1*\n"
        "RSI: `" + str(ind.get("rsi")) + "` [" + rsi_label + "] | MACD: `" + str(ind.get("macd", {}).get("hist")) + "`\n"
        "ADX: `" + str(ind.get("adx", {}).get("adx")) + "` | ATR: `" + str(ind.get("atr")) + "`\n"
        "Pivot: `" + str(ind.get("res", {}).get("pivot")) + "` R1:`" + str(ind.get("res", {}).get("r1")) + "` S1:`" + str(ind.get("res", {}).get("s1")) + "`\n\n"
        "---\n"
        "*ACTUALITES*\n" + format_news_short(news) + "\n"
        "---\n"
        "*NEWS ECONOMIQUES*\n" + events_text + "\n"
        "---\n"
        "*ANALYSE CLAUDE AI (Niveau Institutionnel)*\n"
        "_Macro + COT + Ichimoku + OB + Divergences + News_\n\n"
        "" + raison + "\n\n"
        "" + analyse + "\n\n"
        "---\n"
        "_Suivi trade auto actif_\n"
        "_Signaux indicatifs - Pas un conseil financier - SL obligatoire_"
    )


# ── BACKTEST ──────────────────────────────────────────────────────

def run_backtest():
    try:
        r = requests.get("https://api.twelvedata.com/time_series",
            params={"symbol": SYMBOL, "interval": "1h", "outputsize": 500, "apikey": TWELVE_KEY}, timeout=20)
        d = r.json()
        if d.get("status") == "error":
            return None
        data = list(reversed(d.get("values", [])))
        closes = [float(b["close"]) for b in data]
        highs = [float(b["high"]) for b in data]
        lows = [float(b["low"]) for b in data]
        opens = [float(b["open"]) for b in data]
        results = []
        for i in range(50, len(closes) - 10):
            cl = closes[:i+1]
            hi = highs[:i+1]
            lo = lows[:i+1]
            op = opens[:i+1]
            rsi_v = calc_rsi(cl)
            macd_v = calc_macd(cl)
            e20 = ema(last_n(cl, 20), 20)
            e50 = ema(last_n(cl, 50), 50)
            atr_v = calc_atr(cl, hi, lo)
            adx_v = calc_adx(cl)
            bull = 0
            bear = 0
            if rsi_v < 40: bull += 2
            elif rsi_v > 60: bear += 2
            if macd_v["hist"] > 0: bull += 2
            else: bear += 2
            if e20 > e50: bull += 3
            else: bear += 3
            if adx_v["diP"] > adx_v["diN"]: bull += 1
            else: bear += 1
            tot = bull + bear or 1
            ratio = bull / tot
            if ratio >= 0.62: sig, conf = "BUY", round(ratio * 100)
            elif ratio <= 0.38: sig, conf = "SELL", round((1 - ratio) * 100)
            else: continue
            entry = cl[-1]
            if sig == "BUY":
                tp = entry + atr_v * 2.5
                sl = entry - atr_v * 1.2
            else:
                tp = entry - atr_v * 2.5
                sl = entry + atr_v * 1.2
            outcome = "OPEN"
            for j in range(i+1, min(i+20, len(closes))):
                if sig == "BUY":
                    if highs[j] >= tp: outcome = "WIN"; break
                    if lows[j] <= sl: outcome = "LOSS"; break
                else:
                    if lows[j] <= tp: outcome = "WIN"; break
                    if highs[j] >= sl: outcome = "LOSS"; break
            if outcome != "OPEN":
                results.append({"sig": sig, "conf": conf, "outcome": outcome})
        if not results:
            return None
        total = len(results)
        wins = sum(1 for r in results if r["outcome"] == "WIN")
        losses = sum(1 for r in results if r["outcome"] == "LOSS")
        win_rate = round((wins / total) * 100, 1) if total > 0 else 0
        buy_r = [r for r in results if r["sig"] == "BUY"]
        sell_r = [r for r in results if r["sig"] == "SELL"]
        buy_wins = sum(1 for r in buy_r if r["outcome"] == "WIN")
        sell_wins = sum(1 for r in sell_r if r["outcome"] == "WIN")
        buy_rate = round((buy_wins / len(buy_r)) * 100, 1) if buy_r else 0
        sell_rate = round((sell_wins / len(sell_r)) * 100, 1) if sell_r else 0
        high_conf = [r for r in results if r["conf"] >= 80]
        hc_wins = sum(1 for r in high_conf if r["outcome"] == "WIN")
        hc_rate = round((hc_wins / len(high_conf)) * 100, 1) if high_conf else 0
        return {
            "total": total, "wins": wins, "losses": losses, "win_rate": win_rate,
            "buy_total": len(buy_r), "buy_rate": buy_rate,
            "sell_total": len(sell_r), "sell_rate": sell_rate,
            "high_conf_total": len(high_conf), "high_conf_rate": hc_rate,
            "period": str(len(data)) + " bougies H1 (~" + str(len(data)//24) + " jours)"
        }
    except Exception as e:
        print("Backtest error: " + str(e))
        return None


# ── MAIN BOT HANDLER ──────────────────────────────────────────────

def handle(update):
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "").strip()
    text_lower = text.lower()
    if not chat_id:
        return

    if text_lower in ["/start", "/aide", "/help"]:
        subscribers.add(chat_id)
        send(chat_id, (
            "*XAU/USD Signal Pro v10 - FINAL*\n\n"
            "Alertes automatiques activees.\n\n"
            "*COMMANDES PRINCIPALES*\n"
            "/analyse - Analyse complete niveau institutionnel\n"
            "/prix - Prix actuel\n"
            "/rapport - Rapport matinal complet\n\n"
            "*MACRO & INSTITUTIONNEL*\n"
            "/macro - Donnees FRED (Fed, CPI, taux)\n"
            "/cot - COT Report (positionnement hedge funds)\n"
            "/sentiment - Fear & Greed + sentiment or\n\n"
            "*TECHNIQUE*\n"
            "/news - Actualites temps reel\n"
            "/niveaux - Supports, resistances, pivots\n"
            "/fibonacci - Niveaux Fibonacci\n"
            "/structure - Structure HH/HL/LH/LL\n"
            "/ichimoku - Signal Ichimoku\n"
            "/session - Session de trading\n"
            "/patterns - Patterns bougies\n"
            "/correlations - Dollar, Petrole, Bourse, VIX\n"
            "/orderblocks - Zones institutionnelles\n"
            "/divergences - Divergences RSI/MACD\n\n"
            "*GESTION*\n"
            "/backtest - Backtest sur 6 mois\n"
            "/performance - Rapport hebdomadaire\n"
            "/capital MONTANT RISQUE% - Ex: /capital 10000 1\n"
            "/trade - Statut trade en cours\n"
            "/fermer - Fermer suivi trade\n"
            "/alertes - Activer alertes auto\n"
            "/stop - Desactiver alertes\n\n"
            "_Pas un conseil financier. SL obligatoire._"
        ))

    elif text_lower == "/alertes":
        subscribers.add(chat_id)
        send(chat_id, "Alertes ACTIVEES.\nSysteme complet: Macro + COT + Ichimoku + Supertrend + Fib + OB + Patterns + News + Claude AI\nScan toutes les 15 min.")

    elif text_lower == "/stop":
        subscribers.discard(chat_id)
        send(chat_id, "Alertes DESACTIVEES.")

    elif text_lower == "/fermer":
        if chat_id in active_trades:
            del active_trades[chat_id]
            send(chat_id, "Trade ferme. Suivi desactive.")
        else:
            send(chat_id, "Aucun trade actif.")

    elif text_lower == "/performance":
        typing(chat_id)
        db_perf = format_db_performance()
        local_perf = format_weekly_performance()
        if "Aucune donnee" not in db_perf:
            send(chat_id, db_perf)
        else:
            send(chat_id, local_perf)

    elif text_lower.startswith("/capital"):
        parts = text.split()
        if len(parts) == 3:
            try:
                capital = float(parts[1])
                risk_pct = float(parts[2])
                user_capital[chat_id] = {"capital": capital, "risk_pct": risk_pct}
                send(chat_id,
                    "Capital enregistre!\n"
                    "Capital: `" + str(capital) + "$` | Risque: `" + str(risk_pct) + "%` = `" + str(round(capital * risk_pct / 100, 2)) + "$`\n"
                    "Lot size calcule automatiquement dans les prochaines alertes."
                )
            except:
                send(chat_id, "Format: /capital 10000 1")
        else:
            if chat_id in user_capital:
                cap = user_capital[chat_id]
                send(chat_id, "Capital: `" + str(cap["capital"]) + "$` | Risque: `" + str(cap["risk_pct"]) + "%`")
            else:
                send(chat_id, "Aucun capital configure.\nUtilise: /capital 10000 1")

    elif text_lower == "/macro":
        typing(chat_id)
        try:
            fred = get_fred_data()
            fred_signal, fred_interp = interpret_fred_for_gold(fred)
            direction = "HAUSSIER" if fred_signal == "bull" else ("BAISSIER" if fred_signal == "bear" else "NEUTRE")
            msg_text = "*Donnees Macro FRED (Federal Reserve)*\n\nSignal or: *" + direction + "*\n\n"
            msg_text += format_fred_data(fred) + "\n\n"
            if fred_interp:
                msg_text += "*Interpretation:*\n"
                for i in fred_interp:
                    msg_text += "- " + i + "\n"
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur macro: " + str(e))

    elif text_lower == "/cot":
        typing(chat_id)
        try:
            cot = get_cot_report()
            if cot:
                send(chat_id, "*COT Report CFTC - Or*\n_(Commitment of Traders)_\n\n" + format_cot(cot) + "\n\n_Mise a jour hebdomadaire_")
            else:
                send(chat_id, "Donnees COT indisponibles.")
        except Exception as e:
            send(chat_id, "Erreur COT: " + str(e))

    elif text_lower == "/sentiment":
        typing(chat_id)
        try:
            fg = get_fear_greed()
            sentiment, sent_score = get_forex_sentiment()
            msg_text = "*Sentiment de Marche*\n\n"
            if fg:
                msg_text += "Fear & Greed Index: *" + str(fg["value"]) + "*\n"
                msg_text += "Classification: *" + fg["classification"] + "*\n"
                msg_text += "Signal or: *" + ("ACHETER (panique = opportunite)" if fg["signal"] == "bull" else ("PRUDENCE (euphorie)" if fg["signal"] == "bear" else "NEUTRE")) + "*\n\n"
            if sentiment:
                msg_text += "Sentiment GLD (ETF or): *" + sentiment + "* (" + str(sent_score) + "%)\n"
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur sentiment: " + str(e))

    elif text_lower == "/ichimoku":
        typing(chat_id)
        try:
            closes, highs, lows, opens, times, volumes = get_history("1h", 200)
            ichi = calc_ichimoku(closes, highs, lows)
            if ichi:
                send(chat_id,
                    "*Ichimoku XAU/USD H1*\n\n"
                    "Signal: *" + ichi["label"] + "*\n"
                    "Tenkan/Kijun: *" + ichi["tk_signal"].upper() + "*\n\n"
                    "Tenkan-sen: `" + str(ichi["tenkan"]) + "`\n"
                    "Kijun-sen: `" + str(ichi["kijun"]) + "`\n"
                    "Senkou A: `" + str(ichi["senkou_a"]) + "`\n"
                    "Senkou B: `" + str(ichi["senkou_b"]) + "`\n"
                    "Nuage: `" + str(ichi["cloud_bot"]) + "` - `" + str(ichi["cloud_top"]) + "`"
                )
            else:
                send(chat_id, "Donnees insuffisantes pour Ichimoku.")
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower == "/orderblocks":
        typing(chat_id)
        try:
            closes, highs, lows, opens, times, volumes = get_history("1h", 100)
            obs = detect_order_blocks(opens, highs, lows, closes)
            price = closes[-1]
            ob_active = price_in_order_block(price, obs)
            if obs:
                msg_text = "*Order Blocks Institutionnels H1*\n\n"
                if ob_active:
                    msg_text += "Prix actuellement dans: *" + ob_active["type"] + "*\n\n"
                for ob in obs:
                    icon = "▲" if ob["dir"] == "bull" else "▼"
                    msg_text += icon + " *" + ob["type"] + "*\n"
                    msg_text += "  Zone: `" + str(ob["low"]) + "` - `" + str(ob["high"]) + "`\n\n"
            else:
                msg_text = "Aucun order block significatif detecte."
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower == "/divergences":
        typing(chat_id)
        try:
            closes, highs, lows, opens, times, volumes = get_history("1h", 100)
            divs = detect_divergences(closes, highs, lows)
            if divs:
                msg_text = "*Divergences RSI/MACD H1*\n\n"
                for d in divs:
                    icon = "▲" if d["dir"] == "bull" else "▼"
                    msg_text += icon + " *" + d["type"] + "*\n" + d["desc"] + "\n\n"
            else:
                msg_text = "Aucune divergence detectee sur H1."
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower == "/backtest":
        typing(chat_id)
        send(chat_id, "Backtest en cours... 30-60 secondes.")
        try:
            bt = run_backtest()
            if bt:
                send(chat_id,
                    "*RESULTATS BACKTESTING*\n\n"
                    "Periode: `" + bt["period"] + "`\n\n"
                    "Total: `" + str(bt["total"]) + "` | Wins: `" + str(bt["wins"]) + "` | Losses: `" + str(bt["losses"]) + "`\n"
                    "Taux reussite global: *" + str(bt["win_rate"]) + "%*\n\n"
                    "BUY: " + str(bt["buy_total"]) + " signaux → *" + str(bt["buy_rate"]) + "%*\n"
                    "SELL: " + str(bt["sell_total"]) + " signaux → *" + str(bt["sell_rate"]) + "%*\n\n"
                    "Haute confiance (>80%): " + str(bt["high_conf_total"]) + " signaux → *" + str(bt["high_conf_rate"]) + "%*\n\n"
                    "_Performances passees ne garantissent pas resultats futurs._"
                )
            else:
                send(chat_id, "Backtest indisponible.")
        except Exception as e:
            send(chat_id, "Erreur backtest: " + str(e))

    elif text_lower == "/fibonacci":
        typing(chat_id)
        try:
            closes, highs, lows, opens, times, volumes = get_history("1h", 100)
            fib = calc_fibonacci(highs, lows)
            price = closes[-1]
            nearest_name, nearest_val, is_near = find_nearest_fib(price, fib)
            send(chat_id,
                "*Fibonacci XAU/USD*\n\n"
                "Swing High: `" + str(fib["swing_high"]) + "`\n"
                "Fib 23.6%:  `" + str(fib["fib_236"]) + "`\n"
                "Fib 38.2%:  `" + str(fib["fib_382"]) + "`\n"
                "Fib 50.0%:  `" + str(fib["fib_500"]) + "`\n"
                "Fib 61.8%:  `" + str(fib["fib_618"]) + "` (niveau d or)\n"
                "Fib 78.6%:  `" + str(fib["fib_786"]) + "`\n"
                "Swing Low:  `" + str(fib["swing_low"]) + "`\n\n"
                "Prix sur: *Fib " + nearest_name + "* (" + str(nearest_val) + ")" + (" - NIVEAU CLE" if is_near else "")
            )
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower == "/structure":
        typing(chat_id)
        try:
            closes, highs, lows, opens, times, volumes = get_history("1h", 100)
            structure, ph, pl = detect_market_structure(highs, lows, closes)
            send(chat_id,
                "*Structure de Marche H1*\n\n"
                "Structure: *" + structure + "*\n\n"
                "Pivots hauts:\n" + "".join("  `" + str(round(p[1], 2)) + "`\n" for p in ph[-3:]) +
                "\nPivots bas:\n" + "".join("  `" + str(round(p[1], 2)) + "`\n" for p in pl[-3:]) +
                "\nHH+HL = Tendance haussiere\nLH+LL = Tendance baissiere"
            )
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower == "/session":
        session = get_session_info()
        send(chat_id,
            "*Session de Trading*\n\n"
            "Session: *" + session["session"] + "*\n"
            "Qualite: *" + session["quality"] + "*\n"
            "Heure UTC: " + str(session["hour_utc"]) + "h\n\n"
            "Overlap 13-16h UTC: OPTIMALE\n"
            "Londres 8-16h UTC: EXCELLENTE\n"
            "New York 16-21h UTC: EXCELLENTE\n"
            "Asie 21-2h UTC: EVITER"
        )

    elif text_lower == "/trade":
        if chat_id in active_trades:
            trade = active_trades[chat_id]
            try:
                q = get_quote()
                price = q["price"]
                sig = trade["sig"]
                pnl = (price - trade["entry"]) if sig == "BUY" else (trade["entry"] - price)
                send(chat_id,
                    "*Suivi Trade Actif*\n\n"
                    "Direction: `" + sig + "`\n"
                    "Entree: `" + str(trade["entry"]) + "`\n"
                    "Prix actuel: `" + str(round(price, 2)) + "`\n"
                    "P&L: `" + ("+" if pnl >= 0 else "") + str(round(pnl, 2)) + " USD/oz`\n\n"
                    "SL: `" + str(trade["sl"]) + "`\n"
                    "TP1: `" + str(trade["tp1"]) + "` [" + ("ATTEINT" if trade["tp1_hit"] else "en cours") + "]\n"
                    "TP2: `" + str(trade["tp2"]) + "` [" + ("ATTEINT" if trade["tp2_hit"] else "en cours") + "]\n"
                    "TP3: `" + str(trade["tp3"]) + "` [" + ("ATTEINT" if trade["tp3_hit"] else "en cours") + "]"
                )
            except Exception as e:
                send(chat_id, "Erreur: " + str(e))
        else:
            send(chat_id, "Aucun trade actif.")

    elif text_lower == "/prix":
        typing(chat_id)
        try:
            q = get_quote()
            chg = ("+" if q["change"] >= 0 else "") + str(round(q["change"], 2))
            pct = ("+" if q["pct"] >= 0 else "") + str(round(q["pct"], 2))
            send(chat_id, "*XAU/USD*\n`" + str(round(q["price"], 2)) + " USD/oz`\n" + chg + " (" + pct + "%)")
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower == "/patterns":
        typing(chat_id)
        try:
            closes, highs, lows, opens, times, volumes = get_history("1h", 50)
            ind = compute_indicators(closes, highs, lows, opens)
            patterns = ind.get("patterns", [])
            msg_text = "*Patterns Bougies H1*\n\n"
            if patterns:
                for p in patterns:
                    icon = "▲" if p["dir"] == "bull" else ("▼" if p["dir"] == "bear" else "◆")
                    msg_text += icon + " *" + p["name"] + "*\n  " + p["desc"] + "\n\n"
            else:
                msg_text += "Aucun pattern detecte."
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower == "/correlations":
        typing(chat_id)
        try:
            corr = get_correlated_assets()
            corr_signal, corr_analysis = analyze_correlations(corr)
            direction = "HAUSSIER pour l or" if corr_signal == "bull" else ("BAISSIER pour l or" if corr_signal == "bear" else "NEUTRE")
            msg_text = "*Correlations Marche*\n\nSignal: *" + direction + "*\n\n"
            msg_text += format_correlations(corr, corr_analysis)
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower == "/news":
        typing(chat_id)
        try:
            news = get_real_news()
            sentiment, sent_score = get_forex_sentiment()
            msg_text = "*Actualites XAU/USD*\n"
            if sentiment:
                msg_text += "Sentiment: *" + sentiment + "* (" + str(sent_score) + "%)\n\n"
            if news:
                for item in news[:6]:
                    msg_text += "[" + item["source"] + " - " + str(item["age_hours"]) + "h]\n*" + item["headline"] + "*\n"
                    if item["summary"]:
                        msg_text += item["summary"][:100] + "\n"
                    msg_text += "\n"
            else:
                msg_text += "Aucune actualite recente."
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower == "/niveaux":
        typing(chat_id)
        try:
            closes, highs, lows, opens, times, volumes = get_history("1h", 50)
            ind = compute_indicators(closes, highs, lows, opens)
            sr = ind["res"]
            liq = ind.get("liquidity", [])
            msg_text = (
                "*Niveaux XAU/USD*\n\n"
                "Pivot: `" + str(sr.get("pivot")) + "`\n"
                "R2: `" + str(sr.get("r2")) + "`\n"
                "R1: `" + str(sr.get("r1")) + "`\n"
                "S1: `" + str(sr.get("s1")) + "`\n"
                "S2: `" + str(sr.get("s2")) + "`\n\n"
                "Resistance: `" + str(sr.get("res")) + "`\n"
                "Support:    `" + str(sr.get("sup")) + "`\n"
                "High 24h:   `" + str(ind.get("high24")) + "`\n"
                "Low 24h:    `" + str(ind.get("low24")) + "`\n"
                "ATR:        `" + str(ind.get("atr")) + "`\n"
            )
            if liq:
                msg_text += "\n*Zones de Liquidite*\n"
                for l in liq[:4]:
                    msg_text += l["type"] + " @ `" + str(l["level"]) + "` (dist: " + str(l["dist_pct"]) + "%)\n"
            send(chat_id, msg_text)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower in ["/rapport", "/report"]:
        typing(chat_id)
        send(chat_id, "Rapport institutionnel en cours... 30 secondes.")
        try:
            (price, quote, result, ind, mtf, events, dxy, news, sentiment, sent_score,
             corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg) = run_full_analysis()
            ai_text = get_daily_report_ai(price, quote, mtf, events, dxy, ind, news, sentiment, sent_score,
                                          corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg)
            send(chat_id, ai_text)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    elif text_lower in ["/analyse", "/signal", "/a"]:
        subscribers.add(chat_id)
        typing(chat_id)
        send(chat_id, "Analyse institutionnelle v10...\nMacro + COT + Ichimoku + Supertrend + OB + Divergences + News\n30-40 secondes.")
        try:
            (price, quote, result, ind, mtf, events, dxy, news, sentiment, sent_score,
             corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg) = run_full_analysis()
            validated, raison, analyse, risque, lot = claude_validate_signal(
                price, result, ind, quote, mtf, events, dxy, news, sentiment, sent_score,
                corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg)
            message = format_analyse_complete(
                price, quote, result, ind, mtf, events, news, sentiment,
                corr, corr_analysis, session, structure, cot, fg,
                validated, raison, analyse, risque, lot, chat_id)
            send(chat_id, message)
        except Exception as e:
            send(chat_id, "Erreur: " + str(e))

    else:
        send(chat_id, "Tape /aide pour les commandes.")


# ── AUTO SCAN ─────────────────────────────────────────────────────

def auto_scan():
    print("Scan automatique v10 lance...")
    while True:
        try:
            if subscribers:
                print("Scan... " + str(len(subscribers)) + " abonnes")
                (price, quote, result, ind, mtf, events, dxy, news, sentiment, sent_score,
                 corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg) = run_full_analysis()

                sig = result.get("sig", "NEUTRE")
                conf = result.get("conf", 0)
                confluence_sig, _ = mtf_confluence(mtf)
                aligned = (confluence_sig == sig and sig != "NEUTRE")

                if not session["active"]:
                    print("Session " + session["session"] + " - pas d alerte")
                elif sig != "NEUTRE" and conf >= ALERT_THRESHOLD and aligned:
                    now_ts = time.time()
                    last_time = last_alert_time.get(sig, 0)
                    last_sig = last_alert_sig.get("last", "")
                    time_ok = (now_ts - last_time) >= MIN_ALERT_DELAY
                    sig_changed = (sig != last_sig)

                    if time_ok or sig_changed:
                        print("Validation Claude AI niveau institutionnel...")
                        validated, raison, analyse, risque, lot = claude_validate_signal(
                            price, result, ind, quote, mtf, events, dxy, news, sentiment, sent_score,
                            corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg)

                        if validated:
                            print("ALERTE VALIDEE: " + sig + " " + str(conf) + "%")
                            record_signal(sig, conf, result["entry"], result["tp2"], result["sl"])
                            db_save_signal(
                                sig, conf, result["entry"],
                                result["tp1"], result["tp2"], result["tp3"],
                                result["sl"], result["rr"],
                                structure, session["session"], True
                            )
                            for chat_id in list(subscribers):
                                message = format_precise_alert(
                                    price, quote, result, ind, mtf, events, news,
                                    corr, corr_analysis, session, structure, cot, fg,
                                    raison, analyse, risque, lot, chat_id)
                                send(chat_id, message)
                                register_trade(chat_id, sig, result["entry"],
                                             result["tp1"], result["tp2"], result["tp3"], result["sl"])
                            last_alert_time[sig] = now_ts
                            last_alert_sig["last"] = sig
                        else:
                            print("REJETE par Claude AI: " + sig + " " + str(conf) + "%")
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
    last_weekly_report = None
    while True:
        try:
            now = datetime.now()
            if now.hour == 8 and now.minute < 5 and last_report_day != now.date():
                if subscribers:
                    (price, quote, result, ind, mtf, events, dxy, news, sentiment, sent_score,
                     corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg) = run_full_analysis()
                    ai_text = get_daily_report_ai(price, quote, mtf, events, dxy, ind, news, sentiment, sent_score,
                                                  corr, corr_analysis, session, structure, cot, fred, fred_interpretation, fg)
                    for chat_id in list(subscribers):
                        send(chat_id, ai_text)
                    last_report_day = now.date()
                    print("Rapport quotidien envoye")

            if now.weekday() == 0 and now.hour == 8 and now.minute < 5 and last_weekly_report != now.date():
                if subscribers:
                    db_perf = format_db_performance()
                    local_perf = format_weekly_performance()
                    perf = db_perf if "Aucune donnee" not in db_perf else local_perf
                    for chat_id in list(subscribers):
                        send(chat_id, perf)
                    last_weekly_report = now.date()
                    print("Rapport hebdomadaire envoye")

        except Exception as e:
            print("Erreur rapport: " + str(e))
        time.sleep(60)


def main():
    print("XAU/USD Signal Pro v10 - VERSION FINALE")
    print("Niveau institutionnel: Macro FRED + COT CFTC + Fear&Greed + Ichimoku + Supertrend + KAMA + OrderBlocks + Divergences + Liquidite")
    print("Seuil: " + str(ALERT_THRESHOLD) + "% | Delai: " + str(MIN_ALERT_DELAY//60) + "min | Scan: " + str(SCAN_INTERVAL//60) + "min")

    threading.Thread(target=auto_scan, daemon=True).start()
    threading.Thread(target=daily_report_scheduler, daemon=True).start()
    threading.Thread(target=trade_monitor, daemon=True).start()

    offset = 0
    while True:
        try:
            r = requests.get(API_URL + "/getUpdates",
                params={"offset": offset, "timeout": 30}, timeout=35)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                handle(u)
        except Exception as e:
            print("Erreur: " + str(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
