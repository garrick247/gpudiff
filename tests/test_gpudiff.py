"""Basic smoke tests for gpudiff. Doesn't require an actual GPU compiler —
uses tiny shell echo/cp tricks to exercise the comparator harness."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Insert parent dir so `import gpudiff` works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gpudiff  # noqa: E402


def _make_corpus(tmpdir: Path, n: int = 3) -> list[Path]:
    """Create N tiny 'source' files with distinct content."""
    paths = []
    for i in range(n):
        p = tmpdir / f"k{i}.ptx"
        p.write_text(f"// kernel {i}\n.version 8.7\n.target sm_120\n", encoding="utf-8")
        paths.append(p)
    return paths


def _identity_compiler() -> str:
    """A trivial 'compiler' that just copies input to output. Used to
    simulate two compilers that always agree (BYTE_MATCH everywhere)."""
    if os.name == "nt":
        # Use Python so we don't depend on `cp`/`copy` on Windows
        py = sys.executable.replace("\\", "/")
        return f'{py} -c "import shutil,sys; shutil.copy(sys.argv[1], sys.argv[2])" {{in}} {{out}}'
    return "cp {in} {out}"


def _diverging_compiler() -> str:
    """A 'compiler' that copies input but appends a marker byte, simulating
    a candidate that always differs from the reference."""
    py = sys.executable.replace("\\", "/")
    return (
        f'{py} -c '
        '"import shutil,sys; shutil.copy(sys.argv[1], sys.argv[2]); '
        'open(sys.argv[2], \'ab\').write(b\'\\x00\\x01\\x02\')" '
        "{in} {out}"
    )


def _failing_compiler() -> str:
    """A 'compiler' that always exits non-zero."""
    py = sys.executable.replace("\\", "/")
    return f'{py} -c "import sys; sys.exit(2)" {{in}} {{out}}'


def test_byte_match_when_compilers_agree(tmp_path):
    paths = _make_corpus(tmp_path)
    for p in paths:
        r = gpudiff.compare_kernel(
            p,
            ref_template=_identity_compiler(),
            cand_template=_identity_compiler(),
            disasm_template=None,  # skip disasm
        )
        assert r.verdict() == "BYTE_MATCH", f"{r.name}: {r.verdict()}"
        assert r.byte_match is True
        assert r.cand.cubin_sha == r.ref.cubin_sha
        assert r.cand.error is None and r.ref.error is None


def test_major_diff_when_compilers_disagree(tmp_path):
    paths = _make_corpus(tmp_path, n=1)
    r = gpudiff.compare_kernel(
        paths[0],
        ref_template=_identity_compiler(),
        cand_template=_diverging_compiler(),
        disasm_template=None,
    )
    assert r.verdict() == "MAJOR_DIFF"
    assert r.byte_match is False


def test_ours_failed_when_candidate_errors(tmp_path):
    paths = _make_corpus(tmp_path, n=1)
    r = gpudiff.compare_kernel(
        paths[0],
        ref_template=_identity_compiler(),
        cand_template=_failing_compiler(),
        disasm_template=None,
    )
    assert r.verdict() == "OURS_FAILED"
    assert r.cand.error is not None


def test_ref_failed_when_reference_errors(tmp_path):
    paths = _make_corpus(tmp_path, n=1)
    r = gpudiff.compare_kernel(
        paths[0],
        ref_template=_failing_compiler(),
        cand_template=_identity_compiler(),
        disasm_template=None,
    )
    assert r.verdict() == "REF_FAILED"


def test_corpus_expansion_directory(tmp_path):
    paths = _make_corpus(tmp_path, n=3)
    found = gpudiff.expand_corpus([str(tmp_path)])
    assert len(found) == 3
    assert {p.name for p in found} == {"k0.ptx", "k1.ptx", "k2.ptx"}


def test_corpus_expansion_glob(tmp_path):
    paths = _make_corpus(tmp_path, n=3)
    found = gpudiff.expand_corpus([str(tmp_path / "*.ptx")])
    assert len(found) == 3


def test_render_markdown_includes_summary(tmp_path):
    paths = _make_corpus(tmp_path, n=2)
    results = [
        gpudiff.compare_kernel(
            p, _identity_compiler(), _identity_compiler(), disasm_template=None
        )
        for p in paths
    ]
    header = {
        "timestamp": "2026-01-01T00:00:00",
        "ref_template": "id",
        "cand_template": "id",
        "corpus": [str(tmp_path)],
        "version": "test",
    }
    md = gpudiff.render_markdown(results, header)
    assert "BYTE_MATCH" in md
    assert "## Summary" in md
    assert "## Per-kernel" in md


def test_render_json_roundtrip(tmp_path):
    paths = _make_corpus(tmp_path, n=1)
    results = [
        gpudiff.compare_kernel(
            paths[0], _identity_compiler(), _identity_compiler(), disasm_template=None
        )
    ]
    header = {"timestamp": "2026-01-01T00:00:00", "ref_template": "id",
              "cand_template": "id", "corpus": [], "version": "test"}
    js = gpudiff.render_json(results, header)
    parsed = json.loads(js)
    assert parsed["header"]["version"] == "test"
    assert len(parsed["results"]) == 1
    assert parsed["results"][0]["verdict"] == "BYTE_MATCH"


def test_render_junit_includes_failure_when_diff(tmp_path):
    paths = _make_corpus(tmp_path, n=1)
    r = gpudiff.compare_kernel(
        paths[0], _identity_compiler(), _diverging_compiler(), disasm_template=None
    )
    header = {"timestamp": "2026-01-01T00:00:00", "ref_template": "id",
              "cand_template": "id", "corpus": [], "version": "test"}
    xml = gpudiff.render_junit([r], header)
    assert '<failure' in xml
    assert 'failures="1"' in xml


def test_cli_exit_code_byte_match_returns_zero(tmp_path):
    """Smoke-test the CLI entry point with a happy path."""
    paths = _make_corpus(tmp_path, n=1)
    out_md = tmp_path / "report.md"
    rc = gpudiff.main([
        "--ref", _identity_compiler(),
        "--candidate", _identity_compiler(),
        "--corpus", str(tmp_path),
        "--output", str(out_md),
        "--no-disasm",
        "--fail-on", "any",
    ])
    assert rc == 0
    assert out_md.exists()
    assert "BYTE_MATCH" in out_md.read_text(encoding="utf-8")


def test_cli_exit_code_diff_returns_one(tmp_path):
    paths = _make_corpus(tmp_path, n=1)
    rc = gpudiff.main([
        "--ref", _identity_compiler(),
        "--candidate", _diverging_compiler(),
        "--corpus", str(tmp_path),
        "--output", str(tmp_path / "report.md"),
        "--no-disasm",
        "--fail-on", "any",
    ])
    assert rc == 1


def test_cli_fail_on_never_always_zero(tmp_path):
    paths = _make_corpus(tmp_path, n=1)
    rc = gpudiff.main([
        "--ref", _identity_compiler(),
        "--candidate", _diverging_compiler(),
        "--corpus", str(tmp_path),
        "--output", str(tmp_path / "report.md"),
        "--no-disasm",
        "--fail-on", "never",
    ])
    assert rc == 0
