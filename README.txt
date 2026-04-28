FLOWSCAN v3 MAC
================

Objetivo:
Backend Python local + frontend HTML. Ahora usa datos demo/manuales, pero queda listo para conectar una API real después.

INSTALACIÓN EN WINDOWS
1) Instala Python 3.11 o superior.
2) Abre CMD o PowerShell en esta carpeta.
3) Ejecuta:

   pip install flask flask-cors
   python server.py

4) Abre en el navegador:

   http://127.0.0.1:5050

QUÉ HACE
- /api/flow entrega flujo demo ya rankeado.
- /api/manual permite pegar JSON manual exportado de otra fuente.
- Aplica filtros duros:
  premium mínimo, Vol/OI mínimo, spread máximo, ejecución at/above ask, score >= 7.

DÓNDE SE CONECTA UNA API REAL DESPUÉS
En server.py, reemplazar DEMO_FLOW o crear una función fetch_real_flow() que consulte:
- Unusual Whales, si tienes API/plan compatible.
- Barchart, si tu plan permite endpoints de opciones.
- Polygon.io o Tradier para datos de mercado/opciones.

IMPORTANTE
No pongas API keys en el HTML. Van en el backend, idealmente como variables de entorno.
