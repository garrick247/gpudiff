#!/usr/bin/env python3
"""CLI shim that wraps openptxas (a Python module) into a CLI compiler
matching gpudiff's {in}/{out} contract.

Usage:
    python openptxas_shim.py <input.ptx> <output.cubin>
    OPENPTXAS=C:/Users/kraken/openptxas python openptxas_shim.py in.ptx out.cubin
"""
import os
import sys


def main(argv):
    if len(argv) != 3:
        print("usage: openptxas_shim.py <input.ptx> <output.cubin>", file=sys.stderr)
        return 2

    _, in_path, out_path = argv
    sys.path.insert(0, os.environ.get("OPENPTXAS", r"C:\Users\kraken\openptxas"))
    from sass.pipeline import compile_ptx_source

    with open(in_path, encoding="utf-8") as f:
        ptx = f.read()

    result = compile_ptx_source(ptx)

    if isinstance(result, dict):
        if not result:
            print("openptxas returned empty result dict", file=sys.stderr)
            return 1
        # Take first kernel's cubin (single-kernel inputs mostly).  For
        # multi-entry PTX, gpudiff's per-entry support handles the split
        # at the corpus level by passing per-entry PTX shards.
        cubin = next(iter(result.values()))
    else:
        cubin = result

    with open(out_path, "wb") as f:
        f.write(cubin)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
