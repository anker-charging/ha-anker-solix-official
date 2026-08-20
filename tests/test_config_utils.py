"""Unit tests for config_utils: range parsing and device config assembly."""

from custom_components.anker_solix_official.config_utils import (
    _parse_batch_ranges,
    _parse_range_string,
    parse_device_configuration,
)


class TestParseRangeString:
    """_parse_range_string('10000-10050') -> (start, end) | None."""

    def test_valid_range_returns_tuple(self) -> None:
        assert _parse_range_string("10000-10050") == (10000, 10050)

    def test_reversed_range_is_normalized(self) -> None:
        # Arrange/Act: end < start in the input string.
        result = _parse_range_string("10050-10000")

        # Assert: swapped so start <= end.
        assert result == (10000, 10050)

    def test_range_with_spaces_is_stripped(self) -> None:
        assert _parse_range_string(" 100 - 200 ") == (100, 200)

    def test_single_number_without_dash_returns_none(self) -> None:
        assert _parse_range_string("10000") is None

    def test_non_numeric_parts_return_none(self) -> None:
        assert _parse_range_string("abc-def") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_range_string("") is None

    def test_three_part_range_returns_none(self) -> None:
        # Arrange: malformed "a-b-c" splits into 3 parts, not the expected 2.
        assert _parse_range_string("10-20-30") is None


class TestParseBatchRanges:
    """_parse_batch_ranges(raw_ranges) -> list[(start, end, register_type)]."""

    def test_none_input_returns_empty_list(self) -> None:
        assert _parse_batch_ranges(None) == []

    def test_empty_dict_returns_empty_list(self) -> None:
        assert _parse_batch_ranges({}) == []

    def test_new_format_dict_with_holding_and_input(self) -> None:
        # Arrange
        raw = {
            "input": ["10000-10050", "32768-32774"],
            "holding": ["10060-10072"],
        }

        # Act
        result = _parse_batch_ranges(raw)

        # Assert: holding ranges are emitted first (loop order in the source
        # iterates ("holding", "input") in that order).
        assert result == [
            (10060, 10072, "holding"),
            (10000, 10050, "input"),
            (32768, 32774, "input"),
        ]

    def test_new_format_with_only_input_key(self) -> None:
        raw = {"input": ["1-2"]}
        assert _parse_batch_ranges(raw) == [(1, 2, "input")]

    def test_new_format_skips_malformed_range_string(self) -> None:
        # Arrange: one valid, one malformed entry in the same list.
        raw = {"input": ["1-2", "not-a-range-!!"]}

        # Act
        result = _parse_batch_ranges(raw)

        # Assert: malformed entry silently dropped, valid one kept.
        assert result == [(1, 2, "input")]

    def test_legacy_list_format_defaults_to_input_type(self) -> None:
        # Arrange
        raw = ["10000-10074", "32768-32774"]

        # Act
        result = _parse_batch_ranges(raw)

        # Assert
        assert result == [(10000, 10074, "input"), (32768, 32774, "input")]

    def test_legacy_comma_separated_string_format(self) -> None:
        # Arrange
        raw = "10000-10074, 32768-32774"

        # Act
        result = _parse_batch_ranges(raw)

        # Assert
        assert result == [(10000, 10074, "input"), (32768, 32774, "input")]

    def test_unsupported_type_returns_empty_list(self) -> None:
        # Arrange: an int is neither a dict, str, nor a generic Iterable of
        # range strings, so it must degrade gracefully rather than raise.
        assert _parse_batch_ranges(12345) == []


class TestParseDeviceConfiguration:
    """parse_device_configuration(cfg) -> (data_points, batch_ranges)."""

    def test_non_dict_input_returns_empty_results(self) -> None:
        assert parse_device_configuration(None) == ({}, [])
        assert parse_device_configuration("not a dict") == ({}, [])

    def test_merges_read_quantities_and_control_items_sections(self) -> None:
        # Arrange
        cfg = {
            "read_quantities": {"power": {"address": 100}},
            "control_items": {"mode": {"address": 200}},
        }

        # Act
        data_points, batch_ranges = parse_device_configuration(cfg)

        # Assert
        assert data_points == {
            "power": {"address": 100},
            "mode": {"address": 200},
        }
        assert batch_ranges == []

    def test_write_quantities_enumeration_selection_builds_select_data_point(self) -> None:
        # Arrange
        cfg = {
            "write_quantities": {
                "enumeration_selection": {
                    "operating_mode": {
                        "address": 300,
                        "data_type": "UINT16",
                        "options": {"0": "self_use", "1": "backup"},
                    }
                }
            }
        }

        # Act
        data_points, _ = parse_device_configuration(cfg)

        # Assert
        dp = data_points["operating_mode"]
        assert dp["address"] == 300
        assert dp["data_type"] == "UINT16"
        assert dp["control_type"] == "select"
        assert dp["display_type"] == "select"
        assert dp["options"] == {"0": "self_use", "1": "backup"}

    def test_switch_control_type_sets_switch_display_type(self) -> None:
        # Arrange
        cfg = {
            "write_quantities": {
                "enumeration_selection": {
                    "ac_output": {
                        "address": 400,
                        "data_type": "UINT16",
                        "control_type": "switch",
                    }
                }
            }
        }

        # Act
        data_points, _ = parse_device_configuration(cfg)

        # Assert
        assert data_points["ac_output"]["display_type"] == "switch"

    def test_item_missing_address_or_data_type_is_skipped(self) -> None:
        # Arrange: "bad_entry" has no address, must not appear in the output.
        cfg = {
            "write_quantities": {
                "enumeration_selection": {
                    "bad_entry": {"data_type": "UINT16"},
                    "good_entry": {"address": 500, "data_type": "UINT16"},
                }
            }
        }

        # Act
        data_points, _ = parse_device_configuration(cfg)

        # Assert
        assert "bad_entry" not in data_points
        assert "good_entry" in data_points

    def test_optional_fields_propagate_only_when_present(self) -> None:
        # Arrange: read_entity_key, is_direction_selector, capability_entity,
        # option_capability_bits, visibility_* are all conditionally added.
        cfg = {
            "write_quantities": {
                "enumeration_selection": {
                    "item": {
                        "address": 600,
                        "data_type": "UINT16",
                        "read_entity_key": "item_status",
                        "is_direction_selector": True,
                        "capability_entity": "cap_mask",
                        "option_capability_bits": {"0": 1},
                        "visibility_entity": "vis_mask",
                        "visibility_value": 5,
                        "visibility_bit": 2,
                    }
                }
            }
        }

        # Act
        dp = parse_device_configuration(cfg)[0]["item"]

        # Assert
        assert dp["read_entity_key"] == "item_status"
        assert dp["is_direction_selector"] is True
        assert dp["capability_entity"] == "cap_mask"
        assert dp["option_capability_bits"] == {"0": 1}
        assert dp["visibility_entity"] == "vis_mask"
        assert dp["visibility_value"] == 5
        assert dp["visibility_bit"] == 2

    def test_optional_fields_absent_when_not_configured(self) -> None:
        # Arrange
        cfg = {
            "write_quantities": {
                "enumeration_selection": {
                    "item": {"address": 700, "data_type": "UINT16"}
                }
            }
        }

        # Act
        dp = parse_device_configuration(cfg)[0]["item"]

        # Assert
        assert "read_entity_key" not in dp
        assert "is_direction_selector" not in dp
        assert "capability_entity" not in dp
        assert "visibility_entity" not in dp

    def test_batch_read_ranges_are_parsed_and_returned(self) -> None:
        # Arrange
        cfg = {"batch_read_ranges": {"input": ["100-200"]}}

        # Act
        _, batch_ranges = parse_device_configuration(cfg)

        # Assert
        assert batch_ranges == [(100, 200, "input")]

    def test_non_dict_item_in_enumeration_selection_is_skipped(self) -> None:
        # Arrange: a malformed entry that is not itself a dict.
        cfg = {
            "write_quantities": {
                "enumeration_selection": {"broken": "not-a-dict"}
            }
        }

        # Act
        data_points, _ = parse_device_configuration(cfg)

        # Assert
        assert data_points == {}
