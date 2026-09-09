"""Shared single-send HTTP transport and raster signature checks."""
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API-key header or input images to another endpoint.
        return None


def _http_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=headers, method="POST")
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        # Error bodies may echo credentials or input text. Do not read them.
        status = error.code
        error.close()
        return status, b""


def _is_raster(data: bytes, mime_type: str) -> bool:
    """Match file signatures only; decoding and visual review are separate."""
    if not isinstance(data, bytes) or not data:
        return False
    return (
        mime_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")
        or mime_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff")
        or mime_type == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    )
