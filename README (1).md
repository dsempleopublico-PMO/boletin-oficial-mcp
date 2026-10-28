# MCP del Boletín Oficial de la República Argentina

Servidor MCP que conecta Claude con [boletinoficial.gob.ar](https://www.boletinoficial.gob.ar). Con esto le podés preguntar cosas como:

- "¿Qué decretos salieron hoy en el Boletín Oficial?"
- "Buscame resoluciones de ANMAT del último mes y resumime las más importantes."
- "Traeme el texto del DNU 70/2023."

## Qué es un MCP (en 30 segundos)

Un **MCP** (Model Context Protocol) es un programa chico que le ofrece **herramientas** a Claude. Claude lee el nombre y la descripción de cada herramienta, decide cuándo usarla y la llama con parámetros. El servidor hace el trabajo (acá: pedirle datos al Boletín) y le devuelve el resultado.

Este servidor (versión 0.2) tiene 3 herramientas:

| Herramienta | Qué hace |
|---|---|
| `edicion_del_dia` | Sumario de una edición: cada decreto, resolución, disposición o aviso con su organismo, número y página. Sin fecha = la más reciente; si pedís un fin de semana, devuelve la edición anterior. |
| `leer_norma` | Texto completo de una norma (ej. `"Decreto 1286/2026"`, `"ANSES 304"`) o de una página del PDF. |
| `buscar_en_boletin` | Busca palabras en el texto completo de las ediciones de un rango de fechas (hasta 31 días), sin importar tildes ni mayúsculas. |

### De dónde salen los datos

El sitio principal (`www.boletinoficial.gob.ar`) **no acepta conexiones desde servidores en la nube fuera de Argentina** (lo comprobamos desde Render y desde la nube de Anthropic). Por eso el servidor usa el **listado oficial de PDFs** del Boletín, `https://otslist.boletinoficial.gob.ar/ots/`, que sí responde:

1. Busca en el listado el PDF de la fecha y sección pedidas (`/ots/?date_filter=DD-MM-AAAA`).
2. Lo descarga y **verifica su hash SHA-256** contra el publicado por el Boletín.
3. Extrae el texto de cada página con `pypdfium2` y arma la lista de normas a partir del **SUMARIO** de la propia edición.

Los PDF ya leídos quedan en memoria (los últimos 10), así que las consultas repetidas son instantáneas. El sumario se entiende bien en la primera sección; en la segunda devuelve los rubros (sociedades anónimas, SRL, etc.), y en la tercera y la cuarta conviene usar `buscar_en_boletin`.

## Archivos

```
boletin-oficial-mcp/
├── server.py           ← el servidor MCP (todo el código, comentado)
├── probar_en_vivo.py   ← prueba contra el sitio real
├── tests/test_server.py← pruebas sin internet (simulan el sitio)
├── pyproject.toml      ← dependencias
└── Dockerfile          ← para publicarlo en internet (opción C)
```

> **Cómo está armado hoy:** opción C, con el código en GitHub y el servidor en Render (gratis), conectado como conector personalizado en claude.ai. No hace falta instalar nada en la compu. Para actualizarlo: editás `server.py` en GitHub y en Render tocás **Manual Deploy → Deploy latest commit**.

---

## Paso 1 — Instalar y probar

Necesitás **Python 3.11+** y **uv** (el gestor de paquetes que recomienda la documentación de MCP).

```bash
# Instalar uv (una sola vez)
# macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell):
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Entrar a la carpeta y probar contra el sitio real
cd boletin-oficial-mcp
uv run probar_en_vivo.py
```

`uv run` crea el entorno virtual e instala las dependencias solo la primera vez. Si ves avisos en los 3 pasos, anda. Si no, ver **Problemas frecuentes** abajo.

Opcional:

```bash
uv run --with pytest pytest          # pruebas sin internet
npx @modelcontextprotocol/inspector uv run server.py   # interfaz web para probar las tools a mano
```

## Paso 2 — Conectarlo a Claude

Elegí según dónde uses Claude.

### Opción A: Claude Desktop (local, lo más simple)

1. En Claude Desktop: **Configuración → Desarrollador → Editar configuración**. Se abre `claude_desktop_config.json`:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Agregá esto (cambiá la ruta por la tuya, **ruta absoluta**):

```json
{
  "mcpServers": {
    "boletin-oficial": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/rosana/boletin-oficial-mcp", "server.py"]
    }
  }
}
```

   En Windows la ruta va con barras dobles: `"C:\\Users\\rosana\\boletin-oficial-mcp"`.

3. Cerrá Claude Desktop del todo y volvé a abrirlo. En el chat vas a ver el ícono de herramientas con `boletin-oficial`.

> Si dice que no encuentra `uv`, poné la ruta completa en `"command"` (sale de `which uv` en macOS/Linux o `where uv` en Windows).

### Opción B: Claude Code (terminal)

```bash
claude mcp add boletin-oficial -- uv run --directory /ruta/absoluta/boletin-oficial-mcp server.py
claude mcp list        # para verificar que quedó
```

Agregá `--scope user` si lo querés disponible en todos tus proyectos.

### Opción C: claude.ai en el navegador o el celular (conector personalizado)

Los conectores personalizados se conectan **desde la nube de Anthropic**, no desde tu compu. Por eso el servidor tiene que estar publicado en internet con HTTPS.

**Para probar rápido (con tu compu prendida):**

```bash
# Terminal 1: el servidor en modo HTTP
uv run server.py --http          # queda en http://localhost:8000/mcp

# Terminal 2: un túnel público (https://ngrok.com, gratis)
ngrok http 8000                  # te da algo como https://abc123.ngrok-free.app
```

**Para dejarlo fijo:** subí la carpeta a un hosting que acepte Docker (Render, Railway, Fly.io, Google Cloud Run). El `Dockerfile` ya está listo; el servidor escucha en la variable `PORT` que pone el hosting.

**Después, en claude.ai:** **Configuración → Conectores → Agregar conector personalizado**, y pegá la URL terminada en **`/mcp`** (ej. `https://abc123.ngrok-free.app/mcp`). No pide login porque son datos públicos.

---

## Cómo agregar una herramienta nueva

Una herramienta es una función `async` con el decorador `@mcp.tool()`. El **docstring es lo que lee Claude** para saber cuándo usarla, así que escribilo claro. Los tipos de los parámetros se convierten solos en el esquema que ve Claude.

```python
@mcp.tool()
async def decretos_de_hoy() -> dict[str, Any]:
    """Lista solo los decretos de la edición más reciente del Boletín Oficial."""
    return await edicion_del_dia(filtro="Decreto")
```

Después de editarlo en GitHub, en Render tocá **Manual Deploy → Deploy latest commit**. Si lo usás local, reiniciá Claude Desktop o Claude Code.

Ideas para seguir: combinarlo con [InfoLEG](https://www.infoleg.gob.ar) para ver el texto actualizado de una ley, o armar una tarea programada que cada mañana te avise si aparece una palabra clave.

## Problemas frecuentes

| Síntoma | Qué revisar |
|---|---|
| "No se pudo conectar con el Boletín Oficial" | Abrí https://otslist.boletinoficial.gob.ar/ots/ en el navegador. Si carga ahí pero no en el servidor, el Boletín empezó a bloquear también ese subdominio. |
| El sumario sale vacío en la primera sección | Cambió el formato del PDF. Ajustá `parsear_sumario` en `server.py` (las pruebas en `tests/` muestran el formato esperado). |
| La primera consulta tarda casi un minuto | Normal en el plan gratis de Render: el servidor se duerme a los 15 minutos sin uso y tarda en despertar. |
| Claude Desktop no muestra la herramienta | JSON mal formado (una coma de más), ruta relativa, o no reiniciaste la app. Los logs están en Configuración → Desarrollador. |
| `ModuleNotFoundError: mcp.server.fastmcp` | Ese es código de la versión 1 del SDK. En la v2, `FastMCP` pasó a llamarse `MCPServer` (`from mcp.server.mcpserver import MCPServer`). Muchos tutoriales todavía usan la v1. |
| Funciona local pero no publicado | Algunos sitios del Estado bloquean IPs de servidores en la nube. Probá con ngrok desde tu compu para confirmarlo. |

## Uso responsable

Es información pública, pero el servidor le pide datos a un sitio del Estado. Usalo para consultas puntuales, no para descargar el archivo histórico entero. Si lo publicás (opción C), cualquiera que tenga la URL puede usarlo: no la compartas públicamente. El servidor solo se conecta a `otslist.boletinoficial.gob.ar`, así que nadie puede usarlo para pedir otros sitios.
