// cuda_ptx_tgemmw.h -- embedded PTX wide-tile fp32 GEMM for the
// training lane (NativeCudaTrainingPlane §26): 64x64 output tile /
// 256 threads, each thread owns a 4x4 register micro-tile
// (rows ty+16i, cols tx+16j), kt=16. ~4x less global traffic per FLOP
// than the 16x16 tgemm kernels.
// One entry multiplexes the three trainer layouts via %p_layout:
//   1 nn  c[m,n] = a[m,k] * b[k,n]        (bwd dx = dy * w)
//   2 nt  c[m,n] = a[m,k] * w[n,k]^T      (fwd  y  = x * w^T)
//   3 tn  c[m,n] = aT[k,m] * b[k,n]       (bwd  dW = dy^T * x)
// A stage-in resolves either row*k+kk (nn/nt) or kk*m+row (tn); B
// stage-in resolves kk*n+col (nn/tn) or col*k+kk (nt) -- the shared
// FMA body always sees As[kk][row] / Bs[kk][col].
// %p_acc != 0 preloads the accumulators from c (axpy += semantics).
// Per-element accumulation stays in strict k order.

#pragma once

namespace xcuda_ptx {

inline const char* tgemmw() {
    return R"PTX(
// ---- fp32 wide-tile GEMM: 64x64 tile, layout-multiplexed ------------
.visible .entry xc_sgemm_w(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u64 %p_m, .param .u64 %p_k, .param .u64 %p_n,
    .param .u32 %p_acc, .param .u32 %p_layout)
{
    .shared .align 4 .b8 xc_gfw_as[4096];          // [kk][row] f32
    .shared .align 4 .b8 xc_gfw_bs[4096];          // [kk][col] f32
    .reg .pred %p<10>;
    .reg .b32  %r<16>;
    .reg .b64  %rd<32>;
    .reg .f32  %f<40>;
    ld.param.u64 %rd1, [%p_a];
    ld.param.u64 %rd2, [%p_b];
    ld.param.u64 %rd3, [%p_c];
    ld.param.u64 %rd4, [%p_m];
    ld.param.u64 %rd5, [%p_k];
    ld.param.u64 %rd6, [%p_n];
    ld.param.u32 %r9, [%p_acc];
    ld.param.u32 %r8, [%p_layout];
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
    // ---- acc preload: acc[i][j] = c[row][col] when %p_acc ------------
    setp.eq.u32 %p7, %r9, 0;
    @%p7 bra XC_SW_T0;
    cvt.u64.u32 %rd12, %r2;
    add.u64 %rd12, %rd12, %rd7;                    // row0+ty
    cvt.u64.u32 %rd13, %r1;
    add.u64 %rd13, %rd13, %rd8;                    // col0+tx
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_SW_P1;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 ld.global.f32 %f10, [%rd14+0];
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f11, [%rd14+64];
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f12, [%rd14+128];
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f13, [%rd14+192];
XC_SW_P1:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_SW_P2;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 ld.global.f32 %f14, [%rd14+0];
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f15, [%rd14+64];
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f16, [%rd14+128];
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f17, [%rd14+192];
XC_SW_P2:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_SW_P3;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 ld.global.f32 %f18, [%rd14+0];
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f19, [%rd14+64];
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f20, [%rd14+128];
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f21, [%rd14+192];
XC_SW_P3:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_SW_T0;
    mul.lo.u64 %rd14, %rd12, %rd6;
    add.u64 %rd14, %rd14, %rd13;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.lt.u64 %p2, %rd13, %rd6;
    @%p2 ld.global.f32 %f22, [%rd14+0];
    add.u64 %rd15, %rd13, 16;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f23, [%rd14+64];
    add.u64 %rd15, %rd13, 32;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f24, [%rd14+128];
    add.u64 %rd15, %rd13, 48;
    setp.lt.u64 %p2, %rd15, %rd6;
    @%p2 ld.global.f32 %f25, [%rd14+192];
XC_SW_T0:
    add.u64 %rd9, %rd5, 15;
    shr.u64 %rd9, %rd9, 4;                         // nt = ceil(k/16)
    mov.u64 %rd10, 0;                              // t
XC_SW_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_SW_OUT;
    shl.b64 %rd11, %rd10, 4;                       // k0 = t*16
    // ---- stage A tile: idx = L+256j over a 64x16 window --------------
    mov.u64 %rd12, 0;
XC_SW_LA:
    cvt.u64.u32 %rd13, %r7;
    add.u64 %rd13, %rd13, %rd12;                   // idx
    shr.u64 %rd14, %rd13, 4;                       // row (nn/nt)
    and.b64 %rd15, %rd13, 15;                      // kk
    setp.eq.u32 %p5, %r8, 3;
    @!%p5 bra XC_SW_LAD;
    and.b64 %rd14, %rd13, 63;                      // tn: row = idx&63
    shr.u64 %rd15, %rd13, 6;                       // tn: kk = idx>>6
    // (consecutive idx -> consecutive row: coalesced reads, and the
    // kk*64+row store index below still equals idx)
XC_SW_LAD:
    mov.f32 %f1, 0f00000000;
    add.u64 %rd16, %rd7, %rd14;
    setp.lt.u64 %p2, %rd16, %rd4;                  // row0+row < m
    add.u64 %rd17, %rd11, %rd15;
    setp.lt.u64 %p3, %rd17, %rd5;                  // k0+kk < k
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_SW_LAS;
    setp.eq.u32 %p5, %r8, 3;
    @%p5 bra XC_SW_AT;
    mul.lo.u64 %rd18, %rd16, %rd5;                 // nn/nt: row*k+kk
    add.u64 %rd18, %rd18, %rd17;
    bra XC_SW_AJ;
XC_SW_AT:
    mul.lo.u64 %rd18, %rd17, %rd4;                 // tn: kk*m+row
    add.u64 %rd18, %rd18, %rd16;
XC_SW_AJ:
    shl.b64 %rd18, %rd18, 2;
    add.u64 %rd18, %rd1, %rd18;
    ld.global.f32 %f1, [%rd18];
XC_SW_LAS:
    shl.b64 %rd19, %rd15, 6;
    add.u64 %rd19, %rd19, %rd14;                   // kk*64+row
    shl.b64 %rd19, %rd19, 2;
    mov.u64 %rd20, xc_gfw_as;
    add.u64 %rd20, %rd20, %rd19;
    st.shared.f32 [%rd20], %f1;
    add.u64 %rd12, %rd12, 256;
    setp.lt.u64 %p5, %rd12, 1024;
    @%p5 bra XC_SW_LA;
    // ---- stage B tile: idx = L+256j over a 16x64 window --------------
    mov.u64 %rd12, 0;
XC_SW_LB:
    cvt.u64.u32 %rd13, %r7;
    add.u64 %rd13, %rd13, %rd12;                   // idx
    shr.u64 %rd14, %rd13, 6;                       // kk (nn/tn)
    and.b64 %rd15, %rd13, 63;                      // col
    setp.eq.u32 %p5, %r8, 2;
    @!%p5 bra XC_SW_LBD;
    and.b64 %rd14, %rd13, 15;                      // nt: kk = idx&15
    shr.u64 %rd15, %rd13, 4;                       // nt: col = idx>>4
    // (consecutive idx -> consecutive kk: coalesced reads; kk*64+col
    // below remains the correct store index)
XC_SW_LBD:
    mov.f32 %f1, 0f00000000;
    add.u64 %rd16, %rd11, %rd14;
    setp.lt.u64 %p2, %rd16, %rd5;                  // k0+kk < k
    add.u64 %rd17, %rd8, %rd15;
    setp.lt.u64 %p3, %rd17, %rd6;                  // col0+col < n
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_SW_LBS;
    setp.eq.u32 %p5, %r8, 2;
    @%p5 bra XC_SW_BT;
    mul.lo.u64 %rd18, %rd16, %rd6;                 // nn/tn: kk*n+col
    add.u64 %rd18, %rd18, %rd17;
    bra XC_SW_BJ;
XC_SW_BT:
    mul.lo.u64 %rd18, %rd17, %rd5;                 // nt: col*k+kk
    add.u64 %rd18, %rd18, %rd16;
XC_SW_BJ:
    shl.b64 %rd18, %rd18, 2;
    add.u64 %rd18, %rd2, %rd18;
    ld.global.f32 %f1, [%rd18];
XC_SW_LBS:
    shl.b64 %rd19, %rd14, 6;
    add.u64 %rd19, %rd19, %rd15;                   // kk*64+col
    shl.b64 %rd19, %rd19, 2;
    mov.u64 %rd20, xc_gfw_bs;
    add.u64 %rd20, %rd20, %rd19;
    st.shared.f32 [%rd20], %f1;
    add.u64 %rd12, %rd12, 256;
    setp.lt.u64 %p5, %rd12, 1024;
    @%p5 bra XC_SW_LB;
    bar.sync 0;
    // ---- inner: kk loop, 4x4 micro-tile (strict k order) -------------
    mov.u64 %rd21, xc_gfw_as;
    cvt.u64.u32 %rd22, %r2;
    shl.b64 %rd22, %rd22, 2;
    add.u64 %rd21, %rd21, %rd22;                   // &As[0][ty]
    mov.u64 %rd23, xc_gfw_bs;
    cvt.u64.u32 %rd24, %r1;
    shl.b64 %rd24, %rd24, 2;
    add.u64 %rd23, %rd23, %rd24;                   // &Bs[0][tx]
    mov.u64 %rd20, 0;                              // kk
XC_SW_KK:
    ld.shared.f32 %f30, [%rd21+0];                 // As[kk][ty+0]
    ld.shared.f32 %f31, [%rd21+64];
    ld.shared.f32 %f32, [%rd21+128];
    ld.shared.f32 %f33, [%rd21+192];
    ld.shared.f32 %f34, [%rd23+0];                 // Bs[kk][tx+0]
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
    @%p6 bra XC_SW_KK;
    bar.sync 0;
    add.u64 %rd10, %rd10, 1;
    bra XC_SW_TILE;
XC_SW_OUT:
    cvt.u64.u32 %rd12, %r2;
    add.u64 %rd12, %rd12, %rd7;                    // row0+ty
    cvt.u64.u32 %rd13, %r1;
    add.u64 %rd13, %rd13, %rd8;                    // col0+tx
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_SW_S1;
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
XC_SW_S1:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_SW_S2;
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
XC_SW_S2:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_SW_S3;
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
XC_SW_S3:
    add.u64 %rd12, %rd12, 16;
    setp.ge.u64 %p1, %rd12, %rd4;
    @%p1 bra XC_SW_RET;
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
XC_SW_RET:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
