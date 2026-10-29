"""Configuration-driven, bounded, read-only Zabbix and Influx monitoring."""
from __future__ import annotations

import csv
import datetime as dt
import importlib.resources
import io
import json
import math
import os
import re
import stat
import time
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigurationError, load_scalar_yaml
from .http import ApiError, request_raw

MAX_CONFIG_BYTES = 256_000
MAX_HOSTS = 100
MAX_SAMPLES_PER_SIGNAL = 10_000
MAX_REPORT_BYTES = 256_000
MAX_ZABBIX_API_CALLS = 1_000
UTC = dt.timezone.utc


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool, type(None))):
            raise yaml.constructor.ConstructorError("while constructing a mapping", node.start_mark,
                                                    "mapping keys must be scalars", key_node.start_mark)
        if key in mapping:
            raise yaml.constructor.ConstructorError("while constructing a mapping", node.start_mark,
                                                    f"duplicate key {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                                 _construct_unique_mapping)


def _bounded_int(value: Any, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer between {low} and {high}")
    return value


def load_monitoring_config(path: str | None = None) -> dict[str, Any]:
    """Load bounded, safe-YAML check definitions (never credentials)."""
    if path:
        target = Path(path).expanduser()
        try:
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
                raise ConfigurationError("Monitoring configuration must be a regular file under 256 KB")
            raw = target.read_text(encoding="utf-8")
        except OSError:
            raise ConfigurationError("Monitoring configuration is unavailable") from None
    else:
        try:
            target = importlib.resources.files("hbf_api_tools").joinpath("monitoring.yml")
            raw = target.read_text(encoding="utf-8")
        except OSError:
            raise ConfigurationError("Bundled monitoring configuration is unavailable") from None
    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ConfigurationError("Monitoring configuration exceeds 256 KB")
    try:
        data = yaml.load(raw, Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, TypeError):
        raise ConfigurationError("Monitoring configuration contains invalid YAML") from None
    if not isinstance(data, dict) or set(data) - {"zabbix", "influx"}:
        raise ConfigurationError("Monitoring configuration must contain only zabbix and influx profiles")
    for provider in ("zabbix", "influx"):
        profiles = data.get(provider, {})
        if not isinstance(profiles, dict) or len(profiles) > 100:
            raise ConfigurationError(f"Invalid {provider} profile map")
        for name, profile in profiles.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
                raise ConfigurationError(f"Invalid {provider} profile name")
            if not isinstance(profile, dict):
                raise ConfigurationError(f"Invalid {provider} profile {name}")
    return data


def list_profiles(config: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    return {provider: [{"name": name, "description": str(profile.get("description", ""))[:200]}
                       for name, profile in sorted(config.get(provider, {}).items())]
            for provider in ("zabbix", "influx")}


def _profile(config: dict[str, Any], provider: str, name: str) -> dict[str, Any]:
    try:
        profile = config[provider][name]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown {provider} monitoring profile") from None
    if not isinstance(profile, dict):
        raise ValueError("Invalid monitoring profile")
    return profile


def _https_endpoint(value: str, label: str, api_path: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ConfigurationError(f"{label} URL must be a valid HTTPS origin/path")
    base = urllib.parse.urlunsplit(("https", parsed.netloc, "", "", ""))
    path = parsed.path.rstrip("/")
    route = path if path.endswith(api_path) else (path + api_path) or api_path
    return base, route


class ZabbixClient:
    """Small JSON-RPC client; callers can issue only methods used by reports."""

    def __init__(self) -> None:
        conf = load_scalar_yaml("zabbix.yml")
        url = conf.get("zabbix_url", "")
        token = conf.get("zabbix_token_value", "")
        if not token:
            raise ConfigurationError("Zabbix token is missing")
        self.base, self.route = _https_endpoint(url, "Zabbix", "/api_jsonrpc.php")
        self.token = token
        self.request_id = 0
        self.call_count = 0

    def call(self, method: str, params: dict[str, Any]) -> Any:
        if method not in {"apiinfo.version", "host.get", "item.get", "history.get", "problem.get", "trigger.get"}:
            raise ValueError("Unsupported Zabbix read method")
        self.call_count += 1
        if self.call_count > MAX_ZABBIX_API_CALLS:
            raise ValueError("Zabbix profile exceeded the API call safety limit")
        self.request_id += 1
        try:
            headers_token = self.token if method != "apiinfo.version" else None
            raw = request_raw(self.base, self.route, headers_token, method="POST",
                              payload={"jsonrpc": "2.0", "method": method,
                                       "params": params, "id": self.request_id})
            data = json.loads(raw)
        except (ApiError, UnicodeError, ValueError):
            raise RuntimeError("Zabbix API request failed") from None
        if not isinstance(data, dict) or "error" in data or "result" not in data:
            raise RuntimeError(f"Zabbix API rejected {method}")
        return data["result"]


def _timestamp(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, UTC).isoformat()


def _summarise(rows: Iterable[tuple[int, float]], thresholds: list[int] | None = None) -> dict[str, Any]:
    ordered = sorted(rows)
    if not ordered:
        return {"available": False, "samples": 0}
    values = sorted(value for _, value in ordered)
    count = len(values)
    result: dict[str, Any] = {
        "available": True, "samples": count,
        "first_utc": _timestamp(ordered[0][0]), "last_utc": _timestamp(ordered[-1][0]),
        "latest": round(ordered[-1][1], 2), "mean": round(sum(values) / count, 2),
        "min": round(values[0], 2), "max": round(values[-1], 2),
        "p95": round(values[int((count - 1) * .95)], 2),
    }
    if thresholds:
        threshold_results = {}
        for threshold in thresholds:
            episodes: list[list[tuple[int, float]]] = []
            current: list[tuple[int, float]] = []
            for item in ordered:
                if item[1] >= threshold:
                    if current and item[0] - current[-1][0] > 120:
                        episodes.append(current)
                        current = []
                    current.append(item)
                elif current:
                    episodes.append(current)
                    current = []
            if current:
                episodes.append(current)
            longest = max(episodes, key=len, default=[])
            threshold_results[str(threshold)] = {
                "samples_at_or_above": sum(v >= threshold for _, v in ordered),
                "episodes": len(episodes),
                "longest_episode_samples": len(longest),
                "longest_start_utc": _timestamp(longest[0][0]) if longest else None,
            }
        result["thresholds"] = threshold_results
    return result


def _validate_patterns(patterns: Any, label: str, *, item_keys: bool = False) -> list[str]:
    if not isinstance(patterns, list) or not 1 <= len(patterns) <= 100:
        raise ValueError(f"{label} must contain 1 to 100 patterns")
    pattern = r"[A-Za-z0-9_.:, \[\]*-]{1,180}" if item_keys else r"[A-Za-z0-9_.:*\-]{1,180}"
    if any(not isinstance(x, str) or not re.fullmatch(pattern, x)
           for x in patterns):
        raise ValueError(f"{label} contains an invalid pattern")
    return patterns


def _glob_match(pattern: str, value: str) -> bool:
    """Treat only * as a wildcard; brackets are literal Zabbix key syntax."""
    expression = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
    return re.fullmatch(expression, value) is not None


def _zabbix_report(client: ZabbixClient, profile: dict[str, Any], hours: int) -> dict[str, Any]:
    hosts_patterns = _validate_patterns(profile.get("hosts"), "hosts")
    metrics = profile.get("metrics")
    if not isinstance(metrics, dict) or len(metrics) > 100:
        raise ValueError("Profile metrics must be a map of at most 100 entries")
    now = time.time_ns() // 1_000_000_000
    max_sample_age = profile.get("max_sample_age_minutes")
    if max_sample_age is not None:
        max_sample_age = _bounded_int(max_sample_age, "max_sample_age_minutes", 1, 10080)
    start = now - hours * 3600
    found: dict[str, dict[str, Any]] = {}
    for pattern in hosts_patterns:
        matches = client.call("host.get", {"search": {"host": pattern},
                                             "searchWildcardsEnabled": True,
                                             "output": ["hostid", "host", "status"]})
        if not isinstance(matches, list):
            raise RuntimeError("Zabbix returned invalid host data")
        for host in matches:
            if (isinstance(host, dict) and isinstance(host.get("host"), str)
                    and _glob_match(pattern, host["host"])):
                found[host["host"]] = host
        if len(found) > MAX_HOSTS:
            raise ValueError("Profile matched more than 100 hosts")
    result: dict[str, Any] = {"provider": "zabbix", "lookback_hours": hours,
                              "checked_utc": _timestamp(now), "api_version": client.call("apiinfo.version", {}),
                              "profile": profile.get("description", ""), "hosts": {}, "alerts": {}}
    for hostname, host in sorted(found.items()):
        telemetry: dict[str, Any] = {"found": True, "enabled": host.get("status") == "0", "metrics": {}}
        for metric_name, definition in metrics.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", str(metric_name)) or not isinstance(definition, dict):
                raise ValueError("Invalid Zabbix metric definition")
            keys = _validate_patterns(definition.get("item_keys"), "item_keys", item_keys=True)
            thresholds = definition.get("thresholds", [])
            if (not isinstance(thresholds, list) or len(thresholds) > 20
                    or any(type(x) not in (int, float) for x in thresholds)):
                raise ValueError("Invalid metric thresholds")
            matched_by_id: dict[str, dict[str, Any]] = {}
            for key in keys:
                found_items = client.call("item.get", {"hostids": [host["hostid"]],
                                                        "search": {"key_": key},
                                                        "searchWildcardsEnabled": True,
                                                        "output": ["itemid", "key_", "value_type", "lastclock", "status"]})
                if not isinstance(found_items, list):
                    raise RuntimeError("Zabbix returned invalid item data")
                for item in found_items:
                    if (isinstance(item, dict) and item.get("status") == "0"
                            and _glob_match(key, str(item.get("key_", "")))):
                        matched_by_id[str(item.get("itemid"))] = item
                if len(matched_by_id) > 20:
                    raise ValueError("A metric selector matched more than 20 items; narrow the profile")
            matched = list(matched_by_id.values())
            values = []
            for item in matched[:20]:
                try:
                    history_type = _bounded_int(int(item["value_type"]), "value_type", 0, 5)
                    history = client.call("history.get", {
                        "history": history_type, "itemids": [item["itemid"]],
                        "time_from": start, "time_till": now,
                        "sortfield": "clock", "sortorder": "ASC", "limit": MAX_SAMPLES_PER_SIGNAL,
                    })
                    if not isinstance(history, list):
                        raise RuntimeError("Zabbix returned invalid history")
                    values.extend((int(row["clock"]), float(row["value"])) for row in history
                                  if isinstance(row, dict) and str(row.get("clock", "")).isdigit()
                                  and math.isfinite(float(row["value"])))
                except (KeyError, TypeError, ValueError):
                    raise RuntimeError("Zabbix returned malformed history data") from None
            summary = _summarise(values, [int(x) for x in thresholds])
            if summary.get("available"):
                age = round((now - max(stamp for stamp, _ in values)) / 60, 1)
                summary["last_sample_age_minutes"] = age
                if max_sample_age is not None:
                    summary["fresh"] = -5 <= age <= max_sample_age
            telemetry["metrics"][metric_name] = summary
        result["hosts"][hostname] = telemetry
    alert_cfg = profile.get("alerts", {})
    if alert_cfg:
        if not isinstance(alert_cfg, dict):
            raise ValueError("Invalid alert configuration")
        minimum = _bounded_int(alert_cfg.get("minimum_severity", 4), "minimum_severity", 0, 5)
        terms = alert_cfg.get("availability_terms", [])
        if not isinstance(terms, list) or len(terms) > 50 or any(not isinstance(x, str) or len(x) > 80 for x in terms):
            raise ValueError("Invalid availability terms")
        problems = client.call("problem.get", {"source": 0, "object": 0,
                                                 "output": ["eventid", "name", "severity", "clock", "acknowledged", "objectid"],
                                                 "sortfield": "eventid", "sortorder": "DESC", "limit": 600})
        if not isinstance(problems, list):
            raise RuntimeError("Zabbix returned invalid problem data")
        selected = [p for p in problems if isinstance(p, dict) and
                    (int(p.get("severity", 0)) >= minimum or any(term.lower() in str(p.get("name", "")).lower() for term in terms))]
        trigger_ids = list(dict.fromkeys(str(p.get("objectid")) for p in selected if p.get("objectid")))
        host_by_trigger: dict[str, list[str]] = {}
        for offset in range(0, len(trigger_ids), 100):
            triggers = client.call("trigger.get", {"triggerids": trigger_ids[offset:offset + 100],
                                                     "output": ["triggerid"], "selectHosts": ["host"]})
            if not isinstance(triggers, list):
                raise RuntimeError("Zabbix returned invalid trigger data")
            for trigger in triggers:
                host_by_trigger[str(trigger.get("triggerid"))] = [str(h.get("host")) for h in trigger.get("hosts", []) if isinstance(h, dict)]
        serialize = lambda p: {"eventid": str(p.get("eventid", "")), "name": str(p.get("name", ""))[:240],
                               "severity": int(p.get("severity", 0)), "started_utc": _timestamp(int(p.get("clock", now))),
                               "age_hours": round((now - int(p.get("clock", now))) / 3600, 1),
                               "acknowledged": p.get("acknowledged") == "1",
                               "hosts": host_by_trigger.get(str(p.get("objectid")), [])}
        high = [p for p in selected if int(p.get("severity", 0)) >= minimum]
        unavailable = [p for p in selected if any(t.lower() in str(p.get("name", "")).lower() for t in terms)]
        result["alerts"] = {"unresolved_selected": len(selected),
                            "new": [serialize(p) for p in high if int(p.get("clock", 0)) >= start][:50],
                            "older": [serialize(p) for p in high if int(p.get("clock", 0)) < start][:50],
                            "availability": [serialize(p) for p in unavailable[:50]]}
    complete = bool(found) and all(
        all(metric.get("available", False) and metric.get("fresh", True)
            for metric in host_data["metrics"].values())
        for host_data in result["hosts"].values()
    )
    has_alerts = bool(result.get("alerts", {}).get("new") or result.get("alerts", {}).get("older")
                      or result.get("alerts", {}).get("availability"))
    result["data_complete"] = complete
    result["status"] = "ALERT" if has_alerts else "OK" if complete else "UNKNOWN"
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise RuntimeError("Zabbix report exceeded the output limit")
    return result


def zabbix_report(name: str, hours: int = 24, *, config_path: str | None = None) -> dict[str, Any]:
    hours = _bounded_int(hours, "hours", 1, 168)
    profile = _profile(load_monitoring_config(config_path), "zabbix", name)
    return _zabbix_report(ZabbixClient(), profile, hours)


def _flux_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


class InfluxClient:
    def __init__(self, credential_name: str) -> None:
        if not isinstance(credential_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", credential_name):
            raise ValueError("Invalid Influx credential profile")
        conf = load_scalar_yaml(f"influxdb_{credential_name}.yml")
        self.base, self.route = _https_endpoint(conf.get("influx_url", ""), "InfluxDB", "/api/v2/query")
        self.org = conf.get("influx_org", "")
        self.bucket = conf.get("influx_bucket", "")
        self.token = conf.get("influx_token", "")
        if not all(isinstance(v, str) and v for v in (self.org, self.bucket, self.token)):
            raise ConfigurationError("InfluxDB credentials are incomplete")
        if self.bucket != credential_name:
            raise ConfigurationError("InfluxDB credential bucket does not match its filename")

    def query_signal(self, name: str, definition: dict[str, Any], hours: int) -> list[tuple[int, float]]:
        field = definition.get("field")
        tags = definition.get("tags")
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", field):
            raise ValueError("Invalid Influx signal field")
        if not isinstance(tags, dict) or not 1 <= len(tags) <= 12:
            raise ValueError("Influx signal needs 1 to 12 exact tag matchers")
        clauses = [f'r._field == {_flux_quote(field)}']
        measurement = definition.get("measurement")
        if measurement is not None:
            if not isinstance(measurement, str) or not measurement or len(measurement) > 180:
                raise ValueError("Invalid Influx measurement selector")
            clauses.append(f'r._measurement == {_flux_quote(measurement)}')
        for key, value in tags.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key)
                    or not isinstance(value, str) or not value or len(value) > 180):
                raise ValueError("Invalid Influx tag matcher")
            clauses.append(f'r[{_flux_quote(key)}] == {_flux_quote(value)}')
        aggregate = definition.get("aggregate_window")
        if aggregate is not None and (not isinstance(aggregate, str) or not re.fullmatch(r"[1-9][0-9]{0,3}(s|m|h)", aggregate)):
            raise ValueError("Invalid aggregate window")
        aggregate_function = definition.get("aggregate_function", "mean")
        if aggregate_function not in {"mean", "min", "max", "sum", "last"}:
            raise ValueError("Unsupported Influx aggregate function")
        flux = (f'from(bucket: {_flux_quote(self.bucket)})\n'
                f'  |> range(start: -{hours}h)\n'
                f'  |> filter(fn: (r) => {" and ".join(clauses)})\n')
        if aggregate:
            flux += f'  |> aggregateWindow(every: {aggregate}, fn: {aggregate_function}, createEmpty: false)\n'
        flux += ('  |> keep(columns: ["_time", "_value"])\n'
                 '  |> sort(columns: ["_time"], desc: true)\n'
                 f'  |> limit(n: {MAX_SAMPLES_PER_SIGNAL})\n'
                 '  |> sort(columns: ["_time"])')
        try:
            raw = request_raw(self.base, self.route, self.token, method="POST",
                              query={"org": self.org}, payload=flux,
                              auth_scheme="Token", accept="application/csv",
                              content_type="application/vnd.flux")
            decoded = raw.decode("utf-8")
        except (ApiError, UnicodeError):
            raise RuntimeError("InfluxDB query failed") from None
        values: list[tuple[int, float]] = []
        for row in csv.DictReader(io.StringIO(decoded)):
            stamp, value = row.get("_time"), row.get("_value")
            if not stamp or value in (None, ""):
                continue
            try:
                parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                numeric = float(value)
                if math.isfinite(numeric):
                    values.append((int(parsed.timestamp()), numeric))
            except (ValueError, OverflowError):
                continue
            if len(values) >= MAX_SAMPLES_PER_SIGNAL:
                break
        return values


def _in_range(value: float | None, limits: list[Any]) -> bool:
    if (not isinstance(limits, (list, tuple)) or len(limits) != 2
            or any(type(x) not in (int, float) for x in limits)
            or float(limits[0]) > float(limits[1])):
        raise ValueError("Invalid numeric range in monitoring rule")
    return value is not None and float(limits[0]) <= value <= float(limits[1])


def _epoch_values(rows: list[tuple[int, float]]) -> dict[int, float]:
    return {stamp: value for stamp, value in sorted(rows)}


def _align_signals(raw: dict[str, list[tuple[int, float]]], derived: dict[str, Any],
                   anchor_name: str, stale_seconds: int) -> list[dict[str, Any]]:
    if anchor_name not in raw:
        raise ValueError("Influx evaluation anchor must name a configured signal")
    points_by_signal = {name: sorted(_epoch_values(rows).items()) for name, rows in raw.items()}
    for name, spec in derived.items():
        if not isinstance(spec, dict) or spec.get("operation") not in {"subtract", "add", "multiply", "divide"}:
            raise ValueError("Unsupported derived signal operation")
        left, right = spec.get("left"), spec.get("right")
        if left not in raw or right not in raw:
            raise ValueError("Derived signals must refer to configured source signals")
    anchor_times = [t for t, _ in points_by_signal[anchor_name]]
    if not anchor_times:
        return []
    result = []
    positions = {name: 0 for name in raw}
    last: dict[str, tuple[int, float]] = {}
    for timestamp in anchor_times:
        row: dict[str, Any] = {"time": timestamp}
        for name, points in points_by_signal.items():
            i = positions[name]
            while i < len(points) and points[i][0] <= timestamp:
                last[name] = points[i]
                i += 1
            positions[name] = i
            point = last.get(name)
            row[name] = point[1] if point and timestamp - point[0] <= stale_seconds else None
        for name, spec in derived.items():
            left, right = spec["left"], spec["right"]
            a, b = row[left], row[right]
            if a is None or b is None or (spec["operation"] == "divide" and b == 0):
                row[name] = None
            else:
                operation = spec["operation"]
                value = (a - b if operation == "subtract" else a + b if operation == "add"
                         else a * b if operation == "multiply" else a / b)
                row[name] = value if math.isfinite(value) else None
        result.append(row)
    return result


def _rule_alerts(samples: list[dict[str, Any]], rules: Any) -> list[dict[str, Any]]:
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Influx rules must be a list of at most 100 entries")
    alerts = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Invalid Influx rule")
        rule_id, kind = rule.get("id"), rule.get("type")
        if not isinstance(rule_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", rule_id):
            raise ValueError("Invalid Influx rule id")
        minutes = _bounded_int(rule.get("duration_minutes", rule.get("window_minutes", 1)), "rule minutes", 1, 1440)
        hits: list[tuple[int, dict[str, Any]]] = []
        if kind == "threshold":
            signal = rule.get("signal")
            operator = rule.get("operator")
            threshold = rule.get("value")
            if (not isinstance(signal, str) or operator not in {"gt", "gte", "lt", "lte", "eq"}
                    or type(threshold) not in (int, float) or not math.isfinite(float(threshold))):
                raise ValueError("Invalid Influx threshold rule")
            latest = next((row for row in reversed(samples) if row.get(signal) is not None), None)
            if latest is not None:
                observed = float(latest[signal])
                matched = ((operator == "gt" and observed > threshold) or (operator == "gte" and observed >= threshold)
                           or (operator == "lt" and observed < threshold) or (operator == "lte" and observed <= threshold)
                           or (operator == "eq" and observed == threshold))
                if matched:
                    alerts.append({"id": rule_id, "value": round(observed, 2),
                                   "at_utc": _timestamp(latest["time"])})
            continue
        if kind in {"sustained_delta", "sustained_range_mismatch", "sustained_pair"}:
            for row in samples:
                when = rule.get("when_signal")
                if kind == "sustained_pair":
                    active = row.get(rule.get("first_signal")) == rule.get("first_equals") and _in_range(row.get(rule.get("second_signal")), rule.get("second_between", []))
                else:
                    active = _in_range(row.get(when), rule.get("when_between", []))
                if kind == "sustained_range_mismatch":
                    active = active and row.get(rule.get("measure_signal")) is not None and not _in_range(row.get(rule.get("measure_signal")), rule.get("expected_between", []))
                if kind == "sustained_delta":
                    active = active and row.get(rule.get("measure_signal")) is not None
                if active:
                    hits.append((row["time"], row))
            groups: list[list[tuple[int, dict[str, Any]]]] = []
            group = []
            for point in hits:
                if group and point[0] - group[-1][0] > 120:
                    groups.append(group)
                    group = []
                group.append(point)
            if group:
                groups.append(group)
            for group in groups:
                minimum_samples = _bounded_int(rule.get("minimum_samples", minutes + 1),
                                               "minimum_samples", 1, MAX_SAMPLES_PER_SIGNAL)
                if len(group) < minimum_samples:
                    continue
                elapsed = (group[-1][0] - group[0][0]) / 60
                extra: dict[str, Any] = {}
                if kind == "sustained_delta":
                    values = [p[1].get(rule.get("measure_signal")) for p in group]
                    values = [float(x) for x in values if x is not None and math.isfinite(float(x))]
                    if len(values) < 2:
                        continue
                    change = (values[0] - values[-1]) if rule.get("measure") == "decrease" else (values[-1] - values[0])
                    op = rule.get("operator")
                    threshold = float(rule.get("value"))
                    if not ((op == "lt" and change < threshold) or (op == "lte" and change <= threshold)
                            or (op == "gt" and change > threshold) or (op == "gte" and change >= threshold)):
                        continue
                    extra["change"] = round(change, 2)
                alerts.append({"id": rule_id, "minutes": round(elapsed, 1), "samples": len(group),
                               "from_utc": _timestamp(group[0][0]), "to_utc": _timestamp(group[-1][0]), **extra})
        elif kind == "paired_window_delta":
            window = _bounded_int(rule.get("window_minutes"), "window_minutes", 1, 1440) * 60
            ordered = samples
            for i, row in enumerate(ordered):
                if not _in_range(row.get(rule.get("when_signal")), rule.get("when_between", [])):
                    continue
                initial = row.get(rule.get("measure_signal"))
                if initial is None:
                    continue
                previous_time = row["time"]
                for end in ordered[i + 1:]:
                    if end["time"] - row["time"] > window:
                        break
                    if end["time"] - previous_time > 120:
                        break
                    previous_time = end["time"]
                    if not _in_range(end.get(rule.get("when_signal")), rule.get("when_between", [])):
                        break
                    final = end.get(rule.get("measure_signal"))
                    if final is None:
                        break
                    change = float(initial) - float(final) if rule.get("measure", "decrease") == "decrease" else float(final) - float(initial)
                    threshold = float(rule.get("value"))
                    if ((rule.get("operator") == "gte" and change >= threshold)
                            or (rule.get("operator") == "gt" and change > threshold)):
                        alerts.append({"id": rule_id, "change": round(change, 2),
                                       "from_utc": _timestamp(row["time"]), "to_utc": _timestamp(end["time"])})
                        break
        else:
            raise ValueError("Unsupported Influx rule type")
        if len(alerts) > 100:
            return alerts[:100]
    return alerts


def _influx_report(client: InfluxClient, profile: dict[str, Any], hours: int | None) -> dict[str, Any]:
    window = _bounded_int(hours if hours is not None else profile.get("window_hours", 24), "hours", 1, 168)
    signals = profile.get("signals")
    if not isinstance(signals, dict) or not 1 <= len(signals) <= 100:
        raise ValueError("Influx profile needs 1 to 100 signals")
    raw = {name: client.query_signal(name, definition,
                                     window if hours is not None else
                                     min(window, _bounded_int(definition.get("range_hours", window), "signal range_hours", 1, 168)))
           for name, definition in signals.items()
           if isinstance(name, str) and isinstance(definition, dict)}
    if len(raw) != len(signals):
        raise ValueError("Invalid Influx signal definitions")
    derived = profile.get("derived_signals", {})
    if not isinstance(derived, dict) or len(derived) > 100:
        raise ValueError("Invalid derived signal map")
    if set(signals) & set(derived) or any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
                                          for name in derived):
        raise ValueError("Derived signal names must be unique lowercase identifiers")
    anchor = profile.get("evaluation_anchor", next(iter(signals)))
    stale_minutes = _bounded_int(profile.get("max_staleness_minutes", 5), "max_staleness_minutes", 1, 1440)
    samples = _align_signals(raw, derived, anchor, stale_minutes * 60)
    evaluation_hours = (window if hours is not None else
                        min(window, _bounded_int(profile.get("evaluation_hours", window), "evaluation_hours", 1, 168)))
    evaluation_start = int(dt.datetime.now(UTC).timestamp()) - evaluation_hours * 3600
    samples = [sample for sample in samples if sample["time"] >= evaluation_start]
    latest = samples[-1] if samples else {}
    alerts = _rule_alerts(samples, profile.get("rules", []))
    series_summary = {name: {"available": bool(rows), "samples": len(rows),
                             "first_utc": _timestamp(rows[0][0]) if rows else None,
                             "last_utc": _timestamp(rows[-1][0]) if rows else None,
                             "latest": round(rows[-1][1], 2) if rows else None,
                             "min": round(min((v for _, v in rows), default=0), 2) if rows else None,
                             "max": round(max((v for _, v in rows), default=0), 2) if rows else None}
                      for name, rows in raw.items()}
    for name, rows in raw.items():
        series_summary[name]["latest_time_utc"] = _timestamp(rows[-1][0]) if rows else None
    required_signals = [name for name, definition in signals.items() if definition.get("required", True)]
    max_latest_age = profile.get("max_latest_age_minutes")
    if max_latest_age is not None:
        max_latest_age = _bounded_int(max_latest_age, "max_latest_age_minutes", 1, 10080)
    latest_age = (int(dt.datetime.now(UTC).timestamp()) - latest["time"]) / 60 if samples else None
    anchor_fresh = latest_age is not None and (max_latest_age is None or -5 <= latest_age <= max_latest_age)
    complete = bool(samples) and anchor_fresh and all(series_summary[name]["available"] for name in required_signals)
    complete = complete and all(latest.get(name) is not None for name in required_signals)
    complete = complete and all(latest.get(name) is not None for name in derived)
    result = {"provider": "influx", "profile": profile.get("description", ""),
              "window_hours": window, "checked_utc": _timestamp(int(dt.datetime.now(UTC).timestamp())),
              "latest_anchor_age_minutes": round(latest_age, 1) if latest_age is not None else None,
              "signals": series_summary,
              "derived_signals": {name: {"latest": round(float(latest[name]), 2) if latest.get(name) is not None else None}
                                  for name in derived},
              "alerts": alerts, "data_complete": complete,
              "status": "ALERT" if alerts else "OK" if complete else "UNKNOWN"}
    if len(json.dumps(result).encode("utf-8")) > MAX_REPORT_BYTES:
        raise RuntimeError("Influx report exceeded the output limit")
    return result


def influx_report(name: str, hours: int | None = None, *, config_path: str | None = None) -> dict[str, Any]:
    profile = _profile(load_monitoring_config(config_path), "influx", name)
    credential_name = profile.get("credentials")
    if not isinstance(credential_name, str):
        raise ValueError("Influx profile must select a credential bucket")
    return _influx_report(InfluxClient(credential_name), profile, hours)
