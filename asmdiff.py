#!/usr/bin/env python3
"""Compare per-function assembly between paired C implementations.

Compiles a harness C file across a matrix of compilers, extracts each
variant function's assembly from the -S output, and prints side-by-side
listings plus a summary of instruction counts and outbound calls.
Automates fold-vs-libcall analysis when evaluating micro-optimisations
(e.g. "does this still compile to one instruction, or is it a libcall?").

Usage:
    tools/asmdiff/asmdiff.py SOURCE.c [SOURCE2.c] [--pair OLD:NEW]...
                             [--across FUNC]... [--cc 'CC FLAGS']...
                             [-- EXTRA_FLAGS...]

Two comparison modes:
  --pair OLD:NEW  compares two different functions within one compilation
                  (default; with no --pair, old_X/new_X names auto-pair).
  --across FUNC   compares the SAME function across two compilations:
                  either one file under two --cc entries (flag/define
                  variants), or two source files (before/after versions)
                  under each compiler in the matrix.

With no --cc, gcc and clang are each run with AMY's Makefile flags.
Flags after a bare `--` are appended to every compiler invocation.
"""
import argparse
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from itertools import zip_longest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[2] / "src"

# AMY's real build flags, from the repo Makefile.  When porting this tool
# to another project, replace these with that project's release flags —
# the whole point is to read asm at the flags the code actually ships with.
AMY_CFLAGS = [
    "-O3", "-Wall", "-Wno-strict-aliasing", "-Wextra",
    "-Wno-unused-parameter", "-Wpointer-arith", "-Wno-float-conversion",
    "-Wno-missing-declarations", "-DAMY_WAVETABLE",
]
# When the tool lives inside the AMY repo, let a harness #include "amy.h"
# and exercise the real macros.  Harmlessly absent anywhere else.
if SRC_DIR.is_dir():
    AMY_CFLAGS.append("-I" + str(SRC_DIR))
DEFAULT_COMPILERS = ["gcc", "clang"]

# A label at column 0 that is not a local (.L*) label starts a function.
FUNC_LABEL = re.compile(r"^([A-Za-z_][\w$.]*):")
# Assembler directives that carry no information worth reading.
NOISE = re.compile(
    r"^\s*\.(cfi_|p2align|align\b|loc\b|file\b|text\b|globl\b|global\b|"
    r"type\b|section\b|ident\b|weak\b|hidden\b|addrsig|build_version)"
)
# Compiler-generated bracketing labels that add nothing (.LFB0:, .Lfunc_end0:).
NOISE_LABEL = re.compile(r"^\.(LFB|LFE|Lfunc_begin|Lfunc_end)\d*:")


def extract_functions(asm_text):
    """Map function name -> cleaned asm lines from compiler -S output.

    A function body runs from its column-0 label to the matching .size
    directive (gcc and clang both emit one on ELF) or the next function
    label.  Comment lines, CFI/section/alignment directives, and
    compiler bracketing labels are dropped; instructions and meaningful
    local labels (loop targets) are kept, whitespace-stripped.
    """
    funcs = {}
    current = None
    for raw in asm_text.splitlines():
        m = FUNC_LABEL.match(raw)
        if m:
            current = m.group(1)
            funcs[current] = []
            continue
        if current is None:
            continue
        if re.match(r"^\s*\.size\b", raw):
            current = None
            continue
        line = raw.strip()
        if not line or line.startswith(("#", "//")):
            continue
        if NOISE.match(line) or NOISE_LABEL.match(line):
            continue
        funcs[current].append(line)
    return funcs


# Direct-call / tail-call mnemonics across x86 (call, jmp), ARM (bl, blx),
# RISC-V (call, tail, jal), and Xtensa (call0/4/8/12, callx*, j).  Longest
# alternatives first so e.g. "callx8" is not consumed as "call".  The
# symbol must be the sole/final operand (optionally @PLT-suffixed), so
# multi-operand forms like "jal ra, exp2f" don't report the register.
CALL_RE = re.compile(
    r"^(?:callx\d+|call\d*|callq|jalr|jal|jmp|blx|bl|tail|j)\s+"
    r"([A-Za-z_][\w$.]*)(?:@[\w.]+)?\s*(?:[#;].*)?$"
)


def analyze(lines):
    """Return (instruction_count, called_symbols) for cleaned asm lines.

    A call is a call/tail-call mnemonic whose first operand looks like a
    symbol name — local labels (.L*) and %-registers never match, so
    branches inside the function are not counted.
    """
    insns = 0
    calls = []
    for line in lines:
        if line.endswith(":"):
            continue
        insns += 1
        m = CALL_RE.match(line)
        if m:
            sym = m.group(1)
            if sym not in calls:
                calls.append(sym)
    return insns, calls


def auto_pairs(names):
    """Pair old_X with new_X for every X present in both."""
    names = list(names)
    return [(n, "new_" + n[4:]) for n in names
            if n.startswith("old_") and "new_" + n[4:] in names]


def build_matrix(cc_args):
    """Explicit --cc strings verbatim, else gcc+clang with AMY's flags."""
    if cc_args:
        return list(cc_args)
    return [" ".join([cc] + AMY_CFLAGS) for cc in DEFAULT_COMPILERS]


def compile_to_asm(cc_cmd, extra_flags, harness, out_dir):
    """Run one compiler to -S; return the asm text.

    Returns None (with a warning) if the compiler is not on PATH.
    Exits with the compiler's stderr on a compile failure.
    """
    argv = shlex.split(cc_cmd)
    if shutil.which(argv[0]) is None:
        print(f"warning: {argv[0]} not found on PATH, skipping",
              file=sys.stderr)
        return None
    out_s = Path(out_dir) / (
        re.sub(r"\W+", "_", cc_cmd) + "_" + Path(harness).stem + ".s")
    cmd = argv + list(extra_flags) + ["-S", "-o", str(out_s), str(harness)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"error: compile failed: {' '.join(cmd)}\n{proc.stderr}")
    return out_s.read_text()


def side_by_side(left, right, ltitle, rtitle, width=44):
    """Two-column view of a pair's asm lines."""
    rows = [f"{ltitle:<{width}} | {rtitle}",
            f"{'-' * width}-+-{'-' * width}"]
    for l, r in zip_longest(left, right, fillvalue=""):
        l = l.expandtabs(8)[:width]
        r = r.expandtabs(8)[:width]
        rows.append(f"{l:<{width}} | {r}")
    return "\n".join(rows)


def summary_table(pairs, funcs):
    """Instruction counts and outbound calls for every pair member."""
    rows = [("function", "role", "insns", "calls")]
    for old, new in pairs:
        for name, role in ((old, "baseline"), (new, "candidate")):
            insns, calls = analyze(funcs[name])
            rows.append((name, role, str(insns), ", ".join(calls) or "-"))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip()
        for row in rows)


def file_tags(a, b):
    """Shortest distinct labels for two source paths in across-mode output."""
    pa, pb = Path(a), Path(b)
    if pa.name != pb.name:
        return pa.name, pb.name
    ta = f"{pa.parent.name}/{pa.name}"
    tb = f"{pb.parent.name}/{pb.name}"
    if ta != tb:
        return ta, tb
    return str(a), str(b)


def report_across(fn_names, left_funcs, right_funcs, left_tag, right_tag):
    """Side-by-side + summary for the same functions from two compilations."""
    missing = sorted({f for f in fn_names
                      if f not in left_funcs or f not in right_funcs})
    if missing:
        sys.exit("error: function(s) not in asm: " + ", ".join(missing)
                 + f"; {left_tag} has: " + (", ".join(left_funcs) or "none")
                 + f"; {right_tag} has: " + (", ".join(right_funcs) or "none"))
    decorated, pairs = {}, []
    for f in fn_names:
        lt, rt = f"{f} [{left_tag}]", f"{f} [{right_tag}]"
        decorated[lt], decorated[rt] = left_funcs[f], right_funcs[f]
        pairs.append((lt, rt))
    for lt, rt in pairs:
        print(side_by_side(decorated[lt], decorated[rt], lt, rt))
        print()
    print(summary_table(pairs, decorated))


def run_across(sources, matrix, fn_names, extra_flags, tmp):
    """--across mode: same function, two compilations.

    Two source files: compare fileA's FUNC vs fileB's FUNC under each
    compiler in the matrix.  One source file: compare FUNC between the
    first --cc entry (baseline) and each subsequent entry.
    """
    if len(sources) == 2:
        ran_any = False
        for cc_cmd in matrix:
            sides = []
            for src in sources:
                asm = compile_to_asm(cc_cmd, extra_flags, src, tmp)
                if asm is None:
                    break
                sides.append(extract_functions(asm))
            if len(sides) < 2:
                continue
            ran_any = True
            print(f"\n== {cc_cmd} ==\n")
            report_across(fn_names, sides[0], sides[1],
                          *file_tags(sources[0], sources[1]))
        if not ran_any:
            sys.exit("error: no usable compiler in the matrix")
        return 0

    usable = []
    for idx, cc_cmd in enumerate(matrix, start=1):
        asm = compile_to_asm(cc_cmd, extra_flags, sources[0], tmp)
        if asm is not None:
            usable.append((f"cc#{idx}", cc_cmd, extract_functions(asm)))
    if len(usable) < 2:
        sys.exit("error: --across needs at least two usable compilers "
                 "in the matrix")
    print()
    for tag, cc_cmd, _ in usable:
        print(f"{tag}: {cc_cmd}")
    base_tag, _, base_funcs = usable[0]
    for tag, _, funcs in usable[1:]:
        print(f"\n== {base_tag} vs {tag} ==\n")
        report_across(fn_names, base_funcs, funcs, base_tag, tag)
    return 0


def run_pairs(source, matrix, pair_specs, extra_flags, tmp):
    """--pair mode: two different functions within one compilation."""
    ran_any = False
    for cc_cmd in matrix:
        asm = compile_to_asm(cc_cmd, extra_flags, source, tmp)
        if asm is None:
            continue
        ran_any = True
        funcs = extract_functions(asm)
        pairs = ([tuple(p.split(":", 1)) for p in pair_specs]
                 or auto_pairs(funcs))
        if not pairs:
            sys.exit("error: no --pair given and no old_X/new_X "
                     "functions found; functions in asm: "
                     + (", ".join(funcs) or "none"))
        missing = sorted({n for p in pairs for n in p if n not in funcs})
        if missing:
            sys.exit("error: function(s) not in asm: "
                     + ", ".join(missing)
                     + "; functions seen: " + ", ".join(funcs))
        print(f"\n== {cc_cmd} ==\n")
        for old, new in pairs:
            print(side_by_side(funcs[old], funcs[new], old, new))
            print()
        print(summary_table(pairs, funcs))
    if not ran_any:
        sys.exit("error: no usable compiler in the matrix")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    extra_flags = []
    if "--" in argv:
        cut = argv.index("--")
        argv, extra_flags = argv[:cut], argv[cut + 1:]

    parser = argparse.ArgumentParser(
        description=(__doc__ or "").partition("\n")[0],
        epilog="Flags after a bare -- are appended to every compiler "
               "invocation, e.g.: asmdiff.py h.c -- -fno-math-errno")
    parser.add_argument("sources", nargs="+", metavar="SOURCE.c",
                        help="C file to compile; give two files with "
                             "--across to compare versions of a function")
    parser.add_argument("--pair", action="append", default=[],
                        metavar="OLD:NEW",
                        help="compare two functions within one compilation "
                             "(repeatable); default: auto-pair old_X/new_X")
    parser.add_argument("--across", action="append", default=[],
                        metavar="FUNC",
                        help="compare the same function across two "
                             "compilations (repeatable): one file + two "
                             "--cc entries, or two files")
    parser.add_argument("--cc", action="append", default=[],
                        metavar="'CC FLAGS'",
                        help="compiler and flags as one string (repeatable); "
                             "default: gcc and clang with AMY's Makefile flags")
    args = parser.parse_args(argv)

    if len(args.sources) > 2:
        parser.error("at most two source files may be given")
    if args.across and args.pair:
        parser.error("--across and --pair are mutually exclusive")
    if len(args.sources) == 2 and not args.across:
        parser.error("two source files require --across FUNC")
    matrix = build_matrix(args.cc)
    if args.across and len(args.sources) == 1 and len(matrix) < 2:
        parser.error("--across on one file needs at least two --cc entries")
    for spec in args.pair:
        if ":" not in spec:
            parser.error(f"--pair expects OLD:NEW, got {spec!r}")

    with tempfile.TemporaryDirectory(prefix="asmdiff") as tmp:
        if args.across:
            return run_across(args.sources, matrix, args.across,
                              extra_flags, tmp)
        return run_pairs(args.sources[0], matrix, args.pair,
                         extra_flags, tmp)


if __name__ == "__main__":
    sys.exit(main())
