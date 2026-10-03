"""The sample sales and stock files that ship with the app, and one recorded run of them."""
import logging
import threading
from pathlib import Path

from pydantic import ValidationError

from web.config import ROOT
from web.models import RecordedSample
from web.repositories.uploads import UploadedFile

log = logging.getLogger(__name__)
SAMPLES = {"sales": ROOT / "data" / "sample_sales.csv", "stock": ROOT / "data" / "sample_stock.csv"}
RECORDED = ROOT / "data" / "sample_result.json"


class SampleRepository:
    def __init__(self, paths: dict[str, Path] = SAMPLES, recorded: Path | None = RECORDED):
        """recorded is the saved run of the sample, None to always run it live."""
        self.paths, self.recorded_path = paths, recorded
        self._recorded: RecordedSample | None = None
        self._lock = threading.Lock()

    def path(self, kind: str) -> Path:
        return self.paths[kind]

    def file(self, kind: str) -> UploadedFile:
        path = self.paths[kind]
        return UploadedFile(path.name, path.read_bytes())

    def recorded(self) -> RecordedSample | None:
        """The saved run, read once and shared by every visitor. None when there is none."""
        if self.recorded_path is None:
            return None
        with self._lock:
            if self._recorded is None and self.recorded_path.exists():
                try:
                    self._recorded = RecordedSample.model_validate_json(self.recorded_path.read_bytes())
                except (OSError, ValidationError) as error:
                    log.warning("the recorded sample could not be read: %s", type(error).__name__)
            return self._recorded
