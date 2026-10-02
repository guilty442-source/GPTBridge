// cuda_ptx_tgemm.h -- embedded PTX fp32 GEMM kernels for the training
// lane (NativeCudaTrainingPlane §26): the trainer's tpu_linear*
// operands are fp32 in three layouts, one kernel each so every stage-in
// stays coalesced:
//   xc_sgemm_nn  c[m,n]  = a[m,k] * b[k,n]          (bwd dx = dy * w)
//   xc_sgemm_nt  c[m,n]  = a[m,k] * w[n,k]^T        (fwd  y  = x * w^T)
//   xc_sgemm_tn  c[m,n]  = aT[k,m] * b[k,n]         (bwd  dW = dy^T * x)
// All accumulate per element in p-ascending order (deterministic for a
// fixed shape on a fixed device).  When %p_acc != 0 the kernel does
// c += dot — gradient semantics mirror the CPU axpy lanes.
// 16x16 shared tiles, 256 threads, fp32 accumulate.

#pragma once

namespace xcuda_ptx {

inline const char* tgemm() {
    return R"PTX(
// ---- fp32 NN: c[r,c] = sum_p a[r*k+p] * b[p*n+c], c += when acc -------
.visible .entry xc_sgemm_nn(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u64 %p_m, .param .u64 %p_k, .param .u64 %p_n,
    .param .u32 %p_acc)
{
    .shared .align 4 .b8 xc_snn_as[1024];
    .shared .align 4 .b8 xc_snn_bs[1024];
    .reg .pred %p<8>;
    .reg .b32  %r<12>;
    .reg .b64  %rd<24>;
    .reg .f32  %f<8>;
    ld.param.u64 %rd1, [%p_a];
    ld.param.u64 %rd2, [%p_b];
    ld.param.u64 %rd3, [%p_c];
    ld.param.u64 %rd4, [%p_m];
    ld.param.u64 %rd5, [%p_k];
    ld.param.u64 %rd6, [%p_n];
    ld.param.u32 %r10, [%p_acc];
    mov.u32 %r1, %ctaid.y;
    mov.u32 %r2, %ctaid.x;
    mov.u32 %r3, %tid.y;
    mov.u32 %r4, %tid.x;
    shl.b32 %r5, %r1, 4;
    add.s32 %r5, %r5, %r3;                        // row
    shl.b32 %r6, %r2, 4;
    add.s32 %r6, %r6, %r4;                        // col
    cvt.u64.u32 %rd7, %r5;
    cvt.u64.u32 %rd8, %r6;
    mov.f32 %f1, 0f00000000;                      // acc
    add.u64 %rd9, %rd5, 15;
    shr.u64 %rd9, %rd9, 4;                        // nt
    mov.u64 %rd10, 0;                             // t
XC_SNN_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_SNN_OUT;
    shl.b64 %rd11, %rd10, 4;                      // t*16
    cvt.u64.u32 %rd12, %r4;
    add.u64 %rd12, %rd11, %rd12;                  // a_col = t*16+tx
    cvt.u64.u32 %rd13, %r3;
    add.u64 %rd13, %rd11, %rd13;                  // b_row = t*16+ty
    mov.f32 %f2, 0f00000000;
    setp.lt.u64 %p2, %rd7, %rd4;                  // row < m
    setp.lt.u64 %p3, %rd12, %rd5;                 // a_col < k
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_SNN_SA;
    mul.lo.u64 %rd14, %rd7, %rd5;
    add.u64 %rd14, %rd14, %rd12;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd1, %rd14;
    ld.global.f32 %f2, [%rd14];
XC_SNN_SA:
    mov.u64 %rd15, xc_snn_as;
    shl.b32 %r7, %r3, 4;
    add.s32 %r7, %r7, %r4;                        // ty*16+tx
    cvt.u64.u32 %rd16, %r7;
    shl.b64 %rd16, %rd16, 2;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f32 [%rd15], %f2;
    mov.f32 %f3, 0f00000000;
    setp.lt.u64 %p5, %rd13, %rd5;                 // b_row < k
    setp.lt.u64 %p6, %rd8, %rd6;                  // col < n
    and.pred %p7, %p5, %p6;
    @!%p7 bra XC_SNN_SB;
    mul.lo.u64 %rd14, %rd13, %rd6;
    add.u64 %rd14, %rd14, %rd8;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd2, %rd14;
    ld.global.f32 %f3, [%rd14];
XC_SNN_SB:
    mov.u64 %rd15, xc_snn_bs;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f32 [%rd15], %f3;
    bar.sync 0;
    mov.u64 %rd17, xc_snn_as;
    cvt.u64.u32 %rd18, %r3;
    shl.b64 %rd18, %rd18, 6;                      // ty*64
    add.u64 %rd17, %rd17, %rd18;
    mov.u64 %rd19, xc_snn_bs;
    cvt.u64.u32 %rd18, %r4;
    shl.b64 %rd18, %rd18, 2;
    add.u64 %rd19, %rd19, %rd18;
    ld.shared.f32 %f4, [%rd17+0];   ld.shared.f32 %f5, [%rd19+0];    fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+4];   ld.shared.f32 %f5, [%rd19+64];   fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+8];   ld.shared.f32 %f5, [%rd19+128];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+12];  ld.shared.f32 %f5, [%rd19+192];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+16];  ld.shared.f32 %f5, [%rd19+256];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+20];  ld.shared.f32 %f5, [%rd19+320];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+24];  ld.shared.f32 %f5, [%rd19+384];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+28];  ld.shared.f32 %f5, [%rd19+448];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+32];  ld.shared.f32 %f5, [%rd19+512];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+36];  ld.shared.f32 %f5, [%rd19+576];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+40];  ld.shared.f32 %f5, [%rd19+640];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+44];  ld.shared.f32 %f5, [%rd19+704];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+48];  ld.shared.f32 %f5, [%rd19+768];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+52];  ld.shared.f32 %f5, [%rd19+832];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+56];  ld.shared.f32 %f5, [%rd19+896];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+60];  ld.shared.f32 %f5, [%rd19+960];  fma.rn.f32 %f1, %f4, %f5, %f1;
    bar.sync 0;
    add.u64 %rd10, %rd10, 1;
    bra XC_SNN_TILE;
XC_SNN_OUT:
    setp.lt.u64 %p1, %rd7, %rd4;
    setp.lt.u64 %p2, %rd8, %rd6;
    and.pred %p3, %p1, %p2;
    @!%p3 bra XC_SNN_RET;
    mul.lo.u64 %rd14, %rd7, %rd6;
    add.u64 %rd14, %rd14, %rd8;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.eq.u32 %p4, %r10, 0;
    @%p4 bra XC_SNN_ST;
    ld.global.f32 %f6, [%rd14];
    add.f32 %f1, %f1, %f6;                        // acc: dot + old
XC_SNN_ST:
    st.global.f32 [%rd14], %f1;
XC_SNN_RET:
    ret;
}

// ---- fp32 NT: c[r,c] = sum_p a[r*k+p] * w[c*k+p], c += when acc ------
// B stage-in maps p to tid.x so the transposed operand still loads
// coalesced; bs stays [p][c] for the shared FMA body.
.visible .entry xc_sgemm_nt(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u64 %p_m, .param .u64 %p_k, .param .u64 %p_n,
    .param .u32 %p_acc)
{
    .shared .align 4 .b8 xc_snt_as[1024];
    .shared .align 4 .b8 xc_snt_bs[1024];
    .reg .pred %p<8>;
    .reg .b32  %r<12>;
    .reg .b64  %rd<24>;
    .reg .f32  %f<8>;
    ld.param.u64 %rd1, [%p_a];
    ld.param.u64 %rd2, [%p_b];
    ld.param.u64 %rd3, [%p_c];
    ld.param.u64 %rd4, [%p_m];
    ld.param.u64 %rd5, [%p_k];
    ld.param.u64 %rd6, [%p_n];
    ld.param.u32 %r10, [%p_acc];
    mov.u32 %r1, %ctaid.y;
    mov.u32 %r2, %ctaid.x;
    mov.u32 %r3, %tid.y;
    mov.u32 %r4, %tid.x;
    shl.b32 %r5, %r1, 4;
    add.s32 %r5, %r5, %r3;                        // row
    shl.b32 %r6, %r2, 4;
    add.s32 %r6, %r6, %r4;                        // col
    cvt.u64.u32 %rd7, %r5;
    cvt.u64.u32 %rd8, %r6;
    mov.f32 %f1, 0f00000000;                      // acc
    add.u64 %rd9, %rd5, 15;
    shr.u64 %rd9, %rd9, 4;                        // nt
    mov.u64 %rd10, 0;                             // t
XC_SNT_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_SNT_OUT;
    shl.b64 %rd11, %rd10, 4;                      // t*16
    cvt.u64.u32 %rd12, %r4;
    add.u64 %rd12, %rd11, %rd12;                  // a_col / p = t*16+tx
    mov.f32 %f2, 0f00000000;
    setp.lt.u64 %p2, %rd7, %rd4;
    setp.lt.u64 %p3, %rd12, %rd5;
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_SNT_SA;
    mul.lo.u64 %rd14, %rd7, %rd5;
    add.u64 %rd14, %rd14, %rd12;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd1, %rd14;
    ld.global.f32 %f2, [%rd14];
XC_SNT_SA:
    mov.u64 %rd15, xc_snt_as;
    shl.b32 %r7, %r3, 4;
    add.s32 %r7, %r7, %r4;                        // ty*16+tx
    cvt.u64.u32 %rd16, %r7;
    shl.b64 %rd16, %rd16, 2;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f32 [%rd15], %f2;
    // B tile: bs[p_loc][c_loc] = w[(cb + c_loc)*k + t*16 + p_loc]
    // with p_loc=tx, c_loc=ty -> consecutive tx read consecutive p.
    mov.f32 %f3, 0f00000000;
    shl.b32 %r11, %r2, 4;
    add.s32 %r11, %r11, %r3;                      // bcol = ctaid.x*16 + ty
    cvt.u64.u32 %rd20, %r11;
    setp.lt.u64 %p5, %rd12, %rd5;                 // p < k
    setp.lt.u64 %p6, %rd20, %rd6;                 // bcol < n
    and.pred %p7, %p5, %p6;
    @!%p7 bra XC_SNT_SB;
    mul.lo.u64 %rd14, %rd20, %rd5;                // bcol * k
    add.u64 %rd14, %rd14, %rd12;                  // + p
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd2, %rd14;
    ld.global.f32 %f3, [%rd14];
XC_SNT_SB:
    mov.u64 %rd15, xc_snt_bs;
    shl.b32 %r8, %r4, 4;
    add.s32 %r8, %r8, %r3;                        // tx*16 + ty = p*16+c
    cvt.u64.u32 %rd21, %r8;
    shl.b64 %rd21, %rd21, 2;
    add.u64 %rd15, %rd15, %rd21;
    st.shared.f32 [%rd15], %f3;
    bar.sync 0;
    mov.u64 %rd17, xc_snt_as;
    cvt.u64.u32 %rd18, %r3;
    shl.b64 %rd18, %rd18, 6;                      // ty*64
    add.u64 %rd17, %rd17, %rd18;
    mov.u64 %rd19, xc_snt_bs;
    cvt.u64.u32 %rd18, %r4;
    shl.b64 %rd18, %rd18, 2;
    add.u64 %rd19, %rd19, %rd18;
    ld.shared.f32 %f4, [%rd17+0];   ld.shared.f32 %f5, [%rd19+0];    fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+4];   ld.shared.f32 %f5, [%rd19+64];   fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+8];   ld.shared.f32 %f5, [%rd19+128];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+12];  ld.shared.f32 %f5, [%rd19+192];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+16];  ld.shared.f32 %f5, [%rd19+256];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+20];  ld.shared.f32 %f5, [%rd19+320];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+24];  ld.shared.f32 %f5, [%rd19+384];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+28];  ld.shared.f32 %f5, [%rd19+448];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+32];  ld.shared.f32 %f5, [%rd19+512];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+36];  ld.shared.f32 %f5, [%rd19+576];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+40];  ld.shared.f32 %f5, [%rd19+640];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+44];  ld.shared.f32 %f5, [%rd19+704];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+48];  ld.shared.f32 %f5, [%rd19+768];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+52];  ld.shared.f32 %f5, [%rd19+832];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+56];  ld.shared.f32 %f5, [%rd19+896];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+60];  ld.shared.f32 %f5, [%rd19+960];  fma.rn.f32 %f1, %f4, %f5, %f1;
    bar.sync 0;
    add.u64 %rd10, %rd10, 1;
    bra XC_SNT_TILE;
XC_SNT_OUT:
    setp.lt.u64 %p1, %rd7, %rd4;
    setp.lt.u64 %p2, %rd8, %rd6;
    and.pred %p3, %p1, %p2;
    @!%p3 bra XC_SNT_RET;
    mul.lo.u64 %rd14, %rd7, %rd6;
    add.u64 %rd14, %rd14, %rd8;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.eq.u32 %p4, %r10, 0;
    @%p4 bra XC_SNT_ST;
    ld.global.f32 %f6, [%rd14];
    add.f32 %f1, %f1, %f6;
XC_SNT_ST:
    st.global.f32 [%rd14], %f1;
XC_SNT_RET:
    ret;
}

// ---- fp32 TN: c[r,c] = sum_p a[p*m+r] * b[p*n+c], c += when acc ------
// A stage-in maps r to tid.x so the transposed operand still loads
// coalesced; as stays [r][p] for the shared FMA body.
.visible .entry xc_sgemm_tn(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u64 %p_m, .param .u64 %p_k, .param .u64 %p_n,
    .param .u32 %p_acc)
{
    .shared .align 4 .b8 xc_stn_as[1024];
    .shared .align 4 .b8 xc_stn_bs[1024];
    .reg .pred %p<8>;
    .reg .b32  %r<12>;
    .reg .b64  %rd<24>;
    .reg .f32  %f<8>;
    ld.param.u64 %rd1, [%p_a];
    ld.param.u64 %rd2, [%p_b];
    ld.param.u64 %rd3, [%p_c];
    ld.param.u64 %rd4, [%p_m];
    ld.param.u64 %rd5, [%p_k];
    ld.param.u64 %rd6, [%p_n];
    ld.param.u32 %r10, [%p_acc];
    mov.u32 %r1, %ctaid.y;
    mov.u32 %r2, %ctaid.x;
    mov.u32 %r3, %tid.y;
    mov.u32 %r4, %tid.x;
    shl.b32 %r5, %r1, 4;
    add.s32 %r5, %r5, %r3;                        // row
    shl.b32 %r6, %r2, 4;
    add.s32 %r6, %r6, %r4;                        // col
    cvt.u64.u32 %rd7, %r5;
    cvt.u64.u32 %rd8, %r6;
    mov.f32 %f1, 0f00000000;                      // acc
    add.u64 %rd9, %rd5, 15;
    shr.u64 %rd9, %rd9, 4;                        // nt
    mov.u64 %rd10, 0;                             // t
XC_STN_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_STN_OUT;
    shl.b64 %rd11, %rd10, 4;                      // t*16
    // A tile: as[r_loc][p_loc] = a[(t*16+p_loc)*m + rb + r_loc]
    // with r_loc=tx, p_loc=ty -> consecutive tx read consecutive r.
    cvt.u64.u32 %rd12, %r3;
    add.u64 %rd12, %rd11, %rd12;                  // a_row = t*16+ty
    mov.f32 %f2, 0f00000000;
    shl.b32 %r11, %r1, 4;
    add.s32 %r11, %r11, %r4;                      // a_col = rb + tx
    cvt.u64.u32 %rd20, %r11;
    setp.lt.u64 %p2, %rd20, %rd4;                 // a_col < m
    setp.lt.u64 %p3, %rd12, %rd5;                 // a_row < k
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_STN_SA;
    mul.lo.u64 %rd14, %rd12, %rd4;                // a_row * m
    add.u64 %rd14, %rd14, %rd20;                  // + a_col
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd1, %rd14;
    ld.global.f32 %f2, [%rd14];
XC_STN_SA:
    mov.u64 %rd15, xc_stn_as;
    shl.b32 %r7, %r4, 4;
    add.s32 %r7, %r7, %r3;                        // tx*16 + ty = r*16+p
    cvt.u64.u32 %rd16, %r7;
    shl.b64 %rd16, %rd16, 2;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f32 [%rd15], %f2;
    cvt.u64.u32 %rd13, %r3;
    add.u64 %rd13, %rd11, %rd13;                  // b_row = t*16+ty
    mov.f32 %f3, 0f00000000;
    setp.lt.u64 %p5, %rd13, %rd5;                 // b_row < k
    setp.lt.u64 %p6, %rd8, %rd6;                  // col < n
    and.pred %p7, %p5, %p6;
    @!%p7 bra XC_STN_SB;
    mul.lo.u64 %rd14, %rd13, %rd6;
    add.u64 %rd14, %rd14, %rd8;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd2, %rd14;
    ld.global.f32 %f3, [%rd14];
XC_STN_SB:
    mov.u64 %rd15, xc_stn_bs;
    shl.b32 %r9, %r3, 4;
    add.s32 %r9, %r9, %r4;                        // ty*16+tx
    cvt.u64.u32 %rd16, %r9;
    shl.b64 %rd16, %rd16, 2;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f32 [%rd15], %f3;
    bar.sync 0;
    mov.u64 %rd17, xc_stn_as;
    cvt.u64.u32 %rd18, %r3;
    shl.b64 %rd18, %rd18, 6;                      // ty*64
    add.u64 %rd17, %rd17, %rd18;
    mov.u64 %rd19, xc_stn_bs;
    cvt.u64.u32 %rd18, %r4;
    shl.b64 %rd18, %rd18, 2;
    add.u64 %rd19, %rd19, %rd18;
    ld.shared.f32 %f4, [%rd17+0];   ld.shared.f32 %f5, [%rd19+0];    fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+4];   ld.shared.f32 %f5, [%rd19+64];   fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+8];   ld.shared.f32 %f5, [%rd19+128];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+12];  ld.shared.f32 %f5, [%rd19+192];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+16];  ld.shared.f32 %f5, [%rd19+256];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+20];  ld.shared.f32 %f5, [%rd19+320];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+24];  ld.shared.f32 %f5, [%rd19+384];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+28];  ld.shared.f32 %f5, [%rd19+448];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+32];  ld.shared.f32 %f5, [%rd19+512];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+36];  ld.shared.f32 %f5, [%rd19+576];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+40];  ld.shared.f32 %f5, [%rd19+640];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+44];  ld.shared.f32 %f5, [%rd19+704];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+48];  ld.shared.f32 %f5, [%rd19+768];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+52];  ld.shared.f32 %f5, [%rd19+832];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+56];  ld.shared.f32 %f5, [%rd19+896];  fma.rn.f32 %f1, %f4, %f5, %f1;
    ld.shared.f32 %f4, [%rd17+60];  ld.shared.f32 %f5, [%rd19+960];  fma.rn.f32 %f1, %f4, %f5, %f1;
    bar.sync 0;
    add.u64 %rd10, %rd10, 1;
    bra XC_STN_TILE;
XC_STN_OUT:
    setp.lt.u64 %p1, %rd7, %rd4;
    setp.lt.u64 %p2, %rd8, %rd6;
    and.pred %p3, %p1, %p2;
    @!%p3 bra XC_STN_RET;
    mul.lo.u64 %rd14, %rd7, %rd6;
    add.u64 %rd14, %rd14, %rd8;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    setp.eq.u32 %p4, %r10, 0;
    @%p4 bra XC_STN_ST;
    ld.global.f32 %f6, [%rd14];
    add.f32 %f1, %f1, %f6;
XC_STN_ST:
    st.global.f32 [%rd14], %f1;
XC_STN_RET:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
