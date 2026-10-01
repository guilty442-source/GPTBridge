// cuda_ptx_fp8.h -- embedded PTX fp8 (E4M3) kernels (B132):
//   xc_gemm_fp8       fp32 a x fp8 b -> fp32 c; shared-tile decode hoist
//                     (each b element decoded once via xc_fp8_dec).
//   xc_gemv_fp8_part  skinny-m split-k GEMV pass 1 -- same fused-row
//                     register pattern as the bf16 lane.

#pragma once

namespace xcuda_ptx {

inline const char* fp8() {
    return R"PTX(
// ---- fp8 tiled GEMM: fp32 shared tiles, fp32 accumulate -------------
.visible .entry xc_gemm_fp8(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u32 %p_m, .param .u32 %p_k, .param .u32 %p_n)
{
    .shared .align 4 .b8 xc_g8_as[1024];
    .shared .align 4 .b8 xc_g8_bs[1024];
    .reg .pred %p<8>;
    .reg .b32  %r<12>;
    .reg .b64  %rd<20>;
    .reg .f32  %f<10>;
    .param .b32 %po_dec, %pi_dec;
    ld.param.u64 %rd1, [%p_a];
    ld.param.u64 %rd2, [%p_b];
    ld.param.u64 %rd3, [%p_c];
    ld.param.u32 %r1, [%p_m];
    ld.param.u32 %r2, [%p_k];
    ld.param.u32 %r3, [%p_n];
    mov.u32 %r4, %ctaid.y;
    mov.u32 %r5, %ctaid.x;
    mov.u32 %r6, %tid.y;
    mov.u32 %r7, %tid.x;
    shl.b32 %r8, %r4, 4;
    add.s32 %r8, %r8, %r6;                        // row
    shl.b32 %r9, %r5, 4;
    add.s32 %r9, %r9, %r7;                        // col
    cvt.u64.u32 %rd7, %r8;
    cvt.u64.u32 %rd8, %r9;
    cvt.u64.u32 %rd4, %r2;                        // k64
    cvt.u64.u32 %rd5, %r3;                        // n64
    mov.f32 %f1, 0f00000000;                      // acc
    add.u64 %rd9, %rd4, 15;
    shr.u64 %rd9, %rd9, 4;                        // nt
    mov.u64 %rd10, 0;                             // t
XC_G8_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_G8_OUT;
    shl.b64 %rd11, %rd10, 4;                      // t*16
    cvt.u64.u32 %rd12, %r7;
    add.u64 %rd12, %rd11, %rd12;                  // a_col
    cvt.u64.u32 %rd13, %r6;
    add.u64 %rd13, %rd11, %rd13;                  // b_row
    mov.f32 %f2, 0f00000000;
    setp.lt.s32 %p2, %r8, %r1;                    // row < m
    setp.lt.u64 %p3, %rd12, %rd4;                 // a_col < k
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_G8_SA;
    mul.lo.u64 %rd14, %rd7, %rd4;
    add.u64 %rd14, %rd14, %rd12;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd1, %rd14;
    ld.global.f32 %f2, [%rd14];                   // a is fp32
XC_G8_SA:
    mov.u64 %rd15, xc_g8_as;
    shl.b32 %r11, %r6, 4;
    add.s32 %r11, %r11, %r7;                      // ty*16+tx
    cvt.u64.u32 %rd16, %r11;
    shl.b64 %rd16, %rd16, 2;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f32 [%rd15], %f2;
    mov.f32 %f3, 0f00000000;
    setp.lt.u64 %p5, %rd13, %rd4;                 // b_row < k
    setp.lt.u64 %p6, %rd8, %rd5;                  // col < n
    and.pred %p7, %p5, %p6;
    @!%p7 bra XC_G8_SB;
    mul.lo.u64 %rd14, %rd13, %rd5;
    add.u64 %rd14, %rd14, %rd8;
    add.u64 %rd14, %rd2, %rd14;                   // &b[b_row*n+col] u8
    ld.global.u8 %r10, [%rd14];
    st.param.b32 [%pi_dec], %r10;
    call (%po_dec), xc_fp8_dec, (%pi_dec);
    ld.param.b32 %r11, [%po_dec];
    mov.b32 %f3, %r11;
XC_G8_SB:
    mov.u64 %rd15, xc_g8_bs;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f32 [%rd15], %f3;
    bar.sync 0;
    mov.u64 %rd17, xc_g8_as;
    cvt.u64.u32 %rd18, %r6;
    shl.b64 %rd18, %rd18, 6;                      // ty*64
    add.u64 %rd17, %rd17, %rd18;
    mov.u64 %rd19, xc_g8_bs;
    cvt.u64.u32 %rd18, %r7;
    shl.b64 %rd18, %rd18, 2;
    add.u64 %rd19, %rd19, %rd18;
    ld.shared.f32 %f8, [%rd17+0];   ld.shared.f32 %f9, [%rd19+0];   fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+4];   ld.shared.f32 %f9, [%rd19+64];  fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+8];   ld.shared.f32 %f9, [%rd19+128]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+12];  ld.shared.f32 %f9, [%rd19+192]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+16];  ld.shared.f32 %f9, [%rd19+256]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+20];  ld.shared.f32 %f9, [%rd19+320]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+24];  ld.shared.f32 %f9, [%rd19+384]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+28];  ld.shared.f32 %f9, [%rd19+448]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+32];  ld.shared.f32 %f9, [%rd19+512]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+36];  ld.shared.f32 %f9, [%rd19+576]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+40];  ld.shared.f32 %f9, [%rd19+640]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+44];  ld.shared.f32 %f9, [%rd19+704]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+48];  ld.shared.f32 %f9, [%rd19+768]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+52];  ld.shared.f32 %f9, [%rd19+832]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+56];  ld.shared.f32 %f9, [%rd19+896]; fma.rn.f32 %f1, %f8, %f9, %f1;
    ld.shared.f32 %f8, [%rd17+60];  ld.shared.f32 %f9, [%rd19+960]; fma.rn.f32 %f1, %f8, %f9, %f1;
    bar.sync 0;
    add.u64 %rd10, %rd10, 1;
    bra XC_G8_TILE;
XC_G8_OUT:
    setp.lt.s32 %p1, %r8, %r1;                    // row < m
    setp.lt.u64 %p2, %rd8, %rd5;                  // col < n
    and.pred %p3, %p1, %p2;
    @!%p3 bra XC_G8_RET;
    mul.lo.u64 %rd14, %rd7, %rd5;
    add.u64 %rd14, %rd14, %rd8;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    st.global.f32 [%rd14], %f1;
XC_G8_RET:
    ret;
}

// ---- fp8 split-k GEMV pass 1 (a fp32 device, b E4M3 bytes) ----------
.visible .entry xc_gemv_fp8_part(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_part,
    .param .u32 %p_m, .param .u32 %p_k, .param .u32 %p_n,
    .param .u32 %p_ksplit, .param .u32 %p_kchunk)
{
    .reg .pred %p<6>;
    .reg .b32  %r<10>;
    .reg .b64  %rd<24>;
    .reg .f32  %f<32>;
    .param .b32 %po_dec, %pi_dec;
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
    add.u32 %r8, %r8, %r7;                        // col
    setp.ge.u32 %p1, %r8, %r3;
    @%p1 bra XC_GVF_END;
    cvt.u64.u32 %rd8, %r8;
    cvt.u64.u32 %rd4, %r1;                        // m64
    cvt.u64.u32 %rd5, %r2;                        // k64
    cvt.u64.u32 %rd6, %r3;                        // n64
    mov.u32 %r9, %ctaid.y;
    cvt.u64.u32 %rd9, %r9;
    cvt.u64.u32 %rd7, %r4;                        // kchunk64
    mul.lo.u64 %rd9, %rd9, %rd7;                  // i0
    add.u64 %rd10, %rd9, %rd7;
    setp.lt.u64 %p2, %rd10, %rd5;
    selp.u64 %rd10, %rd10, %rd5, %p2;             // i1
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
    shl.b64 %rd14, %rd5, 2;                       // k*4 byte stride
    mov.u64 %rd11, %rd9;
XC_GVF_LOOP:
    setp.ge.u64 %p3, %rd11, %rd10;
    @%p3 bra XC_GVF_STORE;
    mul.lo.u64 %rd12, %rd11, %rd6;
    add.u64 %rd12, %rd12, %rd8;                   // b idx i*n+col
    add.u64 %rd12, %rd2, %rd12;
    ld.global.u8 %r6, [%rd12];
    st.param.b32 [%pi_dec], %r6;
    call (%po_dec), xc_fp8_dec, (%pi_dec);
    ld.param.b32 %r7, [%po_dec];
    mov.b32 %f1, %r7;                             // bv
    shl.b64 %rd13, %rd11, 2;
    add.u64 %rd13, %rd1, %rd13;                   // &a[i] (f32)
    setp.gt.u64 %p4, %rd4, 0;
    @!%p4 bra XC_GVF_R0;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f16, %f2, %f1, %f16;
XC_GVF_R0:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 1;
    @!%p4 bra XC_GVF_R1;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f17, %f2, %f1, %f17;
XC_GVF_R1:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 2;
    @!%p4 bra XC_GVF_R2;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f18, %f2, %f1, %f18;
XC_GVF_R2:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 3;
    @!%p4 bra XC_GVF_R3;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f19, %f2, %f1, %f19;
XC_GVF_R3:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 4;
    @!%p4 bra XC_GVF_R4;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f20, %f2, %f1, %f20;
XC_GVF_R4:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 5;
    @!%p4 bra XC_GVF_R5;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f21, %f2, %f1, %f21;
XC_GVF_R5:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 6;
    @!%p4 bra XC_GVF_R6;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f22, %f2, %f1, %f22;
XC_GVF_R6:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 7;
    @!%p4 bra XC_GVF_R7;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f23, %f2, %f1, %f23;
XC_GVF_R7:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 8;
    @!%p4 bra XC_GVF_R8;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f24, %f2, %f1, %f24;
XC_GVF_R8:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 9;
    @!%p4 bra XC_GVF_R9;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f25, %f2, %f1, %f25;
XC_GVF_R9:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 10;
    @!%p4 bra XC_GVF_R10;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f26, %f2, %f1, %f26;
XC_GVF_R10:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 11;
    @!%p4 bra XC_GVF_R11;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f27, %f2, %f1, %f27;
XC_GVF_R11:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 12;
    @!%p4 bra XC_GVF_R12;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f28, %f2, %f1, %f28;
XC_GVF_R12:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 13;
    @!%p4 bra XC_GVF_R13;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f29, %f2, %f1, %f29;
XC_GVF_R13:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 14;
    @!%p4 bra XC_GVF_R14;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f30, %f2, %f1, %f30;
XC_GVF_R14:
    add.u64 %rd13, %rd13, %rd14;
    setp.gt.u64 %p4, %rd4, 15;
    @!%p4 bra XC_GVF_R15;
    ld.global.f32 %f2, [%rd13];
    fma.rn.f32 %f31, %f2, %f1, %f31;
XC_GVF_R15:
    add.u64 %rd11, %rd11, 1;
    bra XC_GVF_LOOP;
XC_GVF_STORE:
    cvt.u64.u32 %rd16, %r9;                       // s = ctaid.y
    mul.lo.u64 %rd16, %rd16, %rd4;
    mul.lo.u64 %rd16, %rd16, %rd6;                // s*m*n
    add.u64 %rd16, %rd16, %rd8;                   // +col
    shl.b64 %rd16, %rd16, 2;
    add.u64 %rd16, %rd3, %rd16;                   // &part[..]
    shl.b64 %rd17, %rd6, 2;                       // n*4
    setp.gt.u64 %p4, %rd4, 0;
    @!%p4 bra XC_GVF_S0;
    st.global.f32 [%rd16], %f16;
XC_GVF_S0:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 1;
    @!%p4 bra XC_GVF_S1;
    st.global.f32 [%rd16], %f17;
XC_GVF_S1:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 2;
    @!%p4 bra XC_GVF_S2;
    st.global.f32 [%rd16], %f18;
XC_GVF_S2:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 3;
    @!%p4 bra XC_GVF_S3;
    st.global.f32 [%rd16], %f19;
XC_GVF_S3:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 4;
    @!%p4 bra XC_GVF_S4;
    st.global.f32 [%rd16], %f20;
XC_GVF_S4:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 5;
    @!%p4 bra XC_GVF_S5;
    st.global.f32 [%rd16], %f21;
XC_GVF_S5:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 6;
    @!%p4 bra XC_GVF_S6;
    st.global.f32 [%rd16], %f22;
XC_GVF_S6:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 7;
    @!%p4 bra XC_GVF_S7;
    st.global.f32 [%rd16], %f23;
XC_GVF_S7:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 8;
    @!%p4 bra XC_GVF_S8;
    st.global.f32 [%rd16], %f24;
XC_GVF_S8:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 9;
    @!%p4 bra XC_GVF_S9;
    st.global.f32 [%rd16], %f25;
XC_GVF_S9:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 10;
    @!%p4 bra XC_GVF_S10;
    st.global.f32 [%rd16], %f26;
XC_GVF_S10:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 11;
    @!%p4 bra XC_GVF_S11;
    st.global.f32 [%rd16], %f27;
XC_GVF_S11:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 12;
    @!%p4 bra XC_GVF_S12;
    st.global.f32 [%rd16], %f28;
XC_GVF_S12:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 13;
    @!%p4 bra XC_GVF_S13;
    st.global.f32 [%rd16], %f29;
XC_GVF_S13:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 14;
    @!%p4 bra XC_GVF_S14;
    st.global.f32 [%rd16], %f30;
XC_GVF_S14:
    add.u64 %rd16, %rd16, %rd17;
    setp.gt.u64 %p4, %rd4, 15;
    @!%p4 bra XC_GVF_S15;
    st.global.f32 [%rd16], %f31;
XC_GVF_S15:
XC_GVF_END:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
