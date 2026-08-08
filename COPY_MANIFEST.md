# COPY_MANIFEST

本阶段只做复制，不做代码修正。源仓库 D:/prg/prg_cam 保持只读；新目录为 D:/prg/prg_cam_host。

## A. 已复制清单

### A1. Python 入口与其依赖闭包

- taxi_receiver/__init__.py - 包标记，保证 python -m taxi_receiver.cli 以同一包名导入。
- taxi_receiver/cli.py - 接收机主入口，直接挂载 argparse 参数表和运行流程。
- taxi_receiver/viewer_cli.py - 查看器入口，供 host 端查看归档数据。
- taxi_receiver/async_sink.py - 入口闭包中的异步输出分发器。
- taxi_receiver/archive_layout.py - viewer 入口依赖的归档布局解析。
- taxi_receiver/archive_monitor.py - viewer 后端轮询与最新帧选择。
- taxi_receiver/camera_lane.py - 接收机核心 lane 路由与发布策略。
- taxi_receiver/camera_parser.py - 解析 camera / fixed 模式数据。
- taxi_receiver/camera_viewer.py - viewer UI 实现。
- taxi_receiver/capture.py - Layer 1 抓包与 RawEthernetFrame 构造。
- taxi_receiver/demo_archive_producer.py - viewer 测试用归档生成器。
- taxi_receiver/eth_validate.py - Layer 2 校验。
- taxi_receiver/image_loader.py - viewer 读取 COMPLETE / RECOVERED 图像归档。
- taxi_receiver/image_pipeline.py - 图像发布链路。
- taxi_receiver/packet_format.py - 包格式与 CRC / ByteStreamFramer。
- taxi_receiver/pcap_stdlib.py - PCAP/PCAPNG 解析与回放。
- taxi_receiver/pipeline.py - 组装 queue 与 worker 的编排层。
- taxi_receiver/reassembler.py - Layer 5 重组。
- taxi_receiver/recorder.py - pcap / error frame 记录。
- taxi_receiver/session_audit.py - session_audit.csv 记录器。
- taxi_receiver/stages.py - stage 组合与 build_stage_chain。
- taxi_receiver/storage.py - 完整帧归档与 summary.csv。
- taxi_receiver/stream_monitor.py - 统计与速率报告。
- taxi_receiver/threshold_recover.py - 阈值图像恢复。

### A2. 测试入口与传递依赖

- tests/__init__.py - pytest 包标记。
- tests/synthetic.py - 测试用 RawEthernetFrame / camera frame 构造。
- tests/test_camera_lane.py - lane 路由、背压、发布策略测试。
- tests/test_camera_parser.py - 解析与校验测试。
- tests/test_camera_viewer.py - viewer 与归档扫描测试。
- tests/test_image_pipeline.py - 图像发布链路测试。
- tests/test_image_recovery.py - 恢复 / zero-fill / 发布门控测试。
- tests/test_packet_format.py - 包格式、CRC、ByteStreamFramer 测试。
- tests/test_pcap_stdlib.py - 纯标准库 PCAP/PCAPNG 回放测试。
- tests/test_pipeline_synthetic.py - 端到端管线与异步分发测试。
- tests/test_reassembler.py - 重组器行为测试。
- tests/test_row_csv_recorder.py - rows.csv 记录器测试。
- tests/test_session_audit.py - session audit 测试。
- tests/test_stages.py - stage 组合测试。
- tests/test_storage_and_reassembly.py - 归档与重组测试。
- tests/test_stream_monitor.py - 统计器测试。
- tests/test_threshold_recover.py - 恢复器测试。

### A3. 闭包之外的补齐文件

- requirements-live.txt - live capture 的第三方依赖清单，pytest / replay / help 之外的运行补齐项。
- analyze_rows_csv.py - 直接分析 host 侧 rows.csv 的辅助脚本。
- analyze_camera_archive.py - 直接分析 host 侧 camera archive 的辅助脚本。
- run_receiver.ps1 - 接收机主驱动脚本，直接调用 python -m taxi_receiver.cli。
- verify_s2.ps1 - S2 A/B 验证脚本。
- replay_pcap.ps1 - 离线回放驱动脚本。
- run_camera_viewer.ps1 - viewer 驱动脚本。
- monitor_camera_output.ps1 - 运行时轮询输出监视脚本。
- tests/vectors/ila_camera_payload_legacy_v0.hex - pytest 用的回归向量数据。

## B. 已排除清单

- FPGA / RTL / 工程产物：0 个在本次接收机 host 子树中命中；未复制任何 .v .sv .vhd .xdc .xpr .bit .ltx 或 Vivado 工程目录。
- MCU / 固件源码：0 个命中；未复制任何 .c .h .uf2 或 PIO 程序。
- 运行产物与实测数据：0 个被复制；源子树内这类文件 / 目录 2 个，均已排除。
- 环境垃圾：55 个命中，均排除；包括 __pycache__ 和 .pytest_cache。
- 大于 5 MB 的单文件二进制：0 个命中。

## C. 灰区待裁决清单

以下文件两边都沾，已先保留在报告中，等你裁决阶段二怎么处理。

- taxi_receiver/run_camera_viewer.ps1 - 含本机绝对路径，且既是 host 启动脚本又依赖本机环境；倾向保留，因为它是直接入口，但阶段二应把默认路径参数化。
- taxi_receiver/run_receiver.ps1 - 含本机绝对路径；倾向保留，因为它是接收机主入口脚本，但阶段二应去硬编码路径。
- taxi_receiver/verify_s2.ps1 - 含本机绝对路径，且同时承载接收机与测试验证；倾向保留，因为它是明确的运维入口，但阶段二要改成可配置根路径。
- taxi_receiver/replay_pcap.ps1 - 含本机绝对路径；倾向保留，因为它是离线回放入口，但阶段二应去掉机器相关默认值。
- taxi_receiver/monitor_camera_output.ps1 - 含本机绝对路径，且更像本地监视工具；倾向保留，因为它直接服务接收机可观测性，但阶段二最好改成可注入路径。
- taxi_receiver/taxi_receiver/camera_viewer.py - 默认归档根是 D:/prg/prg_cam/images/temp/archive，带有本机绝对路径；倾向保留，因为 viewer 入口依赖它，但阶段二应把默认值改为相对路径或配置参数。
- taxi_receiver/README.md - 大量本机绝对路径与运行命令，属于说明文档而不是纯使用接口；倾向保留，因为它是 host 侧说明，但阶段二应改写路径示例。
- taxi_receiver/CHEATSHEET.md - 大量本机绝对路径与调优记录；倾向保留，因为它是接收机使用手册，但阶段二应清理机器绑定路径。
- taxi_receiver/p11_python_receiver_performance_optimization.md - 大量本机实测数字与研究记录，明显更像审计笔记；倾向暂缓复制到独立仓库的正式根目录，或者改成附录文档。

## D. 验证结果

- pytest collect-only：源树与新目录都在 tests/test_pcap_stdlib.py:20 触发同一个 IndexError: 6，说明测试发现集合被原样复制；我没有观察到测试项遗漏，但当前这类测试文件本身依赖更深的目录层级，导致收集阶段就中断。
- pytest 实跑：在 D:/prg/prg_cam_host 下执行 python -m pytest tests --basetemp D:/prg/prg_cam_host/.pytest_tmp -q 时，同样在 tests/test_pcap_stdlib.py 收集阶段失败，原因还是 Path(__file__).resolve().parents[6] 越界。
- CLI help：python -m taxi_receiver.cli --help 成功启动并打印完整参数表，参数项包括 --interface、--replay-pcap、--mode、--images-root、--publish-images、--max-stage 等。
- CLI list：python -m taxi_receiver.cli --list 能进入 CLI，但当前 Python 环境没有安装 scapy，因此在打印接口前退出，并提示需要先安装 requirements-live.txt。
- 残留源仓库绝对路径与 sys.path hack 搜索：在新目录内全文搜索 D:/prg/prg_cam/、sys.path.append、sys.path.insert、PYTHONPATH，均未命中。

## E. 阶段二需要处理的问题

- 所有带 D:/prg/prg_cam 的默认值和示例路径都要参数化，至少包括 ps1 脚本、camera_viewer.py 和文档里的命令示例。
- requirements-live.txt 只被复制，没有安装；如果要让 --list 正常工作，需要在新目录补齐 scapy 依赖。
- tests/test_pcap_stdlib.py 的父目录假设与新目录层级不兼容，这是当前 pytest 收集失败的直接原因。
- README.md、CHEATSHEET.md、p11_python_receiver_performance_optimization.md 里包含大量机器绑定内容，阶段二要决定是清理成通用文档、迁到附录，还是从正式复制目标中剥离。
- 目前只做了复制，没有 git init、没有提交、没有改代码逻辑，也没有做路径修正。