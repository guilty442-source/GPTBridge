// cuda_ptx_gemm.h — embedded PTX tiled GEMM kernels (B132):
//   xc_gemm_f64   row-major C=A·B in fp64 — the self-authored
//                 replacement for the retired cuBLAS Dgemm; 16×16
//                 shared tiles, f64 accumulate, fma order identical to
//                 a row-major mul+add within fp64 parity tolerance.
//   xc_gemm_bf16  bf16 operands → fp32 shared tiles (each element
//                 converted once at load), fp32 FMA accumulate —
//                 semantics identical to the retired NVRTC source.

#pragma once

namespace xcuda_ptx {

inline const char* gemm() {
    return R"PTX(
// ---- fp64 tiled GEMM: c[m,n] = a[m,k] · b[k,n] ----------------------
.visible .entry xc_gemm_f64(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u64 %p_m, .param .u64 %p_k, .param .u64 %p_n)
{
    .shared .align 8 .b8 xc_g64_as[2048];
    .shared .align 8 .b8 xc_g64_bs[2048];
    .reg .pred %p<8>;
    .reg .b32  %r<10>;
    .reg .b64  %rd<24>;
    .reg .f64  %fd<6>;
    ld.param.u64 %rd1, [%p_a];
    ld.param.u64 %rd2, [%p_b];
    ld.param.u64 %rd3, [%p_c];
    ld.param.u64 %rd4, [%p_m];
    ld.param.u64 %rd5, [%p_k];
    ld.param.u64 %rd6, [%p_n];
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
    mov.f64 %fd1, 0d0000000000000000;             // acc
    add.u64 %rd9, %rd5, 15;
    shr.u64 %rd9, %rd9, 4;                        // nt
    mov.u64 %rd10, 0;                             // t
XC_G64_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_G64_OUT;
    shl.b64 %rd11, %rd10, 4;                      // t*16
    cvt.u64.u32 %rd12, %r4;
    add.u64 %rd12, %rd11, %rd12;                  // a_col
    cvt.u64.u32 %rd13, %r3;
    add.u64 %rd13, %rd11, %rd13;                  // b_row
    mov.f64 %fd2, 0d0000000000000000;
    setp.lt.u64 %p2, %rd7, %rd4;
    setp.lt.u64 %p3, %rd12, %rd5;
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_G64_SA;
    mul.lo.u64 %rd14, %rd7, %rd5;
    add.u64 %rd14, %rd14, %rd12;
    shl.b64 %rd14, %rd14, 3;
    add.u64 %rd14, %rd1, %rd14;
    ld.global.f64 %fd2, [%rd14];
XC_G64_SA:
    mov.u64 %rd15, xc_g64_as;
    shl.b32 %r7, %r3, 4;
    add.s32 %r7, %r7, %r4;                        // ty*16+tx
    cvt.u64.u32 %rd16, %r7;
    shl.b64 %rd16, %rd16, 3;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f64 [%rd15], %fd2;
    mov.f64 %fd3, 0d0000000000000000;
    setp.lt.u64 %p5, %rd13, %rd5;
    setp.lt.u64 %p6, %rd8, %rd6;
    and.pred %p7, %p5, %p6;
    @!%p7 bra XC_G64_SB;
    mul.lo.u64 %rd14, %rd13, %rd6;
    add.u64 %rd14, %rd14, %rd8;
    shl.b64 %rd14, %rd14, 3;
    add.u64 %rd14, %rd2, %rd14;
    ld.global.f64 %fd3, [%rd14];
XC_G64_SB:
    mov.u64 %rd15, xc_g64_bs;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f64 [%rd15], %fd3;
    bar.sync 0;
    mov.u64 %rd17, xc_g64_as;
    cvt.u64.u32 %rd18, %r3;
    shl.b64 %rd18, %rd18, 7;                      // ty*128
    add.u64 %rd17, %rd17, %rd18;
    mov.u64 %rd19, xc_g64_bs;
    cvt.u64.u32 %rd18, %r4;
    shl.b64 %rd18, %rd18, 3;
    add.u64 %rd19, %rd19, %rd18;
    ld.shared.f64 %fd4, [%rd17+0];   ld.shared.f64 %fd5, [%rd19+0];    fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+8];   ld.shared.f64 %fd5, [%rd19+128];  fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+16];  ld.shared.f64 %fd5, [%rd19+256];  fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+24];  ld.shared.f64 %fd5, [%rd19+384];  fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+32];  ld.shared.f64 %fd5, [%rd19+512];  fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+40];  ld.shared.f64 %fd5, [%rd19+640];  fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+48];  ld.shared.f64 %fd5, [%rd19+768];  fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+56];  ld.shared.f64 %fd5, [%rd19+896];  fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+64];  ld.shared.f64 %fd5, [%rd19+1024]; fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+72];  ld.shared.f64 %fd5, [%rd19+1152]; fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+80];  ld.shared.f64 %fd5, [%rd19+1280]; fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+88];  ld.shared.f64 %fd5, [%rd19+1408]; fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+96];  ld.shared.f64 %fd5, [%rd19+1536]; fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+104]; ld.shared.f64 %fd5, [%rd19+1664]; fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+112]; ld.shared.f64 %fd5, [%rd19+1792]; fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    ld.shared.f64 %fd4, [%rd17+120]; ld.shared.f64 %fd5, [%rd19+1920]; fma.rn.f64 %fd1, %fd4, %fd5, %fd1;
    bar.sync 0;
    add.u64 %rd10, %rd10, 1;
    bra XC_G64_TILE;
XC_G64_OUT:
    setp.lt.u64 %p1, %rd7, %rd4;
    setp.lt.u64 %p2, %rd8, %rd6;
    and.pred %p3, %p1, %p2;
    @!%p3 bra XC_G64_RET;
    mul.lo.u64 %rd14, %rd7, %rd6;
    add.u64 %rd14, %rd14, %rd8;
    shl.b64 %rd14, %rd14, 3;
    add.u64 %rd14, %rd3, %rd14;
    st.global.f64 [%rd14], %fd1;
XC_G64_RET:
    ret;
}

// ---- bf16 tiled GEMM: fp32 shared tiles, fp32 accumulate ------------
.visible .entry xc_gemm_bf16(
    .param .u64 %p_a, .param .u64 %p_b, .param .u64 %p_c,
    .param .u32 %p_m, .param .u32 %p_k, .param .u32 %p_n)
{
    .shared .align 4 .b8 xc_g16_as[1024];
    .shared .align 4 .b8 xc_g16_bs[1024];
    .reg .pred %p<8>;
    .reg .b16  %rs<3>;
    .reg .b32  %r<12>;
    .reg .b64  %rd<24>;
    .reg .f32  %f<10>;
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
XC_G16_TILE:
    setp.ge.u64 %p1, %rd10, %rd9;
    @%p1 bra XC_G16_OUT;
    shl.b64 %rd11, %rd10, 4;                      // t*16
    cvt.u64.u32 %rd12, %r7;
    add.u64 %rd12, %rd11, %rd12;                  // a_col
    cvt.u64.u32 %rd13, %r6;
    add.u64 %rd13, %rd11, %rd13;                  // b_row
    mov.f32 %f2, 0f00000000;
    setp.lt.s32 %p2, %r8, %r1;                    // row < m
    setp.lt.u64 %p3, %rd12, %rd4;                 // a_col < k
    and.pred %p4, %p2, %p3;
    @!%p4 bra XC_G16_SA;
    mul.lo.u64 %rd14, %rd7, %rd4;
    add.u64 %rd14, %rd14, %rd12;
    add.u64 %rd14, %rd14, %rd14;
    add.u64 %rd14, %rd1, %rd14;
    ld.global.u16 %rs1, [%rd14];
    cvt.u32.u16 %r10, %rs1;
    shl.b32 %r10, %r10, 16;
    mov.b32 %f2, %r10;
XC_G16_SA:
    mov.u64 %rd15, xc_g16_as;
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
    @!%p7 bra XC_G16_SB;
    mul.lo.u64 %rd14, %rd13, %rd5;
    add.u64 %rd14, %rd14, %rd8;
    add.u64 %rd14, %rd14, %rd14;
    add.u64 %rd14, %rd2, %rd14;
    ld.global.u16 %rs2, [%rd14];
    cvt.u32.u16 %r10, %rs2;
    shl.b32 %r10, %r10, 16;
    mov.b32 %f3, %r10;
XC_G16_SB:
    mov.u64 %rd15, xc_g16_bs;
    add.u64 %rd15, %rd15, %rd16;
    st.shared.f32 [%rd15], %f3;
    bar.sync 0;
    mov.u64 %rd17, xc_g16_as;
    cvt.u64.u32 %rd18, %r6;
    shl.b64 %rd18, %rd18, 6;                      // ty*64
    add.u64 %rd17, %rd17, %rd18;
    mov.u64 %rd19, xc_g16_bs;
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
    bra XC_G16_TILE;
XC_G16_OUT:
    setp.lt.s32 %p1, %r8, %r1;                    // row < m
    setp.lt.u64 %p2, %rd8, %rd5;                  // col < n
    and.pred %p3, %p1, %p2;
    @!%p3 bra XC_G16_RET;
    mul.lo.u64 %rd14, %rd7, %rd5;
    add.u64 %rd14, %rd14, %rd8;
    shl.b64 %rd14, %rd14, 2;
    add.u64 %rd14, %rd3, %rd14;
    st.global.f32 [%rd14], %f1;
XC_G16_RET:
    ret;
}
)PTX";
}

}  // namespace xcuda_ptx
