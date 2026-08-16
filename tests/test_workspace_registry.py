"""项目工作区注册表：路径边界、快照契约与持久化行为。"""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.workspace_registry import (
    discover_project_assets,
    get_project,
    list_projects,
    load_registry,
    normalize_symbol,
    register_project,
)


ROOT = Path(__file__).resolve().parent.parent


class WorkspaceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "data" / "projects").mkdir(parents=True)
        (self.root / "data" / "research_items_empty.json").write_text(
            json.dumps({"meta": {}, "items": []}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_snapshot(
        self,
        underlying: str = "US.AAPL",
        filename: str = "aapl_inputs.json",
        name: str = "Apple Inc.",
    ) -> Path:
        source = json.loads((ROOT / "data" / "hero_inputs.json").read_text(encoding="utf-8"))
        source["underlying"] = underlying
        source["name"] = name
        path = self.root / "data" / "projects" / filename
        path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        return path

    def _write_research(
        self,
        filename: str = "aapl_research.json",
        underlying: str = "US.AAPL",
    ) -> Path:
        path = self.root / "data" / "projects" / filename
        path.write_text(
            json.dumps(
                {
                    "meta": {"underlying": underlying, "name": "Apple Inc."},
                    "items": [{"id": "apple-note", "title": "Apple note"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_default_registry_exposes_tencent_as_one_project(self) -> None:
        registry = load_registry()

        project = get_project(registry)

        self.assertEqual(project["symbol"], "HK.00700")
        self.assertEqual(project["name"], "腾讯控股")
        self.assertEqual(len(list_projects(registry)), 1)

    def test_register_project_validates_snapshot_and_persists(self) -> None:
        registry_path = self.root / "data" / "workspaces.json"
        snapshot_path = self._write_snapshot()

        project = register_project(
            name="苹果公司",
            symbol="US.AAPL",
            input_path=snapshot_path,
            registry_path=registry_path,
            root=self.root,
        )
        registry = load_registry(registry_path, root=self.root)

        self.assertEqual(project["symbol"], "US.AAPL")
        self.assertEqual(get_project(registry)["id"], project["id"])
        self.assertTrue(registry_path.is_file())
        projects = list_projects(registry)
        self.assertEqual(projects[-1]["inputPath"], "data/projects/aapl_inputs.json")

    def test_register_project_rejects_snapshot_outside_data_root(self) -> None:
        snapshot_path = self.root / "outside.json"
        snapshot_path.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "data"):
            register_project(
                name="越界项目",
                symbol="US.AAPL",
                input_path=snapshot_path,
                registry_path=self.root / "data" / "workspaces.json",
                root=self.root,
            )

    def test_register_project_rejects_symbol_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "underlying"):
            register_project(
                name="错误映射",
                symbol="HK.09988",
                input_path=self._write_snapshot(),
                registry_path=self.root / "data" / "workspaces.json",
                root=self.root,
            )

    def test_register_project_reports_missing_snapshot_with_guidance(self) -> None:
        with self.assertRaisesRegex(ValueError, r"data/projects/.*SNAPSHOT_RECORDING"):
            register_project(
                name="贵州茅台",
                symbol="SSE.600519",
                registry_path=self.root / "data" / "workspaces.json",
                root=self.root,
            )

    def test_discover_project_assets_finds_snapshot_and_matching_research(self) -> None:
        self._write_snapshot()
        self._write_research()
        (self.root / "data" / "projects" / "not-a-snapshot.json").write_text(
            json.dumps({"hello": "world"}), encoding="utf-8"
        )

        candidates = discover_project_assets(symbol="US.AAPL", root=self.root)

        self.assertEqual({item["kind"] for item in candidates}, {"snapshot", "research"})
        self.assertEqual(
            {item["path"] for item in candidates},
            {
                "data/projects/aapl_inputs.json",
                "data/projects/aapl_research.json",
            },
        )
        research = next(item for item in candidates if item["kind"] == "research")
        self.assertEqual(research["itemCount"], 1)

    def test_register_project_discovers_paths_when_they_are_omitted(self) -> None:
        self._write_snapshot()
        self._write_research()

        project = register_project(
            name="苹果公司",
            symbol="US.AAPL",
            input_path=None,
            registry_path=self.root / "data" / "workspaces.json",
            root=self.root,
        )

        self.assertEqual(project["input_path"], self.root / "data" / "projects" / "aapl_inputs.json")
        self.assertEqual(
            project["research_items_path"],
            self.root / "data" / "projects" / "aapl_research.json",
        )

    def test_register_project_reports_ambiguous_snapshot_candidates(self) -> None:
        self._write_snapshot(filename="aapl_inputs_1.json")
        self._write_snapshot(filename="aapl_inputs_2.json")

        with self.assertRaisesRegex(ValueError, "多个.*快照"):
            register_project(
                name="苹果公司",
                symbol="US.AAPL",
                input_path=None,
                registry_path=self.root / "data" / "workspaces.json",
                root=self.root,
            )

    def test_normalize_symbol_accepts_generic_market_prefixes(self) -> None:
        self.assertEqual(normalize_symbol("SSE.600519"), "SSE.600519")
        self.assertEqual(normalize_symbol("NASDAQ.AAPL"), "NASDAQ.AAPL")
        with self.assertRaises(ValueError):
            normalize_symbol("600519")

    def test_discovery_matches_generic_market_code_and_company_name(self) -> None:
        self._write_snapshot(
            underlying="SSE.600519",
            filename="kweichow_inputs.json",
            name="贵州茅台",
        )
        self._write_research(
            filename="kweichow_research.json",
            underlying="SSE.600519",
        )

        candidates = discover_project_assets(
            query="研究 贵州茅台 600519",
            root=self.root,
        )

        snapshot = next(item for item in candidates if item["kind"] == "snapshot")
        self.assertEqual(snapshot["symbol"], "SSE.600519")
        self.assertEqual(snapshot["name"], "贵州茅台")


    def test_register_project_idempotent_requires_same_name(self) -> None:
        """同 id 同 symbol 同路径但 name 不同不是幂等重试，必须显式 422。"""

        registry_path = self.root / "data" / "workspaces.json"
        snapshot_path = self._write_snapshot()
        common = dict(
            symbol="US.AAPL",
            input_path=snapshot_path,
            registry_path=registry_path,
            root=self.root,
            project_id="aapl-project",
        )

        first = register_project(name="Apple Inc.", **common)

        with self.assertRaisesRegex(ValueError, "项目 id 已存在"):
            register_project(name="苹果公司", **common)

        self.assertEqual(first["name"], "Apple Inc.")

    def test_register_project_idempotent_retry_same_payload(self) -> None:
        registry_path = self.root / "data" / "workspaces.json"
        snapshot_path = self._write_snapshot()
        common = dict(
            name="Apple Inc.",
            symbol="US.AAPL",
            input_path=snapshot_path,
            registry_path=registry_path,
            root=self.root,
        )

        first = register_project(**common)
        second = register_project(**common)

        self.assertEqual(first["id"], second["id"])
        registry = load_registry(registry_path, root=self.root)
        self.assertEqual(
            sum(1 for p in registry["projects"] if p["id"] == first["id"]), 1
        )

    def test_register_project_concurrent_same_request_is_idempotent(self) -> None:
        """并发相同注册不得写坏 registry，也不得产生重复项目。"""

        registry_path = self.root / "data" / "workspaces.json"
        snapshot_path = self._write_snapshot()
        common = dict(
            name="Apple Inc.",
            symbol="US.AAPL",
            input_path=snapshot_path,
            registry_path=registry_path,
            root=self.root,
        )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: register_project(**common), range(4)))

        self.assertTrue(all(item["id"] == results[0]["id"] for item in results))
        registry = load_registry(registry_path, root=self.root)
        matching = [p for p in registry["projects"] if p["id"] == results[0]["id"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["name"], "Apple Inc.")


if __name__ == "__main__":
    unittest.main()
