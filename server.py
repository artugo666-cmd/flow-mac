from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='.')
CORS(app)

# ============================================================
# FlowScan v3 Backend
# Fase 1: datos demo/manuales
# Fase 2: aquí se conectará Unusual Whales / Barchart / Polygon / Tradier
# ============================================================

@dataclass
class FlowAlert:
    ticker: str
    direction: str
    contract: str
    expiry: str
    strike: float
    spot: float
    premium: float
    volume: int
    open_interest: int
    bid: float
    ask: float
    execution: str
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


DEMO_FLOW: List[FlowAlert] = [
    FlowAlert('NVDA', 'CALL', 'NVDA 2026-05-15 C 950', '2026-05-15', 950, 914.20, 850000, 8200, 920, 7.80, 8.20, 'above_ask', 'AI / semis momentum', 'alcista sobre VWAP', 'romper 918 con volumen 5m > promedio', 'pierde VWAP 5m', '930', '950'),
    FlowAlert('AMD', 'CALL', 'AMD 2026-05-15 C 190', '2026-05-15', 190, 181.50, 410000, 6500, 700, 2.10, 2.28, 'ask', 'semis sympathy + volumen fresco', 'alcista', 'romper 183.20 y sostener VWAP', '181 bajo VWAP', '187', '190'),
    FlowAlert('TSLA', 'PUT', 'TSLA 2026-05-08 P 145', '2026-05-08', 145, 151.10, 620000, 9800, 1300, 3.40, 3.75, 'above_ask', 'presión técnica / debilidad relativa', 'bajista bajo VWAP', 'rechazo en 152 y pierde 150.50', 'recupera 153', '147', '145'),
    FlowAlert('AAPL', 'CALL', 'AAPL 2026-05-15 C 190', '2026-05-15', 190, 184.80, 210000, 3000, 2600, 1.15, 1.32, 'mid', 'sin catalizador fuerte', 'lateral', 'solo si rompe 186 con volumen', '183.50', '188', '190'),
    FlowAlert('RBLX', 'PUT', 'RBLX 2026-05-15 P 60', '2026-05-15', 60, 63.20, 355000, 4400, 500, 1.70, 1.88, 'ask', 'debilidad growth / ruptura soporte', 'bajista', 'perder 62.80 con volumen', '64.20', '61', '60'),
]


def score_alert(a: FlowAlert, market_mode: str, max_spread: float, min_vol_oi: float, min_premium: float) -> Dict[str, Any]:
    vol_oi = a.vol_oi
    spread = a.spread_pct

    pts_catalyst = 2.5 if a.catalyst and 'sin catalizador' not in a.catalyst.lower() else 0.5
    pts_flow = 0.0
    if a.premium >= 1_000_000:
        pts_flow = 2.5
    elif a.premium >= 500_000:
        pts_flow = 2.0
    elif a.premium >= 300_000:
        pts_flow = 1.5
    elif a.premium >= 150_000:
        pts_flow = 0.8

    pts_voloi = 1.5 if vol_oi >= 8 else 1.0 if vol_oi >= 5 else 0.5 if vol_oi >= 2 else 0
    pts_execution = 1.5 if a.execution == 'above_ask' else 1.0 if a.execution == 'ask' else 0.3
    pts_momentum = 2.0 if ('alcista' in a.momentum.lower() and a.direction == 'CALL') or ('bajista' in a.momentum.lower() and a.direction == 'PUT') else 0.6
    pts_spread = 1.0 if spread <= max_spread else 0.0

    raw = pts_catalyst + pts_flow + pts_voloi + pts_execution + pts_momentum + pts_spread

    # Ajuste dinámico simple por régimen del mercado.
    if market_mode == 'volatile':
        raw += 0.4 if a.execution in ('ask', 'above_ask') else -0.2
    elif market_mode == 'bearish' and a.direction == 'PUT':
        raw += 0.5
    elif market_mode == 'bullish' and a.direction == 'CALL':
        raw += 0.5

    score = round(min(raw, 10.0), 1)

    passes = (
        a.premium >= min_premium and
        vol_oi >= min_vol_oi and
        spread <= max_spread and
        a.execution in ('ask', 'above_ask') and
        score >= 7.0
    )

    if passes:
        action = 'VAMOS_CON_TODO'
        semaforo = 'VERDE'
    elif score >= 5.0:
        action = 'ESPERAR_CONFIRMACION'
        semaforo = 'AMARILLO'
    else:
        action = 'NO_OPERAR'
        semaforo = 'ROJO'

    d = asdict(a)
    d.update({
        'vol_oi': vol_oi,
        'spread_pct': spread,
        'score': score,
        'semaforo': semaforo,
        'action': action,
        'passes_filters': passes,
        'score_breakdown': {
            'catalyst': pts_catalyst,
            'premium_flow': pts_flow,
            'vol_oi': pts_voloi,
            'execution': pts_execution,
            'momentum': pts_momentum,
            'spread': pts_spread,
        }
    })
    return d


@app.route('/')
def home():
    return send_from_directory('.', 'index.html')


@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'service': 'FlowScan v3', 'time': datetime.now().isoformat(timespec='seconds')})


@app.route('/api/flow')
def api_flow():
    market_mode = request.args.get('market', 'neutral')
    max_spread = float(request.args.get('max_spread', 15))
    min_vol_oi = float(request.args.get('min_vol_oi', 5))
    min_premium = float(request.args.get('min_premium', 300000))

    ranked = [score_alert(x, market_mode, max_spread, min_vol_oi, min_premium) for x in DEMO_FLOW]
    ranked.sort(key=lambda x: x['score'], reverse=True)

    verdes = sum(1 for x in ranked if x['semaforo'] == 'VERDE')
    return jsonify({
        'source': 'demo_manual_ready_for_api',
        'market_mode': market_mode,
        'filters': {
            'max_spread': max_spread,
            'min_vol_oi': min_vol_oi,
            'min_premium': min_premium,
        },
        'summary': 'NO HAY NADA' if verdes == 0 else f'{verdes} setup(s) pasan filtros duros.',
        'items': ranked,
    })


@app.route('/api/manual', methods=['POST'])
def api_manual():
    payload = request.get_json(force=True, silent=True) or {}
    rows = payload.get('items', [])
    market_mode = payload.get('market', 'neutral')
    max_spread = float(payload.get('max_spread', 15))
    min_vol_oi = float(payload.get('min_vol_oi', 5))
    min_premium = float(payload.get('min_premium', 300000))

    alerts = []
    for r in rows:
        alerts.append(FlowAlert(
            ticker=str(r.get('ticker', '')).upper(),
            direction=str(r.get('direction', 'CALL')).upper(),
            contract=str(r.get('contract', '')),
            expiry=str(r.get('expiry', '')),
            strike=float(r.get('strike', 0)),
            spot=float(r.get('spot', 0)),
            premium=float(r.get('premium', 0)),
            volume=int(r.get('volume', 0)),
            open_interest=int(r.get('open_interest', 0)),
            bid=float(r.get('bid', 0)),
            ask=float(r.get('ask', 0)),
            execution=str(r.get('execution', 'mid')),
            catalyst=str(r.get('catalyst', '')),
            momentum=str(r.get('momentum', 'lateral')),
            vwap_trigger=str(r.get('vwap_trigger', '')),
            stop=str(r.get('stop', '')),
            target_1=str(r.get('target_1', '')),
            target_2=str(r.get('target_2', '')),
        ))

    ranked = [score_alert(x, market_mode, max_spread, min_vol_oi, min_premium) for x in alerts]
    ranked.sort(key=lambda x: x['score'], reverse=True)
    verdes = sum(1 for x in ranked if x['semaforo'] == 'VERDE')
    return jsonify({
        'source': 'manual_payload',
        'summary': 'NO HAY NADA' if verdes == 0 else f'{verdes} setup(s) pasan filtros duros.',
        'items': ranked,
    })


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5050, debug=True)
