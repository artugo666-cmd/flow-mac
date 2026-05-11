from __future__ import annotations

import os
import json
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ══════════════════════════════════════════════════════════════
# ETFs y tickers que nunca deben aparecer como señales
# ══════════════════════════════════════════════════════════════
EXCLUDE_ALWAYS = {
    "SPY","QQQ","IWM","DIA","VIX","TLT","GLD","SLV","USO","UNG",
    "XLF","XLE","XLK","XLY","XLU","XLB","XLP","XLI","XLV","XLC",
    "EEM","EFA","HYG","LQD","UVXY","SVXY","SQQQ","TQQQ","VXX",
    "JEPI","JEPQ","SCHD","VOO","VTI","BND",
}

# ══════════════════════════════════════════════════════════════
# WATCHLIST PERSISTENTE
# Solo se usa en /api/watchlist/scan — nunca en el scan autónomo
# ══════════════════════════════════════════════════════════════
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")

def load_watchlist() -> List[str]:
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                tickers = [t.upper().strip() for t in data.get("tickers", []) if t.strip()]
                return tickers if tickers else []
    except Exception as e:
        print(f"Watchlist load error: {e}")
    return []

def save_watchlist(tickers: List[str]) -> bool:
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump({
                "tickers": [t.upper().strip() for t in tickers],
                "updated": datetime.now().isoformat(timespec="seconds"),
                "count":   len(tickers),
            }, f, indent=2)
        return True
    except Exception as e:
        print(f"Watchlist save error: {e}")
        return False

if not os.path.exists(WATCHLIST_FILE):
    save_watchlist([])


# ══════════════════════════════════════════════════════════════
# NOTICIAS — palabras clave con impacto en precio
# ══════════════════════════════════════════════════════════════
NEWS_IMPACT: Dict[str, int] = {
    # Corporativo — máximo impacto
    "merger": 4, "acquisition": 4, "acquired": 4, "buyout": 4, "takeover": 4,
    "fda approval": 4, "approved": 3, "clearance": 3,
    "record revenue": 3, "record earnings": 3,
    # Analyst actions
    "upgrade": 3, "upgraded": 3, "initiates": 2, "initiated": 2,
    "raises target": 2, "outperform": 2, "overweight": 2,
    # Earnings
    "beats": 2, "beat": 2, "revenue beat": 2, "eps beat": 2,
    "raises guidance": 2,
    # Contratos / partnership
    "contract": 2, "awarded": 2, "partnership": 2, "collaboration": 2,
    "wins": 1, "launch": 1, "launches": 1, "breakthrough": 2,
    # Negativos
    "downgrade": -3, "downgraded": -3,
    "misses": -2, "miss": -2, "lowers guidance": -2,
    "investigation": -2, "lawsuit": -2, "sec": -1,
    "ban": -2, "restriction": -1, "recall": -2,
}

def score_news(title: str) -> Tuple[int, List[str]]:
    t = title.lower()
    total, matched = 0, []
    for kw, pts in NEWS_IMPACT.items():
        if kw in t:
            total += pts
            matched.append(kw)
    return total, matched


# ══════════════════════════════════════════════════════════════
# CAPA 1: DATOS CRUDOS
# Una sola función por concepto — sin duplicaciones
# ══════════════════════════════════════════════════════════════

def get_price_data(ticker: str) -> Dict[str, Any]:
    """
    Precio, volumen, VWAP y MAs de Yahoo Finance.
    Una sola llamada 1m + una 1d para avg_vol.
    """
    out = {}
    try:
        # 1m intraday para precio en tiempo real y VWAP
        r1 = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1m", "range": "1d"},
            headers=HEADERS, timeout=10
        )
        r1.raise_for_status()
        d1    = r1.json().get("chart", {}).get("result", [])
        meta  = d1[0].get("meta", {}) if d1 else {}
        q1    = d1[0].get("indicators", {}).get("quote", [{}])[0] if d1 else {}

        closes_1m = [c for c in (q1.get("close") or []) if c]
        vols_1m   = [v for v in (q1.get("volume") or []) if v]
        highs_1m  = [h for h in (q1.get("high") or []) if h]
        lows_1m   = [l for l in (q1.get("low") or []) if l]

        price   = round(float(closes_1m[-1]), 2) if closes_1m else float(meta.get("regularMarketPrice") or 0)
        vol_hoy = int(sum(vols_1m)) if vols_1m else int(meta.get("regularMarketVolume") or 0)

        # VWAP intraday
        if closes_1m and highs_1m and lows_1m and vols_1m:
            tp  = [(h+l+c)/3 for h,l,c in zip(highs_1m, lows_1m, closes_1m)]
            vwap = round(sum(t*v for t,v in zip(tp, vols_1m)) / max(sum(vols_1m),1), 2)
        else:
            vwap = price

        # Precio sobre/bajo VWAP — importante para timing de entrada
        precio_vs_vwap = round((price - vwap) / vwap * 100, 2) if vwap else 0

        prev    = float(meta.get("chartPreviousClose") or 0)
        chg_pct = round(float(meta.get("regularMarketChangePercent") or 0), 2)
        chg_abs = round(float(meta.get("regularMarketChange") or 0), 2)
        if chg_pct == 0 and prev and price:
            chg_abs = round(price - prev, 2)
            chg_pct = round(chg_abs / prev * 100, 2)

        day_hi = float(meta.get("regularMarketDayHigh") or 0)
        day_lo = float(meta.get("regularMarketDayLow") or 0)
        day_op = float(meta.get("regularMarketOpen") or 0)

        # Posición dentro del rango del día (0=mín, 1=máx) — entrada en extremo bajo = mejor R/R
        day_range_pos = round((price - day_lo) / (day_hi - day_lo), 2) if (day_hi - day_lo) > 0 else 0.5

        out.update({
            "price": price, "prev_close": round(prev, 2),
            "open": round(day_op, 2), "high": round(day_hi, 2), "low": round(day_lo, 2),
            "vwap": vwap, "precio_vs_vwap": precio_vs_vwap,
            "volume": vol_hoy,
            "change_pct": chg_pct, "change_abs": chg_abs,
            "day_range_pos": day_range_pos,
        })

    except Exception as e:
        print(f"Price data error {ticker}: {e}")

    try:
        # 1y diario para avg_vol, MAs, RSI, ATR
        r2 = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "1y"},
            headers=HEADERS, timeout=12
        )
        r2.raise_for_status()
        d2   = r2.json().get("chart", {}).get("result", [])
        meta2 = d2[0].get("meta", {}) if d2 else {}
        q2   = d2[0].get("indicators", {}).get("quote", [{}])[0] if d2 else {}

        closes = [c for c in (q2.get("close") or []) if c is not None]
        highs  = [h for h in (q2.get("high") or []) if h is not None]
        lows   = [l for l in (q2.get("low") or []) if l is not None]
        vols_d = [v for v in (q2.get("volume") or []) if v]

        price = out.get("price", 0) or float(meta2.get("regularMarketPrice") or 0)

        # Avg volume 20 días
        avg_vol = int(sum(vols_d[-21:-1]) / 20) if len(vols_d) >= 21 else int(sum(vols_d) / max(len(vols_d), 1))
        vol_hoy = out.get("volume", 0)
        rel_vol = round(vol_hoy / avg_vol, 2) if avg_vol > 0 else 1.0

        def sma(n):
            return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

        def pct_vs_ma(ma):
            if ma and price:
                d = round((price - ma) / ma * 100, 2)
                return f"+{d:.2f}%" if d >= 0 else f"{d:.2f}%"
            return "N/A"

        sma20  = sma(20);  sma50 = sma(50);  sma200 = sma(200)
        above_sma20  = bool(sma20  and price > sma20)
        above_sma50  = bool(sma50  and price > sma50)
        above_sma200 = bool(sma200 and price > sma200)

        # RSI 14
        rsi = 50.0
        if len(closes) >= 15:
            diffs  = [closes[i]-closes[i-1] for i in range(1, len(closes))][-14:]
            gains  = [max(d, 0) for d in diffs]
            losses = [max(-d, 0) for d in diffs]
            ag, al = sum(gains)/14, sum(losses)/14
            rsi = round(100 - 100/(1 + ag/al), 1) if al > 0 else 100.0

        # ATR 14
        atr = 0.0
        if len(highs) >= 15:
            trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
                   for i in range(max(1, len(highs)-14), len(highs))]
            atr = round(sum(trs)/len(trs), 2)

        # Performance
        def perf(days):
            if len(closes) > days:
                p = (price - closes[-days-1]) / closes[-days-1] * 100
                return f"+{p:.2f}%" if p >= 0 else f"{p:.2f}%"
            return "N/A"

        out.update({
            "avg_volume": avg_vol, "rel_volume": rel_vol,
            "sma20": sma20, "sma50": sma50, "sma200": sma200,
            "above_sma20": above_sma20, "above_sma50": above_sma50, "above_sma200": above_sma200,
            "sma20_pct": pct_vs_ma(sma20), "sma50_pct": pct_vs_ma(sma50), "sma200_pct": pct_vs_ma(sma200),
            "rsi": rsi, "atr": atr,
            "perf_week": perf(5), "perf_month": perf(21),
        })

    except Exception as e:
        print(f"Historical data error {ticker}: {e}")

    return out


def get_fundamentals(ticker: str) -> Dict[str, Any]:
    """Fundamentales, short float, earnings, sector — Yahoo quoteSummary."""
    out = {}
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
            params={"modules": "summaryDetail,defaultKeyStatistics,financialData,assetProfile,calendarEvents,upgradeDowngradeHistory"},
            headers=HEADERS, timeout=12
        )
        r.raise_for_status()
        res = r.json().get("quoteSummary", {}).get("result", [])
        if not res:
            return out

        def v(d, k):
            x = d.get(k, {})
            return (x.get("raw") if isinstance(x, dict) else x)

        sd  = res[0].get("summaryDetail", {})
        ks  = res[0].get("defaultKeyStatistics", {})
        fd  = res[0].get("financialData", {})
        ap  = res[0].get("assetProfile", {})
        cal = res[0].get("calendarEvents", {})
        udh = res[0].get("upgradeDowngradeHistory", {}).get("history", [])

        # Short float
        sf = v(ks, "shortPercentOfFloat")
        try:
            sfv = float(sf)
            sfv = sfv * 100 if sfv < 1 else sfv
            out["short_pct"] = round(sfv, 1)
        except:
            out["short_pct"] = 0.0

        out["beta"]        = round(float(v(ks, "beta") or 0), 2)
        out["sector"]      = ap.get("sector", "N/A")
        out["industry"]    = ap.get("industry", "N/A")
        out["target_price"]= round(float(v(fd, "targetMeanPrice") or 0), 2) or None

        rec = str(v(fd, "recommendationKey") or "")
        rec_map = {"strong_buy":"Strong Buy","buy":"Buy","hold":"Hold","sell":"Sell","strong_sell":"Strong Sell"}
        out["recommendation"] = rec_map.get(rec.lower(), rec.title() or "N/A")

        h52 = v(sd, "fiftyTwoWeekHigh"); l52 = v(sd, "fiftyTwoWeekLow")
        out["high_52w"] = round(float(h52), 2) if h52 else None
        out["low_52w"]  = round(float(l52), 2) if l52 else None

        ii = v(ks, "heldPercentInstitutions")
        out["inst_own"] = f"{float(ii)*100:.1f}%" if ii else "N/A"

        # Earnings
        ed = cal.get("earnings", {}).get("earningsDate", [])
        out["earnings_ts"]   = ed[0].get("raw", 0) if ed else 0
        out["earnings_date"] = datetime.fromtimestamp(ed[0].get("raw",0)).strftime("%d-%b-%Y") if ed else "N/A"
        if out["earnings_ts"]:
            days = (out["earnings_ts"] - datetime.now().timestamp()) / 86400
            out["earnings_days"] = round(days, 0)
        else:
            out["earnings_days"] = None

        # Analyst action reciente (últimos 5 días)
        cutoff = datetime.now().timestamp() - (5 * 86400)
        recent = [a for a in udh if a.get("epochGradeDate", 0) >= cutoff]
        if recent:
            lat = recent[0]
            out["analyst_action"] = f"{lat.get('firm','')}: {lat.get('action','')} → {lat.get('toGrade','')}"
            out["analyst_action_type"] = lat.get("action", "").lower()
        else:
            out["analyst_action"] = None
            out["analyst_action_type"] = None

    except Exception as e:
        print(f"Fundamentals error {ticker}: {e}")
    return out


def get_news_score(ticker: str) -> Dict[str, Any]:
    """Noticias de las últimas 48h con score de impacto."""
    out = {"score": 0, "title": None, "keywords": [], "age_hours": None}
    try:
        r = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": ticker, "newsCount": 8, "quotesCount": 0},
            headers=HEADERS, timeout=8
        )
        r.raise_for_status()
        now_ts = datetime.now().timestamp()
        best_score, best_title, best_kws, best_age = 0, None, [], None

        for item in r.json().get("news", []):
            title = item.get("title", "")
            pub   = item.get("providerPublishTime", 0)
            if not title or not pub:
                continue
            age_h = (now_ts - pub) / 3600
            if age_h > 48:
                continue
            ns, kws = score_news(title)
            if abs(ns) > abs(best_score):
                best_score, best_title, best_kws, best_age = ns, title, kws, round(age_h, 1)

        out.update({"score": best_score, "title": best_title, "keywords": best_kws, "age_hours": best_age})
    except Exception as e:
        print(f"News error {ticker}: {e}")
    return out


def get_options_data(ticker: str) -> Dict[str, Any]:
    """
    Cadena de opciones real de Yahoo — vol/OI, P/C ratio, call wall, put wall, max pain.
    Una sola función que calcula todo para evitar requests duplicados.
    """
    out = {
        "has_options": False,
        "vol_oi": 0.0, "call_vol_oi": 0.0, "put_vol_oi": 0.0,
        "atm_call_vol_oi": 0.0, "atm_put_vol_oi": 0.0,
        "total_call_vol": 0, "total_put_vol": 0,
        "total_call_oi": 0, "total_put_oi": 0,
        "pc_ratio": 1.0, "dominant": "NEUTRAL",
        "is_unusual": False, "is_sweep": False,
        "call_wall": None, "put_wall": None, "max_pain": None,
        "atm_oi_concentration": 0.0,
        "top_strikes": [],
        # Gamma squeeze
        "gamma_score": 0, "gamma_signal": "NONE", "gamma_triggers": [],
        "dealer_pressure": "NEUTRAL",
    }

    try:
        r = requests.get(
            f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}",
            headers=HEADERS, timeout=12
        )
        r.raise_for_status()
        chain = r.json().get("optionChain", {}).get("result", [])
        if not chain:
            return out

        current_price = float(chain[0].get("quote", {}).get("regularMarketPrice") or 0)
        dates = chain[0].get("expirationDates", [])
        if not dates or not current_price:
            return out

        out["has_options"] = True

        # Acumular datos de las 3 expiraciones más cercanas
        best_call_voi = 0.0; best_put_voi = 0.0
        total_cv = 0; total_pv = 0; total_coi = 0; total_poi = 0
        atm_cv = 0.0; atm_pv = 0.0
        call_oi_strike: Dict[float, Dict] = {}
        put_oi_strike:  Dict[float, Dict] = {}
        top_strikes = []

        atm_lo = current_price * 0.95
        atm_hi = current_price * 1.05

        for exp_ts in dates[:3]:
            r2 = requests.get(
                f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}",
                params={"date": exp_ts}, headers=HEADERS, timeout=12
            )
            r2.raise_for_status()
            res2 = r2.json().get("optionChain", {}).get("result", [])
            if not res2:
                continue
            calls = res2[0].get("options", [{}])[0].get("calls", [])
            puts  = res2[0].get("options", [{}])[0].get("puts",  [])

            for c in calls:
                vol = int(c.get("volume", 0) or 0)
                oi  = int(c.get("openInterest", 0) or 0)
                sk  = float(c.get("strike", 0) or 0)
                total_cv  += vol; total_coi += oi
                if sk > 0:
                    prev = call_oi_strike.get(sk, {"oi": 0, "vol": 0})
                    call_oi_strike[sk] = {"oi": prev["oi"]+oi, "vol": prev["vol"]+vol}
                if oi >= 50 and vol > 0:
                    ratio = vol / oi
                    if ratio > best_call_voi:
                        best_call_voi = round(ratio, 2)
                    if atm_lo <= sk <= atm_hi and ratio > atm_cv:
                        atm_cv = round(ratio, 2)
                    if ratio >= 1.5:
                        top_strikes.append({"type":"CALL","strike":sk,"vol":vol,"oi":oi,"ratio":round(ratio,2)})

            for p in puts:
                vol = int(p.get("volume", 0) or 0)
                oi  = int(p.get("openInterest", 0) or 0)
                sk  = float(p.get("strike", 0) or 0)
                total_pv  += vol; total_poi += oi
                if sk > 0:
                    prev = put_oi_strike.get(sk, {"oi": 0, "vol": 0})
                    put_oi_strike[sk] = {"oi": prev["oi"]+oi, "vol": prev["vol"]+vol}
                if oi >= 50 and vol > 0:
                    ratio = vol / oi
                    if ratio > best_put_voi:
                        best_put_voi = round(ratio, 2)
                    if atm_lo <= sk <= atm_hi and ratio > atm_pv:
                        atm_pv = round(ratio, 2)
                    if ratio >= 1.5:
                        top_strikes.append({"type":"PUT","strike":sk,"vol":vol,"oi":oi,"ratio":round(ratio,2)})

        best_voi = max(best_call_voi, best_put_voi)
        pc_ratio = round(total_pv / total_cv, 2) if total_cv > 0 else 1.0
        dominant = "CALL" if total_cv > total_pv * 1.3 else "PUT" if total_pv > total_cv * 1.3 else "NEUTRAL"
        top_strikes.sort(key=lambda x: x["ratio"], reverse=True)

        # Call wall / Put wall (strike con mayor OI acumulado)
        call_wall = max(call_oi_strike, key=lambda s: call_oi_strike[s]["oi"]) if call_oi_strike else None
        put_wall  = max(put_oi_strike,  key=lambda s: put_oi_strike[s]["oi"])  if put_oi_strike  else None

        # Max pain
        all_strikes = sorted(set(list(call_oi_strike.keys()) + list(put_oi_strike.keys())))
        max_pain = None
        if all_strikes:
            min_pain = None
            for tp in all_strikes:
                pain = (sum(max(0, tp-s)*d["oi"] for s,d in call_oi_strike.items()) +
                        sum(max(0, s-tp)*d["oi"] for s,d in put_oi_strike.items()))
                if min_pain is None or pain < min_pain:
                    min_pain = pain; max_pain = tp

        # Concentración OI ATM
        atm_call_oi  = sum(d["oi"] for s,d in call_oi_strike.items() if atm_lo <= s <= atm_hi)
        atm_oi_conc  = round(atm_call_oi / total_coi, 3) if total_coi > 0 else 0.0

        out.update({
            "vol_oi": best_voi, "call_vol_oi": best_call_voi, "put_vol_oi": best_put_voi,
            "atm_call_vol_oi": atm_cv, "atm_put_vol_oi": atm_pv,
            "total_call_vol": total_cv, "total_put_vol": total_pv,
            "total_call_oi": total_coi, "total_put_oi": total_poi,
            "pc_ratio": pc_ratio, "dominant": dominant,
            "is_unusual": best_voi >= 1.5, "is_sweep": best_voi >= 5.0,
            "call_wall": call_wall, "put_wall": put_wall, "max_pain": max_pain,
            "atm_oi_concentration": atm_oi_conc,
            "top_strikes": top_strikes[:5],
        })

        # ── GAMMA SQUEEZE con los datos ya calculados ─────────
        gamma_score = 0
        gamma_triggers = []

        # 1. Precio acercándose a call wall desde abajo
        if call_wall and current_price < call_wall <= current_price * 1.08:
            dist = round((call_wall - current_price) / current_price * 100, 1)
            gamma_score += 3
            gamma_triggers.append(f"🎯 Call Wall ${call_wall:.0f} a {dist}% — dealers deben comprar si sube")

        # 2. Alta concentración OI ATM
        if atm_oi_conc >= 0.40:
            gamma_score += 2
            gamma_triggers.append(f"🔥 {atm_oi_conc*100:.0f}% OI concentrado ATM — máxima exposición gamma")
        elif atm_oi_conc >= 0.25:
            gamma_score += 1
            gamma_triggers.append(f"⚡ {atm_oi_conc*100:.0f}% OI ATM — gamma moderado")

        # 3. Vol/OI calls ATM muy elevado (nuevas posiciones)
        if atm_cv >= 3.0:
            gamma_score += 3
            gamma_triggers.append(f"🚨 Vol/OI calls ATM {atm_cv:.1f}x — acumulación masiva cerca del precio")
        elif atm_cv >= 1.5:
            gamma_score += 2
            gamma_triggers.append(f"⚡ Vol/OI calls ATM {atm_cv:.1f}x — flujo inusual ATM")

        # 4. Precio sobre max pain (dealers en modo hedging alcista)
        if max_pain and current_price > max_pain * 1.03:
            gamma_score += 2
            gamma_triggers.append(f"📈 Precio ${current_price:.2f} sobre Max Pain ${max_pain:.0f}")

        # 5. Precio en zona comprimida entre put wall y call wall
        if call_wall and put_wall and put_wall < current_price < call_wall:
            if (call_wall - put_wall) / current_price <= 0.10:
                gamma_score += 1
                gamma_triggers.append(f"📊 Comprimido entre Put Wall ${put_wall:.0f} y Call Wall ${call_wall:.0f}")

        gamma_score = min(gamma_score, 10)
        if gamma_score >= 7:
            gs = "🚨 GAMMA SQUEEZE INMINENTE"; dp = "COMPRA_FORZADA"
        elif gamma_score >= 5:
            gs = "⚡ GAMMA SQUEEZE POSIBLE";   dp = "COMPRA_ELEVADA"
        elif gamma_score >= 3:
            gs = "📌 PRESIÓN GAMMA";           dp = "NEUTRAL_ALCISTA"
        else:
            gs = "NONE";                        dp = "NEUTRAL"

        out.update({
            "gamma_score": gamma_score, "gamma_signal": gs,
            "gamma_triggers": gamma_triggers, "dealer_pressure": dp,
        })

    except Exception as e:
        print(f"Options data error {ticker}: {e}")
    return out


def get_sentiment(ticker: str) -> Dict[str, Any]:
    """Sentimiento de Stocktwits."""
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json",
            headers=HEADERS, timeout=8
        )
        r.raise_for_status()
        sym = r.json().get("symbol", {})
        sent = sym.get("sentiment", {}) or {}
        return {
            "watchlist_count": sym.get("watchlist_count", 0),
            "bullish":  sent.get("bullish"),
            "bearish":  sent.get("bearish"),
        }
    except:
        return {}


def get_market_pulse() -> Dict[str, Any]:
    """SPY, QQQ, IWM, VIX — sentimiento general del mercado."""
    pulse = {}
    for sym in ["SPY", "QQQ", "IWM", "^VIX"]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"interval": "1d", "range": "1d"},
                headers=HEADERS, timeout=8
            )
            r.raise_for_status()
            meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            key  = "VIX" if sym == "^VIX" else sym
            pulse[key] = {
                "price":      round(float(meta.get("regularMarketPrice") or 0), 2),
                "change_pct": round(float(meta.get("regularMarketChangePercent") or 0), 2),
            }
        except:
            pass

    spy = pulse.get("SPY", {}).get("change_pct", 0)
    qqq = pulse.get("QQQ", {}).get("change_pct", 0)
    iwm = pulse.get("IWM", {}).get("change_pct", 0)
    vix = pulse.get("VIX", {}).get("price", 20)
    avg = (spy + qqq + iwm) / 3

    if vix >= 30:
        sentiment, emoji = "MUY_VOLATIL", "⚡"
    elif avg >= 0.5:
        sentiment, emoji = "ALCISTA", "📈"
    elif avg <= -0.5:
        sentiment, emoji = "BAJISTA", "📉"
    else:
        sentiment, emoji = "NEUTRAL", "➡️"

    pulse.update({
        "sentiment": sentiment, "emoji": emoji,
        "avg_change": round(avg, 2), "vix_price": vix,
        "vix_risk": "ALTO" if vix >= 25 else "MODERADO" if vix >= 18 else "BAJO",
    })
    return pulse


# ══════════════════════════════════════════════════════════════
# CAPA 2: DESCUBRIMIENTO DE CANDIDATOS
# Una sola función — múltiples screeners de Yahoo, sin watchlist
# ══════════════════════════════════════════════════════════════

def discover_candidates() -> List[Dict[str, Any]]:
    """
    Descubre candidatos SOLO desde screeners de mercado en vivo.
    Sin watchlists, sin listas hardcodeadas.
    Screeners usados:
      - most_actives       → mayor volumen absoluto
      - day_gainers        → mayores subidas del día
      - day_losers         → mayores caídas del día
      - small_cap_gainers  → small caps e IPOs recientes en movimiento
      + Yahoo trending     → lo que la gente está buscando
    """
    candidates: Dict[str, Dict] = {}  # ticker → datos

    SCREENERS = [
        ("most_actives",         50),   # mayor volumen absoluto
        ("day_gainers",          40),   # mayores subidas del dia
        ("day_losers",           40),   # mayores caidas del dia
        ("small_cap_gainers",    35),   # IPOs recientes y small caps
        ("growth_technology_stocks", 30), # tech en momentum — ASTS, CRCL, etc.
    ]

    for scrId, count in SCREENERS:
        try:
            r = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"scrIds": scrId, "count": count},
                headers=HEADERS, timeout=10
            )
            r.raise_for_status()
            quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])

            for q in quotes:
                tk = q.get("symbol", "")
                if not tk or tk in EXCLUDE_ALWAYS:
                    continue
                if len(tk) > 6 or "." in tk:
                    continue
                if tk in candidates:
                    continue  # ya lo tenemos

                price   = float(q.get("regularMarketPrice", 0) or 0)
                avg_vol = int(q.get("averageDailyVolume3Month", 1) or 1)
                vol     = int(q.get("regularMarketVolume", 0) or 0)
                chg_pct = float(q.get("regularMarketChangePercent", 0) or 0)
                rel_vol = round(vol / avg_vol, 2) if avg_vol > 0 else 1.0

                # Filtros de calidad: precio >= $5, avg_vol >= 1M diario
                # IPO exception: si vol HOY > 2M aunque avg_vol sea bajo (sin 3m historial)
                is_ipo = (avg_vol < 1_000_000 and vol_hoy > 3_000_000)
                if price < 5.0:
                    continue
                if avg_vol < 1_000_000 and not is_ipo:
                    continue

                candidates[tk] = {
                    "ticker":  tk,
                    "price":   price,
                    "chg_pct": chg_pct,
                    "rel_vol": rel_vol,
                    "avg_vol": avg_vol,
                    "vol_hoy": vol,
                    "source":  scrId,
                    "is_ipo":  is_ipo,
                }

            print(f"discover [{scrId}]: {len(candidates)} candidatos acumulados")
        except Exception as e:
            print(f"discover [{scrId}] error: {e}")

    # Trending Yahoo (no trae precio — solo añadir si no está ya)
    try:
        r2 = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/trending/US",
            params={"count": 20}, headers=HEADERS, timeout=10
        )
        r2.raise_for_status()
        for q in r2.json().get("finance", {}).get("result", [{}])[0].get("quotes", []):
            tk = q.get("symbol", "")
            if tk and tk not in EXCLUDE_ALWAYS and tk not in candidates and len(tk) <= 6 and "." not in tk:
                candidates[tk] = {"ticker": tk, "price": 0, "chg_pct": 0, "rel_vol": 1.0, "source": "trending"}
        print(f"discover [trending]: {len(candidates)} candidatos total")
    except Exception as e:
        print(f"discover [trending] error: {e}")

    # ── FILTRO DE RELEVANCIA ─────────────────────────────────
    # most_actives incluye acciones con alto volumen ABSOLUTO pero sin
    # movimiento (CTRA, KO, JNJ en días normales). Son líquidas pero no
    # son oportunidades hoy. Se filtran ANTES del análisis completo.
    #
    # Pasa el filtro si tiene AL MENOS UNO:
    #   1. chg_pct >= 2%      → se está moviendo hoy
    #   2. rel_vol >= 2x      → flujo inusual vs su promedio
    #   3. Viene de day_gainers, day_losers, small_cap_gainers
    #      → Yahoo ya lo identificó como mover del día

    MOVER_SOURCES = {"day_gainers", "day_losers", "small_cap_gainers"}

    def is_relevant(c):
        # Screener de movers → relevante por definicion
        if c.get("source", "") in MOVER_SOURCES:
            return True
        # Movimiento fuerte del dia (CRCL +12%, ASTS +10% pasan aqui)
        if abs(c.get("chg_pct", 0)) >= 2.0:
            return True
        # Volumen relativo inusual aunque el precio no se mueva mucho
        if c.get("rel_vol", 1.0) >= 2.0:
            return True
        # IPO reciente con volumen masivo hoy
        if c.get("is_ipo", False):
            return True
        # tech growth con cualquier movimiento positivo
        if c.get("source", "") == "growth_technology_stocks" and c.get("chg_pct", 0) >= 1.0:
            return True
        return False

    filtered = [c for c in candidates.values() if is_relevant(c)]
    print(f"discover: {len(candidates)} candidatos → {len(filtered)} relevantes (filtro mov/vol aplicado)")

    # Ordenar: mayor (|cambio%| × rel_vol) primero
    ranked = sorted(
        filtered,
        key=lambda x: abs(x.get("chg_pct", 0)) * max(x.get("rel_vol", 1.0), 1.0),
        reverse=True
    )

    return ranked


# ══════════════════════════════════════════════════════════════
# CAPA 3: SCORING — DETECCIÓN DE ENTRADAS
#
# Filosofía: detectar setups donde la probabilidad de movimiento
# direccional está por encima del azar, con R/R favorable.
#
# Las señales se agrupan en 4 categorías:
#   A. FLUJO INSTITUCIONAL  — el dinero grande ya está posicionado
#   B. CATALIZADOR          — razón fundamental para el movimiento
#   C. MOMENTUM TÉCNICO     — el precio confirma la dirección
#   D. TIMING DE ENTRADA    — ¿es buen momento para entrar AHORA?
#
# Cada categoría aporta hasta 2.5 pts → máximo teórico 10.0
# VERDE ≥ 7.0 | AMARILLO ≥ 4.5 | ROJO < 4.5
# ══════════════════════════════════════════════════════════════

def compute_score(
    ticker:  str,
    price:   Dict,   # get_price_data
    fund:    Dict,   # get_fundamentals
    opts:    Dict,   # get_options_data
    news:    Dict,   # get_news_score
    sent:    Dict,   # get_sentiment
    market:  Dict,   # get_market_pulse
    alert_context: str = "",
) -> Dict[str, Any]:

    # ── Datos base ────────────────────────────────────────────
    current_price = price.get("price", 0)
    chg_pct       = price.get("change_pct", 0)
    rel_vol       = price.get("rel_volume", 1.0)
    rsi           = price.get("rsi", 50.0)
    above_sma20   = price.get("above_sma20", False)
    above_sma50   = price.get("above_sma50", False)
    above_sma200  = price.get("above_sma200", False)
    vwap          = price.get("vwap", current_price)
    precio_vs_vwap= price.get("precio_vs_vwap", 0)
    day_range_pos = price.get("day_range_pos", 0.5)
    atr           = price.get("atr", 0)

    short_pct     = fund.get("short_pct", 0.0)
    beta          = fund.get("beta", 1.0)
    earn_days     = fund.get("earnings_days")
    analyst_type  = fund.get("analyst_action_type")

    has_opts      = opts.get("has_options", False)
    vol_oi        = opts.get("vol_oi", 0.0)
    atm_cv        = opts.get("atm_call_vol_oi", 0.0)
    atm_pv        = opts.get("atm_put_vol_oi", 0.0)
    pc_ratio      = opts.get("pc_ratio", 1.0)
    dominant      = opts.get("dominant", "NEUTRAL")
    is_sweep      = opts.get("is_sweep", False)
    gamma_score   = opts.get("gamma_score", 0)

    news_score    = news.get("score", 0)
    news_age      = news.get("age_hours")

    mkt_sent      = market.get("sentiment", "NEUTRAL")
    has_alert     = bool(alert_context and len(alert_context.strip()) > 5)
    abs_chg       = abs(chg_pct)

    # ── DIRECCIÓN — cuál lado tiene más evidencia ─────────────
    # Prioridad: alert > opciones institucionales > precio
    if has_alert:
        if "put" in alert_context.lower():
            direction = "PUT"
        elif "call" in alert_context.lower():
            direction = "CALL"
        else:
            direction = "CALL" if chg_pct >= 0 else "PUT"
    elif has_opts and dominant != "NEUTRAL":
        direction = dominant
    elif chg_pct >= 0:
        direction = "CALL"
    else:
        direction = "PUT"

    is_bull = direction == "CALL"

    # ════════════════════════════════════════════════════════
    # A. FLUJO INSTITUCIONAL (max 2.5)
    # El dinero inteligente deja huella en opciones antes de moverse
    # ════════════════════════════════════════════════════════
    if has_alert and is_sweep:
        pts_A = 2.5  # alerta confirmada por sweep = máxima confianza
    elif is_sweep:
        pts_A = 2.4  # sweep institucional sin alerta
    elif has_alert:
        pts_A = 2.0  # alerta manual (Discord/Telegram)
    elif has_opts and vol_oi >= 5.0:
        pts_A = 2.2  # flujo muy inusual
    elif has_opts and vol_oi >= 3.0:
        pts_A = 2.0
    elif has_opts and vol_oi >= 1.5:
        pts_A = 1.6
    elif has_opts and vol_oi >= 0.8:
        pts_A = 1.0
    elif has_opts:
        pts_A = 0.6  # tiene opciones pero flujo normal
    else:
        pts_A = 0.2  # sin datos de opciones

    # Bonus: flujo ATM confirma dirección (más específico que flujo general)
    atm_bonus = 0.0
    if is_bull and atm_cv >= 2.0:
        atm_bonus = 0.3
    elif not is_bull and atm_pv >= 2.0:
        atm_bonus = 0.3
    pts_A = min(pts_A + atm_bonus, 2.5)

    # Bonus: gamma squeeze potencia el movimiento
    if gamma_score >= 7:
        pts_A = min(pts_A + 0.4, 2.5)
    elif gamma_score >= 5:
        pts_A = min(pts_A + 0.2, 2.5)

    # ════════════════════════════════════════════════════════
    # B. CATALIZADOR (max 2.5)
    # ¿Por qué se va a mover? Sin razón, el movimiento no dura
    # ════════════════════════════════════════════════════════
    pts_B = 0.0

    # Earnings próximos — máxima volatilidad garantizada
    if earn_days is not None:
        if 0 <= earn_days <= 1:
            pts_B = 2.5   # earnings HOY o MAÑANA
        elif earn_days <= 3:
            pts_B = 2.2
        elif earn_days <= 7:
            pts_B = 1.8
        elif earn_days <= 14:
            pts_B = 1.2

    # Noticia de alto impacto reciente
    if news_score >= 4 and (news_age or 999) <= 6:
        pts_B = max(pts_B, 2.3)   # noticia muy fuerte y fresca
    elif news_score >= 3 and (news_age or 999) <= 12:
        pts_B = max(pts_B, 2.0)
    elif news_score >= 2 and (news_age or 999) <= 24:
        pts_B = max(pts_B, 1.6)
    elif news_score >= 1:
        pts_B = max(pts_B, 1.2)
    elif news_score <= -3:
        pts_B = max(pts_B, 1.8)   # noticia negativa fuerte también es catalizador (PUT)

    # Analyst upgrade/downgrade reciente
    if analyst_type in ("up", "upgrade", "init", "initiated"):
        pts_B = max(pts_B, 1.8)
    elif analyst_type in ("down", "downgrade"):
        pts_B = max(pts_B, 1.5)
    elif analyst_type in ("main", "reit", "reiterate"):
        pts_B = max(pts_B, 1.2)

    # Volumen inusual sin opciones = algo está pasando aunque no sepamos qué
    if pts_B == 0:
        if rel_vol >= 5:
            pts_B = 1.5   # volumen 5x sin noticia obvia = acumulación silenciosa
        elif rel_vol >= 3:
            pts_B = 1.0
        elif rel_vol >= 2:
            pts_B = 0.6
        else:
            pts_B = 0.2

    pts_B = min(pts_B, 2.5)

    # ════════════════════════════════════════════════════════
    # C. MOMENTUM TÉCNICO (max 2.5)
    # El precio debe confirmar la dirección — no entrar contra tendencia
    # ════════════════════════════════════════════════════════
    pts_C = 0.0

    if is_bull:
        # Estructura alcista progresiva
        if above_sma20 and above_sma50 and above_sma200:
            pts_C = 2.0   # tendencia alcista en todos los marcos
        elif above_sma20 and above_sma50:
            pts_C = 1.6
        elif above_sma50:
            pts_C = 1.2
        elif above_sma20:
            pts_C = 0.8
        elif chg_pct > 2:
            pts_C = 0.6   # subiendo con fuerza aunque esté bajo MAs
        else:
            pts_C = 0.2

        # Bonus: volumen confirma el movimiento alcista
        if rel_vol >= 3 and chg_pct > 1:
            pts_C = min(pts_C + 0.4, 2.5)
        elif rel_vol >= 2 and chg_pct > 0.5:
            pts_C = min(pts_C + 0.2, 2.5)

        # Bonus: short squeeze fuel
        if short_pct >= 20:
            pts_C = min(pts_C + 0.3, 2.5)
        elif short_pct >= 15:
            pts_C = min(pts_C + 0.2, 2.5)

    else:  # PUT / bajista
        if not above_sma20 and not above_sma50 and not above_sma200:
            pts_C = 2.0
        elif not above_sma20 and not above_sma50:
            pts_C = 1.6
        elif not above_sma50:
            pts_C = 1.2
        elif not above_sma20:
            pts_C = 0.8
        elif chg_pct < -2:
            pts_C = 0.6
        else:
            pts_C = 0.2

        if rel_vol >= 3 and chg_pct < -1:
            pts_C = min(pts_C + 0.4, 2.5)
        elif rel_vol >= 2 and chg_pct < -0.5:
            pts_C = min(pts_C + 0.2, 2.5)

    # Mercado general — viento a favor o en contra
    mkt_mod = 0.3 if mkt_sent == "ALCISTA" and is_bull else \
              0.3 if mkt_sent == "BAJISTA" and not is_bull else \
             -0.3 if mkt_sent == "BAJISTA" and is_bull else \
             -0.3 if mkt_sent == "ALCISTA" and not is_bull else 0.0
    pts_C = min(max(pts_C + mkt_mod, 0), 2.5)

    # ════════════════════════════════════════════════════════
    # D. TIMING DE ENTRADA (max 2.5)
    # ¿Es buen momento para entrar AHORA? R/R y setup limpio
    # ════════════════════════════════════════════════════════
    pts_D = 1.0  # base neutral

    # RSI — sobrecomprado/sobrevendido penaliza el timing
    if rsi >= 80 and is_bull:
        pts_D -= 0.8   # muy sobrecomprado, prima de opciones inflada
    elif rsi >= 70 and is_bull:
        pts_D -= 0.4
    elif rsi <= 20 and not is_bull:
        pts_D -= 0.8
    elif rsi <= 30 and not is_bull:
        pts_D -= 0.4
    elif rsi <= 40 and is_bull:
        pts_D += 0.3   # RSI moderado en tendencia alcista = mejor entrada
    elif rsi >= 60 and not is_bull:
        pts_D += 0.3

    # Movimiento del día — si ya se movió mucho, el tren pasó
    if abs_chg >= 20:
        pts_D -= 1.5   # movimiento extremo, prima de opciones inflada
    elif abs_chg >= 15:
        pts_D -= 1.0
    elif abs_chg >= 10:
        pts_D -= 0.6
    elif abs_chg >= 7:
        pts_D -= 0.3
    elif 2 <= abs_chg <= 5 and rel_vol >= 2:
        pts_D += 0.4   # movimiento saludable con volumen = setup ideal

    # Posición vs VWAP — entrada cerca del VWAP = mejor R/R
    if is_bull:
        if -1 <= precio_vs_vwap <= 1:
            pts_D += 0.5   # tocando VWAP = entrada limpia
        elif precio_vs_vwap > 5:
            pts_D -= 0.3   # muy extendido sobre VWAP
    else:
        if -1 <= precio_vs_vwap <= 1:
            pts_D += 0.5
        elif precio_vs_vwap < -5:
            pts_D -= 0.3

    # Posición en el rango del día
    if is_bull and day_range_pos <= 0.3:
        pts_D += 0.3   # comprando cerca del mínimo del día = mejor R/R
    elif not is_bull and day_range_pos >= 0.7:
        pts_D += 0.3   # vendiendo cerca del máximo del día

    pts_D = min(max(pts_D, 0), 2.5)

    # ── SCORE FINAL ───────────────────────────────────────────
    raw   = pts_A + pts_B + pts_C + pts_D
    score = round(min(max(raw, 0), 10.0), 1)

    if score >= 7.0:
        semaforo = "VERDE"
    elif score >= 4.5:
        semaforo = "AMARILLO"
    else:
        semaforo = "ROJO"

    # ── ETIQUETAS ─────────────────────────────────────────────
    if rsi >= 70:
        rsi_label = f"Sobrecomprado ({rsi:.0f})"
    elif rsi <= 30:
        rsi_label = f"Sobrevendido ({rsi:.0f})"
    else:
        rsi_label = f"Normal ({rsi:.0f})"

    if rel_vol >= 2.0 and chg_pct > 0.5:
        momentum = "ALCISTA FUERTE"
    elif rel_vol >= 2.0 and chg_pct < -0.5:
        momentum = "BAJISTA FUERTE"
    elif chg_pct > 1.5:
        momentum = "ALCISTA FUERTE"
    elif chg_pct > 0.3:
        momentum = "ALCISTA"
    elif chg_pct < -1.5:
        momentum = "BAJISTA FUERTE"
    elif chg_pct < -0.3:
        momentum = "BAJISTA"
    else:
        momentum = "LATERAL"

    if has_opts:
        vol_label = f"Vol/OI {vol_oi:.1f}x"
        if is_sweep:
            vol_label += " 🚨 SWEEP INSTITUCIONAL"
        elif vol_oi >= 3.0:
            vol_label += " 🔥 MUY INUSUAL"
        elif vol_oi >= 1.5:
            vol_label += " ⚡ INUSUAL"
        vol_label += f" | P/C {pc_ratio:.2f} | {dominant}"
    else:
        vol_label = f"{rel_vol:.1f}x promedio"
        vol_label += " 🔥 MUY INUSUAL" if rel_vol >= 3 else " ⚡ INUSUAL" if rel_vol >= 2 else ""

    # ── POR QUÉ / RIESGOS ────────────────────────────────────
    why = []
    if has_alert:
        why.append(f"🔔 Alerta: {alert_context}")
    if is_sweep:
        why.append(f"🚨 SWEEP INSTITUCIONAL — Vol/OI {vol_oi:.1f}x, lado {dominant}")
    elif has_opts and vol_oi >= 1.5:
        why.append(f"⚡ Flujo inusual opciones: Vol/OI {vol_oi:.1f}x — {dominant} dominante (P/C {pc_ratio:.2f})")
    for gt in opts.get("gamma_triggers", [])[:2]:
        why.append(gt)
    if news.get("title") and news_score >= 2:
        age_str = f"hace {news_age:.0f}h" if news_age else ""
        why.append(f"📰 Noticia {age_str}: {news.get('title','')[:80]}")
    if fund.get("analyst_action"):
        why.append(f"📊 Analista: {fund['analyst_action']}")
    if earn_days is not None and earn_days <= 7:
        why.append(f"⏰ Earnings en {earn_days:.0f} día(s) — volatilidad esperada")
    if rel_vol >= 2 and not has_opts:
        why.append(f"📈 Volumen {rel_vol:.1f}x sobre promedio — actividad inusual sin opciones")
    if above_sma20 and above_sma50 and is_bull:
        why.append("✅ Estructura alcista: precio sobre MA20 y MA50")
    if short_pct >= 15 and is_bull:
        why.append(f"🔥 Short float {short_pct:.0f}% — combustible para squeeze")
    if not why:
        why.append(f"Cambio del día {chg_pct:+.2f}%. Sin catalizadores claros identificados.")

    risk = []
    if rsi >= 75 and is_bull:
        risk.append(f"⚠️ RSI {rsi:.0f} sobrecomprado — espera pullback o recorta tamaño")
    if abs_chg >= 10:
        risk.append(f"⚠️ Ya se movió {chg_pct:+.1f}% hoy — prima de opciones inflada")
    if not above_sma50 and is_bull:
        risk.append("⚠️ Bajo MA50 — tendencia bajista de fondo")
    if mkt_sent == "BAJISTA" and is_bull:
        risk.append("⚠️ Mercado general bajista — viento en contra")
    if precio_vs_vwap > 5 and is_bull:
        risk.append(f"⚠️ Precio {precio_vs_vwap:+.1f}% sobre VWAP — extendido, espera retroceso")
    if not risk:
        risk.append(f"Monitorear VWAP ${vwap:.2f} como soporte. Stop bajo mínimo del día ${price.get('low',0):.2f}.")

    # ── STRIKES Y TARGETS ────────────────────────────────────
    if is_bull:
        suggested_strike = round(current_price * 1.05 / 5) * 5 if current_price >= 10 else round(current_price * 1.05, 1)
        target1 = round(current_price * 1.05, 2)
        target2 = round(current_price * 1.10, 2)
        stop    = round(current_price * 0.95, 2)
    else:
        suggested_strike = round(current_price * 0.95 / 5) * 5 if current_price >= 10 else round(current_price * 0.95, 1)
        target1 = round(current_price * 0.95, 2)
        target2 = round(current_price * 0.90, 2)
        stop    = round(current_price * 1.05, 2)

    # ── ATR-based stop (más preciso) ──────────────────────────
    if atr > 0:
        atr_stop = round(current_price - 1.5*atr, 2) if is_bull else round(current_price + 1.5*atr, 2)
    else:
        atr_stop = stop

    st_bulls = sent.get("bullish")
    st_bears = sent.get("bearish")
    st_label = f"Bulls {st_bulls:.0f}% · Bears {st_bears:.0f}%" if st_bulls and st_bears else "Sin datos"

    return {
        "direction":    direction,
        "score":        score,
        "semaforo":     semaforo,
        "pts_A_flujo":  round(pts_A, 1),
        "pts_B_catalyst": round(pts_B, 1),
        "pts_C_momentum": round(pts_C, 1),
        "pts_D_timing": round(pts_D, 1),
        "score_breakdown": f"Flujo {pts_A:.1f} + Catalyst {pts_B:.1f} + Mom {pts_C:.1f} + Timing {pts_D:.1f} = {score}",
        "momentum":     momentum,
        "rsi_label":    rsi_label,
        "vol_label":    vol_label,
        "why":          " | ".join(why),
        "risk":         " | ".join(risk),
        "st_sentiment": st_label,
        "suggested_strike": suggested_strike,
        "target1":      target1,
        "target2":      target2,
        "stop":         stop,
        "atr_stop":     atr_stop,
        "vwap":         vwap,
        "vwap_trigger": f"Sobre VWAP ${vwap:.2f}" if is_bull else f"Bajo VWAP ${vwap:.2f}",
    }


# ══════════════════════════════════════════════════════════════
# CAPA 4: ANÁLISIS COMPLETO DE UN TICKER
# ══════════════════════════════════════════════════════════════

def analyze_ticker(ticker: str, market: Dict = None, alert_context: str = "") -> Dict[str, Any]:
    ticker = ticker.upper().strip()
    if not market:
        market = {}

    # Recolectar datos — cada función hace UNA cosa
    price_data = get_price_data(ticker)
    fund_data  = get_fundamentals(ticker)
    opts_data  = get_options_data(ticker)
    news_data  = get_news_score(ticker)
    sent_data  = get_sentiment(ticker)

    if not price_data.get("price"):
        return {"ticker": ticker, "error": f"Sin datos para {ticker}", "score": 0, "semaforo": "ROJO"}

    scoring = compute_score(
        ticker, price_data, fund_data, opts_data,
        news_data, sent_data, market, alert_context
    )

    p = price_data.get("price", 0)

    return {
        "ticker":          ticker,
        "direction":       scoring["direction"],
        "type":            scoring["direction"],
        "score":           scoring["score"],
        "semaforo":        scoring["semaforo"],
        # Score breakdown por categoría
        "pts_flujo":       scoring["pts_A_flujo"],
        "pts_catalyst":    scoring["pts_B_catalyst"],
        "pts_momentum":    scoring["pts_C_momentum"],
        "pts_timing":      scoring["pts_D_timing"],
        "score_breakdown": scoring["score_breakdown"],
        # Precio
        "price":           f"${p:.2f}" if p else "N/A",
        "spot":            p,
        "open":            price_data.get("open", 0),
        "high":            price_data.get("high", 0),
        "low":             price_data.get("low", 0),
        "vwap":            f"${scoring['vwap']:.2f}",
        "change_pct":      f"{price_data.get('change_pct',0):+.2f}%",
        "change_abs":      f"${price_data.get('change_abs',0):+.2f}",
        "precio_vs_vwap":  f"{price_data.get('precio_vs_vwap',0):+.2f}%",
        "day_range_pos":   f"{price_data.get('day_range_pos',0.5)*100:.0f}% del rango",
        # Volumen
        "volume":          price_data.get("volume", 0),
        "avg_volume":      price_data.get("avg_volume", 0),
        "rel_volume":      price_data.get("rel_volume", 1.0),
        "vol_vs_avg":      scoring["vol_label"],
        # Técnico
        "rsi":             scoring["rsi_label"],
        "rsi_value":       price_data.get("rsi", 50),
        "atr":             f"${price_data.get('atr',0):.2f}",
        "momentum":        scoring["momentum"],
        "ma20_pct":        price_data.get("sma20_pct", "N/A"),
        "ma50_pct":        price_data.get("sma50_pct", "N/A"),
        "ma200_pct":       price_data.get("sma200_pct", "N/A"),
        "above_sma20":     price_data.get("above_sma20", False),
        "above_sma50":     price_data.get("above_sma50", False),
        "above_sma200":    price_data.get("above_sma200", False),
        "perf_week":       price_data.get("perf_week", "N/A"),
        "perf_month":      price_data.get("perf_month", "N/A"),
        # Fundamentales
        "short_float":     f"{fund_data.get('short_pct',0):.1f}%",
        "short_pct":       fund_data.get("short_pct", 0),
        "beta":            fund_data.get("beta", "N/A"),
        "sector":          fund_data.get("sector", "N/A"),
        "earnings_date":   fund_data.get("earnings_date", "N/A"),
        "earnings_days":   fund_data.get("earnings_days"),
        "analyst_action":  fund_data.get("analyst_action"),
        "target_price":    f"${fund_data.get('target_price',0):.2f}" if fund_data.get("target_price") else "N/A",
        "recommendation":  fund_data.get("recommendation", "N/A"),
        "high_52w":        f"${fund_data.get('high_52w',0):.2f}" if fund_data.get("high_52w") else "N/A",
        "low_52w":         f"${fund_data.get('low_52w',0):.2f}" if fund_data.get("low_52w") else "N/A",
        "inst_own":        fund_data.get("inst_own", "N/A"),
        # Opciones
        "has_options":     opts_data.get("has_options", False),
        "opt_vol_oi":      opts_data.get("vol_oi", 0),
        "opt_call_vol_oi": opts_data.get("call_vol_oi", 0),
        "opt_put_vol_oi":  opts_data.get("put_vol_oi", 0),
        "opt_pc_ratio":    opts_data.get("pc_ratio", 1.0),
        "opt_dominant":    opts_data.get("dominant", "N/A"),
        "opt_is_sweep":    opts_data.get("is_sweep", False),
        "opt_call_wall":   opts_data.get("call_wall"),
        "opt_put_wall":    opts_data.get("put_wall"),
        "opt_max_pain":    opts_data.get("max_pain"),
        "opt_top_strikes": opts_data.get("top_strikes", []),
        "opt_signal": (
            "🚨 SWEEP" if opts_data.get("is_sweep")
            else "🔥 MUY INUSUAL" if opts_data.get("vol_oi", 0) >= 3.0
            else "⚡ INUSUAL" if opts_data.get("vol_oi", 0) >= 1.5
            else "Normal" if opts_data.get("has_options") else "Sin datos"
        ),
        # Gamma
        "gamma_score":     opts_data.get("gamma_score", 0),
        "gamma_signal":    opts_data.get("gamma_signal", "NONE"),
        "gamma_triggers":  opts_data.get("gamma_triggers", []),
        "dealer_pressure": opts_data.get("dealer_pressure", "NEUTRAL"),
        # Noticias
        "news_score":      news_data.get("score", 0),
        "news_title":      news_data.get("title"),
        "news_age_hours":  news_data.get("age_hours"),
        "catalyst":        news_data.get("title") or fund_data.get("analyst_action") or "Sin catalizador reciente",
        # Sentimiento
        "st_sentiment":    scoring["st_sentiment"],
        # Análisis
        "why":             scoring["why"],
        "risk":            scoring["risk"],
        # Operación sugerida
        "strike":          scoring["suggested_strike"],
        "stop":            f"${scoring['stop']:.2f}",
        "atr_stop":        f"${scoring['atr_stop']:.2f}",
        "target_1":        f"${scoring['target1']:.2f}",
        "target_2":        f"${scoring['target2']:.2f}",
        "vwap_trigger":    scoring["vwap_trigger"],
        "ez":              f"${p*0.98:.2f}–${p:.2f}" if p else "N/A",
        "expiry":          "Ver cadena en IBKR",
        "contract":        f"{ticker} {scoring['direction']} ${scoring['suggested_strike']:.0f}",
        "sl":              f"${scoring['atr_stop']:.2f}",
        # Contexto
        "sentiment":       "POSITIVO" if price_data.get("change_pct",0) > 0 else "NEGATIVO",
        "control":         "COMPRADORES" if price_data.get("change_pct",0) > 0 and price_data.get("rel_volume",1) >= 1.5 else "VENDEDORES",
        "source":          "yahoo_finance",
        "timestamp":       datetime.now().isoformat(timespec="seconds"),
    }


# ══════════════════════════════════════════════════════════════
# RUTAS HTML
# ══════════════════════════════════════════════════════════════

@app.route("/")
def home():
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True, "service": "FlowScan 9", "version": "5.0",
        "sources": ["yahoo_finance", "stocktwits"],
        "scoring": "4-factor: Flujo + Catalizador + Momentum + Timing",
        "time": datetime.now().isoformat(timespec="seconds"),
    })


# ══════════════════════════════════════════════════════════════
# ENDPOINTS PRINCIPALES
# ══════════════════════════════════════════════════════════════

@app.route("/api/pulse")
def api_pulse():
    """Sentimiento de mercado: SPY, QQQ, IWM, VIX."""
    return jsonify(get_market_pulse())


@app.route("/api/scan")
def api_scan():
    """
    Scan autónomo — descubre y analiza las mejores oportunidades del mercado HOY.
    Sin listas hardcodeadas. Solo fuentes de mercado en vivo.
    """
    market = get_market_pulse()

    # Paso 1: descubrir candidatos desde screeners de Yahoo
    candidates = discover_candidates()
    if not candidates:
        return jsonify({"error": "Todos los feeds de Yahoo fallaron. Verifica conectividad.", "items": []}), 503

    # Paso 2: analizar los top candidatos con análisis completo
    # Analizamos más de los que mostramos para filtrar mejor
    items = []
    for cand in candidates[:20]:
        result = analyze_ticker(cand["ticker"], market=market)
        if "error" not in result:
            items.append(result)

    # Paso 3: ordenar por score descendente
    items.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Métricas del scan
    verdes  = sum(1 for x in items if x.get("semaforo") == "VERDE")
    sweeps  = sum(1 for x in items if x.get("opt_is_sweep"))
    gammas  = sum(1 for x in items if x.get("gamma_score", 0) >= 5)

    top = items[0] if items else None
    if verdes > 0:
        summary = f"{verdes} setup(s) VERDE de {len(items)} analizados. Mejor: {top['ticker']} {top['score']}/10 ({top['direction']})."
        if sweeps:
            summary += f" 🚨 {sweeps} sweep(s) institucional(es)."
        if gammas:
            summary += f" ⚡ {gammas} posible(s) gamma squeeze."
    else:
        summary = f"{len(items)} tickers analizados. Sin señales verdes claras hoy — esperar mejor setup."

    return jsonify({
        "source":    "live_market_scan",
        "market":    market,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "summary":   summary,
        "verdes":    verdes,
        "sweeps":    sweeps,
        "gammas":    gammas,
        "analyzed":  len(items),
        "items":     items,
    })


@app.route("/api/ticker/<ticker>")
def api_ticker(ticker: str):
    """Análisis completo de un ticker específico."""
    market = get_market_pulse()
    result = analyze_ticker(ticker.upper(), market=market)
    if "error" in result:
        return jsonify(result), 404
    return jsonify({"ticker": ticker.upper(), "market": market, "items": [result]})


@app.route("/api/flow")
def api_flow():
    """Analiza tickers específicos: /api/flow?tickers=NVDA,ASTS,CRCL"""
    tickers_raw = request.args.get("tickers", "")
    if not tickers_raw:
        return api_scan()

    market  = get_market_pulse()
    tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()][:10]
    items   = []
    for tk in tickers:
        result = analyze_ticker(tk, market=market)
        if "error" not in result:
            items.append(result)

    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")
    return jsonify({
        "source":    "yahoo_finance",
        "market":    market,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "summary":   f"{verdes} verde(s) de {len(items)} analizados." if verdes else f"{len(items)} analizados. Sin señales verdes.",
        "items":     items,
    })


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """Analiza alertas del grupo Discord/Telegram."""
    payload     = request.get_json(force=True, silent=True) or {}
    tickers_raw = payload.get("tickers", "")
    alert_raw   = payload.get("alert_raw", "")
    direction   = payload.get("direction", "")
    strike      = payload.get("strike", "")
    expiry      = payload.get("expiry", "")
    zona        = payload.get("zona", "")
    premium     = payload.get("premium", "")
    comment     = payload.get("comment", "")

    if not tickers_raw:
        return jsonify({"error": "Falta el campo tickers"}), 400

    market  = get_market_pulse()
    tickers = [t.strip().upper() for t in str(tickers_raw).split(",") if t.strip()]

    ctx_parts = [p for p in [alert_raw, f"Dirección: {direction}" if direction else "",
                              f"Strike: ${strike}" if strike else "",
                              f"Vence: {expiry}" if expiry else "",
                              f"Zona: ${zona}" if zona else "",
                              f"Premium: {premium}" if premium else "",
                              comment] if p]
    ctx = " | ".join(ctx_parts)

    items = []
    for tk in tickers:
        result = analyze_ticker(tk, market=market, alert_context=ctx)
        if "error" not in result:
            if direction:
                result["direction"] = direction.upper()
                result["type"] = direction.upper()
            if strike:
                try: result["strike"] = float(strike)
                except: pass
            if expiry:
                result["expiry"] = expiry
            if zona:
                result["ez"] = f"${zona} zona de interés"
            items.append(result)

    if not items:
        return jsonify({"error": "Sin datos disponibles", "items": [], "market": market})

    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")
    return jsonify({
        "source":    "yahoo_finance",
        "market":    market,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "summary":   f"{verdes} setup(s) VERDE." if verdes else "Sin señales verdes claras.",
        "items":     items,
    })


@app.route("/api/options/<ticker>")
def api_options(ticker: str):
    """Cadena de opciones y gamma squeeze de un ticker: /api/options/NVDA"""
    ticker = ticker.upper().strip()
    pd     = get_price_data(ticker)
    if not pd.get("price"):
        return jsonify({"ticker": ticker, "error": "Sin precio", "ok": False}), 404
    opts = get_options_data(ticker)
    return jsonify({
        "ticker":      ticker, "ok": True,
        "price":       pd.get("price"),
        "timestamp":   datetime.now().isoformat(timespec="seconds"),
        **opts,
    })


@app.route("/api/gamma/<ticker>")
def api_gamma(ticker: str):
    """Análisis gamma squeeze específico: /api/gamma/NVDA"""
    ticker = ticker.upper().strip()
    pd   = get_price_data(ticker)
    opts = get_options_data(ticker)
    return jsonify({
        "ticker":          ticker,
        "price":           pd.get("price"),
        "timestamp":       datetime.now().isoformat(timespec="seconds"),
        "gamma_score":     opts.get("gamma_score", 0),
        "gamma_signal":    opts.get("gamma_signal", "NONE"),
        "gamma_triggers":  opts.get("gamma_triggers", []),
        "call_wall":       opts.get("call_wall"),
        "put_wall":        opts.get("put_wall"),
        "max_pain":        opts.get("max_pain"),
        "atm_oi_conc":     opts.get("atm_oi_concentration"),
        "dealer_pressure": opts.get("dealer_pressure", "NEUTRAL"),
    })


# ══════════════════════════════════════════════════════════════
# WATCHLIST — solo para monitoreo personal, no afecta el scan
# ══════════════════════════════════════════════════════════════

@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    tickers = load_watchlist()
    return jsonify({"tickers": tickers, "count": len(tickers),
                    "timestamp": datetime.now().isoformat(timespec="seconds")})

@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_post():
    payload = request.get_json(force=True, silent=True) or {}
    current = load_watchlist()

    if "tickers" in payload:
        new_list = [t.upper().strip() for t in payload["tickers"] if t.strip()]
        save_watchlist(new_list)
        return jsonify({"ok": True, "action": "replaced", "tickers": new_list, "count": len(new_list)})
    if "add" in payload:
        to_add = [t.upper().strip() for t in payload["add"] if t.strip()]
        merged = list(dict.fromkeys(current + to_add))
        save_watchlist(merged)
        return jsonify({"ok": True, "action": "added", "added": to_add, "tickers": merged, "count": len(merged)})
    if "remove" in payload:
        to_remove = {t.upper().strip() for t in payload["remove"]}
        filtered = [t for t in current if t not in to_remove]
        save_watchlist(filtered)
        return jsonify({"ok": True, "action": "removed", "removed": list(to_remove), "tickers": filtered, "count": len(filtered)})
    return jsonify({"error": "Payload must have 'tickers', 'add', or 'remove'"}), 400

@app.route("/api/watchlist/scan")
def api_watchlist_scan():
    """Escanea los tickers de tu watchlist personal."""
    tickers = load_watchlist()
    if not tickers:
        return jsonify({"error": "Watchlist vacía. Agrega tickers via POST /api/watchlist", "items": []})
    market = get_market_pulse()
    items  = []
    for tk in tickers:
        result = analyze_ticker(tk, market=market)
        if "error" not in result:
            items.append(result)
    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")
    sweeps = sum(1 for x in items if x.get("opt_is_sweep"))
    return jsonify({
        "source":    "watchlist_scan",
        "market":    market,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "watchlist": tickers,
        "count":     len(items),
        "verdes":    verdes,
        "sweeps":    sweeps,
        "summary":   f"{verdes} verde(s) · {sweeps} sweep(s) de {len(items)} en watchlist",
        "items":     items,
    })


@app.route("/api/debug")
def api_debug():
    out = {}
    pd   = get_price_data("NVDA")
    out["price"]    = {"ok": bool(pd.get("price")), "price": pd.get("price"), "rel_vol": pd.get("rel_volume")}
    opts = get_options_data("NVDA")
    out["options"]  = {"ok": opts.get("has_options"), "vol_oi": opts.get("vol_oi"), "gamma": opts.get("gamma_score")}
    news = get_news_score("NVDA")
    out["news"]     = {"ok": bool(news.get("title")), "score": news.get("score"), "title": news.get("title")}
    cands = discover_candidates()
    out["discover"] = {"ok": len(cands) > 0, "count": len(cands), "top5": [c["ticker"] for c in cands[:5]]}
    pulse = get_market_pulse()
    out["market"]   = {"sentiment": pulse.get("sentiment"), "vix": pulse.get("vix_price")}
    out["watchlist"]= {"count": len(load_watchlist())}
    out["timestamp"] = datetime.now().isoformat()
    return jsonify(out)


# ── START ─────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
