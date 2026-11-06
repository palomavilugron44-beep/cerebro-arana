"""Ejecuta una revisión completa: Araña -> Cerebro -> publica la decisión -> avisa por Telegram.

En la nube (GitHub Actions) se ejecuta solo cada hora. Las claves se leen de variables de entorno:
  ANTHROPIC_API_KEY   clave de la API de Claude (si falta, decide con reglas simples)
  TELEGRAM_TOKEN      token de tu bot de Telegram (el mismo de BotCripto)
  TELEGRAM_CHAT_ID    tu chat_id (el mismo de BotCripto)
"""
import csv
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import arana
import cerebro

BASE = os.path.dirname(os.path.abspath(__file__))
# En la prueba local se guarda aparte, para no mezclarla con lo que se publica en la nube
PUBLICO = os.path.join(BASE, "prueba_local" if os.environ.get("CEREBRO_LOCAL") else "publico")
ARCH_DECISION = os.path.join(PUBLICO, "decision.json")
ARCH_HISTORIAL = os.path.join(PUBLICO, "historial.csv")
ARCH_ESTADO = os.path.join(PUBLICO, "estado_avisos.json")
ZONA = ZoneInfo("America/Santiago")
HORA_RESUMEN = 9          # resumen diario por Telegram desde las 9:00 (hora de Chile)
EMOJI = {"normal": "🟢", "cautela": "🟡", "pausa": "🔴"}
NOMBRE = {"normal": "NORMAL (opera como siempre)",
          "cautela": "CAUTELA (compras a mitad de tamaño)",
          "pausa": "PAUSA (sin compras nuevas)"}


def telegram(texto):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("[telegram] sin token/chat_id: el aviso solo queda en pantalla.")
        return
    datos = urllib.parse.urlencode({"chat_id": chat, "text": texto[:4000],
                                    "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=datos, timeout=20)
    except Exception as ex:  # noqa: BLE001
        print(f"[telegram] no se pudo enviar: {ex}")


def leer_json(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defecto


def huella(d):
    """Lo que, si cambia, merece un aviso."""
    return {"modo": d["modo"], "monedas": {p: v["modo"] for p, v in d["monedas"].items()},
            "alerta": d.get("alerta_posiciones", "")}


def texto_aviso(d, titulo):
    l = [f"🧠 CEREBRO · {titulo}",
         f"{EMOJI[d['modo']]} Modo general: {NOMBRE[d['modo']]}"]
    for par, v in d["monedas"].items():
        l.append(f"{EMOJI[v['modo']]} {par.replace('USDT', '')}: {v['modo'].upper()} — {v['motivo']}")
    if d.get("resumen"):
        l.append(f"\n{d['resumen']}")
    if d.get("motivos"):
        l.append("\nMotivos:\n" + "\n".join(f"• {m}" for m in d["motivos"]))
    x = d["datos"]
    l.append(f"\nMiedo/Codicia: {x['miedo_codicia']} · Mercado 24 h: {x['cambio_mercado_24h']}% · "
             f"{x['noticias_analizadas']} noticias, {x['articulos_leidos']} artículos leídos")
    if d.get("alerta_posiciones"):
        l.append(f"\n⚠️ REVISA TUS POSICIONES ABIERTAS: {d['alerta_posiciones']}\n"
                 f"(El Cerebro no cierra nada solo. Si quieres cerrar todo, escribe /cerrar a BotCripto.)")
    if d["origen"] == "reglas":
        l.append("\n(Decidido con reglas de respaldo: Claude no respondió en esta revisión.)")
    return "\n".join(l)


def guardar_historial(d):
    nuevo = not os.path.exists(ARCH_HISTORIAL)
    with open(ARCH_HISTORIAL, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["fecha_utc", "modo", "monedas_especiales", "miedo_codicia", "mercado_24h",
                        "noticias", "articulos", "origen", "resumen"])
        x = d["datos"]
        w.writerow([d["generado"], d["modo"],
                    " ".join(f"{p}:{v['modo']}" for p, v in d["monedas"].items()),
                    x["miedo_codicia"], x["cambio_mercado_24h"], x["noticias_analizadas"],
                    x["articulos_leidos"], d["origen"], d["resumen"]])


def main():
    os.makedirs(PUBLICO, exist_ok=True)
    datos = arana.recorrer()
    d = cerebro.decidir(datos)
    with open(ARCH_DECISION, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    guardar_historial(d)
    print(f"[cerebro] decisión: {d['modo']} {d['monedas'] or ''} ({d['origen']})")
    print(f"[cerebro] {d['resumen']}")

    estado = leer_json(ARCH_ESTADO, {})
    ahora_local = dt.datetime.now(ZONA)
    hoy = ahora_local.date().isoformat()
    if estado.get("huella") is None:
        telegram(texto_aviso(d, "conectado y funcionando"))
    elif huella(d) != estado.get("huella"):
        telegram(texto_aviso(d, "cambio de decisión"))
    elif ahora_local.hour >= HORA_RESUMEN and estado.get("ultimo_resumen") != hoy:
        telegram(texto_aviso(d, f"resumen del día {ahora_local:%d-%m}"))
        estado["ultimo_resumen"] = hoy
    if estado.get("huella") is None:
        estado["ultimo_resumen"] = hoy
    estado["huella"] = huella(d)
    with open(ARCH_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
