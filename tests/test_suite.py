"""Regression tests using real v42 shard fixtures (read-only against Steam)."""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # run.py / run_desktop.py 位于项目根

# 测试保持纯 JSON/CSV 路径：否则 ensure_config 会在 fixtures 目录建 renamer.db，
# 污染只读样例并让后续用例走另一条分支。SQLite 由 DbStorageTests 显式覆盖。
os.environ["STEAM_CN_RENAMER_NO_DB"] = "1"

from backup import apply_changes, restore_backup  # noqa: E402
from cli import main  # noqa: E402
from mapping import build_plan  # noqa: E402
from rules import apply_rules, load_rules  # noqa: E402
from shard import (  # noqa: E402
    app_names,
    build_bytes,
    parse_file,
    verify,
)

FIXTURES = ROOT / "tests" / "fixtures" / "APPInfo"
FIXTURE_CONFIG = ROOT / "tests" / "fixtures" / "config"
SNAPSHOT_PATH = ROOT / "tests" / "snapshots" / "plan_snapshot.json"
# 测试只依赖 fixtures，不依赖项目根的运行时 config/（那里是用户的真实台账，
# 不入库也不应成为测试前提——否则 clone 下来就跑不过）。
CONFIG = FIXTURE_CONFIG


class ShardTests(unittest.TestCase):
    def test_parse_verify_and_lossless_roundtrip(self):
        for name in ("appinfo_17.vdf", "appinfo_00.vdf"):
            with self.subTest(shard=name):
                path = FIXTURES / name
                shard = parse_file(path)
                self.assertEqual(verify(shard), [])
                self.assertEqual(build_bytes(shard, {}), path.read_bytes())

    def test_name_change_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="steam-cn-test-") as tmp:
            root = Path(tmp)
            appinfo = root / "APPInfo"
            shutil.copytree(FIXTURES, appinfo)
            changes = {
                "appinfo_00.vdf": {
                    2342040: {
                        "common.name": "猛兽派对 - 豪华版",
                        "name_localized.schinese": "猛兽派对 - 豪华版",
                    }
                }
            }
            report = apply_changes(
                appinfo, changes, root / "backups", require_steam_closed=False
            )
            shard = parse_file(appinfo / "appinfo_00.vdf")
            entry = next(e for e in shard.entries if e.appid == 2342040)
            name, localized = app_names(shard, entry)
            self.assertEqual(name, "猛兽派对 - 豪华版")
            self.assertEqual(localized["schinese"], "猛兽派对 - 豪华版")
            self.assertEqual(verify(shard), [])

            restore_backup(report["backup"], appinfo)
            shard = parse_file(appinfo / "appinfo_00.vdf")
            entry = next(e for e in shard.entries if e.appid == 2342040)
            name, _ = app_names(shard, entry)
            self.assertEqual(name, "Party Animals - Deluxe Pack")


class HardeningTests(unittest.TestCase):
    def test_regex_flags_are_applied(self):
        rules = {
            "schema_version": 1,
            "execution": {"order": ["R"], "default_mode": "review"},
            "rules": {
                "R": {
                    "enabled": True,
                    "mode": "review",
                    "operations": [
                        {"pattern": "^abc$", "replacement": "ok", "flags": ["i"]}
                    ],
                    "preconditions": {},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(rules), encoding="utf-8")
            ruleset = load_rules(path)
        self.assertEqual(apply_rules("ABC", ruleset)[0], "ok")

    def test_unknown_regex_flag_is_rejected(self):
        rules = {
            "schema_version": 1,
            "execution": {"order": ["R"], "default_mode": "review"},
            "rules": {
                "R": {
                    "operations": [
                        {"pattern": "x", "replacement": "y", "flags": ["q"]}
                    ]
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(rules), encoding="utf-8")
            with self.assertRaises(Exception):
                load_rules(path)

    def test_build_bytes_rejects_missing_target(self):
        from shard import ShardError

        shard = parse_file(FIXTURES / "appinfo_17.vdf")
        with self.assertRaises(ShardError):
            build_bytes(shard, {443510: {"common.not_a_field": "x"}})

    def test_community_names_priority_over_stale_english_base(self):
        from mapping import load_community_names

        names = {
            "schema_version": 1,
            "defaults": {
                "primary_target": "common.name",
                "sync_existing_localized": [],
                "mapping_priority": ["local_schinese", "community"],
            },
            "entries": {
                "443510": {"base_name": "Guide Button Chord", "source": "none"}
            },
        }
        community = {443510: "社区译名"}
        plan = build_plan(
            FIXTURES, names, load_rules(CONFIG / "rules.json"),
            include_no_zh=False, community_names=community,
        )
        item = next(p for p in plan if p.appid == 443510)
        self.assertEqual(item.source, "community")
        self.assertEqual(item.desired_name, "社区译名")
        self.assertEqual(item.status, "candidate")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "community.json"
            path.write_text(
                json.dumps({"schemaVersion": 1, "games": {"443510": "社区译名"}}),
                encoding="utf-8",
            )
            self.assertEqual(load_community_names(path), {443510: "社区译名"})

    def test_community_csv_overrides_json(self):
        from mapping import load_community_names

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "community_names"
            base.with_suffix(".json").write_text(
                json.dumps({"schemaVersion": 1, "games": {"10": "旧名", "20": "保留"}}),
                encoding="utf-8",
            )
            base.with_suffix(".csv").write_text(
                "appid,name\n10,反恐精英\n440,军团要塞 2\n", encoding="utf-8"
            )
            loaded = load_community_names(base.with_suffix(".json"))
            self.assertEqual(loaded[10], "反恐精英")
            self.assertEqual(loaded[20], "保留")
            self.assertEqual(loaded[440], "军团要塞 2")

    def test_store_cache_priority(self):
        names = {
            "schema_version": 1,
            "defaults": {
                "primary_target": "common.name",
                "sync_existing_localized": [],
                "mapping_priority": ["local_schinese", "store_api"],
            },
            "entries": {},
        }
        plan = build_plan(
            FIXTURES, names, load_rules(CONFIG / "rules.json"),
            include_no_zh=False, store_cache={443510: "测试中文名"},
        )
        item = next(p for p in plan if p.appid == 443510)
        self.assertEqual(item.desired_name, "测试中文名")
        self.assertEqual(item.source, "store_api")

    def test_gui_rejects_foreign_host(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer

        from gui import make_handler

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            shutil.copy(CONFIG / "rules.json", config)
            shutil.copy(CONFIG / "names.json", config)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(str(FIXTURES.parent), config, config / "backups"),
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.putrequest("GET", "/api/plan", skip_host=True)
                conn.putheader("Host", "evil.example")
                conn.endheaders()
                self.assertEqual(conn.getresponse().status, 403)
            finally:
                server.shutdown()
                server.server_close()


class SafetyTests(unittest.TestCase):
    def test_apply_reuses_parsed_shards(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            appinfo = root / "APPInfo"
            shutil.copytree(FIXTURES, appinfo)
            shard = parse_file(appinfo / "appinfo_17.vdf")
            with mock.patch(
                "backup.parse_file",
                side_effect=AssertionError("should reuse parsed shard"),
            ):
                report = apply_changes(
                    appinfo,
                    {"appinfo_17.vdf": {443510: {"common.name": "指南按钮和弦"}}},
                    root / "backups",
                    require_steam_closed=False,
                    parsed_shards={"appinfo_17.vdf": shard},
                )
            self.assertEqual(report["changed"], ["appinfo_17.vdf"])

    def test_preflight_blocks_before_backup(self):
        from backup import BackupError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            appinfo = root / "APPInfo"
            shutil.copytree(FIXTURES, appinfo)
            with self.assertRaises(BackupError):
                apply_changes(
                    appinfo,
                    {"appinfo_17.vdf": {443510: {"common.missing": "x"}}},
                    root / "backups",
                    require_steam_closed=False,
                )
            self.assertFalse((root / "backups").exists())

    def test_verify_changes_detects_missing_target(self):
        from shard import verify_changes

        shard = parse_file(FIXTURES / "appinfo_17.vdf")
        problems = verify_changes(shard, {443510: {"common.missing": "x"}})
        self.assertTrue(any("missing" in p for p in problems))

    def test_control_character_rejected(self):
        from vdf import VDFError, set_node_text
        from vdf import Node, TYPE_STRING

        with self.assertRaises(VDFError):
            set_node_text(Node(TYPE_STRING, 0, b"x\x00"), "bad\nname")


class RuleTests(unittest.TestCase):
    def test_documented_examples(self):
        ruleset = load_rules(CONFIG / "rules.json")
        cases = [
            ("暖雪 Warm Snow", "暖雪"),
            ("异形丛生：虫启天降 (Alien Swarm: Reactive Drop)", "异形丛生：虫启天降"),
            ("使命召唤®：战争地带™", "使命召唤：战争地带"),
            (" 霍格沃茨之遗：黑魔法驻军帽", "霍格沃茨之遗：黑魔法驻军帽"),
            ("《守望先锋》", "守望先锋"),
            ("《暗黑破坏神 IV：憎恨之躯》", "暗黑破坏神 IV：憎恨之躯"),
            ("《暗黑破坏神 IV》：“憎恨之王” - 标准版", "暗黑破坏神 IV：“憎恨之王” - 标准版"),
            ("Hogwarts Legacy", "Hogwarts Legacy"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                actual, _ = apply_rules(source, ruleset)
                self.assertEqual(actual, expected)


class PlanTests(unittest.TestCase):
    def test_manual_override_is_approved(self):
        names = {
            "schema_version": 1,
            "defaults": {
                "primary_target": "common.name",
                "sync_existing_localized": [],
                "mapping_priority": ["local_schinese", "local_sc_schinese"],
            },
            "entries": {
                "443510": {
                    "manual_override": True,
                    "desired_name": "指南按钮和弦",
                    "targets": ["common.name"],
                }
            },
        }
        plan = build_plan(FIXTURES, names, load_rules(CONFIG / "rules.json"))
        item = next(p for p in plan if p.appid == 443510)
        self.assertTrue(item.approved)
        self.assertEqual(item.desired_name, "指南按钮和弦")
        self.assertTrue(any(p.appid == 4628710 and p.status == "no_zh" for p in plan))


class ClassificationTests(unittest.TestCase):
    def test_app_types(self):
        from mapping import classify_app

        types: dict[int, str] = {}
        for name in ("appinfo_00.vdf", "appinfo_17.vdf"):
            shard = parse_file(FIXTURES / name)
            for entry in shard.entries:
                types[entry.appid] = classify_app(shard, entry)[0]
        self.assertEqual(types.get(2752710), "game")
        self.assertEqual(types.get(4628710), "tool")
        self.assertEqual(types.get(443510), "other")


class NameSourceTests(unittest.TestCase):
    def test_chinese_common_name_is_a_source(self):
        from mapping import resolve_base

        self.assertEqual(resolve_base("秦殇", {}, ["common_name"]), ("秦殇", "common_name"))

    def test_chinese_name_is_fallback_even_without_priority_entry(self):
        from mapping import resolve_base

        self.assertEqual(
            resolve_base("秦殇", {}, ["local_schinese", "community"]),
            ("秦殇", "common_name"),
        )

    def test_english_name_stays_untranslated(self):
        from mapping import resolve_base

        self.assertEqual(resolve_base("Dead Cells", {}, ["common_name"]), ("", ""))

    def test_localized_wins_over_common_name(self):
        from mapping import resolve_base

        self.assertEqual(
            resolve_base("秦殇", {"schinese": "秦殇：复刻"}, ["local_schinese", "common_name"]),
            ("秦殇：复刻", "local_schinese"),
        )


class ConfigTests(unittest.TestCase):
    def test_pyproject_version_matches_source(self):
        """pyproject 与 version.py 必须同版本：发版时最容易只改一处。"""
        import tomllib

        import version

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            data["project"]["version"],
            version.__version__,
            "pyproject.toml 与 src/version.py 版本号不一致",
        )

    def test_py_modules_cover_every_src_file(self):
        """py-modules 漏项会让 pip install 后 ImportError（曾漏掉 db/pinyin）。"""
        import tomllib

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        listed = set(data["tool"]["setuptools"]["py-modules"])
        actual = {p.stem for p in (ROOT / "src").glob("*.py")}
        self.assertEqual(actual - listed, set(), "src 下有模块未列入 py-modules")

    def test_plan_from_config_matches_build_plan(self):
        from mapping import plan_from_config, build_plan, load_names

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            shutil.copy(CONFIG / "rules.json", config)
            shutil.copy(CONFIG / "names.json", config)
            via_config = plan_from_config(FIXTURES, config, include_no_zh=True)
            direct = build_plan(
                FIXTURES, load_names(config / "names.json"),
                load_rules(config / "rules.json"),
                include_no_zh=True, store_cache={}, community_names={},
            )
        self.assertEqual(len(via_config), len(direct))
        self.assertEqual(
            [p.appid for p in via_config], [p.appid for p in direct]
        )

    def test_default_paths_come_from_paths_module(self):
        import paths

        # 源码运行时用项目内 config/；clone 下来还没有该目录时回退到用户目录，
        # 两种都合法，所以不能把名字写死成 "config"。
        self.assertIn(paths.DEFAULT_CONFIG.name, ("config", "steam-cn-renamer"))
        self.assertTrue(paths.DEFAULT_BACKUPS.name.endswith("backups"))
        self.assertIsInstance(paths.ensure_config(paths.DEFAULT_CONFIG), Path)


class CommunityCsvTests(unittest.TestCase):
    def test_utf16_csv_is_readable(self):
        from mapping import load_community_names

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "community_names.json"
            json_path.write_text(json.dumps({"schemaVersion": 1, "games": {}}), encoding="utf-8")
            (Path(tmp) / "community_names.csv").write_bytes(
                "appid,name\n970500,秦殇\n".encode("utf-16")
            )
            self.assertEqual(load_community_names(json_path)[970500], "秦殇")

    def test_broken_bytes_do_not_drop_whole_table(self):
        from mapping import load_community_names

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "community_names.json"
            json_path.write_text(json.dumps({"schemaVersion": 1, "games": {}}), encoding="utf-8")
            # 非法 UTF-8/GBK 字节：旧实现会整表丢弃，新实现应降级解析
            (Path(tmp) / "community_names.csv").write_bytes(
                b"appid,name\n440,\xff\xfe\x00bad\n588650,Dead Cells\n"
            )
            loaded = load_community_names(json_path)
            self.assertIn(588650, loaded)


class CliTests(unittest.TestCase):
    def test_scan_and_verify(self):
        for command in ("scan", "verify"):
            with self.subTest(command=command):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    code = main([
                        "--steam-dir", str(FIXTURES.parent),
                        "--config-dir", str(CONFIG),
                        command,
                    ])
                self.assertEqual(code, 0)
                self.assertIn("443510" if command == "scan" else "问题数: 0", stream.getvalue())

    def test_prune_keeps_newest(self):
        from backup import list_backups, prune_backups

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "backups"
            for name in ("20260101-000001", "20260102-000002", "20260103-000003"):
                folder = root / name
                folder.mkdir(parents=True)
                (folder / "appinfo_00.vdf").write_bytes(b"x")
                (folder / "manifest.json").write_text(
                    json.dumps({"created_at": name, "files": {"appinfo_00.vdf": "x"}}),
                    encoding="utf-8",
                )
            self.assertEqual(len(list_backups(root)), 3)
            removed = prune_backups(root, keep=2)
            self.assertEqual(removed, ["20260101-000001"])
            self.assertEqual(len(list_backups(root)), 2)


class RobustnessTests(unittest.TestCase):
    def test_scan_shards_reports_filename(self):
        from mapping import scan_shards
        from shard import ShardError

        with tempfile.TemporaryDirectory() as tmp:
            appinfo = Path(tmp) / "APPInfo"
            shutil.copytree(FIXTURES, appinfo)
            (appinfo / "appinfo_01.vdf").write_bytes(b"garbage")
            with self.assertRaises(ShardError) as ctx:
                scan_shards(appinfo)
            self.assertIn("appinfo_01.vdf", str(ctx.exception))

    def test_unsupported_targets_are_excluded(self):
        from mapping import PlanEntry, approved_changes

        plan = [
            PlanEntry(appid=1, shard="a.vdf", original_name="", desired_name="名字",
                      targets=[], status="unsupported", approved=True),
            PlanEntry(appid=2, shard="a.vdf", original_name="Old", desired_name="新名",
                      targets=["common.name"], status="approved", approved=True),
        ]
        changes = approved_changes(plan)
        self.assertEqual(changes, {"a.vdf": {2: {"common.name": "新名"}}})

    def test_missing_primary_target_marks_unsupported(self):
        # appinfo_17 的 443510 没有 name_localized，构造一个不存在的 target 观察过滤
        from mapping import _existing_targets

        shard = parse_file(FIXTURES / "appinfo_17.vdf")
        entry = next(e for e in shard.entries if e.appid == 443510)
        existing, primary_ok = _existing_targets(
            shard, entry, ["common.name", "name_localized.japanese"]
        )
        self.assertTrue(primary_ok)
        self.assertEqual(existing, ["common.name"])

        existing, primary_ok = _existing_targets(shard, entry, ["common.missing_field"])
        self.assertFalse(primary_ok)
        self.assertEqual(existing, [])

    def test_unknown_mapping_priority_is_rejected(self):
        names = {
            "schema_version": 1,
            "defaults": {"mapping_priority": ["local_schinese", "steam_store"]},
            "entries": {},
        }
        with self.assertRaises(ValueError):
            build_plan(FIXTURES, names, load_rules(CONFIG / "rules.json"))

    def test_auto_rule_mode_is_rejected(self):
        from rules import RulesError

        rules = {
            "schema_version": 1,
            "execution": {"order": ["R"], "default_mode": "review"},
            "rules": {"R": {"mode": "auto",
                            "operations": [{"pattern": "a", "replacement": "b"}]}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(rules), encoding="utf-8")
            with self.assertRaises(RulesError):
                load_rules(path)

    def test_ensure_config_creates_templates(self):
        from cli import ensure_config

        with tempfile.TemporaryDirectory() as tmp:
            folder = ensure_config(tmp)
            self.assertTrue((folder / "names.json").exists())
            self.assertTrue((folder / "rules.json").exists())
            self.assertEqual(json.loads((folder / "names.json").read_text(encoding="utf-8"))["schema_version"], 1)

    def test_update_community_offline_raises_value_error(self):
        from mapping import update_community_names

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                update_community_names(Path(tmp) / "c.json", url="http://127.0.0.1:1/none")


class GuiApiTests(unittest.TestCase):
    """End-to-end HTTP checks against a temp Steam copy."""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        import backup as backup_mod
        import gui as gui

        self._tmp = tempfile.TemporaryDirectory(prefix="steam-gui-test-")
        root = Path(self._tmp.name)
        self.appinfo = root / "steam" / "APPCache" / "APPInfo"
        self.appinfo.parent.mkdir(parents=True)
        shutil.copytree(FIXTURES, self.appinfo)
        self.config = root / "config"
        self.config.mkdir()
        shutil.copy(CONFIG / "rules.json", self.config)
        names = {
            "schema_version": 1,
            "defaults": {
                "primary_target": "common.name",
                "sync_existing_localized": [],
                "mapping_priority": ["local_schinese", "community", "store_api"],
            },
            "entries": {
                "443510": {
                    "appid": 443510, "shard": "appinfo_17.vdf",
                    "manual_override": True, "approved": True,
                    "desired_name": "指南按钮和弦", "targets": ["common.name"],
                    "status": "approved",
                }
            },
        }
        (self.config / "names.json").write_text(
            json.dumps(names, ensure_ascii=False), encoding="utf-8"
        )
        self.backups = root / "backups"

        self._orig_backup_steam = backup_mod.steam_running
        self._orig_gui_steam = gui.steam_running
        backup_mod.steam_running = lambda *a, **k: False
        gui.steam_running = lambda *a, **k: False

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), gui.make_handler(str(root / "steam"), self.config, self.backups)
        )
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        import backup as backup_mod
        import gui as gui

        backup_mod.steam_running = self._orig_backup_steam
        gui.steam_running = self._orig_gui_steam
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _request(self, method, path, body=None, content_type="application/json"):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            headers["Content-Type"] = content_type
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def test_save_then_apply_then_restore(self):
        import json as _json

        status, body = self._request("GET", "/api/plan")
        self.assertEqual(status, 200)
        plan = _json.loads(body)
        item = next(p for p in plan if p["appid"] == 443510)
        self.assertTrue(item["approved"])
        self.assertFalse(item["applied"])

        status, body = self._request("POST", "/api/save",
                                     _json.dumps({"entries": [item]}).encode())
        self.assertEqual(status, 200)

        status, body = self._request("POST", "/api/apply", b"")
        self.assertEqual(status, 200, body)
        payload = _json.loads(body)
        self.assertIn("appinfo_17.vdf", payload["changed"])
        self.assertGreater(payload["backup_size"], 0)

        shard = parse_file(self.appinfo / "appinfo_17.vdf")
        entry = next(e for e in shard.entries if e.appid == 443510)
        self.assertEqual(app_names(shard, entry)[0], "指南按钮和弦")

        # 写入后应标记为已生效
        status, body = self._request("GET", "/api/plan")
        item = next(p for p in json.loads(body) if p["appid"] == 443510)
        self.assertTrue(item["applied"])

        status, body = self._request("GET", "/api/backups")
        self.assertEqual(status, 200)
        backups = json.loads(body)["backups"]
        self.assertEqual(len(backups), 1)

        status, body = self._request(
            "POST", "/api/restore", json.dumps({"backup": backups[0]["name"]}).encode()
        )
        self.assertEqual(status, 200, body)
        shard = parse_file(self.appinfo / "appinfo_17.vdf")
        entry = next(e for e in shard.entries if e.appid == 443510)
        self.assertEqual(app_names(shard, entry)[0], "Guide Button Chord")

    def test_save_rejects_illegal_target(self):
        import json as _json

        body = _json.dumps({"entries": [{
            "appid": 443510, "approved": True,
            "desired_name": "指南按钮和弦", "targets": ["common.icon"],
        }]}).encode()
        status, payload = self._request("POST", "/api/save", body)
        self.assertEqual(status, 400)
        self.assertIn("targets 非法", _json.loads(payload)["error"])

    def test_plan_cache_is_invalidated_by_save(self):
        import json as _json

        status, body = self._request("GET", "/api/plan")
        before = next(p for p in _json.loads(body) if p["appid"] == 443510)
        self.assertTrue(before["approved"])

        status, _ = self._request("POST", "/api/save", _json.dumps({"entries": [{
            "appid": 443510, "approved": False, "manual_override": True,
            "desired_name": "指南按钮和弦", "targets": ["common.name"],
        }]}).encode())
        self.assertEqual(status, 200)

        status, body = self._request("GET", "/api/plan")
        after = next(p for p in _json.loads(body) if p["appid"] == 443510)
        self.assertFalse(after["approved"], "保存后台账变化，计划缓存必须失效")

    def test_bad_requests_return_json_errors(self):
        status, body = self._request("POST", "/api/save", b"{bad")
        self.assertEqual(status, 400)
        self.assertIn("JSON", json.loads(body)["error"])

        status, body = self._request("POST", "/api/save", json.dumps({"entries": "x"}).encode())
        self.assertEqual(status, 400)

        status, body = self._request("POST", "/api/restore", json.dumps({"backup": "../x"}).encode())
        self.assertEqual(status, 400)

        status, _ = self._request("GET", "/api/nope")
        self.assertEqual(status, 404)

    def test_non_numeric_content_length_is_rejected(self):
        import socket

        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(
                b"POST /api/save HTTP/1.1\r\n"
                b"Host: 127.0.0.1:" + str(self.port).encode() + b"\r\n"
                b"Content-Length: abc\r\n"
                b"Content-Type: application/json\r\n\r\n"
            )
            data = sock.recv(4096)
        self.assertIn(b"400", data.split(b"\r\n")[0])

    def test_shutdown_stops_server(self):
        status, body = self._request("POST", "/api/shutdown", b"")
        self.assertEqual(status, 200)
        self._thread.join(timeout=5)
        self.assertFalse(self._thread.is_alive(), "server did not stop after /api/shutdown")


class SharedNamesTests(unittest.TestCase):
    """共享词库：编码兼容、行级校验、两层覆盖、新用户自动读取。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="shared-names-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def _write(self, name: str, text: str, encoding: str = "utf-8-sig") -> Path:
        path = self.folder / name
        path.write_text(text, encoding=encoding)
        return path

    def test_reads_bom_utf16_and_gbk(self):
        from game_names import load_shared_names

        header = "GameID,OriginalName,ModifiedName\n"
        for encoding in ("utf-8-sig", "utf-16", "gbk"):
            with self.subTest(encoding=encoding):
                path = self._write(
                    "g.csv", header + "1,Hello,你好\n", encoding
                )
                shared = load_shared_names(path)
                self.assertEqual(shared.names, {1: "你好"})
                self.assertEqual(shared.warnings, [])

    def test_duplicate_disabled_and_invalid_rows(self):
        from game_names import load_shared_names

        path = self._write("g.csv", (
            "GameID,OriginalName,ModifiedName,Enabled\n"
            "1,A,甲,true\n"
            "1,B,乙,true\n"      # 重复，后者覆盖
            "2,C,丙,false\n"     # 禁用
            "x,D,丁,true\n"      # 非法 ID
            "3,E,,true\n"        # 目标名为空
        ))
        shared = load_shared_names(path)
        self.assertEqual(shared.names, {1: "乙"})
        self.assertEqual(shared.duplicates, 1)
        self.assertEqual(shared.skipped, 2)
        self.assertTrue(any("重复" in w for w in shared.warnings))

    def test_local_override_wins_without_touching_baseline(self):
        from game_names import load_shared_names

        base = self._write("base.csv", "GameID,OriginalName,ModifiedName\n1,A,甲\n2,B,乙\n")
        local = self._write("local.csv", "GameID,OriginalName,ModifiedName\n1,A,壹\n")
        shared = load_shared_names(base, local)
        self.assertEqual(shared.names, {1: "壹", 2: "乙"})

    def test_names_match_ignores_whitespace(self):
        from game_names import names_match

        self.assertTrue(names_match("911 接线员", "911接线员"))
        self.assertFalse(names_match("半衰期", "半衰期 2"))

    def test_new_user_gets_shared_source(self):
        from game_names import load_shared_names
        from mapping import build_plan
        from paths import asset_dir
        from rules import load_rules

        names = json.loads(
            (ROOT / "src" / "defaults" / "names.json").read_text(encoding="utf-8")
        )
        shared = load_shared_names(asset_dir("defaults") / "game_names.csv")
        plan = build_plan(
            FIXTURES, names, load_rules(FIXTURE_CONFIG / "rules.json"), shared=shared
        )
        row = next(p for p in plan if p.appid == 2342040)
        self.assertEqual(row.source, "shared_csv")
        self.assertEqual(row.desired_name, "猛兽派对")

    def test_shared_source_can_be_disabled(self):
        from game_names import load_shared_names
        from mapping import build_plan
        from paths import asset_dir
        from rules import load_rules

        names = json.loads(
            (ROOT / "src" / "defaults" / "names.json").read_text(encoding="utf-8")
        )
        names["defaults"]["use_shared_names"] = False
        shared = load_shared_names(asset_dir("defaults") / "game_names.csv")
        plan = build_plan(
            FIXTURES, names, load_rules(FIXTURE_CONFIG / "rules.json"), shared=shared
        )
        self.assertFalse(any(p.source == "shared_csv" for p in plan))

    def test_cli_exports_approved_rows(self):
        from cli import main

        dest = self.folder / "export.csv"
        self.assertEqual(
            main(["--config-dir", str(FIXTURE_CONFIG), "export-mappings",
                  "-o", str(dest)]),
            0,
        )
        with dest.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ModifiedName"], "指南按钮和弦")


class SpacingWarningTests(unittest.TestCase):
    """汉字数字间距提示：只警告，不改字、不拦截写入、不改变计划口径。"""

    def test_detects_adjacent_cjk_and_ascii_digit(self):
        from vdf import spacing_warning

        cases = {
            "合金弹头3": True,
            "拳皇97": True,
            "3代目": True,
            "合金弹头 3": False,
            "007": False,
            "合金弹头": False,
            "Metal Gear 4": False,
            "合金弹头Ⅲ": False,   # 罗马数字不是 ASCII 数字
            "": False,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(bool(spacing_warning(name)), expected)

    def test_warning_never_mutates_input(self):
        from vdf import spacing_warning

        name = "合金弹头3"
        warning = spacing_warning(name)
        self.assertIsNotNone(warning)
        self.assertEqual(name, "合金弹头3")
        self.assertIn("半角空格", warning)
        self.assertIn("不自动修改", warning)

    def test_plan_attaches_warning_without_changing_desired_name(self):
        from mapping import plan_from_config

        plan = plan_from_config(FIXTURES, FIXTURE_CONFIG)
        flagged = next(p for p in plan if p.appid == 651430)
        # 台账原文是紧邻写法；警告不得把它改成「铁拳 7 …」
        self.assertEqual(flagged.desired_name, "铁拳7 - 太鼓达人包")
        self.assertEqual(len(flagged.warnings), 1)
        self.assertEqual(flagged.to_dict()["warnings"], flagged.warnings)
        clean = next(p for p in plan if p.appid == 443510)
        self.assertEqual(clean.warnings, [])

    def test_warning_is_additive_to_snapshot(self):
        row = {"appid": 1, "status": "approved", "desired_name": "合金弹头3"}
        flagged = dict(row, warnings=["提示"])
        self.assertEqual(snapshot_from_rows([row]), snapshot_from_rows([flagged]))

    def test_frontend_rule_matches_backend(self):
        from vdf import _CJK_DIGIT_ADJACENT

        page = (ROOT / "src" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn(
            _CJK_DIGIT_ADJACENT.pattern, page,
            "前端正则与后端检测规则不一致，警告会误报或漏报",
        )


def snapshot_from_rows(rows: list[dict]) -> dict:
    """Compact fingerprint of a plan: row count, status mix and content digest."""
    lines = []
    for row in rows:
        lines.append("|".join((
            str(row.get("appid")),
            row.get("shard") or "",
            str(row.get("status") or ""),
            "A" if row.get("approved") else "-",
            "E" if row.get("applied") else "-",
            row.get("source") or "",
            row.get("original_name") or "",
            row.get("desired_name") or "",
            ",".join(row.get("targets") or []),
            ",".join(row.get("applied_rules") or []),
        )))
    return {
        "total": len(rows),
        "status": dict(sorted(Counter(str(r.get("status")) for r in rows).items())),
        "approved": sum(1 for r in rows if r.get("approved")),
        "applied": sum(1 for r in rows if r.get("applied")),
        "digest": "sha256:" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(),
    }


def read_snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def write_snapshot(data: dict) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


HINT = (
    "计划输出与快照不一致。若为有意改动（新增来源/状态、规则调整），"
    "执行 UPDATE_SNAPSHOTS=1 python -m pytest tests -q 重新生成 "
    "tests/snapshots/plan_snapshot.json 并 review diff。"
)


class PlanSnapshotTests(unittest.TestCase):
    """锁住"行数 + 状态分布 + 内容"，防止再次出现静默丢行（如 803 → 253）。"""

    def test_plan_matches_snapshot(self):
        from mapping import plan_from_config

        rows = [p.to_dict() for p in plan_from_config(FIXTURES, FIXTURE_CONFIG)]
        actual = snapshot_from_rows(rows)
        if os.environ.get("UPDATE_SNAPSHOTS") == "1":
            write_snapshot(actual)
            return
        if not SNAPSHOT_PATH.exists():
            write_snapshot(actual)
            self.skipTest(f"首次运行已生成快照 {SNAPSHOT_PATH.name}，请复查后再提交")
        self.assertEqual(read_snapshot(), actual, HINT)
        # 显式断言一遍关键口径，让失败信息更直白
        self.assertEqual(actual["total"], 5)
        self.assertEqual(actual["status"], {"approved": 2, "candidate": 1, "no_zh": 2})


class PlanApiSnapshotTests(unittest.TestCase):
    """HTTP 层的 /api/plan 必须与函数级计划同口径（默认值漂移会在这里暴露）。"""

    def test_api_plan_matches_snapshot(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer

        from gui import make_handler

        with tempfile.TemporaryDirectory(prefix="steam-api-snap-") as tmp:
            root = Path(tmp)
            steam = root / "steam"
            (steam / "APPCache").mkdir(parents=True)
            shutil.copytree(FIXTURES, steam / "APPCache" / "APPInfo")
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(str(steam), FIXTURE_CONFIG, root / "backups"),
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=30
                )
                conn.request("GET", "/api/plan", headers={
                    "Host": f"127.0.0.1:{server.server_port}",
                })
                response = conn.getresponse()
                body = response.read()
                conn.close()
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual(response.status, 200)
            rows = json.loads(body)
            self.assertEqual(
                snapshot_from_rows(rows), read_snapshot(),
                "HTTP /api/plan 与快照不一致：检查 plan_from_config 的 include_no_zh 等默认值",
            )


class ApplyRoundTripTests(unittest.TestCase):
    """写入链路：plan → approved_changes → apply → 再 plan → applied 必须为真。

    反向用 restore 证明 `applied` 是"文件现状"推导出来的，不是内存/台账缓存。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="steam-apply-rt-")
        root = Path(self._tmp.name)
        steam = root / "steam"
        (steam / "APPCache").mkdir(parents=True)
        self.appinfo = steam / "APPCache" / "APPInfo"
        shutil.copytree(FIXTURES, self.appinfo)

        self.config = root / "config"
        shutil.copytree(FIXTURE_CONFIG, self.config)
        names = json.loads((self.config / "names.json").read_text(encoding="utf-8"))
        # 额外覆盖"同步 name_localized 三处"的分支
        names["entries"]["2342040"] = {
            "appid": 2342040, "shard": "appinfo_00.vdf",
            "base_name": "猛兽派对 - 豪华版升级包",
            "desired_name": "猛兽派对 - 豪华版升级包",
            "targets": [
                "common.name",
                "name_localized.schinese",
                "name_localized.sc_schinese",
            ],
            "source": "user_override", "status": "approved",
            "approved": True, "manual_override": True,
        }
        (self.config / "names.json").write_text(
            json.dumps(names, ensure_ascii=False), encoding="utf-8"
        )
        self.backups = root / "backups"

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self):
        from mapping import plan_from_config

        return plan_from_config(self.appinfo, self.config)

    def test_apply_then_replan_marks_applied(self):
        from mapping import approved_changes

        plan = self._plan()
        changes = approved_changes(plan)
        self.assertEqual(sorted(changes), ["appinfo_00.vdf", "appinfo_17.vdf"])
        self.assertEqual(
            sorted(changes["appinfo_00.vdf"][2342040]),
            ["common.name", "name_localized.sc_schinese", "name_localized.schinese"],
        )
        # 写之前：已批准但都没有生效
        self.assertTrue(any(p.approved for p in plan))
        self.assertFalse(any(p.applied for p in plan))

        report = apply_changes(
            self.appinfo, changes, self.backups, require_steam_closed=False
        )
        self.assertEqual(sorted(report["changed"]), ["appinfo_00.vdf", "appinfo_17.vdf"])
        self.assertGreater(report["backup_size"], 0)
        for name in report["changed"]:
            self.assertEqual(verify(parse_file(self.appinfo / name)), [])

        # 写之后：所有已批准条目都必须 marked applied，且状态不变
        replanned = self._plan()
        approved_ids = {p.appid for p in plan if p.approved}
        applied_ids = {p.appid for p in replanned if p.applied}
        self.assertEqual(applied_ids, approved_ids)
        for item in replanned:
            if item.appid in approved_ids:
                self.assertTrue(item.applied, f"{item.appid} 写入后仍未标记已生效")
                self.assertEqual(item.status, "approved")
            else:
                self.assertFalse(item.applied)

        # 落盘内容：三处字段都要是新名字
        shard = parse_file(self.appinfo / "appinfo_00.vdf")
        entry = next(e for e in shard.entries if e.appid == 2342040)
        name, localized = app_names(shard, entry)
        self.assertEqual(name, "猛兽派对 - 豪华版升级包")
        self.assertEqual(localized.get("schinese"), "猛兽派对 - 豪华版升级包")
        self.assertEqual(localized.get("sc_schinese"), "猛兽派对 - 豪华版升级包")

    def test_in_file_survives_unapproval(self):
        from mapping import approved_changes as group

        apply_changes(
            self.appinfo, group(self._plan()), self.backups, require_steam_closed=False
        )
        names = json.loads((self.config / "names.json").read_text(encoding="utf-8"))
        names["entries"]["443510"]["approved"] = False   # 只取消批准，不改名字
        (self.config / "names.json").write_text(
            json.dumps(names, ensure_ascii=False), encoding="utf-8"
        )
        item = next(p for p in self._plan() if p.appid == 443510)
        self.assertFalse(item.approved)
        self.assertFalse(item.applied)    # applied 仍与批准绑定（避免"无需改动"的行误亮）
        self.assertTrue(item.in_file)     # 文件里确实已经是这个名字

    def test_restore_clears_applied_flag(self):
        from mapping import approved_changes

        plan = self._plan()
        report = apply_changes(
            self.appinfo, approved_changes(plan), self.backups,
            require_steam_closed=False,
        )
        self.assertTrue(all(p.applied for p in self._plan() if p.approved))

        restore_backup(report["backup"], self.appinfo)
        after = self._plan()
        self.assertFalse(any(p.applied for p in after), "回滚后 applied 仍为真，说明判断未落到文件")

        shard = parse_file(self.appinfo / "appinfo_00.vdf")
        entry = next(e for e in shard.entries if e.appid == 2342040)
        self.assertEqual(app_names(shard, entry)[0], "Party Animals - Deluxe Pack")


class VdfCodecTests(unittest.TestCase):
    def test_widestring_at_odd_offset(self):
        """UTF-16 对齐是相对字符串起点，不是文件绝对偏移：奇数偏移必须能解析。"""
        from vdf import (
            TYPE_END, TYPE_NONE, TYPE_STRING, TYPE_WIDESTRING, BinaryVDF,
        )

        codec = BinaryVDF(["a", "b", "c"])
        blob = bytearray()
        blob += bytes([TYPE_NONE]) + (0).to_bytes(4, "little")
        blob += bytes([TYPE_STRING]) + (1).to_bytes(4, "little") + b"xy\x00"
        # 上一个值是奇数长度，使下一个节点（含 widestring）落在奇数偏移
        self.assertEqual(len(blob) % 2, 1, "构造未落在奇数偏移")
        blob += bytes([TYPE_WIDESTRING]) + (2).to_bytes(4, "little")
        blob += "中".encode("utf-16-le") + b"\x00\x00"
        blob += bytes([TYPE_END])

        root = codec.parse(bytes(blob))
        self.assertEqual(root.children[1].type, TYPE_WIDESTRING)
        self.assertEqual(root.children[1].raw, "中".encode("utf-16-le") + b"\x00\x00")
        self.assertEqual(codec.serialize(root), bytes(blob))


class PathGuardTests(unittest.TestCase):
    def test_missing_appinfo_dir_raises(self):
        from mapping import plan_from_config

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError) as ctx:
                plan_from_config(Path(tmp) / "nope", FIXTURE_CONFIG)
            self.assertIn("appinfo 目录不存在", str(ctx.exception))

    def test_cli_verify_survives_broken_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            appinfo = Path(tmp) / "APPCache" / "APPInfo"
            appinfo.parent.mkdir(parents=True)
            shutil.copytree(FIXTURES, appinfo)
            (appinfo / "appinfo_99.vdf").write_bytes(b"garbage")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = main([
                    "--steam-dir", str(Path(tmp)), "--config-dir", str(CONFIG), "verify",
                ])
            output = stream.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("appinfo_99.vdf", output)
        self.assertIn("解析失败", output)
        self.assertIn("问题数: 1", output)   # 坏分片计数，其余分片继续校验


class BackupManifestTests(unittest.TestCase):
    def _make_backup(self, root: Path, name: str, created_at: str, mtime: int):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "appinfo_00.vdf").write_bytes(b"x")
        (folder / "manifest.json").write_text(
            json.dumps({"manifest_version": 1, "created_at": created_at,
                        "files": {"appinfo_00.vdf": "x"}}),
            encoding="utf-8",
        )
        os.utime(folder, (mtime, mtime))
        return folder

    def test_sort_is_time_based_not_lexicographic(self):
        from backup import list_backups

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 同一秒内的 -2 / -10 后缀，字典序会排错；这里按 mtime 排序
            self._make_backup(root, "20260101-000000-2", "2026-01-01T00:00:00+0800", 1000)
            self._make_backup(root, "20260101-000000-10", "2026-01-01T00:00:00+0800", 2000)
            names = [item["name"] for item in list_backups(root)]
        self.assertEqual(names[0], "20260101-000000-10")

    def test_unknown_manifest_version_is_rejected(self):
        from backup import BackupError, verify_backup

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "20260101-000000"
            folder.mkdir()
            (folder / "manifest.json").write_text(
                json.dumps({"manifest_version": 2, "files": {}}), encoding="utf-8"
            )
            with self.assertRaises(BackupError) as ctx:
                verify_backup(folder)
            self.assertIn("版本 2", str(ctx.exception))


class InputValidationTests(unittest.TestCase):
    def _names(self, tmp: Path, entry: dict) -> Path:
        path = tmp / "names.json"
        path.write_text(
            json.dumps({"schema_version": 1, "entries": {"443510": entry}},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def test_illegal_target_is_rejected(self):
        from mapping import load_names

        with tempfile.TemporaryDirectory() as tmp:
            path = self._names(Path(tmp), {
                "appid": 443510, "approved": True, "desired_name": "指南按钮和弦",
                "targets": ["common.icon"],
            })
            with self.assertRaises(ValueError) as ctx:
                load_names(path)
            self.assertIn("非法写入目标", str(ctx.exception))

    def test_string_boolean_is_normalized(self):
        from mapping import load_names

        with tempfile.TemporaryDirectory() as tmp:
            path = self._names(Path(tmp), {
                "appid": 443510, "manual_override": True, "approved": "false",
                "desired_name": "指南按钮和弦", "targets": ["common.name"],
            })
            data = load_names(path)
        self.assertIs(data["entries"]["443510"]["approved"], False)

    def test_unknown_entry_fields_are_kept(self):
        from mapping import load_names

        with tempfile.TemporaryDirectory() as tmp:
            path = self._names(Path(tmp), {
                "appid": 443510, "approved": True, "desired_name": "x",
                "targets": ["common.name"], "my_field": "keep me",
            })
            data = load_names(path)
        self.assertEqual(data["entries"]["443510"]["my_field"], "keep me")

    def test_deep_nesting_raises_shard_error(self):
        from shard import ShardError, parse_bytes
        import vdf as V

        blob = bytearray()
        for _ in range(200):
            blob += bytes([V.TYPE_NONE]) + (0).to_bytes(4, "little")
        blob += bytes([V.TYPE_END]) * 200
        with self.assertRaises(ShardError):
            parse_bytes(bytes(blob))


class BackupNameTests(unittest.TestCase):
    def test_manifest_traversal_is_rejected(self):
        from backup import BackupError, verify_backup

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "20260101-000000"
            folder.mkdir()
            (folder / "manifest.json").write_text(
                json.dumps({"manifest_version": 1, "created_at": "x",
                            "files": {"../../evil.vdf": "abc"}}),
                encoding="utf-8",
            )
            with self.assertRaises(BackupError) as ctx:
                verify_backup(folder)
            self.assertIn("非法文件名", str(ctx.exception))


class RulesOrderTests(unittest.TestCase):
    def _write(self, tmp: Path, order: list, rules: dict) -> Path:
        path = tmp / "rules.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "execution": {"order": order, "default_mode": "review"},
            "rules": rules,
        }), encoding="utf-8")
        return path

    def test_order_with_missing_rule_is_rejected(self):
        from rules import RulesError, load_rules

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), ["A", "MISSING"], {
                "A": {"operations": [{"pattern": "a", "replacement": "b"}]},
            })
            with self.assertRaises(RulesError) as ctx:
                load_rules(path)
            self.assertIn("MISSING", str(ctx.exception))

    def test_unused_rule_warns(self):
        from rules import load_rules

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), ["A"], {
                "A": {"operations": [{"pattern": "a", "replacement": "b"}]},
                "B": {"operations": [{"pattern": "c", "replacement": "d"}]},
            })
            with self.assertWarns(UserWarning):
                load_rules(path)


class SteamOriginalNameTests(unittest.TestCase):
    """台账留存的最初英文名只用于展示，不改变写入/审批口径。"""

    APPID = 651430
    EN_NAME = "TEKKEN 7 - TAIKO NO TATSUJIN Pack"
    ZH_NAME = "铁拳7 - 太鼓达人包"

    def _build_renamed_shard(self, tmp: Path) -> Path:
        """复制 fixture 后把 Steam 文件里的名字改成中文，模拟已写入状态。"""
        import shutil
        import shard as shard_mod

        src = FIXTURES / "appinfo_00.vdf"
        target = tmp / "appinfo_00.vdf"
        shutil.copy2(src, target)
        parsed = shard_mod.parse_file(target)
        target.write_bytes(shard_mod.build_bytes(
            parsed, {self.APPID: {"common.name": self.ZH_NAME}}
        ))
        return tmp

    @staticmethod
    def _names_fixture(with_record: bool = True) -> dict:
        return {
            "schema_version": 1,
            "defaults": {"mapping_priority": ["local_schinese", "community", "store_api"]},
            "entries": {
                "651430": {
                    "shard": "appinfo_00.vdf",
                    "original_name": SteamOriginalNameTests.EN_NAME if with_record else "",
                    "desired_name": SteamOriginalNameTests.ZH_NAME,
                    "source": "community",
                    "approved": True,
                    "targets": ["common.name"],
                }
            },
        }

    def test_original_name_preserved_after_steam_rewrite(self):
        import mapping

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            names = self._names_fixture()
            config = tmp / "config"
            config.mkdir()
            shutil.copy2(FIXTURES.parent / "config" / "rules.json", config / "rules.json")
            (config / "names.json").write_text(
                json.dumps(names, ensure_ascii=False), encoding="utf-8"
            )
            plan = mapping.plan_from_config(self._build_renamed_shard(tmp), config)
            row = next(x for x in plan if x.appid == self.APPID)
            # Steam 文件里已变成中文名，但展示字段保留最初英文名
            self.assertEqual(row.original_name, self.ZH_NAME)
            self.assertEqual(row.steam_original_name, self.EN_NAME)
            self.assertEqual(row.to_dict()["steam_original_name"], self.EN_NAME)
            # 业务口径不变：目标名仍是台账里批准的那个
            self.assertEqual(row.desired_name, self.ZH_NAME)
            self.assertTrue(row.approved)

    def test_original_name_empty_without_csv_or_ledger_record(self):
        import mapping

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = tmp / "config"
            config.mkdir()
            shutil.copy2(FIXTURES.parent / "config" / "rules.json", config / "rules.json")
            (config / "names.json").write_text(
                json.dumps(self._names_fixture(with_record=False), ensure_ascii=False),
                encoding="utf-8",
            )
            # 不传 original_names 且台账无记录时，字段留空，前端回退到实时名
            plan = mapping.build_plan(
                self._build_renamed_shard(tmp),
                mapping.load_names(config / "names.json"),
                mapping.load_rules(config / "rules.json"),
            )
            row = next(x for x in plan if x.appid == self.APPID)
            self.assertEqual(row.original_name, self.ZH_NAME)
            self.assertEqual(row.steam_original_name, "")

    def test_bundled_csv_is_loaded_and_wins_over_ledger(self):
        import game_names
        import mapping

        table = game_names.load_original_names(
            mapping.asset_dir("defaults") / "original_names.csv"
        )
        self.assertGreater(len(table), 500)
        self.assertEqual(table.get(588650), "Dead Cells")
        self.assertEqual(table.get(990080), "Hogwarts Legacy")
        # 即使台账的 original_name 已被改写成中文，展示字段仍取 CSV 里的英文名
        self.assertEqual(table.get(651430), self.EN_NAME)


class LauncherTests(unittest.TestCase):
    def test_library_folder_is_not_a_steam_root(self):
        import steam_locator

        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "SteamLibrary"
            (lib / "steamapps").mkdir(parents=True)
            self.assertFalse(steam_locator.is_steam_dir(lib))
            self.assertTrue(steam_locator.looks_like_library(lib))


class PinyinSortasTests(unittest.TestCase):
    def test_common_initials(self):
        import pinyin

        cases = {
            "猛兽派对": "MSPD",
            "合金弹头3": "HJDT3",
            "暖雪": "NX",
            "艾尔登法环": "AEDFH",
            "Hades": "HADES",
            "只狼：影逝二度": "ZLYSED",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(pinyin.to_initials(name), expected)

    def test_level2_hanzi_kept_not_guessed(self):
        import pinyin

        # 殇 是 GB2312 二级字库（按部首排），不能按拼音猜成 Z
        self.assertEqual(pinyin.to_initials("秦殇"), "Q殇")
        self.assertEqual(pinyin.sortas_for("猛兽派对"), "MSPD")


class NewKeyWriteTests(unittest.TestCase):
    def test_add_sortas_to_shard_is_byte_exact(self):
        from shard import (
            build_bytes_with_new_keys, check_targets, parse_bytes, verify,
            verify_changes,
        )

        shard = parse_file(FIXTURES / "appinfo_00.vdf")
        entry = next(
            e for e in shard.entries
            if any(c.is_object() and shard.codec.key(c) == "common" for c in e.root.children)
        )
        changes = {entry.appid: {"common.sortas": "MSPD"}}
        # 默认严格预检会报缺失
        self.assertTrue(any("sortas" in p for p in check_targets(shard, changes)))
        # 放行后无问题
        self.assertEqual(
            check_targets(shard, changes, allow_create={"common.sortas"}), []
        )
        rebuilt = build_bytes_with_new_keys(
            shard, changes, allow_create={"common.sortas"}
        )
        new_shard = parse_bytes(rebuilt, FIXTURES / "appinfo_00.vdf")
        self.assertEqual(verify(new_shard), [])
        self.assertEqual(verify_changes(new_shard, {entry.appid: changes[entry.appid]}), [])
        common = next(
            c for c in new_shard.by_appid[entry.appid].root.children
            if c.is_object() and new_shard.codec.key(c) == "common"
        )
        sortas = [c for c in common.children if new_shard.codec.key(c) == "sortas"]
        self.assertEqual(len(sortas), 1)
        self.assertEqual(sortas[0].raw[:-1].decode("utf-8"), "MSPD")
        # 其它 app 的 blob 字节不变
        for other in shard.entries:
            if other.appid != entry.appid:
                self.assertEqual(
                    new_shard.by_appid[other.appid].blob, other.blob
                )

    def test_apply_changes_creates_sortas_end_to_end(self):
        from backup import apply_changes
        from shard import parse_file, verify

        with tempfile.TemporaryDirectory(prefix="steam-cn-sortas-") as tmp:
            root = Path(tmp)
            appinfo = root / "APPInfo"
            shutil.copytree(FIXTURES, appinfo)
            shard = parse_file(appinfo / "appinfo_00.vdf")
            entry = next(
                e for e in shard.entries
                if any(c.is_object() and shard.codec.key(c) == "common"
                       for c in e.root.children)
            )
            changes = {shard.path.name: {entry.appid: {"common.sortas": "MSPD"}}}
            report = apply_changes(appinfo, changes, root / "backups",
                                   require_steam_closed=False)
            self.assertTrue((Path(report["backup"]) / "manifest.json").exists())
            self.assertEqual(verify(parse_file(appinfo / shard.path.name)), [])

    def test_allowed_target_includes_sortas(self):
        from mapping import is_allowed_target

        self.assertTrue(is_allowed_target("common.name"))
        self.assertTrue(is_allowed_target("common.sortas"))
        self.assertTrue(is_allowed_target("name_localized.schinese"))
        self.assertFalse(is_allowed_target("common.icon"))


class LearnedOriginalsTests(unittest.TestCase):
    def test_merge_and_write(self):
        import game_names

        merged = game_names.merge_original_names(
            {1: "BaselineA", 2: "BaselineB"}, {2: "LearnedB", 3: "LearnedC"}
        )
        self.assertEqual(merged, {1: "BaselineA", 2: "BaselineB", 3: "LearnedC"})
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "learned.csv"
            self.assertEqual(game_names.write_learned_originals(merged, dest), 3)
            loaded = game_names.load_original_names(dest)
            self.assertEqual(loaded, merged)

    def test_learn_captures_non_chinese_only(self):
        import mapping

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config"
            cfg.mkdir()
            # 用现有 fixture 的分片：它们含英文名
            new, total = mapping.learn_original_names(FIXTURES, cfg)
            self.assertGreaterEqual(total, 0)
            dest = cfg / "learned_originals.csv"
            if dest.exists():
                for value in mapping.load_original_names(dest).values():
                    self.assertFalse(any("\u4e00" <= c <= "\u9fff" for c in value))

    def test_learn_skips_approved_and_manually_modified(self):
        """已批准/手改的纯英文别名（如 CS2）不得被学成'最初英文名'。"""
        import mapping
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.mkdir()
            ledger = {
                "schema_version": 1,
                "entries": {
                    "651430": {"approved": True, "desired_name": "CS2",
                               "targets": ["common.name"]},
                    "2342040": {"manual_override": True, "desired_name": "PA",
                                "targets": ["common.name"]},
                    "2752710": {"desired_name": "待批准别名",
                                "targets": ["common.name"]},
                },
            }
            (cfg / "names.json").write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            # 基线置空，让 fixture 里的纯英文名进入学习候选
            with mock.patch.object(mapping, "load_original_names", return_value={}):
                mapping.learn_original_names(FIXTURES, cfg)
            learned = mapping.load_original_names(cfg / "learned_originals.csv")
            self.assertNotIn(651430, learned)   # approved
            self.assertNotIn(2342040, learned)  # manual_override
            self.assertNotIn(2752710, learned)  # desired_name 非空
            self.assertIn(4628710, learned)     # 未改动的纯英文名照常学习

    def test_pending_changes_skips_in_file(self):
        import mapping

        def make(appid: int, in_file: bool) -> mapping.PlanEntry:
            return mapping.PlanEntry(
                appid=appid, shard="appinfo_00.vdf", original_name="X",
                desired_name="中文", targets=["common.name"],
                status="approved", approved=True, in_file=in_file,
            )

        changes = mapping.pending_changes([make(1, True), make(2, False)])
        self.assertIn(2, changes["appinfo_00.vdf"])
        self.assertNotIn(1, changes["appinfo_00.vdf"])

    def test_pending_includes_sortas_value(self):
        import mapping

        item = mapping.PlanEntry(
            appid=3, shard="appinfo_00.vdf", original_name="X",
            desired_name="猛兽派对", targets=["common.name"],
            status="approved", approved=True, in_file=False, sortas="MSPD",
        )
        changes = mapping.pending_changes([item])
        self.assertEqual(changes["appinfo_00.vdf"][3]["common.sortas"], "MSPD")


class EdgeCaseRegressionTests(unittest.TestCase):
    """Three hidden edge cases reported in the v0.4.3 code review."""

    def test_cli_apply_allows_sortas_creation(self):
        import cli
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            changes = {"appinfo_00.vdf": {
                651430: {"common.name": "中文", "common.sortas": "ZW"},
            }}
            with mock.patch.object(cli, "plan_from_config", return_value=[]), \
                 mock.patch.object(cli, "approved_changes", return_value=changes), \
                 mock.patch.object(cli, "parse_file", return_value=object()), \
                 mock.patch.object(cli, "check_targets", return_value=[]) as check:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main([
                        "--steam-dir", tmp, "--config-dir", tmp, "apply",
                    ])
                self.assertEqual(code, 0)
                self.assertTrue(check.called)
                self.assertIn("common.sortas", check.call_args.kwargs["allow_create"])

    def test_cli_sync_auto_restart_aborts_when_shutdown_fails(self):
        import cli
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            changes = {"appinfo_00.vdf": {651430: {"common.name": "中文"}}}
            with mock.patch.object(cli, "plan_from_config", return_value=[]), \
                 mock.patch.object(cli, "pending_changes", return_value=changes), \
                 mock.patch.object(cli, "parse_file", return_value=object()), \
                 mock.patch.object(cli, "check_targets", return_value=[]), \
                 mock.patch.object(cli, "steam_running", return_value=True), \
                 mock.patch.object(cli, "steam_shutdown", return_value=False) as down, \
                 mock.patch.object(cli, "apply_changes") as write:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main([
                        "--steam-dir", tmp, "--config-dir", tmp, "sync",
                        "--execute", "--auto-restart",
                    ])
                self.assertEqual(code, 1)
                self.assertTrue(down.called)
                self.assertFalse(write.called, "关闭失败后绝不能写入")

    def test_cli_sync_auto_restart_restarts_after_write(self):
        import cli
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            changes = {"appinfo_00.vdf": {651430: {"common.name": "中文"}}}
            with mock.patch.object(cli, "plan_from_config", return_value=[]), \
                 mock.patch.object(cli, "pending_changes", return_value=changes), \
                 mock.patch.object(cli, "parse_file", return_value=object()), \
                 mock.patch.object(cli, "check_targets", return_value=[]), \
                 mock.patch.object(cli, "steam_running", return_value=True), \
                 mock.patch.object(cli, "steam_shutdown", return_value=True), \
                 mock.patch.object(cli, "apply_changes", return_value={
                     "changed": ["appinfo_00.vdf"], "backup": "bk",
                 }), \
                 mock.patch.object(cli, "steam_start", return_value=True) as up:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main([
                        "--steam-dir", tmp, "--config-dir", tmp, "sync",
                        "--execute", "--auto-restart",
                    ])
                self.assertEqual(code, 0)
                self.assertTrue(up.called)

    def test_cli_sync_without_auto_restart_rejects_running_steam(self):
        import cli
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            changes = {"appinfo_00.vdf": {651430: {"common.name": "中文"}}}
            with mock.patch.object(cli, "plan_from_config", return_value=[]), \
                 mock.patch.object(cli, "pending_changes", return_value=changes), \
                 mock.patch.object(cli, "parse_file", return_value=object()), \
                 mock.patch.object(cli, "check_targets", return_value=[]), \
                 mock.patch.object(cli, "steam_running", return_value=True), \
                 mock.patch.object(cli, "steam_shutdown") as down, \
                 mock.patch.object(cli, "apply_changes") as write:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main([
                        "--steam-dir", tmp, "--config-dir", tmp, "sync", "--execute",
                    ])
                self.assertEqual(code, 1)
                self.assertFalse(down.called)
                self.assertFalse(write.called)


class ScheduledTaskTests(unittest.TestCase):
    def test_task_command_shape(self):
        import cli

        command = cli.task_command("D:/Steam", Path("D:/cfg"), Path("D:/bk"))
        self.assertIn("pythonw.exe", command)
        self.assertIn("sync --execute --skip-if-running", command)

    def test_task_command_rejects_shell_metacharacters(self):
        import cli

        for bad in ("D:/a%b", 'D:/a"b'):
            with self.assertRaises(ValueError):
                cli.task_command(bad, Path("c"), Path("b"))

    def test_skip_if_running_exits_zero_without_writing(self):
        import cli
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            changes = {"appinfo_00.vdf": {651430: {"common.name": "中文"}}}
            with mock.patch.object(cli, "plan_from_config", return_value=[]), \
                 mock.patch.object(cli, "pending_changes", return_value=changes), \
                 mock.patch.object(cli, "parse_file", return_value=object()), \
                 mock.patch.object(cli, "check_targets", return_value=[]), \
                 mock.patch.object(cli, "steam_running", return_value=True), \
                 mock.patch.object(cli, "steam_shutdown") as down, \
                 mock.patch.object(cli, "apply_changes") as write:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main([
                        "--steam-dir", tmp, "--config-dir", tmp, "sync",
                        "--execute", "--skip-if-running",
                    ])
                self.assertEqual(code, 0)
                self.assertFalse(down.called)
                self.assertFalse(write.called)


class ShardCacheTests(unittest.TestCase):
    def test_reuses_parsed_shard(self):
        import mapping

        mapping.clear_shard_cache()
        path = next(iter(sorted(FIXTURES.glob("appinfo_*.vdf"))))
        first = mapping._load_shard(path)
        second = mapping._load_shard(path)
        self.assertIs(first, second, "同一文件未变化时应复用解析结果")
        mapping.clear_shard_cache()

    def test_invalidates_when_file_changes(self):
        import mapping

        mapping.clear_shard_cache()
        path = next(iter(sorted(FIXTURES.glob("appinfo_*.vdf"))))
        before = path.stat()
        first = mapping._load_shard(path)
        try:
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            second = mapping._load_shard(path)
            self.assertIsNot(first, second, "mtime 变化后必须重新解析，绝不能返回过期树")
        finally:
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            mapping.clear_shard_cache()

    def test_plan_reflects_write_without_stale_cache(self):
        """写入分片后，同进程内再次 plan 必须看到新值（缓存不能吞掉更新）。"""
        import mapping
        from shard import build_bytes

        mapping.clear_shard_cache()
        with tempfile.TemporaryDirectory() as tmp:
            appinfo = Path(tmp) / "APPCache" / "APPInfo"
            appinfo.parent.mkdir(parents=True)
            shutil.copytree(FIXTURES, appinfo)
            path = next(iter(sorted(appinfo.glob("appinfo_*.vdf"))))
            shard = mapping._load_shard(path)
            entry = shard.entries[0]
            rebuilt = build_bytes(shard, {entry.appid: {"common.name": "缓存验证名"}})
            path.write_bytes(rebuilt)
            again = mapping._load_shard(path)
            self.assertIsNot(shard, again)
            self.assertNotEqual(shard.entries[0].blob, again.entries[0].blob)
            mapping.clear_shard_cache()


class SqliteStorageTests(unittest.TestCase):
    """SQLite 是运行期真相源，JSON 是镜像；db 缺失时必须无缝回退。"""

    def _ledger(self):
        return {
            "schema_version": 1, "meta": {}, "summary": {},
            "defaults": {"write_sortas_pinyin": False, "primary_target": "common.name"},
            "entries": {
                "651430": {
                    "shard": "appinfo_00.vdf", "original_name": "TEKKEN 7",
                    "desired_name": "铁拳7", "approved": True,
                    "targets": ["common.name"], "note": "",
                    "custom_field": "用户自定义字段必须保留",
                },
            },
        }

    def test_ledger_roundtrip_is_lossless(self):
        import db as dbmod
        from mapping import load_names, save_names

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            data = self._ledger()
            save_names(data, cfg / "names.json")  # 建 JSON（此时无 db）
            before = load_names(cfg / "names.json")
            self.assertEqual(before["entries"]["651430"]["desired_name"], "铁拳7")

            dbmod.ensure_db(cfg, ROOT / "src" / "defaults")
            after = load_names(cfg / "names.json")  # 现在走 db
            self.assertEqual(before["entries"], after["entries"])
            self.assertEqual(before["defaults"], after["defaults"])
            self.assertEqual(
                after["entries"]["651430"]["custom_field"],
                "用户自定义字段必须保留",
            )

    def test_query_returns_only_owned_appids(self):
        import db as dbmod

        with tempfile.TemporaryDirectory() as tmp:
            conn = dbmod.connect(Path(tmp) / "renamer.db")
            try:
                dbmod.write_community(conn, {1: "一", 2: "二", 3: "三", 4: "四"})
                self.assertEqual(
                    dbmod.query_community(conn, {2, 4, 999}),
                    {2: "二", 4: "四"},
                )
                self.assertEqual(dbmod.query_community(conn, set()), {})
            finally:
                conn.close()

    def test_save_updates_db_and_json_mirror(self):
        import db as dbmod
        from mapping import load_names, save_names

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            conn = dbmod.connect(cfg / "renamer.db")
            try:
                dbmod.write_ledger(conn, self._ledger())
            finally:
                conn.close()

            data = load_names(cfg / "names.json")
            data["entries"]["651430"]["note"] = "镜像同步检查"
            save_names(data, cfg / "names.json")

            conn = dbmod.connect(cfg / "renamer.db")
            try:
                stored = dbmod.read_ledger(conn)
            finally:
                conn.close()
            self.assertEqual(stored["entries"]["651430"]["note"], "镜像同步检查")

            mirror = json.loads((cfg / "names.json").read_text(encoding="utf-8"))
            self.assertEqual(mirror["entries"]["651430"]["note"], "镜像同步检查")

    def test_corrupt_json_recovers_from_db(self):
        import db as dbmod
        from mapping import load_names, save_names

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            save_names(self._ledger(), cfg / "names.json")
            dbmod.ensure_db(cfg, None)  # 从 names.json 迁移进 ledger 表
            (cfg / "names.json").write_text("{ not valid json", encoding="utf-8")
            recovered = load_names(cfg / "names.json")
            self.assertEqual(len(recovered["entries"]), 1, "JSON 损坏必须能从 db 救回")

    def test_invalid_ledger_does_not_fall_back_to_db(self):
        """校验失败必须报错：静默回退会让用户的手改看起来没生效。"""
        import db as dbmod
        from mapping import load_names, save_names

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            save_names(self._ledger(), cfg / "names.json")
            dbmod.ensure_db(cfg, None)
            broken = self._ledger()
            broken["entries"]["651430"]["targets"] = ["common.hack"]
            (cfg / "names.json").write_text(
                json.dumps(broken, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_names(cfg / "names.json")

    def test_shared_names_roundtrip_through_db(self):
        import db as dbmod
        from game_names import load_shared_names_from_files

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            base = cfg / "base.csv"
            local = cfg / "game_names.csv"
            base.write_text(
                "GameID,OriginalName,ModifiedName\n"
                "1,Alpha,阿尔法\n2,Beta,贝塔\n",
                encoding="utf-8-sig",
            )
            local.write_text(
                "GameID,OriginalName,ModifiedName\n2,Beta,贝塔覆盖\n3,Gamma,伽马\n",
                encoding="utf-8-sig",
            )
            expected = load_shared_names_from_files(base, local)

            conn = dbmod.connect(cfg / "renamer.db")
            try:
                dbmod.sync_shared_from_files(conn, base, local)
                stored = dbmod.read_shared_names(conn)
            finally:
                conn.close()
            self.assertEqual(stored.names, expected.names)
            self.assertEqual(stored.originals, expected.originals)
            self.assertEqual(stored.sources, expected.sources)
            self.assertEqual(stored.names[2], "贝塔覆盖", "本地覆盖层必须生效")

    def test_shared_csv_edit_takes_effect_without_restart(self):
        import db as dbmod
        from game_names import load_shared_names

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            base = cfg / "base.csv"
            local = cfg / "game_names.csv"
            base.write_text(
                "GameID,OriginalName,ModifiedName\n1,Alpha,阿尔法\n",
                encoding="utf-8-sig",
            )
            local.write_text(
                "GameID,OriginalName,ModifiedName\n1,Alpha,旧译名\n",
                encoding="utf-8-sig",
            )
            dbmod.ensure_db(cfg, None)
            self.assertEqual(load_shared_names(base, local).names[1], "旧译名")

            time.sleep(0.02)  # 确保 mtime 前进，触发重新导入
            local.write_text(
                "GameID,OriginalName,ModifiedName\n1,Alpha,新译名\n",
                encoding="utf-8-sig",
            )
            self.assertEqual(
                load_shared_names(base, local).names[1], "新译名",
                "改 CSV 后不得需要重启",
            )

    def test_originals_prefer_baseline_over_learned(self):
        import db as dbmod

        with tempfile.TemporaryDirectory() as tmp:
            conn = dbmod.connect(Path(tmp) / "renamer.db")
            try:
                dbmod.write_original_names(conn, {7: "BaselineName"}, "baseline")
                dbmod.write_original_names(conn, {7: "LearnedName", 8: "OnlyLearned"}, "learned")
                got = dbmod.query_originals(conn, {7, 8})
            finally:
                conn.close()
            self.assertEqual(got[7], "BaselineName", "baseline 必须优先于 learned")
            self.assertEqual(got[8], "OnlyLearned")


class DesktopShellTests(unittest.TestCase):
    """桌面壳的纯逻辑部分；GUI 窗口本身需要人工实测。"""

    def test_guard_only_syncs_on_exit_edge(self):
        import run_desktop as rd

        self.assertFalse(rd.SteamGuard.should_sync(None, False), "初始未知不得写入")
        self.assertFalse(rd.SteamGuard.should_sync(None, True))
        self.assertFalse(rd.SteamGuard.should_sync(False, False))
        self.assertFalse(rd.SteamGuard.should_sync(True, True))
        self.assertTrue(rd.SteamGuard.should_sync(True, False), "运行→退出 必须补写")

    def test_guard_interval_has_floor(self):
        import threading

        import run_desktop as rd

        guard = rd.SteamGuard(".", ".", ".", threading.Lock(), interval=0.01)
        self.assertGreaterEqual(guard._interval, 5.0, "防止把轮询设得过密")
        guard.stop()

    def test_guard_skips_when_steam_running(self):
        import threading
        from unittest import mock

        import backup
        import run_desktop as rd

        guard = rd.SteamGuard(".", ".", ".", threading.Lock())
        with mock.patch.object(backup, "steam_running", return_value=True):
            self.assertEqual(guard.sync_once(), 0)
        self.assertIn("运行中", guard.last_result)

    def test_tray_image_is_valid_png(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow 未安装")
        import io

        import run_desktop as rd

        image = rd.tray_image()
        self.assertEqual(image.size, (64, 64))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self.assertEqual(buffer.getvalue()[:8], b"\x89PNG\r\n\x1a\n")

    def test_missing_deps_reports_reason(self):
        import run_desktop as rd

        # 当前环境未装 desktop 依赖时必须给出可读原因，而不是抛异常
        missing = rd.missing_desktop_deps()
        self.assertTrue(missing is None or isinstance(missing, str))


class WebSaveMergeTests(unittest.TestCase):
    """Web 保存必须保留白名单外的用户字段（曾整条替换导致静默丢失）。"""

    def _serve(self, cfg):
        import gui
        from http.server import ThreadingHTTPServer

        handler = gui.make_handler(str(FIXTURES.parent), cfg, cfg / "backups")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _call(self, server, path, payload=None):
        import urllib.request

        port = server.server_port
        url = f"http://127.0.0.1:{port}{path}"
        headers = {"Origin": f"http://127.0.0.1:{port}"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data else "GET")
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            try:
                return response, json.loads(raw)
            except ValueError:  # 静态页面返回 HTML，不是 JSON
                return response, raw

    def test_custom_field_survives_save(self):
        from mapping import load_names, save_names

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            shutil.copy(FIXTURE_CONFIG / "rules.json", cfg / "rules.json")
            ledger = {
                "schema_version": 1, "meta": {}, "defaults": {}, "summary": {},
                "entries": {"651430": {
                    "shard": "appinfo_00.vdf", "desired_name": "铁拳7",
                    "approved": True, "targets": ["common.name"],
                    "custom_note": "我自己加的备注",
                }},
            }
            save_names(ledger, cfg / "names.json")
            server = self._serve(cfg)
            try:
                self._call(server, "/api/save", {"entries": [{
                    "appid": "651430", "desired_name": "铁拳7",
                    "approved": True, "targets": ["common.name"],
                }]})
            finally:
                server.shutdown()
                server.server_close()
            saved = load_names(cfg / "names.json")["entries"]["651430"]
            self.assertEqual(saved.get("custom_note"), "我自己加的备注")
            self.assertEqual(saved["desired_name"], "铁拳7")

    def test_html_response_has_csp(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            shutil.copy(FIXTURE_CONFIG / "rules.json", cfg / "rules.json")
            shutil.copy(FIXTURE_CONFIG / "names.json", cfg / "names.json")
            server = self._serve(cfg)
            try:
                response, _ = self._call(server, "/")
                csp = response.headers.get("Content-Security-Policy") or ""
            finally:
                server.shutdown()
                server.server_close()
            self.assertIn("default-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)

    def test_handler_has_socket_timeout(self):
        import gui

        self.assertIsNotNone(getattr(gui, "HANDLER_TIMEOUT", None))


class ShutdownEndpointTests(unittest.TestCase):
    """桌面版的「退出服务」必须能拆掉整个程序，而不只是停掉 HTTP。"""

    def _serve(self, cfg, on_shutdown=None):
        import threading
        from http.server import ThreadingHTTPServer

        import gui

        handler = gui.make_handler(str(cfg), cfg, cfg / "backups", None, on_shutdown)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _post(self, server, path):
        import urllib.request

        url = f"http://127.0.0.1:{server.server_port}{path}"
        request = urllib.request.Request(url, data=b"{}", method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def test_callback_fires_for_desktop_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            shutil.copy(FIXTURE_CONFIG / "names.json", cfg / "names.json")
            shutil.copy(FIXTURE_CONFIG / "rules.json", cfg / "rules.json")
            fired = threading.Event()
            server = self._serve(cfg, fired.set)
            try:
                payload = self._post(server, "/api/shutdown")
                self.assertTrue(payload["desktop"])
                self.assertTrue(fired.wait(3), "桌面版必须收到退出回调")
            finally:
                server.shutdown()
                server.server_close()

    def test_browser_mode_reports_no_desktop(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            shutil.copy(FIXTURE_CONFIG / "names.json", cfg / "names.json")
            shutil.copy(FIXTURE_CONFIG / "rules.json", cfg / "rules.json")
            server = self._serve(cfg, None)
            try:
                payload = self._post(server, "/api/shutdown")
                self.assertFalse(payload["desktop"])
                self.assertIn("服务已停止", payload["message"])
            finally:
                server.shutdown()
                server.server_close()


class WebDefaultsTests(unittest.TestCase):
    def test_round_trip_and_field_guard(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        import gui

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.mkdir()
            shutil.copy(ROOT / "src" / "defaults" / "names.json", cfg / "names.json")
            shutil.copy(ROOT / "src" / "defaults" / "rules.json", cfg / "rules.json")
            handler = gui.make_handler(tmp, cfg, Path(tmp) / "backups")
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"

            def call(path: str, body: dict | None = None):
                data = json.dumps(body).encode() if body is not None else None
                request = urllib.request.Request(
                    base + path, data=data, method="POST" if data else "GET",
                    headers={"Content-Type": "application/json"} if data else {},
                )
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            try:
                status, payload = call("/api/defaults")
                self.assertEqual(status, 200)
                self.assertIn("write_sortas_pinyin", payload["defaults"])
                status, _ = call("/api/defaults", {"defaults": {
                    "write_sortas_pinyin": True,
                    "sync_existing_localized": ["schinese", "tchinese"],
                }})
                self.assertEqual(status, 200)
                status, payload = call("/api/defaults")
                self.assertTrue(payload["defaults"]["write_sortas_pinyin"])
                self.assertIn("tchinese", payload["defaults"]["sync_existing_localized"])
                status, _ = call("/api/defaults", {"defaults": {"entries": {}}})
                self.assertEqual(status, 400)
                status, _ = call("/api/defaults", {"defaults": {
                    "write_sortas_pinyin": "yes"
                }})
                self.assertEqual(status, 400)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
