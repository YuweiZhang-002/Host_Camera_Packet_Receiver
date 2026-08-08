"""
eth_validate.py  --  Layer 2 (Ethernet Validation).

Generic, protocol-agnostic checks on a RawEthernetFrame: EtherType,
optional source-MAC allow-list, and coarse frame-length sanity. This
deliberately does NOT know about the 128-byte camera packet or the
fixed self-test payload -- that exact-length / content check is
Layer 3's job (camera_parser.py), since it's specific to *what* is
carried, not to Ethernet itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .capture import RawEthernetFrame

ETHER_TYPE = 0x88B5
MIN_ETHERNET_PAYLOAD = 46    # 802.3 minimum payload before padding is stripped
MAX_ETHERNET_PAYLOAD = 1500  # standard MTU -- raise this if jumbo frames are enabled


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    reason: str = ""  # "", "not_taxi_ethertype", "mac_filtered", "payload_too_short", "payload_too_long"


def validate_ethernet_frame(
    frame: RawEthernetFrame,
    *,
    ether_type: int = ETHER_TYPE,
    allowed_src_macs: Optional[set[str]] = None,
) -> ValidationResult:
    if frame.ethertype != ether_type:
        return ValidationResult(ok=False, reason="not_taxi_ethertype")

    if allowed_src_macs is not None and frame.src_mac.lower() not in allowed_src_macs:
        return ValidationResult(ok=False, reason="mac_filtered")

    if len(frame.payload) < MIN_ETHERNET_PAYLOAD:
        return ValidationResult(ok=False, reason="payload_too_short")

    if len(frame.payload) > MAX_ETHERNET_PAYLOAD:
        return ValidationResult(ok=False, reason="payload_too_long")

    return ValidationResult(ok=True)
