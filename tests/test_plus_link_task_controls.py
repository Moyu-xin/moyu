from __future__ import annotations

import base64
import json
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

    def test_compact_state_skips_inventory_and_proxy_pool(self) -> None:
        self.service._state["logs"] = [
            {"time": str(index), "text": f"log-{index}", "level": "info"}
            for index in range(50)
        ]
        with patch.object(
            plus_link_store.proxy_pool_store,
            "get_proxy_pool_config",
            side_effect=AssertionError("compact state must not load proxy pool"),
        ):
            state = self.service.get(include_inventory=False, compact=True)

        self.assertNotIn("account_pool", state)
        self.assertNotIn("partial_account_pool", state)
        self.assertNotIn("proxy_pool", state)
        self.assertEqual(len(state["logs"]), 30)
        self.assertEqual(state["logs"][0]["text"], "log-20")

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

    def test_running_task_cancel_is_immediate_and_rejects_late_success(self) -> None:
        self.task["submitted"] = True
        self.task["started"] = True
        self.service._state["results"][0]["status"] = "running"
        self.service._state["stats"].update({"total": 1, "running": 1})

        state = self.service.cancel_task(self.task["task_id"])

        result = state["results"][0]
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["cleanup_pending"])
        self.assertEqual(state["stats"]["cancelled"], 1)
        self.assertEqual(state["stats"]["running"], 0)
        self.assertFalse(self.service._set_task_result(self.task["task_id"], {"status": "success"}))
        self.assertEqual(self.service._state["results"][0]["status"], "cancelled")

        self.service._mark_task_settled(self.task["task_id"])
        self.assertFalse(self.service._state["results"][0]["cleanup_pending"])

    def test_task_metadata_uses_access_token_profile(self) -> None:
        payload = {
            "email": "jwt-user@example.com",
            "exp": 2_000_000_000,
            "https://api.openai.com/auth": {"chatgpt_plan_type": "free"},
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
        token = f"header.{encoded}.signature"

        metadata = plus_link_store._task_account_metadata({
            "email": "manual-1@token.local",
            "access_token": token,
        })

        self.assertEqual(metadata["account_email"], "jwt-user@example.com")
        self.assertEqual(metadata["account_type"], "free")
        self.assertEqual(metadata["token_expires_at"], 2_000_000_000)

    def test_global_stop_cancels_a_paused_queued_task(self) -> None:
        self.service._state["stats"]["total"] = 1
        self.service.pause_task(self.task["task_id"])
        cancelled = self.service._cancel_queued_tasks([self.task], reason="用户停止任务")
        self.assertEqual(cancelled, 1)
        self.assertEqual(self.service._state["results"][0]["status"], "cancelled")

    def test_retry_requeues_terminal_task_with_same_account_and_config(self) -> None:
        self.service._state["results"][0]["status"] = "failed"
        self.service._state["results"][0]["attempt_diagnostics_history"] = [{
            "run_number": 1,
            "attempt": 1,
            "status": "failed",
            "failure_category": "generic_decline",
        }]
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
        self.assertEqual(retry["attempt_diagnostics_history"][0]["failure_category"], "generic_decline")
        self.assertEqual(retry["matrix_index"], 1)
        self.assertEqual(state["stats"]["total"], 1)
        self.assertEqual(state["stats"]["done"], 0)
        self.assertEqual(len(self.service._retry_queue), 1)
        queued_task = self.service._retry_queue[0]
        self.assertEqual(queued_task["task_id"], self.task["task_id"])
        self.assertEqual(queued_task["account"]["email"], self.account["email"])
        self.assertTrue(queued_task["cfg"]["country_rotation_enabled"])

    def test_retry_failed_tasks_requeues_only_ready_retryable_tasks(self) -> None:
        self.service._state["results"][0].update({"status": "failed", "retryable": True})
        second_account = {"email": "cancelled@example.com", "access_token": "second-token"}
        second_task = self.service._new_task(second_account, self.cfg, 2)
        self.service._state["results"].extend([
            {
                "task_id": second_task["task_id"],
                "email": second_account["email"],
                "status": "cancelled",
                "retryable": True,
            },
            {
                "task_id": "plus-success",
                "email": "success@example.com",
                "status": "success",
                "retryable": True,
            },
            {
                "task_id": "plus-cleanup",
                "email": "cleanup@example.com",
                "status": "cancelled",
                "retryable": True,
                "cleanup_pending": True,
            },
            {
                "task_id": "plus-disabled",
                "email": "disabled@example.com",
                "status": "failed",
                "retryable": False,
            },
        ])
        self.service._recalculate_stats_locked()
        release = threading.Event()
        runner = threading.Thread(target=lambda: release.wait(2))
        runner.start()
        self.service._runner = runner
        try:
            state = self.service.retry_failed_tasks()
        finally:
            release.set()
            runner.join(2)

        self.assertEqual(state["bulk_retry"], {"requested": 2, "restarted": 2, "skipped": 2})
        result_by_id = {item["task_id"]: item for item in state["results"]}
        self.assertEqual(result_by_id[self.task["task_id"]]["status"], "queued")
        self.assertEqual(result_by_id[second_task["task_id"]]["status"], "queued")
        self.assertEqual(result_by_id["plus-success"]["status"], "success")
        self.assertTrue(result_by_id["plus-cleanup"]["cleanup_pending"])
        self.assertFalse(result_by_id["plus-disabled"]["retryable"])
        self.assertEqual(len(self.service._retry_queue), 2)

    def test_retry_failed_tasks_rejects_when_no_task_is_ready(self) -> None:
        self.service._state["results"][0].update({
            "status": "cancelled",
            "retryable": True,
            "cleanup_pending": True,
        })

        with self.assertRaisesRegex(RuntimeError, "暂无已完成后台收尾"):
            self.service.retry_failed_tasks()

    def test_manual_token_task_can_restart_without_reimporting_token(self) -> None:
        account = plus_link_store._manual_account_from_item("a" * 40, 1)
        self.assertIsNotNone(account)
        assert account is not None
        with patch.object(self.service, "_run_task_batch"):
            self.service._runner_main([account], self.cfg)

        result = self.service._state["results"][-1]
        self.assertTrue(result["retryable"])
        result["status"] = "cancelled"
        release = threading.Event()
        runner = threading.Thread(target=lambda: release.wait(2))
        runner.start()
        self.service._runner = runner
        try:
            state = self.service.retry_task(result["task_id"])
        finally:
            release.set()
            runner.join(2)

        self.assertEqual(state["results"][-1]["status"], "queued")
        self.assertEqual(self.service._retry_queue[-1]["account"]["access_token"], "a" * 40)

    def test_start_appends_new_tasks_while_runner_is_active(self) -> None:
        self.service._state["stats"]["total"] = 1
        release = threading.Event()
        runner = threading.Thread(target=lambda: release.wait(2))
        runner.start()
        self.service._runner = runner
        added_account = {"email": "added@example.com", "access_token": "added-token"}
        try:
            with patch.object(self.service, "_select_accounts", return_value=[added_account]):
                state = self.service.start({"emails": ["added@example.com"], "clear_results": True})
        finally:
            release.set()
            runner.join(2)

        self.assertEqual(len(state["results"]), 2)
        self.assertEqual(state["results"][0]["task_id"], self.task["task_id"])
        self.assertEqual(state["results"][1]["email"], "added@example.com")
        self.assertEqual(state["stats"]["total"], 2)
        self.assertEqual(len(self.service._retry_queue), 1)
        self.assertEqual(self.service._retry_queue[0]["account"]["access_token"], "added-token")

    def test_temporary_country_config_is_snapshotted_without_overwriting_default(self) -> None:
        self.service._state["config"] = plus_link_store._normalize_config({
            "billing_country_rotation": ["US"],
            "proxy_country_rotation": ["BR"],
        })
        release = threading.Event()
        runner = threading.Thread(target=lambda: release.wait(2))
        runner.start()
        self.service._runner = runner
        added_account = {"email": "temporary@example.com", "access_token": "temporary-token"}
        try:
            with patch.object(self.service, "_select_accounts", return_value=[added_account]):
                state = self.service.start({
                    "emails": ["temporary@example.com"],
                    "clear_results": False,
                    "persist_config": False,
                    "config": {
                        "billing_country_rotation": ["DE", "GB"],
                        "proxy_country_rotation": ["JP", "TR"],
                    },
                })
        finally:
            release.set()
            runner.join(2)

        self.assertEqual(state["config"]["billing_country_rotation"], ["US"])
        self.assertEqual(state["config"]["proxy_country_rotation"], ["BR"])
        queued_task = self.service._retry_queue[-1]
        self.assertEqual(queued_task["cfg"]["billing_country_rotation"], ["DE", "GB"])
        self.assertEqual(queued_task["cfg"]["proxy_country_rotation"], ["JP", "TR"])
        result = state["results"][-1]
        self.assertEqual(result["task_left_countries"], ["DE", "GB"])
        self.assertEqual(result["task_right_countries"], ["JP", "TR"])

    def test_active_runner_executes_task_added_during_first_task(self) -> None:
        service = plus_link_store.PlusLinkService(Path(self.temp_dir.name) / "dynamic.json")
        service._state = plus_link_store._default_state()
        first_account = {"email": "first@example.com", "access_token": "first-token"}
        second_account = {"email": "second@example.com", "access_token": "second-token"}
        first_started = threading.Event()
        release_first = threading.Event()
        seen: list[str] = []

        def run_one(task_id: str, account: dict[str, str], _cfg: dict, _index: int) -> None:
            seen.append(account["email"])
            if account["email"] == "first@example.com":
                first_started.set()
                release_first.wait(3)
            service._set_task_result(task_id, {"status": "success", "stage": "完成"})

        with (
            patch.object(plus_link_store, "_load_plus_engine", return_value=object()),
            patch.object(service, "_run_one", side_effect=run_one),
        ):
            with patch.object(service, "_select_accounts", return_value=[first_account]):
                service.start({"emails": ["first@example.com"], "clear_results": False})
            self.assertTrue(first_started.wait(2))
            with patch.object(service, "_select_accounts", return_value=[second_account]):
                service.start({"emails": ["second@example.com"], "clear_results": False})
            release_first.set()
            deadline = time.time() + 5
            while time.time() < deadline:
                runner = service._runner
                if runner is None or not runner.is_alive():
                    break
                time.sleep(0.05)

        self.assertEqual(seen, ["first@example.com", "second@example.com"])
        self.assertEqual(len(service._state["results"]), 2)
        self.assertTrue(all(item["status"] == "success" for item in service._state["results"]))

    def test_active_runner_expands_when_concurrency_is_increased(self) -> None:
        service = plus_link_store.PlusLinkService(Path(self.temp_dir.name) / "dynamic-concurrency.json")
        service._state = plus_link_store._default_state()
        service._state["config"] = plus_link_store._normalize_config({"concurrency": 1})
        release = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        accounts = [
            {"email": "first@example.com", "access_token": "first-token"},
            {"email": "second@example.com", "access_token": "second-token"},
        ]
        tasks = [service._new_task(account, service._state["config"], index) for index, account in enumerate(accounts, 1)]
        service._state["results"] = [
            {"task_id": task["task_id"], "email": task["email"], "status": "queued"}
            for task in tasks
        ]

        def run_one(task_id: str, account: dict[str, str], _cfg: dict, _index: int) -> None:
            (first_started if account["email"] == "first@example.com" else second_started).set()
            release.wait(3)
            service._set_task_result(task_id, {"status": "success", "stage": "完成"})

        with (
            patch.object(plus_link_store, "_load_plus_engine", return_value=object()),
            patch.object(service, "_run_one", side_effect=run_one),
        ):
            runner = threading.Thread(target=service._run_task_batch, args=(tasks,))
            runner.start()
            try:
                self.assertTrue(first_started.wait(2))
                self.assertFalse(second_started.is_set())
                service.update_config({"concurrency": 2})
                self.assertTrue(second_started.wait(2))
                self.assertEqual(tasks[1]["cfg"]["concurrency"], 2)
            finally:
                release.set()
                runner.join(5)

        self.assertFalse(runner.is_alive())

    def test_delete_queued_task_removes_it_from_results_and_queue(self) -> None:
        self.service._retry_queue = [self.task]
        self.service._state["stats"]["total"] = 1

        state = self.service.delete_task(self.task["task_id"])

        self.assertEqual(state["results"], [])
        self.assertEqual(state["stats"]["total"], 0)
        self.assertEqual(self.service._retry_queue, [])
        self.assertNotIn(self.task["task_id"], self.service._task_controls)

    def test_delete_running_task_cancels_and_hides_it_immediately(self) -> None:
        self.task["submitted"] = True
        self.task["started"] = True
        self.service._state["results"][0]["status"] = "running"
        self.service._state["stats"].update({"total": 1, "running": 1})

        state = self.service.delete_task(self.task["task_id"])

        self.assertEqual(state["results"], [])
        self.assertTrue(self.task["cancel_event"].is_set())
        self.assertTrue(self.task["deleted"])
        self.assertEqual(state["stats"]["running"], 0)
        self.service._mark_task_settled(self.task["task_id"])
        self.assertNotIn(self.task["task_id"], self.service._task_controls)

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
            "proxy_quality_gate_enabled": False,
        })
        self.service._state["stats"].update({"total": 1, "running": 1})
        seen: list[tuple[object, ...]] = []

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
            current = self.service._result_for_task(task_id) or {}
            seen.append((
                payload["billing_country"],
                payload["payment_method_country"],
                payload["promotion_country"],
                payload["provider_retry_limit"],
                current.get("matrix_checkout_country"),
                current.get("matrix_provider_country"),
                current.get("matrix_promotion_country"),
            ))
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

        self.assertEqual(seen, [
            ("US", "US", "TR", 3, "US", "US", "TR"),
            ("US", "US", "JP", 3, "US", "US", "JP"),
            ("US", "DE", "JP", 3, "US", "DE", "JP"),
            ("US", "DE", "TR", 3, "US", "DE", "TR"),
        ])
        result = self.service._state["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["matrix_index"], 4)
        self.assertEqual(result["matrix_total"], 4)
        self.assertEqual(result["matrix_failures"], 3)

    def test_quality_gate_keeps_rotating_without_consuming_link_attempts(self) -> None:
        cfg = plus_link_store._normalize_config({
            "country_rotation_enabled": False,
            "checkout_country": "DE",
            "provider_country": "TH",
            "promotion_country": "TR",
            "fixed_retry_limit": 10,
            "refresh_tokens": False,
            "proxy_quality_gate_enabled": True,
        })
        self.service._state["stats"].update({"total": 1, "running": 1})
        payload_indices: list[int] = []

        def payload_for_account(_account, _cfg, _matrix_index, *, proxy_index=None):
            payload_indices.append(int(proxy_index or 0))
            suffix = len(payload_indices)
            proxy = f"http://candidate-{suffix}.example:8080"
            return ({
                "accessToken": "token",
                "billing_country": "DE",
                "payment_method_country": "TH",
                "promotion_country": "TR",
                "promotion_proxy_country": "TR",
                "batch_checkout_proxy_country": "DE",
                "batch_target_country": "TH",
                "batch_approve_proxy_country": "DE",
                "checkout_proxy_override": proxy,
                "provider_proxy_override": proxy,
                "approve_proxy_override": proxy,
                "promotion_proxy_override": proxy,
            }, {
                "label": f"candidate-{suffix}",
                "source": "test",
                "provider_country": "TH",
                "approve_country": "DE",
            })

        rejected = {
            "ok": False,
            "candidate_id": "rejected",
            "failed_stage": "checkout",
            "error": "proxy CONNECT failed",
            "stages": [],
        }
        accepted = {
            "ok": True,
            "candidate_id": "accepted",
            "failed_stage": "",
            "error": "",
            "stages": [],
        }
        generated = {
            "ok": True,
            "long_url": "https://www.paypal.com/agreements/approve?ba_token=BA-quality-test",
            "link_attempt_count": 1,
            "link_attempt_limit": 10,
        }

        with (
            patch.object(self.service, "_precheck_account_eligibility", return_value={"eligible": True, "account_type": "free", "has_email": True}),
            patch.object(self.service, "_payload_for_account", side_effect=payload_for_account),
            patch.object(self.service, "_check_payload_proxy_quality", side_effect=[rejected, rejected, accepted]) as quality,
            patch.object(self.service, "_wait_for_quality_candidate_retry"),
            patch.object(self.service, "_generate_local_long_link", return_value=generated) as generate,
        ):
            self.service._run_one(self.task["task_id"], self.account, cfg, 1)

        result = self.service._state["results"][0]
        self.assertEqual(quality.call_count, 3)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["quality_gate_attempt"], 3)
        self.assertEqual(result["quality_gate_attempt_limit"], 0)
        self.assertEqual(result["link_attempt_count"], 1)
        self.assertEqual(self.service._state["stats"]["fail"], 0)

    def test_matrix_quality_gate_skips_country_mismatch_and_tries_next_combo(self) -> None:
        cfg = plus_link_store._normalize_config({
            "country_rotation_enabled": True,
            "billing_country_rotation": ["AF", "BR"],
            "proxy_country_rotation": ["TR"],
            "checkout_country": "DE",
            "refresh_tokens": False,
            "proxy_quality_gate_enabled": True,
            "proxy_quality_candidate_limit": 3,
        })
        self.service._state["stats"].update({"total": 1, "running": 1})

        def payload_for_account(_account, _cfg, matrix_index, *, proxy_index=None):
            provider_country = "AF" if matrix_index == 1 else "BR"
            proxy = f"http://candidate-{proxy_index}.example:8080"
            return ({
                "accessToken": "token",
                "billing_country": "DE",
                "payment_method_country": provider_country,
                "promotion_country": "TR",
                "promotion_proxy_country": "TR",
                "batch_checkout_proxy_country": "DE",
                "batch_target_country": provider_country,
                "batch_approve_proxy_country": "DE",
                "checkout_proxy_override": proxy,
                "provider_proxy_override": proxy,
                "approve_proxy_override": proxy,
                "promotion_proxy_override": proxy,
            }, {
                "label": provider_country,
                "source": "test",
                "provider_country": provider_country,
                "approve_country": "DE",
            })

        rejected = {
            "ok": False,
            "candidate_id": "rejected",
            "failed_stage": "provider",
            "error": "Provider/PM/Poll 出口不是 AF: DE 203.0.113.10",
            "stages": [{
                "stage": "provider",
                "label": "Provider/PM/Poll",
                "expected_country": "AF",
                "country": "DE",
                "ip": "203.0.113.10",
                "ok": False,
            }],
        }
        accepted = {"ok": True, "candidate_id": "accepted", "failed_stage": "", "error": "", "stages": []}
        generated = {
            "ok": True,
            "long_url": "https://www.paypal.com/agreements/approve?ba_token=BA-quality-next-combo",
            "link_attempt_count": 1,
            "link_attempt_limit": 1,
        }

        with (
            patch.object(self.service, "_precheck_account_eligibility", return_value={"eligible": True, "account_type": "free", "has_email": True}),
            patch.object(self.service, "_payload_for_account", side_effect=payload_for_account),
            patch.object(self.service, "_check_payload_proxy_quality", side_effect=[rejected, rejected, rejected, accepted]) as quality,
            patch.object(self.service, "_wait_for_quality_candidate_retry"),
            patch.object(self.service, "_generate_local_long_link", return_value=generated) as generate,
        ):
            self.service._run_one(self.task["task_id"], self.account, cfg, 1)

        result = self.service._state["results"][0]
        self.assertEqual(quality.call_count, 4)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["matrix_index"], 2)
        self.assertEqual(result["matrix_skipped"], 1)
        self.assertEqual(result["matrix_failures"], 0)
        self.assertEqual(result["link_attempt_count"], 1)

    def test_retryable_full_link_failure_starts_a_new_cycle_automatically(self) -> None:
        cfg = plus_link_store._normalize_config({
            "country_rotation_enabled": False,
            "checkout_country": "DE",
            "provider_country": "TH",
            "promotion_country": "TR",
            "fixed_retry_limit": 2,
            "refresh_tokens": False,
            "proxy_quality_gate_enabled": False,
        })
        self.service._state["stats"].update({"total": 1, "running": 1})
        proxy = "http://proxy.example:8080"
        payload = {
            "accessToken": "token",
            "billing_country": "DE",
            "payment_method_country": "TH",
            "promotion_country": "TR",
            "promotion_proxy_country": "TR",
            "batch_checkout_proxy_country": "DE",
            "batch_target_country": "TH",
            "batch_approve_proxy_country": "DE",
            "checkout_proxy_override": proxy,
            "provider_proxy_override": proxy,
            "approve_proxy_override": proxy,
            "promotion_proxy_override": proxy,
        }
        proxy_info = {
            "label": "test proxy",
            "source": "test",
            "provider_country": "TH",
            "approve_country": "DE",
        }
        generated = {
            "ok": True,
            "long_url": "https://www.paypal.com/agreements/approve?ba_token=BA-auto-retry",
            "link_attempt_count": 1,
            "link_attempt_limit": 2,
        }

        with (
            patch.object(self.service, "_precheck_account_eligibility", return_value={"eligible": True, "account_type": "free", "has_email": True}),
            patch.object(self.service, "_payload_for_account", return_value=(payload, proxy_info)),
            patch.object(
                self.service,
                "_generate_local_long_link",
                side_effect=[RuntimeError("checkout 1 次重试失败：403 拦截"), generated],
            ) as generate,
            patch.object(self.service, "_wait_for_task_auto_retry") as retry_wait,
        ):
            self.service._run_one(self.task["task_id"], self.account, cfg, 1)

        result = self.service._state["results"][0]
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(retry_wait.call_count, 1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["auto_retry_count"], 1)
        self.assertEqual(self.service._state["stats"]["fail"], 0)

    def test_new_matrix_combo_resets_live_diagnostics_and_keeps_history(self) -> None:
        cfg = plus_link_store._normalize_config({
            "country_rotation_enabled": True,
            "billing_country_rotation": ["US"],
            "proxy_country_rotation": ["JP", "TR"],
            "matrix_combo_retry_limit": 1,
            "refresh_tokens": False,
            "proxy_quality_gate_enabled": False,
        })
        self.service._state["stats"].update({"total": 1, "running": 1})
        snapshots: list[dict[str, object]] = []

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

        def generate(_payload, *, task_id, email):
            current = dict(self.service._result_for_task(task_id) or {})
            snapshots.append(current)
            if len(snapshots) == 1:
                self.service._set_task_result(task_id, {
                    "last_engine_detail": "promotion update failed: status=403",
                    "promotion_error": "promotion update failed: status=403",
                    "provider_error": "provider from first combo",
                })
                raise RuntimeError("first combo failed")
            return {
                "ok": True,
                "long_url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
            }

        with (
            patch.object(self.service, "_assign_residential_proxy", side_effect=assign_proxy),
            patch.object(self.service, "_assign_project_parent_proxy", return_value=None),
            patch.object(
                self.service,
                "_precheck_account_eligibility",
                return_value={"eligible": True, "account_type": "free", "has_email": True},
            ),
            patch.object(self.service, "_generate_local_long_link", side_effect=generate),
        ):
            self.service._run_one(self.task["task_id"], self.account, cfg, 1)

        self.assertEqual(len(snapshots), 2)
        second_combo = snapshots[1]
        self.assertEqual(second_combo.get("last_engine_detail"), "")
        self.assertEqual(second_combo.get("promotion_error"), "")
        self.assertEqual(second_combo.get("provider_error"), "")
        history = second_combo.get("combo_diagnostics_history")
        self.assertIsInstance(history, list)
        assert isinstance(history, list)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["promotion_error"], "promotion update failed: status=403")
        self.assertEqual(history[0]["provider_error"], "provider from first combo")

    def test_matrix_skips_all_remaining_combos_when_fixed_checkout_has_no_paypal(self) -> None:
        cfg = plus_link_store._normalize_config({
            "country_rotation_enabled": True,
            "billing_country_rotation": ["US", "DE"],
            "proxy_country_rotation": ["JP", "TR"],
            "matrix_combo_retry_limit": 1,
            "refresh_tokens": False,
            "proxy_quality_gate_enabled": False,
        })
        self.service._state["stats"].update({"total": 1, "running": 1})
        seen: list[tuple[str, str]] = []

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
            pair = (payload["billing_country"], payload["promotion_country"])
            seen.append(pair)
            if payload["billing_country"] == "US":
                raise RuntimeError(
                    "paypal_unavailable_for_left_country: country=US; "
                    "paypal_availability=unsupported; methods=card,link"
                )
            return {
                "ok": True,
                "long_url": "https://www.paypal.com/agreements/approve?ba_token=test",
                "link_attempt_count": 1,
                "link_attempt_limit": 1,
            }

        with patch.object(self.service, "_assign_residential_proxy", side_effect=assign_proxy), \
             patch.object(self.service, "_assign_project_parent_proxy", return_value=None), \
             patch.object(self.service, "_precheck_account_eligibility", return_value={"eligible": True, "account_type": "free", "has_email": True}), \
             patch.object(self.service, "_generate_local_long_link", side_effect=generate):
            self.service._run_one(self.task["task_id"], self.account, cfg, 1)

        self.assertEqual(seen, [("US", "TR")])
        result = self.service._state["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["matrix_index"], 4)
        self.assertEqual(result["matrix_failures"], 1)
        self.assertEqual(result["matrix_skipped"], 3)
        self.assertEqual(result["paypal_availability_by_country"]["US"], "unsupported")

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
                emit("proxy", "right mismatch", proxy_stage="promotion", proxy_ip="203.0.113.99", proxy_country_code="IN", failure_category="proxy_country_mismatch")
                emit("proxy", "approve", proxy_stage="approve", proxy_ip="2001:db8::10", proxy_country_code="US")
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
        self.assertEqual(result["left_proxy_ip"], "2001:db8::10")
        self.assertEqual(result["left_proxy_country"], "US")
        self.assertEqual(result["provider_proxy_ip"], "203.0.113.10")
        self.assertEqual(result["provider_proxy_country"], "US")
        self.assertEqual(result["approve_proxy_ip"], "2001:db8::10")
        self.assertEqual(result["approve_proxy_country"], "US")
        self.assertEqual(result["right_proxy_ip"], "203.0.113.99")
        self.assertEqual(result["right_proxy_country"], "IN")
        self.assertEqual(result["promotion_proxy_ip"], "203.0.113.99")
        self.assertEqual(result["promotion_proxy_country"], "IN")
        self.assertEqual(result["failure_category"], "proxy_country_mismatch")
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

    def test_restart_can_automatically_requeue_interrupted_tasks(self) -> None:
        store_path = Path(self.temp_dir.name) / "auto-resume.json"
        plus_link_store.write_json(store_path, {
            "enabled": True,
            "config": {},
            "stats": {"running": 2, "done": 0, "cancelled": 0},
            "logs": [],
            "results": [
                {"task_id": "running-task", "email": "running@example.com", "status": "running"},
                {"task_id": "queued-task", "email": "queued@example.com", "status": "queued"},
                {"task_id": "stopped-task", "email": "stopped@example.com", "status": "cancelled"},
            ],
        }, schema_version=1)

        recovered = plus_link_store.PlusLinkService(store_path)
        with patch.object(recovered, "retry_task", return_value={}) as retry_task:
            summary = recovered.resume_interrupted_tasks()

        self.assertEqual(summary, {"requested": 2, "restarted": 2, "skipped": 0})
        self.assertEqual(
            [call.args[0] for call in retry_task.call_args_list],
            ["running-task", "queued-task"],
        )
        self.assertEqual(recovered.resume_interrupted_tasks()["requested"], 0)

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
