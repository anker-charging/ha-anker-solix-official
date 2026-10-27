"""Unit tests for config_utils: range parsing and device config assembly."""

import json
from pathlib import Path

import yaml
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


METER_CONFIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "anker_solix_official"
    / "config"
    / "42bcf12f6663b19b5eaa517f050b6cbf286037eed3c569a5ccae6724c19de64e.yaml"
)

REQUIREMENT_3671_ADDRESSES = {
    10635, 10636, 10637, 10638, 10640, 10642, 10644, 10646, 10648,
    10650, 10652, 10654, 10656, 10658, 10660, 10662, 10664,
    10666, 10667, 10668, 10669, 10671, 10673, 10675, 10677, 10679,
    10680, 10682, 10684, 10686, 10688, 10690, 10692, 10694,
}

NEW_CT_DATA_POINTS = {
    "primary_total_reactive_power": (10646, "INT32", "W", 1, 2),
    "primary_total_power_factor": (10648, "INT16", "/", 1000, 1),
    "secondary_total_reactive_power": (10677, "INT32", "W", 1, 2),
    "secondary_total_power_factor": (10679, "INT16", "/", 1000, 1),
    "secondary_phase_a_forward_active_energy": (10680, "UINT32", "kWh", 10, 2),
    "secondary_phase_b_forward_active_energy": (10682, "UINT32", "kWh", 10, 2),
    "secondary_phase_c_forward_active_energy": (10684, "UINT32", "kWh", 10, 2),
    "secondary_total_forward_active_energy": (10686, "UINT32", "kWh", 10, 2),
    "secondary_phase_a_reverse_active_energy": (10688, "UINT32", "kWh", 10, 2),
    "secondary_phase_b_reverse_active_energy": (10690, "UINT32", "kWh", 10, 2),
    "secondary_phase_c_reverse_active_energy": (10692, "UINT32", "kWh", 10, 2),
    "secondary_total_reverse_active_energy": (10694, "UINT32", "kWh", 10, 2),
}


def _load_meter_config():
    cfg = yaml.safe_load(METER_CONFIG_PATH.read_text(encoding="utf-8"))
    return parse_device_configuration(cfg)


class TestSmartMeterGen2Requirement3671:
    """The shipped AE1X0 config must satisfy Coding #3671 (CT group data)."""

    def test_new_ct_data_points_match_protocol_sheet(self) -> None:
        data_points, _ = _load_meter_config()
        for key, expected in NEW_CT_DATA_POINTS.items():
            dp = data_points[key]
            actual = (
                dp["address"],
                dp["data_type"],
                dp["unit"],
                dp["gain"],
                dp["count"],
            )
            assert actual == expected, key

    def test_all_requirement_addresses_are_covered(self) -> None:
        data_points, _ = _load_meter_config()
        have = {dp["address"] for dp in data_points.values()}
        assert REQUIREMENT_3671_ADDRESSES <= have

    def test_every_data_point_sits_inside_a_batch_range(self) -> None:
        data_points, batch_ranges = _load_meter_config()
        for key, dp in data_points.items():
            if dp.get("register_type"):
                continue
            last = dp["address"] + dp.get("count", 1) - 1
            inside = any(
                start <= dp["address"] and last <= end
                for start, end, _ in batch_ranges
            )
            assert inside, key

    def test_every_translation_key_exists_in_all_languages(self) -> None:
        base = METER_CONFIG_PATH.parents[1]
        lang_files = [base / "strings.json"] + [
            base / "translations" / f"{code}.json"
            for code in ("en", "de", "fr", "nl")
        ]
        data_points, _ = _load_meter_config()
        for lang_file in lang_files:
            names = set(
                json.loads(lang_file.read_text(encoding="utf-8"))["entity"]["sensor"]
            )
            for key, dp in data_points.items():
                assert dp.get("translation_key", key) in names, (
                    lang_file.name,
                    key,
                )


PLUG_CONFIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "anker_solix_official"
    / "config"
    / "575f793986f8d2a7c714eab09c021064533d656b4c56bd9e8c72824e351d3bce.yaml"
)


def _load_plug_config():
    cfg = yaml.safe_load(PLUG_CONFIG_PATH.read_text(encoding="utf-8"))
    return parse_device_configuration(cfg)


class TestSmartPlugRequirement3669:
    """The shipped A17X8 config must expose the cumulative energy register."""

    def test_cumulative_energy_matches_protocol_sheet(self) -> None:
        data_points, _ = _load_plug_config()
        dp = data_points["cumulative_energy"]
        actual = (dp["address"], dp["data_type"], dp["unit"], dp["gain"], dp["count"])
        assert actual == (30033, "UINT32", "kWh", 1000, 2)

    def test_cumulative_energy_sits_inside_a_batch_range(self) -> None:
        data_points, batch_ranges = _load_plug_config()
        dp = data_points["cumulative_energy"]
        last = dp["address"] + dp["count"] - 1
        assert any(
            start <= dp["address"] and last <= end for start, end, _ in batch_ranges
        )

    def test_cumulative_energy_translation_exists_in_all_languages(self) -> None:
        base = PLUG_CONFIG_PATH.parents[1]
        lang_files = [base / "strings.json"] + [
            base / "translations" / f"{code}.json"
            for code in ("en", "de", "fr", "nl")
        ]
        for lang_file in lang_files:
            names = set(
                json.loads(lang_file.read_text(encoding="utf-8"))["entity"]["sensor"]
            )
            assert "cumulative_energy" in names, lang_file.name


CONFIG_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "anker_solix_official"
    / "config"
)

STORED_ENERGY_KEYS = {"rated_energy"}
CUMULATIVE_ENERGY_KEYS = {
    "pv_total_generation",
    "cumulative_charge_energy",
    "cumulative_discharge_energy",
}


def _shipped_battery_data_points():
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        data_points, _ = parse_device_configuration(cfg)
        if STORED_ENERGY_KEYS & data_points.keys():
            yield path.name, data_points


class TestShippedEnergyClasses:
    """Issue #138: stored energy must not be declared as a cumulative meter."""

    def test_stored_energy_declares_energy_storage_without_state_class(self) -> None:
        checked = 0
        for name, data_points in _shipped_battery_data_points():
            for key in STORED_ENERGY_KEYS & data_points.keys():
                dp = data_points[key]
                assert dp.get("device_class") == "energy_storage", (name, key)
                assert str(dp.get("state_class")).lower() == "none", (name, key)
                checked += 1
        assert checked > 0

    def test_cumulative_energy_does_not_override_state_class(self) -> None:
        for name, data_points in _shipped_battery_data_points():
            for key in CUMULATIVE_ENERGY_KEYS & data_points.keys():
                assert "state_class" not in data_points[key], (name, key)


class TestShippedPowerFactorClasses:
    """Issue #130: a unitless ratio needs declared classes to get statistics."""

    def test_power_factor_declares_power_factor_measurement(self) -> None:
        data_points, _ = _load_meter_config()
        keys = [k for k in data_points if k.endswith("_power_factor")]
        assert keys
        for key in keys:
            dp = data_points[key]
            assert dp.get("device_class") == "power_factor", key
            assert dp.get("state_class") == "measurement", key
