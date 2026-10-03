from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/sample")


@router.get("/{kind}")
def sample(kind: str, request: Request) -> FileResponse:
    if kind not in ("sales", "stock"):
        raise HTTPException(404)
    path = request.app.state.samples.path(kind)
    return FileResponse(path, media_type="text/csv", filename=path.name)
