/* Worked example for asmdiff.py: constant-folding of x * exp2f(k) versus
 * the ldexpf(x, k) libcall, for constant and runtime shift amounts.
 *
 * Run:  tools/asmdiff/asmdiff.py tools/asmdiff/asmdiff_example.c
 *
 * Expected at -O3: old_const folds to a bare multiply (no calls);
 * new_const lowers to an ldexpf call — neither gcc nor clang has a
 * constant-fold rule for a literal ldexpf exponent.  For a runtime
 * shift the roles reverse: old_rt needs int->float conversion plus an
 * exp2f call plus a multiply, new_rt is a lone ldexpf tail call.
 */
#include <math.h>

float old_const(float x) { return x * exp2f(-(5)); }
float new_const(float x) { return ldexpf(x, -(5)); }

float old_rt(float x, int b) { return x * exp2f((float)b); }
float new_rt(float x, int b) { return ldexpf(x, b); }
