# Supervisor entry point. Run from a private local checkout with a prepared .venv/.env.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Bot', 'Llm', 'Backup')][string]$Mode,
    [string]$Repository = (Join-Path $PSScriptRoot '..\..'),
    [string]$Server = 'vendor\llama.cpp\llama-server.exe',
    [string]$Model = 'models\Qwen3-8B-Q4_K_M.gguf',
    [string]$BackupDirectory = 'data\backups'
)
$ErrorActionPreference = 'Stop'
try {
    Set-Location -LiteralPath (Resolve-Path -LiteralPath $Repository)
    $python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Prepare .venv first' }
    switch ($Mode) {
        'Bot' { & $python -m pozor.cli run }
        'Backup' { & $python -m pozor.cli db backup --directory $BackupDirectory --retention-days 7 }
        'Llm' {
            # Refuse unknown artifacts. This check does not certify GPU/quality gates.
            & $python -m pozor.cli deployment check --model $Model --server $Server
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            $deployment = Get-Content -LiteralPath 'deployment-lock.json' -Raw | ConvertFrom-Json
            $env:LLAMA_ARG_CHAT_TEMPLATE_KWARGS = '{"enable_thinking":false}'
            & $Server -m $Model -ngl 99 -c $deployment.runtime.context_size -np 1 --jinja --host 127.0.0.1 --port 8080 --alias $deployment.model.alias
        }
    }
    exit $LASTEXITCODE
} catch {
    # Avoid accidental credential/path disclosure through arbitrary exception text.
    Write-Error 'pozor supervisor failed; check local installation and configuration'
    exit 1
}
