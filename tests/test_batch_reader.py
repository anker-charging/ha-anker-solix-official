"""Unit tests for BatchRegisterReader / RegisterGroup grouping logic."""

from custom_components.anker_solix_official.batch_reader import (
    BatchRegisterReader,
    RegisterGroup,
)


class TestRegisterGroup:
    """RegisterGroup basic behaviour."""

    def test_initializes_count_from_address_span(self) -> None:
        # Arrange/Act
        group = RegisterGroup(100, 105)

        # Assert
        assert group.count == 6
        assert group.data_points == []

    def test_add_data_point_appends_tuple(self) -> None:
        # Arrange
        group = RegisterGroup(0, 0)

        # Act
        group.add_data_point("power", {"address": 0})

        # Assert
        assert group.data_points == [("power", {"address": 0})]

    def test_repr_includes_key_fields(self) -> None:
        group = RegisterGroup(10, 20)
        assert "start=10" in repr(group)
        assert "end=20" in repr(group)
        assert "count=11" in repr(group)


class TestGroupDataPoints:
    """BatchRegisterReader.group_data_points()."""

    def test_empty_input_returns_empty_list(self) -> None:
        reader = BatchRegisterReader()
        assert reader.group_data_points({}) == []

    def test_single_data_point_forms_one_group(self) -> None:
        # Arrange
        reader = BatchRegisterReader()
        data_points = {"power": {"address": 100, "count": 1}}

        # Act
        groups = reader.group_data_points(data_points)

        # Assert
        assert len(groups) == 1
        assert groups[0].start_address == 100
        assert groups[0].end_address == 100

    def test_adjacent_registers_within_gap_threshold_merge_into_one_group(self) -> None:
        # Arrange: default gap_threshold=5, so addresses 3 apart must merge.
        reader = BatchRegisterReader(gap_threshold=5, max_registers=100)
        data_points = {
            "a": {"address": 100, "count": 1},
            "b": {"address": 103, "count": 1},
        }

        # Act
        groups = reader.group_data_points(data_points)

        # Assert
        assert len(groups) == 1
        assert groups[0].start_address == 100
        assert groups[0].end_address == 103

    def test_gap_larger_than_threshold_starts_new_group(self) -> None:
        # Arrange: gap of 10 exceeds gap_threshold=5.
        reader = BatchRegisterReader(gap_threshold=5, max_registers=100)
        data_points = {
            "a": {"address": 100, "count": 1},
            "b": {"address": 111, "count": 1},
        }

        # Act
        groups = reader.group_data_points(data_points)

        # Assert
        assert len(groups) == 2
        assert groups[0].end_address == 100
        assert groups[1].start_address == 111

    def test_group_size_exceeding_max_registers_starts_new_group(self) -> None:
        # Arrange: gap is 0 (perfectly adjacent) but combined span would
        # exceed max_registers=10, so a new group must start anyway.
        reader = BatchRegisterReader(gap_threshold=100, max_registers=10)
        data_points = {
            "a": {"address": 0, "count": 8},
            "b": {"address": 8, "count": 8},
        }

        # Act
        groups = reader.group_data_points(data_points)

        # Assert: first group covers [0,7] (8 registers); adding [8,15] would
        # make a 16-register span > max_registers=10, so it splits.
        assert len(groups) == 2
        assert groups[0].start_address == 0
        assert groups[0].end_address == 7
        assert groups[1].start_address == 8
        assert groups[1].end_address == 15

    def test_data_point_missing_address_is_skipped(self) -> None:
        # Arrange
        reader = BatchRegisterReader()
        data_points = {
            "bad": {"count": 1},
            "good": {"address": 50, "count": 1},
        }

        # Act
        groups = reader.group_data_points(data_points)

        # Assert
        assert len(groups) == 1
        assert groups[0].data_points == [("good", {"address": 50, "count": 1})]

    def test_default_count_is_one_when_omitted(self) -> None:
        # Arrange: no explicit "count" key.
        reader = BatchRegisterReader()
        data_points = {"a": {"address": 10}}

        # Act
        groups = reader.group_data_points(data_points)

        # Assert
        assert groups[0].start_address == 10
        assert groups[0].end_address == 10

    def test_data_points_are_grouped_in_address_order_regardless_of_dict_order(self) -> None:
        # Arrange: dict insertion order is address 200 before 100.
        reader = BatchRegisterReader(gap_threshold=5, max_registers=100)
        data_points = {
            "later": {"address": 200, "count": 1},
            "earlier": {"address": 100, "count": 1},
        }

        # Act
        groups = reader.group_data_points(data_points)

        # Assert: sorted by address, so two separate groups in ascending order.
        assert len(groups) == 2
        assert groups[0].start_address == 100
        assert groups[1].start_address == 200


class TestCalculateEfficiency:
    """BatchRegisterReader.calculate_efficiency()."""

    def test_empty_data_points_returns_zero_efficiency(self) -> None:
        # Arrange
        reader = BatchRegisterReader()

        # Act
        stats = reader.calculate_efficiency({})

        # Assert: division-by-zero guarded, efficiency defaults to 0.
        assert stats["individual_reads"] == 0
        assert stats["batch_reads"] == 0
        assert stats["efficiency_percent"] == 0
        assert stats["num_groups"] == 0

    def test_savings_reflects_grouping_overlap(self) -> None:
        # Arrange: two adjacent single-register points get read as one
        # 2-register batch instead of 2 separate 1-register reads -- no
        # savings in *count* here (2 individual == 2 batch) but the grouping
        # itself must still be correct.
        reader = BatchRegisterReader(gap_threshold=5, max_registers=100)
        data_points = {
            "a": {"address": 100, "count": 1},
            "b": {"address": 101, "count": 1},
        }

        # Act
        stats = reader.calculate_efficiency(data_points)

        # Assert
        assert stats["individual_reads"] == 2
        assert stats["batch_reads"] == 2
        assert stats["savings"] == 0
        assert stats["num_groups"] == 1
        assert stats["num_data_points"] == 2

    def test_efficiency_percent_reflects_gap_filling_overhead(self) -> None:
        # Arrange: two 1-register points 5 apart get merged into a single
        # 6-register group (filling the gap), so batch_reads > individual_reads
        # and efficiency is negative (the batch reads MORE than the sum of
        # individually-needed registers, because of the gap-fill).
        reader = BatchRegisterReader(gap_threshold=5, max_registers=100)
        data_points = {
            "a": {"address": 100, "count": 1},
            "b": {"address": 105, "count": 1},
        }

        # Act
        stats = reader.calculate_efficiency(data_points)

        # Assert
        assert stats["individual_reads"] == 2
        assert stats["batch_reads"] == 6
        assert stats["savings"] == -4
        assert stats["efficiency_percent"] < 0
