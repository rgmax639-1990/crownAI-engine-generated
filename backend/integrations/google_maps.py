"""Google Maps adapter used by the Contact page to show the Chennai office.

Credentials are read exclusively from the GOOGLE_MAPS_API_KEY environment
variable -- never hardcoded. If it is missing, calling code gets a clear,
actionable error instead of a silent no-op or an import-time crash.
"""
import os
import urllib.parse

STATIC_MAP_ENDPOINT = "https://maps.googleapis.com/maps/api/staticmap"

CROWNWRIGHT_OFFICE_ADDRESS = "Maraimalar Nagar, Chennai, Tamil Nadu, India"


class GoogleMapsNotConfigured(RuntimeError):
    def __init__(self):
        super().__init__(
            "Google Maps is not configured on this server. Set the GOOGLE_MAPS_API_KEY "
            "environment variable to a real Google Maps Platform API key to enable the office map."
        )


def get_office_static_map_url(*, width: int = 640, height: int = 400, zoom: int = 15) -> str:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise GoogleMapsNotConfigured()
    params = {
        "center": CROWNWRIGHT_OFFICE_ADDRESS,
        "zoom": str(zoom),
        "size": f"{width}x{height}",
        "markers": f"color:red|{CROWNWRIGHT_OFFICE_ADDRESS}",
        "key": api_key,
    }
    return f"{STATIC_MAP_ENDPOINT}?{urllib.parse.urlencode(params)}"
