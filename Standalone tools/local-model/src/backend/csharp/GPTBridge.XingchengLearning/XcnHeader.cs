// XcnHeader.cs — single shared reader for the XCN1..XCN10 checkpoint
// header into a trainer ``model`` config dict.
//
// Previously parsed in three places (JobExecutor.XcnConfig,
// SelfLearning.ReadXcnHeaderConfig, SelfLearning.WeightsConfigFingerprint
// via the former): field order must match ``xct_util.parse_model`` /
// ``xct_ckpt.h`` write order, so every drift triples the risk. This is
// now the sole owner; callers only adapt success/failure semantics
// (throw vs null).
//
// LANGUAGE-ARCHITECTURE TRANSITION (2026-10-02, human-governor directive):
// binary format validation ownership migrates to the Rust xc-format crate
// (src/backend/rust/xc-format). This reader stays as the thin governed
// entry until the Rust lane is wired; no new format logic lands here.
// B166 remains the sole language authority.

namespace GPTBridge.XingchengLearning;

internal static class XcnHeader
{
    /// <summary>Read the XCN1..XCN10 header into a trainer ``model``
    /// config dict (XCN2 MoE widths, XCN3 hybrid-attention geometry,
    /// XCN4 vision early-fusion block, XCN5 Gemma A4B axis, XCN6
    /// fused-router flag, XCN7 DeepSeek V4-Pro axis, XCN8 YaRN block,
    /// XCN9 Gemma4 marker block, XCN10 MTP-stack — field names match
    /// ``xct_util.parse_model``). Throws <see cref="ExecutorError"/>
    /// with the same codes the executor has always emitted.</summary>
    public static Dictionary<string, object?> ReadConfig(string ckptPath)
    {
        using var f = new FileStream(ckptPath, FileMode.Open, FileAccess.Read);
        using var r = new BinaryReader(f);
        byte[] magic = r.ReadBytes(4);
        if (magic.Length != 4 ||
            magic[0] != 'X' || magic[1] != 'C' || magic[2] != 'N' || magic[3] != '1')
            throw new ExecutorError("EXECUTOR_CKPT_BAD_MAGIC", ckptPath);
        uint ver = r.ReadUInt32();
        if (ver < 1 || ver > 10)
            throw new ExecutorError("EXECUTOR_CKPT_VERSION", $"v{ver}");
        uint vocab = r.ReadUInt32();
        uint hidden = r.ReadUInt32();
        uint inter = r.ReadUInt32();
        uint layers = r.ReadUInt32();
        uint heads = r.ReadUInt32();
        uint kvHeads = r.ReadUInt32();
        uint maxPos = r.ReadUInt32();
        uint moeExperts = r.ReadUInt32();
        uint moeTopK = r.ReadUInt32();
        uint moeInterval = r.ReadUInt32();
        double ropeTheta = r.ReadSingle();
        double rmsEps = r.ReadSingle();
        double moeAux = r.ReadSingle();
        var cfg = new Dictionary<string, object?>
        {
            ["vocab_size"] = (long)vocab,
            ["hidden_size"] = (long)hidden,
            ["intermediate_size"] = (long)inter,
            ["num_hidden_layers"] = (long)layers,
            ["num_attention_heads"] = (long)heads,
            ["num_key_value_heads"] = (long)kvHeads,
            ["max_position_embeddings"] = (long)maxPos,
            ["moe_num_experts"] = (long)moeExperts,
            ["moe_top_k"] = (long)moeTopK,
            ["moe_layer_interval"] = (long)moeInterval,
            ["rope_theta"] = (double)(float)ropeTheta,
            ["rms_norm_eps"] = (double)(float)rmsEps,
            ["moe_aux_loss_weight"] = (double)(float)moeAux,
        };
        if (ver >= 2)
        {
            cfg["moe_expert_intermediate_size"] = (long)r.ReadUInt32();
            cfg["moe_num_shared_experts"] = (long)r.ReadUInt32();
            cfg["moe_shared_intermediate_size"] = (long)r.ReadUInt32();
        }
        if (ver >= 3)
        {
            cfg["full_attention_interval"] = (long)r.ReadUInt32();
            uint flags = r.ReadUInt32();
            cfg["attn_output_gate"] = (flags & 1u) != 0;
            cfg["qk_norm"] = (flags & 2u) != 0;
            cfg["shared_expert_gate"] = (flags & 4u) != 0;
            cfg["partial_rotary_factor"] = (double)r.ReadSingle();
            cfg["linear_num_key_heads"] = (long)r.ReadUInt32();
            cfg["linear_key_head_dim"] = (long)r.ReadUInt32();
            cfg["linear_num_value_heads"] = (long)r.ReadUInt32();
            cfg["linear_value_head_dim"] = (long)r.ReadUInt32();
            cfg["linear_conv_kernel_dim"] = (long)r.ReadUInt32();
        }
        if (ver >= 4)
        {
            cfg["use_vision"] = r.ReadUInt32() != 0u;
            cfg["vision_patch_dim"] = (long)r.ReadUInt32();
            cfg["vision_max_patches"] = (long)r.ReadUInt32();
        }
        if (ver >= 5)
        {
            // XCN5 Gemma A4B block (see xct_ckpt.h write order):
            // global_attention_interval, sliding_window, num_global_kv_heads,
            // flag bits, rope proportions/base frequencies, softcap.
            cfg["global_attention_interval"] = (long)r.ReadUInt32();
            cfg["sliding_window_size"] = (long)r.ReadUInt32();
            cfg["num_global_kv_heads"] = (long)r.ReadUInt32();
            uint gflags = r.ReadUInt32();
            cfg["k_eq_v_global"] = (gflags & 1u) != 0;
            cfg["use_post_attn_norm"] = (gflags & 2u) != 0;
            cfg["use_post_ffw_norm"] = (gflags & 4u) != 0;
            if ((gflags & 8u) != 0) cfg["ffn_activation"] = "gelu_tanh";
            cfg["local_rope_proportion"] = (double)r.ReadSingle();
            cfg["global_rope_proportion"] = (double)r.ReadSingle();
            cfg["local_base_frequency"] = (double)r.ReadSingle();
            cfg["global_base_frequency"] = (double)r.ReadSingle();
            cfg["final_logit_softcap"] = (double)r.ReadSingle();
        }
        if (ver >= 6)
        {
            // XCN6 fused router: Qwen3-A3B softmax | Qwen3.5 sigmoid
            // scoring flag (see xct_ckpt.h).
            cfg["moe_router_sigmoid"] = r.ReadUInt32() != 0u;
        }
        if (ver >= 7)
        {
            // XCN7 DeepSeek V4-Pro block (see xct_ckpt.h write order):
            // MLA dims, aux-free balance flag + bias rate, MTP depth +
            // loss weight.
            cfg["kv_lora_rank"] = (long)r.ReadUInt32();
            cfg["q_lora_rank"] = (long)r.ReadUInt32();
            cfg["qk_nope_head_dim"] = (long)r.ReadUInt32();
            cfg["qk_rope_head_dim"] = (long)r.ReadUInt32();
            cfg["moe_auxfree_balance"] = r.ReadUInt32() != 0u;
            cfg["moe_lb_bias_rate"] = (double)r.ReadSingle();
            cfg["num_nextn_predict_layers"] = (long)r.ReadUInt32();
            cfg["mtp_loss_weight"] = (double)r.ReadSingle();
        }
        if (ver >= 8)
        {
            // XCN8 Qwen3-Coder YaRN block (see xct_ckpt.h write order):
            // extension factor, original context length, beta band
            // bounds, attention factor (mscale).
            cfg["yarn_factor"] = (double)r.ReadSingle();
            cfg["yarn_original_max_position_embeddings"] =
                (long)r.ReadUInt32();
            cfg["yarn_beta_fast"] = (double)r.ReadSingle();
            cfg["yarn_beta_slow"] = (double)r.ReadSingle();
            cfg["yarn_attention_factor"] = (double)r.ReadSingle();
        }
        if (ver >= 9)
        {
            // XCN9 Gemma4 block (see xct_ckpt.h write order): the marker
            // u32 is always present at ver >= 9 — 1 = g4 fields follow,
            // 0 = non-gemma4 (canonical fused generation checkpoints
            // land here).
            uint g4m = r.ReadUInt32();
            if (g4m == 1u)
            {
                cfg["model_type"] = "gemma4_text";
                cfg["head_dim"] = (long)r.ReadUInt32();
                cfg["global_head_dim"] = (long)r.ReadUInt32();
                cfg["sliding_window"] = (long)r.ReadUInt32();
                cfg["num_kv_shared_layers"] = (long)r.ReadUInt32();
                cfg["hidden_size_per_layer_input"] = (long)r.ReadUInt32();
                cfg["vocab_size_per_layer_input"] = (long)r.ReadUInt32();
                uint g4flags = r.ReadUInt32();
                cfg["use_double_wide_mlp"] = (g4flags & 1u) != 0;
                cfg["tie_word_embeddings"] = (g4flags & 2u) != 0;
                cfg["rope_theta_full"] = (double)r.ReadSingle();
                cfg["rope_partial_rotary_factor"] = (double)r.ReadSingle();
                cfg["final_logit_softcapping"] = (double)r.ReadSingle();
                cfg["attention_scale"] = (double)r.ReadSingle();
                uint nt = r.ReadUInt32();
                var types = new List<object?>();
                for (uint i = 0; i < nt; ++i)
                {
                    uint nl = r.ReadUInt32();
                    types.Add(System.Text.Encoding.UTF8.GetString(
                        r.ReadBytes((int)nl)));
                }
                cfg["layer_types"] = types;
                uint al = r.ReadUInt32();
                cfg["hidden_activation"] =
                    System.Text.Encoding.UTF8.GetString(
                        r.ReadBytes((int)al));
            }
            else if (g4m != 0u)
            {
                throw new ExecutorError("EXECUTOR_CKPT_VERSION",
                    "bad gemma4 marker");
            }
        }
        if (ver >= 10)
        {
            // XCN10 v29 MTP-stack block (see xct_ckpt.h write order):
            // mtp_stack_depth u32 + mtp_stack_loss_weight float.
            cfg["mtp_stack_depth"] = (long)r.ReadUInt32();
            cfg["mtp_stack_loss_weight"] = (double)r.ReadSingle();
        }
        return cfg;
    }

    /// <summary>Fail-closed probe variant: null when the header is
    /// unreadable or out of range (rollback eligibility is
    /// deny-by-default).</summary>
    public static Dictionary<string, object?>? TryReadConfig(string path)
    {
        try
        {
            return ReadConfig(path);
        }
        catch (Exception ex) when (ex is ExecutorError or IOException
            or UnauthorizedAccessException or ArgumentException)
        {
            return null;
        }
    }
}
