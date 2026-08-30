# 第三方通知与许可边界

仓库根目录的 BSD 3-Clause License 仅适用于
Host_Camera_Packet_Receiver 中由项目作者原创的内容；如果文件或目录另有
声明，则以该声明为准。从 `requirements-*.txt` 安装第三方包不会改变
这些包的许可证，BSD 许可也不会取代外部运行时、驱动、库或工具的许可。

本仓库不内嵌第三方 Python 源码树、wheel、Npcap 安装程序或 OpenCV
二进制文件。使用者从各上游发行方独立获取这些依赖。

## 已声明的 Python 依赖

| 组件 | 项目中的用途 | 上游许可证 | 与本项目直接相关的条件 |
|---|---|---|---|
| Scapy | 实时网卡枚举与抓包、PCAP 回放后备路径、可选 PCAP 记录 | 上游声明为 GPL version 2；当前包元数据标识为 GPL-2.0-only | Scapy 由使用者单独安装，不属于本仓库 BSD 许可范围。重新分发 Scapy 或包含 Scapy 的组合包/可执行文件时，必须保留通知并履行 GPL 条款。`requirements-live.txt` 目前没有固定版本，因此还要核对实际安装版本的许可元数据。 |
| pytest | 开发与回归测试 | MIT | 实时接收机不依赖 pytest。重新分发时保留上游版权与许可文本。 |
| NumPy | 标定数组、矩阵和数值计算 | BSD 3-Clause | 仅用于标定/离线工作。重新分发时保留上游版权、条件和免责声明。 |
| opencv-python-headless / OpenCV | 圆阵列检测、内参、固定 K/D 双目标定、校正与 holdout 验证 | Python 打包项目和 wheel 内组件有各自通知；OpenCV 4.5 及以后为 Apache-2.0 | `requirements-calibration.txt` 选择 `>=4.8,<5`。重新分发二进制时保留 Apache-2.0、NOTICE 与 wheel 内第三方通知。本仓库不会额外授予超出上游条款的专利或其他权利。 |

上游项目：

- Scapy：`https://github.com/secdev/scapy`
- pytest：`https://github.com/pytest-dev/pytest`
- NumPy：`https://github.com/numpy/numpy`
- OpenCV Python packages：`https://github.com/opencv/opencv-python`
- OpenCV：`https://github.com/opencv/opencv`

该表是项目依赖清单，不能替代实际安装版本附带的完整许可文本。

## Npcap 与外部工具

Npcap 是 Scapy 实时抓包路径使用的独立 Windows 驱动，本仓库不分发它。
不得把免费版 Npcap 随本项目打包或重新分发；如果安装程序或产品需要捆绑
Npcap，必须先取得相应的 Npcap OEM 许可。

Python、PowerShell、Git、Wireshark 与 Xilinx Vivado 也是外部工具。
文档中调用这些工具，不代表它们受本仓库 BSD 许可约束。

## 与 FPGA 和 MCU 仓库的关系

MCU 原创内容、Host 原创内容和 FPGA 第一方原创内容均采用 BSD 3-Clause。
这一统一的第一方许可不覆盖或重新授权 FPGA 端 TAXI
（CERN-OHL-S-2.0）、FPGA-RMII-SMII（GPL-3.0）、Xilinx IP 或包含这些
组件的生成产物。

## 重新分发检查表

1. 记录 Scapy、pytest、NumPy 与 OpenCV 的实际解析版本。
2. 保留全部适用的上游版权、许可和 NOTICE 文件。
3. 未取得重新分发许可时，不将 Npcap 打包进安装程序。
4. 若分发的包或可执行文件包含 Scapy，而不是让用户独立安装，则重新评估 GPL 义务。
5. 私有抓包、接口 GUID、标定数据和生成输出未经单独审查与授权时不得进入源码发行包。
