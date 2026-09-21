#!/usr/bin/env python3
"""Unit tests for asmdiff.py.  Run: python3 tools/asmdiff/test_asmdiff.py -v"""
import contextlib
import difflib
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
        self.assertEqual(asmdiff.analyze(["j\t.L4"])[1], [])

    def test_register_indirect_calls_marked(self):
        self.assertEqual(asmdiff.analyze(["callx8\ta10"])[1],
                         ["indirect(a10)"])
        self.assertEqual(asmdiff.analyze(["jalr\ta5"])[1],
                         ["indirect(a5)"])
        self.assertEqual(asmdiff.analyze(["blx\tr3"])[1],
                         ["indirect(r3)"])

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

    def test_nested_span_depths(self):
        lines = [".L1:", "movl\t$0, %ecx", ".L2:", "addl\t$1, %ecx",
                 "cmpl\t$4, %ecx", "jne\t.L2", "decl\t%edi",
                 "jnz\t.L1", "ret"]
        ranges = asmdiff.loop_span_ranges(lines)
        self.assertEqual(asmdiff.span_depths(ranges), {".L1": 0, ".L2": 1})

    def test_zero_overhead_loop_nested_in_a_branch_loop(self):
        lines = [".L1:", "loopgt\ta3, .L5", "addi.n\ta2, a2, 1",
                 "s32i.n\ta2, a4, 0", ".L5:", "addi\ta6, a6, -1",
                 "bnez\ta6, .L1", "retw.n"]
        ranges = asmdiff.loop_span_ranges(lines)
        self.assertEqual(asmdiff.span_depths(ranges), {".L1": 0, ".L5": 1})

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
        self.assertEqual(calls,
                         ["__divsf3", "indirect(a9)", "indirect(a10)"])

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


class TestOffsetAnnotations(unittest.TestCase):
    """Nearest-symbol offset annotations (<_etext+0x100>) on targets
    outside the current function are noise - a pool address or literal
    value named after whatever symbol happens to precede it - and are
    rendered as raw hex.  Bare-symbol annotations and offsets into the
    function itself are real information and are kept."""

    def setUp(self):
        self.funcs = asmdiff.extract_functions_objdump(OBJDUMP_LONGCALL_ASM)

    def test_offset_annotations_become_raw_hex(self):
        # Pool address <_stext+0x104> and literal value <_etext+0x100>
        # both decorate unrelated symbols; neither name survives.
        self.assertIn("l32r\ta9, 0x40370104 (0x473b8000)",
                      self.funcs["render_partial"])

    def test_bare_symbol_annotation_kept(self):
        # The literal's value IS a symbol start (a real object): keep it.
        self.assertIn("l32r\ta8, 0x40370108 (amy_global)",
                      self.funcs["memberptr"])

    def test_own_function_offset_kept(self):
        # A branch into this function at a non-instruction address (a
        # mid-body gap objdump could not label) is accurate as fn+off;
        # a cross-symbol offset target is not, and becomes hex.
        dump = (
            "firmware.elf:     file format elf32-xtensa-le\n\n\n"
            "Disassembly of section .flash.text:\n\n"
            "40370400 <veneer>:\n"
            "40370400:\t004136        \tentry\ta1, 32\n"
            "40370403:\t56faff        \tbeqz\ta2, 40370409 <veneer+0x9>\n"
            "40370406:\t0c0a          \tblt\ta3, a4, 40380010 <other_fn+0x10>\n"
            "40370408:\tf01d          \tretw.n\n"
        )
        body = asmdiff.extract_functions_objdump(dump)["veneer"]
        self.assertIn("beqz\ta2, veneer+0x9", body)
        self.assertIn("blt\ta3, a4, 0x40380010", body)


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

    def test_filter_prints_table_without_listings(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, out = self._run([self._elf(tmp), "--filter", "render_",
                                     "--objdump", "od"])
        self.assertEqual(status, 0)
        self.assertIn("render_lut", out)
        self.assertIn("render_lpf_lut$constprop$0", out)
        self.assertNotIn("fx_mix", out)
        self.assertNotIn("entry\t", out)         # table only, no listings

    def test_filter_suppressed_listings_noted(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, out = self._run([self._elf(tmp), "--filter", "render_",
                                     "--objdump", "od"])
        self.assertEqual(status, 0)
        self.assertIn("summarized without listings", out)
        with tempfile.TemporaryDirectory() as tmp:
            _, out = self._run([self._elf(tmp), "render_lut",
                                "--objdump", "od"])
        self.assertNotIn("summarized without listings", out)

    def test_layout_list_prints_filter_listings(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, out = self._run([self._elf(tmp), "--filter", "render_",
                                     "-l", "list", "--objdump", "od"])
        self.assertEqual(status, 0)
        self.assertIn("entry\t", out)            # listings present
        self.assertIn("render_lut:", out)
        self.assertNotIn("summarized without listings", out)

    # (case, argv with ELF and SOURCE standing in for the two files the
    # case needs on disk, fragment the message must carry)
    ARGUMENT_ERRORS = [
        ("side-by-side layout",
         ["ELF", "--filter", "r", "-l", "side-by-side", "--objdump", "od"],
         "-l list"),
        ("misspelled function name",
         ["ELF", "rendr_lut", "--objdump", "od"], "render_lut"),
        ("neither a name nor a filter",
         ["ELF", "--objdump", "od"], "--filter"),
        ("filter matches nothing",
         ["ELF", "--filter", "zzz", "--objdump", "od"], "matched no function"),
        ("bad filter regex",
         ["ELF", "--filter", "(", "--objdump", "od"], "bad --filter regex"),
        ("compile-only flags",
         ["ELF", "f", "--pair", "a:b", "--objdump", "od"], "disassembled"),
        ("a second file",
         ["ELF", "SOURCE", "--objdump", "od"], "one binary"),
        ("filter with a pair",
         ["SOURCE", "--pair", "a:b", "--filter", "r"],
         "--pair/--across name their functions"),
        ("no gcc to derive an objdump from",
         ["ELF", "f", "--cc", "clang -O3"], "--objdump"),
    ]

    def test_elf_argument_errors(self):
        for case, argv, fragment in self.ARGUMENT_ERRORS:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / "b.c"
                    source.touch()
                    filled = [{"ELF": self._elf(tmp),
                               "SOURCE": str(source)}.get(arg, arg)
                              for arg in argv]
                    self._expect_error(filled, fragment)

    def test_names_and_filter_combine(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, out = self._run([self._elf(tmp), "fx_mix",
                                     "--filter", "render_lut$",
                                     "--objdump", "od"])
        self.assertEqual(status, 0)
        self.assertIn("fx_mix:", out)            # named: listed
        self.assertIn("render_lut", out)         # filtered: in the table

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


class TestCompileModeFilter(unittest.TestCase):
    """--filter in compile modes: matches are full peers of named
    functions in inspect mode, lenient across the matrix (a clone under
    one compiler only is a finding, not an error), and narrow the
    whole-file summary."""

    def _patch(self, per_cc):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: per_cc(cc)
        self.addCleanup(setattr, asmdiff, "compile_to_asm", real)

    def _capture(self, fn, *args, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args, **kw)
        return out.getvalue()

    def test_inspect_filter_selects_and_lists(self):
        self._patch(lambda cc: GCC_ASM)
        out = self._capture(asmdiff.run_inspect, "h.c", ["gcc -O2"], [],
                            None, [], "/tmp", filter_regex="_const")
        self.assertIn("old_const:", out)      # listing: full peer of a name
        self.assertIn("new_const:", out)

    def test_inspect_filter_lenient_across_matrix(self):
        self._patch(lambda cc: GCC_ASM if "gcc" in cc else CLANG_ASM)
        out = self._capture(asmdiff.run_inspect, "h.c",
                            ["gcc -O2", "clang -O2"], [],
                            "list", [], "/tmp", filter_regex="_const")
        self.assertIn("old_const:", out)      # gcc block has the clone
        self.assertIn("new_const:", out)      # both blocks have this one

    def test_inspect_filter_side_by_side_notes_one_sided(self):
        self._patch(lambda cc: GCC_ASM if "gcc" in cc else CLANG_ASM)
        out = self._capture(asmdiff.run_inspect, "h.c",
                            ["gcc -O2", "clang -O2"], [],
                            None, [], "/tmp", filter_regex="_const")
        self.assertIn("only one compiler", out)
        self.assertIn("old_const", out)

    def test_inspect_filter_no_match_errors(self):
        self._patch(lambda cc: GCC_ASM)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(io.StringIO()):
                asmdiff.run_inspect("h.c", ["gcc -O2"], [], None, [],
                                    "/tmp", filter_regex="zzz")

    def test_summary_filter_narrows_table(self):
        self._patch(lambda cc: GCC_ASM)
        out = self._capture(asmdiff.run_summary, ["a/h.c", "b/h.c"],
                            ["gcc -O2"], [], "/tmp", filter_regex="old_")
        self.assertIn("old_const", out)
        self.assertNotIn("new_const", out)

    def test_summary_filter_no_match_errors(self):
        self._patch(lambda cc: GCC_ASM)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(io.StringIO()):
                asmdiff.run_summary(["a/h.c", "b/h.c"], ["gcc -O2"], [],
                                    "/tmp", filter_regex="zzz")

    def test_main_routes_single_source_filter_to_inspect(self):
        self._patch(lambda cc: GCC_ASM)
        self.addCleanup(setattr, asmdiff, "JSON_OUT", None)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.c"
            src.touch()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                asmdiff.main([str(src), "--filter", "_const", "--json",
                              "--cc", "gcc -O2"])
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["mode"], "inspect")
        names = sorted(r["function"] for r in doc["results"])
        self.assertEqual(names, ["new_const", "old_const"])


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

    def test_positional_grammar(self):
        with tempfile.TemporaryDirectory() as tmp:
            second = Path(tmp) / "b.c"
            second.touch()
            # (positionals, sources, function names)
            cases = [
                (["a.c", str(second)], ["a.c", str(second)], []),
                (["a.c", "render_lut"], ["a.c"], ["render_lut"]),
                (["a.c", "f", "g"], ["a.c"], ["f", "g"]),
                # The first positional is the source whatever it looks like.
                (["no_suffix_name"], ["no_suffix_name"], []),
            ]
            for argv, sources, fns in cases:
                with self.subTest(argv=argv):
                    self.assertEqual(asmdiff.split_positionals(argv),
                                     (sources, fns))

    def test_path_like_positional_errors(self):
        # A suffix, a path separator, or an uppercase .S: a mistyped file
        # rather than a function name, and named as such.
        for argv in (["a.c", "typo.c"], ["a.c", "src/render"],
                     ["a.c", "startup.S"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    asmdiff.split_positionals(argv)
                self.assertIn("no such file", str(ctx.exception))


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

    # `default` is expanded exactly like -t, so the standing matrix can
    # be named by a group, a comma list, or a glob.
    GROUPED = {"groups": {"native": ["gcc", "clang"]},
               "gcc": {"cc": "gcc", "flags": ["-O3"]},
               "clang": {"cc": "clang", "flags": ["-O3"]},
               "esp32c3": {"cc": "riscv-gcc", "flags": ["-O2"]},
               "esp32c6": {"cc": "riscv-gcc", "flags": ["-Os"]}}

    # (config, its `default` value, the matrix it builds)
    DEFAULTS = [
        (CONFIG, "s3", ["xtensa-gcc -O2 -mlongcalls"]),
        (CONFIG, ["s3", "host"], ["xtensa-gcc -O2 -mlongcalls", "gcc -O3"]),
        (GROUPED, "native", ["gcc -O3", "clang -O3"]),
        (GROUPED, "esp32c*", ["riscv-gcc -O2", "riscv-gcc -Os"]),
        (GROUPED, ["esp32c3", "native"],
         ["riscv-gcc -O2", "gcc -O3", "clang -O3"]),
        (GROUPED, "clang,esp32c3", ["clang -O3", "riscv-gcc -O2"]),
    ]

    def test_config_default_expands_like_target(self):
        for config, default, matrix in self.DEFAULTS:
            with self.subTest(default=default):
                cfg = dict(config, default=default)
                self.assertEqual(asmdiff.build_matrix([], [], cfg, "c"),
                                 matrix)

    def test_fallback_is_bare_gcc_and_clang(self):
        self.assertEqual(asmdiff.build_matrix([], [], None, None),
                         ["gcc -O3", "clang -O3"])

    # (case, config, -t tokens, fragments the error must carry)
    CONFIG_ERRORS = [
        ("unknown default", dict(CONFIG, default="ghost"), [],
         ["no [ghost] target"]),
        ("default of the wrong type", dict(CONFIG, default=3), [],
         ["default"]),
        ("unknown target names the known ones", CONFIG, ["nope"],
         ["host", "s3"]),
        ("flags not an array", {"bad": {"cc": "gcc", "flags": "-O3"}},
         ["bad"], ["flags must be an array"]),
        ("target without cc", {"bad": {"flags": ["-O3"]}}, ["bad"],
         ['needs cc = "compiler"']),
    ]

    def test_matrix_config_errors(self):
        for case, cfg, targets, fragments in self.CONFIG_ERRORS:
            with self.subTest(case=case):
                with self.assertRaises(SystemExit) as ctx:
                    asmdiff.build_matrix([], targets, cfg, "cfg.toml")
                for fragment in fragments:
                    self.assertIn(fragment, str(ctx.exception))

    def test_costs_table_is_not_a_target(self):
        cfg = dict(self.CONFIG, costs={"bench": {"measured_on": "S3",
                                                 "method": "h"}})
        self.assertEqual(asmdiff.config_target_names(cfg), ["s3", "host"])
        with self.assertRaises(SystemExit):
            asmdiff.build_matrix([], ["costs"], cfg, "cfg.toml")

    def test_cc_naming_a_target_is_still_a_plain_command(self):
        # [gcc] exists in the example config; `--cc gcc` must stay a
        # verbatim compiler command, not a typo of `-t gcc`.
        cfg = {"gcc": {"cc": "gcc", "flags": ["-O3"]}}
        self.assertEqual(asmdiff.build_matrix(["gcc"], [], cfg, "c"),
                         ["gcc"])


class TestTargetGroups(unittest.TestCase):
    """-t NAME expands groups, comma lists, and globs over target names."""
    CONFIG = {"groups": {"esp": ["c3", "s3"], "s3": ["c3"]},
              "c3": {"cc": "riscv-gcc", "flags": ["-O2"]},
              "s3": {"cc": "xtensa-gcc", "flags": ["-Os"]},
              "host": {"cc": "gcc", "flags": ["-O3"]}}

    def expand(self, *tokens, config=None):
        return asmdiff.expand_target_args(list(tokens),
                                          config or self.CONFIG, "cfg.toml")

    # (-t tokens, the target names they expand to, in order)
    EXPANSIONS = [
        (["esp"], ["c3", "s3"]),                    # a group, as declared
        (["host,c3", "s3"], ["host", "c3", "s3"]),  # comma list then a name
        (["?3"], ["c3", "s3"]),                     # glob, in config order
        (["s3"], ["s3"]),                           # target beats the group
        (["[c,s]3"], ["c3", "s3"]),                 # comma inside a class
    ]

    def test_expansion_order(self):
        for tokens, expected in self.EXPANSIONS:
            with self.subTest(tokens=tokens):
                self.assertEqual(self.expand(*tokens), expected)

    def test_group_resolves_through_build_matrix(self):
        self.assertEqual(
            asmdiff.build_matrix([], ["esp"], self.CONFIG, "cfg.toml"),
            ["riscv-gcc -O2", "xtensa-gcc -Os"])

    # A name the README uses but a hand-written config lacks gets a
    # pointer at the built-in example config rather than a bare list.
    BARE = {"host": {"cc": "gcc", "flags": ["-O3"]}}

    # (case, config, -t tokens, fragments present, fragments absent)
    EXPANSION_ERRORS = [
        ("glob matching nothing", None, ["zz*"], ["matched no target"], []),
        ("unknown name lists what there is", None, ["nope"],
         ["targets: c3, s3, host", "groups: esp, s3"], []),
        ("group naming an undefined target",
         dict(CONFIG, groups={"bad": ["c3", "ghost"]}), ["bad"],
         ["ghost"], []),
        ("empty group", dict(CONFIG, groups={"none": []}), ["none"],
         ["non-empty"], []),
        ("group that is not an array",
         dict(CONFIG, groups={"bad": "c3"}), ["bad"], ["groups.bad"], []),
        ("empty token", None, [","], ["empty --target"], []),
        ("example group name", BARE, ["native"],
         ["native is a group in the built-in example config "
          "(asmdiff --example-config)"], []),
        ("example target name", BARE, ["esp32s3"],
         ["esp32s3 is a target in the built-in example config"], []),
        ("name in no config at all", BARE, ["nosuch"], [], ["example config"]),
    ]

    def test_expansion_errors(self):
        for case, config, tokens, present, absent in self.EXPANSION_ERRORS:
            with self.subTest(case=case):
                with self.assertRaises(SystemExit) as ctx:
                    self.expand(*tokens, config=config)
                msg = str(ctx.exception)
                for fragment in present:
                    self.assertIn(fragment, msg)
                for fragment in absent:
                    self.assertNotIn(fragment, msg)

    def test_pointer_is_skipped_without_tomllib(self):
        with mock.patch.object(asmdiff, "tomllib", None), \
                mock.patch.object(asmdiff, "_EXAMPLE_NAMES", None), \
                self.assertRaises(SystemExit) as ctx:
            self.expand("native", config=self.BARE)
        self.assertNotIn("example config", str(ctx.exception))

    def test_groups_table_is_not_a_target(self):
        self.assertEqual(asmdiff.config_target_names(self.CONFIG),
                         ["c3", "s3", "host"])


class TestListTargets(unittest.TestCase):
    """--list-targets prints the resolved config and needs no source."""

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = asmdiff.main(argv)
        return status, out.getvalue()

    def test_lists_default_groups_and_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "asmdiff.toml"
            cfg.write_text('default = ["c3", "host"]\n'
                           '[groups]\nesp = ["c3"]\n'
                           '[c3]\ncc = "riscv-gcc"\nflags = ["-O2"]\n'
                           '[host]\ncc = "gcc"\n')
            if asmdiff.tomllib is None:
                self.skipTest("tomllib requires Python >= 3.11")
            status, out = self._run(["--list-targets", "--config", str(cfg)])
        self.assertEqual(status, 0)
        self.assertIn(f"config: {cfg}", out)
        self.assertIn("default: c3, host", out)
        self.assertIn("  esp: c3", out)
        self.assertIn("  c3: riscv-gcc", out)
        self.assertIn("  host: gcc", out)
        self.assertNotIn("  groups: ", out)     # [groups] is not a target

    def test_lists_cost_profiles_with_where_they_were_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "asmdiff.toml"
            cfg.write_text('[costs.bench]\nmeasured_on = "S3 rev 0.2"\n'
                           'method = "cycle counter"\n'
                           '[c3]\ncc = "riscv-gcc"\n')
            if asmdiff.tomllib is None:
                self.skipTest("tomllib requires Python >= 3.11")
            status, out = self._run(["--list-targets", "--config", str(cfg)])
        self.assertEqual(status, 0)
        self.assertIn("costs:", out)
        self.assertIn("  bench: S3 rev 0.2", out)
        self.assertIn("  c3: riscv-gcc", out)
        self.assertNotIn("  costs: ", out)   # [costs] is not a target

    def test_without_a_config_names_the_fallback_matrix(self):
        status, out = self._run(["--list-targets"])
        self.assertEqual(status, 0)
        self.assertIn("no config file found", out)
        self.assertIn("gcc -O3", out)
        self.assertIn("clang -O3", out)


class TestIncludeFlags(unittest.TestCase):
    """Lifting include/define flags out of one recorded compile command."""

    # (case, tokens of the recorded command, directory, flags lifted)
    CASES = [
        ("glued and split include paths",
         ["cc", "-Iinc", "-I", "inc2", "-c", "a.c"], "/build",
         ["-I", "/build/inc", "-I", "/build/inc2"]),
        ("absolute path left alone", ["-I/abs/inc"], "/build",
         ["-I", "/abs/inc"]),
        ("defines glued and split",
         ["-DFOO=1", "-D", "BAR", "-UNDEBUG"], "/b",
         ["-DFOO=1", "-DBAR", "-UNDEBUG"]),
        ("system and forced include families",
         ["-isystem", "sys", "-iquote", "q", "-idirafter", "d",
          "-include", "cfg.h", "-imacros", "m.h"], "/build",
         ["-isystem", "/build/sys", "-iquote", "/build/q",
          "-idirafter", "/build/d", "-include", "/build/cfg.h",
          "-imacros", "/build/m.h"]),
        ("non-include flags and the source dropped",
         ["gcc", "-O2", "-std=c11", "-Wall", "-g", "-c", "a.c",
          "-o", "a.o", "-Iinc"], "/b", ["-I", "/b/inc"]),
        ("dangling flag at the end", ["-Iinc", "-I"], "/b",
         ["-I", "/b/inc"]),
        # -isystem must not be read as -I + "system".
        ("lowercase isystem", ["-isystem", "/s"], "", ["-isystem", "/s"]),
    ]

    def test_include_flag_families(self):
        for case, toks, directory, expected in self.CASES:
            with self.subTest(case=case):
                self.assertEqual(asmdiff.include_flags(toks, directory),
                                 expected)


class TestSpecsAndSysroot(unittest.TestCase):
    """Driver flags that change the header environment (-specs, --sysroot)."""

    # (case, tokens, directory, flags lifted)
    CASES = [
        # A bare specs name (no path separator) is found in the compiler's
        # own search dirs; gluing a directory onto it would break it.
        ("bare specs name", ["-specs=picolibc.specs"], "/build",
         ["-specs=picolibc.specs"]),
        ("specs with a path", ["-specs=./custom/my.specs"], "/build",
         ["-specs=/build/custom/my.specs"]),
        ("split and double-dash specs",
         ["-specs", "nano.specs", "--specs=nosys.specs"], "/b",
         ["-specs=nano.specs", "--specs=nosys.specs"]),
        ("sysroot glued and split",
         ["--sysroot=sr", "--sysroot", "/abs"], "/b",
         ["--sysroot=/b/sr", "--sysroot=/abs"]),
    ]

    def test_specs_and_sysroot_forms(self):
        for case, toks, directory, expected in self.CASES:
            with self.subTest(case=case):
                self.assertEqual(asmdiff.include_flags(toks, directory),
                                 expected)


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


class TestMissingCompilerNamed(unittest.TestCase):
    """The no-usable-compiler error names each missed compiler, so
    "--target rp2350 with no ARM toolchain" explains itself."""

    def _expect_exit(self, fn, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
             self.assertRaises(SystemExit) as ctx:
            fn(*args)
        return str(ctx.exception)

    def test_legend_marks_a_failed_row(self):
        asmdiff._FAILURES[:] = [("cc#2", "error: [cc#2] gcc failed on h.c")]
        self.addCleanup(asmdiff._FAILURES.clear)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asmdiff.print_legend([asmdiff.Target("gcc -O2"),
                                  asmdiff.Target("gcc -O3")])
        lines = out.getvalue().splitlines()
        self.assertTrue(any(ln.endswith("gcc -O3 (compile failed, see stderr)")
                            for ln in lines), lines)
        self.assertTrue(any(ln.endswith("gcc -O2") for ln in lines), lines)

    def test_legend_marks_a_skipped_row(self):
        asmdiff._SKIPPED_CCS[:] = ["nosuchcc: not found on PATH"]
        self.addCleanup(asmdiff._SKIPPED_CCS.clear)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asmdiff.print_legend([asmdiff.Target("gcc -O2"),
                                  asmdiff.Target("nosuchcc -O2")])
        lines = out.getvalue().splitlines()
        self.assertTrue(any("nosuchcc -O2 (not found on PATH, skipped)"
                            in ln for ln in lines))
        self.assertTrue(any(ln.endswith("gcc -O2") for ln in lines))

    def test_pairs_error_names_the_miss(self):
        msg = self._expect_exit(asmdiff.run_pairs, "h.c",
                                ["no-such-cc-xyz -O2"], [], [], "/tmp")
        self.assertIn("no usable compiler", msg)
        self.assertIn("no-such-cc-xyz: not found on PATH", msg)

    def test_inspect_error_names_the_miss(self):
        msg = self._expect_exit(asmdiff.run_inspect, "h.c",
                                ["no-such-cc-xyz -O2"], ["f"], None,
                                [], "/tmp")
        self.assertIn("no-such-cc-xyz: not found on PATH", msg)

    def test_across_two_cc_error_names_both(self):
        msg = self._expect_exit(asmdiff.run_across, ["h.c"],
                                ["no-such-cc-one -O2", "no-such-cc-two -O2"],
                                ["f"], [], "/tmp")
        self.assertIn("at least two usable", msg)
        self.assertIn("no-such-cc-one: not found on PATH", msg)
        self.assertIn("no-such-cc-two: not found on PATH", msg)


class TestDbIncludes(unittest.TestCase):
    """--db-includes: borrow only header-search paths from the database.

    A project's -I paths are target-portable; its defines, forced
    includes, and -specs are tied to the arch the database was built
    for - the host-target escape hatch."""

    def _db(self, tmp):
        path = Path(tmp) / "compile_commands.json"
        path.write_text(json.dumps([{
            "directory": tmp, "file": f"{tmp}/real.c",
            "command": ("cc -Iinc -isystem sys -iquote q -DESP_PLATFORM "
                        "-include sdkconfig.h -specs=picolibc.specs "
                        "-c real.c")}]))
        asmdiff._DB_CACHE.clear()
        asmdiff._MISS_NOTED.clear()
        asmdiff._BORROW_NOTED.clear()
        return str(path)

    def test_keeps_search_paths_drops_defines_and_specs(self):
        # Kept paths are re-emitted as -idirafter: searched AFTER the
        # host's system directories, so a database include dir that
        # overlays libc headers (ESP-IDF's esp_libc/platform_include)
        # cannot shadow the host's own stdio.h.
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            asmdiff.DB_INCLUDES = True
            self.addCleanup(setattr, asmdiff, "DB_INCLUDES", False)
            flags = asmdiff.compile_commands_flags(db, f"{tmp}/real.c")
            self.assertEqual(flags,
                             ["-idirafter", str(Path(tmp) / "inc"),
                              "-idirafter", str(Path(tmp) / "sys"),
                              "-idirafter", str(Path(tmp) / "q")])

    def test_flags_like_borrow_is_filtered_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            asmdiff.DB_INCLUDES = True
            asmdiff.FLAGS_LIKE = f"{tmp}/real.c"
            self.addCleanup(setattr, asmdiff, "DB_INCLUDES", False)
            self.addCleanup(setattr, asmdiff, "FLAGS_LIKE", None)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                flags = asmdiff.compile_commands_flags(
                    db, f"{tmp}/copy.c", missing_ok=True)
            self.assertIn("-idirafter", flags)
            self.assertNotIn("-DESP_PLATFORM", flags)

    def test_off_by_default_borrow_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            flags = asmdiff.compile_commands_flags(db, f"{tmp}/real.c")
            self.assertIn("-DESP_PLATFORM", flags)

    def test_main_wires_the_flag(self):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: GCC_ASM
        self.addCleanup(setattr, asmdiff, "compile_to_asm", real)
        self.addCleanup(setattr, asmdiff, "DB_INCLUDES", False)
        with contextlib.redirect_stdout(io.StringIO()):
            asmdiff.main(["h.c", "--db-includes", "--cc", "gcc -O2"])
        self.assertTrue(asmdiff.DB_INCLUDES)


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
    # (function names seen in the asm, hint offered)
    CASES = [
        (["_Z9old_scalef", "_Z9new_scalef"], True),
        (["scale", "helper"], False),
        (["_Z6renderv"], False),
    ]

    def test_mangled_pair_hint_cases(self):
        for names, expected in self.CASES:
            with self.subTest(names=names):
                self.assertEqual(bool(asmdiff.mangled_pair_hint(names)),
                                 expected)


class TestCompileFailureOutput(unittest.TestCase):
    """What a failed compile records for report_failures to print."""

    CMD = ["xtensa-gcc", "-O2"] + [f"-I/inc{i}" for i in range(50)] + ["a.c"]
    STDERR = "\n".join(f"err line {i}" for i in range(60))

    def setUp(self):
        self.addCleanup(asmdiff._FAILURES.clear)

    def _record(self, *args):
        self.assertIsNone(asmdiff._compile_failure(*args))
        self.assertEqual(len(asmdiff._FAILURES), 1)
        return asmdiff._FAILURES[0][1]

    def test_default_trims_flags_and_stderr(self):
        asmdiff.VERBOSE = False
        msg = self._record(self.CMD, self.STDERR)
        self.assertIn("xtensa-gcc failed on a.c", msg)
        self.assertNotIn("-I/inc0", msg)                 # no flag dump
        self.assertIn("err line 0", msg)
        self.assertNotIn("err line 30", msg)             # stderr trimmed
        self.assertIn("40 more stderr lines", msg)
        self.assertIn("--verbose", msg)

    def test_verbose_shows_everything(self):
        asmdiff.VERBOSE = True
        try:
            msg = self._record(self.CMD, self.STDERR)
        finally:
            asmdiff.VERBOSE = False
        self.assertIn("-I/inc0", msg)
        self.assertIn("err line 59", msg)

    def test_short_stderr_not_annotated_with_more(self):
        asmdiff.VERBOSE = False
        self.assertNotIn("more stderr", self._record(["gcc", "x.c"],
                                                     "one error\n"))

    def test_unlabelled_row_keeps_todays_message(self):
        asmdiff.VERBOSE = False
        msg = self._record(["gcc", "x.c"], "one error\n")
        self.assertTrue(msg.startswith("error: gcc failed on x.c"), msg)

    def test_label_names_the_row_it_came_from(self):
        asmdiff.VERBOSE = False
        msg = self._record(["gcc", "x.c"], "one error\n", "esp32s3")
        self.assertIn("error: [esp32s3] gcc failed on x.c", msg)
        self.assertEqual(asmdiff._FAILURES[0][0], "esp32s3")

    def test_report_prints_on_stderr_and_reports_any(self):
        asmdiff.VERBOSE = False
        self._record(["gcc", "x.c"], "one error\n", "esp32s3")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertTrue(asmdiff.report_failures())
        self.assertIn("[esp32s3] gcc failed on x.c", err.getvalue())
        asmdiff._FAILURES.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(asmdiff.report_failures())


class TestLabels(unittest.TestCase):
    """Every matrix row carries the name it is reported under."""

    CONFIG = {"default": "s3",
              "s3": {"cc": "xtensa-gcc", "flags": ["-O2"]},
              "host": {"cc": "gcc", "flags": ["-O3"]}}

    def labels(self, *args, **kw):
        return [row.label for row in asmdiff.build_matrix(*args, **kw)]

    # (case, --cc strings, -t names, config, labels)
    ROWS = [
        ("target rows take the config name", [], ["host"], CONFIG, ["host"]),
        ("the default entry too", [], [], CONFIG, ["s3"]),
        ("--cc rows are numbered by position", ["gcc -O2", "clang -O2"], [],
         None, ["cc#1", "cc#2"]),
        ("a lone --cc row keeps its command", ["gcc -O2"], [], None,
         ["gcc -O2"]),
        ("a mixed matrix numbers only the --cc rows", ["gcc -O2"], ["host"],
         CONFIG, ["cc#1", "host"]),
        ("the fallback matrix is numbered", [], [], None, ["cc#1", "cc#2"]),
    ]

    def test_matrix_row_labels(self):
        for case, ccs, targets, config, expected in self.ROWS:
            with self.subTest(case=case):
                self.assertEqual(
                    self.labels(ccs, targets, config,
                                "cfg.toml" if config else None),
                    expected)

    def test_label_survives_the_costs_override(self):
        cfg = dict(self.CONFIG,
                   costs={"bench": {"measured_on": "S3", "method": "h",
                                    "other": 1}})
        matrix = asmdiff.build_matrix([], ["host"], cfg, "cfg.toml",
                                      costs_arg="bench")
        self.assertEqual(matrix[0].label, "host")
        self.assertEqual(matrix[0].costs["name"], "bench")

    def test_bare_command_rows_are_labelled_on_the_fly(self):
        # A matrix that never went through build_matrix still renders.
        self.assertEqual(asmdiff.matrix_labels(["gcc -O2", "clang -O2"]),
                         ["cc#1", "cc#2"])
        self.assertEqual(asmdiff.matrix_labels(["gcc -O2"]), ["gcc -O2"])

    def _legend(self, rows):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asmdiff.print_legend(rows)
        return out.getvalue()

    def test_legend_per_matrix_shape(self):
        # A lone --cc row is its own legend, so printing one would only
        # repeat the command line; every other shape resolves a label.
        cases = [
            ("lone --cc row", ["gcc -O2"], [], None, []),
            ("lone target row", [], ["host"], self.CONFIG, ["host: gcc -O3"]),
            ("two --cc rows", ["gcc -O2", "clang -O2"], [], None,
             ["cc#1: gcc -O2", "cc#2: clang -O2"]),
        ]
        for case, ccs, targets, config, expected in cases:
            with self.subTest(case=case):
                rows = asmdiff.build_matrix(ccs, targets, config,
                                            "cfg.toml" if config else None)
                legend = self._legend(rows)
                if not expected:
                    self.assertEqual(legend, "")
                for line in expected:
                    self.assertIn(line, legend)


class TestCollectedFailures(unittest.TestCase):
    """A row that fails to compile is reported after the run, not
    instead of it."""

    def setUp(self):
        self.addCleanup(asmdiff._FAILURES.clear)

    def _fake_run(self, cmd, **kw):
        """Stand in for the compiler: clang fails, gcc writes GCC_ASM."""
        if cmd[0] == "clang":
            return mock.Mock(returncode=1, stderr="boom: broken\n")
        Path(cmd[cmd.index("-o") + 1]).write_text(GCC_ASM)
        return mock.Mock(returncode=0, stderr="")

    @contextlib.contextmanager
    def _compilers(self):
        with mock.patch.object(asmdiff.shutil, "which",
                               return_value="/usr/bin/cc"), \
             mock.patch.object(asmdiff.subprocess, "run",
                               side_effect=self._fake_run):
            yield

    def test_compile_to_asm_records_the_row_and_returns_none(self):
        row = asmdiff.Target("clang -O2", label="esp32s3")
        with self._compilers():
            self.assertIsNone(asmdiff.compile_to_asm(row, [], "h.c", "/tmp"))
        self.assertEqual(asmdiff._FAILURES[0][0], "esp32s3")
        self.assertIn("[esp32s3] clang failed on h.c",
                      asmdiff._FAILURES[0][1])

    def test_lone_row_failure_is_unlabelled(self):
        row = asmdiff.Target("clang -O2", label="clang -O2")
        with self._compilers():
            asmdiff.compile_to_asm(row, [], "h.c", "/tmp")
        self.assertIsNone(asmdiff._FAILURES[0][0])

    def test_failed_row_keeps_the_others_table(self):
        matrix = asmdiff.build_matrix(["gcc -O2", "clang -O2"], [],
                                      None, None)
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, self._compilers(), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            status = asmdiff.run_pairs("h.c", matrix, [], [], tmp)
        self.assertEqual(status, 1)
        self.assertIn("old_const", out.getvalue())       # cc#1 reported
        self.assertNotIn("cc#2 ==", out.getvalue())      # cc#2 has no table
        self.assertIn("[cc#2] clang failed on h.c", err.getvalue())

    def test_single_failing_row_keeps_todays_message(self):
        matrix = asmdiff.build_matrix(["clang -O2"], [], None, None)
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, self._compilers(), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as ctx:
            asmdiff.run_pairs("h.c", matrix, [], [], tmp)
        self.assertIn("error: clang failed on h.c", err.getvalue())
        self.assertNotIn("[", err.getvalue())            # nothing to label
        self.assertIn("no usable compiler", str(ctx.exception))

    def test_json_run_keeps_stdout_pure_and_exits_one(self):
        matrix = asmdiff.build_matrix(["gcc -O2", "clang -O2"], [],
                                      None, None)
        self.addCleanup(setattr, asmdiff, "JSON_OUT", None)
        asmdiff.JSON_OUT = []
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, self._compilers(), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            status = asmdiff.run_pairs("h.c", matrix, [], [], tmp)
        self.assertEqual(status, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual({r["target"] for r in asmdiff.JSON_OUT}, {"cc#1"})
        self.assertIn("clang failed", err.getvalue())


class TestFormatCalls(unittest.TestCase):
    def test_callee_cell_cases(self):
        # (case, callees, the cell they render as)
        cases = [
            ("short list unchanged", ["a", "b"], "a, b"),
            ("no callees", [], "-"),
            ("long list capped, the count kept",
             [f"fn{i}" for i in range(12)],
             "fn0, fn1, fn2, fn3, fn4, fn5, fn6, fn7, ... (12 total)"),
        ]
        for case, calls, expected in cases:
            with self.subTest(case=case):
                self.assertEqual(asmdiff.format_calls(calls), expected)


class TestTableMaxWidth(unittest.TestCase):
    """render_table(max_width=) fits rows to the terminal by trimming
    the last column only, whole callees at a time, replacing the tail
    with (or preserving) a "... (N total)" elision marker."""

    def test_no_max_width_keeps_long_cells(self):
        rows = [("function", "calls"),
                ("f", "alpha, bravo, charlie, delta")]
        out = asmdiff.render_table(rows)
        self.assertIn("alpha, bravo, charlie, delta", out)

    def test_trims_to_fit_with_total_summary(self):
        rows = [("function", "insns", "calls"),
                ("f", "5", "alpha, bravo, charlie, delta")]
        out = asmdiff.render_table(rows, max_width=37)
        self.assertIn("alpha, ... (4 total)", out)
        self.assertNotIn("bravo", out)
        self.assertTrue(all(len(l) <= 37 for l in out.splitlines()))

    def test_fitting_cell_left_alone(self):
        rows = [("function", "calls"), ("f", "alpha, bravo")]
        out = asmdiff.render_table(rows, max_width=80)
        self.assertIn("alpha, bravo", out)
        self.assertNotIn("total)", out)

    def test_merges_existing_total_summary(self):
        rows = [("fn", "calls"), ("f", "a, b, c, ... (12 total)")]
        out = asmdiff.render_table(rows, max_width=21)
        self.assertIn("a, ... (12 total)", out)
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
        out = asmdiff.inspect_table([asmdiff.Block("gcc", funcs, ["f"])],
                                    max_width=49)
        self.assertIn("alpha, ... (4 total)", out)
        self.assertNotIn("bravo", out)


class TestWidth(unittest.TestCase):
    """--width, the width tables fall back to off a terminal, and the
    caps that hang off that budget."""

    def setUp(self):
        self.addCleanup(setattr, asmdiff, "WIDTH", None)

    def _table_width(self, width, tty, columns):
        asmdiff.WIDTH = width
        env = {} if columns is None else {"COLUMNS": columns}
        with mock.patch.dict(asmdiff.os.environ, env, clear=True), \
             mock.patch.object(asmdiff.sys.stdout, "isatty",
                               return_value=tty), \
             mock.patch.object(asmdiff.shutil, "get_terminal_size",
                               return_value=os.terminal_size((90, 24))):
            return asmdiff.table_width()

    def test_width_resolution_order(self):
        cases = [
            # (--width, stdout is a tty, $COLUMNS, budget)
            (None, True, None, 90),      # the terminal
            (None, False, "100", 100),   # what the pipe's reader said
            (None, True, "100", 90),     # a real terminal outranks it
            (None, False, None, 120),    # nothing to go on
            (None, False, "wide", 120),  # nor anything usable
            (200, True, None, 200),      # --width outranks the terminal
            (0, False, "100", None),     # 0 is unlimited
        ]
        for width, tty, columns, expected in cases:
            with self.subTest(width=width, tty=tty, columns=columns):
                self.assertEqual(self._table_width(width, tty, columns),
                                 expected)

    def test_listing_width_halves_the_budget(self):
        # A side-by-side pair gets half the table, less the " | ", and
        # never narrows past the floor.
        for width, expected in [(200, 98),
                                (60, asmdiff.MIN_LISTING_WIDTH),
                                (0, asmdiff.MIN_LISTING_WIDTH)]:
            with self.subTest(width=width):
                asmdiff.WIDTH = width
                self.assertEqual(asmdiff.listing_width(), expected)

    def test_span_column_caps_with_a_count(self):
        spans = [(f".L{n}", n + 1) for n in range(7)]
        out = asmdiff.format_spans(spans)
        self.assertIn(".L5:6", out)
        self.assertNotIn(".L6", out)
        self.assertTrue(out.endswith("+1 more"))

    def test_span_column_within_the_cap_is_whole(self):
        spans = [(f".L{n}", n + 1) for n in range(6)]
        out = asmdiff.format_spans(spans)
        self.assertIn(".L5:6", out)
        self.assertNotIn("more", out)

    def test_main_wires_the_flag(self):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: GCC_ASM
        self.addCleanup(setattr, asmdiff, "compile_to_asm", real)
        with contextlib.redirect_stdout(io.StringIO()):
            asmdiff.main(["h.c", "--width", "60", "--cc", "gcc -O2"])
        self.assertEqual(asmdiff.WIDTH, 60)

    def test_width_argument_is_a_budget_or_a_usage_error(self):
        # USAGE: argparse rejects the value; anything else is the
        # budget the renderers get.
        USAGE = object()
        cases = [("0", 0), ("1", 1), ("200", 200),
                 ("-1", USAGE), ("-200", USAGE), ("wide", USAGE)]
        for value, expected in cases:
            with self.subTest(value=value):
                if expected is USAGE:
                    with self.assertRaises(asmdiff.argparse.ArgumentTypeError):
                        asmdiff.width_arg(value)
                else:
                    self.assertEqual(asmdiff.width_arg(value), expected)

    def test_negative_width_exits_with_the_usage_status(self):
        with contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit) as ctx:
            asmdiff.main(["h.c", "--width", "-1", "--cc", "gcc -O2"])
        self.assertEqual(ctx.exception.code, 2)


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

    def test_target_true_with_no_db_anywhere_is_a_note(self):
        # The config describes where the target is usually compiled; a
        # run from outside that project still gets its compilers.
        cfg = {"t": {"cc": "gcc", "compile_commands": True}}
        err = io.StringIO()
        with mock.patch.object(asmdiff, "find_compile_commands",
                               return_value=None), \
                contextlib.redirect_stderr(err):
            matrix = asmdiff.build_matrix([], ["t"], cfg, "c")
        self.assertEqual(matrix, ["gcc"])
        self.assertIsNone(matrix[0].compile_commands)
        self.assertFalse(matrix[0].db_discovered)
        note = err.getvalue()
        self.assertEqual(note.count("compile_commands.json"), 1)
        self.assertIn("target [t]", note)
        self.assertIn("without borrowed flags", note)

    def test_cli_bare_flag_with_no_db_anywhere_still_errors(self):
        # --compile-commands asks for a database in this run, so an
        # empty search is an error rather than a note.
        with mock.patch.object(asmdiff, "find_compile_commands",
                               return_value=None), \
                self.assertRaises(SystemExit) as ctx:
            asmdiff.build_matrix(["gcc -O3"], [], None, None, db_arg=True)
        self.assertIn("--compile-commands", str(ctx.exception))

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
                asmdiff.announce_glob_choices()
            # numeric sort: 15 > 13 > 9 (lexically "esp-9" would win)
            self.assertEqual(resolved, f"{tmp}/esp-15.2.0/bin/xgcc")
            self.assertIn("target [t]: cc pattern matched 3 toolchains",
                          err.getvalue())

    def test_glob_choices_sharing_a_directory_announce_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            for ver in ("esp-13.2.0", "esp-15.2.0"):
                d = Path(tmp) / ver / "bin"
                d.mkdir(parents=True)
                for chip in ("esp32", "esp32s3"):
                    (d / f"xtensa-{chip}-elf-gcc").touch()
            cfg = {"esp32": {"cc": f"{tmp}/esp-*/bin/xtensa-esp32-elf-gcc"},
                   "esp32s3": {"cc": f"{tmp}/esp-*/bin/xtensa-esp32s3-elf-gcc"}}
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                matrix = asmdiff.build_matrix([], ["esp32", "esp32s3"], cfg, "c")
            self.assertEqual(len(matrix), 2)
            lines = err.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0],
                             "targets [esp32, esp32s3]: cc patterns matched "
                             f"2 toolchains, using {tmp}/esp-15.2.0/bin/")
            self.assertEqual(asmdiff._GLOB_CHOICES, [])

    def test_glob_single_match_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "esp-15.2.0" / "bin"
            d.mkdir(parents=True)
            (d / "xgcc").touch()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                resolved = asmdiff.resolve_cc(f"{tmp}/esp-*/bin/xgcc", "t")
                asmdiff.announce_glob_choices()
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
    def test_side_by_side_drops_trailing_comments(self):
        cases = [
            # (line, width, what survives, what the comment took away)
            ("jmp\tldexpf@PLT # TAILCALL", 20, "ldexpf@PLT", "#"),
            ("movss\t8(%rsp), %xmm0 # 8-byte Reload", 24, "%xmm0", "#"),
            # An ARM immediate has no space after the hash.
            ("mov\tr0, #4", 20, "r0, #4", None),
        ]
        for line, width, kept, dropped in cases:
            with self.subTest(line=line):
                out = asmdiff.side_by_side([line], ["ret"], "L", "R",
                                           width=width)
                self.assertIn(kept, out)
                if dropped:
                    self.assertNotIn(dropped, out)

    def test_single_column_listing_keeps_the_comment(self):
        out = asmdiff.listing("f", ["jmp\tldexpf@PLT # TAILCALL"])
        self.assertIn("# TAILCALL", out)

    def test_pairs_mode_without_pairs_falls_back_to_summary(self):
        asm = ".text\nlonely:\n\tret\n\t.size lonely, .-lonely\n"
        saved = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: asm
        self.addCleanup(setattr, asmdiff, "compile_to_asm", saved)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asmdiff.run_pairs("h.c", ["gcc -O2"], [], [], "/tmp")
        self.assertIn("TOTAL (1 functions)", out.getvalue())

    def test_several_rows_lead_with_a_target_column(self):
        funcs = {"f": ["ret"]}
        out = asmdiff.file_summary_table(
            [asmdiff.Block("gcc", funcs, "a.c"),
             asmdiff.Block("esp32s3", funcs, "a.c")]).splitlines()
        self.assertRegex(out[0], r"^target\s+file\s+function\s+insns")
        self.assertRegex(out[1], r"^gcc\s+a\.c\s+f\s+")
        self.assertRegex(out[3], r"^esp32s3\s+a\.c\s+f\s+")

    def test_empty_block_keeps_its_line(self):
        out = asmdiff.file_summary_table(
            [asmdiff.Block("gcc", {"f": ["ret"]}, "a.c"),
             asmdiff.Block("gcc", {}, "b.c")]).splitlines()
        self.assertRegex(out[-1], r"^b\.c\s+\(none\)")

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

    def test_three_compilers_sequential_blocks(self):
        out = self._run(["gcc -O1", "gcc -O2", "gcc -O3"], ["new_const"])
        self.assertEqual(out.count("== cc#"), 3)   # one block per label
        self.assertIn("cc#3: gcc -O3", out)        # the legend resolves it
        self.assertNotIn(" | ", out)

    def test_multiple_functions_listed(self):
        out = self._run(["gcc -O2"], ["old_const", "new_const"])
        self.assertIn("old_const:", out)
        self.assertIn("new_const:", out)

    def test_forced_list_with_two_compilers(self):
        out = self._run(["gcc -O2", "clang -O2"], ["new_const"],
                        layout="list")
        self.assertIn("cc#1: gcc -O2", out)
        self.assertIn("== cc#1 ==", out)
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


# Pair whose bodies share a long identical prefix - the collapse fixture.
PADDED_ASM = """\
\t.text
\t.globl\told_pad
\t.type\told_pad, @function
old_pad:
\tmovl\t$1, %eax
\tmovl\t$2, %eax
\tmovl\t$3, %eax
\tmovl\t$4, %eax
\tmovl\t$5, %eax
\tmovl\t$6, %eax
\tmovl\t$7, %eax
\tmovl\t$8, %eax
\taddl\t$9, %eax
\tret
\t.size\told_pad, .-old_pad
\t.globl\tnew_pad
\t.type\tnew_pad, @function
new_pad:
\tmovl\t$1, %eax
\tmovl\t$2, %eax
\tmovl\t$3, %eax
\tmovl\t$4, %eax
\tmovl\t$5, %eax
\tmovl\t$6, %eax
\tmovl\t$7, %eax
\tmovl\t$8, %eax
\tsubl\t$9, %eax
\tret
\t.size\tnew_pad, .-new_pad
"""


class TestCollapse(unittest.TestCase):
    """--collapse elides identical runs in side-by-side listings,
    keeping COLLAPSE_CONTEXT lines around each differing pair."""

    def test_identical_run_elided_with_context(self):
        left = [f"insn{n}" for n in range(20)]
        right = list(left)
        right[10] = "DIFF"
        out = asmdiff.side_by_side(left, right, "L", "R", collapse=True)
        self.assertIn("... 7 identical lines ...", out)   # 0..6 elided
        self.assertNotIn("insn2 ", out.replace("|", " "))
        self.assertIn("insn7", out)                       # context kept
        self.assertIn("DIFF", out)
        self.assertIn("insn13", out)                      # context kept
        self.assertIn("... 6 identical lines ...", out)   # 14..19 elided

    def test_fully_identical_pair_collapses_to_its_header(self):
        lines = [f"insn{n}" for n in range(10)]
        out = asmdiff.side_by_side(lines, lines, "L", "R", collapse=True)
        self.assertEqual(len(out.splitlines()), 2)   # titles and the rule
        self.assertIn("L (identical)", out)
        self.assertNotIn("insn5", out)

    def test_identical_pair_without_collapse_still_prints(self):
        lines = [f"insn{n}" for n in range(10)]
        out = asmdiff.side_by_side(lines, lines, "L", "R")
        self.assertNotIn("(identical)", out)
        self.assertIn("insn5", out)

    def test_insertion_does_not_desync_collapse(self):
        # Positional pairing would leave every pair after an inserted
        # line unequal; collapse aligns the sides first, so the tail
        # still elides.
        left = [f"insn{n}" for n in range(20)]
        right = left[:10] + ["EXTRA"] + left[10:]
        out = asmdiff.side_by_side(left, right, "L", "R", collapse=True)
        self.assertIn("EXTRA", out)
        self.assertEqual(out.count("... 7 identical lines ..."), 2)

    def test_collapse_off_by_default(self):
        lines = ["alpha", "beta"]
        out = asmdiff.side_by_side(lines, lines, "L", "R")
        self.assertNotIn("identical lines", out)
        self.assertIn("alpha", out)

    def test_main_wires_the_flag_for_pairs(self):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: PADDED_ASM
        self.addCleanup(setattr, asmdiff, "compile_to_asm", real)
        self.addCleanup(setattr, asmdiff, "COLLAPSE", False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asmdiff.main(["h.c", "--collapse", "--cc", "gcc -O2"])
        text = out.getvalue()
        # context is 3: identical 0..7, diff at 8, keep 5..9, elide 0..4
        self.assertIn("... 5 identical lines ...", text)
        self.assertNotIn("$1,", text)
        self.assertIn("addl", text)
        self.assertIn("subl", text)

    def test_main_wires_the_flag_for_across(self):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: PADDED_ASM
        self.addCleanup(setattr, asmdiff, "compile_to_asm", real)
        self.addCleanup(setattr, asmdiff, "COLLAPSE", False)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.c", Path(tmp) / "b.c"
            a.touch()
            b.touch()
            with contextlib.redirect_stdout(out):
                asmdiff.main([str(a), str(b), "-a", "old_pad", "--collapse",
                              "--cc", "gcc -O2"])
        # both sides compile to the same asm: nothing left to list
        self.assertIn("(identical)", out.getvalue())


class TestJsonOutput(unittest.TestCase):
    """--json: machine-readable summary records instead of tables."""

    def setUp(self):
        self._compile = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: GCC_ASM
        self.addCleanup(setattr, asmdiff, "compile_to_asm", self._compile)
        self.addCleanup(setattr, asmdiff, "JSON_OUT", None)
        self.addCleanup(setattr, asmdiff, "SUMMARY_ONLY", False)
        self.addCleanup(setattr, asmdiff, "SPAN_STATS", False)
        self.addCleanup(setattr, asmdiff, "COST", False)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asmdiff.main(argv)
        return json.loads(out.getvalue())   # stdout must be pure JSON

    def test_pairs_mode_records(self):
        doc = self._run(["h.c", "--json", "--cc", "gcc -O2"])
        self.assertEqual(doc["asmdiff"], asmdiff.__version__)
        self.assertEqual(doc["mode"], "pairs")
        old, new = doc["results"]
        self.assertEqual(old["function"], "old_const")
        self.assertEqual(old["role"], "baseline")
        self.assertEqual(old["cc"], "gcc -O2")
        self.assertEqual(old["target"], "gcc -O2")   # lone row: its command
        self.assertEqual(old["insns"], 2)
        self.assertEqual(new["function"], "new_const")
        self.assertEqual(new["role"], "candidate")
        self.assertEqual(new["calls"], ["ldexpf"])
        self.assertNotIn("delta", old)  # the baseline has no counterpart
        self.assertEqual(new["delta"], {"insns": 0,
                                        "calls_added": ["ldexpf"],
                                        "calls_removed": []})

    def test_across_two_files_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.c", Path(tmp) / "b.c"
            a.touch()
            b.touch()
            doc = self._run([str(a), str(b), "-a", "old_const",
                             "--json", "--cc", "gcc -O2"])
        self.assertEqual(doc["mode"], "across")
        base, cand = doc["results"]
        self.assertEqual(base["tag"], "a.c")
        self.assertEqual(base["role"], "baseline")
        self.assertEqual(base["target"], "gcc -O2")
        self.assertEqual(cand["tag"], "b.c")
        self.assertEqual(cand["role"], "candidate")
        self.assertEqual(cand["target"], "gcc -O2")

    def test_inspect_records_and_no_listing(self):
        doc = self._run(["h.c", "new_const", "--json", "--cc", "gcc -O2"])
        self.assertEqual(doc["mode"], "inspect")
        self.assertEqual(len(doc["results"]), 1)
        rec = doc["results"][0]
        self.assertEqual(rec["function"], "new_const")
        self.assertEqual(rec["target"], "gcc -O2")
        self.assertNotIn("role", rec)
        self.assertNotIn("delta", rec)   # nothing to pair it with

    def test_two_file_summary_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.c", Path(tmp) / "b.c"
            a.touch()
            b.touch()
            doc = self._run([str(a), str(b), "--json", "--cc", "gcc -O2"])
        self.assertEqual(doc["mode"], "summary")
        tags = {(r["tag"], r["function"]) for r in doc["results"]}
        self.assertIn(("a.c", "old_const"), tags)
        self.assertIn(("b.c", "new_const"), tags)
        self.assertEqual({r["target"] for r in doc["results"]},
                         {"gcc -O2"})

    def test_span_stats_key_present_only_when_asked(self):
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: LOOP_ASM
        doc = self._run(["h.c", "looper", "--json", "--span-stats",
                         "--cc", "gcc -O2"])
        rec = doc["results"][0]
        self.assertEqual(rec["loop_spans"],
                         [{"label": ".L2", "insns": 3, "depth": 0}])
        self.assertEqual(rec["span_stats"],
                         [{"label": ".L2", "depth": 0, "insns": 3,
                           "load": 0, "store": 0, "mul": 0, "div": 0,
                           "branch": 1, "other": 2}])
        doc = self._run(["h.c", "looper", "--json", "--cc", "gcc -O2"])
        self.assertNotIn("span_stats", doc["results"][0])

    def test_cost_object_present_only_when_asked(self):
        doc = self._run(["h.c", "--json", "--cost", "--cc", "gcc -O2"])
        old, new = doc["results"]
        self.assertEqual(old["cost"]["classes"]["mul"], 1)
        self.assertEqual(old["cost"]["tiers"], {})
        self.assertEqual(new["cost"]["tiers"], {"libm": 1})
        self.assertEqual(new["cost"]["tiers_in_loop"], {})
        self.assertIsNone(new["cost"]["score"])
        self.assertIsNone(new["cost"]["profile"])
        self.assertEqual(new["cost"]["unweighted"], 3)   # 2 insns + 1 site
        self.assertEqual(new["delta"]["cost"],
                         {"classes": {"mul": -1, "other": 1},
                          "tiers": {"libm": 1}, "score": None})
        doc = self._run(["h.c", "--json", "--cc", "gcc -O2"])
        self.assertNotIn("cost", doc["results"][0])
        self.assertNotIn("cost", doc["results"][1]["delta"])

    def test_elf_mode_records(self):
        real = asmdiff.run_objdump
        asmdiff.run_objdump = lambda objdump, elf: OBJDUMP_ASM
        self.addCleanup(setattr, asmdiff, "run_objdump", real)
        with tempfile.TemporaryDirectory() as tmp:
            elf = Path(tmp) / "fw.elf"
            elf.write_bytes(b"\x7fELF" + b"\0" * 12)
            doc = self._run([str(elf), "render_lut", "--json",
                             "--objdump", "od"])
        self.assertEqual(doc["mode"], "elf")
        self.assertEqual(doc["elf"], str(elf))
        names = [r["function"] for r in doc["results"]]
        self.assertEqual(names, ["render_lut"])
        # No matrix row stands behind a linked binary, so no target.
        self.assertNotIn("target", doc["results"][0])

    def test_multi_compiler_matrix_tags_each_record(self):
        doc = self._run(["h.c", "--json", "--cc", "gcc -O2",
                         "--cc", "gcc -O3"])
        ccs = {r["cc"] for r in doc["results"]}
        self.assertEqual(ccs, {"gcc -O2", "gcc -O3"})
        self.assertEqual({r["target"] for r in doc["results"]},
                         {"cc#1", "cc#2"})
        self.assertEqual(len(doc["results"]), 4)


class TestDelta(unittest.TestCase):
    """The delta row closing each pair, the JSON delta object, and
    --fail-on-growth's exit status."""

    # old_rt calls exp2f in a loop; new_rt is the rewrite that folded
    # the loop away and reaches ldexpf instead.
    PAIR_ASM = """\
\t.globl\told_rt
\t.type\told_rt, @function
old_rt:
\tmovl\t$0, %eax
.L2:
\tcall\texp2f
\taddl\t$1, %eax
\tcmpl\t$8, %eax
\tjne\t.L2
\tret
\t.size\told_rt, .-old_rt
\t.globl\tnew_rt
\t.type\tnew_rt, @function
new_rt:
\tmovl\t$-5, %edi
\tjmp\tldexpf@PLT
\t.size\tnew_rt, .-new_rt
"""

    # The same pair the other way round: the candidate is two
    # instructions bigger than the baseline.
    GROW_ASM = """\
\t.globl\told_rt
\t.type\told_rt, @function
old_rt:
\tret
\t.size\told_rt, .-old_rt
\t.globl\tnew_rt
\t.type\tnew_rt, @function
new_rt:
\tmovl\t$0, %eax
\taddl\t$1, %eax
\tret
\t.size\tnew_rt, .-new_rt
"""

    ONE_INSN_ASM = """\
\t.globl\trt
\t.type\trt, @function
rt:
\tret
\t.size\trt, .-rt
"""

    THREE_INSN_ASM = """\
\t.globl\trt
\t.type\trt, @function
rt:
\tmovl\t$0, %eax
\taddl\t$1, %eax
\tret
\t.size\trt, .-rt
"""

    def _patch_compile(self, fn):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = fn
        self.addCleanup(setattr, asmdiff, "compile_to_asm", real)
        self.addCleanup(setattr, asmdiff, "FAIL_ON_GROWTH", False)
        self.addCleanup(setattr, asmdiff, "JSON_OUT", None)
        self.addCleanup(setattr, asmdiff, "SUMMARY_ONLY", False)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            status = asmdiff.main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_delta_row_closes_each_pair(self):
        funcs = asmdiff.extract_functions(self.PAIR_ASM)
        lines = asmdiff.summary_table(
            [asmdiff.Block("gcc -O2", funcs,
                           [("old_rt", "new_rt")])]).splitlines()
        self.assertRegex(lines[1],
                         r"old_rt\s+baseline\s+6\s+\.L2:4\s+exp2f")
        self.assertRegex(lines[2], r"new_rt\s+candidate\s+2\s+-\s+ldexpf")
        self.assertRegex(lines[3],
                         r"^\s+delta\s+-4\s+-\s+\+ldexpf -exp2f$")

    def test_span_cell_pairs_positions(self):
        funcs = {"old_l": [".L2:", "addl\t$1, %eax", "jne\t.L2"],
                 "new_l": [".L7:", "jne\t.L7"]}
        lines = asmdiff.summary_table(
            [asmdiff.Block("gcc -O2", funcs,
                           [("old_l", "new_l")])]).splitlines()
        self.assertRegex(lines[3], r"delta\s+-1\s+2 -> 1\s+-$")

    def test_fail_on_growth_names_the_offender(self):
        self._patch_compile(lambda cc, extra, src, tmp: self.GROW_ASM)
        status, _, err = self._run(["h.c", "--fail-on-growth",
                                    "--cc", "gcc -O2"])
        self.assertEqual(status, 3)
        self.assertIn("growth: new_rt +2 insns (old_rt -> new_rt)", err)

    def test_fail_on_growth_passes_when_nothing_grew(self):
        self._patch_compile(lambda cc, extra, src, tmp: GCC_ASM)
        status, _, err = self._run(["h.c", "--fail-on-growth",
                                    "--cc", "gcc -O2"])
        self.assertEqual(status, 0)
        self.assertNotIn("growth:", err)

    def test_fail_on_growth_across_two_files_with_json(self):
        asms = {"a.c": self.ONE_INSN_ASM, "b.c": self.THREE_INSN_ASM}
        self._patch_compile(
            lambda cc, extra, src, tmp: asms[Path(src).name])
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.c", Path(tmp) / "b.c"
            a.touch()
            b.touch()
            status, out, err = self._run([str(a), str(b), "-a", "rt",
                                          "--json", "--fail-on-growth",
                                          "--cc", "gcc -O2"])
        doc = json.loads(out)          # stdout stays pure JSON
        base, cand = doc["results"]
        self.assertNotIn("delta", base)
        self.assertEqual(cand["delta"]["insns"], 2)
        self.assertEqual(status, 3)
        self.assertIn("growth: rt +2 insns (a.c -> b.c)", err)

    def test_fail_on_growth_rejected_without_pairs(self):
        self._patch_compile(lambda cc, extra, src, tmp: GCC_ASM)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            asmdiff.main(["h.c", "new_const", "--fail-on-growth"])
        self.assertIn("--fail-on-growth needs paired functions",
                      err.getvalue())


class TestVersion(unittest.TestCase):
    """--version prints the module's single-source version."""

    _pyproject = Path(asmdiff.__file__).resolve().parent / "pyproject.toml"

    def test_version_flag_prints_and_exits_zero(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             self.assertRaises(SystemExit) as ctx:
            asmdiff.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(),
                         f"asmdiff {asmdiff.__version__}")

    @unittest.skipUnless(_pyproject.is_file(),
                         "pyproject.toml only exists in a repo checkout")
    def test_pyproject_takes_version_from_module(self):
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        data = asmdiff.tomllib.loads(self._pyproject.read_text())
        self.assertIn("version", data["project"].get("dynamic", []))
        self.assertNotIn("version", data["project"])
        self.assertEqual(data["tool"]["hatch"]["version"]["path"],
                         "asmdiff.py")


class TestClassifyInsn(unittest.TestCase):
    """Mnemonic buckets behind --span-stats: load/store/mul/div/branch/
    other."""

    # (ISA, instruction, bucket)
    CASES = [
        ("xtensa", "l32i.n\ta8, a2, 0", "load"),
        ("xtensa", "s32i\ta8, a2, 0", "store"),
        ("xtensa", "mull\ta8, a8, a9", "mul"),
        ("xtensa", "mul.s\tf0, f1, f2", "mul"),
        ("xtensa", "madd.s\tf0, f1, f2", "mul"),
        ("xtensa", "bne\ta2, a6, .L2", "branch"),
        ("xtensa", "loop\ta4, .L11_LEND", "branch"),
        ("xtensa", "call8\tfoo", "branch"),
        ("xtensa", "quos\ta2, a2, a3", "div"),
        ("xtensa", "remu\ta2, a2, a3", "div"),
        # The FPU divide is an inline sequence, not one instruction.
        ("xtensa", "div0.s\tf0, f1", "other"),
        ("xtensa", "nexp01.s\tf2, f1", "other"),
        ("xtensa", "addi\ta2, a2, 4", "other"),
        ("xtensa", "nop.n", "other"),
        ("riscv", "lw\ta0, 0(a1)", "load"),
        ("riscv", "fsw\tfa0, 4(a1)", "store"),
        ("riscv", "mulh\ta0, a1, a2", "mul"),
        ("riscv", "fmadd.s\tfa0, fa1, fa2, fa3", "mul"),
        ("riscv", "beqz\ta0, .L4", "branch"),
        ("riscv", "jal\tra, memcpy", "branch"),
        ("riscv", "divu\ta0, a1, a2", "div"),
        ("riscv", "remw\ta0, a1, a2", "div"),
        ("riscv", "fdiv.s\tfa0, fa1, fa2", "div"),
        ("riscv", "fsqrt.d\tfa0, fa1", "div"),
        ("riscv", "slli\ta0, a0, 2", "other"),
        # x86 has no load/store mnemonics: the memory operand decides.
        ("x86-att", "movl\t8(%rax), %eax", "load"),
        ("x86-att", "movl\t%eax, 8(%rax)", "store"),
        ("x86-att", "addl\t(%rdi), %eax", "load"),
        ("x86-att", "leaq\t8(%rax), %rbx", "other"),
        ("x86-att", "nopw\t0x0(%rax,%rax,1)", "other"),
        ("x86-att", "mulss\t.LC0(%rip), %xmm0", "mul"),
        ("x86-att", "jne\t.L2", "branch"),
        ("x86-att", "call\tmalloc", "branch"),
        ("x86-att", "movl\t$-5, %edi", "other"),
        ("x86-att", "pushq\t%rbp", "store"),
        ("x86-att", "idivl\t%ecx", "div"),
        ("x86-att", "divsd\t%xmm1, %xmm0", "div"),
        ("x86-att", "sqrtss\t%xmm0, %xmm0", "div"),
        ("arm", "ldr\tr0, [r1]", "load"),
        ("arm", "str\tr0, [r1, #4]", "store"),
        ("arm", "vmul.f32\ts0, s1, s2", "mul"),
        ("arm", "cbz\tr0, .L3", "branch"),
        ("arm", "push\t{r4, lr}", "store"),
        ("arm", "sdiv\tr0, r1, r2", "div"),
        ("arm", "vdiv.f32\ts0, s1, s2", "div"),
        ("arm", "vsqrt.f64\td0, d1", "div"),
        ("arm", "eor\tr0, r0, r1", "other"),
    ]

    def test_mnemonic_classes(self):
        for isa, insn, bucket in self.CASES:
            with self.subTest(isa=isa, insn=insn):
                self.assertEqual(asmdiff.classify_insn(insn), bucket)


class TestLibcallTier(unittest.TestCase):
    """Called symbols tiered by what the callee is, libgcc and ARM EABI
    spellings of one operation landing in the same tier."""

    CASES = [
        ("__addsf3", "softfp"), ("__muldf3", "softfp"),
        ("__negdf2", "softfp"), ("__unordsf2", "softfp"),
        ("__extendsfdf2", "softfp"), ("__truncdfsf2", "softfp"),
        ("__fixsfsi", "softfp"), ("__fixunsdfdi", "softfp"),
        ("__floatsidf", "softfp"), ("__floatunsisf", "softfp"),
        ("__aeabi_dmul", "softfp"), ("__aeabi_fcmplt", "softfp"),
        ("__aeabi_f2d", "softfp"), ("__aeabi_ui2d", "softfp"),
        ("__divsf3", "softfp-div"), ("__divdf3", "softfp-div"),
        ("__aeabi_fdiv", "softfp-div"), ("__aeabi_ddiv", "softfp-div"),
        ("__divsi3", "int-div"), ("__udivdi3", "int-div"),
        ("__umodsi3", "int-div"), ("__udivmoddi4", "int-div"),
        ("__aeabi_idiv", "int-div"), ("__aeabi_uidivmod", "int-div"),
        ("__aeabi_ldivmod", "int-div"),
        ("sqrtf", "libm"), ("exp2f", "libm"), ("pow", "libm"),
        ("atan2f", "libm"), ("ldexpf", "libm"),
        ("memcpy", "mem"), ("memset", "mem"), ("memmove", "mem"),
        ("memcmp", "mem"),
        ("indirect(a8)", "call"), ("esp_timer_get_time", "call"),
        ("__my_helper", "call"), ("sqrtish", "call"),
    ]

    def test_tiers(self):
        for sym, tier in self.CASES:
            with self.subTest(sym=sym):
                self.assertEqual(asmdiff.libcall_tier(sym), tier)


class TestSpanStats(unittest.TestCase):
    """--span-stats: per-loop-span instruction mix table."""

    LINES = [".L2:", "l32i.n\ta8, a2, 0", "mull\ta8, a8, a9",
             "s32i\ta8, a2, 0", "addi\ta2, a2, 4", "bne\ta2, a6, .L2"]

    def test_loop_span_ranges_and_counts_agree(self):
        self.assertEqual(asmdiff.loop_span_ranges(self.LINES),
                         [(".L2", 0, 5)])   # range includes the label line
        self.assertEqual(asmdiff.loop_spans(self.LINES), [(".L2", 5)])

    def test_table_counts_the_mix(self):
        out = asmdiff.span_stats_table(
            [asmdiff.Block("gcc -O2", {"f": self.LINES}, ["f"])])
        lines = out.splitlines()
        self.assertRegex(lines[0],
                         r"function\s+span\s+depth\s+insns\s+load\s+store"
                         r"\s+mul\s+div\s+branch\s+other")
        self.assertRegex(lines[1],
                         r"f\s+\.L2\s+0\s+5\s+1\s+1\s+1\s+0\s+1\s+1")

    def test_table_reports_nesting_depth(self):
        lines = [".L1:", "movl\t$0, %ecx", ".L2:", "addl\t$1, %ecx",
                 "jne\t.L2", "jnz\t.L1"]
        out = asmdiff.span_stats_table(
            [asmdiff.Block("gcc -O2", {"f": lines}, ["f"])]).splitlines()
        self.assertRegex(out[1], r"f\s+\.L1\s+0\s+")
        self.assertRegex(out[2], r"f\s+\.L2\s+1\s+")

    def test_no_spans_prints_note(self):
        out = asmdiff.span_stats_table(
            [asmdiff.Block("gcc -O2", {"g": ["ret"]}, ["g"])])
        self.assertIn("no loop spans", out)

class TestCostMix(unittest.TestCase):
    """cost_mix: class counts, call tiers, and what a loop span holds."""

    # One __muldf3 call inside the .L2 span, one after it, plus a
    # memcpy - tiered so it is counted, never priced.
    LINES = [".L2:", "movsd\t(%rdi), %xmm0", "call\t__muldf3",
             "addq\t$8, %rdi", "cmpq\t%rdx, %rdi", "jne\t.L2",
             "call\t__muldf3", "call\tmemcpy", "ret"]

    # other = 1, load = 2 price two classes; the symbol weight for
    # __muldf3 beats its softfp tier; branch, mem and call have none.
    PROFILE = {"name": "bench",
               "weights": {"other": 1, "load": 2, "softfp": 60,
                           "__muldf3": 90},
               "provenance": {"measured_on": "S3 rev 0.2",
                              "method": "cycle counter, median of 1000"}}

    def test_classes_count_the_whole_function(self):
        mix = asmdiff.cost_mix(self.LINES)
        self.assertEqual(mix["classes"],
                         {"load": 1, "store": 0, "mul": 0, "div": 0,
                          "branch": 5, "other": 2})

    def test_tiers_count_call_sites_and_the_loop_subset(self):
        mix = asmdiff.cost_mix(self.LINES)
        self.assertEqual(mix["tiers"], {"softfp": 2, "mem": 1})
        self.assertEqual(mix["tiers_in_loop"], {"softfp": 1})
        self.assertEqual(mix["calls"], {"__muldf3": 2, "memcpy": 1})
        # analyze names each callee once; the mix counts the sites.
        self.assertEqual(asmdiff.analyze(self.LINES)[1],
                         ["__muldf3", "memcpy"])

    def test_precomputed_ranges_give_the_same_mix(self):
        ranges = asmdiff.loop_span_ranges(self.LINES)
        self.assertEqual(asmdiff.cost_mix(self.LINES, ranges),
                         asmdiff.cost_mix(self.LINES))

    def test_score_prices_what_the_profile_covers(self):
        mix = asmdiff.cost_mix(self.LINES)
        # 1 load * 2 + 2 other * 1 + 2 __muldf3 * 90 = 184; the five
        # branches and the memcpy have no weight.
        self.assertEqual(asmdiff.cost_score(mix, self.PROFILE), (184, 6))

    def test_float_weight_rounds_the_score(self):
        profile = dict(self.PROFILE, weights={"branch": 1.5})
        mix = asmdiff.cost_mix(self.LINES)
        # 5 branches * 1.5; the other 3 instructions and 3 call sites
        # are unweighted.
        self.assertEqual(asmdiff.cost_score(mix, profile), (7.5, 6))

    def test_record_without_a_profile_is_ordinal(self):
        rec = asmdiff.cost_record(asmdiff.cost_mix(self.LINES))
        self.assertIsNone(rec["score"])
        self.assertIsNone(rec["profile"])
        self.assertEqual(rec["unweighted"], 11)   # 8 insns + 3 sites

    def test_record_carries_the_profile_provenance(self):
        rec = asmdiff.cost_record(asmdiff.cost_mix(self.LINES),
                                  self.PROFILE)
        self.assertEqual(rec["score"], 184)
        self.assertEqual(rec["profile"],
                         {"name": "bench", "measured_on": "S3 rev 0.2",
                          "method": "cycle counter, median of 1000"})


class TestCostColumn(unittest.TestCase):
    """--cost: the cell, the delta cell, and the column that appears
    only when asked for."""

    LINES = TestCostMix.LINES
    PROFILE = TestCostMix.PROFILE

    BASE = ["call\t__muldf3", "ret"]
    CAND = ["movsd\t(%rdi), %xmm0", "ret"]

    def setUp(self):
        self.addCleanup(setattr, asmdiff, "COST", False)

    def test_cell_lists_classes_then_tiers(self):
        cell = asmdiff.format_cost(asmdiff.cost_mix(self.LINES))
        self.assertEqual(cell, "ld 1 br 5 oth 2 softfp 2 (1 in loop) mem 1")

    def test_cell_leads_with_the_score_and_fits_the_budget(self):
        cell = asmdiff.format_cost(asmdiff.cost_mix(self.LINES),
                                   self.PROFILE)
        self.assertTrue(cell.startswith("score 184 (6 unweighted) "), cell)
        self.assertTrue(cell.endswith("..."), cell)
        self.assertLessEqual(len(cell), asmdiff.COST_CELL_BUDGET)

    def test_delta_cell_names_only_what_moved(self):
        delta = asmdiff.cost_delta(asmdiff.cost_mix(self.BASE),
                                   asmdiff.cost_mix(self.CAND))
        self.assertEqual(delta, {"classes": {"load": 1, "branch": -1},
                                 "tiers": {"softfp": -1}, "score": None})
        self.assertEqual(asmdiff.format_cost_delta(delta),
                         "ld +1 br -1 softfp -1")

    def test_delta_cell_leads_with_the_score_difference(self):
        delta = asmdiff.cost_delta(asmdiff.cost_mix(self.BASE),
                                   asmdiff.cost_mix(self.CAND),
                                   self.PROFILE)
        self.assertEqual(delta["score"], -88)   # 2 against 90
        self.assertEqual(asmdiff.format_cost_delta(delta),
                         "score -88 ld +1 br -1 softfp -1")

    def test_unchanged_mix_reads_as_a_dash(self):
        delta = asmdiff.cost_delta(asmdiff.cost_mix(self.BASE),
                                   asmdiff.cost_mix(self.BASE))
        self.assertEqual(asmdiff.format_cost_delta(delta), "-")

    def _blocks(self, funcs, sel, profile=None):
        return [asmdiff.Block("gcc -O2", funcs, sel, profile)]

    def test_inspect_and_file_tables_gain_the_column(self):
        asmdiff.COST = True
        funcs = {"f": self.CAND}
        for table in (asmdiff.inspect_table(self._blocks(funcs, ["f"])),
                      asmdiff.file_summary_table(self._blocks(funcs, None))):
            self.assertRegex(table.splitlines()[0],
                             r"^function\s+insns\s+loop spans\s+cost"
                             r"\s+calls$")
            self.assertRegex(table.splitlines()[1], r"f\s+2\s+-\s+ld 1 br 1")

    def test_each_row_is_priced_by_its_own_profile(self):
        asmdiff.COST = True
        funcs = {"f": self.BASE}
        lines = asmdiff.inspect_table(
            [asmdiff.Block("bench", funcs, ["f"], self.PROFILE),
             asmdiff.Block("plain", funcs, ["f"])]).splitlines()
        self.assertIn("score", lines[1])         # priced by [costs.bench]
        self.assertNotIn("score", lines[2])      # the other row stays ordinal

    def test_footer_names_each_distinct_profile_once(self):
        asmdiff.COST = True
        funcs = {"f": self.BASE}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asmdiff.table_footer(
                [asmdiff.Block("a", funcs, ["f"], self.PROFILE),
                 asmdiff.Block("b", funcs, ["f"], self.PROFILE),
                 asmdiff.Block("c", funcs, ["f"])])
        self.assertEqual(out.getvalue().count("costs: bench"), 1)


class TestCostProfiles(unittest.TestCase):
    """[costs.NAME] tables: what loads, what errors, and the
    provenance a score is never printed without."""

    CONFIG = """\
[costs.bench]
measured_on = "S3 rev 0.2, code in IRAM"
method = "cycle counter, median of 1000"
note = "divides measured on one operand pair"
other = 1
load = 2
softfp = 60
"__muldf3" = 90

[costs.bench-lto]
extends = "bench"
measured_on = "S3 rev 0.2, -flto"
method = "same harness"
load = 3

[s3]
cc = "xtensa-gcc"
flags = ["-O2"]
costs = "bench"
"""

    def _config(self, text=None):
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        return asmdiff.tomllib.loads(self.CONFIG if text is None else text)

    def _error(self, text, name="p"):
        with self.assertRaises(SystemExit) as ctx:
            asmdiff.load_costs(self._config(text), name, "cfg.toml")
        return str(ctx.exception)

    def test_numbers_are_weights_and_strings_provenance(self):
        profile = asmdiff.load_costs(self._config(), "bench", "cfg.toml")
        self.assertEqual(profile["weights"],
                         {"other": 1, "load": 2, "softfp": 60,
                          "__muldf3": 90})
        self.assertEqual(list(profile["provenance"]),
                         ["measured_on", "method", "note"])

    def test_extends_copies_weights_then_overrides(self):
        profile = asmdiff.load_costs(self._config(), "bench-lto",
                                     "cfg.toml")
        self.assertEqual(profile["weights"],
                         {"other": 1, "load": 3, "softfp": 60,
                          "__muldf3": 90})
        self.assertEqual(profile["provenance"]["measured_on"],
                         "S3 rev 0.2, -flto")

    EXTENDS_CHAIN = ('[costs.a]\nmeasured_on = "x"\nmethod = "y"\n'
                     '[costs.b]\nextends = "a"\nmeasured_on = "x"\n'
                     'method = "y"\n'
                     '[costs.c]\nextends = "b"\nmeasured_on = "x"\n'
                     'method = "y"\n')

    # (case, config text, profile asked for, fragments of the error)
    PROFILE_ERRORS = [
        ("extends chain", EXTENDS_CHAIN, "c", ["one level only"]),
        ("unknown profile lists the known ones", CONFIG, "nope",
         ["no [costs.nope]", "cost profiles: bench, bench-lto"]),
        ("config with no profile at all", '[s3]\ncc = "gcc"\n', "bench",
         ["defines no cost profiles"]),
        ("measured_on without method",
         '[costs.p]\nmeasured_on = "S3"\nload = 2\n', "p", ["method"]),
        ("method without measured_on",
         '[costs.p]\nmethod = "harness"\nload = 2\n', "p", ["measured_on"]),
        ("a tier that is never priced",
         '[costs.p]\nmeasured_on = "S3"\nmethod = "h"\nmem = 40\n', "p",
         ["mem has no weight"]),
        ("a weight of the wrong type",
         '[costs.p]\nmeasured_on = "S3"\nmethod = "h"\nload = [1, 2]\n', "p",
         ["load must be a weight"]),
    ]

    def test_profile_errors(self):
        for case, text, name, fragments in self.PROFILE_ERRORS:
            with self.subTest(case=case):
                msg = self._error(text, name)
                for fragment in fragments:
                    self.assertIn(fragment, msg)

    def test_provenance_line_names_where_and_how(self):
        profile = asmdiff.load_costs(self._config(), "bench", "cfg.toml")
        self.assertEqual(
            asmdiff.format_provenance(profile),
            "costs: bench - S3 rev 0.2, code in IRAM; cycle counter, "
            "median of 1000; note: divides measured on one operand pair")

    def test_target_resolves_its_profile_before_compiling(self):
        matrix = asmdiff.build_matrix([], ["s3"], self._config(),
                                      "cfg.toml")
        self.assertEqual(matrix[0].costs["name"], "bench")
        self.assertEqual(matrix[0].costs["weights"]["softfp"], 60)

    def test_target_naming_a_missing_profile_is_an_error(self):
        text = self.CONFIG.replace('costs = "bench"', 'costs = "ghost"')
        with self.assertRaises(SystemExit) as ctx:
            asmdiff.build_matrix([], ["s3"], self._config(text),
                                 "cfg.toml")
        self.assertIn("no [costs.ghost]", str(ctx.exception))

    def test_target_costs_must_be_a_string(self):
        text = self.CONFIG.replace('costs = "bench"', "costs = 3")
        with self.assertRaises(SystemExit) as ctx:
            asmdiff.build_matrix([], ["s3"], self._config(text),
                                 "cfg.toml")
        self.assertIn("costs must name", str(ctx.exception))

    def test_costs_arg_overrides_every_row_including_cc(self):
        matrix = asmdiff.build_matrix(["gcc -O2"], ["s3"], self._config(),
                                      "cfg.toml", costs_arg="bench-lto")
        self.assertEqual([e.costs["name"] for e in matrix],
                         ["bench-lto", "bench-lto"])

    def test_costs_table_is_not_a_target(self):
        config = self._config()
        self.assertEqual(asmdiff.config_target_names(config), ["s3"])
        self.assertEqual(asmdiff.config_cost_names(config),
                         ["bench", "bench-lto"])

    def _run_main(self, argv, asm):
        real = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: asm
        self.addCleanup(setattr, asmdiff, "compile_to_asm", real)
        self.addCleanup(setattr, asmdiff, "COST", False)
        self.addCleanup(setattr, asmdiff, "COST_PROFILE", None)
        self.addCleanup(setattr, asmdiff, "SUMMARY_ONLY", False)
        self.addCleanup(setattr, asmdiff, "JSON_OUT", None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asmdiff.main(argv)
        return out.getvalue()

    def test_costs_flag_implies_cost_and_prints_provenance(self):
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "asmdiff.toml"
            cfg.write_text(self.CONFIG)
            out = self._run_main(["h.c", "--costs", "bench", "-s",
                                  "--config", str(cfg),
                                  "--cc", "gcc -O2"], GCC_ASM)
        # old_const: 1 mul (unweighted) + 1 branch (unweighted) -> 0.
        self.assertRegex(out, r"old_const\s+baseline\s+2\s+-\s+"
                              r"score 0 \(2 unweighted\)")
        # new_const: 1 other * 1, the branch and ldexpf unweighted.
        self.assertRegex(out, r"new_const\s+candidate\s+2\s+-\s+"
                              r"score 1 \(2 unweighted\)")
        self.assertIn("costs: bench - S3 rev 0.2, code in IRAM; "
                      "cycle counter, median of 1000", out)

    def test_json_carries_the_profile(self):
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "asmdiff.toml"
            cfg.write_text(self.CONFIG)
            out = self._run_main(["h.c", "--json", "--costs", "bench",
                                  "--config", str(cfg),
                                  "--cc", "gcc -O2"], GCC_ASM)
        doc = json.loads(out)
        old, new = doc["results"]
        self.assertEqual(old["cost"]["score"], 0)
        self.assertEqual(new["cost"]["score"], 1)
        self.assertEqual(new["cost"]["unweighted"], 2)
        self.assertEqual(new["cost"]["profile"]["name"], "bench")
        self.assertEqual(new["cost"]["profile"]["method"],
                         "cycle counter, median of 1000")
        self.assertEqual(new["delta"]["cost"]["score"], 1)

    def _run_elf(self, argv):
        real = asmdiff.run_objdump
        asmdiff.run_objdump = lambda objdump, elf: OBJDUMP_ASM
        self.addCleanup(setattr, asmdiff, "run_objdump", real)
        self.addCleanup(setattr, asmdiff, "COST", False)
        self.addCleanup(setattr, asmdiff, "COST_PROFILE", None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = asmdiff.main(argv)
        return status, out.getvalue()

    def test_elf_mode_scores_with_costs(self):
        # ELF mode resolves no target, so --costs is the only way a
        # profile reaches it; the column would otherwise stay ordinal.
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "asmdiff.toml"
            cfg.write_text(self.CONFIG)
            elf = Path(tmp) / "fw.elf"
            elf.write_bytes(b"\x7fELF" + b"\0" * 12)
            status, out = self._run_elf([str(elf), "render_lut", "-s",
                                         "--objdump", "od", "--costs",
                                         "bench", "--config", str(cfg)])
        self.assertEqual(status, 0)
        # render_lut: 1 l32i * 2, 5 other * 1, loop and retw.n unweighted.
        self.assertRegex(out, r"render_lut\s+8\s+\S+\s+"
                              r"score 7 \(2 unweighted\)")
        self.assertIn("costs: bench - S3 rev 0.2, code in IRAM; "
                      "cycle counter, median of 1000", out)

    def test_elf_mode_unknown_profile_errors(self):
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "asmdiff.toml"
            cfg.write_text(self.CONFIG)
            elf = Path(tmp) / "fw.elf"
            elf.write_bytes(b"\x7fELF" + b"\0" * 12)
            with self.assertRaises(SystemExit) as ctx:
                self._run_elf([str(elf), "render_lut", "--objdump", "od",
                               "--costs", "ghost", "--config", str(cfg)])
        self.assertIn("no [costs.ghost]", str(ctx.exception))


class TestSummaryOnly(unittest.TestCase):
    """--summary-only suppresses listings; the stats tables stay."""

    def setUp(self):
        self._compile = asmdiff.compile_to_asm
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: GCC_ASM
        asmdiff.SUMMARY_ONLY = True
        self.addCleanup(setattr, asmdiff, "compile_to_asm", self._compile)
        self.addCleanup(setattr, asmdiff, "SUMMARY_ONLY", False)

    def _capture(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args)
        return out.getvalue()

    def test_across_mode_prints_table_only(self):
        out = self._capture(asmdiff.run_inspect, "h.c",
                            ["gcc -O2", "clang -O2"], ["new_const"],
                            None, [], "/tmp")
        self.assertIn("baseline", out)
        self.assertNotIn(" | ", out)

    def test_inspect_list_prints_table_only(self):
        out = self._capture(asmdiff.run_inspect, "h.c", ["gcc -O2"],
                            ["new_const"], None, [], "/tmp")
        self.assertIn("function", out)       # stats table present
        self.assertNotIn("\tjmp", out)       # no listing body

    def test_elf_mode_prints_table_only(self):
        real = asmdiff.run_objdump
        asmdiff.run_objdump = lambda objdump, elf: OBJDUMP_ASM
        self.addCleanup(setattr, asmdiff, "run_objdump", real)
        out = self._capture(asmdiff.run_elf, "fw.elf", ["render_lut"],
                            None, "od")
        self.assertIn("render_lut", out)     # stats row
        self.assertNotIn("render_lut:", out)  # no listing header

    def test_main_wires_the_flag(self):
        asmdiff.SUMMARY_ONLY = False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asmdiff.main(["h.c", "--summary-only"])
        self.assertIn("baseline", out.getvalue())
        self.assertNotIn(" | ", out.getvalue())


class TestFileTags(unittest.TestCase):
    def test_file_tag_cases(self):
        # (case, the two paths, the tags they are labelled with)
        cases = [
            ("distinct basenames", ("p/old.c", "p/new.c"),
             ("old.c", "new.c")),
            ("same basename, distinct parents",
             ("/tmp/amy-exp2f/log2.c", "/tmp/amy-ldexpf/log2.c"),
             ("amy-exp2f/log2.c", "amy-ldexpf/log2.c")),
            ("same basename and parent", ("a/src/f.c", "b/src/f.c"),
             ("a/src/f.c", "b/src/f.c")),
        ]
        for case, paths, tags in cases:
            with self.subTest(case=case):
                self.assertEqual(asmdiff.file_tags(*paths), tags)


class TestAcrossValidation(unittest.TestCase):
    """CLI validation rejects bad --across usage before any compilation."""

    def _expect_error(self, argv, fragment):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                asmdiff.main(argv)
        self.assertIn(fragment, err.getvalue())

    # (case, argv with A, B, C standing in for files on disk, fragment)
    ARGUMENT_ERRORS = [
        ("--across with --pair", ["x.c", "--across", "f", "--pair", "a:b"],
         "mutually exclusive"),
        ("two files with --pair", ["A", "B", "--pair", "x:y"],
         "--pair compares within one file"),
        ("three files", ["A", "B", "C", "--across", "f"], "at most two"),
        ("one file, one compiler",
         ["a.c", "--across", "f", "--cc", "gcc -O3"], "at least two --cc"),
    ]

    def test_across_argument_errors(self):
        for case, argv, fragment in self.ARGUMENT_ERRORS:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    files = {}
                    for key, name in (("A", "a.c"), ("B", "b.c"),
                                      ("C", "c.c")):
                        path = Path(tmp) / name
                        path.touch()
                        files[key] = str(path)
                    self._expect_error([files.get(arg, arg) for arg in argv],
                                       fragment)


class TestInspectValidation(unittest.TestCase):
    """CLI validation for the SOURCE.c FUNC inspect grammar."""

    def _expect_error(self, argv, fragment):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                asmdiff.main(argv)
        # parser.error writes to stderr; sys.exit carries the message
        self.assertIn(fragment, err.getvalue() + str(ctx.exception))

    # (case, argv with A and B standing in for files on disk, fragment)
    ARGUMENT_ERRORS = [
        ("function with --pair", ["x.c", "f", "--pair", "a:b"],
         "cannot be combined"),
        ("function with --across", ["x.c", "f", "--across", "g"],
         "cannot be combined"),
        ("two files plus a function", ["A", "B", "f"], "--across"),
        ("--layout without a function", ["x.c", "--layout", "list"],
         "--layout only applies"),
        ("unknown --layout value", ["x.c", "f", "--layout", "diagonal"],
         "invalid choice"),
        ("a mistyped filename is not a function", ["x.c", "typo.c"],
         "no such file"),
    ]

    def test_inspect_argument_errors(self):
        for case, argv, fragment in self.ARGUMENT_ERRORS:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    files = {}
                    for key, name in (("A", "a.c"), ("B", "b.c")):
                        path = Path(tmp) / name
                        path.touch()
                        files[key] = str(path)
                    self._expect_error([files.get(arg, arg) for arg in argv],
                                       fragment)


class TestShortAliases(unittest.TestCase):
    """-p/-a/-db/-l are aliases of --pair/--across/--compile-commands/
    --layout: each hits that option's own validation."""

    def _expect_error(self, argv, fragment):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                asmdiff.main(argv)
        self.assertIn(fragment, err.getvalue() + str(ctx.exception))

    # (alias, argv, fragment of the long option's own error)
    ALIASES = [
        ("-p", ["x.c", "-p", "nocolon"], "expects OLD:NEW"),
        ("-a", ["x.c", "-a", "f", "--cc", "gcc -O3"], "at least two --cc"),
        ("-l", ["x.c", "-l", "list"], "--layout only applies"),
        ("-t", ["x.c", "f", "-t", "nope"], "no [nope] target"),
        ("-f", ["x.c", "-f", "old_", "-p", "a:b"],
         "--filter selects functions"),
    ]

    def test_aliases_reach_their_option(self):
        for alias, argv, fragment in self.ALIASES:
            with self.subTest(alias=alias):
                self._expect_error(argv, fragment)

    def test_C_is_collapse(self):
        # --collapse has no validation of its own; check the global it sets.
        self._expect_error(["x.c", "f", "-C", "-t", "nope"], "no [nope]")
        self.assertTrue(asmdiff.COLLAPSE)

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

    def test_example_groups_name_defined_targets(self):
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        data = asmdiff.tomllib.loads(asmdiff.EXAMPLE_CONFIG)
        targets = asmdiff.config_target_names(data)
        groups = asmdiff.config_groups(data, "example")
        self.assertIn("riscv32-esp", groups)
        self.assertIn("xtensa-esp", groups)
        for name, members in groups.items():
            for member in members:
                self.assertIn(member, targets, f"groups.{name}")

    def test_example_config_defines_host_target(self):
        # The README and skill text tell users `--target host` works out
        # of the box; the example config must actually define it.
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        data = asmdiff.tomllib.loads(asmdiff.EXAMPLE_CONFIG)
        self.assertIn("cc", data.get("host", {}))

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


class TestCompletion(unittest.TestCase):
    """--completion scripts, --install-completion, and the hidden
    --complete helper the generated scripts call back into."""

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = asmdiff.main(argv)
        return status, out.getvalue()

    def _script(self, shell):
        status, out = self._run(["--completion", shell])
        self.assertEqual(status, 0)
        return out

    def _parser_options(self):
        """Option strings argparse itself advertises in --help, so this
        test cannot go stale when a flag is added elsewhere."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            asmdiff.main(["--help"])
        opts = set()
        for line in out.getvalue().splitlines():
            if not line.startswith("  -"):
                continue
            # argparse separates the invocation from its help by 2+ spaces.
            for piece in line.strip().split("  ")[0].split(", "):
                opts.add(piece.split()[0].split("=")[0])
        return opts

    def _bash_candidates(self, script):
        """The flag list the bash script offers on a leading dash."""
        for line in script.splitlines():
            if "compgen -W" in line and "--version" in line:
                return line.split('"')[1].split()
        self.fail("bash script has no flag candidate list")

    def _config(self, tmp, text):
        if asmdiff.tomllib is None:
            self.skipTest("tomllib requires Python >= 3.11")
        path = Path(tmp) / "asmdiff.toml"
        path.write_text(text)
        return path

    def test_parser_options_are_found(self):
        # Guards the helper the two "lists every option" tests rest on.
        opts = self._parser_options()
        self.assertIn("--target", opts)
        self.assertIn("-db", opts)
        self.assertNotIn("--complete", opts)   # help=SUPPRESS

    def test_scripts_list_every_option(self):
        # fish spells a long option as "-l NAME" and has nothing to say
        # about the short ones.
        options = self._parser_options()
        for shell in asmdiff.COMPLETION_SHELLS:
            with self.subTest(shell=shell):
                script = self._script(shell)
                for opt in options:
                    if shell == "fish":
                        if opt.startswith("--"):
                            self.assertIn("-l " + opt[2:], script, opt)
                    else:
                        self.assertIn(opt, script, opt)

    def test_candidate_list_omits_the_hidden_helper(self):
        # --complete exists for the script to call, not for the user to
        # tab into; it would print nothing useful at a prompt.
        candidates = self._bash_candidates(self._script("bash"))
        self.assertIn("--target", candidates)
        self.assertNotIn("--complete", candidates)

    def test_scripts_carry_the_overwrite_marker(self):
        for shell in asmdiff.COMPLETION_SHELLS:
            head = self._script(shell).split("\n")[:2]
            self.assertIn(asmdiff.COMPLETION_MARKER, head, shell)

    def test_zsh_script_starts_with_compdef(self):
        # compinit reads the tag off the first line only.
        self.assertTrue(self._script("zsh").startswith("#compdef "))

    def test_bash_script_parses(self):
        if not asmdiff.shutil.which("bash"):
            self.skipTest("bash not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asmdiff.bash"
            path.write_text(self._script("bash"))
            self.assertEqual(
                asmdiff.subprocess.call(["bash", "-n", str(path)]), 0)

    def test_complete_targets_lists_targets_then_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp,
                               '[groups]\nesp = ["c3"]\n'
                               '[c3]\ncc = "riscv-gcc"\n'
                               '[host]\ncc = "gcc"\n')
            status, out = self._run(["--config", str(cfg),
                                     "--complete", "targets"])
        self.assertEqual(status, 0)
        self.assertEqual(out.split(), ["c3", "host", "esp"])

    def test_complete_costs_lists_profile_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp,
                               '[host]\ncc = "gcc"\n'
                               '[costs.s3-iram]\nmeasured_on = "bench"\n')
            status, out = self._run(["--config", str(cfg),
                                     "--complete", "costs"])
        self.assertEqual(status, 0)
        self.assertEqual(out.split(), ["s3-iram"])

    def test_complete_is_silent_without_a_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch.object(asmdiff.Path, "home",
                                       return_value=Path(tmp)):
                    status, out = self._run(["--complete", "targets"])
            finally:
                os.chdir(cwd)
        self.assertEqual(status, 0)
        self.assertEqual(out, "")

    def test_complete_is_silent_on_a_bad_config_path(self):
        # An error here would land in the middle of the user's prompt.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, out = self._run(["--config", "/no/such/asmdiff.toml",
                                     "--complete", "targets"])
        self.assertEqual(status, 0)
        self.assertEqual(out, "")
        self.assertEqual(err.getvalue(), "")

    def test_complete_is_silent_on_an_unknown_kind(self):
        # An old script against a new tool, or the other way round.
        status, out = self._run(["--complete", "functions"])
        self.assertEqual(status, 0)
        self.assertEqual(out, "")

    def test_install_writes_to_the_bash_user_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"BASH_COMPLETION_USER_DIR": tmp}):
                status, out = self._run(["--install-completion", "bash"])
            written = Path(tmp) / "completions" / "asmdiff"
            self.assertEqual(status, 0)
            self.assertIn(str(written), out)
            self.assertTrue(written.read_text()
                            .startswith(asmdiff.COMPLETION_MARKER))

    def test_install_replaces_its_own_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = Path(tmp) / "completions" / "asmdiff"
            written.parent.mkdir(parents=True)
            written.write_text(asmdiff.COMPLETION_MARKER + "\n# stale\n")
            with mock.patch.dict(os.environ,
                                 {"BASH_COMPLETION_USER_DIR": tmp}):
                status, _ = self._run(["--install-completion", "bash"])
            self.assertEqual(status, 0)
            self.assertNotIn("# stale", written.read_text())

    def test_install_refuses_a_foreign_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = Path(tmp) / "completions" / "asmdiff"
            written.parent.mkdir(parents=True)
            written.write_text("# hand-written, do not clobber\n")
            err = io.StringIO()
            with mock.patch.dict(os.environ,
                                 {"BASH_COMPLETION_USER_DIR": tmp}), \
                 contextlib.redirect_stderr(err), \
                 self.assertRaises(SystemExit) as ctx:
                asmdiff.main(["--install-completion", "bash"])
            # sys.exit(message): the interpreter prints it and exits 1.
            self.assertIn("was not written by asmdiff",
                          str(ctx.exception))
            self.assertIn(str(written), str(ctx.exception))
            self.assertEqual(written.read_text(),
                             "# hand-written, do not clobber\n")

    def test_install_zsh_names_the_fpath_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asmdiff.Path, "home",
                                   return_value=Path(tmp)):
                status, out = self._run(["--install-completion", "zsh"])
            self.assertEqual(status, 0)
            self.assertTrue((Path(tmp) / ".zfunc" / "_asmdiff").is_file())
            self.assertIn("fpath=(~/.zfunc $fpath)", out)
            self.assertIn("compinit", out)

    def test_install_fish_uses_the_autoloaded_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(asmdiff.Path, "home",
                                   return_value=Path(tmp)):
                status, _ = self._run(["--install-completion", "fish"])
            self.assertEqual(status, 0)
            self.assertTrue((Path(tmp) / ".config" / "fish" / "completions"
                             / "asmdiff.fish").is_file())

    def test_install_rejects_an_unknown_shell(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/tcsh"}), \
             self.assertRaises(SystemExit) as ctx:
            asmdiff.main(["--install-completion"])
        self.assertIn("bash, zsh, fish", str(ctx.exception))
        self.assertIn("tcsh", str(ctx.exception))

    def test_default_shell_is_the_basename_of_shell(self):
        self.assertEqual(asmdiff.default_shell({"SHELL": "/usr/bin/fish"}),
                         "fish")
        self.assertEqual(asmdiff.default_shell({}), "")

    def test_prog_name_falls_back_to_the_published_name(self):
        self.assertEqual(asmdiff.completion_prog("/usr/bin/asmdiff"),
                         "asmdiff")
        self.assertEqual(asmdiff.completion_prog("tools/asmdiff.py"),
                         "asmdiff.py")
        # A test runner or an interpreter is not a command to complete.
        self.assertEqual(asmdiff.completion_prog("python3"), "asmdiff")


GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


class TestGolden(unittest.TestCase):
    """What each (mode, flag set) renders, against a file in golden/.

    These carry the presentation: column order, headers, the legend, the
    delta row, group headers, listings.  A contract test asserts the one
    thing it is about; the shape of the whole page is what a diff against
    a checked-in file reads best.

    Every case runs through main() with the compiler (or objdump) mocked
    to a fixture and --width 100 pinned, so nothing here depends on a
    terminal, on $COLUMNS, or on a compiler being installed.
    ASMDIFF_UPDATE_GOLDEN=1 rewrites the files.
    """

    # (golden file, argv, fixture, files to create in the run directory)
    CASES = [
        ("pairs.txt",
         ["asmdiff_example.c", "--cc", "gcc -O2"], "GCC_ASM", ()),
        ("pairs-two-cc.txt",
         ["asmdiff_example.c", "--cc", "gcc -O2", "--cc", "clang -O2"],
         "GCC_ASM", ()),
        ("pairs-s.txt",
         ["asmdiff_example.c", "-s", "--cc", "gcc -O2", "--cc", "clang -O2"],
         "GCC_ASM", ()),
        ("pairs-cost.txt",
         ["asmdiff_example.c", "--cost", "-s", "--cc", "gcc -O2"],
         "GCC_ASM", ()),
        ("inspect.txt",
         ["asmdiff_example.c", "looper", "--cc", "gcc -O2"], "LOOP_ASM", ()),
        ("inspect-span-stats.txt",
         ["asmdiff_example.c", "looper", "--span-stats", "--cc", "gcc -O2"],
         "LOOP_ASM", ()),
        ("inspect-two-cc.txt",
         ["asmdiff_example.c", "new_const", "--cc", "gcc -O2",
          "--cc", "clang -O2"], "GCC_ASM", ()),
        ("across-two-files.txt",
         ["a.c", "b.c", "-a", "looper", "--cc", "gcc -O2"],
         "LOOP_ASM", ("a.c", "b.c")),
        ("summary-two-files.txt",
         ["a.c", "b.c", "--cc", "gcc -O2"], "GCC_ASM", ("a.c", "b.c")),
        ("elf.txt",
         ["fw.elf", "render_lut", "--objdump", "od"],
         "OBJDUMP_ASM", ("fw.elf",)),
        ("elf-span-stats.txt",
         ["fw.elf", "render_lut", "--span-stats", "--objdump", "od"],
         "OBJDUMP_ASM", ("fw.elf",)),
    ]

    # Globals main() sets from its arguments; restored so a case cannot
    # leak a flag into the next one or into another class.
    GLOBALS = ("WIDTH", "COST", "COST_PROFILE", "SPAN_STATS", "SUMMARY_ONLY",
               "COLLAPSE", "JSON_OUT", "FAIL_ON_GROWTH", "VERBOSE")

    def _render(self, argv, fixture, needs):
        asm = globals()[fixture]
        for name in ("compile_to_asm", "run_objdump"):
            self.addCleanup(setattr, asmdiff, name, getattr(asmdiff, name))
        for name in self.GLOBALS:
            self.addCleanup(setattr, asmdiff, name, getattr(asmdiff, name))
        asmdiff.compile_to_asm = lambda cc, extra, src, tmp: asm
        asmdiff.run_objdump = lambda objdump, elf: asm
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            for name in needs:
                path = Path(tmp) / name
                # is_elf() reads the magic bytes; the disassembly comes
                # from the mock, so the rest of the file is padding.
                if name.endswith(".elf"):
                    path.write_bytes(b"\x7fELF" + b"\0" * 12)
                else:
                    path.touch()
            # Relative paths only: an absolute tempdir would land in the
            # output as a file tag and no two runs would agree.
            with _inside(tmp), contextlib.redirect_stdout(out):
                asmdiff.main(list(argv) + ["--width", "100"])
        return out.getvalue()

    def test_rendered_output(self):
        update = os.environ.get("ASMDIFF_UPDATE_GOLDEN")
        for name, argv, fixture, needs in self.CASES:
            with self.subTest(case=name):
                text = self._render(argv, fixture, needs)
                path = GOLDEN_DIR / name
                if update:
                    GOLDEN_DIR.mkdir(exist_ok=True)
                    path.write_text(text)
                    continue
                diff = "".join(difflib.unified_diff(
                    path.read_text().splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    f"golden/{name}", "this run"))
                if diff:
                    self.fail(f"{name} does not match this run "
                              "(ASMDIFF_UPDATE_GOLDEN=1 rewrites it):\n"
                              + diff)


if __name__ == "__main__":
    unittest.main()
