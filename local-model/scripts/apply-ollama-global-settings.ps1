param(
    [switch]$RestartOllama
)

$ErrorActionPreference = "Stop"
$localAiRoot = Split-Path -Parent $PSScriptRoot
$configRoot = Join-Path $localAiRoot "config"
$basePath = Join-Path $configRoot "ollama-model-parameters.json"
$dynamicPath = Join-Path $configRoot "ollama-model-parameters.dynamic.json"

$base = Get-Content -LiteralPath $basePath -Raw -Encoding utf8 | ConvertFrom-Json
$dynamic = Get-Content -LiteralPath $dynamicPath -Raw -Encoding utf8 | ConvertFrom-Json
$flashAttention = [bool]$base.global_ollama.flash_attention
$kvCacheType = [string]$base.global_ollama.kv_cache_type
if ($dynamic.enabled -ne $false -and $null -ne $dynamic.global_ollama) {
    if ($null -ne $dynamic.global_ollama.flash_attention) {
        $flashAttention = [bool]$dynamic.global_ollama.flash_attention
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$dynamic.global_ollama.kv_cache_type)) {
        $kvCacheType = [string]$dynamic.global_ollama.kv_cache_type
    }
}
if ($kvCacheType -notin @("f16", "q8_0", "q4_0")) {
    throw "Unsupported OLLAMA_KV_CACHE_TYPE: $kvCacheType"
}

[Environment]::SetEnvironmentVariable(
    "OLLAMA_FLASH_ATTENTION",
    $(if ($flashAttention) { "1" } else { "0" }),
    "User"
)
[Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE", $kvCacheType, "User")
$env:OLLAMA_FLASH_ATTENTION = $(if ($flashAttention) { "1" } else { "0" })
$env:OLLAMA_KV_CACHE_TYPE = $kvCacheType

if ($RestartOllama) {
    $ollamaRoot = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
    $ollamaRoot = [System.IO.Path]::GetFullPath($ollamaRoot)
    $ollamaProcesses = Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue
    foreach ($process in $ollamaProcesses) {
        $processPath = [System.IO.Path]::GetFullPath([string]$process.Path)
        if (-not $processPath.StartsWith($ollamaRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to stop Ollama process outside expected directory: $processPath"
        }
    }
    foreach ($process in $ollamaProcesses) {
        Stop-Process -Id $process.Id -Force
    }
    $appPath = Join-Path $ollamaRoot "ollama app.exe"
    $serverPath = Join-Path $ollamaRoot "ollama.exe"
    if (Test-Path -LiteralPath $appPath) {
        Start-Process -FilePath $appPath -WindowStyle Hidden
    }
    elseif (Test-Path -LiteralPath $serverPath) {
        Start-Process -FilePath $serverPath -ArgumentList "serve" -WindowStyle Hidden
    }
    else {
        throw "Ollama executable was not found under $ollamaRoot"
    }
}

[pscustomobject]@{
    OLLAMA_FLASH_ATTENTION = $(if ($flashAttention) { "1" } else { "0" })
    OLLAMA_KV_CACHE_TYPE = $kvCacheType
    Scope = "User"
    Restarted = [bool]$RestartOllama
}
