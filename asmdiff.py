#!/usr/bin/env python3
"""Per-function assembly inspection and comparison, across a compiler matrix or from a shipped ELF.

Compiles a C source file across a matrix of compilers, extracts each
variant function's assembly from the -S output, and prints side-by-side
listings plus a summary of instruction counts, loop spans (instructions
between a local label and its last backward branch), and outbound calls.
Automates fold-vs-libcall analysis when evaluating micro-optimisations
(e.g. "does this still compile to one instruction, or is it a libcall?").

Usage:
    tools/asmdiff/asmdiff.py SOURCE.c [SOURCE2.c | FUNC...]
                             [--pair OLD:NEW]... [--across FUNC]...
                             [--target NAME]... [--cc 'CC FLAGS']...
                             [--config PATH] [--compile-commands [PATH]]
                             [--flags-like PATH] [--db-includes]
                             [--filter REGEX] [--summary-only]
                             [--collapse] [--span-stats] [--cost]
                             [--costs NAME] [--fail-on-growth]
                             [--json] [--width N]
                             [--layout list|side-by-side] [-v]
                             [-- EXTRA_FLAGS...]
    tools/asmdiff/asmdiff.py FIRMWARE.elf [FUNC...] [--filter REGEX]
                             [--objdump PATH] [--layout list]
                             [--summary-only] [--span-stats] [--cost]
                             [--costs NAME] [--json] [--width N]
    tools/asmdiff/asmdiff.py --edit-config | --example-config
                             | --list-targets | --completion SHELL
                             | --install-completion [SHELL] | --version

Four modes, plus the no-flag whole-file summary:
  SOURCE.c FUNC   inspect: print the named function's assembly, no
                  comparison.  One usable compiler prints a listing
                  plus a stats row, exactly two print side by side,
                  more print one block per compiler; --layout forces
                  list or side-by-side.  --filter REGEX selects
                  matching functions as full peers of named ones
                  (compiler-generated clones included; lenient when a
                  match exists under only part of the matrix), and in
                  summary mode narrows the table.
  FIRMWARE.elf    ELF input (detected by magic bytes): disassemble a
                  linked binary through the toolchain's objdump and
                  run named and/or --filter REGEX matched functions
                  through the same analyzers - post-LTO/link reality
                  instead of single-TU codegen.  Named functions are
                  listed in full; --filter matches are summarized
                  only, unless -l list asks for their listings too.
                  objdump comes from
                  --objdump PATH or the first gcc in the matrix
                  (trailing gcc swapped for objdump); Xtensa
                  -mlongcalls call sequences are resolved to their
                  real callees when the disassembly names them,
                  and report as indirect(<reg>) when it does not.
  --pair OLD:NEW  compares two different functions within one compilation
                  (with no --pair, old_X/new_X names auto-pair).
  --across FUNC   compares the SAME function across two compilations:
                  either one file under two matrix entries (flag/define
                  variants), or two source files (before/after versions)
                  under each compiler in the matrix.
  (neither)       whole-file summary: per-function counts plus a file
                  total, for one file or side by side for two.

Every compile mode prints the same three parts in this order: a legend
naming each matrix row ("label: resolved command", once per run), one
stats table for the whole matrix, which leads with a `target` column
of those labels once more than one row reaches it, and the listings,
grouped per row under "== LABEL ==".  The label is the config target a
row came from, or cc#N for a --cc row in a matrix of several; a lone
--cc row is its own label, and then neither the legend nor the
`target` column appears.  Two source files add a `file` column after
`target`; ELF input has no matrix rows, hence neither column.

A row whose compile fails is dropped rather than ending the run: the
rest of the matrix still renders, the recorded compiler output goes to
stderr after everything else, and the run returns 1.  A compiler
missing from PATH is skipped with a warning at matrix time, and only
an empty matrix is an error.

Tables and listings are rendered to a column budget: --width N, else
the terminal, else $COLUMNS, else 120; --width 0 is unlimited, which
leaves the callee column untrimmed for a script to grep.

Exit status: 0 printed what was asked for (differing assembly is the
expected result), 1 the tool failed (compile error, unknown --pair
name, no usable compiler), 2 argparse's usage error, 3
--fail-on-growth found a grown candidate.

Five flags shape the output: --summary-only prints only the stats
tables, --collapse elides identical runs in side-by-side listings
keeping context around each difference, --span-stats follows the
stats table with a per-loop-span instruction mix (nesting depth and
load/store/mul/div/branch/other counts; branch includes calls), --cost
adds a column saying what each function is made of (the same six
instruction classes, plus the libcall tier of every call site -
softfp, softfp-div, int-div, libm, mem, call - with the sites a loop
span holds counted apart), and --json replaces the tables with one
JSON document of per-function records (implies --summary-only) for
scripted callers.

The cost column counts; it prices nothing until a measured profile
says what a class or a tier costs.  A [costs.NAME] table in the config
carries those weights (keyed by class, tier, or call symbol) together
with the measured_on and method strings it may not omit; a target
names one with costs = "NAME", and --costs NAME applies one to every
matrix row, --cc rows and ELF input included, implying --cost.  A
score is then printed with the count of instructions and call sites no
weight covered, followed by the profile's provenance; mem and call are
never weighted, since their cost is a size argument or a callee the
assembly does not show.

Paired output answers the paired question itself: every summary table
closes a pair with a delta row (signed instruction count, per-span
before -> after, callees gained with + and lost with -), --json carries
the same under each candidate's `delta`, and --fail-on-growth exits 3,
naming every grown candidate on stderr, so CI can fail a rewrite that
was meant to shrink without reconstructing the pairing in jq.

Compilers come from named targets in an asmdiff.toml config file
(-t/--target NAME: a table, a [groups] name, a comma-list, or a glob),
from ad-hoc --cc 'CC FLAGS' rows, from the config's `default` entry, or
— failing all of those — plain `gcc -O3` and `clang -O3`.
Flags after a bare `--` are appended to every compiler invocation.
--edit-config opens the config (--config PATH, else ~/.config/
asmdiff.toml) in $VISUAL/$EDITOR, creating it from the built-in example
when missing; --example-config prints that example to stdout;
--list-targets prints the resolved config's default, groups, cost
profiles, and targets.
--completion bash|zsh|fish prints a completion script built from this
parser's own flag list, so it cannot drift from the tool; target and
cost-profile names come from the config at each tab press, honouring a
--config already on the line.  --install-completion [SHELL] writes that
script to the shell's user completion directory (SHELL defaults to the
basename of $SHELL), never to an rc file, and refuses to replace a file
asmdiff did not write.

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
against its original under one header environment.  --db-includes limits
any borrow to the header-search paths, re-emitted as -idirafter so they
cannot shadow the host's own system headers: a host target can then
compile a cross project's source without inheriting cross-only defines,
-specs, or a libc-overlay include directory.
"""
import argparse
import collections
import difflib
import fnmatch
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

# Single source of truth for the package version: pyproject.toml reads
# it from here (hatchling dynamic version).
__version__ = "0.4.0"

DEFAULT_COMPILERS = ["gcc", "clang"]
FALLBACK_FLAGS = "-O3"
CONFIG_NAME = "asmdiff.toml"
# Top-level keys that are not compiler targets.
CONFIG_META_KEYS = ("default", "groups", "costs")

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
# Each [table] is a target usable as `-t/--target NAME`; the optional
# top-level `default` names the target(s) used when no --cc/--target
# is given.  It takes whatever -t takes: a target name, a [groups]
# name, a comma list, a glob, or an array mixing them
# (default = ["native", "esp32s3"]).
# [groups] names a matrix of those tables for one `-t GROUP` (comma
# lists and globs also work: `-t esp32c3,esp32s3`, `-t 'esp32c*'`).
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

# The name the docs use for "your native compiler" (--target host): an
# alias of [gcc] kept as its own table so the examples work verbatim.
[host]
cc = "gcc"
flags = ["-O3", "-Wall", "-Wextra"]

# `-t GROUP` runs every named target.  `-t host` is still the [host]
# gcc alias above; use `-t native` (or `-t gcc,clang`) for both host
# compilers.  Does not change `default`.
[groups]
riscv32-esp = ["esp32c3", "esp32c6", "esp32h2", "esp32p4"]
xtensa-esp = ["esp32", "esp32s2", "esp32s3"]
esp = ["esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2", "esp32p4"]
native = ["gcc", "clang"]

# --- Cost profiles ---------------------------------------------------
# A target may name a [costs.NAME] table; --cost then prints a
# weighted score next to the class counts, with the profile's
# provenance.  Weights are static issue costs in cycles, measured
# on hardware, approximate by nature; measured_on and method are
# required so a reader knows how approximate.  No profile ships
# measured yet: fill one in from your own board (see README,
# "Cost profiles") and consider contributing it.
#
# [costs.esp32s3-iram]
# measured_on = "ESP32-S3 rev 0.2, code in IRAM, esp-15.2.0 libgcc"
# method = "esp_cpu_get_cycle_count harness, median of 1000 runs"
# other = 1
# load = 2
# store = 1
# mul = 2
# div = 20
# branch = 2
# softfp = 60
# softfp-div = 200
# int-div = 40
# "__muldf3" = 90
#
# [esp32s3]
# ...
# costs = "esp32s3-iram"

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
# targets differ only in -march/-mabi.  `-t riscv32-esp` runs the
# whole profile without editing `default`.

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
# `-t xtensa-esp` runs the whole profile; `-t esp` runs every ESP chip.

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
# --db-includes: borrow only the header-search paths from the database.
# A project's -I paths are valid on any target; its -D defines, forced
# includes, and -specs/--sysroot are tied to the arch the database was
# built for — this is how a host target compiles a cross project's
# source without inheriting cross-only flags.
DB_INCLUDES = False
_DB_SEARCH_PATH_FLAGS = ("-I", "-iquote", "-isystem", "-idirafter")
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
    where an explicit database makes that an error.

    ``costs`` is the resolved cost profile this target's
    ``costs = "NAME"`` (or --costs) names, since what an instruction
    costs is a property of the target that runs it.

    ``label`` is how the row is named everywhere it is reported: the
    legend, the table's target column, the listing headers, a failure
    message.  build_matrix fills it in (see row_label); None means the
    row has not been through it."""

    def __new__(cls, cmd, compile_commands=None, db_discovered=False,
                costs=None, label=None):
        self = super().__new__(cls, cmd)
        self.compile_commands = compile_commands
        self.db_discovered = db_discovered
        self.costs = costs
        self.label = label
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


def _discovered_db(who, required=True):
    """find_compile_commands() for a caller that opted in.

    ``required=True`` is a bare --compile-commands: the user asked for a
    database in this run, so finding none is an error.  ``required=False``
    is a target's ``compile_commands = true``, which describes where that
    target is usually compiled, not what this run must have: a standalone
    harness run from outside the project then gets a note and compiles
    without borrowed flags instead of a dead config.
    """
    found = find_compile_commands()
    if found is None:
        if not required:
            print(f"{who}: no compile_commands.json found near the current "
                  "directory; compiling without borrowed flags",
                  file=sys.stderr)
            return None
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


def _borrowed(flags):
    """Apply --db-includes to one borrowed flag list: keep the
    header-search pairs (-I/-iquote/-isystem/-idirafter PATH), drop
    defines, forced includes, and driver-level flags.  Identity when
    the option is off.

    Kept paths are re-emitted as -idirafter — searched AFTER the
    compiler's own system directories — because a cross project's
    include set can overlay libc headers (ESP-IDF ships an esp_libc/
    platform_include whose stdio.h would otherwise shadow the host's);
    project-only headers still resolve, the host's libc wins.
    """
    if not DB_INCLUDES:
        return flags
    kept, i = [], 0
    while i < len(flags):
        f = flags[i]
        if f in _CC_DB_PATH_FLAGS:              # split "-I path" pair
            if f in _DB_SEARCH_PATH_FLAGS:
                kept += ["-idirafter", flags[i + 1]]
            i += 2
        else:                                   # -DFOO / -specs=... token
            i += 1
    return kept


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
        return _borrowed(flags)
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
        return _borrowed(like_flags)
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


# `objdump -d` function header: "40370400 <render_lut>:".
OBJDUMP_HEADER = re.compile(r"^([0-9a-f]+) <([^>]+)>:\s*$")
# `objdump -d` instruction line: address, one byte-dump field (hex digits
# and spaces; "004136" is the encoding, not the mnemonic), then the
# mnemonic and operands.
OBJDUMP_INSN = re.compile(
    r"^\s*([0-9a-f]+):\t[0-9a-f ]+\t\s*(\S+)[ \t]*(.*?)\s*$")
# An "addr <annotation>" operand: branch, loop or call target.
OBJDUMP_TARGET = re.compile(r"\b([0-9a-f]+) <([^>]+)>")
# A nearest-symbol offset annotation ("<_etext+0x100>"): objdump's name
# for an address that is not a symbol start.  For addresses outside the
# current function the base symbol is usually unrelated (a literal-pool
# word or data landing after some other object), so the annotation is
# misleading, not informative.
OFFSET_ANNOT = re.compile(r"[+-]0x[0-9a-f]+$")


def extract_functions_objdump(dump_text):
    """Map function name -> cleaned asm lines from `objdump -d` output.

    Produces the shape extract_functions() yields for -S output, so
    analyze() and loop_spans() work unchanged: one mnemonic + operands
    per line, labels ending with ':'.  A linked ELF has addresses where
    -S output has labels, so in-function branch targets are rewritten to
    synthetic local labels (.L<hex-offset>) inserted at the target
    instruction; an Xtensa zero-overhead loop's end address becomes
    .L<off>_LEND, keeping the ZOL-vs-branch naming convention -S output
    has.  Targets outside the function keep a bare-symbol annotation,
    so calls still name their callee; offset-form annotations of other
    symbols are nearest-symbol noise and become raw hex addresses (see
    _relabel_objdump).  Offset-form headers
    (``<sym+0x..>``/``<sym-0x..>``: literal pools and other symbol-less
    gaps that disassemble as garbage) and ``...`` filler are skipped.
    """
    funcs = {}
    current = None
    for raw in dump_text.splitlines():
        h = OBJDUMP_HEADER.match(raw)
        if h:
            name = h.group(2)
            if OFFSET_ANNOT.search(name):
                current = None          # symbol-less gap, not a function
            else:
                current = []
                funcs[name] = (int(h.group(1), 16), current)
            continue
        if current is None:
            continue
        m = OBJDUMP_INSN.match(raw)
        if m:
            current.append((int(m.group(1), 16), m.group(2), m.group(3)))
    return {name: _relabel_objdump(name, start, _resolve_longcalls(insns))
            for name, (start, insns) in funcs.items()}


# The value annotation objdump appends to an l32r whose literal resolves
# to a bare symbol: "l32r a8, <lit> (40002274 <__divsf3>)".  An offset
# form (<_etext+0x100>) is a constant that happens to fall near a
# symbol, not a callee, so it deliberately does not match.
L32R_VALUE = re.compile(r"\([0-9a-f]+ <([A-Za-z_][\w$.]*)>\)$")

# Xtensa integer stores: the only mnemonics whose first a-register
# operand is read, not written.  Everything else writing its first
# operand (l32i, mov.n, arithmetic) clobbers a loaded call target.
XTENSA_STORES = {"s8i", "s16i", "s32i", "s32i.n", "s32e"}


def _resolve_longcalls(insns):
    """Name the callees of Xtensa -mlongcalls sequences.

    -mlongcalls emits every call as "l32r aN, <lit>" + "callx8 aN"; the
    linker relaxes in-range pairs back to call8, so the ones left in an
    ELF are real out-of-range calls (typically flash to ROM).  When the
    l32r's value annotation names a symbol, rewrite the callx operand to
    it so CALL_RE reports the callee.  The nearest write to the call's
    register within the previous 8 instructions decides: an annotated
    l32r resolves, anything else (an unannotated l32r, or a non-store
    overwriting the register - e.g. l32i fetching a function pointer
    out of a just-loaded struct) leaves the call indirect.  A wrong
    callee would be worse than a register name.
    """
    out = list(insns)
    for i, (addr, mnem, ops) in enumerate(insns):
        if not (re.fullmatch(r"callx\d+", mnem)
                and re.fullmatch(r"a\d+", ops)):
            continue
        for j in range(i - 1, max(i - 9, -1), -1):
            _, mid_mnem, mid_ops = insns[j]
            if not mid_ops.startswith(ops + ","):
                continue
            if mid_mnem == "l32r":
                value = L32R_VALUE.search(mid_ops)
                if value:
                    out[i] = (addr, mnem, value.group(1))
                break
            if mid_mnem not in XTENSA_STORES:
                break                     # register rewritten in between
    return out


def _relabel_objdump(name, start, insns):
    """Turn one function's (addr, mnemonic, operands) rows into cleaned
    lines, synthesizing local labels for in-function targets.

    Out-of-function targets keep a bare-symbol annotation (a real callee
    or object), but an offset-form annotation whose base is not this
    function is nearest-symbol noise - typically an l32r pool address or
    literal value decorated with whatever symbol precedes it - and is
    rendered as the raw hex address instead."""
    addrs = {addr for addr, _, _ in insns}
    labels = {}
    for _, mnem, ops in insns:
        for m in OBJDUMP_TARGET.finditer(ops):
            taddr = int(m.group(1), 16)
            if taddr not in addrs:
                continue
            if mnem.startswith("loop"):     # ZOL references its END
                labels[taddr] = ".L%x_LEND" % (taddr - start)
            elif taddr not in labels:
                labels[taddr] = ".L%x" % (taddr - start)

    def target_name(m):
        taddr = int(m.group(1), 16)
        if taddr in labels:
            return labels[taddr]
        annot = m.group(2)
        if OFFSET_ANNOT.search(annot) and OFFSET_ANNOT.sub("", annot) != name:
            return "0x%x" % taddr
        return annot

    lines = []
    for addr, mnem, ops in insns:
        if addr in labels:
            lines.append(labels[addr] + ":")
        ops = OBJDUMP_TARGET.sub(target_name, ops)
        lines.append(mnem + "\t" + ops if ops else mnem)
    return lines


# Direct-call / tail-call mnemonics across x86 (call, jmp), ARM (bl, blx),
# RISC-V (call, tail, jal), and Xtensa (call0/4/8/12, callx*, j).  Longest
# alternatives first so e.g. "callx8" is not consumed as "call".  The
# symbol must be the sole/final operand (optionally @PLT-suffixed), so
# multi-operand forms like "jal ra, exp2f" don't report the register.
CALL_RE = re.compile(
    r"^(?:callx\d+|call\d*|callq|jalr|jal|jmp|blx|bl|tail|j)\s+"
    r"([A-Za-z_][\w$.]*)(?:@[\w.]+)?\s*(?:[#;].*)?$"
)

# Register-indirect call forms whose sole operand is a register: Xtensa
# callx* (always a register), RISC-V single-operand jalr, ARM blx rN.
# Without this, CALL_RE would report the register name as the callee
# ("callx8 a8" -> a call to "a8") - technically visible, but easy to
# mistake for a symbol, and a grep for the real callee silently misses
# the site.  Reported as "indirect(<reg>)" instead.
INDIRECT_CALL_RE = re.compile(
    r"^(?:callx\d+\s+(a\d+)"
    r"|jalr\s+(x\d+|ra|[ast]\d+)"
    r"|blx\s+(r\d+|lr|ip))\s*$"
)


def call_symbol(line):
    """The symbol one cleaned instruction calls, or None.

    A call is a call/tail-call mnemonic whose first operand looks like a
    symbol name — local labels (.L*) and %-registers never match, so
    branches inside the function are not counted.  A call through a
    register is reported as "indirect(<reg>)".
    """
    m = INDIRECT_CALL_RE.match(line)
    if m:
        return "indirect(%s)" % next(g for g in m.groups() if g)
    m = CALL_RE.match(line)
    return m.group(1) if m else None


def analyze(lines):
    """Return (instruction_count, called_symbols) for cleaned asm lines.

    Each callee is named once, in first-call order, since the calls
    column answers what a function reaches; cost_mix counts the call
    sites instead, since two calls to one helper cost twice.
    """
    insns = 0
    calls = []
    for line in lines:
        if line.endswith(":"):
            continue
        insns += 1
        sym = call_symbol(line)
        if sym is not None and sym not in calls:
            calls.append(sym)
    return insns, calls


def pair_delta(base_lines, cand_lines):
    """What the candidate changed against its baseline: the signed
    instruction delta and the callees it gained and lost.

    The pairing is the tool's own, so the reader never has to
    reconstruct it from two rows (or a caller from two JSON records).
    """
    base_insns, base_calls = analyze(base_lines)
    cand_insns, cand_calls = analyze(cand_lines)
    return {"insns": cand_insns - base_insns,
            "calls_added": [s for s in cand_calls if s not in base_calls],
            "calls_removed": [s for s in base_calls if s not in cand_calls]}


# A local-label operand (branch target, zero-overhead loop end).  Literal
# pool labels (.LC0) also match, but they are emitted outside function
# bodies, so they never appear in the label map built from a body.
LABEL_REF = re.compile(r"\.L[\w$.]+")


def loop_span_ranges(lines):
    """Return [(label, lo, hi)] line-index ranges for loop spans.

    A span is the run of instructions from a local label to the last
    instruction that references it from below — a backward branch, which
    is what a compiled loop looks like on every target the tool parses.
    Xtensa zero-overhead loops (loop/loopnez/loopgt) reference their END
    label instead; there the span is the instructions the loop encloses.
    Spans are reported in order of appearance, one per label; nested
    labels yield nested spans.
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
    return [(ref, lo, hi) for ref, (lo, hi)
            in sorted(spans.items(), key=lambda kv: kv[1])]


def span_depths(ranges):
    """Return {label: depth} for loop_span_ranges output.

    Depth counts the spans strictly containing a span: an outermost
    loop is 0, a loop nested in it 1.  It says where a span sits, not
    how hot it is - the trip counts are the source's business - but a
    span at depth 1 runs its body once per iteration of the span at
    depth 0.  Two labels over exactly the same range (one loop body
    reached by two edges) contain each other under no reading, so both
    keep the depth of whatever encloses them.
    """
    return {label: sum(1 for _, lo2, hi2 in ranges
                       if (lo2, hi2) != (lo, hi) and lo2 <= lo and hi <= hi2)
            for label, lo, hi in ranges}


def loop_spans(lines, ranges=None):
    """Return [(label, insns)] spans for cleaned asm lines.

    The count states how many instructions lie in the span — nothing
    about trip count or hotness, which the reader must judge from the
    source.  ``ranges`` hands in loop_span_ranges output the caller
    already has, so a record that wants spans, depths, and the cost mix
    walks the lines once.
    """
    result = []
    if ranges is None:
        ranges = loop_span_ranges(lines)
    for ref, lo, hi in ranges:
        insns = sum(1 for ln in lines[lo:hi + 1] if not ln.endswith(":"))
        result.append((ref, insns))
    return result


# --span-stats buckets, by mnemonic table.  Covers the targets the tool
# routinely meets: Xtensa, RISC-V, and ARM by exact (or dot-stripped)
# mnemonic; x86 AT&T by the memory-operand heuristic in classify_insn,
# since there loads and stores are mostly spellings of mov.
_LOAD_MNEMONICS = frozenset("""
    l8ui l16ui l16si l32i l32i.n l32r l32e l32ai lsi lsiu lsx lsxu
    lb lbu lh lhu lw lwu ld flw fld c.lw c.lwsp c.ld c.ldsp c.flw c.fld
    ldr ldrb ldrh ldrsb ldrsh ldrd ldm ldmia vldr vldm
    pop popl popq
""".split())
_STORE_MNEMONICS = frozenset("""
    s8i s16i s32i s32i.n s32e s32ri s32c1i ssi ssiu ssx ssxu
    sb sh sw sd fsw fsd c.sw c.swsp c.sd c.sdsp c.fsw c.fsd
    str strb strh strd stm stmia vstr vstm
    push pushl pushq
""".split())
# Any multiply, including multiply-accumulate and FP forms (mull,
# mulsh, mula.dd.*, fmadd.s, vmla.f32, imull, mulss, ...).
_MUL_PREFIXES = ("mul", "imul", "fmul", "fmadd", "fmsub", "fnmadd",
                 "fnmsub", "madd", "msub", "mla", "mls", "smul", "umul",
                 "smla", "umla", "vmul", "vmla", "vmls", "vfma", "vfms")
# Hardware divide and square root, the two multi-cycle arithmetic
# instructions worth separating from the rest.  Xtensa FPU divide is an
# inline Newton-Raphson sequence (div0.s, nexp01.s, divn.s, ...) rather
# than one instruction, so those stay in "other": counting them as
# divides would price one divide as eight.
_DIV_MNEMONICS = frozenset("""
    quos quou rems remu
    div divu rem remu divw divuw remw remuw fdiv.s fdiv.d fsqrt.s fsqrt.d
    sdiv udiv vdiv vsqrt
    idiv divb divl divq idivb idivw idivl idivq
    divss divsd divps divpd sqrtss sqrtsd
""".split())
_BRANCH_MNEMONICS = frozenset("""
    beq bne blt bge bltu bgeu beqz bnez beqi bnei blti bgei bltui bgeui
    bbci bbsi bbc bbs bany bnone ball bnall bt bf
    blez bgez bltz bgtz bgt ble bgtu bleu
    b bl bx blx cbz cbnz bcs bcc bmi bpl bvs bvc bhi bls bal
    jr jal jalr ret retw tail
    c.j c.jr c.jal c.jalr c.beqz c.bnez
""".split())


def classify_insn(line):
    """Bucket one cleaned instruction: load/store/mul/div/branch/other.

    Branch means any control transfer, calls included (a span's
    outbound calls are already itemised in the calls column).  For
    mnemonics no table knows, an AT&T-style memory operand decides
    load (source side) vs store (destination, the last operand);
    lea and alignment nops are excluded from that heuristic first.
    """
    parts = line.split(None, 1)
    mnem = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    base = mnem.split(".", 1)[0]
    if mnem in _LOAD_MNEMONICS or base in _LOAD_MNEMONICS:
        return "load"
    if mnem in _STORE_MNEMONICS or base in _STORE_MNEMONICS:
        return "store"
    if base.startswith(_MUL_PREFIXES):
        return "mul"
    if mnem in _DIV_MNEMONICS or base in _DIV_MNEMONICS:
        return "div"
    if mnem in _BRANCH_MNEMONICS or base in _BRANCH_MNEMONICS:
        return "branch"
    if base.startswith(("j", "call", "loop")):
        return "branch"
    if base.startswith(("lea", "nop")):
        return "other"
    if "(" in rest:
        depth, cut = 0, -1
        for i, ch in enumerate(rest):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                cut = i
        return "store" if "(" in rest[cut + 1:] else "load"
    return "other"


# Outbound calls by what the callee costs, most specific tier first.
# libgcc and the ARM EABI spell the same helper differently (__muldf3
# vs __aeabi_dmul), so both names reach the same tier.  mem* is tiered
# to be counted, never priced: its cost is the size argument, which the
# asm does not show.  Names no pattern knows stay in "call", where the
# calls column already shows them by name.
_LIBCALL_TIERS = (
    ("softfp-div", (
        r"__div(sf|df)3",
        r"__aeabi_[fd]div",
    )),
    ("int-div", (
        r"__u?(div|mod)(si|di|ti)3",
        r"__u?divmod\w+",
        r"__aeabi_(u?idiv(mod)?|u?ldivmod)",
    )),
    ("softfp", (
        r"__(add|sub|mul|neg|eq|ne|gt|ge|lt|le|unord|cmp)(sf|df)[23]?",
        r"__(extend|trunc)(sf|df)(sf|df)2",
        r"__fix(uns)?(sf|df)(si|di|ti)",
        r"__float(un)?(si|di|ti)(sf|df)",
        r"__aeabi_[fd](add|sub|mul|neg|cmp\w*|2\w+)",
        r"__aeabi_u?[il]2[fd]",
    )),
    ("libm", (
        r"(sqrt|sin|cos|tan|exp|exp2|log|log2|log10|pow|atan2?|fmod"
        r"|ldexp|frexp)f?",
    )),
    ("mem", (
        r"mem(cpy|set|move|cmp)",
    )),
)
_LIBCALL_TIER_RES = [(tier, re.compile("|".join(pats)))
                     for tier, pats in _LIBCALL_TIERS]


def libcall_tier(sym):
    """Tier one called symbol: softfp, softfp-div, int-div, libm, mem,
    or call.

    The tier says what a call to that symbol is, not what it costs: a
    weight per tier comes from a measured cost profile, and mem and
    call never get one.  A call through a register ("indirect(a8)")
    tiers as call.
    """
    for tier, pattern in _LIBCALL_TIER_RES:
        if pattern.fullmatch(sym):
            return tier
    return "call"


# Tiers in reading order, floating point down to the two that are only
# ever counted.  _LIBCALL_TIERS is ordered by match specificity, which
# is the matcher's business, not the reader's.
_TIER_KEYS = ("softfp", "softfp-div", "int-div", "libm", "mem", "call")


_MIX_KEYS = ("load", "store", "mul", "div", "branch", "other")


def span_mix(lines, ranges=None):
    """[{label, depth, insns, load, store, mul, div, branch, other}] per
    loop span of one function — the data behind --span-stats, table and
    JSON.  ``ranges`` is loop_span_ranges output a caller already
    walked, as in loop_spans."""
    result = []
    if ranges is None:
        ranges = loop_span_ranges(lines)
    depths = span_depths(ranges)
    for label, lo, hi in ranges:
        body = [ln for ln in lines[lo:hi + 1] if not ln.endswith(":")]
        counts = dict.fromkeys(_MIX_KEYS, 0)
        for ln in body:
            counts[classify_insn(ln)] += 1
        entry = {"label": label, "depth": depths[label],
                 "insns": len(body)}
        entry.update(counts)
        result.append(entry)
    return result


def cost_mix(lines, ranges=None):
    """What one function is made of: {classes, tiers, tiers_in_loop,
    calls}.

    classes holds all six counts over the whole function; tiers counts
    call sites per libcall tier and tiers_in_loop the subset a loop
    span contains, which is where a softfp helper stops being a
    detail; calls counts the sites per callee, for a profile that
    prices a symbol directly.  Only classes is always fully present -
    a function that calls nothing carries no empty tier table.

    This is counting, not pricing: what a class or a tier costs comes
    from a measured profile (see cost_score) and from nowhere else.
    """
    if ranges is None:
        ranges = loop_span_ranges(lines)
    in_loop = set()
    for _, lo, hi in ranges:
        in_loop.update(range(lo, hi + 1))
    classes = dict.fromkeys(_MIX_KEYS, 0)
    tiers, tiers_in_loop, calls = {}, {}, {}
    for i, line in enumerate(lines):
        if line.endswith(":"):
            continue
        classes[classify_insn(line)] += 1
        sym = call_symbol(line)
        if sym is None:
            continue
        calls[sym] = calls.get(sym, 0) + 1
        tier = libcall_tier(sym)
        tiers[tier] = tiers.get(tier, 0) + 1
        if i in in_loop:
            tiers_in_loop[tier] = tiers_in_loop.get(tier, 0) + 1
    return {"classes": classes, "tiers": tiers,
            "tiers_in_loop": tiers_in_loop, "calls": calls}


def cost_score(mix, profile):
    """(score, unweighted) for one cost_mix under a cost profile.

    An instruction costs its class weight; a call site adds its
    callee's, by symbol where the profile prices that symbol and by
    tier otherwise.  What no weight covers is counted rather than
    guessed at, so the score is never read without knowing how much of
    the function it left out.  A profile of whole numbers scores as
    one; a fractional weight anywhere rounds the total to a decimal,
    since the third digit of a measured cycle count is noise.
    """
    weights = profile["weights"]
    total, unweighted = 0, 0
    for cls, n in mix["classes"].items():
        weight = weights.get(cls)
        if weight is None:
            unweighted += n
        else:
            total += weight * n
    for sym, n in mix["calls"].items():
        weight = weights.get(sym)
        if weight is None:
            weight = weights.get(libcall_tier(sym))
        if weight is None:
            unweighted += n
        else:
            total += weight * n
    if all(isinstance(w, int) for w in weights.values()):
        return total, unweighted
    return round(total, 1), unweighted


def cost_record(mix, profile=None):
    """The --json cost object for one function: the counts, the score
    and what it left unweighted, and the profile that priced it.

    Without a profile the counts stand alone - score and profile are
    null and every instruction and call site is unweighted, which is
    exactly what an ordinal run claims.
    """
    rec = {"classes": mix["classes"], "tiers": mix["tiers"],
           "tiers_in_loop": mix["tiers_in_loop"]}
    if profile is None:
        rec["score"] = None
        rec["unweighted"] = (sum(mix["classes"].values())
                             + sum(mix["calls"].values()))
        rec["profile"] = None
        return rec
    rec["score"], rec["unweighted"] = cost_score(mix, profile)
    rec["profile"] = {"name": profile["name"]}
    rec["profile"].update(profile["provenance"])
    return rec


# One matrix row's contribution to a table: the label it is reported
# under, the functions it compiled, what to report from them (the
# pairs, the function names, or the file tag of a two-file summary),
# and the cost profile pricing its instructions.  A run renders one
# table for the whole matrix, so the builders see every row at once and
# whatever differs per row has to travel with it.
Block = collections.namedtuple("Block", "label funcs sel profile",
                               defaults=(None, None))


def block_names(block):
    """The functions a block reports, in order and without repeats: a
    pair list's members, the inspected names as given, or - when the
    selection is a file tag or nothing - everything the block holds."""
    if not isinstance(block.sel, list):
        return list(block.funcs)
    names = []
    for item in block.sel:
        for name in ((item,) if isinstance(item, str) else item):
            if name not in names:
                names.append(name)
    return names


def target_column(blocks):
    """("target",) when several matrix rows share one table, ()
    otherwise: with a single row the legend (or the lone --cc command)
    already says whose numbers these are, and a column repeating it on
    every line would push the calls off the screen for nothing."""
    return ("target",) if len({b.label for b in blocks}) > 1 else ()


def span_stats_table(blocks, max_width=None):
    """Per-span instruction mix for every function the run reports.

    One row per loop span: its nesting depth, and how many of its
    instructions load, store, multiply, divide, or branch.  This weighs
    the span the way the doctrine asks — the hand-written awk this
    replaces kept counting past the loop end into the epilogue.
    """
    lead = target_column(blocks)
    rows = [lead + ("function", "span", "depth", "insns") + _MIX_KEYS]
    for block in blocks:
        head = (block.label,) if lead else ()
        for name in block_names(block):
            for mix in span_mix(block.funcs[name]):
                rows.append(head + (name, mix["label"], str(mix["depth"]),
                                    str(mix["insns"]))
                            + tuple(str(mix[k]) for k in _MIX_KEYS))
    if len(rows) == 1:
        return "(no loop spans)"
    return render_table(rows, max_width)


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


def is_elf(path):
    """True when path is an ELF binary - by magic bytes, not extension,
    so a stripped-suffix or oddly named firmware image still routes to
    ELF mode and a text file named *.elf does not."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def find_config(explicit, sources=None):
    """Locate the config file; first hit wins, no merging.

    Order: --config PATH, then asmdiff.toml next to the first source
    file (a harness directory can carry its own targets), then the
    current directory, then ~/.config/asmdiff.toml.  ``sources`` may
    be empty (--list-targets with no file): the next-to-source slot
    is skipped.
    """
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            sys.exit(f"error: config file not found: {explicit}")
        return path
    candidates = []
    if sources:
        candidates.append(Path(sources[0]).resolve().parent / CONFIG_NAME)
    candidates += [Path.cwd() / CONFIG_NAME,
                   Path.home() / ".config" / CONFIG_NAME]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_config(path):
    """Parse a TOML config: one [table] per target, optional top-level
    `default` naming the target(s) to run when no --cc/--target is given,
    optional [groups] naming matrices of those targets."""
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
        _GLOB_CHOICES.append((name, len(matches), matches[-1]))
    return matches[-1]


# Glob resolutions made while building the matrix: (target, match
# count, chosen path).  announce_glob_choices prints them grouped, so a
# three-target ESP profile whose patterns all land in one toolchain
# directory costs one stderr line instead of three.
_GLOB_CHOICES = []


def announce_glob_choices():
    """Print the pending glob resolutions, one line per chosen
    directory, and forget them."""
    groups = {}
    for name, count, chosen in _GLOB_CHOICES:
        groups.setdefault((os.path.dirname(chosen), count), []).append(
            (name, chosen))
    _GLOB_CHOICES.clear()
    for (directory, count), members in groups.items():
        if len(members) == 1:
            name, chosen = members[0]
            print(f"target [{name}]: cc pattern matched {count} "
                  f"toolchains, using {chosen}", file=sys.stderr)
        else:
            names = ", ".join(n for n, _ in members)
            print(f"targets [{names}]: cc patterns matched {count} "
                  f"toolchains, using {directory}/", file=sys.stderr)


def config_target_names(config):
    """[table] names that are compiler targets, in file order."""
    return [k for k, v in (config or {}).items()
            if k not in CONFIG_META_KEYS and isinstance(v, dict)]


def config_groups(config, config_path=None):
    """Name -> list of target names from the optional [groups] table."""
    raw = (config or {}).get("groups")
    if raw is None:
        return {}
    where = config_path or "the config"
    if not isinstance(raw, dict):
        sys.exit(f"error: {where}: groups must be a table of "
                 "name = [target, ...] arrays")
    groups = {}
    for name, members in raw.items():
        if (isinstance(members, str)
                or not isinstance(members, list)
                or not members
                or not all(isinstance(m, str) and m for m in members)):
            sys.exit(f"error: {where}: groups.{name} must be a non-empty "
                     "array of target names")
        groups[name] = list(members)
    return groups


def config_cost_names(config):
    """[costs.NAME] profile names, in file order."""
    costs = (config or {}).get("costs")
    if not isinstance(costs, dict):
        return []
    return [name for name, table in costs.items()
            if isinstance(table, dict)]


# Tiers a profile may never price: what a memcpy or an unknown callee
# costs is the size argument or the callee's body, neither of which
# the assembly shows.  They stay counted, and the score says how many
# call sites it left out.
_UNPRICED_TIERS = ("mem", "call")


def load_costs(config, name, config_path, extended_by=None):
    """Resolve one [costs.NAME] table to {name, weights, provenance}.

    A number is a weight, keyed by instruction class, libcall tier, or
    call symbol; a string is provenance, and measured_on and method
    are required, because a cycle count whose origin is out of sight
    invites more trust than it earned.  extends = "OTHER" copies
    another profile's weights first, one level only: these are
    measurements, not a class hierarchy.
    """
    where = config_path or "the config"
    tables = (config or {}).get("costs")
    entry = tables.get(name) if isinstance(tables, dict) else None
    if not isinstance(entry, dict):
        known = ", ".join(config_cost_names(config))
        sys.exit(f"error: no [costs.{name}] in {where}"
                 + ("; cost profiles: " + known if known
                    else "; it defines no cost profiles"))
    weights, provenance = {}, {}
    base = entry.get("extends")
    if base is not None:
        if not isinstance(base, str):
            sys.exit(f"error: [costs.{name}]: extends must name another "
                     "cost profile")
        if extended_by is not None:
            sys.exit(f"error: [costs.{name}]: extends is one level only, "
                     f"and [costs.{extended_by}] already extends it")
        weights.update(load_costs(config, base, config_path, name)["weights"])
    for key, value in entry.items():
        if key == "extends":
            continue
        if isinstance(value, str):
            provenance[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            sys.exit(f"error: [costs.{name}]: {key} must be a weight "
                     "(number) or provenance (string)")
        if key in _UNPRICED_TIERS:
            sys.exit(f"error: [costs.{name}]: {key} has no weight; its "
                     "cost is the size argument or the callee's body, "
                     "which the assembly does not show")
        weights[key] = value
    for field in ("measured_on", "method"):
        if not provenance.get(field):
            sys.exit(f'error: [costs.{name}]: {field} = "..." is '
                     "required; a score nobody can place is not a "
                     "measurement")
    ordered = {field: provenance.pop(field)
               for field in ("measured_on", "method")}
    ordered.update(provenance)
    return {"name": name, "weights": weights, "provenance": ordered}


# Name -> "target" / "group" for everything EXAMPLE_CONFIG defines, parsed
# on first use by _example_names().
_EXAMPLE_NAMES = None


def _example_names():
    """What the built-in example config defines, parsed once and only when
    a name has already failed to resolve - a normal run never pays for it.
    Without tomllib (Python < 3.11) there is nothing to parse and the
    pointer is simply omitted."""
    global _EXAMPLE_NAMES
    if _EXAMPLE_NAMES is None:
        kinds = {}
        if tomllib is not None:
            example = tomllib.loads(EXAMPLE_CONFIG)
            for name in config_groups(example):
                kinds[name] = "group"
            for name in config_target_names(example):
                kinds[name] = "target"
        _EXAMPLE_NAMES = kinds
    return _EXAMPLE_NAMES


def _known_suffix(config, config_path, name=None):
    """'; targets: ...; groups: ...' for unknown-name errors.

    A name the user's config lacks but the example config defines is
    almost always a README example run against a hand-written config, so
    say where the name comes from instead of only what is missing.
    """
    targets = config_target_names(config)
    groups = list(config_groups(config, config_path))
    bits = []
    if targets:
        bits.append("targets: " + ", ".join(targets))
    if groups:
        bits.append("groups: " + ", ".join(groups))
    suffix = "; no targets defined" if not bits else "; " + "; ".join(bits)
    kind = _example_names().get(name) if name else None
    if kind:
        suffix += (f"; {name} is a {kind} in the built-in example config "
                   "(asmdiff --example-config)")
    return suffix


def _split_target_token(token):
    """Split a --target value on commas, except inside a [...] glob class."""
    parts, buf, depth = [], "", 0
    for ch in token:
        if ch == "[":
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
        if ch == "," and not depth:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def expand_target_args(target_args, config, config_path):
    """Expand --target tokens: commas, [groups] names, fnmatch globs.

    An exact target table wins over a group of the same name.  Globs
    match target table names only (in config order).
    """
    targets = config_target_names(config)
    groups = config_groups(config, config_path)
    names = []
    for token in target_args:
        parts = _split_target_token(token)
        if not parts:
            sys.exit(f"error: empty --target {token!r}")
        for part in parts:
            names.extend(_resolve_target_token(part, targets, groups,
                                               config, config_path))
    return names


def _resolve_target_token(part, targets, groups, config, config_path):
    if part in targets:
        return [part]
    if part in groups:
        missing = [m for m in groups[part] if m not in targets]
        if missing:
            sys.exit(f"error: group {part!r} names unknown target(s) "
                     + ", ".join(missing)
                     + _known_suffix(config, config_path))
        return list(groups[part])
    if any(ch in part for ch in "*?["):
        matched = [n for n in targets if fnmatch.fnmatch(n, part)]
        if not matched:
            sys.exit(f"error: --target {part!r} matched no target in "
                     f"{config_path or 'any config file'}"
                     + _known_suffix(config, config_path))
        return matched
    sys.exit(f"error: no [{part}] target in "
             f"{config_path or 'any config file'}"
             + _known_suffix(config, config_path, part))


def list_targets(config, config_path):
    """Print default, groups, and targets; used by --list-targets."""
    if not config:
        print("no config file found; default matrix is "
              + ", ".join(f"{cc} {FALLBACK_FLAGS}"
                          for cc in DEFAULT_COMPILERS))
        return 0
    print(f"config: {config_path}")
    default = config.get("default")
    if default:
        if isinstance(default, str):
            default = [default]
        print("default: " + ", ".join(default))
    else:
        print("default: (none; gcc -O3 and clang -O3)")
    groups = config_groups(config, config_path)
    if groups:
        print("groups:")
        for name, members in groups.items():
            print(f"  {name}: " + ", ".join(members))
    costs = config_cost_names(config)
    if costs:
        print("costs:")
        for name in costs:
            measured = config["costs"][name].get("measured_on", "?")
            print(f"  {name}: {measured}")
    print("targets:")
    for name in config_target_names(config):
        cc = config[name].get("cc", "")
        print(f"  {name}: {cc}")
    return 0


def target_command(config, name, config_path, want_db=True):
    """Resolve a named [target] table to one 'CC FLAGS' matrix entry.

    A `compile_commands = true` entry asks for the discovery walk, and
    tolerates a miss: finding no database leaves the target with none
    after a note on stderr, since the config says where the target is
    usually compiled, not what this run must have.  A path that does
    not resolve stays an error, and so does a bare --compile-commands
    that finds nothing (build_matrix's call, not this one).

    want_db=False skips the lookup and the walk entirely - ELF mode
    resolves targets only to find their toolchain.
    """
    entry = (config or {}).get(name)
    if not isinstance(entry, dict) or name in CONFIG_META_KEYS:
        sys.exit(f"error: no [{name}] target in "
                 f"{config_path or 'any config file'}"
                 + _known_suffix(config, config_path, name))
    cc = entry.get("cc")
    if not isinstance(cc, str):
        sys.exit(f'error: target [{name}] needs cc = "compiler"')
    flags = entry.get("flags", [])
    if isinstance(flags, str) or not all(isinstance(f, str) for f in flags):
        sys.exit(f"error: target [{name}]: flags must be an array of strings")
    flags = [os.path.expandvars(f) for f in flags]
    db = entry.get("compile_commands") if want_db else None
    discovered = False
    if db is True:                       # opt in to CWD-based discovery
        db = _discovered_db(f"target [{name}]", required=False)
        discovered = db is not None
    elif db is False:
        db = None
    elif db is not None:
        if not isinstance(db, str):
            sys.exit(f"error: target [{name}]: compile_commands must be a "
                     "path to a compile_commands.json, or true to search "
                     "upward from the current directory")
        db = os.path.expandvars(os.path.expanduser(db))
    # Resolved here rather than at render time, so a misspelled profile
    # fails before the matrix spends a compile on it.
    costs = entry.get("costs")
    if costs is not None:
        if not isinstance(costs, str):
            sys.exit(f"error: target [{name}]: costs must name a "
                     "[costs.NAME] profile")
        costs = load_costs(config, costs, config_path)
    return Target(shlex.join([resolve_cc(cc, name), *flags]), db, discovered,
                  costs)


def row_label(row, index, total):
    """How one matrix row is named in the output: the config target it
    came from, else its position in the matrix (``cc#N``).

    A lone unnamed row keeps its whole command as its label: with
    nothing to tell it apart from, a position number would only stand
    between the reader and the compiler that produced the table.
    """
    label = getattr(row, "label", None)
    if label is not None:
        return label
    return f"cc#{index}" if total > 1 else str(row)


def matrix_labels(rows):
    """Labels for a whole matrix, in row order.  Rows that never went
    through build_matrix - a bare command string from a caller or a
    test - are labelled here instead."""
    return [row_label(row, n, len(rows)) for n, row in enumerate(rows, 1)]


def _named(target, name):
    """Label a matrix row with the config target name it came from."""
    target.label = name
    return target


def build_matrix(cc_args, target_args, config, config_path, db_arg=None,
                 want_db=True, costs_arg=None):
    """Resolve the compiler matrix.

    --cc strings verbatim, then --target entries (comma-lists, groups,
    and globs expanded), in that order.  With neither, the config's
    `default`, which goes through the same expansion as -t: a target
    name, a [groups] name, a comma list, a glob, or an array mixing
    those, and a name it cannot resolve is the error -t would give.
    With no config or no default, plain gcc/clang at -O3.

    Every row comes back as a Target carrying the label it is reported
    under, so the legend, the tables, and the listings all name the
    same thing (see row_label).

    ``db_arg`` is the --compile-commands value: a PATH applies that
    database to every entry, True discovers one near the CWD.  A target
    whose config names its own compile_commands keeps it.

    ``costs_arg`` is --costs: one profile for every entry, overriding
    what a target named, which is how a --cc row gets one at all.
    """
    entries = [Target(cc) for cc in cc_args]
    entries += [_named(target_command(config, name, config_path, want_db),
                       name)
                for name in expand_target_args(target_args, config,
                                               config_path)]
    if not entries:
        default = (config or {}).get("default")
        if default:
            if isinstance(default, str):
                tokens = [default]
            elif (isinstance(default, list)
                    and all(isinstance(n, str) and n for n in default)):
                tokens = list(default)
            else:
                sys.exit(f"error: {config_path or 'the config'}: default "
                         "must be a target or group name, or an array of "
                         "such names")
            # Same expansion as -t, so a group, a comma list or a glob
            # names the standing matrix as readily as a single target.
            entries = [_named(target_command(config, name, config_path,
                                             want_db), name)
                       for name in expand_target_args(tokens, config,
                                                      config_path)]
        else:
            entries = [Target(f"{cc} {FALLBACK_FLAGS}")
                       for cc in DEFAULT_COMPILERS]
    if db_arg is not None:
        if db_arg is True:
            db, discovered = _discovered_db("--compile-commands",
                                            required=True), True
        else:
            db = os.path.expandvars(os.path.expanduser(db_arg))
            discovered = False
        entries = [e if getattr(e, "compile_commands", None) is not None
                   else Target(e, db, discovered,
                               getattr(e, "costs", None),
                               getattr(e, "label", None))
                   for e in entries]
    if costs_arg is not None:
        profile = load_costs(config, costs_arg, config_path)
        entries = [Target(e, getattr(e, "compile_commands", None),
                          getattr(e, "db_discovered", False), profile,
                          getattr(e, "label", None))
                   for e in entries]
    for n, entry in enumerate(entries, start=1):
        if entry.label is None:
            entry.label = row_label(entry, n, len(entries))
    announce_glob_choices()
    return entries


# --verbose: print full compiler command lines and untrimmed stderr.
VERBOSE = False
# --summary-only: print the stats tables and skip every listing.  The
# tables are the decision input; a listing is pulled on a second run
# when a delta needs explaining.
SUMMARY_ONLY = False
# --span-stats: follow the stats table with the per-loop-span
# instruction mix (see span_stats_table).
SPAN_STATS = False
# --json: collect summary records here instead of printing tables;
# None means normal table output.  main() dumps the collected list.
JSON_OUT = None
# --cost: add the cost column to the stats tables and the cost object
# to --json.  COST_PROFILE is the [costs.NAME] table pricing the
# counts, None until a target or --costs names one; without it the
# column is ordinal - classes and tiers, no score.
COST = False
COST_PROFILE = None
# --fail-on-growth: candidates whose instruction count exceeds their
# baseline's, as (function, delta, baseline label, candidate label).
# main() turns a non-empty list into exit status 3, which a CI job can
# tell from the tool's own error status 1 and argparse's usage 2.
FAIL_ON_GROWTH = False
GROWTH = []


def note_growth(func, base_lines, cand_lines, base_label, cand_label):
    """Record a pair whose candidate grew, for --fail-on-growth.

    A no-op without the flag, so the pair walk costs nothing when
    nobody asked for the exit status.
    """
    if not FAIL_ON_GROWTH:
        return
    grew = analyze(cand_lines)[0] - analyze(base_lines)[0]
    if grew > 0:
        GROWTH.append((func, grew, base_label, cand_label))


def use_cost_profile(cc_cmd):
    """Point COST_PROFILE at the profile this matrix row names.

    A profile prices one target's instruction set, so it follows the
    row being reported rather than the run; comparing two targets, the
    candidate's is the one its score belongs to.  A plain command
    string (a --cc row without --costs) leaves the column ordinal.
    """
    global COST_PROFILE
    if cc_cmd is not None:
        COST_PROFILE = getattr(cc_cmd, "costs", None)


def print_legend(rows):
    """"label: command" for every matrix row, once per run.

    A single unnamed row is its own label, so there is nothing to look
    up and nothing is printed; anything else - several rows, or a row a
    config target named - gets the legend, and the tables and listings
    below it say only the label.
    """
    labels = matrix_labels(rows)
    if len(rows) < 2 and all(label == str(row)
                             for label, row in zip(labels, rows)):
        return
    skipped = {miss.split(":", 1)[0] for miss in _SKIPPED_CCS}
    failed = {label for label, _ in _FAILURES}
    print()
    for label, row in zip(labels, rows):
        # The legend prints after the compile phase, so a row that
        # produced nothing can say why where the reader looks it up.
        if shlex.split(row)[0] in skipped:
            note = " (not found on PATH, skipped)"
        elif label in failed:
            note = " (compile failed, see stderr)"
        else:
            note = ""
        print(f"{label}: {row}{note}")


def table_footer(blocks):
    """What follows the run's stats table: the profiles that priced its
    scores, then the --span-stats mix table.  Both are silent no-ops
    when their flag is off or --json holds the output.

    One line per distinct profile, not per row: a matrix of four
    targets sharing one measured profile has one thing to say about
    where its numbers come from.
    """
    if JSON_OUT is not None:
        return
    if COST:
        seen = []
        for block in blocks:
            if block.profile is not None and block.profile["name"] not in seen:
                seen.append(block.profile["name"])
                print(format_provenance(block.profile))
    if SPAN_STATS:
        print()
        print(span_stats_table(blocks, table_width()))


def json_record(name, lines, cc=None, tag=None, role=None, baseline=None,
                target=None):
    """One --json result: a summary-table row as data.  target is the
    matrix row's label (the table's target column), cc the resolved
    compiler command, tag the source/side label of a two-file
    comparison, role baseline/candidate for paired rows, baseline the
    paired baseline's lines, which adds the candidate's delta object;
    each is omitted where the mode has no such notion.  Spans, depths,
    and the cost mix share one walk over the lines."""
    insns, calls = analyze(lines)
    ranges = loop_span_ranges(lines)
    mix = cost_mix(lines, ranges) if COST else None
    rec = {"function": name}
    if target is not None:
        rec["target"] = target
    if cc is not None:
        rec["cc"] = str(cc)
    if tag is not None:
        rec["tag"] = tag
    if role is not None:
        rec["role"] = role
    rec["insns"] = insns
    depths = span_depths(ranges)
    rec["loop_spans"] = [{"label": label, "insns": n,
                          "depth": depths[label]}
                         for label, n in loop_spans(lines, ranges)]
    rec["calls"] = calls
    if baseline is not None:
        rec["delta"] = pair_delta(baseline, lines)
        if COST:
            rec["delta"]["cost"] = cost_delta(cost_mix(baseline), mix,
                                              COST_PROFILE)
    if COST:
        rec["cost"] = cost_record(mix, COST_PROFILE)
    if SPAN_STATS:
        rec["span_stats"] = span_mix(lines, ranges)
    return rec
# Without --verbose, a failed compile shows this many stderr lines — enough
# for the include chain plus the first error, which is the actionable part.
MAX_STDERR_LINES = 20

# Compilers skipped because their binary was missing, in matrix order.
# The final no-usable-compiler error repeats them (via _skipped_suffix)
# so the fix is visible in the error itself, not only in an earlier
# stderr warning that may have scrolled away.
_SKIPPED_CCS = []


def _skipped_suffix():
    """" (cc: not found on PATH; ...)" naming every skipped compiler,
    empty when nothing was skipped."""
    return f" ({'; '.join(_SKIPPED_CCS)})" if _SKIPPED_CCS else ""


# Compile failures recorded during a run, as (label, message).  A row
# that fails to compile no longer ends the run: the rows that did
# compile still answer the question that was asked, so their tables
# print first and the messages wait for report_failures.
_FAILURES = []


def _compile_failure(cmd, stderr, label=None):
    """Record a failed compile; the caller drops that row and goes on.

    A borrowed-flags command runs to hundreds of tokens and a broken
    header environment produces pages of stderr; dumping both buries the
    actual error.  Default: compiler + source + the first stderr lines.
    --verbose restores the complete command and output.  ``label`` names
    the matrix row when the matrix has more than one thing to blame.
    """
    where = f"[{label}] " if label else ""
    if VERBOSE:
        _FAILURES.append((label, f"error: {where}compile failed: "
                                 f"{shlex.join(cmd)}\n{stderr}"))
        return None
    lines = stderr.splitlines()
    shown = "\n".join(lines[:MAX_STDERR_LINES])
    dropped = len(lines) - MAX_STDERR_LINES
    more = f"\n... {dropped} more stderr lines" if dropped > 0 else ""
    _FAILURES.append((label, f"error: {where}{cmd[0]} failed on {cmd[-1]}\n"
                             f"{shown}{more}\n(re-run with --verbose for "
                             "the full command and output)"))
    return None


def report_failures():
    """Print every recorded compile failure on stderr; True if there
    were any, which the run turns into exit status 1.

    They come last, after the tables: what did compile is the answer
    the caller asked for, and a row that broke should not push it off
    the screen."""
    for _, message in _FAILURES:
        print(message, file=sys.stderr)
    return bool(_FAILURES)


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
    flags.  Returns None if the compiler is not on PATH (with a warning)
    or the compile failed (with the error recorded for report_failures);
    either way the caller drops the row and reports the rest.
    """
    argv = shlex.split(cc_cmd)
    if shutil.which(argv[0]) is None:
        print(f"warning: {argv[0]} not found on PATH, skipping",
              file=sys.stderr)
        miss = f"{argv[0]}: not found on PATH"
        if miss not in _SKIPPED_CCS:
            _SKIPPED_CCS.append(miss)
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
        # A lone --cc row is labelled with its own command; naming it in
        # front of a message that already quotes the compiler would only
        # make today's single-row error longer.
        label = getattr(cc_cmd, "label", None)
        return _compile_failure(cmd, proc.stderr,
                                label if label != str(cc_cmd) else None)
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


# --collapse keeps this many line pairs of context around each
# differing pair; longer identical runs are elided with a count.
COLLAPSE_CONTEXT = 3
# --collapse: elide identical runs in side-by-side listings.
COLLAPSE = False

# A trailing assembler comment (" # TAILCALL", " # 8-byte Reload",
# " # sp + 16") is the compiler talking, not an instruction, and it
# costs columns the operands need at half a terminal's width.  The
# space after the hash is what tells it from an ARM "#4" immediate.
TRAILING_COMMENT = re.compile(r"\s+#\s.*$")


def side_by_side(left, right, ltitle, rtitle, width=44, collapse=False):
    """Two-column view of a pair's asm lines.

    Trailing assembler comments are dropped here only: the single
    column listings and --json keep the line as it was extracted.

    With collapse, runs of identical line pairs shrink to the
    COLLAPSE_CONTEXT pairs around each difference plus an elision
    marker — two ~500-insn functions differing by a handful of
    instructions render as a few hunks instead of ~1000 lines.  The
    sides are aligned first (difflib), not paired by position: an
    inserted instruction gets a gap opposite it instead of desyncing
    every following pair.  Two sides that are equal collapse to their
    header: there is no reading to do, and the table above has the
    counts.
    """
    identical = collapse and left == right
    if identical:
        ltitle += " (identical)"
    rows = [f"{ltitle:<{width}} | {rtitle}",
            f"{'-' * width}-+-{'-' * width}"]
    if identical:
        return "\n".join(rows)
    if collapse:
        pairs = []
        matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            pairs += zip_longest(left[i1:i2], right[j1:j2], fillvalue="")
        keep = [False] * len(pairs)
        for i, (l, r) in enumerate(pairs):
            if l != r:
                for j in range(max(0, i - COLLAPSE_CONTEXT),
                               min(len(pairs), i + COLLAPSE_CONTEXT + 1)):
                    keep[j] = True
    else:
        pairs = list(zip_longest(left, right, fillvalue=""))
        keep = [True] * len(pairs)
    elided = 0
    for (l, r), kept in zip(pairs, keep):
        if not kept:
            elided += 1
            continue
        if elided:
            rows.append(f"    ... {elided} identical lines ...")
            elided = 0
        l = TRAILING_COMMENT.sub("", l.expandtabs(8))[:width]
        r = TRAILING_COMMENT.sub("", r.expandtabs(8))[:width]
        rows.append(f"{l:<{width}} | {r}")
    if elided:
        rows.append(f"    ... {elided} identical lines ...")
    return "\n".join(rows)


# The elision marker closing a truncated callee cell.  The leading
# "..." keeps it from reading as one more callee name.
CALLS_ELIDED_RE = re.compile(r"\.\.\. \((\d+) total\)")


def _fit_calls(cell, budget):
    """Trim a comma-joined callee cell to at most budget columns by
    dropping whole callees from the end, closing with a "... (N total)"
    elision marker (N is always the full callee count, preserved from
    an existing marker when the cell was already capped). The first
    callee is never dropped; a cell that still overflows just wraps."""
    if len(cell) <= budget:
        return cell
    items = cell.split(", ")
    m = CALLS_ELIDED_RE.fullmatch(items[-1])
    if m:
        items, summary = items[:-1], items[-1]
    else:
        summary = f"... ({len(items)} total)"
    if len(items) < 2:
        return cell
    for keep in range(len(items) - 1, 0, -1):
        cand = ", ".join(items[:keep]) + ", " + summary
        if len(cand) <= budget:
            return cand
    return items[0] + ", " + summary


def render_table(rows, max_width=None):
    """Column-aligned text for a list of equal-length string tuples.

    max_width (terminal columns) keeps each row on one line by
    trimming the last column's cells (see _fit_calls); the other
    columns are never touched. None renders untrimmed."""
    widths = column_widths(rows)
    if max_width is not None:
        fixed = widths[:-1]
        budget = max_width - sum(fixed) - 2 * len(fixed)
        rows = [row[:-1] + (_fit_calls(row[-1], budget),) for row in rows]
        widths[-1] = max(len(row[-1]) for row in rows)
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip()
        for row in rows)


def column_widths(*row_sets):
    """Widest cell per column over every row set given."""
    rows = [row for rows in row_sets for row in rows]
    return [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]


# --width: the column budget the user asked for, 0 meaning unlimited.
# None leaves the decision to table_width().
WIDTH = None
# What a table falls back to off a terminal.  Wide enough for a matrix
# row with a few callees, narrow enough to paste into a report or an
# issue without it rewrapping.
DEFAULT_WIDTH = 120
# One side of a side-by-side listing never narrows past this: an asm
# line with a label and two operands still fits.
MIN_LISTING_WIDTH = 44


def width_arg(value):
    """argparse type for --width: 0 or a positive column budget.

    A negative budget would reach the renderers as a width every
    column overflows, which reads as a broken table rather than as a
    rejected argument."""
    try:
        width = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid int value: %r" % value)
    if width < 0:
        raise argparse.ArgumentTypeError(
            "must be 0 (unlimited) or a positive column count, not %d"
            % width)
    return width


def table_width():
    """Column budget for tables and listings.

    --width wins; then the terminal when stdout is a tty; then
    $COLUMNS, which is how a caller driving a pipe says how wide its
    reader is; else DEFAULT_WIDTH.  None means unlimited, which keeps
    every callee greppable at whatever width the row ends up."""
    if WIDTH is not None:
        return WIDTH or None
    if sys.stdout.isatty():
        return shutil.get_terminal_size().columns
    columns = os.environ.get("COLUMNS", "")
    return int(columns) if columns.isdigit() and int(columns) else DEFAULT_WIDTH


def listing_width():
    """Width of one column of a side-by-side listing: half the table
    budget, less the " | " between the sides."""
    total = table_width()
    if total is None:
        return MIN_LISTING_WIDTH
    return max(MIN_LISTING_WIDTH, (total - 3) // 2)


# Past a handful of spans the column stops being readable and the
# count is all that is left to say; --json keeps every span.
MAX_SPANS_SHOWN = 6


def format_spans(spans):
    """Space-joined "label:insns" per span, capped with a "+N more"
    tail so one dispatch function cannot widen the whole table."""
    shown = " ".join(f"{label}:{n}"
                     for label, n in spans[:MAX_SPANS_SHOWN])
    if len(spans) > MAX_SPANS_SHOWN:
        shown += f" +{len(spans) - MAX_SPANS_SHOWN} more"
    return shown or "-"


# Real firmware dispatch functions call dozens of distinct symbols; an
# uncapped list makes summary rows thousands of characters wide.
MAX_CALLS_SHOWN = 8


def format_calls(calls):
    """Comma-joined callee list; longer lists keep the first
    MAX_CALLS_SHOWN callees and close with a "... (N total)" elision
    marker."""
    if not calls:
        return "-"
    if len(calls) > MAX_CALLS_SHOWN:
        return (", ".join(calls[:MAX_CALLS_SHOWN])
                + f", ... ({len(calls)} total)")
    return ", ".join(calls)


def format_delta_insns(n):
    """Signed instruction delta, with an unsigned zero so a row that
    changed nothing does not read as a small improvement."""
    return f"{n:+d}" if n else "0"


def format_delta_spans(base_spans, cand_spans):
    """Per-position "before -> after" span sizes.

    Only the sizes: the labels are the compiler's and rarely survive a
    rewrite.  Sides with different span counts print "-" instead, since
    a loop that was fused, split or unrolled away leaves the positions
    with nothing to line up against.
    """
    if not base_spans or len(base_spans) != len(cand_spans):
        return "-"
    return " ".join(f"{b} -> {c}" for (_, b), (_, c)
                    in zip(base_spans, cand_spans))


def format_delta_calls(delta):
    """Callees the candidate gained (+) and lost (-), "-" for an
    unchanged call set."""
    changed = (["+" + s for s in delta["calls_added"]]
               + ["-" + s for s in delta["calls_removed"]])
    return " ".join(changed) or "-"


# Class names abbreviated for the cost cell; the tiers keep the names
# libcall_tier gives them, which is what a profile's keys spell.
_COST_LABELS = {"load": "ld", "store": "st", "mul": "mul", "div": "div",
                "branch": "br", "other": "oth"}

# The cost cell sits before the ragged calls column, so render_table
# never trims it: it caps itself instead.
COST_CELL_BUDGET = 48


def _fit_cost(parts, budget=COST_CELL_BUDGET):
    """Join cost cell parts within budget, dropping whole ones from the
    end and closing with "...".  The first part is always kept: with a
    profile it is the score, and a cell that dropped that would say
    less than nothing."""
    cell = " ".join(parts)
    if len(cell) <= budget:
        return cell
    kept = []
    for part in parts:
        if kept and len(" ".join(kept + [part, "..."])) > budget:
            break
        kept.append(part)
    return " ".join(kept + ["..."])


def format_cost(mix, profile=None):
    """One cost cell: the non-zero classes and tiers of a function,
    each tier followed by how many of its call sites a loop span
    holds, after a score when a profile prices them."""
    parts = []
    if profile is not None:
        score, unweighted = cost_score(mix, profile)
        parts.append(f"score {score} ({unweighted} unweighted)")
    parts += [f"{_COST_LABELS[k]} {mix['classes'][k]}"
              for k in _MIX_KEYS if mix["classes"][k]]
    for tier in _TIER_KEYS:
        n = mix["tiers"].get(tier, 0)
        if not n:
            continue
        hot = mix["tiers_in_loop"].get(tier, 0)
        parts.append(f"{tier} {n}" + (f" ({hot} in loop)" if hot else ""))
    return _fit_cost(parts) if parts else "-"


def format_provenance(profile):
    """The line under any table that shows a score: which profile
    priced it, measured on what and how, and whatever else the table
    said about itself."""
    prov = profile["provenance"]
    extra = "".join(f"; {key}: {value}" for key, value in prov.items()
                    if key not in ("measured_on", "method"))
    return (f"costs: {profile['name']} - {prov['measured_on']}; "
            + prov["method"] + extra)


def cost_delta(base_mix, cand_mix, profile=None):
    """What the candidate's mix changed: signed counts for the classes
    and tiers that moved, and the score difference when a profile
    prices them.  Counts that stayed put are left out, so the row says
    what the rewrite did instead of restating what it kept."""
    classes = {k: cand_mix["classes"][k] - base_mix["classes"][k]
               for k in _MIX_KEYS
               if cand_mix["classes"][k] != base_mix["classes"][k]}
    tiers = {}
    for tier in _TIER_KEYS:
        moved = (cand_mix["tiers"].get(tier, 0)
                 - base_mix["tiers"].get(tier, 0))
        if moved:
            tiers[tier] = moved
    score = None
    if profile is not None:
        score = (cost_score(cand_mix, profile)[0]
                 - cost_score(base_mix, profile)[0])
        if isinstance(score, float):
            score = round(score, 1)
    return {"classes": classes, "tiers": tiers, "score": score}


def format_cost_delta(delta):
    """The delta row's cost cell: the signed score first when one was
    computed, then the classes and tiers that moved; "-" when the mix
    came through the rewrite unchanged."""
    parts = []
    if delta["score"] is not None:
        parts.append("score " + (f"{delta['score']:+g}"
                                 if delta["score"] else "0"))
    parts += [f"{_COST_LABELS[k]} {n:+d}"
              for k, n in delta["classes"].items()]
    parts += [f"{tier} {n:+d}" for tier, n in delta["tiers"].items()]
    return _fit_cost(parts) if parts else "-"


def summary_table(blocks, max_width=None):
    """Instruction counts, loop spans, and outbound calls per pair
    member, and a delta row closing each pair, for the whole matrix in
    one table.  Calls come last: the one unbounded column stays ragged
    right so the counts and spans keep their alignment."""
    lead = target_column(blocks)
    rows = [lead + ("function", "role", "insns", "loop spans")
            + (("cost",) if COST else ()) + ("calls",)]
    for block in blocks:
        # The target cell repeats on every row of a block rather than
        # dittoing: one grep for a target name catches all of its rows.
        head = (block.label,) if lead else ()
        funcs = block.funcs
        for old, new in block.sel:
            mixes = {}
            for name, role in ((old, "baseline"), (new, "candidate")):
                insns, calls = analyze(funcs[name])
                row = (name, role, str(insns),
                       format_spans(loop_spans(funcs[name])))
                if COST:
                    mixes[name] = cost_mix(funcs[name])
                    row += (format_cost(mixes[name], block.profile),)
                rows.append(head + row + (format_calls(calls),))
            delta = pair_delta(funcs[old], funcs[new])
            row = ("", "delta", format_delta_insns(delta["insns"]),
                   format_delta_spans(loop_spans(funcs[old]),
                                      loop_spans(funcs[new])))
            if COST:
                row += (format_cost_delta(cost_delta(mixes[old], mixes[new],
                                                     block.profile)),)
            rows.append(head + row + (format_delta_calls(delta),))
    return render_table(rows, max_width)


def file_summary_table(blocks, max_width=None):
    """Per-function counts plus a whole-file total row, for every
    matrix row (and, comparing two files, every file) in one table.

    The total sums instruction counts over every function parsed from
    the -S output and unions their outbound calls — a coarse A/B sanity
    check, not a code-size measurement (literal pools, data, and
    alignment are not included).  A block that selected nothing keeps
    its line: which target and file came back empty is a finding.
    """
    lead = target_column(blocks)
    tagged = ("file",) if any(b.sel for b in blocks) else ()
    rows = [lead + tagged + ("function", "insns", "loop spans")
            + (("cost",) if COST else ()) + ("calls",)]
    for block in blocks:
        head = ((block.label,) if lead else ()) + ((block.sel,) if tagged
                                                   else ())
        total_insns, all_calls = 0, []
        for name, lines in block.funcs.items():
            insns, calls = analyze(lines)
            total_insns += insns
            for sym in calls:
                if sym not in all_calls:
                    all_calls.append(sym)
            row = (name, str(insns), format_spans(loop_spans(lines)))
            if COST:
                row += (format_cost(cost_mix(lines), block.profile),)
            rows.append(head + row + (format_calls(calls),))
        if not block.funcs:
            rows.append(head + ("(none)", "-", "-")
                        + (("-",) if COST else ()) + ("-",))
            continue
        total = (f"TOTAL ({len(block.funcs)} functions)",
                 str(total_insns), "-")
        if COST:
            # A mix is per function; a file-wide sum of them would price
            # nothing the rows do not already say.
            total += ("-",)
        rows.append(head + total + (format_calls(all_calls),))
    return render_table(rows, max_width)


def listing(name, lines):
    """gcc-style listing of one extracted function: the function label
    at column 0, instructions tabbed, kept local labels back at
    column 0 (extract_functions stores them whitespace-stripped)."""
    body = [line if line.endswith(":") else "\t" + line for line in lines]
    return "\n".join([f"{name}:"] + body)


def inspect_table(blocks, max_width=None):
    """Stats rows for the inspected functions only - no pair roles and
    no whole-file total, unlike summary_table/file_summary_table."""
    lead = target_column(blocks)
    rows = [lead + ("function", "insns", "loop spans")
            + (("cost",) if COST else ()) + ("calls",)]
    for block in blocks:
        head = (block.label,) if lead else ()
        for name in block.sel:
            insns, calls = analyze(block.funcs[name])
            row = (name, str(insns),
                   format_spans(loop_spans(block.funcs[name])))
            if COST:
                row += (format_cost(cost_mix(block.funcs[name]),
                                    block.profile),)
            rows.append(head + row + (format_calls(calls),))
    return render_table(rows, max_width)


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


def report_across(fn_names, left_funcs, right_funcs, left_tag, right_tag,
                  ccs=(None, None), labels=(None, None)):
    """One two-sided comparison as a table block.

    Checks that both sides hold every function, records the growth
    check and the --json records, and decorates each pair member with
    its side's tag so one table can carry both sides.  ``labels`` are
    the matrix rows the sides came from: the same row twice when the
    sides are two source files, two rows when they are two compilers -
    which is also what names the block.  Returns None under --json,
    where there is no table to build.
    """
    missing = sorted({f for f in fn_names
                      if f not in left_funcs or f not in right_funcs})
    if missing:
        sys.exit("error: function(s) not in asm: " + ", ".join(missing)
                 + f"; {left_tag} has: " + (", ".join(left_funcs) or "none")
                 + f"; {right_tag} has: " + (", ".join(right_funcs) or "none"))
    use_cost_profile(ccs[1])
    for f in fn_names:
        note_growth(f, left_funcs[f], right_funcs[f], left_tag, right_tag)
    if JSON_OUT is not None:
        for f in fn_names:
            JSON_OUT.append(json_record(f, left_funcs[f], cc=ccs[0],
                                        tag=left_tag, role="baseline",
                                        target=labels[0]))
            JSON_OUT.append(json_record(f, right_funcs[f], cc=ccs[1],
                                        tag=right_tag, role="candidate",
                                        baseline=left_funcs[f],
                                        target=labels[1]))
        return None
    decorated, pairs = {}, []
    for f in fn_names:
        lt, rt = f"{f} [{left_tag}]", f"{f} [{right_tag}]"
        decorated[lt], decorated[rt] = left_funcs[f], right_funcs[f]
        pairs.append((lt, rt))
    label = (labels[0] if labels[0] == labels[1]
             else f"{labels[0]} vs {labels[1]}")
    # The candidate's target is the one whose instruction set a score
    # would belong to, so its profile prices the block.
    return Block(label, decorated, pairs, getattr(ccs[1], "costs", None))


def print_listings(blocks, headed):
    """The side-by-side listings under each block, after the table.

    ``headed`` prints a ``== LABEL ==`` line above each group, which is
    what tells two targets' listings apart; a run with one group has
    the legend and the table above it saying the same thing.
    """
    for block in blocks:
        if headed:
            print(f"\n== {block.label} ==")
        for left, right in block.sel:
            print()
            print(side_by_side(block.funcs[left], block.funcs[right],
                               left, right, width=listing_width(),
                               collapse=COLLAPSE))


def compile_matrix(matrix, sources, extra_flags, tmp):
    """Phase one of every compile mode: every row, every source.

    Returns (label, cc_cmd, [functions per source]) for each row that
    produced assembly for all of its sources; a row whose compiler is
    missing or whose compile failed is left out, and report_failures
    has the reason.  Compiling the whole matrix before anything renders
    is what lets one broken row keep the others' table.
    """
    collected = []
    for label, cc_cmd in zip(matrix_labels(matrix), matrix):
        sides = []
        for src in sources:
            asm = compile_to_asm(cc_cmd, extra_flags, src, tmp)
            if asm is None:
                break
            sides.append(extract_functions(asm))
        if len(sides) == len(sources):
            collected.append((label, cc_cmd, sides))
    return collected


def run_across(sources, matrix, fn_names, extra_flags, tmp):
    """--across mode: same function, two compilations.

    Two source files: compare fileA's FUNC vs fileB's FUNC under each
    compiler in the matrix.  One source file: compare FUNC between the
    first --cc entry (baseline) and each subsequent entry.
    """
    collected = compile_matrix(matrix, sources, extra_flags, tmp)
    blocks = []
    if len(sources) == 2:
        if not collected:
            report_failures()
            sys.exit("error: no usable compiler in the matrix"
                     + _skipped_suffix())
        for label, cc_cmd, sides in collected:
            tags = mark_db_misses(cc_cmd, sources,
                                  file_tags(sources[0], sources[1]))
            blocks.append(report_across(fn_names, sides[0], sides[1], *tags,
                                        ccs=(cc_cmd, cc_cmd),
                                        labels=(label, label)))
    else:
        if len(collected) < 2:
            report_failures()
            sys.exit("error: --across needs at least two usable compilers "
                     "in the matrix" + _skipped_suffix())
        base_label, base_cc, base_sides = collected[0]
        for label, cc_cmd, sides in collected[1:]:
            blocks.append(report_across(fn_names, base_sides[0], sides[0],
                                        base_label, label,
                                        ccs=(base_cc, cc_cmd),
                                        labels=(base_label, label)))
    if JSON_OUT is None:
        blocks = [b for b in blocks if b is not None]
        print_legend(matrix)
        print()
        print(summary_table(blocks, table_width()))
        table_footer(blocks)
        if not SUMMARY_ONLY:
            # One source: every block is a "A vs B" comparison and says
            # which two rows it holds, so it keeps its header alone.
            print_listings(blocks, len(blocks) > 1 or len(sources) == 1)
    return 1 if report_failures() else 0


def _compile_filter(filter_regex):
    """Compile a --filter regex, exiting with a clean error on a bad one."""
    try:
        return re.compile(filter_regex)
    except re.error as exc:
        sys.exit(f"error: bad --filter regex: {exc}")


def run_summary(sources, matrix, extra_flags, tmp, filter_regex=None):
    """No pairs to compare: the whole-file summary of every
    compilation, one table with a file column when two files are given.

    --filter narrows the table to matching functions - the subsystem
    view of a big TU, same selection idea as ELF mode.
    """
    pattern = _compile_filter(filter_regex) if filter_regex else None
    tags = (file_tags(*sources) if len(sources) == 2
            else [Path(sources[0]).name])
    collected = compile_matrix(matrix, sources, extra_flags, tmp)
    if not collected:
        report_failures()
        sys.exit("error: no usable compiler in the matrix"
                 + _skipped_suffix())
    blocks, matched_any = [], False
    for label, cc_cmd, sections in collected:
        if pattern is not None:
            sections = [{n: v for n, v in funcs.items() if pattern.search(n)}
                        for funcs in sections]
        use_cost_profile(cc_cmd)
        matched_any = matched_any or any(sections)
        shown = (mark_db_misses(cc_cmd, sources, tags)
                 if len(sources) == 2 else tags)
        for tag, funcs in zip(shown, sections):
            if JSON_OUT is not None:
                JSON_OUT.extend(
                    json_record(name, lines, cc=cc_cmd, target=label,
                                tag=tag if len(sections) > 1 else None)
                    for name, lines in funcs.items())
                continue
            blocks.append(Block(label, funcs,
                                tag if len(sections) > 1 else None,
                                getattr(cc_cmd, "costs", None)))
    if pattern is not None and not matched_any:
        report_failures()
        sys.exit(f"error: --filter {filter_regex!r} matched no function "
                 "in any compilation")
    if JSON_OUT is None:
        print_legend(matrix)
        print()
        print(file_summary_table(blocks, table_width())
              if any(b.funcs for b in blocks) else "(no functions found)")
        table_footer(blocks)
    return 1 if report_failures() else 0


def run_pairs(source, matrix, pair_specs, extra_flags, tmp):
    """--pair mode: two different functions within one compilation.

    With no --pair and no old_X/new_X functions to auto-pair, falls
    back to the whole-file summary for this compilation.
    """
    collected = compile_matrix(matrix, [source], extra_flags, tmp)
    if not collected:
        report_failures()
        sys.exit("error: no usable compiler in the matrix"
                 + _skipped_suffix())
    blocks, unpaired = [], []
    for label, cc_cmd, (funcs,) in collected:
        use_cost_profile(cc_cmd)
        profile = getattr(cc_cmd, "costs", None)
        pairs = ([tuple(p.split(":", 1)) for p in pair_specs]
                 or auto_pairs(funcs))
        if not pairs:
            if mangled_pair_hint(funcs):
                print('note: C++ mangling defeats old_*/new_* auto-pairing; '
                      'declare the pairs extern "C" or use --pair with the '
                      'mangled names', file=sys.stderr)
            if JSON_OUT is not None:
                JSON_OUT.extend(json_record(name, lines, cc=cc_cmd,
                                            target=label)
                                for name, lines in funcs.items())
            else:
                unpaired.append(Block(label, funcs, None, profile))
            continue
        missing = sorted({n for p in pairs for n in p if n not in funcs})
        if missing:
            sys.exit("error: function(s) not in asm: "
                     + ", ".join(missing)
                     + "; functions seen: " + ", ".join(funcs))
        for old, new in pairs:
            note_growth(new, funcs[old], funcs[new], old, new)
        if JSON_OUT is not None:
            for old, new in pairs:
                JSON_OUT.append(json_record(old, funcs[old], cc=cc_cmd,
                                            role="baseline", target=label))
                JSON_OUT.append(json_record(new, funcs[new], cc=cc_cmd,
                                            role="candidate", target=label,
                                            baseline=funcs[old]))
            continue
        blocks.append(Block(label, funcs, pairs, profile))
    if JSON_OUT is None:
        print_legend(matrix)
        if blocks:
            print()
            print(summary_table(blocks, table_width()))
            table_footer(blocks)
        # A row whose functions do not pair falls back to its whole-file
        # summary; it only shares the run with paired rows when a
        # compiler dropped one side of the pair.
        if unpaired:
            print()
            print(file_summary_table(unpaired, table_width())
                  if any(b.funcs for b in unpaired)
                  else "(no functions found)")
            table_footer(unpaired)
        if blocks and not SUMMARY_ONLY:
            print_listings(blocks, len(blocks) > 1)
    return 1 if report_failures() else 0


def run_inspect(source, matrix, fn_names, layout, extra_flags, tmp,
                filter_regex=None):
    """Inspect mode: print named functions' assembly, no comparison.

    The presentation adapts to how many matrix entries compiled: one
    gives a plain listing with a stats table, exactly two give the
    --across side-by-side, more give one block per compiler.  --layout
    forces list or side-by-side instead.

    --filter adds every matching function as a full peer of the named
    ones - the way to reach compiler-generated clones ($constprop$0,
    .isra.0) whose exact names only the -S output knows.  Named
    functions must exist under every compiler; filter matches are
    lenient and appear only where that compiler emitted them, since a
    clone existing under one compiler but not another is a finding,
    not an error.
    """
    pattern = _compile_filter(filter_regex) if filter_regex else None
    usable = [(label, cc_cmd, sides[0]) for label, cc_cmd, sides
              in compile_matrix(matrix, [source], extra_flags, tmp)]
    if not usable:
        report_failures()
        sys.exit("error: no usable compiler in the matrix"
                 + _skipped_suffix())
    for _, cc_cmd, funcs in usable:
        missing = sorted({f for f in fn_names if f not in funcs})
        if missing:
            sys.exit("error: function(s) not in asm: " + ", ".join(missing)
                     + f" under {cc_cmd}; functions seen: "
                     + ", ".join(funcs))
    matched = []
    if pattern is not None:
        union = {}
        for _, _, funcs in usable:
            union.update(dict.fromkeys(funcs))
        matched = [n for n in union
                   if pattern.search(n) and n not in fn_names]
        if not matched and not fn_names:
            sys.exit(f"error: --filter {filter_regex!r} matched no function "
                     f"in {source}")

    def selected(funcs):
        return list(fn_names) + [m for m in matched if m in funcs]

    if JSON_OUT is not None:
        for label, cc_cmd, funcs in usable:
            use_cost_profile(cc_cmd)
            JSON_OUT.extend(json_record(name, funcs[name], cc=cc_cmd,
                                        target=label)
                            for name in selected(funcs))
        return 1 if report_failures() else 0
    if layout == "side-by-side" and len(usable) < 2:
        report_failures()
        sys.exit("error: --layout side-by-side needs at least two usable "
                 "compilers in the matrix")
    if layout is None:
        layout = "side-by-side" if len(usable) == 2 else "list"
    print_legend(matrix)
    if layout == "side-by-side":
        blocks, notes = [], []
        base_label, base_cc, base_funcs = usable[0]
        for label, cc_cmd, funcs in usable[1:]:
            both = list(fn_names) + [m for m in matched
                                     if m in base_funcs and m in funcs]
            blocks.append(report_across(both, base_funcs, funcs, base_label,
                                        label, ccs=(base_cc, cc_cmd),
                                        labels=(base_label, label)))
            one_sided = [m for m in matched
                         if (m in base_funcs) != (m in funcs)]
            if one_sided:
                # Which comparison, only when there are several to tell
                # apart; one comparison speaks for the whole run.
                where = (f" in {base_label} vs {label}"
                         if len(usable) > 2 else "")
                notes.append("note: --filter match(es) present under only "
                             f"one compiler, not compared{where}: "
                             + ", ".join(one_sided)
                             + " - inspect with -l list")
        print()
        print(summary_table(blocks, table_width()))
        table_footer(blocks)
        for note in notes:
            print("\n" + note)
        if not SUMMARY_ONLY:
            # Each block names the two rows it compares, so it keeps its
            # header even when it is the only one.
            print_listings(blocks, True)
        return 1 if report_failures() else 0
    blocks = [Block(label, funcs, selected(funcs),
                    getattr(cc_cmd, "costs", None))
              for label, cc_cmd, funcs in usable]
    print()
    print(inspect_table(blocks, table_width()))
    table_footer(blocks)
    if not SUMMARY_ONLY:
        for block in blocks:
            if len(blocks) > 1:
                print(f"\n== {block.label} ==")
            for name in block.sel:
                print()
                print(listing(name, block.funcs[name]))
    return 1 if report_failures() else 0


def derive_objdump(matrix):
    """Objdump belonging to the first gcc in the matrix: the toolchain
    prefix stays, the tool name swaps (xtensa-esp32s3-elf-gcc ->
    xtensa-esp32s3-elf-objdump).  None when no entry is gcc-based;
    clang has no paired cross-objdump to guess at."""
    for cc_cmd in matrix:
        cc = shlex.split(cc_cmd)[0]
        if cc == "gcc" or cc.endswith(("-gcc", "/gcc")):
            return cc[:-3] + "objdump"
    return None


def run_objdump(objdump, elf):
    """`objdump -d` an ELF, exiting with the tool's stderr on failure
    (a host objdump given a cross ELF says "file format not recognized"
    here - the cue to pass --objdump or a matching --target)."""
    try:
        proc = subprocess.run([objdump, "-d", elf],
                              capture_output=True, text=True)
    except OSError as exc:
        sys.exit(f"error: cannot run {objdump}: {exc}")
    if proc.returncode != 0:
        sys.exit(f"error: {objdump} -d {elf} failed: "
                 + (proc.stderr.strip() or f"exit {proc.returncode}"))
    return proc.stdout


def run_elf(elf, fn_names, filter_regex, objdump, list_matches=False,
            costs=None):
    """ELF mode: disassemble one linked binary and run the selected
    functions through the same analyzers as -S output.

    This answers what actually shipped: LTO can inline a function out
    of existence, and only the linked ELF shows whether its loops
    survived as zero-overhead loops inside the caller.  Named functions
    are listed in full; --filter matches are summarized in the stats
    table only, since a match can cover hundreds of functions - unless
    list_matches (-l list) asks for their listings too, the way to see
    the assembly when only the filter's spelling of a mangled name is
    known.  When filter matches go unlisted, a trailing note says so:
    a summary that looks complete but silently withholds the listings
    costs the caller a detour through raw objdump.

    ``costs`` is the --costs profile.  Nothing is compiled here, so no
    matrix row owns the binary's instructions and use_cost_profile has
    nothing to follow: the flag sets the profile for the whole run.
    """
    global COST_PROFILE
    COST_PROFILE = costs
    funcs = extract_functions_objdump(run_objdump(objdump, elf))
    missing = [f for f in fn_names if f not in funcs]
    if missing:
        hints = sorted({m for f in missing
                        for m in difflib.get_close_matches(f, funcs)})
        sys.exit(f"error: function(s) not in {elf}: " + ", ".join(missing)
                 + ("; close matches: " + ", ".join(hints) if hints else ""))
    selected = list(fn_names)
    if filter_regex:
        pattern = _compile_filter(filter_regex)
        selected += [n for n in funcs
                     if pattern.search(n) and n not in fn_names]
        if not selected:
            sys.exit(f"error: --filter {filter_regex!r} matched no function "
                     f"among the {len(funcs)} in {elf}")
    if JSON_OUT is not None:
        JSON_OUT.extend(json_record(name, funcs[name]) for name in selected)
        return 0
    listed = selected if list_matches else fn_names
    # No matrix row stands behind a linked binary: one block, labelled
    # by the ELF, so the table keeps the shape a single row has.
    blocks = [Block(elf, funcs, selected, costs)]
    print()
    print(inspect_table(blocks, table_width()))
    table_footer(blocks)
    if not SUMMARY_ONLY:
        for name in listed:
            print()
            print(listing(name, funcs[name]))
    unlisted = len(selected) - len(listed)
    if unlisted and not SUMMARY_ONLY:
        print(f"\nnote: {unlisted} --filter match(es) summarized without "
              "listings; add -l list for their assembly, or name a "
              "function after the ELF")
    return 0


# Marks a file as ours, so --install-completion can tell a script it
# wrote from one the user maintains by hand and must not clobber.
COMPLETION_MARKER = "# asmdiff completion, generated by --completion"

COMPLETION_SHELLS = ("bash", "zsh", "fish")

# Option dests whose value is a path, so the scripts offer files there.
_COMPLETION_FILE_DESTS = ("config", "objdump", "compile_commands",
                          "flags_like")

_BASH_COMPLETION = """\
@MARKER@
# Regenerate after upgrading: asmdiff --completion bash

_asmdiff_names() {
    # A --config earlier on the line decides which config the names come
    # from, so what is offered is what the run would actually resolve.
    local kind=$1 cfg="" i
    for ((i = 1; i < COMP_CWORD; i++)); do
        case ${COMP_WORDS[i]} in
            --config)
                cfg=${COMP_WORDS[i+1]}
                # With = in COMP_WORDBREAKS, --config=P splits in three.
                [[ $cfg == "=" ]] && cfg=${COMP_WORDS[i+2]}
                ;;
            --config=*) cfg=${COMP_WORDS[i]#--config=} ;;
        esac
    done
    if [[ -n $cfg ]]; then
        @PROG@ --config "$cfg" --complete "$kind" 2>/dev/null
    else
        @PROG@ --complete "$kind" 2>/dev/null
    fi
}

_asmdiff() {
    local cur prev head kind names
    cur=${COMP_WORDS[COMP_CWORD]}
    prev=${COMP_WORDS[COMP_CWORD-1]}

    case $prev in
        -t|--target|--costs)
            kind=targets
            [[ $prev == --costs ]] && kind=costs
            # Only the last element of a comma list is completed, the
            # earlier ones are kept: -t esp32,c3<TAB> offers for c3.
            head=""
            if [[ $cur == *,* ]]; then
                head=${cur%,*},
                cur=${cur##*,}
            fi
            names=$(_asmdiff_names "$kind")
            COMPREPLY=( $(compgen -P "$head" -W "$names" -- "$cur") )
            return
            ;;
        -l|--layout)
            COMPREPLY=( $(compgen -W "@LAYOUTS@" -- "$cur") )
            return
            ;;
        --config|--objdump|-db|--compile-commands|--flags-like)
            COMPREPLY=( $(compgen -f -- "$cur") )
            return
            ;;
    esac

    if [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W "@OPTS@" -- "$cur") )
    else
        COMPREPLY=( $(compgen -f -- "$cur") )
    fi
}

complete -o default -F _asmdiff @PROG@
"""

_ZSH_COMPLETION = """\
#compdef @PROG@
@MARKER@
# compinit reads #compdef off the first line, so the marker follows it.
# Regenerate after upgrading: asmdiff --completion zsh

_asmdiff_names() {
    local kind=$1 cfg="" i
    for ((i = 1; i < $#words; i++)); do
        case ${words[i]} in
            --config) cfg=${words[i+1]} ;;
            --config=*) cfg=${words[i]#--config=} ;;
        esac
    done
    if [[ -n $cfg ]]; then
        @PROG@ --config "$cfg" --complete "$kind" 2>/dev/null
    else
        @PROG@ --complete "$kind" 2>/dev/null
    fi
}

_asmdiff_targets() {
    local -a names
    names=( ${(f)"$(_asmdiff_names targets)"} )
    # -s , keeps completing after each element of a comma list.
    _values -s , target $names
}

_asmdiff_costs() {
    local -a names
    names=( ${(f)"$(_asmdiff_names costs)"} )
    _describe -t costs 'cost profile' names
}

_asmdiff() {
    _arguments -s \\
@ARGSPEC@        '*:file:_files'
}

_asmdiff "$@"
"""

_FISH_COMPLETION = """\
@MARKER@
# Regenerate after upgrading: asmdiff --completion fish
# Positionals (a source file, an ELF, function names) are left to
# fish's own file completion.

@RULES@"""


def completion_prog(argv0=None):
    """The command name a generated script completes.

    'asmdiff' for the installed console script, the invoked file name
    when the single module is run directly (asmdiff.py).  Anything else
    - a test runner, an interpreter - falls back to the published name
    rather than emitting a script for a command nobody types.
    """
    name = Path(argv0 if argv0 is not None else (sys.argv[0] or "")).name
    if name == "asmdiff" or name.startswith("asmdiff."):
        return name
    return "asmdiff"


def completion_options(parser):
    """Every option string the parser advertises, in declaration order.

    Read off the live parser at emission time, so a script regenerated
    after an upgrade cannot advertise a flag the tool has dropped or
    miss one it gained.  Suppressed options (the --complete helper)
    stay out of the candidate list.
    """
    opts = []
    for action in parser._actions:
        if action.help == argparse.SUPPRESS:
            continue
        for opt in action.option_strings:
            if opt not in opts:
                opts.append(opt)
    return opts


def _action_choices(parser, dest):
    """One option's fixed choices, for scripts that inline them instead
    of calling back into the tool."""
    for action in parser._actions:
        if action.dest == dest and action.choices:
            return [str(choice) for choice in action.choices]
    return []


def _takes_value(action):
    """True when the flag is followed by a value; argparse gives the
    store_true/version/help kinds nargs=0."""
    return action.nargs != 0


def _completion_kind(action):
    """What a flag's value completes to: config names, fixed choices, a
    path, or nothing the shell can guess (a regex, a compiler string)."""
    if action.dest == "target":
        return "targets"
    if action.dest == "costs":
        return "costs"
    if action.choices:
        return "choices"
    if action.dest == "install_completion":
        return "shells"   # nargs="?" rules out argparse choices
    if action.dest in _COMPLETION_FILE_DESTS:
        return "file"
    return "none"


def _completion_desc(text, limit=60):
    """One line of plain ASCII, short enough for a completion menu."""
    text = " ".join((text or "").split())
    # Long dashes read fine in help text but not in a completion menu.
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = "".join(ch if ch.isascii() else " " for ch in text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text


def _zsh_desc(text, limit=60):
    """A description safe inside a single-quoted _arguments spec: the
    quote ends the spec, brackets and colons delimit its fields."""
    text = _completion_desc(text, limit)
    text = text.replace("'", "").replace("[", "(").replace("]", ")")
    return text.replace(":", " -")


def _fish_desc(text, limit=60):
    """A description safe inside a double-quoted fish -d argument."""
    text = _completion_desc(text, limit)
    for char in ("\\", '"', "$"):
        text = text.replace(char, "")
    return text


def _completion_tag(action):
    """A one-word name for the value a flag takes, safe in a zsh spec
    field: the metavar reduced to word characters ('CC FLAGS' ->
    cc-flags, OLD:NEW -> old-new)."""
    raw = str(action.metavar or action.dest)
    tag = "".join(ch if (ch.isalnum() or ch == "_") else "-" for ch in raw)
    return "-".join(part for part in tag.lower().split("-") if part) or "value"


def _zsh_argspec(parser):
    """The _arguments spec lines, one per option, from the live parser."""
    lines = []
    for action in parser._actions:
        if action.help == argparse.SUPPRESS or not action.option_strings:
            continue
        body = "[" + _zsh_desc(action.help) + "]"
        if _takes_value(action):
            kind = _completion_kind(action)
            value_action = {"targets": "_asmdiff_targets",
                            "costs": "_asmdiff_costs",
                            "file": "_files",
                            "choices": "(%s)" % " ".join(
                                str(c) for c in action.choices or ()),
                            "shells": "(%s)" % " ".join(COMPLETION_SHELLS),
                            "none": ""}[kind]
            # A double colon marks an argument argparse made optional.
            sep = "::" if action.nargs == "?" else ":"
            body += sep + _completion_tag(action) + ":" + value_action
        # Repeatable flags need * or zsh stops offering them.
        repeat = "*" if isinstance(action, argparse._AppendAction) else ""
        if len(action.option_strings) > 1:
            prefix = "'" + repeat + "'" if repeat else ""
            spec = (prefix + "{" + ",".join(action.option_strings) + "}'"
                    + body + "'")
        else:
            spec = "'" + repeat + action.option_strings[0] + body + "'"
        lines.append("        " + spec + " \\")
    return "\n".join(lines) + "\n"


def _fish_rules(parser, prog):
    """One `complete -c PROG` line per option, from the live parser."""
    lines = []
    for action in parser._actions:
        if action.help == argparse.SUPPRESS or not action.option_strings:
            continue
        parts = ["complete", "-c", prog]
        for opt in action.option_strings:
            if opt.startswith("--"):
                parts += ["-l", opt[2:]]
            elif len(opt) == 2:
                parts += ["-s", opt[1]]
            else:
                parts += ["-o", opt[1:]]   # single-dash multi-char, -db
        if _takes_value(action):
            kind = _completion_kind(action)
            # fish has no optional-argument form; -r on a flag argparse
            # lets stand alone would make fish demand a value.
            if action.nargs != "?":
                parts.append("-r")
            if kind == "file":
                parts.append("-F")
            elif kind in ("targets", "costs"):
                parts += ["-f", "-a",
                          '"(%s --complete %s)"' % (prog, kind)]
            elif kind in ("choices", "shells"):
                values = (COMPLETION_SHELLS if kind == "shells"
                          else action.choices or ())
                parts += ["-f", "-a",
                          '"%s"' % " ".join(str(v) for v in values)]
            else:
                parts.append("-f")
        desc = _fish_desc(action.help)
        if desc:
            parts += ["-d", '"%s"' % desc]
        lines.append(" ".join(parts))
    return "\n".join(lines) + "\n"


def completion_script(shell, parser, prog=None):
    """Render the completion script for one shell."""
    prog = completion_prog() if prog is None else prog
    if shell == "bash":
        return (_BASH_COMPLETION
                .replace("@OPTS@", " ".join(completion_options(parser)))
                .replace("@LAYOUTS@",
                         " ".join(_action_choices(parser, "layout")))
                .replace("@MARKER@", COMPLETION_MARKER)
                .replace("@PROG@", prog))
    if shell == "zsh":
        return (_ZSH_COMPLETION
                .replace("@ARGSPEC@", _zsh_argspec(parser))
                .replace("@MARKER@", COMPLETION_MARKER)
                .replace("@PROG@", prog))
    if shell == "fish":
        return (_FISH_COMPLETION
                .replace("@RULES@", _fish_rules(parser, prog))
                .replace("@MARKER@", COMPLETION_MARKER)
                .replace("@PROG@", prog))
    sys.exit("error: --completion supports " + ", ".join(COMPLETION_SHELLS))


def completion_names(kind, explicit_config=None):
    """Config names for the hidden --complete helper.

    The helper runs on every tab press and its stdout goes straight
    into the shell's candidate list, so every failure - no config, an
    unreadable one, a Python without tomllib, a kind this version does
    not know - yields an empty list instead of a message that would
    land in the middle of the user's prompt.
    """
    try:
        path = find_config(explicit_config, None)
        config = load_config(path) if path else None
    except (SystemExit, OSError, ValueError):
        return []
    if not config:
        return []
    if kind == "costs":
        return config_cost_names(config)
    if kind != "targets":
        return []
    names = config_target_names(config)
    groups = config.get("groups")
    if isinstance(groups, dict):
        names += [name for name in groups if name not in names]
    return names


def default_shell(env=None):
    """The shell $SHELL names, by basename; '' when it is unset."""
    env = os.environ if env is None else env
    return Path(env.get("SHELL") or "").name


def completion_path(shell, env=None, home=None):
    """Where a shell autoloads user completions from.

    bash-completion 2.x loads completions/<command> on first use; fish
    and zsh read their directories at startup.  None of the three needs
    an rc edit, except the fpath line zsh wants for ~/.zfunc.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if shell == "bash":
        base = env.get("BASH_COMPLETION_USER_DIR")
        if not base:
            data = env.get("XDG_DATA_HOME") or str(home / ".local" / "share")
            base = os.path.join(data, "bash-completion")
        return Path(base) / "completions" / "asmdiff"
    if shell == "fish":
        return home / ".config" / "fish" / "completions" / "asmdiff.fish"
    return home / ".zfunc" / "_asmdiff"


def install_completion(shell, parser, env=None, home=None):
    """Write the script to the shell's user completion directory.

    rc files belong to the user, so nothing here appends to one.  An
    existing file is replaced only when it carries COMPLETION_MARKER:
    a hand-written completion for the same command is named, not
    overwritten.  The installed script always completes `asmdiff`, the
    name the console script is installed under.
    """
    env = os.environ if env is None else env
    if not shell:
        shell = default_shell(env)
    if shell not in COMPLETION_SHELLS:
        named = f"$SHELL names {shell}" if shell else "$SHELL is unset"
        sys.exit(f"error: --install-completion supports "
                 + ", ".join(COMPLETION_SHELLS)
                 + f" ({named}); name one explicitly")
    path = completion_path(shell, env, home)
    if path.exists():
        try:
            head = path.read_text(errors="replace")
        except OSError:
            head = ""
        if COMPLETION_MARKER not in head.split("\n")[:2]:
            sys.exit(f"error: {path} was not written by asmdiff; move it "
                     "aside, or install the script yourself with "
                     f"asmdiff --completion {shell}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(completion_script(shell, parser, prog="asmdiff"))
    print(f"wrote {path}")
    if shell == "zsh":
        print("add these two lines to ~/.zshrc (zsh reads ~/.zfunc only "
              "once it is on fpath):")
        print("  fpath=(~/.zfunc $fpath)")
        print("  autoload -Uz compinit && compinit")
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
               "invocation, e.g.: asmdiff.py h.c -t host -- -fno-math-errno")
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
                             "matrix entries, or two files")
    parser.add_argument("-l", "--layout", choices=["list", "side-by-side"],
                        help="force the inspect presentation instead of "
                             "adapting to the matrix (1 usable compiler "
                             "lists, 2 go side by side, more list); with "
                             "ELF input, 'list' also prints --filter "
                             "matches' listings")
    parser.add_argument("-f", "--filter", metavar="REGEX",
                        help="also analyze every function whose name "
                             "matches REGEX (re.search) - sweep a "
                             "subsystem, or reach compiler-generated "
                             "clones, without naming each function. In "
                             "ELF input matches are summarized only (see "
                             "-l list); in compile modes they are full "
                             "peers of named functions. Not with "
                             "--pair/--across")
    parser.add_argument("--objdump", metavar="PATH",
                        help="disassembler for ELF input; default: derived "
                             "from the first gcc in the matrix by swapping "
                             "the trailing gcc for objdump")
    parser.add_argument("-t", "--target", action="append", default=[],
                        metavar="NAME",
                        help="named [table], [groups] name, comma-list, or "
                             "glob from the config (repeatable; appended "
                             "to the matrix after --cc entries)")
    parser.add_argument("--cc", action="append", default=[],
                        metavar="'CC FLAGS'",
                        help="compiler and flags as one string, one matrix "
                             "row (repeatable); default: config default "
                             "target, else gcc and clang at " + FALLBACK_FLAGS)
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
    parser.add_argument("--db-includes", action="store_true",
                        help="borrow only the header-search paths from the "
                             "database (re-emitted as -idirafter, so they "
                             "never shadow the host's system headers), "
                             "dropping its defines, forced includes, and "
                             "-specs/--sysroot - lets a host/foreign-arch "
                             "target resolve a cross project's headers "
                             "without inheriting cross-only flags")
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
    parser.add_argument("--list-targets", action="store_true",
                        help="print the resolved config's default, groups, "
                             "and targets, then exit (no source file needed)")
    parser.add_argument("--span-stats", action="store_true",
                        help="follow the stats table with a per-loop-span "
                             "instruction mix (nesting depth and load/"
                             "store/mul/div/branch/other counts) - weighs "
                             "the span instead of the whole function")
    parser.add_argument("--cost", action="store_true",
                        help="add a cost column to the stats tables: the "
                             "instruction classes of each function and "
                             "what its calls are (softfp, softfp-div, "
                             "int-div, libm, mem, call), with the call "
                             "sites a loop span holds counted apart.  "
                             "Counts only, until a measured cost profile "
                             "prices them")
    parser.add_argument("--costs", metavar="NAME",
                        help="price the cost column with the config's "
                             "[costs.NAME] profile on every matrix row, "
                             "--cc rows included (implies --cost).  A "
                             "target may name its own with "
                             'costs = "NAME"; this overrides it')
    parser.add_argument("--fail-on-growth", action="store_true",
                        help="exit 3 if any candidate has more "
                             "instructions than its baseline, naming each "
                             "one on stderr - the CI check for a rewrite "
                             "that was meant to shrink.  Needs paired "
                             "functions (--pair, auto-paired old_X/new_X, "
                             "or --across); status 1 stays a tool error, "
                             "2 a usage error")
    parser.add_argument("-C", "--collapse", action="store_true",
                        help="in side-by-side listings, elide runs of "
                             "identical line pairs, keeping "
                             f"{COLLAPSE_CONTEXT} lines of context around "
                             "each difference - near-identical functions "
                             "render as a few hunks")
    parser.add_argument("--width", type=width_arg, metavar="N",
                        help="column budget for tables and side-by-side "
                             "listings (default: the terminal, else "
                             f"$COLUMNS, else {DEFAULT_WIDTH}).  0 is "
                             "unlimited, which is what a script parsing "
                             "the callee column wants")
    parser.add_argument("--json", action="store_true",
                        help="emit the summary as JSON on stdout instead "
                             "of tables: one record per function per "
                             "compiler (insns, loop spans, calls; span "
                             "mix with --span-stats).  Implies "
                             "--summary-only; errors stay plain text on "
                             "stderr")
    parser.add_argument("-s", "--summary-only", action="store_true",
                        help="print only the summary/stats tables, "
                             "suppressing every assembly listing - the "
                             "scripted-caller view (pull a listing with a "
                             "second run when a delta needs explaining)")
    parser.add_argument("--completion", choices=list(COMPLETION_SHELLS),
                        metavar="SHELL",
                        help="print a completion script for bash, zsh, or "
                             "fish on stdout and exit; its flag list is "
                             "read off this parser, so a regenerated "
                             "script cannot drift from the tool")
    parser.add_argument("--install-completion", nargs="?", const="",
                        default=None, metavar="SHELL",
                        help="write that script to the shell's own user "
                             "completion directory and exit - no rc file "
                             "is touched; SHELL defaults to the basename "
                             "of $SHELL, and an existing file is replaced "
                             "only if asmdiff wrote it")
    parser.add_argument("--complete", metavar="KIND",
                        help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version",
                        version=f"asmdiff {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="on compile failure, print the full compiler "
                             "command and complete error output instead of "
                             "the first lines")
    args = parser.parse_args(argv)
    global VERBOSE, FLAGS_LIKE, SUMMARY_ONLY, COLLAPSE, SPAN_STATS
    global DB_INCLUDES, JSON_OUT, FAIL_ON_GROWTH, COST, COST_PROFILE
    global WIDTH
    VERBOSE = args.verbose
    WIDTH = args.width
    FLAGS_LIKE = args.flags_like
    SUMMARY_ONLY = args.summary_only or args.json
    COLLAPSE = args.collapse
    SPAN_STATS = args.span_stats
    DB_INCLUDES = args.db_includes
    JSON_OUT = [] if args.json else None
    COST = args.cost or args.costs is not None
    COST_PROFILE = None                 # set per matrix row as it runs
    FAIL_ON_GROWTH = args.fail_on_growth
    del GROWTH[:]                       # main() may run twice in a process
    del _FAILURES[:]
    _GLOB_CHOICES.clear()

    if args.example_config:
        sys.stdout.write(EXAMPLE_CONFIG)
        return 0
    if args.edit_config:
        return edit_config(args.config)
    if args.list_targets:
        config_path = find_config(args.config, args.sources)
        config = load_config(config_path) if config_path else None
        return list_targets(config, config_path)
    if args.complete:
        # Whatever this prints becomes the shell's candidate list, so it
        # prints names or nothing; see completion_names.
        for name in completion_names(args.complete, args.config):
            print(name)
        return 0
    if args.completion:
        sys.stdout.write(completion_script(args.completion, parser))
        return 0
    if args.install_completion is not None:
        return install_completion(args.install_completion, parser)
    if not args.sources:
        parser.error("SOURCE.c required")

    sources, fn_names = split_positionals(args.sources)
    # Growth is a property of a pair, so the modes that never pair
    # (inspect, whole-file summary, ELF) reject the flag rather than
    # exiting 0 on a check that never ran.
    if args.fail_on_growth and (is_elf(sources[0]) or fn_names
                                or (args.filter and len(sources) == 1)
                                or (len(sources) == 2 and not args.across)):
        parser.error("--fail-on-growth needs paired functions: --pair, "
                     "auto-pairs, or --across")
    if is_elf(sources[0]):
        if len(sources) > 1:
            parser.error("ELF input analyzes one binary; a second file "
                         "cannot be combined with it")
        if args.pair or args.across:
            parser.error("--pair/--across compare compilations; "
                         "an ELF is disassembled, not compiled")
        if args.layout == "side-by-side":
            parser.error("-l side-by-side compares compilations; with ELF "
                         "input use -l list to also print --filter "
                         "matches' listings")
        if not fn_names and not args.filter:
            parser.error("a whole ELF has too many functions to table; "
                         "name functions after the file or select them "
                         "with --filter REGEX")
        config_path = find_config(args.config, sources)
        config = load_config(config_path) if config_path else None
        objdump = args.objdump
        if objdump is None:
            matrix = build_matrix(args.cc, args.target, config, config_path,
                                  want_db=False)
            objdump = derive_objdump(matrix)
            if objdump is None:
                sys.exit("error: no gcc in the matrix to derive an objdump "
                         "from (" + "; ".join(matrix) + "); pass --objdump "
                         "PATH or a gcc-based --cc/--target")
        # A target's costs = "NAME" stays unread: ELF mode compiles
        # nothing, so no target stands behind the binary's code.
        profile = (load_costs(config, args.costs, config_path)
                   if args.costs else None)
        status = run_elf(sources[0], fn_names, args.filter, objdump,
                         list_matches=args.layout == "list", costs=profile)
        if JSON_OUT is not None:
            print(json.dumps({"asmdiff": __version__, "mode": "elf",
                              "elf": sources[0], "results": JSON_OUT},
                             indent=2))
        return status
    if args.objdump:
        parser.error("--objdump applies to ELF input only")
    if args.filter and (args.pair or args.across):
        parser.error("--filter selects functions to inspect or summarize; "
                     "--pair/--across name their functions explicitly")
    if len(sources) > 2:
        parser.error("at most two source files may be given")
    if fn_names and len(sources) == 2:
        parser.error("bare function names inspect within one file; "
                     "use --across FUNC for two files")
    if fn_names and (args.pair or args.across):
        parser.error("bare function names (inspect) cannot be combined "
                     "with --pair or --across")
    if args.layout and not fn_names and not args.filter:
        parser.error("--layout only applies when inspecting functions "
                     "(SOURCE.c FUNC ... or --filter REGEX)")
    if args.across and args.pair:
        parser.error("--across and --pair are mutually exclusive")
    if len(sources) == 2 and args.pair:
        parser.error("--pair compares within one file; "
                     "use --across FUNC for two files")
    config_path = find_config(args.config, sources)
    config = load_config(config_path) if config_path else None
    matrix = build_matrix(args.cc, args.target, config, config_path,
                          args.compile_commands, costs_arg=args.costs)
    if args.across and len(sources) == 1 and len(matrix) < 2:
        parser.error("--across on one file needs at least two --cc entries")
    for spec in args.pair:
        if ":" not in spec:
            parser.error(f"--pair expects OLD:NEW, got {spec!r}")

    with tempfile.TemporaryDirectory(prefix="asmdiff") as tmp:
        if fn_names or (args.filter and len(sources) == 1):
            mode = "inspect"
            status = run_inspect(sources[0], matrix, fn_names, args.layout,
                                 extra_flags, tmp,
                                 filter_regex=args.filter)
        elif args.across:
            mode = "across"
            status = run_across(sources, matrix, args.across,
                                extra_flags, tmp)
        elif len(sources) == 2:
            mode = "summary"
            status = run_summary(sources, matrix, extra_flags, tmp,
                                 filter_regex=args.filter)
        else:
            mode = "pairs"
            status = run_pairs(sources[0], matrix, args.pair,
                               extra_flags, tmp)
    if JSON_OUT is not None:
        print(json.dumps({"asmdiff": __version__, "mode": mode,
                          "results": JSON_OUT}, indent=2))
    if GROWTH:
        for func, grew, base_label, cand_label in GROWTH:
            print(f"growth: {func} +{grew} insns "
                  f"({base_label} -> {cand_label})", file=sys.stderr)
        return 3
    return status


if __name__ == "__main__":
    sys.exit(main())
