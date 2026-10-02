// cuda_ptx_kv.h -- embedded PTX online-softmax KV attention (B132):
//   xc_kv_attention -- 1:1 port of the retired NVRTC kernel: fp64 online
//   softmax over the device-resident KV cache, tile=128, shared layout
//   scores[128] | acc[head_dim] | red[32] | scal[5] | q[head_dim]; exp()
//   through the in-module xc_exp_f64 helper. The q row is staged into
//   shared once per block instead of being re-fetched per candidate --
//   accumulation order and rescaling stay identical to the host fp64
//   reference.

#pragma once

namespace xcuda_ptx {

inline const char* kv() {
    return R"PTX(
.extern .shared .align 8 .b8 xc_kv_smem[];
.visible .entry xc_kv_attention(
    .param .u64 %p_q, .param .u64 %p_kbuf, .param .u64 %p_vbuf,
    .param .u64 %p_heads, .param .u64 %p_seq, .param .u64 %p_kv_heads,
    .param .u64 %p_head_dim, .param .u64 %p_max_len,
    .param .u64 %p_pos_off,
    .param .u64 %p_out, .param .u64 %p_out_stride)
{
    .reg .pred %p<13>;
    .reg .b32  %r<14>;
    .reg .b64  %rd<44>;
    .reg .f64  %fd<24>;
    .param .b64 %po_exp, %pi_exp;
    ld.param.u64 %rd1, [%p_q];
    ld.param.u64 %rd2, [%p_kbuf];
    ld.param.u64 %rd3, [%p_vbuf];
    ld.param.u64 %rd4, [%p_heads];
    ld.param.u64 %rd5, [%p_seq];
    ld.param.u64 %rd6, [%p_kv_heads];
    ld.param.u64 %rd7, [%p_head_dim];
    ld.param.u64 %rd8, [%p_max_len];
    ld.param.u64 %rd9, [%p_pos_off];
    ld.param.u64 %rd10, [%p_out];
    ld.param.u64 %rd11, [%p_out_stride];
    mov.u32 %r1, %ctaid.x;
    mov.u32 %r2, %tid.x;
    mov.u32 %r3, %ntid.x;
    cvt.u64.u32 %rd12, %r1;                       // bx
    cvt.u64.u32 %rd13, %r2;                       // tid
    cvt.u64.u32 %rd14, %r3;                       // ntid
    div.u64 %rd15, %rd12, %rd5;                   // h = bx/seq
    mul.lo.u64 %rd16, %rd15, %rd5;
    sub.u64 %rd16, %rd12, %rd16;                  // s = bx%seq
    div.u64 %rd17, %rd4, %rd6;                    // heads/kv_heads
    div.u64 %rd18, %rd15, %rd17;                  // kvh
    add.u64 %rd19, %rd9, %rd16;                   // last = po + s
    mul.lo.u64 %rd20, %rd12, %rd7;                // bx*hd (h*seq+s == bx)
    shl.b64 %rd20, %rd20, 3;
    add.u64 %rd20, %rd1, %rd20;                   // q_row
    mul.lo.u64 %rd21, %rd18, %rd8;
    mul.lo.u64 %rd21, %rd21, %rd7;
    shl.b64 %rd21, %rd21, 3;
    add.u64 %rd21, %rd2, %rd21;                   // kbase
    mul.lo.u64 %rd22, %rd18, %rd8;
    mul.lo.u64 %rd22, %rd22, %rd7;
    shl.b64 %rd22, %rd22, 3;
    add.u64 %rd22, %rd3, %rd22;                   // vbase
    cvt.rn.f64.u64 %fd1, %rd7;
    sqrt.rn.f64 %fd2, %fd1;
    div.rn.f64 %fd3, 0d3FF0000000000000, %fd2;    // scale
    mov.u64 %rd23, xc_kv_smem;                    // scores
    add.u64 %rd24, %rd23, 1024;                   // acc
    shl.b64 %rd25, %rd7, 3;
    add.u64 %rd25, %rd24, %rd25;                  // red
    add.u64 %rd26, %rd25, 256;                    // scal
    add.u64 %rd42, %rd26, 40;                     // q staging (head_dim)
    add.u32 %r4, %r3, 31;
    shr.u32 %r4, %r4, 5;                          // warps
    shr.u32 %r5, %r2, 5;                          // wid
    and.b32 %r6, %r2, 31;                         // lane
    setp.ne.u32 %p1, %r2, 0;
    @%p1 bra XC_KV_I0;
    st.shared.f64 [%rd26], 0dFFF0000000000000;    // scal[0] = -inf
    st.shared.f64 [%rd26+8], 0d0000000000000000;  // scal[1] = 0
XC_KV_I0:
    mov.u64 %rd43, %rd13;                         // d = tid
XC_KV_Q:                                          // q[d] -> smem
    setp.ge.u64 %p2, %rd43, %rd7;
    @%p2 bra XC_KV_QD;
    shl.b64 %rd35, %rd43, 3;
    add.u64 %rd36, %rd20, %rd35;
    ld.global.f64 %fd23, [%rd36];
    add.u64 %rd36, %rd42, %rd35;
    st.shared.f64 [%rd36], %fd23;
    add.u64 %rd43, %rd43, %rd14;
    bra XC_KV_Q;
XC_KV_QD:
    mov.u64 %rd27, %rd13;                         // d
XC_KV_ZACC:
    setp.ge.u64 %p2, %rd27, %rd7;
    @%p2 bra XC_KV_ZD;
    shl.b64 %rd28, %rd27, 3;
    add.u64 %rd28, %rd24, %rd28;
    st.shared.f64 [%rd28], 0d0000000000000000;    // acc[d] = 0
    add.u64 %rd27, %rd27, %rd14;
    bra XC_KV_ZACC;
XC_KV_ZD:
    bar.sync 0;
    mov.u64 %rd29, 0;                             // t0
XC_KV_TILE:
    setp.gt.u64 %p3, %rd29, %rd19;
    @%p3 bra XC_KV_FIN;
    sub.u64 %rd30, %rd19, %rd29;
    add.u64 %rd30, %rd30, 1;                      // rem
    setp.lt.u64 %p4, %rd30, 128;
    selp.u64 %rd30, %rd30, 128, %p4;              // tn
    mov.u64 %rd31, %rd13;                         // j
XC_KV_DOT:
    setp.ge.u64 %p5, %rd31, %rd30;
    @%p5 bra XC_KV_DOTD;
    add.u64 %rd32, %rd29, %rd31;
    mul.lo.u64 %rd32, %rd32, %rd7;
    shl.b64 %rd32, %rd32, 3;
    add.u64 %rd32, %rd21, %rd32;                  // krow
    mov.f64 %fd5, 0d0000000000000000;             // dot
    mov.u64 %rd33, 0;                             // d
XC_KV_DOTI:
    setp.ge.u64 %p6, %rd33, %rd7;
    @%p6 bra XC_KV_DOTW;
    shl.b64 %rd35, %rd33, 3;
    add.u64 %rd36, %rd42, %rd35;
    ld.shared.f64 %fd6, [%rd36];                  // q[d] staged
    add.u64 %rd36, %rd32, %rd35;
    ld.global.f64 %fd7, [%rd36];
    fma.rn.f64 %fd5, %fd6, %fd7, %fd5;
    add.u64 %rd33, %rd33, 1;
    bra XC_KV_DOTI;
XC_KV_DOTW:
    mul.rn.f64 %fd5, %fd5, %fd3;
    shl.b64 %rd35, %rd31, 3;
    add.u64 %rd35, %rd23, %rd35;
    st.shared.f64 [%rd35], %fd5;                  // scores[j]
    add.u64 %rd31, %rd31, %rd14;
    bra XC_KV_DOT;
XC_KV_DOTD:
    bar.sync 0;
    mov.f64 %fd8, 0dFFF0000000000000;             // tmax = -inf
    mov.u64 %rd31, %rd13;
XC_KV_MAX:
    setp.ge.u64 %p7, %rd31, %rd30;
    @%p7 bra XC_KV_MAXW;
    shl.b64 %rd35, %rd31, 3;
    add.u64 %rd35, %rd23, %rd35;
    ld.shared.f64 %fd9, [%rd35];
    max.f64 %fd8, %fd8, %fd9;
    add.u64 %rd31, %rd31, %rd14;
    bra XC_KV_MAX;
XC_KV_MAXW:                                       // f64 warp max reduce
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 16, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 16, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    max.f64 %fd8, %fd8, %fd10;
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 8, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 8, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    max.f64 %fd8, %fd8, %fd10;
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 4, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 4, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    max.f64 %fd8, %fd8, %fd10;
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 2, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 2, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    max.f64 %fd8, %fd8, %fd10;
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 1, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 1, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    max.f64 %fd8, %fd8, %fd10;
    setp.ne.u32 %p8, %r6, 0;
    @%p8 bra XC_KV_MAXS;
    cvt.u64.u32 %rd40, %r5;
    shl.b64 %rd40, %rd40, 3;
    add.u64 %rd40, %rd25, %rd40;
    st.shared.f64 [%rd40], %fd8;                  // red[wid] = tmax
XC_KV_MAXS:
    bar.sync 0;
    setp.ne.u32 %p9, %r2, 0;
    @%p9 bra XC_KV_MAXP;
    ld.shared.f64 %fd11, [%rd25];                 // m_tile = red[0]
    mov.u32 %r11, 1;
XC_KV_MAXR:
    setp.ge.u32 %p10, %r11, %r4;
    @%p10 bra XC_KV_MAXR_D;
    cvt.u64.u32 %rd41, %r11;
    shl.b64 %rd41, %rd41, 3;
    add.u64 %rd41, %rd25, %rd41;
    ld.shared.f64 %fd12, [%rd41];
    max.f64 %fd11, %fd11, %fd12;
    add.u32 %r11, %r11, 1;
    bra XC_KV_MAXR;
XC_KV_MAXR_D:
    ld.shared.f64 %fd12, [%rd26];                 // scal[0]
    max.f64 %fd11, %fd11, %fd12;                  // m_new
    sub.rn.f64 %fd13, %fd12, %fd11;               // scal[0]-m_new
    st.param.f64 [%pi_exp], %fd13;
    call (%po_exp), xc_exp_f64, (%pi_exp);
    ld.param.f64 %fd14, [%po_exp];
    st.shared.f64 [%rd26+16], %fd14;              // scal[2] = r
    st.shared.f64 [%rd26+24], %fd11;              // scal[3] = m_new
XC_KV_MAXP:
    bar.sync 0;
    ld.shared.f64 %fd15, [%rd26+16];              // r
    ld.shared.f64 %fd16, [%rd26+24];              // m_new
    mov.u64 %rd31, %rd13;
XC_KV_EXP:                                        // scores[j] = exp(-)
    setp.ge.u64 %p11, %rd31, %rd30;
    @%p11 bra XC_KV_EXPD;
    shl.b64 %rd35, %rd31, 3;
    add.u64 %rd35, %rd23, %rd35;
    ld.shared.f64 %fd17, [%rd35];
    sub.rn.f64 %fd17, %fd17, %fd16;
    st.param.f64 [%pi_exp], %fd17;
    call (%po_exp), xc_exp_f64, (%pi_exp);
    ld.param.f64 %fd17, [%po_exp];
    st.shared.f64 [%rd35], %fd17;
    add.u64 %rd31, %rd31, %rd14;
    bra XC_KV_EXP;
XC_KV_EXPD:
    bar.sync 0;
    mov.f64 %fd8, 0d0000000000000000;             // tsum
    mov.u64 %rd31, %rd13;
XC_KV_SUM:
    setp.ge.u64 %p7, %rd31, %rd30;
    @%p7 bra XC_KV_SUMW;
    shl.b64 %rd35, %rd31, 3;
    add.u64 %rd35, %rd23, %rd35;
    ld.shared.f64 %fd9, [%rd35];
    add.rn.f64 %fd8, %fd8, %fd9;
    add.u64 %rd31, %rd31, %rd14;
    bra XC_KV_SUM;
XC_KV_SUMW:                                       // f64 warp sum reduce
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 16, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 16, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    add.rn.f64 %fd8, %fd8, %fd10;
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 8, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 8, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    add.rn.f64 %fd8, %fd8, %fd10;
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 4, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 4, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    add.rn.f64 %fd8, %fd8, %fd10;
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 2, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 2, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    add.rn.f64 %fd8, %fd8, %fd10;
    mov.b64 {%r7, %r8}, %fd8;
    shfl.sync.down.b32 %r9, %r7, 1, 31, 0xffffffff;
    shfl.sync.down.b32 %r10, %r8, 1, 31, 0xffffffff;
    mov.b64 %rd39, {%r9, %r10};
    mov.b64 %fd10, %rd39;
    add.rn.f64 %fd8, %fd8, %fd10;
    setp.ne.u32 %p8, %r6, 0;
    @%p8 bra XC_KV_SUMS;
    cvt.u64.u32 %rd40, %r5;
    shl.b64 %rd40, %rd40, 3;
    add.u64 %rd40, %rd25, %rd40;
    st.shared.f64 [%rd40], %fd8;                  // red[wid] = tsum
XC_KV_SUMS:
    bar.sync 0;
    setp.ne.u32 %p9, %r2, 0;
    @%p9 bra XC_KV_SUMP;
    ld.shared.f64 %fd11, [%rd25];                 // l_tile = red[0]
    mov.u32 %r11, 1;
XC_KV_SUMR:
    setp.ge.u32 %p10, %r11, %r4;
    @%p10 bra XC_KV_SUMR_D;
    cvt.u64.u32 %rd41, %r11;
    shl.b64 %rd41, %rd41, 3;
    add.u64 %rd41, %rd25, %rd41;
    ld.shared.f64 %fd12, [%rd41];
    add.rn.f64 %fd11, %fd11, %fd12;
    add.u32 %r11, %r11, 1;
    bra XC_KV_SUMR;
XC_KV_SUMR_D:
    ld.shared.f64 %fd13, [%rd26+8];               // scal[1]
    mul.rn.f64 %fd13, %fd13, %fd15;
    add.rn.f64 %fd13, %fd13, %fd11;               // scal[1]*r + l_tile
    st.shared.f64 [%rd26+8], %fd13;
    st.shared.f64 [%rd26], %fd16;                 // scal[0] = m_new
XC_KV_SUMP:
    mov.u64 %rd31, %rd13;                         // d
XC_KV_ACC:
    setp.ge.u64 %p12, %rd31, %rd7;
    @%p12 bra XC_KV_ACCD;
    shl.b64 %rd35, %rd31, 3;
    add.u64 %rd36, %rd24, %rd35;                  // &acc[d]
    ld.shared.f64 %fd18, [%rd36];
    mul.rn.f64 %fd18, %fd18, %fd15;               // acc[d]*r
    mov.u64 %rd37, 0;                             // j
XC_KV_ACCI:
    setp.ge.u64 %p7, %rd37, %rd30;
    @%p7 bra XC_KV_ACCS;
    shl.b64 %rd38, %rd37, 3;
    add.u64 %rd38, %rd23, %rd38;
    ld.shared.f64 %fd19, [%rd38];                 // scores[j]
    add.u64 %rd39, %rd29, %rd37;
    mul.lo.u64 %rd39, %rd39, %rd7;
    add.u64 %rd39, %rd39, %rd31;                  // (t0+j)*hd + d
    shl.b64 %rd39, %rd39, 3;
    add.u64 %rd39, %rd22, %rd39;
    ld.global.f64 %fd20, [%rd39];
    fma.rn.f64 %fd18, %fd19, %fd20, %fd18;
    add.u64 %rd37, %rd37, 1;
    bra XC_KV_ACCI;
XC_KV_ACCS:
    st.shared.f64 [%rd36], %fd18;
    add.u64 %rd31, %rd31, %rd14;
    bra XC_KV_ACC;
XC_KV_ACCD:
    bar.sync 0;
    add.u64 %rd29, %rd29, 128;
    bra XC_KV_TILE;
XC_KV_FIN:
    setp.ne.u32 %p1, %r2, 0;
    @%p1 bra XC_KV_F2;
    ld.shared.f64 %fd21, [%rd26+8];
    div.rn.f64 %fd21, 0d3FF0000000000000, %fd21;
    st.shared.f64 [%rd26+32], %fd21;              // scal[4] = 1/l
XC_KV_F2:
    bar.sync 0;
    ld.shared.f64 %fd22, [%rd26+32];              // inv_l
    mul.lo.u64 %rd35, %rd16, %rd11;               // s*out_stride
    mul.lo.u64 %rd36, %rd15, %rd7;                // h*hd
    add.u64 %rd35, %rd35, %rd36;
    shl.b64 %rd35, %rd35, 3;
    add.u64 %rd35, %rd10, %rd35;                  // orow
    mov.u64 %rd31, %rd13;
XC_KV_OUT:
    setp.ge.u64 %p2, %rd31, %rd7;
    @%p2 bra XC_KV_END;
    shl.b64 %rd36, %rd31, 3;
    add.u64 %rd37, %rd24, %rd36;
    ld.shared.f64 %fd23, [%rd37];
    mul.rn.f64 %fd23, %fd23, %fd22;
    add.u64 %rd37, %rd35, %rd36;
    st.global.f64 [%rd37], %fd23;
    add.u64 %rd31, %rd31, %rd14;
    bra XC_KV_OUT;
XC_KV_END:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
