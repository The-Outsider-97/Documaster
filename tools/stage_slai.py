#!/usr/bin/env python3
"""Stage the part of SLAI that Documaster needs into a folder that can serve as ``SLAI_ROOT`` inside the desktop app.

    python tools/stage_slai.py --slai-root <SLAI checkout> --out <dest> [--models lantra|none] [--report report.json]
    python tools/stage_slai.py --slai-root <SLAI checkout> --report report.json --dry-run

Why a closure and not a hand-kept list: SLAI changes every week. A fixed list of files goes stale and the app breaks on the first
module somebody adds. This tool starts from the modules Documaster actually uses (``ENTRY_MODULES``), reads the *source* of each one
with ``ast`` (no SLAI code is imported or run, so it needs neither torch nor any other SLAI dependency), follows every import it
finds, including imports inside functions and ``importlib.import_module("literal")``, and copies exactly that set of ``.py`` files
unchanged. It then adds the data those modules read at run time (see ``DATA RULES``) and, if asked, the model files.

What it can not know and therefore does not claim:

* imports built from a variable (``importlib.import_module(name)``) cannot be followed. The factory does this for the agent
  classes; the eight agents Documaster uses are therefore listed in ``ENTRY_MODULES`` by hand.
* data files that code builds a path to at run time are found only through the rules below. ``verify_staged`` (see
  ``tools/build_engine.py --verify``) imports the staged tree with the bundled Python, which is what catches a miss.

The result is described by ``slai_bundle.json`` in the destination (every file with size and SHA-256, the module closure, the
third-party packages the closure imports, and the models).
"""
from __future__ import annotations

import argparse
import ast
import functools
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

MANIFEST = "slai_bundle.json"
MANIFEST_VERSION = 1

# --- what Documaster uses ---------------------------------------------------------------------------------------------------------
# (module, required). A required module that is missing means this is not the SLAI the list was made for: the tool stops.
ENTRY_MODULES: tuple[tuple[str, bool], ...] = (
    ("src.utils.sldc", True),                       # the authoritative SLDC reader/writer (slai.bridge.sldc)
    ("src.utils.sldc_sheets", False),               # spreadsheet side of SLDC, present in newer checkouts
    ("src.agents.reader.parser_engine", True),      # Reader engines Documaster calls directly (slai.bridge.reader)
    ("src.agents.reader.detection_engine", True),
    ("src.agents.reader.integrity_engine", True),
    ("src.agents.reader.compression_engine", True),
    ("src.agents.agent_factory", True),             # AgentFactory().create(role) (slai.bridge.agents)
    ("src.agents.collaborative.shared_memory", False),
    ("src.agents.reader_agent", True),              # the eight roles in slai.bridge.agents.ROLES
    ("src.agents.language_agent", True),
    ("src.agents.privacy_agent", True),
    ("src.agents.safety_agent", True),
    ("src.agents.quality_agent", True),
    ("src.agents.observability_agent", True),
    ("src.agents.handler_agent", True),
    ("src.agents.planning_agent", True),
    ("src.agents.language.lantra_runtime", False),  # LANTRA: needs torch; the agent runs without it ("lightweight mode")
)

# --- DATA RULES -------------------------------------------------------------------------------------------------------------------
DATA_SUFFIXES = {".yaml", ".yml", ".json", ".txt", ".csv", ".j2", ".md"}
CONFIG_DIR_NAMES = {"configs", "config"}                  # <package>/configs/** is read by bind_config()/get_config_section()
SIBLING_SUFFIXES = {".yaml", ".yml", ".json"}             # small data files that sit next to a module that is carried
MAX_SIBLING_BYTES = 1_000_000
SKIP_DIRS = {"__pycache__", "tests", "test", "tests_py", "docs", "doc", "node_modules", ".git", "checkpoints", "venv", ".venv",
             "cache", "sessions", "memory", "artifacts", "evaluation_logs", "benchmarks"}
# Always carried, whether or not code imports them. Paths are relative to the SLAI root. ``required`` False = carry if present.
EXTRA_TREES: tuple[tuple[str, bool], ...] = (
    ("src/agents/language/resources", True),   # packs the language agent loads (language_config.yaml: resource_loading)
    ("src/agents/language/library", True),     # word lists (spell checker, lexicon)
    ("src/agents/language/templates", False),
    ("logs", True),                            # logs.logger and its configuration
)
EXTRA_FILES: tuple[tuple[str, bool], ...] = (
    ("data/bpe_200d_50k_model.json", True),    # LANTRA tokenizer (language_config.yaml: bpe_model_path / bpe_vocab_path)
    ("data/bpe_200d_50k_vocab.json", True),
    ("requirements.txt", False),               # kept for the record; nothing reads it
    ("LICENSE", False),
)

# Packages whose presence under the SLAI root is *code* of this app, not SLAI; never carried.
NEVER_CARRY_TOPS = {"venv", ".venv", "applications", "tests", "docs", "benchmarks", "evaluation_logs"}

STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


# --------------------------------------------------------------------------------------------------------------------------------
class StageError(RuntimeError):
    pass


def module_file(root: Path, module: str) -> Path | None:
    """The source file that implements ``module`` under ``root`` (``a/b.py`` or ``a/b/__init__.py``), or None."""
    parts = module.split(".")
    base = root.joinpath(*parts)
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    init = base / "__init__.py"
    if init.is_file():
        return init
    return None


def is_package_dir(root: Path, module: str) -> bool:
    return root.joinpath(*module.split(".")).is_dir()


def _package_of(module: str, path: Path) -> str:
    """The package that relative imports inside ``module`` are resolved against."""
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _resolve_relative(package: str, level: int, name: str | None) -> str | None:
    if level == 0:
        return name
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (level - 1)]
    if name:
        base = base + name.split(".")
    return ".".join(base) or None


class _Visitor(ast.NodeVisitor):
    """Collect imports with how they are guarded: ``module`` (always run), ``guarded`` (inside try), ``lazy`` (inside a function)."""

    def __init__(self, module: str, package: str) -> None:
        self.module, self.package = module, package
        self.found: list[tuple[str, tuple[str, ...], str]] = []    # (module, names, kind)
        self._func = 0
        self._try = 0

    def _kind(self) -> str:
        return "lazy" if self._func else ("guarded" if self._try else "module")

    def visit_FunctionDef(self, node: ast.AST) -> None:
        self._func += 1
        self.generic_visit(node)
        self._func -= 1

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Try(self, node: ast.Try) -> None:
        self._try += 1
        for child in node.body:
            self.visit(child)
        self._try -= 1
        for part in (node.handlers, node.orelse, node.finalbody):
            for child in part:
                self.visit(child)

    visit_TryStar = visit_Try  # type: ignore[assignment]

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        name = test.attr if isinstance(test, ast.Attribute) else (test.id if isinstance(test, ast.Name) else "")
        if name == "TYPE_CHECKING":                  # typing-only imports never run
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.found.append((alias.name, (), self._kind()))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        target = _resolve_relative(self.package, node.level, node.module)
        if target:
            self.found.append((target, tuple(a.name for a in node.names if a.name != "*"), self._kind()))

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
        if name in {"import_module", "__import__"} and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            literal = node.args[0].value
            if node.args[0].value.startswith(".") and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                literal = (node.args[1].value or "") + literal   # import_module(".x", package=...) - rare; keep best effort
            self.found.append((literal, (), "lazy"))
        self.generic_visit(node)


@functools.lru_cache(maxsize=None)
def parse_imports(path: Path, module: str) -> list[tuple[str, tuple[str, ...], str]]:
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except (SyntaxError, ValueError) as exc:
        raise StageError(f"{path}: cannot parse ({exc}); SLAI source this tool cannot read is not carried silently") from exc
    visitor = _Visitor(module, _package_of(module, path))
    visitor.visit(tree)
    return visitor.found


def closure(root: Path, entries: Iterable[tuple[str, bool]] = ENTRY_MODULES) -> dict:
    """Follow imports from ``entries``.

    Returns ``{"modules": {module: file}, "third_party": {top: {"module": n, "guarded": n, "lazy": n, "by": [..]}}, "missing": [...]}``.
    """
    modules: dict[str, Path] = {}
    third: dict[str, dict] = {}
    missing: list[str] = []
    queue: list[tuple[str, str]] = []

    def want(module: str, by: str) -> None:
        if module and module not in modules and (module, by) not in queue:
            queue.append((module, by))

    for module, required in entries:
        if module_file(root, module) is None:
            if required:
                raise StageError(f"{module} is not in {root}; this does not look like the SLAI checkout this tool was made for")
            missing.append(module)
            continue
        want(module, "<entry>")

    while queue:
        module, by = queue.pop(0)
        if module in modules:
            continue
        path = module_file(root, module)
        if path is None:
            continue
        modules[module] = path
        # python runs every parent package's __init__ first
        parent = module.rpartition(".")[0]
        while parent:
            want(parent, module)
            parent = parent.rpartition(".")[0]
        for target, names, kind in parse_imports(path, module):
            top = target.split(".")[0]
            local = module_file(root, target) is not None or is_package_dir(root, target)
            if local and top not in NEVER_CARRY_TOPS:
                if module_file(root, target) is not None:
                    want(target, module)
                for name in names:                       # ``from pkg import submodule``
                    sub = f"{target}.{name}"
                    if module_file(root, sub) is not None:
                        want(sub, module)
                if not names and "." in target:
                    pass                                    # plain ``import a.b.c``: parents are added above
            elif top and top not in STDLIB and not is_package_dir(root, top) and module_file(root, top) is None:
                info = third.setdefault(top, {"module": 0, "guarded": 0, "lazy": 0, "by": []})
                info[kind] += 1
                if len(info["by"]) < 5 and module not in info["by"]:
                    info["by"].append(module)
    return {"modules": modules, "third_party": third, "missing": missing}


def hard_chains(root: Path, target: str, entries: Iterable[tuple[str, bool]] = ENTRY_MODULES) -> dict[str, list[str]]:
    """For each entry module: the shortest chain of *unguarded, module-level* imports that reaches third-party ``target``, or ``[]``.

    This answers "does importing this agent need torch?" from the source alone: an import inside ``try`` or inside a function does
    not run unless that code path runs, so only module-level statements count. An empty chain means no unconditional path exists.
    """
    edges: dict[str, list[str]] = {}
    hits: set[str] = set()

    def expand(module: str) -> None:
        if module in edges:
            return
        edges[module] = []
        path = module_file(root, module)
        if path is None:
            return
        out: list[str] = []
        parent = module.rpartition(".")[0]
        while parent:
            out.append(parent)
            parent = parent.rpartition(".")[0]
        for tgt, names, kind in parse_imports(path, module):
            if kind != "module":
                continue
            if tgt.split(".")[0] == target:
                hits.add(module)
            if module_file(root, tgt) is not None:
                out.append(tgt)
            for name in names:
                if module_file(root, f"{tgt}.{name}") is not None:
                    out.append(f"{tgt}.{name}")
        edges[module] = out
        for nxt in out:
            expand(nxt)

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))
    result: dict[str, list[str]] = {}
    for entry, _ in entries:
        if module_file(root, entry) is None:
            continue
        expand(entry)
        prev: dict[str, str | None] = {entry: None}
        frontier = [entry]
        found: str | None = None
        while frontier and not found:
            nxt_frontier = []
            for m in frontier:
                if m in hits:
                    found = m
                    break
                for n in edges.get(m, []):
                    if n not in prev:
                        prev[n] = m
                        nxt_frontier.append(n)
            frontier = nxt_frontier
        chain: list[str] = []
        cur = found
        while cur:
            chain.append(cur)
            cur = prev[cur]
        result[entry] = list(reversed(chain)) + ([target] if found else [])
    return result


# --------------------------------------------------------------------------------------------------------------------------------
def _data_files(root: Path, modules: dict[str, Path]) -> list[Path]:
    """Data that sits with the carried code: ``configs/`` directories and small yaml/json next to a carried module."""
    out: set[Path] = set()
    dirs = {p.parent for p in modules.values()}
    for d in dirs:
        for child in d.iterdir():
            if child.is_file() and child.suffix in SIBLING_SUFFIXES and child.stat().st_size <= MAX_SIBLING_BYTES:
                out.add(child)
            elif child.is_dir() and child.name in CONFIG_DIR_NAMES:
                for f in child.rglob("*"):
                    if f.is_file() and f.suffix in DATA_SUFFIXES and not (set(f.relative_to(child).parts[:-1]) & SKIP_DIRS):
                        out.add(f)
    return sorted(out)


def _tree_files(base: Path) -> list[Path]:
    out = []
    for f in sorted(base.rglob("*")):
        rel_parts = set(f.relative_to(base).parts[:-1])
        if f.is_file() and not (rel_parts & (SKIP_DIRS - {"cache"})) and f.suffix not in {".pyc", ".pt", ".pth", ".bin", ".ckpt", ".safetensors"}:
            out.append(f)
    return out


def configured_checkpoint(root: Path) -> str:
    """The LANTRA checkpoint the language agent loads, as written in its own config (``lantra_runtime.checkpoint_path``)."""
    cfg = root / "src/agents/language/configs/language_config.yaml"
    if not cfg.is_file():
        raise StageError(f"{cfg} is missing")
    text = cfg.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"^lantra_runtime:\s*\n((?:[ \t]+.*\n|\s*\n)+)", text, re.M)
    match = re.search(r"^\s+checkpoint_path:\s*['\"]?([^\s'\"#]+)", block.group(1) if block else "", re.M)
    if not match:
        raise StageError("language_config.yaml has no lantra_runtime.checkpoint_path")
    return match.group(1)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_revision(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def selection(root: Path, models: str = "lantra") -> dict:
    """Everything that would be carried: ``{"files": [Path...], "models": [Path...], "closure": {...}}`` (relative to ``root``)."""
    result = closure(root)
    files: set[Path] = {p.relative_to(root) for p in result["modules"].values()}
    files |= {p.relative_to(root) for p in _data_files(root, result["modules"])}
    for rel, required in EXTRA_TREES:
        base = root / rel
        if not base.is_dir():
            if required:
                raise StageError(f"{base} is missing; this does not look like the SLAI checkout this tool was made for")
            continue
        files |= {p.relative_to(root) for p in _tree_files(base)}
    for rel, required in EXTRA_FILES:
        if not (root / rel).is_file():
            if required:
                raise StageError(f"{root / rel} is missing; this does not look like the SLAI checkout this tool was made for")
            continue
        files.add(Path(rel))
    # every package directory needs its __init__.py chain so that ``import src.agents...`` works, also for data-only directories
    models_out: list[Path] = []
    if models == "lantra":
        ckpt = Path(configured_checkpoint(root))
        if ckpt.is_absolute() or ".." in ckpt.parts:
            raise StageError(f"the configured checkpoint path {ckpt} is not inside the SLAI root")
        if not (root / ckpt).is_file():
            raise StageError(f"the LANTRA checkpoint the language agent is configured to load is missing: {root / ckpt}")
        models_out.append(ckpt)
    elif models != "none":
        raise StageError(f"unknown --models value {models!r} (lantra | none)")
    return {"files": sorted(files), "models": models_out, "closure": result}


def stage(root: Path, dest: Path, models: str = "lantra", report: Path | None = None, dry_run: bool = False) -> dict:
    root = root.resolve()
    sel = selection(root, models)
    entries = []
    total = 0
    for rel in sel["files"] + sel["models"]:
        size = (root / rel).stat().st_size
        total += size
        entries.append({"path": rel.as_posix(), "bytes": size, "kind": "model" if rel in sel["models"] else
                        ("code" if rel.suffix == ".py" else "data")})
    third = sel["closure"]["third_party"]
    manifest = {
        "manifestVersion": MANIFEST_VERSION,
        "slaiRevision": git_revision(root),
        "analysedWith": sys.version.split()[0],
        "entryModules": [m for m, _ in ENTRY_MODULES],
        "entryModulesAbsent": sel["closure"]["missing"],
        "modules": len(sel["closure"]["modules"]),
        "files": len(entries),
        "bytes": total,
        "thirdParty": {k: v for k, v in sorted(third.items())},
        "models": [{"path": m.as_posix(), "bytes": (root / m).stat().st_size} for m in sel["models"]],
        "entries": entries,
    }
    if not dry_run:
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        for item in entries:
            src = root / item["path"]
            target = dest / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            item["sha256"] = sha256_of(target)
        # python needs the package chain to exist as packages
        for rel in sel["files"]:
            for parent in rel.parents:
                if parent == Path("."):
                    continue
                init = root / parent / "__init__.py"
                if init.is_file() and not (dest / parent / "__init__.py").is_file():
                    (dest / parent / "__init__.py").write_bytes(init.read_bytes())
                    manifest["entries"].append({"path": (parent / "__init__.py").as_posix(), "bytes": init.stat().st_size,
                                                "kind": "code", "sha256": sha256_of(dest / parent / "__init__.py")})
        manifest["files"] = len(manifest["entries"])
        (dest / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if report:
        report.write_text(json.dumps({k: v for k, v in manifest.items() if k != "entries"} | {"largest": sorted(
            entries, key=lambda e: -e["bytes"])[:25]}, indent=1), encoding="utf-8")
    return manifest


def verify_manifest(dest: Path) -> list[str]:
    """Problems found when comparing a staged folder with its manifest (an empty list means it is exactly what was recorded)."""
    problems: list[str] = []
    try:
        manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{MANIFEST} cannot be read: {exc}"]
    known = {MANIFEST}
    for item in manifest.get("entries", []):
        known.add(item["path"])
        path = dest / item["path"]
        if not path.is_file():
            problems.append(f"missing: {item['path']}")
        elif path.stat().st_size != item["bytes"]:
            problems.append(f"size differs: {item['path']}")
        elif item.get("sha256") and sha256_of(path) != item["sha256"]:
            problems.append(f"checksum differs: {item['path']}")
    for path in dest.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.relative_to(dest).as_posix() not in known:
            problems.append(f"not in manifest: {path.relative_to(dest).as_posix()}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slai-root", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--models", default="lantra", choices=["lantra", "none"])
    ap.add_argument("--report", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="analyse only; copy nothing")
    ap.add_argument("--why", metavar="PACKAGE[,PACKAGE...]",
                    help="print, per entry module, the unguarded import chain that reaches each PACKAGE (e.g. torch)")
    ap.add_argument("--verify", type=Path, help="check an already staged folder against its manifest and exit")
    args = ap.parse_args(argv)
    if args.verify:
        problems = verify_manifest(args.verify)
        for p in problems:
            print(p)
        print("staged folder matches its manifest" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if args.why:
        for package in args.why.split(","):
            chains = hard_chains(args.slai_root.resolve(), package.strip())
            print(f"== {package.strip()}: unguarded path from {sum(1 for c in chains.values() if c)} of {len(chains)} entry modules")
            for entry, chain in chains.items():
                print(f"  {entry}: " + (" -> ".join(chain) if chain else "no unguarded path"))
        return 0
    if not args.dry_run and not args.out:
        ap.error("--out is required unless --dry-run")
    try:
        manifest = stage(args.slai_root, args.out or Path("."), args.models, args.report, args.dry_run)
    except StageError as exc:
        print(f"stage_slai: {exc}", file=sys.stderr)
        return 2
    mb = manifest["bytes"] / 1_048_576
    print(f"{manifest['modules']} modules, {manifest['files']} files, {mb:.1f} MiB"
          f"{' (dry run: nothing copied)' if args.dry_run else f' -> {args.out}'}")
    if manifest["models"]:
        for m in manifest["models"]:
            print(f"model: {m['path']} ({m['bytes'] / 1_048_576:.0f} MiB)")
    if manifest["entryModulesAbsent"]:
        print("optional entry modules not present: " + ", ".join(manifest["entryModulesAbsent"]))
    print("third-party packages imported by the carried code: " + ", ".join(manifest["thirdParty"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
