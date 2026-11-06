"""ARAÑA: recorre la web y junta la información que el Cerebro necesita.

Qué recorre:
  1. Feeds RSS de noticias cripto (CoinDesk, Cointelegraph, Decrypt, Bitcoin.com, NewsBTC).
  2. Las páginas de los artículos más relevantes (respeta robots.txt y espera entre visitas).
  3. Índice de Miedo y Codicia (alternative.me).
  4. Precios y cambio 24 h / 7 d de las 10 monedas y del mercado total (CoinGecko).
  5. Calendario económico de la semana: eventos de alto impacto en USD (Forex Factory).

Solo usa la librería estándar de Python (no hay que instalar nada).
"""
import datetime as dt
import email.utils
import html
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

AGENTE = "CerebroAranaBot/1.0 (uso personal; lector de noticias para un bot de trading)"
TIMEOUT = 20
ESPERA_MISMO_SITIO = 2.0      # segundos entre dos visitas al mismo sitio
HORAS_NOTICIAS = 12           # solo noticias de las últimas N horas
MAX_PAGINAS = 8               # artículos que la araña abre y lee completos
MAX_TEXTO_PAGINA = 1500       # caracteres que guarda de cada artículo

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "Bitcoin.com": "https://news.bitcoin.com/feed/",
    "NewsBTC": "https://www.newsbtc.com/feed/",
}

# par de Binance -> (id en CoinGecko, palabras que la identifican en una noticia)
MONEDAS = {
    "BTCUSDT": ("bitcoin", ["bitcoin", "btc"]),
    "ETHUSDT": ("ethereum", ["ethereum", "ether", "eth"]),
    "SOLUSDT": ("solana", ["solana", "sol"]),
    "BNBUSDT": ("binancecoin", ["bnb", "binance coin", "bnb chain"]),
    "XRPUSDT": ("ripple", ["xrp", "ripple"]),
    "ADAUSDT": ("cardano", ["cardano", "ada"]),
    "DOGEUSDT": ("dogecoin", ["dogecoin", "doge"]),
    "LINKUSDT": ("chainlink", ["chainlink", "link"]),
    "AVAXUSDT": ("avalanche-2", ["avalanche", "avax"]),
    "LTCUSDT": ("litecoin", ["litecoin", "ltc"]),
}

# Palabras que suelen anticipar movimientos bruscos o riesgo.
# Las que terminan en * se buscan como inicio de palabra (liquidat* = liquidation, liquidated...);
# las demás, como palabra completa (así "rate" no coincide con "corporate").
RIESGO = ["hack*", "exploit*", "drain*", "stolen", "breach*", "sec", "lawsuit*", "sue*", "charged",
          "ban", "bans", "banned", "crackdown", "delist*", "halt*", "outage*", "insolv*", "bankrupt*",
          "liquidat*", "crash*", "plung*", "sell-off", "selloff", "dump*", "depeg*", "fed", "fomc",
          "rate", "rates", "inflation", "cpi", "tariff*", "etf", "etfs", "regulat*", "sanction*", "war",
          "recession", "approv*", "reject*", "freez*", "frozen"]


def _patron(p):
    if p.endswith("*"):
        return re.compile(rf"(?<![a-z0-9]){re.escape(p[:-1])}")
    return re.compile(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])")


PATRONES_RIESGO = [(p.rstrip("*"), _patron(p)) for p in RIESGO]


# ---------------------------------------------------------------- red
_ultima_visita = {}
_robots = {}


def _esperar_turno(url):
    host = urllib.parse.urlparse(url).netloc
    falta = ESPERA_MISMO_SITIO - (time.time() - _ultima_visita.get(host, 0))
    if falta > 0:
        time.sleep(falta)
    _ultima_visita[host] = time.time()


def bajar(url, intentos=2):
    """Descarga una URL y devuelve el texto (o None si falla)."""
    for i in range(intentos):
        _esperar_turno(url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": AGENTE,
                                                       "Accept": "*/*", "Accept-Language": "en,es"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                datos = r.read(3_000_000)
                cod = r.headers.get_content_charset() or "utf-8"
                return datos.decode(cod, errors="replace")
        except Exception as ex:  # noqa: BLE001 - la araña nunca debe caerse por una página
            error = ex
            time.sleep(2 * (i + 1))
    print(f"  [araña] no pude leer {url}: {error}")
    return None


def bajar_json(url):
    t = bajar(url)
    if t is None:
        return None
    try:
        return json.loads(t)
    except ValueError:
        print(f"  [araña] respuesta no es JSON: {url}")
        return None


def permitido(url):
    """Respeta robots.txt de cada sitio."""
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        texto = bajar(base + "/robots.txt", intentos=1)
        if texto is None:
            rp = None            # sin robots.txt legible: se asume permitido
        else:
            rp.parse(texto.splitlines())
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(AGENTE, url)


# ---------------------------------------------------------------- texto
class _ExtractorTexto(HTMLParser):
    """Saca el texto de los párrafos <p> de un artículo y su meta descripción."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parrafos, self._en_p, self._saltar = [], False, 0
        self._actual, self.descripcion = [], ""

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "nav", "footer", "aside"):
            self._saltar += 1
        elif tag == "p" and not self._saltar:
            self._en_p, self._actual = True, []
        elif tag == "meta":
            a = dict(attrs)
            if (a.get("name") or a.get("property") or "").lower() in ("description", "og:description"):
                self.descripcion = self.descripcion or (a.get("content") or "")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "nav", "footer", "aside") and self._saltar:
            self._saltar -= 1
        elif tag == "p" and self._en_p:
            t = " ".join("".join(self._actual).split())
            if len(t) > 60:
                self.parrafos.append(t)
            self._en_p = False

    def handle_data(self, data):
        if self._en_p and not self._saltar:
            self._actual.append(data)


def limpiar_html(texto):
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    return " ".join(html.unescape(texto).split())


def texto_articulo(url):
    pagina = bajar(url, intentos=1)
    if not pagina:
        return ""
    ex = _ExtractorTexto()
    try:
        ex.feed(pagina)
    except Exception:  # noqa: BLE001
        pass
    cuerpo = " ".join(ex.parrafos)
    texto = (ex.descripcion + " " + cuerpo).strip()
    return texto[:MAX_TEXTO_PAGINA]


# ---------------------------------------------------------------- noticias
def _fecha(texto):
    if not texto:
        return None
    texto = texto.strip()
    try:
        d = email.utils.parsedate_to_datetime(texto)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(texto.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _hijo(nodo, *nombres):
    for n in nombres:
        for h in nodo:
            if h.tag.split("}")[-1] == n:
                return h
    return None


def leer_feed(nombre, url):
    xml = bajar(url)
    if not xml:
        return []
    try:
        raiz = ET.fromstring(xml.encode("utf-8", errors="replace"))
    except ET.ParseError:
        print(f"  [araña] feed ilegible: {nombre}")
        return []
    salida = []
    for nodo in raiz.iter():
        if nodo.tag.split("}")[-1] not in ("item", "entry"):
            continue
        t = _hijo(nodo, "title")
        l = _hijo(nodo, "link")
        f = _hijo(nodo, "pubDate", "published", "updated", "date")
        d = _hijo(nodo, "description", "summary", "encoded")
        link = (l.text or l.get("href") or "").strip() if l is not None else ""
        salida.append({
            "fuente": nombre,
            "titulo": limpiar_html(t.text if t is not None else ""),
            "link": link,
            "fecha": _fecha(f.text if f is not None else None),
            "resumen": limpiar_html(d.text if d is not None else "")[:400],
        })
    return salida


def _palabra(texto, p):
    return re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", texto) is not None


def puntuar(noticia):
    t = f" {noticia['titulo']} {noticia['resumen']} ".lower()
    monedas = [par for par, (_, pal) in MONEDAS.items() if any(_palabra(t, p) for p in pal)]
    riesgo = sorted({nombre for nombre, pat in PATRONES_RIESGO if pat.search(t)})
    noticia["monedas"] = monedas
    noticia["palabras_riesgo"] = riesgo
    noticia["puntaje"] = len(monedas) + 2 * len(riesgo) + (1 if "market" in t or "price" in t else 0)
    return noticia


def recolectar_noticias():
    limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=HORAS_NOTICIAS)
    todas, vistos, estado_fuentes = [], set(), {}
    for nombre, url in FEEDS.items():
        items = leer_feed(nombre, url)
        estado_fuentes[nombre] = len(items)
        for n in items:
            clave = re.sub(r"\W+", "", n["titulo"].lower())[:80]
            if not n["titulo"] or clave in vistos:
                continue
            if n["fecha"] and n["fecha"] < limite:
                continue
            vistos.add(clave)
            todas.append(puntuar(n))
    todas.sort(key=lambda n: (n["puntaje"], n["fecha"] or limite), reverse=True)

    # La araña entra a leer los artículos más relevantes
    leidas = 0
    for n in todas:
        if leidas >= MAX_PAGINAS:
            break
        if n["puntaje"] < 2 or not n["link"].startswith("http"):
            continue
        if not permitido(n["link"]):
            n["texto"] = "(robots.txt no permite leer esta página)"
            continue
        n["texto"] = texto_articulo(n["link"])
        leidas += 1 if n["texto"] else 0

    for n in todas:
        n["fecha"] = n["fecha"].isoformat(timespec="minutes") if n["fecha"] else None
    return todas, estado_fuentes, leidas


# ---------------------------------------------------------------- mercado
def miedo_codicia():
    d = bajar_json("https://api.alternative.me/fng/?limit=7")
    try:
        datos = d["data"]
        return {"hoy": int(datos[0]["value"]), "clasificacion": datos[0]["value_classification"],
                "ultimos_7_dias": [int(x["value"]) for x in datos]}
    except (TypeError, KeyError, ValueError, IndexError):
        return None


def mercado():
    ids = ",".join(v[0] for v in MONEDAS.values())
    precios = bajar_json(
        "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids="
        f"{ids}&price_change_percentage=24h,7d")
    glob = bajar_json("https://api.coingecko.com/api/v3/global")
    salida = {"monedas": {}, "global": None}
    por_id = {v[0]: par for par, v in MONEDAS.items()}
    for m in precios or []:
        par = por_id.get(m.get("id"))
        if par:
            salida["monedas"][par] = {
                "precio": m.get("current_price"),
                "cambio_24h": _r(m.get("price_change_percentage_24h_in_currency")),
                "cambio_7d": _r(m.get("price_change_percentage_7d_in_currency")),
                "volumen_24h_usd": m.get("total_volume"),
            }
    try:
        g = glob["data"]
        salida["global"] = {
            "cambio_capitalizacion_24h": _r(g["market_cap_change_percentage_24h_usd"]),
            "dominancia_btc": _r(g["market_cap_percentage"]["btc"]),
        }
    except (TypeError, KeyError):
        pass
    return salida


def _r(x, d=2):
    return round(x, d) if isinstance(x, (int, float)) else None


def calendario_economico():
    """Eventos de alto impacto en USD en las próximas 24 horas (Forex Factory)."""
    d = bajar_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    if not isinstance(d, list):
        return None
    ahora = dt.datetime.now(dt.timezone.utc)
    eventos = []
    for e in d:
        if e.get("country") != "USD" or e.get("impact") != "High":
            continue
        f = _fecha(e.get("date"))
        if f and ahora - dt.timedelta(hours=2) <= f <= ahora + dt.timedelta(hours=24):
            eventos.append({"evento": e.get("title"), "hora_utc": f.isoformat(timespec="minutes"),
                            "previsto": e.get("forecast"), "anterior": e.get("previous")})
    return eventos


# ---------------------------------------------------------------- todo junto
def recorrer():
    print("[araña] recorriendo noticias...")
    noticias, fuentes, leidas = recolectar_noticias()
    print(f"[araña] {len(noticias)} noticias de las últimas {HORAS_NOTICIAS} h; {leidas} artículos leídos completos.")
    print("[araña] leyendo índice de miedo/codicia, precios y calendario...")
    return {
        "recolectado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fuentes": fuentes,
        "noticias": noticias,
        "articulos_leidos": leidas,
        "miedo_codicia": miedo_codicia(),
        "mercado": mercado(),
        "calendario_usd": calendario_economico(),
    }


if __name__ == "__main__":
    r = recorrer()
    print(json.dumps({k: v for k, v in r.items() if k != "noticias"}, ensure_ascii=False, indent=2))
    for n in r["noticias"][:10]:
        print(f"- ({n['puntaje']}) [{n['fuente']}] {n['titulo']}")
