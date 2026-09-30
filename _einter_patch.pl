use strict; use warnings;
my $f = 'Standalone tools/local-model/src/backend/cpp/src/engine_lifecycle.h';
open my $in, '<:raw', $f or die $!;
local $/; my $s = <$in>; close $in;
my $n = 0;
sub norm { my $x = shift; $x =~ s/\r//g; return $x }
$s =~ s/\r\n/\n/g;

my $o1 = norm(<<'OLD');
            for (int64_t e = 0; e < experts; ++e) {
                const std::string ep =
                    prefix + "mlp.experts." + std::to_string(e) + ".";
OLD
my $n1 = norm(<<'NEW');
            // Effective expert inner widths: forward falls back to
            // intermediate_size when the optional moe_*_intermediate_size
            // manifest fields are absent -- the fused gate_up/down views
            // must be built with the same width or they silently come out
            // empty (null data -> C_ABI_CALL_FAILED:matmul-grouped).
            const int64_t expert_inter =
                cfg.moe_expert_intermediate_size > 0
                    ? cfg.moe_expert_intermediate_size
                    : cfg.intermediate_size;
            const int64_t shared_inter =
                cfg.moe_shared_intermediate_size > 0
                    ? cfg.moe_shared_intermediate_size
                    : expert_inter;
            for (int64_t e = 0; e < experts; ++e) {
                const std::string ep =
                    prefix + "mlp.experts." + std::to_string(e) + ".";
NEW
if ($s =~ s/\Q$o1\E/$n1/) { $n++ } else { die "P1 anchor not found\n" }

my $o2 = norm(<<'OLD');
                layer.expert_gate_up_t.push_back(hcat_weights(
                    {{&gate_t, cfg.moe_expert_intermediate_size},
                     {&up_t, cfg.moe_expert_intermediate_size}},
                    cfg.hidden_size));
OLD
my $n2 = norm(<<'NEW');
                layer.expert_gate_up_t.push_back(hcat_weights(
                    {{&gate_t, expert_inter},
                     {&up_t, expert_inter}},
                    cfg.hidden_size));
NEW
if ($s =~ s/\Q$o2\E/$n2/) { $n++ } else { die "P2 anchor not found\n" }

my $o3 = norm(<<'OLD');
                layer.shared_gate_up_t.push_back(hcat_weights(
                    {{&gate_t, cfg.moe_shared_intermediate_size},
                     {&up_t, cfg.moe_shared_intermediate_size}},
                    cfg.hidden_size));
OLD
my $n3 = norm(<<'NEW');
                layer.shared_gate_up_t.push_back(hcat_weights(
                    {{&gate_t, shared_inter},
                     {&up_t, shared_inter}},
                    cfg.hidden_size));
NEW
if ($s =~ s/\Q$o3\E/$n3/) { $n++ } else { die "P3 anchor not found\n" }

$s =~ s/\n/\r\n/g;
open my $out, '>:raw', $f or die $!;
print $out $s; close $out;
print "patched $n blocks\n";
