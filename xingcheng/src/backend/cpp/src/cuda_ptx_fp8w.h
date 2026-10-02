// cuda_ptx_fp8w.h -- embedded PTX wide-tile fp8 GEMM (B132):
//   xc_gemm_fp8_w   64x64 output tile / 256 threads, 4x4 register
//                   micro-tile, kt=16; a is device fp32, b is E4M3
//                   bytes decoded at stage-in through xc_fp8_dec --
//                   the decode runs once per element, never per FMA.

#pragma once

namespace xcuda_ptx {

inline const char* fp8w() {
    return R"PTX(
// ---- fp8 wide-tile GEMM: a f32 x b E4M3 -> f32 c, 64x64 tile ---------
.visible .entry xc_gemm_fp8_w(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u32 %p_m, .param .u32 %p_k, .param .u32 %p_n)
{
    .shared .align 4 .b8 xc_g8w_as[4096];          // [kk][row] f32
    .shared .align 4 .b8 xc_g8w_bs[4096];          // [kk][col] f32
    .reg .pred %p<10>;
    .reg .b32  %r<16>;
    .reg .b64  %rd<32>;
    .reg .f32  %f<40>;
    .param .b32 %po_dec, %pi_dec;
    ld.param.u64 %rd1, [%p_a];
    ld.param.u64 %rd2, [%p_b];
    ld.param.u64 %rd3, [%p_c];
    ld.param.u32 %r1, [%p_m];
    ld.param.u32 %r2, [%p_k];
    ld.param.u32 %r3, [%p_n];
    cvt.u64.u32 %rd4, %r1;                         // m64
    cvt.u64.u32 %rd5, %r2;                         // k64
    cvt.u64.u32 %rd6, %r3;                         // n64
    mov.u32 %r1, %tid.x;
    mov.u32 %r2, %tid.y;
    mov.u32 %r3, %ctaid.x;
    mov.u32 %r4, %ctaid.y;
    shl.b32 %r5, %r4, 6;
    cvt.u64.u32 %rd7, %r5;                         // row0
    shl.b32 %r6, %r3, 6;
    cvt.u64.u32 %rd8, %r6;                         // col0
    shl.b32 %r7, %r2, 4;
    add.s32 %r7, %r7, %r1;                         // L
    mov.f32 %f10, 0f00000000;
    mov.f32 %f11, 0f00000000;
    mov.f32 %f12, 0f00000000;
    mov.f32 %f13, 0f00000000;
    mov.f32 %f14, 0f00000000;
    mov.f32 %f15, 0f00000000;
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
    add.u64 %rd9, %rd5, 15;
    shr.u64 %rd9, %rd9, 4;                         // nt
    mov.u64 %rd10, 0;
XC_W8_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_W8_OUT;
    shl.b64 %rd11, %rd10, 4;                       // k0
    // ---- stage A (f32): idx = L+256j over a 64x16 window --------------
    mov.u64 %rd12, 0;
XC_W8_LA:
    cvt.u64.u32 %rd13, %r7;
    add.u64 %rd13, %rd13, %rd12;
    shr.u64 %rd14, %rd13, 4;                       // row
    and.b64 %rd15, %rd13, 15;                      // kk
    mov.f32 %f1, 0f00000000;
    add.u64 %rd16, %rd7, %rd14;
    setp.lt.u64 %p2, %rd16, %rd4;
    add.u64 %rd17, %rd11, %rd15;
    setp.lt.u64 %p3, %rd17, %rd5;
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_W8_LAS;
    mul.lo.u64 %rd18, %rd16, %rd5;
    add.u64 %rd18, %rd18, %rd17;
    shl.b64 %rd18, %rd18, 2;
    add.u64 %rd18, %rd1, %rd18;
    ld.global.f32 %f1, [%rd18];
XC_W8_LAS:
    shl.b64 %rd19, %rd15, 6;
    add.u64 %rd19, %rd19, %rd14;
    shl.b64 %rd19, %rd19, 2;
    mov.u64 %rd20, xc_g8w_as;
    add.u64 %rd20, %rd20, %rd19;
    st.shared.f32 [%rd20], %f1;
    add.u64 %rd12, %rd12, 256;
    setp.lt.u64 %p5, %rd12, 1024;
    @%p5 bra XC_W8_LA;
    // ---- stage B (E4M3 byte -> f32): idx = L+256j over 16x64 ----------
    mov.u64 %rd12, 0;
XC_W8_LB:
    cvt.u64.u32 %rd13, %r7;
    add.u64 %rd13, %rd13, %rd12;
    shr.u64 %rd14, %rd13, 6;                       // kk
    and.b64 %rd15, %rd13, 63;                      // col
    mov.f32 %f1, 0f00000000;
    add.u64 %rd16, %rd11, %rd14;
    setp.lt.u64 %p2, %rd16, %rd5;
    add.u64 %rd17, %rd8, %rd15;
    setp.lt.u64 %p3, %rd17, %rd6;
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_W8_LBS;
    mul.lo.u64 %rd18, %rd16, %rd6;
    add.u64 %rd18, %rd18, %rd17;
    add.u64 %rd18, %rd2, %rd18;
    ld.global.u8 %r10, [%rd18];
    st.param.b32 [%pi_dec], %r10;
    call (%po_dec), xc_fp8_dec, (%pi_dec);
    ld.param.b32 %r10, [%po_dec];
    mov.b32 %f1, %r10;
XC_W8_LBS:
    shl.b64 %rd19, %rd13, 2;
    mov.u64 %rd20, xc_g8w_bs;
    add.u64 %rd20, %rd20, %rd19;
    st.shared.f32 [%rd20], %f1;
    add.u64 %rd12, %rd12, 256;
    setp.lt.u64 %p5, %rd12, 1024;
    @%p5 bra XC_W8_LB;
    bar.sync 0;
    mov.u64 %rd21, xc_g8w_as;
    cvt.u64.u32 %rd22, %r2;
    shl.b64 %rd22, %rd22, 2;
    add.u64 %rd21, %rd21, %rd22;
    mov.u64 %rd23, xc_g8w_bs;
    cvt.u64.u32 %rd24, %r1;
    shl.b64 %rd24, %rd24, 2;
    add.u64 %rd23, %rd23, %rd24;
    mov.u64 %rd20, 0;
XC_W8_KK:
    ld.shared.f32 %f30, [%rd21+0];
    ld.shared.f32 %f31, [%rd21+64];
    ld.shared.f32 %f32, [%rd21+128];
    ld.shared.f32 %f33, [%rd21+192];
    ld.shared.f32 %f34, [%rd23+0];
    ld.shared.f32 %f35, [%rd23+64];
    ld.shared.f32 %f36, [%rd23+128];
    ld.shared.f32 %f37, [%rd23+192];
    fma.rn.f32 %f10, %f30, %f34, %f10;
    fma.rn.f32 %f11, %f30, %f35, %f11;
    fma.rn.f32 %f12, %f30, %f36, %f12;
    fma.rn.f32 %f13, %f30, %f37, %f13;
    fma.rn.f32 %f14, %f31, %f34, %f14;
    fma.rn.f32 %f15, %f31, %f35, %f15;
    fma.rn.f32 %f16, %f31, %f36, %f16;
    fma.rn.f32 %f17, %f31, %f37, %f17;
    fma.rn.f32 %f18, %f32, %f34, %f18;
    fma.rn.f32 %f19, %f32, %f35, %f19;
    fma.rn.f32 %f20, %f32, %f36, %f20;
    fma.rn.f32 %f21, %f32, %f37, %f21;
    fma.rn.f32 %f22, %f33, %f34, %f22;
    fma.rn.f32 %f23, %f33, %f35, %f23;
    fma.rn.f32 %f24, %f33, %f36, %f24;
    fma.rn.f32 %f25, %f33, %f37, %f25;
    add.u64 %rd21, %rd21, 256;
    add.u64 %rd23, %rd23, 256;
    add.u64 %rd20, %rd20, 1;
    setp.lt.u64 %p6, %rd20, 16;
    @%p6 bra XC_W8_KK;
    bar.sync 0;
    add.u64 %rd10, %rd10, 1;
    bra XC_W8_TILE;
XC_W8_OUT:
    cvt.u64.u32 %rd12, %r2;
    add.u64 %rd12, %rd12, %rd7;
    cvt.u64.u32 %rd13, %r1;
    add.u64 %rd13, %rd13, %rd8;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W8_S1;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 st.global.f32 [%rd14+0], %f10;
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+64], %f11;
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+128], %f12;
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+192], %f13;
XC_W8_S1:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W8_S2;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 st.global.f32 [%rd14+0], %f14;
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+64], %f15;
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+128], %f16;
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+192], %f17;
XC_W8_S2:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W8_S3;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 st.global.f32 [%rd14+0], %f18;
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+64], %f19;
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+128], %f20;
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+192], %f21;
XC_W8_S3:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W8_RET;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 st.global.f32 [%rd14+0], %f22;
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+64], %f23;
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+128], %f24;
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f32 [%rd14+192], %f25;
XC_W8_RET:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
