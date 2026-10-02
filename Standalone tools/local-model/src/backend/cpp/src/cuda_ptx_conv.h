// cuda_ptx_conv.h -- embedded PTX conversion kernels (B132):
//   xc_f64_to_bf16  f64 -> bf16u16 (RN, via cvt.rn.bf16.f32)
//   xc_f64_to_fp32  f64 -> f32   (RN)
//   xc_f64_to_fp8   f64 -> fp8 E4M3 (RNE, satfinite) -- same semantics as
//                   the CUDA FP8 pipeline (double->half->e4m3): NaN->0x7f,
//                   |x|>=464->+/-448, subnormal via *2^9 + round-to-nearest-int.

#pragma once

namespace xcuda_ptx {

inline const char* conv() {
    return R"PTX(
// ---- f64 -> bf16 (u16) ----------------------------------------------
.visible .entry xc_f64_to_bf16(
    .param .u64 %p_in, .param .u64 %p_out, .param .u64 %p_n)
{
    .reg .pred %p<2>;
    .reg .b32  %r<6>;
    .reg .b64  %rd<8>;
    .reg .f64  %fd<2>;
    .reg .f32  %f<2>;
    .reg .b16  %rs<2>;
    ld.param.u64 %rd1, [%p_in];
    ld.param.u64 %rd2, [%p_out];
    ld.param.u64 %rd3, [%p_n];
    mov.u32 %r1, %ctaid.x;
    mov.u32 %r2, %ntid.x;
    mov.u32 %r3, %tid.x;
    cvt.u64.u32 %rd4, %r1;
    cvt.u64.u32 %rd5, %r2;
    mul.lo.u64 %rd4, %rd4, %rd5;
    cvt.u64.u32 %rd5, %r3;
    add.u64 %rd4, %rd4, %rd5;                    // i
    setp.ge.u64 %p1, %rd4, %rd3;
    @%p1 bra XC_CVT16_DONE;
    mul.lo.u64 %rd5, %rd4, 8;
    add.u64 %rd5, %rd1, %rd5;
    ld.global.f64 %fd1, [%rd5];
    cvt.rn.f32.f64 %f1, %fd1;
    cvt.rn.bf16.f32 %rs1, %f1;
    add.u64 %rd5, %rd4, %rd4;
    add.u64 %rd5, %rd2, %rd5;
    st.global.u16 [%rd5], %rs1;
XC_CVT16_DONE:
    ret;
}

// ---- f64 -> f32 ------------------------------------------------------
.visible .entry xc_f64_to_fp32(
    .param .u64 %p_in, .param .u64 %p_out, .param .u64 %p_n)
{
    .reg .pred %p<2>;
    .reg .b32  %r<6>;
    .reg .b64  %rd<8>;
    .reg .f64  %fd<2>;
    .reg .f32  %f<2>;
    ld.param.u64 %rd1, [%p_in];
    ld.param.u64 %rd2, [%p_out];
    ld.param.u64 %rd3, [%p_n];
    mov.u32 %r1, %ctaid.x;
    mov.u32 %r2, %ntid.x;
    mov.u32 %r3, %tid.x;
    cvt.u64.u32 %rd4, %r1;
    cvt.u64.u32 %rd5, %r2;
    mul.lo.u64 %rd4, %rd4, %rd5;
    cvt.u64.u32 %rd5, %r3;
    add.u64 %rd4, %rd4, %rd5;
    setp.ge.u64 %p1, %rd4, %rd3;
    @%p1 bra XC_CVT32_DONE;
    mul.lo.u64 %rd5, %rd4, 8;
    add.u64 %rd5, %rd1, %rd5;
    ld.global.f64 %fd1, [%rd5];
    cvt.rn.f32.f64 %f1, %fd1;
    shl.b64 %rd5, %rd4, 2;
    add.u64 %rd5, %rd2, %rd5;
    st.global.f32 [%rd5], %f1;
XC_CVT32_DONE:
    ret;
}

// ---- f64 -> fp8 E4M3 (RNE, satfinite) --------------------------------
.visible .entry xc_f64_to_fp8(
    .param .u64 %p_in, .param .u64 %p_out, .param .u64 %p_n)
{
    .reg .pred %p<7>;
    .reg .b32  %r<16>;
    .reg .b64  %rd<8>;
    .reg .f64  %fd<2>;
    .reg .f32  %f<3>;
    ld.param.u64 %rd1, [%p_in];
    ld.param.u64 %rd2, [%p_out];
    ld.param.u64 %rd3, [%p_n];
    mov.u32 %r1, %ctaid.x;
    mov.u32 %r2, %ntid.x;
    mov.u32 %r3, %tid.x;
    cvt.u64.u32 %rd4, %r1;
    cvt.u64.u32 %rd5, %r2;
    mul.lo.u64 %rd4, %rd4, %rd5;
    cvt.u64.u32 %rd5, %r3;
    add.u64 %rd4, %rd4, %rd5;
    setp.ge.u64 %p1, %rd4, %rd3;
    @%p1 bra XC_FP8_DONE;
    mul.lo.u64 %rd5, %rd4, 8;
    add.u64 %rd5, %rd1, %rd5;
    ld.global.f64 %fd1, [%rd5];
    cvt.rn.f32.f64 %f1, %fd1;
    mov.b32 %r4, %f1;
    and.b32 %r5, %r4, 0x80000000;
    shr.u32 %r5, %r5, 24;                        // sign -> bit7
    and.b32 %r6, %r4, 0x7fffffff;                // |x| bits
    setp.gt.u32 %p2, %r6, 0x7f800000;
    @%p2 bra XC_FP8_NAN;                         // NaN -> 0x7f
    setp.ge.u32 %p3, %r6, 0x43E80000;
    @%p3 bra XC_FP8_SAT;                         // |x| >= 464 -> +/-448
    shr.u32 %r7, %r6, 23;                        // f32 biased exp
    add.s32 %r8, %r7, -120;                      // e4m3 biased exp
    setp.le.s32 %p4, %r8, 0;
    @%p4 bra XC_FP8_SUB;
    shr.u32 %r9, %r6, 20;
    and.b32 %r9, %r9, 1;                         // kept-mantissa LSB
    add.u32 %r10, %r6, 0x7ffff;                  // RNE: +half-ulp-1
    add.u32 %r10, %r10, %r9;                     //        +lsb (tie-even)
    shr.u32 %r11, %r10, 23;
    add.s32 %r11, %r11, -120;                    // post-carry exp
    setp.gt.s32 %p5, %r11, 15;
    @%p5 bra XC_FP8_SAT;
    shl.b32 %r11, %r11, 3;
    shr.u32 %r12, %r10, 20;
    and.b32 %r12, %r12, 7;
    or.b32 %r13, %r11, %r12;
    or.b32 %r13, %r13, %r5;
    bra XC_FP8_ST;
XC_FP8_SUB:                                      // |x| < 2^-6
    mov.b32 %f2, %r6;
    mul.rn.f32 %f2, %f2, 0f44000000;             // x*512 -> subnorm steps
    cvt.rni.s32.f32 %r13, %f2;                   // RNE to integer
    or.b32 %r13, %r13, %r5;
    bra XC_FP8_ST;
XC_FP8_NAN:
    mov.u32 %r13, 0x7f;
    bra XC_FP8_ST;
XC_FP8_SAT:
    mov.u32 %r13, 0x7e;
    or.b32 %r13, %r13, %r5;
XC_FP8_ST:
    add.u64 %rd6, %rd2, %rd4;
    st.global.u8 [%rd6], %r13;
XC_FP8_DONE:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
