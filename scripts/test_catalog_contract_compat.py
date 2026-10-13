"""Regression tests proving the real Catalog sources, bundler, and
.rhoai/contracts.yaml descriptor integrate correctly with the generalized
openapi-compat engine.

The engine itself (baseline resolution, oasdiff invocation, severity
policy, adapter dispatch) is generic and has its own provider-agnostic test
suite in the central repository
(modules/openapi-compat/tests/test_checker.py). This file only needs to
prove that *this repository's* descriptor and bundler produce the expected
result when run through that engine — it does not re-derive every generic
edge case.

Requires the shared `red-hat-data-services/contract-tests` repository
checked out locally, with its path given via the CONTRACT_TESTS environment
variable. A missing or misconfigured checkout is a failure, not a skip, so a
check that never ran cannot be mistaken for a pass.

Run with:
  CONTRACT_TESTS=/path/to/contract-tests \
    python3 -m pytest scripts/test_catalog_contract_compat.py -v
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _central_repo():
    value = os.environ.get("CONTRACT_TESTS")
    if not value:
        raise RuntimeError(
            "Set CONTRACT_TESTS to a local checkout of "
            "red-hat-data-services/contract-tests."
        )
    central = Path(value).resolve()
    cli = central / "cmd" / "contract-tests" / "cli.py"
    if not cli.is_file():
        raise RuntimeError(f"{cli} not found; CONTRACT_TESTS={central} does not look like the shared repository")
    return central, cli


CENTRAL_REPO, CLI = _central_repo()

spec = importlib.util.spec_from_file_location("openapi_compat_checker", CENTRAL_REPO / "modules/openapi-compat/checker.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class TestCatalogContractCompat:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.oasdiff = runner.tool_path("oasdiff", os.environ.get("OASDIFF"), ROOT)
        self.yq = runner.tool_path("yq", os.environ.get("YQ"), ROOT)
        self.work = tmp_path
        self.repo = self.work / "provider"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / ".rhoai").mkdir()
        shutil.copytree(ROOT / "api/openapi/src", self.repo / "api/openapi/src")
        shutil.copyfile(ROOT / "api/openapi/catalog-v1.yaml", self.repo / "api/openapi/catalog-v1.yaml")
        shutil.copyfile(ROOT / "api/openapi/catalog.yaml", self.repo / "api/openapi/catalog.yaml")
        shutil.copyfile(ROOT / "scripts/merge_catalog_specs.sh", self.repo / "scripts/merge_catalog_specs.sh")
        shutil.copyfile(ROOT / ".rhoai/contracts.yaml", self.repo / ".rhoai/contracts.yaml")
        self.git("init", "--initial-branch=main")
        self.git("add", "api", "scripts", ".rhoai")
        self.commit("Accepted Catalog baseline")
        self.baseline = self.git("rev-parse", "HEAD").stdout.strip()
        self.calls = 0

    def git(self, *args):
        environment = dict(
            os.environ,
            GIT_AUTHOR_NAME="Contract Tests",
            GIT_COMMITTER_NAME="Contract Tests",
            GIT_AUTHOR_EMAIL="contract-tests@example.invalid",
            GIT_COMMITTER_EMAIL="contract-tests@example.invalid",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
        )
        return subprocess.run(["git", *args], cwd=self.repo, env=environment,
                              capture_output=True, text=True, check=True)

    def commit(self, message):
        self.git("-c", "commit.gpgsign=false", "commit", "-m", message)

    def mutate(self, expression, relative="api/openapi/src/plugins/model-v1.yaml"):
        runner.command([self.yq, "eval", "-i", expression, self.repo / relative], cwd=self.repo)

    def regenerate(self):
        runner.adapters.generate(
            "openapi.bundle.yq-merge", self.repo,
            {"script": "scripts/merge_catalog_specs.sh", "sourceDir": "api/openapi/src",
             "sourceGlobs": ["catalog-v1.yaml", "lib/*.yaml", "plugins/*-v1.yaml"],
             "entrypointArg": "catalog-v1.yaml", "output": "api/openapi/catalog-v1.yaml", "yqEnvVar": "YQ"},
            self.yq, self.repo / "api/openapi/catalog-v1.yaml",
        )

    def check(self, refs=None, enforce=True, extra=()):
        self.calls += 1
        output = self.work / f"report-{self.calls}"
        args = ["--repo", str(self.repo), "--contract-set", ".rhoai/contracts.yaml",
                "--oasdiff", str(self.oasdiff), "--yq", str(self.yq), "--output-dir", str(output)]
        for ref in refs if refs is not None else [self.baseline]:
            args.extend(["--baseline-ref", ref])
        if enforce:
            args.append("--enforce")
        result = subprocess.run([sys.executable, str(CLI), "check", *args, *extra],
                                capture_output=True, text=True, timeout=120)
        assert (output / "report.json").is_file(), result.stderr
        return result, json.loads((output / "report.json").read_text()), output

    def test_unchanged_contract_is_compatible(self):
        result, report, _ = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"
        assert report["contract_id"] == "model-catalog-v1"

    def test_optional_response_field_is_compatible(self):
        self.mutate('.components.schemas.CatalogModel.allOf[0].properties.supportUrl = {"type": "string"}')
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"

    def test_removed_operation_is_breaking(self):
        self.mutate('del(.paths."/api/model_catalog/v1/models".get)')
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 1, result.stdout + result.stderr
        assert report["status"] == "breaking"
        assert any("api-removed" in item["id"] for item in report["baselines"][0]["findings"])

    def test_removed_optional_response_field_is_breaking_via_severity_override(self):
        self.mutate('del(.components.schemas.CatalogModel.allOf[0].properties.sourceId)')
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 1, result.stdout + result.stderr
        assert any("response-optional-property-removed" in item["id"] for item in report["baselines"][0]["findings"])

    def test_stale_bundle_fails_even_in_report_only_mode(self):
        self.mutate('.components.schemas.CatalogModel.allOf[0].properties.supportUrl = {"type": "string"}')
        before = (self.repo / "api/openapi/catalog-v1.yaml").read_bytes()
        result, report, _ = self.check(enforce=False)
        assert result.returncode == 2
        assert report["status"] == "error"
        assert "Stale committed" in report["errors"][0]
        assert (self.repo / "api/openapi/catalog-v1.yaml").read_bytes() == before

    def test_other_plugin_operation_is_outside_model_catalog_scope(self):
        self.mutate('del(.paths."/api/mcp_catalog/v1/mcp_servers".get)', relative="api/openapi/src/plugins/mcp-v1.yaml")
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"
        assert report["baselines"][0]["sha256"] != report["candidate"]["sha256"]

    def test_missing_baseline_fails_in_report_only_mode(self):
        result, report, _ = self.check(refs=["0" * 40], enforce=False)
        assert result.returncode == 2
        assert "Cannot resolve baseline" in report["errors"][0]

    def test_wrong_api_version_baseline_fails(self):
        shutil.copyfile(ROOT / "api/openapi/catalog.yaml", self.repo / "api/openapi/catalog-v1.yaml")
        self.git("add", "api")
        self.commit("Alpha contract is not a v1 baseline")
        ref = self.git("rev-parse", "HEAD").stdout.strip()
        self.regenerate()
        result, report, _ = self.check(refs=[ref])
        assert result.returncode == 2
        assert "wrong API/version" in report["errors"][0]
