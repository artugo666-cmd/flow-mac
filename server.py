"""
FlowScan v10 - Módulo de Análisis Profundo
============================================
Analiza un ticker en 4 capas:
  1. Fundamentales (PER, EPS, Deuda)
  2. Niveles técnicos (Soportes/Resistencias)
  3. Interpretación de Options Flow (input manual desde SensaMarket/Unusual Whales)
  4. Score compuesto con variables alcistas/bajistas

Diseñado para integrarse como blueprint en tu app Flask existente.
Uso standalone:  python analyzer_module.py DXYZ
"""

import sys
import yfinance as yf
import pandas as pd
import numpy as np


# ============================================================
# CAPA 1 — FUNDAMENTALES
# ============================================================

def get_fundamentals(ticker: str) -> dict:
    """Extrae métricas fundamentales clave vía yfinance."""
    t = yf.Ticker(ticker)
    info = t.info

    data = {
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
    return data


def score_fundamentals(f: dict) -> dict:
    """
    Convierte fundamentales en señales ALZA / BAJA / NEUTRAL.
    Cada regla es ajustable según tu criterio de PEMEX-grade analysis.
    """
    signals = []
    score = 0  # -10 a +10 acumulado

    # --- EPS growth ---
    eg = f.get("earnings_growth")
    if eg is not None:
        if eg > 0.15:
            signals.append(("EPS creciendo fuerte (+%.1f%%)" % (eg * 100), "ALZA", 2))
            score += 2
        elif eg < -0.05:
            signals.append(("EPS cayendo (%.1f%%)" % (eg * 100), "BAJA", -2))
            score -= 2

    # --- PER vs sector (regla simple: PER muy alto = riesgo de corrección) ---
    per = f.get("per_forward") or f.get("per_trailing")
    if per is not None:
        if per > 60:
            signals.append((f"PER forward muy elevado ({per:.1f}x) — valuación exigente", "BAJA", -1))
            score -= 1
        elif 0 < per < 20:
            signals.append((f"PER forward bajo ({per:.1f}x) — posible value", "ALZA", 1))
            score += 1

    # --- Deuda ---
    dte = f.get("debt_to_equity")
    if dte is not None:
        if dte > 150:
            signals.append((f"Debt/Equity alto ({dte:.0f}%) — apalancamiento agresivo", "BAJA", -2))
            score -= 2
        elif dte < 50:
            signals.append((f"Debt/Equity sano ({dte:.0f}%)", "ALZA", 1))
            score += 1

    # --- Free cash flow ---
    fcf = f.get("free_cashflow")
    if fcf is not None:
        if fcf < 0:
            signals.append(("Free Cash Flow NEGATIVO — quemando caja", "BAJA", -2))
            score -= 2
        else:
            signals.append(("Free Cash Flow positivo", "ALZA", 1))
            score += 1

    # --- Precio vs medias móviles ---
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

    # --- Distancia a 52w high/low (zona de compra) ---
    h52 = f.get("52w_high")
    l52 = f.get("52w_low")
    if price and h52 and l52 and h52 != l52:
        pos = (price - l52) / (h52 - l52)  # 0 = en el low, 1 = en el high
        if pos < 0.25:
            signals.append((f"Precio cerca del low 52w ({pos*100:.0f}% del rango) — posible zona de compra", "ALZA", 1))
            score += 1
        elif pos > 0.85:
            signals.append((f"Precio cerca del high 52w ({pos*100:.0f}% del rango) — extendido, cuidado con entradas", "BAJA", -1))
            score -= 1

    # --- Target de analistas ---
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
# CAPA 2 — NIVELES TÉCNICOS (Soportes / Resistencias)
# ============================================================

def get_technical_levels(ticker: str, period="6mo") -> dict:
    """
    Calcula soportes y resistencias usando:
      - Pivot points clásicos (último periodo)
      - Máximos/mínimos locales (swing highs/lows)
      - Medias móviles como soporte/resistencia dinámico
    """
    t = yf.Ticker(ticker)
    hist = t.history(period=period)

    if hist.empty:
        return {"error": "Sin datos históricos"}

    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]

    last_close = close.iloc[-1]
    last_high = high.iloc[-1]
    last_low = low.iloc[-1]

    # --- Pivot Points clásicos (basado en última sesión) ---
    pivot = (last_high + last_low + last_close) / 3
    r1 = 2 * pivot - last_low
    s1 = 2 * pivot - last_high
    r2 = pivot + (last_high - last_low)
    s2 = pivot - (last_high - last_low)

    # --- Swing highs / lows (ventana de 5 días) ---
    window = 5
    swing_highs = []
    swing_lows = []
    for i in range(window, len(hist) - window):
        seg_high = high.iloc[i - window:i + window + 1]
        seg_low = low.iloc[i - window:i + window + 1]
        if high.iloc[i] == seg_high.max():
            swing_highs.append(round(high.iloc[i], 2))
        if low.iloc[i] == seg_low.min():
            swing_lows.append(round(low.iloc[i], 2))

    # Niveles más relevantes = los más cercanos al precio actual, sin duplicados
    resistencias = sorted(set([r for r in swing_highs if r > last_close]))[:3]
    soportes = sorted(set([s for s in swing_lows if s < last_close]), reverse=True)[:3]

    # --- Medias móviles ---
    ma20 = close.rolling(20).mean().iloc[-1]
    ma50 = close.rolling(50).mean().iloc[-1]
    ma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None

    return {
        "precio_actual": round(last_close, 2),
        "pivot_points": {
            "pivot": round(pivot, 2),
            "r1": round(r1, 2),
            "r2": round(r2, 2),
            "s1": round(s1, 2),
            "s2": round(s2, 2),
        },
        "resistencias_swing": resistencias,
        "soportes_swing": soportes,
        "ma20": round(ma20, 2) if not np.isnan(ma20) else None,
        "ma50": round(ma50, 2) if not np.isnan(ma50) else None,
        "ma200": round(ma200, 2) if ma200 and not np.isnan(ma200) else None,
    }


# ============================================================
# CAPA 3 — OPTIONS FLOW (input manual desde SensaMarket / Unusual Whales)
# ============================================================

def score_options_flow(flow_data: list) -> dict:
    """
    flow_data: lista de dicts con sweeps detectados manualmente, ej:
    [
      {"type": "CALL", "side": "Ask", "strike": 40, "premium": 150000, "sentiment": "BULLISH"},
      {"type": "PUT",  "side": "Ask", "strike": 35, "premium": 50000,  "sentiment": "BEARISH"},
    ]

    Calcula ratio put/call ponderado por premium y dirección neta del flujo.
    """
    if not flow_data:
        return {"score_flow": 0, "signals": [("Sin datos de flow", "NEUTRAL", 0)]}

    call_premium = sum(f["premium"] for f in flow_data if f["type"] == "CALL")
    put_premium = sum(f["premium"] for f in flow_data if f["type"] == "PUT")
    total = call_premium + put_premium

    signals = []
    score = 0

    if total == 0:
        return {"score_flow": 0, "signals": [("Sin premium registrado", "NEUTRAL", 0)]}

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
    """
    Combina los scores en una decisión final tipo FlowScan
    (ENTRAR / ESPERAR / NO OPERAR)
    """
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
# RUNNER PRINCIPAL
# ============================================================

def analyze(ticker: str, flow_data: list = None):
    print(f"\n{'='*60}")
    print(f"  ANÁLISIS COMPLETO: {ticker}")
    print(f"{'='*60}\n")

    # Capa 1
    fund = get_fundamentals(ticker)
    fund_score_data = score_fundamentals(fund)

    print("--- FUNDAMENTALES ---")
    print(f"Precio actual: ${fund.get('precio_actual')}")
    print(f"PER forward: {fund.get('per_forward')}")
    print(f"EPS forward: {fund.get('eps_forward')}")
    print(f"Debt/Equity: {fund.get('debt_to_equity')}")
    print(f"Sector: {fund.get('sector')}")
    print()
    print("Señales fundamentales:")
    for s, direction, pts in fund_score_data["signals"]:
        print(f"  [{direction:>7}] {s}  ({pts:+d})")
    print(f"\n  Score Fundamental: {fund_score_data['score_fundamental']:+d}")

    # Capa 2
    print("\n--- NIVELES TÉCNICOS ---")
    tech = get_technical_levels(ticker)
    if "error" not in tech:
        print(f"Precio: ${tech['precio_actual']}")
        print(f"Pivot: {tech['pivot_points']}")
        print(f"Resistencias cercanas: {tech['resistencias_swing']}")
        print(f"Soportes cercanos: {tech['soportes_swing']}")
        print(f"MA20: {tech['ma20']} | MA50: {tech['ma50']} | MA200: {tech['ma200']}")

        # Bias técnico simple
        price = tech["precio_actual"]
        if tech["ma50"] and price > tech["ma50"]:
            technical_bias = "ALZA"
        elif tech["ma50"] and price < tech["ma50"]:
            technical_bias = "BAJA"
        else:
            technical_bias = "NEUTRAL"
    else:
        print(tech["error"])
        technical_bias = "NEUTRAL"

    # Capa 3
    print("\n--- OPTIONS FLOW ---")
    flow_score_data = score_options_flow(flow_data or [])
    for s, direction, pts in flow_score_data["signals"]:
        print(f"  [{direction:>7}] {s}")
    print(f"\n  Score Flow: {flow_score_data['score_flow']:+d}")

    # Capa 4
    print("\n--- DECISIÓN COMPUESTA ---")
    decision = composite_decision(
        fund_score_data["score_fundamental"],
        flow_score_data["score_flow"],
        technical_bias,
    )
    for k, v in decision.items():
        print(f"  {k}: {v}")

    print(f"\n{'='*60}\n")

    return {
        "fundamentals": fund,
        "fundamental_score": fund_score_data,
        "technical": tech,
        "flow_score": flow_score_data,
        "decision": decision,
    }


if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "DXYZ"

    # Ejemplo de flow_data manual (lo que copiarías de tu screenshot de SensaMarket)
    example_flow = [
        {"type": "CALL", "side": "Ask", "strike": 40, "premium": 102530, "sentiment": "BULLISH"},
        {"type": "PUT", "side": "Ask", "strike": 40, "premium": 29050, "sentiment": "BEARISH"},
    ]

    analyze(ticker, flow_data=example_flow)
