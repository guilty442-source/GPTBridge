# JuliaCompute.jl — governed Julia compute endpoint (contract julia-compute/v1)
#
# A610 PYTHON-WORK-TRANSFER: Julia is the single formal execution owner for
# statistical/scientific compute and optimization/simulation work
# (按需分析 — on-demand, non-resident).  It never replaces JAX for model
# training and never parses untrusted data: the job document is produced
# by the governed Python wrapper from already-authorized request data.
#
# Protocol: one JSON job object on stdin (or --job <path>), one JSON result
# object on stdout.  Any contract violation exits nonzero with ok=false.
#
# Latency: this module is precompiled with a @compile_workload exercising
# every op, so the spawn-per-call entry (src/compute.jl) only pays for
# cached-code loading, never first-run codegen.

module JuliaCompute

using JSON
using Statistics
using LinearAlgebra
using Random
using PrecompileTools

const CONTRACT = "julia-compute/v1"
const SCHEMA_VERSION = 1

function reply(ok::Bool; op::AbstractString="", result=nothing, err=nothing)
    out = Dict{String,Any}("ok" => ok, "contract" => CONTRACT)
    isempty(op) || (out["op"] = op)
    result === nothing || (out["result"] = result)
    err === nothing || (out["error"] = err)
    println(stdout, JSON.json(out))
    return ok ? 0 : 2
end

fail(op::AbstractString, err::AbstractString) = reply(false; op=op, err=err)

# JSON.parse already yields Vector{Any}/Dict{String,Any}; convert in place
# instead of re-wrapping through Vector{Any}(x) which copies the payload.
as_vector(x::AbstractVector)::Vector{Float64} = Float64[v for v in x]
as_vector(x)::Vector{Float64} = Float64[v for v in Vector{Any}(x)]

function as_matrix(x)::Matrix{Float64}
    raw = x isa AbstractVector ? x : Vector{Any}(x)
    nrow = length(raw)
    nrow == 0 && return Matrix{Float64}(undef, 0, 0)
    rows = [Float64[v for v in r] for r in raw]
    ncol = length(rows[1])
    all(r -> length(r) == ncol, rows) || throw(ArgumentError("RAGGED_MATRIX"))
    # Fill directly instead of reduce(vcat, r') — avoids n intermediate
    # adjoint wrappers and one big cat allocation.
    out = Matrix{Float64}(undef, nrow, ncol)
    @inbounds for i in 1:nrow
        row_i = rows[i]
        for j in 1:ncol
            out[i, j] = row_i[j]
        end
    end
    return out
end

# -- stats ---------------------------------------------------------------

function op_stats_describe(params)
    v = as_vector(get(params, "values", Any[]))
    isempty(v) && return ("STATS_VALUES_EMPTY", nothing)
    # One sort for all quantiles; mean computed once and reused by var/std.
    m = mean(v)
    qs = quantile(v, [0.25, 0.50, 0.75, 0.95, 0.99])
    lo, hi = extrema(v)
    vvar = var(v; mean=m)
    return (nothing, Dict{String,Any}(
        "n" => length(v),
        "mean" => m,
        "std" => sqrt(vvar),
        "var" => vvar,
        "min" => lo,
        "p25" => qs[1],
        "p50" => qs[2],
        "p75" => qs[3],
        "p95" => qs[4],
        "p99" => qs[5],
        "max" => hi,
    ))
end

function op_stats_quantiles(params)
    v = as_vector(get(params, "values", Any[]))
    isempty(v) && return ("STATS_VALUES_EMPTY", nothing)
    qs = as_vector(get(params, "qs", Any[]))
    all(q -> 0.0 <= q <= 1.0, qs) || return ("STATS_Q_OUT_OF_RANGE", nothing)
    # Single sort across all requested quantiles.
    values = isempty(qs) ? Float64[] : quantile(v, qs)
    return (nothing, Dict{String,Any}(
        "quantiles" => [Dict("q" => q, "value" => values[i])
                        for (i, q) in enumerate(qs)],
    ))
end

function op_stats_correlation(params)
    x = as_vector(get(params, "x", Any[]))
    y = as_vector(get(params, "y", Any[]))
    (length(x) == length(y) && length(x) >= 2) || return ("STATS_LENGTH_MISMATCH", nothing)
    return (nothing, Dict{String,Any}(
        "pearson" => cor(x, y),
        "covariance" => cov(x, y),
    ))
end

# -- linalg ----------------------------------------------------------------

function op_linalg_lstsq(params)
    raw = get(params, "a", Any[])
    isempty(raw) && return ("LINALG_A_EMPTY", nothing)
    a = as_matrix(raw)
    b = as_vector(get(params, "b", Any[]))
    size(a, 1) == length(b) || return ("LINALG_SHAPE_MISMATCH", nothing)
    x = a \ b
    resid = a * x
    resid .-= b
    return (nothing, Dict{String,Any}(
        "x" => x,
        "residual_norm" => norm(resid),
        "rank" => rank(a),
        "rcond" => 1.0 / cond(a),
    ))
end

# -- optimize --------------------------------------------------------------
#
# Objectives avoid broadcast temporaries: they run inside nelder_mead's
# per-iteration loop, so every slice/map allocation is paid once per call.

function _rosenbrock(x::Vector{Float64})::Float64
    n = length(x)
    s = 0.0
    @inbounds for i in 1:(n - 1)
        t = x[i + 1] - x[i] * x[i]
        u = 1.0 - x[i]
        s += 100.0 * t * t + u * u
    end
    return s
end

function _objective(name::AbstractString, params)
    if name == "sphere"
        return x -> sum(abs2, x)
    elseif name == "rosenbrock"
        return _rosenbrock
    elseif name == "quadratic"
        qa = get(params, "a", Any[])
        isempty(qa) && return nothing
        a = as_matrix(qa)
        b = as_vector(get(params, "b", fill(0.0, size(a, 1))))
        c = Float64(get(params, "c", 0.0))
        # f(x) = 0.5 x'Ax - b'x + c without LinearAlgebra temporaries
        return x -> begin
            n = length(x)
            quad = 0.0
            @inbounds for j in 1:n
                acc = 0.0
                for i in 1:n
                    acc += x[i] * a[i, j]
                end
                quad += acc * x[j]
            end
            lin = 0.0
            @inbounds for i in 1:n
                lin += b[i] * x[i]
            end
            0.5 * quad - lin + c
        end
    end
    return nothing
end

function nelder_mead(f, x0::Vector{Float64}; max_iter::Int=400, tol::Float64=1e-8)
    n = length(x0)
    simplex = Vector{Vector{Float64}}(undef, n + 1)
    simplex[1] = copy(x0)
    for i in 1:n
        p = copy(x0)
        p[i] += p[i] == 0.0 ? 0.00025 : 0.05 * abs(p[i])
        simplex[i + 1] = p
    end
    vals = Vector{Float64}(undef, n + 1)
    for i in 1:(n + 1)
        vals[i] = f(simplex[i])
    end
    # Preallocated iteration state — nothing inside the loop allocates
    # except the accepted simplex point itself.
    order = Vector{Int}(undef, n + 1)
    centroid = Vector{Float64}(undef, n)
    xr = Vector{Float64}(undef, n)
    xc = Vector{Float64}(undef, n)
    sqrt_tol = sqrt(tol)
    for _ in 1:max_iter
        sortperm!(order, vals)
        permute!(simplex, order)
        permute!(vals, order)
        v1 = vals[1]
        span = 0.0
        @inbounds for k in 2:(n + 1)
            d = abs(vals[k] - v1)
            d > span && (span = d)
        end
        # simplex diameter without per-point temporaries
        diam2 = 0.0
        @inbounds for i in 2:(n + 1)
            si = simplex[i]; s1 = simplex[1]
            acc = 0.0
            for j in 1:n
                d = si[j] - s1[j]
                acc += d * d
            end
            acc > diam2 && (diam2 = acc)
        end
        (span < tol && diam2 < tol) && break
        fill!(centroid, 0.0)
        @inbounds for i in 1:n
            si = simplex[i]
            for j in 1:n
                centroid[j] += si[j]
            end
        end
        inv_n = 1.0 / n
        @inbounds for j in 1:n
            centroid[j] *= inv_n
        end
        worst = simplex[n + 1]
        @inbounds for j in 1:n
            xr[j] = 2.0 * centroid[j] - worst[j]
        end
        fr = f(xr)
        if vals[1] <= fr < vals[n]
            simplex[n + 1] = xr; vals[n + 1] = fr
            xr = Vector{Float64}(undef, n)
        elseif fr < vals[1]
            # expansion: xe = 2*xr - centroid, evaluated into xc
            @inbounds for j in 1:n
                xc[j] = 2.0 * xr[j] - centroid[j]
            end
            fe = f(xc)
            if fe < fr
                simplex[n + 1] = xc; vals[n + 1] = fe
                xc = Vector{Float64}(undef, n)
            else
                simplex[n + 1] = xr; vals[n + 1] = fr
                xr = Vector{Float64}(undef, n)
            end
        else
            # contraction: inside or outside
            outside = fr < vals[n + 1]
            @inbounds for j in 1:n
                base = outside ? xr[j] : worst[j]
                xc[j] = centroid[j] + 0.5 * (base - centroid[j])
            end
            fc = f(xc)
            if fc < min(fr, vals[n + 1])
                simplex[n + 1] = xc; vals[n + 1] = fc
                xc = Vector{Float64}(undef, n)
            else
                # shrink toward the best point
                s1 = simplex[1]
                @inbounds for i in 2:(n + 1)
                    si = simplex[i]
                    for j in 1:n
                        si[j] = s1[j] + 0.5 * (si[j] - s1[j])
                    end
                    vals[i] = f(si)
                end
            end
        end
    end
    sortperm!(order, vals)
    return simplex[order[1]], vals[order[1]]
end

function op_optimize_nelder_mead(params)
    x0 = as_vector(get(params, "x0", Any[]))
    isempty(x0) && return ("OPT_X0_EMPTY", nothing)
    name = String(get(params, "objective", "sphere"))
    f = _objective(name, params)
    f === nothing && return ("OPT_UNKNOWN_OBJECTIVE:$name", nothing)
    max_iter = Int(get(params, "max_iter", 400))
    tol = Float64(get(params, "tol", 1e-8))
    (1 <= max_iter <= 100_000) || return ("OPT_MAX_ITER_OUT_OF_RANGE", nothing)
    x, fx = nelder_mead(f, x0; max_iter=max_iter, tol=tol)
    return (nothing, Dict{String,Any}(
        "x" => x, "f" => fx, "objective" => name,
        "converged" => fx < 1e-6,
    ))
end

# -- simulate ---------------------------------------------------------------

function op_simulate_monte_carlo(params)
    trials = Int(get(params, "trials", 10_000))
    (1 <= trials <= 10_000_000) || return ("SIM_TRIALS_OUT_OF_RANGE", nothing)
    seed = Int(get(params, "seed", 42))
    rng = MersenneTwister(seed)
    model = String(get(params, "model", "normal"))
    samples = Vector{Float64}(undef, trials)
    randn!(rng, samples)
    if model == "normal"
        mu = Float64(get(params, "mu", 0.0))
        sigma = Float64(get(params, "sigma", 1.0))
        sigma > 0 || return ("SIM_SIGMA_NONPOSITIVE", nothing)
        @inbounds for i in 1:trials
            samples[i] = mu + sigma * samples[i]
        end
    elseif model == "gbm"
        s0 = Float64(get(params, "s0", 100.0))
        mu = Float64(get(params, "mu", 0.0))
        sigma = Float64(get(params, "sigma", 0.2))
        t = Float64(get(params, "t", 1.0))
        (s0 > 0 && sigma >= 0 && t > 0) || return ("SIM_GBM_PARAMS_INVALID", nothing)
        drift = (mu - 0.5 * sigma^2) * t
        vol = sigma * sqrt(t)
        @inbounds for i in 1:trials
            samples[i] = s0 * exp(drift + vol * samples[i])
        end
    else
        return ("SIM_UNKNOWN_MODEL:$model", nothing)
    end
    m = mean(samples)
    qs = quantile(samples, [0.05, 0.50, 0.95])
    lo, hi = extrema(samples)
    return (nothing, Dict{String,Any}(
        "model" => model,
        "trials" => trials,
        "seed" => seed,
        "mean" => m,
        "std" => std(samples; mean=m),
        "p05" => qs[1],
        "p50" => qs[2],
        "p95" => qs[3],
        "min" => lo,
        "max" => hi,
    ))
end

# -- dispatch ---------------------------------------------------------------

const OPS = Dict{String,Function}(
    "stats.describe" => op_stats_describe,
    "stats.quantiles" => op_stats_quantiles,
    "stats.correlation" => op_stats_correlation,
    "linalg.lstsq" => op_linalg_lstsq,
    "optimize.nelder_mead" => op_optimize_nelder_mead,
    "simulate.monte_carlo" => op_simulate_monte_carlo,
)

function main()::Cint
    args = ARGS
    i = findfirst(==("--job"), args)
    job_text = if i !== nothing && i < length(args)
        read(args[i + 1], String)
    else
        read(stdin, String)
    end
    job = try
        JSON.parse(job_text)
    catch e
        return fail("", "INVALID_JOB_JSON")
    end
    job isa AbstractDict || return fail("", "JOB_NOT_OBJECT")
    String(get(job, "contract", "")) == CONTRACT || return fail("", "CONTRACT_MISMATCH")
    Int(get(job, "schema", SCHEMA_VERSION)) == SCHEMA_VERSION ||
        return fail("", "SCHEMA_VERSION_MISMATCH")
    op = String(get(job, "op", ""))
    handler = get(OPS, op, nothing)
    handler === nothing && return fail(op, "UNKNOWN_OP")
    params = get(job, "params", Dict{String,Any}())
    err, result = try
        handler(params)
    catch e
        ("OP_EXCEPTION:$(typeof(e))", nothing)
    end
    err === nothing || return fail(op, err)
    return reply(true; op=op, result=result)
end

# -- precompile workload ----------------------------------------------------
#
# Exercise every op + the JSON/parse path once during Pkg.precompile so a
# spawned call hits cached native code (the dominant cold-start cost for a
# spawn-per-call tool).

@compile_workload begin
    JSON.parse("""{"contract":"x"}""")
    op_stats_describe(Dict{String,Any}("values" => Any[1, 2, 3, 4, 5]))
    op_stats_quantiles(Dict{String,Any}(
        "values" => Any[1, 2, 3], "qs" => Any[0.25, 0.5]))
    op_stats_correlation(Dict{String,Any}(
        "x" => Any[1, 2, 3], "y" => Any[2, 4, 6]))
    op_linalg_lstsq(Dict{String,Any}(
        "a" => Any[Any[1, 1], Any[1, 2], Any[1, 3]], "b" => Any[1, 2, 2]))
    op_optimize_nelder_mead(Dict{String,Any}("x0" => Any[1.0, 2.0]))
    op_optimize_nelder_mead(Dict{String,Any}(
        "x0" => Any[1.0, 2.0], "objective" => "rosenbrock", "max_iter" => 50))
    op_simulate_monte_carlo(Dict{String,Any}(
        "trials" => 200, "seed" => 1))
    op_simulate_monte_carlo(Dict{String,Any}(
        "trials" => 200, "seed" => 1, "model" => "gbm"))
end

end # module JuliaCompute
