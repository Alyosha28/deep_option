"""本地研究项目注册表。

一个项目把标的、期权链、账户约束、业绩数据和投研资料绑定在同一个
冻结快照上下文中。注册表只允许引用 ``data/`` 内的 JSON，浏览器不能借此
读取任意本机文件。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = ROOT / "data"
PROJECTS_ROOT = DATA_ROOT / "projects"
DEFAULT_REGISTRY_PATH = DATA_ROOT / "workspaces.json"
EMPTY_RESEARCH_PATH = DATA_ROOT / "research_items_empty.json"

# 每个注册表文件一把进程内锁：register_project 的 load-check-write 必须
# 串行，否则两个相同注册并发时都会看到“项目不存在”，后写者覆盖先写者，
# 幂等重试语义被破坏。锁按 resolved path 分片，不影响不同临时目录的测试。
_REGISTRY_LOCKS_GUARD = threading.Lock()
_REGISTRY_LOCKS: dict[str, threading.Lock] = {}


def _registry_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _REGISTRY_LOCKS_GUARD:
        lock = _REGISTRY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REGISTRY_LOCKS[key] = lock
        return lock


def _root_path(root: str | Path | None) -> Path:
    return (Path(root) if root is not None else ROOT).resolve()


def _default_record(root: Path) -> dict[str, str]:
    return {
        "id": "tencent-0700",
        "name": "腾讯控股",
        "symbol": "HK.00700",
        "input_path": "data/hero_inputs.json",
        "research_items_path": "data/research_items_hero.json",
        "description": "业绩前方向不确定的冻结快照示例",
    }


def normalize_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    if not re.fullmatch(
        r"[A-Z][A-Z0-9_-]{1,15}\.[A-Z0-9][A-Z0-9._-]{0,46}",
        symbol,
    ):
        raise ValueError(
            "标的代码必须使用市场前缀，例如 HK.00700、US.AAPL 或 SSE.600519"
        )
    return symbol


_DATA_MODES = {"live", "replay"}


def _normalize_data_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    if not mode:
        return "replay"
    if mode not in _DATA_MODES:
        raise ValueError(f"data_mode 必须为 live 或 replay，收到：{value!r}")
    return mode


def _relative_label(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _resolve_data_path(
    value: str | Path,
    *,
    root: Path,
    label: str,
    must_exist: bool = False,
) -> Path:
    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError(f"{label} 不能为空")
    raw = Path(raw_value)
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    data_root = (root / "data").resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于 data/ 目录内") from exc
    if resolved.suffix.lower() != ".json":
        raise ValueError(f"{label} 必须是 .json 文件")
    if must_exist and not resolved.is_file():
        raise ValueError(f"{label} 不存在：{_relative_label(resolved, root)}")
    return resolved


def _project_from_record(record: Mapping[str, Any], root: Path) -> dict[str, Any]:
    project_id = str(record.get("id") or record.get("project_id") or "").strip()
    name = str(record.get("name") or "").strip()
    if not project_id or not name:
        raise ValueError("工作区项目必须包含 id 和 name")
    input_value = record.get("input_path") or record.get("inputPath")
    if not input_value:
        raise ValueError(f"项目 {project_id} 缺少 input_path")
    research_value = (
        record.get("research_items_path")
        or record.get("researchItemsPath")
        or _relative_label(root / "data" / "research_items_empty.json", root)
    )
    return {
        "id": project_id,
        "name": name,
        "symbol": normalize_symbol(str(record.get("symbol") or "")),
        "input_path": _resolve_data_path(
            input_value, root=root, label=f"项目 {project_id} 的快照路径"
        ),
        "research_items_path": _resolve_data_path(
            research_value, root=root, label=f"项目 {project_id} 的研究资料路径"
        ),
        "description": str(record.get("description") or "").strip(),
        "data_mode": _normalize_data_mode(record.get("data_mode")),
    }


def _registry_file_path(path: str | Path | None, root: Path) -> Path:
    if path is None:
        return root / "data" / "workspaces.json"
    return Path(path).resolve()


def load_registry(
    path: str | Path | None = None,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """读取注册表；首次运行没有配置文件时返回腾讯默认项目。"""

    root_path = _root_path(root)
    registry_path = _registry_file_path(path, root_path)
    if not registry_path.is_file():
        projects = [_project_from_record(_default_record(root_path), root_path)]
        return {
            "version": 1,
            "active_project_id": projects[0]["id"],
            "projects": projects,
            "_root": root_path,
            "_path": registry_path,
        }

    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("工作区注册表不是合法 JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("projects"), list):
        raise ValueError("工作区注册表必须包含 projects 数组")
    projects = [_project_from_record(item, root_path) for item in payload["projects"]]
    if not projects:
        raise ValueError("工作区至少需要一个项目")
    project_ids = [project["id"] for project in projects]
    if len(set(project_ids)) != len(project_ids):
        raise ValueError("工作区项目 id 不能重复")
    active = str(payload.get("active_project_id") or payload.get("activeProjectId") or "")
    if active not in project_ids:
        active = project_ids[0]
    return {
        "version": int(payload.get("version") or 1),
        "active_project_id": active,
        "projects": projects,
        "_root": root_path,
        "_path": registry_path,
    }


def get_project(registry: Mapping[str, Any], project_id: str | None = None) -> dict[str, Any]:
    wanted = str(project_id or registry.get("active_project_id") or "").strip()
    for project in registry.get("projects", []):
        if project.get("id") == wanted:
            if not Path(project["input_path"]).is_file():
                root = Path(registry.get("_root") or ROOT)
                raise ValueError(
                    f"项目 {project['name']} 的快照不存在："
                    f"{_relative_label(Path(project['input_path']), root)}"
                )
            return dict(project)
    raise ValueError(f"工作区项目不存在：{wanted or '未指定'}")


def list_projects(
    registry: Mapping[str, Any],
    active_project_id: str | None = None,
) -> list[dict[str, Any]]:
    """转换成前端可用的元数据，不把快照内容直接复制到列表。"""

    root = Path(registry.get("_root") or ROOT)
    active = active_project_id or registry.get("active_project_id")
    result: list[dict[str, Any]] = []
    for project in registry.get("projects", []):
        input_path = Path(project["input_path"])
        research_path = Path(project["research_items_path"])
        result.append(
            {
                "id": project["id"],
                "name": project["name"],
                "symbol": project["symbol"],
                "description": project.get("description", ""),
                "inputPath": _relative_label(input_path, root),
                "researchItemsPath": _relative_label(research_path, root),
                "data_mode": project.get("data_mode", "replay"),
                "available": input_path.is_file(),
                "researchAvailable": research_path.is_file(),
                "active": project["id"] == active,
            }
        )
    return result


def _validate_research_file(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("研究资料文件不是合法 JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
        raise ValueError("研究资料文件必须包含 items 数组")


def _iter_data_json_files(root: Path) -> list[Path]:
    """返回受控 ``data/`` 目录内的 JSON 文件，不跟随越界 symlink。"""

    data_root = (root / "data").resolve()
    if not data_root.is_dir():
        return []
    files: list[Path] = []
    for path in data_root.rglob("*.json"):
        try:
            resolved = path.resolve()
            resolved.relative_to(data_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            files.append(resolved)
    return sorted(set(files), key=lambda item: _relative_label(item, root))


def _query_terms(value: str | None) -> list[str]:
    return [
        token.casefold()
        for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", str(value or ""))
        if token
    ]


def _candidate_score(query: str | None, fields: list[str]) -> int:
    terms = _query_terms(query)
    if not terms:
        return 0
    haystack = " ".join(str(field or "").casefold() for field in fields)
    compact_haystack = re.sub(r"[^a-z0-9]+", "", haystack)
    score = 0
    for term in terms:
        if term in haystack:
            score += 2 if len(term) >= 3 else 1
    compact_query = re.sub(r"[^a-z0-9]+", "", str(query or "").casefold())
    if compact_query and compact_query in compact_haystack:
        score += 5
    return score


def _research_payload(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
        return None
    return payload


def _metadata_value(payload: Mapping[str, Any], key: str) -> str:
    direct = payload.get(key)
    if isinstance(direct, str):
        return direct.strip()
    meta = payload.get("meta")
    if isinstance(meta, Mapping) and isinstance(meta.get(key), str):
        return str(meta[key]).strip()
    return ""


def _symbol_aliases(symbol: str) -> set[str]:
    market, code = symbol.split(".", 1)
    aliases = {symbol.casefold(), market.casefold(), code.casefold()}
    compact_code = code.lstrip("0") or "0"
    aliases.add(compact_code.casefold())
    if len(code) > 4:
        aliases.add(code[-4:].casefold())
    return aliases


def _research_matches_symbol(
    candidate: Mapping[str, Any],
    symbol: str,
) -> bool:
    candidate_symbol = str(candidate.get("symbol") or "").strip()
    if candidate_symbol:
        try:
            if normalize_symbol(candidate_symbol) == symbol:
                return True
        except ValueError:
            pass
    fields = [
        str(candidate.get("path") or ""),
        str(candidate.get("name") or ""),
        candidate_symbol,
    ]
    haystack = re.sub(r"[^a-z0-9]+", "", " ".join(fields).casefold())
    return any(
        re.sub(r"[^a-z0-9]+", "", alias.casefold()) in haystack
        for alias in _symbol_aliases(symbol)
        if len(re.sub(r"[^a-z0-9]+", "", alias.casefold())) >= 3
    )


def _discover_project_assets(
    *,
    query: str | None = None,
    symbol: str | None = None,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """发现并校验快照/研究资料；返回值包含内部 ``_path`` 供注册使用。"""

    root_path = _root_path(root)
    clean_symbol = normalize_symbol(symbol) if str(symbol or "").strip() else None
    # 延迟导入，避免注册表与决策管线形成导入环。
    from src.decision_pipeline import load_frozen_snapshot

    projects_root = (root_path / "data" / "projects").resolve()
    candidates: list[dict[str, Any]] = []
    for path in _iter_data_json_files(root_path):
        relative = _relative_label(path, root_path)
        try:
            snapshot = load_frozen_snapshot(path)
        except (OSError, ValueError, KeyError, TypeError):
            snapshot = None
        if snapshot is not None:
            try:
                path.relative_to(projects_root)
            except ValueError:
                # data/hero_inputs.json 是默认演示项目，不作为“新增项目”候选。
                snapshot = None
        if snapshot is not None:
            payload = snapshot["payload"]
            try:
                actual_symbol = normalize_symbol(str(payload.get("underlying") or ""))
            except ValueError:
                continue
            if clean_symbol and actual_symbol != clean_symbol:
                continue
            name = str(payload.get("name") or "").strip()
            score = _candidate_score(query, [relative, actual_symbol, name])
            if query and score <= 0:
                continue
            candidates.append(
                {
                    "kind": "snapshot",
                    "path": relative,
                    "name": name,
                    "symbol": actual_symbol,
                    "itemCount": len(payload.get("legs") or []),
                    "score": score,
                    "_path": path,
                }
            )
            continue

        if path.name.casefold() == "research_items_empty.json":
            continue
        payload = _research_payload(path)
        if payload is None:
            continue
        candidate_symbol = (
            _metadata_value(payload, "underlying")
            or _metadata_value(payload, "symbol")
            or _metadata_value(payload, "ticker")
            or _metadata_value(payload, "code")
        )
        candidate_name = _metadata_value(payload, "name") or _metadata_value(
            payload, "company"
        )
        candidate = {
            "kind": "research",
            "path": relative,
            "name": candidate_name,
            "symbol": candidate_symbol,
            "itemCount": len(payload.get("items") or []),
            "score": _candidate_score(query, [relative, candidate_symbol, candidate_name]),
            "_path": path,
        }
        if clean_symbol and not _research_matches_symbol(candidate, clean_symbol):
            continue
        if query and candidate["score"] <= 0:
            continue
        candidates.append(candidate)

    kind_order = {"snapshot": 0, "research": 1}
    return sorted(
        candidates,
        key=lambda item: (
            kind_order.get(str(item.get("kind")), 9),
            -int(item.get("score") or 0),
            str(item.get("path") or ""),
        ),
    )


def discover_project_assets(
    query: str | None = None,
    *,
    symbol: str | None = None,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """扫描受控数据目录并返回可供 Agent 选择的有效资产候选。

    只返回通过冻结快照/研究资料契约校验的 JSON，并且只暴露 ``data/``
    内的相对路径。调用方可以传入完整标的代码做精确筛选，或传入自然语言
    查询让 Agent 按文件名、标的代码和快照名称筛选。
    """

    return [
        {key: value for key, value in candidate.items() if key != "_path"}
        for candidate in _discover_project_assets(query=query, symbol=symbol, root=root)
    ]


def _write_registry(registry: Mapping[str, Any], path: Path, root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = {
        "version": int(registry.get("version") or 1),
        "active_project_id": registry["active_project_id"],
        "projects": [
            {
                "id": project["id"],
                "name": project["name"],
                "symbol": project["symbol"],
                "input_path": _relative_label(Path(project["input_path"]), root),
                "research_items_path": _relative_label(
                    Path(project["research_items_path"]), root
                ),
                "description": project.get("description", ""),
                "data_mode": project.get("data_mode", "replay"),
            }
            for project in registry["projects"]
        ],
    }
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(serialised, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _project_id_for_symbol(symbol: str) -> str:
    project_id = re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-")
    return project_id or "research-project"


def register_project(
    *,
    name: str,
    symbol: str,
    input_path: str | Path | None = None,
    research_items_path: str | Path | None = None,
    project_id: str | None = None,
    data_mode: str | None = None,
    registry_path: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """注册一个新公司研究项目，并将它设为当前项目。

    新项目快照必须放在 ``data/projects/`` 下；省略路径时会自动发现并校验
    该标的的快照和投研资料。找不到资料时才回退到空资料集，不会误用腾讯
    的示例新闻。
    """

    root_path = _root_path(root)
    clean_name = str(name or "").strip()
    if len(clean_name) > 80:
        raise ValueError("项目名称不能超过 80 个字符")
    clean_symbol = normalize_symbol(symbol)
    clean_data_mode = _normalize_data_mode(data_mode)
    discovered = _discover_project_assets(symbol=clean_symbol, root=root_path)
    snapshot_candidates = [
        candidate for candidate in discovered if candidate["kind"] == "snapshot"
    ]
    if input_path:
        snapshot_path = _resolve_data_path(
            input_path, root=root_path, label="项目快照路径", must_exist=True
        )
    elif not snapshot_candidates:
        raise ValueError(
            f"没有找到 {clean_symbol} 的有效期权快照：请先将该标的的冻结快照 JSON "
            "放入 data/projects/（录制与格式见 docs/SNAPSHOT_RECORDING.md）；"
            "当前 P0 演示范围以 data/ 内已有快照为准"
        )
    elif len(snapshot_candidates) > 1:
        paths = "、".join(str(item["path"]) for item in snapshot_candidates[:5])
        raise ValueError(
            f"发现多个 {clean_symbol} 快照，请在高级路径中指定一个：{paths}"
        )
    else:
        snapshot_path = Path(snapshot_candidates[0]["_path"])
    try:
        snapshot_path.relative_to((root_path / "data" / "projects").resolve())
    except ValueError as exc:
        raise ValueError("新项目快照必须放在 data/projects/ 目录内") from exc

    # 延迟导入，避免注册表模块成为决策管线的导入环。
    from src.decision_pipeline import load_frozen_snapshot

    snapshot = load_frozen_snapshot(snapshot_path)
    actual_symbol = normalize_symbol(snapshot["payload"]["underlying"])
    if actual_symbol != clean_symbol:
        raise ValueError(
            f"项目 symbol ({clean_symbol}) 与快照 underlying ({actual_symbol}) 不一致"
        )

    if not clean_name:
        clean_name = str(snapshot["payload"].get("name") or clean_symbol).strip()
    if research_items_path:
        research_path = _resolve_data_path(
            research_items_path,
            root=root_path,
            label="研究资料路径",
            must_exist=True,
        )
    else:
        research_candidates = [
            candidate
            for candidate in discovered
            if candidate["kind"] == "research"
        ]
        if len(research_candidates) > 1:
            paths = "、".join(str(item["path"]) for item in research_candidates[:5])
            raise ValueError(
                f"发现多个 {clean_symbol} 投研资料，请在高级路径中指定一个：{paths}"
            )
        research_path = (
            Path(research_candidates[0]["_path"])
            if research_candidates
            else root_path / "data" / "research_items_empty.json"
        )
        research_path = _resolve_data_path(
            research_path,
            root=root_path,
            label="研究资料路径",
            must_exist=True,
        )
    _validate_research_file(research_path)

    registry_file = _registry_file_path(registry_path, root_path)
    with _registry_lock(registry_file):
        registry = load_registry(registry_file, root=root_path)
        new_id = str(project_id or _project_id_for_symbol(clean_symbol)).strip()
        if not new_id or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", new_id):
            raise ValueError("项目 id 只能包含小写字母、数字、下划线和短横线")
        existing = next(
            (project for project in registry["projects"] if project["id"] == new_id),
            None,
        )
        if existing is not None:
            same_name = existing.get("name") == clean_name
            same_symbol = existing["symbol"] == clean_symbol
            same_input = Path(existing["input_path"]) == snapshot_path
            same_research = Path(existing["research_items_path"]) == research_path
            same_mode = existing.get("data_mode", "replay") == clean_data_mode
            if same_name and same_symbol and same_input and same_research and same_mode:
                # 幂等重试：同一注册请求（例如 OpenD 不可用时客户端重试
                # POST /api/projects）不应因项目已持久化而返回 422。
                # name 也参与相等判断：同 id 不同名称是真实变更，必须显式报错，
                # 不能静默吞掉。
                registry["active_project_id"] = existing["id"]
                _write_registry(registry, registry_file, root_path)
                return dict(existing)
            raise ValueError(f"项目 id 已存在：{new_id}")
        if any(project["symbol"] == clean_symbol for project in registry["projects"]):
            raise ValueError(f"标的已经在工作区中：{clean_symbol}")

        record = {
            "id": new_id,
            "name": clean_name,
            "symbol": clean_symbol,
            "input_path": snapshot_path,
            "research_items_path": research_path,
            "description": "已导入的公司期权研究快照",
            "data_mode": clean_data_mode,
        }
        registry["projects"].append(record)
        registry["active_project_id"] = new_id
        _write_registry(registry, registry_file, root_path)
        return dict(record)
