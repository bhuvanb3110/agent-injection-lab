"""Typed errors that decide what happens to a run (DECISIONS.md D5, D11).

None of these is ever converted into a score inside a run. They propagate out of `run_case`
and the runner decides: re-queue (InfraError), record as a model error (ModelError), or stop
the experiment (ConfigError).
"""


class InfraError(Exception):
    """The provider or the network failed: 429, 5xx, timeout, connection error.

    Says nothing about the model, so the run is never scored; the runner re-queues it.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code  # None for timeouts and connection errors
        self.retry_after_s = retry_after_s  # from the Retry-After header, when sent


class ModelError(Exception):
    """The provider rejected the request or returned something unusable (other 4xx, no choices).

    Retrying the same request would fail the same way, so it is not re-queued.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        subtype: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        # Stored as the run's outcome subtype (D21): "http_400", "no_choices", ...
        self.subtype = subtype
        self.status_code = status_code


class ConfigError(Exception):
    """Our setup is wrong: missing key, bad key (401/403), unknown model (404), unknown provider.

    It would fail every run identically, so the experiment stops instead of scoring anything.
    """
