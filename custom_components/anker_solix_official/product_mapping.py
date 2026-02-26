"""Product code to product name mapping.

This module provides mapping from product codes (extracted from SN) to user-friendly product names.
Product code mappings are configured in device YAML files under product_info section.
"""


def extract_product_code_from_sn(sn: str) -> str | None:
    """Extract product code from serial number.

    Rules:
    - 16-digit SN: characters 4-6 (index 3-5, 3 characters)
    - 17-digit SN: characters 4-7 (index 3-6, 4 characters)

    Args:
        sn: Serial number string

    Returns:
        Product code string, or None if SN is invalid

    Example:
        >>> extract_product_code_from_sn("123DMWH4567890123")  # 17-digit
        'DMWH'
        >>> extract_product_code_from_sn("123QNA4567890123")   # 16-digit
        'QNA'
    """
    if not sn or not isinstance(sn, str):
        return None

    # Remove whitespace
    sn = sn.strip()

    # Extract product code based on SN length
    if len(sn) == 16:
        # 16-digit SN: extract characters 4-6 (index 3-5)
        return sn[3:6] if len(sn) >= 6 else None
    elif len(sn) == 17:
        # 17-digit SN: extract characters 4-7 (index 3-6)
        return sn[3:7] if len(sn) >= 7 else None
    else:
        # Invalid SN length
        return None


def get_product_name_from_config(
    sn: str,
    device_config: dict | None = None,
    fallback_name: str | None = None
) -> str:
    """Get user-friendly product name from serial number and device config.

    This function extracts the product code from SN and looks it up in the
    device configuration's product_info section.

    Args:
        sn: Serial number string
        device_config: Device configuration dict (from YAML)
        fallback_name: Fallback name if product code not found (e.g., PN from register 32768)

    Returns:
        User-friendly product name

    Example device_config structure:
        {
            "product_info": {
                "default_name": "Anker SOLIX Solarbank Max AC",
                "product_code_mapping": {
                    "DMWH": "Anker SOLIX Solarbank Max AC",
                    "DNMS": "Anker SOLIX XE AC"
                }
            },
            ...
        }
    """
    # Extract product code from SN
    product_code = extract_product_code_from_sn(sn)

    if product_code and device_config:
        # Get product_info section from config
        product_info = device_config.get("product_info", {})

        # Check product_code_mapping first
        product_code_mapping = product_info.get("product_code_mapping", {})
        if product_code in product_code_mapping:
            return product_code_mapping[product_code]

    # Fallback to default_name from config
    if device_config:
        product_info = device_config.get("product_info", {})
        default_name = product_info.get("default_name")
        if default_name:
            return default_name

    # Final fallback: use provided fallback_name or "Unknown Device"
    return fallback_name if fallback_name else "Unknown Device"
