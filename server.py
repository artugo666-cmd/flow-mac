"""
FlowScan v10 - Server Principal
============================================
Servidor Flask con endpoint de análisis profundo.

IMPORTANTE: este archivo NO ejecuta yfinance al arrancar.
analyze() solo se llama cuando alguien pide /analyze/<ticker>.

Si tenías rutas adicionales en tu server.py anterior
(dashboard HTML, scanner autónomo, parser de Discord, etc.)
ESAS RUTAS NO ESTÁN AQUÍ — hay que recuperarlas de un commit
anterior en GitHub y agregarlas a este archivo.
"""

import os
import time
from flask import Flask, jsonify

import yfinance as yf
import pandas as pd
import numpy as np

try:
    from yfinance.exceptions import YFRateLimitError
except ImportError:
    class YFRateLimitError(Exception):
        pass


app = Flask(__name__)


# ============================================================
# HELPER — Retry con backoff para evitar rate limit de Yahoo
# ============================================================

def _safe_yf_call(func, *args, retries=3, base_delay=2, **kwargs):
    last_error = None
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except YFRateLimitError as e:
            last_error = e
            time.sleep(base_delay * (2 ** attempt))
        except Exception as e:
            last_error = e
            time.sleep(base_delay)
    raise last_error


# ============================================================
# CAPA 1 — FUNDAMENTALES
# ============================================================

def get_fundamentals(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    try:
        info = _safe_yf_call(lambda: t.info)
    except Exception as e:
        return {"ticker": ticker, "error": f"No se pudieron obtener fundamentales (rate limit Yahoo): {e}"}

    if not info:
        return {"ticker": ticker, "error": "Yahoo devolvió respuesta vacía (posible rate limit)"}

    return {
        "ticker": ticker,
        "precio_actual": info.get("currentPrice") or info.get("regularMarketPrice"),
        "per_trailing": info.get("trailingPE"),
        "per_forward": info.get("forwardPE"),
        "eps_trailing": info.get("trailingEps"),
        "eps_forward": info.get("forwardEps"),
        "price_to_book": info.get("priceToBook"),
        "market_cap": info.get("marketCap"),
        "debt_to_equity": info.get("debtToEquity"),
        "total_debt": info.get("totalDebt"),
        "total_cash": info.get("totalCash"),
        "free_cashflow": info.get("freeCashflow"),
        "sector": info.get("sector"),
        "short_percent_float": info.get("shortPercentOfFloat"),
        "target_mean_price": info.get("targetMeanPrice"),
        "recommendation": info.get("recommendationKey"),
        "revenue_growth": info.get("revenueGrowth"),
        "earnings_growth": info.get("earningsGrowth"),
        "profit_margins": info.get("profitMargins"),
        "ma50": info.get("fiftyDayAverage"),
        "ma200": info.get("twoHundredDayAverage"),
        "52w_high": info.get("fiftyTwoWeekHigh"),
        "52w_low": info.get("fiftyTwoWeekLow"),
    }


def score_fundamentals(f: dict) -> dict:
    if f.get("error"):
        return {"score_fundamental": 0, "signals": [(f["error"], "ERROR", 0)]}

    signals = []
    score = 0

    eg = f.get("earnings_growth")
    if eg is not None:
        if eg > 0.15:
            signals.append(("EPS creciendo fuerte (+%.1f%%)" % (eg * 100), "ALZA", 2))
            score += 2
        elif eg < -0.05:
            signals.append(("EPS cayendo (%.1f%%)" % (eg * 100), "BAJA", -2))
            score -= 2

    per = f.get("per_forward") or f.get("per_trailing")
    if per is not None:
        if per > 60:
            signals.append((f"PER forward muy elevado ({per:.1f}x) — valuación exigente", "BAJA", -1))
            score -= 1
        elif 0 < per < 20:
            signals.append((f"PER forward bajo ({per:.1f}x) — posible value", "ALZA", 1))
            score += 1

    dte = f.get("debt_to_equity")
    if dte is not None:
        if dte > 150:
            signals.append((f"Debt/Equity alto ({dte:.0f}%) — apalancamiento agresivo", "BAJA", -2))
            score -= 2
        elif dte < 50:
            signals.append((f"Debt/Equity sano ({dte:.0f}%)", "ALZA", 1))
            score += 1

    fcf = f.get("free_cashflow")
    if fcf is not None:
        if fcf < 0:
            signals.append(("Free Cash Flow NEGATIVO — quemando caja", "BAJA", -2))
            score -= 2
        else:
            signals.append(("Free Cash Flow positivo", "ALZA", 1))
            score += 1

    price = f.get("precio_actual")
    ma50 = f.get("ma50")
    ma200 = f.get("ma200")
    if price and ma50 and ma200:
        if price > ma50 > ma200:
            signals.append(("Tendencia alcista confirmada (Precio > MA50 > MA200)", "ALZA", 2))
            score += 2
        elif price < ma50 < ma200:
            signals.append(("Tendencia bajista confirmada (Precio < MA50 < MA200)", "BAJA", -2))
            score -= 2
        elif price > ma200 and price < ma50:
            signals.append(("Corrección dentro de tendencia alcista mayor", "NEUTRAL", 0))

    h52 = f.get("52w_high")
    l52 = f.get("52w_low")
    if price and h52 and l52 and h52 != l52:
        pos = (price - l52) / (h52 - l52)
        if pos < 0.25:
            signals.append((f"Precio cerca del low 52w ({pos*100:.0f}% del rango) — posible zona de compra", "ALZA", 1))
            score += 1
        elif pos > 0.85:
            signals.append((f"Precio cerca del high 52w ({pos*100:.0f}% del rango) — extendido, cuidado con entradas", "BAJA", -1))
            score -= 1

    target = f.get("target_mean_price")
    if price and target:
        upside = (target - price) / price
        if upside > 0.15:
            signals.append((f"Target analistas implica +{upside*100:.0f}% upside", "ALZA", 1))
            score += 1
        elif upside < -0.10:
            signals.append((f"Target analistas implica {upside*100:.0f}% downside", "BAJA", -1))
            score -= 1

    return {"score_fundamental": score, "signals": signals}


# ============================================================
# CAPA 2 — NIVELES TÉCNICOS
# ============================================================

def get_technical_levels(ticker: str, period="6mo") -> dict:
    t = yf.Ticker(ticker)
    try:
        hist = _safe_yf_call(lambda: t.history(period=period))
    except Exception as e:
        return {"error": f"No se pudo obtener histórico (rate limit Yahoo): {e}"}

    if hist.empty:
        return {"error": "Sin datos históricos"}

    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]

    last_close = close.iloc[-1]
    last_high = high.iloc[-1]
    last_low = low.iloc[-1]

    pivot = (last_high + last_low + last_close) / 3
    r1 = 2 * pivot - last_low
    s1 = 2 * pivot - last_high
    r2 = pivot + (last_high - last_low)
    s2 = pivot - (last_high - last_low)

    window = 5
    swing_highs, swing_lows = [], []
    for i in range(window, len(hist) - window):
        seg_high = high.iloc[i - window:i + window + 1]
        seg_low = low.iloc[i - window:i + window + 1]
        if high.iloc[i] == seg_high.max():
            swing_highs.append(round(high.iloc[i], 2))
        if low.iloc[i] == seg_low.min():
            swing_lows.append(round(low.iloc[i], 2))

    resistencias = sorted(set([r for r in swing_highs if r > last_close]))[:3]
    soportes = sorted(set([s for s in swing_lows if s < last_close]), reverse=True)[:3]

    ma20 = close.rolling(20).mean().iloc[-1]
    ma50 = close.rolling(50).mean().iloc[-1]
    ma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None

    return {
        "precio_actual": round(last_close, 2),
        "pivot_points": {
            "pivot": round(pivot, 2), "r1": round(r1, 2), "r2": round(r2, 2),
            "s1": round(s1, 2), "s2": round(s2, 2),
        },
        "resistencias_swing": resistencias,
        "soportes_swing": soportes,
        "ma20": round(ma20, 2) if not np.isnan(ma20) else None,
        "ma50": round(ma50, 2) if not np.isnan(ma50) else None,
        "ma200": round(ma200, 2) if ma200 is not None and not np.isnan(ma200) else None,
    }


# ============================================================
# CAPA 3 — OPTIONS FLOW
# ============================================================

def score_options_flow(flow_data: list) -> dict:
    if not flow_data:
        return {"score_flow": 0, "signals": [("Sin datos de flow", "NEUTRAL", 0)]}

    call_premium = sum(f["premium"] for f in flow_data if f["type"] == "CALL")
    put_premium = sum(f["premium"] for f in flow_data if f["type"] == "PUT")
    total = call_premium + put_premium

    if total == 0:
        return {"score_flow": 0, "signals": [("Sin premium registrado", "NEUTRAL", 0)]}

    signals = []
    score = 0
    call_pct = call_premium / total
    put_call_ratio = put_premium / call_premium if call_premium > 0 else float("inf")

    if call_pct > 0.75:
        signals.append((f"Flujo dominado por CALLS ({call_pct*100:.0f}% del premium)", "ALZA", 3))
        score += 3
    elif call_pct < 0.25:
        signals.append((f"Flujo dominado por PUTS ({(1-call_pct)*100:.0f}% del premium)", "BAJA", -3))
        score -= 3
    else:
        signals.append((f"Flujo mixto (Calls {call_pct*100:.0f}% / Puts {(1-call_pct)*100:.0f}%)", "NEUTRAL", 0))

    signals.append((f"Put/Call ratio (premium): {put_call_ratio:.2f}", "INFO", 0))
    signals.append((f"Total premium analizado: ${total:,.0f}", "INFO", 0))

    return {
        "score_flow": score,
        "call_premium": call_premium,
        "put_premium": put_premium,
        "put_call_ratio": round(put_call_ratio, 2),
        "signals": signals,
    }


# ============================================================
# CAPA 4 — SCORE COMPUESTO Y DECISIÓN
# ============================================================

def composite_decision(fund_score: int, flow_score: int, technical_bias: str = "NEUTRAL") -> dict:
    bias_score = {"ALZA": 2, "NEUTRAL": 0, "BAJA": -2}.get(technical_bias, 0)
    total = fund_score + flow_score + bias_score

    if total >= 4:
        decision = "ENTRAR (LONG)"
    elif total <= -4:
        decision = "ENTRAR (SHORT/EVITAR LONG)"
    elif -1 <= total <= 1:
        decision = "ESPERAR"
    else:
        decision = "MONITOREAR"

    return {
        "score_total": total,
        "score_fundamental": fund_score,
        "score_flow": flow_score,
        "score_tecnico_bias": bias_score,
        "decision": decision,
    }


# ============================================================
# RUNNER
# ============================================================

def analyze(ticker: str, flow_data: list = None):
    fund = get_fundamentals(ticker)
    fund_score_data = score_fundamentals(fund)

    tech = get_technical_levels(ticker)
    if "error" not in tech:
        price = tech["precio_actual"]
        if tech["ma50"] and price > tech["ma50"]:
            technical_bias = "ALZA"
        elif tech["ma50"] and price < tech["ma50"]:
            technical_bias = "BAJA"
        else:
            technical_bias = "NEUTRAL"
    else:
        technical_bias = "NEUTRAL"

    flow_score_data = score_options_flow(flow_data or [])

    decision = composite_decision(
        fund_score_data["score_fundamental"],
        flow_score_data["score_flow"],
        technical_bias,
    )

    return {
        "fundamentals": fund,
        "fundamental_score": fund_score_data,
        "technical": tech,
        "flow_score": flow_score_data,
        "decision": decision,
    }


# ============================================================
# RUTAS FLASK
# ============================================================

@app.route("/")
def home():
    return jsonify({"status": "ok", "service": "FlowScan v10 Analyzer"})


@app.route("/analyze/<ticker>")
def analyze_ticker(ticker):
    try:
        result = analyze(ticker.upper())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "alive"})


# ============================================================
# ARRANQUE — NO ejecuta yfinance aquí
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
