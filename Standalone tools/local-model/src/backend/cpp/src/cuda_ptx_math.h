// cuda_ptx_math.h -- embedded PTX device helpers for the native CUDA
// lane (B132): shared .func subroutines called by the module's kernels.
// Text is authored in-tree and JIT-compiled by the NVIDIA driver via
// cuModuleLoadData -- no toolkit, NVRTC or external library involved.
//
//   xc_exp_f64  IEEE-f64 exp(): Cody-Waite ln2 reduction + degree-15
//               Horner + 2^k exponent scale. |rel err| ? 1e-15 on the
//               contracted range -- matches host exp() within the fp64
//               parity tolerance (1e-9).
//   xc_fp8_dec  E4M3 (nv fp8e4m3fn) byte -> f32, identical semantics to
//               the CUDA FP8 pipeline's conversion: subnormal = m*2^-9,
//               e+120 bias shift, canonical NaN = 0x7fc00000.

#pragma once

namespace xcuda_ptx {

inline const char* math() {
    return R"PTX(
// ---- f64 exp() -- Cody-Waite + Horner + exponent scale -------------
.func (.param .b64 %p_out) xc_exp_f64 (.param .b64 %p_in)
{
    .reg .pred %p<5>;
    .reg .b32  %r<4>;
    .reg .b64  %rd<4>;
    .reg .f64  %fd<16>;
    ld.param.f64 %fd1, [%p_in];
    setp.lt.f64 %p1, %fd1, 0dC0874910D52D3051;   // x < -745.1332191019411
    @%p1 bra XC_EXP_ZERO;
    setp.gt.f64 %p2, %fd1, 0d40862E42FEFA39EF;   // x > 709.782712893384
    @%p2 bra XC_EXP_INF;
    mul.rn.f64 %fd2, %fd1, 0d3FF71547652B82FE;   // x * log2(e)
    cvt.rni.s32.f64 %r1, %fd2;                   // k = rni
    cvt.rn.f64.s32 %fd3, %r1;
    neg.f64 %fd4, %fd3;
    fma.rn.f64 %fd5, %fd4, 0d3FE62E42FEE00000, %fd1; // x - k*ln2_hi
    fma.rn.f64 %fd5, %fd4, 0d3DEA39EF35793C76, %fd5; //   - k*ln2_lo
    mov.f64 %fd6, 0d3D6AE7F3E733B81F;            // 1/15!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3DA93974A8C07C9D; // +1/14!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3DE6124613A86D09; // +1/13!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3E21EED8EFF8D898; // +1/12!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3E5AE64567F544E4; // +1/11!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3E927E4FB7789F5C; // +1/10!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3EC71DE3A556C734; // +1/9!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3EFA01A01A01A01A; // +1/8!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3F2A01A01A01A01A; // +1/7!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3F56C16C16C16C17; // +1/6!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3F81111111111111; // +1/5!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3FA5555555555555; // +1/4!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3FC5555555555555; // +1/3!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3FE0000000000000; // +1/2!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3FF0000000000000; // +1/1!
    fma.rn.f64 %fd6, %fd6, %fd5, 0d3FF0000000000000; // +1/0!
    add.s32 %r2, %r1, 1023;                      // biased 2^k exponent
    setp.le.s32 %p3, %r2, 0;
    @%p3 bra XC_EXP_ZERO;                        // underflow -> 0
    setp.ge.s32 %p4, %r2, 2047;
    @%p4 bra XC_EXP_BIG;                         // k >= 1024 -> two-step
    cvt.s64.s32 %rd1, %r2;
    shl.b64 %rd1, %rd1, 52;
    mov.b64 %fd7, %rd1;                          // 2^k
    mul.rn.f64 %fd8, %fd6, %fd7;
    st.param.f64 [%p_out], %fd8;
    ret;
XC_EXP_BIG:
    mul.rn.f64 %fd9, %fd6, 0d7FE0000000000000;   // e * 2^1023
    mul.rn.f64 %fd8, %fd9, 0d4000000000000000;   //   * 2
    st.param.f64 [%p_out], %fd8;
    ret;
XC_EXP_ZERO:
    st.param.f64 [%p_out], 0d0000000000000000;
    ret;
XC_EXP_INF:
    st.param.f64 [%p_out], 0d7FF0000000000000;
    ret;
}

// ---- E4M3 fp8 byte -> f32 (nv-fp8 semantics: subnormal m*2^-9) ----
.func (.param .b32 %p_out) xc_fp8_dec (.param .b32 %p_in)
{
    .reg .pred %p<5>;
    .reg .b32  %r<12>;
    .reg .f32  %f<4>;
    ld.param.u32 %r1, [%p_in];
    and.b32 %r1, %r1, 255;
    and.b32 %r2, %r1, 128;           // sign (bit7 -> bit31 later)
    shr.u32 %r3, %r1, 3;
    and.b32 %r3, %r3, 15;            // e4
    and.b32 %r4, %r1, 7;             // m3
    setp.eq.u32 %p1, %r3, 15;
    setp.eq.u32 %p2, %r4, 7;
    and.pred %p3, %p1, %p2;
    @%p3 bra XC_FP8D_NAN;            // e=15,m=7 -> NaN
    setp.eq.u32 %p4, %r3, 0;
    @%p4 bra XC_FP8D_SUB;
    add.u32 %r5, %r3, 120;           // f32 bias = e4-7+127
    shl.b32 %r5, %r5, 23;
    shl.b32 %r6, %r4, 20;
    or.b32  %r5, %r5, %r6;
    shl.b32 %r7, %r2, 24;            // sign -> bit31
    or.b32  %r5, %r5, %r7;
    st.param.b32 [%p_out], %r5;
    ret;
XC_FP8D_SUB:
    cvt.rn.f32.u32 %f1, %r4;
    mul.rn.f32 %f1, %f1, 0f3B000000; // m * 2^-9
    mov.b32 %r8, %f1;
    shl.b32 %r7, %r2, 24;
    or.b32  %r8, %r8, %r7;
    st.param.b32 [%p_out], %r8;
    ret;
XC_FP8D_NAN:
    st.param.b32 [%p_out], 0x7fc00000;
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
