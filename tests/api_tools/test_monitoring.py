from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hbf_api_tools import config
from hbf_api_tools.cli import main
from hbf_api_tools.monitoring import (
    InfluxClient, ZabbixClient, _align_signals, _rule_alerts, _summarise,
    influx_report, list_profiles, load_monitoring_config, zabbix_report,
)


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.credentials = self.root / ".credentials"
        self.credentials.mkdir(mode=0o700)
        for name, body in (
            ("zabbix.yml", "zabbix_url: https://zabbix.example/zabbix\nzabbix_token_value: secret\n"),
            ("influxdb_test-bucket.yml", "influx_url: https://influx.example\ninflux_org: hbf\ninflux_bucket: test-bucket\ninflux_token: secret\n"),
        ):
            path = self.credentials / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o600)
        self.env = patch.dict(os.environ, {"HBF_CREDENTIALS_DIR": str(self.credentials)}, clear=False)
        self.env.start()
        self.config_file = self.root / "monitoring.yml"
        self.config_file.write_text(
            "zabbix:\n  sample:\n    description: test\n    hosts: [node*]\n"
            "    metrics:\n      temp:\n        item_keys: ['sensor.temp[*]']\n        thresholds: [50, 70]\n"
            "influx:\n  sample:\n    window_hours: 2\n    evaluation_anchor: power\n"
            "    signals:\n      power:\n        field: value\n        tags: {entity_id: heater}\n"
            "      temperature:\n        field: value\n        tags: {entity_id: temp}\n"
            "    rules: []\n", encoding="utf-8")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_bundled_profiles_are_yaml_and_complete(self):
        profiles = list_profiles(load_monitoring_config())
        self.assertEqual([x["name"] for x in profiles["zabbix"]], ["pve_temperatures"])
        self.assertEqual([x["name"] for x in profiles["influx"]], ["kuk_boiler"])

    def test_config_file_is_bounded_regular_file(self):
        link = self.root / "alias.yml"
        link.symlink_to(self.config_file)
        with self.assertRaisesRegex(config.ConfigurationError, "regular file"):
            load_monitoring_config(str(link))
        self.config_file.write_text("x" * 256_001, encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigurationError, "regular file"):
            load_monitoring_config(str(self.config_file))

    def test_safe_yaml_rejects_python_object_tags(self):
        self.config_file.write_text("!!python/object/apply:os.system ['echo unsafe']", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigurationError, "invalid YAML"):
            load_monitoring_config(str(self.config_file))

    def test_config_rejects_duplicate_mapping_keys(self):
        self.config_file.write_text("zabbix:\n  sample: {}\nzabbix:\n  other: {}\n", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigurationError, "invalid YAML"):
            load_monitoring_config(str(self.config_file))

    def test_zabbix_endpoint_and_read_method_allowlist(self):
        with patch("hbf_api_tools.monitoring.request_raw", return_value=b'{"result":"7.0","id":1}') as request:
            api = ZabbixClient()
            self.assertEqual(api.call("apiinfo.version", {}), "7.0")
        self.assertEqual(request.call_args.args[:2], ("https://zabbix.example", "/zabbix/api_jsonrpc.php"))
        self.assertIsNone(request.call_args.args[2])
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            api.call("host.delete", {})

    def test_zabbix_endpoint_accepts_full_api_path_without_duplication(self):
        path = self.credentials / "zabbix.yml"
        path.write_text("zabbix_url: https://zabbix.example/zabbix/api_jsonrpc.php\nzabbix_token_value: secret\n")
        path.chmod(0o600)
        api = ZabbixClient()
        self.assertEqual((api.base, api.route),
                         ("https://zabbix.example", "/zabbix/api_jsonrpc.php"))

    def test_zabbix_report_uses_bounded_profile_selectors(self):
        class FakeZabbix:
            def call(self, method, params):
                if method == "host.get":
                    return [{"hostid": "4", "host": "node01", "status": "0"},
                            {"hostid": "5", "host": "other", "status": "0"}]
                if method == "apiinfo.version":
                    return "7.0"
                if method == "item.get":
                    return [{"itemid": "10", "key_": "sensor.temp[cpu]", "value_type": "0", "status": "0"}]
                if method == "history.get":
                    return [{"clock": "100", "value": "48"}, {"clock": "160", "value": "72"}]
                if method == "problem.get":
                    return []
                raise AssertionError(method)

        from hbf_api_tools.monitoring import _zabbix_report
        profile = load_monitoring_config(str(self.config_file))["zabbix"]["sample"]
        report = _zabbix_report(FakeZabbix(), profile, 24)
        self.assertEqual(list(report["hosts"]), ["node01"])
        self.assertEqual(report["hosts"]["node01"]["metrics"]["temp"]["max"], 72.0)
        self.assertEqual(report["hosts"]["node01"]["metrics"]["temp"]["thresholds"]["70"]["samples_at_or_above"], 1)
        profile["max_sample_age_minutes"] = 1
        stale = _zabbix_report(FakeZabbix(), profile, 24)
        self.assertEqual(stale["status"], "UNKNOWN")
        self.assertFalse(stale["data_complete"])

    def test_summary_reports_threshold_episodes_and_gaps(self):
        result = _summarise([(0, 75), (60, 80), (600, 81), (660, 40)], [70])
        threshold = result["thresholds"]["70"]
        self.assertEqual(threshold["samples_at_or_above"], 3)
        self.assertEqual(threshold["episodes"], 2)
        self.assertEqual(threshold["longest_episode_samples"], 2)

    def test_influx_request_is_query_scoped_and_read_only(self):
        csv_body = b",result,table,_time,_value\n,,0,2026-10-22T10:00:00Z,2000\n"
        with patch("hbf_api_tools.monitoring.request_raw", return_value=csv_body) as request:
            rows = InfluxClient("test-bucket").query_signal("power", {"field": "value", "tags": {"entity_id": "heater"}}, 24)
        self.assertEqual(len(rows), 1)
        self.assertEqual(request.call_args.kwargs["method"], "POST")
        self.assertEqual(request.call_args.kwargs["content_type"], "application/vnd.flux")
        self.assertEqual(request.call_args.kwargs["auth_scheme"], "Token")
        self.assertIn('r["entity_id"] == "heater"', request.call_args.kwargs["payload"])
        self.assertIn("limit(n: 10000)", request.call_args.kwargs["payload"])
        self.assertEqual(request.call_args.kwargs["query"], {"org": "hbf"})

    def test_influx_rejects_invalid_matchers_before_network(self):
        client = InfluxClient("test-bucket")
        with self.assertRaisesRegex(ValueError, "tag matcher"):
            client.query_signal("x", {"field": "value", "tags": {"entity_id) or true": "x"}}, 24)

    def test_align_signals_derives_and_rejects_stale_values(self):
        aligned = _align_signals({"main": [(60, 2400), (120, 2300)],
                                  "server": [(60, 400)], "heater": [(60, 2000)]},
                                 {"office": {"operation": "subtract", "left": "main", "right": "server"},
                                  "combined": {"operation": "add", "left": "main", "right": "server"}},
                                 "main", 30)
        self.assertEqual(aligned[0]["office"], 2000)
        self.assertEqual(aligned[0]["combined"], 2800)
        self.assertEqual(aligned[1]["office"], None)

    def test_rule_engine_covers_sustained_delta_range_pair_and_window(self):
        rows = []
        base = 1_800_000_000
        for i, (power, temp, office) in enumerate([
            (2000, 40, 1000), (2000, 40.5, 1000), (2000, 41, 1000),
            (2000, 41.5, 1000), (2000, 42, 1000), (0, 42, 2000), (0, 42, 2000),
        ]):
            rows.append({"time": base + i * 60, "power": power, "temp": temp, "office": office})
        rules = [
            {"id": "no_rise", "type": "sustained_delta", "when_signal": "power", "when_between": [1900, 2100],
             "duration_minutes": 3, "measure_signal": "temp", "measure": "delta", "operator": "lt", "value": 3},
            {"id": "mismatch", "type": "sustained_range_mismatch", "when_signal": "power", "when_between": [1900, 2100],
             "measure_signal": "office", "expected_between": [1700, 2800], "duration_minutes": 3},
            {"id": "pair", "type": "sustained_pair", "first_signal": "power", "first_equals": 0,
             "second_signal": "office", "second_between": [1700, 2800], "duration_minutes": 1},
            {"id": "drop", "type": "paired_window_delta", "when_signal": "power", "when_between": [1900, 2100],
             "measure_signal": "temp", "window_minutes": 2, "measure": "decrease", "operator": "gte", "value": 2},
        ]
        self.assertEqual({a["id"] for a in _rule_alerts(rows, rules)}, {"no_rise", "mismatch", "pair"})

    def test_influx_missing_measurements_fail_closed_as_unknown(self):
        class EmptyInflux:
            def query_signal(self, name, definition, hours):
                return []

        from hbf_api_tools.monitoring import _influx_report
        profile = load_monitoring_config(str(self.config_file))["influx"]["sample"]
        result = _influx_report(EmptyInflux(), profile, None)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["data_complete"])

    def test_explicit_influx_hours_override_profile_and_signal_ranges(self):
        class EmptyInflux:
            def __init__(self):
                self.ranges = []

            def query_signal(self, name, definition, hours):
                self.ranges.append(hours)
                return []

        from hbf_api_tools.monitoring import _influx_report
        profile = load_monitoring_config()["influx"]["kuk_boiler"]
        client = EmptyInflux()
        _influx_report(client, profile, 6)
        self.assertEqual(client.ranges, [6, 6, 6, 6])

    def test_threshold_rule_is_configurable_and_reports_latest_match(self):
        samples = [{"time": 1_800_000_000, "temperature": 81.2}]
        result = _rule_alerts(samples, [{"id": "hot", "type": "threshold", "signal": "temperature",
                                         "operator": "gte", "value": 80}])
        self.assertEqual(result[0]["id"], "hot")
        self.assertEqual(result[0]["value"], 81.2)

    def test_cli_lists_profiles_without_credentials_or_network(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["monitor", "list"]), 0)
        self.assertIn("pve_temperatures", out.getvalue())
        self.assertIn("kuk_boiler", out.getvalue())

    def test_cli_requires_profile_and_bounds_windows(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["monitor", "zabbix", "sample", "--hours", "0", "--config", str(self.config_file)]), 2)
        self.assertNotIn("secret", err.getvalue())


if __name__ == "__main__":
    unittest.main()
