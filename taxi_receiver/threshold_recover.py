"""
threshold_recover.py -- Layer 6 / completed-frame business processing.

The Ethernet packet carries one threshold row as 80 packed bytes
(640 one-bit pixels).  This module expands each bit to one uint8-style
byte: 0 -> 0x00 and 1 -> 0xFF.

Recovery deliberately happens *after* FrameReassembler emits a
CompletedFrame.  Keeping rows packed until this boundary avoids an
eightfold increase in the pipeline queue/reassembly memory and leaves
the 128-byte wire-format and CRC code unchanged.

Typical pipeline wiring::

    recovered_frames = []
    recoverer = ThresholdFrameRecoverer(
        expected_rows=480,
        bit_order=BitOrder.MSB_FIRST,
        missing_policy=MissingRowPolicy.REJECT,
        on_recovered_frame=recovered_frames.append,
    )
    pipeline = TaxiReceiverPipeline(
        frame_source=source,
        mode="camera",
        max_stage="reassemble",
        reassembler=FrameReassembler(),
        on_completed_frame=recoverer,
    )

The sender/FPGA specification must define the bit order within each
payload byte.  Little-endian multi-byte struct fields do not determine
whether the first pixel is bit 7 or bit 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Callable, Optional

from .packet_format import ROW_BYTES
from .reassembler import CompletedFrame

BITS_PER_BYTE = 8
ROW_BYTES_PACKED = ROW_BYTES
ROW_PIXELS = ROW_BYTES_PACKED * BITS_PER_BYTE


class BitOrder(str, Enum):
    """Pixel order inside each packed payload byte."""

    MSB_FIRST = "msb_first"  # bit 7 is the first/leftmost pixel
    LSB_FIRST = "lsb_first"  # bit 0 is the first/leftmost pixel


class MissingRowPolicy(str, Enum):
    """How a completed frame with missing rows/overflow is handled."""

    REJECT = "reject"
    ZERO_FILL = "zero_fill"


class IncompleteThresholdFrameError(ValueError):
    """Raised when REJECT policy receives an incomplete/overflowed frame."""


def _coerce_bit_order(bit_order: BitOrder | str) -> BitOrder:
    if isinstance(bit_order, BitOrder):
        return bit_order
    try:
        return BitOrder(bit_order.lower().replace("-", "_"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            f"bit_order must be 'msb_first' or 'lsb_first', got {bit_order!r}"
        ) from exc


def _coerce_missing_policy(policy: MissingRowPolicy | str) -> MissingRowPolicy:
    if isinstance(policy, MissingRowPolicy):
        return policy
    try:
        return MissingRowPolicy(policy.lower().replace("-", "_"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            f"missing_policy must be 'reject' or 'zero_fill', got {policy!r}"
        ) from exc


def _bit_positions(bit_order: BitOrder) -> range:
    if bit_order is BitOrder.MSB_FIRST:
        return range(7, -1, -1)
    return range(8)


@lru_cache(maxsize=2)
def build_lut(bit_order: BitOrder | str) -> tuple[bytes, ...]:
    """Return an immutable 256-entry lookup table.

    Each entry is eight bytes long and contains only 0x00/0xFF.  The
    previous implementation referenced undefined ``value`` and
    ``slot`` variables while constructing the table; building complete
    entries directly also removes the project's undeclared NumPy
    dependency.
    """

    order = _coerce_bit_order(bit_order)
    positions = _bit_positions(order)
    return tuple(
        bytes(0xFF if packed_byte & (1 << bit) else 0x00 for bit in positions)
        for packed_byte in range(256)
    )


class ThresholdRowDecoder:
    """LUT-backed decoder for one confirmed payload bit order."""

    def __init__(self, bit_order: BitOrder | str = BitOrder.MSB_FIRST):
        self.bit_order = _coerce_bit_order(bit_order)
        self._lut = build_lut(self.bit_order)

    def expand_row(self, packed_row: bytes) -> bytes:
        """Expand one 80-byte packed row into 640 0x00/0xFF bytes."""

        if len(packed_row) != ROW_BYTES_PACKED:
            raise ValueError(
                f"expected {ROW_BYTES_PACKED} packed bytes, got {len(packed_row)}"
            )
        return b"".join(self._lut[value] for value in packed_row)

    def expand_frame(self, packed_frame: bytes, expected_rows: int) -> bytes:
        """Expand a flat ``expected_rows * 80`` buffer.

        The returned bytes are row-major and have exactly
        ``expected_rows * 640`` bytes.  A caller needing an image array
        can reshape a zero-copy view with NumPy downstream, but NumPy is
        intentionally not required by the receiver core.
        """

        if expected_rows <= 0:
            raise ValueError(f"expected_rows must be positive, got {expected_rows}")
        expected_len = expected_rows * ROW_BYTES_PACKED
        if len(packed_frame) != expected_len:
            raise ValueError(
                f"expected {expected_len} packed bytes for {expected_rows} rows, "
                f"got {len(packed_frame)}"
            )
        return b"".join(self._lut[value] for value in packed_frame)


@dataclass(frozen=True, slots=True)
class RecoveredThresholdFrame:
    """640-pixel-wide threshold image plus its transport-quality metadata."""

    camera_id: int
    frame_id: int
    width: int
    height: int
    pixels: bytes
    missing_rows: tuple[int, ...]
    had_overflow: bool
    bit_order: BitOrder

    def row(self, row_index: int) -> bytes:
        """Return one recovered 640-byte row."""

        if not 0 <= row_index < self.height:
            raise IndexError(f"row_index out of range: {row_index}")
        start = row_index * self.width
        return self.pixels[start:start + self.width]


def recover_completed_frame(
    completed: CompletedFrame,
    expected_rows: int,
    *,
    bit_order: BitOrder | str = BitOrder.MSB_FIRST,
    missing_policy: MissingRowPolicy | str = MissingRowPolicy.REJECT,
) -> RecoveredThresholdFrame:
    """Recover one reassembled packed frame at the business boundary.

    ``expected_rows`` is sensor geometry, not ``completed.row_count``.
    It is used to detect missing tail rows that FrameReassembler cannot
    infer when the last-row packet itself was lost.
    """

    if expected_rows <= 0:
        raise ValueError(f"expected_rows must be positive, got {expected_rows}")

    # Validate before CompletedFrame.to_bytes(): bytearray slice
    # assignment would otherwise accept a malformed row of another size.
    for row_index, packed_row in completed.rows.items():
        if not 0 <= row_index < expected_rows:
            raise ValueError(
                f"row index {row_index} is outside expected range "
                f"0..{expected_rows - 1}"
            )
        if len(packed_row) != ROW_BYTES_PACKED:
            raise ValueError(
                f"row {row_index} must contain {ROW_BYTES_PACKED} packed bytes, "
                f"got {len(packed_row)}"
            )

    missing_rows = tuple(
        row_index
        for row_index in range(expected_rows)
        if row_index not in completed.rows
    )
    policy = _coerce_missing_policy(missing_policy)
    if policy is MissingRowPolicy.REJECT and (
        missing_rows or completed.had_overflow
    ):
        raise IncompleteThresholdFrameError(
            f"camera={completed.camera_id} frame={completed.frame_id} "
            f"missing_rows={list(missing_rows)} "
            f"had_overflow={completed.had_overflow}"
        )

    decoder = ThresholdRowDecoder(bit_order)
    # to_bytes() preserves row_idx order and zero-fills missing packed
    # rows.  Expanding those zeros yields 640 black bytes per missing row.
    packed_frame = completed.to_bytes(expected_rows)
    pixels = decoder.expand_frame(packed_frame, expected_rows)

    return RecoveredThresholdFrame(
        camera_id=completed.camera_id,
        frame_id=completed.frame_id,
        width=ROW_PIXELS,
        height=expected_rows,
        pixels=pixels,
        missing_rows=missing_rows,
        had_overflow=completed.had_overflow,
        bit_order=decoder.bit_order,
    )


class ThresholdFrameRecoverer:
    """Callable adapter suitable for ``on_completed_frame=...``.

    Pipeline ignores callback return values, so ``on_recovered_frame``
    is the downstream hand-off for storage/display/inference.  Keep
    expensive work out of that callback or enqueue it to another
    bounded worker, because normal completion runs on ``taxi-worker``.

    Under REJECT policy an incomplete frame is handled inside this
    adapter instead of raising into Pipeline (where it would be
    miscounted as a parser error). Inspect ``rejected_frames`` /
    ``last_rejection`` or provide ``on_rejected_frame``.
    """

    def __init__(
        self,
        expected_rows: int,
        *,
        bit_order: BitOrder | str = BitOrder.MSB_FIRST,
        missing_policy: MissingRowPolicy | str = MissingRowPolicy.REJECT,
        on_recovered_frame: Optional[
            Callable[[RecoveredThresholdFrame], None]
        ] = None,
        on_rejected_frame: Optional[
            Callable[[IncompleteThresholdFrameError], None]
        ] = None,
    ) -> None:
        if expected_rows <= 0:
            raise ValueError(f"expected_rows must be positive, got {expected_rows}")
        self.expected_rows = expected_rows
        self.bit_order = _coerce_bit_order(bit_order)
        self.missing_policy = _coerce_missing_policy(missing_policy)
        self.on_recovered_frame = on_recovered_frame
        self.on_rejected_frame = on_rejected_frame
        self.rejected_frames = 0
        self.last_rejection: Optional[IncompleteThresholdFrameError] = None

    def __call__(
        self, completed: CompletedFrame
    ) -> Optional[RecoveredThresholdFrame]:
        try:
            recovered = recover_completed_frame(
                completed,
                self.expected_rows,
                bit_order=self.bit_order,
                missing_policy=self.missing_policy,
            )
        except IncompleteThresholdFrameError as exc:
            self.rejected_frames += 1
            self.last_rejection = exc
            if self.on_rejected_frame is not None:
                self.on_rejected_frame(exc)
            return None

        if self.on_recovered_frame is not None:
            self.on_recovered_frame(recovered)
        return recovered
