import asyncio
import logging
import time

_LOGGER = logging.getLogger(__name__)

MDNS_SERVICE_TYPE = "_anker_power._udp.local."
MDNS_SCAN_TIMEOUT = 5


def _discover_devices_sync(timeout: int = MDNS_SCAN_TIMEOUT) -> list[dict]:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceListener

    class _Listener(ServiceListener):
        def __init__(self):
            self.devices: list[dict] = []

        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if info is None:
                return

            server = info.server or ""
            mac = ""
            if "Anker-Device_" in server:
                mac = server.split("Anker-Device_")[1].replace(".local", "")

            ips = info.parsed_addresses()
            props = info.decoded_properties
            sn = props.get("sn", "") or ""

            # update_service fires on every mDNS TTL refresh (common within a
            # single scan window). Remove any existing entry for this SN
            # before appending, otherwise the same device accumulates
            # duplicate entries in self.devices.
            if sn:
                self.devices = [d for d in self.devices if d["sn"] != sn]

            self.devices.append(
                {
                    "sn": sn,
                    "pn": props.get("pn", "") or "",
                    "ip": ips[0] if ips else "",
                    "ips": ips,
                    "mac": mac,
                    "port": info.port,
                    "properties": props,
                }
            )

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf(use_asyncio=False)
    listener = _Listener()
    browser = ServiceBrowser(zc, MDNS_SERVICE_TYPE, listener)
    try:
        time.sleep(timeout)
    except Exception:
        pass
    finally:
        browser.cancel()
        zc.close()

    return listener.devices


async def find_device_ip_by_sn(sn: str, timeout: int = MDNS_SCAN_TIMEOUT) -> str | None:
    if not sn or len(sn) < 8:
        return None

    loop = asyncio.get_event_loop()
    try:
        devices = await loop.run_in_executor(None, _discover_devices_sync, timeout)
    except Exception:
        return None

    for dev in devices:
        if dev["sn"] == sn:
            ip = dev["ip"]
            if ip:
                return ip

    return None
