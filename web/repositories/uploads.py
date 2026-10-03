"""Uploaded files, kept in memory only and read through intake. Never written to disk, never logged."""
import subprocess
from pathlib import Path

from core.intake import DataProblem, OpenedFile, clean_sales, clean_stock, open_sales, open_stock


class FileTooBig(DataProblem):
    """An upload over the size cap."""


class UploadedFile:
    """The bytes of one upload, shaped like the file object intake already reads (name and getvalue)."""

    def __init__(self, name: str, data: bytes):
        self.name, self._data = Path(name or "").name, data

    def getvalue(self) -> bytes:
        return self._data


class FileRepository:
    def __init__(self, file_types: tuple[str, ...], max_mb: float):
        self.file_types, self.max_mb, self.max_bytes = file_types, max_mb, int(max_mb * 1024 * 1024)

    def accept(self, label: str, name: str, data: bytes) -> UploadedFile:
        if not name.lower().endswith(self.file_types):
            raise DataProblem(f"The {label} file must be a PDF, Excel or CSV file.")
        if len(data) > self.max_bytes:
            raise FileTooBig(f"The {label} file is over {self.max_mb:g} MB. "
                              "Export a shorter date range and try again.")
        return UploadedFile(name, data)

    @staticmethod
    def _open(read, file) -> OpenedFile:
        try:
            return read(file)
        except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
            # pdftotext failed, timed out or is missing
            raise DataProblem(f"Could not read {Path(file.name).name}. Export the report as Excel or CSV instead.") from None

    def open_sales(self, file) -> OpenedFile:
        return self._open(open_sales, file)

    def open_stock(self, file) -> OpenedFile:
        return self._open(open_stock, file)

    @staticmethod
    def clean_sales(opened: OpenedFile, chosen: dict):
        return clean_sales(opened, chosen)

    @staticmethod
    def clean_stock(opened: OpenedFile, chosen: dict):
        return clean_stock(opened, chosen)
