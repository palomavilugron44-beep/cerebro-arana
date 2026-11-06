# Cerebro + Araña para BotCripto

Funciona solo en la nube (GitHub Actions), **cada hora, gratis**:

1. **Araña** (`arana.py`): recorre feeds de noticias cripto (CoinDesk, Cointelegraph, Decrypt,
   Bitcoin.com, NewsBTC), entra a leer los artículos más importantes (respetando robots.txt),
   y junta el índice de Miedo/Codicia, precios de las 10 monedas (CoinGecko) y el calendario
   económico de EE. UU. (Forex Factory).
2. **Cerebro** (`cerebro.py`): Claude lee todo eso y decide, como gestor de riesgo:
   - 🟢 **normal**: BotCripto opera como siempre.
   - 🟡 **cautela**: compra con la mitad del tamaño.
   - 🔴 **pausa**: no abre compras nuevas (las abiertas siguen con su stop).
   Puede decidir para todo el mercado o para una moneda puntual.
   Si Claude no responde, decide con reglas simples de respaldo.
3. Publica la decisión en `publico/decision.json` y te avisa por Telegram cuando cambia
   (y un resumen diario a las 9:00 de Chile).

**El Cerebro solo puede hacer al bot más prudente.** Nunca lo obliga a comprar, nunca sube
el riesgo y nunca cierra operaciones solo (si ve algo grave, te avisa para que tú decidas).

## Archivos
| Archivo | Para qué |
|---|---|
| `arana.py` | El crawler |
| `cerebro.py` | La IA que decide (y las reglas de respaldo) |
| `main.py` | Ejecuta todo y avisa por Telegram |
| `.github/workflows/cerebro.yml` | Lo programa cada hora en GitHub |
| `probar_en_mi_pc.bat` | Probarlo en tu PC antes de subirlo |
| `publico/` | Lo crea solo: `decision.json` (lo lee BotCripto) e `historial.csv` |

## Secretos (Settings → Secrets and variables → Actions)
- `ANTHROPIC_API_KEY` – clave de la API de Claude
- `TELEGRAM_TOKEN` – el token de tu bot (el mismo de BotCripto)
- `TELEGRAM_CHAT_ID` – tu chat_id (el mismo de BotCripto)

Nunca escribas claves dentro de los archivos: este repositorio es público.

## Costo
GitHub Actions es gratis en repositorios públicos. La API de Claude cobra por uso:
con el modelo Haiku, cada revisión cuesta una fracción de centavo (del orden de 1 dólar al mes revisando cada hora).
Para usar otro modelo, crea la variable `CLAUDE_MODELO` (por ejemplo `claude-sonnet-5-5`).
