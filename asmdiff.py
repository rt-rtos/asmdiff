#!/usr/bin/env python3
"""Compare per-function assembly between paired C implementations.

Compiles a harness C file across a matrix of compilers, extracts each
variant function's assembly from the -S output, and prints side-by-side
listings plus a summary of instruction counts, outbound calls, and loop
spans (instructions between a local label and its last backward branch).
Automates fold-vs-libcall analysis when evaluating micro-optimisations
(e.g. "does this still compile to one instruction, or is it a libcall?").

Usage:
    tools/asmdiff/asmdiff.py SOURCE.c [SOURCE2.c] [--pair OLD:NEW]...
                             [--across FUNC]... [--cc 'CC FLAGS']...
                             [--target NAME]... [--config PATH]
                             [--compile-commands [PATH]]
                             [-- EXTRA_FLAGS...]

Three modes:
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

A config target may name a compile_commands.json (compile_commands = PATH):
the include/define flags recorded there for the source being compiled are
added to that target's command, so a real project source resolves its
headers the way its own build system does (e.g. an ESP-IDF component).
compile_commands = true in a target — or a bare --compile-commands on the
command line — instead searches ./, ./build, ../, ../build for the
database; a source absent from a database found that way is compiled
without borrowed flags (with a note) rather than being an error.
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
_DB_CACHE = {}
_MISS_NOTED = set()


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
    """Locate a compile_commands.json near the current directory.

    Searched in order: ./, ./build, ../, ../build — first hit wins.  This
    covers running asmdiff from a project root or from one directory below
    it (a component dir), with the database where CMake/idf.py leaves it.
    Returns None when nothing is found.
    """
    cwd = Path.cwd()
    for d in (cwd, cwd / "build", cwd.parent, cwd.parent / "build"):
        candidate = d / "compile_commands.json"
        if candidate.is_file():
            return str(candidate)
    return None


def _discovered_db(who):
    """find_compile_commands() for a caller that opted in (compile_commands
    = true, or a bare --compile-commands): finding nothing is then an error,
    not a silent no-op."""
    found = find_compile_commands()
    if found is None:
        sys.exit(f"error: {who}: no compile_commands.json found in "
                 "./, ./build, ../, or ../build")
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
    """
    entries = load_compile_commands(db_path)
    want = Path(source).resolve()
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
            return include_flags(tokens, directory)
        if Path(f).name == want.name:
            name_seen = True
    if missing_ok:
        key = (db_path, str(want))
        if key not in _MISS_NOTED:
            _MISS_NOTED.add(key)
            print(f"note: {source} not in {db_path}; "
                  "compiling without its include/define flags",
                  file=sys.stderr)
        return []
    hint = (f"; an entry named {want.name} exists under a different path — "
            "pass the source as it appears in the database") if name_seen else ""
    sys.exit(f"error: {source} not found in {db_path}{hint}")

# A label at column 0 that is not a local (.L*) label starts a function.
FUNC_LABEL = re.compile(r"^([A-Za-z_][\w$.]*):")
# Assembler directives that carry no information worth reading.
NOISE = re.compile(
    r"^\s*\.(cfi_|p2align|align\b|loc\b|file\b|text\b|globl\b|global\b|"
    r"type\b|section\b|ident\b|weak\b|hidden\b|addrsig|build_version)"
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
    label.  Comment lines, CFI/section/alignment directives, compiler
    bracketing labels, and inline data (switch jump tables, constants) are
    dropped; instructions and meaningful local labels (loop targets) are
    kept, whitespace-stripped.
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
                     "./, ./build, ../, ../build")
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


def render_table(rows):
    """Column-aligned text for a list of equal-length string tuples."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip()
        for row in rows)


def format_spans(spans):
    return " ".join(f"{label}:{n}" for label, n in spans) or "-"


def summary_table(pairs, funcs):
    """Instruction counts, outbound calls, and loop spans per pair member."""
    rows = [("function", "role", "insns", "calls", "loop spans")]
    for old, new in pairs:
        for name, role in ((old, "baseline"), (new, "candidate")):
            insns, calls = analyze(funcs[name])
            rows.append((name, role, str(insns),
                         ", ".join(calls) or "-",
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
        rows.append((name, str(insns), ", ".join(calls) or "-",
                     format_spans(loop_spans(lines))))
    rows.append((f"TOTAL ({len(funcs)} functions)", str(total_insns),
                 ", ".join(all_calls) or "-", "-"))
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
        print(f"\n== {cc_cmd} ==")
        for tag, funcs in zip(tags, sections):
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
                             "default: config default target, else gcc and "
                             "clang at " + FALLBACK_FLAGS)
    parser.add_argument("--target", action="append", default=[],
                        metavar="NAME",
                        help="named [table] from the config file, resolved "
                             "to a --cc entry (repeatable; appended to the "
                             "matrix after --cc entries)")
    parser.add_argument("--compile-commands", nargs="?", const=True,
                        default=None, metavar="PATH",
                        help="borrow each source's include/define flags from "
                             "a compile_commands.json; with no PATH, search "
                             "./, ./build, ../, ../build.  A target whose "
                             "config names its own compile_commands keeps it")
    parser.add_argument("--config", metavar="PATH",
                        help=f"config file; default search: {CONFIG_NAME} "
                             "next to SOURCE.c, in the current directory, "
                             "then in ~/.config/")
    args = parser.parse_args(argv)

    if len(args.sources) > 2:
        parser.error("at most two source files may be given")
    if args.across and args.pair:
        parser.error("--across and --pair are mutually exclusive")
    if len(args.sources) == 2 and args.pair:
        parser.error("--pair compares within one file; "
                     "use --across FUNC for two files")
    config_path = find_config(args.config, args.sources)
    config = load_config(config_path) if config_path else None
    matrix = build_matrix(args.cc, args.target, config, config_path,
                          args.compile_commands)
    if args.across and len(args.sources) == 1 and len(matrix) < 2:
        parser.error("--across on one file needs at least two --cc entries")
    for spec in args.pair:
        if ":" not in spec:
            parser.error(f"--pair expects OLD:NEW, got {spec!r}")

    with tempfile.TemporaryDirectory(prefix="asmdiff") as tmp:
        if args.across:
            return run_across(args.sources, matrix, args.across,
                              extra_flags, tmp)
        if len(args.sources) == 2:
            return run_summary(args.sources, matrix, extra_flags, tmp)
        return run_pairs(args.sources[0], matrix, args.pair,
                         extra_flags, tmp)


if __name__ == "__main__":
    sys.exit(main())
