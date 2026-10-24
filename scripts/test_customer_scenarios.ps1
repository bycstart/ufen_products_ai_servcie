param([string]$Cases = '', [switch]$List)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
$testPath = Join-Path $PSScriptRoot 'test_customer_scenarios.py'
$reportRoot = Join-Path $projectRoot 'reports'
New-Item -ItemType Directory -Force -Path $reportRoot | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$reportPath = Join-Path $reportRoot ('customer-scenarios-' + $stamp + '.json')
$logPath = Join-Path $reportRoot ('customer-scenarios-' + $stamp + '.log')

# 使用 UTF-8 和无缓冲输出，测试过程中逐题展示日志，并同时保存文本及 JSON。
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$arguments = @('-u', $testPath, '--output', $reportPath)
if ($Cases) { $arguments += @('--cases', $Cases) }
if ($List) { $arguments += '--list' }
& $pythonPath @arguments | Tee-Object -FilePath $logPath
$testExitCode = $LASTEXITCODE
Write-Host ('Terminal log: ' + $logPath)
if (-not $List) { Write-Host ('JSON report: ' + $reportPath) }
exit $testExitCode
