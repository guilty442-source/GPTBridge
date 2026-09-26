# Provision the pinned tool environment (contract julia-compute/v1).
#
# Perf/no-recompile + no-drift: instantiate() resolves strictly from the
# committed Manifest.toml (no registry resolve, no version drift) and
# precompile() warms the depot pkgimage cache, so every spawn-per-call
# hits cache instead of JIT-compiling JSON per process.  Never Pkg.add
# here — add() would re-resolve and mutate the pin (compat drift) and
# invalidate the very cache this script warms.
using Pkg
Pkg.instantiate()
Pkg.precompile()
println("PROVISION_OK")
