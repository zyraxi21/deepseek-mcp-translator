<#
.SYNOPSIS
    启动 DeepSeek Role Translator 的 HTTP 模式（本地代理 + MCP 服务器）。

.DESCRIPTION
    原 run_http.bat 的 PowerShell 版本：以脚本所在目录为工作目录，运行 python server.py --http，
    结束后停住窗口以便查看日志和报错。

.EXAMPLE
    .\run_http.ps1

.EXAMPLE
    端口被占用时换一个：

    .\run_http.ps1 -Port 8766

.NOTES
    双击 .ps1 默认是用记事本打开，请右键选择“使用 PowerShell 运行”，或在终端执行 .\run_http.ps1。
    如果提示“因为在此系统上禁止运行脚本”，先放宽一次当前用户的执行策略：

    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

    或只对这一次调用绕过策略：

    powershell -ExecutionPolicy Bypass -File .\run_http.ps1
#>
[CmdletBinding()]
param(
    # 监听端口，省略时沿用 server.py 的默认值（8765）
    [int]$Port
)

$ErrorActionPreference = 'Stop'

# 始终切到脚本所在目录，避免从别处调用时找不到 .env 和 server.py
Set-Location -LiteralPath $PSScriptRoot

# 优先使用项目内的虚拟环境，没有则退回 PATH 里的 python
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = 'python'
}
else {
    Write-Host '找不到 python 命令。请先安装 Python，或执行 pip install -r requirements.txt。' -ForegroundColor Red
    if ($Host.Name -eq 'ConsoleHost') { Read-Host '按回车键关闭窗口' | Out-Null }
    exit 1
}

$serverArgs = @('server.py', '--http')
if ($PSBoundParameters.ContainsKey('Port')) {
    $serverArgs += @('--port', $Port)
}

try {
    & $python @serverArgs
}
finally {
    # 对应 bat 里的 pause，让窗口停在日志上，方便排查启动失败的原因
    if ($Host.Name -eq 'ConsoleHost') {
        Write-Host ''
        Read-Host '按回车键关闭窗口' | Out-Null
    }
}

exit $LASTEXITCODE
