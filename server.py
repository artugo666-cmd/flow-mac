from __future__ import annotations

import os
import json
import re
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")

app = Flask(__name__)
CORS(app)


# ── SERVE INDEX ───────────────────────────────────────────────
@app.route('/')
def home():
    path = os.path.join(BASE_DIR, 'index.html')
    with open(path, 'r', encoding='utf-8') as f:
        return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/api/health')
def health():
    return jsonify({
        'ok': True,
        'service': 'FlowScan 8',
        'version': '3.0',
        'time': datetime.now().isoformat(timespec='seconds'),
        'polygon_key': 'configurada' if POLYGON_API_KEY else 'FALTA',
    })


# ── POLYGON STOCK SNAPSHOT ────────────────────────────────────
def get_polygon_stock(ticker: str) -> Dict[str, Any]:
    """Get stock data using /v2/aggs/prev — works on basic Polygon plan."""
    if not POLYGON_API_KEY:
        return {}
    try:
        # Get last 2 days of data to calculate change
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/2024-01-01/2030-01-01",
            params={"apiKey": POLYGON_API_KEY, "limit": 2, "sort": "desc", "adjusted": "true"},
            timeout=10
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return {}

        today = results[0]
        prev  = results[1] if len(results) > 1 else {}

        price      = round(float(today.get("c", 0)), 2)
        prev_close = round(float(prev.get("c", price)), 2)
        volume     = int(today.get("v", 0))
        prev_vol   = int(prev.get("v", 1))
        change_abs = round(price - prev_close, 2)
        change_pct = round((change_abs / prev_close * 100) if prev_close else 0, 2)
        vwap       = round(float(today.get("vw", price)), 2)

        return {
            "price":       price,
            "open":        round(float(today.get("o", 0)), 2),
            "high":        round(float(today.get("h", 0)), 2),
            "low":         round(float(today.get("l", 0)), 2),
            "volume":      volume,
            "vwap":        vwap,
            "prev_close":  prev_close,
            "prev_volume": prev_vol,
            "change_pct":  change_pct,
            "change_abs":  change_abs,
            "source":      "polygon_aggs",
        }
    except Exception as e:
        print(f"Polygon aggs error {ticker}: {e}")
        return {}


def get_polygon_aggregates(ticker: str) -> Dict[str, Any]:
    """Get moving averages from Polygon /v1/indicators/sma."""
    if not POLYGON_API_KEY:
        return {}
    results = {}
    for window in [5, 20, 50, 200]:
        try:
            r = requests.get(
                f"https://api.polygon.io/v1/indicators/sma/{ticker}",
                params={
                    "apiKey":      POLYGON_API_KEY,
                    "window":      window,
                    "series_type": "close",
                    "order":       "desc",
                    "limit":       1,
                    "timespan":    "day",
                    "adjusted":    "true",
                },
                timeout=10
            )
            r.raise_for_status()
            vals = r.json().get("results", {}).get("values", [])
            if vals:
                results[f"sma{window}"] = round(float(vals[0].get("value", 0)), 2)
        except Exception as e:
            print(f"SMA{window} error {ticker}: {e}")
    return results


def get_polygon_rsi(ticker: str) -> Optional[float]:
    """Get RSI14 from Polygon /v1/indicators/rsi."""
    if not POLYGON_API_KEY:
        return None
    try:
        r = requests.get(
            f"https://api.polygon.io/v1/indicators/rsi/{ticker}",
            params={
                "apiKey":      POLYGON_API_KEY,
                "window":      14,
                "series_type": "close",
                "order":       "desc",
                "limit":       1,
                "timespan":    "day",
                "adjusted":    "true",
            },
            timeout=10
        )
        r.raise_for_status()
        vals = r.json().get("results", {}).get("values", [])
        if vals:
            return round(float(vals[0].get("value", 0)), 1)
        return None
    except Exception as e:
        print(f"RSI error {ticker}: {e}")
        return None


# ── FINVIZ SCRAPER ────────────────────────────────────────────
def get_finviz_data(ticker: str) -> Dict[str, Any]:
    """Scrape key stats from Finviz."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        r = requests.get(
            f"https://finviz.com/quote.ashx?t={ticker}",
            headers=headers,
            timeout=15
        )
        html = r.text
        data = {}

        # Extract table data using regex
        patterns = {
            "short_float":  r"Short Float[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "short_ratio":  r"Short Ratio[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "avg_volume":   r"Avg Volume[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "rel_volume":   r"Rel Volume[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "pe":           r"P/E[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "eps":          r"EPS \(ttm\)[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "beta":         r"Beta[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "target_price": r"Target Price[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "recommendation": r"Recom[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "52w_high":     r"52W High[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "52w_low":      r"52W Low[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "rsi14":        r"RSI \(14\)[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "insider_own":  r"Insider Own[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "inst_own":     r"Inst Own[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "perf_week":    r"Perf Week[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "perf_month":   r"Perf Month[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "perf_ytd":     r"Perf YTD[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "sma20":        r"SMA20[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "sma50":        r"SMA50[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
            "sma200":       r"SMA200[^<]*</td>\s*<td[^>]*>([^<]+)</td>",
        }

        for key, pattern in patterns.items():
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                val = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                data[key] = val

        # Get news headlines
        news_pattern = r'class="news-link-container"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
        news = re.findall(news_pattern, html, re.DOTALL)
        data["news"] = [{"url": n[0], "title": n[1].strip()} for n in news[:5]]

        return data
    except Exception as e:
        print(f"Finviz error {ticker}: {e}")
        return {}


# ── SCORING ───────────────────────────────────────────────────
def compute_score(stock: Dict, sma: Dict, rsi: Optional[float], finviz: Dict, alert_context: str) -> Dict[str, Any]:
    """Compute scoring based on available data."""
    price      = stock.get("price", 0)
    change_pct = stock.get("change_pct", 0)
    volume     = stock.get("volume", 0)
    prev_vol   = stock.get("prev_volume", 1)
    rel_vol    = round(volume / prev_vol, 2) if prev_vol > 0 else 1.0

    # Finviz relative volume (more accurate)
    fv_rel_vol = finviz.get("rel_volume", "")
    if fv_rel_vol and fv_rel_vol != "-":
        try:
            rel_vol = float(fv_rel_vol.replace(",", ""))
        except:
            pass

    sma20  = sma.get("sma20", 0)
    sma50  = sma.get("sma50", 0)
    sma200 = sma.get("sma200", 0)

    # Percentages vs MAs
    pct_vs_sma5   = round((price - sma.get("sma5", price)) / sma.get("sma5", price) * 100, 2) if sma.get("sma5") else 0
    pct_vs_sma20  = round((price - sma20) / sma20 * 100, 2) if sma20 else 0
    pct_vs_sma50  = round((price - sma50) / sma50 * 100, 2) if sma50 else 0
    pct_vs_sma200 = round((price - sma200) / sma200 * 100, 2) if sma200 else 0

    # MA trend
    finviz_sma20  = finviz.get("sma20", "")
    finviz_sma50  = finviz.get("sma50", "")
    finviz_sma200 = finviz.get("sma200", "")
    above_sma20   = "-" not in str(finviz_sma20) if finviz_sma20 else (price > sma20 if sma20 else False)
    above_sma50   = "-" not in str(finviz_sma50) if finviz_sma50 else (price > sma50 if sma50 else False)
    above_sma200  = "-" not in str(finviz_sma200) if finviz_sma200 else (price > sma200 if sma200 else False)

    # RSI
    rsi_val = rsi
    if not rsi_val:
        try:
            rsi_val = float(finviz.get("rsi14", 0) or 0)
        except:
            rsi_val = 50.0

    # Momentum
    momentum = "ALCISTA" if change_pct > 1 else "BAJISTA" if change_pct < -1 else "LATERAL"
    perf_week = finviz.get("perf_week", "0%")
    perf_month = finviz.get("perf_month", "0%")

    # Short interest
    short_float = finviz.get("short_float", "0%")
    try:
        short_pct = float(str(short_float).replace("%","").replace(",","").strip())
    except:
        short_pct = 0

    # Has news catalyst
    has_news = len(finviz.get("news", [])) > 0
    has_alert = bool(alert_context and len(alert_context) > 5)

    # V1 — Catalizador (max 3.0)
    pts_catalyst = 0.0
    if has_alert:
        pts_catalyst = 2.5
    elif has_news:
        pts_catalyst = 1.5
    else:
        pts_catalyst = 0.5

    # V2 — Volumen inusual (max 2.5)
    if rel_vol >= 5:
        pts_flow = 2.5
    elif rel_vol >= 3:
        pts_flow = 2.0
    elif rel_vol >= 2:
        pts_flow = 1.5
    elif rel_vol >= 1.5:
        pts_flow = 1.0
    else:
        pts_flow = 0.3

    # V3 — Momentum tecnico (max 2.0)
    if above_sma20 and above_sma50 and rel_vol >= 1.5:
        pts_momentum = 2.0
    elif above_sma50:
        pts_momentum = 1.2
    elif price > sma.get("sma5", 0) > 0:
        pts_momentum = 0.6
    else:
        pts_momentum = 0.0

    # V4 — Sector/macro (max 1.5) — approximated by MA200
    if above_sma200:
        pts_sector = 1.5
    elif above_sma50:
        pts_sector = 0.8
    else:
        pts_sector = 0.2

    # V5 — Short interest squeeze (max 1.0)
    if short_pct >= 20:
        pts_short = 1.0
    elif short_pct >= 10:
        pts_short = 0.6
    else:
        pts_short = 0.2

    score = round(min(pts_catalyst + pts_flow + pts_momentum + pts_sector + pts_short, 10.0), 1)
    semaforo = "VERDE" if score >= 7.0 else "AMARILLO" if score >= 4.0 else "ROJO"

    # Direction
    direction = "CALL" if (momentum == "ALCISTA" or change_pct >= 0) else "PUT"

    # RSI description
    if rsi_val >= 70:
        rsi_desc = f"Sobrecomprado ({rsi_val})"
    elif rsi_val <= 30:
        rsi_desc = f"Sobrevendido ({rsi_val})"
    else:
        rsi_desc = f"Normal ({rsi_val})"

    # Vol description
    vol_desc = f"{rel_vol:.1f}x promedio"
    if rel_vol >= 2:
        vol_desc += " ⚡ SPIKE"

    return {
        "direction":      direction,
        "score":          score,
        "semaforo":       semaforo,
        "pts_catalyst":   pts_catalyst,
        "pts_flow":       pts_flow,
        "pts_momentum":   pts_momentum,
        "pts_sector":     pts_sector,
        "pts_short":      pts_short,
        "score_breakdown": f"Cat {pts_catalyst}+Vol {pts_flow}+Mom {pts_momentum}+Sector {pts_sector}+Short {pts_short}={score}",
        "momentum":       momentum,
        "rsi":            rsi_desc,
        "rsi_value":      rsi_val,
        "rel_volume":     rel_vol,
        "vol_desc":       vol_desc,
        "pct_vs_sma5":    pct_vs_sma5,
        "pct_vs_sma20":   pct_vs_sma20,
        "pct_vs_sma50":   pct_vs_sma50,
        "pct_vs_sma200":  pct_vs_sma200,
        "above_sma20":    above_sma20,
        "above_sma50":    above_sma50,
        "above_sma200":   above_sma200,
        "short_float":    short_float,
        "short_pct":      short_pct,
        "target_price":   finviz.get("target_price", "N/A"),
        "recommendation": finviz.get("recommendation", "N/A"),
        "beta":           finviz.get("beta", "N/A"),
        "perf_week":      perf_week,
        "perf_month":     perf_month,
        "perf_ytd":       finviz.get("perf_ytd", "N/A"),
        "52w_high":       finviz.get("52w_high", "N/A"),
        "52w_low":        finviz.get("52w_low", "N/A"),
        "inst_own":       finviz.get("inst_own", "N/A"),
        "insider_own":    finviz.get("insider_own", "N/A"),
        "news":           finviz.get("news", []),
    }


def get_yahoo_price(ticker: str) -> Dict[str, Any]:
    """Get stock price from Yahoo Finance as fallback."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xhtml+xml,application/xml",
        }
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "5d"},
            headers=headers,
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return {}
        meta = result[0].get("meta", {})
        indicators = result[0].get("indicators", {})
        quote = indicators.get("quote", [{}])[0]
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])
        # Get last valid values
        price = meta.get("regularMarketPrice", 0)
        prev_close = meta.get("chartPreviousClose", 0)
        volume = meta.get("regularMarketVolume", 0)
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0
        # Average volume from last 5 days
        valid_vols = [v for v in volumes if v]
        avg_vol = int(sum(valid_vols) / len(valid_vols)) if valid_vols else 1
        rel_vol = round(volume / avg_vol, 2) if avg_vol > 0 else 1.0
        day_high = meta.get("regularMarketDayHigh", 0)
        day_low  = meta.get("regularMarketDayLow", 0)
        day_open = meta.get("regularMarketOpen", 0)
        return {
            "price":       round(float(price), 2),
            "open":        round(float(day_open), 2),
            "high":        round(float(day_high), 2),
            "low":         round(float(day_low), 2),
            "volume":      int(volume),
            "vwap":        round(float(meta.get("regularMarketPrice", price)), 2),
            "prev_close":  round(float(prev_close), 2),
            "prev_volume": avg_vol,
            "change_pct":  change_pct,
            "change_abs":  round(float(price - prev_close), 2),
            "rel_volume":  rel_vol,
            "source":      "yahoo",
        }
    except Exception as e:
        print(f"Yahoo error {ticker}: {e}")
        return {}


# ── FULL TICKER ANALYSIS ──────────────────────────────────────
def analyze_ticker(ticker: str, alert_context: str = "", comment: str = "") -> Dict[str, Any]:
    """Full analysis of a ticker using Polygon + Finviz."""
    ticker = ticker.upper().strip()

    # Get stock data - Polygon first, Yahoo as fallback
    stock = get_polygon_stock(ticker)
    if not stock or not stock.get("price"):
        print(f"Polygon empty for {ticker}, trying Yahoo...")
        stock = get_yahoo_price(ticker)

    sma    = get_polygon_aggregates(ticker)
    rsi    = get_polygon_rsi(ticker)
    finviz = get_finviz_data(ticker)

    if not stock and not finviz:
        return {
            "ticker": ticker,
            "error": f"Sin datos disponibles para {ticker}. Verifica el simbolo.",
            "score": 0,
            "semaforo": "ROJO",
        }

    ctx = alert_context or comment or ""
    scoring = compute_score(stock, sma, rsi, finviz, ctx)

    price      = stock.get("price", 0)
    change_pct = stock.get("change_pct", 0)
    volume     = stock.get("volume", 0)
    vwap       = stock.get("vwap", 0)

    # Build why/risk texts
    news_titles = [n["title"] for n in scoring["news"][:2]]
    news_str = " | ".join(news_titles) if news_titles else "Sin noticias recientes en Finviz"

    why_parts = []
    if ctx:
        why_parts.append(f"Alerta del grupo: {ctx}")
    if scoring["rel_volume"] >= 2:
        why_parts.append(f"Volumen {scoring['rel_volume']:.1f}x sobre promedio — actividad inusual")
    if scoring["above_sma50"] and scoring["above_sma20"]:
        why_parts.append(f"Precio sobre MA20 y MA50 — tendencia alcista confirmada")
    if scoring["rsi_value"] >= 50 and scoring["rsi_value"] < 70:
        why_parts.append(f"RSI {scoring['rsi_value']} — zona saludable sin sobrecompra")
    if scoring["short_pct"] >= 15:
        why_parts.append(f"Short float {scoring['short_float']} — potencial squeeze")
    if not why_parts:
        why_parts.append(f"Cambio del dia: {change_pct:+.2f}%. Sin catalizadores claros.")

    risk_parts = []
    if scoring["rsi_value"] >= 70:
        risk_parts.append(f"RSI {scoring['rsi_value']} sobrecomprado — posible pullback")
    if not scoring["above_sma50"]:
        risk_parts.append("Precio bajo MA50 — tendencia bajista")
    if change_pct < -3:
        risk_parts.append(f"Caida del {change_pct:.1f}% hoy — momentum negativo")
    if not risk_parts:
        risk_parts.append("Monitorear VWAP como soporte clave")

    # Suggested option strike
    call_strike = round(price * 1.05 / 5) * 5 if price else 0
    put_strike  = round(price * 0.95 / 5) * 5 if price else 0
    strike = call_strike if scoring["direction"] == "CALL" else put_strike

    return {
        "ticker":         ticker,
        "type":           scoring["direction"],
        "direction":      scoring["direction"],
        "score":          scoring["score"],
        "semaforo":       scoring["semaforo"],
        "pts_catalyst":   scoring["pts_catalyst"],
        "pts_flow":       scoring["pts_flow"],
        "pts_momentum":   scoring["pts_momentum"],
        "pts_sector":     scoring["pts_sector"],
        "pts_short":      scoring["pts_short"],
        "score_breakdown": scoring["score_breakdown"],

        # Price data
        "price":          f"${price:.2f}" if price else "N/A",
        "spot":           price,
        "open":           stock.get("open", 0),
        "high":           stock.get("high", 0),
        "low":            stock.get("low", 0),
        "vwap":           f"${vwap:.2f}" if vwap else "N/A",
        "change_pct":     f"{change_pct:+.2f}%",
        "change_abs":     f"${stock.get('change_abs', 0):+.2f}",

        # Volume
        "volume":         volume,
        "prev_volume":    stock.get("prev_volume", 0),
        "rel_volume":     scoring["rel_volume"],
        "vol_vs_avg":     scoring["vol_desc"],

        # MAs
        "sma5":           sma.get("sma5", 0),
        "sma20":          sma.get("sma20", 0),
        "sma50":          sma.get("sma50", 0),
        "sma200":         sma.get("sma200", 0),
        "ma5_pct":        f"{scoring['pct_vs_sma5']:+.2f}%" if scoring["pct_vs_sma5"] else "N/A",
        "ma20_pct":       f"{scoring['pct_vs_sma20']:+.2f}%" if scoring["pct_vs_sma20"] else "N/A",
        "ma50_pct":       f"{scoring['pct_vs_sma50']:+.2f}%" if scoring["pct_vs_sma50"] else "N/A",
        "ma200_pct":      f"{scoring['pct_vs_sma200']:+.2f}%" if scoring["pct_vs_sma200"] else "N/A",

        # Technical
        "rsi":            scoring["rsi"],
        "rsi_value":      scoring["rsi_value"],
        "momentum":       scoring["momentum"],

        # Finviz extras
        "short_float":    scoring["short_float"],
        "short_pct":      scoring["short_pct"],
        "target_price":   scoring["target_price"],
        "recommendation": scoring["recommendation"],
        "beta":           scoring["beta"],
        "perf_week":      scoring["perf_week"],
        "perf_month":     scoring["perf_month"],
        "perf_ytd":       scoring["perf_ytd"],
        "high_52w":       scoring["52w_high"],
        "low_52w":        scoring["52w_low"],
        "inst_own":       scoring["inst_own"],
        "insider_own":    scoring["insider_own"],

        # Option suggestion
        "strike":         strike,
        "expiry":         "N/A — ver cadena de opciones",
        "flow_usd":       "N/A",
        "contract":       f"{ticker} sugerido {scoring['direction']} ${strike}",

        # Analysis
        "catalyst":       news_str,
        "news":           scoring["news"],
        "why":            " | ".join(why_parts),
        "risk":           " | ".join(risk_parts),
        "vwap_trigger":   f"Sobre VWAP ${vwap:.2f}" if vwap else "Ver VWAP en tu plataforma",
        "stop":           f"${price * 0.95:.2f}" if price else "N/A",
        "target_1":       f"${price * 1.05:.2f}" if price else "N/A",
        "target_2":       f"${price * 1.10:.2f}" if price else "N/A",
        "ez":             f"${price * 0.98:.2f}-${price:.2f}" if price else "N/A",
        "pm":             "Ver cadena de opciones en IBKR",
        "c5":             "—",
        "c15":            "—",
        "sl":             f"${price * 0.95:.2f}" if price else "N/A",
        "sentiment":      "POSITIVO" if change_pct > 0 else "NEGATIVO",
        "control":        "COMPRADORES" if change_pct > 0 and scoring["rel_volume"] >= 1.5 else "VENDEDORES",
        "control_detail": f"Volumen {scoring['rel_volume']:.1f}x promedio, precio {'sobre' if scoring['above_sma50'] else 'bajo'} MA50",
        "source":         "polygon+finviz",
        "timestamp":      datetime.now().isoformat(timespec='seconds'),
    }


@app.route('/api/debug')
def api_debug():
    """Test Polygon connection directly and show raw response."""
    results = {}

    # Test 1: Basic stock snapshot
    try:
        r = requests.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/NVDA",
            params={"apiKey": POLYGON_API_KEY},
            timeout=10
        )
        results["polygon_snapshot"] = {
            "status": r.status_code,
            "ok": r.status_code == 200,
            "response": r.json() if r.status_code == 200 else r.text[:300]
        }
    except Exception as e:
        results["polygon_snapshot"] = {"error": str(e)}

    # Test 2: SMA indicator
    try:
        r2 = requests.get(
            "https://api.polygon.io/v1/indicators/sma/NVDA",
            params={"apiKey": POLYGON_API_KEY, "window": 50, "timespan": "day", "limit": 1},
            timeout=10
        )
        results["polygon_sma"] = {
            "status": r2.status_code,
            "ok": r2.status_code == 200,
            "response": r2.json() if r2.status_code == 200 else r2.text[:300]
        }
    except Exception as e:
        results["polygon_sma"] = {"error": str(e)}

    # Test 3: Yahoo Finance
    try:
        r3 = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/NVDA",
            params={"interval": "1d", "range": "2d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        meta = r3.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
        results["yahoo"] = {
            "status": r3.status_code,
            "ok": r3.status_code == 200,
            "price": meta.get("regularMarketPrice"),
            "volume": meta.get("regularMarketVolume"),
            "change_pct": meta.get("regularMarketChangePercent"),
        }
    except Exception as e:
        results["yahoo"] = {"error": str(e)}

    # Test 4: Finviz
    try:
        r4 = requests.get(
            "https://finviz.com/quote.ashx?t=NVDA",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        results["finviz"] = {
            "status": r4.status_code,
            "ok": r4.status_code == 200,
            "has_data": "Short Float" in r4.text
        }
    except Exception as e:
        results["finviz"] = {"error": str(e)}

    results["polygon_key_prefix"] = POLYGON_API_KEY[:8] + "..." if POLYGON_API_KEY else "NO KEY"
    results["timestamp"] = datetime.now().isoformat()

    return jsonify(results)


# ── ENDPOINTS ─────────────────────────────────────────────────
@app.route('/api/ticker/<ticker>')
def api_ticker(ticker: str):
    result = analyze_ticker(ticker.upper())
    if "error" in result:
        return jsonify(result), 404
    return jsonify({"ticker": ticker.upper(), "items": [result], "source": "polygon+finviz"})


@app.route('/api/flow')
def api_flow():
    tickers_raw = request.args.get('tickers', '')
    market      = request.args.get('market', 'neutral')

    if not tickers_raw:
        return jsonify({"source": "demo", "summary": "Selecciona tickers para analizar.", "items": []})

    tickers = [t.strip().upper() for t in tickers_raw.split(',') if t.strip()][:10]
    items   = []
    for tk in tickers:
        result = analyze_ticker(tk)
        if "error" not in result:
            items.append(result)

    if not items:
        return jsonify({"source": "error", "summary": "Sin datos disponibles.", "items": []})

    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")

    return jsonify({
        "source":     "polygon+finviz",
        "market_mode": market,
        "timestamp":  datetime.now().isoformat(timespec='seconds'),
        "summary":    f"{verdes} setup(s) con señal verde de {len(items)} analizados." if verdes else f"{len(items)} tickers analizados. Sin señales verdes claras.",
        "items":      items,
    })


@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    payload = request.get_json(force=True, silent=True) or {}
    tickers_raw = payload.get('tickers', '')
    market      = payload.get('market', 'neutral')
    comment     = payload.get('comment', '')
    alert_raw   = payload.get('alert_raw', '')

    if not tickers_raw:
        return jsonify({'error': 'Falta el campo tickers'}), 400

    tickers = [t.strip().upper() for t in str(tickers_raw).split(',') if t.strip()]
    items   = []
    for tk in tickers:
        result = analyze_ticker(tk, alert_context=alert_raw, comment=comment)
        if "error" not in result:
            items.append(result)

    if not items:
        return jsonify({"source": "polygon+finviz", "summary": "Sin datos disponibles.", "items": []})

    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    verdes = sum(1 for x in items if x.get("semaforo") == "VERDE")

    return jsonify({
        "source":    "polygon+finviz",
        "timestamp": datetime.now().isoformat(timespec='seconds'),
        "summary":   f"{verdes} setup(s) con señal verde." if verdes else "Sin señales verdes.",
        "items":     items,
    })


# ── START ─────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
