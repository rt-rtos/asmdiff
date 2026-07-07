#!/usr/bin/env python3
"""Compare per-function assembly between paired C implementations.

Compiles a harness C file across a matrix of compilers, extracts each
variant function's assembly from the -S output, and prints side-by-side
listings plus a summary of instruction counts, outbound calls, and loop
spans (instructions between a local label and its last backward branch).
Automates fold-vs-libcall analysis when evaluating micro-optimisations
(e.g. "does this still compile to one instruction, or is it a libcall?").

Usage:
    tools/asmdiff/asmdiff.py SOURCE.c [SOURCE2.c | FUNC...]
                             [--pair OLD:NEW]... [--across FUNC]...
                             [--cc 'CC FLAGS']... [--target NAME]...
                             [--config PATH] [--compile-commands [PATH]]
                             [--flags-like PATH]
                             [--layout list|side-by-side] [-v]
                             [-- EXTRA_FLAGS...]
    tools/asmdiff/asmdiff.py --edit-config | --example-config

Four modes:
  SOURCE.c FUNC   inspect: print the named function's assembly, no
                  comparison.  One usable compiler prints a listing
                  plus a stats row, exactly two print side by side,
                  more print one block per compiler; --layout forces
                  list or side-by-side.
  --pair OLD:NEW  compares two different functions within one compilation
                  (with no --pair, old_X/new_X names auto-pair).
  --across FUNC   compares the SAME function across two compilations:
                  either one file under two --cc entries (flag/define
                  variants), or two source files (before/after versions)
                  under each compiler in the matrix.
  (neither)       whole-file summary: per-function counts plus a file
                  total, for one file or side by side for two.

Compilers come from --cc strings, from named targets in an asmdiff.toml
config file (--target NAME), from the config's `default` entry, or —
failing all of those — plain `gcc -O3` and `clang -O3`.
Flags after a bare `--` are appended to every compiler invocation.
--edit-config opens the config (--config PATH, else ~/.config/
asmdiff.toml) in $VISUAL/$EDITOR, creating it from the built-in example
when missing; --example-config prints that example to stdout.

A config target may name a compile_commands.json (compile_commands = PATH):
the include/define flags recorded there for the source being compiled are
added to that target's command, so a real project source resolves its
headers the way its own build system does (e.g. an ESP-IDF component).
compile_commands = true in a target — or a bare --compile-commands on the
command line — instead finds the database by checking each directory from
the CWD up to the repository root, and its build/ subdirectory; a source
absent from a database found that way is compiled without borrowed flags
(with a note) rather than being an error.  --flags-like PATH lets such a
source borrow the flags recorded for PATH, so a modified copy compares
against its original under one header environment.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from itertools import zip_longest
from pathlib import Path

try:
    import tomllib  # Python >= 3.11; config files are optional without it
except ModuleNotFoundError:
    tomllib = None

DEFAULT_COMPILERS = ["gcc", "clang"]
FALLBACK_FLAGS = "-O3"
CONFIG_NAME = "asmdiff.toml"

# Mirror of asmdiff.example.toml, embedded because the wheel ships only
# this module: --example-config prints it and --edit-config seeds a new
# config from it.  A unit test keeps it byte-identical to the repo file.
EXAMPLE_CONFIG = """\
# asmdiff config — named compiler+flags targets.
#
# Copy to one of the locations asmdiff searches (first hit wins):
#   1. the path given with --config
#   2. asmdiff.toml next to the SOURCE.c being compiled
#   3. asmdiff.toml in the current directory
#   4. ~/.config/asmdiff.toml
#
# Each [table] is a target usable as `--target NAME`; the optional
# top-level `default` names the target(s) used when no --cc/--target
# is given (a list runs several: default = ["gcc", "clang"]).
# Compile at the flags your project ships with — that is the whole
# point of the tool.

default = "gcc"

[gcc]
cc = "gcc"
flags = [
  "-O3", "-Wall", "-Wextra",
  # Project-specific include paths and defines go here:
  # "-I/path/to/project/src", "-DMY_FEATURE",
]

[clang]
cc = "clang"
flags = ["-O3", "-Wall", "-Wextra"]

# cc values may use ~, $VARS, and glob patterns.  A glob that matches
# several installed toolchains resolves to the highest version-sorted
# one (announced on stderr); pin the exact esp-NN directory instead if
# reproducibility across sessions matters more than convenience.
# On native Windows HOME is usually unset - use $USERPROFILE (or
# %USERPROFILE%) in the patterns below instead.
#
# The ESP profiles below keep flags minimal (-O2 plus arch selection);
# append what your project ships with (-DNDEBUG, -Os, -I..., ...).
# A source that pulls in framework headers (ESP-IDF's freertos/*, a
# generated sdkconfig.h, ...) additionally needs the build's -I/-D
# flags: add `compile_commands = true` (or a path) to any target to
# borrow them from the build's compile_commands.json - see README.

# --- Profile: riscv32-esp (ESP32-C3 / C6 / H2 / P4) -------------------
# All RISC-V ESP chips share one riscv32-esp-elf-gcc binary; the
# targets differ only in -march/-mabi.  Uncomment to run the whole
# profile as the default matrix:
# default = ["esp32c3", "esp32c6", "esp32h2", "esp32p4"]

[esp32c3]
cc = "$HOME/.espressif/tools/riscv32-esp-elf/esp-*/riscv32-esp-elf/bin/riscv32-esp-elf-gcc"
flags = ["-O2", "-march=rv32imc_zicsr_zifencei", "-mabi=ilp32"]

[esp32c6]
cc = "$HOME/.espressif/tools/riscv32-esp-elf/esp-*/riscv32-esp-elf/bin/riscv32-esp-elf-gcc"
flags = ["-O2", "-march=rv32imac_zicsr_zifencei", "-mabi=ilp32"]

[esp32h2]
cc = "$HOME/.espressif/tools/riscv32-esp-elf/esp-*/riscv32-esp-elf/bin/riscv32-esp-elf-gcc"
flags = ["-O2", "-march=rv32imac_zicsr_zifencei", "-mabi=ilp32"]

# ESP32-P4 is the only ESP RISC-V chip with an FPU, hence the
# hard-float ABI.
[esp32p4]
cc = "$HOME/.espressif/tools/riscv32-esp-elf/esp-*/riscv32-esp-elf/bin/riscv32-esp-elf-gcc"
flags = ["-O2", "-march=rv32imafc_zicsr_zifencei", "-mabi=ilp32f"]

# --- Profile: xtensa-esp (ESP32 / S2 / S3) ----------------------------
# The unified xtensa-esp-elf toolchain ships one gcc binary per chip.
# default = ["esp32", "esp32s2", "esp32s3"]

[esp32]
cc = "$HOME/.espressif/tools/xtensa-esp-elf/esp-*/xtensa-esp-elf/bin/xtensa-esp32-elf-gcc"
flags = ["-O2", "-mlongcalls"]

[esp32s2]
cc = "$HOME/.espressif/tools/xtensa-esp-elf/esp-*/xtensa-esp-elf/bin/xtensa-esp32s2-elf-gcc"
flags = ["-O2", "-mlongcalls"]

[esp32s3]
cc = "$HOME/.espressif/tools/xtensa-esp-elf/esp-*/xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc"
flags = ["-O2", "-mlongcalls"]

# --- Profile: other embedded (STM32 / RP2350) -------------------------
# Unlike the ESP toolchains these have no single well-known install
# path: the compilers must be on PATH, or edit cc to a full path.

# Any Cortex-M STM32; adjust -mcpu to your family (cortex-m0plus,
# cortex-m3, cortex-m7, cortex-m33, ...) and add -mfloat-abi/-mfpu
# flags if your project uses the FPU.
[stm32]
cc = "arm-none-eabi-gcc"
flags = ["-O2", "-mcpu=cortex-m4", "-mthumb"]

# RP2350 Hazard3 cores in RISC-V mode; riscv64-unknown-elf-gcc is
# multilib, -march/-mabi select rv32.  Hazard3 also implements the
# Zba/Zbb/Zbs/Zbkb extensions (append to -march on gcc >= 13, as
# pico-sdk does).
[rp2350]
cc = "riscv64-unknown-elf-gcc"
flags = ["-O2", "-march=rv32imac_zicsr_zifencei", "-mabi=ilp32"]
"""

# Preprocessor flags lifted from a compile_commands.json entry so a project
# source compiles the way its build system compiles it: header search paths,
# forced includes, and defines.  Everything else the entry records (the
# compiler, -O/-std/-W flags, -c, -o OUT, the source itself) is ignored —
# asmdiff supplies the compiler and optimisation flags from the target.
# Path-bearing flags may be glued (-Ipath) or split (-I path); defines too
# (-DFOO / -D FOO).  None of the path flags is a prefix of another and -I is
# case-distinct from -i*, so a single left-to-right scan is unambiguous.
_CC_DB_PATH_FLAGS = ("-I", "-iquote", "-isystem", "-idirafter",
                     "-include", "-imacros", "-isysroot")
_CC_DB_PLAIN_FLAGS = ("-D", "-U")
# Driver-level flags that also shape the header environment: a specs file
# can swap the entire libc header set (ESP-IDF v6 selects picolibc via
# -specs=picolibc.specs), and --sysroot moves every system include.  Both
# accept =-glued and split spellings; both are re-emitted =-glued.
_CC_DB_EQ_FLAGS = ("-specs", "--specs", "--sysroot")
_DB_CACHE = {}
_MISS_NOTED = set()
_BORROW_NOTED = set()
# --flags-like PATH: a source with no database entry borrows the flags
# recorded for PATH.  This is how a modified copy of a project source gets
# the same header environment as its original, so an --across between them
# compares codegen instead of header configurations.
FLAGS_LIKE = None


class Target(str):
    """A compiler-matrix entry: the ``CC FLAGS`` command string, plus an
    optional compile_commands.json whose per-source include/define flags are
    appended at compile time.  Subclassing str means it prints, compares, and
    shlex-splits as the bare command everywhere the matrix is consumed, so
    only compile_to_asm needs to know about the extra attributes.

    ``db_discovered`` marks a database that was found by searching near the
    CWD rather than named explicitly; a source absent from a discovered
    database is tolerated (compiled without borrowed flags, with a note),
    where an explicit database makes that an error."""

    def __new__(cls, cmd, compile_commands=None, db_discovered=False):
        self = super().__new__(cls, cmd)
        self.compile_commands = compile_commands
        self.db_discovered = db_discovered
        return self


def _abs_against(directory, value):
    """Resolve a path recorded in a compile_commands entry against that
    entry's ``directory``, so a relative -I still works from asmdiff's CWD."""
    p = Path(value)
    if directory and not p.is_absolute():
        p = Path(directory) / p
    return str(p)


def _expand_response_files(tokens, directory, depth=0):
    """Inline GCC @file response files: each @FILE token is replaced by the
    shlex-split contents of FILE, resolved against the entry's ``directory``
    (the CWD the driver ran from).  Build systems park header-environment
    flags there — ESP-IDF v6 hides -specs=picolibc.specs in one — so
    skipping them silently loses flags this feature exists to borrow.
    An unreadable file is warned about and dropped; nesting is bounded as
    a cycle guard.
    """
    out = []
    for tok in tokens:
        if not tok.startswith("@") or len(tok) == 1:
            out.append(tok)
            continue
        path = _abs_against(directory, tok[1:])
        try:
            content = Path(path).read_text()
        except OSError:
            print(f"warning: response file {path} not readable; "
                  "flags inside it are not borrowed", file=sys.stderr)
            continue
        inner = shlex.split(content)
        if depth < 8:
            inner = _expand_response_files(inner, directory, depth + 1)
        out += inner
    return out


def _specs_value(directory, value):
    """A specs argument with a path separator is a file path (resolved like
    any other); a bare name (picolibc.specs) is looked up in the compiler's
    own search directories and must pass through untouched."""
    if "/" in value or os.sep in value:
        return _abs_against(directory, value)
    return value


def include_flags(tokens, directory):
    """Pick header-search, forced-include, and define flags out of one
    recorded compile command; make relative paths absolute against
    ``directory``.  Glued and split spellings are both recognised; path
    flags are re-emitted in split form (-I path), which every driver accepts.
    """
    flags = []
    i, n = 0, len(tokens)
    while i < n:
        tok, extra, matched = tokens[i], 0, False
        for f in _CC_DB_PATH_FLAGS:
            if tok == f and i + 1 < n:                    # -I path
                flags += [f, _abs_against(directory, tokens[i + 1])]
                extra, matched = 1, True
                break
            if tok.startswith(f) and len(tok) > len(f):   # -Ipath
                flags += [f, _abs_against(directory, tok[len(f):])]
                matched = True
                break
        if not matched:
            for f in _CC_DB_EQ_FLAGS:
                value = None
                if tok == f and i + 1 < n:                # -specs file
                    value, extra = tokens[i + 1], 1
                elif tok.startswith(f + "="):             # -specs=file
                    value = tok[len(f) + 1:]
                if value is not None:
                    resolve = (_specs_value if f.endswith("specs")
                               else _abs_against)
                    flags.append(f + "=" + resolve(directory, value))
                    matched = True
                    break
        if not matched:
            for f in _CC_DB_PLAIN_FLAGS:
                if tok == f and i + 1 < n:                # -D FOO
                    flags.append(f + tokens[i + 1])
                    extra = 1
                    break
                if tok.startswith(f) and len(tok) > len(f):  # -DFOO
                    flags.append(tok)
                    break
        i += 1 + extra
    return flags


def find_compile_commands():
    """Locate a compile_commands.json by walking up from the current
    directory.

    Each directory from the CWD upward is checked for the database itself,
    then for build/compile_commands.json (where CMake and idf.py leave it),
    so the search works from a project root or from any depth of component
    directory.  The walk stops at the first directory containing .git (the
    repository root — checked after that directory's own candidates) or
    after a bounded number of levels, so an unrelated database further up
    the filesystem is never picked up.  Returns None when nothing is found.
    """
    d = Path.cwd()
    for _ in range(10):
        for candidate in (d / "compile_commands.json",
                          d / "build" / "compile_commands.json"):
            if candidate.is_file():
                return str(candidate)
        if (d / ".git").exists() or d.parent == d:
            return None
        d = d.parent
    return None


def _discovered_db(who):
    """find_compile_commands() for a caller that opted in (compile_commands
    = true, or a bare --compile-commands): finding nothing is then an error,
    not a silent no-op."""
    found = find_compile_commands()
    if found is None:
        sys.exit(f"error: {who}: no compile_commands.json found in the "
                 "current directory, its build/, or any parent up to the "
                 "repository root")
    return found


def load_compile_commands(path):
    """Parse a compile_commands.json into its list of entries (cached, since
    one database is queried once per source per compiler in the matrix)."""
    if path in _DB_CACHE:
        return _DB_CACHE[path]
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        sys.exit(f"error: compile_commands.json not found: {path}")
    except (json.JSONDecodeError, OSError) as exc:
        sys.exit(f"error: {path}: {exc}")
    if not isinstance(data, list):
        sys.exit(f"error: {path}: expected a JSON array of compile entries")
    _DB_CACHE[path] = data
    return data


def compile_commands_flags(db_path, source, missing_ok=False):
    """Header/define flags for ``source`` taken from a compile_commands.json.

    The entry whose ``file`` resolves to the same path as ``source`` supplies
    its include/define flags (relative paths made absolute against the entry's
    ``directory``).  A ``command`` string is tokenised; an ``arguments`` array
    is used as-is.  A source absent from the database is an error — compiling
    it would otherwise fail on the very header this feature exists to supply —
    and the message flags a same-name entry recorded under a different path.
    With ``missing_ok`` (auto-discovered databases) an absent source instead
    compiles without borrowed flags, after a one-time note per (db, source).
    In either mode, --flags-like PATH satisfies an absent source with the
    flags recorded for PATH — the modified-copy workflow.
    """
    entries = load_compile_commands(db_path)
    want = Path(source).resolve()
    flags, name_seen = _lookup_entry(entries, want)
    if flags is not None:
        return flags
    if FLAGS_LIKE:
        like = Path(FLAGS_LIKE).resolve()
        like_flags, _ = _lookup_entry(entries, like)
        if like_flags is None:
            sys.exit(f"error: --flags-like {FLAGS_LIKE} "
                     f"not found in {db_path}")
        key = (db_path, str(want))
        if key not in _BORROW_NOTED:
            _BORROW_NOTED.add(key)
            print(f"note: {source} not in {db_path}; borrowing the flags "
                  f"recorded for {FLAGS_LIKE}", file=sys.stderr)
        return like_flags
    suggest = ("; --flags-like PATH can borrow another entry's flags"
               if name_seen else "")
    if missing_ok:
        key = (db_path, str(want))
        if key not in _MISS_NOTED:
            _MISS_NOTED.add(key)
            print(f"note: {source} not in {db_path}; compiling without "
                  f"its include/define flags{suggest}", file=sys.stderr)
        return []
    hint = (f"; an entry named {want.name} exists under a different path — "
            "pass the source as it appears in the database, or borrow its "
            "flags with --flags-like") if name_seen else ""
    sys.exit(f"error: {source} not found in {db_path}{hint}")


def _lookup_entry(entries, want):
    """Flags for the entry whose file resolves to ``want``, or None.

    The second result reports whether some entry shares ``want``'s
    basename — the raw material for the miss hints above.
    """
    name_seen = False
    for entry in entries:
        f = entry.get("file")
        if not f:
            continue
        directory = entry.get("directory", "")
        fp = Path(f)
        if not fp.is_absolute():
            fp = Path(directory) / fp
        try:
            same = fp.resolve() == want
        except OSError:
            same = False
        if same:
            args = entry.get("arguments")
            tokens = (list(args) if isinstance(args, list)
                      else shlex.split(entry.get("command", "")))
            tokens = _expand_response_files(tokens, directory)
            return include_flags(tokens, directory), name_seen
        if Path(f).name == want.name:
            name_seen = True
    return None, name_seen

# A label at column 0 that is not a local (.L*) label starts a function.
FUNC_LABEL = re.compile(r"^([A-Za-z_][\w$.]*):")
# .type NAME, @object|@function — gcc/clang emit this before the label on
# ELF.  Objects (string constants like __func__, global state, LUTs) get
# column-0 labels too and must not be reported as functions.  The type
# marker is @ on x86/Xtensa/RISC-V, % on ARM, # on some targets.
TYPE_DIRECTIVE = re.compile(r'^\s*\.type\s+([^,\s]+)\s*,\s*[@%#]?(\w+)')
# Assembler directives that carry no information worth reading.  .local/
# .comm/.lcomm declare zero-initialised data, .literal/.literal_position
# are Xtensa literal-pool bookkeeping — none of them is an instruction.
NOISE = re.compile(
    r"^\s*\.(cfi_|p2align|align\b|loc\b|file\b|text\b|globl\b|global\b|"
    r"type\b|section\b|ident\b|weak\b|hidden\b|addrsig|build_version|"
    r"local\b|comm\b|lcomm\b|literal_position|literal\b)"
)
# Compiler-generated bracketing labels that add nothing (.LFB0:, .Lfunc_end0:).
NOISE_LABEL = re.compile(r"^\.(LFB|LFE|Lfunc_begin|Lfunc_end)\d*:")
# Data emitted *inside* a function body: switch jump tables (.long/.word
# entries), inline constants, strings.  These are not instructions, so they
# must not be counted; and a self-relative table entry (".long .L5-.L4")
# references its base label from below, which the loop-span scan would
# otherwise read as a backward branch and report as a phantom loop.
DATA = re.compile(
    r"^\.(long|quad|word|hword|short|byte|[248]byte|value|zero|octa|"
    r"string|ascii|asciz|single|double|float|dc(\.[abwlq])?)\b"
)


def extract_functions(asm_text):
    """Map function name -> cleaned asm lines from compiler -S output.

    A function body runs from its column-0 label to the matching .size
    directive (gcc and clang both emit one on ELF) or the next function
    label.  Labels typed ``@object`` (string constants, global state,
    lookup tables) are data, not functions, and are skipped entirely;
    an untyped label still counts as a function so hand-written asm
    keeps working.  Comment lines, CFI/section/alignment directives,
    compiler bracketing labels, and inline data (switch jump tables,
    constants) are dropped; instructions and meaningful local labels
    (loop targets) are kept, whitespace-stripped.
    """
    funcs = {}
    current = None
    data_labels = set()
    for raw in asm_text.splitlines():
        t = TYPE_DIRECTIVE.match(raw)
        if t and t.group(2) != "function":
            data_labels.add(t.group(1))
        m = FUNC_LABEL.match(raw)
        if m:
            if m.group(1) in data_labels:
                current = None
            else:
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
        if NOISE.match(line) or NOISE_LABEL.match(line) or DATA.match(line):
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


# A local-label operand (branch target, zero-overhead loop end).  Literal
# pool labels (.LC0) also match, but they are emitted outside function
# bodies, so they never appear in the label map built from a body.
LABEL_REF = re.compile(r"\.L[\w$.]+")


def loop_spans(lines):
    """Return [(label, insns)] spans for cleaned asm lines.

    A span is the run of instructions from a local label to the last
    instruction that references it from below — a backward branch, which
    is what a compiled loop looks like on every target the tool parses.
    Xtensa zero-overhead loops (loop/loopnez/loopgt) reference their END
    label instead; there the span is the instructions the loop encloses.
    Spans are reported in order of appearance, one per label; nested
    labels yield nested spans.  The count states how many instructions
    lie in the span — nothing about trip count or hotness, which the
    reader must judge from the source.
    """
    label_at = {ln[:-1]: i for i, ln in enumerate(lines)
                if ln.endswith(":")}
    spans = {}
    for i, ln in enumerate(lines):
        if ln.endswith(":"):
            continue
        mnem = ln.split(None, 1)[0]
        for ref in LABEL_REF.findall(ln):
            if ref not in label_at:
                continue
            j = label_at[ref]
            if j < i:                       # label above: backward branch
                lo, hi = j, i
            elif mnem.startswith("loop"):   # Xtensa: end label below
                lo, hi = i + 1, j - 1
            else:
                continue
            if ref in spans:                # several edges to one label
                lo = min(lo, spans[ref][0])
                hi = max(hi, spans[ref][1])
            spans[ref] = (lo, hi)
    result = []
    for ref, (lo, hi) in sorted(spans.items(), key=lambda kv: kv[1]):
        insns = sum(1 for ln in lines[lo:hi + 1] if not ln.endswith(":"))
        result.append((ref, insns))
    return result


def auto_pairs(names):
    """Pair old_X with new_X for every X present in both."""
    names = list(names)
    return [(n, "new_" + n[4:]) for n in names
            if n.startswith("old_") and "new_" + n[4:] in names]


# Suffixes that make a nonexistent positional read as a mistyped source
# file rather than a function name to inspect.
SOURCE_SUFFIXES = {".c", ".h", ".i", ".s", ".cc", ".cpp", ".cxx", ".hpp"}


def split_positionals(positionals):
    """Split positional arguments into source files and function names.

    The first positional is always a source.  A later one that exists
    on disk is a source too; a bare name is a function to inspect; a
    path-looking argument that does not exist (a separator, or a source
    suffix) is a mistyped file - exiting beats searching the assembly
    for a symbol named like a filename.
    """
    sources, fn_names = positionals[:1], []
    for arg in positionals[1:]:
        if Path(arg).exists():
            sources.append(arg)
        elif ("/" in arg or os.sep in arg
                or Path(arg).suffix.lower() in SOURCE_SUFFIXES):
            sys.exit(f"error: no such file: {arg} (a function name to "
                     "inspect must be a bare symbol, not a path)")
        else:
            fn_names.append(arg)
    return sources, fn_names


def find_config(explicit, sources):
    """Locate the config file; first hit wins, no merging.

    Order: --config PATH, then asmdiff.toml next to the first source
    file (a harness directory can carry its own targets), then the
    current directory, then ~/.config/asmdiff.toml.
    """
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            sys.exit(f"error: config file not found: {explicit}")
        return path
    for candidate in (Path(sources[0]).resolve().parent / CONFIG_NAME,
                      Path.cwd() / CONFIG_NAME,
                      Path.home() / ".config" / CONFIG_NAME):
        if candidate.is_file():
            return candidate
    return None


def load_config(path):
    """Parse a TOML config: one [table] per target, optional top-level
    `default` naming the target(s) to run when no --cc/--target is given."""
    if tomllib is None:
        sys.exit(f"error: {path} exists but this Python has no tomllib "
                 "(config files need Python >= 3.11)")
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"error: {path}: {exc}")


def resolve_editor(env=None):
    """$VISUAL, then $EDITOR, split so values like 'code -w' work; on
    Windows fall back to notepad (neither variable is normally set
    there), elsewhere an unset editor is an error, never a guess."""
    env = os.environ if env is None else env
    editor = env.get("VISUAL") or env.get("EDITOR")
    if editor:
        return shlex.split(editor)
    if os.name == "nt":
        return ["notepad"]
    sys.exit("error: set $VISUAL or $EDITOR to edit the config")


def edit_config(explicit):
    """Open the config in the user's editor, creating it first from
    EXAMPLE_CONFIG when missing (a pip/uvx install has no example file
    on disk).  With --config PATH that file is edited; otherwise the
    global fallback location find_config searches last.  The TOML check
    afterwards is warn-only: a half-finished edit should not eat the
    editor's exit status."""
    path = (Path(explicit) if explicit
            else Path.home() / ".config" / CONFIG_NAME)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_CONFIG)
        print(f"created {path}", file=sys.stderr)
    status = subprocess.call(resolve_editor() + [str(path)])
    if tomllib is not None:
        try:
            with open(path, "rb") as fh:
                tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            print(f"warning: {path}: {exc}", file=sys.stderr)
    return status


def _version_key(path):
    """Sort key that orders embedded numbers numerically, so
    esp-15.2.0 ranks above esp-9.1.0 (lexical order would not)."""
    return [(0, int(tok)) if tok.isdigit() else (1, tok)
            for tok in re.split(r"(\d+)", path)]


def resolve_cc(cc, name):
    """Expand ~, $VARS, and glob patterns in a target's cc value.

    A pattern like .../xtensa-esp-elf/esp-*/bin/...-gcc keeps the config
    toolchain-version agnostic.  If it matches several installed
    toolchains the highest version-sorted one is used, and the choice is
    printed so it is never silent; no match is an error.
    """
    expanded = os.path.expandvars(os.path.expanduser(cc))
    if not any(ch in expanded for ch in "*?["):
        return expanded
    matches = sorted(glob.glob(expanded), key=_version_key)
    if not matches:
        sys.exit(f"error: target [{name}]: cc pattern matched nothing: "
                 + expanded)
    if len(matches) > 1:
        print(f"target [{name}]: cc pattern matched {len(matches)} "
              f"toolchains, using {matches[-1]}", file=sys.stderr)
    return matches[-1]


def target_command(config, name, config_path):
    """Resolve a named [target] table to one 'CC FLAGS' matrix entry."""
    entry = (config or {}).get(name)
    if not isinstance(entry, dict):
        known = sorted(k for k, v in (config or {}).items()
                       if isinstance(v, dict))
        sys.exit(f"error: no [{name}] target in "
                 f"{config_path or 'any config file'}"
                 + ("; targets: " + ", ".join(known) if known
                    else "; no targets defined"))
    cc = entry.get("cc")
    if not isinstance(cc, str):
        sys.exit(f'error: target [{name}] needs cc = "compiler"')
    flags = entry.get("flags", [])
    if isinstance(flags, str) or not all(isinstance(f, str) for f in flags):
        sys.exit(f"error: target [{name}]: flags must be an array of strings")
    flags = [os.path.expandvars(f) for f in flags]
    db = entry.get("compile_commands")
    discovered = False
    if db is True:                       # opt in to CWD-based discovery
        db, discovered = _discovered_db(f"target [{name}]"), True
    elif db is False:
        db = None
    elif db is not None:
        if not isinstance(db, str):
            sys.exit(f"error: target [{name}]: compile_commands must be a "
                     "path to a compile_commands.json, or true to search "
                     "upward from the current directory")
        db = os.path.expandvars(os.path.expanduser(db))
    return Target(shlex.join([resolve_cc(cc, name), *flags]), db, discovered)


def build_matrix(cc_args, target_args, config, config_path, db_arg=None):
    """Resolve the compiler matrix.

    --cc strings verbatim, then --target entries, in that order.  With
    neither, the config's `default` (a target name or list of names);
    with no config or no default, plain gcc/clang at -O3.

    ``db_arg`` is the --compile-commands value: a PATH applies that
    database to every entry, True discovers one near the CWD.  A target
    whose config names its own compile_commands keeps it.
    """
    entries = list(cc_args)
    entries += [target_command(config, name, config_path)
                for name in target_args]
    if not entries:
        default = (config or {}).get("default")
        if default:
            names = [default] if isinstance(default, str) else list(default)
            entries = [target_command(config, name, config_path)
                       for name in names]
        else:
            entries = [f"{cc} {FALLBACK_FLAGS}" for cc in DEFAULT_COMPILERS]
    if db_arg is not None:
        if db_arg is True:
            db, discovered = _discovered_db("--compile-commands"), True
        else:
            db = os.path.expandvars(os.path.expanduser(db_arg))
            discovered = False
        entries = [e if getattr(e, "compile_commands", None) is not None
                   else Target(e, db, discovered) for e in entries]
    return entries


# --verbose: print full compiler command lines and untrimmed stderr.
VERBOSE = False
# Without --verbose, a failed compile shows this many stderr lines — enough
# for the include chain plus the first error, which is the actionable part.
MAX_STDERR_LINES = 20


def _compile_failure(cmd, stderr):
    """Exit for a failed compile.

    A borrowed-flags command runs to hundreds of tokens and a broken
    header environment produces pages of stderr; dumping both buries the
    actual error.  Default: compiler + source + the first stderr lines.
    --verbose restores the complete command and output.
    """
    if VERBOSE:
        sys.exit(f"error: compile failed: {shlex.join(cmd)}\n{stderr}")
    lines = stderr.splitlines()
    shown = "\n".join(lines[:MAX_STDERR_LINES])
    dropped = len(lines) - MAX_STDERR_LINES
    more = f"\n... {dropped} more stderr lines" if dropped > 0 else ""
    sys.exit(f"error: {cmd[0]} failed on {cmd[-1]}\n{shown}{more}\n"
             "(re-run with --verbose for the full command and output)")


def asm_output_name(cc_cmd, harness):
    """Filesystem-safe .s name for one (compiler, source) compilation.

    The readable slug of a compiler command can exceed NAME_MAX when the
    command embeds absolute toolchain/include paths; long slugs are
    truncated and kept unique with a short hash of the full command.
    """
    tag = re.sub(r"\W+", "_", cc_cmd)
    if len(tag) > 64:
        tag = tag[:53] + "_" + hashlib.sha1(cc_cmd.encode()).hexdigest()[:10]
    return tag + "_" + Path(harness).stem + ".s"


def compile_to_asm(cc_cmd, extra_flags, harness, out_dir):
    """Run one compiler to -S; return the asm text.

    If the matrix entry names a compile_commands.json, this source's
    include/define flags from that database are inserted before any bare-``--``
    flags.  Returns None (with a warning) if the compiler is not on PATH.
    Exits with the compiler's stderr on a compile failure.
    """
    argv = shlex.split(cc_cmd)
    if shutil.which(argv[0]) is None:
        print(f"warning: {argv[0]} not found on PATH, skipping",
              file=sys.stderr)
        return None
    db = getattr(cc_cmd, "compile_commands", None)
    db_flags = (compile_commands_flags(
                    db, harness,
                    missing_ok=getattr(cc_cmd, "db_discovered", False))
                if db else [])
    out_s = Path(out_dir) / asm_output_name(shlex.join(argv + db_flags), harness)
    cmd = (argv + db_flags + list(extra_flags)
           + ["-S", "-o", str(out_s), str(harness)])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        _compile_failure(cmd, proc.stderr)
    return out_s.read_text()


def mark_db_misses(cc_cmd, sources, tags):
    """Column tags for a two-file comparison, marking a side that compiled
    without borrowed flags while the other side had them.

    That situation — one source in the database, its copy not — means the
    columns differ in header configuration (defines, include paths), not
    just in source, so byte-identical code can show different assembly.
    The marked tag and a warning keep that from reading as a codegen
    finding.  Both sides missing is a consistent environment: no warning.
    """
    db = getattr(cc_cmd, "compile_commands", None)
    if not db:
        return list(tags)
    missed = [(db, str(Path(s).resolve())) in _MISS_NOTED for s in sources]
    if not any(missed) or all(missed):
        return list(tags)
    print("warning: only one side got flags from the database — assembly "
          "differences may reflect header configuration, not source "
          "changes; --flags-like PATH gives the copy its original's flags",
          file=sys.stderr)
    return [t + " [no db entry]" if m else t for t, m in zip(tags, missed)]


# C++ mangles old_scale(float) to _Z9old_scalef: the old_/new_ prefixes
# survive inside the mangled name but no longer lead it, so auto-pairing
# finds nothing and would fall back to the summary without explanation.
MANGLED_PAIR = re.compile(r"_Z\d+(?:old|new)_")


def mangled_pair_hint(names):
    """True when the extracted names look like C++-mangled old_*/new_*
    functions — the harness probably just needs extern \"C\"."""
    return any(MANGLED_PAIR.match(n) for n in names)


def side_by_side(left, right, ltitle, rtitle, width=44):
    """Two-column view of a pair's asm lines."""
    rows = [f"{ltitle:<{width}} | {rtitle}",
            f"{'-' * width}-+-{'-' * width}"]
    for l, r in zip_longest(left, right, fillvalue=""):
        l = l.expandtabs(8)[:width]
        r = r.expandtabs(8)[:width]
        rows.append(f"{l:<{width}} | {r}")
    return "\n".join(rows)


def render_table(rows):
    """Column-aligned text for a list of equal-length string tuples."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip()
        for row in rows)


def format_spans(spans):
    return " ".join(f"{label}:{n}" for label, n in spans) or "-"


# Real firmware dispatch functions call dozens of distinct symbols; an
# uncapped list makes summary rows thousands of characters wide.
MAX_CALLS_SHOWN = 8


def format_calls(calls):
    """Comma-joined callee list, capped at MAX_CALLS_SHOWN for readability."""
    if not calls:
        return "-"
    if len(calls) > MAX_CALLS_SHOWN:
        return (", ".join(calls[:MAX_CALLS_SHOWN])
                + f", +{len(calls) - MAX_CALLS_SHOWN} more")
    return ", ".join(calls)


def summary_table(pairs, funcs):
    """Instruction counts, outbound calls, and loop spans per pair member."""
    rows = [("function", "role", "insns", "calls", "loop spans")]
    for old, new in pairs:
        for name, role in ((old, "baseline"), (new, "candidate")):
            insns, calls = analyze(funcs[name])
            rows.append((name, role, str(insns), format_calls(calls),
                         format_spans(loop_spans(funcs[name]))))
    return render_table(rows)


def file_summary_table(funcs):
    """Per-function counts plus a whole-file total row.

    The total sums instruction counts over every function parsed from
    the -S output and unions their outbound calls — a coarse A/B sanity
    check, not a code-size measurement (literal pools, data, and
    alignment are not included).
    """
    rows = [("function", "insns", "calls", "loop spans")]
    total_insns, all_calls = 0, []
    for name, lines in funcs.items():
        insns, calls = analyze(lines)
        total_insns += insns
        for sym in calls:
            if sym not in all_calls:
                all_calls.append(sym)
        rows.append((name, str(insns), format_calls(calls),
                     format_spans(loop_spans(lines))))
    rows.append((f"TOTAL ({len(funcs)} functions)", str(total_insns),
                 format_calls(all_calls), "-"))
    return render_table(rows)


def listing(name, lines):
    """gcc-style listing of one extracted function: the function label
    at column 0, instructions tabbed, kept local labels back at
    column 0 (extract_functions stores them whitespace-stripped)."""
    body = [line if line.endswith(":") else "\t" + line for line in lines]
    return "\n".join([f"{name}:"] + body)


def inspect_table(fn_names, funcs):
    """Stats rows for the inspected functions only - no pair roles and
    no whole-file total, unlike summary_table/file_summary_table."""
    rows = [("function", "insns", "calls", "loop spans")]
    for name in fn_names:
        insns, calls = analyze(funcs[name])
        rows.append((name, str(insns), format_calls(calls),
                     format_spans(loop_spans(funcs[name]))))
    return render_table(rows)


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
            tags = mark_db_misses(cc_cmd, sources,
                                  file_tags(sources[0], sources[1]))
            print(f"\n== {cc_cmd} ==\n")
            report_across(fn_names, sides[0], sides[1], *tags)
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


def run_summary(sources, matrix, extra_flags, tmp):
    """No pairs to compare: whole-file summary, one block per file."""
    tags = (file_tags(*sources) if len(sources) == 2
            else [Path(sources[0]).name])
    ran_any = False
    for cc_cmd in matrix:
        sections = []
        for src in sources:
            asm = compile_to_asm(cc_cmd, extra_flags, src, tmp)
            if asm is None:
                break
            sections.append(extract_functions(asm))
        if len(sections) < len(sources):
            continue
        ran_any = True
        shown = (mark_db_misses(cc_cmd, sources, tags)
                 if len(sources) == 2 else tags)
        print(f"\n== {cc_cmd} ==")
        for tag, funcs in zip(shown, sections):
            if len(sections) > 1:
                print(f"\n-- {tag} --")
            print()
            print(file_summary_table(funcs) if funcs
                  else "(no functions found)")
    if not ran_any:
        sys.exit("error: no usable compiler in the matrix")
    return 0


def run_pairs(source, matrix, pair_specs, extra_flags, tmp):
    """--pair mode: two different functions within one compilation.

    With no --pair and no old_X/new_X functions to auto-pair, falls
    back to the whole-file summary for this compilation.
    """
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
            if mangled_pair_hint(funcs):
                print('note: C++ mangling defeats old_*/new_* auto-pairing; '
                      'declare the pairs extern "C" or use --pair with the '
                      'mangled names', file=sys.stderr)
            print(f"\n== {cc_cmd} ==\n")
            print(file_summary_table(funcs) if funcs
                  else "(no functions found)")
            continue
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


def run_inspect(source, matrix, fn_names, layout, extra_flags, tmp):
    """Inspect mode: print named functions' assembly, no comparison.

    The presentation adapts to how many matrix entries compiled: one
    gives a plain listing with a stats table, exactly two give the
    --across side-by-side, more give one block per compiler.  --layout
    forces list or side-by-side instead.
    """
    usable = []
    for idx, cc_cmd in enumerate(matrix, start=1):
        asm = compile_to_asm(cc_cmd, extra_flags, source, tmp)
        if asm is not None:
            usable.append((f"cc#{idx}", cc_cmd, extract_functions(asm)))
    if not usable:
        sys.exit("error: no usable compiler in the matrix")
    for _, cc_cmd, funcs in usable:
        missing = sorted({f for f in fn_names if f not in funcs})
        if missing:
            sys.exit("error: function(s) not in asm: " + ", ".join(missing)
                     + f" under {cc_cmd}; functions seen: "
                     + ", ".join(funcs))
    if layout == "side-by-side" and len(usable) < 2:
        sys.exit("error: --layout side-by-side needs at least two usable "
                 "compilers in the matrix")
    if layout is None:
        layout = "side-by-side" if len(usable) == 2 else "list"
    if layout == "side-by-side":
        print()
        for tag, cc_cmd, _ in usable:
            print(f"{tag}: {cc_cmd}")
        base_tag, _, base_funcs = usable[0]
        for tag, _, funcs in usable[1:]:
            print(f"\n== {base_tag} vs {tag} ==\n")
            report_across(fn_names, base_funcs, funcs, base_tag, tag)
        return 0
    for _, cc_cmd, funcs in usable:
        if len(usable) > 1:
            print(f"\n== {cc_cmd} ==")
        for name in fn_names:
            print()
            print(listing(name, funcs[name]))
        print()
        print(inspect_table(fn_names, funcs))
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
    parser.add_argument("sources", nargs="*", metavar="SOURCE.c",
                        help="C file to compile; follow it with bare "
                             "function names to inspect their assembly, "
                             "or give two files with --across to compare "
                             "versions of a function")
    parser.add_argument("-p", "--pair", action="append", default=[],
                        metavar="OLD:NEW",
                        help="compare two functions within one compilation "
                             "(repeatable); default: auto-pair old_X/new_X")
    parser.add_argument("-a", "--across", action="append", default=[],
                        metavar="FUNC",
                        help="compare the same function across two "
                             "compilations (repeatable): one file + two "
                             "--cc entries, or two files")
    parser.add_argument("-l", "--layout", choices=["list", "side-by-side"],
                        help="force the inspect presentation instead of "
                             "adapting to the matrix (1 usable compiler "
                             "lists, 2 go side by side, more list)")
    parser.add_argument("--cc", action="append", default=[],
                        metavar="'CC FLAGS'",
                        help="compiler and flags as one string (repeatable); "
                             "default: config default target, else gcc and "
                             "clang at " + FALLBACK_FLAGS)
    parser.add_argument("--target", action="append", default=[],
                        metavar="NAME",
                        help="named [table] from the config file, resolved "
                             "to a --cc entry (repeatable; appended to the "
                             "matrix after --cc entries)")
    parser.add_argument("-db", "--compile-commands", nargs="?", const=True,
                        default=None, metavar="PATH",
                        help="borrow each source's include/define flags from "
                             "a compile_commands.json; with no PATH, search "
                             "each directory (and its build/) from the CWD "
                             "up to the repository root.  A target whose "
                             "config names its own compile_commands keeps it")
    parser.add_argument("--config", metavar="PATH",
                        help=f"config file; default search: {CONFIG_NAME} "
                             "next to SOURCE.c, in the current directory, "
                             "then in ~/.config/")
    parser.add_argument("--flags-like", metavar="PATH",
                        help="when a source has no compile_commands entry, "
                             "borrow the include/define flags recorded for "
                             "PATH — lets a modified copy of a project "
                             "source compile (and compare) under its "
                             "original's header environment")
    parser.add_argument("--edit-config", action="store_true",
                        help="open the config in $VISUAL/$EDITOR (the "
                             "--config file, else ~/.config/asmdiff.toml), "
                             "creating it from the built-in example first "
                             "if missing")
    parser.add_argument("--example-config", action="store_true",
                        help="print the built-in example config (the "
                             "repository's asmdiff.example.toml) to stdout, "
                             "ready to redirect into a config file")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="on compile failure, print the full compiler "
                             "command and complete error output instead of "
                             "the first lines")
    args = parser.parse_args(argv)
    global VERBOSE, FLAGS_LIKE
    VERBOSE = args.verbose
    FLAGS_LIKE = args.flags_like

    if args.example_config:
        sys.stdout.write(EXAMPLE_CONFIG)
        return 0
    if args.edit_config:
        return edit_config(args.config)
    if not args.sources:
        parser.error("SOURCE.c required")

    sources, fn_names = split_positionals(args.sources)
    if len(sources) > 2:
        parser.error("at most two source files may be given")
    if fn_names and len(sources) == 2:
        parser.error("bare function names inspect within one file; "
                     "use --across FUNC for two files")
    if fn_names and (args.pair or args.across):
        parser.error("bare function names (inspect) cannot be combined "
                     "with --pair or --across")
    if args.layout and not fn_names:
        parser.error("--layout only applies when inspecting functions "
                     "(SOURCE.c FUNC ...)")
    if args.across and args.pair:
        parser.error("--across and --pair are mutually exclusive")
    if len(sources) == 2 and args.pair:
        parser.error("--pair compares within one file; "
                     "use --across FUNC for two files")
    config_path = find_config(args.config, sources)
    config = load_config(config_path) if config_path else None
    matrix = build_matrix(args.cc, args.target, config, config_path,
                          args.compile_commands)
    if args.across and len(sources) == 1 and len(matrix) < 2:
        parser.error("--across on one file needs at least two --cc entries")
    for spec in args.pair:
        if ":" not in spec:
            parser.error(f"--pair expects OLD:NEW, got {spec!r}")

    with tempfile.TemporaryDirectory(prefix="asmdiff") as tmp:
        if fn_names:
            return run_inspect(sources[0], matrix, fn_names, args.layout,
                               extra_flags, tmp)
        if args.across:
            return run_across(sources, matrix, args.across,
                              extra_flags, tmp)
        if len(sources) == 2:
            return run_summary(sources, matrix, extra_flags, tmp)
        return run_pairs(sources[0], matrix, args.pair,
                         extra_flags, tmp)


if __name__ == "__main__":
    sys.exit(main())
