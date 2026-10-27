"""
MCP del Boletín Oficial de la República Argentina (BORA)
=========================================================

Servidor MCP que le da a Claude tres herramientas:

  1. edicion_del_dia  -> lista los avisos/normas publicados en una sección y fecha
  2. buscar_avisos    -> búsqueda avanzada (texto, número de norma, rango de fechas)
  3. leer_aviso       -> trae el texto completo de un aviso

El Boletín no tiene una API pública documentada. Este servidor usa las mismas
URLs que usa el sitio web (y que usan otros proyectos open source como
chequeado/botletin-oficial y juancarlospaco/borapp). Si el sitio cambia su HTML,
hay que ajustar los selectores de las funciones `parsear_*`.

Uso:
    python server.py            # modo local (stdio): Claude Desktop / Claude Code
    python server.py --http     # modo remoto (HTTP): conector personalizado en claude.ai
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# El SDK de MCP v2 trae `httpx2` (sucesor de httpx, misma API). Si no está, usamos httpx.
try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

BASE_URL = "https://www.boletinoficial.gob.ar"
HOSTS_PERMITIDOS = {"www.boletinoficial.gob.ar", "boletinoficial.gob.ar"}

Seccion = Literal["primera", "segunda", "tercera"]
NUMERO_SECCION = {"primera": 1, "segunda": 2, "tercera": 3}

HEADERS = {
    "User-Agent": "boletin-oficial-mcp/0.1 (consulta personal via Claude)",
    "Accept-Language": "es-AR,es;q=0.9",
}
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Los links a cada aviso tienen la forma /detalleAviso/<seccion>/<id>/<AAAAMMDD>
RE_DETALLE = re.compile(r"/detalleAviso/(primera|segunda|tercera|cuarta)/(\d+)/(\d{8})")

# Los tests reemplazan esto por un transporte falso para no salir a internet.
_transport: Any = None


mcp = MCPServer(
    name="boletin-oficial",
    title="Boletín Oficial de la República Argentina",
    instructions=(
        "Herramientas para consultar el Boletín Oficial de la República Argentina "
        "(boletinoficial.gob.ar). Secciones: primera = legislación y avisos oficiales "
        "(leyes, decretos, resoluciones); segunda = sociedades y avisos judiciales; "
        "tercera = contrataciones. Flujo típico: usar edicion_del_dia o buscar_avisos "
        "para obtener la lista, y después leer_aviso con la URL para ver el texto completo. "
        "Citá siempre la URL del aviso cuando uses su contenido."
    ),
)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _cliente() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers=HEADERS,
        timeout=TIMEOUT,
        follow_redirects=True,
        transport=_transport,
    )


def parsear_fecha(valor: str | None) -> date:
    """Acepta 'AAAA-MM-DD', 'DD/MM/AAAA' o 'DD-MM-AAAA'. Vacío = hoy."""
    if not valor:
        return date.today()
    valor = valor.strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(valor, formato).date()
        except ValueError:
            continue
    raise ToolError(f"Fecha inválida: {valor!r}. Usá el formato AAAA-MM-DD (ej. 2026-10-27).")


def _limpiar(texto: str) -> str:
    """Colapsa espacios y líneas en blanco repetidas."""
    lineas = [re.sub(r"[ \t\xa0]+", " ", linea).strip() for linea in texto.splitlines()]
    salida: list[str] = []
    for linea in lineas:
        if linea or (salida and salida[-1]):
            salida.append(linea)
    return "\n".join(salida).strip()


def _url_aviso(seccion: str, id_aviso: str, fecha_aaaammdd: str) -> str:
    return f"{BASE_URL}/detalleAviso/{seccion}/{id_aviso}/{fecha_aaaammdd}"


def _validar_url(url: str) -> str:
    """Evita que el servidor se use para pedir URLs de otros sitios (importante si lo publicás)."""
    if url.startswith("/"):
        url = BASE_URL + url
    partes = urlparse(url)
    if partes.scheme not in ("http", "https") or partes.hostname not in HOSTS_PERMITIDOS:
        raise ToolError("Solo se aceptan URLs de www.boletinoficial.gob.ar")
    if not RE_DETALLE.search(partes.path):
        raise ToolError(
            "La URL no parece ser de un aviso. Debe tener la forma "
            "https://www.boletinoficial.gob.ar/detalleAviso/<seccion>/<id>/<AAAAMMDD>"
        )
    return f"{BASE_URL}{partes.path}"  # sin parámetros de consulta


async def _get(ruta: str) -> str:
    try:
        async with _cliente() as cliente:
            resp = await cliente.get(ruta)
            resp.raise_for_status()
            return resp.text
    except httpx.HTTPStatusError as e:
        raise ToolError(f"El Boletín Oficial respondió {e.response.status_code} para {ruta}") from e
    except httpx.HTTPError as e:
        raise ToolError(f"No se pudo conectar con el Boletín Oficial: {e.__class__.__name__}: {e}") from e


# ---------------------------------------------------------------------------
# Parsers (acá se adapta todo si el sitio cambia su HTML)
# ---------------------------------------------------------------------------


def parsear_listado(html: str) -> list[dict[str, str]]:
    """Extrae los avisos de una página de sección o del HTML que devuelve la búsqueda."""
    soup = BeautifulSoup(html, "html.parser")
    contenedor = soup.find(id="avisosSeccionDiv") or soup
    avisos: list[dict[str, str]] = []
    vistos: set[str] = set()
    for a in contenedor.find_all("a", href=True):
        m = RE_DETALLE.search(a["href"])
        if not m:
            continue
        seccion, id_aviso, fecha = m.groups()
        if id_aviso in vistos:
            continue
        vistos.add(id_aviso)
        partes = [t for t in a.stripped_strings if t]
        avisos.append(
            {
                "id": id_aviso,
                "seccion": seccion,
                "fecha": f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}",
                "resumen": " | ".join(partes)[:400],
                "url": _url_aviso(seccion, id_aviso, fecha),
            }
        )
    return avisos


def parsear_detalle(html: str) -> dict[str, Any]:
    """Extrae organismo, título, tipo, texto y anexos de la página de un aviso."""
    soup = BeautifulSoup(html, "html.parser")
    titulo_div = soup.find(id="tituloDetalleAviso")
    cuerpo = soup.find(id="cuerpoDetalleAviso")
    if cuerpo is None:
        raise ToolError("No se encontró el cuerpo del aviso en la página (¿cambió el sitio o la URL es incorrecta?).")

    def _texto(el: Any) -> str:
        return _limpiar(el.get_text(" ")) if el is not None else ""

    tipo = soup.select_one(".puntero.first-section")
    anexos = []
    for a in cuerpo.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") or "anexo" in href.lower():
            anexos.append({"texto": _limpiar(a.get_text(" ")) or "anexo", "url": href})

    return {
        "organismo": _texto(titulo_div.find("h1")) if titulo_div else "",
        "titulo": _texto(titulo_div.find("h2")) if titulo_div else "",
        "tipo": _texto(tipo),
        "texto": _limpiar(cuerpo.get_text("\n")),
        "anexos": anexos,
    }


def armar_params_busqueda(
    texto: str,
    secciones: list[int],
    fecha_desde: date,
    fecha_hasta: date,
    nro_norma: str = "",
    anio_norma: str = "",
    todas_las_palabras: bool = True,
    pagina: int = 1,
) -> str:
    """El formulario de búsqueda avanzada manda un JSON con estos campos (copiados del sitio)."""
    params = {
        "busquedaRubro": False,
        "hayMasResultadosBusqueda": True,
        "ejecutandoLlamadaAsincronicaBusqueda": False,
        "ultimaSeccion": "",
        "filtroPorRubrosSeccion": False,
        "filtroPorRubroBusqueda": False,
        "filtroPorSeccionBusqueda": False,
        "busquedaOriginal": True,
        "ordenamientoSegunda": False,
        "seccionesOriginales": secciones,
        "ultimoItemExterno": None,
        "ultimoItemInterno": None,
        "texto": texto,
        "rubros": [],
        "nroNorma": nro_norma,
        "anioNorma": anio_norma,
        "denominacion": "",
        "tipoContratacion": "",
        "anioContratacion": "",
        "nroContratacion": "",
        "fechaDesde": fecha_desde.strftime("%d/%m/%Y"),
        "fechaHasta": fecha_hasta.strftime("%d/%m/%Y"),
        "todasLasPalabras": todas_las_palabras,
        "comienzaDenominacion": True,
        "seccion": secciones,
        "tipoBusqueda": "Avanzada",
        "numeroPagina": pagina,
        "ultimoRubro": "",
    }
    return json.dumps(params, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Herramientas (tools) que ve Claude
# ---------------------------------------------------------------------------
# El docstring de cada función es la descripción que lee Claude para decidir
# cuándo usarla, y los tipos de los parámetros se convierten en el esquema JSON.


@mcp.tool()
async def edicion_del_dia(
    seccion: Seccion = "primera",
    fecha: str | None = None,
    filtro: str | None = None,
    limite: int = 100,
) -> dict[str, Any]:
    """Lista los avisos publicados en una sección del Boletín Oficial para una fecha.

    Args:
        seccion: "primera" (leyes, decretos, resoluciones), "segunda" (sociedades y
            avisos judiciales) o "tercera" (contrataciones).
        fecha: fecha de la edición en formato AAAA-MM-DD. Si se omite, la edición de hoy.
        filtro: texto opcional para quedarse solo con avisos que lo contengan
            (por ejemplo "DECRETO", "MINISTERIO DE ECONOMÍA", "ANMAT").
        limite: cantidad máxima de avisos a devolver (por defecto 100).
    """
    dia = parsear_fecha(fecha)
    ruta = f"/seccion/{seccion}" if fecha is None else f"/seccion/{seccion}/{dia:%Y%m%d}"
    avisos = parsear_listado(await _get(ruta))
    total = len(avisos)
    if filtro:
        f = filtro.casefold()
        avisos = [a for a in avisos if f in a["resumen"].casefold()]
    return {
        "seccion": seccion,
        "fecha": dia.isoformat(),
        "fuente": BASE_URL + ruta,
        "total_en_la_edicion": total,
        "devueltos": len(avisos[:limite]),
        "avisos": avisos[:limite],
        "nota": None if total else "No se encontraron avisos (puede ser feriado, fin de semana o un cambio en el sitio).",
    }


@mcp.tool()
async def buscar_avisos(
    texto: str = "",
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    secciones: list[Seccion] | None = None,
    nro_norma: str = "",
    anio_norma: str = "",
    todas_las_palabras: bool = True,
    pagina: int = 1,
) -> dict[str, Any]:
    """Búsqueda avanzada en el Boletín Oficial (equivale a la "Búsqueda avanzada" del sitio).

    Args:
        texto: palabras a buscar (ej. "monotributo", "ANMAT", "Ley de Bases").
        fecha_desde: AAAA-MM-DD. Por defecto, hace 30 días.
        fecha_hasta: AAAA-MM-DD. Por defecto, hoy.
        secciones: lista de secciones a incluir. Por defecto ["primera"].
        nro_norma: número de la norma (ej. "70" para el DNU 70/2023).
        anio_norma: año de la norma (ej. "2023").
        todas_las_palabras: True = deben aparecer todas las palabras; False = cualquiera.
        pagina: página de resultados (empieza en 1). Pedí la siguiente si necesitás más.
    """
    if not (texto.strip() or nro_norma.strip()):
        raise ToolError("Indicá al menos `texto` o `nro_norma`.")
    hasta = parsear_fecha(fecha_hasta)
    desde = parsear_fecha(fecha_desde) if fecha_desde else date.fromordinal(hasta.toordinal() - 30)
    if desde > hasta:
        raise ToolError("fecha_desde no puede ser posterior a fecha_hasta.")
    nums = [NUMERO_SECCION[s] for s in (secciones or ["primera"])]
    params = armar_params_busqueda(texto, nums, desde, hasta, nro_norma, anio_norma, todas_las_palabras, pagina)

    # El sitio espera un formulario multipart con dos campos: `params` (JSON) y `array_volver`.
    campos = {"params": (None, params), "array_volver": (None, "[]")}
    try:
        async with _cliente() as cliente:
            resp = await cliente.post(
                "/busquedaAvanzada/realizarBusqueda",
                files=campos,
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            resp.raise_for_status()
            datos = resp.json()
    except httpx.HTTPStatusError as e:
        raise ToolError(f"La búsqueda devolvió {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise ToolError(f"No se pudo conectar con el Boletín Oficial: {e.__class__.__name__}: {e}") from e
    except ValueError as e:
        raise ToolError("La respuesta de la búsqueda no fue JSON (¿cambió el sitio?).") from e

    contenido = datos.get("content") if isinstance(datos, dict) else None
    html = contenido.get("html", "") if isinstance(contenido, dict) else ""
    resultados = parsear_listado(html)
    return {
        "consulta": {"texto": texto, "nro_norma": nro_norma, "anio_norma": anio_norma,
                     "desde": desde.isoformat(), "hasta": hasta.isoformat(), "pagina": pagina},
        "cantidad": len(resultados),
        "resultados": resultados,
        "nota": "Si la cantidad es alta, puede haber más resultados: pedí pagina+1." if resultados else
                "Sin resultados. Probá ampliar las fechas o usar todas_las_palabras=False.",
    }


@mcp.tool()
async def leer_aviso(
    url: str | None = None,
    seccion: Seccion | None = None,
    id_aviso: str | None = None,
    fecha: str | None = None,
    max_caracteres: int = 15000,
) -> dict[str, Any]:
    """Devuelve el texto completo de un aviso del Boletín Oficial.

    Pasá la `url` que devolvieron edicion_del_dia o buscar_avisos, o bien
    `seccion` + `id_aviso` + `fecha` (AAAA-MM-DD).

    Args:
        url: URL del aviso (https://www.boletinoficial.gob.ar/detalleAviso/...).
        seccion: sección del aviso, si no pasás la URL.
        id_aviso: número de aviso, si no pasás la URL.
        fecha: fecha de publicación AAAA-MM-DD, si no pasás la URL.
        max_caracteres: corta el texto si es más largo (por defecto 15000) para no llenar el contexto.
    """
    if url:
        url_final = _validar_url(url)
    elif seccion and id_aviso and fecha:
        if not id_aviso.isdigit():
            raise ToolError("id_aviso debe ser numérico.")
        url_final = _url_aviso(seccion, id_aviso, f"{parsear_fecha(fecha):%Y%m%d}")
    else:
        raise ToolError("Pasá `url`, o bien `seccion` + `id_aviso` + `fecha`.")

    detalle = parsear_detalle(await _get(url_final.removeprefix(BASE_URL)))
    texto = detalle["texto"]
    recortado = len(texto) > max_caracteres
    detalle["texto"] = texto[:max_caracteres] + ("\n\n[... texto recortado ...]" if recortado else "")
    detalle.update({"url": url_final, "largo_original": len(texto), "recortado": recortado})
    return detalle


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------


def main() -> None:
    if "--http" in sys.argv:
        # Modo remoto: Streamable HTTP en http://HOST:PORT/mcp
        # Con HOST=0.0.0.0 acepta cualquier dominio (necesario detrás de ngrok o un hosting).
        mcp.run(
            transport="streamable-http",
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8000")),
        )
    else:
        # Modo local: Claude Desktop / Claude Code lanzan este proceso y hablan por stdin/stdout.
        mcp.run()


if __name__ == "__main__":
    main()
