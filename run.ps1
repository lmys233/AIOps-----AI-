# ============================================================
#  AIOps 项目 · PowerShell 操作入口
#
#  本机未安装 make，因此全部操作都收在这一个脚本里
#  （task.md T1 细节 5）。**没有** Makefile —— 不要去找它。
#
#  用法：
#     .\run.ps1 <命令> [参数]
#     .\run.ps1 help
#
#  示例：
#     .\run.ps1 env              # 打印 Python 环境
#     .\run.ps1 selftest         # 验收测试代码本身
#     .\run.ps1 verify-observed  # 验收被观测系统
#     .\run.ps1 up               # 启动被观测环境
#     .\run.ps1 logs order       # 跟踪某服务日志
#
#  ★ 关键：脚本自动把 src/ 加入 PYTHONPATH，保证 `import agent` / `import observed`
#    在任意工作目录下都可用（src-layout 的要求）。
# ============================================================

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = 'help',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Continue'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SrcDir = Join-Path $ScriptDir 'src'
$ComposeFile = Join-Path $ScriptDir 'deploy\docker-compose.yml'

# src-layout：把 src/ 放进 PYTHONPATH
$env:PYTHONPATH = $SrcDir

function Say  { param([string]$T, [string]$C = 'Gray') Write-Host $T -ForegroundColor $C }
function Head { param([string]$T) Say ''; Say ('=' * 66) 'Cyan'; Say "  $T" 'Cyan'; Say ('=' * 66) 'Cyan' }

# ------------------------------------------------------------
#  ★ 虚拟环境（隔离）
#
#  解释器选择优先级：
#    1. 环境变量 $env:AIOPS_PYTHON（显式指定，最高优先级）
#    2. 项目内 .venv\Scripts\python.exe（虚拟环境，推荐）
#    3. PATH 中的 python（最后兜底）
#
#  ★ 为什么必须有 venv：spec §7.1 要求 Pydantic v2，
#    而系统 Python 3.11 装的是 pydantic 1.x —— 会直接不满足契约要求。
#
#  ⚠️ 已知问题：本环境下 `python -m venv .venv` 会因 ensurepip 权限失败
#     而导致 venv 内没有 pip（详见 fix_bugs.md BUG-001）。
#     在此问题裁决前，若 .venv 缺少 pip，请用下面的方式重建：
#         <base-python> -m venv --system-site-packages --without-pip .venv
#     该方式仍能得到真正隔离的 venv（sys.prefix != sys.base_prefix）。
# ------------------------------------------------------------
$VenvPython = Join-Path $ScriptDir '.venv\Scripts\python.exe'

if ($env:AIOPS_PYTHON) {
    $Py = $env:AIOPS_PYTHON
    $PySource = '环境变量 AIOPS_PYTHON'
} elseif (Test-Path $VenvPython) {
    $Py = $VenvPython
    $PySource = '项目虚拟环境 .venv'
} else {
    $Py = 'python'
    $PySource = 'PATH（未检测到 .venv）'
}

function Invoke-Py {
    param([string[]]$PyArgs)
    & $Py @PyArgs
}

# ------------------------------------------------------------
#  ★ 部署身份（T7c）：把 `deploy/.env` 读进环境
#
#  为什么要有这一步：集群名/环境是**部署事实**，它的唯一定义处是
#  `deploy/.env`（compose 自己也读这个文件，所以观测栈拿到的是同一个值）。
#  但被观测业务与 Agent 是**宿主原生进程**，compose 不会替它们注入 ——
#  于是由这里读出来，导出成 SDK/Agent 认的环境变量名（`AIOPS_*`）。
#
#  ★ 这样做之后全仓库只有一个地方写着集群名：
#    改 `deploy/.env` 一处 → 观测栈 + SDK 自报 + Agent 对齐**同时**跟着变。
#    修前它散在 5 个文件里、还有 2 个不同取值（fix_bugs.md BUG-063 的同族问题）。
# ------------------------------------------------------------
function Import-DeployEnv {
    $deployEnv = Join-Path $ScriptDir 'deploy\.env'
    if (-not (Test-Path $deployEnv)) { return }
    $mapping = @{
        'OBSERVED_CLUSTER'   = 'AIOPS_CLUSTER'
        'OBSERVED_ENV'       = 'AIOPS_ENV'
        'OBSERVED_NAMESPACE' = 'AIOPS_NAMESPACE'
    }
    foreach ($line in Get-Content $deployEnv) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith('#')) { continue }
        $pair = $text.Split('=', 2)
        if ($pair.Count -ne 2) { continue }
        $key = $pair[0].Trim()
        $value = $pair[1].Trim()
        if ($mapping.ContainsKey($key)) {
            # ★ 显式设过环境变量就不覆盖（环境变量优先级更高，便于临时试另一个集群名）
            $target = $mapping[$key]
            if (-not [Environment]::GetEnvironmentVariable($target)) {
                Set-Item -Path "Env:$target" -Value $value
            }
        }
    }
}

Import-DeployEnv

# 只导入部署 profile 路径；凭据仍由 SDK 按 env_refs 读取，不导入平台密钥。
function Import-SdkDeploymentProfile {
    if ($env:AIOPS_SDK_DEPLOYMENT_PROFILE) { return }
    $sdkEnvFile = Join-Path $ScriptDir '.env'
    if (-not (Test-Path $sdkEnvFile)) { return }
    foreach ($sdkEnvLine in Get-Content -LiteralPath $sdkEnvFile) {
        if ($sdkEnvLine -match '^\s*AIOPS_SDK_DEPLOYMENT_PROFILE\s*=(.*)$') {
            $env:AIOPS_SDK_DEPLOYMENT_PROFILE = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}
Import-SdkDeploymentProfile

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    & docker compose -f $ComposeFile @ComposeArgs
}

# ------------------------------------------------------------
#  ★ 原生日志目录（阶段 1R）
#
#  被观测系统在宿主上原生跑，其 JSONL 日志写到这里；
#  promtail 在容器里通过 bind mount 只读采集同一个目录。
#
#  ★ 目录必须在 `compose up` **之前**存在：
#    bind mount 一个不存在的宿主路径时，Docker 会替你创建一个
#    （Windows 上位置可能出乎意料），先建好可以避免"日志写到了别处"。
# ------------------------------------------------------------
$LogDir = Join-Path $ScriptDir '.run\logs'

function Ensure-LogDir {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        Say "  已创建原生日志目录: $LogDir" 'Gray'
    }
}

# ------------------------------------------------------------
#  ★ 等一个端口就绪（T13）
#
#  为什么 `up` 需要它：`docker compose up -d` 只是"容器已创建"，
#  Collector 往往还要几秒才开始监听 4317。而 SDK 在启动时**探一次** OTLP 端点：
#  探不到就不启用 span 导出（避免它每批重试刷日志，见 fix_bugs.md BUG-024）。
#  于是"先起业务、后起采集者"会让本轮链路是空的 ——
#  看起来像"没配链路"，实际只是**顺序**问题。
#
#  所以 `up` 在起业务之前显式等一下：把"采集者已就绪"变成**被检查过的前置条件**，
#  而不是一个期望。
# ------------------------------------------------------------
function Wait-Port {
    param([int]$Port, [int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $client.Connect('127.0.0.1', $Port)
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        } finally {
            $client.Dispose()
        }
    }
    return $false
}

switch ($Command.ToLower()) {

    'help' {
        Say ''
        Say '  AIOps · 操作入口（PowerShell 版）' 'Cyan'
        Say ''
        Say '  .\run.ps1 env                打印当前 Python 环境与关键依赖版本'
        Say '  .\run.ps1 venv [基础解释器]   创建/重建项目虚拟环境（BUG-001 方案 B）'
        Say '  .\run.ps1 venv-check         校验虚拟环境健康状态（pip 可用 + 真正隔离）'
        Say '  .\run.ps1 check-deps         校验 pydantic 是否为 v2'
        Say ''
        Say '  .\run.ps1 selftest           验收测试代码本身（不请求服务）'
        Say '  .\run.ps1 verify-t1 ~ t5     阶段 0 各任务的阶段性验收'
        Say '  .\run.ps1 verify-t1-5        阶段 0 出口验收（T1~T5）'
        Say '  .\run.ps1 verify-t6 ~ t14    阶段 1~3 各任务的阶段性验收'
        Say '  .\run.ps1 verify-phase1      T6~T14 验收（离线部分）'
        Say '  .\run.ps1 verify             全量验收（需目标已部署）'
        Say '  .\run.ps1 verify-observed    只验收被观测系统（黑盒契约）'
        Say '  .\run.ps1 sdk-verify [服务]   SDK 接入自检（默认 order，走 venv 解释器）'
        Say '  .\run.ps1 verify-agent       只验收 Agent 外部接口'
        Say '  .\run.ps1 verify-contracts   只验收内部契约'
        Say '  .\run.ps1 list               列出全部用例'
        Say ''
        Say '  .\run.ps1 up                 启动整套环境（基础设施 + 业务系统）'
        Say '  .\run.ps1 down               停止整套环境'
        Say ''
        Say '  ── 基础设施（Docker：Postgres + 观测栈）── compose 只含这些'
        Say '  .\run.ps1 infra-up           启动基础设施'
        Say '  .\run.ps1 infra-down         停止（保留数据卷）'
        Say '  .\run.ps1 infra-clean        ⚠️ 停止并删除数据卷'
        Say '  .\run.ps1 infra-ps           查看容器健康状态'
        Say '  .\run.ps1 infra-logs <服务名> 跟踪某个基础设施容器的日志'
        Say ''
        Say '  ── 被观测业务系统（宿主原生进程，不需要 Docker）──'
        Say '  .\run.ps1 business-up        启动 4 个业务服务（**前台运行，Ctrl+C 停止**）'
        Say '  .\run.ps1 business-up order  只启动指定服务（排查单个服务时用）'
        Say '                              停止 = 在它的终端按 Ctrl+C'
        Say ''
        Say '  .\run.ps1 loadgen [参数]      并发流量发生器（纯标准库）'
        Say '  .\run.ps1 observed-discover  ★ 探测被观测系统，重写 observed_info.yaml（服务清单唯一来源）'
        Say '  .\run.ps1 collect-report      ★ 四通道证据清单（T14：人工核对采集结果）'
        Say ''
        Say '  ── Agent（T15 起）──'
        Say '  .\run.ps1 agent-up           启动 Agent 服务壳（**前台运行，Ctrl+C 停止**，默认 8000）'
        Say '  .\run.ps1 tools [--role X]   查看工具注册表（在册工具 / 能力 / 各角色可见集合）'
        Say '  .\run.ps1 forensics [参数]   ★ 跑一轮真取证（5 路并行 + 黑板清单；--plan-only 只看计划）'
        Say '  .\run.ps1 topology-migrate  建拓扑表（幂等；服务 + 依赖边）'
        Say '  .\run.ps1 topology-sync     从"来源"取一次拓扑事实并灌库（打印用了哪个来源）'
        Say '  .\run.ps1 topology-check    拓扑体检（--show 打印整张图与爆炸半径）'
        Say '  .\run.ps1 memory-migrate     建记忆层的表（幂等；pgvector + pg_trgm）'
        Say '  .\run.ps1 memory-check       记忆层体检（四层读写；--demo 演示写入+召回）'
        Say '  .\run.ps1 agent-unit          Agent 实现级单测（不需要外网）'
        Say '  .\run.ps1 intent-eval [--arm rules|full|both]  ★ 意图识别效果评测（两臂对照；唯一真调模型）'
        Say '  .\run.ps1 llm-check           ★ 模型连通性自检（唯一需要外网；不打印 key）'
        Say ''
        Say "  PYTHONPATH 已设为: $SrcDir"
        Say "  使用的 Python   : $Py"
        Say "  解释器来源      : $PySource"
        Say '  （可用 $env:AIOPS_PYTHON 覆盖；否则优先用项目内 .venv）'
        Say ''
    }

    'env' {
        Head 'Python 环境'
        Say "  解释器来源: $PySource"
        Invoke-Py @('-c', @'
import sys
print("  解释器 :", sys.executable)
print("  版本   :", sys.version.split()[0])
print("  隔离   :", "独立 venv" if sys.prefix != sys.base_prefix else "非 venv（共用 base）")
print("  prefix :", sys.prefix)
try:
    import pydantic
    print("  pydantic:", pydantic.VERSION)
except Exception as e:
    print("  pydantic: 未安装 (%s)" % e)
try:
    import yaml
    print("  PyYAML  :", yaml.__version__)
except Exception as e:
    print("  PyYAML  : 未安装 (%s)" % e)
'@)
        Say ''
    }

    'venv' {
        Head '创建项目虚拟环境（BUG-001 方案 B + BUG-003 离线 wheel）'
        Write-Host ""
        Write-Host "  采用方案：" -ForegroundColor Gray
        Write-Host "    1) venv --without-pip（此步不触碰 tempfile，不会失败）" -ForegroundColor Gray
        Write-Host "    2) 用 zipfile 把 pip wheel 解包进 site-packages（绕开根因）" -ForegroundColor Gray
        Write-Host "    3) 从 wheels/ 离线安装全部依赖（带 constraints 固定版本）" -ForegroundColor Gray
        Write-Host ""
        Write-Host "  若 wheels/ 不存在，请先在有网机器上执行：" -ForegroundColor Yellow
        Write-Host "    pip download -r requirements.txt -r requirements-services.txt -d wheels --only-binary=:all:" -ForegroundColor DarkGray
        Write-Host ""

        # ★ 必须用 base 解释器运行：脚本要删除并重建 .venv，
        #   若用 .venv 自己的 python 运行，会因 python.exe 被占用而 Permission denied。
        $basePy = if ($args.Count -ge 1) { $args[0] }
                  elseif ($env:AIOPS_BASE_PYTHON) { $env:AIOPS_BASE_PYTHON }
                  else { (Get-Command py.exe -ErrorAction SilentlyContinue).Source }

        if (-not (Test-Path $basePy)) {
            Say "  [X] 找不到基础解释器: $basePy" 'Red'
            Say '      用法: .\run.ps1 venv <基础解释器路径>' 'Yellow'
            Say '      或设置环境变量 AIOPS_BASE_PYTHON' 'Yellow'
            break
        }

        $wheelsDir = Join-Path $ScriptDir 'wheels'
        $script = Join-Path $ScriptDir 'tests\venv_init.py'

        if (Test-Path $wheelsDir) {
            & $basePy $script --venv (Join-Path $ScriptDir '.venv') --base $basePy --from-wheels $wheelsDir
        } else {
            Say "  [!] 未找到 wheels/ 目录，降级为仅搬运 base 已有依赖" 'Yellow'
            Say "      这将缺少 fastapi 等包（T7 需要）" 'Yellow'
            & $basePy $script --venv (Join-Path $ScriptDir '.venv') --base $basePy --bundle-deps
        }
        if ($LASTEXITCODE -eq 0) {
            Say ''
            Say '  [OK] 虚拟环境已就绪' 'Green'
        } else {
            Say ''
            Say '  [X] 创建失败，请查看上面的输出' 'Red'
        }
        Say ''
    }

    'venv-check' {
        Head '校验虚拟环境健康状态'
        $script = Join-Path $ScriptDir 'tests\venv_init.py'
        $basePy = if ($args.Count -ge 1) { $args[0] }
                  elseif ($env:AIOPS_BASE_PYTHON) { $env:AIOPS_BASE_PYTHON }
                  else { (Get-Command py.exe -ErrorAction SilentlyContinue).Source }
        if (-not $basePy) {
            Say '  [X] 找不到 Python Launcher；请传入基础解释器路径或设置 AIOPS_BASE_PYTHON' 'Red'
            break
        }
        & $basePy $script --check (Join-Path $ScriptDir '.venv')
        Say ''
    }

    'check-deps' {
        Head '校验依赖（spec §7.1 要求 pydantic v2）'
        Invoke-Py @('-c', @'
import sys
try:
    import pydantic
except ImportError:
    sys.exit("  [X] 未安装 pydantic")
major = int(pydantic.VERSION.split(".")[0])
if major < 2:
    print("  [X] pydantic %s 不满足要求（spec §7.1 需要 v2）" % pydantic.VERSION)
    print("      请使用装有 pydantic v2 的解释器：")
    print("        $env:AIOPS_PYTHON = 'D:\\mydownload\\Anaconda\\body\\python.exe'")
    sys.exit(1)
print("  [OK] pydantic %s" % pydantic.VERSION)
'@)
        Say ''
    }

    'selftest'   { Invoke-Py @((Join-Path $ScriptDir 'tests\selftest.py')) }
    'verify'     { Invoke-Py @((Join-Path $ScriptDir 'tests\run.py')) }
    'list'       { Invoke-Py @((Join-Path $ScriptDir 'tests\run.py'), '--list') }

    # ---- 各任务的阶段性验收 ----
    'verify-t1'  { Invoke-Py @((Join-Path $ScriptDir 'tests\verify_t1.py')) }
    'verify-t2'  { Invoke-Py @((Join-Path $ScriptDir 'tests\verify_t2.py')) }
    'verify-t3'  { Invoke-Py @((Join-Path $ScriptDir 'tests\verify_t3.py')) }
    'verify-t4'  { Invoke-Py @((Join-Path $ScriptDir 'tests\verify_t4.py')) }
    'verify-t5'  { Invoke-Py @((Join-Path $ScriptDir 'tests\verify_t5.py')) }
    'verify-t1-5' {
        Head 'T1~T5 阶段性验收（阶段 0 出口）'
        $script:AllOk = $true
        foreach ($t in @('verify_t1.py', 'verify_t2.py', 'verify_t3.py',
                         'verify_t4.py', 'verify_t5.py')) {
            $f = Join-Path $ScriptDir "tests\$t"
            if (-not (Test-Path $f)) { Say "  [--] $t 不存在，跳过" 'Yellow'; continue }
            & $Py $f | Out-Null
            if ($LASTEXITCODE -eq 0) { Say "  [OK] $t" 'Green' }
            else { Say "  [X]  $t" 'Red'; $script:AllOk = $false }
        }
        Say ''
        if ($script:AllOk) { Say '  阶段 0 出口条件达成' 'Green' }
        else { Say '  阶段 0 仍有任务未通过' 'Red' }
        Say ''
    }

    # ---- 阶段 1~3（T6~T14）验收 ----
    # 九个任务共用一个脚本，用 --task 选择，避免九份几乎相同的文件。
    { $_ -in 'verify-t6', 'verify-t7', 'verify-t8', 'verify-t9', 'verify-t10',
              'verify-t11', 'verify-t12', 'verify-t13', 'verify-t14' } {
        $task = $Command.ToLower().Replace('verify-', '')
        Head "$($task.ToUpper()) 验收（task.md）"
        Invoke-Py @((Join-Path $ScriptDir 'tests\verify_phase1.py'), '--task', $task)
        Say ''
    }

    'verify-phase1' {
        Head 'T6~T14 验收（离线部分）'
        Invoke-Py @((Join-Path $ScriptDir 'tests\verify_phase1.py'))
        Say ''
    }

    # ---- 流量发生器 ----
    'loadgen' {
        Head '并发流量发生器'
        Invoke-Py (@('-m', 'observed.loadgen') + $Args)
        Say ''
    }

    #  ★ 必须把 $Args 透传下去：task.md 的验收写的是
    #    `.\run.ps1 verify-agent -k "I1"`，不透传的话 -k 会被静默丢掉
    #    （然后"只跑了 I1"其实是"跑了全部套件"，判定完全不同）。
    'verify-observed'  { Invoke-Py (@((Join-Path $ScriptDir 'tests\run.py'), '--suite', 'observed') + $Args) }
    'verify-agent'     { Invoke-Py (@((Join-Path $ScriptDir 'tests\run.py'), '--suite', 'agent-external') + $Args) }
    'verify-contracts' { Invoke-Py (@((Join-Path $ScriptDir 'tests\run.py'), '--suite', 'internal-contracts') + $Args) }

    # ---- 被观测系统探测器（服务清单的唯一来源）----
    #  ★ 一次 `GET /services` 拿到"现在谁活着"，整份重写 src\agent\observed_info.yaml
    #    （AIOps 侧的槽位抽取从这里读服务清单）。实现见 src\agent\discovery\probe.py。
    #  ★ `-watch` / `-interval` 之类参数原样透传：`run.ps1 observed-discover --watch`。
    'observed-discover' {
        Head '被观测系统探测器（GET /services → observed_info.yaml）'
        Invoke-Py (@('-m', 'agent.discovery') + $Args)
        Say ''
    }

    # ---- 四通道证据清单（T14 的人工核对入口）----
    #  ★ 它**只摆证据**：四路各取样（条数 / 时间范围 / 样例 / 原始查询命令），
    #    "采集是否成功"由人判断 —— 脚本自己下结论就会出"脚本说绿了其实没数据"。
    #    实现见 tools\collect_report.py（只读，不改任何状态）。
    'collect-report' {
        Invoke-Py (@((Join-Path $ScriptDir 'tools\collect_report.py')) + $Args)
    }

    # ---- Agent 侧（T15 起）----
    #  ★ agent-unit：实现级单测（不需要外网/Docker；用 httpx.MockTransport 假供应商）
    #  ★ llm-check ：模型连通性自检（**唯一需要外网**的一条；只报键名不报 key）
    'agent-unit' { Invoke-Py @((Join-Path $ScriptDir 'tests\agent_unit.py')) }
    'llm-check'  { Invoke-Py @((Join-Path $ScriptDir 'tools\llm_check.py')) }

    # ---- SDK 接入自检（T12 交付）----
    #  ★ 为什么要有这个子命令：直接敲 `python -m sdk verify` 会用到 PATH 上的
    #    python，而它不是本项目的 .venv —— 实测报
    #    `ModuleNotFoundError: No module named 'prometheus_client'`。
    #    这里强制用 $Py（venv 解释器），并把唯一那份项目配置传进去。
    'sdk-verify' {
        Head 'SDK 接入自检（python -m sdk verify）'
        $svc = if ($Args.Count -ge 1) { $Args[0] } else { 'order' }
        Invoke-Py @('-m', 'sdk', 'verify',
                    '--config', (Join-Path $SrcDir 'observed\app.yaml'),
                    '--service', $svc)
        Say ''
    }

    # ---- Agent 服务壳（T15 交付：POST /v1/chat）----
    #  ★ 端口默认取 AIOPS_AGENT（http://127.0.0.1:8000）→ 也可 `agent-up --port 8010`。
    #  ★ 没配 AIOPS_LLM_* 也能起：意图识别退化为纯规则层，并在 /health 与
    #    响应里如实说明降级（task.md T15 验收项 7/8）。
    'agent-up' { Invoke-Py (@('-m', 'agent') + $Args) }

    # ---- 工具注册表（T16b：动态注册后"现在到底有哪些工具 / 谁能调"）----
    #  ★ 注册表是动态的（内置 9 个 + extensions.yaml 里的扩展），
    #    所以必须能**被外部观察到**：`--role` 看某角色的候选，`--json` 给脚本用。
    'tools' { Invoke-Py (@('-m', 'agent.tools') + $Args) }

    # ---- 取证层（T18：跑一轮真取证 / 只看计划）----
    #  ★ 为什么必须有这个入口：取证层的产物是"证据"，而"这一路没查成"与
    #    "这一路查了没异常"只能靠**看得见**才分得清（P20）；不给入口就只能读日志。
    #  ★ `--plan-only` 只打印计划、**不发任何工具调用** —— 用来核对可填充性检查。
    'forensics' { Invoke-Py (@('-m', 'agent.agents') + $Args) }

    # ---- 意图识别效果评测（T15b：唯一必须真调模型的一条）----
    #  ★ 两臂对照：规则层（0 token）vs 完整三阶段（快路由 → 不够自信才升级大模型）。
    #    它回答的是"规则未命中时模型判得准不准、值不值得花这个钱" ——
    #    T15 的 15 条验收项全是结构性的，**没有一条是准确率**。
    'intent-eval' {
        Invoke-Py (@((Join-Path $ScriptDir 'tools\intent_eval.py')) + $Args)
    }

    # ---- 拓扑层（T18c：服务与依赖边落库，让拓扑这一路不再必然降级）----
    #  ★ 三件事分开：建表（幂等）/ 从"来源"取一次事实并灌库 / 体检（可 --show 打印整张图）。
    #  ★ 同步会**打印它用了哪个来源**（自报 or 同仓配置）—— 图的权威性取决于来源。
    'topology-migrate' { Invoke-Py (@('-m', 'agent.topology', '--migrate') + $Args) }
    'topology-sync'    { Invoke-Py (@('-m', 'agent.topology', '--sync') + $Args) }
    'topology-check'   { Invoke-Py (@('-m', 'agent.topology', '--check') + $Args) }

    # ---- Agent 记忆层（T17：四层记忆 + 五道闸门）----
    #  ★ 记忆层引入了**两样新依赖**：Postgres 的记忆表（自己设计、自己迁移）与 Redis。
    #    "连不上 / 没建表"必须能一条命令看出来，而不是等 recall 静默降级。
    'memory-migrate' { Invoke-Py (@('-m', 'agent.memory', '--migrate') + $Args) }
    'memory-check'   { Invoke-Py (@('-m', 'agent.memory', '--check') + $Args) }

    # ---- 被观测业务系统（宿主原生进程，不需要 Docker）----
    #  ★ 4 个业务服务在宿主上跑；实现见 src/observed/native_run.py。
    #  ★ business-up 会**占住当前终端**（前台运行，Ctrl+C 停止）——
    #    停止就是 Ctrl+C，所以没有 business-down / business-ps 这类子命令，
    #    也不需要 PID 记账（理由写在 native_run.py 的模块文档里）。
    # ---- Web 台（T34）：前端由 Agent 应用托管，起一条命令即可 ----
    'webui' { Ensure-LogDir
        Say '  Web 台（用户 ↔ AIOps）：http://127.0.0.1:8013/webui/' 'Cyan'
        Say '  （静态资源零外部依赖；能力表由 /webui/api/capabilities 现算）' 'DarkGray'
        Invoke-Py (@('-m', 'agent') + $Args) }
    'business-up' { Ensure-LogDir; Invoke-Py (@('-m', 'observed.native_run') + $Args) }

    'up'         {
        Ensure-LogDir
        Say '  [1/3] 基础设施（Docker）' 'Cyan'
        Invoke-Compose @('up', '-d')
        Say ''
        Say '  [2/3] 等采集者就绪（SDK 启动时会探一次这些端口）' 'Cyan'
        foreach ($item in @(@(5432, 'Postgres'), @(4317, 'OTel Collector'))) {
            $port = $item[0]; $label = $item[1]
            if (Wait-Port -Port $port) {
                Say "      [OK] $label ($port)" 'Green'
            } else {
                Say "      [!]  $label ($port) 未在 60s 内就绪 —— 变更留痕/链路可能不可用" 'Yellow'
                Say "           （业务与指标/日志不受影响；修好后重启业务即可）" 'DarkGray'
            }
        }
        Say ''
        Say '  [3/3] 被观测业务系统（**经 SDK 启动**，前台运行，Ctrl+C 停止）' 'Cyan'
        Invoke-Py (@('-m', 'observed.native_run') + $Args)
    }
    'down'       {
        Say '  停止基础设施（业务系统由它的终端 Ctrl+C 停止）' 'Cyan'
        Invoke-Compose @('down')
    }

    # ---- 基础设施（Docker：Postgres + 观测栈）----
    #  ★ 阶段 1R 起，compose 只含有状态服务；被观测系统在宿主上原生跑。
    'infra-up'    { Ensure-LogDir; Invoke-Compose @('up', '-d') }
    'infra-down'  { Invoke-Compose @('down') }
    'infra-clean' { Invoke-Compose @('down', '-v') }
    'infra-ps'    { Invoke-Compose @('ps') }
    'infra-logs'  {
        if (-not $Args) { Say '  用法: .\run.ps1 infra-logs <服务名>' 'Yellow'; break }
        Invoke-Compose @('logs', '-f', '--tail=100', $Args[0])
    }
    'down-clean' { Invoke-Compose @('down', '-v') }
    'ps'         { Invoke-Compose @('ps') }
    'logs' {
        if (-not $Args) { Say '  用法: .\run.ps1 logs <服务名>' 'Yellow'; break }
        Invoke-Compose @('logs', '-f', '--tail=100', $Args[0])
    }

    default {
        Say "  未知命令: $Command" 'Red'
        Say '  运行 .\run.ps1 help 查看全部命令' 'Yellow'
    }
}
