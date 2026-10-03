"""Where TabPFN runs: Prior Labs' hosted API (the web default) or TabPFN v2 on this machine."""
import importlib.util
import logging
import threading

from predictor import HostedTabPFN, LocalTabPFN

log = logging.getLogger(__name__)


class ModelNotConfigured(Exception):
    """No forecasting model is set up on this server. The message is shown as it is."""


class ForecastingGateway:
    def __init__(self, backend: str, token: str | None, workers: int = 4, make_provider=None):
        """make_provider replaces the real model, for tests."""
        self.backend, self.token, self.workers, self.make_provider = backend, token, workers, make_provider
        self._token_set = False
        self._lock = threading.Lock()

    def status(self) -> str:
        if self.make_provider is not None:
            return self.backend
        if self.backend == "api":
            return "api" if self.token else "off"
        if self.backend == "local":
            found = all(importlib.util.find_spec(name) for name in ("torch", "tabpfn"))
            return "local" if found else "off"
        return "off"

    def _set_token(self) -> None:
        with self._lock:
            if not self._token_set:
                import tabpfn_client
                # the token stays in this process, tabpfn-client never asks for a login this way
                tabpfn_client.set_access_token(self.token)
                self._token_set = True

    def warm_up(self) -> threading.Thread | None:
        """Loads tabpfn-client and opens its connection in the background, so the first visitor does not wait."""
        if self.make_provider is not None or self.status() != "api":
            return None

        def load() -> None:
            try:
                self._set_token()
                from tabpfn_client.client import ServiceClient
                ServiceClient.get_settings()  # a free settings read, it also opens the connection
                log.info("tabpfn-client ready")
            except Exception as error:  # the first check will try again
                log.warning("tabpfn-client warm-up failed: %s", type(error).__name__)

        thread = threading.Thread(target=load, name="tabpfn-warm-up", daemon=True)
        thread.start()
        return thread

    def provider(self):
        if self.make_provider is not None:
            return self.make_provider()
        status = self.status()
        if status == "api":
            self._set_token()
            return HostedTabPFN(self.workers)
        if status == "local":
            return LocalTabPFN()
        if self.backend == "local":
            raise ModelNotConfigured("TabPFN is not configured on this server: TABPFN_BACKEND is local "
                                     "but torch and tabpfn are not installed.")
        raise ModelNotConfigured("TabPFN is not configured on this server, so it cannot predict yet. "
                                 "The owner needs to set TABPFN_TOKEN.")
