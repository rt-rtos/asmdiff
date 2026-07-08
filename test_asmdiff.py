#!/usr/bin/env python3
"""Unit tests for asmdiff.py.  Run: python3 tools/asmdiff/test_asmdiff.py -v"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import asmdiff

# Holds the isolation temp dir alive for the module's lifetime.
_ISOLATION = None


def setUpModule():
    """Isolate config discovery from the developer's real environment.

    Tests run ``asmdiff.main()`` in-process, and ``find_config`` falls back
    to ``$HOME/.config/asmdiff.toml`` (and a ``./asmdiff.toml`` in the CWD).
    A real config on the machine - e.g. one whose ``default`` names cross
    targets - would otherwise leak into any test that passes no ``--config``,
    changing the matrix and masking the arg-validation errors it asserts on.
    Point HOME and the CWD at empty temp dirs so discovery finds nothing
    unless a test sets one up itself.
    """
    global _ISOLATION
    _ISOLATION = tempfile.TemporaryDirectory()
    home = Path(_ISOLATION.name) / "home"
    cwd = Path(_ISOLATION.name) / "cwd"
    home.mkdir()
    cwd.mkdir()
    setUpModule._saved = (os.environ.get("HOME"),
                          os.environ.get("USERPROFILE"),
                          os.getcwd())
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)   # Path.home() on native Windows
    os.chdir(cwd)


def tearDownModule():
    home, userprofile, cwd = setUpModule._saved
    os.chdir(cwd)
    for name, value in (("HOME", home), ("USERPROFILE", userprofile)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _ISOLATION.cleanup()


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

# Trimmed but structurally faithful `xtensa-esp32s3-elf-objdump -d` output:
# a ZOL loop, a backward branch, a cross-function call, a symbol-less data
# gap (offset-form header) with a `...` filler, and a $-mangled symbol.
OBJDUMP_ASM = """\
firmware.elf:     file format elf32-xtensa-le


Disassembly of section .flash.text:

40370400 <render_lut>:
40370400:\t004136        \tentry\ta1, 32
40370403:\t0c0a          \tmovi.n\ta10, 0
40370405:\ta48c76        \tloop\ta4, 40370411 <render_lut+0x11>
40370408:\t3a2a          \tadd.n\ta2, a10, a3
4037040a:\t020222        \tl32i\ta0, a2, 0
4037040d:\t0a1a          \tadd.n\ta10, a10, a0
4037040f:\tf03d          \tnop.n
40370411:\tf01d          \tretw.n

40370414 <fx_mix>:
40370414:\t004136        \tentry\ta1, 32
40370417:\te5fffe        \tcall8\t40370400 <render_lut>
4037041a:\t0c0a          \tmovi.n\ta10, 0
4037041c:\t1baa          \taddi.n\ta10, a10, 1
4037041e:\t56faff        \tbnez\ta10, 4037041c <fx_mix+0x8>
40370421:\te50c00        \tcall8\t40380000 <memset>
40370424:\tf01d          \tretw.n

40370428 <render_lpf_lut$constprop$0-0x1130>:
40370428:\t00000000      \till
\t...

40371558 <render_lpf_lut$constprop$0>:
40371558:\t004136        \tentry\ta1, 32
4037155b:\tf01d          \tretw.n
"""

# `-mlongcalls` call sequences as objdump renders them: the linker left
# these out of range, so each is "l32r aN, <lit> (VALUE <sym>)" feeding
# "callx8 aN".  Covers: adjacent load, load a few insns back, a literal
# that is a constant (offset-form annotation), no load at all, a load
# with no value annotation, and a load beyond the 8-insn window.
OBJDUMP_LONGCALL_ASM = """\
firmware.elf:     file format elf32-xtensa-le


Disassembly of section .flash.text:

40380000 <render_partial>:
40380000:\t004136        \tentry\ta1, 32
40380003:\tc0d681        \tl32r\ta8, 40370100 <_stext+0x100> (40002274 <__divsf3>)
40380006:\t0008e0        \tcallx8\ta8
40380009:\tc19ea1        \tl32r\ta9, 40370104 <_stext+0x104> (473b8000 <_etext+0x100>)
4038000c:\tc0cf81        \tl32r\ta8, 40370100 <_stext+0x100> (40002274 <__divsf3>)
4038000f:\t05bd          \tmov.n\ta11, a5
40380011:\t51b8          \tl32i.n\ta11, a1, 20
40380013:\t0008e0        \tcallx8\ta8
40380016:\t0009e0        \tcallx8\ta9
40380019:\t000ae0        \tcallx8\ta10
4038001c:\tf01d          \tretw.n

40380020 <far_call>:
40380020:\tc0d681        \tl32r\ta8, 40370100 <_stext+0x100> (40002274 <__divsf3>)
40380023:\tf03d          \tnop.n
40380025:\tf03d          \tnop.n
40380027:\tf03d          \tnop.n
40380029:\tf03d          \tnop.n
4038002b:\tf03d          \tnop.n
4038002d:\tf03d          \tnop.n
4038002f:\tf03d          \tnop.n
40380031:\tf03d          \tnop.n
40380033:\t0008e0        \tcallx8\ta8
40380036:\tf01d          \tretw.n

40380040 <no_annot>:
40380040:\tc0d681        \tl32r\ta8, 40370100
40380043:\t0008e0        \tcallx8\ta8
40380046:\tf01d          \tretw.n

40380060 <memberptr>:
40380060:\tc0d681        \tl32r\ta8, 40370108 <_stext+0x108> (3fc90000 <amy_global>)
40380063:\t880848        \tl32i\ta8, a8, 32
40380066:\t0008e0        \tcallx8\ta8
40380069:\tf01d          \tretw.n

40380070 <spilled>:
40380070:\tc0d681        \tl32r\ta8, 40370100 <_stext+0x100> (40002274 <__divsf3>)
40380073:\t6189          \ts32i.n\ta8, a1, 24
40380075:\t0008e0        \tcallx8\ta8
40380078:\tf01d          \tretw.n
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


class TestObjdumpExtract(unittest.TestCase):
    """extract_functions_objdump: linked-ELF `objdump -d` output becomes
    the same cleaned-lines shape extract_functions produces, with branch
    and loop target addresses rewritten to synthetic local labels so
    analyze() and loop_spans() work unchanged."""

    def setUp(self):
        self.funcs = asmdiff.extract_functions_objdump(OBJDUMP_ASM)

    def test_function_headers_found(self):
        self.assertEqual(sorted(self.funcs),
                         ["fx_mix", "render_lpf_lut$constprop$0",
                          "render_lut"])

    def test_data_region_headers_skipped(self):
        # <sym-0x1130> marks a literal pool / symbol-less gap whose bytes
        # disassemble as garbage; nothing from it may leak into a body.
        for body in self.funcs.values():
            self.assertFalse(any(ln.startswith("ill") for ln in body))

    def test_filler_lines_skipped(self):
        for body in self.funcs.values():
            self.assertNotIn("...", body)

    def test_insn_lines_cleaned(self):
        self.assertEqual(self.funcs["render_lut"][0], "entry\ta1, 32")

    def test_hex_bytes_column_dropped(self):
        # The byte dump ("004136") is one token; it must never be read
        # as the mnemonic or survive into the cleaned line.
        for body in self.funcs.values():
            for ln in body:
                self.assertNotRegex(ln, r"^[0-9a-f]+\s")

    def test_zol_end_label_synthesized(self):
        body = self.funcs["render_lut"]
        self.assertIn("loop\ta4, .L11_LEND", body)
        self.assertEqual(body[-2:], [".L11_LEND:", "retw.n"])

    def test_zol_span_via_loop_spans(self):
        self.assertEqual(asmdiff.loop_spans(self.funcs["render_lut"]),
                         [(".L11_LEND", 4)])

    def test_backward_branch_label_synthesized(self):
        body = self.funcs["fx_mix"]
        self.assertIn(".L8:", body)
        self.assertIn("bnez\ta10, .L8", body)

    def test_backward_branch_span(self):
        self.assertEqual(asmdiff.loop_spans(self.funcs["fx_mix"]),
                         [(".L8", 2)])

    def test_cross_function_target_uses_symbol(self):
        body = self.funcs["fx_mix"]
        self.assertIn("call8\trender_lut", body)
        self.assertIn("call8\tmemset", body)

    def test_calls_reported_by_analyze(self):
        _, calls = asmdiff.analyze(self.funcs["fx_mix"])
        self.assertEqual(calls, ["render_lut", "memset"])

    def test_zol_body_is_call_free(self):
        insns, calls = asmdiff.analyze(self.funcs["render_lut"])
        self.assertEqual((insns, calls), (8, []))


class TestLongcallResolver(unittest.TestCase):
    """Xtensa -mlongcalls survivors: a callx8 fed by an "l32r aN, <lit>
    (VALUE <sym>)" reports <sym> as the callee; without that evidence
    the register is kept (genuinely indirect, or binutils format
    drift)."""

    def setUp(self):
        self.funcs = asmdiff.extract_functions_objdump(OBJDUMP_LONGCALL_ASM)

    def test_adjacent_load_resolved(self):
        self.assertIn("callx8\t__divsf3", self.funcs["render_partial"])

    def test_load_a_few_insns_back_resolved(self):
        # Both __divsf3 sites resolve, including the one whose l32r is
        # three instructions above the call.
        body = self.funcs["render_partial"]
        self.assertEqual(body.count("callx8\t__divsf3"), 2)

    def test_constant_literal_not_a_callee(self):
        # a9's literal annotation is <_etext+0x100> - a value, not a
        # function symbol; the call must stay indirect.
        self.assertIn("callx8\ta9", self.funcs["render_partial"])

    def test_no_load_stays_indirect(self):
        self.assertIn("callx8\ta10", self.funcs["render_partial"])

    def test_calls_reported_by_analyze(self):
        _, calls = asmdiff.analyze(self.funcs["render_partial"])
        self.assertEqual(calls, ["__divsf3", "a9", "a10"])

    def test_load_beyond_window_stays_indirect(self):
        self.assertIn("callx8\ta8", self.funcs["far_call"])

    def test_unannotated_load_stays_indirect(self):
        self.assertIn("callx8\ta8", self.funcs["no_annot"])

    def test_clobbered_register_stays_indirect(self):
        # l32r loads a struct address (amy_global) but the l32i then
        # replaces a8 with a member function pointer: reporting the
        # struct as the callee would be wrong, so the call must stay
        # indirect.
        self.assertIn("callx8\ta8", self.funcs["memberptr"])
        _, calls = asmdiff.analyze(self.funcs["memberptr"])
        self.assertNotIn("amy_global", calls)

    def test_intervening_store_does_not_block(self):
        # s32i.n reads a8 (stores it to the stack) without writing it,
        # so the loaded callee is still live at the call.
        self.assertIn("callx8\t__divsf3", self.funcs["spilled"])


class TestElfMode(unittest.TestCase):
    """FIRMWARE.elf positional: disassemble a linked binary through the
    toolchain's objdump instead of compiling, selecting functions by
    name or --filter REGEX."""

    def _elf(self, tmp):
        p = Path(tmp) / "fw.elf"
        p.write_bytes(b"\x7fELF" + b"\0" * 12)
        return str(p)

    def _run(self, argv):
        real = asmdiff.run_objdump
        asmdiff.run_objdump = lambda objdump, elf: OBJDUMP_ASM
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                status = asmdiff.main(argv)
        finally:
            asmdiff.run_objdump = real
        return status, out.getvalue()

    def _expect_error(self, argv, fragment):
        real = asmdiff.run_objdump
        asmdiff.run_objdump = lambda objdump, elf: OBJDUMP_ASM
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), \
                 self.assertRaises(SystemExit) as ctx:
                with contextlib.redirect_stdout(io.StringIO()):
                    asmdiff.main(argv)
        finally:
            asmdiff.run_objdump = real
        self.assertIn(fragment, err.getvalue() + str(ctx.exception))

    def test_is_elf_magic_not_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(asmdiff.is_elf(self._elf(tmp)))
            fake = Path(tmp) / "not-really.elf"
            fake.write_text("int main;")
            self.assertFalse(asmdiff.is_elf(str(fake)))
            self.assertFalse(asmdiff.is_elf(str(Path(tmp) / "absent.elf")))

    def test_derive_objdump_swaps_gcc(self):
        self.assertEqual(
            asmdiff.derive_objdump(
                ["/tc/bin/xtensa-esp32s3-elf-gcc -O2 -mlongcalls"]),
            "/tc/bin/xtensa-esp32s3-elf-objdump")
        self.assertEqual(asmdiff.derive_objdump(["gcc -O3"]), "objdump")
        self.assertIsNone(asmdiff.derive_objdump(["clang -O3"]))
        self.assertEqual(asmdiff.derive_objdump(["clang -O3", "gcc -O2"]),
                         "objdump")

    def test_named_function_listing_and_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, out = self._run([self._elf(tmp), "render_lut",
                                     "--objdump", "od"])
        self.assertEqual(status, 0)
        self.assertIn("render_lut:", out)
        self.assertIn("loop\ta4, .L11_LEND", out)
        self.assertIn("function", out)           # stats table header

    def test_filter_prints_table_without_listings(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, out = self._run([self._elf(tmp), "--filter", "render_",
                                     "--objdump", "od"])
        self.assertEqual(status, 0)
        self.assertIn("render_lut", out)
        self.assertIn("render_lpf_lut$constprop$0", out)
        self.assertNotIn("fx_mix", out)
        self.assertNotIn("entry\t", out)         # table only, no listings

    def test_names_and_filter_combine(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, out = self._run([self._elf(tmp), "fx_mix",
                                     "--filter", "render_lut$",
                                     "--objdump", "od"])
        self.assertEqual(status, 0)
        self.assertIn("fx_mix:", out)            # named: listed
        self.assertIn("render_lut", out)         # filtered: in the table

    def test_unknown_function_suggests_close_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._expect_error([self._elf(tmp), "rendr_lut",
                                "--objdump", "od"],
                               "render_lut")

    def test_bare_elf_needs_names_or_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._expect_error([self._elf(tmp), "--objdump", "od"],
                               "--filter")

    def test_filter_matching_nothing_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._expect_error([self._elf(tmp), "--filter", "zzz",
                                "--objdump", "od"], "matched no function")

    def test_bad_filter_regex_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._expect_error([self._elf(tmp), "--filter", "(",
                                "--objdump", "od"], "bad --filter regex")

    def test_compile_flags_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._expect_error([self._elf(tmp), "f", "--pair", "a:b",
                                "--objdump", "od"], "disassembled")

    def test_second_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "b.c"
            other.touch()
            self._expect_error([self._elf(tmp), str(other),
                                "--objdump", "od"], "one binary")

    def test_filter_without_elf_rejected(self):
        self._expect_error(["x.c", "f", "--filter", "r"], "ELF input")

    def test_no_gcc_in_matrix_needs_objdump(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._expect_error([self._elf(tmp), "f", "--cc", "clang -O3"],
                               "--objdump")

    def test_target_db_discovery_not_triggered(self):
        # ELF mode never compiles, so a target's compile_commands = true
        # must not launch (and fail) database discovery while the target
        # is only being used to locate its objdump.
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "asmdiff.toml"
            cfg.write_text('[t]\ncc = "/tc/bin/xtensa-esp32s3-elf-gcc"\n'
                           'compile_commands = true\n')
            status, out = self._run([self._elf(tmp), "render_lut",
                                     "--config", str(cfg), "--target", "t"])
        self.assertEqual(status, 0)
        self.assertIn("render_lut:", out)


class TestAutoPairs(unittest.TestCase):
    def test_pairs_by_convention(self):
        names = ["old_const", "new_const", "old_rt", "new_rt", "helper"]
        self.assertEqual(asmdiff.auto_pairs(names),
                         [("old_const", "new_const"), ("old_rt", "new_rt")])

    def test_unmatched_old_ignored(self):
        self.assertEqual(asmdiff.auto_pairs(["old_x", "new_y"]), [])


class TestSplitPositionals(unittest.TestCase):
    """SOURCE.c FUNC grammar: extra positionals are files when they
    exist, function names when bare, and errors when path-like typos."""

    def test_existing_file_is_a_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            second = Path(tmp) / "b.c"
            second.touch()
            sources, fns = asmdiff.split_positionals(["a.c", str(second)])
        self.assertEqual(sources, ["a.c", str(second)])
        self.assertEqual(fns, [])

    def test_bare_name_is_a_function(self):
        sources, fns = asmdiff.split_positionals(["a.c", "render_lut"])
        self.assertEqual(sources, ["a.c"])
        self.assertEqual(fns, ["render_lut"])

    def test_several_function_names(self):
        sources, fns = asmdiff.split_positionals(["a.c", "f", "g"])
        self.assertEqual(sources, ["a.c"])
        self.assertEqual(fns, ["f", "g"])

    def test_missing_source_suffix_arg_errors(self):
        with self.assertRaises(SystemExit) as ctx:
            asmdiff.split_positionals(["a.c", "typo.c"])
        self.assertIn("no such file", str(ctx.exception))

    def test_missing_path_separator_arg_errors(self):
        with self.assertRaises(SystemExit) as ctx:
            asmdiff.split_positionals(["a.c", "src/render"])
        self.assertIn("no such file", str(ctx.exception))

    def test_uppercase_asm_suffix_is_path_like(self):
        with self.assertRaises(SystemExit):
            asmdiff.split_positionals(["a.c", "startup.S"])

    def test_first_positional_is_always_a_source(self):
        sources, fns = asmdiff.split_positionals(["no_suffix_name"])
        self.assertEqual(sources, ["no_suffix_name"])
        self.assertEqual(fns, [])


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


class TestSpecsAndSysroot(unittest.TestCase):
    """Driver flags that change the header environment (-specs, --sysroot)."""

    def test_specs_glued_bare_name_not_resolved(self):
        # A bare specs name (no path separator) is found in the compiler's
        # own search dirs; gluing a directory onto it would break it.
        self.assertEqual(
            asmdiff.include_flags(["-specs=picolibc.specs"], "/build"),
            ["-specs=picolibc.specs"])

    def test_specs_with_path_resolved_against_directory(self):
        self.assertEqual(
            asmdiff.include_flags(["-specs=./custom/my.specs"], "/build"),
            ["-specs=/build/custom/my.specs"])

    def test_specs_split_and_double_dash(self):
        self.assertEqual(
            asmdiff.include_flags(["-specs", "nano.specs",
                                   "--specs=nosys.specs"], "/b"),
            ["-specs=nano.specs", "--specs=nosys.specs"])

    def test_sysroot_glued_and_split(self):
        self.assertEqual(
            asmdiff.include_flags(["--sysroot=sr", "--sysroot", "/abs"],
                                  "/b"),
            ["--sysroot=/b/sr", "--sysroot=/abs"])


class TestResponseFiles(unittest.TestCase):
    """GCC @file response files inside compile_commands entries."""

    def test_flags_inside_response_file_are_borrowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rsp = Path(tmp) / "toolchain" / "cflags"
            rsp.parent.mkdir()
            rsp.write_text("-mlongcalls\n-specs=picolibc.specs\n-Irspinc\n")
            src = Path(tmp) / "a.c"
            src.touch()
            db = Path(tmp) / "compile_commands.json"
            db.write_text(json.dumps([{
                "directory": tmp, "file": str(src),
                "command": f"cc -Iinc @{rsp} -c a.c"}]))
            asmdiff._DB_CACHE.clear()
            self.assertEqual(
                asmdiff.compile_commands_flags(str(db), str(src)),
                ["-I", f"{tmp}/inc", "-specs=picolibc.specs",
                 "-I", f"{tmp}/rspinc"])

    def test_relative_response_file_resolves_against_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cflags").write_text("-DFROMRSP")
            self.assertEqual(
                asmdiff._expand_response_files(["@cflags"], tmp),
                ["-DFROMRSP"])

    def test_nested_response_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "outer").write_text("@inner -DOUTER")
            (Path(tmp) / "inner").write_text("-DINNER")
            self.assertEqual(
                asmdiff._expand_response_files(["@outer"], tmp),
                ["-DINNER", "-DOUTER"])

    def test_missing_response_file_warns_and_continues(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = asmdiff._expand_response_files(["-DKEEP", "@/nope/x"], "/b")
        self.assertEqual(out, ["-DKEEP"])
        self.assertIn("/nope/x", err.getvalue())


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
    """Auto-discovery of compile_commands.json by walking up from the CWD."""

    def _touch_db(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "compile_commands.json").write_text("[]")

    def _repo(self, tmp):
        """A fake repo root: .git bounds the walk so tests never escape
        the tempdir and pick up a stray database further up."""
        root = Path(tmp)
        (root / ".git").mkdir()
        return root

    def test_nearer_hits_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
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

    def test_walks_up_from_nested_component_dir(self):
        # components/amy/src is three levels below the project root where
        # idf.py leaves build/compile_commands.json.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            self._touch_db(root / "build")
            cwd = root / "components" / "amy" / "src"
            cwd.mkdir(parents=True)
            with _inside(cwd):
                self.assertEqual(asmdiff.find_compile_commands(),
                                 str(root / "build" / "compile_commands.json"))

    def test_stops_at_repository_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch_db(Path(tmp))          # db ABOVE the repo root
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").write_text("gitdir: elsewhere")  # worktree form
            cwd = repo / "src"
            cwd.mkdir()
            with _inside(cwd):
                self.assertIsNone(asmdiff.find_compile_commands())

    def test_nothing_found_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            cwd = root / "a" / "b"
            cwd.mkdir(parents=True)
            with _inside(cwd):
                self.assertIsNone(asmdiff.find_compile_commands())


class TestObjectLabels(unittest.TestCase):
    """Data labels must not be reported as functions."""

    ASM = """\
\t.type\tscale, @function
scale:
\tentry\tsp, 32
\tretw.n
\t.size\tscale, .-scale
\t.type\t__func__$1, @object
\t.size\t__func__$1, 9
__func__$1:
\t.string\t"app_main"
\t.local\ts_queue
\t.comm\ts_queue,4,4
\t.lcomm\ts_tmp,8
"""

    def test_object_labels_skipped(self):
        funcs = asmdiff.extract_functions(self.ASM)
        self.assertEqual(list(funcs), ["scale"])

    def test_arm_percent_function_type_still_reported(self):
        asm = "\t.type\tf, %function\nf:\n\tbx\tlr\n\t.size\tf, .-f\n"
        self.assertEqual(list(asmdiff.extract_functions(asm)), ["f"])

    def test_untyped_label_still_treated_as_function(self):
        # Hand-written asm often has no .type at all.
        asm = "myfunc:\n\tret\n"
        self.assertEqual(list(asmdiff.extract_functions(asm)), ["myfunc"])

    def test_local_comm_literal_lines_not_counted(self):
        asm = ("f:\n\tmov.n\ta2, a3\n\t.literal_position\n"
               "\t.literal .LC1, 4096\n\t.local\tx\n\t.comm\tx,4,4\n"
               "\tretw.n\n")
        funcs = asmdiff.extract_functions(asm)
        insns, _ = asmdiff.analyze(funcs["f"])
        self.assertEqual(insns, 2)


class TestFlagsLike(unittest.TestCase):
    """--flags-like: a source absent from the db borrows a named entry."""

    def _db(self, tmp):
        path = Path(tmp) / "compile_commands.json"
        path.write_text(json.dumps([{
            "directory": tmp, "file": f"{tmp}/real.c",
            "command": "cc -Iinc -DESP_PLATFORM -c real.c"}]))
        asmdiff._DB_CACHE.clear()
        asmdiff._MISS_NOTED.clear()
        return str(path)

    def test_missing_source_borrows_named_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            asmdiff.FLAGS_LIKE = f"{tmp}/real.c"
            try:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    flags = asmdiff.compile_commands_flags(
                        db, f"{tmp}/copy.c", missing_ok=True)
            finally:
                asmdiff.FLAGS_LIKE = None
            self.assertEqual(flags, ["-I", f"{tmp}/inc", "-DESP_PLATFORM"])
            self.assertIn("real.c", err.getvalue())     # borrow is announced
            # A satisfied borrow is not a miss: no apples-to-oranges state.
            self.assertEqual(asmdiff._MISS_NOTED, set())

    def test_applies_to_explicit_db_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            asmdiff.FLAGS_LIKE = f"{tmp}/real.c"
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    flags = asmdiff.compile_commands_flags(
                        db, f"{tmp}/copy.c", missing_ok=False)
            finally:
                asmdiff.FLAGS_LIKE = None
            self.assertEqual(flags[-1], "-DESP_PLATFORM")

    def test_bad_flags_like_target_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            asmdiff.FLAGS_LIKE = f"{tmp}/nowhere.c"
            try:
                with self.assertRaises(SystemExit):
                    asmdiff.compile_commands_flags(db, f"{tmp}/copy.c",
                                                   missing_ok=True)
            finally:
                asmdiff.FLAGS_LIKE = None

    def test_miss_messages_suggest_flags_like(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            with self.assertRaises(SystemExit) as ctx:   # hard error path
                asmdiff.compile_commands_flags(db, f"{tmp}/other/real.c")
            self.assertIn("--flags-like", str(ctx.exception))
            err = io.StringIO()                          # soft-miss path
            with contextlib.redirect_stderr(err):
                asmdiff.compile_commands_flags(db, f"{tmp}/sub/real.c",
                                               missing_ok=True)
            self.assertIn("--flags-like", err.getvalue())


class TestMarkDbMisses(unittest.TestCase):
    """Two-file comparisons where only one side got borrowed flags."""

    def _entry(self, db):
        return asmdiff.Target("gcc -O2", db, db_discovered=True)

    def test_one_sided_miss_tags_and_warns(self):
        asmdiff._MISS_NOTED.clear()
        asmdiff._MISS_NOTED.add(("/db.json", str(Path("b.c").resolve())))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            tags = asmdiff.mark_db_misses(self._entry("/db.json"),
                                          ["a.c", "b.c"], ("a.c", "b.c"))
        self.assertEqual(tags, ["a.c", "b.c [no db entry]"])
        self.assertIn("header", err.getvalue())

    def test_both_missed_means_same_env_no_warning(self):
        asmdiff._MISS_NOTED.clear()
        for s in ("a.c", "b.c"):
            asmdiff._MISS_NOTED.add(("/db.json", str(Path(s).resolve())))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            tags = asmdiff.mark_db_misses(self._entry("/db.json"),
                                          ["a.c", "b.c"], ("a.c", "b.c"))
        self.assertEqual(tags, ["a.c", "b.c"])
        self.assertEqual(err.getvalue(), "")

    def test_no_db_entry_untouched(self):
        tags = asmdiff.mark_db_misses("gcc -O2", ["a.c", "b.c"],
                                      ("a.c", "b.c"))
        self.assertEqual(tags, ["a.c", "b.c"])


class TestMangledPairHint(unittest.TestCase):
    def test_mangled_old_new_detected(self):
        self.assertTrue(asmdiff.mangled_pair_hint(
            ["_Z9old_scalef", "_Z9new_scalef"]))

    def test_plain_c_names_no_hint(self):
        self.assertFalse(asmdiff.mangled_pair_hint(["scale", "helper"]))

    def test_mangled_but_not_old_new_no_hint(self):
        self.assertFalse(asmdiff.mangled_pair_hint(["_Z6renderv"]))


class TestCompileFailureOutput(unittest.TestCase):
    CMD = ["xtensa-gcc", "-O2"] + [f"-I/inc{i}" for i in range(50)] + ["a.c"]
    STDERR = "\n".join(f"err line {i}" for i in range(60))

    def test_default_trims_flags_and_stderr(self):
        asmdiff.VERBOSE = False
        with self.assertRaises(SystemExit) as ctx:
            asmdiff._compile_failure(self.CMD, self.STDERR)
        msg = str(ctx.exception)
        self.assertIn("xtensa-gcc failed on a.c", msg)
        self.assertNotIn("-I/inc0", msg)                 # no flag dump
        self.assertIn("err line 0", msg)
        self.assertNotIn("err line 30", msg)             # stderr trimmed
        self.assertIn("40 more stderr lines", msg)
        self.assertIn("--verbose", msg)

    def test_verbose_shows_everything(self):
        asmdiff.VERBOSE = True
        try:
            with self.assertRaises(SystemExit) as ctx:
                asmdiff._compile_failure(self.CMD, self.STDERR)
        finally:
            asmdiff.VERBOSE = False
        msg = str(ctx.exception)
        self.assertIn("-I/inc0", msg)
        self.assertIn("err line 59", msg)

    def test_short_stderr_not_annotated_with_more(self):
        asmdiff.VERBOSE = False
        with self.assertRaises(SystemExit) as ctx:
            asmdiff._compile_failure(["gcc", "x.c"], "one error\n")
        self.assertNotIn("more stderr", str(ctx.exception))


class TestFormatCalls(unittest.TestCase):
    def test_short_list_unchanged(self):
        self.assertEqual(asmdiff.format_calls(["a", "b"]), "a, b")

    def test_empty_is_dash(self):
        self.assertEqual(asmdiff.format_calls([]), "-")

    def test_long_list_capped(self):
        calls = [f"fn{i}" for i in range(12)]
        out = asmdiff.format_calls(calls)
        self.assertTrue(out.endswith("Total Calls:12"))
        self.assertIn("fn7", out)
        self.assertNotIn("fn8,", out)


class TestTableMaxWidth(unittest.TestCase):
    """render_table(max_width=) fits rows to the terminal by trimming
    the last column only, whole callees at a time, replacing the tail
    with (or preserving) a Total Calls:N summary."""

    def test_no_max_width_keeps_long_cells(self):
        rows = [("function", "calls"),
                ("f", "alpha, bravo, charlie, delta")]
        out = asmdiff.render_table(rows)
        self.assertIn("alpha, bravo, charlie, delta", out)

    def test_trims_to_fit_with_total_summary(self):
        rows = [("function", "insns", "calls"),
                ("f", "5", "alpha, bravo, charlie, delta")]
        out = asmdiff.render_table(rows, max_width=37)
        self.assertIn("alpha, Total Calls:4", out)
        self.assertNotIn("bravo", out)
        self.assertTrue(all(len(l) <= 37 for l in out.splitlines()))

    def test_fitting_cell_left_alone(self):
        rows = [("function", "calls"), ("f", "alpha, bravo")]
        out = asmdiff.render_table(rows, max_width=80)
        self.assertIn("alpha, bravo", out)
        self.assertNotIn("Total Calls", out)

    def test_merges_existing_total_summary(self):
        rows = [("fn", "calls"), ("f", "a, b, c, Total Calls:12")]
        out = asmdiff.render_table(rows, max_width=21)
        self.assertIn("a, Total Calls:12", out)
        self.assertNotIn("b", out.splitlines()[1])

    def test_single_callee_never_dropped(self):
        rows = [("fn", "calls"), ("f", "very_long_single_symbol_name")]
        out = asmdiff.render_table(rows, max_width=10)
        self.assertIn("very_long_single_symbol_name", out)

    def test_dash_cell_untouched(self):
        rows = [("function_with_a_very_long_header", "calls"), ("f", "-")]
        out = asmdiff.render_table(rows, max_width=10)
        self.assertRegex(out.splitlines()[1], r"f\s+-")

    def test_inspect_table_passes_max_width(self):
        funcs = {"f": ["call\talpha", "call\tbravo",
                       "call\tcharlie", "call\tdelta", "ret"]}
        out = asmdiff.inspect_table(["f"], funcs, max_width=49)
        self.assertIn("alpha, Total Calls:4", out)
        self.assertNotIn("bravo", out)

    def test_table_width_is_terminal_width_on_tty(self):
        with mock.patch.object(asmdiff.sys.stdout, "isatty",
                               return_value=True), \
             mock.patch.object(asmdiff.shutil, "get_terminal_size",
                               return_value=os.terminal_size((100, 24))):
            self.assertEqual(asmdiff.table_width(), 100)

    def test_table_width_is_none_when_piped(self):
        with mock.patch.object(asmdiff.sys.stdout, "isatty",
                               return_value=False):
            self.assertIsNone(asmdiff.table_width())


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
        self.assertTrue(lines[0].endswith("calls"))
        self.assertRegex(lines[1], r"old_c\s+baseline\s+2\s+-\s+-")
        self.assertRegex(lines[2], r"new_c\s+candidate\s+1\s+-\s+ldexpf")

    def test_summary_table_loop_spans_column(self):
        funcs = {"a": [".L2:", "addl\t$1, %eax", "jne\t.L2"],
                 "b": ["ret"]}
        out = asmdiff.summary_table([("a", "b")], funcs)
        lines = out.splitlines()
        self.assertRegex(lines[1], r"a\s+baseline\s+2\s+\.L2:2\s+-")
        self.assertRegex(lines[2], r"b\s+candidate\s+1\s+-\s+-")

    def test_file_summary_totals_and_call_union(self):
        funcs = {"f": ["call\tmalloc", "ret"],
                 "g": [".L2:", "addl\t$1, %eax", "jne\t.L2",
                       "call\tmalloc", "call\tfree", "ret"]}
        out = asmdiff.file_summary_table(funcs)
        lines = out.splitlines()
        self.assertRegex(lines[1], r"f\s+2\s+-\s+malloc")
        self.assertRegex(lines[2], r"g\s+5\s+\.L2:2\s+malloc, free")
        self.assertRegex(lines[3],
                         r"TOTAL \(2 functions\)\s+7\s+-\s+malloc, free")


class TestInspectRendering(unittest.TestCase):
    def test_listing_layout(self):
        funcs = asmdiff.extract_functions(LOOP_ASM)
        lines = asmdiff.listing("looper", funcs["looper"]).splitlines()
        self.assertEqual(lines[0], "looper:")
        self.assertEqual(lines[1], "\txorl\t%eax, %eax")
        self.assertEqual(lines[2], ".L2:")   # local label back at column 0
        self.assertEqual(lines[3], "\taddl\t$1, %eax")

    def test_inspect_table_only_requested_functions(self):
        funcs = asmdiff.extract_functions(GCC_ASM)
        out = asmdiff.inspect_table(["new_const"], funcs)
        lines = out.splitlines()
        self.assertIn("function", lines[0])
        self.assertEqual(len(lines), 2)      # header + the one function
        self.assertRegex(lines[1], r"new_const\s+2\s+-\s+ldexpf")
        self.assertNotIn("TOTAL", out)

    def test_inspect_table_loop_spans(self):
        funcs = asmdiff.extract_functions(LOOP_ASM)
        out = asmdiff.inspect_table(["looper"], funcs)
        self.assertRegex(out.splitlines()[1], r"looper\s+5\s+\.L2:3\s+-")


class TestRunInspect(unittest.TestCase):
    """Layout selection in inspect mode, with compile_to_asm stubbed."""

    def _run(self, matrix, fn_names, layout=None):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: GCC_ASM
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                asmdiff.run_inspect("h.c", matrix, fn_names, layout,
                                    [], "/tmp")
        finally:
            asmdiff.compile_to_asm = real
        return out.getvalue()

    def test_one_compiler_plain_listing_and_stats(self):
        out = self._run(["gcc -O2"], ["new_const"])
        self.assertIn("new_const:", out)
        self.assertIn("\tjmp\tldexpf@PLT", out)
        self.assertIn("function", out)           # stats table present
        self.assertNotIn("==", out)              # no per-compiler header
        self.assertNotIn(" | ", out)             # not side-by-side

    def test_two_compilers_side_by_side(self):
        out = self._run(["gcc -O2", "clang -O2"], ["new_const"])
        self.assertIn("cc#1: gcc -O2", out)
        self.assertIn("== cc#1 vs cc#2 ==", out)
        self.assertIn(" | ", out)

    def test_three_compilers_sequential_blocks(self):
        out = self._run(["gcc -O1", "gcc -O2", "gcc -O3"], ["new_const"])
        self.assertEqual(out.count("== gcc -O"), 3)
        self.assertNotIn(" | ", out)

    def test_multiple_functions_listed(self):
        out = self._run(["gcc -O2"], ["old_const", "new_const"])
        self.assertIn("old_const:", out)
        self.assertIn("new_const:", out)

    def test_forced_list_with_two_compilers(self):
        out = self._run(["gcc -O2", "clang -O2"], ["new_const"],
                        layout="list")
        self.assertIn("== gcc -O2 ==", out)
        self.assertNotIn(" | ", out)

    def test_forced_side_by_side_with_three_compilers(self):
        out = self._run(["gcc -O1", "gcc -O2", "gcc -O3"], ["new_const"],
                        layout="side-by-side")
        self.assertIn("== cc#1 vs cc#2 ==", out)
        self.assertIn("== cc#1 vs cc#3 ==", out)

    def test_forced_side_by_side_with_one_compiler_errors(self):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: GCC_ASM
        try:
            with self.assertRaises(SystemExit) as ctx:
                with contextlib.redirect_stdout(io.StringIO()):
                    asmdiff.run_inspect("h.c", ["gcc -O2"], ["new_const"],
                                        "side-by-side", [], "/tmp")
        finally:
            asmdiff.compile_to_asm = real
        self.assertIn("side-by-side", str(ctx.exception))

    def test_unknown_function_lists_seen(self):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: GCC_ASM
        try:
            with self.assertRaises(SystemExit) as ctx:
                asmdiff.run_inspect("h.c", ["gcc -O2"], ["nope"],
                                    None, [], "/tmp")
        finally:
            asmdiff.compile_to_asm = real
        msg = str(ctx.exception)
        self.assertIn("not in asm: nope", msg)
        self.assertIn("old_const", msg)          # functions seen listed

    def test_no_usable_compiler_errors(self):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: None
        try:
            with self.assertRaises(SystemExit) as ctx:
                asmdiff.run_inspect("h.c", ["gcc -O2"], ["f"],
                                    None, [], "/tmp")
        finally:
            asmdiff.compile_to_asm = real
        self.assertIn("no usable compiler", str(ctx.exception))


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
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.c", Path(tmp) / "b.c"
            a.touch()
            b.touch()
            self._expect_error([str(a), str(b), "--pair", "x:y"],
                               "--pair compares within one file")

    def test_at_most_two_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = []
            for name in ("a.c", "b.c", "c.c"):
                p = Path(tmp) / name
                p.touch()
                argv.append(str(p))
            self._expect_error(argv + ["--across", "f"], "at most two")

    def test_across_one_file_needs_two_cc_entries(self):
        self._expect_error(["a.c", "--across", "f", "--cc", "gcc -O3"],
                           "at least two --cc")


class TestInspectValidation(unittest.TestCase):
    """CLI validation for the SOURCE.c FUNC inspect grammar."""

    def _expect_error(self, argv, fragment):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                asmdiff.main(argv)
        # parser.error writes to stderr; sys.exit carries the message
        self.assertIn(fragment, err.getvalue() + str(ctx.exception))

    def test_function_with_pair_rejected(self):
        self._expect_error(["x.c", "f", "--pair", "a:b"],
                           "cannot be combined")

    def test_function_with_across_rejected(self):
        self._expect_error(["x.c", "f", "--across", "g"],
                           "cannot be combined")

    def test_two_files_plus_function_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.c", Path(tmp) / "b.c"
            a.touch()
            b.touch()
            self._expect_error([str(a), str(b), "f"], "--across")

    def test_layout_without_function_rejected(self):
        self._expect_error(["x.c", "--layout", "list"],
                           "--layout only applies")

    def test_layout_value_checked(self):
        self._expect_error(["x.c", "f", "--layout", "diagonal"],
                           "invalid choice")

    def test_typo_filename_is_not_a_function(self):
        self._expect_error(["x.c", "typo.c"], "no such file")


class TestShortAliases(unittest.TestCase):
    """-p/-a/-db/-l are aliases of --pair/--across/--compile-commands/
    --layout: each hits that option's own validation."""

    def _expect_error(self, argv, fragment):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                asmdiff.main(argv)
        self.assertIn(fragment, err.getvalue() + str(ctx.exception))

    def test_p_is_pair(self):
        self._expect_error(["x.c", "-p", "nocolon"], "expects OLD:NEW")

    def test_a_is_across(self):
        self._expect_error(["x.c", "-a", "f", "--cc", "gcc -O3"],
                           "at least two --cc")

    def test_l_is_layout(self):
        self._expect_error(["x.c", "-l", "list"], "--layout only applies")

    def test_db_is_compile_commands(self):
        # Bare -db in a directory tree with no database is the
        # --compile-commands discovery error.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "a" / "b"
            cwd.mkdir(parents=True)
            (Path(tmp) / ".git").mkdir()     # stop the walk-up inside tmp
            with _inside(cwd):
                self._expect_error(["x.c", "f", "-db"],
                                   "no compile_commands.json")


class TestConfigEditing(unittest.TestCase):
    """--edit-config / --example-config and the embedded example."""

    _example_path = (Path(asmdiff.__file__).resolve().parent
                     / "asmdiff.example.toml")

    def test_example_constant_parses(self):
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        asmdiff.tomllib.loads(asmdiff.EXAMPLE_CONFIG)

    @unittest.skipUnless(_example_path.is_file(),
                         "asmdiff.example.toml only exists in a repo checkout")
    def test_example_constant_matches_repo_file(self):
        self.assertEqual(asmdiff.EXAMPLE_CONFIG,
                         self._example_path.read_text())

    def test_visual_beats_editor(self):
        self.assertEqual(
            asmdiff.resolve_editor({"VISUAL": "vim", "EDITOR": "nano"}),
            ["vim"])

    def test_editor_value_is_split(self):
        self.assertEqual(asmdiff.resolve_editor({"EDITOR": "code -w"}),
                         ["code", "-w"])

    def test_no_editor_is_an_error_on_posix(self):
        if os.name == "nt":
            self.skipTest("POSIX behaviour")
        with self.assertRaises(SystemExit):
            asmdiff.resolve_editor({})

    def test_notepad_fallback_on_windows(self):
        with mock.patch.object(asmdiff.os, "name", "nt"):
            self.assertEqual(asmdiff.resolve_editor({}), ["notepad"])

    def test_edit_creates_missing_config_from_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "asmdiff.toml"
            calls = []
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"VISUAL": "myedit"}), \
                 mock.patch.object(asmdiff.subprocess, "call",
                                   side_effect=lambda cmd:
                                       calls.append(cmd) or 7), \
                 contextlib.redirect_stderr(err):
                status = asmdiff.edit_config(str(target))
            self.assertEqual(status, 7)   # editor's status passes through
            self.assertEqual(target.read_text(), asmdiff.EXAMPLE_CONFIG)
            self.assertEqual(calls, [["myedit", str(target)]])
            self.assertIn("created", err.getvalue())
            self.assertNotIn("warning", err.getvalue())

    def test_edit_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "asmdiff.toml"
            target.write_text("default = 'gcc'\n")
            with mock.patch.dict(os.environ, {"VISUAL": "e"}), \
                 mock.patch.object(asmdiff.subprocess, "call",
                                   return_value=0):
                status = asmdiff.edit_config(str(target))
            self.assertEqual(status, 0)
            self.assertEqual(target.read_text(), "default = 'gcc'\n")

    def test_edit_warns_on_unparsable_result(self):
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "asmdiff.toml"
            target.write_text("not = valid = toml\n")
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"VISUAL": "e"}), \
                 mock.patch.object(asmdiff.subprocess, "call",
                                   return_value=0), \
                 contextlib.redirect_stderr(err):
                status = asmdiff.edit_config(str(target))
            self.assertEqual(status, 0)   # warn-only, status untouched
            self.assertIn("warning", err.getvalue())

    def test_edit_defaults_to_global_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with mock.patch.object(asmdiff.Path, "home",
                                   return_value=Path(tmp)), \
                 mock.patch.dict(os.environ, {"VISUAL": "e"}), \
                 mock.patch.object(asmdiff.subprocess, "call",
                                   return_value=0), \
                 contextlib.redirect_stderr(err):
                asmdiff.edit_config(None)
            self.assertTrue(
                (Path(tmp) / ".config" / "asmdiff.toml").is_file())

    def test_example_config_prints_constant(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = asmdiff.main(["--example-config"])
        self.assertEqual(status, 0)
        self.assertEqual(out.getvalue(), asmdiff.EXAMPLE_CONFIG)

    def test_no_sources_is_still_an_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
             self.assertRaises(SystemExit) as ctx:
            asmdiff.main([])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("SOURCE.c required", err.getvalue())


if __name__ == "__main__":
    unittest.main()
