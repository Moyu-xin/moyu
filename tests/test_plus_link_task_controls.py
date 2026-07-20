from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services import plus_link_store


class PlusLinkTaskControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = plus_link_store.PlusLinkService(Path(self.temp_dir.name) / "plus_link.json")
        self.service._state = plus_link_store._default_state()
        self.cfg = plus_link_store._normalize_config({"concurrency": 1})
        self.account = {"email": "control@example.com", "access_token": "token"}
        self.task = self.service._new_task(self.account, self.cfg, 1)
        self.service._state["results"] = [{
            "task_id": self.task["task_id"],
            "email": self.account["email"],
            "status": "queued",
            "stage": "排队中",
        }]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_queued_task_can_pause_and_resume(self) -> None:
        paused = self.service.pause_task(self.task["task_id"])
        self.assertEqual(paused["results"][0]["status"], "paused")
        self.assertFalse(self.task["resume_event"].is_set())

        resumed = self.service.resume_task(self.task["task_id"])
        self.assertEqual(resumed["results"][0]["status"], "queued")
        self.assertTrue(self.task["resume_event"].is_set())

    def test_running_task_pauses_at_cooperative_checkpoint(self) -> None:
        self.task["started"] = True
        self.service._state["results"][0]["status"] = "running"
        self.service.pause_task(self.task["task_id"])

        finished = threading.Event()

        def wait_for_resume() -> None:
            self.service._wait_for_task_ready(self.task["task_id"], self.account["email"], running=True)
            finished.set()

        worker = threading.Thread(target=wait_for_resume)
        worker.start()
        deadline = time.time() + 2
        while time.time() < deadline and self.service._state["results"][0]["status"] != "paused":
            time.sleep(0.02)
        self.assertEqual(self.service._state["results"][0]["status"], "paused")
        self.assertFalse(finished.is_set())

        self.service.resume_task(self.task["task_id"])
        worker.join(2)
        self.assertTrue(finished.is_set())

    def test_queued_task_cancel_updates_stats_once(self) -> None:
        self.service._state["stats"]["total"] = 1
        state = self.service.cancel_task(self.task["task_id"])
        self.assertEqual(state["results"][0]["status"], "cancelled")
        self.assertEqual(state["stats"]["cancelled"], 1)
        self.assertEqual(state["stats"]["done"], 1)

        state = self.service.cancel_task(self.task["task_id"])
        self.assertEqual(state["stats"]["cancelled"], 1)

    def test_global_stop_cancels_a_paused_queued_task(self) -> None:
        self.service._state["stats"]["total"] = 1
        self.service.pause_task(self.task["task_id"])
        cancelled = self.service._cancel_queued_tasks([self.task], reason="用户停止任务")
        self.assertEqual(cancelled, 1)
        self.assertEqual(self.service._state["results"][0]["status"], "cancelled")

    def test_retry_requeues_terminal_task_with_same_account_and_config(self) -> None:
        self.service._state["results"][0]["status"] = "failed"
        self.service._state["stats"]["total"] = 1
        release = threading.Event()
        runner = threading.Thread(target=lambda: release.wait(2))
        runner.start()
        self.service._runner = runner
        try:
            state = self.service.retry_task(self.task["task_id"])
        finally:
            release.set()
            runner.join(2)

        self.assertEqual(len(state["results"]), 1)
        retry = state["results"][0]
        self.assertEqual(retry["task_id"], self.task["task_id"])
        self.assertEqual(retry["status"], "queued")
        self.assertEqual(retry["retry_count"], 1)
        self.assertEqual(retry["matrix_index"], 1)
        self.assertEqual(state["stats"]["total"], 1)
        self.assertEqual(state["stats"]["done"], 0)
        self.assertEqual(len(self.service._retry_queue), 1)
        queued_task = self.service._retry_queue[0]
        self.assertEqual(queued_task["task_id"], self.task["task_id"])
        self.assertEqual(queued_task["account"]["email"], self.account["email"])
        self.assertTrue(queued_task["cfg"]["country_rotation_enabled"])

    def test_missing_matrix_value_defaults_to_enabled(self) -> None:
        self.assertTrue(plus_link_store._normalize_config({"country_rotation_enabled": None})["country_rotation_enabled"])
        self.assertEqual(plus_link_store._normalize_config({})["matrix_combo_retry_limit"], 1)

    def test_each_account_walks_every_matrix_combo_with_per_combo_retry_limit(self) -> None:
        cfg = plus_link_store._normalize_config({
            "country_rotation_enabled": True,
            "billing_country_rotation": ["US", "DE"],
            "proxy_country_rotation": ["JP", "TR"],
            "matrix_combo_retry_limit": 3,
            "refresh_tokens": False,
        })
        self.service._state["stats"].update({"total": 1, "running": 1})
        seen: list[tuple[str, str, int]] = []

        def assign_proxy(_cfg, matrix_index, *, left_country, promotion_country):
            proxy = f"http://proxy-{matrix_index}.test:8080"
            return {
                "proxy": proxy,
                "checkout_proxy": proxy,
                "provider_proxy": proxy,
                "approve_proxy": proxy,
                "promotion_proxy": proxy,
                "checkout_proxy_pool": [proxy],
                "provider_proxy_pool": [proxy],
                "approve_proxy_pool": [proxy],
                "promotion_proxy_pool": [proxy],
                "source": "test",
                "label": f"{left_country}+{promotion_country}",
            }

        def generate(payload, *, task_id, email):
            seen.append((payload["billing_country"], payload["promotion_country"], payload["provider_retry_limit"]))
            if len(seen) < 4:
                raise RuntimeError("combo failed")
            return {
                "ok": True,
                "long_url": "https://example.test/approve",
                "link_attempt_count": 2,
                "link_attempt_limit": 3,
            }

        with patch.object(self.service, "_assign_residential_proxy", side_effect=assign_proxy), \
             patch.object(self.service, "_assign_project_parent_proxy", return_value=None), \
             patch.object(self.service, "_precheck_account_eligibility", return_value={"eligible": True, "account_type": "free", "has_email": True}), \
             patch.object(self.service, "_generate_local_long_link", side_effect=generate):
            self.service._run_one(self.task["task_id"], self.account, cfg, 99)

        self.assertEqual(seen, [("US", "JP", 3), ("US", "TR", 3), ("DE", "JP", 3), ("DE", "TR", 3)])
        result = self.service._state["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["matrix_index"], 4)
        self.assertEqual(result["matrix_total"], 4)
        self.assertEqual(result["matrix_failures"], 3)

    def test_engine_progress_tracks_attempt_and_keeps_left_and_right_proxy_ips(self) -> None:
        class FakeRequest:
            @classmethod
            def model_validate(cls, payload):
                return payload

        class FakeCancelContext:
            event = None

        class FakeEngine:
            LongLinkRequest = FakeRequest
            CANCEL_CONTEXT = FakeCancelContext()

            @staticmethod
            def generate_long_link_payload(req, emit):
                emit("link_attempt", "attempt", link_attempt_count=2, link_attempt_limit=10)
                emit("proxy", "left", proxy_stage="provider", proxy_ip="203.0.113.10", proxy_country_code="US")
                emit("proxy", "right", proxy_stage="promotion", proxy_ip="198.51.100.20", proxy_country_code="JP")
                emit("stripe_amount", "amount")
                emit("unknown_internal_step", "ignored progress step")
                return {"ok": True, "long_url": "https://example.test/approve"}

        self.service._state["results"][0].update({
            "billing_country": "US",
            "promotion_country": "JP",
            "progress_percent": 2,
            "progress_label": "准备矩阵代理",
        })
        with patch.object(plus_link_store, "_load_plus_engine", return_value=FakeEngine):
            self.service._generate_local_long_link({}, task_id=self.task["task_id"], email=self.account["email"])

        result = self.service._state["results"][0]
        self.assertEqual(result["link_attempt_count"], 2)
        self.assertEqual(result["left_proxy_ip"], "203.0.113.10")
        self.assertEqual(result["left_proxy_country"], "US")
        self.assertEqual(result["right_proxy_ip"], "198.51.100.20")
        self.assertEqual(result["right_proxy_country"], "JP")
        self.assertEqual(result["progress_percent"], plus_link_store.PLUS_STEP_PROGRESS["stripe_amount"][0])
        self.assertEqual(result["progress_label"], plus_link_store.PLUS_STEP_PROGRESS["stripe_amount"][1])

    def test_restart_closes_tasks_without_execution_threads(self) -> None:
        store_path = Path(self.temp_dir.name) / "restart.json"
        plus_link_store.write_json(store_path, {
            "enabled": False,
            "config": {},
            "stats": {"running": 1, "done": 0, "cancelled": 0},
            "logs": [],
            "results": [{"task_id": "stale", "email": "stale@example.com", "status": "running"}],
        }, schema_version=1)

        recovered = plus_link_store.PlusLinkService(store_path)
        result = recovered._state["results"][0]
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(recovered._state["stats"]["running"], 0)
        self.assertEqual(recovered._state["stats"]["done"], 1)
        self.assertEqual(recovered._state["stats"]["cancelled"], 1)

    def test_bulk_clear_keeps_active_tasks(self) -> None:
        self.service._state["results"] = [
            {"task_id": "running", "status": "running"},
            {"task_id": "success", "status": "success"},
            {"task_id": "failed", "status": "failed"},
            {"task_id": "cancelled", "status": "cancelled"},
        ]
        failed_cleared = self.service.clear_failed_results()
        self.assertEqual({item["task_id"] for item in failed_cleared["results"]}, {"running", "success"})
        self.assertEqual(failed_cleared["stats"]["running"], 1)
        self.assertEqual(failed_cleared["stats"]["success"], 1)

        finished_cleared = self.service.clear_finished_results()
        self.assertEqual([item["task_id"] for item in finished_cleared["results"]], ["running"])
        self.assertEqual(finished_cleared["stats"]["total"], 1)
        self.assertEqual(finished_cleared["stats"]["done"], 0)

    def test_retry_after_restart_recovers_account_by_email(self) -> None:
        self.service._state["results"][0].update({"status": "failed", "matrix_index": 7})
        self.service._task_controls = {}
        release = threading.Event()
        runner = threading.Thread(target=lambda: release.wait(2))
        runner.start()
        self.service._runner = runner
        try:
            with patch.object(self.service, "_select_accounts", return_value=[self.account]) as select_accounts:
                state = self.service.retry_task(self.task["task_id"])
        finally:
            release.set()
            runner.join(2)

        select_accounts.assert_called_once_with([self.account["email"]], partial_accounts=[self.account["email"]])
        self.assertEqual(len(state["results"]), 1)
        self.assertEqual(state["results"][0]["status"], "queued")
        self.assertEqual(state["results"][0]["matrix_index"], 1)
        self.assertEqual(self.service._retry_queue[-1]["index"], 1)


if __name__ == "__main__":
    unittest.main()
