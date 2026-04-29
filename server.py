from __future__ import annotations

import os
import json
import requests
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# ── CONFIG ───────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")

app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')
CORS(app)


# ── MODELO ───────────────────────────────────────────────────
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


# ── DATOS DEMO ───────────────────────────────────────────────
DEMO_FLOW: List[FlowAlert] = [
    FlowAlert('NVDA','CALL','NVDA 2026-05-15 C 950','2026-05-15',950,914.20,850000,8200,920,7.80,8.20,'above_ask','AI / semis momentum','alcista sobre VWAP','romper 918 con volumen','pierde VWAP 5m','930','950'),
    FlowAlert('AMD','CALL','AMD 2026-05-15 C 190','2026-05-15',190,181.50,410000,6500,700,2.10,2.28,'ask','semis sympathy + volumen fresco','alcista','romper 183.20 y sostener VWAP','181 bajo VWAP','187','190'),
    FlowAlert('TSLA','PUT','TSLA 2026-05-08 P 145','2026-05-08',145,151.10,620000,9800,1300,3.40,3.75,'above_ask','presion tecnica / debilidad','bajista bajo VWAP','rechazo en 152 y pierde 150.50','recupera 153','147','145'),
    FlowAlert('AAPL','CALL','AAPL 2026-05-15 C 190','2026-05-15',190,184.80,210000,3000,2600,1.15,1.32,'mid','sin catalizador fuerte','lateral','solo si rompe 186 con volumen','183.50','188','190'),
    FlowAlert('RBLX','PUT','RBLX 2026-05-15 P 60','2026-05-15',60,63.20,355000,4400,500,1.70,1.88,'ask','debilidad growth / ruptura soporte','bajista','perder 62.80 con volumen','64.20','61','60'),
]


# ── SCORING ──────────────────────────────────────────────────
def score_alert(a: FlowAlert, market_mode: str, max_spread: float, min_vol_oi: float, min_premium: float) -> Dict[str, Any]:
    vol_oi = a.vol_oi
    spread = a.spread_pct

    pts_catalyst  = 2.5 if a.catalyst and 'sin catalizador' not in a.catalyst.lower() else 0.5
    pts_flow      = 2.5 if a.premium >= 1_000_000 else 2.0 if a.premium >= 500_000 else 1.5 if a.premium >= 300_000 else 0.8 if a.premium >= 150_000 else 0.3
    pts_voloi     = 1.5 if vol_oi >= 8 else 1.0 if vol_oi >= 5 else 0.5 if vol_oi >= 2 else 0.0
    pts_execution = 1.5 if a.execution == 'above_ask' else 1.0 if a.execution == 'ask' else 0.2
    pts_momentum  = 2.0 if ('alcista' in a.momentum.lower() and a.direction == 'CALL') or ('bajista' in a.momentum.lower() and a.direction == 'PUT') else 0.6
    pts_spread    = 1.0 if spread <= max_spread else 0.0

    raw = pts_catalyst + pts_flow + pts_voloi + pts_execution + pts_momentum + pts_spread

    if market_mode == 'volatile':
        raw += 0.4 if a.execution in ('ask', 'above_ask') else -0.2
    elif market_mode == 'bearish' and a.direction == 'PUT':
        raw += 0.5
    elif market_mode == 'bullish' and a.direction == 'CALL':
        raw += 0.5

    score  = round(min(raw, 10.0), 1)
    passes = a.premium >= min_premium and vol_oi >= min_vol_oi and spread <= max_spread and a.execution in ('ask', 'above_ask') and score >= 7.0

    if passes:
        action, semaforo = 'VAMOS_CON_TODO', 'VERDE'
    elif score >= 5.0:
        action, semaforo = 'ESPERAR_CONFIRMACION', 'AMARILLO'
    else:
        action, semaforo = 'NO_OPERAR', 'ROJO'

    d = asdict(a)
    d.update({
        'vol_oi': vol_oi, 'spread_pct': spread,
        'score': score, 'semaforo': semaforo, 'action': action, 'passes_filters': passes,
        'pts_catalyst': pts_catalyst, 'pts_flow': pts_flow,
        'pts_momentum': pts_momentum, 'pts_sector': 0.8, 'pts_short': 0.2,
        'score_breakdown': f'Cat {pts_catalyst}+Flujo {pts_flow}+Mom {pts_momentum}+Ejec {pts_execution}+Spread {pts_spread}={score}',
    })
    return d


# ── POLYGON ──────────────────────────────────────────────────
def fetch_polygon_flow(ticker: str) -> List[FlowAlert]:
    if not POLYGON_API_KEY:
        return []
    try:
        r = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker}",
            params={"apiKey": POLYGON_API_KEY, "limit": 50, "order": "desc", "sort": "volume"},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Polygon error {ticker}: {e}")
        return []

    alerts = []
    for item in data.get("results", []):
        details = item.get("details", {})
        day     = item.get("day", {})
        last    = item.get("last_quote", {})
        volume  = int(day.get("volume", 0))
        oi      = int(item.get("open_interest", 0))
        if volume == 0 or oi == 0 or volume / oi < 1.5:
            continue
        contract_type = details.get("contract_type", "call").upper()
        strike  = float(details.get("strike_price", 0))
        expiry  = details.get("expiration_date", "")
        bid     = float(last.get("bid", 0))
        ask     = float(last.get("ask", 0))
        mid     = (bid + ask) / 2
        premium = round(mid * volume * 100, 2)
        last_p  = float(day.get("close", mid))
        execution = "above_ask" if last_p >= ask else "ask" if last_p >= ask * 0.98 else "mid"
        alerts.append(FlowAlert(
            ticker=ticker, direction=contract_type,
            contract=f"{ticker} {expiry} {contract_type[0]} {strike}",
            expiry=expiry, strike=strike,
            spot=float(item.get("underlying_asset", {}).get("price", 0)),
            premium=premium, volume=volume, open_interest=oi,
            bid=bid, ask=ask, execution=execution,
            catalyst="flujo inusual via Polygon",
            momentum="alcista" if contract_type == "CALL" else "bajista",
            vwap_trigger=f"romper strike {strike} con volumen",
            stop="", target_1=str(round(strike * 1.03, 2)), target_2=str(round(strike * 1.06, 2)),
        ))
    return alerts


def fetch_polygon_snapshot(ticker: str) -> Optional[FlowAlert]:
    if not POLYGON_API_KEY:
        return None
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_API_KEY},
            timeout=10
        )
        r.raise_for_status()
        td  = r.json().get("ticker", {})
        day = td.get("day", {})
        pv  = td.get("prevDay", {})
        close     = float(day.get("c", 0))
        vol       = int(day.get("v", 0))
        pvol      = int(pv.get("v", 1))
        change    = float(td.get("todaysChangePerc", 0))
        momentum  = "alcista" if change > 1 else "bajista" if change < -1 else "lateral"
        return FlowAlert(
            ticker=ticker, direction="CALL" if change >= 0 else "PUT",
            contract=f"{ticker} snapshot", expiry="N/A",
            strike=round(close * 1.05, 2), spot=close,
            premium=vol * close * 0.01, volume=vol, open_interest=max(pvol, 1),
            bid=round(close * 0.99, 2), ask=round(close * 1.01, 2),
            execution="mid", catalyst=f"Cambio del dia: {change:+.2f}%",
            momentum=momentum, vwap_trigger=f"precio actual ${close}",
            stop=str(round(close * 0.95, 2)),
            target_1=str(round(close * 1.05, 2)),
            target_2=str(round(close * 1.10, 2)),
        )
    except Exception as e:
        print(f"Snapshot error {ticker}: {e}")
        return None


# ── RUTAS ────────────────────────────────────────────────────
@app.route('/')
def home():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/api/health')
def health():
    return jsonify({
        'ok': True,
        'service': 'FlowScan 8',
        'version': '2.0',
        'time': datetime.now().isoformat(timespec='seconds'),
        'polygon_key': 'configurada' if POLYGON_API_KEY else 'FALTA',
    })


@app.route('/api/flow')
def api_flow():
    market_mode = request.args.get('market', 'neutral')
    max_spread  = float(request.args.get('max_spread', 15))
    min_vol_oi  = float(request.args.get('min_vol_oi', 2))
    min_premium = float(request.args.get('min_premium', 100_000))
    tickers_raw = request.args.get('tickers', '')

    if POLYGON_API_KEY and tickers_raw:
        tickers   = [t.strip().upper() for t in tickers_raw.split(',') if t.strip()]
        flow_data = []
        for tk in tickers:
            found = fetch_polygon_flow(tk)
            if found:
                flow_data.extend(found)
            else:
                snap = fetch_polygon_snapshot(tk)
                if snap:
                    flow_data.append(snap)
        source = 'polygon_realtime'
    else:
        flow_data = DEMO_FLOW
        source    = 'demo'

    if not flow_data:
        return jsonify({'source': source, 'summary': 'Sin flujo inusual detectado hoy.', 'items': []})

    ranked = [score_alert(x, market_mode, max_spread, min_vol_oi, min_premium) for x in flow_data]
    ranked.sort(key=lambda x: x['score'], reverse=True)
    verdes = sum(1 for x in ranked if x['semaforo'] == 'VERDE')

    return jsonify({
        'source': source,
        'market_mode': market_mode,
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'summary': f'{verdes} setup(s) con señal verde.' if verdes else 'Sin setups claros hoy.',
        'items': ranked,
    })


@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    payload     = request.get_json(force=True, silent=True) or {}
    tickers_raw = payload.get('tickers', '')
    market      = payload.get('market', 'neutral')
    min_vol_oi  = float(payload.get('min_vol_oi', 1.0))
    min_premium = float(payload.get('min_premium', 50_000))
    max_spread  = float(payload.get('max_spread', 20))

    if not tickers_raw:
        return jsonify({'error': 'Falta el campo tickers'}), 400

    tickers = [t.strip().upper() for t in str(tickers_raw).split(',') if t.strip()]
    all_alerts: List[FlowAlert] = []
    for tk in tickers:
        found = fetch_polygon_flow(tk)
        if found:
            all_alerts.extend(found)
        else:
            snap = fetch_polygon_snapshot(tk)
            if snap:
                all_alerts.append(snap)

    if not all_alerts:
        return jsonify({'source': 'polygon_realtime', 'summary': 'Sin datos disponibles.', 'items': []})

    ranked = [score_alert(x, market, max_spread, min_vol_oi, min_premium) for x in all_alerts]
    ranked.sort(key=lambda x: x['score'], reverse=True)
    verdes = sum(1 for x in ranked if x['semaforo'] == 'VERDE')

    return jsonify({
        'source': 'polygon_realtime',
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'summary': f'{verdes} setup(s) con señal verde.' if verdes else 'Sin setups claros.',
        'items': ranked,
    })


@app.route('/api/ticker/<ticker>')
def api_ticker(ticker: str):
    if not POLYGON_API_KEY:
        return jsonify({'error': 'POLYGON_API_KEY no configurada'}), 503
    market     = request.args.get('market', 'neutral')
    min_vol_oi = float(request.args.get('min_vol_oi', 1.0))
    min_premium= float(request.args.get('min_premium', 50_000))
    alerts     = fetch_polygon_flow(ticker.upper())
    if not alerts:
        snap = fetch_polygon_snapshot(ticker.upper())
        if snap:
            alerts = [snap]
    if not alerts:
        return jsonify({'ticker': ticker.upper(), 'message': 'Sin datos hoy.', 'items': []})
    ranked = [score_alert(x, market, 20, min_vol_oi, min_premium) for x in alerts]
    ranked.sort(key=lambda x: x['score'], reverse=True)
    return jsonify({'ticker': ticker.upper(), 'source': 'polygon_realtime', 'timestamp': datetime.now().isoformat(timespec='seconds'), 'items': ranked})


# ── ARRANQUE ─────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
