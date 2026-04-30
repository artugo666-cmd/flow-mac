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

        prev    = float(meta2.get("chartPreviousClose") or meta1.get("chartPreviousClose") or price)
        day_hi  = float(meta1.get("regularMarketDayHigh") or 0)
        day_lo  = float(meta1.get("regularMarketDayLow") or 0)
        day_op  = float(meta1.get("regularMarketOpen") or 0)
        chg_abs = round(price - prev, 2)
        chg_pct = round((chg_abs / prev * 100) if prev else 0, 2)
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
    """RSI, MAs, short float, news, analyst targets from Finviz."""
    try:
        r = requests.get(
            f"https://finviz.com/quote.ashx?t={ticker}&ty=c&ta=1&p=d",
            headers=HEADERS,
            timeout=15
        )
        html = r.text
        data = {}
        fields = {
            "short_float":    r"Short Float[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "short_ratio":    r"Short Ratio[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "avg_volume":     r"Avg Volume[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "rel_volume":     r"Rel Volume[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "rsi14":          r"RSI.{1,10}14.{1,10}</td>\s*<td[^>]*>([^<]+)</td>",
            "beta":           r"Beta[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "target_price":   r"Target Price[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "recommendation": r"Recom[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "52w_high":       r"52W High[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "52w_low":        r"52W Low[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "inst_own":       r"Inst Own[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "insider_own":    r"Insider Own[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "perf_week":      r"Perf Week[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "perf_month":     r"Perf Month[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "perf_ytd":       r"Perf YTD[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "sma20_pct":      r"SMA20[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "sma50_pct":      r"SMA50[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "sma200_pct":     r"SMA200[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "atr":            r"ATR[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "earnings_date":  r"Earnings[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "sector":         r"Sector[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "industry":       r"Industry[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
        }
        for key, pat in fields.items():
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                data[key] = re.sub(r'<[^>]+>', '', m.group(1)).strip()

        # News
        news = re.findall(
            r'class="news-link-container"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
            html, re.DOTALL
        )
        data["news"] = [{"url": n[0], "title": n[1].strip()} for n in news[:5]]

        return data
    except Exception as e:
        print(f"Finviz error {ticker}: {e}")
        return {}


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


def get_market_chameleon_unusual() -> List[Dict[str, Any]]:
    """Scrape Market Chameleon unusual options volume report."""
    try:
        r = requests.get(
            "https://marketchameleon.com/Reports/UnusualOptionVolumeReport",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://marketchameleon.com/",
            },
            timeout=20
        )
        html = r.text

        # Find all ticker symbols from the vol links
        tickers_raw = re.findall(r'href="/vol/([A-Z]{1,6})[/"?]', html)

        # Find relative volumes - pattern: number followed by x or just decimal > 1
        # Also find from table data patterns
        results = []
        seen = set()

        for ticker in tickers_raw:
            if ticker in seen:
                continue
            if ticker in ('ETF', 'SPY', 'QQQ', 'IWM', 'VIX', 'TLT', 'GLD', 'SLV'):
                continue
            seen.add(ticker)

            # Try to find relative volume near this ticker in HTML
            idx = html.find(f'/vol/{ticker}')
            if idx == -1:
                continue
            snippet = html[idx:idx+500]

            # Look for relative volume number
            rv_match = re.search(r'(\d+\.?\d*)\s*(?:x|</td>)', snippet)
            rel_vol = float(rv_match.group(1)) if rv_match else 1.5

            # Look for volume number
            vol_match = re.search(r'([\d,]{4,})', snippet)
            volume = int(vol_match.group(1).replace(',','')) if vol_match else 0

            # Look for change
            chg_match = re.search(r'([+-]?\d+\.?\d*)%', snippet)
            chg = float(chg_match.group(1)) if chg_match else 0.0

            if rel_vol >= 1.2:
                results.append({
                    "ticker":     ticker,
                    "mc_volume":  volume,
                    "mc_rel_vol": rel_vol,
                    "mc_chg_pct": chg,
                    "mc_bullish": chg >= 0,
                })

        # Sort by rel_vol desc
        results.sort(key=lambda x: x.get("mc_rel_vol", 0), reverse=True)
        print(f"Market Chameleon: found {len(results)} tickers")
        return results[:25]

    except Exception as e:
        print(f"Market Chameleon error: {e}")
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
    mc_data: Dict = None
) -> Dict[str, Any]:

    price     = yahoo.get("price", 0)
    chg_pct   = yahoo.get("change_pct", 0)
    rel_vol   = yahoo.get("rel_volume", 1.0)
    volume    = yahoo.get("volume", 0)

    # Override rel_vol with Finviz (more accurate) or Market Chameleon
    fv_rv = finviz.get("rel_volume", "")
    if fv_rv and fv_rv not in ("-", "N/A"):
        try:
            rel_vol = float(fv_rv)
        except:
            pass
    if mc_data and mc_data.get("mc_rel_vol", 0) > rel_vol:
        rel_vol = mc_data["mc_rel_vol"]

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

    # Direction
    if has_alert and "put" in alert_context.lower():
        direction = "PUT"
    elif has_alert and "call" in alert_context.lower():
        direction = "CALL"
    elif chg_pct >= 0:
        direction = "CALL"
    else:
        direction = "PUT"

    # ── V1 CATALIZADOR (max 3.0) ──────────────────────────────
    if has_alert:
        pts_catalyst = 2.5
    elif has_earn:
        pts_catalyst = 2.0
    elif has_news:
        pts_catalyst = 1.5
    elif mc_data:
        pts_catalyst = 1.2
    else:
        pts_catalyst = 0.5

    # ── V2 VOLUMEN INUSUAL (max 2.5) ─────────────────────────
    if rel_vol >= 5:
        pts_vol = 2.5
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

    raw   = pts_catalyst + pts_vol + pts_mom + pts_sector + pts_short + mkt_modifier
    score = round(min(max(raw, 0), 10.0), 1)

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

    # Momentum
    if chg_pct > 1.5:
        momentum = "ALCISTA FUERTE"
    elif chg_pct > 0.3:
        momentum = "ALCISTA"
    elif chg_pct < -1.5:
        momentum = "BAJISTA FUERTE"
    elif chg_pct < -0.3:
        momentum = "BAJISTA"
    else:
        momentum = "LATERAL"

    # Vol label
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
    if rel_vol >= 2:
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
        "score_breakdown": f"Cat {pts_catalyst:.1f}+Vol {pts_vol:.1f}+Mom {pts_mom:.1f}+Sector {pts_sector:.1f}+Short {pts_short:.1f}={score}",
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

    if not yahoo and not finviz:
        return {"ticker": ticker, "error": f"Sin datos para {ticker}. Verifica el simbolo.", "score": 0, "semaforo": "ROJO"}

    scoring = compute_score(ticker, yahoo, finviz, st, market, alert_context, mc_data)
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
    Autonomous scan:
    1. Get market pulse (SPY/QQQ/IWM/VIX)
    2. Get unusual options from Market Chameleon
    3. Analyze top tickers with Yahoo + Finviz
    """
    market = get_market_pulse()
    mc_list = get_market_chameleon_unusual()

    if not mc_list:
        # Fallback to popular tickers if MC scraping fails
        mc_list = [{"ticker": t, "mc_rel_vol": 1.0, "mc_volume": 0} 
                   for t in ["NVDA","AMD","TSLA","META","AAPL","MSFT","GOOGL","AMZN"]]

    # Analyze top 8 by relative volume
    items = []
    for mc in mc_list[:8]:
        result = analyze_ticker(mc["ticker"], market=market, mc_data=mc)
        if "error" not in result:
            items.append(result)

    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")

    return jsonify({
        "source":    "yahoo+finviz+market_chameleon",
        "market":    market,
        "timestamp": datetime.now().isoformat(timespec='seconds'),
        "summary":   f"{verdes} setup(s) con señal verde de {len(items)} analizados." if verdes else f"{len(items)} tickers analizados. Sin señales verdes claras.",
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

    if not tickers_raw:
        return jsonify({'error': 'Falta el campo tickers'}), 400

    market  = get_market_pulse()
    tickers = [t.strip().upper() for t in str(tickers_raw).split(',') if t.strip()]
    ctx     = alert_raw or comment or ""
    items   = []

    for tk in tickers:
        result = analyze_ticker(tk, alert_context=ctx, market=market)
        if "error" not in result:
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


@app.route('/api/debug')
def api_debug():
    """Test all data sources."""
    out = {}
    # Yahoo
    y = get_yahoo("NVDA")
    out["yahoo"] = {"ok": bool(y.get("price")), "price": y.get("price"), "vol": y.get("volume")}
    # Finviz
    f = get_finviz("NVDA")
    out["finviz"] = {"ok": bool(f.get("rsi14")), "rsi": f.get("rsi14"), "news_count": len(f.get("news",[]))}
    # Market Chameleon
    mc = get_market_chameleon_unusual()
    out["market_chameleon"] = {"ok": len(mc) > 0, "count": len(mc), "top3": [x["ticker"] for x in mc[:3]]}
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
