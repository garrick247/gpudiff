#!/usr/bin/env python3
"""gpudiff — differential cubin tester for GPU compilers.

Take two compilers and a corpus of PTX (or any source format the compilers
accept), compile each input through both, byte-diff the outputs, classify
per-input as BYTE_MATCH / MAJOR_DIFF / OURS_FAILED / REF_FAILED, emit a
structured report (markdown / JSON / JUnit XML).

Use cases:
  - CI: gate openptxas (or any alt-toolchain) against ptxas on a regression
    corpus.  Surface byte-level deltas + per-opcode histograms.
  - Bisect: combine with `git bisect run` to narrow the introducing commit.
  - Research: track BYTE_MATCH percentage across compiler iterations.
  - Bug-bounty: differential testing surfaces miscompiles.

Compiler invocation contract:
  Each compiler is specified as a shell command template containing two
  placeholders:
    {in}   — the input source file
    {out}  — the output binary (cubin/elf) the tool will diff
  The tool invokes the command, expects exit-0 on success, expects {out}
  to exist after invocation.  Anything else: we mark as a failure.

  Example: "ptxas -arch=sm_120 -o {out} {in}"

  No Python API coupling — both compilers are external binaries.  If your
  tool is Python-only, wrap it in a CLI shim.

Usage:
  gpudiff --ref 'ptxas -arch=sm_120 -o {out} {in}' \\
          --candidate 'mycompiler -arch=sm_120 -o {out} {in}' \\
          --corpus ./kernels/*.ptx \\
          --output report.md \\
          --json report.json

Per-kernel verdicts:
  BYTE_MATCH    cubins are byte-identical
  MAJOR_DIFF    cubins differ; per-opcode histogram delta surfaced
  OURS_FAILED   candidate compiler errored / produced no output
  REF_FAILED    reference compiler errored (rare; usually means bad input)

Disasm: by default, gpudiff invokes nvdisasm to dump SASS for opcode
histograms.  Override with --disasm-cmd if your toolchain uses a
different disassembler.  Set --no-disasm to skip the histogram and
just byte-diff (faster).
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


__version__ = "0.1.0"


@dataclass
class CompileResult:
    cubin_bytes: Optional[bytes] = None
    cubin_size: int = 0
    cubin_sha: str = ""
    elapsed_s: float = 0.0
    error: Optional[str] = None
    stdout: str = ""
    stderr: str = ""


@dataclass
class KernelResult:
    name: str
    path: str
    ptx_size: int
    ref: CompileResult = field(default_factory=CompileResult)
    cand: CompileResult = field(default_factory=CompileResult)
    byte_match: bool = False
    ref_n: int = 0
    cand_n: int = 0
    opcode_deltas: dict = field(default_factory=dict)  # op -> (cand, ref)
    cand_unique_ops: list = field(default_factory=list)
    ref_unique_ops: list = field(default_factory=list)

    def verdict(self) -> str:
        if self.cand.error and self.ref.error:
            return "BOTH_FAILED"
        if self.cand.error:
            return "OURS_FAILED"
        if self.ref.error:
            return "REF_FAILED"
        if self.byte_match:
            return "BYTE_MATCH"
        return "MAJOR_DIFF"

    def to_dict(self) -> dict:
        d = asdict(self)
        # Strip cubin_bytes from JSON dumps (not meaningful in text)
        for side in ("ref", "cand"):
            d[side].pop("cubin_bytes", None)
        d["verdict"] = self.verdict()
        return d


def run_compiler(template: str, input_path: Path, timeout: int = 60) -> CompileResult:
    """Invoke a compiler template with {in}/{out} placeholders.

    Tokenization happens BEFORE placeholder substitution so that input/
    output paths (which on Windows contain backslashes that POSIX shlex
    would eat) never pass through shlex.  Quote-aware splitting still
    works for the rest of the template.
    """
    result = CompileResult()
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.cubin"
        # Tokenize the template (with placeholders intact)
        tokens = shlex.split(template, posix=True)
        # Substitute placeholders per-token (no further shlex pass)
        cmd_args = [
            tok.replace("{in}", str(input_path)).replace("{out}", str(out_path))
            for tok in tokens
        ]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            result.elapsed_s = time.monotonic() - t0
            result.stdout = proc.stdout[:2000]
            result.stderr = proc.stderr[:2000]
            if proc.returncode != 0:
                result.error = f"exit={proc.returncode}: {proc.stderr.strip()[:300]}"
                return result
            if not out_path.exists():
                result.error = f"compiler exited 0 but produced no {out_path.name}"
                return result
            result.cubin_bytes = out_path.read_bytes()
            result.cubin_size = len(result.cubin_bytes)
            result.cubin_sha = hashlib.sha256(result.cubin_bytes).hexdigest()[:16]
        except subprocess.TimeoutExpired:
            result.elapsed_s = time.monotonic() - t0
            result.error = f"timeout after {timeout}s"
        except FileNotFoundError as e:
            result.error = f"command not found: {e}"
        except Exception as e:
            result.error = f"{type(e).__name__}: {str(e)[:200]}"
    return result


def disasm_cubin(disasm_template: str, cubin_bytes: bytes, timeout: int = 30) -> str:
    """Run a disassembler over a cubin blob, return the SASS text."""
    if not cubin_bytes:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".cubin", delete=False) as f:
        f.write(cubin_bytes)
        cubin_path = f.name
    try:
        # Same tokenize-then-substitute pattern as run_compiler so Windows
        # paths with backslashes survive POSIX shlex.
        tokens = shlex.split(disasm_template, posix=True)
        cmd_args = [tok.replace("{in}", cubin_path) for tok in tokens]
        proc = subprocess.run(
            cmd_args, capture_output=True, text=True, timeout=timeout
        )
        return proc.stdout if proc.returncode == 0 else ""
    finally:
        try:
            os.unlink(cubin_path)
        except OSError:
            pass


_OPCODE_RE = re.compile(r"\*/\s+(@\S+\s+)?([A-Z][A-Z0-9_.]+)")


def opcode_histogram(sass: str) -> dict[str, int]:
    counts: collections.Counter = collections.Counter()
    for line in sass.splitlines():
        m = _OPCODE_RE.search(line)
        if m:
            counts[m.group(2)] += 1
    return dict(counts)


def instr_count(sass: str) -> int:
    return sum(1 for ln in sass.splitlines() if "/*" in ln and "*/" in ln)


def compare_kernel(
    path: Path,
    ref_template: str,
    cand_template: str,
    disasm_template: Optional[str],
    timeout_compile: int = 60,
    timeout_disasm: int = 30,
) -> KernelResult:
    out = KernelResult(name=path.stem, path=str(path), ptx_size=path.stat().st_size)

    out.ref = run_compiler(ref_template, path, timeout=timeout_compile)
    out.cand = run_compiler(cand_template, path, timeout=timeout_compile)

    if out.ref.error or out.cand.error:
        return out

    out.byte_match = out.ref.cubin_bytes == out.cand.cubin_bytes

    if disasm_template and out.ref.cubin_bytes and out.cand.cubin_bytes:
        ref_sass = disasm_cubin(disasm_template, out.ref.cubin_bytes, timeout_disasm)
        cand_sass = disasm_cubin(disasm_template, out.cand.cubin_bytes, timeout_disasm)
        out.ref_n = instr_count(ref_sass)
        out.cand_n = instr_count(cand_sass)
        ref_hist = opcode_histogram(ref_sass)
        cand_hist = opcode_histogram(cand_sass)
        out.cand_unique_ops = sorted(set(cand_hist) - set(ref_hist))
        out.ref_unique_ops = sorted(set(ref_hist) - set(cand_hist))
        diff = {}
        for op in set(ref_hist) | set(cand_hist):
            r, c = ref_hist.get(op, 0), cand_hist.get(op, 0)
            if r != c:
                diff[op] = {"cand": c, "ref": r, "delta": c - r}
        # Sort by absolute delta descending for readability
        out.opcode_deltas = dict(
            sorted(diff.items(), key=lambda kv: -abs(kv[1]["delta"]))
        )

    return out


def render_markdown(results: list[KernelResult], header: dict) -> str:
    lines = []
    lines.append(f"# gpudiff report — {header['timestamp']}")
    lines.append("")
    lines.append(f"- ref:       `{header['ref_template']}`")
    lines.append(f"- candidate: `{header['cand_template']}`")
    lines.append(f"- corpus:    {header['corpus']}")
    lines.append(f"- kernels:   {len(results)}")
    lines.append("")
    summary = collections.Counter(r.verdict() for r in results)
    lines.append("## Summary")
    for verdict, count in sorted(summary.items()):
        lines.append(f"- **{verdict}**: {count}")
    lines.append("")

    lines.append("## Per-kernel")
    lines.append("")
    lines.append("| kernel | size | verdict | cand | ref | delta | top opcode deltas |")
    lines.append("|:---|---:|:---|---:|---:|---:|:---|")
    for r in results:
        verdict = r.verdict()
        if verdict in ("OURS_FAILED", "REF_FAILED", "BOTH_FAILED"):
            err = r.cand.error if "OURS" in verdict else r.ref.error
            lines.append(
                f"| `{r.name}` | {r.ptx_size//1024}KB | **{verdict}** "
                f"| — | — | — | {err[:80] if err else ''} |"
            )
            continue
        delta = r.cand_n - r.ref_n
        sign = "+" if delta >= 0 else ""
        if r.opcode_deltas:
            top = ", ".join(
                f"`{op}` {d['cand']}/{d['ref']}"
                for op, d in list(r.opcode_deltas.items())[:3]
            )
        else:
            top = "—"
        lines.append(
            f"| `{r.name}` | {r.ptx_size//1024}KB | **{verdict}** "
            f"| {r.cand_n} | {r.ref_n} | {sign}{delta} | {top} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_json(results: list[KernelResult], header: dict) -> str:
    return json.dumps(
        {"header": header, "results": [r.to_dict() for r in results]},
        indent=2,
    )


def render_junit(results: list[KernelResult], header: dict) -> str:
    """Emit JUnit XML so CI systems (GitHub Actions, GitLab CI, Jenkins) can
    consume the report and surface failures inline on the PR / build page."""
    import xml.etree.ElementTree as ET

    suite = ET.Element("testsuite")
    suite.set("name", "gpudiff")
    suite.set("tests", str(len(results)))
    suite.set("timestamp", header["timestamp"])

    fail_count = 0
    for r in results:
        case = ET.SubElement(suite, "testcase")
        case.set("classname", "gpudiff")
        case.set("name", r.name)
        case.set("time", f"{r.cand.elapsed_s + r.ref.elapsed_s:.3f}")
        v = r.verdict()
        if v != "BYTE_MATCH":
            fail_count += 1
            failure = ET.SubElement(case, "failure")
            failure.set("type", v)
            if v == "OURS_FAILED":
                failure.text = r.cand.error or "candidate compile failed"
            elif v == "REF_FAILED":
                failure.text = r.ref.error or "reference compile failed"
            else:
                failure.text = (
                    f"cand_n={r.cand_n} ref_n={r.ref_n} "
                    f"delta={r.cand_n - r.ref_n}; "
                    f"top deltas: {list(r.opcode_deltas.items())[:3]}"
                )
    suite.set("failures", str(fail_count))
    return ET.tostring(suite, encoding="unicode")


def expand_corpus(patterns: list[str]) -> list[Path]:
    """Expand glob patterns or directory paths into a list of input files."""
    paths: list[Path] = []
    for pat in patterns:
        p = Path(pat)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.ptx")))
            paths.extend(sorted(p.glob("*.cu")))
        else:
            for match in glob.glob(pat):
                paths.append(Path(match))
    return sorted(set(paths))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="gpudiff",
        description="Differential cubin tester for GPU compilers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--ref",
        required=True,
        help="Reference compiler template, e.g. 'ptxas -arch=sm_120 -o {out} {in}'",
    )
    p.add_argument(
        "--candidate",
        required=True,
        help="Candidate compiler template (same {in}/{out} placeholders)",
    )
    p.add_argument(
        "--corpus",
        nargs="+",
        required=True,
        help="Corpus paths (directories or glob patterns) of source files",
    )
    p.add_argument(
        "--disasm",
        default=None,
        help="Disassembler template with {in} placeholder. Default: nvdisasm -c {in}. "
        "Pass --no-disasm to skip opcode histograms.",
    )
    p.add_argument(
        "--no-disasm",
        action="store_true",
        help="Skip disassembly + opcode histograms (faster; verdict is byte-diff only)",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Markdown report output path. Default: stdout.",
    )
    p.add_argument(
        "--json",
        default=None,
        help="JSON output path (for programmatic consumption).",
    )
    p.add_argument(
        "--junit",
        default=None,
        help="JUnit XML output path (for CI integration).",
    )
    p.add_argument(
        "--timeout-compile",
        type=int,
        default=60,
        help="Per-compile timeout in seconds (default: 60).",
    )
    p.add_argument(
        "--timeout-disasm",
        type=int,
        default=30,
        help="Per-disasm timeout in seconds (default: 30).",
    )
    p.add_argument(
        "--fail-on",
        choices=["any", "regression", "never"],
        default="any",
        help="Exit code policy: 'any' (any non-BYTE_MATCH = exit 1), "
        "'regression' (only OURS_FAILED or new MAJOR_DIFF = exit 1), "
        "'never' (always exit 0).  Default: any.",
    )
    p.add_argument(
        "--baseline",
        default=None,
        help="Path to a previous --json output. With --fail-on regression, "
        "exit non-zero only if a kernel regressed vs this baseline.",
    )
    p.add_argument("--version", action="version", version=f"gpudiff {__version__}")

    args = p.parse_args(argv)

    if args.no_disasm:
        disasm_template = None
    elif args.disasm:
        disasm_template = args.disasm
    else:
        disasm_template = "nvdisasm -c {in}"

    paths = expand_corpus(args.corpus)
    if not paths:
        print(f"gpudiff: no input files matched corpus={args.corpus}", file=sys.stderr)
        return 2

    results: list[KernelResult] = []
    for path in paths:
        r = compare_kernel(
            path,
            ref_template=args.ref,
            cand_template=args.candidate,
            disasm_template=disasm_template,
            timeout_compile=args.timeout_compile,
            timeout_disasm=args.timeout_disasm,
        )
        results.append(r)

    header = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ref_template": args.ref,
        "cand_template": args.candidate,
        "disasm_template": disasm_template,
        "corpus": args.corpus,
        "n_kernels": len(results),
        "version": __version__,
    }

    md = render_markdown(results, header)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
    else:
        # Force UTF-8 on stdout to survive Windows code-page defaults
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
        print(md)

    if args.json:
        Path(args.json).write_text(render_json(results, header), encoding="utf-8")
    if args.junit:
        Path(args.junit).write_text(render_junit(results, header), encoding="utf-8")

    # Exit code policy
    if args.fail_on == "never":
        return 0

    if args.fail_on == "regression" and args.baseline:
        try:
            base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            base_verdicts = {
                r["name"]: r["verdict"] for r in base.get("results", [])
            }
        except Exception as e:
            print(f"gpudiff: failed to load baseline: {e}", file=sys.stderr)
            return 2
        for r in results:
            base_v = base_verdicts.get(r.name)
            now_v = r.verdict()
            # Regression = something that was BYTE_MATCH or MAJOR_DIFF before
            # is now FAILED, or BYTE_MATCH dropped to MAJOR_DIFF.
            if base_v == "BYTE_MATCH" and now_v != "BYTE_MATCH":
                return 1
            if base_v == "MAJOR_DIFF" and now_v in ("OURS_FAILED", "BOTH_FAILED"):
                return 1
        return 0

    # fail_on == "any" or "regression" without baseline
    for r in results:
        if r.verdict() != "BYTE_MATCH":
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
