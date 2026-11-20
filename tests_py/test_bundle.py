"""Staging SLAI and assembling the desktop bundle: ``tools/stage_slai.py`` and ``tools/build_bundle.py``.

These run on a small fake SLAI tree built on the fly (the real one is 15 GB) and a fake ``pip``. They check the *logic* - what is followed,
carried, left out, recorded and verified. They do not download or install anything, and a bundle built from them is not a working app.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

APP_ROOT = Path(__file__).resolve().parent.parent      # no import of ``support``: this file must run with a bare Python (build-windows.cmd)

sys.path.insert(0, str(APP_ROOT / "tools"))
import build_bundle as BB  # noqa: E402
import stage_slai as SS  # noqa: E402


def write(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


CHECKPOINT = "src/agents/language/checkpoints/lantra/lantra_chosen.pt"


def make_fake_slai(root: Path) -> None:
    """The shape of SLAI the tools rely on: every required entry module, a lazy and a literal-dynamic import, a guarded torch, a
    TYPE_CHECKING-only import, config folders, language data, tokenizer files, the model and a second checkpoint that must stay out."""
    files: dict[str, str | bytes] = {
        "LICENSE": "licence",
        "requirements.txt": "torch\n",
        "src/__init__.py": "",
        "src/utils/__init__.py": "",
        "src/utils/sldc.py": "import json\n\ndef pack():\n    from cryptography.fernet import Fernet\n    return Fernet\n",
        "src/agents/__init__.py": "",
        "src/agents/reader/__init__.py": "",
        "src/agents/reader/parser_engine.py": "import yaml\nfrom .utils import helper\nfrom logs.logger import get_logger\n",
        "src/agents/reader/utils.py": "def helper():\n    pass\n",
        "src/agents/reader/detection_engine.py": "",
        "src/agents/reader/integrity_engine.py": "import pypdf\n",
        "src/agents/reader/compression_engine.py": "",
        "src/agents/reader/configs/reader.yaml": "x: 1\n",
        "src/agents/agent_factory.py": (
            "import importlib\nfrom . import base_agent\nfrom .factory.helpers import *\n"
            "def create(role):\n    return importlib.import_module('src.agents.dyn_agent')\n"),
        "src/agents/factory/__init__.py": "",
        "src/agents/factory/helpers.py": "",
        "src/agents/dyn_agent.py": "",
        "src/agents/base_agent.py": (
            "from typing import TYPE_CHECKING\nimport numpy\ntry:\n    import torch\nexcept ImportError:\n    torch = None\n"
            "if TYPE_CHECKING:\n    from .only_for_types import Hint\n"),
        "src/agents/only_for_types.py": "",
        "src/agents/reader_agent.py": "from .base_agent import X\n",
        "src/agents/language_agent.py": "from .language import nlp\ndef init():\n    from .language.lantra_runtime import LantraRuntime\n",
        "src/agents/privacy_agent.py": "",
        "src/agents/safety_agent.py": "def f():\n    import psutil\n",
        "src/agents/quality_agent.py": "",
        "src/agents/observability_agent.py": "",
        "src/agents/handler_agent.py": "",
        "src/agents/planning_agent.py": "",
        "src/agents/unused_agent.py": "import sklearn\n",
        "src/agents/language/__init__.py": "",
        "src/agents/language/nlp.py": "",
        "src/agents/language/lantra_runtime.py": "import torch\nfrom ..model_hub import resolve\n",
        "src/agents/model_hub.py": "def resolve():\n    pass\n",
        "src/agents/language/settings.yaml": "a: 1\n",
        "src/agents/language/configs/language_config.yaml":
            f"other:\n  checkpoint_path: nothing.pt\nlantra_runtime:\n  version: '1.0'\n  checkpoint_path: {CHECKPOINT}\n  device: auto\n",
        "src/agents/language/resources/en/pack.yaml": "p: 1\n",
        "src/agents/language/library/wordlist.json": "[]",
        "src/agents/language/checkpoints/lantra/other_checkpoint.pt": b"x" * 50,
        "src/agents/language/cache/junk.json": "{}",
        CHECKPOINT: b"MODEL" * 1000,
        "logs/__init__.py": "",
        "logs/logger.py": "import colorama\n\ndef get_logger():\n    pass\n",
        "data/bpe_200d_50k_model.json": "{}",
        "data/bpe_200d_50k_vocab.json": "{}",
        "data/raw/huge.txt": "never carried",
        "tests/test_x.py": "",
        "venv/Lib/site-packages/junk.py": "",
    }
    write(root, files)


class StageSlaiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-stage-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "SLAI"
        make_fake_slai(self.root)

    def selected(self, models="lantra") -> set[str]:
        return {p.as_posix() for p in SS.selection(self.root, models)["files"] + SS.selection(self.root, models)["models"]}

    def test_the_closure_follows_every_kind_of_import(self):
        have = set(SS.closure(self.root)["modules"])
        for module in ("src.utils.sldc", "src.agents.reader.utils",                      # relative ``from .utils import helper``
                       "src.agents.base_agent",                                          # ``from . import base_agent``
                       "src.agents.factory.helpers",                                     # ``from .factory.helpers import *``
                       "src.agents.dyn_agent",                                           # importlib.import_module('literal')
                       "src.agents.language.nlp",                                        # ``from .language import nlp``
                       "src.agents.language.lantra_runtime",                             # a lazy import inside a function
                       "src.agents.model_hub",                                           # ``from ..model_hub import``
                       "src.agents.language", "src.agents", "src", "logs.logger", "logs"):
            self.assertIn(module, have, module)

    def test_what_nothing_imports_is_left_out(self):
        have = set(SS.closure(self.root)["modules"])
        self.assertNotIn("src.agents.unused_agent", have)
        self.assertNotIn("src.agents.only_for_types", have, "an import under TYPE_CHECKING never runs")
        picked = self.selected()
        for path in ("src/agents/unused_agent.py", "tests/test_x.py", "venv/Lib/site-packages/junk.py", "data/raw/huge.txt",
                     "src/agents/language/cache/junk.json", "src/agents/language/checkpoints/lantra/other_checkpoint.pt"):
            self.assertNotIn(path, picked, path)

    def test_third_party_packages_are_listed_with_how_they_are_imported(self):
        third = SS.closure(self.root)["third_party"]
        self.assertEqual(sorted(third), ["colorama", "cryptography", "numpy", "psutil", "pypdf", "torch", "yaml"])
        self.assertEqual((third["yaml"]["module"], third["yaml"]["lazy"]), (1, 0))
        self.assertEqual((third["cryptography"]["module"], third["cryptography"]["lazy"]), (0, 1))
        self.assertEqual(third["psutil"]["lazy"], 1)
        self.assertGreaterEqual(third["torch"]["guarded"], 1)
        self.assertNotIn("sklearn", third, "only reached from a module nothing imports")

    def test_data_rules(self):
        picked = self.selected()
        for path in ("src/agents/reader/configs/reader.yaml",                    # a configs/ folder next to carried code
                     "src/agents/language/configs/language_config.yaml",
                     "src/agents/language/settings.yaml",                         # small yaml beside a carried module
                     "src/agents/language/resources/en/pack.yaml", "src/agents/language/library/wordlist.json",
                     "data/bpe_200d_50k_model.json", "data/bpe_200d_50k_vocab.json", "logs/logger.py", "LICENSE", CHECKPOINT):
            self.assertIn(path, picked, path)

    def test_only_the_configured_checkpoint_is_carried_and_none_on_request(self):
        sel = SS.selection(self.root, "lantra")
        self.assertEqual([p.as_posix() for p in sel["models"]], [CHECKPOINT])
        self.assertEqual(SS.selection(self.root, "none")["models"], [])
        self.assertNotIn(CHECKPOINT, self.selected("none"))

    def test_a_missing_checkpoint_or_entry_stops_the_build(self):
        (self.root / CHECKPOINT).unlink()
        with self.assertRaisesRegex(SS.StageError, "checkpoint .* is missing"):
            SS.selection(self.root, "lantra")
        SS.selection(self.root, "none")                      # not asked for, not needed
        (self.root / "src/agents/privacy_agent.py").unlink()
        with self.assertRaisesRegex(SS.StageError, "privacy_agent"):
            SS.closure(self.root)

    def test_a_checkpoint_path_pointing_outside_the_root_is_refused(self):
        cfg = self.root / "src/agents/language/configs/language_config.yaml"
        cfg.write_text(cfg.read_text().replace(CHECKPOINT, "../../secret.pt"))
        with self.assertRaisesRegex(SS.StageError, "not inside the SLAI root"):
            SS.selection(self.root, "lantra")

    def test_unreadable_source_is_an_error_not_a_silent_gap(self):
        (self.root / "src/agents/reader/utils.py").write_text("def broken(:\n")
        with self.assertRaisesRegex(SS.StageError, "cannot parse"):
            SS.closure(self.root)

    def test_stage_copies_records_and_verifies(self):
        dest = self.tmp / "out"
        manifest = SS.stage(self.root, dest)
        self.assertTrue((dest / "src/utils/sldc.py").is_file())
        self.assertEqual((dest / CHECKPOINT).read_bytes(), (self.root / CHECKPOINT).read_bytes())
        self.assertEqual(manifest["models"], [{"path": CHECKPOINT, "bytes": 5000}])
        self.assertEqual(SS.verify_manifest(dest), [])
        listed = {e["path"]: e for e in json.loads((dest / SS.MANIFEST).read_text())["entries"]}
        self.assertEqual(listed[CHECKPOINT]["sha256"], hashlib.sha256(b"MODEL" * 1000).hexdigest())
        self.assertEqual(listed[CHECKPOINT]["kind"], "model")
        self.assertEqual(listed["src/utils/sldc.py"]["kind"], "code")
        self.assertEqual(listed["data/bpe_200d_50k_model.json"]["kind"], "data")
        self.assertEqual(manifest["files"], len(listed))

    def test_the_source_is_copied_unchanged(self):
        dest = self.tmp / "out"
        SS.stage(self.root, dest)
        for rel in ("src/utils/sldc.py", "src/agents/agent_factory.py", "logs/logger.py"):
            self.assertEqual((dest / rel).read_bytes(), (self.root / rel).read_bytes(), rel)

    def test_dry_run_changes_nothing(self):
        dest = self.tmp / "never"
        SS.stage(self.root, dest, dry_run=True, report=self.tmp / "r.json")
        self.assertFalse(dest.exists())
        self.assertIn("largest", json.loads((self.tmp / "r.json").read_text()))

    def test_verify_notices_edits_deletions_and_additions(self):
        dest = self.tmp / "out"
        SS.stage(self.root, dest)
        (dest / "src/utils/sldc.py").write_text("tampered, same length? no")
        (dest / "src/agents/dyn_agent.py").unlink()
        (dest / "src/agents/extra.py").write_text("")
        problems = "\n".join(SS.verify_manifest(dest))
        self.assertIn("src/utils/sldc.py", problems)
        self.assertIn("missing: src/agents/dyn_agent.py", problems)
        self.assertIn("not in manifest: src/agents/extra.py", problems)

    def test_same_size_different_content_is_caught_by_the_checksum(self):
        dest = self.tmp / "out"
        SS.stage(self.root, dest)
        (dest / CHECKPOINT).write_bytes(b"EDOLM" * 1000)
        self.assertEqual(SS.verify_manifest(dest), [f"checksum differs: {CHECKPOINT}"])

    def test_hard_chains_answer_does_importing_need_torch(self):
        chains = SS.hard_chains(self.root, "torch")
        self.assertEqual(chains["src.agents.language.lantra_runtime"], ["src.agents.language.lantra_runtime", "torch"])
        self.assertEqual(chains["src.agents.language_agent"], [], "its lantra import is lazy, so importing the agent does not need torch")
        self.assertEqual(chains["src.agents.reader_agent"], [], "base_agent imports torch inside try/except only")
        self.assertEqual(SS.hard_chains(self.root, "yaml")["src.agents.reader.parser_engine"],
                         ["src.agents.reader.parser_engine", "yaml"])

    def test_command_line(self):
        out = self.tmp / "cli"
        done = subprocess.run([sys.executable, str(APP_ROOT / "tools" / "stage_slai.py"), "--slai-root", str(self.root), "--out", str(out)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("model: " + CHECKPOINT, done.stdout)
        self.assertEqual(subprocess.run([sys.executable, str(APP_ROOT / "tools" / "stage_slai.py"), "--slai-root", str(self.root),
                                         "--verify", str(out)], capture_output=True).returncode, 0)
        bad = subprocess.run([sys.executable, str(APP_ROOT / "tools" / "stage_slai.py"), "--slai-root", str(self.tmp / "nowhere"),
                              "--out", str(self.tmp / "x")], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("does not look like", bad.stderr)


# ------------------------------------------------------------------------------------------------------------------ the bundle
class FakePip:
    """Stands in for ``pip``: for each requirement writes the ``*.dist-info`` pip would, so the builder's bookkeeping runs for real."""

    def __init__(self, compiled_in_pure: bool = False, fail_pure: bool = False, fail_main: bool = False):
        self.calls: list[list[str]] = []
        self.compiled_in_pure, self.fail_pure, self.fail_main = compiled_in_pure, fail_pure, fail_main

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        target = Path(cmd[cmd.index("--target") + 1])
        requirements = Path(cmd[cmd.index("-r") + 1])
        pure = "--no-deps" in cmd
        if (pure and self.fail_pure) or (not pure and self.fail_main):
            return SimpleNamespace(returncode=1)
        for line in requirements.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "==" in line:
                name, version = line.split("==")
                (target / f"{name.replace('-', '_')}-{version}.dist-info").mkdir(parents=True, exist_ok=True)
                (target / f"{name.replace('-', '_')}-{version}.dist-info" / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
                (target / name.replace("-", "_").lower()).mkdir(exist_ok=True)
                (target / name.replace("-", "_").lower() / "__init__.py").write_text("")
                if pure and self.compiled_in_pure:
                    (target / name.lower() / "_speedups.pyd").write_bytes(b"MZ")
        (target / "Scripts").mkdir(exist_ok=True)
        (target / "Scripts" / "tool.exe").write_bytes(b"MZ")
        return SimpleNamespace(returncode=0)


def fake_python_zip(path: Path, with_dll: bool = True) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("python.exe", b"MZ")
        if with_dll:
            zf.writestr("python313.dll", b"MZ")
        zf.writestr("python313.zip", b"PK")
        zf.writestr("python313._pth", "python313.zip\n.\n#import site\n")


class PythonRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-py-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.zip = self.tmp / "embed.zip"
        fake_python_zip(self.zip)
        self.lock_path = self.tmp / "lock.json"
        self.lock = {"python": {"version": "3.13.14", "url": "https://example.invalid/python-3.13.14-embed-amd64.zip", "sha256": "",
                                "pip_platform": "win_amd64", "pip_python_version": "3.13", "pip_abi": "cp313"}}

    def test_first_download_needs_explicit_trust_and_is_then_recorded(self):
        with self.assertRaisesRegex(BB.BuildError, r"(?s)no SHA-256 .*--trust-first-download"):
            BB.fetch_python(self.lock, self.tmp, self.zip, False, self.lock_path)
        self.assertFalse(self.lock_path.exists())
        got = BB.fetch_python(self.lock, self.tmp, self.zip, True, self.lock_path)
        self.assertEqual(got, self.zip)
        digest = hashlib.sha256(self.zip.read_bytes()).hexdigest()
        self.assertEqual(json.loads(self.lock_path.read_text())["python"]["sha256"], digest)

    def test_a_recorded_hash_is_enforced(self):
        self.lock["python"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(BB.BuildError, "Not using it"):
            BB.fetch_python(self.lock, self.tmp, self.zip, True, self.lock_path)
        self.lock["python"]["sha256"] = hashlib.sha256(self.zip.read_bytes()).hexdigest().upper()
        self.assertEqual(BB.fetch_python(self.lock, self.tmp, self.zip, False, self.lock_path), self.zip)

    def test_download_is_used_and_a_bad_download_is_deleted(self):
        def fake_download(url: str, target: Path) -> None:
            target.write_bytes(b"not the zip")
        self.lock["python"]["sha256"] = hashlib.sha256(self.zip.read_bytes()).hexdigest()
        with self.assertRaises(BB.BuildError):
            BB.fetch_python(self.lock, self.tmp, None, False, self.lock_path, download=fake_download)
        self.assertFalse((self.tmp / "python-3.13.14-embed-amd64.zip").exists(), "a download that fails its check is not kept for next time")

    def test_extract_points_the_search_path_at_the_bundle_only(self):
        dest = self.tmp / "python"
        self.assertEqual(BB.extract_python(self.zip, dest), "python313")
        pth = (dest / "python313._pth").read_text()
        self.assertEqual(pth.splitlines(), ["python313.zip", ".", "../site-packages", "../app"])
        self.assertNotIn("import site", pth)

    def test_a_zip_that_is_not_an_embeddable_python_is_refused(self):
        bad = self.tmp / "bad.zip"
        fake_python_zip(bad, with_dll=False)
        with self.assertRaisesRegex(BB.BuildError, "does not look like an embeddable"):
            BB.extract_python(bad, self.tmp / "p")
        evil = self.tmp / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escape.txt", "x")
        with self.assertRaisesRegex(BB.BuildError, "unsafe path"):
            BB.extract_python(evil, self.tmp / "p2")

    def test_pip_commands(self):
        py = self.lock["python"]
        main = BB.pip_command(Path("T"), Path("R"), py, pure=False, python="PY")
        self.assertEqual(main[:4], ["PY", "-m", "pip", "install"])
        for flag in ("--only-binary=:all:", "win_amd64", "3.13", "cp313", "--target", "T"):
            self.assertIn(flag, main)
        pure = BB.pip_command(Path("T"), Path("R"), py, pure=True)
        self.assertIn("--no-deps", pure)
        self.assertNotIn("--only-binary=:all:", pure)

    def test_requirement_files_pin_everything_and_name_what_the_agents_need(self):
        names = BB.requirement_names(BB.REQUIREMENTS)
        for needed in ("torch", "torchvision", "librosa", "einops", "rotary-embedding-torch", "matplotlib", "psutil", "scipy", "scikit-learn"):
            self.assertIn(needed, names)
        for path in (BB.REQUIREMENTS, BB.REQUIREMENTS_PURE):
            for line in path.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    self.assertRegex(line, r"^[A-Za-z0-9_.\-]+==[0-9][^\s]*$", f"{path.name}: every package is pinned to one version: {line}")
        self.assertIn("torch==2.15.0", BB.REQUIREMENTS.read_text())
        self.assertNotIn("huggingface", "\n".join(names), "the model hub stays out: models load from the bundle only")


class BuildBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-bundle-build-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.slai = self.tmp / "SLAI"
        make_fake_slai(self.slai)
        self.zip = self.tmp / "embed.zip"
        fake_python_zip(self.zip)
        self.out = self.tmp / "engine"
        self.lock_backup = BB.LOCK.read_text()
        self.addCleanup(BB.LOCK.write_text, self.lock_backup)       # --trust-first-download writes the real lock file; put it back

    def args(self, **over) -> argparse.Namespace:
        base = dict(slai_root=self.slai, out=self.out, cache=self.tmp / "cache", models="lantra", python_embed=self.zip,
                    trust_first_download=True, helper=None, skip=None, no_compile=True, dry_run=False, allow_non_windows=True)
        base.update(over)
        return argparse.Namespace(**base)

    def test_a_full_build_is_complete_and_records_what_it_holds(self):
        pip = FakePip()
        record = BB.build(self.args(), pip)
        self.assertEqual(BB.verify(self.out), [])
        self.assertEqual(json.loads((self.out / "bundle.json").read_text()), record)
        self.assertEqual(record["documasterVersion"], BB.documaster_version())
        self.assertEqual(record["python"], "3.13.14")
        self.assertEqual(record["slai"]["models"], [CHECKPOINT])
        self.assertEqual(record["packages"]["torch"], "2.15.0")
        self.assertEqual(sorted(record["optionalPurePackages"]), ["gputil", "metaphone"])
        self.assertEqual(record["sizes"]["total"], sum(v for k, v in record["sizes"].items() if k != "total"))
        for part in ("python/python.exe", "app/run_documaster.py", "app/slai/engine.py", "client/index.html", "slai-root/slai_bundle.json",
                     CHECKPOINT.join(["slai-root/", ""])):
            self.assertTrue((self.out / part).is_file(), part)
        self.assertFalse((self.out / "site-packages" / "Scripts").exists(), "launchers for the build machine's Python are removed")
        self.assertFalse(list((self.out / "app").rglob("__pycache__")))
        self.assertTrue(any("--only-binary=:all:" in c for c in pip.calls) and any("--no-deps" in c for c in pip.calls))

    def test_bundle_json_is_written_last_so_a_failed_build_is_not_a_bundle(self):
        BB.build(self.args(), FakePip())
        self.assertTrue((self.out / "bundle.json").is_file())
        with self.assertRaises(BB.BuildError):
            BB.build(self.args(), FakePip(fail_main=True))
        self.assertFalse((self.out / "bundle.json").exists(), "the previous bundle.json is removed before anything else changes")
        self.assertTrue(BB.verify(self.out))

    def test_pure_packages_that_contain_compiled_code_are_refused(self):
        with self.assertRaisesRegex(BB.BuildError, "compiled files"):
            BB.build(self.args(), FakePip(compiled_in_pure=True))

    def test_optional_pure_packages_failing_only_warns(self):
        record = BB.build(self.args(), FakePip(fail_pure=True))
        self.assertEqual(record["optionalPurePackages"], {})
        self.assertEqual(BB.verify(self.out), [])

    def test_dry_run_checks_inputs_and_changes_nothing(self):
        self.assertEqual(BB.build(self.args(dry_run=True), FakePip()), {})
        self.assertFalse(self.out.exists())
        with self.assertRaisesRegex(BB.BuildError, "not an SLAI checkout"):
            BB.build(self.args(dry_run=True, slai_root=self.tmp / "empty"), FakePip())
        (self.slai / CHECKPOINT).unlink()
        with self.assertRaises(SS.StageError):
            BB.build(self.args(dry_run=True), FakePip())

    def test_a_bundle_without_a_model_is_only_acceptable_when_asked_for(self):
        BB.build(self.args(models="none"), FakePip())
        self.assertEqual(BB.verify(self.out), [])
        manifest = json.loads((self.out / "bundle.json").read_text())
        manifest["modelsRequired"] = True
        (self.out / "bundle.json").write_text(json.dumps(manifest))
        self.assertTrue(any("carries no model" in p for p in BB.verify(self.out)))

    def test_verify_finds_each_kind_of_damage(self):
        BB.build(self.args(), FakePip())
        problems = lambda: "\n".join(BB.verify(self.out))  # noqa: E731
        (self.out / "slai-root" / CHECKPOINT).write_bytes(b"short")
        self.assertIn(f"slai-root: size differs: {CHECKPOINT}", problems())
        (self.out / "slai-root" / CHECKPOINT).unlink()
        self.assertIn("the model", problems())
        shutil.rmtree(self.out / "site-packages" / "torch-2.15.0.dist-info")
        self.assertIn("site-packages has no torch", problems())
        (self.out / "python" / "python313._pth").write_text("python313.zip\n.\nimport site\n")
        self.assertIn("does not list ../site-packages", problems())
        (self.out / "client" / "index.html").unlink()
        self.assertIn("client/index.html is missing", problems())
        (self.out / "bundle.json").unlink()
        self.assertIn("the bundle was not finished", problems())

    def test_helpers_are_copied_by_name_and_recorded(self):
        tool = self.tmp / "qpdf-folder"
        write(tool, {"bin/qpdf.exe": "MZ", "README": "x"})
        record = BB.build(self.args(helper=[f"qpdf={tool}"]), FakePip())
        self.assertTrue((self.out / "helpers" / "qpdf" / "bin" / "qpdf.exe").is_file())
        self.assertEqual(list(record["helpers"]), ["qpdf"])
        for bad in ("noequals", f"BAD NAME={tool}", f"x={self.tmp / 'missing'}"):
            with self.assertRaisesRegex(BB.BuildError, "--helper expects"):
                BB.stage_helpers(self.tmp / "h", [bad])

    def test_the_app_is_copied_without_caches_or_tests(self):
        BB.build(self.args(), FakePip())
        app = self.out / "app"
        self.assertTrue((app / "slai" / "docmodel").is_dir())
        self.assertFalse(list(app.rglob("*.pyc")))
        self.assertFalse((app / "tests_py").exists())

    def test_non_windows_is_refused_unless_staging_only(self):
        if sys.platform == "win32":
            self.skipTest("this is the platform the build is for")
        with self.assertRaisesRegex(BB.BuildError, "build on Windows"):
            BB.build(self.args(allow_non_windows=False), FakePip())


class VersionsAgreeTests(unittest.TestCase):
    def test_every_place_that_states_the_version_states_the_same_one(self):
        import re
        want = BB.documaster_version()
        self.assertEqual(json.loads((APP_ROOT / "package.json").read_text())["version"], want)
        self.assertEqual(json.loads((APP_ROOT / "src-tauri" / "tauri.conf.json").read_text())["version"], want)
        self.assertRegex((APP_ROOT / "src-tauri" / "Cargo.toml").read_text(), rf'(?m)^version = "{re.escape(want)}"$')

    def test_the_shell_config_matches_what_the_bundle_provides(self):
        conf = json.loads((APP_ROOT / "src-tauri" / "tauri.conf.json").read_text())
        windows = json.loads((APP_ROOT / "src-tauri" / "tauri.windows.conf.json").read_text())
        self.assertEqual(windows["bundle"]["resources"], {"engine/": "engine/"}, "the folder tools/build_bundle.py fills")
        self.assertEqual(BB.DEFAULT_OUT, APP_ROOT / "src-tauri" / "engine")
        self.assertEqual(windows["bundle"]["targets"], ["nsis"], "per-user install only: the agents write beside SLAI, so no Program Files")
        self.assertEqual(conf["bundle"]["windows"]["nsis"]["installMode"], "currentUser")
        self.assertFalse(conf["app"]["windows"][0]["dragDropEnabled"], "otherwise WebView2 never delivers dropped files to the page")
        caps = json.loads((APP_ROOT / "src-tauri" / "capabilities" / "default.json").read_text())
        self.assertEqual(caps["permissions"], [], "the engine's page never calls Tauri, so it gets no permissions")

    def test_the_shell_launches_the_same_script_and_port_the_bundle_and_engine_use(self):
        rust = (APP_ROOT / "src-tauri" / "src" / "desktop.rs").read_text()
        self.assertIn('"python"', rust)
        self.assertIn('"run_documaster.py"', rust)
        self.assertIn('join("app")', rust)
        self.assertTrue((APP_ROOT / "run_documaster.py").is_file())
        for flag in ("--ready-line", "--exit-on-stdin-close", "--port-fallback"):
            self.assertIn(flag, (APP_ROOT / "src-tauri" / "src" / "protocol.rs").read_text())


if __name__ == "__main__":
    unittest.main()
