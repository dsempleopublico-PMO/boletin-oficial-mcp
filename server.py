"""
MCP del Boletín Oficial de la República Argentina (BORA) — v0.2
================================================================

Le da a Claude tres herramientas:

  1. edicion_del_dia    -> sumario de una edición (decretos, resoluciones, etc.)
  2. leer_norma         -> texto completo de una norma de esa edición
  3. buscar_en_boletin  -> busca palabras en las ediciones de un rango de fechas

De dónde saca los datos
-----------------------
El sitio principal (www.boletinoficial.gob.ar) no acepta conexiones desde
servidores en la nube fuera de Argentina. Por eso este servidor usa el listado
oficial de PDFs del Boletín (https://otslist.boletinoficial.gob.ar/ots/), donde
se publica el PDF de cada sección de cada día junto con su hash SHA-256.
El servidor descarga el PDF, verifica el hash, extrae el texto y lo ordena
usando el SUMARIO que trae la propia edición.

Uso:
    python server.py            # modo local (stdio)
    python server.py --http     # modo remoto (HTTP), para el conector de claude.ai
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

import anyio
import pypdfium2 as pdfium
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

OTS_URL = "https://otslist.boletinoficial.gob.ar"
Seccion = Literal["primera", "segunda", "tercera", "cuarta"]
NUMERO_SECCION = {"primera": "1", "segunda": "2", "tercera": "3", "cuarta": "4"}
HEADERS = {"User-Agent": "boletin-oficial-mcp/0.2 (consulta personal via Claude)"}
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
MAX_DIAS_BUSQUEDA = 31
DESCARGAS_EN_PARALELO = 3

# Los tests reemplazan esto por un transporte falso para no salir a internet.
_transport: Any = None

mcp = MCPServer(
    name="boletin-oficial",
    title="Boletín Oficial de la República Argentina",
    instructions=(
        "Herramientas para consultar el Boletín Oficial de la República Argentina a partir de los "
        "PDF oficiales de cada edición. Secciones: primera = legislación y avisos oficiales (leyes, "
        "decretos, resoluciones, disposiciones); segunda = sociedades y avisos judiciales; tercera = "
        "contrataciones; cuarta = dominios de internet. Flujo típico: edicion_del_dia para ver el "
        "sumario, leer_norma para el texto completo (por ejemplo 'Decreto 1286/2026'), y "
        "buscar_en_boletin para encontrar un tema en varios días. Citá siempre la fecha, la página "
        "y el enlace al PDF oficial."
    ),
)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def parsear_fecha(valor: str | None) -> date | None:
    """Acepta 'AAAA-MM-DD', 'DD/MM/AAAA' o 'DD-MM-AAAA'. Vacío = None."""
    if not valor:
        return None
    valor = valor.strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(valor, formato).date()
        except ValueError:
            continue
    raise ToolError(f"Fecha inválida: {valor!r}. Usá el formato AAAA-MM-DD (ej. 2026-10-27).")


_SIN_TILDES = str.maketrans("áéíóúüñàèìòùâêîôûäëïöçºª", "aeiouunaeiouaeiouaeiocoa")


def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar sin importar cómo se escriba (rápido)."""
    return re.sub(r"\s+", " ", texto.casefold().translate(_SIN_TILDES)).strip()


def normalizar_con_mapa(texto: str) -> tuple[str, list[int]]:
    """Como normalizar(), pero recuerda de qué posición del texto original viene cada letra.
    Es más lento, así que solo se usa en las páginas donde ya sabemos que hay coincidencia."""
    plano: list[str] = []
    mapa: list[int] = []
    for i, c in enumerate(texto):
        if c.isspace():
            if plano and plano[-1] != " ":
                plano.append(" ")
                mapa.append(i)
            continue
        for n in c.casefold().translate(_SIN_TILDES):
            plano.append(n)
            mapa.append(i)
    return "".join(plano), mapa


def _cliente() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=OTS_URL, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True, transport=_transport
    )


async def _get(ruta: str, params: dict[str, str] | None = None) -> httpx.Response:
    try:
        async with _cliente() as cliente:
            resp = await cliente.get(ruta, params=params)
            resp.raise_for_status()
            return resp
    except httpx.HTTPStatusError as e:
        raise ToolError(f"El servidor del Boletín respondió {e.response.status_code} para {ruta}") from e
    except httpx.HTTPError as e:
        raise ToolError(f"No se pudo conectar con el Boletín Oficial: {e.__class__.__name__}: {e}") from e


# ---------------------------------------------------------------------------
# 1) Listado de PDFs por fecha
# ---------------------------------------------------------------------------


def parsear_listado(html: str) -> dict[date, dict[str, str]]:
    """Devuelve {fecha: {"1": hash, "2": hash, ...}} a partir del HTML de /ots/."""
    soup = BeautifulSoup(html, "html.parser")
    ediciones: dict[date, dict[str, str]] = {}
    for h4 in soup.find_all("h4"):
        try:
            dia = datetime.strptime(h4.get_text(strip=True), "%d-%m-%Y").date()
        except ValueError:
            continue
        tabla = h4.find_next("table")
        if tabla is None:
            continue
        secciones: dict[str, str] = {}
        for fila in tabla.find_all("tr"):
            celdas = fila.find_all("td")
            if len(celdas) < 2:
                continue
            numero = celdas[0].get_text(strip=True)
            hash_pdf = celdas[1].get_text(strip=True)
            if numero and re.fullmatch(r"[0-9a-f]{64}", hash_pdf):
                secciones[numero] = hash_pdf
        ediciones[dia] = secciones
    return ediciones


_cache_listado: dict[str, tuple[float, dict[date, dict[str, str]]]] = {}


async def listado(dia: date | None) -> dict[date, dict[str, str]]:
    """Listado de una fecha (o el más reciente si dia es None). Cachea 10 minutos."""
    clave = dia.isoformat() if dia else "ultimo"
    guardado = _cache_listado.get(clave)
    if guardado and time.time() - guardado[0] < 600:
        return guardado[1]
    params = {"date_filter": dia.strftime("%d-%m-%Y")} if dia else None
    resp = await _get("/ots/", params=params)
    datos = parsear_listado(resp.text)
    _cache_listado[clave] = (time.time(), datos)
    return datos


async def resolver_edicion(fecha: str | None, seccion: str) -> tuple[date, str]:
    """Devuelve (fecha, hash del PDF) para la sección pedida. Sin fecha = edición más reciente."""
    dia = parsear_fecha(fecha)
    datos = await listado(dia)
    if dia and not datos:
        # Fin de semana o feriado: buscamos la edición anterior más cercana (hasta 7 días antes).
        for atras in range(1, 8):
            datos = await listado(dia - timedelta(days=atras))
            if datos:
                break
    if not datos:
        cuando = f"el {dia:%d/%m/%Y}" if dia else "hoy"
        raise ToolError(
            f"No hay edición del Boletín Oficial {cuando} (puede ser fin de semana o feriado). "
            "Probá con otra fecha, o sin fecha para la edición más reciente."
        )
    dia_encontrado = dia if dia in datos else max(datos)
    numero = NUMERO_SECCION[seccion]
    hash_pdf = datos[dia_encontrado].get(numero)
    if not hash_pdf:
        raise ToolError(f"La edición del {dia_encontrado:%d/%m/%Y} no tiene {seccion} sección.")
    return dia_encontrado, hash_pdf


def url_pdf(hash_pdf: str) -> str:
    return f"{OTS_URL}/ots/download/{hash_pdf}/0/"


# ---------------------------------------------------------------------------
# 2) PDF -> texto por página (con caché)
# ---------------------------------------------------------------------------

_cache_paginas: OrderedDict[str, list[str]] = OrderedDict()
_CACHE_MAX = 10
_pdfium_lock = threading.Lock()  # pdfium no es thread-safe
_descargas: dict[str, asyncio.Future] = {}


def extraer_paginas(pdf: bytes) -> list[str]:
    with _pdfium_lock:
        doc = pdfium.PdfDocument(pdf)
        try:
            return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
        finally:
            doc.close()


async def paginas_de(hash_pdf: str) -> list[str]:
    """Descarga el PDF (si no está en caché), verifica el hash y devuelve el texto de cada página."""
    if hash_pdf in _cache_paginas:
        _cache_paginas.move_to_end(hash_pdf)
        return _cache_paginas[hash_pdf]
    if hash_pdf in _descargas:  # otra llamada ya lo está bajando
        return await _descargas[hash_pdf]
    futuro: asyncio.Future = asyncio.get_running_loop().create_future()
    _descargas[hash_pdf] = futuro
    try:
        resp = await _get(f"/ots/download/{hash_pdf}/0/")
        contenido = resp.content
        if hashlib.sha256(contenido).hexdigest() != hash_pdf:
            raise ToolError("El PDF descargado no coincide con el hash publicado por el Boletín.")
        paginas = await anyio.to_thread.run_sync(extraer_paginas, contenido)
        _cache_paginas[hash_pdf] = paginas
        while len(_cache_paginas) > _CACHE_MAX:
            _cache_paginas.popitem(last=False)
        futuro.set_result(paginas)
        return paginas
    except BaseException as e:
        if isinstance(e, Exception):
            futuro.set_exception(e)
            futuro.exception()  # marca la excepción como leída
        else:
            futuro.cancel()  # la llamada se canceló: las que esperaban también
        raise
    finally:
        _descargas.pop(hash_pdf, None)


# ---------------------------------------------------------------------------
# 3) Sumario: la lista de normas con su página
# ---------------------------------------------------------------------------

# Una entrada termina con puntos suspensivos (o ". ") y el número de página.
RE_ENTRADA = re.compile(r"^(?P<titulo>.*?\S)[\s.]*\.\s+(?P<pagina>\d{1,4})\s*$")
# "Decreto 1286/2026", "Resolución General 5917/2026", "Decisión Administrativa 29/2026"...
RE_NORMA = re.compile(r"([A-ZÁÉÍÓÚ][A-Za-zÁÉÍÓÚáéíóúñÑ ]{2,60}?\s\d{1,6}/\d{4})")
RE_ENCABEZADO = re.compile(r"^BOLET[IÍ]N OFICIAL N", re.IGNORECASE)


@dataclass
class Entrada:
    categoria: str
    grupo: str
    titulo: str
    pagina: int
    norma: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def como_dict(self) -> dict[str, Any]:
        return {"categoria": self.categoria, "titulo": self.titulo, "norma": self.norma or None,
                "pagina": self.pagina, "avisos": self.grupo}


def _es_titulo_de_categoria(linea: str) -> bool:
    return "." not in linea and len(linea) <= 60 and any(c.islower() for c in linea)


def parsear_sumario(paginas: list[str]) -> list[Entrada]:
    """Lee el SUMARIO de las primeras páginas y devuelve las entradas en orden."""
    entradas: list[Entrada] = []
    categoria, grupo = "", "nuevos"
    pendiente = ""
    primeras = paginas[:8]
    # Empezamos a leer en la línea "SUMARIO" (si no aparece, desde la página 2).
    en_sumario = not any(l.strip().upper() == "SUMARIO" for t in primeras for l in t.splitlines())
    for i, texto in enumerate(primeras):
        if i == 0 and en_sumario:
            continue  # sin "SUMARIO": salteamos la tapa
        if i > 0 and entradas and i + 1 >= min(e.pagina for e in entradas):
            break  # ya llegamos a la primera página con contenido
        for linea in texto.splitlines():
            linea = linea.strip()
            if linea.upper() == "SUMARIO":
                en_sumario = True
                continue
            if not en_sumario or not linea or RE_ENCABEZADO.match(linea):
                continue
            if normalizar(linea) in ("avisos nuevos", "avisos anteriores"):
                grupo = "nuevos" if "nuevos" in normalizar(linea) else "anteriores"
                pendiente = ""
                continue
            m = RE_ENTRADA.match(linea)
            if m:
                titulo = re.sub(r"\s+", " ", f"{pendiente} {m['titulo']}").strip().strip(" .")
                titulo = titulo or categoria  # ej. "Avisos Oficiales ........ 103"
                pendiente = ""
                norma = RE_NORMA.findall(titulo)
                entradas.append(Entrada(categoria, grupo, titulo, int(m["pagina"]),
                                        norma[0].strip() if norma else ""))
            elif _es_titulo_de_categoria(linea) and not pendiente:
                categoria = linea
            else:
                pendiente = f"{pendiente} {linea}".strip()  # título largo partido en 2 líneas
    return entradas


def numero_de_edicion(paginas: list[str]) -> str | None:
    m = re.search(r"N[úu]mero\s+([\d.]+)", paginas[0] if paginas else "")
    return m.group(1) if m else None


def _ubicar(paginas: list[str], etiqueta: str, desde_pagina: int) -> tuple[int, int] | None:
    """Busca la línea exacta de una norma (ej. 'Decreto 1286/2026') desde una página."""
    objetivo = normalizar(etiqueta)
    for idx in range(max(desde_pagina - 1, 0), min(desde_pagina + 2, len(paginas))):
        for n, linea in enumerate(paginas[idx].splitlines()):
            if normalizar(linea) == objetivo:
                return idx, n
    return None


def texto_de_entrada(paginas: list[str], entradas: list[Entrada], i: int) -> str:
    """Recorta del PDF el texto de la entrada i (hasta donde empieza la siguiente)."""
    actual = entradas[i]
    siguiente = entradas[i + 1] if i + 1 < len(entradas) else None
    ini_pag = actual.pagina - 1
    fin_pag = (siguiente.pagina - 1) if siguiente else min(ini_pag + 5, len(paginas) - 1)
    fin_pag = max(fin_pag, ini_pag)

    ini_linea = 0
    if actual.norma and (pos := _ubicar(paginas, actual.norma, actual.pagina)):
        ini_pag, ini_linea = pos[0], max(pos[1] - 1, 0)  # incluye el organismo
    fin_linea = None
    if siguiente and siguiente.norma and (pos := _ubicar(paginas, siguiente.norma, siguiente.pagina)):
        fin_pag, fin_linea = pos[0], max(pos[1] - 1, 0)

    trozos: list[str] = []
    for idx in range(ini_pag, fin_pag + 1):
        lineas = paginas[idx].splitlines()
        if lineas and RE_ENCABEZADO.match(lineas[0].strip()):
            lineas = lineas[1:]  # saca el encabezado de cada página
            desplazo = 1
        else:
            desplazo = 0
        desde = max(ini_linea - desplazo, 0) if idx == ini_pag else 0
        hasta = (fin_linea - desplazo) if (idx == fin_pag and fin_linea is not None) else len(lineas)
        trozos.append("\n".join(lineas[desde:max(hasta, desde)]))
    return "\n".join(t for t in trozos if t.strip()).strip()


async def edicion(fecha: str | None, seccion: str) -> tuple[date, str, list[str], list[Entrada]]:
    dia, hash_pdf = await resolver_edicion(fecha, seccion)
    paginas = await paginas_de(hash_pdf)
    return dia, hash_pdf, paginas, parsear_sumario(paginas)


# ---------------------------------------------------------------------------
# Herramientas (tools) que ve Claude
# ---------------------------------------------------------------------------
# El docstring de cada función es la descripción que lee Claude para decidir
# cuándo usarla; los tipos de los parámetros se convierten en el esquema JSON.


@mcp.tool()
async def edicion_del_dia(
    fecha: str | None = None,
    seccion: Seccion = "primera",
    filtro: str | None = None,
) -> dict[str, Any]:
    """Devuelve el sumario de una edición del Boletín Oficial: cada decreto, resolución,
    disposición o aviso con su organismo, número y página.

    Args:
        fecha: AAAA-MM-DD. Si se omite, la edición más reciente.
        seccion: "primera" (leyes, decretos, resoluciones), "segunda" (sociedades y avisos
            judiciales), "tercera" (contrataciones) o "cuarta" (dominios).
        filtro: texto opcional para quedarse solo con lo que lo contenga
            (ej. "Decreto", "ANSES", "Ministerio de Economía"). No distingue tildes.
    """
    dia, hash_pdf, paginas, entradas = await edicion(fecha, seccion)
    lista = entradas
    if filtro:
        f = normalizar(filtro)
        lista = [e for e in entradas if f in normalizar(f"{e.categoria} {e.titulo}")]
    pedido = parsear_fecha(fecha)
    return {
        "fecha": dia.isoformat(),
        "aviso_fecha": (f"No hubo edición el {pedido:%d/%m/%Y}; se muestra la del {dia:%d/%m/%Y}."
                        if pedido and pedido != dia else None),
        "seccion": seccion,
        "edicion_numero": numero_de_edicion(paginas),
        "paginas_pdf": len(paginas),
        "pdf_oficial": url_pdf(hash_pdf),
        "total_en_sumario": len(entradas),
        "devueltos": len(lista),
        "entradas": [e.como_dict() for e in lista],
        "nota": None if entradas else
                "No se pudo leer un sumario en esta sección. Usá buscar_en_boletin o leer_norma con pagina.",
    }


@mcp.tool()
async def leer_norma(
    consulta: str | None = None,
    fecha: str | None = None,
    seccion: Seccion = "primera",
    pagina: int | None = None,
    max_caracteres: int = 15000,
) -> dict[str, Any]:
    """Devuelve el texto completo de una norma o aviso de una edición del Boletín Oficial.

    Args:
        consulta: qué norma leer, como aparece en el sumario. Ej: "Decreto 1286/2026",
            "Resolución 303/2026", "ANSES 304", "DNU". Se eligen las entradas que contengan
            todas las palabras.
        fecha: AAAA-MM-DD de la edición. Si se omite, la edición más reciente.
        seccion: sección de la edición (por defecto "primera").
        pagina: alternativa a `consulta`: devuelve el texto de esa página del PDF.
        max_caracteres: corta el texto si es más largo (por defecto 15000).
    """
    if not consulta and not pagina:
        raise ToolError("Indicá `consulta` (ej. 'Decreto 1286/2026') o `pagina`.")
    dia, hash_pdf, paginas, entradas = await edicion(fecha, seccion)

    if pagina:
        if not 1 <= pagina <= len(paginas):
            raise ToolError(f"La edición tiene {len(paginas)} páginas.")
        texto, titulo, encontrada = paginas[pagina - 1], f"Página {pagina}", None
    else:
        palabras = normalizar(consulta or "").replace("nº", " ").replace("n°", " ").split()
        candidatos = [i for i, e in enumerate(entradas)
                      if all(p in normalizar(f"{e.categoria} {e.titulo}") for p in palabras)]
        if not candidatos:
            raise ToolError(
                f"No encontré '{consulta}' en el sumario del {dia:%d/%m/%Y}. "
                "Revisá el sumario con edicion_del_dia o usá buscar_en_boletin."
            )
        if len(candidatos) > 1:
            exactas = [i for i in candidatos if normalizar(entradas[i].norma) == normalizar(consulta or "")]
            if len(exactas) == 1:
                candidatos = exactas
            else:
                return {
                    "fecha": dia.isoformat(),
                    "mensaje": f"Hay {len(candidatos)} entradas que coinciden. Pedí una más específica.",
                    "coincidencias": [entradas[i].como_dict() for i in candidatos[:30]],
                }
        encontrada = entradas[candidatos[0]]
        texto = texto_de_entrada(paginas, entradas, candidatos[0])
        titulo = encontrada.titulo

    recortado = len(texto) > max_caracteres
    return {
        "fecha": dia.isoformat(),
        "seccion": seccion,
        "titulo": titulo,
        "categoria": encontrada.categoria if encontrada else None,
        "pagina": encontrada.pagina if encontrada else pagina,
        "pdf_oficial": url_pdf(hash_pdf),
        "texto": texto[:max_caracteres] + ("\n\n[... texto recortado ...]" if recortado else ""),
        "largo_original": len(texto),
        "recortado": recortado,
    }


@mcp.tool()
async def buscar_en_boletin(
    texto: str,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    seccion: Seccion = "primera",
    solo_sumario: bool = False,
    max_resultados: int = 30,
) -> dict[str, Any]:
    """Busca palabras en las ediciones del Boletín Oficial de un rango de fechas
    (texto completo de los PDF oficiales). No distingue mayúsculas ni tildes.

    Args:
        texto: palabras o frase a buscar (ej. "monotributo", "ANMAT", "Ley 27.430").
            La primera búsqueda de varios días puede tardar hasta un minuto (baja los PDF).
        fecha_desde: AAAA-MM-DD. Por defecto, 7 días antes de fecha_hasta.
        fecha_hasta: AAAA-MM-DD. Por defecto, hoy.
        seccion: sección donde buscar (por defecto "primera").
        solo_sumario: True para buscar solo en los títulos del sumario (más preciso).
        max_resultados: tope de coincidencias a devolver (por defecto 30).
    """
    if len(texto.strip()) < 3:
        raise ToolError("Indicá un texto de al menos 3 letras.")
    hasta = parsear_fecha(fecha_hasta) or date.today()
    desde = parsear_fecha(fecha_desde) or hasta - timedelta(days=7)
    if desde > hasta:
        raise ToolError("fecha_desde no puede ser posterior a fecha_hasta.")
    if (hasta - desde).days > MAX_DIAS_BUSQUEDA:
        raise ToolError(f"El rango máximo es de {MAX_DIAS_BUSQUEDA} días. Partí la búsqueda en tramos.")

    dias = [desde + timedelta(days=n) for n in range((hasta - desde).days + 1)]
    dias = [d for d in dias if d.weekday() < 5]  # el Boletín sale de lunes a viernes
    limite = asyncio.Semaphore(DESCARGAS_EN_PARALELO)
    numero = NUMERO_SECCION[seccion]

    async def un_dia(d: date) -> tuple[date, str, list[str]] | None:
        async with limite:
            datos = await listado(d)
            hash_pdf = datos.get(d, {}).get(numero)
            if not hash_pdf:
                return None
            return d, hash_pdf, await paginas_de(hash_pdf)

    ediciones = [r for r in await asyncio.gather(*(un_dia(d) for d in dias)) if r]
    objetivo = normalizar(texto)
    resultados: list[dict[str, Any]] = []
    for d, hash_pdf, paginas in sorted(ediciones, key=lambda r: r[0], reverse=True):
        entradas = parsear_sumario(paginas)
        if solo_sumario:
            for e in entradas:
                if objetivo in normalizar(f"{e.categoria} {e.titulo}"):
                    resultados.append({"fecha": d.isoformat(), **e.como_dict(), "pdf_oficial": url_pdf(hash_pdf)})
            continue
        inicio_contenido = min((e.pagina for e in entradas), default=1)
        for idx, pag in enumerate(paginas):
            if idx + 1 < inicio_contenido:
                continue  # saltea tapa y sumario
            if objetivo not in normalizar(pag):
                continue
            plano, mapa = normalizar_con_mapa(pag)
            pos = plano.find(objetivo)
            if pos < 0:
                continue
            norma = max((e for e in entradas if e.pagina <= idx + 1), key=lambda e: e.pagina, default=None)
            ini = mapa[pos]
            fin = mapa[min(pos + len(objetivo), len(mapa) - 1)]
            fragmento = re.sub(r"\s+", " ", pag[max(ini - 160, 0): fin + 160]).strip()
            resultados.append({
                "fecha": d.isoformat(),
                "pagina": idx + 1,
                "dentro_de": norma.titulo if norma else None,
                "fragmento": f"…{fragmento}…",
                "pdf_oficial": url_pdf(hash_pdf),
            })
    total = len(resultados)
    return {
        "consulta": {"texto": texto, "desde": desde.isoformat(), "hasta": hasta.isoformat(), "seccion": seccion},
        "ediciones_revisadas": sorted(d.isoformat() for d, _, _ in ediciones),
        "total": total,
        "resultados": resultados[:max_resultados],
        "nota": (f"Se muestran {max_resultados} de {total}. Acotá las fechas o el texto." if total > max_resultados
                 else None if total else "Sin coincidencias. Probá con otras palabras o un rango más amplio."),
    }


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------


def main() -> None:
    if "--http" in sys.argv:
        # Modo remoto: Streamable HTTP en http://HOST:PORT/mcp
        mcp.run(
            transport="streamable-http",
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8000")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
