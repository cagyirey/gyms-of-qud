"""Stable machine-readable failures, distinct from game outcomes."""

class QudGymError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TransportUncertain(QudGymError):
    """A request may have committed. Reconcile; never blindly resend a mutation."""

    def __init__(self, message: str = "Transport failed; request outcome is unknown", *,
                 request_id: str | None = None, replayable: bool = True):
        super().__init__("transport_uncertain", message)
        self.request_id = request_id
        # Pre-dispatch HTTP rejections are not replayable: the handler never ran.
        self.replayable = replayable
