from __future__ import annotations

import subprocess, sys

def install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

try:
    import requests
except ImportError:
    install("requests")
    import requests

try:
    import flask
except ImportError:
    install("flask")

try:
    import flask_cors
except ImportError:
    install("flask-cors")

import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='.')
CORS(app)

# ============================================================
# FlowScan Backend — Corregido
# Fase 1: datos demo con scoring real
# Fase 2: Polygon.io en tiempo real (ya conectado)
# ============================================================

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")


# ── MODELO DE ALERTA ────────────────────────────────────────
@dataclass
class FlowAlert:
    ticker: str
    direction: str        # CALL | PUT
    contract: str
    expiry: str
    strike: float
    spot: float
    premium: float        # dolares totales del flujo
    volume: int
    open_interest: int
    bid: float
    ask: float
    execution: str        # above_ask | ask | mid | below_bid
    catalyst: str
    momentum: str
    vwap_trigger: str
    stop: str
    target_1: str
    target_2: str

    @property
    def vol_oi(self) -> float:
        if self.open_interest <= 0:
            return 999.0
        return round(self.volume / self.open_interest, 2)

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return 999.0
        return round(((self.ask - self.bid) / mid) * 100, 2)


# ── DATOS DEMO ───────────────────────────────────────────────
DEMO_FLOW: List[FlowAlert] = [
    FlowAlert('NVDA', 'CALL', 'NVDA 2026-05-15 C 950',  '2026-05-15', 950,  914.20, 850000, 8200, 920,  7.80, 8.20,  'above_ask', 'AI / semis momentum',              'alcista sobre VWAP', 'romper 918 con volumen 5m > promedio', 'pierde VWAP 5m', '930', '950'),
    FlowAlert('AMD',  'CALL', 'AMD 2026-05-15 C 190',   '2026-05-15', 190,  181.50, 410000, 6500, 700,  2.10, 2.28,  'ask',       'semis sympathy + volumen fresco',   'alcista',            'romper 183.20 y sostener VWAP',        '181 bajo VWAP',  '187', '190'),
    FlowAlert('TSLA', 'PUT',  'TSLA 2026-05-08 P 145',  '2026-05-08', 145,  151.10, 620000, 9800, 1300, 3.40, 3.75,  'above_ask', 'presión técnica / debilidad',        'bajista bajo VWAP',  'rechazo en 152 y pierde 150.50',       'recupera 153',   '147', '145'),
    FlowAlert('AAPL', 'CALL', 'AAPL 2026-05-15 C 190',  '2026-05-15', 190,  184.80, 210000, 3000, 2600, 1.15, 1.32,  'mid',       'sin catalizador fuerte',             'lateral',            'solo si rompe 186 con volumen',         '183.50',         '188', '190'),
    FlowAlert('RBLX', 'PUT',  'RBLX 2026-05-15 P 60',   '2026-05-15', 60,   63.20,  355000, 4400, 500,  1.70, 1.88,  'ask',       'debilidad growth / ruptura soporte', 'bajista',            'perder 62.80 con volumen',             '64.20',          '61',  '60'),
]


# ── SISTEMA DE SCORING ───────────────────────────────────────
def score_alert(
    a: FlowAlert,
    market_mode: str,
    max_spread: float,
    min_vol_oi: float,
    min_premium: float
) -> Dict[str, Any]:

    vol_oi = a.vol_oi
    spread = a.spread_pct

    # V1 — Catalizador (max 2.5)
    pts_catalyst = 2.5 if a.catalyst and 'sin catalizador' not in a.catalyst.lower() else 0.5

    # V2 — Flujo premium (max 2.5)
    if a.premium >= 1_000_000:
        pts_flow = 2.5
    elif a.premium >= 500_000:
        pts_flow = 2.0
    elif a.premium >= 300_000:
        pts_flow = 1.5
    elif a.premium >= 150_000:
        pts_flow = 0.8
    else:
        pts_flow = 0.3

    # V3 — Vol / OI ratio (max 1.5)
    if vol_oi >= 8:
        pts_voloi = 1.5
    elif vol_oi >= 5:
        pts_voloi = 1.0
    elif vol_oi >= 2:
        pts_voloi = 0.5
    else:
        pts_voloi = 0.0

    # V4 — Tipo de ejecucion (max 1.5) — above ask = institucional agresivo
    if a.execution == 'above_ask':
        pts_execution = 1.5
    elif a.execution == 'ask':
        pts_execution = 1.0
    else:
        pts_execution = 0.2

    # V5 — Momentum alineado con direccion (max 2.0)
    direction_alcista = 'alcista' in a.momentum.lower() and a.direction == 'CALL'
    direction_bajista = 'bajista' in a.momentum.lower() and a.direction == 'PUT'
    pts_momentum = 2.0 if (direction_alcista or direction_bajista) else 0.6

    # V6 — Spread ajustado (max 1.0)
    pts_spread = 1.0 if spread <= max_spread else 0.0

    raw = pts_catalyst + pts_flow + pts_voloi + pts_execution + pts_momentum + pts_spread

    # Ajuste dinamico por regimen de mercado
    if market_mode == 'volatile':
        raw += 0.4 if a.execution in ('ask', 'above_ask') else -0.2
    elif market_mode == 'bearish' and a.direction == 'PUT':
        raw += 0.5
    elif market_mode == 'bullish' and a.direction == 'CALL':
        raw += 0.5

    score = round(min(raw, 10.0), 1)

    # Filtros duros para VERDE
    passes = (
        a.premium >= min_premium and
        vol_oi >= min_vol_oi and
        spread <= max_spread and
        a.execution in ('ask', 'above_ask') and
        score >= 7.0
    )

    if passes:
        action   = 'VAMOS_CON_TODO'
        semaforo = 'VERDE'
    elif score >= 5.0:
        action   = 'ESPERAR_CONFIRMACION'
        semaforo = 'AMARILLO'
    else:
        action   = 'NO_OPERAR'
        semaforo = 'ROJO'

    d = asdict(a)
    d.update({
        'vol_oi':          vol_oi,
        'spread_pct':      spread,
        'score':           score,
        'semaforo':        semaforo,
        'action':          action,
        'passes_filters':  passes,
        'score_breakdown': {
            'catalyst':      pts_catalyst,
            'premium_flow':  pts_flow,
            'vol_oi_ratio':  pts_voloi,
            'execution':     pts_execution,
            'momentum':      pts_momentum,
            'spread':        pts_spread,
        }
    })
    return d


# ── POLYGON HELPER ───────────────────────────────────────────
def fetch_polygon_flow(ticker: str) -> List[FlowAlert]:
    """
    Llama a Polygon.io para obtener snapshot de opciones de un ticker.
    Filtra contratos con volumen inusual (vol/OI > 2) y los convierte a FlowAlert.
    """
    if not POLYGON_API_KEY:
        return []

    url = f"https://api.polygon.io/v3/snapshot/options/{ticker}"
    params = {
        "apiKey": POLYGON_API_KEY,
        "limit":  50,
        "order":  "desc",
        "sort":   "volume",
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Polygon error for {ticker}: {e}")
        return []

    alerts = []
    results = data.get("results", [])

    for item in results:
        details = item.get("details", {})
        greeks  = item.get("greeks", {})
        day     = item.get("day", {})
        last    = item.get("last_quote", {})

        volume = int(day.get("volume", 0))
        oi     = int(item.get("open_interest", 0))
        if volume == 0 or oi == 0:
            continue

        vol_oi_ratio = volume / oi
        if vol_oi_ratio < 1.5:   # solo flujo inusual
            continue

        contract_type = details.get("contract_type", "call").upper()
        strike        = float(details.get("strike_price", 0))
        expiry        = details.get("expiration_date", "")
        bid           = float(last.get("bid", 0))
        ask           = float(last.get("ask", 0))
        mid           = (bid + ask) / 2
        premium       = round(mid * volume * 100, 2)

        # Determinar tipo de ejecucion aproximado
        last_price = float(day.get("close", mid))
        if last_price >= ask:
            execution = "above_ask"
        elif last_price >= ask * 0.98:
            execution = "ask"
        else:
            execution = "mid"

        alert = FlowAlert(
            ticker        = ticker,
            direction     = contract_type,
            contract      = f"{ticker} {expiry} {contract_type[0]} {strike}",
            expiry        = expiry,
            strike        = strike,
            spot          = float(item.get("underlying_asset", {}).get("price", 0)),
            premium       = premium,
            volume        = volume,
            open_interest = oi,
            bid           = bid,
            ask           = ask,
            execution     = execution,
            catalyst      = "flujo inusual detectado via Polygon",
            momentum      = "alcista" if contract_type == "CALL" else "bajista",
            vwap_trigger  = f"romper strike {strike} con volumen",
            stop          = "",
            target_1      = str(round(strike * 1.03, 2)),
            target_2      = str(round(strike * 1.06, 2)),
        )
        alerts.append(alert)

    return alerts


# ── RUTAS ────────────────────────────────────────────────────
@app.route('/')
def home():
    return send_from_directory('.', 'index.html')


@app.route('/api/health')
def health():
    polygon_ok = bool(POLYGON_API_KEY)
    return jsonify({
        'ok':          True,
        'service':     'FlowScan Backend',
        'version':     '1.0',
        'time':        datetime.now().isoformat(timespec='seconds'),
        'polygon_key': 'configurada' if polygon_ok else 'FALTA — agrega POLYGON_API_KEY en Render',
    })


@app.route('/api/flow')
def api_flow():
    """
    Devuelve alertas rankeadas.
    Si POLYGON_API_KEY esta configurada, busca flujo real de los tickers solicitados.
    Si no, devuelve datos demo.
    """
    market_mode  = request.args.get('market',      'neutral')
    max_spread   = float(request.args.get('max_spread',   15))
    min_vol_oi   = float(request.args.get('min_vol_oi',    5))
    min_premium  = float(request.args.get('min_premium', 300_000))
    tickers_raw  = request.args.get('tickers', '')   # ej: "NVDA,AMD,TSLA"

    # Fuente de datos: Polygon real o demo
    if POLYGON_API_KEY and tickers_raw:
        tickers = [t.strip().upper() for t in tickers_raw.split(',') if t.strip()]
        flow_data: List[FlowAlert] = []
        for tk in tickers:
            flow_data.extend(fetch_polygon_flow(tk))
        source = 'polygon_realtime'
    else:
        flow_data = DEMO_FLOW
        source    = 'demo_manual'

    if not flow_data:
        return jsonify({
            'source':  source,
            'summary': 'Sin datos. Verifica POLYGON_API_KEY y tickers.',
            'items':   [],
        })

    ranked = [score_alert(x, market_mode, max_spread, min_vol_oi, min_premium) for x in flow_data]
    ranked.sort(key=lambda x: x['score'], reverse=True)

    verdes = sum(1 for x in ranked if x['semaforo'] == 'VERDE')
    summary = 'NO HAY NADA QUE OPERE HOY' if verdes == 0 else f'{verdes} setup(s) pasan todos los filtros — listos para entrar.'

    return jsonify({
        'source':      source,
        'market_mode': market_mode,
        'timestamp':   datetime.now().isoformat(timespec='seconds'),
        'filters': {
            'max_spread':  max_spread,
            'min_vol_oi':  min_vol_oi,
            'min_premium': min_premium,
        },
        'summary': summary,
        'items':   ranked,
    })


@app.route('/api/manual', methods=['POST'])
def api_manual():
    """
    Recibe alertas manuales en JSON y las rankea con el mismo scoring.
    Util para pegar las alertas de tu grupo de Discord/Telegram.
    """
    payload     = request.get_json(force=True, silent=True) or {}
    rows        = payload.get('items', [])
    market_mode = payload.get('market',      'neutral')
    max_spread  = float(payload.get('max_spread',   15))
    min_vol_oi  = float(payload.get('min_vol_oi',    5))
    min_premium = float(payload.get('min_premium', 300_000))

    if not rows:
        return jsonify({'error': 'Manda items en el body JSON'}), 400

    alerts = []
    for r in rows:
        try:
            alerts.append(FlowAlert(
                ticker        = str(r.get('ticker',       '')).upper(),
                direction     = str(r.get('direction',    'CALL')).upper(),
                contract      = str(r.get('contract',     '')),
                expiry        = str(r.get('expiry',       '')),
                strike        = float(r.get('strike',     0)),
                spot          = float(r.get('spot',       0)),
                premium       = float(r.get('premium',    0)),
                volume        = int(r.get('volume',       0)),
                open_interest = int(r.get('open_interest',0)),
                bid           = float(r.get('bid',        0)),
                ask           = float(r.get('ask',        0)),
                execution     = str(r.get('execution',    'mid')),
                catalyst      = str(r.get('catalyst',     '')),
                momentum      = str(r.get('momentum',     'lateral')),
                vwap_trigger  = str(r.get('vwap_trigger', '')),
                stop          = str(r.get('stop',         '')),
                target_1      = str(r.get('target_1',     '')),
                target_2      = str(r.get('target_2',     '')),
            ))
        except Exception as e:
            print(f"Error parsing row {r}: {e}")
            continue

    if not alerts:
        return jsonify({'error': 'Ninguna alerta valida en el payload'}), 400

    ranked = [score_alert(x, market_mode, max_spread, min_vol_oi, min_premium) for x in alerts]
    ranked.sort(key=lambda x: x['score'], reverse=True)
    verdes = sum(1 for x in ranked if x['semaforo'] == 'VERDE')

    return jsonify({
        'source':  'manual_payload',
        'summary': 'NO HAY NADA' if verdes == 0 else f'{verdes} setup(s) pasan filtros duros.',
        'items':   ranked,
    })


@app.route('/api/ticker/<ticker>')
def api_ticker(ticker: str):
    """
    Endpoint rapido para ver flujo inusual de un ticker especifico via Polygon.
    Ejemplo: GET /api/ticker/AMD
    """
    if not POLYGON_API_KEY:
        return jsonify({'error': 'POLYGON_API_KEY no configurada en Render'}), 503

    market_mode = request.args.get('market',      'neutral')
    max_spread  = float(request.args.get('max_spread',   15))
    min_vol_oi  = float(request.args.get('min_vol_oi',    2))
    min_premium = float(request.args.get('min_premium', 100_000))

    alerts = fetch_polygon_flow(ticker.upper())
    if not alerts:
        return jsonify({'ticker': ticker.upper(), 'message': 'Sin flujo inusual detectado hoy.', 'items': []})

    ranked = [score_alert(x, market_mode, max_spread, min_vol_oi, min_premium) for x in alerts]
    ranked.sort(key=lambda x: x['score'], reverse=True)

    return jsonify({
        'ticker':    ticker.upper(),
        'source':    'polygon_realtime',
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'items':     ranked,
    })



# ── ANTHROPIC ANALYZE ─────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    """
    Recibe un ticker + contexto, llama a Anthropic Claude desde el servidor
    y devuelve el analisis completo. Evita el problema CORS del navegador.
    """
    import json as _json

    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'ANTHROPIC_API_KEY no configurada en Render'}), 503

    payload  = request.get_json(force=True, silent=True) or {}
    ticker   = str(payload.get('ticker', '')).upper()
    raw      = str(payload.get('alert_raw', ticker))
    comment  = str(payload.get('comment', ''))
    market   = str(payload.get('market', 'neutral'))
    cap_min  = str(payload.get('cap_min', '500'))
    cap_max  = str(payload.get('cap_max', '1500'))

    if not ticker:
        return jsonify({'error': 'Falta el campo ticker'}), 400

    mkt_map = {'bullish':'alcista','bearish':'bajista','neutral':'neutral/mixto','volatile':'muy volatil'}
    mkt_txt = mkt_map.get(market, 'neutral/mixto')
    cmt_ctx = f"\nInfo adicional: {comment}" if comment else ""

    prompt = (
        f"Analiza {ticker} para opciones. BUSCA EN INTERNET: precio actual, "
        f"volumen vs promedio 20d, %vs MA5/MA20/MA50, noticias 7 dias, "
        f"short interest, sector, compradores vs vendedores (OBV).\n"
        f"Contexto: {raw}{cmt_ctx}\n"
        f"Mercado: {mkt_txt}. Capital: ${cap_min}-${cap_max} IBKR.\n\n"
        f"SCORING (max 10.0):\n"
        f"V1 Catalizador(max3.0): earnings/M&A=3.0|upgrade=2.5|noticia=1.5|nada=0.0\n"
        f"V2 Flujo(max2.5): >5M=2.5|2-5M=2.0|1-2M=1.5|<1M=0.5\n"
        f"V3 Momentum(max2.0): sobreMA20+MA50+vol>150pct=2.0|sobreMA50=1.2|lateral=0.6|bajo=0.0\n"
        f"V4 Sector(max1.5): alcista=1.5|neutral=0.8|cae=0.0\n"
        f"V5 Short(max1.0): >20pct=1.0|10-20pct=0.6|<10pct=0.2\n"
        f"VERDE>=7|AMARILLO>=4|ROJO<4\n\n"
        f"Responde SOLO JSON sin markdown:\n"
        f"{{\"ticker\":\"{ticker}\",\"type\":\"CALL\",\"score\":7.5,\"semaforo\":\"VERDE\","
        f"\"pts_catalyst\":2.5,\"pts_flow\":2.0,\"pts_momentum\":1.5,\"pts_sector\":1.0,\"pts_short\":0.5,"
        f"\"score_breakdown\":\"Cat 2.5+Flujo 2.0+Mom 1.5+Sec 1.0+Short 0.5=7.5\","
        f"\"strike\":100,\"expiry\":\"jun 2025\",\"flow_usd\":\"$1.8M\",\"price\":\"$95\"," 
        f"\"vol_vs_avg\":\"185%\",\"ma5_pct\":\"+6%\",\"ma20_pct\":\"+11%\",\"ma50_pct\":\"+18%\"," 
        f"\"momentum\":\"ALCISTA\",\"rsi\":\"Normal 55\",\"short_interest\":\"12%\"," 
        f"\"news\":\"noticia o ninguna\",\"sentiment\":\"POSITIVO\"," 
        f"\"control\":\"COMPRADORES 71%\",\"control_detail\":\"OBV al alza\"," 
        f"\"why\":\"razon en 2 oraciones\",\"risk\":\"riesgo en 1 oracion\",\"catalyst\":\"evento o ninguno\"," 
        f"\"ez\":\"$90-92\",\"pm\":\"$120-200\",\"c5\":3,\"c15\":9,\"sl\":\"$85\"}}"
    )

    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':      'claude-sonnet-4-6',
                'max_tokens': 1400,
                'tools': [{'type': 'web_search_20250305', 'name': 'web_search'}],
                'messages': [{'role': 'user', 'content': prompt}]
            },
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return jsonify({'error': f'Error llamando a Anthropic: {str(e)}'}), 502

    text  = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
    start = text.find('{')
    end   = text.rfind('}')
    if start == -1 or end == -1:
        return jsonify({'error': 'Claude no devolvio JSON', 'raw': text[:300]}), 502
    try:
        return jsonify(_json.loads(text[start:end+1]))
    except Exception as e:
        return jsonify({'error': f'JSON invalido: {str(e)}', 'raw': text[:300]}), 502

# ── ARRANQUE ─────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
