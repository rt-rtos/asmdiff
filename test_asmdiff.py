#!/usr/bin/env python3
"""Unit tests for asmdiff.py.  Run: python3 tools/asmdiff/test_asmdiff.py -v"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import asmdiff

# Trimmed but structurally faithful `gcc -O3 -S` x86-64 output.
GCC_ASM = """\
\t.file\t"cmp.c"
\t.text
\t.p2align 4
\t.globl\told_const
\t.type\told_const, @function
old_const:
.LFB0:
\t.cfi_startproc
\tmulss\t.LC0(%rip), %xmm0
\tret
\t.cfi_endproc
.LFE0:
\t.size\told_const, .-old_const
\t.p2align 4
\t.globl\tnew_const
\t.type\tnew_const, @function
new_const:
.LFB1:
\t.cfi_startproc
\tmovl\t$-5, %edi
\tjmp\tldexpf@PLT
\t.cfi_endproc
.LFE1:
\t.size\tnew_const, .-new_const
\t.section\t.rodata.cst4,"aM",@progbits,4
.LC0:
\t.long\t1023410176
\t.ident\t"GCC: (GNU) 13.2.0"
"""

# Trimmed but structurally faithful `clang -O3 -S` x86-64 output.
CLANG_ASM = """\
\t.text
\t.file\t"cmp.c"
\t.globl\tnew_const
\t.p2align\t4, 0x90
\t.type\tnew_const,@function
new_const:
\t.cfi_startproc
# %bb.0:
\tmovl\t$-5, %edi
\tjmp\tldexpf@PLT
.Lfunc_end0:
\t.size\tnew_const, .Lfunc_end0-new_const
\t.cfi_endproc
"""

# A function containing a kept local label (loop target).
LOOP_ASM = """\
\t.globl\tlooper
\t.type\tlooper, @function
looper:
\t.cfi_startproc
\txorl\t%eax, %eax
.L2:
\taddl\t$1, %eax
\tcmpl\t$8, %eax
\tjne\t.L2
\tret
\t.cfi_endproc
\t.size\tlooper, .-looper
"""

# A switch lowered to a jump table, faithful to `gcc -O2 -S`.  The table is
# emitted *inside* the function body (between the label and `.size`) via a
# .rodata/.text toggle, with self-relative entries `.long .Lx-.L4` — data,
# not instructions, and their .L4 operand must not read as a backward branch.
SWITCH_ASM = """\
\t.globl\tsel
\t.type\tsel, @function
sel:
.LFB0:
\t.cfi_startproc
\tendbr64
\tcmpl\t$4, %edi
\tja\t.L9
\tleaq\t.L4(%rip), %rcx
\tmovl\t%edi, %edi
\tmovslq\t(%rcx,%rdi,4), %rax
\taddq\t%rcx, %rax
\tnotrack jmp\t*%rax
\t.section\t.rodata
\t.align 4
.L4:
\t.long\t.L8-.L4
\t.long\t.L7-.L4
\t.long\t.L6-.L4
\t.long\t.L5-.L4
\t.long\t.L3-.L4
\t.text
\t.p2align 4,,10
.L5:
\tmovl\t%esi, %eax
\txorl\t%edx, %eax
\tret
.L3:
\tmovl\t%esi, %eax
\torl\t%edx, %eax
\tret
.L8:
\tleal\t(%rsi,%rdx), %eax
\tret
.L7:
\tmovl\t%esi, %eax
\tsubl\t%edx, %eax
\tret
.L6:
\tmovl\t%esi, %eax
\timull\t%edx, %eax
\tret
\t.cfi_endproc
\t.size\tsel, .-sel
"""

# Jump tables on other targets use plain (non-self-relative) label entries,
# plus stray inline constants; all are data directives, none are branches.
DATA_DIRECTIVES_ASM = """\
\t.type\ttbl, @function
tbl:
\t.cfi_startproc
\tjx\ta8
.Ltab:
\t.word\t.La
\t.word\t.Lb
\t.byte\t3
\t.quad\t0
.La:
\tadd.n\ta2, a2, a2
\tretw.n
.Lb:
\tretw.n
\t.size\ttbl, .-tbl
"""


class TestExtractFunctions(unittest.TestCase):
    def test_gcc_functions_found(self):
        funcs = asmdiff.extract_functions(GCC_ASM)
        self.assertEqual(sorted(funcs), ["new_const", "old_const"])

    def test_gcc_bodies_cleaned(self):
        funcs = asmdiff.extract_functions(GCC_ASM)
        self.assertEqual(funcs["old_const"],
                         ["mulss\t.LC0(%rip), %xmm0", "ret"])
        self.assertEqual(funcs["new_const"],
                         ["movl\t$-5, %edi", "jmp\tldexpf@PLT"])

    def test_rodata_not_captured(self):
        funcs = asmdiff.extract_functions(GCC_ASM)
        for body in funcs.values():
            self.assertNotIn("\t.long\t1023410176", body)
            self.assertFalse(any(".long" in line for line in body))

    def test_clang_output(self):
        funcs = asmdiff.extract_functions(CLANG_ASM)
        self.assertEqual(funcs["new_const"],
                         ["movl\t$-5, %edi", "jmp\tldexpf@PLT"])

    def test_local_loop_label_kept(self):
        funcs = asmdiff.extract_functions(LOOP_ASM)
        self.assertIn(".L2:", funcs["looper"])


class TestAnalyze(unittest.TestCase):
    def test_fold_case_no_calls(self):
        insns, calls = asmdiff.analyze(["mulss\t.LC0(%rip), %xmm0", "ret"])
        self.assertEqual((insns, calls), (2, []))

    def test_tail_call_detected_plt_stripped(self):
        insns, calls = asmdiff.analyze(["movl\t$-5, %edi", "jmp\tldexpf@PLT"])
        self.assertEqual((insns, calls), (2, ["ldexpf"]))

    def test_plain_call_detected(self):
        _, calls = asmdiff.analyze(["call\texp2f@PLT", "mulss\t%xmm1, %xmm0"])
        self.assertEqual(calls, ["exp2f"])

    def test_local_jumps_and_labels_not_calls(self):
        insns, calls = asmdiff.analyze(
            [".L2:", "addl\t$1, %eax", "jne\t.L2", "jmp\t.L4",
             "jmp\t*%rax", "ret"])
        self.assertEqual(calls, [])
        self.assertEqual(insns, 5)  # .L2: is a label, not an instruction

    def test_arm_riscv_xtensa_mnemonics(self):
        self.assertEqual(asmdiff.analyze(["bl\tldexpf"])[1], ["ldexpf"])
        self.assertEqual(asmdiff.analyze(["blt\ta0, a1, .L2"])[1], [])
        self.assertEqual(asmdiff.analyze(["tail\tldexpf@plt"])[1], ["ldexpf"])
        self.assertEqual(asmdiff.analyze(["jal\tra, exp2f"])[1], [])  # reg first: not a symbol
        self.assertEqual(asmdiff.analyze(["call8\texp2f"])[1], ["exp2f"])
        self.assertEqual(asmdiff.analyze(["callx8\ta10"])[1], ["a10"])
        self.assertEqual(asmdiff.analyze(["j\t.L4"])[1], [])

    def test_duplicate_calls_reported_once(self):
        _, calls = asmdiff.analyze(["call\tf", "call\tf", "call\tg"])
        self.assertEqual(calls, ["f", "g"])


class TestLoopSpans(unittest.TestCase):
    def test_simple_backward_branch(self):
        lines = ["xorl\t%eax, %eax", ".L2:", "addl\t$1, %eax",
                 "cmpl\t$8, %eax", "jne\t.L2", "ret"]
        self.assertEqual(asmdiff.loop_spans(lines), [(".L2", 3)])

    def test_forward_branch_is_not_a_span(self):
        lines = ["testl\t%edi, %edi", "jle\t.L4", "addl\t$1, %eax",
                 ".L4:", "ret"]
        self.assertEqual(asmdiff.loop_spans(lines), [])

    def test_several_backedges_to_one_label_merge(self):
        lines = [".L3:", "addl\t$1, %eax", "je\t.L3",
                 "subl\t$1, %ebx", "jne\t.L3", "ret"]
        self.assertEqual(asmdiff.loop_spans(lines), [(".L3", 4)])

    def test_nested_spans_reported_separately(self):
        lines = [".L1:", "movl\t$0, %ecx", ".L2:", "addl\t$1, %ecx",
                 "cmpl\t$4, %ecx", "jne\t.L2", "decl\t%edi",
                 "jnz\t.L1", "ret"]
        self.assertEqual(asmdiff.loop_spans(lines),
                         [(".L1", 6), (".L2", 3)])

    def test_xtensa_zero_overhead_loop(self):
        # loop* references its END label; the span is what it encloses.
        lines = ["loopgt\ta3, .L5", "addi.n\ta2, a2, 1",
                 "s32i.n\ta2, a4, 0", ".L5:", "retw.n"]
        self.assertEqual(asmdiff.loop_spans(lines), [(".L5", 2)])

    def test_literal_pool_reference_ignored(self):
        # .LC44 lives outside the body, so it is not a span even though
        # the operand matches the label-reference pattern.
        lines = ["l32r\ta8, .LC44", "ret"]
        self.assertEqual(asmdiff.loop_spans(lines), [])


class TestJumpTableData(unittest.TestCase):
    """Inline data (switch jump tables, constants) emitted inside a function
    body is not counted as instructions and never reads as a loop span."""

    def test_table_entries_stripped_from_body(self):
        body = asmdiff.extract_functions(SWITCH_ASM)["sel"]
        self.assertFalse(any(".long" in line for line in body))
        self.assertIn(".L4:", body)  # the table's anchor label is kept

    def test_table_entries_not_counted_as_instructions(self):
        body = asmdiff.extract_functions(SWITCH_ASM)["sel"]
        insns, calls = asmdiff.analyze(body)
        self.assertEqual(insns, 22)   # 27 before the fix (5 .long entries)
        self.assertEqual(calls, [])   # notrack jmp *%rax is not a call

    def test_self_relative_table_is_not_a_phantom_span(self):
        # `.long .L5-.L4` references the table base .L4 from below; without
        # stripping, that reads as a backward branch and invents a loop.
        body = asmdiff.extract_functions(SWITCH_ASM)["sel"]
        self.assertEqual(asmdiff.loop_spans(body), [])

    def test_various_data_directives_stripped(self):
        # .word/.byte/.quad jump tables and constants on other targets.
        body = asmdiff.extract_functions(DATA_DIRECTIVES_ASM)["tbl"]
        for directive in (".word", ".byte", ".quad"):
            self.assertFalse(any(directive in line for line in body), directive)
        insns, _ = asmdiff.analyze(body)
        self.assertEqual(insns, 4)    # 8 before the fix (4 data entries)
        self.assertEqual(asmdiff.loop_spans(body), [])


class TestAutoPairs(unittest.TestCase):
    def test_pairs_by_convention(self):
        names = ["old_const", "new_const", "old_rt", "new_rt", "helper"]
        self.assertEqual(asmdiff.auto_pairs(names),
                         [("old_const", "new_const"), ("old_rt", "new_rt")])

    def test_unmatched_old_ignored(self):
        self.assertEqual(asmdiff.auto_pairs(["old_x", "new_y"]), [])


class TestAsmOutputName(unittest.TestCase):
    def test_short_command_stays_readable(self):
        self.assertEqual(asmdiff.asm_output_name("gcc -O3", "h.c"),
                         "gcc_O3_h.s")

    def test_long_command_fits_name_max(self):
        cc = "/opt/toolchain/" + "x" * 300 + "/gcc -O2 -I/long/include"
        name = asmdiff.asm_output_name(cc, "harness.c")
        self.assertLessEqual(len(name), 255)
        self.assertTrue(name.endswith("_harness.s"))

    def test_truncated_commands_do_not_collide(self):
        base = "/opt/toolchain/" + "x" * 300 + "/gcc -O2"
        self.assertNotEqual(asmdiff.asm_output_name(base, "h.c"),
                            asmdiff.asm_output_name(base + " -DX", "h.c"))


class TestBuildMatrix(unittest.TestCase):
    CONFIG = {"default": "s3",
              "s3": {"cc": "xtensa-gcc", "flags": ["-O2", "-mlongcalls"]},
              "host": {"cc": "gcc", "flags": ["-O3"]}}

    def test_explicit_cc_used_verbatim(self):
        self.assertEqual(asmdiff.build_matrix(["tcc -O1"], [], None, None),
                         ["tcc -O1"])

    def test_targets_resolve_and_follow_cc_entries(self):
        matrix = asmdiff.build_matrix(["tcc -O1"], ["host"],
                                      self.CONFIG, "cfg.toml")
        self.assertEqual(matrix, ["tcc -O1", "gcc -O3"])

    def test_config_default_target_used_when_nothing_given(self):
        self.assertEqual(asmdiff.build_matrix([], [], self.CONFIG, "c"),
                         ["xtensa-gcc -O2 -mlongcalls"])

    def test_config_default_may_be_a_list(self):
        cfg = dict(self.CONFIG, default=["s3", "host"])
        self.assertEqual(asmdiff.build_matrix([], [], cfg, "c"),
                         ["xtensa-gcc -O2 -mlongcalls", "gcc -O3"])

    def test_fallback_is_bare_gcc_and_clang(self):
        self.assertEqual(asmdiff.build_matrix([], [], None, None),
                         ["gcc -O3", "clang -O3"])

    def test_unknown_target_errors_and_lists_known(self):
        with self.assertRaises(SystemExit) as ctx:
            asmdiff.build_matrix([], ["nope"], self.CONFIG, "cfg.toml")
        self.assertIn("host", str(ctx.exception))
        self.assertIn("s3", str(ctx.exception))

    def test_target_flags_must_be_an_array(self):
        cfg = {"bad": {"cc": "gcc", "flags": "-O3"}}
        with self.assertRaises(SystemExit):
            asmdiff.build_matrix([], ["bad"], cfg, "cfg.toml")

    def test_target_needs_cc_string(self):
        cfg = {"bad": {"flags": ["-O3"]}}
        with self.assertRaises(SystemExit):
            asmdiff.build_matrix([], ["bad"], cfg, "cfg.toml")


class TestIncludeFlags(unittest.TestCase):
    """Lifting include/define flags out of one recorded compile command."""

    def test_glued_and_split_include_paths(self):
        toks = ["cc", "-Iinc", "-I", "inc2", "-c", "a.c"]
        self.assertEqual(asmdiff.include_flags(toks, "/build"),
                         ["-I", "/build/inc", "-I", "/build/inc2"])

    def test_absolute_paths_left_alone(self):
        self.assertEqual(asmdiff.include_flags(["-I/abs/inc"], "/build"),
                         ["-I", "/abs/inc"])

    def test_defines_glued_and_split(self):
        self.assertEqual(
            asmdiff.include_flags(["-DFOO=1", "-D", "BAR", "-UNDEBUG"], "/b"),
            ["-DFOO=1", "-DBAR", "-UNDEBUG"])

    def test_system_and_forced_include_families(self):
        toks = ["-isystem", "sys", "-iquote", "q", "-idirafter", "d",
                "-include", "cfg.h", "-imacros", "m.h"]
        self.assertEqual(
            asmdiff.include_flags(toks, "/build"),
            ["-isystem", "/build/sys", "-iquote", "/build/q",
             "-idirafter", "/build/d", "-include", "/build/cfg.h",
             "-imacros", "/build/m.h"])

    def test_non_include_flags_and_source_dropped(self):
        toks = ["gcc", "-O2", "-std=c11", "-Wall", "-g", "-c", "a.c",
                "-o", "a.o", "-Iinc"]
        self.assertEqual(asmdiff.include_flags(toks, "/b"), ["-I", "/b/inc"])

    def test_dangling_flag_at_end_ignored(self):
        self.assertEqual(asmdiff.include_flags(["-Iinc", "-I"], "/b"),
                         ["-I", "/b/inc"])

    def test_lowercase_isystem_not_split_as_capital_I(self):
        # -isystem must not be read as -I + "system".
        self.assertEqual(asmdiff.include_flags(["-isystem", "/s"], ""),
                         ["-isystem", "/s"])


class TestCompileCommandsFlags(unittest.TestCase):
    def _db(self, tmp, entries):
        path = Path(tmp) / "compile_commands.json"
        path.write_text(json.dumps(entries))
        asmdiff._DB_CACHE.clear()
        return str(path)

    def test_matches_by_resolved_path_command_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src" / "foo.c"
            src.parent.mkdir()
            src.touch()
            db = self._db(tmp, [{
                "directory": tmp, "file": str(src),
                "command": f"cc -Iinc -DX=1 -c {src} -o foo.o"}])
            self.assertEqual(asmdiff.compile_commands_flags(db, str(src)),
                             ["-I", f"{tmp}/inc", "-DX=1"])

    def test_matches_relative_file_and_arguments_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "foo.c"
            src.touch()
            db = self._db(tmp, [{
                "directory": tmp, "file": "foo.c",
                "arguments": ["cc", "-Iinc", "-c", "foo.c"]}])
            self.assertEqual(asmdiff.compile_commands_flags(db, str(src)),
                             ["-I", f"{tmp}/inc"])

    def test_missing_source_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, [{"directory": tmp, "file": f"{tmp}/a.c",
                                 "command": "cc -c a.c"}])
            with self.assertRaises(SystemExit) as ctx:
                asmdiff.compile_commands_flags(db, f"{tmp}/b.c")
            self.assertIn("not found", str(ctx.exception))

    def test_same_name_different_path_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp, [{"directory": tmp,
                                 "file": f"{tmp}/other/foo.c",
                                 "command": "cc -c foo.c"}])
            with self.assertRaises(SystemExit) as ctx:
                asmdiff.compile_commands_flags(db, f"{tmp}/foo.c")
            self.assertIn("different path", str(ctx.exception))

    def test_bad_json_shape_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cc.json"
            path.write_text('{"not": "a list"}')
            asmdiff._DB_CACHE.clear()
            with self.assertRaises(SystemExit):
                asmdiff.compile_commands_flags(str(path), "x.c")


class TestTargetCompileCommands(unittest.TestCase):
    def test_target_carries_expanded_db_path(self):
        os.environ["ASMDIFF_TEST_DB"] = "/proj/build"
        try:
            cfg = {"t": {"cc": "gcc", "flags": ["-O2"],
                         "compile_commands": "$ASMDIFF_TEST_DB/cc.json"}}
            matrix = asmdiff.build_matrix([], ["t"], cfg, "c")
            self.assertEqual(matrix, ["gcc -O2"])          # str value unchanged
            self.assertEqual(matrix[0].compile_commands,
                             "/proj/build/cc.json")         # attribute carried
        finally:
            del os.environ["ASMDIFF_TEST_DB"]

    def test_compile_commands_must_be_a_string(self):
        cfg = {"t": {"cc": "gcc", "flags": [], "compile_commands": ["x"]}}
        with self.assertRaises(SystemExit):
            asmdiff.build_matrix([], ["t"], cfg, "c")

    def test_cc_entries_have_no_db_attribute(self):
        matrix = asmdiff.build_matrix(["gcc -O3"], [], None, None)
        self.assertIsNone(getattr(matrix[0], "compile_commands", None))


@contextlib.contextmanager
def _inside(directory):
    """Run a block with CWD set to ``directory`` (discovery is CWD-based)."""
    prev = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(prev)


class TestFindCompileCommands(unittest.TestCase):
    """Auto-discovery of compile_commands.json near the current directory."""

    def _touch_db(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "compile_commands.json").write_text("[]")

    def test_search_order_cwd_build_parent_parentbuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "sub"
            cwd.mkdir()
            for where, expect in [(root / "build", root / "build"),
                                  (root, root),
                                  (cwd / "build", cwd / "build"),
                                  (cwd, cwd)]:
                self._touch_db(where)
                with _inside(cwd):
                    self.assertEqual(asmdiff.find_compile_commands(),
                                     str(expect / "compile_commands.json"))

    def test_nothing_found_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "a" / "b"
            cwd.mkdir(parents=True)
            with _inside(cwd):
                self.assertIsNone(asmdiff.find_compile_commands())


class TestDiscoveredCompileCommands(unittest.TestCase):
    """compile_commands = true in a target / bare --compile-commands."""

    def test_target_true_discovers_and_is_soft(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "build" / "compile_commands.json"
            db.parent.mkdir()
            db.write_text("[]")
            cfg = {"t": {"cc": "gcc", "compile_commands": True}}
            with _inside(tmp):
                matrix = asmdiff.build_matrix([], ["t"], cfg, "c")
            self.assertEqual(matrix[0].compile_commands, str(db))
            self.assertTrue(matrix[0].db_discovered)

    def test_target_true_with_no_db_anywhere_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "a" / "b"
            cwd.mkdir(parents=True)
            cfg = {"t": {"cc": "gcc", "compile_commands": True}}
            with _inside(cwd), self.assertRaises(SystemExit) as ctx:
                asmdiff.build_matrix([], ["t"], cfg, "c")
            self.assertIn("compile_commands.json", str(ctx.exception))

    def test_target_false_means_off(self):
        cfg = {"t": {"cc": "gcc", "compile_commands": False}}
        matrix = asmdiff.build_matrix([], ["t"], cfg, "c")
        self.assertIsNone(matrix[0].compile_commands)

    def test_explicit_path_stays_hard(self):
        cfg = {"t": {"cc": "gcc", "compile_commands": "/x/cc.json"}}
        matrix = asmdiff.build_matrix([], ["t"], cfg, "c")
        self.assertEqual(matrix[0].compile_commands, "/x/cc.json")
        self.assertFalse(matrix[0].db_discovered)

    def test_cli_bare_flag_fills_cc_and_default_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "compile_commands.json"
            db.write_text("[]")
            with _inside(tmp):
                matrix = asmdiff.build_matrix(["gcc -O3"], [], None, None,
                                              db_arg=True)
                fallback = asmdiff.build_matrix([], [], None, None,
                                                db_arg=True)
        for entry in [matrix[0]] + list(fallback):
            self.assertEqual(entry.compile_commands, str(db))
            self.assertTrue(entry.db_discovered)

    def test_cli_path_is_explicit_and_expanded(self):
        os.environ["ASMDIFF_TEST_DB2"] = "/proj"
        try:
            matrix = asmdiff.build_matrix(["gcc -O3"], [], None, None,
                                          db_arg="$ASMDIFF_TEST_DB2/cc.json")
        finally:
            del os.environ["ASMDIFF_TEST_DB2"]
        self.assertEqual(matrix[0].compile_commands, "/proj/cc.json")
        self.assertFalse(matrix[0].db_discovered)

    def test_target_own_path_beats_cli(self):
        cfg = {"t": {"cc": "gcc", "compile_commands": "/own/cc.json"}}
        matrix = asmdiff.build_matrix([], ["t"], cfg, "c",
                                      db_arg="/cli/cc.json")
        self.assertEqual(matrix[0].compile_commands, "/own/cc.json")

    def test_missing_source_soft_skips_with_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compile_commands.json"
            path.write_text(json.dumps([{"directory": tmp,
                                         "file": f"{tmp}/a.c",
                                         "command": "cc -Iinc -c a.c"}]))
            asmdiff._DB_CACHE.clear()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                flags = asmdiff.compile_commands_flags(
                    str(path), f"{tmp}/b.c", missing_ok=True)
            self.assertEqual(flags, [])
            self.assertIn("b.c", err.getvalue())


class TestResolveCc(unittest.TestCase):
    def test_home_and_env_vars_expand(self):
        resolved = asmdiff.resolve_cc("~/bin/mycc", "t")
        self.assertEqual(resolved, str(Path.home() / "bin/mycc"))
        os.environ["ASMDIFF_TEST_DIR"] = "/opt/tc"
        try:
            self.assertEqual(asmdiff.resolve_cc("$ASMDIFF_TEST_DIR/gcc", "t"),
                             "/opt/tc/gcc")
        finally:
            del os.environ["ASMDIFF_TEST_DIR"]

    def test_plain_command_untouched(self):
        self.assertEqual(asmdiff.resolve_cc("gcc", "t"), "gcc")

    def test_glob_picks_highest_numeric_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            for ver in ("esp-9.1.0", "esp-13.2.0", "esp-15.2.0"):
                d = Path(tmp) / ver / "bin"
                d.mkdir(parents=True)
                (d / "xgcc").touch()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                resolved = asmdiff.resolve_cc(f"{tmp}/esp-*/bin/xgcc", "t")
            # numeric sort: 15 > 13 > 9 (lexically "esp-9" would win)
            self.assertEqual(resolved, f"{tmp}/esp-15.2.0/bin/xgcc")
            self.assertIn("matched 3 toolchains", err.getvalue())

    def test_glob_single_match_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "esp-15.2.0" / "bin"
            d.mkdir(parents=True)
            (d / "xgcc").touch()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                resolved = asmdiff.resolve_cc(f"{tmp}/esp-*/bin/xgcc", "t")
            self.assertEqual(resolved, f"{tmp}/esp-15.2.0/bin/xgcc")
            self.assertEqual(err.getvalue(), "")

    def test_glob_no_match_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                asmdiff.resolve_cc(f"{tmp}/esp-*/bin/xgcc", "t")

    def test_env_vars_expand_in_flags(self):
        os.environ["ASMDIFF_TEST_INC"] = "/opt/proj/src"
        try:
            cfg = {"t": {"cc": "gcc",
                         "flags": ["-O2", "-I$ASMDIFF_TEST_INC"]}}
            self.assertEqual(asmdiff.build_matrix([], ["t"], cfg, "c"),
                             ["gcc -O2 -I/opt/proj/src"])
        finally:
            del os.environ["ASMDIFF_TEST_INC"]


class TestRendering(unittest.TestCase):
    def test_side_by_side_pads_and_fills(self):
        out = asmdiff.side_by_side(["a"], ["b", "c"], "L", "R", width=4)
        lines = out.splitlines()
        self.assertEqual(lines[0], "L    | R")
        self.assertEqual(lines[2], "a    | b")
        self.assertEqual(lines[3], "     | c")

    def test_summary_table(self):
        funcs = {"old_c": ["mulss\tx, %xmm0", "ret"],
                 "new_c": ["jmp\tldexpf@PLT"]}
        out = asmdiff.summary_table([("old_c", "new_c")], funcs)
        lines = out.splitlines()
        self.assertIn("function", lines[0])
        self.assertIn("loop spans", lines[0])
        self.assertRegex(lines[1], r"old_c\s+baseline\s+2\s+-\s+-")
        self.assertRegex(lines[2], r"new_c\s+candidate\s+1\s+ldexpf\s+-")

    def test_summary_table_loop_spans_column(self):
        funcs = {"a": [".L2:", "addl\t$1, %eax", "jne\t.L2"],
                 "b": ["ret"]}
        out = asmdiff.summary_table([("a", "b")], funcs)
        lines = out.splitlines()
        self.assertRegex(lines[1], r"a\s+baseline\s+2\s+-\s+\.L2:2")
        self.assertRegex(lines[2], r"b\s+candidate\s+1\s+-\s+-")

    def test_file_summary_totals_and_call_union(self):
        funcs = {"f": ["call\tmalloc", "ret"],
                 "g": [".L2:", "addl\t$1, %eax", "jne\t.L2",
                       "call\tmalloc", "call\tfree", "ret"]}
        out = asmdiff.file_summary_table(funcs)
        lines = out.splitlines()
        self.assertRegex(lines[1], r"f\s+2\s+malloc\s+-")
        self.assertRegex(lines[2], r"g\s+5\s+malloc, free\s+\.L2:2")
        self.assertRegex(lines[3],
                         r"TOTAL \(2 functions\)\s+7\s+malloc, free\s+-")


class TestFileTags(unittest.TestCase):
    def test_distinct_basenames_used_directly(self):
        self.assertEqual(asmdiff.file_tags("p/old.c", "p/new.c"),
                         ("old.c", "new.c"))

    def test_same_basename_disambiguated_by_parent(self):
        self.assertEqual(asmdiff.file_tags("/tmp/amy-exp2f/log2.c",
                                           "/tmp/amy-ldexpf/log2.c"),
                         ("amy-exp2f/log2.c", "amy-ldexpf/log2.c"))

    def test_identical_parents_fall_back_to_full_paths(self):
        self.assertEqual(asmdiff.file_tags("a/src/f.c", "b/src/f.c"),
                         ("a/src/f.c", "b/src/f.c"))


class TestAcrossValidation(unittest.TestCase):
    """CLI validation rejects bad --across usage before any compilation."""

    def _expect_error(self, argv, fragment):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                asmdiff.main(argv)
        self.assertIn(fragment, err.getvalue())

    def test_across_and_pair_mutually_exclusive(self):
        self._expect_error(["x.c", "--across", "f", "--pair", "a:b"],
                           "mutually exclusive")

    def test_two_files_with_pair_rejected(self):
        self._expect_error(["a.c", "b.c", "--pair", "x:y"],
                           "--pair compares within one file")

    def test_at_most_two_files(self):
        self._expect_error(["a.c", "b.c", "c.c", "--across", "f"],
                           "at most two")

    def test_across_one_file_needs_two_cc_entries(self):
        self._expect_error(["a.c", "--across", "f", "--cc", "gcc -O3"],
                           "at least two --cc")


if __name__ == "__main__":
    unittest.main()
