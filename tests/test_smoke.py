"""Sanity check that the pytest-homeassistant-custom-component plugin and
custom component package import cleanly under the configured test harness."""

from custom_components.anker_solix_official.const import DOMAIN


def test_domain_constant() -> None:
    """DOMAIN must match the manifest.json domain (enforced by HA at load time)."""
    assert DOMAIN == "anker_solix_official"


async def test_hass_fixture_available(hass) -> None:  # noqa: ANN001 - fixture type from plugin
    """The pytest-homeassistant-custom-component `hass` fixture must be usable."""
    assert hass is not None
    assert hass.config.components is not None
