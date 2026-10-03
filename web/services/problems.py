class Problem(Exception):
    """A request the service cannot do, with the HTTP status and the JSON body to answer."""

    def __init__(self, status: int, problem: str | None, **extra):
        super().__init__(problem or "")
        self.status, self.body = status, {"problem": problem, **extra}
