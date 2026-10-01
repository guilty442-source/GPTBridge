// cuda_ptx_gemv.h — embedded PTX skinny-m split-k GEMV kernels (B132):
//   xc_gemv_bf16_part  bf16 a/b → fp32 split-k partials; one thread per
//                      output column, all ≤16 rows fused in registers
//                      (acc[r] = fma(av, bv, acc[r]) in k order).
//   xc_gemv_fp8_part   same shape; a is device fp32, b is E4M3 bytes
//                      decoded through xc_fp8_dec.
//   xc_gemv_reduce     shared pass 2: c[r][col] = Σ_s part[s][r][col]
//                      in fixed slice order — deterministic.

#pragma once

namespace xcuda_ptx {

inline const char* gemv() {
    return R"PTX(
// ---- bf16 split-k GEMV pass 1 ----------------------------------------
.visible .entry xc_gemv_bf16_part(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_part,
    .param .u32 %p_m, .param .u32 %p_k, .param .u32 %p_n,
    .param .u32 %p_ksplit, .param .u32 %p_kchunk)
{
    .reg .pred %p<6>;
    .reg .b16  %rs<3>;
    .reg .b32  %r<10>;
    .reg .b64  %rd<24>;
    .reg .f32  %f<32>;
    ld.param.u64 %rd1, [%p_a];
    ld.param.u64 %rd2, [%p_b];
    ld.param.u64 %rd3, [%p_part];
    ld.param.u32 %r1, [%p_m];
    ld.param.u32 %r2, [%p_k];
    ld.param.u32 %r3, [%p_n];
    ld.param.u32 %r4, [%p_kchunk];
    mov.u32 %r5, %ctaid.x;
    mov.u32 %r6, %ntid.x;
    mov.u32 %r7, %tid.x;
    mul.lo.u32 %r8, %r5, %r6;
    add.u32 %r8, %r8, %r7;                        // col (u32)
    setp.ge.u32 %p1, %r8, %r3;
    @%p1 bra XC_GVB_END;
    cvt.u64.u32 %rd8, %r8;                        // col
    cvt.u64.u32 %rd4, %r1;                        // m64
    cvt.u64.u32 %rd5, %r2;                        // k64
    cvt.u64.u32 %rd6, %r3;                        // n64
    mov.u32 %r9, %ctaid.y;
    cvt.u64.u32 %rd9, %r9;
    cvt.u64.u32 %rd7, %r4;                        // kchunk64
    mul.lo.u64 %rd9, %rd9, %rd7;                  // i0
    add.u64 %rd10, %rd9, %rd7;
    setp.lt.u64 %p2, %rd10, %rd5;
    selp.u64 %rd10, %rd10, %rd5, %p2;             // i1 = min(k, i0+kc)
    mov.f32 %f16, 0f00000000;
    mov.f32 %f17, 0f00000000;
    mov.f32 %f18, 0f00000000;
    mov.f32 %f19, 0f00000000;
    mov.f32 %f20, 0f00000000;
    mov.f32 %f21, 0f00000000;
    mov.f32 %f22, 0f00000000;
    mov.f32 %f23, 0f00000000;
    mov.f32 %f24, 0f00000000;
    mov.f32 %f25, 0f00000000;
    mov.f32 %f26, 0f00000000;
    mov.f32 %f27, 0f00000000;
    mov.f32 %f28, 0f00000000;
    mov.f32 %f29, 0f00000000;
    mov.f32 %f30, 0f00000000;
    mov.f32 %f31, 0f00000000;
    add.u64 %rd14, %rd5, %rd5;                    // k*2 byte stride
    mov.u64 %rd11, %rd9;
XC_GVB_LOOP:                                      // for i in [i0,i1)
    setp.ge.u64 %p3, %rd11, %rd10;
    @%p3 bra XC_GVB_STORE;
    mul.lo.u64 %rd12, %rd11, %rd6;
    add.u64 %rd12, %rd12, %rd8;
    add.u64 %rd12, %rd12, %rd12;
    add.u64 %rd12, %rd2, %rd12;
    ld.global.u16 %rs1, [%rd12];
    cvt.u32.u16 %r6, %rs1;
    shl.b32 %r6, %r6, 16;
    mov.b32 %f1, %r6;                             // bv
    add.u64 %rd13, %rd11, %rd11;
    add.u64 %rd13, %rd1, %rd13;                   // &a[i]
    setp.gt.u64 %p4, %rd4, 0;
    @!%p4 bra XC_GVB_R0;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f16, %f2, %f1, %f16;
XC_GVB_R0:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 1;
    @!%p4 bra XC_GVB_R1;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f17, %f2, %f1, %f17;
XC_GVB_R1:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 2;
    @!%p4 bra XC_GVB_R2;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f18, %f2, %f1, %f18;
XC_GVB_R2:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 3;
    @!%p4 bra XC_GVB_R3;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f19, %f2, %f1, %f19;
XC_GVB_R3:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 4;
    @!%p4 bra XC_GVB_R4;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f20, %f2, %f1, %f20;
XC_GVB_R4:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 5;
    @!%p4 bra XC_GVB_R5;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f21, %f2, %f1, %f21;
XC_GVB_R5:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 6;
    @!%p4 bra XC_GVB_R6;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f22, %f2, %f1, %f22;
XC_GVB_R6:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 7;
    @!%p4 bra XC_GVB_R7;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f23, %f2, %f1, %f23;
XC_GVB_R7:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 8;
    @!%p4 bra XC_GVB_R8;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f24, %f2, %f1, %f24;
XC_GVB_R8:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 9;
    @!%p4 bra XC_GVB_R9;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f25, %f2, %f1, %f25;
XC_GVB_R9:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 10;
    @!%p4 bra XC_GVB_R10;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f26, %f2, %f1, %f26;
XC_GVB_R10:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 11;
    @!%p4 bra XC_GVB_R11;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f27, %f2, %f1, %f27;
XC_GVB_R11:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 12;
    @!%p4 bra XC_GVB_R12;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f28, %f2, %f1, %f28;
XC_GVB_R12:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 13;
    @!%p4 bra XC_GVB_R13;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f29, %f2, %f1, %f29;
XC_GVB_R13:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 14;
    @!%p4 bra XC_GVB_R14;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f30, %f2, %f1, %f30;
XC_GVB_R14:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 15;
    @!%p4 bra XC_GVB_R15;
    ld.global.u16 %rs2, [%rd13];
    cvt.u32.u16 %r7, %rs2;
    shl.b32 %r7, %r7, 16;
    mov.b32 %f2, %r7;
    fma.rn.f32 %f31, %f2, %f1, %f31;
XC_GVB_R15:
    add.u64 %rd11, %rd11, 1;
    bra XC_GVB_LOOP;
XC_GVB_STORE:
    // part base: (s*m + r)*n + col ; s = ctaid.y ; row stride n*4
    cvt.u64.u32 %rd16, %r9;
    mul.lo.u64 %rd16, %rd16, %rd4;
    mul.lo.u64 %rd16, %rd16, %rd6;                // s*m*n
    add.u64 %rd16, %rd16, %rd8;                   // +col
    shl.b64 %rd16, %rd16, 2;
    add.u64 %rd16, %rd3, %rd16;                   // &part[..]
    shl.b64 %rd17, %rd6, 2;                       // n*4
    setp.gt.u64 %p4, %rd4, 0;
    @!%p4 bra XC_GVB_S0;
    st.global.f32 [%rd16], %f16;
XC_GVB_S0:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 1;
    @!%p4 bra XC_GVB_S1;
    st.global.f32 [%rd16], %f17;
XC_GVB_S1:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 2;
    @!%p4 bra XC_GVB_S2;
    st.global.f32 [%rd16], %f18;
XC_GVB_S2:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 3;
    @!%p4 bra XC_GVB_S3;
    st.global.f32 [%rd16], %f19;
XC_GVB_S3:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 4;
    @!%p4 bra XC_GVB_S4;
    st.global.f32 [%rd16], %f20;
XC_GVB_S4:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 5;
    @!%p4 bra XC_GVB_S5;
    st.global.f32 [%rd16], %f21;
XC_GVB_S5:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 6;
    @!%p4 bra XC_GVB_S6;
    st.global.f32 [%rd16], %f22;
XC_GVB_S6:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 7;
    @!%p4 bra XC_GVB_S7;
    st.global.f32 [%rd16], %f23;
XC_GVB_S7:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 8;
    @!%p4 bra XC_GVB_S8;
    st.global.f32 [%rd16], %f24;
XC_GVB_S8:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 9;
    @!%p4 bra XC_GVB_S9;
    st.global.f32 [%rd16], %f25;
XC_GVB_S9:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 10;
    @!%p4 bra XC_GVB_S10;
    st.global.f32 [%rd16], %f26;
XC_GVB_S10:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 11;
    @!%p4 bra XC_GVB_S11;
    st.global.f32 [%rd16], %f27;
XC_GVB_S11:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 12;
    @!%p4 bra XC_GVB_S12;
    st.global.f32 [%rd16], %f28;
XC_GVB_S12:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 13;
    @!%p4 bra XC_GVB_S13;
    st.global.f32 [%rd16], %f29;
XC_GVB_S13:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 14;
    @!%p4 bra XC_GVB_S14;
    st.global.f32 [%rd16], %f30;
XC_GVB_S14:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 15;
    @!%p4 bra XC_GVB_S15;
    st.global.f32 [%rd16], %f31;
XC_GVB_S15:
XC_GVB_END:
    ret;
}

// ---- split-k reduce (shared pass 2): c[r][col] = Σ_s part[s][r][col] -
.visible .entry xc_gemv_reduce(
    .param .u64 %p_part, .param .u64 %p_c,
    .param .u32 %p_m, .param .u32 %p_n, .param .u32 %p_ksplit)
{
    .reg .pred %p<4>;
    .reg .b32  %r<10>;
    .reg .b64  %rd<16>;
    .reg .f32  %f<4>;
    ld.param.u64 %rd1, [%p_part];
    ld.param.u64 %rd2, [%p_c];
    ld.param.u32 %r1, [%p_m];
    ld.param.u32 %r2, [%p_n];
    ld.param.u32 %r3, [%p_ksplit];
    mov.u32 %r4, %ctaid.x;
    mov.u32 %r5, %ntid.x;
    mov.u32 %r6, %tid.x;
    mul.lo.u32 %r7, %r4, %r5;
    add.u32 %r7, %r7, %r6;                        // col
    setp.ge.u32 %p1, %r7, %r2;
    @%p1 bra XC_GVR_END;
    cvt.u64.u32 %rd4, %r7;                        // col64
    cvt.u64.u32 %rd5, %r1;                        // m64
    cvt.u64.u32 %rd6, %r2;                        // n64
    cvt.u64.u32 %rd7, %r3;                        // ksplit64
    mul.lo.u64 %rd8, %rd5, %rd6;                  // m*n slice stride
    mov.u64 %rd9, 0;                              // r
XC_GVR_ROW:
    setp.ge.u64 %p2, %rd9, %rd5;
    @%p2 bra XC_GVR_END;
    mov.f32 %f1, 0f00000000;
    mul.lo.u64 %rd10, %rd9, %rd6;
    add.u64 %rd10, %rd10, %rd4;                   // r*n + col
    mov.u64 %rd11, %rd10;                         // part idx base
    mov.u64 %rd12, 0;                             // s
XC_GVR_SLICE:
    setp.ge.u64 %p3, %rd12, %rd7;
    @%p3 bra XC_GVR_ST;
    mul.lo.u64 %rd13, %rd12, %rd8;
    add.u64 %rd13, %rd13, %rd11;
    shl.b64 %rd13, %rd13, 2;
    add.u64 %rd13, %rd1, %rd13;
    ld.global.f32 %f2, [%rd13];
    add.rn.f32 %f1, %f1, %f2;
    add.u64 %rd12, %rd12, 1;
    bra XC_GVR_SLICE;
XC_GVR_ST:
    shl.b64 %rd13, %rd10, 2;
    add.u64 %rd13, %rd2, %rd13;
    st.global.f32 [%rd13], %f1;
    add.u64 %rd9, %rd9, 1;
    bra XC_GVR_ROW;
XC_GVR_END:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
