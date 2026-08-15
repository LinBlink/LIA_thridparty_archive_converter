<#
.SYNOPSIS
    run.py 的看门狗：被外部杀掉就自动拉起来，接着跑。

.DESCRIPTION
    run.py 本身是常驻进程，但实测它会**被外部静默终止**——没有 traceback、没有
    Python 异常、Windows 事件日志里也没有 python.exe 的错误报告，控制台直接回到
    提示符。这类死法在进程内部是拦不住的（catch 不到自己被 kill），只能在外面
    守着重启。

    退出码决定要不要重启：
      0        正常结束 / Ctrl+C 主动中止      → 不重启
      1        run.py 的熔断 Halted（链路坏了） → 不重启，等人看 data/run_errors.log
      2        命令行参数错                     → 不重启
      其它/被杀 说明是意外死亡                   → 重启

    重启是安全的：已入库的源文件已被删除，未处理的还在 FILES_TO_SQL/，
    jobs_done.txt 也记着账，所以接着跑不会重复建档。唯一的代价是内存里的
    handled 集合丢了——上一轮解析失败的文件会再试一次。

.PARAMETER PythonArgs
    透传给 run.py 的参数。**建议显式带上 --provider 和 --concurrency**，否则重启后
    会卡在「选择 API provider」/「输入并发数」的交互提示上等人按回车。并发数建议用长
    写法 --concurrency（-j 目前也能透传，但它长得像 PowerShell 的参数名，
    以后本脚本一旦加个 j 开头的参数就会被抢走）。

.EXAMPLE
    .\run_forever.ps1 --provider minimax --concurrency 4 --force

.NOTES
    必须从 repo 根目录运行（run.py 依赖相对路径的 .env / data / FILES_TO_SQL）。
#>
# PositionalBinding=$false 是必须的：否则 `.\run_forever.ps1 --provider minimax`
# 里的 `--provider` 会被按位置绑到 $MaxRestarts 上直接报类型错误
# （PowerShell 的 `--` 只对原生命令是「参数终止符」，对脚本参数不是）。
[CmdletBinding(PositionalBinding = $false)]
param(
    [int]$MaxRestarts = 100,        # 最多重启多少次，防止无限热循环
    [int]$BackoffSeconds = 10,      # 每次重启前等几秒
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PythonArgs = @()
)

$ErrorActionPreference = 'Continue'

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$script = Join-Path $PSScriptRoot 'archive_content_markdown_update\run.py'
$log = Join-Path $PSScriptRoot 'data\supervisor.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

# 透传参数里 `--` 是 PowerShell 的参数终止符，会被原样收进来，去掉
$PythonArgs = @($PythonArgs | Where-Object { $_ -ne '--' })

if ($PythonArgs -notcontains '--provider') {
    Write-Host "⚠️  没有传 --provider：重启后会停在交互选择提示上等人。" -ForegroundColor Yellow
    Write-Host "   建议：.\run_forever.ps1 --provider minimax --concurrency 4 --force" -ForegroundColor Yellow
}

# 并发数同理：run.py 没拿到 --concurrency 就会弹交互提示，重启后没人按回车。
# 这里按长写法 --concurrency 检测；-j 也能透传，但不在检测范围内。
if ($PythonArgs -notcontains '--concurrency') {
    Write-Host "[warn] 没有传 --concurrency：重启后会停在并发数输入提示上等人。" -ForegroundColor Yellow
}

function Write-Log($message) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host "🐕 $line" -ForegroundColor Cyan
}

Write-Log "看门狗启动：$python $script $($PythonArgs -join ' ')"

$restarts = 0
while ($true) {
    $started = Get-Date
    & $python -u $script @PythonArgs
    $code = $LASTEXITCODE
    $ranFor = [int]((Get-Date) - $started).TotalSeconds

    if ($code -eq 0) {
        Write-Log "run.py 正常退出（跑了 ${ranFor}s），不再重启"
        break
    }
    if ($code -eq 1) {
        Write-Log "run.py 熔断停机（跑了 ${ranFor}s）——链路坏了，先看 data/run_errors.log，不重启"
        break
    }
    if ($code -eq 2) {
        Write-Log "命令行参数有误，不重启"
        break
    }

    $restarts++
    if ($restarts -gt $MaxRestarts) {
        Write-Log "已重启 $MaxRestarts 次，超过上限，停手"
        break
    }

    # 秒退说明不是「跑着跑着被杀」，而是一起步就挂——多半是配置问题，别热循环
    if ($ranFor -lt 30) {
        $BackoffSeconds = [Math]::Min($BackoffSeconds * 2, 300)
        Write-Log "run.py 起步 ${ranFor}s 就退出（code=$code），退避加长到 ${BackoffSeconds}s"
    } else {
        Write-Log "run.py 意外终止（code=$code，跑了 ${ranFor}s），第 $restarts 次重启，${BackoffSeconds}s 后继续"
    }

    Start-Sleep -Seconds $BackoffSeconds
}
