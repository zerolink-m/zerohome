"""Send a single physical opening command to Ufanet Smart Home."""
from urllib.parse import quote

import httpx


async def open_shared(contract: str, password: str, external_id: str) -> bool:
    from ufanet_async import UfanetClient, UfanetConfig

    async with UfanetClient(UfanetConfig(contract=contract, password=password)) as client:
        smart = client.smart_home
        await smart.login()
        if not await smart.is_authenticated():
            raise RuntimeError("Ufanet authentication failed")
        devices = await smart.get_skuds()
        if not await smart.is_authenticated():
            raise RuntimeError("Ufanet session expired")
        if not any(device.id == external_id and device.shared is True for device in devices):
            raise ValueError("Shared device is unavailable")

        base = str(smart.base_url).rstrip("/")
        cookies = {cookie.key: cookie.value for cookie in smart.session.cookie_jar}
        path = f"/api/v0/skud/shared/{quote(external_id, safe='')}/open/"
        # HTTPX's default transport has zero connection retries. Redirects are
        # disabled so the physical command is sent at most once.
        async with httpx.AsyncClient(cookies=cookies, follow_redirects=False, timeout=10.0) as http:
            response = await http.get(base + path, headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": base + "/office/skud/",
            })
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or type(result.get("result")) is not bool:
                raise RuntimeError("Unexpected Ufanet response")
            return result["result"]
