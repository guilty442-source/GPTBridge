# native_transformer model specs

Canonical architecture specifications consumed by the native engine
(`cpp/src/engine_gemma4.h`), the native trainer
(`training/xct_gemma4.h`), and `tools/xc_modeltool` name translation.

## gemma4-e4b.json

Replicates the Gemma 4 E4B **text** stack (HF `Gemma4TextConfig`,
`config.json` at `huggingface.co/google/gemma-4-E4B-it`):

- 42 hybrid layers: `sliding_attention` (window 512, RoPE theta 1e4,
  head_dim 256) and `full_attention` at layers 5/11/17/23/29/35/41
  (RoPE theta 1e6, `partial_rotary_factor` 0.25 → 256 rotary dims,
  head_dim 512).
- Tail KV sharing (`num_kv_shared_layers` 18): layers 24–41 compute
  Q only and attend over the last same-type non-shared layer's K/V —
  `sliding_attention` anchor = layer 22, `full_attention` anchor =
  layer 23. Gradients route to the owning anchor tensors.
- Per-layer embeddings (`hidden_size_per_layer_input` 256):
  `ple_in = (ple_norm(H^-1/2 * ple_model_proj(x0)) + sqrt(ple) *
  embed_tokens_per_layer[tok]) * 2^-1/2`, then per layer
  `x += ple_norm(ple_proj(gelu_tanh(ple_gate(x)) * ple_in_l))`.
- Four norms per block: `input_norm`, `post_attention_norm`,
  `pre_feedforward_norm`, `post_feedforward_norm`; plus `attention.
  q_norm`/`attention.k_norm` (QK-RMSNorm) and a scale-free V norm.
- `gelu_pytorch_tanh` GLU MLP, `final_logit_softcapping` 30.0 applied
  as `cap*tanh(logits/cap)`, tied word embeddings, embedding scale
  `sqrt(hidden_size)`, attention scale 1.0.
- Dense only: `enable_moe_block` / `use_moe` fail closed on this lane.

### Bundle tensor contract

Model-level:
`model.embeddings.word_embeddings.weight`,
`model.embed_tokens_per_layer.weight`,
`model.per_layer_model_projection.weight`,
`model.per_layer_projection_norm.weight`,
`model.final_norm.weight`
(`lm_head.weight` only when `tie_word_embeddings` is false).

Per layer (`model.layers.N.`):
`input_norm.weight`, `attention.{q,k,v,o}_proj.weight`,
`attention.{q,k}_norm.weight`, `post_attention_norm.weight`,
`pre_feedforward_norm.weight`, `post_feedforward_norm.weight`,
`mlp.{gate,up,down}_proj.weight`, and when PLE is enabled
`per_layer_input_gate.weight`, `per_layer_projection.weight`,
`post_per_layer_input_norm.weight`. KV-shared tail layers carry no
`attention.{k,v}_proj.weight` / `attention.k_norm.weight`.

### Trainer (xct) names

`embed`, `embed_ple`, `ple_model_proj`, `ple_proj_norm`, `norm_f`,
`layers.N.{norm1, wq, wk, wv, wo, q_norm, k_norm, norm_attn, norm2,
w1, w3, w2, norm_ffn, ple_gate, ple_proj, ple_post}` —
round-trips through `xc_modeltool` `import-bundle`/`export-bundle`
and the XCN3 checkpoint header (ver=3, `XCN1` magic).

## native-thinking.json

Latent-space reasoning (Native Thinking) and reinforcement learning —
`star-native-thinking/v1`. Hypotheses are formed and verified inside
hidden state space; no textual chain-of-thought is produced.

- **Inference** (`engine_thinking.h`, serve `op: "think"`):
  prompt prefill → `think_steps` continuous-thought iterations that feed
  the last hidden row back as the next input embedding
  (`BatchSpan::embed_override`, legacy + gemma4 paths) → `branches`
  parallel hypothesis decodes on private KV slots (`kv_copy_slot`) →
  CoVe self-verification by mean token logprob → winning branch is the
  answer. Bounds fail closed: `think_steps<=32`, `branches<=8`,
  `max_new_tokens<=2048`, sequence fits `max_position_embeddings`.
  Response adds a `thinking` block (`chosen_branch`, `branch_scores`,
  `branch_lengths`, `verify:"confidence"`).
- **Training** (`task: "grpo"`): G=group_size on-policy rollouts per
  prompt, verifiable reward (`exact` | `prefix`), group-normalized
  advantage, policy gradient with `kl_coef` KL-to-reference
  regularization over completion positions only. Data:
  `{"prompt_ids": [...], "completion_ids": [...]}` jsonl. Report adds
  `rollouts`, `reward_mean`, `kl_mean`; smoke leg `smoke-grpo`.
