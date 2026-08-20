"""
task_queue.py — лёгкий асинхронный менеджер задач.

Позволяет боту запускать несколько поисков параллельно на уже
существующем asyncio-цикле, ограничивая одновременность семафором.

Без импортов telegram. Идентификаторы — через itertools.count(),
временные метки — через time.time().
"""

import asyncio
import itertools
import time

try:
    from config import MAX_CONCURRENT_JOBS
except Exception:  # pragma: no cover - защита на случай проблем с конфигом
    MAX_CONCURRENT_JOBS = 3


class JobManager:
    """Асинхронный менеджер фоновых задач.

    Каждая задача описывается словарём:
        {
            "id":       str,
            "key":      str,
            "meta":     dict,
            "status":   'queued'|'running'|'done'|'error'|'cancelled',
            "created":  float,
            "started":  float|None,
            "finished": float|None,
            "error":    str|None,
        }

    Ссылка на asyncio.Task хранится в приватном ключе "_task"
    (в наружные представления не влияет — используется только внутри).
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_JOBS):
        try:
            mc = int(max_concurrent)
            if mc < 1:
                mc = 1
        except Exception:
            mc = 1
        self.sem = asyncio.Semaphore(mc)
        self.jobs: dict[str, dict] = {}
        self._id = itertools.count(1)

    # ------------------------------------------------------------------ submit
    def submit(self, key: str, coro_factory, meta: dict = None) -> str:
        """Поставить задачу в очередь и запланировать выполнение.

        coro_factory — вызываемое без аргументов, возвращающее свежую
        корутину (fresh coroutine). Возвращает job_id (str). Никогда не
        бросает исключение — при ошибке возвращает "".
        """
        try:
            job_id = str(next(self._id))
            job = {
                "id": job_id,
                "key": key,
                "meta": meta or {},
                "status": "queued",
                "created": time.time(),
                "started": None,
                "finished": None,
                "error": None,
            }
            self.jobs[job_id] = job
            task = asyncio.create_task(self._run(job_id, coro_factory))
            job["_task"] = task
            return job_id
        except Exception:
            return ""

    # -------------------------------------------------------------------- _run
    async def _run(self, job_id: str, coro_factory):
        """Внутренний раннер: держит семафор на время выполнения задачи."""
        job = self.jobs.get(job_id)
        try:
            async with self.sem:
                if job is not None:
                    job["status"] = "running"
                    job["started"] = time.time()
                try:
                    await coro_factory()
                    if job is not None:
                        job["status"] = "done"
                except asyncio.CancelledError:
                    if job is not None:
                        job["status"] = "cancelled"
                    raise
                except Exception as e:
                    if job is not None:
                        job["status"] = "error"
                        job["error"] = str(e)[:300]
                finally:
                    if job is not None:
                        job["finished"] = time.time()
        except asyncio.CancelledError:
            # Отмена могла прийти до входа в семафор — зафиксируем состояние.
            if job is not None:
                if job.get("status") not in ("done", "error"):
                    job["status"] = "cancelled"
                if job.get("finished") is None:
                    job["finished"] = time.time()
        except Exception:
            # Любая иная неожиданная ошибка не должна ронять раннер.
            if job is not None:
                if job.get("status") not in ("done", "cancelled"):
                    job["status"] = "error"
                    if job.get("error") is None:
                        job["error"] = "runner failure"
                if job.get("finished") is None:
                    job["finished"] = time.time()

    # --------------------------------------------------------------------- get
    def get(self, job_id: str) -> dict | None:
        """Вернуть словарь задачи (без внутреннего ключа _task) или None."""
        try:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {k: v for k, v in job.items() if k != "_task"}
        except Exception:
            return None

    # ------------------------------------------------------------- list_active
    def list_active(self) -> list[dict]:
        """Список задач в статусе queued/running (без внутреннего _task)."""
        try:
            result = []
            for job in list(self.jobs.values()):
                if job.get("status") in ("queued", "running"):
                    result.append({k: v for k, v in job.items() if k != "_task"})
            return result
        except Exception:
            return []

    # ------------------------------------------------------------------ cancel
    def cancel(self, job_id: str) -> bool:
        """Отменить задачу. True — если найдена и ещё не завершена."""
        try:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if job.get("status") in ("done", "error", "cancelled"):
                return False
            task = job.get("_task")
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict:
        """Счётчики по статусам."""
        counts = {"queued": 0, "running": 0, "done": 0, "error": 0, "cancelled": 0}
        try:
            for job in list(self.jobs.values()):
                st = job.get("status")
                if st in counts:
                    counts[st] += 1
        except Exception:
            pass
        return counts


# Модульный синглтон.
manager = JobManager()
