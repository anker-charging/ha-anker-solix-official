"""Batch reading optimization for Modbus registers."""
import logging
from typing import Any, Dict, List, Tuple

from .const import BATCH_READ_GAP_THRESHOLD, MAX_REGISTERS_PER_READ


class RegisterGroup:
    """Represents a contiguous group of registers."""

    def __init__(self, start_address: int, end_address: int):
        """Initialize register group.

        Args:
            start_address: Starting register address
            end_address: Ending register address (inclusive)
        """
        self.start_address = start_address
        self.end_address = end_address
        self.count = end_address - start_address + 1
        self.data_points: List[Tuple[str, Dict[str, Any]]] = []

    def add_data_point(self, key: str, config: Dict[str, Any]) -> None:
        """Add a data point to this register group.

        Args:
            key: Data point key
            config: Data point configuration
        """
        self.data_points.append((key, config))

    def __repr__(self) -> str:
        """String representation."""
        return f"RegisterGroup(start={self.start_address}, end={self.end_address}, count={self.count}, points={len(self.data_points)})"


class BatchRegisterReader:
    """Optimizes Modbus register reading by grouping nearby registers.

    This class analyzes data point configurations and groups registers
    that are close together (within gap_threshold) to minimize the number
    of Modbus read operations.
    """

    def __init__(
        self,
        gap_threshold: int = BATCH_READ_GAP_THRESHOLD,
        max_registers: int = MAX_REGISTERS_PER_READ,
    ):
        """Initialize batch reader.

        Args:
            gap_threshold: Maximum gap between registers to group together (default: 5)
            max_registers: Maximum number of registers to read in one operation (default: 100)
        """
        self._gap_threshold = gap_threshold
        self._max_registers = max_registers
        self._logger = logging.getLogger(__name__)

    def group_data_points(
        self, data_points: Dict[str, Any]
    ) -> List[RegisterGroup]:
        """Group data points into register groups for batch reading.

        Args:
            data_points: Dictionary of data point configurations

        Returns:
            List of RegisterGroup objects
        """
        if not data_points:
            return []

        # Sort data points by address
        sorted_points = sorted(
            data_points.items(),
            key=lambda x: x[1].get("address", 0)
        )

        groups: List[RegisterGroup] = []
        current_group: RegisterGroup | None = None

        for key, config in sorted_points:
            address = config.get("address")
            count = config.get("count", 1)

            if address is None:
                self._logger.warning("Data point %s has no address, skipping", key)
                continue

            end_address = address + count - 1

            # Start new group if:
            # 1. This is the first data point
            # 2. Gap is too large
            # 3. Group would exceed max_registers
            if current_group is None:
                current_group = RegisterGroup(address, end_address)
                current_group.add_data_point(key, config)
            else:
                gap = address - current_group.end_address - 1
                new_group_size = end_address - current_group.start_address + 1

                if gap > self._gap_threshold or new_group_size > self._max_registers:
                    # Start new group
                    groups.append(current_group)
                    current_group = RegisterGroup(address, end_address)
                    current_group.add_data_point(key, config)
                else:
                    # Extend current group
                    current_group.end_address = end_address
                    current_group.count = current_group.end_address - current_group.start_address + 1
                    current_group.add_data_point(key, config)

        # Add last group
        if current_group:
            groups.append(current_group)

        self._logger.debug(
            "Grouped %d data points into %d register groups",
            len(data_points),
            len(groups),
        )

        return groups

    def calculate_efficiency(
        self, data_points: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Calculate read efficiency metrics.

        Args:
            data_points: Dictionary of data point configurations

        Returns:
            Dictionary with efficiency metrics
        """
        groups = self.group_data_points(data_points)

        # Calculate individual reads
        individual_reads = sum(config.get("count", 1) for config in data_points.values())

        # Calculate batch reads
        batch_reads = sum(group.count for group in groups)

        # Calculate savings
        savings = individual_reads - batch_reads
        efficiency = (1 - batch_reads / individual_reads) * 100 if individual_reads > 0 else 0

        return {
            "individual_reads": individual_reads,
            "batch_reads": batch_reads,
            "savings": savings,
            "efficiency_percent": round(efficiency, 2),
            "num_groups": len(groups),
            "num_data_points": len(data_points),
        }
