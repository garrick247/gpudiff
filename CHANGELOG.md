# Changelog

## 0.1.0 — initial release

First public version. Extracted from the openptxas / FORGE compiler
differential-testing infrastructure.

### Features

- Differential cubin tester: take two compilers + a corpus, classify each
  kernel as `BYTE_MATCH` / `MAJOR_DIFF` / `OURS_FAILED` / `REF_FAILED`.
- Pluggable compiler invocations via `{in}` / `{out}` shell templates.
- Pluggable disassembler for opcode histogram (default: `nvdisasm -c`).
- Output formats: Markdown (human), JSON (programmatic), JUnit XML (CI).
- Exit-code policies: `--fail-on any | regression | never`.
- Baseline-comparison mode (`--baseline prev.json`) for regression-only CI.
- Tested on Windows + Linux. POSIX-style template tokenization with
  per-token placeholder substitution to avoid Windows-path-mangling.
