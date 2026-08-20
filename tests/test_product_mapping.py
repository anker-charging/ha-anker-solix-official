"""Unit tests for product_mapping.extract_product_code_from_sn and
get_product_name_from_config."""

from custom_components.anker_solix_official.product_mapping import (
    extract_product_code_from_sn,
    get_product_name_from_config,
)


class TestExtractProductCodeFromSn:
    """extract_product_code_from_sn(sn) -> str | None."""

    def test_17_digit_sn_extracts_chars_4_to_7(self) -> None:
        # Arrange
        sn = "123DMWH4567890123"

        # Act
        result = extract_product_code_from_sn(sn)

        # Assert
        assert result == "DMWH"

    def test_16_digit_sn_extracts_chars_4_to_6(self) -> None:
        # Arrange
        sn = "123QNA4567890123"

        # Act
        result = extract_product_code_from_sn(sn)

        # Assert
        assert result == "QNA"

    def test_strips_surrounding_whitespace_before_length_check(self) -> None:
        # Arrange: 16 significant chars, padded with whitespace that must be
        # stripped before the length check runs.
        sn = "  123QNA4567890123  "

        # Act
        result = extract_product_code_from_sn(sn)

        # Assert
        assert result == "QNA"

    def test_invalid_length_returns_none(self) -> None:
        # Arrange
        sn = "TOO-SHORT"

        # Act
        result = extract_product_code_from_sn(sn)

        # Assert
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        assert extract_product_code_from_sn("") is None

    def test_none_input_returns_none(self) -> None:
        assert extract_product_code_from_sn(None) is None

    def test_non_string_input_returns_none(self) -> None:
        assert extract_product_code_from_sn(12345) is None

    def test_18_digit_sn_is_invalid_length_returns_none(self) -> None:
        # Arrange: neither 16 nor 17 characters.
        sn = "1234567890123456789"

        # Act
        result = extract_product_code_from_sn(sn)

        # Assert
        assert result is None


class TestGetProductNameFromConfig:
    """get_product_name_from_config(sn, device_config, fallback_name) -> str."""

    def test_matches_product_code_mapping(self) -> None:
        # Arrange
        sn = "123DMWH4567890123"
        device_config = {
            "product_info": {
                "default_name": "Anker SOLIX Solarbank Max AC",
                "product_code_mapping": {
                    "DMWH": "Anker SOLIX Solarbank Max AC Special",
                    "DNMS": "Anker SOLIX XE AC",
                },
            }
        }

        # Act
        result = get_product_name_from_config(sn, device_config)

        # Assert
        assert result == "Anker SOLIX Solarbank Max AC Special"

    def test_falls_back_to_default_name_when_code_not_in_mapping(self) -> None:
        # Arrange: SN's product code ("ZZZZ") is absent from the mapping.
        sn = "123ZZZZ4567890123"
        device_config = {
            "product_info": {
                "default_name": "Anker SOLIX Solarbank Max AC",
                "product_code_mapping": {"DMWH": "Solarbank Max AC"},
            }
        }

        # Act
        result = get_product_name_from_config(sn, device_config)

        # Assert
        assert result == "Anker SOLIX Solarbank Max AC"

    def test_falls_back_to_fallback_name_when_no_config(self) -> None:
        # Arrange
        sn = "123DMWH4567890123"

        # Act
        result = get_product_name_from_config(sn, None, fallback_name="Raw PN Value")

        # Assert
        assert result == "Raw PN Value"

    def test_falls_back_to_unknown_device_when_nothing_available(self) -> None:
        # Arrange: invalid SN, no config, no fallback_name provided.
        result = get_product_name_from_config("bad-sn", None, None)

        # Assert
        assert result == "Unknown Device"

    def test_config_without_product_info_section_uses_fallback(self) -> None:
        # Arrange
        sn = "123DMWH4567890123"
        device_config: dict = {}

        # Act
        result = get_product_name_from_config(sn, device_config, fallback_name="PN123")

        # Assert
        assert result == "PN123"

    def test_invalid_sn_with_config_still_falls_back_to_default_name(self) -> None:
        # Arrange: product code extraction fails, but default_name is present.
        device_config = {"product_info": {"default_name": "Generic Anker Device"}}

        # Act
        result = get_product_name_from_config("bad", device_config)

        # Assert
        assert result == "Generic Anker Device"
