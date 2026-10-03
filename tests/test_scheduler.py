import asyncio, os, sys, unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")


class Scheduler(unittest.TestCase):
    def _pending(self):
        return main.build_scheduler(object()).get_jobs()

    def test_job_is_coroutine_function_not_lambda(self):
        job = self._pending()[0]
        self.assertTrue(asyncio.iscoroutinefunction(job.func))

    def test_fires_at_10_and_15_moscow_on_weekdays(self):
        trigger = self._pending()[0].trigger
        # пятница 2026-10-02, 09:00 МСК → ближайший запуск 10:00 МСК
        t = trigger.get_next_fire_time(None, datetime(2026, 10, 2, 9, 0, tzinfo=MSK))
        self.assertEqual((t.hour, t.minute), (10, 0))
        self.assertEqual(t.utcoffset().total_seconds(), 3 * 3600)
        t = trigger.get_next_fire_time(t, t)
        self.assertEqual((t.hour, t.minute), (15, 0))
        # после 15:00 в пятницу — понедельник 10:00 МСК
        t = trigger.get_next_fire_time(t, t)
        self.assertEqual((t.date().isoformat(), t.hour), ("2026-10-05", 10))


class RunsInEventLoop(unittest.IsolatedAsyncioTestCase):
    async def test_job_actually_runs_without_event_loop_error(self):
        """Регресс: lambda + ensure_future падали с «no current event loop»."""
        called = asyncio.Event()

        async def fake_checks(bot):
            called.set()

        with mock.patch.object(main, "run_all_checks", fake_checks):
            sched = main.build_scheduler("BOT")
        sched.start()
        try:
            sched.modify_job(sched.get_jobs()[0].id, next_run_time=datetime.now(MSK))
            await asyncio.wait_for(called.wait(), timeout=5)
        finally:
            sched.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
