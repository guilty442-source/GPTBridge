// cuda_ptx_gemmw.h -- embedded PTX wide-tile GEMM kernels (B132):
//   xc_gemm_f64_w   64x64 output tile / 256 threads; each thread owns a
//                   4x4 register micro-tile (rows ty+16i, cols tx+16j),
//                   kt=16; A staged transposed [kk][row], B [kk][col].
//                   Per-element accumulation stays in strict k order --
//                   bit-identical to xc_gemm_f64, ~4x less global
//                   traffic per FLOP.
//   xc_gemm_bf16_w  same schedule, bf16 -> fp32 shared tiles.
//   xc_gemm_fp8_w   same schedule, b bytes decoded at stage-in.
// Dispatch: the host picks a wide kernel when the shape fills the
// 64x64 grid, the 16x16 kernels otherwise (small/skinny shapes).

#pragma once

namespace xcuda_ptx {

inline const char* gemmw() {
    return R"PTX(
// ---- fp64 wide-tile GEMM: c[m,n] = a[m,k] * b[k,n], 64x64 tile -------
.visible .entry xc_gemm_f64_w(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u64 %p_m, .param .u64 %p_k, .param .u64 %p_n)
{
    .shared .align 8 .b8 xc_g64w_as[8192];         // [kk][row] f64
    .shared .align 8 .b8 xc_g64w_bs[8192];         // [kk][col] f64
    .reg .pred %p<10>;
    .reg .b32  %r<16>;
    .reg .b64  %rd<32>;
    .reg .f64  %fd<40>;
    ld.param.u64 %rd1, [%p_a];
    ld.param.u64 %rd2, [%p_b];
    ld.param.u64 %rd3, [%p_c];
    ld.param.u64 %rd4, [%p_m];
    ld.param.u64 %rd5, [%p_k];
    ld.param.u64 %rd6, [%p_n];
    mov.u32 %r1, %tid.x;
    mov.u32 %r2, %tid.y;
    mov.u32 %r3, %ctaid.x;
    mov.u32 %r4, %ctaid.y;
    shl.b32 %r5, %r4, 6;
    cvt.u64.u32 %rd7, %r5;                         // row0 = by*64
    shl.b32 %r6, %r3, 6;
    cvt.u64.u32 %rd8, %r6;                         // col0 = bx*64
    shl.b32 %r7, %r2, 4;
    add.s32 %r7, %r7, %r1;                         // L = ty*16+tx
    mov.f64 %fd10, 0d0000000000000000;
    mov.f64 %fd11, 0d0000000000000000;
    mov.f64 %fd12, 0d0000000000000000;
    mov.f64 %fd13, 0d0000000000000000;
    mov.f64 %fd14, 0d0000000000000000;
    mov.f64 %fd15, 0d0000000000000000;
    mov.f64 %fd16, 0d0000000000000000;
    mov.f64 %fd17, 0d0000000000000000;
    mov.f64 %fd18, 0d0000000000000000;
    mov.f64 %fd19, 0d0000000000000000;
    mov.f64 %fd20, 0d0000000000000000;
    mov.f64 %fd21, 0d0000000000000000;
    mov.f64 %fd22, 0d0000000000000000;
    mov.f64 %fd23, 0d0000000000000000;
    mov.f64 %fd24, 0d0000000000000000;
    mov.f64 %fd25, 0d0000000000000000;
    add.u64 %rd9, %rd5, 15;
    shr.u64 %rd9, %rd9, 4;                         // nt = ceil(k/16)
    mov.u64 %rd10, 0;                              // t
XC_W64_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_W64_OUT;
    shl.b64 %rd11, %rd10, 4;                       // k0 = t*16
    // ---- stage A tile: idx = L+256j over a 64x16 window --------------
    mov.u64 %rd12, 0;                              // j offset
XC_W64_LA:
    cvt.u64.u32 %rd13, %r7;
    add.u64 %rd13, %rd13, %rd12;                   // idx
    shr.u64 %rd14, %rd13, 4;                       // row
    and.b64 %rd15, %rd13, 15;                      // kk
    mov.f64 %fd1, 0d0000000000000000;
    add.u64 %rd16, %rd7, %rd14;
    setp.lt.u64 %p2, %rd16, %rd4;                  // row0+row < m
    add.u64 %rd17, %rd11, %rd15;
    setp.lt.u64 %p3, %rd17, %rd5;                  // k0+kk < k
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_W64_LAS;
    mul.lo.u64 %rd18, %rd16, %rd5;
    add.u64 %rd18, %rd18, %rd17;
    shl.b64 %rd18, %rd18, 3;
    add.u64 %rd18, %rd1, %rd18;
    ld.global.f64 %fd1, [%rd18];
XC_W64_LAS:
    shl.b64 %rd19, %rd15, 6;
    add.u64 %rd19, %rd19, %rd14;                   // kk*64+row
    shl.b64 %rd19, %rd19, 3;
    mov.u64 %rd20, xc_g64w_as;
    add.u64 %rd20, %rd20, %rd19;
    st.shared.f64 [%rd20], %fd1;
    add.u64 %rd12, %rd12, 256;
    setp.lt.u64 %p5, %rd12, 1024;
    @%p5 bra XC_W64_LA;
    // ---- stage B tile: idx = L+256j over a 16x64 window --------------
    mov.u64 %rd12, 0;
XC_W64_LB:
    cvt.u64.u32 %rd13, %r7;
    add.u64 %rd13, %rd13, %rd12;                   // idx
    shr.u64 %rd14, %rd13, 6;                       // kk
    and.b64 %rd15, %rd13, 63;                      // col
    mov.f64 %fd1, 0d0000000000000000;
    add.u64 %rd16, %rd11, %rd14;
    setp.lt.u64 %p2, %rd16, %rd5;                  // k0+kk < k
    add.u64 %rd17, %rd8, %rd15;
    setp.lt.u64 %p3, %rd17, %rd6;                  // col0+col < n
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_W64_LBS;
    mul.lo.u64 %rd18, %rd16, %rd6;
    add.u64 %rd18, %rd18, %rd17;
    shl.b64 %rd18, %rd18, 3;
    add.u64 %rd18, %rd2, %rd18;
    ld.global.f64 %fd1, [%rd18];
XC_W64_LBS:
    shl.b64 %rd19, %rd13, 3;                       // idx == kk*64+col
    mov.u64 %rd20, xc_g64w_bs;
    add.u64 %rd20, %rd20, %rd19;
    st.shared.f64 [%rd20], %fd1;
    add.u64 %rd12, %rd12, 256;
    setp.lt.u64 %p5, %rd12, 1024;
    @%p5 bra XC_W64_LB;
    bar.sync 0;
    // ---- inner: kk loop, 4x4 micro-tile (strict k order) -------------
    mov.u64 %rd21, xc_g64w_as;
    cvt.u64.u32 %rd22, %r2;
    shl.b64 %rd22, %rd22, 3;
    add.u64 %rd21, %rd21, %rd22;                   // &As[0][ty]
    mov.u64 %rd23, xc_g64w_bs;
    cvt.u64.u32 %rd24, %r1;
    shl.b64 %rd24, %rd24, 3;
    add.u64 %rd23, %rd23, %rd24;                   // &Bs[0][tx]
    mov.u64 %rd20, 0;                              // kk
XC_W64_KK:
    ld.shared.f64 %fd30, [%rd21+0];                // As[kk][ty+0]
    ld.shared.f64 %fd31, [%rd21+128];              // As[kk][ty+16]
    ld.shared.f64 %fd32, [%rd21+256];
    ld.shared.f64 %fd33, [%rd21+384];
    ld.shared.f64 %fd34, [%rd23+0];                // Bs[kk][tx+0]
    ld.shared.f64 %fd35, [%rd23+128];
    ld.shared.f64 %fd36, [%rd23+256];
    ld.shared.f64 %fd37, [%rd23+384];
    fma.rn.f64 %fd10, %fd30, %fd34, %fd10;
    fma.rn.f64 %fd11, %fd30, %fd35, %fd11;
    fma.rn.f64 %fd12, %fd30, %fd36, %fd12;
    fma.rn.f64 %fd13, %fd30, %fd37, %fd13;
    fma.rn.f64 %fd14, %fd31, %fd34, %fd14;
    fma.rn.f64 %fd15, %fd31, %fd35, %fd15;
    fma.rn.f64 %fd16, %fd31, %fd36, %fd16;
    fma.rn.f64 %fd17, %fd31, %fd37, %fd17;
    fma.rn.f64 %fd18, %fd32, %fd34, %fd18;
    fma.rn.f64 %fd19, %fd32, %fd35, %fd19;
    fma.rn.f64 %fd20, %fd32, %fd36, %fd20;
    fma.rn.f64 %fd21, %fd32, %fd37, %fd21;
    fma.rn.f64 %fd22, %fd33, %fd34, %fd22;
    fma.rn.f64 %fd23, %fd33, %fd35, %fd23;
    fma.rn.f64 %fd24, %fd33, %fd36, %fd24;
    fma.rn.f64 %fd25, %fd33, %fd37, %fd25;
    add.u64 %rd21, %rd21, 512;                     // next kk row
    add.u64 %rd23, %rd23, 512;
    add.u64 %rd20, %rd20, 1;
    setp.lt.u64 %p6, %rd20, 16;
    @%p6 bra XC_W64_KK;
    bar.sync 0;
    add.u64 %rd10, %rd10, 1;
    bra XC_W64_TILE;
XC_W64_OUT:
    cvt.u64.u32 %rd12, %r2;
    add.u64 %rd12, %rd12, %rd7;                    // row0+ty
    cvt.u64.u32 %rd13, %r1;
    add.u64 %rd13, %rd13, %rd8;                    // col0+tx
    // i=0: row = row0+ty, acc fd10..fd13
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W64_S1;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 3;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 st.global.f64 [%rd14+0], %fd10;
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+128], %fd11;
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+256], %fd12;
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+384], %fd13;
XC_W64_S1:
    add.u64 %rd12, %rd12, 16;                      // i=1 row
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W64_S2;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 3;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 st.global.f64 [%rd14+0], %fd14;
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+128], %fd15;
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+256], %fd16;
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+384], %fd17;
XC_W64_S2:
    add.u64 %rd12, %rd12, 16;                      // i=2 row
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W64_S3;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 3;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 st.global.f64 [%rd14+0], %fd18;
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+128], %fd19;
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+256], %fd20;
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+384], %fd21;
XC_W64_S3:
    add.u64 %rd12, %rd12, 16;                      // i=3 row
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W64_RET;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 3;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 st.global.f64 [%rd14+0], %fd22;
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+128], %fd23;
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+256], %fd24;
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 st.global.f64 [%rd14+384], %fd25;
XC_W64_RET:
    ret;
}

// ---- bf16 wide-tile GEMM: fp32 smem tiles, fp32 acc, 64x64 -----------
.visible .entry xc_gemm_bf16_w(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u32 %p_m, .param .u32 %p_k, .param .u32 %p_n)
{
    .shared .align 4 .b8 xc_g16w_as[4096];         // [kk][row] f32
    .shared .align 4 .b8 xc_g16w_bs[4096];         // [kk][col] f32
    .reg .pred %p<10>;
    .reg .b16  %rs<3>;
    .reg .b32  %r<16>;
    .reg .b64  %rd<32>;
    .reg .f32  %f<40>;
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
XC_W16_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_W16_OUT;
    shl.b64 %rd11, %rd10, 4;                       // k0
    mov.u64 %rd12, 0;
XC_W16_LA:
    cvt.u64.u32 %rd13, %r7;
    add.u64 %rd13, %rd13, %rd12;                   // idx
    shr.u64 %rd14, %rd13, 4;                       // row
    and.b64 %rd15, %rd13, 15;                      // kk
    mov.f32 %f1, 0f00000000;
    add.u64 %rd16, %rd7, %rd14;
    setp.lt.u64 %p2, %rd16, %rd4;
    add.u64 %rd17, %rd11, %rd15;
    setp.lt.u64 %p3, %rd17, %rd5;
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_W16_LAS;
    mul.lo.u64 %rd18, %rd16, %rd5;
    add.u64 %rd18, %rd18, %rd17;
    add.u64 %rd18, %rd18, %rd18;
    add.u64 %rd18, %rd1, %rd18;
    ld.global.u16 %rs1, [%rd18];
    cvt.u32.u16 %r10, %rs1;
    shl.b32 %r10, %r10, 16;
    mov.b32 %f1, %r10;
XC_W16_LAS:
    shl.b64 %rd19, %rd15, 6;
    add.u64 %rd19, %rd19, %rd14;                   // kk*64+row
    shl.b64 %rd19, %rd19, 2;
    mov.u64 %rd20, xc_g16w_as;
    add.u64 %rd20, %rd20, %rd19;
    st.shared.f32 [%rd20], %f1;
    add.u64 %rd12, %rd12, 256;
    setp.lt.u64 %p5, %rd12, 1024;
    @%p5 bra XC_W16_LA;
    mov.u64 %rd12, 0;
XC_W16_LB:
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
    @!%p4 bra XC_W16_LBS;
    mul.lo.u64 %rd18, %rd16, %rd6;
    add.u64 %rd18, %rd18, %rd17;
    add.u64 %rd18, %rd18, %rd18;
    add.u64 %rd18, %rd2, %rd18;
    ld.global.u16 %rs1, [%rd18];
    cvt.u32.u16 %r10, %rs1;
    shl.b32 %r10, %r10, 16;
    mov.b32 %f1, %r10;
XC_W16_LBS:
    shl.b64 %rd19, %rd13, 2;                       // idx == kk*64+col
    mov.u64 %rd20, xc_g16w_bs;
    add.u64 %rd20, %rd20, %rd19;
    st.shared.f32 [%rd20], %f1;
    add.u64 %rd12, %rd12, 256;
    setp.lt.u64 %p5, %rd12, 1024;
    @%p5 bra XC_W16_LB;
    bar.sync 0;
    mov.u64 %rd21, xc_g16w_as;
    cvt.u64.u32 %rd22, %r2;
    shl.b64 %rd22, %rd22, 2;
    add.u64 %rd21, %rd21, %rd22;                   // &As[0][ty]
    mov.u64 %rd23, xc_g16w_bs;
    cvt.u64.u32 %rd24, %r1;
    shl.b64 %rd24, %rd24, 2;
    add.u64 %rd23, %rd23, %rd24;                   // &Bs[0][tx]
    mov.u64 %rd20, 0;
XC_W16_KK:
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
    add.u64 %rd21, %rd21, 256;                     // next kk row
    add.u64 %rd23, %rd23, 256;
    add.u64 %rd20, %rd20, 1;
    setp.lt.u64 %p6, %rd20, 16;
    @%p6 bra XC_W16_KK;
    bar.sync 0;
    add.u64 %rd10, %rd10, 1;
    bra XC_W16_TILE;
XC_W16_OUT:
    cvt.u64.u32 %rd12, %r2;
    add.u64 %rd12, %rd12, %rd7;
    cvt.u64.u32 %rd13, %r1;
    add.u64 %rd13, %rd13, %rd8;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W16_S1;
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
XC_W16_S1:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W16_S2;
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
XC_W16_S2:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W16_S3;
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
XC_W16_S3:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_W16_RET;
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
XC_W16_RET:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
