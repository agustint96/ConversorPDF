"""
Motor de conversion. Tres modos, sin relacion entre si mas que compartir
las mismas herramientas externas:

  1) PDF -> EPUB    : ImageMagick (portada) + Calibre ebook-convert
  2) JPG -> PDF      : ImageMagick junta varias imagenes en un PDF multipagina
  3) Documento -> PDF: Calibre ebook-convert (TXT, DOCX, ODT, RTF)

Uso rapido:
    python conversor.py pdf-a-epub libro.pdf ./salida/libro.epub
    python conversor.py jpg-a-pdf pagina1.jpg pagina2.jpg ./salida/libro.pdf
    python conversor.py doc-a-pdf informe.docx ./salida/informe.pdf
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

TIMEOUT_PORTADA = 120       # segundos
TIMEOUT_EPUB = 1800         # 30 minutos
TIMEOUT_JPG_A_PDF = 300     # alcanza de sobra para varias paginas
TIMEOUT_DOCUMENTO = 600     # 10 minutos


class ConversionError(RuntimeError):
    """Error controlado del motor de conversion."""


# --------------------------------------------------------------------------
# Dependencias externas
# --------------------------------------------------------------------------

def _binario_imagemagick() -> str:
    # ImageMagick 7 usa "magick"; el 6 usa "convert".
    for nombre in ("magick", "convert"):
        ruta = shutil.which(nombre)
        if not ruta:
            continue
        # En Windows existe un convert.exe del sistema que no tiene nada que
        # ver con ImageMagick: hay que ignorarlo.
        if nombre == "convert" and "windows" in ruta.lower():
            continue
        return ruta
    raise ConversionError(
        "No se encontro ImageMagick en el PATH (probe con 'magick' y 'convert')."
    )


def _binario_calibre() -> str:
    ruta = shutil.which("ebook-convert")
    if not ruta:
        raise ConversionError("No se encontro 'ebook-convert' (Calibre) en el PATH.")
    return ruta


def verificar_dependencias() -> dict[str, str]:
    """Devuelve donde esta cada herramienta. Sirve para chequear antes de arrancar."""
    return {
        "imagemagick": _binario_imagemagick(),
        "ebook-convert": _binario_calibre(),
    }


# --------------------------------------------------------------------------
# Ejecucion de comandos
# --------------------------------------------------------------------------

def _ejecutar(comando: list[str], timeout: int) -> None:
    try:
        proceso = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise ConversionError(f"No se pudo ejecutar {comando[0]}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise ConversionError(
            f"{Path(comando[0]).name} supero el limite de {timeout} segundos."
        ) from error

    if proceso.returncode != 0:
        detalle = (proceso.stderr or proceso.stdout or "").strip()
        raise ConversionError(
            f"{Path(comando[0]).name} fallo (codigo {proceso.returncode}):\n"
            f"{detalle[-2000:]}"
        )


# --------------------------------------------------------------------------
# 1) PDF -> EPUB
# --------------------------------------------------------------------------

def extraer_portada(
    pdf: Path | str,
    destino: Path | str,
    dpi: int = 150,
    calidad: int = 85,
) -> Path:
    """Renderiza la primera pagina del PDF como imagen."""
    pdf = Path(pdf)
    destino = Path(destino)

    if not pdf.is_file():
        raise ConversionError(f"No existe el PDF: {pdf}")

    destino.parent.mkdir(parents=True, exist_ok=True)

    comando = [
        _binario_imagemagick(),
        "-density", str(dpi),
        f"{pdf}[0]",             # [0] = primera pagina
        "-background", "white",  # los PDF con transparencia salen negros sin esto
        "-alpha", "remove",
        "-alpha", "off",
        "-quality", str(calidad),
        str(destino),
    ]
    _ejecutar(comando, TIMEOUT_PORTADA)

    if not destino.is_file():
        raise ConversionError("ImageMagick termino bien pero no genero la portada.")
    return destino


def pdf_a_epub(
    pdf: Path | str,
    destino: Path | str,
    portada: Path | str | None = None,
    titulo: str | None = None,
    autor: str | None = None,
) -> Path:
    """Convierte el PDF a EPUB con Calibre, opcionalmente incrustando la portada."""
    pdf = Path(pdf)
    destino = Path(destino)

    if not pdf.is_file():
        raise ConversionError(f"No existe el PDF: {pdf}")

    destino.parent.mkdir(parents=True, exist_ok=True)

    comando = [_binario_calibre(), str(pdf), str(destino)]
    if portada:
        comando += ["--cover", str(portada)]
    if titulo:
        comando += ["--title", titulo]
    if autor:
        comando += ["--authors", autor]

    _ejecutar(comando, TIMEOUT_EPUB)

    if not destino.is_file():
        raise ConversionError("ebook-convert termino bien pero no genero el EPUB.")
    return destino


def convertir_pdf_a_epub(pdf: Path | str, carpeta_salida: Path | str) -> Path:
    pdf = Path(pdf)
    carpeta_salida = Path(carpeta_salida)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    portada = extraer_portada(pdf, carpeta_salida / "portada.jpg")
    return pdf_a_epub(pdf, carpeta_salida / "salida.epub", portada=portada)


# --------------------------------------------------------------------------
# 2) JPG -> PDF
# --------------------------------------------------------------------------

def imagenes_a_pdf(
    imagenes: list[Path | str],
    destino: Path | str,
    calidad: int = 90,
) -> Path:
    """Junta las imagenes en un solo PDF, una pagina por imagen, en orden."""
    if not imagenes:
        raise ConversionError("No hay imagenes para convertir.")

    rutas = [Path(imagen) for imagen in imagenes]
    for ruta in rutas:
        if not ruta.is_file():
            raise ConversionError(f"No existe la imagen: {ruta}")

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)

    comando = [
        _binario_imagemagick(),
        *[str(ruta) for ruta in rutas],
        "-background", "white",  # las imagenes con transparencia salen negras sin esto
        "-alpha", "remove",
        "-alpha", "off",
        "-quality", str(calidad),
        str(destino),
    ]
    _ejecutar(comando, TIMEOUT_JPG_A_PDF)

    if not destino.is_file():
        raise ConversionError("ImageMagick termino bien pero no genero el PDF.")
    return destino


# --------------------------------------------------------------------------
# 3) Documento (TXT, DOCX, ODT, RTF) -> PDF
# --------------------------------------------------------------------------

def documento_a_pdf(documento: Path | str, destino: Path | str) -> Path:
    """Convierte un documento de texto a PDF con Calibre."""
    documento = Path(documento)
    destino = Path(destino)

    if not documento.is_file():
        raise ConversionError(f"No existe el documento: {documento}")

    destino.parent.mkdir(parents=True, exist_ok=True)

    comando = [_binario_calibre(), str(documento), str(destino)]
    _ejecutar(comando, TIMEOUT_DOCUMENTO)

    if not destino.is_file():
        raise ConversionError("ebook-convert termino bien pero no genero el PDF.")
    return destino


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Conversor de archivos (motor local)")
    subs = parser.add_subparsers(dest="modo")

    subs.add_parser("check")

    p_epub = subs.add_parser("pdf-a-epub")
    p_epub.add_argument("pdf")
    p_epub.add_argument("salida")

    p_jpg = subs.add_parser("jpg-a-pdf")
    p_jpg.add_argument("imagenes", nargs="+")
    p_jpg.add_argument("salida")

    p_doc = subs.add_parser("doc-a-pdf")
    p_doc.add_argument("documento")
    p_doc.add_argument("salida")

    args = parser.parse_args()

    try:
        if args.modo == "check" or args.modo is None:
            for nombre, ruta in verificar_dependencias().items():
                print(f"{nombre:>14}: {ruta}")
            sys.exit(0)

        if args.modo == "pdf-a-epub":
            resultado = convertir_pdf_a_epub(args.pdf, Path(args.salida).parent)
            Path(resultado).replace(args.salida)
        elif args.modo == "jpg-a-pdf":
            resultado = imagenes_a_pdf(args.imagenes, args.salida)
        elif args.modo == "doc-a-pdf":
            resultado = documento_a_pdf(args.documento, args.salida)

        print(f"Salida: {resultado}")
    except ConversionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)