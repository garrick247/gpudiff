# gpudiff

**Differential cubin tester for GPU compilers.**

Take two compilers and a corpus. Compile each input through both. Byte-diff
the outputs. Get a per-kernel verdict (`BYTE_MATCH` / `MAJOR_DIFF` /
`OURS_FAILED`) plus opcode-histogram deltas. Wire into CI.

There's no public tool that does this for GPU compilers. NVIDIA's
`cuobjdump` and `nvdisasm` give you per-binary output but no diff harness;
fuzzers like csmith find bugs but don't track corpus regressions.
`CuAssembler` does cubin parsing but isn't a comparator. `gpudiff` is what
you actually need to gate "did my optimizer regression-break a kernel
that was BYTE_MATCH yesterday?"

## Install

```bash
pip install gpudiff
```

Or directly from source:

```bash
git clone https://github.com/<user>/gpudiff
cd gpudiff
pip install -e .
```

Requires Python 3.10+. No dependencies beyond the standard library.

## Quick start

```bash
gpudiff \
    --ref       'ptxas -arch=sm_120 -o {out} {in}' \
    --candidate 'mycompiler -arch=sm_120 -o {out} {in}' \
    --corpus    ./test-kernels/*.ptx \
    --output    report.md \
    --junit     report.xml
```

Both compilers are specified as **shell command templates** with two
placeholders: `{in}` for the input source file, `{out}` for the output
binary. `gpudiff` invokes both, expects exit-0 plus an `{out}` file, and
classifies each kernel.

No Python API coupling. If your compiler is Python-only, wrap it in a
CLI shim.

## Use cases

### CI gate

Drop into GitHub Actions / GitLab CI. JUnit output integrates with the
PR view; non-zero exit blocks merge if anything regressed.

```yaml
# .github/workflows/gpudiff.yml
- run: |
    gpudiff \
      --ref       'ptxas -arch=sm_120 -o {out} {in}' \
      --candidate './my-compiler -arch=sm_120 -o {out} {in}' \
      --corpus    ./tests/corpus/ \
      --junit     gpudiff.xml \
      --baseline  ./tests/gpudiff-baseline.json \
      --fail-on   regression
- uses: dorny/test-reporter@v1
  with:
    name: gpudiff
    path: gpudiff.xml
    reporter: java-junit
```

### Bisect

Combine with `git bisect run` to narrow the introducing commit when a
kernel regresses:

```bash
git bisect start HEAD known-good
git bisect run gpudiff \
    --ref       'ptxas -arch=sm_120 -o {out} {in}' \
    --candidate './build/mycomp -arch=sm_120 -o {out} {in}' \
    --corpus    ./tests/regression/blake2.ptx \
    --fail-on   any
```

Bisect terminates on the first commit where the kernel stops being
`BYTE_MATCH`.

### Research / paper-track

Run periodically, dump JSON, build a time-series of `BYTE_MATCH%`. The
JSON output includes per-kernel cubin SHA, instruction counts, opcode
histogram deltas, and per-compile elapsed time.

### Differential bug-finding

Feed a fuzzer's output corpus through `gpudiff` to surface miscompiles:
when both compilers exit-0 but produce byte-different output, that's
either an under-specified ISA point or a real bug in one. Cross-reference
hardware execution to determine which.

## Verdict definitions

- `BYTE_MATCH` — cubin bytes are identical
- `MAJOR_DIFF` — cubins differ; opcode histogram delta in report
- `OURS_FAILED` — candidate compiler errored or produced no output
- `REF_FAILED` — reference compiler errored (rare; usually a bad input)
- `BOTH_FAILED` — both errored (input is malformed for both)

## Output formats

| Flag | Format | Use |
|------|--------|-----|
| `--output report.md` | Markdown | Human reading; PR comment bodies |
| `--json report.json` | JSON | Programmatic consumption; baseline tracking |
| `--junit report.xml` | JUnit XML | CI integration (GitHub Actions, GitLab, Jenkins) |

If `--output` is omitted, markdown goes to stdout.

## Exit codes

| Mode | Exit 1 when |
|------|-------------|
| `--fail-on any` (default) | Any kernel is not `BYTE_MATCH` |
| `--fail-on regression --baseline prev.json` | A kernel regressed vs the baseline JSON |
| `--fail-on never` | Never (always exit 0) |
| Exit 2 | Argument or input-resolution error |

## Disassembler

By default, `gpudiff` invokes `nvdisasm -c {in}` to produce SASS for the
opcode histogram. Override with `--disasm 'mydisasm --opt {in}'` if you
have a different tool. `--no-disasm` skips the histogram entirely (~30%
faster; verdict is then byte-diff only).

## Limitations

- **Single-architecture per run.** If your build emits multiple SM
  targets, run `gpudiff` once per target.
- **No cross-cubin link diffs.** Each input is compared independently.
- **No semantic / runtime diff.** Two byte-different cubins may produce
  identical output on real hardware. `gpudiff` is byte-comparison only;
  use a separate runtime test for semantic equivalence.

## Development

The full session that motivated this tool — 37 phases of openptxas vs
ptxas differential testing on the Forge corpus — is documented in the
companion repo. `gpudiff`'s comparator core is the extracted, generalized
form of `forge_kernel_compare.py`.

## License

MIT
