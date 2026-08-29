# 二值圆环相机内参 / 双目标定复刻手册

本文是 Host Camera Packet Receiver 的标定入口。网络接收负责把 EtherType
`0x88B5` 的 128-byte 行包重组为 PGM；标定程序只读取已经落盘的 PGM 和同名
JSON 元数据，不参与实时抓包。因此：接收成功、单相机内参通过、双目外参通过
是三个独立结论，不能跨层提升证据。

## 1. 软件边界与版本

标定依赖与 live receiver 分开维护：

```powershell
$repo = 'D:\prg\prg_cam_host'       # 修改为本机 Host 仓库
$python = Join-Path $repo '.venv\Scripts\python.exe'

Set-Location $repo

& $python -m pip install `
  -r .\requirements-live.txt `
  -r .\requirements-calibration.txt

& $python -c `
  "import cv2,numpy; print('OpenCV',cv2.__version__,'NumPy',numpy.__version__)"
```

`requirements-calibration.txt` 要求 NumPy `>=1.24`，OpenCV
`>=4.8,<5`。这是因为代码记录了 OpenCV 5.0.0.93 的多视图
`fisheye.calibrate` 回归风险。发布前实测环境为 Python 3.14.6、NumPy 2.5.1、
OpenCV 4.14.0；这些是一次环境证据，不是强制锁死的唯一版本。

先执行 CLI smoke test：

```powershell
$entryPoints = @(
  'preflight_calibration_frames.py',
  'calibrate_binary_camera.py',
  'validate_binary_calibration.py',
  'build_stereo_pairs.py',
  'calibrate_binary_stereo.py',
  'validate_binary_extrinsics.py'
)

foreach ($entry in $entryPoints) {
  & $python (Join-Path $repo $entry) --help
  if ($LASTEXITCODE -ne 0) {
    throw "CLI smoke test failed: $entry"
  }
}
```

## 2. 数据目录不变量

每一次 Training、Holdout V1、Holdout V2 都必须使用不同目录。不要把新图片继续
写进旧目录，也不要在失败后覆盖原 audit。建议把数据放在仓库外：

```text
<DATA_ROOT>/
  intrinsic_cam0_train/<cam0 PGM + JSON>
  intrinsic_cam0_holdout_v1/<cam0 PGM + JSON>
  intrinsic_cam0_holdout_v2/<cam0 PGM + JSON>
  stereo_static/
    cam0/<PGM + JSON>
    cam1/<PGM + JSON>
  stereo_train/
    cam0/<PGM + JSON>
    cam1/<PGM + JSON>
  stereo_holdout_v1/cam0 + cam1
  stereo_holdout_v2/cam0 + cam1
```

PGM 是二值图像；同名 JSON 记录 frame/timestamp 等元数据。双目配对不能只靠相似
文件名，它需要同一接收 run 的时间信息。Training 与 Holdout 物理采集也必须独立，
否则 `training_overlap_pairs` 或相同姿态会造成伪验证。

## 3. 获取图片

### 3.1 列出 Npcap 接口

管理员 PowerShell：

```powershell
$repo = 'D:\prg\prg_cam_host'
$python = Join-Path $repo '.venv\Scripts\python.exe'

Set-Location $repo
& $python -m taxi_receiver.cli --list
```

从输出复制真实接口名称。不要把示例占位符原样传给 receiver：

```powershell
$interface = '\Device\NPF_{REPLACE-WITH-REAL-GUID}'
```

### 3.2 每个数据集启动一个全新接收目录

```powershell
$repo = 'D:\prg\prg_cam_host'
$dataRoot = 'D:\camera_runs'        # 修改为本机仓库外数据盘
$runId = Get-Date -Format 'yyyyMMdd_HHmmss'
$captureRoot = Join-Path $dataRoot "${runId}_stereo_train"
$python = Join-Path $repo '.venv\Scripts\python.exe'

if (Test-Path -LiteralPath $captureRoot) {
  throw "Capture root already exists: $captureRoot"
}
New-Item -ItemType Directory -Path $captureRoot | Out-Null

Set-Location $repo
.\run_receiver.ps1 `
  -Interface $interface `
  -ImagesRoot $captureRoot `
  -ExpectedRows 480 `
  -QueueDepth 65536 `
  -FrameOutputQueueDepth 256 `
  -CameraIds '0,1' `
  -SplitByCamera on `
  -ImagePolicy strict `
  -PublishFrames complete `
  -PublishImages process `
  -PublisherQueueDepth 256 `
  -SessionAudit off `
  -PythonExe $python
```

这里只使用 `run_receiver.ps1` 真实存在的参数。FPGA 仓库旧命令中的
`-PcapBufferSize`、`-CrcMode`、`-BitOrder` 或 `-CsvQueueDepth` 不是这个 Host
launcher 的 PowerShell 参数，不能复制到这里。

按 `Ctrl+C` 只停止接收窗口。最终判断查看 Final Report 中的：

- `Capture ingress` / `Matching Ethernet`；
- `Valid packets`；
- `Capture queue drops` / `Lane queue drops`；
- cam0/cam1 的 packet 与 complete frame 数；
- 输出目录中的 PGM 和 `rows_v2.csv`。

### 3.3 两个实时姿态窗口

在窗口 B 观察 cam0：

```powershell
$repo = 'D:\prg\prg_cam_host'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$captureRoot = '<与接收窗口完全相同的路径>'
$liveRoot = Join-Path $captureRoot '_live_audit'

New-Item -ItemType Directory -Force -Path $liveRoot | Out-Null

& $python (Join-Path $repo 'preflight_calibration_frames.py') `
  (Join-Path $captureRoot 'cam0') `
  --watch `
  --poll-interval 1 `
  --min-poses 15 `
  --zone-map `
  --report (Join-Path $liveRoot 'cam0_frames.csv')
```

窗口 C 把 `cam0` 改为 `cam1`，报告名改为 `cam1_frames.csv`。监视器显示的是
单相机 complete-grid / distinct-pose 数，不等于双目 `selected_pose_pairs`。

## 4. 单相机内参完整流程

### 4.1 采样要求

每个相机分别准备：

1. Training：至少 15 个明显不同姿态，覆盖中心、四角、俯仰和深度；
2. Holdout V1：独立重新采集，不继续写 Training；
3. Holdout V2：再次独立采集；
4. 严格模式下不要使用 recovered/zero-filled frame；
5. 圆环必须全部完整，靠近边沿但不能断裂。

“pose 数量”是图像平面中心位移和表观尺度变化的工程去重结果，不是物理世界里
移动次数的逐次计数。连续缓慢移动会产生很多帧，但可能只形成少量独立 pose。

### 4.2 先做不写盘检查

```powershell
$repo = 'D:\prg\prg_cam_host'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$dataRoot = 'D:\camera_runs'

$trainCam = Join-Path $dataRoot 'cam0_intrinsic_train\cam0'
$holdoutV1Cam = Join-Path $dataRoot 'cam0_intrinsic_holdout_v1\cam0'
$holdoutV2Cam = Join-Path $dataRoot 'cam0_intrinsic_holdout_v2\cam0'
$auditRoot = Join-Path $dataRoot 'audit\cam0_intrinsic_run01'

& (Join-Path $repo 'scripts_ps\run_intrinsic_calibration.ps1') `
  -CameraId 0 `
  -TrainingRoot $trainCam `
  -HoldoutV1Root $holdoutV1Cam `
  -HoldoutV2Root $holdoutV2Cam `
  -OutputRoot $auditRoot `
  -PythonExe $python `
  -FisheyeConstraint full `
  -MinPoses 15 `
  -MinViews 15 `
  -MinHoldoutViews 15 `
  -PreflightOnly
```

也可以用 `-WhatIf`。两者都检查 Python、OpenCV、三个输入目录、PGM 数量和输出
目录是否可用，但不会建立 audit 文件。

如果 Windows 提示“禁止运行脚本”，不要修改机器级 Execution Policy。使用仅对
本次进程生效的方式启动，并在后面补齐其余必填参数：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts_ps\run_intrinsic_calibration.ps1 `
  -PreflightOnly <其余必填参数>
```

### 4.3 正式执行

去掉 `-PreflightOnly`，其余参数保持不变。脚本依次执行：

```text
Training PGM
  → complete grid / pose preflight
  → selected_poses（每个 distinct pose 一张）
  → cv2.fisheye.calibrate
  → K/D JSON + per-view CSV
  → Holdout V1 固定 K/D 验证
  → Holdout V2 固定 K/D 验证
  → run_manifest.json
```

输出只有同时满足以下条件才是 PASS：

- intrinsic JSON 的 `quality.status == acceptable`；
- V1 `holdout_summary.json.status == pass`；
- V2 `holdout_summary.json.status == pass`；
- PowerShell 没有抛异常。

默认脚本显式使用源码工程门：median `0.8 px`、P95 `1.2 px`、maximum
`1.5 px`、pass fraction `0.90`。这些来自当前代码默认值，是冻结的工程发布门，
不是 OpenCV 或几何理论规定的普适常数。

对于新的 120°+120° 组合，不要直接复用旧 120°+160° 的 K/D。先分别运行 cam0
和 cam1 内参，且两机应使用相同的 44 点策略。外参入口会调用
`intrinsic_point_set()` 逐元素检查点集，不一致时直接拒绝，不会自动取交集。

## 5. 双目外参完整流程

### 5.1 前置条件

- cam0/cam1 intrinsic JSON 都是 `acceptable`；
- 两个 JSON 的 model、image size、圆环规格和 used point indices 相同；
- 两相机固定安装，采集期间相机之间不能再移动；
- board 同时完整出现在两路画面；
- cam0 是参考坐标系，输出是 cam0 → cam1 的 `R/t`；
- Static、Training、Holdout V1、Holdout V2 是四个不同 run。

Training 建议 15–20 个真实停留位置：每个位置稳定 2–3 秒，位置之间快速移动，
覆盖中心、四角、俯仰和尽可能大的深度跨度。手持情况下使用
`quasi_static_episode_minimum`，它按绝对运动速度形成 episode，并且每个 episode
只保留一个局部最稳帧，避免数百个相邻帧重复加权。

### 5.2 Preflight

```powershell
$repo = 'D:\prg\prg_cam_host'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$dataRoot = 'D:\camera_runs'

$staticRoot = Join-Path $dataRoot 'stereo_static'
$trainRoot = Join-Path $dataRoot 'stereo_train'
$v1Root = Join-Path $dataRoot 'stereo_holdout_v1'
$v2Root = Join-Path $dataRoot 'stereo_holdout_v2'
$cam0Intrinsic = Join-Path $dataRoot 'intrinsics\cam0_intrinsics.json'
$cam1Intrinsic = Join-Path $dataRoot 'intrinsics\cam1_intrinsics.json'
$auditRoot = Join-Path $dataRoot 'audit\stereo_run01'

& (Join-Path $repo 'scripts_ps\run_extrinsic_calibration.ps1') `
  -StaticRoot $staticRoot `
  -TrainingRoot $trainRoot `
  -HoldoutV1Root $v1Root `
  -HoldoutV2Root $v2Root `
  -Cam0Intrinsic $cam0Intrinsic `
  -Cam1Intrinsic $cam1Intrinsic `
  -OutputRoot $auditRoot `
  -PythonExe $python `
  -PairingMode quasi_static_episode_minimum `
  -MinPairs 15 `
  -PreflightOnly
```

Preflight 会实际执行两机内参 JSON 和 point-set 一致性检查，但不写 audit。

### 5.3 正式执行

去掉 `-PreflightOnly`。顺序固定为：

```text
Static 双路数据
  → stillness_config.json
Training 双路数据
  → episode-minimum timestamp pairs
  → 固定 K0/D0/K1/D1 求 cam0→cam1 R/t
  → Training quality/depth-independence gate
Holdout V1
  → 新 pairs → 固定 R/t 验证
Holdout V2
  → 新 pairs → 固定 R/t 验证
  → run_manifest.json
```

脚本故意不传 `--allow-limited` 或 `--allow-limited-extrinsics`：limited 候选可以用来
诊断，但不能被脚本提升成 acceptable。若训练质量为 unacceptable，Python 会拒绝
写正式 extrinsics JSON，后续 Holdout 不会继续。

重要字段：

```powershell
$result = Get-Content -Raw -LiteralPath `
  (Join-Path $auditRoot '02_training_solve\cam0_to_cam1_extrinsics.json') |
  ConvertFrom-Json

$result.quality | Format-List
$result.R_cam1_from_cam0
$result.t_cam1_from_cam0_mm

$v1 = Get-Content -Raw -LiteralPath `
  (Join-Path $auditRoot '04_holdout_v1_validation\holdout_summary.json') |
  ConvertFrom-Json

$v2 = Get-Content -Raw -LiteralPath `
  (Join-Path $auditRoot '06_holdout_v2_validation\holdout_summary.json') |
  ConvertFrom-Json

$v1.status
$v2.status
```

低 stereo RMS 只说明所选 pair 可以被某个 R/t 数值拟合，不自动证明物理外参可发布。
还必须检查 rotation/translation dispersion、depth drift、rectified vertical error，
以及两个独立 Holdout。相机间距、夹角和不同 FOV 会改变观测几何，但不会天然制造
100 px 交叉误差；几十到上百像素通常优先检查错误的 K/D、point set、配对身份、
相机顺序或坐标方向。

## 6. 输出目录和退出语义

两个 PowerShell 入口都拒绝覆盖非空 OutputRoot。失败后保留目录作为不可变证据，
下一次应使用新的 `run_id`，不要清空后复用。

Python 标定入口的核心退出语义：

| Exit | 含义 |
|---:|---|
| 0 | 当前步骤通过 |
| 2 | 输入、JSON、OpenCV 或数据格式错误 |
| 3 | 数据数量/质量门未通过 |
| 4 | stereo solve 为 limited 且未允许发布 |

最终以 JSON `status` 和 manifest 为准；`exit 0` 不能代替人工检查输入身份，历史结果
也不能自动代表当前 run。

## 7. 发布前测试

```powershell
$repo = 'D:\prg\prg_cam_host'
$python = Join-Path $repo '.venv\Scripts\python.exe'

Set-Location $repo
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) {
  throw 'Host receiver/calibration regression failed'
}

git status --short
git diff --check
```

测试只证明软件回归；真实 Npcap、相机、板卡、光照、镜头固定性和采样覆盖仍需硬件
run 证明。
