#!/usr/bin/env python3
"""Generate SHA-256 hash for device PN to identify config files."""

import hashlib

SALT = "anker_solix_ha_2024"

DEVICE_PNS = ["AE103", "A17E2", "AE111", "AE113"]

print("Device PN Hash Mapping")
print("=" * 60)
print(f"{'Device PN':<15} {'SHA-256 Hash':<50}")
print("=" * 60)

for pn in DEVICE_PNS:
    pn_hash = hashlib.sha256((SALT + pn).encode()).hexdigest()
    print(f"{pn:<15} {pn_hash}")

print("=" * 60)
print("\nConfig file mapping:")
for pn in DEVICE_PNS:
    pn_hash = hashlib.sha256((SALT + pn).encode()).hexdigest()
    print(f"  {pn} -> config/{pn_hash}.yaml")
