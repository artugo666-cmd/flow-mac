from __future__ import annotations

import os
import re
import json
import time
import threading
import statistics
import requests
from requests.adapters import HTTPAdapter
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — Python sin zoneinfo
    NY_TZ = timezone(timedelta(hours=-5))

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

# Presupuesto del scan: gunicorn mata al worker a los 30s por default.
# Si en Render subes --timeout, puedes subir FLOWSCAN_SCAN_BUDGET.
SCAN_BUDGET_S = float(os.environ.get("FLOWSCAN_SCAN_BUDGET", "24"))
SCAN_WORKERS  = int(os.environ.get("FLOWSCAN_WORKERS", "10"))
ENRICH_TOP    = int(os.environ.get("FLOWSCAN_ENRICH_TOP", "15"))
SCAN_CACHE_S  = int(os.environ.get("FLOWSCAN_CACHE_S", "45"))


# ══════════════════════════════════════════════════════════════
# SESIÓN YAHOO (cookie + crumb)
# Desde 2023 Yahoo exige cookie + crumb en v7/options y v10/quoteSummary.
# Sin esto regresan 401 en silencio → opciones, short float, earnings
# y analistas quedaban en cero y el score corría a medias.
# ══════════════════════════════════════════════════════════════
# curl_cffi imita la huella TLS de Chrome. Yahoo responde 429 a getcrumb cuando
# detecta la huella de `requests` desde IPs de nube (Render) — es el mismo bloqueo
# que obligó a yfinance a migrar a curl_cffi en 2025.
try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except Exception:  # pragma: no cover
    cffi_requests = None
    HAS_CFFI = False


class _YahooSession:
    """Una sesión HTTP por hilo (curl_cffi no comparte handles entre hilos con
    seguridad) que comparten la misma cookie A3 + crumb."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.local = threading.local()
        self.cookies: Dict[str, str] = {}
        self.gen = 0
        self.crumb: Optional[str] = None
        self.crumb_ts = 0.0
        self.attempt_ts = 0.0
        self.last_error: Optional[str] = None
        self.backend = "curl_cffi" if HAS_CFFI else "requests"

    def _new_session(self, with_cookies: bool = True):
        if HAS_CFFI:
            s = cffi_requests.Session(impersonate="chrome")
        else:
            s = requests.Session()
            s.headers.update(HEADERS)
            s.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=20))
        if with_cookies:
            for k, v in self.cookies.items():
                s.cookies.set(k, v, domain=".yahoo.com")
        return s

    def _session(self):
        s = getattr(self.local, "s", None)
        if s is None or getattr(self.local, "gen", -1) != self.gen:
            s = self._new_session()
            self.local.s, self.local.gen = s, self.gen
        return s

    @staticmethod
    def _cookie_dict(s) -> Dict[str, str]:
        try:
            return dict(s.cookies.get_dict())
        except Exception:
            try:
                return {k: v for k, v in s.cookies.items()}
            except Exception:
                return {}

    def _refresh(self) -> None:
        self.attempt_ts = time.time()
        try:
            s = self._new_session(with_cookies=False)
            try:
                # fc.yahoo.com responde 404 pero deja la cookie A3 — es lo esperado
                s.get("https://fc.yahoo.com", timeout=8)
            except Exception:
                pass
            r = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8)
            if r.status_code != 200:   # a veces bloquean query1 y query2 no
                r = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=8)
            txt = (r.text or "").strip()
            if r.status_code == 200 and txt and "<" not in txt and len(txt) < 40:
                self.cookies = self._cookie_dict(s)
                self.gen += 1
                self.crumb, self.crumb_ts, self.last_error = txt, time.time(), None
            else:
                self.crumb, self.last_error = None, f"getcrumb HTTP {r.status_code} ({self.backend})"
        except Exception as e:
            self.crumb, self.last_error = None, f"{e} ({self.backend})"
        print(f"Yahoo crumb [{self.backend}]: {'OK' if self.crumb else 'FALLÓ — ' + str(self.last_error)}")

    def get_crumb(self, force: bool = False) -> Optional[str]:
        with self.lock:
            expired = (not self.crumb) or (time.time() - self.crumb_ts > 6 * 3600)
            # No martillar a Yahoo: máximo un intento cada 30s
            if (force or expired) and time.time() - self.attempt_ts > 30:
                self._refresh()
            return self.crumb

    def get(self, url: str, params: Dict = None, timeout: float = 10, needs_crumb: bool = False):
        params = dict(params or {})
        if needs_crumb:
            c = self.get_crumb()
            if c:
                params["crumb"] = c
        r = self._session().get(url, params=params, timeout=timeout)
        if needs_crumb and r.status_code in (401, 403):
            c = self.get_crumb(force=True)
            if c:
                params["crumb"] = c
                r = self._session().get(url, params=params, timeout=timeout)
        return r


YF = _YahooSession()


def yget(url: str, params: Dict = None, timeout: float = 10, needs_crumb: bool = False):
    return YF.get(url, params=params, timeout=timeout, needs_crumb=needs_crumb)


# ══════════════════════════════════════════════════════════════
# RELOJ DE MERCADO
# ══════════════════════════════════════════════════════════════
SESSION_MIN = 390  # 9:30–16:00 ET

# Curva genérica de volumen acumulado (fracción del día) por minuto desde la apertura.
# Perfil en U del mercado US: mucho volumen en la apertura y en el cierre.
# Solo se usa como respaldo cuando el ticker no tiene historial intradía propio.
_VOL_CURVE = [(0, 0.0), (15, 0.09), (30, 0.15), (60, 0.25), (90, 0.32), (120, 0.39),
              (180, 0.51), (240, 0.62), (300, 0.74), (330, 0.81), (360, 0.89), (390, 1.0)]


def generic_vol_fraction(minutes: float) -> float:
    m = max(0.0, min(float(SESSION_MIN), float(minutes)))
    for (m0, f0), (m1, f1) in zip(_VOL_CURVE, _VOL_CURVE[1:]):
        if m <= m1:
            return f0 + (f1 - f0) * (m - m0) / (m1 - m0)
    return 1.0


def session_minutes_now() -> Tuple[bool, float]:
    """(mercado_abierto, minutos transcurridos de sesión). Sin feriados — eso lo cubre el dato stale."""
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return False, float(SESSION_MIN)
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    mins = (now - open_t).total_seconds() / 60
    if mins < 0 or mins >= SESSION_MIN:
        return False, float(SESSION_MIN)
    return True, max(mins, 1.0)

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

def _parse_bars(res: Dict) -> List[Dict[str, Any]]:
    """Velas alineadas por timestamp. Antes se filtraban los None de cada serie por
    separado y luego se hacía zip → highs/lows/closes/vols quedaban desfasados y el
    VWAP salía mal en cuanto había un minuto sin volumen."""
    ts = res.get("timestamp") or []
    q  = (res.get("indicators", {}).get("quote") or [{}])[0]
    o, h, l, c, v = (q.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
    out = []
    for i, t in enumerate(ts):
        try:
            hi, lo, cl = h[i], l[i], c[i]
            op = o[i] if i < len(o) else None
            vo = v[i] if i < len(v) else 0
        except IndexError:
            continue
        if hi is None or lo is None or cl is None:
            continue
        out.append({"t": int(t), "o": float(op if op is not None else cl),
                     "h": float(hi), "l": float(lo), "c": float(cl), "v": int(vo or 0)})
    return out


def _wilder_rsi(closes: List[float], n: int = 14) -> float:
    """RSI de Wilder (el mismo que IBKR/TradingView). El anterior era promedio simple."""
    if len(closes) < n + 1:
        return 50.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = sum(max(d, 0) for d in diffs[:n]) / n
    al = sum(max(-d, 0) for d in diffs[:n]) / n
    for d in diffs[n:]:
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def get_price_data(ticker: str) -> Dict[str, Any]:
    """
    Precio, VWAP, rango de apertura, RVOL por hora del día, MAs, RSI, ATR.

    Dos llamadas:
      1) 5m × 10d  → sesión actual (VWAP, opening range) + 9 sesiones previas
                     para saber cuánto volumen suele llevar el ticker A ESTA HORA.
      2) 1d × 1y   → cierre previo oficial, avg vol, MAs, RSI, ATR.

    RVOL: antes era vol_acumulado_hoy / vol_promedio_de_un_DÍA_COMPLETO.
    A las 10am un día explosivo marcaba 0.3x y a las 3pm 2x → el score subía
    solo por el reloj y el verde llegaba cuando el movimiento ya había pasado.
    Ahora se compara contra el volumen que el ticker suele llevar a la misma hora.
    """
    out: Dict[str, Any] = {}
    intraday: Dict[str, Any] = {}

    # ── 1) Intradía ────────────────────────────────────────────
    try:
        r1 = yget(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                  {"interval": "5m", "range": "10d"}, timeout=10)
        r1.raise_for_status()
        res  = (r1.json().get("chart", {}).get("result") or [{}])[0]
        meta = res.get("meta", {}) or {}
        bars = _parse_bars(res)
        off  = int(meta.get("gmtoffset") or -18000)

        days: "OrderedDict[str, List[Dict]]" = OrderedDict()
        for b in bars:
            d = datetime.utcfromtimestamp(b["t"] + off).strftime("%Y-%m-%d")
            days.setdefault(d, []).append(b)

        reg   = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        start = int(reg.get("start") or 0)
        end   = int(reg.get("end") or 0)
        now   = time.time()

        today_bars = [b for b in bars if start and start <= b["t"] < end]
        if today_bars:
            sess = today_bars
            sess_day = datetime.utcfromtimestamp(sess[0]["t"] + off).strftime("%Y-%m-%d")
        elif days:
            sess_day = list(days.keys())[-1]
            sess = days[sess_day]
        else:
            sess_day, sess = None, []

        market_open = bool(today_bars) and start <= now < end
        last_t   = sess[-1]["t"] if sess else int(meta.get("regularMarketTime") or 0)
        age_h    = (now - last_t) / 3600 if last_t else 99.0
        if market_open and age_h > 0.5:      # "abierto" pero sin velas recientes → halt/feriado
            market_open = False

        sess_start = sess[0]["t"] if sess else 0
        if market_open:
            elapsed = min(float(SESSION_MIN), max((last_t + 300 - sess_start) / 60, 5.0))
        else:
            elapsed = float(SESSION_MIN)

        price = float(meta.get("regularMarketPrice") or (sess[-1]["c"] if sess else 0) or 0)
        vol_sess = sum(b["v"] for b in sess)

        # VWAP de la sesión
        pv = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in sess)
        vwap = round(pv / vol_sess, 4) if vol_sess > 0 else price

        # Opening range (primeros 15 min = 3 velas de 5m)
        or_bars = sess[:3] if len(sess) >= 3 else []
        or_high = round(max(b["h"] for b in or_bars), 4) if or_bars else None
        or_low  = round(min(b["l"] for b in or_bars), 4) if or_bars else None

        day_hi = max((b["h"] for b in sess), default=price)
        day_lo = min((b["l"] for b in sess), default=price)
        if today_bars:  # para el día en curso, Yahoo trae hi/lo consolidados
            day_hi = max(day_hi, float(meta.get("regularMarketDayHigh") or 0))
            mlo = float(meta.get("regularMarketDayLow") or 0)
            day_lo = min(day_lo, mlo) if mlo > 0 else day_lo
        day_op = sess[0]["o"] if sess else price

        # Volumen típico de este ticker a esta misma hora (mediana de sesiones previas completas)
        prior_cum = []
        for d, dbars in days.items():
            if d == sess_day or len(dbars) < 70:   # excluir sesión actual y medios días
                continue
            d0 = dbars[0]["t"]
            prior_cum.append(sum(b["v"] for b in dbars if b["t"] < d0 + elapsed * 60))
        prior_cum = prior_cum[-9:]

        intraday = {
            "price": price, "market_open": market_open, "elapsed": elapsed,
            "vol_sess": vol_sess, "prior_cum": prior_cum, "sess_day": sess_day,
        }
        out.update({
            "price": round(price, 2),
            "open": round(day_op, 2), "high": round(day_hi, 2), "low": round(day_lo, 2),
            "vwap": round(vwap, 2),
            "precio_vs_vwap": round((price - vwap) / vwap * 100, 2) if vwap else 0,
            "volume": vol_sess,
            "day_range_pos": round((price - day_lo) / (day_hi - day_lo), 2) if day_hi > day_lo else 0.5,
            "or_high": round(or_high, 2) if or_high else None,
            "or_low":  round(or_low, 2) if or_low else None,
            "market_open": market_open,
            "session_minutes": round(elapsed, 0),
            # stale = dato de hace >20h (fin de semana / feriado). Fuera de horario
            # normal NO es stale: es la última sesión y sirve para planear la apertura.
            "data_is_stale": age_h > 20,
            "data_age_hours": round(age_h, 1),
        })
    except Exception as e:
        print(f"Price data error {ticker}: {e}")

    # ── 2) Diario 1 año ────────────────────────────────────────
    try:
        r2 = yget(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                  {"interval": "1d", "range": "1y"}, timeout=12)
        r2.raise_for_status()
        res2  = (r2.json().get("chart", {}).get("result") or [{}])[0]
        meta2 = res2.get("meta", {}) or {}
        dbars = _parse_bars(res2)
        off2  = int(meta2.get("gmtoffset") or -18000)

        price = out.get("price") or float(meta2.get("regularMarketPrice") or 0)
        sess_day = intraday.get("sess_day")
        if sess_day is None:   # sin intradía: asumir que la última vela diaria es la sesión actual
            last_is_sess = bool(dbars)
        else:
            last_is_sess = bool(dbars) and \
                datetime.utcfromtimestamp(dbars[-1]["t"] + off2).strftime("%Y-%m-%d") == sess_day
        done = dbars[:-1] if last_is_sess else dbars          # sesiones completas
        closes_all = [b["c"] for b in dbars]
        if dbars and not last_is_sess and price:
            closes_all = closes_all + [price]                  # sesión actual aún sin vela diaria

        prev = done[-1]["c"] if done else float(meta2.get("chartPreviousClose") or 0)
        if not out.get("price"):
            out["price"] = round(price, 2)

        chg_abs = round(price - prev, 2) if prev and price else 0.0
        chg_pct = round(chg_abs / prev * 100, 2) if prev else 0.0

        vols = [b["v"] for b in done if b["v"] > 0]
        avg_vol = int(sum(vols[-20:]) / len(vols[-20:])) if vols else 0

        # ── RVOL por hora del día ──
        vol_sess = intraday.get("vol_sess", out.get("volume", 0)) or 0
        elapsed  = intraday.get("elapsed", float(SESSION_MIN))
        prior    = [x for x in intraday.get("prior_cum", []) if x > 0]
        rel_raw  = round(vol_sess / avg_vol, 2) if avg_vol else 1.0
        if intraday.get("market_open") and len(prior) >= 3:
            expected, method = statistics.median(prior), "perfil_propio"
        elif intraday.get("market_open") and avg_vol:
            expected, method = avg_vol * generic_vol_fraction(elapsed), "curva_generica"
        else:
            expected, method = avg_vol, "dia_completo"
        rel_vol = round(vol_sess / expected, 2) if expected else 1.0

        def sma(n):
            return round(sum(closes_all[-n:]) / n, 2) if len(closes_all) >= n else None

        def pct_vs_ma(ma):
            if ma and price:
                d = round((price - ma) / ma * 100, 2)
                return f"+{d:.2f}%" if d >= 0 else f"{d:.2f}%"
            return "N/A"

        sma20, sma50, sma200 = sma(20), sma(50), sma(200)

        # ATR 14 sobre sesiones completas (la vela de hoy aún no tiene su rango final)
        atr = 0.0
        if len(done) >= 15:
            trs = [max(done[i]["h"] - done[i]["l"],
                       abs(done[i]["h"] - done[i - 1]["c"]),
                       abs(done[i]["l"] - done[i - 1]["c"])) for i in range(len(done) - 14, len(done))]
            atr = round(sum(trs) / len(trs), 2)

        def perf(days_back):
            if len(closes_all) > days_back:
                base = closes_all[-days_back - 1]
                p = (price - base) / base * 100 if base else 0
                return f"+{p:.2f}%" if p >= 0 else f"{p:.2f}%"
            return "N/A"

        out.update({
            "prev_close": round(prev, 2),
            "change_pct": chg_pct, "change_abs": chg_abs,
            "avg_volume": avg_vol,
            "rel_volume": rel_vol, "rel_volume_raw": rel_raw, "rvol_method": method,
            "sma20": sma20, "sma50": sma50, "sma200": sma200,
            "above_sma20": bool(sma20 and price > sma20),
            "above_sma50": bool(sma50 and price > sma50),
            "above_sma200": bool(sma200 and price > sma200),
            "sma20_pct": pct_vs_ma(sma20), "sma50_pct": pct_vs_ma(sma50), "sma200_pct": pct_vs_ma(sma200),
            "rsi": _wilder_rsi(closes_all), "atr": atr,
            "perf_week": perf(5), "perf_month": perf(21),
        })
    except Exception as e:
        print(f"Historical data error {ticker}: {e}")

    return out


_FV_SECTORS = {
    "technology": "Technology", "healthcare": "Healthcare", "financial": "Financial Services",
    "consumercyclical": "Consumer Cyclical", "consumerdefensive": "Consumer Defensive",
    "industrials": "Industrials", "energy": "Energy", "basicmaterials": "Basic Materials",
    "realestate": "Real Estate", "utilities": "Utilities",
    "communicationservices": "Communication Services",
}


def _fundamentals_finviz(ticker: str) -> Dict[str, Any]:
    """Respaldo cuando Yahoo bloquea quoteSummary: tabla snapshot de Finviz."""
    out: Dict[str, Any] = {}
    r = YF._session().get("https://finviz.com/quote.ashx", params={"t": ticker, "p": "d"}, timeout=10)
    if r.status_code != 200:
        print(f"Finviz {ticker}: HTTP {r.status_code}")
        return out
    html = r.text or ""
    if "snapshot-table2" not in html and "Short Float" not in html:
        return out

    t0 = html.find("snapshot-table2")
    snap = html[t0:] if t0 >= 0 else html

    def field(label: str) -> str:
        """Valor de la celda que sigue a la etiqueta, sin depender de clases CSS."""
        m = re.search(r">\s*" + re.escape(label) + r"\s*<", snap)
        if not m:
            return ""
        j = snap.find("<td", m.end())
        if j < 0:
            return ""
        k = snap.find("</td>", j)
        cell = snap[snap.find(">", j) + 1: k]
        return re.sub(r"<[^>]+>", "", cell).replace("&nbsp;", " ").strip()

    labels = ("Short Float", "Beta", "Target Price", "Recom", "52W Range", "Inst Own", "Earnings")
    kv = {lb: field(lb) for lb in labels}
    out["fv_fields"] = sum(1 for v in kv.values() if v and v != "-")

    def num(key, pct=False):
        v = kv.get(key, "").replace("%", "").replace(",", "").strip()
        try:
            return float(v)
        except ValueError:
            return None

    out["short_pct"] = round(num("Short Float") or 0.0, 1)
    out["beta"] = round(num("Beta") or 0.0, 2)
    tp = num("Target Price")
    out["target_price"] = round(tp, 2) if tp else None
    rec = num("Recom")
    if rec:
        out["recommendation"] = ("Strong Buy" if rec <= 1.5 else "Buy" if rec <= 2.5 else
                                 "Hold" if rec <= 3.5 else "Sell" if rec <= 4.5 else "Strong Sell")
    else:
        out["recommendation"] = "N/A"
    rng = kv.get("52W Range", "")
    if " - " in rng:
        try:
            lo52, hi52 = (float(x.replace(",", "")) for x in rng.split(" - ")[:2])
            out["low_52w"], out["high_52w"] = round(lo52, 2), round(hi52, 2)
        except ValueError:
            pass
    io = kv.get("Inst Own")
    out["inst_own"] = io if io and io != "-" else "N/A"
    m = re.search(r"f=sec_([a-z]+)", html)
    out["sector"] = _FV_SECTORS.get(m.group(1), m.group(1).title()) if m else "N/A"
    out["industry"] = "N/A"

    # Earnings: "Nov 19 AMC" (sin año)
    out["earnings_date"], out["earnings_ts"], out["earnings_days"] = "N/A", 0, None
    em = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})", kv.get("Earnings", ""))
    if em:
        now = datetime.now(NY_TZ)
        try:
            ed = datetime.strptime(f"{em.group(1)} {em.group(2)} {now.year}", "%b %d %Y").replace(tzinfo=NY_TZ)
            if (ed - now).days < -60:
                ed = ed.replace(year=now.year + 1)
            out["earnings_ts"] = ed.timestamp()
            out["earnings_date"] = ed.strftime("%d-%b-%Y")
            out["earnings_days"] = round((ed.timestamp() - now.timestamp()) / 86400, 0)
        except ValueError:
            pass
    out["analyst_action"] = None
    out["analyst_action_type"] = None
    out["fund_source"] = "finviz"
    return out


def get_fundamentals(ticker: str) -> Dict[str, Any]:
    """Fundamentales: Yahoo quoteSummary (requiere crumb) → respaldo Finviz."""
    out: Dict[str, Any] = {}
    if YF.get_crumb():
        out = _fundamentals_yahoo(ticker)
        if out.get("sector"):
            out["fund_source"] = "yahoo"
            return out
    try:
        fv = _fundamentals_finviz(ticker)
        if fv:
            return fv
    except Exception as e:
        print(f"Finviz error {ticker}: {e}")
    return out


def _fundamentals_yahoo(ticker: str) -> Dict[str, Any]:
    """Fundamentales, short float, earnings, sector — Yahoo quoteSummary."""
    out = {}
    try:
        r = yget(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
            {"modules": "summaryDetail,defaultKeyStatistics,financialData,assetProfile,calendarEvents,upgradeDowngradeHistory"},
            timeout=12, needs_crumb=True
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
        r = yget(
            "https://query2.finance.yahoo.com/v1/finance/search",
            {"q": ticker, "newsCount": 8, "quotesCount": 0}, timeout=8
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

        out.update({"score": best_score, "title": best_title, "keywords": best_kws, "age_hours": best_age,
                    "fetched": len(r.json().get("news", []))})
    except Exception as e:
        print(f"News error {ticker}: {e}")
    return out


def _chain_yahoo(ticker: str) -> Tuple[float, List[Tuple[List[Dict], List[Dict]]]]:
    """Cadena de Yahoo (requiere crumb). Regresa (precio, [(calls, puts)] de las 3 expiraciones más cercanas)."""
    if not YF.get_crumb():      # sin crumb es 401 seguro: no gastar el request
        return 0.0, []
    r = yget(f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}", timeout=12, needs_crumb=True)
    if r.status_code != 200:
        return 0.0, []
    chain = r.json().get("optionChain", {}).get("result", [])
    if not chain:
        return 0.0, []
    price = float(chain[0].get("quote", {}).get("regularMarketPrice") or 0)
    dates = chain[0].get("expirationDates", [])
    exps = []
    for i, exp_ts in enumerate(dates[:3]):
        if i == 0:
            res2 = chain   # la primera respuesta ya trae la expiración más cercana
        else:
            r2 = yget(f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}",
                      {"date": exp_ts}, timeout=12, needs_crumb=True)
            if r2.status_code != 200:
                break
            res2 = r2.json().get("optionChain", {}).get("result", [])
        if not res2:
            continue
        opt = (res2[0].get("options") or [{}])[0]
        exps.append((opt.get("calls", []), opt.get("puts", [])))
    return price, exps


_OCC_RE = re.compile(r"^(.*)(\d{6})([CP])(\d{8})$")


def _chain_cboe(ticker: str) -> Tuple[float, List[Tuple[List[Dict], List[Dict]]]]:
    """Respaldo: cadena retrasada (~15 min) del CDN público de CBOE. Sin crumb ni API key.
    Cada contrato viene como símbolo OCC (NVDA251121C00200000) con volume y open_interest."""
    r = YF._session().get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json", timeout=12)
    if r.status_code != 200:
        return 0.0, []
    d = (r.json() or {}).get("data", {}) or {}
    price = float(d.get("current_price") or d.get("close") or 0)
    by_exp: Dict[str, Tuple[List[Dict], List[Dict]]] = {}
    for o in d.get("options", []) or []:
        m = _OCC_RE.match(str(o.get("option", "")))
        if not m or m.group(1) != ticker:          # ignora raíces ajustadas (NVDA1, etc.)
            continue
        exp, cp, strike = m.group(2), m.group(3), int(m.group(4)) / 1000.0
        rec = {"strike": strike, "volume": int(o.get("volume") or 0),
               "openInterest": int(o.get("open_interest") or 0), "gamma": o.get("gamma")}
        calls, puts = by_exp.setdefault(exp, ([], []))
        (calls if cp == "C" else puts).append(rec)
    today = datetime.now(NY_TZ).strftime("%y%m%d")
    keys = sorted(k for k in by_exp if k >= today)[:3]
    return price, [by_exp[k] for k in keys]


def _fetch_chain(ticker: str) -> Tuple[float, List[Tuple[List[Dict], List[Dict]]], str]:
    try:
        price, exps = _chain_yahoo(ticker)
        if price and exps:
            return price, exps, "yahoo"
    except Exception as e:
        print(f"Yahoo options {ticker}: {e}")
    try:
        price, exps = _chain_cboe(ticker)
        if price and exps:
            return price, exps, "cboe"
    except Exception as e:
        print(f"CBOE options {ticker}: {e}")
    return 0.0, [], "none"


def _voi_ok(strike: float, vol: int, oi: int, spot: float) -> bool:
    """Un strike cuenta para vol/OI solo si es relevante: a ±15% del precio, con
    volumen ≥200 y OI ≥100. Antes bastaba OI≥50 y cualquier volumen: un contrato
    lotería lejano con OI 50 y 2,500 de volumen daba 50x → NVDA siempre 'sweep'."""
    if not spot or vol < 200 or oi < 100:
        return False
    return abs(strike - spot) / spot <= 0.15


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
        "top_strikes": [], "strikes": [], "source_ok": False,
        # Gamma squeeze
        "gamma_score": 0, "gamma_signal": "NONE", "gamma_triggers": [],
        "dealer_pressure": "NEUTRAL",
    }

    try:
        current_price, expirations, source = _fetch_chain(ticker)
        if not expirations or not current_price:
            return out
        out["source_ok"] = True
        out["has_options"] = True
        out["options_source"] = source

        # Acumular datos de las 3 expiraciones más cercanas
        best_call_voi = 0.0; best_put_voi = 0.0
        total_cv = 0; total_pv = 0; total_coi = 0; total_poi = 0
        atm_cv = 0.0; atm_pv = 0.0
        call_oi_strike: Dict[float, Dict] = {}
        put_oi_strike:  Dict[float, Dict] = {}
        top_strikes = []

        atm_lo = current_price * 0.95
        atm_hi = current_price * 1.05

        c0, p0 = expirations[0]
        first_strikes: List[float] = sorted({float(c.get("strike") or 0) for c in c0 + p0 if c.get("strike")})
        for calls, puts in expirations:
            for c in calls:
                vol = int(c.get("volume", 0) or 0)
                oi  = int(c.get("openInterest", 0) or 0)
                sk  = float(c.get("strike", 0) or 0)
                total_cv  += vol; total_coi += oi
                if sk > 0:
                    prev = call_oi_strike.get(sk, {"oi": 0, "vol": 0})
                    call_oi_strike[sk] = {"oi": prev["oi"]+oi, "vol": prev["vol"]+vol}
                if _voi_ok(sk, vol, oi, current_price):
                    ratio = min(vol / oi, 20.0)
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
                if _voi_ok(sk, vol, oi, current_price):
                    ratio = min(vol / oi, 20.0)
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
            "strikes": first_strikes,
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
    """
    SPY, QQQ, IWM, VIX — sentimiento del mercado.
    
    Problema conocido: Yahoo devuelve regularMarketChangePercent = 0
    cuando el mercado está cerrado (antes de abrir o después de cerrar).
    
    Solución: calcular el cambio manualmente con:
      price = regularMarketPrice   (precio del último trade del día)
      prev  = chartPreviousClose   (cierre del día anterior)
      chg   = (price - prev) / prev * 100
    
    Estos dos campos SIEMPRE tienen valores reales independientemente
    del horario de mercado.
    
    Adicionalmente: usar range=5d para tener contexto de varios días
    y detectar si el mercado está abierto o cerrado.
    """
    pulse = {}
    for sym in ["SPY", "QQQ", "IWM", "^VIX"]:
        try:
            r = yget(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                {"interval": "1d", "range": "5d"}, timeout=8
            )
            r.raise_for_status()
            result = r.json().get("chart", {}).get("result", [])
            if not result:
                continue
            meta = result[0].get("meta", {})
            q    = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes = [c for c in (q.get("close") or []) if c is not None]

            price = float(meta.get("regularMarketPrice") or 0)
            # Intentar cambio% directo primero
            chg_pct_direct = float(meta.get("regularMarketChangePercent") or 0)
            
            # Si viene en 0, calcularlo manualmente
            if chg_pct_direct == 0 and len(closes) >= 2:
                prev_close = closes[-2]   # penúltimo cierre = cierre de ayer
                last_close = closes[-1]   # último cierre del período
                if prev_close and prev_close > 0:
                    chg_pct_direct = round((last_close - prev_close) / prev_close * 100, 2)

            # También intentar con chartPreviousClose como fallback
            if chg_pct_direct == 0:
                prev = float(meta.get("chartPreviousClose") or 0)
                if prev > 0 and price > 0:
                    chg_pct_direct = round((price - prev) / prev * 100, 2)

            key = "VIX" if sym == "^VIX" else sym
            pulse[key] = {
                "price":      round(price, 2),
                "change_pct": chg_pct_direct,
            }
        except Exception as e:
            print(f"Market pulse error {sym}: {e}")

    spy = pulse.get("SPY", {}).get("change_pct", 0)
    qqq = pulse.get("QQQ", {}).get("change_pct", 0)
    iwm = pulse.get("IWM", {}).get("change_pct", 0)
    vix = pulse.get("VIX", {}).get("price", 20)
    avg = round((spy + qqq + iwm) / 3, 2)

    # Todos en cero = Yahoo no pudo devolver datos reales
    todos_cero = (spy == 0 and qqq == 0 and iwm == 0)

    if todos_cero:
        sentiment, emoji = "SIN_DATOS", "❓"
    elif vix >= 30:
        sentiment, emoji = "MUY_VOLATIL", "⚡"
    elif avg >= 0.5:
        sentiment, emoji = "ALCISTA", "📈"
    elif avg <= -0.5:
        sentiment, emoji = "BAJISTA", "📉"
    else:
        sentiment, emoji = "NEUTRAL", "➡️"

    pulse.update({
        "sentiment":  sentiment,
        "emoji":      emoji,
        "avg_change": avg,
        "vix_price":  vix,
        "vix_risk":   "ALTO" if vix >= 25 else "MODERADO" if vix >= 18 else "BAJO",
        "todos_cero": todos_cero,
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
        ("most_actives",             50),   # mayor volumen absoluto
        ("day_gainers",              50),   # mayores subidas del dia — AUMENTADO a 50
        ("day_losers",               40),   # mayores caidas del dia
        ("small_cap_gainers",        50),   # IPOs recientes y small caps — AUMENTADO a 50
        ("growth_technology_stocks", 30),   # tech en momentum — ASTS, CRCL, etc.
        ("aggressive_small_caps",    40),   # micro/small caps agresivas — captura GSIT-tipo
    ]

    # RVOL de los screeners viene como vol_hoy / promedio de un día completo.
    # Se normaliza con la curva genérica para no castigar al pre-filtro en la mañana.
    mkt_open, mins = session_minutes_now()
    tod_frac = generic_vol_fraction(mins) if mkt_open else 1.0

    def _fetch_screener(scr: Tuple[str, int]):
        scrId, count = scr
        r = yget(
            "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
            {"scrIds": scrId, "count": count}, timeout=10, needs_crumb=True
        )
        r.raise_for_status()
        return r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])

    # Las 6 consultas en paralelo; se procesan en el orden original (prioridad de fuente)
    with ThreadPoolExecutor(max_workers=len(SCREENERS)) as ex:
        futs = [(scr, ex.submit(_fetch_screener, scr)) for scr in SCREENERS]

    for (scrId, count), fut in futs:
        try:
            quotes = fut.result()

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
                rel_vol = round(vol / avg_vol / max(tod_frac, 0.05), 2) if avg_vol > 0 else 1.0

                # Filtros de calidad: precio >= $5, avg_vol >= 1M diario
                # IPO/mover exception: si vol HOY > 2M aunque avg_vol sea bajo
                # BUG FIX: usar 'vol' no 'vol_hoy' (nombre correcto en este scope)
                is_ipo = (avg_vol < 1_000_000 and vol > 2_000_000)
                # Mover extremo: subida >= 15% con volumen real hoy = siempre analizar
                is_extreme_mover = (abs(chg_pct) >= 15.0 and vol >= 500_000)
                if price < 2.0:  # Bajado de $5 a $2 para no perder small caps en movimiento
                    continue
                if avg_vol < 1_000_000 and not is_ipo and not is_extreme_mover:
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
                    "is_extreme_mover": is_extreme_mover,
                }

            print(f"discover [{scrId}]: {len(candidates)} candidatos acumulados")
        except Exception as e:
            print(f"discover [{scrId}] error: {e}")

    # Trending Yahoo (no trae precio — solo añadir si no está ya)
    try:
        r2 = yget(
            "https://query1.finance.yahoo.com/v1/finance/trending/US",
            {"count": 20}, timeout=10
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
        src  = c.get("source", "")
        chg  = abs(c.get("chg_pct", 0))
        rvol = c.get("rel_vol", 1.0)

        # Mover extremo (+15% o más) → SIEMPRE analizar sin importar fuente
        if c.get("is_extreme_mover", False):
            return True
        if chg >= 15.0:
            return True

        # Screeners de movers directos → siempre relevante
        if src in MOVER_SOURCES:
            return True

        # most_actives: criterio MÁS estricto
        # Problema real: CTRA tiene 7M vol todos los días (S&P500)
        # pero sin movimiento ni catalizador. Necesita chg >= 3% O rvol >= 3x
        if src == "most_actives":
            if chg >= 3.0: return True      # movimiento real hoy
            if rvol >= 3.0: return True     # vol muy inusual vs promedio
            if c.get("is_ipo", False): return True
            return False  # CTRA con chg=0.5% y rvol=1.2x → descartada

        # trending: solo si se mueve
        if src == "trending":
            if chg >= 2.0: return True
            if rvol >= 2.5: return True
            return False

        # growth_technology: umbral bajo (estas siempre son relevantes)
        if src == "growth_technology_stocks":
            if chg >= 1.0: return True
            if rvol >= 2.0: return True
            return False

        # Cualquier otra fuente
        if chg >= 2.0: return True
        if rvol >= 2.0: return True
        if c.get("is_ipo", False): return True
        return False

    filtered = [c for c in candidates.values() if is_relevant(c)]
    print(f"discover: {len(candidates)} candidatos → {len(filtered)} relevantes (filtro mov/vol aplicado)")

    # Ordenar: movers extremos primero, luego por |cambio%| x rel_vol
    # Garantiza que un GSIT +59% no quede enterrado por un NVDA con vol alto
    ranked = sorted(
        filtered,
        key=lambda x: (
            1 if x.get("is_extreme_mover") or abs(x.get("chg_pct", 0)) >= 15 else 0,
            abs(x.get("chg_pct", 0)) * max(x.get("rel_vol", 1.0), 1.0),
        ),
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

def _pick_strike(strikes: List[float], price: float, is_bull: bool) -> float:
    """Strike real de la cadena ~2% OTM. Sin cadena: incremento estándar según precio
    (antes redondeaba siempre a múltiplos de $5 → en una acción de $12 sugería el 15)."""
    target = price * (1.02 if is_bull else 0.98)
    if strikes:
        if is_bull:
            ups = [s for s in strikes if s >= target]
            return ups[0] if ups else strikes[-1]
        downs = [s for s in strikes if s <= target]
        return downs[-1] if downs else strikes[0]
    step = 0.5 if price < 5 else 1.0 if price < 50 else 2.5 if price < 200 else 5.0
    base = target / step
    n = int(base) + (1 if is_bull and base != int(base) else 0)
    return round(n * step, 2)


ENTRY_STATES = ("EN_ZONA", "ESPERA_PULLBACK", "ESPERA_RUPTURA", "EXTENDIDO",
                "INVALIDADO", "ESPERA_RANGO", "MERCADO_CERRADO")


def compute_entry_plan(p: Dict, direction: str, opts: Dict = None) -> Dict[str, Any]:
    """
    Zona de entrada anclada a estructura intradía, no al precio del momento.

    Antes: zona = precio×0.98–precio, stop −5%, targets +5/+10%. La zona perseguía
    al precio: a la hora que vieras la tarjeta, "estabas en zona".

    Ahora (CALL; PUT es el espejo):
      nivel  = el soporte más alto por debajo del precio entre VWAP y máximo del
               rango de apertura (15 min). Si el precio está bajo ambos, el nivel
               es recuperar VWAP.
      zona   = nivel ± banda (10% del ATR diario, mínimo 0.2% del precio)
      estado = EN_ZONA        precio a ≤0.35 ATR sobre el nivel → entrar
               ESPERA_PULLBACK entre 0.35 y 1.0 ATR → esperar regreso a la zona
               EXTENDIDO      >1.0 ATR sobre el nivel → no perseguir
               ESPERA_RUPTURA bajo VWAP → entrar solo si lo recupera
               INVALIDADO     perdió VWAP y rango de apertura por >0.25 ATR
      stop   = borde inferior de la zona − 0.25 ATR (estructural)
      T1/T2  = 1.5R / 3R desde la entrada de referencia
    """
    opts = opts or {}
    price = float(p.get("price") or 0)
    is_bull = direction == "CALL"
    sgn = 1 if is_bull else -1
    vwap = float(p.get("vwap") or price)
    hi, lo = float(p.get("high") or price), float(p.get("low") or price)
    atr_d = float(p.get("atr") or 0) or price * 0.03
    # Unidad de volatilidad del DÍA: en un día explosivo (GSIT +38%) el ATR previo
    # al movimiento es ruido de otra escala — todo saldría "extendido" y el stop
    # quedaría a 1%. Se usa el mayor entre ATR diario y 40% del rango de hoy.
    atr = max(atr_d, 0.40 * (hi - lo))
    band = max(0.10 * atr, price * 0.002)
    orh, orl = p.get("or_high"), p.get("or_low")
    live = bool(p.get("market_open"))
    mins = float(p.get("session_minutes") or 0)

    ext_atr = round(sgn * (price - vwap) / atr, 2) if atr else 0.0

    # Soportes (CALL) o resistencias (PUT) del lado favorable
    if is_bull:
        cands = [(vwap, "VWAP"), (orh, "máximo del rango de apertura")]
        favorable = [(lv, nm) for lv, nm in cands if lv and lv <= price]
        level, level_name = max(favorable, key=lambda x: x[0]) if favorable else (vwap, "VWAP")
    else:
        cands = [(vwap, "VWAP"), (orl, "mínimo del rango de apertura")]
        favorable = [(lv, nm) for lv, nm in cands if lv and lv >= price]
        level, level_name = min(favorable, key=lambda x: x[0]) if favorable else (vwap, "VWAP")

    dist_atr = sgn * (price - level) / atr if atr else 0.0     # >0 = a favor, ya separado del nivel

    if not favorable:
        # CALL bajo VWAP (o PUT sobre VWAP): no hay estructura a favor
        broken_ref = min(x for x in (vwap, orl) if x) if is_bull else max(x for x in (vwap, orh) if x)
        if sgn * (price - broken_ref) < -0.25 * atr:
            state = "INVALIDADO"
        else:
            state = "ESPERA_RUPTURA"
        zone_lo, zone_hi = (vwap, vwap + band) if is_bull else (vwap - band, vwap)
        entry_ref = zone_hi if is_bull else zone_lo
        stop = vwap - sgn * 0.35 * atr
        trigger = (f"Entrar solo si recupera VWAP ${vwap:.2f} y sostiene" if is_bull
                   else f"Entrar solo si pierde VWAP ${vwap:.2f} y no lo recupera")
    else:
        zone_lo, zone_hi = level - band, level + band
        stop = (zone_lo - 0.25 * atr) if is_bull else (zone_hi + 0.25 * atr)
        if dist_atr <= 0.35:
            state = "EN_ZONA"
            entry_ref = price
            trigger = f"En zona sobre {level_name} ${level:.2f}" if is_bull else f"En zona bajo {level_name} ${level:.2f}"
        elif dist_atr <= 1.0:
            state = "ESPERA_PULLBACK"
            entry_ref = level
            trigger = (f"Esperar retroceso a {level_name} ${zone_lo:.2f}–${zone_hi:.2f}" if is_bull
                       else f"Esperar rebote a {level_name} ${zone_lo:.2f}–${zone_hi:.2f}")
        else:
            state = "EXTENDIDO"
            entry_ref = level
            lado, mov = ("sobre", "retroceso") if is_bull else ("bajo", "rebote")
            trigger = (f"Extendido {dist_atr:.1f}× volatilidad {lado} {level_name} — no perseguir; "
                       f"solo en {mov} a ${zone_lo:.2f}–${zone_hi:.2f}")

    # Los primeros 15 min el rango de apertura no existe todavía
    if live and mins < 15:
        state = "ESPERA_RANGO"
        trigger = "Primeros 15 min — esperar que se forme el rango de apertura"
    if not live:
        state = "MERCADO_CERRADO"
        trigger = (f"Mercado cerrado. Niveles de la última sesión: VWAP ${vwap:.2f}, "
                   f"máx ${hi:.2f}, mín ${lo:.2f}. Re-evaluar en la apertura.")

    risk = abs(entry_ref - stop)
    if risk <= 0:
        risk = 0.25 * atr
    # T1 = lo que esté más lejos entre 1.5R y re-probar el extremo del día
    # (en un retroceso, el primer objetivo natural es volver al máximo/mínimo).
    extreme = hi if is_bull else lo
    t1 = entry_ref + sgn * 1.5 * risk
    if sgn * (extreme - t1) > 0:
        t1 = extreme
    t2 = t1 + sgn * 1.5 * risk if sgn * (t1 - entry_ref) >= 3.0 * risk else entry_ref + sgn * 3.0 * risk

    # Obstáculo antes de T1: call/put wall
    obstacles = []
    if is_bull:
        cw = opts.get("call_wall")
        if cw and entry_ref < cw < t1:
            obstacles.append(f"call wall ${cw:.2f}")
    else:
        pw = opts.get("put_wall")
        if pw and t1 < pw < entry_ref:
            obstacles.append(f"put wall ${pw:.2f}")

    return {
        "direction":      direction,
        "entry_state":    state,
        "entry_level":    round(level, 2),
        "entry_level_name": level_name,
        "entry_zone_lo":  round(zone_lo, 2),
        "entry_zone_hi":  round(zone_hi, 2),
        "entry_ref":      round(entry_ref, 2),
        "entry_trigger":  trigger,
        "entry_stop":     round(stop, 2),
        "entry_t1":       round(t1, 2),
        "entry_t2":       round(t2, 2),
        "entry_risk_pct": round(risk / entry_ref * 100, 2) if entry_ref else 0,
        "entry_rr_t1":    round(abs(t1 - entry_ref) / risk, 1) if risk else 0,
        "entry_vol_unit": round(atr, 2),
        "entry_ext_atr":  ext_atr,
        "entry_dist_atr": round(dist_atr, 2),
        "entry_obstacles": obstacles,
    }


def entry_decision(score: float, plan: Dict) -> Tuple[str, str]:
    """Texto de decisión = score (calidad del setup) × estado de entrada (momento)."""
    st = plan.get("entry_state")
    zone = f"${plan['entry_zone_lo']:.2f}–${plan['entry_zone_hi']:.2f}"
    green = score >= 7.0
    if score < 4.5:
        return "❌ NO OPERAR — no reúne criterios", "r"
    if st == "INVALIDADO":
        if plan.get("direction", "CALL") == "CALL":
            return "❌ ESTRUCTURA ROTA — perdió VWAP y rango de apertura", "r"
        return "❌ ESTRUCTURA ROTA — recuperó VWAP y rango de apertura", "r"
    if st == "MERCADO_CERRADO":
        return ("🌙 MERCADO CERRADO — " + ("setup verde, " if green else "") + "plan para la apertura"), "y"
    if st == "ESPERA_RANGO":
        return "⏳ PRIMEROS 15 MIN — espera el rango de apertura", "y"
    if st == "EN_ZONA":
        if green:
            return f"✅ ENTRAR — en zona {zone}", "g"
        return f"⏳ EN ZONA {zone} pero score medio — tamaño reducido", "y"
    if st == "ESPERA_PULLBACK":
        return (("🟢 SETUP VERDE" if green else "⏳ ESPERAR") +
                f" — espera retroceso a {plan['entry_level_name']} {zone}"), "y"
    if st == "ESPERA_RUPTURA":
        verb = "recupera" if plan.get("direction", "CALL") == "CALL" else "pierde"
        return (("🟢 SETUP VERDE" if green else "⏳ ESPERAR") +
                f" — entra solo si {verb} VWAP ${plan['entry_level']:.2f}"), "y"
    if st == "EXTENDIDO":
        return (f"⚠️ {'VERDE' if green else 'AMARILLO'} pero EXTENDIDO "
                f"{plan['entry_dist_atr']:.1f}× volatilidad del día — no perseguir"), "r"
    return ("✅ ENTRAR" if green else "⏳ ESPERAR"), ("g" if green else "y")


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
    data_is_stale = price.get("data_is_stale", False)
    data_age_h    = price.get("data_age_hours", 0)

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
    # Sin opciones — vol masivo + movimiento fuerte = institucional silencioso
    # Subido el techo: antes max 1.2, ahora hasta 2.0 si el vol es verdaderamente extremo
    elif abs_chg >= 20 and rel_vol >= 10:
        pts_A = 2.0  # GSIT/CRCL tipo: +20%+ con vol 10x = dinero real detrás
    elif abs_chg >= 15 and rel_vol >= 7:
        pts_A = 1.8
    elif abs_chg >= 10 and rel_vol >= 5:
        pts_A = 1.6
    elif abs_chg >= 10 and rel_vol >= 4:
        pts_A = 1.4
    elif abs_chg >= 7  and rel_vol >= 3:
        pts_A = 1.0
    elif abs_chg >= 5  and rel_vol >= 2:
        pts_A = 0.7
    else:
        pts_A = 0.2  # sin datos de opciones y sin movimiento relevante

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

    # Volumen inusual — ahora SUMA al catalizador existente, no lo reemplaza
    # Antes: solo activaba si pts_B == 0. Ahora siempre suma si el vol es real
    # Lógica: noticia débil + volumen explosivo = ambas señales cuentan
    vol_bonus_B = 0.0
    if abs_chg >= 20 and rel_vol >= 8:
        vol_bonus_B = 0.7   # movimiento extremo con vol masivo = catalizador implícito fuerte
    elif abs_chg >= 10 and rel_vol >= 5:
        vol_bonus_B = 0.5
    elif abs_chg >= 8 and rel_vol >= 3:
        vol_bonus_B = 0.4
    elif abs_chg >= 5 and rel_vol >= 2:
        vol_bonus_B = 0.3
    elif rel_vol >= 5:
        vol_bonus_B = 0.4   # vol 5x sin movimiento = acumulación silenciosa
    elif rel_vol >= 3:
        vol_bonus_B = 0.2
    elif rel_vol >= 2:
        vol_bonus_B = 0.1

    # Si no había catalizador, el vol implícito ES el catalizador (base mínima)
    if pts_B == 0:
        if abs_chg >= 8 and rel_vol >= 3:
            pts_B = 1.8
        elif abs_chg >= 5 and rel_vol >= 2:
            pts_B = 1.4
        elif abs_chg >= 3 and rel_vol >= 2:
            pts_B = 1.0
        elif rel_vol >= 5:
            pts_B = 1.5
        elif rel_vol >= 3:
            pts_B = 1.0
        elif rel_vol >= 2:
            pts_B = 0.6
        else:
            pts_B = 0.2
    else:
        # Ya había catalizador — sumar el bonus de vol encima
        pts_B = pts_B + vol_bonus_B

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
        elif chg_pct >= 8:
            pts_C = 1.4   # +8% es momentum fuerte aunque esté bajo MAs (ASTS, CRCL)
        elif chg_pct >= 5:
            pts_C = 1.0   # +5% con tendencia rompiendo es señal real
        elif chg_pct > 2:
            pts_C = 0.6
        else:
            pts_C = 0.2

        # Bonus: volumen confirma el movimiento alcista
        if rel_vol >= 5 and chg_pct > 3:
            pts_C = min(pts_C + 0.5, 2.5)
        elif rel_vol >= 3 and chg_pct > 1:
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
    # SIN_DATOS = no penalizar ni bonificar (no sabemos el estado real)
    if mkt_sent in ("SIN_DATOS", "NEUTRAL", "MUY_VOLATIL"):
        mkt_mod = 0.0
    elif mkt_sent == "ALCISTA" and is_bull:
        mkt_mod = 0.3
    elif mkt_sent == "BAJISTA" and not is_bull:
        mkt_mod = 0.3
    elif mkt_sent == "BAJISTA" and is_bull:
        mkt_mod = -0.3
    elif mkt_sent == "ALCISTA" and not is_bull:
        mkt_mod = -0.3
    else:
        mkt_mod = 0.0
    pts_C = min(max(pts_C + mkt_mod, 0), 2.5)

    # ════════════════════════════════════════════════════════
    # D. TIMING DE ENTRADA (max 2.5)
    # ¿Es buen momento para entrar AHORA? R/R y setup limpio
    # ════════════════════════════════════════════════════════
    pts_D = 1.0  # base neutral

    # RSI — sobrecomprado/sobrevendido penaliza el timing
    # EXCEPCIÓN: RSI alto con vol 5x+ = momentum genuino institucional, no sobreextensión
    vol_confirma = rel_vol >= 5.0  # vol masivo = institucionales detrás, RSI no importa tanto
    if rsi >= 80 and is_bull:
        pts_D -= 0.3 if vol_confirma else 0.8   # con vol masivo: penalidad mínima
    elif rsi >= 70 and is_bull:
        pts_D -= 0.1 if vol_confirma else 0.4   # con vol masivo: casi sin penalidad
    elif rsi <= 20 and not is_bull:
        pts_D -= 0.3 if vol_confirma else 0.8
    elif rsi <= 30 and not is_bull:
        pts_D -= 0.1 if vol_confirma else 0.4
    elif rsi <= 40 and is_bull:
        pts_D += 0.3   # RSI moderado en tendencia alcista = mejor entrada
    elif rsi >= 60 and not is_bull:
        pts_D += 0.3

    # Movimiento del día — penalizar solo si es extremo O no tiene catalizador
    # Con catalizador fuerte (earnings, noticia, sweep), el movimiento ES el setup
    # Con vol institucional real (5x+), reducir penalidad — el movimiento tiene combustible
    tiene_catalyst = pts_B >= 1.6
    tiene_vol_inst  = rel_vol >= 5.0  # vol institucional confirmado
    if abs_chg >= 20:
        # +20%: siempre penalizar, pero menos si hay vol institucional real
        pts_D -= 0.6 if tiene_vol_inst else 1.2
    elif abs_chg >= 15:
        pts_D -= 0.3 if tiene_vol_inst else 0.8
    elif abs_chg >= 10:
        # Con razón Y vol = oportunidad real; sin ninguno = peligro
        if tiene_catalyst and tiene_vol_inst:
            pts_D -= 0.0
        elif tiene_catalyst or tiene_vol_inst:
            pts_D -= 0.2
        else:
            pts_D -= 0.6
    elif abs_chg >= 7:
        pts_D -= 0.0 if (tiene_catalyst or tiene_vol_inst) else 0.3
    elif 2 <= abs_chg <= 8 and rel_vol >= 2:
        pts_D += 0.4   # setup ideal: movimiento moderado con volumen

    # Estado de entrada (estructura intradía en unidades de ATR).
    # Antes: ±1% / 5% vs VWAP fijo (para una small cap 5% no es nada, para AAPL es
    # muchísimo) + bonus por comprar cerca del mínimo del día (premiaba debilidad).
    plan = compute_entry_plan(price, direction, opts)
    pts_D += {
        "EN_ZONA":          0.6,
        "ESPERA_PULLBACK":  0.0,
        "ESPERA_RUPTURA":  -0.2,
        "EXTENDIDO":       -0.5,
        "INVALIDADO":      -0.8,
    }.get(plan["entry_state"], 0.0)

    # Datos stale (mercado cerrado): timing no es válido
    # El chg_pct que ves es del día ANTERIOR — no tomes decisiones con eso
    if data_is_stale:
        pts_D = min(pts_D * 0.2, 0.3)

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
    if data_is_stale:
        why.insert(0, f"⛔ DATOS DESACTUALIZADOS ({data_age_h:.0f}h) — mercado cerrado, no operar con estos datos")
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
    if plan["entry_ext_atr"] >= 1.0:
        lado = "sobre" if is_bull else "bajo"
        risk.append(f"⚠️ Precio {plan['entry_ext_atr']:.1f} ATR {lado} VWAP — extendido, espera retroceso")
    if not risk:
        risk.append(f"Monitorear VWAP ${vwap:.2f} como soporte. Stop bajo mínimo del día ${price.get('low',0):.2f}.")

    # ── STRIKE, STOP Y TARGETS — del plan de entrada ─────────
    suggested_strike = _pick_strike(opts.get("strikes") or [], current_price, is_bull)
    stop    = plan["entry_stop"]
    target1 = plan["entry_t1"]
    target2 = plan["entry_t2"]
    if atr > 0:
        atr_stop = round(current_price - 1.5*atr, 2) if is_bull else round(current_price + 1.5*atr, 2)
    else:
        atr_stop = stop
    if plan["entry_obstacles"]:
        risk.append("⚠️ Antes de T1: " + ", ".join(plan["entry_obstacles"]))
    decision, decision_color = entry_decision(score, plan)

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
        "vwap_trigger": plan["entry_trigger"],
        "plan":         plan,
        "decision":     decision,
        "decision_color": decision_color,
    }


# ══════════════════════════════════════════════════════════════
# CAPA 4: ANÁLISIS COMPLETO DE UN TICKER
# ══════════════════════════════════════════════════════════════

def analyze_ticker(ticker: str, market: Dict = None, alert_context: str = "",
                   light: bool = False, price_data: Dict = None) -> Dict[str, Any]:
    """
    light=True  → solo precio/volumen (2 requests). Se usa como primer filtro del scan.
    light=False → precio + fundamentales + opciones + noticias + sentimiento, en paralelo.
    Antes las 9–10 llamadas por ticker iban en serie: 50 tickers ≈ 450 requests
    secuenciales → el scan rebasaba el timeout de gunicorn y moría a medias.
    """
    ticker = ticker.upper().strip()
    if not market:
        market = {}

    fund_data, opts_data, news_data, sent_data = {}, {}, {}, {}
    if light:
        if price_data is None:
            price_data = get_price_data(ticker)
    else:
        with ThreadPoolExecutor(max_workers=5) as ex:
            f_price = ex.submit(get_price_data, ticker) if price_data is None else None
            f_fund  = ex.submit(get_fundamentals, ticker)
            f_opts  = ex.submit(get_options_data, ticker)
            f_news  = ex.submit(get_news_score, ticker)
            f_sent  = ex.submit(get_sentiment, ticker)
        if f_price is not None:
            price_data = f_price.result()
        fund_data, opts_data = f_fund.result(), f_opts.result()
        news_data, sent_data = f_news.result(), f_sent.result()

    if not price_data.get("price"):
        return {"ticker": ticker, "error": f"Sin datos para {ticker}", "score": 0, "semaforo": "ROJO"}

    # Doble check con precio real (el screener puede tener precio diferente al real)
    # Bajado de $5 a $2 para no perder small caps en movimiento (GSIT-tipo)
    if price_data.get("price", 0) < 2.0:
        return {"ticker": ticker, "error": f"{ticker} precio real ${price_data['price']:.2f} < $2.00", "score": 0, "semaforo": "ROJO"}

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
        "options_source":  opts_data.get("options_source"),
        "fund_source":     fund_data.get("fund_source"),
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
        "ez":              f"${scoring['plan']['entry_zone_lo']:.2f}–${scoring['plan']['entry_zone_hi']:.2f}",
        "entry_state":     scoring["plan"]["entry_state"],
        "entry_trigger":   scoring["plan"]["entry_trigger"],
        "entry_decision":  scoring["decision"],
        "entry_color":     scoring["decision_color"],
        "entry_risk_pct":  scoring["plan"]["entry_risk_pct"],
        "entry_ext_atr":   scoring["plan"]["entry_ext_atr"],
        "entry_plan":      scoring["plan"],
        "or_high":         price_data.get("or_high"),
        "or_low":          price_data.get("or_low"),
        "market_open":     price_data.get("market_open", False),
        "rel_volume_raw":  price_data.get("rel_volume_raw"),
        "rvol_method":     price_data.get("rvol_method"),
        "light":           light,
        "_price_data":     price_data if light else None,
        "expiry":          "Ver cadena en IBKR",
        "contract":        f"{ticker} {scoring['direction']} ${scoring['suggested_strike']:g}",
        "sl":              f"${scoring['stop']:.2f}",
        # Contexto
        "sentiment":       "POSITIVO" if price_data.get("change_pct",0) > 0 else "NEGATIVO",
        "control":         "COMPRADORES" if price_data.get("change_pct",0) > 0 and price_data.get("rel_volume",1) >= 1.5 else "VENDEDORES",
        "source":          "yahoo_finance",
        "timestamp":       datetime.now().isoformat(timespec="seconds"),
        "data_is_stale":   price_data.get("data_is_stale", False),
        "data_age_hours":  price_data.get("data_age_hours", 0),
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
        "ok": True, "service": "FlowScan 10", "version": "5.1",
        "sources": ["yahoo_finance", "stocktwits"],
        "scoring": "4-factor: Flujo + Catalizador + Momentum + Timing",
        "entry": "estructural: VWAP / rango de apertura / ATR",
        "rvol": "normalizado por hora del día",
        "yahoo_crumb": bool(YF.crumb), "http_backend": YF.backend,
        "time": datetime.now().isoformat(timespec="seconds"),
    })


# ══════════════════════════════════════════════════════════════
# ENDPOINTS PRINCIPALES
# ══════════════════════════════════════════════════════════════

@app.route("/api/pulse")
def api_pulse():
    """Sentimiento de mercado: SPY, QQQ, IWM, VIX."""
    return jsonify(get_market_pulse())


def run_batch(tickers: List[str], market: Dict, budget_s: float, workers: int,
              alert_context: str = "", light: bool = False,
              price_cache: Dict[str, Dict] = None) -> Tuple[List[Dict], int]:
    """Analiza en paralelo con tope de tiempo. Lo que no termina a tiempo se descarta
    (y se reporta) en vez de tumbar toda la respuesta."""
    price_cache = price_cache or {}
    items: List[Dict] = []
    if not tickers:
        return items, 0
    ex = ThreadPoolExecutor(max_workers=max(1, min(workers, len(tickers))))
    futs = {ex.submit(analyze_ticker, tk, market, alert_context, light, price_cache.get(tk)): tk
            for tk in tickers}
    done, pending = wait(futs, timeout=max(budget_s, 1.0))
    for f in done:
        try:
            r = f.result()
            if "error" not in r:
                if not light:
                    r.pop("_price_data", None)
                items.append(r)
        except Exception as e:
            print(f"analyze {futs[f]} error: {e}")
    ex.shutdown(wait=False, cancel_futures=True)
    if pending:
        print(f"run_batch: {len(pending)} tickers sin terminar dentro de {budget_s:.0f}s")
    return items, len(pending)


_SCAN_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_SCAN_LOCK = threading.Lock()


@app.route("/api/scan")
def api_scan():
    """
    Scan autónomo en dos etapas con presupuesto de tiempo:
      1) descubrir candidatos (screeners de Yahoo, en paralelo)
      2) análisis LIGERO (precio/volumen/estructura) de los top 50 → pre-score
      3) análisis COMPLETO (opciones, fundamentales, noticias) de los mejores 15
    Resultado en caché 45s (?fresh=1 para forzar).
    """
    t0 = time.time()
    fresh = request.args.get("fresh") == "1"
    with _SCAN_LOCK:
        cached = _SCAN_CACHE.get("data")
        if cached and not fresh and time.time() - _SCAN_CACHE["ts"] < SCAN_CACHE_S:
            return jsonify({**cached, "cached": True})

        market = get_market_pulse()

        candidates = discover_candidates()
        if not candidates:
            return jsonify({"error": "Todos los feeds de Yahoo fallaron. Verifica conectividad.", "items": []}), 503

        def remaining():
            return SCAN_BUDGET_S - (time.time() - t0)

        # Etapa 1: ligero sobre 50 candidatos (≈60% del tiempo que queda)
        pool = [c["ticker"] for c in candidates[:50]]
        light_items, light_late = run_batch(pool, market, remaining() * 0.6, SCAN_WORKERS, light=True)
        light_items.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Etapa 2: completo sobre los mejores (reusa los datos de precio ya bajados)
        top = [x["ticker"] for x in light_items[:ENRICH_TOP]]
        pcache = {x["ticker"]: x["_price_data"] for x in light_items[:ENRICH_TOP] if x.get("_price_data")}
        full_items, full_late = run_batch(top, market, remaining() - 1.0, max(4, SCAN_WORKERS // 2),
                                          price_cache=pcache)

        # Si algún completo no llegó, conservar su versión ligera
        got = {x["ticker"] for x in full_items}
        items = full_items + [x for x in light_items if x["ticker"] not in got]
        for x in items:
            x.pop("_price_data", None)
        items.sort(key=lambda x: x.get("score", 0), reverse=True)
        items = items[:10]

        verdes   = sum(1 for x in items if x.get("semaforo") == "VERDE")
        en_zona  = sum(1 for x in items if x.get("semaforo") == "VERDE" and x.get("entry_state") == "EN_ZONA")
        sweeps   = sum(1 for x in items if x.get("opt_is_sweep"))
        gammas   = sum(1 for x in items if x.get("gamma_score", 0) >= 5)

        top1 = items[0] if items else None
        if verdes > 0:
            summary = (f"{verdes} setup(s) VERDE ({en_zona} en zona ahora). "
                       f"Mejor: {top1['ticker']} {top1['score']}/10 ({top1['direction']}).")
            if sweeps:
                summary += f" 🚨 {sweeps} sweep(s) institucional(es)."
            if gammas:
                summary += f" ⚡ {gammas} posible(s) gamma squeeze."
        elif items:
            summary = f"{len(items)} tickers analizados. Sin señales verdes claras — esperar mejor setup."
        else:
            summary = "Sin resultados dentro del tiempo límite. Reintenta en un minuto."

        data = {
            "source":    "live_market_scan",
            "market":    market,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "summary":   summary,
            "verdes":    verdes,
            "en_zona":   en_zona,
            "sweeps":    sweeps,
            "gammas":    gammas,
            "analyzed":  len(items),
            "items":     items,
            "scan_stats": {
                "candidatos": len(candidates),
                "ligeros": len(light_items), "ligeros_tarde": light_late,
                "completos": len(full_items), "completos_tarde": full_late,
                "segundos": round(time.time() - t0, 1),
                "yahoo_crumb": bool(YF.crumb),
            },
        }
        _SCAN_CACHE.update({"ts": time.time(), "data": data})
        return jsonify(data)


@app.route("/api/ticker/<ticker>")
def api_ticker(ticker: str):
    """Análisis completo de un ticker específico."""
    market = get_market_pulse()
    result = analyze_ticker(ticker.upper(), market=market)
    result.pop("_price_data", None)
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
    items, _ = run_batch(tickers, market, SCAN_BUDGET_S, 5)

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
    analyzed, _ = run_batch(tickers, market, SCAN_BUDGET_S, 5, alert_context=ctx)
    for result in analyzed:
        if True:
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
# ALERTAS DEL GRUPO — parser del mensaje completo + clasificador
#   LEAP / POSICIONAL  → posicionamiento de meses: sesgo, no entrada de día
#   COBERTURA          → puts protegiendo una posición larga: no es apuesta bajista
#   DINAMITA           → tiene combustible para moverse YA
# ══════════════════════════════════════════════════════════════

_MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
          "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
          "jan": 1, "apr": 4, "aug": 8, "dec": 12}

_ALERT_EXCLUDE = {"SPY", "QQQ", "IWM", "SPX", "DIA", "VIX", "NDX", "RUT"}


def _num(s: str) -> Optional[float]:
    """'1.9' / '1,9' / '1,250.5' → float."""
    s = s.strip()
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_money(line: str) -> float:
    m = re.search(r"([\d.,]+)\s*(millones|millón|millon|mil|mdd|[MK])\b\s*(?:de\s+)?(?:d[oó]lares|usd)?", line, re.I)
    if not m:
        m = re.search(r"\$\s*([\d.,]+)\s*([MK])?", line, re.I)
    if not m:
        return 0.0
    n = _num(m.group(1)) or 0.0
    u = (m.group(2) or "").lower()
    if u.startswith("mill") or u in ("m", "mdd"):
        return n * 1e6
    if u in ("mil", "k"):
        return n * 1e3
    return n


def _parse_expiry(line: str) -> Optional[datetime]:
    m = re.search(r"vence[n]?\s+(?:el\s+)?(\d{1,2})\s+(?:de\s+)?([a-záéíóú]+)(?:\s+(?:de\s+)?(\d{4}))?", line, re.I)
    if not m:
        return None
    day, mes, year = int(m.group(1)), m.group(2).lower()[:3], m.group(3)
    month = _MESES.get(mes)
    if not month:
        return None
    today = datetime.now(NY_TZ).date()
    try:
        if year:
            d = datetime(int(year), month, day)
        else:
            d = datetime(today.year, month, day)
            if d.date() < today - timedelta(days=3):
                d = datetime(today.year + 1, month, day)
    except ValueError:
        return None
    return d


def parse_alert_message(raw: str) -> Dict[str, Any]:
    """Parser del mensaje diario del grupo, tal cual se recibe."""
    out: Dict[str, Any] = {"pivot_spy": None, "spy_levels": [], "spx_levels": [],
                           "hot_desks": [], "alerts": []}
    raw = (raw or "").replace(" ", " ").replace("⁠", "")

    piv = re.search(r"(\d{3,5}(?:\.\d+)?)\s*PIVOT", raw, re.I) or re.search(r"PIVOT\s*[:·]?\s*(\d{3,5}(?:\.\d+)?)", raw, re.I)
    if piv:
        out["pivot_spy"] = float(piv.group(1))
    for key, field in (("SPY", "spy_levels"), ("SPX", "spx_levels")):
        m = re.search(key + r"\s*:\s*([^\n]+)", raw)
        if m:
            out[field] = [float(x) for x in re.findall(r"\d{3,5}(?:\.\d+)?", m.group(1))]

    seen = set()
    for line in raw.splitlines():
        line = line.strip().strip("-•*·").strip()
        tk = re.match(r"^\$?([A-Z]{1,6})\b", line)
        if not tk:
            continue
        ticker = tk.group(1)
        if ticker in _ALERT_EXCLUDE or ticker in ("ÁREAS", "AREAS", "SPY", "SPX"):
            continue
        low = line.lower()

        # Mesas calientes: "GOOG 939 mil de contratos 61 % calls"
        if "contrato" in low and "%" in line:
            dm = re.search(r"([\d.,]+)\s*(millones|millón|millon|mil|[MK])?\s*(?:de\s+)?contratos?\s*(\d+(?:[.,]\d+)?)\s*%\s*(calls?|puts?)", line, re.I)
            if dm:
                n = _num(dm.group(1)) or 0
                u = (dm.group(2) or "").lower()
                n = n * 1e6 if u.startswith("mill") or u == "m" else n * 1e3 if u in ("mil", "k") else n
                pct = _num(dm.group(3)) or 0
                out["hot_desks"].append({"ticker": ticker, "contracts": int(n),
                                         "pct": pct, "dir": "CALL" if "call" in dm.group(4).lower() else "PUT"})
            continue

        # Alerta: requiere calls/puts + monto o strike
        side = re.search(r"\b(calls?|puts?)\b", line, re.I)
        if not side or ticker in seen:
            continue
        premium = _parse_money(line)
        sk = re.search(r"strike\s*\$?\s*([\d.,]+)", line, re.I)
        strike = _num(sk.group(1)) if sk else None
        if not premium and not strike:
            continue
        exp = _parse_expiry(line)

        # Niveles entre paréntesis: "(54 zona interés en caso de romperse)", "(429 posible gap, 411 posible soporte)"
        levels = []
        for grp in re.findall(r"\(([^)]*)\)", line):
            for part in grp.split(","):
                lm = re.match(r"\s*\$?([\d.]+)\s*(.*)", part)
                if lm:
                    levels.append({"price": float(lm.group(1)), "label": lm.group(2).strip() or "zona"})
        tail = re.sub(r"\([^)]*\)", "", line)
        note = ""
        nm = re.search(r"\s-\s*([a-záéíóú ]+)$", tail, re.I) or re.search(r"\s-([a-záéíóú]+)\s*$", tail, re.I)
        if nm:
            note = nm.group(1).strip()

        seen.add(ticker)
        out["alerts"].append({
            "ticker": ticker, "raw": line,
            "dir": "CALL" if "call" in side.group(1).lower() else "PUT",
            "premium": premium, "strike": strike,
            "expiry": exp.strftime("%Y-%m-%d") if exp else None,
            "levels": levels, "note": note,
        })
    out["alerts"].sort(key=lambda a: a["premium"], reverse=True)
    return out


def classify_alert(alert: Dict, item: Dict, desks: List[Dict], spy_regime: Optional[str]) -> Dict[str, Any]:
    """
    Separa el TIPO de flujo de la OPORTUNIDAD de entrada.
      horizonte : DTE de la alerta
      cobertura : probabilidad 0–100 de que un put sea protección y no apuesta
      dinamita  : 0–10, combustible para moverse YA (independiente del monto de la alerta)
    """
    side = alert["dir"]
    spot = float(item.get("spot") or 0)
    plan = item.get("entry_plan") or {}
    atr_u = float(plan.get("entry_vol_unit") or 0) or (spot * 0.03)
    today = datetime.now(NY_TZ).date()

    # ── Horizonte ──
    dte = None
    if alert.get("expiry"):
        dte = (datetime.strptime(alert["expiry"], "%Y-%m-%d").date() - today).days
    if dte is None:
        horizon = "SIN FECHA"
    elif dte <= 7:
        horizon = "SEMANAL"
    elif dte <= 45:
        horizon = "SWING"
    elif dte <= 180:
        horizon = "POSICIONAL"
    else:
        horizon = "LEAP"

    # ── Moneyness ──
    otm = None
    if alert.get("strike") and spot:
        otm = round(((alert["strike"] - spot) if side == "CALL" else (spot - alert["strike"])) / spot * 100, 1)

    # ── Cobertura (solo puts) ──
    hedge, hedge_why = 0, []
    if side == "PUT":
        hedge = 25
        trend_up = bool(item.get("above_sma50")) and bool(item.get("above_sma200"))
        if trend_up:
            hedge += 25; hedge_why.append("✓ acción en tendencia alcista (sobre MA50 y MA200) — se protegen ganancias")
        if otm is not None and 3 <= otm <= 20:
            hedge += 15; hedge_why.append(f"✓ put {otm:.0f}% OTM — rango típico de protección")
        elif otm is not None and otm < -2:
            hedge -= 15; hedge_why.append(f"✗ put {abs(otm):.0f}% ITM — más típico de apuesta o short sintético")
        if dte is not None and 14 <= dte <= 120:
            hedge += 10; hedge_why.append(f"✓ {dte} días — plazo típico de cobertura")
        beta = item.get("beta")
        try:
            if beta not in (None, "N/A") and float(beta) < 1.2:
                hedge += 10; hedge_why.append(f"✓ beta {float(beta):.2f} — acción defensiva/de cartera")
        except (TypeError, ValueError):
            pass
        ns = item.get("news_score") or 0
        chg = _num(str(item.get("change_pct", "0")).replace("%", "").replace("+", "")) or 0
        if ns <= -2:
            hedge -= 30; hedge_why.append("✗ noticia negativa reciente — más probable apuesta direccional")
        if not item.get("above_sma50") and not item.get("above_sma20"):
            hedge -= 25; hedge_why.append("✗ ya en tendencia bajista — huele a apuesta, no a protección")
        if chg <= -3:
            hedge -= 15; hedge_why.append(f"✗ cayendo {chg:.1f}% hoy — flujo direccional")
        ed = item.get("earnings_days")
        if ed is not None and 0 <= ed <= (dte or 0):
            hedge += 5; hedge_why.append("✓ cubre earnings — protección clásica antes del reporte")
        hedge = max(0, min(100, hedge))

    # ── Dinamita (0–10): ¿puede moverse YA? ──
    dyn, dyn_why = 0.0, []
    rvol = float(item.get("rel_volume") or 1)
    if rvol >= 4:
        dyn += 2.0; dyn_why.append(f"volumen {rvol:.1f}x para la hora")
    elif rvol >= 2:
        dyn += 1.2; dyn_why.append(f"volumen {rvol:.1f}x para la hora")
    elif rvol >= 1.4:
        dyn += 0.5; dyn_why.append(f"volumen {rvol:.1f}x")

    voi = float(item.get("opt_vol_oi") or 0)
    if voi >= 3:
        dyn += 2.0; dyn_why.append(f"contratos nuevos: vol/OI {voi:.1f}x en vencimientos cercanos")
    elif voi >= 1.5:
        dyn += 1.2; dyn_why.append(f"contratos nuevos: vol/OI {voi:.1f}x")
    dom = item.get("opt_dominant")
    if dom in ("CALL", "PUT") and dom == side and voi >= 1:
        dyn += 0.4; dyn_why.append(f"flujo cercano del mismo lado ({dom})")

    ns, na = item.get("news_score") or 0, item.get("news_age_hours")
    if abs(ns) >= 2 and na is not None and na <= 24:
        dyn += 1.5; dyn_why.append(f"noticia de impacto hace {na:.0f}h")
    ed = item.get("earnings_days")
    if ed is not None and 0 <= ed <= 10:
        dyn += 1.2; dyn_why.append(f"earnings en {ed:.0f} día(s)")
    if item.get("analyst_action"):
        dyn += 0.6; dyn_why.append("acción de analista reciente")

    chg = abs(_num(str(item.get("change_pct", "0")).replace("%", "").replace("+", "")) or 0)
    hi, lo = float(item.get("high") or 0), float(item.get("low") or 0)
    if atr_u and hi > lo and (hi - lo) / atr_u >= 1.0 and chg >= 2:
        dyn += 1.0; dyn_why.append("rango de hoy ≥ ATR — expansión de volatilidad")

    st = item.get("entry_state")
    dir_ok = item.get("direction") == side
    if dir_ok and st == "EN_ZONA":
        dyn += 1.2; dyn_why.append("estructura a favor y en zona")
    elif dir_ok and st == "ESPERA_PULLBACK":
        dyn += 0.6; dyn_why.append("estructura a favor, esperando retroceso")
    elif st in ("INVALIDADO",):
        dyn -= 1.0; dyn_why.append("estructura en contra")

    if (item.get("gamma_score") or 0) >= 5 and side == "CALL":
        dyn += 0.6; dyn_why.append("presión gamma cerca del precio")
    if (item.get("short_pct") or 0) >= 15 and side == "CALL":
        dyn += 0.5; dyn_why.append(f"short float {item.get('short_pct'):.0f}%")
    desk = next((d for d in desks if d["ticker"] == alert["ticker"]), None)
    if desk:
        dyn += 0.6; dyn_why.append(f"mesa caliente: {desk['contracts']:,} contratos, {desk['pct']:.0f}% {desk['dir'].lower()}s")
    if spy_regime:
        if (spy_regime == "SOBRE" and side == "CALL") or (spy_regime == "BAJO" and side == "PUT"):
            dyn += 0.4; dyn_why.append(f"SPY {spy_regime.lower()} el pivot — mercado a favor")
        elif spy_regime in ("SOBRE", "BAJO"):
            dyn -= 0.4; dyn_why.append(f"SPY {spy_regime.lower()} el pivot — mercado en contra")
    dyn = round(max(0.0, min(10.0, dyn)), 1)

    # ── Zonas de la alerta vs precio ──
    zones = []
    for lv in alert.get("levels") or []:
        d_atr = (lv["price"] - spot) / atr_u if atr_u and spot else None
        zones.append({**lv, "dist_pct": round((lv["price"] - spot) / spot * 100, 1) if spot else None,
                      "dist_atr": round(d_atr, 2) if d_atr is not None else None,
                      "near": d_atr is not None and abs(d_atr) <= 0.5})
    near_zone = next((z for z in zones if z["near"]), None)

    # ── Etiqueta principal ──
    if side == "PUT" and hedge >= 60:
        label, color, tip = "🛡️ COBERTURA", "b", "Probable protección de una posición larga — no la tomes como señal bajista"
    elif dyn >= 6.5:
        label, color, tip = "🧨 DINAMITA", "g", "Tiene combustible para moverse ya — revisa la entrada"
    elif side == "PUT" and 40 <= hedge < 60:
        label, color, tip = "❓ PUT AMBIGUO", "y", "Puede ser cobertura o apuesta — no operar sin confirmación de precio"
    elif near_zone and dyn < 4.5:
        label, color, tip = "📍 EN ZONA SIN CHISPA", "y", \
            "Llegó al nivel de la alerta pero sin volumen ni catalizador — esperar reacción del precio"
    elif zones and not near_zone and dyn < 6.5:
        label, color, tip = "👀 VIGILAR ZONA", "y", "Esperar a que el precio llegue al nivel de la alerta"
    elif horizon in ("LEAP", "POSICIONAL") and dyn < 5:
        label, color, tip = ("🏦 LEAP" if horizon == "LEAP" else "🏦 POSICIONAL"), "y", \
            "Posicionamiento de semanas/meses — úsalo como sesgo, no como entrada de día"
    elif dyn >= 4.5:
        label, color, tip = "🔥 CALENTANDO", "y", "Señales parciales — falta confirmación"
    else:
        label, color, tip = "⚪ SIN CHISPA", "r", "Sin combustible hoy"

    operable = (label in ("🧨 DINAMITA",) and st == "EN_ZONA" and dir_ok) or \
               (near_zone is not None and dyn >= 4.5 and hedge < 60)

    return {
        "alert_label": label, "alert_color": color, "alert_tip": tip,
        "alert_horizon": horizon, "alert_dte": dte, "alert_otm_pct": otm,
        "hedge_prob": hedge if side == "PUT" else None, "hedge_why": hedge_why,
        "dinamita": dyn, "dinamita_why": dyn_why,
        "alert_zones": zones, "alert_near_zone": near_zone,
        "alert_operable": operable,
    }


def _alert_rank_key(x: Dict) -> Tuple:
    order = {"🧨 DINAMITA": 0, "🔥 CALENTANDO": 1, "📍 EN ZONA SIN CHISPA": 2, "👀 VIGILAR ZONA": 2, "🏦 POSICIONAL": 3,
             "🏦 LEAP": 4, "❓ PUT AMBIGUO": 5, "🛡️ COBERTURA": 6, "⚪ SIN CHISPA": 7}
    return (0 if x.get("alert_operable") else 1, order.get(x.get("alert_label"), 9),
            -(x.get("dinamita") or 0), -(x.get("alert", {}).get("premium") or 0))


@app.route("/api/alerts", methods=["POST"])
def api_alerts():
    """
    Recibe el mensaje completo del grupo (áreas de interés + mesas calientes + alertas)
    y regresa cada alerta clasificada (LEAP / cobertura / dinamita) y rankeada.
    """
    t0 = time.time()
    payload = request.get_json(force=True, silent=True) or {}
    parsed = parse_alert_message(payload.get("raw") or "")
    if not parsed["alerts"]:
        return jsonify({"error": "No encontré alertas (ticker + calls/puts + monto o strike).",
                        "parsed": parsed, "items": []}), 400

    market = get_market_pulse()
    spy = (market.get("SPY") or {}).get("price")
    regime, regime_pct = None, None
    if parsed["pivot_spy"] and spy:
        regime_pct = round((spy - parsed["pivot_spy"]) / parsed["pivot_spy"] * 100, 2)
        regime = "SOBRE" if regime_pct > 0.1 else "BAJO" if regime_pct < -0.1 else "EN"

    alerts = parsed["alerts"][:20]
    ex = ThreadPoolExecutor(max_workers=min(6, len(alerts)))
    futs = {ex.submit(analyze_ticker, a["ticker"], market, a["raw"]): a for a in alerts}
    done, pending = wait(futs, timeout=max(SCAN_BUDGET_S - (time.time() - t0), 5))
    ex.shutdown(wait=False, cancel_futures=True)

    items, failed = [], [futs[f]["ticker"] for f in pending]
    for f in done:
        a = futs[f]
        try:
            it = f.result()
        except Exception as e:
            print(f"alert {a['ticker']} error: {e}")
            failed.append(a["ticker"]); continue
        if "error" in it:
            failed.append(a["ticker"]); continue
        it.pop("_price_data", None)
        it["alert"] = a
        it.update(classify_alert(a, it, parsed["hot_desks"], regime))
        items.append(it)

    items.sort(key=_alert_rank_key)
    counts: Dict[str, int] = {}
    for it in items:
        counts[it["alert_label"]] = counts.get(it["alert_label"], 0) + 1
    ops = [it["ticker"] for it in items if it.get("alert_operable")]
    summary = (f"{len(items)} alertas · " + " · ".join(f"{k} {v}" for k, v in counts.items()) +
               (f" · Operables ahora: {', '.join(ops)}" if ops else " · Ninguna operable en este momento"))

    return jsonify({
        "source": "discord_alerts",
        "market": market,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "pivot_spy": parsed["pivot_spy"], "spy_levels": parsed["spy_levels"], "spx_levels": parsed["spx_levels"],
        "spy_regime": regime, "spy_vs_pivot_pct": regime_pct,
        "hot_desks": parsed["hot_desks"],
        "summary": summary, "counts": counts, "operables": ops,
        "failed": failed, "segundos": round(time.time() - t0, 1),
        "items": items,
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
    items, _ = run_batch(tickers, market, SCAN_BUDGET_S, 6)
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
    crumb = YF.get_crumb()
    out["yahoo_crumb"] = {"ok": bool(crumb), "error": YF.last_error, "backend": YF.backend}
    pd   = get_price_data("NVDA")
    out["price"]    = {"ok": bool(pd.get("price")), "price": pd.get("price"),
                       "rel_vol_hora": pd.get("rel_volume"), "rel_vol_dia_completo": pd.get("rel_volume_raw"),
                       "rvol_method": pd.get("rvol_method"), "vwap": pd.get("vwap"),
                       "or_high": pd.get("or_high"), "or_low": pd.get("or_low"),
                       "market_open": pd.get("market_open"), "session_minutes": pd.get("session_minutes")}
    fund = get_fundamentals("NVDA")
    out["fundamentals"] = {"ok": bool(fund.get("fv_fields") or fund.get("fund_source") == "yahoo"),
                           "fields": fund.get("fv_fields"), "source": fund.get("fund_source"),
                           "short_pct": fund.get("short_pct"), "sector": fund.get("sector"),
                           "earnings_date": fund.get("earnings_date"), "target": fund.get("target_price")}
    opts = get_options_data("NVDA")
    out["options"]  = {"ok": opts.get("has_options"), "vol_oi": opts.get("vol_oi"), "gamma": opts.get("gamma_score"),
                       "strikes": len(opts.get("strikes") or []), "source": opts.get("options_source"),
                       "call_wall": opts.get("call_wall"), "put_wall": opts.get("put_wall")}
    news = get_news_score("NVDA")
    out["news"]     = {"ok": news.get("fetched") is not None, "fetched": news.get("fetched"),
                       "score": news.get("score"), "title": news.get("title")}
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
