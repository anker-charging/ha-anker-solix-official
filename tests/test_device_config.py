"""Unit tests for AnkerSolixDeviceConfig.load_device_config_by_file_async."""

from pathlib import Path

import pytest
import yaml

from custom_components.anker_solix_official.device_config import (
    AnkerSolixDeviceConfig,
)


@pytest.fixture
def tmp_yaml_config(tmp_path: Path) -> Path:
    """Write a small valid YAML config file and return its path."""
    config_file = tmp_path / "device.yaml"
    config_file.write_text(
        yaml.safe_dump({"product_info": {"default_name": "Test Device"}}),
        encoding="utf-8",
    )
    return config_file


class TestLoadDeviceConfigByFileAsync:
    """load_device_config_by_file_async(config_file) -> dict | None."""

    async def test_loads_valid_yaml_file(self, tmp_yaml_config: Path) -> None:
        # Arrange
        manager = AnkerSolixDeviceConfig(hass=None)

        # Act
        result = await manager.load_device_config_by_file_async(str(tmp_yaml_config))

        # Assert
        assert result == {"product_info": {"default_name": "Test Device"}}

    async def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        # Arrange
        manager = AnkerSolixDeviceConfig(hass=None)
        missing = tmp_path / "does_not_exist.yaml"

        # Act
        result = await manager.load_device_config_by_file_async(str(missing))

        # Assert
        assert result is None

    async def test_result_is_cached_after_first_load(self, tmp_yaml_config: Path) -> None:
        # Arrange
        manager = AnkerSolixDeviceConfig(hass=None)

        # Act: load once, then delete the file and load again with the
        # exact same path string (cache key) — must still return the value.
        first = await manager.load_device_config_by_file_async(str(tmp_yaml_config))
        tmp_yaml_config.unlink()
        second = await manager.load_device_config_by_file_async(str(tmp_yaml_config))

        # Assert
        assert first == second == {"product_info": {"default_name": "Test Device"}}

    async def test_malformed_yaml_returns_none(self, tmp_path: Path) -> None:
        # Arrange: unbalanced flow-mapping brackets trigger a YAMLError.
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("key: [unterminated", encoding="utf-8")
        manager = AnkerSolixDeviceConfig(hass=None)

        # Act
        result = await manager.load_device_config_by_file_async(str(bad_file))

        # Assert
        assert result is None

    async def test_relative_path_resolves_against_devices_dir_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: devices_dir is <module_dir>/config; its parent is
        # <module_dir>. A relative path must resolve against that parent.
        manager = AnkerSolixDeviceConfig(hass=None)
        monkeypatch.setattr(manager, "devices_dir", tmp_path / "config")
        relative_file = tmp_path / "relative.yaml"
        relative_file.write_text(
            yaml.safe_dump({"a": 1}), encoding="utf-8"
        )

        # Act
        result = await manager.load_device_config_by_file_async("relative.yaml")

        # Assert
        assert result == {"a": 1}
