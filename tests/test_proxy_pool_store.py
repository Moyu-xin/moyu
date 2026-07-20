from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services import managed_mihomo_service, proxy_pool_store
from services.managed_mihomo_service import ManagedMihomoManager, _is_metadata_node, _merged_proxy_config, _subscription_proxies
from services.register_service import RegisterService, _is_cloudflare_blocked_error


class ProxyPoolAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_file = proxy_pool_store.PROXY_POOL_FILE
        self._temp_dir = tempfile.TemporaryDirectory()
        proxy_pool_store.PROXY_POOL_FILE = Path(self._temp_dir.name) / "proxy_pool.json"
        proxy_pool_store.import_node_text(
            "\n".join([
                "node-a=http://127.0.0.1:18001",
                "node-b=http://127.0.0.1:18002",
                "node-c=http://127.0.0.1:18003",
            ])
        )

    def tearDown(self) -> None:
        proxy_pool_store.PROXY_POOL_FILE = self._original_file
        self._temp_dir.cleanup()

    def test_per_task_rotates_across_reused_thread_slots(self) -> None:
        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "mode": "per_task",
            "strategy": "round_robin",
            "node_mode": "direct_proxy",
        })
        assignments = [
            proxy_pool_store.assign_proxy(1, total_threads=2, slot_index=0),
            proxy_pool_store.assign_proxy(2, total_threads=2, slot_index=1),
            proxy_pool_store.assign_proxy(3, total_threads=2, slot_index=0),
        ]
        self.assertEqual([item["label"] for item in assignments], ["node-a", "node-b", "node-c"])
        self.assertEqual([item["slot"] for item in assignments], [1, 2, 1])

    def test_per_thread_keeps_slot_assignment(self) -> None:
        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "mode": "per_thread",
            "strategy": "round_robin",
            "node_mode": "direct_proxy",
        })
        first = proxy_pool_store.assign_proxy(1, total_threads=2, slot_index=0)
        reused = proxy_pool_store.assign_proxy(3, total_threads=2, slot_index=0)
        self.assertEqual(first["label"], "node-a")
        self.assertEqual(reused["label"], "node-a")

    def test_residential_direct_mode_isolated_from_subscription_nodes(self) -> None:
        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "mode": "per_task",
            "strategy": "round_robin",
            "node_mode": "residential_direct",
            "residential_proxy_text": "\n".join([
                "res-a.example:1000:user-a:pass-a",
                "user-b:pass-b@res-b.example:1001",
            ]),
        })

        first = proxy_pool_store.assign_proxy(1, total_threads=2, slot_index=0)
        second = proxy_pool_store.assign_proxy(2, total_threads=2, slot_index=1)

        self.assertEqual(first["source"], "residential_direct")
        self.assertEqual(first["proxy"], "http://user-a:pass-a@res-a.example:1000")
        self.assertEqual(second["proxy"], "http://user-b:pass-b@res-b.example:1001")
        public = proxy_pool_store.get_proxy_pool_config()
        self.assertEqual(public["residential_proxy_count"], 2)
        self.assertNotIn("residential_proxies", public)

    def test_node_mode_does_not_fall_through_to_direct_nodes(self) -> None:
        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "node_mode": "mihomo_ports",
            "fallback_proxy": "http://127.0.0.1:18999",
            "mihomo": {"enabled": False},
        })
        assignment = proxy_pool_store.assign_proxy(1, total_threads=1, slot_index=0)
        self.assertEqual(assignment["source"], "fallback")
        self.assertEqual(assignment["proxy"], "http://127.0.0.1:18999")

    def test_managed_mode_ignores_registration_level_fallback(self) -> None:
        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "node_mode": "managed_mihomo",
            "fallback_proxy": "",
        })
        with patch("services.managed_mihomo_service.managed_mihomo_manager.assign", side_effect=RuntimeError("unavailable")):
            assignment = proxy_pool_store.assign_proxy(
                1,
                fallback="http://127.0.0.1:7897",
                total_threads=1,
                slot_index=0,
            )
        self.assertEqual(assignment["proxy"], "")
        self.assertTrue(assignment["managed_failure"])
        self.assertEqual(assignment["requested_source"], "managed_mihomo")

    def test_managed_mode_passes_node_name_filter(self) -> None:
        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "node_mode": "managed_mihomo",
            "fallback_proxy": "",
        })
        expected = {"proxy": "http://127.0.0.1:42000", "source": "managed_mihomo", "label": "sg-anytls-aw"}
        with patch("services.managed_mihomo_service.managed_mihomo_manager.assign", return_value=expected) as assign:
            assignment = proxy_pool_store.assign_proxy(
                1,
                total_threads=1,
                slot_index=0,
                node_name_filter="anytls-aw",
                validate_node=False,
            )
        self.assertEqual(assignment, expected)
        self.assertEqual(assign.call_args.kwargs["name_filter"], "anytls-aw")
        self.assertFalse(assign.call_args.kwargs["validate_node"])

    def test_managed_loopback_api_bypasses_cached_system_proxy(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response

        with (
            patch("services.managed_mihomo_service.urllib.request.build_opener", return_value=opener) as build_opener,
            patch("services.managed_mihomo_service.urllib.request.urlopen") as urlopen,
        ):
            result = managed_mihomo_service._request_json("http://127.0.0.1:43000/proxies")

        self.assertEqual(result, {"ok": True})
        build_opener.assert_called_once()
        self.assertEqual(build_opener.call_args.args[0].proxies, {})
        opener.open.assert_called_once()
        urlopen.assert_not_called()

    def test_plus_subscription_is_persisted_without_filtering_registration(self) -> None:
        proxy_pool_store.update_proxy_pool_config({"plus_subscription_id": "sub_plus"})
        store = proxy_pool_store.get_proxy_pool_config(include_secret=True)
        self.assertEqual(store["plus_subscription_id"], "sub_plus")

        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "node_mode": "managed_mihomo",
        })
        expected = {"proxy": "http://127.0.0.1:42000", "source": "managed_mihomo", "label": "registration-node"}
        with patch("services.managed_mihomo_service.managed_mihomo_manager.assign", return_value=expected) as assign:
            proxy_pool_store.assign_proxy(1, purpose="registration")
        self.assertEqual(assign.call_args.kwargs["name_filter"], "")

    def test_private_subscription_permission_survives_save_and_update(self) -> None:
        created = proxy_pool_store.add_subscriptions([
            {
                "name": "local-subscription",
                "url": "http://127.0.0.1:18080/subscription",
                "allow_private": True,
            }
        ], fetch=False)
        subscription = created["subscriptions"][0]
        self.assertTrue(subscription["allow_private"])

        updated = proxy_pool_store.update_proxy_pool_config({
            "subscriptions": [{"id": subscription["id"], "allow_private": False}],
        })
        self.assertFalse(updated["subscriptions"][0]["allow_private"])

    def test_registration_proxy_record_preserves_managed_node_identity(self) -> None:
        record = proxy_pool_store.registration_proxy_record({
            "proxy": "http://127.0.0.1:42003",
            "source": "managed_mihomo",
            "node_id": "node-id",
            "label": "[pool] node-a",
            "slot": 4,
            "managed_slot": 7,
            "region": "JP",
        })

        self.assertEqual(record["source"], "managed_mihomo")
        self.assertEqual(record["node_id"], "node-id")
        self.assertEqual(record["node_name"], "[pool] node-a")
        self.assertEqual(record["proxy"], "http://127.0.0.1:42003")
        self.assertEqual(record["managed_slot"], 7)

    def test_account_registration_node_is_restored_exactly(self) -> None:
        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "node_mode": "managed_mihomo",
            "managed_mihomo": {"enabled": True},
        })
        expected = {
            "proxy": "http://127.0.0.1:42005",
            "source": "managed_mihomo",
            "node_id": "restored-id",
            "label": "[pool] node-a",
        }
        account = {
            "registration_proxy": {
                "source": "managed_mihomo",
                "node_id": "original-id",
                "node_name": "[pool] node-a",
                "proxy": "http://127.0.0.1:42001",
            }
        }
        with patch("services.managed_mihomo_service.managed_mihomo_manager.assign", return_value=expected) as assign:
            result = proxy_pool_store.assign_proxy(
                3,
                total_threads=8,
                slot_index=5,
                purpose="partial_account_health",
                account=account,
                validate_node=False,
            )

        self.assertEqual(result["proxy"], "http://127.0.0.1:42005")
        self.assertTrue(result["sticky"])
        self.assertEqual(result["registration_node_id"], "original-id")
        self.assertEqual(assign.call_args.kwargs["exact_node_name"], "[pool] node-a")
        self.assertFalse(assign.call_args.kwargs["validate_node"])

    def test_missing_registration_node_never_falls_back(self) -> None:
        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "node_mode": "managed_mihomo",
            "fallback_proxy": "http://127.0.0.1:7897",
            "managed_mihomo": {"enabled": True},
        })
        account = {
            "registration_proxy": {
                "source": "managed_mihomo",
                "node_name": "missing-node",
                "proxy": "http://127.0.0.1:42001",
            }
        }
        with patch("services.managed_mihomo_service.managed_mihomo_manager.assign", side_effect=RuntimeError("node unavailable")):
            result = proxy_pool_store.assign_proxy(1, account=account, purpose="inventory_health")

        self.assertTrue(result["sticky_failure"])
        self.assertEqual(result["proxy"], "")
        self.assertIn("node unavailable", result["error"])

    def test_clash_yaml_metadata_is_not_parsed_as_proxy(self) -> None:
        nodes = proxy_pool_store._parse_subscription_nodes(
            """mixed-port: 7890
mode: rule
proxies:
  - name: sg-anytls-aw
    type: anytls
    server: sg.example.com
    port: 443
""",
            subscription_id="cached-profile",
        )
        self.assertEqual([node["name"] for node in nodes], ["sg-anytls-aw"])

    def test_mihomo_base_port_is_not_incremented_in_single_port_mode(self) -> None:
        single = proxy_pool_store._mihomo_proxy_for_slot({
            "proxy_scheme": "http",
            "proxy_host": "127.0.0.1",
            "proxy_port_base": 7897,
            "thread_port_mode": False,
        }, 4)
        threaded = proxy_pool_store._mihomo_proxy_for_slot({
            "proxy_scheme": "http",
            "proxy_host": "127.0.0.1",
            "proxy_port_base": 41000,
            "thread_port_mode": True,
        }, 2)
        self.assertEqual(single, "http://127.0.0.1:7897")
        self.assertEqual(threaded, "http://127.0.0.1:41002")

    def test_subscription_metadata_entries_are_not_proxy_candidates(self) -> None:
        self.assertTrue(_is_metadata_node("剩余流量：28.77 GB"))
        self.assertTrue(_is_metadata_node("套餐到期：2026-07-21"))
        self.assertFalse(_is_metadata_node("🇯🇵日本专线-0.1倍率"))

    def test_managed_nodes_are_interleaved_across_subscriptions(self) -> None:
        subscriptions = [
            {"id": "a", "name": "机场A", "url": "https://a.example"},
            {"id": "b", "name": "机场B", "url": "https://b.example"},
        ]
        payloads = {
            "a": [{"name": "A1", "type": "ss"}, {"name": "A2", "type": "ss"}],
            "b": [{"name": "B1", "type": "ss"}, {"name": "B2", "type": "ss"}],
        }
        with patch("services.managed_mihomo_service._subscription_proxies", side_effect=lambda item: payloads[item["id"]]):
            _proxies, names, dns_config = _merged_proxy_config(subscriptions)
        self.assertEqual(names, ["[机场A] A1", "[机场B] B1", "[机场A] A2", "[机场B] B2"])
        self.assertEqual(dns_config, {})

    def test_managed_runtime_inherits_subscription_dns(self) -> None:
        subscriptions = [
            {"id": "first", "name": "first", "url": "file:///first.yaml"},
            {"id": "cached", "name": "cached", "url": "file:///cached.yaml"},
        ]

        def fake_subscription(item):
            nameserver = "https://dns.alidns.com/dns-query" if item["id"] == "cached" else "223.5.5.5"
            item["_managed_dns"] = {"enable": True, "nameserver": [nameserver]}
            return [{"name": "sg-anytls-aw", "type": "anytls"}]

        with patch("services.managed_mihomo_service._subscription_proxies", side_effect=fake_subscription):
            _proxies, _names, dns_config = _merged_proxy_config(subscriptions)
        self.assertTrue(dns_config["enable"])
        self.assertEqual(dns_config["nameserver"], ["https://dns.alidns.com/dns-query"])

    def test_subscription_dns_is_sanitized_for_managed_runtime(self) -> None:
        subscription = {"id": "cached", "name": "cached", "url": "file:///cached.yaml"}
        document = """dns:
  enable: true
  enhanced-mode: fake-ip
  default-nameserver: [223.5.5.5]
  nameserver: [https://dns.alidns.com/dns-query]
  fallback-filter: {geoip: true}
proxies:
  - {name: sg-anytls-aw, type: anytls, server: sg.example.com, port: 443, password: test}
"""
        with patch("services.managed_mihomo_service._fetch_subscription", return_value=document):
            proxies = _subscription_proxies(subscription)
        self.assertEqual([item["name"] for item in proxies], ["sg-anytls-aw"])
        dns_config = subscription["_managed_dns"]
        self.assertEqual(dns_config["enhanced-mode"], "redir-host")
        self.assertNotIn("fallback-filter", dns_config)

    def test_managed_runtime_failure_quarantines_node(self) -> None:
        manager = ManagedMihomoManager()
        result = manager.report_failure({
            "source": "managed_mihomo",
            "label": "node-a",
            "node": {"name": "node-a"},
        }, "TLS connect error", quarantine_seconds=120)
        self.assertTrue(result["ok"])
        quarantined = manager.status()["quarantined"]
        self.assertEqual([item["name"] for item in quarantined], ["node-a"])

    def test_managed_runtime_quarantine_is_scoped_by_assignment_purpose(self) -> None:
        manager = ManagedMihomoManager()
        manager.report_failure({
            "source": "managed_mihomo",
            "label": "node-a",
            "purpose": "inventory_health",
            "node": {"name": "node-a"},
        }, "TLS connect error", quarantine_seconds=120)

        self.assertIn("node-a", manager._active_quarantine_locked("inventory_health"))
        self.assertNotIn("node-a", manager._active_quarantine_locked("plus_parent_proxy"))
        quarantined = manager.status()["quarantined"]
        self.assertEqual(quarantined[0]["purpose"], "inventory_health")

    def test_managed_assignment_network_wait_does_not_hold_manager_lock(self) -> None:
        manager = ManagedMihomoManager()
        entered = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []
        instance = SimpleNamespace(
            running=lambda: True,
            assignment_lock=threading.RLock(),
            process=SimpleNamespace(pid=1234),
            fingerprint="test",
            selected="",
            proxy_url="http://127.0.0.1:42000",
            api_base="http://127.0.0.1:43000",
        )
        manager._instances[0] = instance

        def candidates(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            return ["node-a"]

        def run_assignment() -> None:
            try:
                manager.assign(
                    slot=0,
                    task_index=1,
                    total_threads=1,
                    store={"mode": "per_task", "strategy": "round_robin", "max_retry_count": 1},
                    purpose="test",
                    validate_node=False,
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        with (
            patch.object(manager, "_ensure_locked"),
            patch.object(manager, "_candidate_names", side_effect=candidates),
            patch.object(managed_mihomo_service, "_request_json", return_value={}),
        ):
            worker = threading.Thread(target=run_assignment)
            worker.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            status = manager.status()
            elapsed = time.monotonic() - started
            release.set()
            worker.join(2)

        self.assertLess(elapsed, 0.5)
        self.assertEqual(status["running"], 1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_managed_candidate_selection_fails_fast_when_all_matches_are_quarantined(self) -> None:
        manager = ManagedMihomoManager()
        manager._quarantine["sg-anytls-aw"] = (time.time() + 120, "TLS failure")
        instance = SimpleNamespace(api_base="http://127.0.0.1:43000")
        response = {
            "proxies": {
                "REGISTER": {"all": ["sg-anytls-aw", "us-anytls-aw"]},
                "sg-anytls-aw": {},
                "us-anytls-aw": {},
            }
        }

        started = time.monotonic()
        with patch.object(managed_mihomo_service, "_request_json", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "匹配节点均在隔离中"):
                manager._candidate_names(
                    instance,
                    {"managed_mihomo": {"startup_timeout": 30}, "node_blacklist": []},
                    name_filter="sg-anytls",
                )

        self.assertLess(time.monotonic() - started, 0.5)

    def test_managed_stop_recovers_verified_legacy_orphan(self) -> None:
        manager = ManagedMihomoManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "managed_mihomo"
            thread_root = runtime_root / "thread-1"
            thread_root.mkdir(parents=True)
            config_path = thread_root / "config.yaml"
            config_path.write_text("mixed-port: 42000\n", encoding="utf-8")

            class FakeProcess:
                info = {
                    "pid": 1234,
                    "name": "mihomo.exe",
                    "exe": str(runtime_root / "mihomo.exe"),
                    "cmdline": [str(runtime_root / "mihomo.exe"), "-d", str(thread_root), "-f", str(config_path)],
                }

                def __init__(self) -> None:
                    self.terminated = False

                def terminate(self) -> None:
                    self.terminated = True

                def wait(self, timeout: int) -> None:
                    self.wait_timeout = timeout

            fake = FakeProcess()
            with (
                patch.object(managed_mihomo_service, "RUNTIME_ROOT", runtime_root),
                patch.object(managed_mihomo_service.psutil, "process_iter", return_value=[fake]),
            ):
                manager._stop_persisted_instances_locked()

            self.assertTrue(fake.terminated)

    def test_managed_atexit_cleanup_does_not_stop_other_process_instances(self) -> None:
        manager = ManagedMihomoManager()
        with patch.object(manager, "_stop_persisted_instances_locked") as stop_persisted:
            manager.stop_owned()
        stop_persisted.assert_not_called()

    def test_managed_capacity_does_not_shrink_for_smaller_workflow(self) -> None:
        manager = ManagedMihomoManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "mihomo.exe"
            executable.write_bytes(b"test")
            store = {
                "managed_mihomo": {
                    "enabled": True,
                    "executable": str(executable),
                    "proxy_port_base": 42000,
                    "api_port_base": 43000,
                },
                "subscriptions": [{"id": "sub", "url": "https://example.invalid/sub", "updated_at": "", "enabled": True}],
            }
            fingerprint = hashlib.sha256(json.dumps({
                "subscriptions": [("sub", "https://example.invalid/sub", "")],
                "executable": str(executable),
                "proxy_port_base": 42000,
                "api_port_base": 43000,
                "config_version": 7,
            }, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")).hexdigest()

            manager._instances = {
                slot: SimpleNamespace(running=lambda: True, fingerprint=fingerprint, process=SimpleNamespace(pid=1000 + slot))
                for slot in range(3)
            }
            with (
                patch.object(managed_mihomo_service, "RUNTIME_ROOT", Path(temp_dir)),
                patch.object(manager, "_stop_persisted_instances_locked"),
                patch.object(manager, "_stop_slot_locked") as stop_slot,
            ):
                manager._ensure_locked(1, store)

            stop_slot.assert_not_called()
            self.assertEqual(set(manager._instances), {0, 1, 2})

    def test_managed_executable_must_stay_in_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "managed"
            runtime_root.mkdir()
            allowed = runtime_root / "mihomo.exe"
            outside = Path(temp_dir) / "outside.exe"
            with patch.object(managed_mihomo_service, "RUNTIME_ROOT", runtime_root):
                self.assertEqual(managed_mihomo_service._managed_executable_path(str(allowed)), allowed.resolve())
                with self.assertRaisesRegex(RuntimeError, "managed runtime"):
                    managed_mihomo_service._managed_executable_path(str(outside))

    def test_managed_workflows_receive_distinct_physical_slots(self) -> None:
        manager = ManagedMihomoManager()
        self.assertEqual(manager._physical_slot_locked("registration", 0), 0)
        self.assertEqual(manager._physical_slot_locked("registration", 1), 1)
        self.assertEqual(manager._physical_slot_locked("mailbox_code", 0), 2)
        self.assertEqual(manager._physical_slot_locked("registration", 0), 0)

    def test_malformed_managed_pid_record_does_not_abort_legacy_scan(self) -> None:
        manager = ManagedMihomoManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "managed_mihomo"
            thread_root = runtime_root / "thread-1"
            thread_root.mkdir(parents=True)
            record_path = thread_root / "managed-instance.json"
            record_path.write_text('{"schema_version": 1, "pid": "not-a-pid"}\n', encoding="utf-8")
            with (
                patch.object(managed_mihomo_service, "RUNTIME_ROOT", runtime_root),
                patch.object(managed_mihomo_service.psutil, "process_iter", return_value=[]) as process_iter,
            ):
                manager._stop_persisted_instances_locked()

            process_iter.assert_called_once()
            self.assertFalse(record_path.exists())

    def test_managed_probe_rejects_unsupported_region(self) -> None:
        manager = ManagedMihomoManager()
        instance = SimpleNamespace(proxy_url="http://127.0.0.1:42000", fingerprint="test")
        trace = SimpleNamespace(status_code=200, text="ip=127.0.0.1\nloc=HK\n")
        with patch("services.managed_mihomo_service.curl_requests.get", return_value=trace):
            with self.assertRaisesRegex(RuntimeError, "节点地区不支持 OpenAI：HK"):
                manager._probe_selected_proxy(instance, "node-hk")

    def test_managed_probe_records_region_and_openai_status(self) -> None:
        manager = ManagedMihomoManager()
        instance = SimpleNamespace(proxy_url="http://127.0.0.1:42000", fingerprint="test")
        trace = SimpleNamespace(status_code=200, text="ip=127.0.0.1\nloc=US\n")
        openai = SimpleNamespace(status_code=302, text="")
        with patch("services.managed_mihomo_service.curl_requests.get", side_effect=[trace, openai]):
            result = manager._probe_selected_proxy(instance, "node-us")
        self.assertEqual(result["region"], "US")
        self.assertEqual(result["openai_status"], 302)
        self.assertFalse(result["openai_restricted"])
        self.assertGreaterEqual(result["latency_ms"], 1)

    def test_managed_probe_accepts_direct_403_for_nested_parent_transport(self) -> None:
        manager = ManagedMihomoManager()
        instance = SimpleNamespace(proxy_url="http://127.0.0.1:42000", fingerprint="test")
        trace = SimpleNamespace(status_code=200, text="ip=127.0.0.1\nloc=SG\n")
        openai = SimpleNamespace(status_code=403, text="")
        with patch("services.managed_mihomo_service.curl_requests.get", side_effect=[trace, openai]):
            result = manager._probe_selected_proxy(instance, "node-sg")
        self.assertEqual(result["openai_status"], 403)
        self.assertTrue(result["openai_restricted"])

    def test_registration_disallowed_is_not_misclassified_as_cloudflare(self) -> None:
        self.assertFalse(_is_cloudflare_blocked_error("cf-ray=abc code=registration_disallowed"))
        self.assertTrue(_is_cloudflare_blocked_error("Cloudflare Just a moment"))

    def test_subscription_network_fetch_runs_outside_store_lock(self) -> None:
        def fetch(_url: str, **_kwargs: object) -> str:
            acquired = proxy_pool_store._lock.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                proxy_pool_store._lock.release()
            return "http://127.0.0.1:7897"

        with patch("services.proxy_pool_store._fetch_text", side_effect=fetch):
            result = proxy_pool_store.add_subscriptions([{"name": "test", "url": "https://subscription.invalid/list"}])

        self.assertEqual(result["imported_nodes"], 1)
        self.assertEqual(result["errors"], [])

    def test_subscription_rejects_file_urls_before_storage(self) -> None:
        result = proxy_pool_store.add_subscriptions(
            [{"name": "unsafe", "url": "file:///C:/Windows/win.ini"}],
            fetch=False,
        )

        self.assertEqual(result["subscriptions"], [])
        self.assertEqual(len(result["errors"]), 1)

    def test_subscription_rejects_private_hosts_by_default(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "allow_private"):
            proxy_pool_store._validate_subscription_url("http://127.0.0.1:8080/sub")

        self.assertEqual(
            proxy_pool_store._validate_subscription_url(
                "http://127.0.0.1:8080/sub",
                allow_private=True,
            ),
            "http://127.0.0.1:8080/sub",
        )

    def test_subscription_download_is_bounded(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.geturl.return_value = "https://subscriptions.example/list"
        response.headers = {}
        response.read.return_value = b"x" * (proxy_pool_store.MAX_SUBSCRIPTION_BYTES + 1)
        opener = MagicMock()
        opener.open.return_value = response

        with (
            patch.object(proxy_pool_store.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
            patch.object(proxy_pool_store.urllib.request, "build_opener", return_value=opener),
        ):
            with self.assertRaisesRegex(RuntimeError, "exceeds 5 MiB"):
                proxy_pool_store._fetch_text("https://subscriptions.example/list")

        response.read.assert_called_once_with(proxy_pool_store.MAX_SUBSCRIPTION_BYTES + 1)

    def test_subscription_download_accepts_a_format_specific_user_agent(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.geturl.return_value = "https://subscriptions.example/list"
        response.headers = {}
        response.read.return_value = b"proxies: []"
        opener = MagicMock()
        opener.open.return_value = response

        with (
            patch.object(proxy_pool_store.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
            patch.object(proxy_pool_store.urllib.request, "build_opener", return_value=opener),
        ):
            result = proxy_pool_store._fetch_text(
                "https://subscriptions.example/list",
                user_agent="Clash.Meta/test",
            )

        request = opener.open.call_args.args[0]
        self.assertEqual(result, "proxies: []")
        self.assertEqual(request.get_header("User-agent"), "Clash.Meta/test")

    def test_managed_subscription_requests_clash_meta_format(self) -> None:
        with patch.object(proxy_pool_store, "_fetch_text", return_value="proxies: []") as fetch:
            result = managed_mihomo_service._fetch_subscription("https://subscriptions.example/list")

        self.assertEqual(result, "proxies: []")
        fetch.assert_called_once_with(
            "https://subscriptions.example/list",
            allow_private=False,
            user_agent=managed_mihomo_service.SUBSCRIPTION_USER_AGENT,
        )

    def test_managed_subscription_can_read_clash_profile_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "clash-verge"
            profile = root / "profiles" / "cached.yaml"
            profile.parent.mkdir(parents=True)
            profile.write_text("proxies: []\n", encoding="utf-8")
            with patch.object(managed_mihomo_service, "_clash_profile_roots", return_value=[root.resolve()]):
                result = managed_mihomo_service._fetch_subscription(profile.as_uri())

        self.assertEqual(result, "proxies: []")

    def test_managed_subscription_rejects_local_file_outside_clash_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "clash-verge"
            root.mkdir()
            outside = Path(temp_dir) / "outside.yaml"
            outside.write_text("proxies: []\n", encoding="utf-8")
            with patch.object(managed_mihomo_service, "_clash_profile_roots", return_value=[root.resolve()]):
                with self.assertRaisesRegex(RuntimeError, "Clash profile directory"):
                    managed_mihomo_service._fetch_subscription(outside.as_uri())

    def test_account_proxy_has_priority_over_pool_assignment(self) -> None:
        result = proxy_pool_store.assign_proxy(
            1,
            account={"proxy": "http://127.0.0.1:9000"},
            purpose="inventory_health",
        )
        self.assertEqual(result["proxy"], "http://127.0.0.1:9000")
        self.assertEqual(result["source"], "account")
        self.assertEqual(result["purpose"], "inventory_health")

    def test_legacy_managed_local_port_is_not_reused_without_node_record(self) -> None:
        proxy_pool_store.update_proxy_pool_config({
            "enabled": True,
            "node_mode": "managed_mihomo",
            "managed_mihomo": {"enabled": True, "proxy_host": "127.0.0.1", "proxy_port_base": 42000},
        })

        result = proxy_pool_store.assign_proxy(
            1,
            account={"proxy": "http://127.0.0.1:42006"},
            purpose="partial_account_health",
        )

        self.assertTrue(result["sticky_failure"])
        self.assertTrue(result["legacy_proxy"])
        self.assertEqual(result["proxy"], "")
        self.assertIn("无法确认注册时的真实节点", result["error"])

    def test_stop_during_proxy_preflight_does_not_submit_worker(self) -> None:
        service = RegisterService(Path(self._temp_dir.name) / "register.json")
        service._config.update({"enabled": True, "total": 1, "threads": 1})

        def stop_during_assignment(*_args, **_kwargs):
            service._config["enabled"] = False
            return {"proxy": "http://127.0.0.1:42000", "source": "managed_mihomo", "slot": 1}

        with (
            patch("services.register_service.proxy_pool_store.assign_proxy", side_effect=stop_during_assignment),
            patch("services.register_service.openai_register.worker") as worker,
        ):
            service._run()
        worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
