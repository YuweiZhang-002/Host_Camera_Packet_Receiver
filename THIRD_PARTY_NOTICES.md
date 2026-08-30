# Third-party notices and licence boundary

The repository-root BSD 3-Clause License applies only to original material
authored for Host_Camera_Packet_Receiver, unless a file or directory carries a
different notice. Installing a package from `requirements-*.txt` does not
relicense that package, and the BSD licence does not replace any external
runtime, driver, library, or tool licence.

No third-party Python source tree, wheel, Npcap installer, or OpenCV binary is
vendored in this repository. Users obtain dependencies separately from their
upstream distributors.

## Declared Python dependencies

| Component | Project use | Upstream licence | Conditions relevant to this project |
|---|---|---|---|
| Scapy | Live NIC enumeration/capture, PCAP replay fallback, and optional PCAP recording | GPL version 2 as declared upstream; current package metadata identifies GPL-2.0-only | Scapy is installed separately and is not covered by this repository's BSD licence. Preserve its notices and comply with its GPL terms when redistributing Scapy or a combined package/executable that includes it. Check the exact licence metadata of the resolved Scapy release because `requirements-live.txt` does not currently pin a version. |
| pytest | Development and regression-test runner | MIT | Not required by the live receiver itself. If redistributed, retain the upstream copyright and permission notice. |
| NumPy | Calibration arrays, matrices, and numerical operations | BSD 3-Clause | Installed only for calibration/offline work. If redistributed, retain the upstream copyright, conditions, and disclaimer. |
| opencv-python-headless / OpenCV | Circle-grid detection, intrinsic calibration, fixed-K/D stereo solve, rectification, and holdout validation | The Python packaging project and bundled components carry their own notices; OpenCV 4.5 and later is Apache-2.0 | `requirements-calibration.txt` selects OpenCV `>=4.8,<5`. Preserve Apache-2.0, NOTICE, and the wheel's third-party notices when redistributing binaries. This repository does not grant patent or other rights beyond the upstream terms. |

The authoritative upstream projects are:

- Scapy: `https://github.com/secdev/scapy`
- pytest: `https://github.com/pytest-dev/pytest`
- NumPy: `https://github.com/numpy/numpy`
- OpenCV Python packages: `https://github.com/opencv/opencv-python`
- OpenCV: `https://github.com/opencv/opencv`

This table is a project inventory, not a substitute for the licence texts
shipped by the exact installed releases.

## Npcap and external tools

Npcap is a separately installed Windows capture driver used by Scapy's live
capture path. It is not open-source project material and is not distributed
with this repository. The free Npcap edition must not be bundled or
redistributed as part of this project; obtain an appropriate Npcap OEM licence
before distributing it with an installer or product.

Python, PowerShell, Git, Wireshark, and Xilinx Vivado are also external tools.
Their presence in instructions does not place them under this repository's BSD
licence.

## Relationship to the FPGA and MCU repositories

The MCU repository's original material and this Host repository both use BSD
3-Clause. The FPGA repository also uses BSD 3-Clause for first-party authored
material. That common first-party licence does not cover or relicense the
FPGA-side TAXI (CERN-OHL-S-2.0), FPGA-RMII-SMII (GPL-3.0), Xilinx IP, or any
generated product containing them.

## Redistribution checklist

Before publishing a source archive, wheel, executable, or installer:

1. Record the exact resolved versions of Scapy, pytest, NumPy, and OpenCV.
2. Retain all applicable upstream copyright, licence, and NOTICE files.
3. Do not bundle Npcap without an applicable redistribution licence.
4. Reassess GPL obligations before distributing a package or executable that
   includes Scapy rather than asking the user to install it separately.
5. Keep private captures, interface GUIDs, calibration data, and generated
   output outside a source release unless separately reviewed and licensed.
