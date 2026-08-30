# Host Camera Packet Receiver 软件配置要求

本文档是从零克隆 Host 仓库时的软件环境合同。离线 PCAP 回放、实时抓包、自动测试、查看图片和运行标定所需的软件不同，因此不能把它们笼统写成一组“必须全部安装”的依赖。

## 术语

- **必需**：缺少该项时，指定流程不能运行。
- **阶段限定**：只在表中指定的阶段安装。
- **已观察**：2026-08-30 收官环境实际存在的版本，是复现实验证据，但不自动等于最低版本。
- **最低版本未验证**：仓库中没有足够证据给出安全数字，禁止凭经验补一个看似合理的版本号。

## 已验证的收官环境快照

| 软件 | 仓库要求 | 收官环境观察值 | 使用范围 |
|---|---:|---:|---|
| Windows | 文档化的 Npcap/PowerShell 实时流程必需 | Windows NT 10.0.26200.0，64 位 | 实时采集及现有包装脚本 |
| Windows PowerShell | 公开 runbook 要求 5.1 或 PowerShell 7 | 5.1.26100.9168 Desktop | `scripts_ps/` |
| Git | 克隆、溯源、检查工作树必需；最低版本未验证 | 2.54.0.windows.1 | 仓库治理 |
| CPython | **3.10 或更高** | 3.14.6，64 位 | 全部 Python 入口 |
| Scapy | `requirements-live.txt` 声明但未锁版本 | 2.7.0 | 实时采集 |
| pytest | `requirements-live.txt` 声明但未锁版本 | 9.1.1 | 回归测试 |
| NumPy | `>=1.24` | 2.5.1 | 标定 |
| OpenCV Python headless | `>=4.8,<5` | 包 4.14.0.94；`cv2` 4.14.0 | 标定 |
| Npcap | Windows 实时抓包必需；精确/最低版本未验证 | 未保留安装版本 | 仅实时采集 |
| Python Tk/Tcl 支持 | 仅查看器必需；最低版本未验证 | 未锁定 | 仅查看器 |
| Wireshark | 可选；最低版本未验证 | 未锁定 | 检查 EtherType `0x88B5` |

Python 3.10 下限来自真实源码：项目使用 `X | None` 这类 PEP 604 类型语法。OpenCV `<5` 是有意约束，项目回归记录已经发现 OpenCV 5.0.0.93 的 fisheye 多视图问题。Scapy、pytest、Npcap、Git 和 PowerShell 没有仓库可证明的数字下限，在新的兼容性测试建立前不能把观察版本写成强制最低版本。

## 按任务拆分依赖

| 任务 | Python | 额外软件/包 | 是否需要 Npcap/抓包权限 |
|---|---|---|---|
| 离线 PCAP 回放与标准库解析 | 必需 | 仓库源码；不需要实时驱动 | 否 |
| 实时抓包 | 必需 | `requirements-live.txt`、Npcap | 是 |
| 自动测试 | 必需 | `requirements-live.txt` 中的 pytest | 合成/离线测试不需要 |
| CSV/PGM 接收输出 | 必需 | 从网卡接收时需要实时依赖 | 实时采集需要 |
| 相机图片查看器 | 必需 | 带 Tk/Tcl 的 Python | 查看归档图片不需要 |
| 内参/外参标定 | 必需 | `requirements-calibration.txt` | PGM 已采集后不需要 |
| Python 外部包检查 | 可选 | Wireshark | 通常配合 Npcap |

## 干净安装

使用新克隆和仓库内 `.venv`。只替换尖括号路径。

```powershell
$host = '<HOST_REPOSITORY_ROOT>'
$basePython = 'C:\Users\<USER>\AppData\Local\Programs\Python\Python314\python.exe'

if (!(Test-Path -LiteralPath $host -PathType Container)) {
  throw "Host 仓库不存在：$host"
}
if (!(Test-Path -LiteralPath $basePython -PathType Leaf)) {
  throw "Python 可执行文件不存在：$basePython"
}

Set-Location $host
& $basePython -m venv .venv
$python = Join-Path $host '.venv\Scripts\python.exe'
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $host 'requirements-live.txt')
& $python -m pip install -r (Join-Path $host 'requirements-calibration.txt')
```

示例 Python 路径只是占位符。用 `Get-Command python -All` 或 Python launcher 找到解释器，并把最终路径写入 run manifest。公开自动化脚本不得复制收官电脑的绝对 Python 路径。

## PRECHECK 与 dry-run

采集数据之前先运行下列检查。先把结果放进数组，可以避免依赖缺失时触发 PowerShell 空管道问题。

```powershell
$host = '<HOST_REPOSITORY_ROOT>'
$python = Join-Path $host '.venv\Scripts\python.exe'
$requiredFiles = @(
  'requirements-live.txt'
  'requirements-calibration.txt'
  'run_receiver.ps1'
  'taxi_receiver\cli.py'
)
$inventory = @(
  foreach ($relativePath in $requiredFiles) {
    $fullPath = Join-Path $host $relativePath
    [pscustomobject]@{
      Path = $fullPath
      Exists = Test-Path -LiteralPath $fullPath -PathType Leaf
    }
  }
)
$inventory | Format-Table -AutoSize
if (@($inventory | Where-Object { -not $_.Exists }).Count -gt 0) {
  throw 'Host 软件预检失败：缺少必需文件'
}

& $python --version
& $python -c "import sys,scapy,pytest,numpy,cv2; print(sys.executable); print('scapy',scapy.__version__); print('pytest',pytest.__version__); print('numpy',numpy.__version__); print('cv2',cv2.__version__)"
```

标定包装脚本支持 `-PreflightOnly` 和 `-WhatIf`；正式长时间求解或建立审计目录前必须先使用它们。实时接收机本身是持续运行进程，没有等价 dry-run；它的安全预检是下一节的接口枚举加一个全新输出目录。

## 实时抓包验证

Npcap 是系统驱动，不是 Python wheel。应独立安装；本机策略需要时，以拥有抓包权限的 PowerShell 打开。然后枚举接口：

```powershell
$host = '<HOST_REPOSITORY_ROOT>'
$python = Join-Path $host '.venv\Scripts\python.exe'
Set-Location $host
& $python -m taxi_receiver.cli --list
if ($LASTEXITCODE -ne 0) {
  throw "Npcap/接口预检失败，退出码：$LASTEXITCODE"
}
```

必须逐字复制输出的 `\Device\NPF_{GUID}`。不可保留 `<actual interface>`，也不可复用已经变化的旧 GUID。Windows 错误 123 表示 adapter 字符串无效；命令看似正常但 ingress 为零时，应依次检查网卡是否选错、权限、物理链路和 Npcap，而不是先修改解析器。

## 测试与标定验证

```powershell
$host = '<HOST_REPOSITORY_ROOT>'
$python = Join-Path $host '.venv\Scripts\python.exe'
Set-Location $host

& $python -m pytest -q
$testExit = $LASTEXITCODE
if ($testExit -ne 0) {
  throw "Host 回归测试失败，退出码：$testExit"
}

& (Join-Path $host 'scripts_ps\calibration\run_intrinsic_calibration.ps1') `
  -CameraId 0 `
  -TrainingRoot '<TRAINING_CAM0_ROOT>' `
  -HoldoutV1Root '<HOLDOUT_V1_CAM0_ROOT>' `
  -HoldoutV2Root '<HOLDOUT_V2_CAM0_ROOT>' `
  -OutputRoot '<FRESH_INTRINSIC_AUDIT_ROOT>' `
  -PythonExe $python `
  -PreflightOnly `
  -WhatIf
```

执行前必须替换每个尖括号路径。用 `Get-Help .\scripts_ps\calibration\run_intrinsic_calibration.ps1 -Full` 和中英文标定 runbook 读取完整的当前参数；对 `run_extrinsic_calibration.ps1` 做同样预检。生成 JSON 不等于通过，必须同时读取 `status`、质量失败项和包装脚本退出码。

## 文件系统与证据要求

- 每次创建新的时间戳 run 目录。正式包装脚本拒绝非空输出根；不得靠删除历史证据绕过该保护。
- `.venv/`、缓存、PGM/PCAP/CSV、K/D、R/t 和接口 GUID 默认不进入公开仓库，除非某个合成 fixture 已经被单独审查批准。
- 当前仓库没有建立 RAM、CPU、可用磁盘或 NIC 吞吐的数字下限。应记录本次电脑、网卡、队列参数、packet counters、剩余磁盘和队列峰值，而不是声称一个无证据的最低配置。
- `run_manifest.json` 应把 run ID 与 Git HEAD/dirty 状态、Python 可执行文件及包版本、接口 GUID、采集根目录、camera IDs、相关 K/D 和 R/t 哈希、主要参数及最终状态绑定起来。

## 许可证边界

安装依赖不会使它们自动采用本仓库的 BSD 3-Clause。重新分发前阅读 [第三方声明](THIRD_PARTY_NOTICES.zh-CN.md)。Npcap 尤其受自身条款约束；本机安装了驱动，不代表公开仓库可以复制其安装包、DLL 或 SDK。
