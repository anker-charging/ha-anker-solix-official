"""Unit tests for AnkerSolixBaseEntity's pure-logic helpers.

A minimal concrete subclass wraps AnkerSolixBaseEntity since it is never
instantiated directly in production (every platform subclasses it). The
coordinator dependency is a lightweight fake exposing only what base_entity.py
reads, so these tests focus on the entity's own gating/condition logic
(availability, capability gate, write-condition operator evaluation) without
depending on real Modbus I/O or a running DataUpdateCoordinator poll loop.
"""

from __future__ import annotations

from typing import Any

import pytest
from custom_components.anker_solix_official.base_entity import AnkerSolixBaseEntity
from custom_components.anker_solix_official.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry


class _ConcreteEntity(AnkerSolixBaseEntity):
    """Minimal concrete subclass; base_entity.py has no entity-platform mixin
    of its own, so a bare subclass is enough to exercise its own methods."""


class _FakeCoordinator:
    """Stand-in exposing only what AnkerSolixBaseEntity reads."""

    def __init__(self, entry) -> None:
        self.entry = entry
        self.device_info = {"model": "Solarbank Max"}
        self.data: dict[str, Any] | None = {}
        self.last_update_success = True
        self._unavailable_registers: set[int] = set()
        self._protected_values: dict[str, tuple[float, Any]] = {}

    def is_connected(self) -> bool:
        return self.last_update_success

    def is_register_available(self, address: int) -> bool:
        return address not in self._unavailable_registers

    def get_data_point_address(self, entity_key: str) -> int | None:
        return None

    def get_protected_value(self, entity_key: str) -> tuple[bool, Any]:
        if entity_key in self._protected_values:
            return True, self._protected_values[entity_key]
        return False, None

    def set_user_selection(self, entity_key: str, value: Any) -> None:
        pass

    def clear_user_selection(self, entity_key: str) -> None:
        pass

    def async_add_listener(self, listener):
        return lambda: None


@pytest.fixture
def entry(hass) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, data={}, unique_id="test-entry")
    e.add_to_hass(hass)
    return e


@pytest.fixture
def fake_coordinator(entry) -> _FakeCoordinator:
    return _FakeCoordinator(entry)


def _make_entity(
    fake_coordinator: _FakeCoordinator,
    entity_key: str = "power",
    config: dict[str, Any] | None = None,
) -> _ConcreteEntity:
    return _ConcreteEntity(fake_coordinator, entity_key, config or {"address": 100})


class TestInit:
    """__init__() attribute assembly from entity_config."""

    def test_unique_id_combines_entry_id_and_entity_key(self, fake_coordinator) -> None:
        entity = _make_entity(fake_coordinator, "power")
        assert entity._attr_unique_id == f"{fake_coordinator.entry.entry_id}_power"

    def test_translation_key_defaults_to_entity_key(self, fake_coordinator) -> None:
        entity = _make_entity(fake_coordinator, "power", {"address": 100})
        assert entity._attr_translation_key == "power"

    def test_translation_key_uses_config_override(self, fake_coordinator) -> None:
        entity = _make_entity(
            fake_coordinator, "power", {"address": 100, "translation_key": "custom_key"}
        )
        assert entity._attr_translation_key == "custom_key"

    def test_icon_set_when_configured(self, fake_coordinator) -> None:
        entity = _make_entity(
            fake_coordinator, "power", {"address": 100, "icon": "mdi:flash"}
        )
        assert entity._attr_icon == "mdi:flash"

    def test_icon_unset_when_not_configured(self, fake_coordinator) -> None:
        entity = _make_entity(fake_coordinator, "power", {"address": 100})
        assert not hasattr(entity, "_attr_icon")

    def test_device_info_copied_from_coordinator(self, fake_coordinator) -> None:
        entity = _make_entity(fake_coordinator)
        assert entity._attr_device_info == fake_coordinator.device_info


class TestAvailable:
    """available() gate derived from the coordinator's last refresh outcome."""

    def test_failed_last_refresh_makes_entity_unavailable(
        self, fake_coordinator
    ) -> None:
        fake_coordinator.last_update_success = False
        entity = _make_entity(fake_coordinator)
        assert entity.available is False

    def test_successful_last_refresh_makes_entity_available(
        self, fake_coordinator
    ) -> None:
        fake_coordinator.last_update_success = True
        entity = _make_entity(fake_coordinator)
        assert entity.available is True

    def test_available_even_when_own_register_is_unreadable(
        self, fake_coordinator
    ) -> None:
        # Arrange: a register read failure must NEVER hide the entity --
        # only the capability mask decides visibility, per the documented
        # design in _log_unreadable_register.
        fake_coordinator.last_update_success = True
        fake_coordinator._unavailable_registers.add(100)
        entity = _make_entity(fake_coordinator, "power", {"address": 100})

        # Act & Assert
        assert entity.available is True


class TestIsCapabilitySupported:
    """_is_capability_supported() fail-open capability-mask gating."""

    def test_no_capability_entity_configured_defaults_to_true(
        self, fake_coordinator
    ) -> None:
        entity = _make_entity(fake_coordinator, "power", {"address": 100})
        assert entity._is_capability_supported() is True

    def test_mask_not_yet_read_fails_open_to_true(self, fake_coordinator) -> None:
        # Arrange: capability_entity configured but absent from coordinator.data.
        fake_coordinator.data = {}
        entity = _make_entity(
            fake_coordinator,
            "backup_reserve",
            {"address": 100, "capability_entity": "cap_mask", "capability_bit": 2},
        )

        # Act & Assert
        assert entity._is_capability_supported() is True

    def test_mask_register_unavailable_fails_open_to_true(self, fake_coordinator) -> None:
        # Arrange: mask was configured with an address, but that address is
        # currently marked unavailable.
        fake_coordinator.data = {"cap_mask": 5}
        fake_coordinator.get_data_point_address = lambda key: 200
        fake_coordinator._unavailable_registers.add(200)
        entity = _make_entity(
            fake_coordinator,
            "backup_reserve",
            {"address": 100, "capability_entity": "cap_mask", "capability_bit": 2},
        )

        # Act & Assert
        assert entity._is_capability_supported() is True

    def test_bit_set_in_mask_returns_true(self, fake_coordinator) -> None:
        # Arrange: bit 2 (value 0b100 = 4) is set in the mask.
        fake_coordinator.data = {"cap_mask": 0b0100}
        entity = _make_entity(
            fake_coordinator,
            "backup_reserve",
            {"address": 100, "capability_entity": "cap_mask", "capability_bit": 2},
        )

        # Act & Assert
        assert entity._is_capability_supported() is True

    def test_bit_unset_in_mask_returns_false(self, fake_coordinator) -> None:
        # Arrange: bit 2 is NOT set.
        fake_coordinator.data = {"cap_mask": 0b0011}
        entity = _make_entity(
            fake_coordinator,
            "backup_reserve",
            {"address": 100, "capability_entity": "cap_mask", "capability_bit": 2},
        )

        # Act & Assert
        assert entity._is_capability_supported() is False

    def test_non_integer_mask_value_fails_open_to_true(self, fake_coordinator) -> None:
        # Arrange: mask read succeeded but decoded to garbage.
        fake_coordinator.data = {"cap_mask": "not-a-number"}
        entity = _make_entity(
            fake_coordinator,
            "backup_reserve",
            {"address": 100, "capability_entity": "cap_mask", "capability_bit": 2},
        )

        # Act & Assert
        assert entity._is_capability_supported() is True


class TestGetRawValue:
    """_get_raw_value() write-protection-aware data accessor."""

    def test_returns_coordinator_value_when_not_protected(self, fake_coordinator) -> None:
        fake_coordinator.data = {"power": 1500}
        entity = _make_entity(fake_coordinator, "power")
        assert entity._get_raw_value() == 1500

    def test_returns_protected_value_when_active(self, fake_coordinator) -> None:
        fake_coordinator.data = {"power": 1500}
        fake_coordinator._protected_values["power"] = 9999
        entity = _make_entity(fake_coordinator, "power")
        assert entity._get_raw_value() == 9999

    def test_returns_default_when_coordinator_data_is_none(self, fake_coordinator) -> None:
        fake_coordinator.data = None
        entity = _make_entity(fake_coordinator, "power")
        assert entity._get_raw_value(default="fallback") == "fallback"

    def test_returns_default_when_key_absent(self, fake_coordinator) -> None:
        fake_coordinator.data = {"other": 1}
        entity = _make_entity(fake_coordinator, "power")
        assert entity._get_raw_value(default=0) == 0


class TestCheckWriteCondition:
    """_check_write_condition() gate evaluation against a reference register."""

    def test_no_condition_configured_always_passes(self, fake_coordinator) -> None:
        entity = _make_entity(fake_coordinator, "power", {"address": 100})
        passed, hint = entity._check_write_condition()
        assert passed is True
        assert hint is None

    def test_coordinator_data_none_fails_with_hint(self, fake_coordinator) -> None:
        fake_coordinator.data = None
        entity = _make_entity(
            fake_coordinator,
            "power",
            {
                "address": 100,
                "write_condition": {"entity": "mode", "operator": "eq", "value": 1, "hint": "mode_wrong"},
            },
        )
        passed, hint = entity._check_write_condition()
        assert passed is False
        assert hint == "mode_wrong"

    def test_missing_reference_entity_key_passes_unconditionally(
        self, fake_coordinator
    ) -> None:
        # Arrange: write_condition dict present but has no "entity" key.
        # coordinator.data must be non-empty here: `if not self.coordinator.data`
        # in the source treats an empty dict the same as None, which would
        # short-circuit on the earlier branch instead of the one under test.
        fake_coordinator.data = {"unrelated": 1}
        entity = _make_entity(
            fake_coordinator, "power", {"address": 100, "write_condition": {"hint": "x"}}
        )
        passed, hint = entity._check_write_condition()
        assert passed is True
        assert hint is None

    def test_reference_value_missing_fails_with_hint(self, fake_coordinator) -> None:
        fake_coordinator.data = {}
        entity = _make_entity(
            fake_coordinator,
            "power",
            {
                "address": 100,
                "write_condition": {"entity": "mode", "operator": "eq", "value": 1, "hint": "mode_missing"},
            },
        )
        passed, hint = entity._check_write_condition()
        assert passed is False
        assert hint == "mode_missing"

    def test_non_numeric_reference_value_fails_with_hint(self, fake_coordinator) -> None:
        fake_coordinator.data = {"mode": "not-a-number"}
        entity = _make_entity(
            fake_coordinator,
            "power",
            {
                "address": 100,
                "write_condition": {"entity": "mode", "operator": "eq", "value": 1, "hint": "bad_mode"},
            },
        )
        passed, hint = entity._check_write_condition()
        assert passed is False
        assert hint == "bad_mode"

    def test_condition_that_evaluates_true_passes(self, fake_coordinator) -> None:
        fake_coordinator.data = {"mode": 1}
        entity = _make_entity(
            fake_coordinator,
            "power",
            {
                "address": 100,
                "write_condition": {"entity": "mode", "operator": "eq", "value": 1, "hint": "x"},
            },
        )
        passed, hint = entity._check_write_condition()
        assert passed is True
        assert hint is None

    def test_condition_that_evaluates_false_fails_with_hint(self, fake_coordinator) -> None:
        fake_coordinator.data = {"mode": 2}
        entity = _make_entity(
            fake_coordinator,
            "power",
            {
                "address": 100,
                "write_condition": {"entity": "mode", "operator": "eq", "value": 1, "hint": "wrong_mode"},
            },
        )
        passed, hint = entity._check_write_condition()
        assert passed is False
        assert hint == "wrong_mode"


class TestEvaluateOperator:
    """_evaluate_operator() static comparison logic across all operators."""

    def test_none_target_always_passes(self) -> None:
        assert AnkerSolixBaseEntity._evaluate_operator(5.0, "eq", None) is True

    def test_eq_with_integer_target_uses_half_tolerance(self) -> None:
        assert AnkerSolixBaseEntity._evaluate_operator(5.0, "eq", 5) is True
        assert AnkerSolixBaseEntity._evaluate_operator(5.4, "eq", 5) is True
        assert AnkerSolixBaseEntity._evaluate_operator(5.6, "eq", 5) is False

    def test_eq_with_float_target_uses_tight_tolerance(self) -> None:
        # Arrange: 5.0 is a whole-number float, so the "target == int(target)"
        # check routes it through the loose 0.5 tolerance branch too -- only
        # a genuinely fractional target (5.5) exercises the tight 1e-6 path.
        assert AnkerSolixBaseEntity._evaluate_operator(5.0, "eq", 5.0000001) is True
        assert AnkerSolixBaseEntity._evaluate_operator(5.6, "eq", 5.5) is False
        assert AnkerSolixBaseEntity._evaluate_operator(5.5, "eq", 5.5) is True

    def test_ne_is_the_inverse_of_eq(self) -> None:
        assert AnkerSolixBaseEntity._evaluate_operator(5.0, "ne", 5) is False
        assert AnkerSolixBaseEntity._evaluate_operator(6.0, "ne", 5) is True

    def test_gt_gte_lt_lte(self) -> None:
        assert AnkerSolixBaseEntity._evaluate_operator(6.0, "gt", 5) is True
        assert AnkerSolixBaseEntity._evaluate_operator(5.0, "gt", 5) is False
        assert AnkerSolixBaseEntity._evaluate_operator(5.0, "gte", 5) is True
        assert AnkerSolixBaseEntity._evaluate_operator(4.0, "lt", 5) is True
        assert AnkerSolixBaseEntity._evaluate_operator(5.0, "lte", 5) is True

    def test_in_operator_with_list_target(self) -> None:
        assert AnkerSolixBaseEntity._evaluate_operator(2.0, "in", [1, 2, 3]) is True
        assert AnkerSolixBaseEntity._evaluate_operator(9.0, "in", [1, 2, 3]) is False

    def test_in_operator_with_scalar_target_is_wrapped_into_a_list(self) -> None:
        # Arrange: a bare scalar target for "in" must still work via the
        # single-element-list coercion.
        assert AnkerSolixBaseEntity._evaluate_operator(2.0, "in", 2) is True

    def test_not_in_operator_is_the_inverse_of_in(self) -> None:
        assert AnkerSolixBaseEntity._evaluate_operator(9.0, "not_in", [1, 2, 3]) is True
        assert AnkerSolixBaseEntity._evaluate_operator(2.0, "not_in", [1, 2, 3]) is False

    def test_non_numeric_target_that_cannot_convert_passes_unconditionally(self) -> None:
        # Arrange: for non-list operators, an unconvertible target is a
        # configuration error the entity fails open on rather than crashing.
        assert AnkerSolixBaseEntity._evaluate_operator(5.0, "gt", "not-a-number") is True

    def test_unknown_operator_defaults_to_true(self) -> None:
        assert AnkerSolixBaseEntity._evaluate_operator(5.0, "unknown_op", 5) is True
