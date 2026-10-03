from fastapi import Request


def client_address(request: Request) -> str:
    """The visitor's address. Render sits behind a proxy, so the first X-Forwarded-For hop comes first."""
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first or (request.client.host if request.client else "unknown")
