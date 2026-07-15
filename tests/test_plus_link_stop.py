from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services import plus_link_store


class PlusLinkStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = plus_link_store.PlusLinkService(Path(self.temp_dir.name) / "plus_link.json")
        self.service._state = plus_link_store._default_state()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_stop_cancels_accounts_that_were_not_submitted(self) -> None:
        accounts = [
            {"email": f"user{index}@example.com", "access_token": f"token-{index}"}
            for index in range(1, 6)
        ]
        cfg = plus_link_store._normalize_config({"concurrency": 1})
        self.service._state["enabled"] = True
        self.service._state["stats"] = {
            **plus_link_store._default_state()["stats"],
            "total": len(accounts),
            "started_at": plus_link_store._now(),
        }
        started = threading.Event()
        release = threading.Event()
        submitted: list[int] = []

        def fake_run(task_id: str, account: dict[str, str], _cfg: dict, index: int) -> None:
            submitted.append(index)
            started.set()
            release.wait(5)
            self.service._set_task_result(task_id, {"status": "success", "stage": "完成"})
            with self.service._lock:
                stats = self.service._state["stats"]
                stats["success"] += 1
                stats["done"] += 1
                stats["running"] -= 1

        with (
            patch.object(plus_link_store, "_load_plus_engine", return_value=object()),
            patch.object(self.service, "_run_one", side_effect=fake_run),
        ):
            runner = threading.Thread(target=self.service._runner_main, args=(accounts, cfg))
            runner.start()
            self.assertTrue(started.wait(2))
            self.service.stop()
            release.set()
            runner.join(5)

        self.assertFalse(runner.is_alive())
        self.assertEqual(submitted, [1])
        results = self.service._state["results"]
        self.assertEqual(sum(item["status"] == "success" for item in results), 1)
        self.assertEqual(sum(item["status"] == "cancelled" for item in results), 4)
        self.assertEqual(self.service._state["stats"]["cancelled"], 4)
        self.assertEqual(self.service._state["stats"]["done"], 5)
        self.assertEqual(self.service._state["stats"]["running"], 0)

    def test_worker_honors_stop_before_proxy_or_engine_work(self) -> None:
        task_id = "plus-stop-before-start"
        self.service._state["results"] = [{"task_id": task_id, "email": "user@example.com", "status": "queued"}]
        self.service._state["stats"]["running"] = 1
        self.service._stop_event.set()

        with patch.object(self.service, "_payload_for_account") as payload:
            self.service._run_one(
                task_id,
                {"email": "user@example.com", "access_token": "token"},
                plus_link_store._normalize_config({}),
                1,
            )

        payload.assert_not_called()
        self.assertEqual(self.service._state["results"][0]["status"], "cancelled")
        self.assertEqual(self.service._state["stats"]["cancelled"], 1)
        self.assertEqual(self.service._state["stats"]["running"], 0)


if __name__ == "__main__":
    unittest.main()
