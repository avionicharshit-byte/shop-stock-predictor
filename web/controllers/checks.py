from fastapi import APIRouter, File, Form, Request, UploadFile

from web.controllers.requests_info import client_address
from web.models import JobStatus, NoteRequest, NoteResult
from web.services.stock_check import Upload

router = APIRouter(prefix="/api/checks")


def _upload(file: UploadFile | None, max_bytes: int) -> Upload | None:
    if file is None or not file.filename:
        return None
    # one byte over the cap is enough to know it is too big, the rest is never read
    return Upload(file.filename, file.file.read(max_bytes + 1))


@router.post("", status_code=202)
def start_check(request: Request, sales: UploadFile | None = File(None), stock: UploadFile | None = File(None),
                use_sample: bool = Form(False), language: str | None = Form(None),
                columns: str | None = Form(None)) -> dict:
    max_bytes = request.app.state.settings.max_upload_bytes
    job_id = request.app.state.checks.start(client_address(request), _upload(sales, max_bytes),
                                            _upload(stock, max_bytes), use_sample, columns, language)
    return {"job_id": job_id}


@router.get("/{job_id}", response_model=JobStatus)
def check_status(job_id: str, request: Request) -> JobStatus:
    return request.app.state.checks.status(job_id)


@router.post("/{job_id}/note", response_model=NoteResult)
def reword(job_id: str, body: NoteRequest, request: Request) -> NoteResult:
    return request.app.state.checks.note(job_id, body.language)
