# compute.jl — governed Julia compute endpoint (contract julia-compute/v1)
#
# A610 PYTHON-WORK-TRANSFER: Julia is the single formal execution owner for
# statistical/scientific compute and optimization/simulation work
# (按需分析 — on-demand, non-resident).  It never replaces JAX for model
# training and never parses untrusted data: the job document is produced
# by the governed Python wrapper from already-authorized request data.
#
# Protocol: one JSON job object on stdin (or --job <path>), one JSON result
# object on stdout.  Any contract violation exits nonzero with ok=false.

using JSON
using Statistics
using LinearAlgebra
using Random

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

as_vector(x) = Float64[v for v in Vector{Any}(x)]

function as_matrix(x)::Matrix{Float64}
    rows = [Float64[v for v in Vector{Any}(r)] for r in Vector{Any}(x)]
    isempty(rows) && return Matrix{Float64}(undef, 0, 0)
    ncol = length(rows[1])
    all(r -> length(r) == ncol, rows) || throw(ArgumentError("RAGGED_MATRIX"))
    return reduce(vcat, (r' for r in rows))
end

# -- stats ---------------------------------------------------------------

function op_stats_describe(params)
    v = as_vector(get(params, "values", Any[]))
    isempty(v) && return ("STATS_VALUES_EMPTY", nothing)
    return (nothing, Dict{String,Any}(
        "n" => length(v),
        "mean" => mean(v),
        "std" => std(v),
        "var" => var(v),
        "min" => minimum(v),
        "p25" => quantile(v, 0.25),
        "p50" => quantile(v, 0.50),
        "p75" => quantile(v, 0.75),
        "p95" => quantile(v, 0.95),
        "p99" => quantile(v, 0.99),
        "max" => maximum(v),
    ))
end

function op_stats_quantiles(params)
    v = as_vector(get(params, "values", Any[]))
    isempty(v) && return ("STATS_VALUES_EMPTY", nothing)
    qs = Float64[q for q in Vector{Any}(get(params, "qs", Any[]))]
    all(q -> 0.0 <= q <= 1.0, qs) || return ("STATS_Q_OUT_OF_RANGE", nothing)
    return (nothing, Dict{String,Any}(
        "quantiles" => [Dict("q" => q, "value" => quantile(v, q)) for q in qs],
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
    resid = a * x .- b
    return (nothing, Dict{String,Any}(
        "x" => x,
        "residual_norm" => norm(resid),
        "rank" => rank(a),
        "rcond" => 1.0 / cond(a),
    ))
end

# -- optimize --------------------------------------------------------------

function _objective(name::AbstractString, params)
    if name == "sphere"
        return x -> sum(x .^ 2)
    elseif name == "rosenbrock"
        return x -> sum(100.0 .* (x[2:end] .- x[1:end-1] .^ 2) .^ 2 .+ (1.0 .- x[1:end-1]) .^ 2)
    elseif name == "quadratic"
        qa = get(params, "a", Any[])
        isempty(qa) && return nothing
        a = as_matrix(qa)
        b = as_vector(get(params, "b", fill(0.0, size(a, 1))))
        c = Float64(get(params, "c", 0.0))
        return x -> 0.5 * dot(x, a, x) - dot(b, x) + c
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
    vals = f.(simplex)
    for _ in 1:max_iter
        order = sortperm(vals)
        simplex = simplex[order]
        vals = vals[order]
        span = maximum(abs.(vals .- vals[1]))
        diam = maximum(norm(simplex[i] .- simplex[1]) for i in 2:(n + 1))
        (span < tol && diam < sqrt(tol)) && break
        centroid = sum(simplex[1:n]) / n
        worst = simplex[n + 1]
        xr = centroid .+ (centroid .- worst)
        fr = f(xr)
        if vals[1] <= fr < vals[n]
            simplex[n + 1] = xr; vals[n + 1] = fr
        elseif fr < vals[1]
            xe = centroid .+ 2.0 .* (xr .- centroid)
            fe = f(xe)
            if fe < fr
                simplex[n + 1] = xe; vals[n + 1] = fe
            else
                simplex[n + 1] = xr; vals[n + 1] = fr
            end
        else
            xc = fr < vals[n + 1] ? centroid .+ 0.5 .* (xr .- centroid) :
                                    centroid .+ 0.5 .* (worst .- centroid)
            fc = f(xc)
            if fc < min(fr, vals[n + 1])
                simplex[n + 1] = xc; vals[n + 1] = fc
            else
                for i in 2:(n + 1)
                    simplex[i] = simplex[1] .+ 0.5 .* (simplex[i] .- simplex[1])
                    vals[i] = f(simplex[i])
                end
            end
        end
    end
    order = sortperm(vals)
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
    if model == "normal"
        mu = Float64(get(params, "mu", 0.0))
        sigma = Float64(get(params, "sigma", 1.0))
        sigma > 0 || return ("SIM_SIGMA_NONPOSITIVE", nothing)
        samples = mu .+ sigma .* randn(rng, trials)
    elseif model == "gbm"
        s0 = Float64(get(params, "s0", 100.0))
        mu = Float64(get(params, "mu", 0.0))
        sigma = Float64(get(params, "sigma", 0.2))
        t = Float64(get(params, "t", 1.0))
        (s0 > 0 && sigma >= 0 && t > 0) || return ("SIM_GBM_PARAMS_INVALID", nothing)
        z = randn(rng, trials)
        samples = s0 .* exp.((mu - 0.5 * sigma^2) .* t .+ sigma .* sqrt(t) .* z)
    else
        return ("SIM_UNKNOWN_MODEL:$model", nothing)
    end
    return (nothing, Dict{String,Any}(
        "model" => model,
        "trials" => trials,
        "seed" => seed,
        "mean" => mean(samples),
        "std" => std(samples),
        "p05" => quantile(samples, 0.05),
        "p50" => quantile(samples, 0.50),
        "p95" => quantile(samples, 0.95),
        "min" => minimum(samples),
        "max" => maximum(samples),
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
    job_text = ""
    args = ARGS
    i = findfirst(==("--job"), args)
    if i !== nothing && i < length(args)
        job_text = read(args[i + 1], String)
    else
        job_text = read(stdin, String)
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

exit(main())
