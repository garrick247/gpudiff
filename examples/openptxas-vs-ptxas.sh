#!/usr/bin/env bash
# Example: differential-test openptxas against ptxas on the Forge corpus.
#
# This is the exact use case gpudiff was extracted from. Replace the paths
# and invocations with your own setup.

set -e

OPENPTXAS=${OPENPTXAS:-C:/Users/kraken/openptxas}
PTXAS=${PTXAS:-"C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.2/bin/ptxas.exe"}
NVDISASM=${NVDISASM:-"C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.2/bin/nvdisasm.exe"}
CORPUS=${CORPUS:-C:/Users/kraken/forge/analysis/vortex_ntt}

# Wrap openptxas (Python module) as a CLI shim that takes {in}/{out}.
# This is the "wrap your Python compiler in a CLI" pattern from the README.
OPENPTXAS_SHIM="$(mktemp -d)/openptxas-shim.py"
cat > "$OPENPTXAS_SHIM" <<'PYTHON'
"""CLI shim: read a PTX file, compile via openptxas, write the cubin."""
import sys, os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.environ["OPENPTXAS"])
from sass.pipeline import compile_ptx_source

# Args: <input-ptx> <output-cubin>
_, in_path, out_path = sys.argv
ptx = open(in_path, encoding="utf-8").read()
result = compile_ptx_source(ptx)
# Pick the first kernel's cubin (corpus has single-kernel files mostly)
cubin = next(iter(result.values())) if isinstance(result, dict) else result
with open(out_path, "wb") as f:
    f.write(cubin)
PYTHON

OPENPTXAS=$OPENPTXAS python gpudiff.py \
    --ref       "$PTXAS -arch=sm_120 -o {out} {in}" \
    --candidate "python $OPENPTXAS_SHIM {in} {out}" \
    --corpus    "$CORPUS" \
    --disasm    "$NVDISASM -c {in}" \
    --output    /tmp/gpudiff-report.md \
    --json      /tmp/gpudiff-report.json \
    --junit     /tmp/gpudiff-report.xml \
    --fail-on   regression \
    --baseline  ./examples/openptxas-baseline.json

cat /tmp/gpudiff-report.md
