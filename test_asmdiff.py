#!/usr/bin/env python3
"""Unit tests for asmdiff.py.  Run: python3 tools/asmdiff/test_asmdiff.py -v"""
import contextlib
import io
import unittest

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


class TestAutoPairs(unittest.TestCase):
    def test_pairs_by_convention(self):
        names = ["old_const", "new_const", "old_rt", "new_rt", "helper"]
        self.assertEqual(asmdiff.auto_pairs(names),
                         [("old_const", "new_const"), ("old_rt", "new_rt")])

    def test_unmatched_old_ignored(self):
        self.assertEqual(asmdiff.auto_pairs(["old_x", "new_y"]), [])


class TestBuildMatrix(unittest.TestCase):
    def test_explicit_cc_used_verbatim(self):
        self.assertEqual(asmdiff.build_matrix(["tcc -O1"]), ["tcc -O1"])

    def test_default_is_gcc_and_clang_with_amy_flags(self):
        matrix = asmdiff.build_matrix([])
        self.assertEqual(len(matrix), 2)
        self.assertTrue(matrix[0].startswith("gcc "))
        self.assertTrue(matrix[1].startswith("clang "))
        for cmd in matrix:
            self.assertIn("-O3", cmd)
            self.assertIn("-DAMY_WAVETABLE", cmd)
            self.assertIn("-Wno-float-conversion", cmd)
            if asmdiff.SRC_DIR.is_dir():  # only added inside the AMY repo
                self.assertIn(str(asmdiff.SRC_DIR), cmd)


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
        self.assertRegex(lines[1], r"old_c\s+baseline\s+2\s+-")
        self.assertRegex(lines[2], r"new_c\s+candidate\s+1\s+ldexpf")


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

    def test_two_files_require_across(self):
        self._expect_error(["a.c", "b.c"], "require --across")

    def test_at_most_two_files(self):
        self._expect_error(["a.c", "b.c", "c.c", "--across", "f"],
                           "at most two")

    def test_across_one_file_needs_two_cc_entries(self):
        self._expect_error(["a.c", "--across", "f", "--cc", "gcc -O3"],
                           "at least two --cc")


if __name__ == "__main__":
    unittest.main()
