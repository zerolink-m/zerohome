"""WebRTC helper paths: GET /v1/webrtc/ice (ICE servers for the browser)."""
from fastapi import APIRouter, Depends

from app.core.rate_limit import rate_limit
from app.core.response import single
from app.deps.auth import AccessContext, resolve_webrtc_ice_access
from app.services.webrtc_manager.ice import build_client_ice_servers

router = APIRouter(prefix="/v1/webrtc", tags=["webrtc"])


@router.get("/ice", dependencies=[Depends(rate_limit("webrtc_ice", capacity=60, per_minute=60))])
async def get_ice(_: AccessContext = Depends(resolve_webrtc_ice_access)):
    """STUN/TURN for RTCPeerConnection: temporary TURN credentials, valid `ttl` seconds."""
    ice_servers, ttl = build_client_ice_servers()
    return single("ice", {"ice_servers": ice_servers, "ttl": ttl})
