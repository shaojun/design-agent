"""PDF ingestion must expose both text and visuals, and stop on missing assets/tools."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from deepagents.backends.protocol import ExecuteResponse, FileUploadResponse

from design_agent.runtime import pdf


def test_extract_keeps_text_pages_and_images_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "brief.pdf"
    source.write_bytes(b"%PDF-1.4")
    output = tmp_path / "extracted"
    calls: list[list[str]] = []

    def run(args: list[str]) -> str:
        calls.append(args)
        if args[0] == "pdfinfo":
            return "Pages: 2\n"
        if args[0] == "pdftotext":
            Path(args[-1]).write_text("Event introduction")
        if args[0] == "pdftoppm":
            Path(args[-1] + ".png").write_bytes(b"page-preview")
        if args[0] == "pdfimages":
            if "-list" in args:
                return "   1  0 image 320 200 rgb 3 8 jpeg no 5 0 72 72 1K 10%\n"
            Path(args[-1] + "-000.jpg").write_bytes(b"source-photo")
        return ""

    monkeypatch.setattr(pdf, "_run", run)
    manifest = pdf.extract(source, output)
    assert Path(manifest["text"]).read_text() == "Event introduction"
    assert len(manifest["pages"]) == 2 and len(manifest["images"]) == 2
    assert all(Path(p["path"]).is_file() for p in manifest["pages"] + manifest["images"])
    assert json.loads((output / "pdf-manifest.json").read_text()) == manifest
    assert "look" in manifest["instructions"]
    # Visual extraction runs even when text extraction succeeded.
    assert any(c[0] == "pdfimages" and "-list" not in c for c in calls)


def test_extract_records_page_and_image_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(args: list[str]) -> str:
        if args[0] == "pdfinfo":
            return "Pages: 30\n"
        if args[0] == "pdftotext":
            Path(args[-1]).write_text("")
        if args[0] == "pdftoppm":
            Path(args[-1] + ".png").write_bytes(b"preview")
        if args[0] == "pdfimages":
            assert "-list" in args, "oversized embedded image must not be decoded"
            return "1 0 image 100000 100000 rgb 3 8 jpeg no 5 0 72 72 1K 10%\n"
        return ""

    monkeypatch.setattr(pdf, "_run", run)
    monkeypatch.setattr(pdf, "MAX_PAGES", 2)
    manifest = pdf.extract(tmp_path / "scan.pdf", tmp_path / "output")
    assert manifest["total_pages"] == 30 and manifest["processed_pages"] == 2
    assert len(manifest["pages"]) == 2 and manifest["images"] == []
    assert len(manifest["warnings"]) == 3


@pytest.mark.parametrize("oversized_images", [False, True])
def test_output_budget_includes_text_previews_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oversized_images: bool
) -> None:
    def run(args: list[str]) -> str:
        if args[0] == "pdfinfo":
            return "Pages: 1\n"
        if args[0] == "pdftotext":
            Path(args[-1]).write_text("a" * 80)
        if args[0] == "pdftoppm":
            Path(args[-1] + ".png").write_bytes(b"b" * 80)
        if args[0] == "pdfimages" and "-list" in args and oversized_images:
            return "1 0 image 100000 100000 rgb 3 8 jpeg no 5 0 72 72 1K 10%\n"
        return ""

    monkeypatch.setattr(pdf, "_run", run)
    monkeypatch.setattr(pdf, "MAX_OUTPUT_BYTES", 100)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="output budget exceeded"):
        pdf.extract(tmp_path / "brief.pdf", output)
    assert not (output / "pdf-manifest.json").exists()


def test_output_budget_includes_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(args: list[str]) -> str:
        if args[0] == "pdfinfo":
            return "Pages: 1\n"
        if args[0] == "pdftotext":
            Path(args[-1]).write_text("")
        if args[0] == "pdftoppm":
            Path(args[-1] + ".png").write_bytes(b"preview")
        return ""

    monkeypatch.setattr(pdf, "_run", run)
    monkeypatch.setattr(pdf, "MAX_OUTPUT_BYTES", 100)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="output budget exceeded"):
        pdf.extract(tmp_path / "brief.pdf", output)
    assert not (output / "pdf-manifest.json").exists()


@pytest.mark.parametrize("upload_error,exit_code", [(True, 0), (False, 1), (False, 0)])
def test_prepare_pdf_is_sandbox_only_and_propagates_failures(
    upload_error: bool, exit_code: int
) -> None:
    class Backend:
        commands: list[str] = []

        def upload_files(self, files: Any) -> list[FileUploadResponse]:
            assert len(files) == 1 and b"def extract(" in files[0][1]
            return [
                FileUploadResponse(
                    path=files[0][0], error="permission_denied" if upload_error else None
                )
            ]

        def execute(self, command: str, *, timeout: int) -> ExecuteResponse:
            assert timeout == 180
            self.commands.append(command)
            return ExecuteResponse(exit_code=exit_code, output="")

    backend = Backend()
    path = "/workspace/attachments/a '; touch bad.pdf"
    if upload_error or exit_code:
        with pytest.raises(RuntimeError):
            pdf.prepare_pdf(backend, path)  # type: ignore[arg-type]
    else:
        result = pdf.prepare_pdf(backend, path)  # type: ignore[arg-type]
        import shlex

        assert path in shlex.split(backend.commands[0].split("; ", 1)[1])
        assert result.endswith("/pdf-manifest.json")


def test_parser_failure_does_not_claim_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(args: list[str]) -> str:
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(pdf, "_run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        pdf.extract(tmp_path / "broken.pdf", tmp_path / "output")
    assert not (tmp_path / "output/pdf-manifest.json").exists()


async def test_attach_passes_pdf_manifest_to_agent_with_correct_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from langchain_core.runnables.config import var_child_runnable_config

    from design_agent.runtime.local import LocalRuntime

    prepared: list[str] = []

    class Backend:
        def upload_files(self, files: Any) -> list[FileUploadResponse]:
            return [FileUploadResponse(path=p, error=None) for p, _ in files]

    backend = Backend()

    def prepare(received: Any, path: str) -> str:
        assert received is backend
        assert var_child_runnable_config.get()["configurable"]["thread_id"] == "pdf-thread"
        prepared.append(path)
        return "/workspace/attachments/extracted/pdf-manifest.json"

    monkeypatch.setattr(pdf, "prepare_pdf", prepare)
    runtime = SimpleNamespace(binding=SimpleNamespace(sandbox=backend))
    paths = await LocalRuntime.attach(runtime, "pdf-thread", [("brief.pdf", b"%PDF-1.4\n")])  # type: ignore[arg-type]
    assert prepared == ["/workspace/attachments/brief.pdf"]
    assert paths == [
        "/workspace/attachments/brief.pdf",
        "/workspace/attachments/extracted/pdf-manifest.json",
    ]


async def test_attach_refused_upload_never_claims_attachment() -> None:
    from types import SimpleNamespace

    from design_agent.runtime.local import LocalRuntime

    class Backend:
        def upload_files(self, files: Any) -> list[FileUploadResponse]:
            return [FileUploadResponse(path=p, error="permission_denied") for p, _ in files]

    runtime = SimpleNamespace(binding=SimpleNamespace(sandbox=Backend()))
    with pytest.raises(RuntimeError, match="Attachment upload failed"):
        await LocalRuntime.attach(runtime, "thread", [("brief.pdf", b"%PDF-1.4")])  # type: ignore[arg-type]
