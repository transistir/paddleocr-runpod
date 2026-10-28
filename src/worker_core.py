"""Validation, secure download, PDF checks, and Markdown helpers."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from pypdf import PdfReader


class InputError(ValueError):
    """Safe, client-visible validation or processing error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class Settings:
    allowed_source_hosts: frozenset[str]
    max_input_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 25
    max_pdf_page_edge_points: int = 3600
    max_pdf_page_area_points: int = 10_000_000
    max_output_bytes: int = 8 * 1024 * 1024
    download_timeout_seconds: int = 120
    max_redirects: int = 0
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_layout_detection: bool = True
    use_chart_recognition: bool = False
    use_seal_recognition: bool = False
    temperature: float = 0.0
    max_new_tokens: int = 8192
    # 0 = o pipeline rasteriza o PDF sozinho (comportamento antigo).
    # >0 = rasterizamos página a página nesta resolução antes de inferir:
    # página densa (matrícula escaneada) em resolução nativa estoura o
    # max_new_tokens e o job inteiro não termina.
    render_dpi: int = 0


@dataclass(frozen=True)
class PreparedPdf:
    path: Path
    sha256: str
    byte_size: int
    page_count: int


_IMAGE_HTML = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_IMAGE_MARKDOWN = re.compile(r"!\[[^\]]*\]\([^)]*\)", re.DOTALL)
_EMPTY_DIV = re.compile(r"<div\b[^>]*>\s*</div>", re.IGNORECASE | re.DOTALL)
_MANY_BLANK_LINES = re.compile(r"\n{4,}")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}")
    return parsed


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def parse_allowed_hosts(value: str) -> frozenset[str]:
    hosts: set[str] = set()
    for item in value.split(","):
        hostname = item.strip().rstrip(".").lower()
        if not hostname:
            continue
        if "://" in hostname or "/" in hostname or ":" in hostname or "*" in hostname:
            raise RuntimeError(
                "ALLOWED_SOURCE_HOSTS must contain exact hostnames only, "
                "without schemes, paths, ports, or wildcards"
            )
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise RuntimeError("ALLOWED_SOURCE_HOSTS contains an invalid hostname") from exc
        labels = hostname.split(".")
        if len(hostname) > 253 or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not re.fullmatch(r"[a-z0-9-]+", label)
            for label in labels
        ):
            raise RuntimeError("ALLOWED_SOURCE_HOSTS contains an invalid hostname")
        hosts.add(hostname)
    if not hosts:
        raise RuntimeError("ALLOWED_SOURCE_HOSTS must contain at least one trusted HTTPS hostname")
    return frozenset(hosts)


def load_settings() -> Settings:
    return Settings(
        allowed_source_hosts=parse_allowed_hosts(os.getenv("ALLOWED_SOURCE_HOSTS", "")),
        max_input_bytes=_env_int("MAX_INPUT_BYTES", 50 * 1024 * 1024, minimum=1),
        max_pdf_pages=_env_int("MAX_PDF_PAGES", 25, minimum=1, maximum=500),
        max_pdf_page_edge_points=_env_int("MAX_PDF_PAGE_EDGE_POINTS", 3600, minimum=72),
        max_pdf_page_area_points=_env_int("MAX_PDF_PAGE_AREA_POINTS", 10_000_000, minimum=5184),
        max_output_bytes=_env_int("MAX_OUTPUT_BYTES", 8 * 1024 * 1024, minimum=1024),
        download_timeout_seconds=_env_int("DOWNLOAD_TIMEOUT_SECONDS", 120, minimum=1, maximum=1800),
        max_redirects=_env_int("MAX_REDIRECTS", 0, minimum=0, maximum=3),
        use_doc_orientation_classify=_env_bool("USE_DOC_ORIENTATION_CLASSIFY", False),
        use_doc_unwarping=_env_bool("USE_DOC_UNWARPING", False),
        use_layout_detection=_env_bool("USE_LAYOUT_DETECTION", True),
        use_chart_recognition=_env_bool("USE_CHART_RECOGNITION", False),
        use_seal_recognition=_env_bool("USE_SEAL_RECOGNITION", False),
        temperature=_env_float("TEMPERATURE", 0.0, minimum=0.0, maximum=2.0),
        max_new_tokens=_env_int("MAX_NEW_TOKENS", 8192, minimum=256, maximum=32768),
        render_dpi=_env_int("RENDER_DPI", 0, minimum=0, maximum=600),
    )


def validate_job_input(job_input: Any) -> str:
    if not isinstance(job_input, dict):
        raise InputError("INVALID_INPUT", "input must be a JSON object.")
    allowed_keys = {"file_url"}
    unknown = sorted(set(job_input) - allowed_keys)
    if unknown:
        raise InputError("INVALID_INPUT", f"Unknown input field: {unknown[0]}.")
    file_url = job_input.get("file_url")
    if not isinstance(file_url, str) or not file_url.strip():
        raise InputError("INVALID_FILE_URL", "file_url must be a non-empty HTTPS URL.")
    return file_url.strip()


def _resolve_host(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        records = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InputError("URL_DNS_ERROR", "The source hostname could not be resolved.") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for record in records:
        try:
            addresses.append(ipaddress.ip_address(record[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise InputError("URL_DNS_ERROR", "The source hostname resolved to no usable address.")
    return addresses


def validate_source_url(
    url: str,
    settings: Settings,
    *,
    resolver: Callable[
        [str], Iterable[ipaddress.IPv4Address | ipaddress.IPv6Address]
    ] = _resolve_host,
) -> str:
    if len(url) > 8192:
        raise InputError("INVALID_FILE_URL", "The source URL is too long.")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise InputError("INVALID_FILE_URL", "The source URL must use HTTPS.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise InputError("INVALID_FILE_URL", "The source URL is invalid.")
    if parsed.fragment:
        raise InputError("INVALID_FILE_URL", "The source URL must not contain a fragment.")
    try:
        port = parsed.port
        hostname = parsed.hostname.rstrip(".").lower().encode("idna").decode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise InputError("INVALID_FILE_URL", "The source URL is invalid.") from exc
    if port not in {None, 443}:
        raise InputError("INVALID_FILE_URL", "The source URL must use the standard HTTPS port.")
    if hostname not in settings.allowed_source_hosts:
        raise InputError("SOURCE_HOST_NOT_ALLOWED", "The source hostname is not allowed.")
    addresses = list(resolver(hostname))
    if not addresses or any(not address.is_global for address in addresses):
        raise InputError(
            "SOURCE_HOST_NOT_PUBLIC",
            "The source hostname must resolve only to public internet addresses.",
        )
    return url


def download_pdf(url: str, destination: Path, settings: Settings) -> int:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": "runpod-paddleocr-vl/3.1.0"})
    current_url = url
    deadline = time.monotonic() + settings.download_timeout_seconds
    try:
        for redirect_index in range(settings.max_redirects + 1):
            validate_source_url(current_url, settings)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InputError("DOWNLOAD_TIMEOUT", "The source PDF download timed out.")
            try:
                response = session.get(
                    current_url,
                    stream=True,
                    timeout=(min(15.0, remaining), max(1.0, remaining)),
                    allow_redirects=False,
                )
            except requests.Timeout as exc:
                raise InputError("DOWNLOAD_TIMEOUT", "The source PDF download timed out.") from exc
            except requests.RequestException as exc:
                raise InputError(
                    "DOWNLOAD_FAILED", "The source PDF could not be downloaded."
                ) from exc
            with response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise InputError(
                            "DOWNLOAD_FAILED", "The source URL returned an invalid redirect."
                        )
                    if redirect_index >= settings.max_redirects:
                        raise InputError(
                            "TOO_MANY_REDIRECTS",
                            "The source URL redirected more than allowed.",
                        )
                    current_url = urljoin(current_url, location)
                    continue
                if not 200 <= response.status_code < 300:
                    raise InputError(
                        "DOWNLOAD_FAILED",
                        f"The source URL returned HTTP {response.status_code}.",
                    )
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        declared_bytes = int(declared)
                    except ValueError:
                        declared_bytes = 0
                    if declared_bytes > settings.max_input_bytes:
                        raise InputError(
                            "FILE_TOO_LARGE",
                            "The source PDF exceeds the configured size limit.",
                        )
                total = 0
                with destination.open("wb") as output:
                    try:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if time.monotonic() >= deadline:
                                destination.unlink(missing_ok=True)
                                raise InputError(
                                    "DOWNLOAD_TIMEOUT",
                                    "The source PDF download timed out.",
                                )
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > settings.max_input_bytes:
                                destination.unlink(missing_ok=True)
                                raise InputError(
                                    "FILE_TOO_LARGE",
                                    "The source PDF exceeds the configured size limit.",
                                )
                            output.write(chunk)
                    except requests.Timeout as exc:
                        destination.unlink(missing_ok=True)
                        raise InputError(
                            "DOWNLOAD_TIMEOUT", "The source PDF download timed out."
                        ) from exc
                    except requests.RequestException as exc:
                        destination.unlink(missing_ok=True)
                        raise InputError(
                            "DOWNLOAD_FAILED",
                            "The source PDF could not be downloaded.",
                        ) from exc
                if total == 0:
                    destination.unlink(missing_ok=True)
                    raise InputError("EMPTY_FILE", "The source PDF is empty.")
                return total
        raise InputError("TOO_MANY_REDIRECTS", "The source URL redirected more than allowed.")
    finally:
        session.close()


def validate_pdf(path: Path, settings: Settings) -> int:
    with path.open("rb") as source:
        if source.read(5) != b"%PDF-":
            raise InputError("INVALID_PDF", "The downloaded file is not a PDF.")
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise InputError("ENCRYPTED_PDF", "Encrypted PDFs are not supported.")
        page_count = len(reader.pages)
        if page_count < 1:
            raise InputError("INVALID_PDF", "The PDF contains no pages.")
        if page_count > settings.max_pdf_pages:
            raise InputError(
                "TOO_MANY_PAGES",
                f"The PDF exceeds the {settings.max_pdf_pages}-page limit.",
            )
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                user_unit = float(getattr(page, "user_unit", 1.0))
                width = abs(float(page.mediabox.width)) * user_unit
                height = abs(float(page.mediabox.height)) * user_unit
            except (TypeError, ValueError, OverflowError) as exc:
                raise InputError(
                    "INVALID_PDF",
                    f"PDF page {page_number} has invalid dimensions.",
                ) from exc
            if (
                not math.isfinite(user_unit)
                or user_unit <= 0
                or not math.isfinite(width)
                or not math.isfinite(height)
                or width <= 0
                or height <= 0
            ):
                raise InputError(
                    "INVALID_PDF",
                    f"PDF page {page_number} has invalid dimensions.",
                )
            if (
                max(width, height) > settings.max_pdf_page_edge_points
                or width * height > settings.max_pdf_page_area_points
            ):
                raise InputError(
                    "PDF_PAGE_TOO_LARGE",
                    f"PDF page {page_number} exceeds the configured dimension limit.",
                )
    except InputError:
        raise
    except Exception as exc:
        raise InputError("INVALID_PDF", "The PDF could not be parsed.") from exc
    return page_count


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_pdf(url: str, workdir: Path, settings: Settings) -> PreparedPdf:
    validate_source_url(url, settings)
    path = workdir / "source.pdf"
    byte_size = download_pdf(url, path, settings)
    page_count = validate_pdf(path, settings)
    return PreparedPdf(
        path=path,
        sha256=sha256_file(path),
        byte_size=byte_size,
        page_count=page_count,
    )


def normalize_markdown(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    text = _IMAGE_HTML.sub("", text)
    text = _IMAGE_MARKDOWN.sub("", text)
    text = _EMPTY_DIV.sub("", text)
    text = _MANY_BLANK_LINES.sub("\n\n\n", text)
    return text.strip()


def combine_pages(markdown_pages: list[str]) -> str:
    return "\n\n".join(
        f"<!-- PAGE {index} -->\n\n{page.strip()}"
        for index, page in enumerate(markdown_pages, start=1)
    )


def _find_single(directory: Path, suffix: str) -> Path:
    matches = sorted(path for path in directory.rglob(f"*{suffix}") if path.is_file())
    if len(matches) != 1:
        raise InputError(
            "INVALID_OCR_RESULT",
            f"PaddleOCR produced {len(matches)} {suffix} files for one page.",
        )
    return matches[0]


def result_to_markdown(result: Any, page_dir: Path, *, max_output_bytes: int) -> str:
    page_dir.mkdir(parents=True, exist_ok=True)
    try:
        result.save_to_markdown(save_path=str(page_dir), pretty=False)
    except TypeError:
        result.save_to_markdown(save_path=str(page_dir))
    markdown_path = _find_single(page_dir, ".md")
    # Permit modest normalization overhead, but never read an unbounded artifact.
    if markdown_path.stat().st_size > max_output_bytes * 2:
        raise InputError("OUTPUT_TOO_LARGE", "PaddleOCR produced oversized Markdown.")
    try:
        markdown = normalize_markdown(markdown_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise InputError("INVALID_OCR_RESULT", "PaddleOCR returned invalid Markdown.") from exc
    if not markdown:
        raise InputError("EMPTY_OCR_RESULT", "PaddleOCR returned empty Markdown.")
    return markdown


def enforce_output_limit(output: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise InputError(
            "OUTPUT_TOO_LARGE",
            "The OCR Markdown exceeds the configured response-size limit.",
        )
    return output


def ensure_markdown_budget(
    pages: list[str], next_page: str, page_number: int, max_bytes: int
) -> None:
    candidate = combine_pages([*pages, next_page])
    if len(candidate.encode("utf-8")) > max_bytes:
        raise InputError(
            "OUTPUT_TOO_LARGE",
            f"OCR Markdown exceeded the response limit at page {page_number}.",
        )


def cleanup_stale_workdirs() -> None:
    root = Path(os.getenv("TMPDIR", "/tmp/paddleocr-runpod"))
    root.mkdir(parents=True, exist_ok=True)
    for path in root.glob("job-*"):
        if not path.is_dir():
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            # Startup cleanup is best effort; each normal job still uses TemporaryDirectory.
            continue


def new_workdir() -> tempfile.TemporaryDirectory[str]:
    root = Path(os.getenv("TMPDIR", "/tmp/paddleocr-runpod"))
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="job-", dir=root)


def sanitizar_nao_finitos(valor: object) -> object:
    """Troca NaN/Infinity por None, recursivamente.

    `json.dumps` os aceita por padrão e produz JSON que o servidor recusa (400
    no job-done). Aqui a normalização é explícita em vez de silenciosa: o
    handler registra `payload_nan` no log quando isso acontece.
    """
    import math

    if isinstance(valor, float):
        return valor if math.isfinite(valor) else None
    if isinstance(valor, dict):
        return {chave: sanitizar_nao_finitos(item) for chave, item in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [sanitizar_nao_finitos(item) for item in valor]
    return valor


def render_pages(pdf_path: Path, output_dir: Path, dpi: int) -> list[Path]:
    """Rasteriza cada página do PDF em PNG na resolução pedida.

    Motivo: página de matrícula escaneada tem carimbo, brasão e texto em
    diagonal. Em resolução nativa o VL gera tantos tokens que o job não
    termina dentro do timeout. Rasterizar em DPI controlado também permite
    isolar a falha por página.
    """
    import pypdfium2 as pdfium

    if dpi <= 0:
        raise ValueError("dpi deve ser positivo")
    output_dir.mkdir(parents=True, exist_ok=True)
    destinos: list[Path] = []
    documento = pdfium.PdfDocument(str(pdf_path))
    try:
        escala = dpi / 72
        for indice in range(len(documento)):
            pagina = documento[indice]
            try:
                imagem = pagina.render(scale=escala)
                destino = output_dir / f"page-{indice + 1:04d}.png"
                imagem.to_pil().save(destino, format="PNG")
                destinos.append(destino)
            finally:
                pagina.close()
    finally:
        documento.close()
    return destinos
