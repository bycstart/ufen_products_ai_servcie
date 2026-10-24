param([switch]$Stop)

$ErrorActionPreference = 'Stop'
$localRoot = $PSScriptRoot
$aiRoot = (Resolve-Path -LiteralPath (Join-Path $localRoot '../..')).Path
$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $aiRoot '../..')).Path
$localPython = Join-Path $aiRoot '.venv/Scripts/python.exe'
$runtimePath = Join-Path $localRoot 'runtime.json'

if ($Stop) {
    if (Test-Path -LiteralPath $runtimePath) {
        foreach ($record in (Get-Content -Raw -LiteralPath $runtimePath | ConvertFrom-Json)) {
            $running = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
            # PID 可能被复用，同时核对启动时间和解释器，避免结束其他进程。
            if ($running -and $running.Path -eq $localPython -and
                $running.StartTime.ToUniversalTime().ToString('o') -eq $record.started) {
                Stop-Process -Id $running.Id
            }
        }
    }
    Write-Output 'Local service stop completed. Docker data is retained.'
    exit 0
}

& $localPython (Join-Path $localRoot 'configure.py') --check
if ($LASTEXITCODE -ne 0) { throw 'Run configure.py before starting services.' }
if (Test-Path -LiteralPath $runtimePath) {
    foreach ($record in (Get-Content -Raw -LiteralPath $runtimePath | ConvertFrom-Json)) {
        $running = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        if ($running -and $running.StartTime.ToUniversalTime().ToString('o') -eq $record.started) {
            throw 'Local services are already running. Stop them before restarting.'
        }
    }
}

$logsRoot = Join-Path $localRoot 'logs'
New-Item -ItemType Directory -Force -Path $logsRoot | Out-Null
$serveScript = '"' + (Join-Path $localRoot 'serve.py') + '"'
$services = @(
    @{ name = 'ecommerce'; root = (Join-Path $sourceRoot 'backend/backend/ecommerce-service'); args = @($serveScript, 'ecommerce') },
    @{ name = 'ai'; root = $aiRoot; args = @($serveScript, 'ai') },
    @{ name = 'customer'; root = (Join-Path $sourceRoot 'customer-service/customer-service'); args = @($serveScript, 'customer') },
    @{ name = 'ai-worker'; root = (Join-Path $sourceRoot 'customer-service/customer-service'); args = @('-m', 'atguigu.worker.ai.worker') },
    @{ name = 'outbox-worker'; root = (Join-Path $sourceRoot 'customer-service/customer-service'); args = @('-m', 'atguigu.worker.realtime') }
)
$records = @()
foreach ($service in $services) {
    $process = Start-Process -FilePath $localPython -ArgumentList $service.args `
        -WorkingDirectory $service.root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logsRoot ($service.name + '.out.log')) `
        -RedirectStandardError (Join-Path $logsRoot ($service.name + '.err.log'))
    $records += @{ name = $service.name; pid = $process.Id; started = $process.StartTime.ToUniversalTime().ToString('o') }
    # 每启动一个进程即保存，后续启动失败仍可通过 -Stop 清理已启动的进程。
    ConvertTo-Json -InputObject @($records) | Set-Content -LiteralPath $runtimePath -Encoding UTF8
}
Write-Output 'Local service processes launched. Run check.py to verify readiness; logs are in deploy/local/logs.'
