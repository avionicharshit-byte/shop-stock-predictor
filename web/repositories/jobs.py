"""Checks in progress and their results, in memory only. A finished check is dropped after a while."""
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

HOUR = 3600


class LimitReached(Exception):
    """Too many checks from one visitor, or too many running at once. The message is shown as it is."""


@dataclass
class Job:
    id: str
    owner: str  # the visitor's address, only for the limits
    language: str
    created: float
    state: str = "queued"
    stage: str = "reading"
    progress: float = 0.0
    result: object = None
    problem: str | None = None
    finished: float | None = None
    plan: object = None  # the plan table, kept to word it again in another language
    notes: dict = field(default_factory=dict)  # language -> worded lines
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")


class JobStore:
    def __init__(self, keep_seconds: float, clock=time.monotonic):
        self.keep_seconds, self.clock = keep_seconds, clock
        self._jobs: dict[str, Job] = {}
        self._started: dict[str, deque] = defaultdict(deque)  # visitor -> when each of their checks started
        self._lock = threading.Lock()

    def _forget_old(self, now: float) -> None:
        for job_id in [job_id for job_id, job in self._jobs.items()
                       if not job.active and now - (job.finished or job.created) > self.keep_seconds]:
            del self._jobs[job_id]
        for owner in list(self._started):
            times = self._started[owner]
            while times and now - times[0] > HOUR:
                times.popleft()
            if not times:
                del self._started[owner]

    def _check(self, owner: str, per_hour: int, running_per_owner: int, running_total: int) -> float:
        now = self.clock()
        self._forget_old(now)
        running = [job for job in self._jobs.values() if job.active]
        if len(self._started.get(owner, ())) >= per_hour:
            raise LimitReached(f"You have run {per_hour} checks in the last hour. Please try again a little later.")
        if sum(job.owner == owner for job in running) >= running_per_owner:
            raise LimitReached("Your other checks are still running. Wait for one to finish, then try again.")
        if len(running) >= running_total:
            raise LimitReached("The app is busy with other shops right now. Please try again in a minute.")
        return now

    def check(self, owner: str, per_hour: int, running_per_owner: int, running_total: int) -> None:
        """LimitReached when a new check would be refused, without recording one. Saves reading files for nothing."""
        with self._lock:
            self._check(owner, per_hour, running_per_owner, running_total)

    def create(self, owner: str, language: str, per_hour: int, running_per_owner: int, running_total: int) -> Job:
        """A new queued job, or LimitReached. Checked and recorded under one lock, so two requests cannot both slip in."""
        with self._lock:
            now = self._check(owner, per_hour, running_per_owner, running_total)
            job = Job(id=secrets.token_urlsafe(12), owner=owner, language=language, created=now)
            self._jobs[job.id] = job
            self._started[owner].append(now)
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            self._forget_old(self.clock())
            return self._jobs.get(job_id)

    def update(self, job: Job, **changes) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(job, name, value)
            if not job.active and job.finished is None:
                job.finished = self.clock()
