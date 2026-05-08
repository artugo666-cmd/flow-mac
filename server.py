
from __future__ import annotations

import os
import re
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
# WATCHLIST PERSISTENTE
# Guardada en watchlist.json en el mismo directorio del server.
# Se puede editar via /api/watchlist (GET/POST/DELETE).
# Siempre se escanea independientemente de los screeners.
# ══════════════════════════════════════════════════════════════

WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")

DEFAULT_WATCHLIST = [
    "NVDA","AMD","TSLA","META","AAPL","MSFT","GOOGL","AMZN",
    "INTC","QCOM","PLTR","SMCI","MSTR","COIN","ARM","CRWD",
    "NFLX","UBER","SHOP","SQ","SOFI","RIVN","NIO","MU","AVGO",
]

def load_watchlist() -> List[str]:
    """Load watchlist from JSON file. Falls back to default if missing."""
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                tickers = [t.upper().strip() for t in data.get("tickers", []) if t.strip()]
                return tickers if tickers else DEFAULT_WATCHLIST
    except Exception as e:
        print(f"Watchlist load error: {e}")
    return list(DEFAULT_WATCHLIST)

def save_watchlist(tickers: List[str]) -> bool:
    """Save watchlist to JSON file."""
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump({
                "tickers":  [t.upper().strip() for t in tickers],
                "updated":  datetime.now().isoformat(timespec='seconds'),
                "count":    len(tickers),
            }, f, indent=2)
        return True
    except Exception as e:
        print(f"Watchlist save error: {e}")
        return False

# Initialize watchlist file if missing
if not os.path.exists(WATCHLIST_FILE):
    save_watchlist(DEFAULT_WATCHLIST)


# ══════════════════════════════════════════════════════════════
# CATALYST DETECTION (pre-movimiento)
# Detecta catalizadores ANTES de que el precio se mueva:
#   - Analyst upgrades/downgrades recientes (Yahoo quoteSummary)
#   - Earnings próximos (< 7 días)
#   - Implied Volatility spike vs histórico
#   - Noticias con palabras clave de alto impacto
# ══════════════════════════════════════════════════════════════

# Palabras clave de alto impacto en títulos de noticias
HIGH_IMPACT_KEYWORDS = [
    # Analyst actions
    "upgrade","upgraded","downgrade","downgraded","outperform","overweight",
    "price target","raises target","cuts target","initiates","initiated",
    # Corporate events
    "merger","acquisition","acquired","buyout","takeover","deal","partnership",
    "contract","awarded","wins","joint venture","collaboration","licensing",
    # Earnings/guidance
    "beats","beat","misses","miss","raises guidance","lowers guidance",
    "revenue beat","eps beat","record revenue","record earnings",
    # Regulatory/macro
    "fda approval","approved","clearance","sec","investigation","lawsuit",
    "tariff","sanction","export","ban","restriction",
    # Tech/product
    "launch","launches","released","announces","breakthrough","ai chip",
    "snapdragon","foundry","fab","manufacturing",
]

IMPACT_SCORE_MAP = {
    # Highest impact
    "upgrade": 3, "upgraded": 3, "merger": 3, "acquisition": 3,
    "acquired": 3, "buyout": 3, "fda approval": 3, "approved": 3,
    "beats": 2, "beat": 2, "record revenue": 3, "record earnings": 3,
    # High impact
    "outperform": 2, "overweight": 2, "raises target": 2, "price target": 1,
    "partnership": 2, "contract": 2, "awarded": 2, "wins": 2,
    "raises guidance": 2, "revenue beat": 2, "eps beat": 2,
    "launch": 1, "launches": 1, "announced": 1, "breakthrough": 2,
    # Moderate
    "downgrade": -2, "downgraded": -2, "misses": -2, "miss": -2,
    "lowers guidance": -2, "investigation": -1, "lawsuit": -1,
    "ban": -1, "restriction": -1,
}

def score_news_title(title: str) -> Tuple[int, List[str]]:
    """
    Score a news title by keyword matches.
    Returns (score, matched_keywords).
    Positive = bullish catalyst, Negative = bearish catalyst.
    """
    title_lower = title.lower()
    total_score = 0
    matched = []
    for kw, pts in IMPACT_SCORE_MAP.items():
        if kw in title_lower:
            total_score += pts
            matched.append(kw)
    return total_score, matched


def get_catalyst_data(ticker: str) -> Dict[str, Any]:
    """
    Detect pre-movement catalysts for a ticker:
    1. Recent analyst actions (upgrades/downgrades/target changes)
    2. Earnings proximity (< 7 days = high alert)
    3. News impact score from keyword analysis
    4. Implied volatility rank (IV spike = options market expecting move)

    Returns a catalyst dict with signal strength 0-10.
    """
    result = {
        "ticker":           ticker,
        "catalyst_score":   0,
        "catalyst_signal":  "NONE",
        "analyst_action":   None,
        "earnings_days":    None,
        "earnings_alert":   False,
        "iv_rank":          None,
        "iv_spike":         False,
        "top_news_score":   0,
        "top_news_title":   None,
        "top_news_kws":     [],
        "catalysts_found":  [],
    }

    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
            params={"modules": "upgradeDowngradeHistory,calendarEvents,financialData,defaultKeyStatistics"},
            headers=HEADERS, timeout=12
        )
        r.raise_for_status()
        res = r.json().get("quoteSummary", {}).get("result", [])
        if not res:
            return result

        # ── 1. ANALYST UPGRADES / DOWNGRADES ─────────────────
        udh = res[0].get("upgradeDowngradeHistory", {}).get("history", [])
        if udh:
            # Look at last 5 days of analyst actions
            cutoff = datetime.now().timestamp() - (5 * 86400)
            recent_actions = [a for a in udh if a.get("epochGradeDate", 0) >= cutoff]
            if recent_actions:
                latest = recent_actions[0]
                action  = latest.get("action", "")
                firm    = latest.get("firm", "")
                to_grd  = latest.get("toGrade", "")
                from_grd = latest.get("fromGrade", "")
                action_str = f"{firm}: {action} → {to_grd}" if to_grd else f"{firm}: {action}"
                result["analyst_action"] = action_str

                if action.lower() in ("up", "upgrade", "init"):
                    result["catalyst_score"] += 3
                    result["catalysts_found"].append(f"Upgrade reciente: {action_str}")
                elif action.lower() in ("down", "downgrade"):
                    result["catalyst_score"] -= 2
                    result["catalysts_found"].append(f"Downgrade reciente: {action_str}")
                elif action.lower() in ("main", "reit", "reiterate"):
                    result["catalyst_score"] += 1
                    result["catalysts_found"].append(f"Reiteración: {action_str}")

        # ── 2. EARNINGS PROXIMITY ─────────────────────────────
        cal = res[0].get("calendarEvents", {})
        earn_dates = cal.get("earnings", {}).get("earningsDate", [])
        if earn_dates:
            ts = earn_dates[0].get("raw", 0)
            if ts:
                days_until = (ts - datetime.now().timestamp()) / 86400
                result["earnings_days"] = round(days_until, 0)
                if 0 <= days_until <= 2:
                    result["catalyst_score"] += 4
                    result["earnings_alert"] = True
                    result["catalysts_found"].append(f"⚠️ EARNINGS HOY/MAÑANA ({days_until:.0f}d)")
                elif days_until <= 5:
                    result["catalyst_score"] += 3
                    result["earnings_alert"] = True
                    result["catalysts_found"].append(f"Earnings en {days_until:.0f} días — volatilidad inminente")
                elif days_until <= 14:
                    result["catalyst_score"] += 1
                    result["catalysts_found"].append(f"Earnings en {days_until:.0f} días")

        # ── 3. IMPLIED VOLATILITY from options chain ──────────
        # Use currentRatio of IV from financialData as proxy
        fd = res[0].get("financialData", {})
        ks = res[0].get("defaultKeyStatistics", {})

        # Beta as volatility proxy when IV not available
        beta_raw = ks.get("beta", {})
        beta = float(beta_raw.get("raw", 0)) if isinstance(beta_raw, dict) else float(beta_raw or 0)
        if beta >= 2.0:
            result["iv_spike"] = True
            result["catalyst_score"] += 1
            result["catalysts_found"].append(f"Beta {beta:.1f} — alta volatilidad implícita")

    except Exception as e:
        print(f"Catalyst data error {ticker}: {e}")

    # ── 4. NEWS IMPACT SCORE ──────────────────────────────────
    try:
        r2 = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": ticker, "newsCount": 8, "quotesCount": 0},
            headers=HEADERS, timeout=8
        )
        r2.raise_for_status()
        news_items = r2.json().get("news", [])
        best_score = 0
        best_title = None
        best_kws   = []

        for item in news_items:
            title = item.get("title", "")
            if not title:
                continue
            # Only consider news from last 48 hours
            pub_time = item.get("providerPublishTime", 0)
            if pub_time and (datetime.now().timestamp() - pub_time) > 172800:
                continue
            ns, kws = score_news_title(title)
            if abs(ns) > abs(best_score):
                best_score = ns
                best_title = title
                best_kws   = kws

        result["top_news_score"] = best_score
        result["top_news_title"] = best_title
        result["top_news_kws"]   = best_kws

        if best_score >= 3:
            result["catalyst_score"] += 3
            result["catalysts_found"].append(f"Noticia alto impacto ({best_score}pts): {best_title[:70] if best_title else ''}")
        elif best_score >= 2:
            result["catalyst_score"] += 2
            result["catalysts_found"].append(f"Noticia relevante: {best_title[:70] if best_title else ''}")
        elif best_score >= 1:
            result["catalyst_score"] += 1
            result["catalysts_found"].append(f"Noticia: {best_title[:70] if best_title else ''}")
        elif best_score <= -2:
            result["catalyst_score"] -= 2
            result["catalysts_found"].append(f"Noticia negativa: {best_title[:70] if best_title else ''}")

    except Exception as e:
        print(f"News catalyst error {ticker}: {e}")

    # ── SIGNAL LABEL ──────────────────────────────────────────
    cs = result["catalyst_score"]
    if cs >= 6:
        result["catalyst_signal"] = "🚨 CATALIZADOR FUERTE"
    elif cs >= 4:
        result["catalyst_signal"] = "⚡ CATALIZADOR MODERADO"
    elif cs >= 2:
        result["catalyst_signal"] = "📌 CATALIZADOR LEVE"
    elif cs <= -2:
        result["catalyst_signal"] = "⚠️ CATALIZADOR NEGATIVO"
    else:
        result["catalyst_signal"] = "NONE"

    return result


# ── SERVE HTML ────────────────────────────────────────────────
@app.route('/')
def home():
    with open(os.path.join(BASE_DIR, 'index.html'), 'r', encoding='utf-8') as f:
        return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/api/health')
def health():
    return jsonify({
        'ok': True,
        'service': 'FlowScan 8',
        'version': '4.0',
        'sources': ['yahoo_finance', 'finviz', 'market_chameleon', 'stocktwits'],
        'time': datetime.now().isoformat(timespec='seconds'),
    })


# ══════════════════════════════════════════════════════════════
# DATA SOURCES
# ══════════════════════════════════════════════════════════════

def get_yahoo(ticker: str) -> Dict[str, Any]:
    """
    Near real-time stock price from Yahoo Finance.
    Uses 1-minute interval for current price (1-2 min delay max).
    Falls back to daily for volume averages.
    """
    try:
        # 1-minute chart for near real-time price
        r1 = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1m", "range": "1d"},
            headers=HEADERS,
            timeout=10
        )
        r1.raise_for_status()
        d1     = r1.json()
        res1   = d1.get("chart", {}).get("result", [])
        meta1  = res1[0].get("meta", {}) if res1 else {}
        quote1 = res1[0].get("indicators", {}).get("quote", [{}])[0] if res1 else {}

        # Get most recent valid price from 1m candles
        closes_1m = [c for c in (quote1.get("close") or []) if c]
        vols_1m   = [v for v in (quote1.get("volume") or []) if v]
        price     = round(float(closes_1m[-1]), 2) if closes_1m else float(meta1.get("regularMarketPrice") or 0)
        vol_today = int(sum(vols_1m)) if vols_1m else int(meta1.get("regularMarketVolume") or 0)

        # VWAP from 1m data
        highs  = [h for h in (quote1.get("high") or []) if h]
        lows   = [l for l in (quote1.get("low") or []) if l]
        closes = [c for c in (quote1.get("close") or []) if c]
        if highs and lows and closes and vols_1m:
            tp  = [(h+l+c)/3 for h,l,c in zip(highs,lows,closes)]
            tpv = [t*v for t,v in zip(tp, vols_1m)]
            vwap = round(sum(tpv)/sum(vols_1m), 2) if sum(vols_1m) > 0 else price
        else:
            vwap = price

        # 5-day daily for avg volume calculation
        r2 = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "30d"},
            headers=HEADERS,
            timeout=10
        )
        r2.raise_for_status()
        d2     = r2.json()
        res2   = d2.get("chart", {}).get("result", [])
        meta2  = res2[0].get("meta", {}) if res2 else {}
        quote2 = res2[0].get("indicators", {}).get("quote", [{}])[0] if res2 else {}
        vols_d = [v for v in (quote2.get("volume") or []) if v]
        # Use last 20 days for avg volume (excludes today)
        avg_vol = int(sum(vols_d[-21:-1]) / 20) if len(vols_d) >= 21 else int(sum(vols_d) / max(len(vols_d), 1))

        prev    = float(meta1.get("chartPreviousClose") or meta2.get("chartPreviousClose") or 0)
        day_hi  = float(meta1.get("regularMarketDayHigh") or 0)
        day_lo  = float(meta1.get("regularMarketDayLow") or 0)
        day_op  = float(meta1.get("regularMarketOpen") or 0)
        # Use Yahoo's own change calculation (most accurate)
        chg_pct = round(float(meta1.get("regularMarketChangePercent") or meta2.get("regularMarketChangePercent") or 0), 2)
        chg_abs = round(float(meta1.get("regularMarketChange") or meta2.get("regularMarketChange") or 0), 2)
        # If still zero, calculate from prev close
        if chg_pct == 0.0 and prev and price:
            chg_abs = round(price - prev, 2)
            chg_pct = round(chg_abs / prev * 100, 2) if prev else 0
        rel_vol = round(vol_today / avg_vol, 2) if avg_vol > 0 else 1.0

        return {
            "price":      price,
            "prev_close": round(prev, 2),
            "open":       round(day_op, 2),
            "high":       round(day_hi, 2),
            "low":        round(day_lo, 2),
            "volume":     vol_today,
            "avg_volume": avg_vol,
            "rel_volume": rel_vol,
            "vwap":       vwap,
            "change_pct": chg_pct,
            "change_abs": chg_abs,
            "market_cap": meta2.get("marketCap") or meta1.get("marketCap"),
            "currency":   meta1.get("currency", "USD"),
            "data_delay": "~1-2 min (Yahoo 1m)",
        }
    except Exception as e:
        print(f"Yahoo error {ticker}: {e}")
        # Fallback to daily if 1m fails
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": "5d"},
                headers=HEADERS,
                timeout=10
            )
            r.raise_for_status()
            res  = r.json().get("chart", {}).get("result", [])
            meta = res[0].get("meta", {}) if res else {}
            q    = res[0].get("indicators", {}).get("quote", [{}])[0] if res else {}
            vols = [v for v in (q.get("volume") or []) if v]
            avg  = int(sum(vols[:-1]) / max(len(vols)-1, 1)) if len(vols) > 1 else 1
            price = float(meta.get("regularMarketPrice") or 0)
            prev  = float(meta.get("chartPreviousClose") or price)
            vol   = int(meta.get("regularMarketVolume") or 0)
            return {
                "price":      round(price, 2),
                "prev_close": round(prev, 2),
                "open":       round(float(meta.get("regularMarketOpen") or 0), 2),
                "high":       round(float(meta.get("regularMarketDayHigh") or 0), 2),
                "low":        round(float(meta.get("regularMarketDayLow") or 0), 2),
                "volume":     vol,
                "avg_volume": avg,
                "rel_volume": round(vol / avg, 2) if avg else 1.0,
                "vwap":       round(price, 2),
                "change_pct": round((price-prev)/prev*100, 2) if prev else 0,
                "change_abs": round(price-prev, 2),
                "data_delay": "~15 min (Yahoo 1d fallback)",
            }
        except:
            return {}


def get_finviz(ticker: str) -> Dict[str, Any]:
    """Get fundamentals and technicals from Yahoo Finance."""
    data = {}

    # 1. Fundamentals from quoteSummary
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
            params={"modules": "summaryDetail,defaultKeyStatistics,financialData,assetProfile,calendarEvents"},
            headers=HEADERS, timeout=12
        )
        r.raise_for_status()
        res = r.json().get("quoteSummary", {}).get("result", [])
        if res:
            sd  = res[0].get("summaryDetail", {})
            ks  = res[0].get("defaultKeyStatistics", {})
            fd  = res[0].get("financialData", {})
            ap  = res[0].get("assetProfile", {})
            cal = res[0].get("calendarEvents", {})

            def v(d, k):
                x = d.get(k, {})
                return (x.get("fmt") or x.get("raw")) if isinstance(x, dict) else x

            sf = v(ks, "shortPercentOfFloat")
            if sf is not None:
                try:
                    sfv = float(sf)
                    sfv = sfv * 100 if sfv < 1 else sfv
                    data["short_float"] = f"{sfv:.1f}%"
                    data["short_pct_raw"] = sfv
                except:
                    pass

            beta = v(sd, "beta")
            data["beta"] = f"{float(beta):.2f}" if beta else "N/A"

            tp = v(fd, "targetMeanPrice")
            data["target_price"] = f"${float(tp):.2f}" if tp else "N/A"

            rec = str(v(fd, "recommendationKey") or "")
            rec_map = {"strong_buy":"Strong Buy","buy":"Buy","hold":"Hold","sell":"Sell","strong_sell":"Strong Sell"}
            data["recommendation"] = rec_map.get(rec.lower(), rec.title()) if rec else "N/A"

            data["sector"]   = ap.get("sector", "N/A")
            data["industry"] = ap.get("industry", "N/A")

            ed = cal.get("earnings", {}).get("earningsDate", [])
            if ed:
                ts = ed[0].get("raw", 0)
                data["earnings_date"] = datetime.fromtimestamp(ts).strftime("%d-%b-%Y") if ts else "N/A"
            else:
                data["earnings_date"] = "N/A"

            h52 = v(sd, "fiftyTwoWeekHigh")
            l52 = v(sd, "fiftyTwoWeekLow")
            data["52w_high"] = f"${float(h52):.2f}" if h52 else "N/A"
            data["52w_low"]  = f"${float(l52):.2f}" if l52 else "N/A"

            ii = v(ks, "heldPercentInstitutions")
            data["inst_own"] = f"{float(ii)*100:.1f}%" if ii else "N/A"
            ins = v(ks, "heldPercentInsiders")
            data["insider_own"] = f"{float(ins)*100:.1f}%" if ins else "N/A"

    except Exception as e:
        print(f"quoteSummary error {ticker}: {e}")

    # 2. MAs, RSI, Performance from 1-year daily prices
    try:
        r2 = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "1y"},
            headers=HEADERS, timeout=12
        )
        r2.raise_for_status()
        res2 = r2.json().get("chart", {}).get("result", [])
        if res2:
            meta   = res2[0].get("meta", {})
            quote2 = res2[0].get("indicators", {}).get("quote", [{}])[0]
            closes = [c for c in (quote2.get("close") or []) if c is not None]
            highs  = [h for h in (quote2.get("high") or [])  if h is not None]
            lows   = [l for l in (quote2.get("low") or [])   if l is not None]
            price  = float(meta.get("regularMarketPrice") or 0)

            if closes and price:
                def sma(n):
                    return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

                def pct(ma):
                    if ma and price:
                        d = round((price - ma) / ma * 100, 2)
                        return f"+{d:.2f}%" if d >= 0 else f"{d:.2f}%"
                    return "N/A"

                data["sma5_pct"]   = pct(sma(5))
                data["sma20_pct"]  = pct(sma(20))
                data["sma50_pct"]  = pct(sma(50))
                data["sma200_pct"] = pct(sma(200))

                if len(closes) >= 15:
                    diffs  = [closes[i]-closes[i-1] for i in range(1, len(closes))][-14:]
                    gains  = [max(d, 0) for d in diffs]
                    losses = [max(-d, 0) for d in diffs]
                    ag = sum(gains) / 14
                    al = sum(losses) / 14
                    rsi = round(100 - 100 / (1 + ag/al), 1) if al > 0 else 100.0
                    data["rsi14"] = str(rsi)

                def perf(days):
                    if len(closes) > days:
                        old = closes[-days-1]
                        p = (price - old) / old * 100
                        return f"+{p:.2f}%" if p >= 0 else f"{p:.2f}%"
                    return "N/A"

                data["perf_week"]  = perf(5)
                data["perf_month"] = perf(21)
                data["perf_ytd"]   = perf(min(len(closes)-1, 252))

                if len(highs) >= 15:
                    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
                           for i in range(max(1,len(highs)-14), len(highs))]
                    data["atr"] = f"${round(sum(trs)/len(trs), 2)}"

    except Exception as e:
        print(f"Yahoo historical error {ticker}: {e}")

    # 3. News
    try:
        r3 = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": ticker, "newsCount": 5, "quotesCount": 0},
            headers=HEADERS, timeout=8
        )
        r3.raise_for_status()
        data["news"] = [{"title": n.get("title",""), "url": n.get("link","")} for n in r3.json().get("news", [])[:5]]
    except:
        data["news"] = []

    return data


def get_stocktwits_sentiment(ticker: str) -> Dict[str, Any]:
    """Get bull/bear sentiment from Stocktwits."""
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json",
            headers=HEADERS,
            timeout=8
        )
        r.raise_for_status()
        data   = r.json()
        symbol = data.get("symbol", {})
        return {
            "watchlist_count": symbol.get("watchlist_count", 0),
            "bullish":  symbol.get("sentiment", {}).get("bullish") if symbol.get("sentiment") else None,
            "bearish":  symbol.get("sentiment", {}).get("bearish") if symbol.get("sentiment") else None,
        }
    except Exception as e:
        print(f"Stocktwits error {ticker}: {e}")
        return {}


def get_options_flow_real(ticker: str) -> Dict[str, Any]:
    """
    Fetch real options chain from Yahoo Finance for a specific ticker.
    Returns the REAL vol/OI ratio (the correct metric for unusual options activity).
    vol/OI > 1.5 = unusual, > 3.0 = very unusual, > 5.0 = institutional sweep.
    """
    try:
        # Step 1: Get available expiration dates
        r = requests.get(
            f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}",
            headers=HEADERS,
            timeout=12
        )
        r.raise_for_status()
        data = r.json()
        result = data.get("optionChain", {}).get("result", [])
        if not result:
            return {}

        dates = result[0].get("expirationDates", [])
        current_price = result[0].get("quote", {}).get("regularMarketPrice", 0)

        # Step 2: Analyze next 2 expirations (closest = most sensitive to unusual activity)
        best_call_vol_oi = 0.0
        best_put_vol_oi  = 0.0
        total_call_vol   = 0
        total_put_vol    = 0
        total_call_oi    = 0
        total_put_oi     = 0
        atm_call_vol_oi  = 0.0  # ATM = most significant signal
        atm_put_vol_oi   = 0.0
        dominant_side    = "NEUTRAL"
        top_strikes      = []

        for exp_ts in dates[:3]:  # Check next 3 expirations
            r2 = requests.get(
                f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}",
                params={"date": exp_ts},
                headers=HEADERS,
                timeout=12
            )
            r2.raise_for_status()
            d2 = r2.json()
            res2 = d2.get("optionChain", {}).get("result", [])
            if not res2:
                continue

            calls = res2[0].get("options", [{}])[0].get("calls", [])
            puts  = res2[0].get("options", [{}])[0].get("puts", [])

            for c in calls:
                vol = int(c.get("volume", 0) or 0)
                oi  = int(c.get("openInterest", 0) or 0)
                strike = float(c.get("strike", 0) or 0)
                total_call_vol += vol
                total_call_oi  += oi
                if oi > 50 and vol > 0:  # Minimum threshold to avoid noise
                    ratio = vol / oi
                    if ratio > best_call_vol_oi:
                        best_call_vol_oi = round(ratio, 2)
                    # ATM = within 5% of current price
                    if current_price and abs(strike - current_price) / current_price <= 0.05:
                        if ratio > atm_call_vol_oi:
                            atm_call_vol_oi = round(ratio, 2)
                    if ratio >= 1.5:
                        top_strikes.append({
                            "type": "CALL", "strike": strike,
                            "vol": vol, "oi": oi, "ratio": round(ratio, 2)
                        })

            for p in puts:
                vol = int(p.get("volume", 0) or 0)
                oi  = int(p.get("openInterest", 0) or 0)
                strike = float(p.get("strike", 0) or 0)
                total_put_vol += vol
                total_put_oi  += oi
                if oi > 50 and vol > 0:
                    ratio = vol / oi
                    if ratio > best_put_vol_oi:
                        best_put_vol_oi = round(ratio, 2)
                    if current_price and abs(strike - current_price) / current_price <= 0.05:
                        if ratio > atm_put_vol_oi:
                            atm_put_vol_oi = round(ratio, 2)
                    if ratio >= 1.5:
                        top_strikes.append({
                            "type": "PUT", "strike": strike,
                            "vol": vol, "oi": oi, "ratio": round(ratio, 2)
                        })

        # Put/Call ratio (< 0.7 = bullish, > 1.3 = bearish)
        pc_ratio = round(total_put_vol / total_call_vol, 2) if total_call_vol > 0 else 1.0

        # Dominant side by volume
        if total_call_vol > total_put_vol * 1.3:
            dominant_side = "CALL"
        elif total_put_vol > total_call_vol * 1.3:
            dominant_side = "PUT"
        else:
            dominant_side = "NEUTRAL"

        # Best vol/OI ratio overall (max of calls/puts, weighted toward dominant)
        best_vol_oi = max(best_call_vol_oi, best_put_vol_oi)

        # Sort top strikes by ratio
        top_strikes.sort(key=lambda x: x["ratio"], reverse=True)

        # Map vol/OI to rel_vol scale used by scoring engine
        # vol/OI >= 5.0 → institutional sweep → rel_vol 10
        # vol/OI >= 3.0 → very unusual       → rel_vol 5-7
        # vol/OI >= 1.5 → unusual             → rel_vol 3-4
        # vol/OI >= 0.8 → elevated            → rel_vol 1.5-2
        # vol/OI <  0.5 → normal              → rel_vol 1.0
        if best_vol_oi >= 5.0:
            mapped_rel_vol = 10.0
        elif best_vol_oi >= 3.0:
            mapped_rel_vol = round(5.0 + (best_vol_oi - 3.0) / 2.0 * 2, 1)
        elif best_vol_oi >= 1.5:
            mapped_rel_vol = round(3.0 + (best_vol_oi - 1.5) / 1.5 * 1, 1)
        elif best_vol_oi >= 0.8:
            mapped_rel_vol = round(1.5 + (best_vol_oi - 0.8) / 0.7 * 0.5, 1)
        else:
            mapped_rel_vol = 1.0

        return {
            "best_vol_oi":      best_vol_oi,
            "best_call_vol_oi": best_call_vol_oi,
            "best_put_vol_oi":  best_put_vol_oi,
            "atm_call_vol_oi":  atm_call_vol_oi,
            "atm_put_vol_oi":   atm_put_vol_oi,
            "total_call_vol":   total_call_vol,
            "total_put_vol":    total_put_vol,
            "total_call_oi":    total_call_oi,
            "total_put_oi":     total_put_oi,
            "pc_ratio":         pc_ratio,
            "dominant_side":    dominant_side,
            "mapped_rel_vol":   mapped_rel_vol,
            "top_strikes":      top_strikes[:5],
            "is_unusual":       best_vol_oi >= 1.5,
            "is_sweep":         best_vol_oi >= 5.0,
        }

    except Exception as e:
        print(f"Options chain error {ticker}: {e}")
        return {}


def get_options_unusual_screener() -> List[Dict[str, Any]]:
    """
    Detect tickers with unusual options activity by scanning Yahoo most-active
    and calculating REAL vol/OI ratios from the options chain.
    This replaces the broken UW scraper.

    Strategy:
    1. Get ~25 most active stocks from Yahoo screener
    2. Get Yahoo trending tickers
    3. For each candidate, fetch real options chain
    4. Filter by vol/OI >= 1.5 (unusual threshold)
    5. Return sorted by vol/OI descending (highest = most institutional)
    """
    candidates = set()

    # Source A: Yahoo most active (highest stock volume today)
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
            params={"scrIds": "most_actives", "count": 30},
            headers=HEADERS, timeout=10
        )
        r.raise_for_status()
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        for q in quotes:
            tk = q.get("symbol", "")
            if tk and len(tk) <= 5 and tk.isalpha():
                candidates.add(tk)
        print(f"Options screener: {len(candidates)} candidates from most_actives")
    except Exception as e:
        print(f"Most active error: {e}")

    # Source B: Yahoo trending
    try:
        r2 = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/trending/US",
            params={"count": 20},
            headers=HEADERS, timeout=10
        )
        r2.raise_for_status()
        quotes2 = r2.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        for q in quotes2:
            tk = q.get("symbol", "")
            if tk and len(tk) <= 5 and tk.isalpha():
                candidates.add(tk)
        print(f"Options screener: {len(candidates)} total candidates after trending")
    except Exception as e:
        print(f"Trending error: {e}")

    # Source C: Always include high-beta watchlist (never miss these)
    WATCHLIST = ["NVDA","AMD","TSLA","META","AAPL","MSFT","GOOGL","AMZN",
                 "INTC","QCOM","PLTR","SMCI","MSTR","COIN","ARM","CRWD",
                 "NFLX","UBER","SHOP","SQ","SOFI","LCID","RIVN","NIO"]
    for tk in WATCHLIST:
        candidates.add(tk)

    # Exclude ETFs and indices
    EXCLUDE = {"SPY","QQQ","IWM","VIX","TLT","GLD","SLV","XLF","XLE","XLK","DIA","EEM"}
    candidates -= EXCLUDE

    print(f"Options screener: scanning {len(candidates)} candidates for real vol/OI...")

    # Fetch options chain for each candidate and filter
    results = []
    checked = 0
    for ticker in list(candidates)[:35]:  # Cap at 35 to avoid timeout
        opts = get_options_flow_real(ticker)
        if not opts:
            continue
        checked += 1

        best_vol_oi   = opts.get("best_vol_oi", 0)
        mapped_rel_vol = opts.get("mapped_rel_vol", 1.0)
        dominant_side  = opts.get("dominant_side", "NEUTRAL")
        pc_ratio       = opts.get("pc_ratio", 1.0)
        is_unusual     = opts.get("is_unusual", False)

        # Get stock change % for context
        try:
            yq = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": "1d"},
                headers=HEADERS, timeout=6
            )
            yq.raise_for_status()
            meta = yq.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            chg_pct = float(meta.get("regularMarketChangePercent", 0))
            vol_today = int(meta.get("regularMarketVolume", 0))
            avg_vol = int(meta.get("averageDailyVolume3Month", 1) or 1)
            stock_rel_vol = round(vol_today / avg_vol, 2) if avg_vol else 1.0
        except:
            chg_pct = 0.0
            stock_rel_vol = 1.0

        results.append({
            "ticker":          ticker,
            "mc_volume":       opts.get("total_call_vol", 0) + opts.get("total_put_vol", 0),
            "mc_rel_vol":      mapped_rel_vol,      # ← NOW based on REAL vol/OI
            "mc_chg_pct":      chg_pct,
            "mc_bullish":      dominant_side == "CALL" or chg_pct >= 0,
            "source":          "yahoo_options_chain",
            # Extra options data passed through to scoring
            "opt_vol_oi":      best_vol_oi,
            "opt_call_vol_oi": opts.get("best_call_vol_oi", 0),
            "opt_put_vol_oi":  opts.get("best_put_vol_oi", 0),
            "opt_atm_call":    opts.get("atm_call_vol_oi", 0),
            "opt_atm_put":     opts.get("atm_put_vol_oi", 0),
            "opt_pc_ratio":    pc_ratio,
            "opt_dominant":    dominant_side,
            "opt_is_sweep":    opts.get("is_sweep", False),
            "opt_top_strikes": opts.get("top_strikes", []),
            "stock_rel_vol":   stock_rel_vol,
            "is_unusual":      is_unusual,
        })

    print(f"Options screener: checked {checked}, found {sum(1 for r in results if r['is_unusual'])} unusual")

    # Sort by vol/OI ratio descending — highest = most institutional interest
    results.sort(key=lambda x: x.get("opt_vol_oi", 0), reverse=True)

    # If nothing unusual found, return all sorted (so at least we have data)
    return results[:15]


def get_market_chameleon_unusual() -> List[Dict[str, Any]]:
    """
    Primary: Real options vol/OI from Yahoo Finance options chain.
    This replaces the broken Unusual Whales + Market Chameleon scrapers.
    """
    return get_options_unusual_screener()


def get_yahoo_most_active() -> List[Dict[str, Any]]:
    """Get most active stocks from Yahoo Finance screener."""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
            params={"scrIds": "most_actives", "count": 25},
            headers=HEADERS,
            timeout=10
        )
        r.raise_for_status()
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        results = []
        for q in quotes:
            ticker = q.get("symbol", "")
            if not ticker or len(ticker) > 6:
                continue
            vol = int(q.get("regularMarketVolume", 0))
            avg = int(q.get("averageDailyVolume3Month", 1))
            rel = round(vol / avg, 2) if avg > 0 else 1.0
            chg = float(q.get("regularMarketChangePercent", 0))
            results.append({
                "ticker":     ticker,
                "mc_volume":  vol,
                "mc_rel_vol": rel,
                "mc_chg_pct": chg,
                "mc_bullish": chg >= 0,
                "source":     "yahoo_most_active",
            })
        results.sort(key=lambda x: x.get("mc_rel_vol", 0), reverse=True)
        print(f"Yahoo most active: {len(results)} tickers")
        return results[:15]
    except Exception as e:
        print(f"Yahoo most active error: {e}")
        return []

def get_yahoo_trending() -> List[Dict[str, Any]]:
    """Get trending tickers from Yahoo Finance."""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/trending/US",
            params={"count": 20},
            headers=HEADERS,
            timeout=10
        )
        r.raise_for_status()
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        results = []
        seen = set()
        for q in quotes:
            ticker = q.get("symbol", "")
            if not ticker or len(ticker) > 6 or ticker in seen:
                continue
            seen.add(ticker)
            results.append({
                "ticker":     ticker,
                "mc_volume":  0,
                "mc_rel_vol": 2.0,
                "mc_chg_pct": 0,
                "mc_bullish": True,
                "source":     "yahoo_trending",
            })
        print(f"Yahoo trending: {len(results)} tickers")
        return results[:15]
    except Exception as e:
        print(f"Yahoo trending error: {e}")
        return []


def get_market_pulse() -> Dict[str, Any]:
    """Get SPY, QQQ, IWM, VIX to determine market sentiment."""
    pulse = {}
    for sym in ['SPY', 'QQQ', 'IWM', '^VIX']:
        data = get_yahoo(sym)
        if data:
            key = 'VIX' if sym == '^VIX' else sym
            pulse[key] = {
                "price":      data.get("price", 0),
                "change_pct": data.get("change_pct", 0),
                "volume":     data.get("volume", 0),
                "rel_volume": data.get("rel_volume", 1.0),
            }

    # Determine overall sentiment
    spy_chg  = pulse.get("SPY", {}).get("change_pct", 0)
    qqq_chg  = pulse.get("QQQ", {}).get("change_pct", 0)
    iwm_chg  = pulse.get("IWM", {}).get("change_pct", 0)
    vix_chg  = pulse.get("VIX", {}).get("change_pct", 0)
    vix_price= pulse.get("VIX", {}).get("price", 0)
    if not vix_price:
        vix_data = get_yahoo("^VIX")
        vix_price = vix_data.get("price", 20)
        pulse["VIX"] = vix_data

    avg_market = (spy_chg + qqq_chg + iwm_chg) / 3
    if vix_price >= 30:
        sentiment = "MUY_VOLATIL"
        emoji     = "⚡"
        color     = "red"
    elif avg_market >= 0.5:
        sentiment = "ALCISTA"
        emoji     = "📈"
        color     = "green"
    elif avg_market <= -0.5:
        sentiment = "BAJISTA"
        emoji     = "📉"
        color     = "red"
    else:
        sentiment = "NEUTRAL"
        emoji     = "➡️"
        color     = "yellow"

    pulse["sentiment"]  = sentiment
    pulse["emoji"]      = emoji
    pulse["color"]      = color
    pulse["avg_change"] = round(avg_market, 2)
    pulse["vix_price"]  = vix_price
    pulse["vix_risk"]   = "ALTO" if vix_price >= 25 else "MODERADO" if vix_price >= 18 else "BAJO"

    return pulse


# ══════════════════════════════════════════════════════════════
# SCORING
# ══════════════════════════════════════════════════════════════

def compute_score(
    ticker: str,
    yahoo: Dict,
    finviz: Dict,
    st: Dict,
    market: Dict,
    alert_context: str = "",
    mc_data: Dict = None,
    catalyst: Dict = None,
) -> Dict[str, Any]:

    price     = yahoo.get("price", 0)
    chg_pct   = yahoo.get("change_pct", 0)
    rel_vol   = yahoo.get("rel_volume", 1.0)
    volume    = yahoo.get("volume", 0)

    # ── CATALYST DATA ─────────────────────────────────────────
    cat_score    = catalyst.get("catalyst_score", 0) if catalyst else 0
    cat_signal   = catalyst.get("catalyst_signal", "NONE") if catalyst else "NONE"
    cat_analyst  = catalyst.get("analyst_action") if catalyst else None
    cat_earn_days = catalyst.get("earnings_days") if catalyst else None
    cat_earn_alert = catalyst.get("earnings_alert", False) if catalyst else False
    cat_news_score = catalyst.get("top_news_score", 0) if catalyst else 0
    cat_news_title = catalyst.get("top_news_title") if catalyst else None
    cat_found    = catalyst.get("catalysts_found", []) if catalyst else []
    has_catalyst = cat_score >= 2

    # ── REAL OPTIONS DATA (new) ───────────────────────────────
    opt_vol_oi      = mc_data.get("opt_vol_oi", 0) if mc_data else 0
    opt_call_vol_oi = mc_data.get("opt_call_vol_oi", 0) if mc_data else 0
    opt_put_vol_oi  = mc_data.get("opt_put_vol_oi", 0) if mc_data else 0
    opt_atm_call    = mc_data.get("opt_atm_call", 0) if mc_data else 0
    opt_atm_put     = mc_data.get("opt_atm_put", 0) if mc_data else 0
    opt_pc_ratio    = mc_data.get("opt_pc_ratio", 1.0) if mc_data else 1.0
    opt_dominant    = mc_data.get("opt_dominant", "NEUTRAL") if mc_data else "NEUTRAL"
    opt_is_sweep    = mc_data.get("opt_is_sweep", False) if mc_data else False
    has_real_opts   = opt_vol_oi > 0

    # Override rel_vol — priority: real options vol/OI > stock rel_vol
    fv_rv = finviz.get("rel_volume", "")
    if fv_rv and fv_rv not in ("-", "N/A"):
        try:
            rel_vol = float(fv_rv)
        except:
            pass
    if mc_data and mc_data.get("mc_rel_vol", 0) > rel_vol:
        rel_vol = mc_data["mc_rel_vol"]  # Already mapped from real vol/OI

    # MA percentages from Finviz
    sma20_pct  = finviz.get("sma20_pct", "")
    sma50_pct  = finviz.get("sma50_pct", "")
    sma200_pct = finviz.get("sma200_pct", "")

    def pct_val(s):
        try:
            return float(str(s).replace('%','').strip())
        except:
            return None

    sma20v  = pct_val(sma20_pct)
    sma50v  = pct_val(sma50_pct)
    sma200v = pct_val(sma200_pct)

    above_sma20  = sma20v  is not None and not str(sma20_pct).startswith('-')
    above_sma50  = sma50v  is not None and not str(sma50_pct).startswith('-')
    above_sma200 = sma200v is not None and not str(sma200_pct).startswith('-')

    # RSI
    try:
        rsi_val = float(finviz.get("rsi14") or 50)
    except:
        rsi_val = 50.0

    # Short float
    try:
        short_pct = float(str(finviz.get("short_float","0")).replace("%","").strip())
    except:
        short_pct = 0.0

    # Has news
    has_news  = len(finviz.get("news", [])) > 0
    has_alert = bool(alert_context and len(alert_context.strip()) > 5)
    has_earn  = bool(finviz.get("earnings_date","") not in ("","N/A","-"))

    # Market sentiment modifier
    mkt_sentiment = market.get("sentiment", "NEUTRAL")
    mkt_modifier  = 0.5 if mkt_sentiment == "ALCISTA" else -0.3 if mkt_sentiment == "BAJISTA" else 0.0

    # Direction — real options dominant side takes priority
    if has_real_opts and opt_dominant in ("CALL", "PUT"):
        direction = opt_dominant
    elif has_alert and "put" in alert_context.lower():
        direction = "PUT"
    elif has_alert and "call" in alert_context.lower():
        direction = "CALL"
    elif chg_pct >= 0:
        direction = "CALL"
    else:
        direction = "PUT"

    # ── V1 CATALIZADOR (max 3.0) ──────────────────────────────
    # Priority: manual alert > sweep > catalyst detector > options flow > news
    if has_alert:
        pts_catalyst = 2.5
    elif opt_is_sweep:
        pts_catalyst = 2.8  # institutional sweep = strongest signal
    elif cat_earn_alert:
        pts_catalyst = 2.8  # earnings today/tomorrow = high volatility
    elif cat_score >= 6:
        pts_catalyst = 2.5  # strong catalyst (upgrade + news + proximity)
    elif cat_score >= 4:
        pts_catalyst = 2.2  # moderate catalyst
    elif has_real_opts and opt_vol_oi >= 3.0:
        pts_catalyst = 2.3  # very unusual options flow
    elif has_real_opts and opt_vol_oi >= 1.5:
        pts_catalyst = 2.0  # unusual options flow
    elif cat_score >= 2:
        pts_catalyst = 1.8  # light catalyst (upgrade or news)
    elif has_earn:
        pts_catalyst = 1.5
    elif has_news:
        pts_catalyst = 1.2
    elif mc_data:
        pts_catalyst = 0.8
    else:
        pts_catalyst = 0.3

    # ── V2 VOLUMEN INUSUAL (max 2.5) — real vol/OI when available ───
    if has_real_opts:
        if opt_vol_oi >= 10.0:
            pts_vol = 2.5
        elif opt_vol_oi >= 5.0:
            pts_vol = 2.3
        elif opt_vol_oi >= 3.0:
            pts_vol = 2.0
        elif opt_vol_oi >= 1.5:
            pts_vol = 1.6
        elif opt_vol_oi >= 0.8:
            pts_vol = 1.0
        else:
            pts_vol = 0.4
        if direction == "CALL" and opt_atm_call >= 2.0:
            pts_vol = min(pts_vol + 0.3, 2.5)
        elif direction == "PUT" and opt_atm_put >= 2.0:
            pts_vol = min(pts_vol + 0.3, 2.5)
    else:
        if rel_vol >= 10:
            pts_vol = 2.5
        elif rel_vol >= 5:
            pts_vol = 2.3
        elif rel_vol >= 3:
            pts_vol = 2.0
        elif rel_vol >= 2:
            pts_vol = 1.5
        elif rel_vol >= 1.5:
            pts_vol = 1.0
        elif rel_vol >= 1.2:
            pts_vol = 0.6
        else:
            pts_vol = 0.2

    # ── V3 MOMENTUM TECNICO (max 2.0) ────────────────────────
    if direction == "CALL":
        if above_sma20 and above_sma50 and rel_vol >= 1.5:
            pts_mom = 2.0
        elif above_sma50 and rel_vol >= 1.2:
            pts_mom = 1.4
        elif above_sma50:
            pts_mom = 1.0
        elif chg_pct > 1:
            pts_mom = 0.6
        else:
            pts_mom = 0.0
    else:  # PUT
        if not above_sma20 and not above_sma50 and rel_vol >= 1.5:
            pts_mom = 2.0
        elif not above_sma50:
            pts_mom = 1.2
        elif chg_pct < -1:
            pts_mom = 0.8
        else:
            pts_mom = 0.3

    # ── V4 SECTOR/MACRO (max 1.5) ────────────────────────────
    if above_sma200:
        pts_sector = 1.5 if mkt_sentiment == "ALCISTA" else 1.0
    elif mkt_sentiment == "BAJISTA":
        pts_sector = 0.2
    else:
        pts_sector = 0.6

    # ── V5 SHORT/SQUEEZE (max 1.0) ───────────────────────────
    if short_pct >= 20:
        pts_short = 1.0
    elif short_pct >= 10:
        pts_short = 0.6
    elif short_pct >= 5:
        pts_short = 0.3
    else:
        pts_short = 0.1

    # RSI moderate penalty
    rsi_penalty = 0.0
    if rsi_val >= 78:
        rsi_penalty = -0.5
    elif rsi_val <= 22:
        rsi_penalty = -0.3

    # Penalty for extreme same-day move - risk/reward already changed
    move_penalty = 0.0
    abs_chg = abs(chg_pct)
    if abs_chg >= 20:
        move_penalty = -1.5  # movimiento extremo - prima inflada
    elif abs_chg >= 15:
        move_penalty = -1.0  # movimiento muy grande
    elif abs_chg >= 10:
        move_penalty = -0.5  # movimiento grande - precaucion

    raw   = pts_catalyst + pts_vol + pts_mom + pts_sector + pts_short + mkt_modifier + rsi_penalty + move_penalty
    score = round(min(max(raw, 0), 10.0), 1)
    
    # Add move penalty info to score breakdown
    if move_penalty < 0:
        move_note = f" MovExtrem{move_penalty}"
    else:
        move_note = ""

    # Semaforo
    if score >= 7.0:
        semaforo = "VERDE"
    elif score >= 4.0:
        semaforo = "AMARILLO"
    else:
        semaforo = "ROJO"

    # RSI label
    if rsi_val >= 70:
        rsi_label = f"Sobrecomprado ({rsi_val:.0f})"
    elif rsi_val <= 30:
        rsi_label = f"Sobrevendido ({rsi_val:.0f})"
    else:
        rsi_label = f"Normal ({rsi_val:.0f})"

    # Momentum - considers both price change AND unusual volume
    if rel_vol >= 2.0 and chg_pct > 0.5:
        momentum = "ALCISTA FUERTE"
    elif rel_vol >= 2.0 and chg_pct < -0.5:
        momentum = "BAJISTA FUERTE"
    elif chg_pct > 1.5 or (chg_pct > 0.5 and rel_vol >= 1.5):
        momentum = "ALCISTA FUERTE"
    elif chg_pct > 0.3:
        momentum = "ALCISTA"
    elif chg_pct < -1.5 or (chg_pct < -0.5 and rel_vol >= 1.5):
        momentum = "BAJISTA FUERTE"
    elif chg_pct < -0.3:
        momentum = "BAJISTA"
    else:
        momentum = "LATERAL"

    # Vol label — show real options data when available
    if has_real_opts:
        vol_label = f"Vol/OI {opt_vol_oi:.1f}x"
        if opt_is_sweep:
            vol_label += " 🚨 SWEEP INSTITUCIONAL"
        elif opt_vol_oi >= 3.0:
            vol_label += " 🔥 MUY INUSUAL"
        elif opt_vol_oi >= 1.5:
            vol_label += " ⚡ INUSUAL"
        vol_label += f" | P/C {opt_pc_ratio:.2f} | {opt_dominant}"
    else:
        vol_label = f"{rel_vol:.1f}x promedio"
        if rel_vol >= 3:
            vol_label += " 🔥 MUY INUSUAL"
        elif rel_vol >= 2:
            vol_label += " ⚡ INUSUAL"

    # Stocktwits sentiment
    st_bulls = st.get("bullish")
    st_bears = st.get("bearish")
    if st_bulls and st_bears:
        st_label = f"Bulls {st_bulls:.0f}% · Bears {st_bears:.0f}%"
    else:
        st_label = "Sin datos"

    # Why / Risk texts
    why_parts = []
    if has_alert:
        why_parts.append(f"Alerta del grupo: {alert_context}")
    # Catalyst detector findings (pre-movement signals)
    for cf in cat_found[:3]:
        why_parts.append(cf)
    if has_real_opts and opt_vol_oi >= 1.5:
        sweep_txt = "SWEEP INSTITUCIONAL — " if opt_is_sweep else ""
        why_parts.append(f"{sweep_txt}Flujo de opciones inusual: Vol/OI {opt_vol_oi:.1f}x — lado {opt_dominant} dominante (P/C {opt_pc_ratio:.2f})")
    elif rel_vol >= 2:
        why_parts.append(f"Volumen {rel_vol:.1f}x sobre promedio — actividad inusual")
    if above_sma50 and above_sma20 and direction == "CALL":
        why_parts.append("Precio sobre MA20 y MA50 — estructura alcista")
    if short_pct >= 15 and direction == "CALL":
        why_parts.append(f"Short float {short_pct:.0f}% — potencial squeeze alcista")
    if has_earn:
        why_parts.append(f"Earnings proximamente: {finviz.get('earnings_date','')} — volatilidad esperada")
    if has_news:
        news = finviz.get("news", [])
        if news:
            why_parts.append(f"Noticia reciente: {news[0]['title'][:80]}")
    if not why_parts:
        why_parts.append(f"Cambio del dia {chg_pct:+.2f}%. Sin catalizadores fuertes identificados.")

    risk_parts = []
    if rsi_val >= 70 and direction == "CALL":
        risk_parts.append(f"RSI {rsi_val:.0f} sobrecomprado — posible pullback")
    if not above_sma50 and direction == "CALL":
        risk_parts.append("Precio bajo MA50 — tendencia bajista de fondo")
    if mkt_sentiment == "BAJISTA" and direction == "CALL":
        risk_parts.append("Mercado general bajista — viento en contra para calls")
    if abs(chg_pct) > 5:
        risk_parts.append(f"Movimiento extremo {chg_pct:+.1f}% — posible reversión")
    if not risk_parts:
        risk_parts.append("Monitorear VWAP como soporte clave. Respetar el stop.")

    # Suggested strike and targets
    call_str = round(price * 1.05 / 5) * 5 if price >= 10 else round(price * 1.05, 1)
    put_str  = round(price * 0.95 / 5) * 5 if price >= 10 else round(price * 0.95, 1)
    strike   = call_str if direction == "CALL" else put_str

    return {
        "direction":       direction,
        "score":           score,
        "semaforo":        semaforo,
        "pts_catalyst":    round(pts_catalyst, 1),
        "pts_flow":        round(pts_vol, 1),
        "pts_momentum":    round(pts_mom, 1),
        "pts_sector":      round(pts_sector, 1),
        "pts_short":       round(pts_short, 1),
        "score_breakdown": f"Cat {pts_catalyst:.1f}+Vol {pts_vol:.1f}+Mom {pts_mom:.1f}+Sector {pts_sector:.1f}+Short {pts_short:.1f}{move_note}={score}",
        # Real options data
        "opt_vol_oi":      round(opt_vol_oi, 2),
        "opt_call_vol_oi": round(opt_call_vol_oi, 2),
        "opt_put_vol_oi":  round(opt_put_vol_oi, 2),
        "opt_atm_call":    round(opt_atm_call, 2),
        "opt_atm_put":     round(opt_atm_put, 2),
        "opt_pc_ratio":    round(opt_pc_ratio, 2),
        "opt_dominant":    opt_dominant,
        "opt_is_sweep":    opt_is_sweep,
        "has_real_opts":   has_real_opts,
        # Catalyst detection
        "catalyst_score":  cat_score,
        "catalyst_signal": cat_signal,
        "catalyst_analyst": cat_analyst,
        "catalyst_earn_days": cat_earn_days,
        "catalyst_earn_alert": cat_earn_alert,
        "catalyst_news_title": cat_news_title,
        "catalysts_found": cat_found,
        "momentum":        momentum,
        "rsi":             rsi_label,
        "rsi_value":       rsi_val,
        "rel_volume":      rel_vol,
        "vol_desc":        vol_label,
        "above_sma20":     above_sma20,
        "above_sma50":     above_sma50,
        "above_sma200":    above_sma200,
        "sma20_pct":       sma20_pct or "N/A",
        "sma50_pct":       sma50_pct or "N/A",
        "sma200_pct":      sma200_pct or "N/A",
        "short_float":     finviz.get("short_float", "N/A"),
        "short_pct":       short_pct,
        "target_price":    finviz.get("target_price", "N/A"),
        "recommendation":  finviz.get("recommendation", "N/A"),
        "beta":            finviz.get("beta", "N/A"),
        "perf_week":       finviz.get("perf_week", "N/A"),
        "perf_month":      finviz.get("perf_month", "N/A"),
        "perf_ytd":        finviz.get("perf_ytd", "N/A"),
        "high_52w":        finviz.get("52w_high", "N/A"),
        "low_52w":         finviz.get("52w_low", "N/A"),
        "inst_own":        finviz.get("inst_own", "N/A"),
        "sector":          finviz.get("sector", "N/A"),
        "earnings_date":   finviz.get("earnings_date", "N/A"),
        "atr":             finviz.get("atr", "N/A"),
        "st_sentiment":    st_label,
        "news":            finviz.get("news", []),
        "why":             " | ".join(why_parts),
        "risk":            " | ".join(risk_parts),
        "strike":          strike,
        "stop":            f"${price * 0.95:.2f}" if price else "N/A",
        "target_1":        f"${price * 1.05:.2f}" if price else "N/A",
        "target_2":        f"${price * 1.10:.2f}" if price else "N/A",
        "ez":              f"${price * 0.98:.2f}–${price:.2f}" if price else "N/A",
        "vwap_trigger":    f"Sobre VWAP ${yahoo.get('vwap', 0):.2f}" if yahoo.get('vwap') else "Ver VWAP en IBKR",
    }


# ══════════════════════════════════════════════════════════════
# FULL TICKER ANALYSIS
# ══════════════════════════════════════════════════════════════

def analyze_ticker(ticker: str, alert_context: str = "", market: Dict = None, mc_data: Dict = None) -> Dict[str, Any]:
    ticker = ticker.upper().strip()
    if not market:
        market = {}

    yahoo  = get_yahoo(ticker)
    finviz = get_finviz(ticker)
    st     = get_stocktwits_sentiment(ticker)

    # If mc_data doesn't have real options data (e.g. called from /api/flow or /api/ticker),
    # fetch it now so individual ticker lookups also get real options analysis
    if not mc_data or not mc_data.get("opt_vol_oi"):
        opts = get_options_flow_real(ticker)
        if opts:
            if mc_data is None:
                mc_data = {}
            mc_data.update({
                "opt_vol_oi":      opts.get("best_vol_oi", 0),
                "opt_call_vol_oi": opts.get("best_call_vol_oi", 0),
                "opt_put_vol_oi":  opts.get("best_put_vol_oi", 0),
                "opt_atm_call":    opts.get("atm_call_vol_oi", 0),
                "opt_atm_put":     opts.get("atm_put_vol_oi", 0),
                "opt_pc_ratio":    opts.get("pc_ratio", 1.0),
                "opt_dominant":    opts.get("dominant_side", "NEUTRAL"),
                "opt_is_sweep":    opts.get("is_sweep", False),
                "opt_top_strikes": opts.get("top_strikes", []),
                "mc_rel_vol":      opts.get("mapped_rel_vol", 1.0),
                "is_unusual":      opts.get("is_unusual", False),
            })

    # Catalyst detection — pre-movement signal
    catalyst = get_catalyst_data(ticker)

    if not yahoo and not finviz:
        return {"ticker": ticker, "error": f"Sin datos para {ticker}. Verifica el simbolo.", "score": 0, "semaforo": "ROJO"}

    scoring = compute_score(ticker, yahoo, finviz, st, market, alert_context, mc_data, catalyst)
    price   = yahoo.get("price", 0)

    return {
        "ticker":          ticker,
        "type":            scoring["direction"],
        "direction":       scoring["direction"],
        "score":           scoring["score"],
        "semaforo":        scoring["semaforo"],
        "pts_catalyst":    scoring["pts_catalyst"],
        "pts_flow":        scoring["pts_flow"],
        "pts_momentum":    scoring["pts_momentum"],
        "pts_sector":      scoring["pts_sector"],
        "pts_short":       scoring["pts_short"],
        "score_breakdown": scoring["score_breakdown"],
        # Price
        "price":           f"${price:.2f}" if price else "N/A",
        "spot":            price,
        "open":            yahoo.get("open", 0),
        "high":            yahoo.get("high", 0),
        "low":             yahoo.get("low", 0),
        "vwap":            f"${yahoo.get('vwap', 0):.2f}" if yahoo.get('vwap') else "N/A",
        "change_pct":      f"{yahoo.get('change_pct', 0):+.2f}%",
        "change_abs":      f"${yahoo.get('change_abs', 0):+.2f}",
        # Volume
        "volume":          yahoo.get("volume", 0),
        "avg_volume":      yahoo.get("avg_volume", 0),
        "rel_volume":      scoring["rel_volume"],
        "vol_vs_avg":      scoring["vol_desc"],
        # Technical
        "rsi":             scoring["rsi"],
        "rsi_value":       scoring["rsi_value"],
        "momentum":        scoring["momentum"],
        "ma5_pct":         "N/A",
        "ma20_pct":        scoring["sma20_pct"],
        "ma50_pct":        scoring["sma50_pct"],
        "ma200_pct":       scoring["sma200_pct"],
        # Risk context
        "short_float":     scoring["short_float"],
        "short_pct":       scoring["short_pct"],
        "target_price":    scoring["target_price"],
        "recommendation":  scoring["recommendation"],
        "beta":            scoring["beta"],
        "perf_week":       scoring["perf_week"],
        "perf_month":      scoring["perf_month"],
        "perf_ytd":        scoring["perf_ytd"],
        "high_52w":        scoring["high_52w"],
        "low_52w":         scoring["low_52w"],
        "inst_own":        scoring["inst_own"],
        "sector":          scoring["sector"],
        "earnings_date":   scoring["earnings_date"],
        "atr":             scoring["atr"],
        "st_sentiment":    scoring["st_sentiment"],
        # Real options flow data
        "opt_vol_oi":      scoring.get("opt_vol_oi", 0),
        "opt_call_vol_oi": scoring.get("opt_call_vol_oi", 0),
        "opt_put_vol_oi":  scoring.get("opt_put_vol_oi", 0),
        "opt_pc_ratio":    scoring.get("opt_pc_ratio", 1.0),
        "opt_dominant":    scoring.get("opt_dominant", "N/A"),
        "opt_is_sweep":    scoring.get("opt_is_sweep", False),
        "has_real_opts":   scoring.get("has_real_opts", False),
        "opt_top_strikes": mc_data.get("opt_top_strikes", []) if mc_data else [],
        "opt_signal": (
            "🚨 SWEEP" if scoring.get("opt_is_sweep")
            else "🔥 MUY INUSUAL" if scoring.get("opt_vol_oi", 0) >= 3.0
            else "⚡ INUSUAL" if scoring.get("opt_vol_oi", 0) >= 1.5
            else "Normal" if scoring.get("has_real_opts") else "Sin datos"
        ),
        # Catalyst detection (pre-movement signals)
        "catalyst_score":    scoring.get("catalyst_score", 0),
        "catalyst_signal":   scoring.get("catalyst_signal", "NONE"),
        "catalyst_analyst":  scoring.get("catalyst_analyst"),
        "catalyst_earn_days": scoring.get("catalyst_earn_days"),
        "catalyst_earn_alert": scoring.get("catalyst_earn_alert", False),
        "catalyst_news":     scoring.get("catalyst_news_title"),
        "catalysts_found":   scoring.get("catalysts_found", []),
        # News
        "news":            scoring["news"],
        "catalyst":        scoring["news"][0]["title"] if scoring["news"] else "Sin noticias recientes",
        # Analysis
        "why":             scoring["why"],
        "risk":            scoring["risk"],
        # Option suggestion
        "strike":          scoring["strike"],
        "expiry":          "Ver cadena en IBKR",
        "flow_usd":        f"${mc_data.get('mc_volume', 0):,}" if mc_data else "N/A",
        "contract":        f"{ticker} {scoring['direction']} ${scoring['strike']:.0f}",
        "vwap_trigger":    scoring["vwap_trigger"],
        "stop":            scoring["stop"],
        "target_1":        scoring["target_1"],
        "target_2":        scoring["target_2"],
        "ez":              scoring["ez"],
        "pm":              "Ver cadena en IBKR",
        "c5":              "—",
        "c15":             "—",
        "sl":              scoring["stop"],
        "sentiment":       "POSITIVO" if yahoo.get("change_pct", 0) > 0 else "NEGATIVO",
        "control":         "COMPRADORES" if yahoo.get("change_pct", 0) > 0 and scoring["rel_volume"] >= 1.5 else "VENDEDORES",
        "control_detail":  f"Volumen {scoring['rel_volume']:.1f}x promedio. RSI {scoring['rsi_value']:.0f}. {'Sobre' if scoring['above_sma50'] else 'Bajo'} MA50.",
        "source":          "yahoo+finviz+stocktwits",
        "timestamp":       datetime.now().isoformat(timespec='seconds'),
    }


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.route('/api/pulse')
def api_pulse():
    """Market pulse: SPY, QQQ, IWM, VIX sentiment."""
    pulse = get_market_pulse()
    return jsonify(pulse)


@app.route('/api/scan')
def api_scan():
    """
    Autonomous scan with multiple data sources and fallbacks:
    1. Market pulse (SPY/QQQ/IWM/VIX)
    2. Unusual Whales free flow (primary)
    3. Yahoo Finance most active (fallback 1)
    4. Yahoo Finance trending (fallback 2)
    5. Analyze top tickers with full Yahoo + Finviz data
    """
    market = get_market_pulse()

    # Try sources in order of quality
    source_name = "unknown"
    ticker_list = []

    # Source 1: Unusual Whales / Market Chameleon
    ticker_list = get_market_chameleon_unusual()
    if ticker_list:
        source_name = ticker_list[0].get("source", "unusual_whales")
    
    # Source 2: Yahoo most active (always reliable)
    yahoo_active = get_yahoo_most_active()
    
    # Source 3: Yahoo trending
    yahoo_trend = get_yahoo_trending()

    # Merge all sources, prioritize by rel_vol
    seen = set()
    merged = []
    for t in ticker_list + yahoo_active + yahoo_trend:
        tk = t.get("ticker", "")
        if tk and tk not in seen and tk not in ("SPY","QQQ","IWM","VIX","TLT","GLD"):
            seen.add(tk)
            merged.append(t)

    # Sort by relative volume descending
    merged.sort(key=lambda x: x.get("mc_rel_vol", 0), reverse=True)

    if not merged:
        # Ultimate fallback
        merged = [{"ticker": t, "mc_rel_vol": 1.5, "mc_volume": 0, "mc_chg_pct": 0, "mc_bullish": True}
                  for t in ["NVDA","AMD","TSLA","META","AAPL","MSFT","GOOGL","AMZN","INTC","PLTR"]]
        source_name = "fallback_default"

    # Analyze top 10 tickers
    items = []
    for mc in merged[:10]:
        result = analyze_ticker(mc["ticker"], market=market, mc_data=mc)
        if "error" not in result:
            items.append(result)

    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")
    
    # Build summary message
    top = items[0] if items else None
    if verdes > 0:
        summary = f"{verdes} setup(s) con señal verde. Mejor: {top['ticker']} {top['score']}/10 — {top['direction']}."
    elif items:
        summary = f"{len(items)} tickers analizados. Ninguno reune criterios verdes hoy. Esperar mejores setups."
    else:
        summary = "Sin datos disponibles. Verifica conexion."

    return jsonify({
        "source":    source_name,
        "market":    market,
        "timestamp": datetime.now().isoformat(timespec='seconds'),
        "summary":   summary,
        "items":     items,
    })


@app.route('/api/flow')
def api_flow():
    """Flow endpoint - analyze specific tickers."""
    tickers_raw = request.args.get('tickers', '')
    market_mode = request.args.get('market', 'neutral')

    if not tickers_raw:
        return api_scan()

    market  = get_market_pulse()
    tickers = [t.strip().upper() for t in tickers_raw.split(',') if t.strip()][:10]
    items   = []
    for tk in tickers:
        result = analyze_ticker(tk, market=market)
        if "error" not in result:
            items.append(result)

    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")

    return jsonify({
        "source":    "yahoo+finviz+stocktwits",
        "market":    market,
        "timestamp": datetime.now().isoformat(timespec='seconds'),
        "summary":   f"{verdes} con señal verde de {len(items)} analizados." if verdes else f"{len(items)} analizados. Sin señales verdes.",
        "items":     items,
    })


@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    """Analyze alerts from Discord/Telegram group."""
    payload     = request.get_json(force=True, silent=True) or {}
    tickers_raw = payload.get('tickers', '')
    alert_raw   = payload.get('alert_raw', '')
    comment     = payload.get('comment', '')
    market_mode = payload.get('market', 'neutral')
    direction   = payload.get('direction', '')
    strike      = payload.get('strike', '')
    expiry      = payload.get('expiry', '')
    zona        = payload.get('zona', '')
    premium     = payload.get('premium', '')

    if not tickers_raw:
        return jsonify({'error': 'Falta el campo tickers'}), 400

    market  = get_market_pulse()
    tickers = [t.strip().upper() for t in str(tickers_raw).split(',') if t.strip()]

    # Build rich context from alert data
    ctx_parts = []
    if alert_raw:
        ctx_parts.append(alert_raw)
    if direction:
        ctx_parts.append(f"Direccion: {direction}")
    if strike:
        ctx_parts.append(f"Strike: ${strike}")
    if expiry:
        ctx_parts.append(f"Vence: {expiry}")
    if zona:
        ctx_parts.append(f"Zona de interes: ${zona}")
    if premium:
        ctx_parts.append(f"Premium: {premium}")
    if comment and comment not in ctx_parts:
        ctx_parts.append(comment)
    ctx = " | ".join(ctx_parts) if ctx_parts else ""

    items = []
    for tk in tickers:
        result = analyze_ticker(tk, alert_context=ctx, market=market)
        if "error" not in result:
            # Override direction from alert if provided
            if direction:
                result["direction"] = direction.upper()
                result["type"] = direction.upper()
            # Override strike from alert if provided
            if strike:
                try:
                    result["strike"] = float(strike)
                except:
                    pass
            # Override expiry from alert if provided
            if expiry:
                result["expiry"] = expiry
            # Add zona de interes
            if zona:
                result["zona_interes"] = zona
                result["ez"] = f"${zona} zona de interes"
            items.append(result)

    if not items:
        return jsonify({"source": "yahoo+finviz", "summary": "Sin datos disponibles.", "items": [], "market": market})

    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")

    return jsonify({
        "source":    "yahoo+finviz+stocktwits",
        "market":    market,
        "timestamp": datetime.now().isoformat(timespec='seconds'),
        "summary":   f"{verdes} setup(s) con señal verde." if verdes else "Sin señales verdes claras.",
        "items":     items,
    })


@app.route('/api/ticker/<ticker>')
def api_ticker(ticker: str):
    market = get_market_pulse()
    result = analyze_ticker(ticker.upper(), market=market)
    if "error" in result:
        return jsonify(result), 404
    return jsonify({"ticker": ticker.upper(), "market": market, "items": [result], "source": "yahoo+finviz+stocktwits"})


@app.route('/api/options/<ticker>')
def api_options(ticker: str):
    """
    Direct options chain analysis for a specific ticker.
    Returns real vol/OI ratios, put/call ratio, dominant side, top strikes.
    Use this to check any ticker manually: /api/options/INTC
    """
    ticker = ticker.upper().strip()
    opts = get_options_flow_real(ticker)
    if not opts:
        return jsonify({"ticker": ticker, "error": "No options data available", "ok": False}), 404

    return jsonify({
        "ticker":          ticker,
        "ok":              True,
        "timestamp":       datetime.now().isoformat(timespec='seconds'),
        "vol_oi":          opts.get("best_vol_oi"),
        "call_vol_oi":     opts.get("best_call_vol_oi"),
        "put_vol_oi":      opts.get("best_put_vol_oi"),
        "atm_call_vol_oi": opts.get("atm_call_vol_oi"),
        "atm_put_vol_oi":  opts.get("atm_put_vol_oi"),
        "total_call_vol":  opts.get("total_call_vol"),
        "total_put_vol":   opts.get("total_put_vol"),
        "pc_ratio":        opts.get("pc_ratio"),
        "dominant_side":   opts.get("dominant_side"),
        "is_unusual":      opts.get("is_unusual"),
        "is_sweep":        opts.get("is_sweep"),
        "mapped_rel_vol":  opts.get("mapped_rel_vol"),
        "top_strikes":     opts.get("top_strikes", []),
        "signal": (
            "🚨 SWEEP INSTITUCIONAL" if opts.get("is_sweep")
            else "🔥 MUY INUSUAL" if opts.get("best_vol_oi", 0) >= 3.0
            else "⚡ INUSUAL" if opts.get("is_unusual")
            else "Normal"
        ),
    })


@app.route('/api/watchlist', methods=['GET'])
def api_watchlist_get():
    """Get current watchlist."""
    tickers = load_watchlist()
    return jsonify({
        "tickers": tickers,
        "count":   len(tickers),
        "file":    WATCHLIST_FILE,
        "timestamp": datetime.now().isoformat(timespec='seconds'),
    })


@app.route('/api/watchlist', methods=['POST'])
def api_watchlist_post():
    """
    Add or set watchlist tickers.
    Body: {"tickers": ["INTC","QCOM","NVDA"]}   → replaces full list
    Body: {"add": ["SOFI","MSTR"]}               → appends to current list
    Body: {"remove": ["RIVN"]}                   → removes from list
    """
    payload = request.get_json(force=True, silent=True) or {}
    current = load_watchlist()

    if "tickers" in payload:
        # Full replace
        new_list = [t.upper().strip() for t in payload["tickers"] if t.strip()]
        save_watchlist(new_list)
        return jsonify({"ok": True, "action": "replaced", "tickers": new_list, "count": len(new_list)})

    if "add" in payload:
        to_add = [t.upper().strip() for t in payload["add"] if t.strip()]
        merged = list(dict.fromkeys(current + to_add))  # preserve order, dedupe
        save_watchlist(merged)
        return jsonify({"ok": True, "action": "added", "added": to_add, "tickers": merged, "count": len(merged)})

    if "remove" in payload:
        to_remove = {t.upper().strip() for t in payload["remove"]}
        filtered = [t for t in current if t not in to_remove]
        save_watchlist(filtered)
        return jsonify({"ok": True, "action": "removed", "removed": list(to_remove), "tickers": filtered, "count": len(filtered)})

    return jsonify({"error": "Payload must have 'tickers', 'add', or 'remove' key"}), 400


@app.route('/api/watchlist/scan')
def api_watchlist_scan():
    """
    Scan ALL watchlist tickers right now — independently of screeners.
    Useful for monitoring INTC, QCOM, etc. at any time.
    Returns full analysis sorted by score descending.
    """
    tickers = load_watchlist()
    market  = get_market_pulse()
    items   = []
    for tk in tickers:
        result = analyze_ticker(tk, market=market)
        if "error" not in result:
            items.append(result)
    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")
    alerts = sum(1 for x in items if x.get("catalyst_earn_alert"))
    sweeps = sum(1 for x in items if x.get("opt_is_sweep"))
    return jsonify({
        "source":    "watchlist_scan",
        "market":    market,
        "timestamp": datetime.now().isoformat(timespec='seconds'),
        "watchlist": tickers,
        "count":     len(items),
        "verdes":    verdes,
        "earn_alerts": alerts,
        "sweeps":    sweeps,
        "summary":   f"{verdes} verdes · {alerts} earnings alert · {sweeps} sweeps de {len(items)} analizados",
        "items":     items,
    })


@app.route('/api/catalyst/<ticker>')
def api_catalyst(ticker: str):
    """
    Pre-movement catalyst analysis for a single ticker.
    Detects: analyst upgrades, earnings proximity, news impact.
    Example: /api/catalyst/QCOM
    """
    ticker = ticker.upper().strip()
    cat = get_catalyst_data(ticker)
    return jsonify({
        "ticker":          ticker,
        "timestamp":       datetime.now().isoformat(timespec='seconds'),
        "catalyst_score":  cat.get("catalyst_score", 0),
        "catalyst_signal": cat.get("catalyst_signal", "NONE"),
        "analyst_action":  cat.get("analyst_action"),
        "earnings_days":   cat.get("earnings_days"),
        "earnings_alert":  cat.get("earnings_alert", False),
        "iv_spike":        cat.get("iv_spike", False),
        "top_news_score":  cat.get("top_news_score", 0),
        "top_news_title":  cat.get("top_news_title"),
        "top_news_kws":    cat.get("top_news_kws", []),
        "catalysts_found": cat.get("catalysts_found", []),
    })


@app.route('/api/debug')
def api_debug():
    """Test all data sources including new options chain and catalyst detector."""
    out = {}
    # Yahoo
    y = get_yahoo("NVDA")
    out["yahoo"] = {"ok": bool(y.get("price")), "price": y.get("price"), "vol": y.get("volume")}
    # Finviz
    f = get_finviz("NVDA")
    out["finviz"] = {"ok": bool(f.get("rsi14")), "rsi": f.get("rsi14"), "news_count": len(f.get("news",[]))}
    # Options chain
    opts = get_options_flow_real("NVDA")
    out["options_chain"] = {
        "ok": bool(opts), "vol_oi": opts.get("best_vol_oi"),
        "pc_ratio": opts.get("pc_ratio"), "dominant": opts.get("dominant_side"),
        "is_unusual": opts.get("is_unusual"), "top_strikes": opts.get("top_strikes", [])[:3],
    }
    # Options screener
    screener = get_options_unusual_screener()
    out["options_screener"] = {
        "ok": len(screener) > 0, "count": len(screener),
        "unusual_count": sum(1 for x in screener if x.get("is_unusual")),
        "top3": [{"ticker": x["ticker"], "vol_oi": x.get("opt_vol_oi"), "side": x.get("opt_dominant")} for x in screener[:3]],
    }
    # Catalyst detector
    cat = get_catalyst_data("NVDA")
    out["catalyst"] = {
        "ok": True, "score": cat.get("catalyst_score"),
        "signal": cat.get("catalyst_signal"), "found": cat.get("catalysts_found", []),
    }
    # Watchlist
    wl = load_watchlist()
    out["watchlist"] = {"ok": True, "count": len(wl), "tickers": wl[:10]}
    # Stocktwits
    st = get_stocktwits_sentiment("NVDA")
    out["stocktwits"] = {"ok": bool(st), "data": st}
    # Market pulse
    pulse = get_market_pulse()
    out["market_pulse"] = {"sentiment": pulse.get("sentiment"), "spy": pulse.get("SPY"), "vix": pulse.get("vix_price")}
    out["timestamp"] = datetime.now().isoformat()
    return jsonify(out)


# ── START ─────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
