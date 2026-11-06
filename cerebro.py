"""CEREBRO: lee lo que juntó la Araña, razona con Claude y decide cómo debe comportarse BotCripto.

Regla de seguridad: el Cerebro solo puede hacer al bot MÁS prudente (comprar menos o no comprar).
Nunca lo obliga a comprar ni sube el riesgo que pusiste en config.ini.

Decisiones posibles (global y por moneda):
  normal  -> BotCripto opera como siempre (riesgo x1)
  cautela -> compra con la mitad del riesgo (o menos)
  pausa   -> no abre compras nuevas (las abiertas siguen con su stop)
"""
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.request

from arana import MONEDAS

MODELO = os.environ.get("CLAUDE_MODELO", "claude-haiku-5-5")
MODELO_RESPALDO = "claude-sonnet-5-5"
URL_API = "https://api.anthropic.com/v1/messages"
HORAS_VALIDEZ = 3
MODOS = ("normal", "cautela", "pausa")
MULT = {"normal": 1.0, "cautela": 0.5, "pausa": 0.0}
GRAVEDAD = {"normal": 0, "cautela": 1, "pausa": 2}

INSTRUCCIONES = """Eres el "Cerebro" de un bot de trading de criptomonedas (Binance spot, sin apalancamiento).
El bot sigue una estrategia de tendencia: compra cuando la EMA20 cruza sobre la EMA50 en velas de 4 horas
con el precio sobre la EMA200, y vende con el cruce contrario o con un stop a 3 ATR. Las operaciones duran días.
Opera estas monedas: BTC, ETH, SOL, BNB, XRP, ADA, DOGE, LINK, AVAX, LTC (contra USDT).

Tu trabajo NO es predecir precios ni dar señales de compra. Tu trabajo es de GESTOR DE RIESGO:
decidir si en las próximas horas conviene que el bot abra compras nuevas normalmente, con cautela
(la mitad del tamaño) o que no abra compras nuevas (pausa), en general o para monedas puntuales.

Criterios:
- "pausa" solo ante riesgos concretos y graves: hackeo/exploit o problema de seguridad de una red o exchange,
  acción regulatoria o judicial fuerte contra una moneda o Binance, caída brusca del mercado en curso
  (por ejemplo, capitalización total -7% o peor en 24 h con noticias de liquidaciones), dato macro de muy alto
  impacto (FOMC/tasas, IPC) en las próximas 2-3 horas, o una moneda en situación anómala (halt, depeg, delist).
- "cautela" ante incertidumbre elevada: miedo extremo o codicia extrema, volatilidad fuerte, noticias
  negativas relevantes pero no graves, evento macro importante dentro de las próximas 24 h.
- "normal" en cualquier otro caso. Esta es la opción por defecto: la estrategia ya tiene su propio stop,
  así que no frenes al bot por ruido, rumores, opiniones de analistas o titulares sensacionalistas.
- Las noticias positivas NO justifican aumentar el riesgo (no existe esa opción).
- Basa cada decisión en los datos entregados; no inventes hechos. Si los datos son escasos, elige "normal".

Responde SOLO con un objeto JSON válido, sin texto antes ni después, con esta forma exacta:
{
  "modo": "normal" | "cautela" | "pausa",
  "monedas": {"SOLUSDT": {"modo": "cautela" | "pausa", "motivo": "frase corta"}},
  "resumen": "2 o 3 frases en español claro sobre la situación del mercado y por qué decidiste esto",
  "motivos": ["motivo concreto 1", "motivo concreto 2"],
  "alerta_posiciones": ""
}
- "monedas": incluye solo las monedas que necesitan un modo MÁS prudente que el global (si no hay, {}).
- "alerta_posiciones": déjalo vacío salvo que haya un riesgo grave e inmediato que justifique que el humano
  revise o cierre las posiciones abiertas; en ese caso explícalo en una frase.
- Escribe todo en español."""


# ---------------------------------------------------------------- preparar datos
def resumir_para_prompt(datos, max_noticias=40):
    """Convierte lo recolectado en un texto compacto (barato en tokens)."""
    lineas = [f"Fecha y hora actual (UTC): {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}"]
    fg = datos.get("miedo_codicia")
    if fg:
        lineas.append(f"Índice Miedo/Codicia: {fg['hoy']} ({fg['clasificacion']}); "
                      f"últimos 7 días: {fg['ultimos_7_dias']}")
    m = datos.get("mercado") or {}
    if m.get("global"):
        g = m["global"]
        lineas.append(f"Capitalización total cripto, cambio 24 h: {g['cambio_capitalizacion_24h']}%; "
                      f"dominancia BTC {g['dominancia_btc']}%")
    if m.get("monedas"):
        lineas.append("Monedas (precio USD | cambio 24 h | cambio 7 d):")
        for par, v in m["monedas"].items():
            lineas.append(f"  {par}: {v['precio']} | {v['cambio_24h']}% | {v['cambio_7d']}%")
    cal = datos.get("calendario_usd")
    if cal is not None:
        if cal:
            lineas.append("Eventos económicos USD de alto impacto (próximas 24 h, hora UTC):")
            for e in cal:
                lineas.append(f"  {e['hora_utc']} {e['evento']} (previsto {e['previsto']}, anterior {e['anterior']})")
        else:
            lineas.append("Sin eventos económicos USD de alto impacto en las próximas 24 h.")
    noticias = datos.get("noticias") or []
    lineas.append(f"\nNoticias de las últimas horas ({len(noticias)} en total; se muestran las "
                  f"{min(len(noticias), max_noticias)} más relevantes):")
    for n in noticias[:max_noticias]:
        mon = ",".join(p.replace("USDT", "") for p in n.get("monedas", [])) or "-"
        lineas.append(f"- [{n['fuente']} {n.get('fecha') or ''}] ({mon}) {n['titulo']}")
        if n.get("texto"):
            lineas.append(f"    Extracto del artículo: {n['texto'][:900]}")
        elif n.get("resumen"):
            lineas.append(f"    {n['resumen'][:250]}")
    return "\n".join(lineas)


# ---------------------------------------------------------------- Claude
# La respuesta se pide como "herramienta" con un esquema fijo: así Claude entrega datos
# estructurados y no texto JSON que pueda venir mal escrito.
HERRAMIENTA = {
    "name": "registrar_decision",
    "description": "Registra la decisión de gestión de riesgo para BotCripto.",
    "input_schema": {
        "type": "object",
        "properties": {
            "modo": {"type": "string", "enum": list(MODOS)},
            "monedas": {
                "type": "object",
                "description": "Solo monedas que necesitan un modo más prudente que el global. Clave: par (ej. SOLUSDT).",
                "additionalProperties": {
                    "type": "object",
                    "properties": {"modo": {"type": "string", "enum": ["cautela", "pausa"]},
                                   "motivo": {"type": "string"}},
                    "required": ["modo", "motivo"],
                },
            },
            "resumen": {"type": "string", "description": "2 o 3 frases en español."},
            "motivos": {"type": "array", "items": {"type": "string"}},
            "alerta_posiciones": {"type": "string", "description": "Vacío salvo riesgo grave e inmediato."},
        },
        "required": ["modo", "monedas", "resumen", "motivos", "alerta_posiciones"],
    },
}


def preguntar_a_claude(texto_datos, clave):
    cuerpo = {
        "max_tokens": 1500,
        "system": INSTRUCCIONES + "\n\nEntrega tu decisión usando la herramienta registrar_decision.",
        "tools": [HERRAMIENTA],
        "tool_choice": {"type": "tool", "name": "registrar_decision"},
        "messages": [{"role": "user", "content":
                      "Datos recolectados por la araña:\n\n" + texto_datos +
                      "\n\nDecide ahora y regístralo con la herramienta."}],
    }
    ultimo_error = None
    for modelo in (MODELO, MODELO_RESPALDO):
        cuerpo["model"] = modelo
        req = urllib.request.Request(
            URL_API, data=json.dumps(cuerpo).encode("utf-8"), method="POST",
            headers={"x-api-key": clave, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            detalle = ex.read().decode("utf-8", errors="replace")[:300]
            ultimo_error = f"HTTP {ex.code}: {detalle}"
            if ex.code in (400, 404):      # p. ej. modelo no disponible: probar el de respaldo
                continue
            break
        except Exception as ex:  # noqa: BLE001
            ultimo_error = str(ex)
            break
        uso = resp.get("usage", {})
        for b in resp.get("content", []):
            if b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                return b["input"], modelo, uso
        texto = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        return texto, modelo, uso
    raise RuntimeError(f"No pude consultar a Claude ({ultimo_error})")


def extraer_json(texto):
    if isinstance(texto, dict):
        return texto
    texto = texto.strip()
    texto = re.sub(r"^```(?:json)?|```$", "", texto, flags=re.M).strip()
    ini, fin = texto.find("{"), texto.rfind("}")
    if ini < 0 or fin < 0:
        raise ValueError("la respuesta no trae JSON")
    return json.loads(texto[ini:fin + 1])


def validar(d):
    """Limpia la respuesta de la IA: solo acepta valores permitidos."""
    modo = d.get("modo") if d.get("modo") in MODOS else "normal"
    monedas = {}
    for par, v in (d.get("monedas") or {}).items():
        par = str(par).upper().replace("/", "")
        if not par.endswith("USDT"):
            par += "USDT"
        if par not in MONEDAS or not isinstance(v, dict):
            continue
        m = v.get("modo")
        if m in MODOS and GRAVEDAD[m] > GRAVEDAD[modo]:
            monedas[par] = {"modo": m, "motivo": str(v.get("motivo", ""))[:200]}
    motivos = [str(x)[:250] for x in (d.get("motivos") or []) if str(x).strip()][:6]
    return {
        "modo": modo,
        "monedas": monedas,
        "resumen": str(d.get("resumen", ""))[:700],
        "motivos": motivos,
        "alerta_posiciones": str(d.get("alerta_posiciones") or "")[:300],
    }


# ---------------------------------------------------------------- respaldo sin IA
def decidir_con_reglas(datos):
    """Si Claude no está disponible, decide con reglas simples y prudentes."""
    modo, motivos = "normal", []
    fg = (datos.get("miedo_codicia") or {}).get("hoy")
    g = ((datos.get("mercado") or {}).get("global") or {}).get("cambio_capitalizacion_24h")
    if g is not None and g <= -7:
        modo = "pausa"
        motivos.append(f"El mercado total cae {g}% en 24 h.")
    elif g is not None and g <= -4:
        modo = "cautela"
        motivos.append(f"El mercado total cae {g}% en 24 h.")
    if fg is not None and (fg <= 15 or fg >= 85) and modo == "normal":
        modo = "cautela"
        motivos.append(f"Índice Miedo/Codicia extremo ({fg}).")
    if datos.get("calendario_usd") and modo == "normal":
        ahora = dt.datetime.now(dt.timezone.utc)
        for e in datos["calendario_usd"]:
            h = dt.datetime.fromisoformat(e["hora_utc"])
            if ahora - dt.timedelta(hours=1) <= h <= ahora + dt.timedelta(hours=3):
                modo = "cautela"
                motivos.append(f"Dato económico de alto impacto cerca: {e['evento']}.")
                break
    monedas = {}
    for par, v in ((datos.get("mercado") or {}).get("monedas") or {}).items():
        c = v.get("cambio_24h")
        if c is not None and c <= -12 and GRAVEDAD[modo] < 2:
            monedas[par] = {"modo": "pausa", "motivo": f"Cae {c}% en 24 h."}
    malas = [n for n in (datos.get("noticias") or []) if
             {"hack", "exploit", "drain", "stolen", "depeg", "halt", "delist"} & set(n["palabras_riesgo"])]
    for n in malas[:5]:
        for par in n["monedas"]:
            if GRAVEDAD[modo] < 1 and par not in monedas:
                monedas[par] = {"modo": "cautela", "motivo": f"Noticia de riesgo: {n['titulo'][:120]}"}
    if not motivos:
        motivos.append("Sin señales de riesgo fuertes en los datos.")
    return {"modo": modo, "monedas": monedas,
            "resumen": "Decisión tomada con reglas simples (Claude no estuvo disponible en esta revisión).",
            "motivos": motivos, "alerta_posiciones": ""}


# ---------------------------------------------------------------- clave
def limpiar_clave(clave):
    """Quita lo que suele colarse al pegar: espacios, saltos de línea, comillas o 'ANTHROPIC_API_KEY='."""
    clave = (clave or "").strip()
    if "=" in clave and clave.upper().startswith("ANTHROPIC_API_KEY"):
        clave = clave.split("=", 1)[1]
    clave = "".join(clave.split()).strip("\"'“”‘’")
    return clave


def describir_clave(clave):
    """Describe la forma de la clave SIN revelarla (para diagnosticar errores)."""
    if not clave:
        return "la clave está vacía"
    inicio = "empieza con sk-ant-" if clave.startswith("sk-ant-") else "NO empieza con sk-ant-"
    extra = " y tiene '...' (es la versión abreviada)" if "..." in clave or "…" in clave else ""
    return f"la clave recibida {inicio}, tiene {len(clave)} caracteres{extra} (una correcta tiene más de 100)"


# ---------------------------------------------------------------- decisión final
def decidir(datos, clave=None):
    clave = limpiar_clave(clave if clave is not None else os.environ.get("ANTHROPIC_API_KEY", ""))
    if clave:
        print(f"[cerebro] {describir_clave(clave)}")
    origen, uso, error = "reglas", {}, None
    if clave:
        try:
            texto_datos = resumir_para_prompt(datos)
            try:
                texto, modelo, uso = preguntar_a_claude(texto_datos, clave)
                d = validar(extraer_json(texto))
            except ValueError:            # respuesta ilegible: un reintento
                print("[cerebro] respuesta ilegible, reintento una vez...")
                texto, modelo, uso = preguntar_a_claude(texto_datos, clave)
                d = validar(extraer_json(texto))
            origen = f"claude:{modelo}"
        except Exception as ex:  # noqa: BLE001
            error = str(ex)[:300]
            if "401" in error:
                error += f" | Diagnóstico: {describir_clave(clave)}."
            print(f"[cerebro] {error} -> uso reglas de respaldo.")
            d = decidir_con_reglas(datos)
    else:
        print("[cerebro] sin ANTHROPIC_API_KEY -> uso reglas de respaldo.")
        d = decidir_con_reglas(datos)

    ahora = dt.datetime.now(dt.timezone.utc)
    m = datos.get("mercado") or {}
    d.update({
        "version": 1,
        "generado": ahora.isoformat(timespec="seconds"),
        "valido_hasta": (ahora + dt.timedelta(hours=HORAS_VALIDEZ)).isoformat(timespec="seconds"),
        "origen": origen,
        "multiplicador_riesgo": MULT[d["modo"]],
        "datos": {
            "miedo_codicia": (datos.get("miedo_codicia") or {}).get("hoy"),
            "cambio_mercado_24h": (m.get("global") or {}).get("cambio_capitalizacion_24h"),
            "noticias_analizadas": len(datos.get("noticias") or []),
            "articulos_leidos": datos.get("articulos_leidos", 0),
            "fuentes": datos.get("fuentes"),
            "eventos_usd_24h": len(datos.get("calendario_usd") or []),
        },
        "titulares": [{"titulo": n["titulo"], "fuente": n["fuente"], "link": n["link"]}
                      for n in (datos.get("noticias") or [])[:6]],
        "uso_tokens": uso,
    })
    for v in d["monedas"].values():
        v["multiplicador_riesgo"] = MULT[v["modo"]]
    if error:
        d["error_ia"] = error
    return d
