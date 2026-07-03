# asmdiff 
## per-function assembly comparison for paired C implementations

> asmdiff is a command-line tool for comparing the generated assembly of individual C functions across implementations, compiler flags, compiler versions, and source revisions. It is intended for investigating compiler code generation rather than benchmarking runtime performance.

`asmdiff.py` answers one question fast: **when I rewrite a C construct, what
does the compiler actually emit - before and after?** It compiles a small
harness file across a matrix of compilers, extracts each variant function's
assembly, and prints side-by-side listings plus a summary of instruction
counts and outbound calls.

Defaults to shorepine/amy compiler flags. Should easily be re-toolable to any GNU-as-ELF asm.
  
Its home use case: checking whether an expression that used to constant-fold
(e.g. `x * exp2f(5)` → one multiply) turns into a library call (e.g.
`ldexpf(x, 5)` → `jmp ldexpf@PLT`) after a "cleanup". That distinction is
invisible in source review and decisive on hot paths.

## Quick start

Write a harness with your two versions as `old_*` / `new_*` function pairs:

```c
/* myharness.c */
#include <math.h>
float old_scale(float x) { return x * exp2f(-5); }
float new_scale(float x) { return ldexpf(x, -5); }
```

Run:

```
$ asmdiff.py myharness.c
```

Output (gcc section shown, one per compiler):

```
== gcc -O3 ... ==

old_scale                                    | new_scale
---------------------------------------------+---------------------------------------------
endbr64                                      | endbr64
mulss   .LC0(%rip), %xmm0                    | movl    $-5, %edi
ret                                          | jmp     ldexpf@PLT

function   role       insns  calls
old_scale  baseline   3      -
new_scale  candidate  3      ldexpf
```

Read the `calls` column first: `-` means the construct lowered to inline
instructions; a symbol name means a libcall. The side-by-side asm above it is
the evidence.

A worked example is included — `asmdiff_example.c` reproduces the
exp2f/ldexpf analysis for both constant and runtime shift amounts:

```
$ asmdiff.py asmdiff_example.c
```

## Command reference

```
asmdiff.py SOURCE.c [SOURCE2.c] [--pair OLD:NEW]... [--across FUNC]...
           [--cc 'CC FLAGS']... [-- EXTRA_FLAGS...]
```

| Option | Meaning |
|---|---|
| `SOURCE.c` | C file to compile — a purpose-built harness or a real project source. A second file may be given with `--across` to compare versions. |
| `--pair OLD:NEW` | Compare two *different* functions within one compilation. Repeatable. Default: every `old_X` is auto-paired with its `new_X`. |
| `--across FUNC` | Compare the *same* function across two compilations (see below). Repeatable. Mutually exclusive with `--pair`. |
| `--cc 'CC FLAGS'` | One compiler invocation, command and flags in a single quoted string. Repeatable to build a matrix. Default: `gcc` and `clang`, each with AMY's Makefile flags (see below). |
| `-- FLAGS...` | Everything after a bare `--` is appended to *every* compiler invocation. |

Examples:

```bash
# Explicit pairs, default compilers
asmdiff.py h.c --pair biquad_v1:biquad_v2 --pair svf_v1:svf_v2

# Cross-compilers: quote command and flags together
asmdiff.py h.c --cc 'xtensa-esp32s3-elf-gcc -O2 -mlongcalls' \
               --cc 'riscv32-esp-elf-gcc -O2'

# Try a flag variant across the whole default matrix
asmdiff.py h.c -- -fno-math-errno
```

Compilers missing from `PATH` are skipped with a warning; the run fails only
if none are usable. Exit status is non-zero only for operational failures
(compile error — the compiler's stderr is shown — unknown `--pair` name, no
usable compiler). Differing assembly is the expected result, never an error.

## Comparing the same function across two builds (`--across`)

`--pair` needs both variants to coexist in one compilation. Real changes
usually don't look like that: the "old" and "new" versions are the same
function under different flags, defines, or file revisions. `--across FUNC`
covers both shapes:

**One file, two (or more) `--cc` entries** — flag/define variants. The first
entry is the baseline; each later entry is compared against it:

```bash
# Did dropping fixed-point change the biquad's codegen?
asmdiff.py src/filters.c --across dsps_biquad_f32_ansi \
    --cc 'gcc -O3 -DMY_FIXED_CONFIG' --cc 'gcc -O3'

# gcc vs clang on the same function
asmdiff.py src/filters.c --across dsps_biquad_f32_ansi \
    --cc 'gcc -O3' --cc 'clang -O3'
```
```bash
# Size vs Performance Optimizations
asmdiff.py src/filters.c --across dsps_biquad_f32_ansi \
    --cc 'gcc -Os' --cc 'gcc -O3'

```

```
cc#1: gcc -Os
cc#2: gcc -O3

== cc#1 vs cc#2 ==

dsps_biquad_f32_ansi [cc#1]                  | dsps_biquad_f32_ansi [cc#2]
---------------------------------------------+---------------------------------------------
endbr64                                      | endbr64
movl    (%r8), %r11d                         | movdqu  (%r8), %xmm0
movl    8(%r8), %r10d                        | pushq   %r13
pushq   %r15                                 | pushq   %r12
xorl    %r9d, %r9d                           | pshufd  $255, %xmm0, %xmm1
pushq   %r14                                 | pushq   %rbp
movl    4(%r8), %r15d                        | movd    %xmm1, %ebp
pushq   %r13                                 | movdqa  %xmm0, %xmm1
movl    12(%r8), %r13d                       | pushq   %rbx
pushq   %r12                                 | punpckhdq       %xmm0, %xmm1
movl    %edx, %r12d                          | movd    %xmm1, %r10d
pushq   %rbp                                 | pshufd  $85, %xmm0, %xmm1
movq    %rsi, %rbp                           | testl   %edx, %edx
pushq   %rbx                                 | jle     .L24
movq    %rdi, %rbx                           | movslq  %edx, %rdx
.L27:                                        | movd    %xmm1, %r12d
cmpl    %r9d, %r12d                          | movd    %xmm0, %r11d
jle     .L30                                 | movq    %rsi, %r9
movl    (%rbx,%r9,4), %r14d                  | leaq    (%rdi,%rdx,4), %rbx
movl    (%rcx), %edi                         | movq    %rdi, %rsi
movl    %r14d, %esi                          | jmp     .L25
call    SMULR6                               | .L26:
movl    4(%rcx), %edi                        | movl    %eax, %r10d
movl    %r11d, %esi                          | movl    %edi, %r11d
movl    %eax, %edx                           | .L25:
call    SMULR6                               | movl    4(%rcx), %eax
movl    8(%rcx), %edi                        | movl    (%rsi), %edi
movl    %r15d, %esi                          | addl    $1024, %r12d
movl    %r11d, %r15d                         | addl    $1024, %ebp
addl    %eax, %edx                           | sarl    $11, %r12d
movl    %r14d, %r11d                         | sarl    $11, %ebp
call    SMULR6                               | leal    1024(%rax), %edx
movl    12(%rcx), %edi                       | leal    1024(%r11), %eax
movl    %r10d, %esi                          | sarl    $11, %eax
addl    %eax, %edx                           | sarl    $11, %edx
call    SMULR6                               | leal    1024(%rdi), %r13d
movl    16(%rcx), %edi                       | imull   %eax, %edx
movl    %r13d, %esi                          | movl    (%rcx), %eax
movl    %r10d, %r13d                         | sarl    $11, %r13d
subl    %eax, %edx                           | addl    $1024, %eax
call    SMULR6                               | sarl    $11, %eax
movl    %eax, %esi                           | addl    $1, %edx
movl    %edx, %eax                           | imull   %r13d, %eax
subl    %esi, %eax                           | sarl    %edx
movl    %eax, 0(%rbp,%r9,4)                  | addl    $1, %eax
movl    %eax, %r10d                          | sarl    %eax
incq    %r9                                  | addl    %eax, %edx
jmp     .L27                                 | movl    8(%rcx), %eax
.L30:                                        | addl    $1024, %eax
popq    %rbx                                 | sarl    $11, %eax
movl    %r15d, 4(%r8)                        | imull   %r12d, %eax
xorl    %eax, %eax                           | leal    1024(%r10), %r12d
popq    %rbp                                 | sarl    $11, %r12d
popq    %r12                                 | addl    $1, %eax
movl    %r13d, 12(%r8)                       | sarl    %eax
movl    %r11d, (%r8)                         | addl    %edx, %eax
popq    %r13                                 | movl    12(%rcx), %edx
movl    %r10d, 8(%r8)                        | addl    $1024, %edx
popq    %r14                                 | sarl    $11, %edx
popq    %r15                                 | imull   %r12d, %edx
ret                                          | movl    %r11d, %r12d
                                             | addl    $1, %edx
                                             | sarl    %edx
                                             | subl    %edx, %eax
                                             | movl    16(%rcx), %edx
                                             | addl    $1024, %edx
                                             | sarl    $11, %edx
                                             | imull   %ebp, %edx
                                             | movl    %r10d, %ebp
                                             | addl    $1, %edx
                                             | addq    $4, %rsi
                                             | addq    $4, %r9
                                             | sarl    %edx
                                             | subl    %edx, %eax
                                             | movl    %eax, -4(%r9)
                                             | cmpq    %rsi, %rbx
                                             | jne     .L26
                                             | movd    %eax, %xmm1
                                             | movd    %r10d, %xmm2
                                             | movd    %edi, %xmm0
                                             | movd    %r11d, %xmm3
                                             | punpckldq       %xmm2, %xmm1
                                             | punpckldq       %xmm3, %xmm0
                                             | punpcklqdq      %xmm1, %xmm0
                                             | .L24:
                                             | popq    %rbx
                                             | xorl    %eax, %eax
                                             | popq    %rbp
                                             | movups  %xmm0, (%r8)
                                             | popq    %r12
                                             | popq    %r13
                                             | ret

function                     role       insns  calls
dsps_biquad_f32_ansi [cc#1]  baseline   59     SMULR6
dsps_biquad_f32_ansi [cc#2]  candidate  89     -
```

The output prints a legend mapping `cc#N` tags to the full compiler
invocations, then one section per baseline/candidate pairing. Runnable
against the bundled example file:

```
$ asmdiff.py asmdiff_example.c --across new_rt --cc 'gcc -O0' --cc 'gcc -O3'

cc#1: gcc -O0
cc#2: gcc -O3

== cc#1 vs cc#2 ==

new_rt [cc#1]                                | new_rt [cc#2]
---------------------------------------------+---------------------------------------------
endbr64                                      | endbr64
pushq   %rbp                                 | jmp     ldexpf@PLT
movq    %rsp, %rbp                           |
subq    $16, %rsp                            |
movss   %xmm0, -4(%rbp)                      |
movl    %edi, -8(%rbp)                       |
movl    -8(%rbp), %edx                       |
movl    -4(%rbp), %eax                       |
movl    %edx, %edi                           |
movd    %eax, %xmm0                          |
call    ldexpf@PLT                           |
leave                                        |
ret                                          |

function       role       insns  calls
new_rt [cc#1]  baseline   13     ldexpf
new_rt [cc#2]  candidate  2      ldexpf
```

**Two files** — before/after versions of a source file (e.g. from a git
worktree, a branch checkout, or a patched copy). Each compiler in the matrix
gets its own section:

```bash
git worktree add ../baseline main
asmdiff.py ../baseline/src/filters.c src/filters.c \
    --across dsps_biquad_f32_ansi
```

Here the tags in the output are the two file paths (shortened to their
distinct suffix) instead of `cc#N` — the worked example in the next section
shows a full result of this shape.

Because C quote-includes (`#include "amy.h"`) resolve relative to the
including file first, each tree picks up **its own** headers automatically —
so a change made in a header (a macro, a typedef) is compared by pointing
`--across` at any `.c` file that uses it, without touching that `.c` file.

## Worked example: exp2f vs ldexpf in shorepine/AMY sources

Suppose the proposal is to change AMY's float-mode shift macros in
`src/amy_fixedpoint.h` from `(s) * exp2f(b)` to `ldexpf((s), (b))`. No
harness needed — compare the real functions the macros expand into:

```bash
# 1. A pristine baseline tree (any ref works)
git worktree add ../amy-baseline HEAD

# 2. The macros in question only exist in the float build, so enable it in
#    BOTH trees: comment out `#define AMY_USE_FIXEDPOINT` in src/amy.h
#    (it is hardcoded there).

# 3. In the working tree only, apply the candidate change in
#    src/amy_fixedpoint.h:
#      #define SHIFTR(s, b) ldexpf((s), -(b))
#      #define SHIFTL(s, b) ldexpf((s), (b))

# 4. Compare real functions containing both kinds of shift site:
asmdiff.py ../amy-baseline/src/log2_exp2.c src/log2_exp2.c \
    --across exp2_lut --across log2_lut --cc 'gcc -O3 -Wall'

# 5. Clean up
git worktree remove ../amy-baseline
```

`src/log2_exp2.c` is a good probe because it contains both site kinds:
`exp2_lut` shifts by a **runtime** amount, `log2_lut` by **constants**.
The summary makes the trade-off immediate:

```
function                              role       insns  calls
exp2_lut [amy-baseline/log2_exp2.c]   baseline   65     exp2f
exp2_lut [amy/log2_exp2.c]            candidate  59     ldexpf
log2_lut [amy-baseline/log2_exp2.c]   baseline   58     -
log2_lut [amy/log2_exp2.c]            candidate  64     ldexpf
```

The runtime site improves (a leaner libcall replaces `exp2f` + multiply),
but the constant site regresses: baseline `log2_lut` had **no** calls —
`exp2f(±1)` folds to a multiply — while the candidate now pays a `ldexpf`
libcall inside its normalisation loop. Any other `.c` file whose hot
functions use the macros (`filters.c`, `oscillators.c`, `delay.c`) can be
probed the same way.

## How it works

1. Each compiler runs with `-S` to emit assembly text.
2. Function bodies are sliced out between the function's label and its
   `.size` directive (or the next function label). CFI/section/alignment
   directives, comments, and compiler bracketing labels are stripped;
   instructions and meaningful local labels (loop targets) are kept.
3. Instruction counts and outbound calls come from a mnemonic scan covering
   x86 (`call`, `jmp` tail calls), ARM (`bl`, `blx`), RISC-V (`call`,
   `tail`, `jal`), and Xtensa (`call0/4/8/12`, `callx*`, `j`). Local-label
   branches and register-indirect x86 jumps are not counted as calls.

No verdicts are printed. The tool reports facts; whether a libcall on that
path matters is your judgment.

## Writing good harnesses

- Give variants **runtime arguments** for anything that is runtime in the
  real code, and **literals** for anything that is compile-time constant
  there. The fold-vs-libcall answer depends on exactly this.
- Keep functions non-`static` so the compiler must emit them standalone.
- Compile at the **flags your project ships with** — a construct that folds
  at `-O3 -ffast-math` may not fold at plain `-O3`. The default flags here
  are AMY's for that reason.
- Beware of over-synthetic harnesses: a function whose whole body is the
  construct can tail-call (`jmp f`) where real surrounding code would
  `call f` and continue. Same libcall either way, but instruction counts
  read differently.

## Porting to another project

The tool is one stdlib-only Python 3 file with no imports outside the
standard library. To port:

1. Copy this directory (or just `asmdiff.py`).
2. Edit the constants at the top of `asmdiff.py`:
   - `AMY_CFLAGS` — replace with your project's real build flags.
   - `SRC_DIR` — points at AMY's `src/` for `#include "amy.h"` harnesses;
     it is only added when the directory exists, so you can delete the
     block or repoint it at your include directory.
   - `DEFAULT_COMPILERS` — the compilers run when `--cc` is not given.
3. Run the self-tests: `python3 test_asmdiff.py -v` (17 tests, no compiler
   needed).

## Limitations

- Parses **GNU-as ELF** assembly (`gcc`, `clang`, and GNU cross-compilers
  targeting ELF). macOS Mach-O asm (`_name` labels, no `.size`) is not
  supported — on a Mac, compare inside a Linux container or with a
  cross-toolchain.
- Call detection is a mnemonic heuristic. Register-indirect calls through a
  loaded address (other than x86 `jmp *reg`) can be reported as a call to
  the register's name (e.g. Xtensa `callx8 a10`), which errs toward
  visibility rather than silence.
- Columns truncate long instruction lines to keep pairs aligned; when a
  line matters, widen it via the `width` parameter of `side_by_side()` or
  read the raw `-S` output by hand

  ---
## pretend FAQ
### Why not just run objdump by hand?

The two commands above (steps 4–5) replace a manual workflow with real
friction at every step. Walking through it end to end on a single,
one-sided example — did `x * exp2f(-5)` fold to a multiply, or did
`ldexpf(x, n)` become a libcall — shows where the effort goes.

**1. Compile to an object, remembering every project flag by hand.**

```bash
gcc -O3 -Wall -Wno-strict-aliasing -Wextra -Wno-unused-parameter \
    -Wpointer-arith -Wno-float-conversion -Wno-missing-declarations \
    -DAMY_WAVETABLE -Isrc -c src/log2_exp2.c -o /tmp/candidate.o
```

Drop one flag (say `-Wno-float-conversion`) and nothing errors — the build
just quietly takes a different codegen path, and the comparison you're
about to make is invalid without telling you so. Repeat this for the
baseline tree with its own `-I`, and again for every extra compiler you
want in the matrix.

**2. Disassemble the function out of the object.**

```bash
objdump -dr --no-show-raw-insn -M no-aliases /tmp/candidate.o
```

For a libcall site (`ldexpf(x, n)` with a runtime `n`), the real output is:

```
0000000000000000 <g>:
   0:	endbr64
   4:	jmp    9 <g+0x9>
			5: R_X86_64_PLT32	ldexpf-0x4
```

The call target isn't in the instruction — `jmp 9 <g+0x9>` points at an
unresolved stub inside the same function. The actual symbol, `ldexpf`, only
shows up in the relocation line underneath, and you have to know to cross-
reference it by hand. Compare that to `gcc -S`, which prints the symbol
inline because it hasn't been through a linker/relocation step yet:

```
g:
	endbr64
	jmp	ldexpf@PLT
```

That's why asmdiff compiles with `-S` instead of going through `objdump` on
a linked object — the thing you're looking for (is this a libcall, and to
what) is already text, not a relocation entry you have to decode.

**3. Strip the noise objdump adds that `-S` doesn't.** Every instruction
line carries a leading address and (unless `--no-show-raw-insn` is passed)
raw opcode bytes; there's a `file format elf64-x86-64` banner, a
`Disassembly of section .text:` header, and an address-annotated function
label instead of a bare one. None of it is informative for a codegen diff,
all of it has to be deleted by hand before two functions are readable
side by side — and it has to be deleted from **every** file in the
comparison, four of them for the two-function/two-tree case above.

**4. Diff the cleaned pair.** `diff -y --width=100 old.txt new.txt` aligns
by content match, not position — once the two versions diverge even
slightly it starts pairing unrelated lines, and it has no header row to
label which side is which. `asmdiff` prints its own aligned columns
(`side_by_side()`) with the two function names as headers, and never loses
the pairing because it doesn't try to align by content — it just walks
both lists in lockstep.

**5. Count instructions and classify calls by hand.** Grep for `call`/`jmp`
in the cleaned text, then manually exclude the ones that are really local
branches (`jmp 4011a0 <exp2_lut+0x40>`) rather than calls to another
symbol — the exact distinction `CALL_RE` in `asmdiff.py` encodes once so
you don't re-derive it per function. Then hand-build a table from four
separate counts.

**6. Do all of the above again per compiler.** asmdiff's default matrix is
gcc *and* clang; by hand that's every step above, twice.

For the full worked example — two functions, two trees, one compiler —
the manual version is roughly: 2 compiles (with hand-retyped flags) → 4
`objdump`/relocation-lookup passes → noise-stripped by hand on 4 files →
2 `diff -y` runs that don't survive drift → manual instruction counts and
call classification on 4 files → a hand-assembled summary table. The
`asmdiff.py` version is the one command already shown above. Neither
workflow can skip understanding *why* the two functions differ — that part
is still your judgment — but everything upstream of that judgment, where a
dropped flag or a misread relocation silently invalidates the comparison,
is what the tool removes.

### Why not just run gcc -S by hand?

`-S` output sidesteps the relocation-decoding problem above — call targets
are already symbolic text, no PLT stub to resolve. That removes step 2 of
the objdump workflow. It does not remove the rest.

**1. Compile to text instead of an object** — same flags, same risk of a
silently dropped one:

```bash
gcc -O3 -Wall -Wno-strict-aliasing -Wextra -Wno-unused-parameter \
    -Wpointer-arith -Wno-float-conversion -Wno-missing-declarations \
    -DAMY_WAVETABLE -Isrc -S src/log2_exp2.c -o /tmp/log2_exp2.s
```

**2. Find where the function starts and ends in the `.s` file.** The real
output for `exp2_lut` in this repo (current build, `AMY_USE_FIXEDPOINT`
on):

```
exp2_lut:
.LFB71:
	.cfi_startproc
	endbr64
	movl	%edi, %edx
	leaq	2+exp2_fxpt_lutable(%rip), %rcx
	...
	ret
	.cfi_endproc
.LFE71:
	.size	exp2_lut, .-exp2_lut
```

There's no `objdump`-style address column to strip, but you still have to
find the boundary by hand: the function starts at a column-0 label
(`exp2_lut:`, not `.LFB71:` — that's a bracketing label, not the function),
and ends at its `.size` directive — which only gcc reliably emits; on a
compiler that doesn't, you'd fall back to "next function label", which is
exactly the two-case rule `extract_functions()` implements once instead of
you re-deriving it per file.

**3. Strip compiler furniture — but not indiscriminately.** `.cfi_*`,
`.LFB`/`.LFE` bracket labels, and `.p2align` carry no information. A local
`.L`-numbered label sometimes does, though, and you can't tell which
without reading the body. `log2_lut` in the same file:

```
log2_lut:
.LFB70:
	.cfi_startproc
	endbr64
	xorl	%eax, %eax
	cmpl	$8388607, %edi
	jg	.L9
	.p2align 4,,10
	.p2align 3
.L3:
	addl	%edi, %edi
	subl	$1, %eax
	cmpl	$8388607, %edi
	jle	.L3
	cmpl	$16777215, %edi
	jle	.L11
	.p2align 4,,10
	.p2align 3
.L5:
	sarl	%edi
	addl	$1, %eax
.L9:
	cmpl	$16777215, %edi
	jg	.L5
.L11:
	...
```

`.L3`, `.L5`, `.L9`, `.L11` are live loop targets — `jg .L9` and `jle .L3`
jump to them. A quick-and-dirty cleanup pass like `grep -v '^\.'` (strip
every line starting with a dot) deletes those labels along with the
`.p2align` noise sitting right next to them, and now the function has
dangling jumps to labels that no longer exist — silently wrong, not an
error. The correct rule is "drop this specific set of directives and this
specific set of *bracketing* labels, keep everything else" — which is a
narrower, easier-to-get-wrong rule than it looks, and it's what `NOISE`
and `NOISE_LABEL` encode once in `asmdiff.py` instead of per file.

**4. Everything downstream is unchanged from the objdump case:** pair the
two cleaned functions up for reading, count instructions, classify
`call`/`jmp` lines as libcalls vs. local branches, repeat per function,
per file, per compiler, and assemble a summary table by hand.

So `-S` over `objdump` buys back exactly one step — the call target is
already a name, not a relocation to look up — and leaves the rest of the
manual pipeline (locate, strip correctly, pair, count, classify, tally,
multiplied by every function/tree/compiler in the matrix) in place. That
remaining pipeline is `extract_functions()`, `analyze()`,
`side_by_side()`, and `summary_table()` in `asmdiff.py` — written once,
instead of re-derived by hand every time someone wants to answer "did this
still fold?"

