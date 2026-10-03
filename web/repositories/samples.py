"""The sample sales and stock files that ship with the app."""
from pathlib import Path

from web.config import ROOT
from web.repositories.uploads import UploadedFile

SAMPLES = {"sales": ROOT / "data" / "sample_sales.csv", "stock": ROOT / "data" / "sample_stock.csv"}


class SampleRepository:
    def __init__(self, paths: dict[str, Path] = SAMPLES):
        self.paths = paths

    def path(self, kind: str) -> Path:
        return self.paths[kind]

    def file(self, kind: str) -> UploadedFile:
        path = self.paths[kind]
        return UploadedFile(path.name, path.read_bytes())
