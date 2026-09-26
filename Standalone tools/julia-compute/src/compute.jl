# compute.jl — governed Julia compute endpoint entry (contract julia-compute/v1)
#
# Thin spawn-per-call entry: the implementation lives in the JuliaCompute
# module (src/JuliaCompute.jl) whose @compile_workload makes cold starts
# load cached native code instead of paying first-run codegen.

using JuliaCompute

exit(JuliaCompute.main())
