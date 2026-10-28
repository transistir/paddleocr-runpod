from __future__ import annotations

import ipaddress
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pypdf import PdfWriter  # noqa: E402
from pypdf.generic import NameObject, NumberObject  # noqa: E402

from worker_core import (  # noqa: E402
    InputError,
    Settings,
    cleanup_stale_workdirs,
    combine_pages,
    download_pdf,
    enforce_output_limit,
    ensure_markdown_budget,
    load_settings,
    normalize_markdown,
    parse_allowed_hosts,
    prepare_pdf,
    result_to_markdown,
    validate_job_input,
    validate_pdf,
    validate_source_url,
)


class FakeResult:
    def save_to_markdown(self, save_path: str, pretty: bool = False) -> None:
        del pretty
        Path(save_path, "page.md").write_text("Text", encoding="utf-8")


class WorkerCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(allowed_source_hosts=frozenset({"files.example.org"}))

    def test_allowed_hosts_are_exact_and_required(self) -> None:
        self.assertEqual(
            parse_allowed_hosts("FILES.EXAMPLE.ORG.,cdn.example.org"),
            frozenset({"files.example.org", "cdn.example.org"}),
        )
        invalid_hosts = (
            "",
            "*.example.org",
            "https://example.org",
            "bad..example.org",
            "bad_host.example.org",
            "-bad.example.org",
        )
        for value in invalid_hosts:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                parse_allowed_hosts(value)

    def test_input_is_small_and_explicit(self) -> None:
        self.assertEqual(
            validate_job_input({"file_url": "https://files.example.org/a.pdf"}),
            "https://files.example.org/a.pdf",
        )
        with self.assertRaises(InputError):
            validate_job_input({"file_url": "https://files.example.org/a.pdf", "extra": 1})

    def test_load_settings_uses_paddle_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {"ALLOWED_SOURCE_HOSTS": "files.example.org"},
            clear=True,
        ):
            settings = load_settings()
        self.assertTrue(settings.use_layout_detection)
        self.assertFalse(settings.use_doc_unwarping)
        self.assertEqual(settings.max_new_tokens, 8192)

    def test_url_requires_https_allowlist_and_public_dns(self) -> None:
        def public(_host: str):
            return [ipaddress.ip_address("8.8.8.8")]

        def private(_host: str):
            return [ipaddress.ip_address("127.0.0.1")]

        self.assertEqual(
            validate_source_url(
                "https://files.example.org/a.pdf?signature=x",
                self.settings,
                resolver=public,
            ),
            "https://files.example.org/a.pdf?signature=x",
        )
        for url, resolver in (
            ("http://files.example.org/a.pdf", public),
            ("https://other.example.org/a.pdf", public),
            ("https://files.example.org:444/a.pdf", public),
            ("https://files.example.org/a.pdf#fragment", public),
            ("https://files.example.org/a.pdf", private),
        ):
            with self.subTest(url=url), self.assertRaises(InputError):
                validate_source_url(url, self.settings, resolver=resolver)

    def test_download_has_wall_clock_timeout(self) -> None:
        class FakeResponse:
            status_code = 200
            headers: dict[str, str] = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_content(self, chunk_size: int):
                del chunk_size
                yield b"pdf"

        class FakeSession:
            def __init__(self) -> None:
                self.trust_env = True
                self.headers: dict[str, str] = {}

            def get(self, *args, **kwargs):
                del args, kwargs
                return FakeResponse()

            def close(self) -> None:
                return None

        settings = Settings(
            allowed_source_hosts=frozenset({"files.example.org"}),
            download_timeout_seconds=120,
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("worker_core.requests.Session", FakeSession),
            patch("worker_core.validate_source_url"),
            patch("worker_core.time.monotonic", side_effect=[0.0, 0.0, 121.0]),
        ):
            destination = Path(temporary, "source.pdf")
            with self.assertRaises(InputError) as context:
                download_pdf("https://files.example.org/a.pdf", destination, settings)
            self.assertEqual(context.exception.code, "DOWNLOAD_TIMEOUT")
            self.assertFalse(destination.exists())

    def test_startup_cleanup_removes_only_job_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / "job-stale"
            stale.mkdir()
            (stale / "source.pdf").write_bytes(b"private")
            keep = root / "keep"
            keep.mkdir()
            with patch.dict(os.environ, {"TMPDIR": str(root)}):
                cleanup_stale_workdirs()
            self.assertFalse(stale.exists())
            self.assertTrue(keep.exists())

    def test_pdf_validation_and_page_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "valid.pdf")
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.write(path)
            self.assertEqual(validate_pdf(path, self.settings), 1)
            limited = Settings(
                allowed_source_hosts=self.settings.allowed_source_hosts,
                max_pdf_pages=1,
            )
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.add_blank_page(width=72, height=72)
            writer.write(path)
            with self.assertRaises(InputError) as context:
                validate_pdf(path, limited)
            self.assertEqual(context.exception.code, "TOO_MANY_PAGES")

    def test_pdf_dimension_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "huge.pdf")
            writer = PdfWriter()
            writer.add_blank_page(width=8000, height=72)
            writer.write(path)
            with self.assertRaises(InputError) as context:
                validate_pdf(path, self.settings)
            self.assertEqual(context.exception.code, "PDF_PAGE_TOO_LARGE")

    def test_pdf_user_unit_is_included_in_dimension_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "scaled.pdf")
            writer = PdfWriter()
            page = writer.add_blank_page(width=100, height=100)
            page[NameObject("/UserUnit")] = NumberObject(100)
            writer.write(path)
            with self.assertRaises(InputError) as context:
                validate_pdf(path, self.settings)
            self.assertEqual(context.exception.code, "PDF_PAGE_TOO_LARGE")

    def test_prepare_pdf_uses_downloaded_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            source = workdir / "fixture.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.write(source)
            pdf_bytes = source.read_bytes()

            def fake_download(url: str, destination: Path, settings: Settings) -> int:
                del url, settings
                destination.write_bytes(pdf_bytes)
                return len(pdf_bytes)

            with (
                patch("worker_core.download_pdf", side_effect=fake_download),
                patch(
                    "worker_core.validate_source_url",
                    return_value="https://files.example.org/a.pdf",
                ),
            ):
                prepared = prepare_pdf("https://files.example.org/a.pdf", workdir, self.settings)
            self.assertEqual(prepared.page_count, 1)
            self.assertEqual(prepared.byte_size, len(pdf_bytes))
            self.assertEqual(len(prepared.sha256), 64)

    def test_markdown_is_sanitized_without_semantic_rewrite(self) -> None:
        raw = (
            '<div style="text-align:center"><img src="https://x/signed?a=b"></div>\n\n'
            "Text  with  spaces\n\n\n\n![chart](data:image/png;base64,abc)\nEnd"
        )
        normalized = normalize_markdown(raw)
        self.assertNotIn("signed", normalized)
        self.assertNotIn("data:image", normalized)
        self.assertIn("Text  with  spaces", normalized)
        self.assertIn("End", normalized)

    def test_result_artifact_uses_documented_markdown_method(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            markdown = result_to_markdown(FakeResult(), Path(temporary), max_output_bytes=1024)
        self.assertEqual(markdown, "Text")

    def test_page_combination_is_explicit(self) -> None:
        self.assertEqual(
            combine_pages(["one", "two"]),
            "<!-- PAGE 1 -->\n\none\n\n<!-- PAGE 2 -->\n\ntwo",
        )

    def test_markdown_budget_fails_before_unbounded_accumulation(self) -> None:
        pages = ["a" * 20]
        ensure_markdown_budget(pages, "b" * 20, 2, 200)
        with self.assertRaises(InputError):
            ensure_markdown_budget(pages, "b" * 500, 2, 200)

    def test_output_limit_is_exact_and_never_truncates(self) -> None:
        output = {"ok": True, "markdown": "short"}
        self.assertIs(enforce_output_limit(output, 500), output)
        with self.assertRaises(InputError):
            enforce_output_limit({"ok": True, "markdown": "x" * 2000}, 500)


if __name__ == "__main__":
    unittest.main()


class SanitizacaoNaofinitosTest(unittest.TestCase):
    """RunPod recusa o job-done (400) quando o payload tem NaN/Infinity.

    `json.dumps` os aceita por padrão e gera JSON inválido; a sanitização é
    explícita e o handler loga `payload_nan=True` quando ela é necessária.
    """

    def test_nan_e_infinitos_viram_none(self):
        import json

        from worker_core import sanitizar_nao_finitos

        entrada = {"a": float("nan"), "b": [float("inf"), 1.5], "c": {"d": float("-inf")}}
        saida = sanitizar_nao_finitos(entrada)
        self.assertEqual({"a": None, "b": [None, 1.5], "c": {"d": None}}, saida)
        # o resultado é serializável sem allow_nan
        self.assertIn('"a": null', json.dumps(saida, allow_nan=False))

    def test_valores_finitos_passam_intactos(self):
        from worker_core import sanitizar_nao_finitos

        entrada = {"x": 1.25, "y": "texto", "z": [1, 2]}
        self.assertEqual(entrada, sanitizar_nao_finitos(entrada))
