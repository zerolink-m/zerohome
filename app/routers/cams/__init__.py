"""Cams router package. Modules register routes on the shared router in the original order."""
from app.routers.cams._common import router  # noqa: F401
from app.routers.cams import crud, stream_ptz, recordings_routes, hls, webrtc  # noqa: F401,E402
