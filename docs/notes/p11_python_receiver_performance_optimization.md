# P11 Python Receiver Performance Optimization Notes

This is a local measurement record for this machine and this repository snapshot.
It includes the measurement conditions used on this host, the FPGA frame rate/load that was observed, and the RAMDisk / storage setup used during the original runs.
It is not a universal performance claim.

## Scope

This note records the mechanism-level improvements that were already present in the source workspace before this host-only copy was split out.
It exists here so the host repository keeps the same performance rationale, while clearly marking the numbers as local measurements.

## Key measurements from the source notes

- 40,000-packet sample on the local Windows host: capture callback time dropped from 28.8 us/packet to 0.5 us/packet after replacing `Ether(packet_bytes)` with byte slicing.
- On the same sample: CRC16 moved from a 10.5 us/packet pure-Python path to the `binascii.crc_hqx` path, and total packet CPU cost fell from 52.0 us to 13.5 us.
- Estimated single-core throughput rose from 19,231 pkt/s to 74,074 pkt/s.
- 923,514-frame dual-camera load: `--publish-images thread` took 83.7 s, while `--publish-images process` took 65.0 s.
- In the source notes, a 9,179,893-packet live run reduced `ps_drop` from 3,047,448 (33.2%) to 0 after lowering per-packet CPU cost.

## Why it matters

These measurements explain why the receiver is split into Layer 1-5 stages, S1 per-camera lanes, and S2 multi-process image publication.
The main point is not that threads are always slow; it is that per-packet CPU work and GIL contention were the limiting factors in the measured setup.

## Local caveat

The numbers above were measured on one Windows machine with one FPGA feed, one NIC path, and one storage layout.
Do not reuse them as a generic benchmark without repeating the same load, capture rate, and disk configuration.
