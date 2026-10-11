<#
    PrivaGlot 部署引擎（Windows）

    由项目根目录的「本机部署.bat」（-Mode web）双击调用，也可以手动运行：
        powershell -ExecutionPolicy Bypass -File setup\bootstrap.ps1 -Mode web
    （「飞书部署.bat」已于 2026-10-09 废除：Windows 收敛为网页单形态，
    飞书部署一律走 VPS/Docker；-Mode feishu 降级为手动兜底，不再有入口脚本）

    参数：
        -Mode web|feishu        部署形态（缺省 web）：
                                  web    网页版 —— 本机浏览器上课
                                  feishu 飞书机器人 —— 手机飞书发语音上课
                                  （双通道模式已于 2026-09-23 退役：两通道
                                   是两个进程、共享 data\ 但互不实时同步，
                                   同跑是测试期形态，部署脚本不再提供）
        -Reinstall   删掉 .venv 重新装依赖（数据在 data\，不受影响）
        -NoLaunch    只准备环境，不启动服务
        -NoUpdate         本次启动跳过自更新检查
        -SelfUpdateOnly   只做自更新检查，不准备环境不启动（维护用）

    核心依赖两种形态完全一致（Python 3.11+ / ffmpeg / requirements / .env），
    差异只在「跑哪个入口」：web 起浏览器，feishu 起长连接机器人（不需要
    域名/HTTPS/备案）。环境自检见 setup\check_env.py。
    Linux VPS 用 setup\install_linux.sh，不需要这个脚本。

    做四件事：
      1. 找到（必要时安装）Python 3.11+ 和 ffmpeg
      2. 建 .venv 并安装 requirements.txt
      3. 准备 .env（飞书凭据优先手机扫码自动创建，也可粘贴已有应用的；
         MiMo Key 一律不在脚本里注入——网页端引导层 / 飞书端 Key 引导卡
         都会在启动后引导用户填写，验证通过立即生效）
      4. 按所选形态启动服务

    !! 本文件必须保存为「UTF-8 with BOM」!!
    Windows PowerShell 5.1 对没有 BOM 的文件按 ANSI 解码，中文会全变乱码。
    用 VS Code 编辑时请确认右下角编码显示为「UTF-8 with BOM」。
#>
[CmdletBinding()]
param(
    [string]$Mode = '',
    [switch]$Reinstall,
    [switch]$NoLaunch,
    [switch]$NoUpdate,
    [switch]$SelfUpdateOnly
)

# 不设 Stop：PS 5.1 会把外部程序写在 stderr 的正常输出当成异常。
# 所有外部程序调用都走 Invoke-Native，并显式检查退出码。
$ErrorActionPreference = 'Continue'

# 让中文在任何 Windows 语言设置下都能正确显示，并让 Python 也输出 UTF-8
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = [Console]::OutputEncoding
} catch { }
$env:PYTHONIOENCODING = 'utf-8'
$ProgressPreference = 'SilentlyContinue'   # 让 Invoke-WebRequest 快很多

# 老一点的 Windows 上 PowerShell 默认可能只协商 TLS 1.0，https 站点会直接连不上。
# 这个脚本要靠下载才活得下去（Python、ffmpeg），所以显式把 1.2 加回来。
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11 -bor [Net.SecurityProtocolType]::Tls
} catch { }

# ---- 唯一需要维护的几处对外信息 -------------------------------------------
# 注册链接、两条通道的 BASE_URL 现在都住在 app/config.py：网页上的
# 「去注册」按钮和 Key 的通道判断用的是那一份，这里不再重复，免得改漏一处。
# 下面这个版本号只给「没有 winget 的机器」用（改走 python.org 官方安装包），
# 跟着 Python 3.12 的安全更新手动往上抬即可。
$PythonFallbackVersion = '3.12.10'

# ---- 自更新（Windows 发行包形态；账本 #28）---------------------------------
# 双击即升级：启动先查清单，发现新版就地把新 zip 覆盖到安装目录再继续。
# 清单 = tools/build_windows_zip.sh 产出的 privaglot-win-latest.json，形如
#   {"version":"1.25","url":"<zip 完整下载地址>","sha256":"..."}
# 置空 = 静默跳过（PRIVAGLOT_UPDATE_URL 环境变量可临时覆盖，内测/排障）。
# git clone（开发者）形态没有 VERSION 文件，天然跳过。
# 清单住 Gitee 公开仓 raw 地址（永久不变，2026-10-11 组织仓转公开后回填）：
$UpdateManifestUrl = 'https://gitee.com/priva-glot-app/PrivaGlot-app/raw/master/privaglot-win-latest.json'
if ($env:PRIVAGLOT_UPDATE_URL) { $UpdateManifestUrl = $env:PRIVAGLOT_UPDATE_URL }

# ---- 路径 -----------------------------------------------------------------
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Root        = Split-Path -Parent $ScriptDir
$VenvDir     = Join-Path $Root '.venv'
$VenvPython  = Join-Path $VenvDir 'Scripts\python.exe'
$EnvPath     = Join-Path $Root '.env'
$EnvExample  = Join-Path $Root '.env.example'
$Requirements = Join-Path $Root 'requirements.txt'
$Port        = 8000
$LogDir      = Join-Path $Root 'logs'
$LogPath     = Join-Path $LogDir 'setup.log'
$TickerErrFile = Join-Path $env:TEMP 'privaglot-ticker.err'

# Python 装完之后可能落在哪几个目录里。
# 刚装好的 Python 不会出现在当前进程的 PATH 里，只能去这些目录里直接找。
$PythonBases = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Python'),   # 官方安装包 InstallAllUsers=0（按用户）
    (Join-Path $env:ProgramFiles 'Python312'),         # 官方安装包 InstallAllUsers=1（按机器）
    'C:\Python312'
)

# ---- 屏幕输出 -------------------------------------------------------------
# 一共几步，显示成 [2/4]，让用户随时知道还剩多少
$TotalSteps = 4
$script:StepNo    = 0
$script:StepStart = Get-Date
$script:RunStart  = Get-Date

# 秒数说成人话
function Format-Elapsed([TimeSpan]$ts) {
    if ($ts.TotalMinutes -ge 1) {
        return ('{0} 分 {1} 秒' -f [int][Math]::Floor($ts.TotalMinutes), $ts.Seconds)
    }
    return ('{0} 秒' -f [int][Math]::Round($ts.TotalSeconds))
}

function Step([string]$t) {
    $script:StepNo++
    $script:StepStart = Get-Date
    Write-Host ''
    Write-Host ("==> [{0}/{1}] {2}" -f $script:StepNo, $TotalSteps, $t) -ForegroundColor Cyan
}
# 一步做完了：绿勾 + 这一步花了多久
function StepDone([string]$t) {
    Write-Host ("    √ {0}（用时 {1}）" -f $t, (Format-Elapsed ((Get-Date) - $script:StepStart))) -ForegroundColor Green
}
function Good([string]$t) { Write-Host "    √ $t" -ForegroundColor Green }
function Note([string]$t) { Write-Host "    · $t" -ForegroundColor Gray }
function Warn([string]$t) { Write-Host "    ! $t" -ForegroundColor Yellow }
function Bad([string]$t)  { Write-Host "    × $t" -ForegroundColor Red }

# 运行外部程序：输出照常显示在屏幕上，返回退出码供调用方判断
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments | Out-Host
    } finally {
        $ErrorActionPreference = $prev
    }
    return $LASTEXITCODE
}

# 起一个"报时钟"小进程：每 10 秒往同一个控制台打一行，让用户看到还在动。
# 它只负责报时，跑完就被杀掉，我们不需要它的退出码；
# 真正的命令留在主线程用 & 跑，退出码走 $LASTEXITCODE —— 这条经得起验证。
function Start-Ticker([string]$What) {
    $text = "for (`$i = 10; `$i -le 7200; `$i += 10) { Start-Sleep -Seconds 10; " +
            "Write-Host ('    · {0}中... 已用 {1} 秒' -f '$What', `$i) -ForegroundColor DarkGray }"
    $enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($text))
    # stderr 必须丢掉：PowerShell 子进程在被重定向时会往 stderr 吐 "#< CLIXML"，
    # 那行噪音不能出现在用户的窗口里。
    Remove-Item -LiteralPath $TickerErrFile -Force -ErrorAction SilentlyContinue
    return Start-Process -FilePath 'powershell.exe' `
                         -ArgumentList @('-NoProfile', '-EncodedCommand', $enc) `
                         -NoNewWindow -PassThru -RedirectStandardError $TickerErrFile
}

# 跑一个可能很久的外部程序：输出照常显示，同时每 10 秒报一次"还在跑"。
# 慢没关系，但屏幕得一直在动，否则用户会以为卡死了。
function Invoke-NativeTicking {
    param([string]$Exe, [string[]]$Arguments = @(), [string]$What = '处理')
    $ticker = $null
    try { $ticker = Start-Ticker $What } catch { }
    try {
        & $Exe @Arguments | Out-Host
        $code = $LASTEXITCODE
    } finally {
        if ($ticker) { Stop-Process -Id $ticker.Id -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $TickerErrFile -Force -ErrorAction SilentlyContinue
    }
    return $code
}

# 下载大文件时把进度、百分比和速度都打出来。
# 慢可以，但一定要让用户看见它在动；顺带也把速度留在日志里，以后排查用它一眼就能定性。
function Get-FileWithProgress {
    param([string]$Url, [string]$OutFile, [string]$What = '文件')
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.Timeout = 60000
    $req.ReadWriteTimeout = 120000
    $req.UserAgent = 'PrivaGlot-bootstrap'
    $resp = $req.GetResponse()
    $total = $resp.ContentLength
    $in  = $resp.GetResponseStream()
    $out = [System.IO.File]::Create($OutFile)
    $buf = New-Object byte[] 131072
    $done = 0
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $next = 0.0
    try {
        while ($true) {
            $n = $in.Read($buf, 0, $buf.Length)
            if ($n -le 0) { break }
            $out.Write($buf, 0, $n)
            $done += $n
            if ($sw.Elapsed.TotalSeconds -ge $next) {
                $next = $sw.Elapsed.TotalSeconds + 5
                $sec = [Math]::Max($sw.Elapsed.TotalSeconds, 0.001)
                $kbs = [Math]::Round($done / 1KB / $sec)
                if ($total -gt 0) {
                    $msg = '    · {0} {1} / {2} MB（{3}%）  {4} KB/s' -f $What,
                           [Math]::Round($done / 1MB, 1), [Math]::Round($total / 1MB, 1),
                           [int](100 * $done / $total), $kbs
                } else {
                    $msg = '    · {0} {1} MB  {2} KB/s' -f $What, [Math]::Round($done / 1MB, 1), $kbs
                }
                Write-Host $msg -ForegroundColor DarkGray
            }
        }
    } finally {
        $out.Close(); $in.Close(); $resp.Close()
    }
    return $done
}

# ---- 自更新（#28：双击即升级）----------------------------------------------
# zip 里没有 data/.venv/logs/.env（都不在 git 里），覆盖天然不伤用户数据与 Key；
# data/ 即便出现也显式跳过（第二道防线）。逐项替换（删旧拷新），新版本删掉的
# 文件不会留尸体；被占用的文件（如正在运行的入口脚本）单独跳过，下次启动补齐。
# 任何失败都只提示、绝不阻断本次启动——升级永远不能把能跑的版本弄坏。
function Update-PrivaGlot {
    if ($NoUpdate -or -not $UpdateManifestUrl) { return }
    $verPath = Join-Path $Root 'VERSION'
    if (-not (Test-Path -LiteralPath $verPath)) { return }
    $local = ''
    try { $local = ([IO.File]::ReadAllText($verPath)).Trim() } catch { return }
    if ($local -notmatch '^[0-9]+(\.[0-9]+)+$') { return }

    $m = $null
    try {
        $req = [System.Net.HttpWebRequest]::Create($UpdateManifestUrl)
        $req.Timeout = 10000; $req.ReadWriteTimeout = 10000
        $req.UserAgent = 'PrivaGlot-bootstrap'
        $resp = $req.GetResponse()
        $body = (New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)).ReadToEnd()
        $resp.Close()
        $m = $body | ConvertFrom-Json
    } catch {
        Note "检查更新失败（离线?），继续用当前版本 $local。"
        return
    }
    if (-not ($m -and $m.version -and $m.url -and $m.sha256)) { return }
    $newer = $false
    try { $newer = [version]$m.version -gt [version]$local } catch { return }
    if (-not $newer) { return }

    Note "发现新版本 $($m.version)（当前 $local），正在升级 ..."
    $zipPath = Join-Path $env:TEMP 'privaglot-update.zip'
    $extDir  = Join-Path $env:TEMP 'privaglot-update'
    try {
        $null = Get-FileWithProgress -Url $m.url -OutFile $zipPath -What '下载更新'
        $got = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
        if ($got -ne ('' + $m.sha256).ToLower()) { throw "sha256 不符（期望 $($m.sha256)，实得 $got）" }
        if (Test-Path -LiteralPath $extDir) { Remove-Item -LiteralPath $extDir -Recurse -Force }
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extDir -Force
        $src = Join-Path $extDir 'PrivaGlot'
        if (-not (Test-Path -LiteralPath $src)) { throw '更新包结构不对（缺顶层 PrivaGlot 目录）' }

        $locked = @()
        Get-ChildItem -LiteralPath $src | ForEach-Object {
            if ($_.Name -eq 'data') { return }
            $dst = Join-Path $Root $_.Name
            try {
                if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }
                Copy-Item -LiteralPath $_.FullName -Destination $dst -Recurse -Force
            } catch {
                $locked += $_.Name
            }
        }
        Write-Host ("    √ 已升级到 {0}（数据与 Key 原样保留，新代码本次启动即生效）" -f $m.version) -ForegroundColor Green
        if ($locked) {
            Note ("个别文件被占用暂未替换（{0}），下次启动自动补齐。" -f ($locked -join '、'))
        }
    } catch {
        Warn "自动更新没有完成：$($_.Exception.Message)"
        Warn "继续用当前版本 $local，不影响本次启动。"
    } finally {
        Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $extDir) { Remove-Item -LiteralPath $extDir -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# ---- .env 读写 ------------------------------------------------------------
# .env 的运行期改动（网页里填 Key）由 app\config.py 负责，脚本这边不碰；
# 例外是部署期的一次性补齐：飞书凭据（扫码创建回填或手动粘贴）、FEISHU_DOMAIN、
# MiMo Key。策略与 config._persist_env 一致：同名替换、缺的追加、注释保留。
function Set-EnvValues([hashtable]$Values) {
    $lines = @()
    if (Test-Path -LiteralPath $EnvPath) {
        $lines = [System.IO.File]::ReadAllLines($EnvPath)
    } elseif (Test-Path -LiteralPath $EnvExample) {
        $lines = [System.IO.File]::ReadAllLines($EnvExample)
    }
    $remaining = New-Object System.Collections.Hashtable $Values
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        $t = $line.Trim()
        if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
            $name = $t.Split('=', 2)[0].Trim()
            if ($remaining.Contains($name)) {
                $out.Add("$name=$($remaining[$name])")
                $remaining.Remove($name)
                continue
            }
        }
        $out.Add($line)
    }
    foreach ($name in @($remaining.Keys)) {
        $out.Add("$name=$($remaining[$name])")
    }
    [System.IO.File]::WriteAllLines($EnvPath, $out)
}

# 只读地解析 .env（与 setup\check_env.py 的读法一致），用来判断还缺哪些配置，
# 缺什么才引导什么 —— 不让用户被用不上的提问打断。
function Get-EnvMap {
    $map = @{}
    if (Test-Path -LiteralPath $EnvPath) {
        foreach ($line in [System.IO.File]::ReadAllLines($EnvPath)) {
            $t = $line.Trim()
            if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
                $kv = $t.Split('=', 2)
                $map[$kv[0].Trim()] = $kv[1].Trim()
            }
        }
    }
    return $map
}

# ---- Python / ffmpeg 探测与安装 -------------------------------------------
function Find-Python {
    $candidates = @(
        [pscustomobject]@{ Exe = 'py';      Args = @('-3.13') },
        [pscustomobject]@{ Exe = 'py';      Args = @('-3.12') },
        [pscustomobject]@{ Exe = 'py';      Args = @('-3.11') },
        [pscustomobject]@{ Exe = 'python';  Args = @() },
        [pscustomobject]@{ Exe = 'python3'; Args = @() }
    )
    foreach ($c in $candidates) {
        $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # WindowsApps 里的 python.exe 是跳转应用商店的占位符，不是真的解释器
        if ($cmd.Source -like '*\WindowsApps\*') { continue }

        $argv = $c.Args + @('--version')
        $out = ''
        try { $out = (& $c.Exe @argv 2>&1 | Out-String) } catch { continue }
        if ($out -match 'Python (\d+)\.(\d+)\.') {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 11) {
                return [pscustomobject]@{
                    Exe = $c.Exe; Args = $c.Args; Version = "$major.$minor"
                }
            }
        }
    }
    # PATH 里没有，但也许是上一次启动用官方安装包装好的（见 Install-Python）
    return (Find-PythonIn $PythonBases)
}

# 在给定目录里递归找一个真的能跑的 python.exe。
# 只在装完之后用：新装的 Python 进不了当前进程的 PATH，只能按目录找。
function Find-PythonIn {
    param([string[]]$Bases)
    foreach ($base in $Bases) {
        if (-not (Test-Path -LiteralPath $base)) { continue }
        $found = Get-ChildItem -LiteralPath $base -Filter 'python.exe' -Recurse -ErrorAction SilentlyContinue |
                 Where-Object { $_.DirectoryName -notlike '*\Lib\venv\*' } |
                 Select-Object -First 1
        if (-not $found) { continue }
        $out = (& $found.FullName --version 2>&1 | Out-String)
        if ($out -match 'Python (\d+)\.(\d+)\.' -and [int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11) {
            return [pscustomobject]@{
                Exe = $found.FullName; Args = @(); Version = "$($Matches[1]).$($Matches[2])"
            }
        }
    }
    return $null
}

function Install-Python {
    # 路线一：winget。有就用它，装不上也不打紧，下面还有官方安装包兜底。
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Note '正在用 winget 安装 Python 3.12，请稍候（约 1-2 分钟）...'
        $null = Invoke-Native 'winget' @(
            'install', '--exact', '--id', 'Python.Python.3.12', '--source', 'winget',
            '--accept-source-agreements', '--accept-package-agreements'
        )
        $py = Find-PythonIn $PythonBases
        if ($py) { return $py }
        Warn 'winget 没能装上 Python，改用官方安装包重试 ...'
    } else {
        # 沙盒、LTSC、Server、被裁掉 Store / App Installer 的镜像都会走到这里
        Note '这台电脑上找不到 winget，改用官方安装包安装 Python ...'
    }

    # 路线二：从 python.org 直接下官方安装包，静默装。
    #   InstallAllUsers=0 只装给当前用户，不会弹 UAC —— 零基础用户不用点任何东西；
    #   装完落在 %LOCALAPPDATA%\Programs\Python，正是 Find-PythonIn 会去找的地方。
    $arch = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { 'win32' }
    $name = "python-$PythonFallbackVersion-$arch.exe"
    $url  = "https://www.python.org/ftp/python/$PythonFallbackVersion/$name"
    $exe  = Join-Path $env:TEMP "privaglot-$name"

    Note "正在下载 Python $PythonFallbackVersion 安装包（约 26 MB）..."
    try {
        $null = Get-FileWithProgress -Url $url -OutFile $exe -What '下载 Python'
    } catch {
        Warn "下载失败：$($_.Exception.Message)"
        return $null
    }

    Note '正在静默安装 Python（约 1-2 分钟，屏幕上不会有新窗口）...'
    $rc = Invoke-NativeTicking -Exe $exe -Arguments @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
        'Include_launcher=1', 'Include_test=0', 'Include_pip=1'
    ) -What '安装 Python'
    Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
    if ($rc -ne 0) { Warn "官方安装包退出码为 $rc，继续按结果判断。" }

    return (Find-PythonIn $PythonBases)
}

function Find-Ffmpeg {
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $local = Join-Path $Root 'tools\ffmpeg\bin\ffmpeg.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    return $null
}

# 用 pip 取 ffmpeg：imageio-ffmpeg 这个轮子里自带一个静态的 ffmpeg.exe
# （实测 win_amd64 轮子里的 ffmpeg-win-x86_64-v7.1.exe，就是 gyan.dev 的官方构建）。
# 选它是因为它走 PyPI —— 和后面装依赖完全同一条道，pip 能通它就能通；
# 轮子只有 30 MB，比官方那个 111 MB 的包小得多，国内一般一分钟内就下来了。
function Install-FfmpegFromPypi {
    if (-not ($py -and $py.Exe)) { return $null }

    $target = Join-Path $env:TEMP 'privaglot-ffmpeg-wheel'
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
    }

    Note '正在通过 pip 获取 ffmpeg（约 30 MB，走 PyPI）...'
    $pipArgs = @(
        '-m', 'pip', 'install', '--no-deps', '--target', $target,
        'imageio-ffmpeg', '--disable-pip-version-check'
    )
    $rc = Invoke-NativeTicking -Exe $py.Exe -Arguments $pipArgs -What '下载并安装 ffmpeg'
    if ($rc -ne 0) {
        Warn '默认源失败，改用清华镜像重试 ...'
        $rc = Invoke-NativeTicking -Exe $py.Exe `
             -Arguments ($pipArgs + @('-i', 'https://pypi.tuna.tsinghua.edu.cn/simple')) `
             -What '下载并安装 ffmpeg（清华镜像）'
    }
    if ($rc -ne 0) {
        Warn 'pip 没能取到 ffmpeg。'
        return $null
    }

    $exe = Get-ChildItem -LiteralPath $target -Recurse -Filter 'ffmpeg*.exe' -ErrorAction SilentlyContinue |
           Where-Object { $_.DirectoryName -like '*\binaries' } |
           Select-Object -First 1
    if (-not $exe) {
        Warn 'pip 装完了，但里面没有找到 ffmpeg.exe。'
        return $null
    }

    $targetBin = Join-Path $Root 'tools\ffmpeg\bin'
    New-Item -ItemType Directory -Force -Path $targetBin | Out-Null
    $final = Join-Path $targetBin 'ffmpeg.exe'
    Copy-Item -LiteralPath $exe.FullName -Destination $final -Force
    if ($target -like "$env:TEMP\*" -and (Test-Path -LiteralPath $target)) {
        Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path -LiteralPath $final)) { return $null }

    # 拿到的东西必须真的能跑，否则宁可用下一条路线
    $out = (& $final -version 2>&1 | Out-String)
    if ($out -notmatch 'ffmpeg version') {
        Warn "取到的 ffmpeg 跑不起来：$(($out -split "`n")[0].Trim())"
        return $null
    }
    return $final
}

function Install-Ffmpeg {
    # 三条路线，按「快且可靠」的顺序试：
    #   1. pip 装 imageio-ffmpeg（走 PyPI，30 MB）
    #   2. winget（有才走；它内部同样是去第三方站点下那个 111 MB 的包，未必快）
    #   3. 直接下载官方免安装包（111 MB，有些网络下只有几十 KB/s，所以放最后）
    $ff = Install-FfmpegFromPypi
    if ($ff) { return $ff }

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Note '正在用 winget 安装 ffmpeg ...'
        $null = Invoke-Native 'winget' @(
            'install', '--exact', '--id', 'Gyan.FFmpeg', '--source', 'winget',
            '--accept-source-agreements', '--accept-package-agreements'
        )
        $pkgRoot = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
        if (Test-Path -LiteralPath $pkgRoot) {
            $found = Get-ChildItem -LiteralPath $pkgRoot -Filter 'ffmpeg.exe' -Recurse -ErrorAction SilentlyContinue |
                     Where-Object { $_.DirectoryName -like '*\bin' } | Select-Object -First 1
            if ($found) { return $found.FullName }
        }
    }

    Note '正在下载 ffmpeg 免安装版（111 MB，视网络可能要十几分钟）...'
    try {
        $zip  = Join-Path $env:TEMP 'privaglot-ffmpeg.zip'
        $dest = Join-Path $Root 'tools\ffmpeg-unpack'
        $url  = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
        $null = Get-FileWithProgress -Url $url -OutFile $zip -What '下载 ffmpeg'
        Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
        $exe = Get-ChildItem -LiteralPath $dest -Filter 'ffmpeg.exe' -Recurse |
               Select-Object -First 1
        if (-not $exe) { return $null }

        # 只把 bin 目录拷进项目，之后每次启动都用 tools\ffmpeg\bin\ffmpeg.exe
        $targetBin = Join-Path $Root 'tools\ffmpeg\bin'
        New-Item -ItemType Directory -Force -Path $targetBin | Out-Null
        Copy-Item -Path (Join-Path $exe.Directory.FullName '*') -Destination $targetBin -Recurse -Force
        if ($dest -like "$Root\*" -and (Test-Path -LiteralPath $dest)) {
            Remove-Item -LiteralPath $dest -Recurse -Force
        }
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        $final = Join-Path $targetBin 'ffmpeg.exe'
        if (Test-Path -LiteralPath $final) { return $final }
    } catch {
        Warn "下载或解压 ffmpeg 失败：$($_.Exception.Message)"
    }
    return $null
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

# 屏幕上的每一行同时落一份到 logs\setup.log：用户遇到问题时可以直接把这个文件
# 发给作者，不用截图；排查新机器 / 沙盒的环境差异时它也比截图有用得多。
$TranscriptOn = $false
try {
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
    Start-Transcript -LiteralPath $LogPath -Force | Out-Null
    $TranscriptOn = $true
} catch { }

Write-Host ''
Write-Host '  PrivaGlot 部署' -ForegroundColor Cyan
Write-Host '  ------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host "  项目目录：$Root" -ForegroundColor DarkGray
if ($TranscriptOn) { Write-Host "  安装日志：$LogPath" -ForegroundColor DarkGray }

# ---- 自更新（先于一切环境准备：升级后的代码立即接管本次启动）----------------
Update-PrivaGlot
if ($SelfUpdateOnly) {
    Write-Host ''
    Write-Host '  仅更新模式完成。重新双击「本机部署.bat」即可启动。' -ForegroundColor Cyan
    exit 0
}

# ---- 0. 部署形态 -----------------------------------------------------------
# 核心依赖两种形态完全一样，差别只在最后一步跑哪个入口。形态由入口脚本
# 固定传入（本机部署.bat=web；feishu 已无入口脚本，仅手动传参）；直接跑本脚本且没传
# -Mode 时默认网页版。双通道模式已退役（2026-09-23）：两通道两个进程、
# 共享 data\ 但互不实时同步，同跑是测试期形态，不再由部署脚本提供。
$ValidModes = @('web', 'feishu')
if ($Mode -and ($ValidModes -notcontains $Mode.ToLower())) {
    Write-Host ''
    Bad "未知的 -Mode '$Mode'（可用：web / feishu；双通道模式已退役）"
    Read-Host '  按回车退出'
    exit 1
}
if (-not $Mode) { $Mode = 'web' }
$Mode = $Mode.ToLower()
$ModeLabel = @{ 'web' = '网页版'; 'feishu' = '飞书机器人' }[$Mode]
$Step4Title = "启动$ModeLabel"
Write-Host ''
Write-Host "  本次部署形态：$Step4Title" -ForegroundColor Cyan

try {
    # ---- 1. Python --------------------------------------------------------
    Step '检查 Python'
    $py = Find-Python
    if (-not $py) {
        Note '这台电脑上没有找到 Python 3.11 或更高版本。'
        $py = Install-Python
    }
    if (-not $py) {
        Bad '没能自动装上 Python。'
        Note '请手动到 https://www.python.org/downloads/ 下载安装（勾选 Add to PATH），'
        Note '装完后重新双击「本机部署.bat」即可。'
        Read-Host '  按回车退出'
        exit 1
    }
    StepDone "Python $($py.Version)（$($py.Exe)）"

    # ---- 2. ffmpeg --------------------------------------------------------
    Step '检查 ffmpeg（把录音转成可识别格式，必需）'
    $ffmpeg = Find-Ffmpeg
    if (-not $ffmpeg) { $ffmpeg = Install-Ffmpeg }
    if (-not $ffmpeg) {
        Bad '没能自动装上 ffmpeg。'
        Note '请到 https://www.gyan.dev/ffmpeg/builds/ 下载 release-essentials，'
        Note '解压后把其中的 bin 目录加入系统 PATH，再重新双击部署脚本。'
        Read-Host '  按回车退出'
        exit 1
    }
    StepDone "ffmpeg：$ffmpeg"
    # 项目自带的 ffmpeg 不在 PATH 里，启动前补上，子进程才找得到
    if ($ffmpeg -like "$Root\*") {
        $env:PATH = "$(Split-Path -Parent $ffmpeg);$env:PATH"
    }

    # ---- 3. 虚拟环境与依赖 -------------------------------------------------
    Step '准备 Python 依赖'
    if (Test-Path -LiteralPath $VenvPython) {
        $rc = Invoke-Native $VenvPython @('-c', 'import sys')
        if ($Reinstall -or $rc -ne 0) {
            Warn '虚拟环境不可用，正在重建 .venv（data\ 里的数据不受影响）...'
            if ($VenvDir -like "$Root\*" -and (Test-Path -LiteralPath $VenvDir)) {
                Remove-Item -LiteralPath $VenvDir -Recurse -Force
            }
        }
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Note '首次运行，正在创建虚拟环境 .venv ...'
        $rc = Invoke-Native $py.Exe ($py.Args + @('-m', 'venv', $VenvDir))
        if ($rc -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
            throw '创建虚拟环境失败。'
        }
    }
    Good '虚拟环境就绪'

    Note '正在安装依赖：第一次要下约 60 MB、装 60 个左右的小包，3～10 分钟都算正常。'
    Note '下面会持续有输出。如果某一段看起来停住了，那是在解压安装，请别关窗口。'
    $rc = Invoke-NativeTicking -Exe $VenvPython -Arguments @(
        '-m', 'pip', 'install', '-r', $Requirements, '--disable-pip-version-check'
    ) -What '安装依赖'
    if ($rc -ne 0) {
        Warn '默认源安装失败，改用清华镜像重试 ...'
        $rc = Invoke-NativeTicking -Exe $VenvPython -Arguments @(
            '-m', 'pip', 'install', '-r', $Requirements,
            '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple', '--disable-pip-version-check'
        ) -What '安装依赖（清华镜像）'
    }
    if ($rc -ne 0) {
        throw '依赖安装失败。请检查网络（或挂上代理）后重新双击部署脚本。'
    }
    StepDone '依赖安装完成'

    # 配置文件在这里备好就行，Key 不在这里问：启动后在浏览器里填。
    # 好处是粘贴变成 Ctrl+V、出错能当场重试，而且「装没装好」和「账号能不能用」
    # 是两件独立的事 —— 页面能打开，就说明部署这一步成功了。
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        Copy-Item -LiteralPath $EnvExample -Destination $EnvPath
        Note '已从 .env.example 准备出 .env（API Key 稍后在网页里填）'
    }

    # ---- 4. 启动 ----------------------------------------------------------
    if ($NoLaunch) {
        Write-Host "`n环境已就绪（形态：$ModeLabel）。" -ForegroundColor Cyan
        if ($Mode -eq 'web') { Write-Host '下次双击「本机部署.bat」即可开始。' -ForegroundColor Cyan }
        Write-Host "或运行 powershell -File setup\bootstrap.ps1 -Mode $Mode 即可开始。" -ForegroundColor Cyan
        exit 0
    }

    Step $Step4Title

    # 飞书形态先补配置再自检：缺什么当场引导补什么，别等机器人启动才报错。
    if ($Mode -ne 'web') {
        $envMap    = Get-EnvMap
        $feishuId  = ('' + $envMap['FEISHU_APP_ID']).Trim()
        $feishuSec = ('' + $envMap['FEISHU_APP_SECRET']).Trim()

        # ---- 飞书应用凭据：扫码一键创建优先，手动粘贴兜底 ------------------
        if (-not $feishuId -or -not $feishuSec) {
            Write-Host ''
            Write-Host '  飞书应用凭据还没配置（FEISHU_APP_ID / FEISHU_APP_SECRET）。' -ForegroundColor White
            Write-Host '    [1] 手机飞书扫码，自动创建应用并回填（推荐，约 1 分钟）'
            Write-Host '        机器人能力 / 消息权限 / 长连接事件订阅由飞书自动配好，'
            Write-Host '        全程不需要去开放平台手动建应用（手机需装好飞书并登录）。'
            Write-Host '        前提：账号名下要有自己创建的企业——纯个人账号'
            Write-Host '        （豆包账号）部署成功也触达不了你。先创建企业：'
            Write-Host '        左上角头像 → 登录更多账号 → 创建企业账号'
            Write-Host '        （免费 1 分钟），再扫；别用公司号。脚本会自动'
            Write-Host '        探测触达链路并现场引导。'
            Write-Host '    [2] 已有应用 —— 手动粘贴 App ID / App Secret'
            Write-Host '    [3] 跳过（本次机器人起不来；之后可重跑本脚本再配）'
            $choice = Read-Host '  输入 1/2/3 后回车'
            if (-not $choice) { $choice = '1' }
            $choice = $choice.Trim()

            if ($choice -eq '1') {
                # 扫码创建：SDK 直接把 App ID / Secret 回传进这个 JSON 文件，
                # 用户不需要复制粘贴任何东西。
                $qrOut = Join-Path $env:TEMP 'privaglot-feishu-qr.json'
                Remove-Item -LiteralPath $qrOut -Force -ErrorAction SilentlyContinue
                $rc = Invoke-Native $VenvPython @(
                    (Join-Path $ScriptDir 'feishu_qr_register.py'), '--out', $qrOut
                )
                if ($rc -eq 0 -and (Test-Path -LiteralPath $qrOut)) {
                    $cred = $null
                    try {
                        $cred = Get-Content -LiteralPath $qrOut -Raw -Encoding UTF8 | ConvertFrom-Json
                    } catch { }
                    if ($cred -and $cred.client_id -and $cred.client_secret) {
                        $values = @{
                            FEISHU_APP_ID     = $cred.client_id
                            FEISHU_APP_SECRET = $cred.client_secret
                        }
                        if ($cred.tenant_brand -eq 'lark') {
                            $values.FEISHU_DOMAIN = 'https://open.larksuite.com'
                        }
                        if ($cred.open_id) {
                            # 扫码人就是管理员：状态/换Key/教材管理只认 TA
                            $values.FEISHU_ADMIN_OPEN_ID = $cred.open_id
                        }
                        Set-EnvValues $values
                        Good "飞书应用已创建，凭据已写入 .env（App ID: $($cred.client_id)）"
                        $feishuId = $cred.client_id
                    }
                }
                if (-not $feishuId) {
                    Warn '扫码创建没有完成（原因见上）。'
                    $appId = (Read-Host '  可改粘贴已有应用的 FEISHU_APP_ID（cli_ 开头，回车跳过）').Trim()
                    if ($appId) {
                        $secret = (Read-Host '  粘贴 FEISHU_APP_SECRET').Trim()
                        if ($secret) {
                            Set-EnvValues @{ FEISHU_APP_ID = $appId; FEISHU_APP_SECRET = $secret }
                            Good '飞书凭据已写入 .env'
                        }
                    }
                }
            }
            elseif ($choice -eq '2') {
                $appId = (Read-Host '  粘贴 FEISHU_APP_ID（cli_ 开头，回车跳过）').Trim()
                if ($appId) {
                    $secret = (Read-Host '  粘贴 FEISHU_APP_SECRET').Trim()
                    if ($secret) {
                        Set-EnvValues @{ FEISHU_APP_ID = $appId; FEISHU_APP_SECRET = $secret }
                        Good '飞书凭据已写入 .env'
                    }
                }
            }
        }

        # ---- MiMo Key：脚本不再注入（2026-09-23 用户裁定）------------------
        # 配置入口收敛到产品界面：飞书=机器人聊天里的 Key 引导卡（无 Key 时
        # 冷启动/开课闸门自动推送，表单卡提交，验证通过立即生效）。
        $envMap = Get-EnvMap
        $mimo = ('' + $envMap['MIMO_API_KEY']).Trim()
        if (-not $mimo -or $mimo -eq 'your_mimo_api_key_here') {
            Note 'MiMo API Key 还没填——启动后在飞书里给机器人发任意消息，'
            Note '会收到 Key 引导卡，粘贴 Key 验证通过立即生效（无需重启）。'
        }

        $chk = Join-Path $ScriptDir 'check_env.py'
        $rc = Invoke-Native $VenvPython @($chk, '--mode', 'feishu')
        if ($rc -ne 0) {
            Bad '飞书形态自检未通过（看上面的 FAIL 行）。'
            Note '手动配置步骤见 docs\飞书接入.md；网页版不受影响——关掉本窗口，'
            Note '双击「本机部署.bat」即可照常上课。'
            Read-Host '  按回车退出'
            exit 1
        }
    }

    if ($Mode -eq 'web') {
        $busy = $null
        try {
            $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        } catch { }
        if ($busy) {
            Warn "端口 $Port 已被占用，多半是上一次的窗口还开着。"
            Warn '请先关掉那个窗口，再重新双击「本机部署.bat」。'
            Read-Host '  按回车退出'
            exit 1
        }

        Write-Host ''
        Write-Host "  访问地址：http://localhost:$Port" -ForegroundColor Green
        Write-Host '  第一次打开会让你在网页里填小米 API Key —— 这个黑窗口里不用敲任何东西。' -ForegroundColor Gray
        Write-Host '  服务就绪后浏览器会自动打开；若一直没打开，手动输入上面的地址即可。' -ForegroundColor Gray
        Write-Host '  停止服务：直接关闭这个窗口。' -ForegroundColor Gray
        Write-Host '  数据（学生档案、会话记录、教材）都在 data\ 目录里，不会离开这台电脑。' -ForegroundColor DarkGray
        Write-Host ''

        Set-Location -LiteralPath $Root
        # 浏览器 opener 不能「睡几秒盲开」：首次部署时服务起得慢（冷磁盘缓存、杀软
        # 首次扫描新装的 Python、reload 的双进程启动——实测热启动也要 7 秒上下），
        # 浏览器一跑赢服务，用户就先看到「无法访问此网站」的死页，紧接着又弹防火墙
        # 警报，看起来就像部署失败。所以在这里轮询本机端口，服务真正能连上了才打开
        # 页面。120 秒上限只是兜底：到点还连不上，说明服务多半已经在黑窗口里报错，
        # 此时再开浏览器也只是再送出一个死页，所以直接放弃，留给用户手动输地址。
        $openCmd = "`$deadline = (Get-Date).AddSeconds(120); " +
                   "while ((Get-Date) -lt `$deadline) { " +
                   "try { `$c = New-Object Net.Sockets.TcpClient; " +
                   "`$c.Connect('127.0.0.1', $Port); `$c.Close(); " +
                   "Start-Process 'http://localhost:$Port'; exit 0 } " +
                   "catch { Start-Sleep -Milliseconds 500 } }; exit 1"
        Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
            '-NoProfile', '-WindowStyle', 'Hidden', '-Command', $openCmd
        )
        $null = Invoke-Native $VenvPython @((Join-Path $Root 'run.py'))
    }
    else {
        # 飞书：机器人跑在当前窗口。长连接是出站连接，
        # 不需要公网端口、域名、HTTPS 或备案。
        Write-Host ''
        Write-Host '  飞书机器人已就绪（长连接模式，无需公网端口）。' -ForegroundColor Green
        Write-Host '  若收到的卡片按钮点了没反应，等几秒再点一次' -ForegroundColor Gray
        Write-Host '  （长连接刚建立或刚重连时，按钮回调可能还没接上）。' -ForegroundColor Gray
        Write-Host '  先自己验一下：用你（扫码建应用的人）的飞书给机器人发一条消息，' -ForegroundColor Gray
        Write-Host '  没配 Key 会先收到 Key 引导卡（粘贴即生效）；之后点选学生、' -ForegroundColor Gray
        Write-Host '  发语音即代表全链路通畅，然后再让学生加入。' -ForegroundColor Gray
        Write-Host '  学生端：手机飞书里给机器人发消息，首条会收到学生选择卡——' -ForegroundColor Gray
        Write-Host '  先选学生（或新建）再上课。' -ForegroundColor Gray
        Write-Host '  文字指令：上课 / 选课 / 自由对话 / 下课 / 进度 / 学生' -ForegroundColor Gray
        Write-Host '  停止机器人：直接关闭这个窗口。' -ForegroundColor Gray
        Write-Host '  详细说明与家长侧指引：docs\飞书接入.md' -ForegroundColor DarkGray
        Write-Host ''

        Set-Location -LiteralPath $Root
        $null = Invoke-Native $VenvPython @((Join-Path $Root 'run_feishu.py'))
    }

    Write-Host ''
    Write-Host ("  服务已停止。本次从准备到退出共 {0}。" -f `
        (Format-Elapsed ((Get-Date) - $script:RunStart))) -ForegroundColor DarkGray
}
catch {
    Write-Host ''
    Bad $_.Exception.Message
    Note '把上面几行截图、或者把 logs\setup.log 整个发给项目作者即可。'
    Note ("本次运行共 {0}。" -f (Format-Elapsed ((Get-Date) - $script:RunStart)))
    Read-Host '  按回车退出'
    exit 1
}
finally {
    # 前面每条失败分支都是 exit，不过 finally 在 exit 时同样会执行，日志不会半截断掉
    if ($TranscriptOn) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}
