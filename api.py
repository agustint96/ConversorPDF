"""
Conversor de archivos. Tres modos, un mismo esquema de trabajos asincronicos.

  POST /convert/{tipo}        -> guarda los archivos, crea un trabajo y responde
                                  al instante con un id (202). No espera nada.
  GET  /jobs/{id}              -> estado: en_cola / procesando / completado / error
  GET  /jobs/{id}/descarga     -> descarga el resultado (solo si esta completado)
  DELETE /jobs/{id}            -> borra el trabajo y sus archivos

{tipo} es uno de: pdf-a-epub, jpg-a-pdf, doc-a-pdf (ver CONVERSIONES).

Los nombres que manda el cliente NUNCA se usan en el disco: adentro cada
archivo se llama 0000<extension>, 0001<extension>, etc., en el orden en que
llegaron (para jpg-a-pdf eso define el orden de las paginas). El nombre
original del primer archivo se guarda aparte, solo para proponerle un nombre
al navegador al momento de descargar. Eso evita dos problemas: rutas que se
pasan del limite de 260 caracteres de Windows, y nombres maliciosos que
intenten escaparse de la carpeta.

Dos limitaciones a proposito, para no complicar esta fase:
  - los trabajos viven en memoria: si reinicias el servidor se pierden
    (y con --reload el servidor se reinicia cada vez que guardas el archivo)
  - los archivos de un trabajo terminado se borran solos recien a los
    TTL_MINUTOS, o cuando haces DELETE

Levantar el servidor:
    python -m uvicorn api:app --reload
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from conversor import (
    ConversionError,
    convertir_pdf_a_epub,
    documento_a_pdf,
    imagenes_a_pdf,
    verificar_dependencias,
)

LIMITE_MB = 300               # por archivo
LIMITE_BYTES = LIMITE_MB * 1024 * 1024
LIMITE_ARCHIVOS = 300
TROZO = 1024 * 1024
TTL_MINUTOS = 60
CUPO_SIMULTANEO = 2
PAGINA = Path(__file__).parent / "index.html"
LARGO_MAXIMO_NOMBRE = 80


@dataclass(frozen=True)
class TipoConversion:
    extensiones: tuple[str, ...]
    multiple: bool
    media_type: str
    extension_salida: str
    etiqueta: str


CONVERSIONES: dict[str, TipoConversion] = {
    "pdf-a-epub": TipoConversion(
        extensiones=(".pdf",),
        multiple=False,
        media_type="application/epub+zip",
        extension_salida="epub",
        etiqueta="PDF a EPUB",
    ),
    "imagen-a-pdf": TipoConversion(
        extensiones=(".jpg", ".jpeg", ".png"),
        multiple=True,
        media_type="application/pdf",
        extension_salida="pdf",
        etiqueta="Imagen a PDF",
    ),
    "doc-a-pdf": TipoConversion(
        extensiones=(".txt", ".docx", ".odt", ".rtf"),
        multiple=False,
        media_type="application/pdf",
        extension_salida="pdf",
        etiqueta="Documento a PDF",
    ),
}

app = FastAPI(title="Conversor de archivos")


# --------------------------------------------------------------------------
# Registro de trabajos
# --------------------------------------------------------------------------

@dataclass
class Trabajo:
    id: str
    tipo: str
    nombre: str           # nombre para mostrar y proponer al descargar
    cantidad: int          # cuantos archivos tiene el trabajo
    carpeta: Path
    creado: datetime = field(default_factory=datetime.now)
    estado: str = "en_cola"
    resultado: Path | None = None
    error: str | None = None


_trabajos: dict[str, Trabajo] = {}
_candado = threading.Lock()
_ejecutor = ThreadPoolExecutor(max_workers=CUPO_SIMULTANEO)


def _nombre_descarga(nombre_original: str, extension: str) -> str:
    """Nombre corto y sin caracteres prohibidos, para el archivo que baja el cliente."""
    base = Path(nombre_original).stem
    base = re.sub(r'[\\/:*?"<>|]', "_", base).strip()
    base = base[:LARGO_MAXIMO_NOMBRE].rstrip(" .") or "salida"
    return f"{base}.{extension}"


def _resumen(trabajo: Trabajo) -> dict:
    datos = {
        "id": trabajo.id,
        "tipo": trabajo.tipo,
        "estado": trabajo.estado,
        "archivo": trabajo.nombre,
        "cantidad": trabajo.cantidad,
        "creado": trabajo.creado.isoformat(timespec="seconds"),
    }
    if trabajo.estado == "completado":
        datos["descarga"] = f"/jobs/{trabajo.id}/descarga"
    if trabajo.estado == "error":
        datos["error"] = trabajo.error
    return datos


def _buscar(id_trabajo: str) -> Trabajo:
    with _candado:
        trabajo = _trabajos.get(id_trabajo)
    if trabajo is None:
        raise HTTPException(status_code=404, detail="No existe ese trabajo.")
    return trabajo


def _purgar_vencidos() -> None:
    """Borra los trabajos terminados que ya pasaron su tiempo de vida."""
    limite = datetime.now() - timedelta(minutes=TTL_MINUTOS)
    with _candado:
        vencidos = [
            clave
            for clave, trabajo in _trabajos.items()
            if trabajo.creado < limite and trabajo.estado in ("completado", "error")
        ]
        for clave in vencidos:
            shutil.rmtree(_trabajos.pop(clave).carpeta, ignore_errors=True)


# --------------------------------------------------------------------------
# El trabajo en si
# --------------------------------------------------------------------------

def _procesar(id_trabajo: str) -> None:
    """Corre en un hilo aparte. Nunca debe dejar escapar una excepcion."""
    trabajo = _trabajos.get(id_trabajo)
    if trabajo is None:
        return

    with _candado:
        trabajo.estado = "procesando"

    try:
        entrada = sorted((trabajo.carpeta / "entrada").iterdir())
        if trabajo.tipo == "pdf-a-epub":
            resultado = convertir_pdf_a_epub(entrada[0], trabajo.carpeta)
        elif trabajo.tipo == "imagen-a-pdf":
            resultado = imagenes_a_pdf(entrada, trabajo.carpeta / "salida.pdf")
        elif trabajo.tipo == "doc-a-pdf":
            resultado = documento_a_pdf(entrada[0], trabajo.carpeta / "salida.pdf")
        else:
            raise ConversionError(f"Tipo de conversion desconocido: {trabajo.tipo}")
    except ConversionError as error:
        with _candado:
            trabajo.estado = "error"
            trabajo.error = str(error)
    except Exception as error:  # cualquier otra cosa inesperada
        with _candado:
            trabajo.estado = "error"
            trabajo.error = f"Error inesperado: {error}"
    else:
        with _candado:
            trabajo.estado = "completado"
            trabajo.resultado = resultado


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def pagina() -> HTMLResponse:
    """Sirve el frontend desde el mismo origen que la API, asi no hay CORS."""
    if not PAGINA.is_file():
        raise HTTPException(
            status_code=404,
            detail="Falta index.html en la misma carpeta que api.py.",
        )
    return HTMLResponse(PAGINA.read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict:
    _purgar_vencidos()
    try:
        herramientas = verificar_dependencias()
    except ConversionError as error:
        return {"estado": "faltan herramientas", "detalle": str(error)}

    with _candado:
        activos = len(_trabajos)
    return {
        "estado": "ok",
        "limite_mb": LIMITE_MB,
        "trabajos_en_memoria": activos,
        "herramientas": herramientas,
    }


def _guardar_archivo(archivo: UploadFile, destino: Path) -> None:
    total = 0
    with destino.open("wb") as salida:
        while trozo := archivo.file.read(TROZO):
            total += len(trozo)
            if total > LIMITE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"'{archivo.filename}' supera el limite de {LIMITE_MB} MB.",
                )
            salida.write(trozo)

    if total == 0:
        raise HTTPException(
            status_code=400, detail=f"'{archivo.filename}' esta vacio."
        )


@app.post("/convert/{tipo}", status_code=202)
def crear_trabajo(tipo: str, archivos: list[UploadFile] = File(...)) -> dict:
    _purgar_vencidos()

    config = CONVERSIONES.get(tipo)
    if config is None:
        raise HTTPException(status_code=404, detail=f"Conversion desconocida: {tipo}.")

    if not archivos:
        raise HTTPException(status_code=400, detail="No se recibio ningun archivo.")
    if not config.multiple and len(archivos) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"'{config.etiqueta}' admite un solo archivo por vez.",
        )
    if len(archivos) > LIMITE_ARCHIVOS:
        raise HTTPException(
            status_code=400,
            detail=f"Como mucho {LIMITE_ARCHIVOS} archivos por trabajo.",
        )

    extensiones_por_archivo = []
    for archivo in archivos:
        nombre = Path(archivo.filename or "").name
        extension = Path(nombre).suffix.lower()
        if extension not in config.extensiones:
            raise HTTPException(
                status_code=400,
                detail=f"'{nombre}' no es un formato valido para '{config.etiqueta}' "
                f"({', '.join(config.extensiones)}).",
            )
        extensiones_por_archivo.append(extension)

    carpeta = Path(tempfile.mkdtemp(prefix="convertir_"))
    try:
        entrada = carpeta / "entrada"
        entrada.mkdir()
        # Ojo: se guardan con nombre fijo y numerado, no con el que vino del
        # cliente, para conservar el orden y evitar nombres peligrosos. La
        # extension original se mantiene porque Calibre la usa para detectar
        # el formato de entrada.
        for indice, (archivo, extension) in enumerate(
            zip(archivos, extensiones_por_archivo)
        ):
            _guardar_archivo(archivo, entrada / f"{indice:04d}{extension}")
    except Exception:
        shutil.rmtree(carpeta, ignore_errors=True)
        raise

    primero = Path(archivos[0].filename or "salida").name
    trabajo = Trabajo(
        id=uuid.uuid4().hex[:12],
        tipo=tipo,
        nombre=primero,
        cantidad=len(archivos),
        carpeta=carpeta,
    )
    with _candado:
        _trabajos[trabajo.id] = trabajo

    _ejecutor.submit(_procesar, trabajo.id)

    datos = _resumen(trabajo)
    datos["consultar"] = f"/jobs/{trabajo.id}"
    return datos


@app.get("/jobs/{id_trabajo}")
def ver_trabajo(id_trabajo: str) -> dict:
    return _resumen(_buscar(id_trabajo))


@app.get("/jobs/{id_trabajo}/descarga")
def descargar(id_trabajo: str) -> FileResponse:
    trabajo = _buscar(id_trabajo)
    config = CONVERSIONES[trabajo.tipo]

    if trabajo.estado != "completado" or trabajo.resultado is None:
        raise HTTPException(
            status_code=409,
            detail=f"El trabajo todavia no esta listo (estado: {trabajo.estado}).",
        )
    if not trabajo.resultado.is_file():
        raise HTTPException(status_code=410, detail="El resultado ya fue borrado.")

    return FileResponse(
        path=trabajo.resultado,
        media_type=config.media_type,
        filename=_nombre_descarga(trabajo.nombre, config.extension_salida),
    )


@app.delete("/jobs/{id_trabajo}", status_code=204)
def borrar_trabajo(id_trabajo: str) -> None:
    trabajo = _buscar(id_trabajo)
    if trabajo.estado == "procesando":
        raise HTTPException(
            status_code=409,
            detail="No se puede borrar un trabajo mientras se esta convirtiendo.",
        )
    with _candado:
        _trabajos.pop(id_trabajo, None)
    shutil.rmtree(trabajo.carpeta, ignore_errors=True)
