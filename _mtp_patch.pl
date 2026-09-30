use strict; use warnings;
my $f = 'Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp';
open my $in, '<:raw', $f or die $!;
local $/; my $s = <$in>; close $in;
my $n = 0;
sub norm { my $x = shift; $x =~ s/\r//g; return $x }
$s =~ s/\r\n/\n/g;  # work in LF; restore CRLF on write

# --- Patch 1: dual-family bind (nextn flat + XCN10 stack0) ---
my $o1 = norm(<<'OLD');
            "model.mtp.norm_out.weight"};
        return names;
    }

    // Returns the first missing/empty head tensor name, or nullptr.
    const char* bind_fail() {
        const char* const* names = head_names();
        const xingcheng::inference::TensorView** dst[] = {
            &norm_h, &norm_e, &w_proj, &norm1, &wq, &wk, &wv, &wo,
            &norm2, &w1, &w3, &w2, &norm_out};
        const char* missing = nullptr;
OLD
my $n1 = norm(<<'NEW');
            "model.mtp.norm_out.weight"};
        return names;
    }

    // XCN10 stack naming (mtp_depth >= 1, xc-fused-1 canonical): the
    // depth-0 module is the draft head; role names differ from the
    // legacy nextn flat names only -- same tensors, same math.
    static const char* const* stack_names(int d) {
        static std::string buf[kHeadCount];
        static const char* ptrs[kHeadCount];
        static const char* role[kHeadCount] = {
            "eh", "et", "proj", "norm1", "wq", "wk", "wv", "wo",
            "norm2", "w1", "w3", "w2", "norm_o"};
        for (int i = 0; i < kHeadCount; ++i) {
            buf[i] = "model.mtp." + std::to_string(d) + "." +
                     role[i] + ".weight";
            ptrs[i] = buf[i].c_str();
        }
        return ptrs;
    }

    const char* bound_family = nullptr;  // "nextn" or "stack0"
    const char* const* bound_names_ = nullptr;

    // Returns the first missing/empty head tensor name, or nullptr.
    const char* bind_fail() {
        const xingcheng::inference::TensorView** dst[] = {
            &norm_h, &norm_e, &w_proj, &norm1, &wq, &wk, &wv, &wo,
            &norm2, &w1, &w3, &w2, &norm_out};
        // Prefer the legacy flat nextn set; fall back to the XCN10
        // depth-0 stack module -- never mix families.
        const char* const* names = head_names();
        bool flat_all = true;
        for (int i = 0; i < kHeadCount; ++i)
            if (!b->has_tensor(names[i]) ||
                b->tensor(names[i]).data == nullptr ||
                b->tensor(names[i]).size() <= 0) {
                flat_all = false;
                break;
            }
        if (!flat_all) names = stack_names(0);
        bound_names_ = names;
        bound_family = flat_all ? "nextn" : "stack0";
        const char* missing = nullptr;
NEW
if ($s =~ s/\Q$o1\E/$n1/) { $n++ } else { die "P1 anchor not found\n" }

# --- Patch 2: mismatch reports bound names ---
my $o2 = norm(<<'OLD');
        const int64_t I = c->intermediate_size, V = c->vocab_size;
        if (norm_h->size() != H) return "model.mtp.norm_h.weight";
        if (norm_e->size() != H) return "model.mtp.norm_e.weight";
        if (norm1->size() != H) return "model.mtp.norm1.weight";
        if (norm2->size() != H) return "model.mtp.norm2.weight";
        if (norm_out->size() != H) return "model.mtp.norm_out.weight";
        if (w_proj->size() != H * 2 * H) return "model.mtp.w_proj.weight";
        if (wq->size() != nh * hd * H) return "model.mtp.wq.weight";
        if (wk->size() != kvh * hd * H) return "model.mtp.wk.weight";
        if (wv->size() != kvh * hd * H) return "model.mtp.wv.weight";
        if (wo->size() != H * nh * hd) return "model.mtp.wo.weight";
        if (w1->size() != I * H) return "model.mtp.w1.weight";
        if (w3->size() != I * H) return "model.mtp.w3.weight";
        if (w2->size() != H * I) return "model.mtp.w2.weight";
OLD
my $n2 = norm(<<'NEW');
        const int64_t I = c->intermediate_size, V = c->vocab_size;
        const char* const* N = bound_names_ ? bound_names_
                                            : head_names();
        // N order: 0 norm_h, 1 norm_e, 2 w_proj, 3 norm1, 4 wq, 5 wk,
        // 6 wv, 7 wo, 8 norm2, 9 w1, 10 w3, 11 w2, 12 norm_out.
        if (norm_h->size() != H) return N[0];
        if (norm_e->size() != H) return N[1];
        if (norm1->size() != H) return N[3];
        if (norm2->size() != H) return N[8];
        if (norm_out->size() != H) return N[12];
        if (w_proj->size() != H * 2 * H) return N[2];
        if (wq->size() != nh * hd * H) return N[4];
        if (wk->size() != kvh * hd * H) return N[5];
        if (wv->size() != kvh * hd * H) return N[6];
        if (wo->size() != H * nh * hd) return N[7];
        if (w1->size() != I * H) return N[9];
        if (w3->size() != I * H) return N[10];
        if (w2->size() != H * I) return N[11];
NEW
if ($s =~ s/\Q$o2\E/$n2/) { $n++ } else { die "P2 anchor not found\n" }

# --- Patch 3: report mtp_family in probe output ---
my $o3 = norm(<<'OLD');
        "\"format\":\"star-mtp-draft-probe/v1\","
        "\"draft_length\":1,"
OLD
my $n3 = norm(<<'NEW');
        "\"format\":\"star-mtp-draft-probe/v1\","
        "\"mtp_family\":\"%s\","
        "\"draft_length\":1,"
NEW
if ($s =~ s/\Q$o3\E/$n3/) { $n++ } else { die "P3a anchor not found\n" }

my $o4 = norm(<<'OLD');
        "\"speedup\":null}\n",
        (long long)proposed, (long long)accepted, rate,
OLD
my $n4 = norm(<<'NEW');
        "\"speedup\":null}\n",
        mtp.bound_family ? mtp.bound_family : "none",
        (long long)proposed, (long long)accepted, rate,
NEW
if ($s =~ s/\Q$o4\E/$n4/) { $n++ } else { die "P3b anchor not found\n" }

$s =~ s/\n/\r\n/g;
open my $out, '>:raw', $f or die $!;
print $out $s; close $out;
print "patched $n blocks\n";
