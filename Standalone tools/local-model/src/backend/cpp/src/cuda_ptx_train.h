// cuda_ptx_train.h -- embedded PTX training kernels (B132 ?26):
//   xc_adamw_fused   one fused pass: grad scale -> bias-corrected m/v ->
//                    decoupled weight decay -> w update (fp32 state, host
//                    fp64 bc1/bc2 -- matches the trainer's std::pow path).
//   xc_sqsum_part    block partials of sumx^2 (fp32); the host reduces in
//                    fp64 -- same precision class as the scalar loop.

#pragma once

namespace xcuda_ptx {

inline const char* train() {
    return R"PTX(
// ---- fused AdamW ------------------------------------------------------
.visible .entry xc_adamw_fused(
    .param .u64 %p_g, .param .u64 %p_m, .param .u64 %p_v,
    .param .u64 %p_w,
    .param .f32 %p_gscale, .param .f32 %p_lr_t, .param .f32 %p_wd,
    .param .f32 %p_b1, .param .f32 %p_b2,
    .param .f32 %p_bc1, .param .f32 %p_bc2, .param .f32 %p_eps,
    .param .u64 %p_n)
{
    .reg .pred %p<2>;
    .reg .b32  %r<6>;
    .reg .b64  %rd<20>;
    .reg .f32  %f<18>;
    ld.param.u64 %rd1, [%p_g];
    ld.param.u64 %rd2, [%p_m];
    ld.param.u64 %rd3, [%p_v];
    ld.param.u64 %rd4, [%p_w];
    ld.param.f32 %f6, [%p_gscale];
    ld.param.f32 %f7, [%p_lr_t];
    ld.param.f32 %f8, [%p_wd];
    ld.param.f32 %f9, [%p_b1];
    ld.param.f32 %f10, [%p_b2];
    ld.param.f32 %f11, [%p_bc1];
    ld.param.f32 %f12, [%p_bc2];
    ld.param.f32 %f13, [%p_eps];
    ld.param.u64 %rd5, [%p_n];
    mov.u32 %r1, %nctaid.x;
    mov.u32 %r2, %ntid.x;
    mov.u32 %r3, %ctaid.x;
    mov.u32 %r4, %tid.x;
    cvt.u64.u32 %rd6, %r1;
    cvt.u64.u32 %rd7, %r2;
    mul.lo.u64 %rd8, %rd6, %rd7;                  // stride
    cvt.u64.u32 %rd6, %r3;
    mul.lo.u64 %rd9, %rd6, %rd7;
    cvt.u64.u32 %rd7, %r4;
    add.u64 %rd9, %rd9, %rd7;                     // i
XC_AW_LOOP:
    setp.ge.u64 %p1, %rd9, %rd5;
    @%p1 bra XC_AW_DONE;
    shl.b64 %rd10, %rd9, 2;                       // i*4
    add.u64 %rd11, %rd1, %rd10;
    ld.global.f32 %f1, [%rd11];                   // g[i]
    add.u64 %rd12, %rd2, %rd10;
    ld.global.f32 %f2, [%rd12];                   // m[i]
    add.u64 %rd13, %rd3, %rd10;
    ld.global.f32 %f3, [%rd13];                   // v[i]
    add.u64 %rd14, %rd4, %rd10;
    ld.global.f32 %f4, [%rd14];                   // w[i]
    mul.rn.f32 %f5, %f1, %f6;                     // gi = g*gscale
    sub.rn.f32 %f14, 0f3F800000, %f9;             // 1-b1
    mul.rn.f32 %f14, %f14, %f5;
    fma.rn.f32 %f15, %f9, %f2, %f14;              // mi
    sub.rn.f32 %f14, 0f3F800000, %f10;            // 1-b2
    mul.rn.f32 %f14, %f14, %f5;
    mul.rn.f32 %f14, %f14, %f5;                   // (1-b2)*gi*gi
    fma.rn.f32 %f16, %f10, %f3, %f14;             // vi
    st.global.f32 [%rd12], %f15;                  // m[i] = mi
    st.global.f32 [%rd13], %f16;                  // v[i] = vi
    div.rn.f32 %f14, %f15, %f11;                  // mh = mi/bc1
    div.rn.f32 %f16, %f16, %f12;                  // vh = vi/bc2
    sqrt.rn.f32 %f16, %f16;
    add.rn.f32 %f16, %f16, %f13;                  // sqrt(vh)+eps
    div.rn.f32 %f16, %f14, %f16;                  // mh/(...)
    fma.rn.f32 %f16, %f8, %f4, %f16;              //   + wd*w
    neg.f32 %f14, %f7;                            // -lr_t
    fma.rn.f32 %f4, %f14, %f16, %f4;              // w -= lr_t*(...)
    st.global.f32 [%rd14], %f4;
    add.u64 %rd9, %rd9, %rd8;
    bra XC_AW_LOOP;
XC_AW_DONE:
    ret;
}

// ---- gradient squared-sum block partials ------------------------------
.visible .entry xc_sqsum_part(
    .param .u64 %p_x, .param .u64 %p_part, .param .u64 %p_n)
{
    .shared .align 4 .b8 xc_sq_red[1024];         // float[256]
    .reg .pred %p<6>;
    .reg .b32  %r<14>;
    .reg .b64  %rd<12>;
    .reg .f32  %f<4>;
    ld.param.u64 %rd1, [%p_x];
    ld.param.u64 %rd2, [%p_part];
    ld.param.u64 %rd3, [%p_n];
    mov.u32 %r1, %nctaid.x;
    mov.u32 %r2, %ntid.x;
    mov.u32 %r3, %ctaid.x;
    mov.u32 %r4, %tid.x;
    cvt.u64.u32 %rd4, %r1;
    cvt.u64.u32 %rd5, %r2;
    mul.lo.u64 %rd6, %rd4, %rd5;                  // stride
    cvt.u64.u32 %rd4, %r3;
    mul.lo.u64 %rd7, %rd4, %rd5;
    cvt.u64.u32 %rd5, %r4;
    add.u64 %rd7, %rd7, %rd5;                     // i
    mov.f32 %f1, 0f00000000;                      // acc
XC_SQ_LOOP:
    setp.ge.u64 %p1, %rd7, %rd3;
    @%p1 bra XC_SQ_RED;
    shl.b64 %rd8, %rd7, 2;
    add.u64 %rd8, %rd1, %rd8;
    ld.global.f32 %f2, [%rd8];
    fma.rn.f32 %f1, %f2, %f2, %f1;
    add.u64 %rd7, %rd7, %rd6;
    bra XC_SQ_LOOP;
XC_SQ_RED:                                        // f32 warp reduce
    mov.b32 %r5, %f1;
    shfl.sync.down.b32 %r6, %r5, 16, 31, 0xffffffff;
    mov.b32 %f2, %r6;
    add.rn.f32 %f1, %f1, %f2;
    mov.b32 %r5, %f1;
    shfl.sync.down.b32 %r6, %r5, 8, 31, 0xffffffff;
    mov.b32 %f2, %r6;
    add.rn.f32 %f1, %f1, %f2;
    mov.b32 %r5, %f1;
    shfl.sync.down.b32 %r6, %r5, 4, 31, 0xffffffff;
    mov.b32 %f2, %r6;
    add.rn.f32 %f1, %f1, %f2;
    mov.b32 %r5, %f1;
    shfl.sync.down.b32 %r6, %r5, 2, 31, 0xffffffff;
    mov.b32 %f2, %r6;
    add.rn.f32 %f1, %f1, %f2;
    mov.b32 %r5, %f1;
    shfl.sync.down.b32 %r6, %r5, 1, 31, 0xffffffff;
    mov.b32 %f2, %r6;
    add.rn.f32 %f1, %f1, %f2;
    and.b32 %r7, %r4, 31;
    setp.ne.u32 %p2, %r7, 0;
    @%p2 bra XC_SQ_RS;
    shr.u32 %r8, %r4, 5;
    mov.u64 %rd9, xc_sq_red;
    cvt.u64.u32 %rd10, %r8;
    shl.b64 %rd10, %rd10, 2;
    add.u64 %rd9, %rd9, %rd10;
    st.shared.f32 [%rd9], %f1;                    // red[wid]
XC_SQ_RS:
    bar.sync 0;
    setp.ne.u32 %p3, %r4, 0;
    @%p3 bra XC_SQ_END;
    add.u32 %r9, %r2, 31;
    shr.u32 %r9, %r9, 5;                          // warps
    mov.f32 %f3, 0f00000000;
    mov.u32 %r10, 0;
    mov.u64 %rd9, xc_sq_red;
XC_SQ_SUM:
    setp.ge.u32 %p4, %r10, %r9;
    @%p4 bra XC_SQ_OUT;
    cvt.u64.u32 %rd10, %r10;
    shl.b64 %rd10, %rd10, 2;
    add.u64 %rd10, %rd9, %rd10;
    ld.shared.f32 %f2, [%rd10];
    add.rn.f32 %f3, %f3, %f2;
    add.u32 %r10, %r10, 1;
    bra XC_SQ_SUM;
XC_SQ_OUT:
    cvt.u64.u32 %rd10, %r3;                       // ctaid.x
    shl.b64 %rd10, %rd10, 2;
    add.u64 %rd10, %rd2, %rd10;
    st.global.f32 [%rd10], %f3;                   // part[bx]
XC_SQ_END:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
