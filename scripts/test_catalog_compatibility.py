"""Regression tests using the real Catalog sources, bundler, Git, yq, and oasdiff."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("catalog_compatibility", ROOT / "scripts/check_catalog_compatibility.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class TestCatalogCompatibility:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        # Missing dependencies are failures, not skips that could conceal a
        # check that never ran. Tools can be supplied through OASDIFF and YQ.
        self.oasdiff = runner.tool_path("oasdiff", os.environ.get("OASDIFF"), ROOT)
        self.yq = runner.tool_path("yq", os.environ.get("YQ"), ROOT)
        self.work = tmp_path
        self.repo = self.work / "provider"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copytree(ROOT / "api/openapi/src", self.repo / "api/openapi/src")
        shutil.copyfile(ROOT / runner.SPEC_PATH, self.repo / runner.SPEC_PATH)
        shutil.copyfile(ROOT / "scripts/merge_catalog_specs.sh", self.repo / "scripts/merge_catalog_specs.sh")
        self.git("init", "--initial-branch=main")
        self.git("add", "api", "scripts")
        self.commit("Accepted Catalog baseline")
        self.baseline = self.git("rev-parse", "HEAD").stdout.strip()
        self.calls = 0

    def git(self, *args):
        environment = dict(os.environ,
                           GIT_AUTHOR_NAME="Contract Tests", GIT_COMMITTER_NAME="Contract Tests",
                           GIT_AUTHOR_EMAIL="contract-tests@example.invalid", GIT_COMMITTER_EMAIL="contract-tests@example.invalid")
        return subprocess.run(["git", *args], cwd=self.repo, env=environment,
                              capture_output=True, text=True, check=True)

    def commit(self, message):
        self.git("-c", "commit.gpgsign=false", "commit", "-m", message)

    def mutate(self, expression, relative="api/openapi/src/plugins/model-v1.yaml"):
        runner.command([self.yq, "eval", "-i", expression, self.repo / relative], cwd=self.repo)

    def regenerate(self):
        runner.generate_candidate(self.repo, self.yq, self.repo / runner.SPEC_PATH)

    def check(self, refs=None, enforce=True, extra=()):
        self.calls += 1
        output = self.work / f"report-{self.calls}"
        args = ["--repo", str(self.repo), "--oasdiff", str(self.oasdiff), "--yq", str(self.yq), "--output-dir", str(output)]
        for ref in refs if refs is not None else [self.baseline]:
            args.extend(["--baseline-ref", ref])
        if enforce:
            args.append("--enforce")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/check_catalog_compatibility.py"), *args, *extra],
                                capture_output=True, text=True, timeout=120)
        assert (output / "report.json").is_file(), result.stderr
        return result, json.loads((output / "report.json").read_text()), output

    def assert_breaking(self, expression, check_id):
        self.mutate(expression)
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 1, result.stdout + result.stderr
        assert report["status"] == "breaking"
        assert any(check_id in item["id"] for item in report["baselines"][0]["findings"]), report

    def test_unchanged_contract_and_saved_baseline(self):
        before = (self.repo / runner.SPEC_PATH).read_bytes()
        result, report, output = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"
        baseline = report["baselines"][0]
        assert baseline["sha"] == self.baseline
        assert (output / baseline["file"]).read_bytes() == before
        assert baseline["sha256"] == report["candidate"]["sha256"]
        assert (self.repo / runner.SPEC_PATH).read_bytes() == before

    def test_optional_response_field_is_compatible(self):
        self.mutate('.components.schemas.CatalogModel.allOf[0].properties.supportUrl = {"type": "string"}')
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"

    def test_removed_operation_is_breaking(self):
        self.assert_breaking('del(.paths."/api/model_catalog/v1/models".get)', "api-removed")

    def test_removed_response_field_is_breaking(self):
        self.assert_breaking('del(.components.schemas.CatalogModel.allOf[0].properties.sourceId)', "response-optional-property-removed")

    def test_changed_response_type_is_breaking(self):
        self.assert_breaking('.components.schemas.CatalogModel.allOf[0].properties.sourceId.type = "integer"', "response-property-type-changed")

    def test_new_required_input_is_breaking(self):
        self.assert_breaking('.paths."/api/model_catalog/v1/models".get.parameters += [{"name": "tenant", "in": "query", "required": true, "schema": {"type": "string"}}]', "required")

    def test_report_only_still_records_break(self):
        self.mutate('del(.components.schemas.CatalogModel.allOf[0].properties.sourceId)')
        self.regenerate()
        result, report, _ = self.check(enforce=False)
        assert result.returncode == 0, result.stderr
        assert report["status"] == "breaking"
        assert report["baselines"][0]["findings"]

    def test_stale_bundle_fails_even_in_report_only_mode(self):
        self.mutate('.components.schemas.CatalogModel.allOf[0].properties.supportUrl = {"type": "string"}')
        before = (self.repo / runner.SPEC_PATH).read_bytes()
        result, report, _ = self.check(enforce=False)
        assert result.returncode == 2
        assert report["status"] == "error"
        assert "Stale committed" in report["errors"][0]
        assert (self.repo / runner.SPEC_PATH).read_bytes() == before

    def test_bundle_only_edit_also_fails_freshness(self):
        self.mutate('.info.description = "Edited generated bundle"', runner.SPEC_PATH)
        result, report, _ = self.check()
        assert result.returncode == 2
        assert "Stale committed" in report["errors"][0]

    def test_committing_source_and_bundle_cannot_move_pinned_baseline(self):
        self.mutate('del(.components.schemas.CatalogModel.allOf[0].properties.sourceId)')
        self.regenerate()
        self.git("add", "api")
        self.commit("Candidate changes source and generated contract together")
        result, report, _ = self.check()
        assert result.returncode == 1, result.stderr
        assert report["baselines"][0]["sha"] == self.baseline
        assert report["candidate"]["head"] != self.baseline

    def test_every_baseline_is_checked(self):
        self.mutate('del(.components.schemas.CatalogModel.allOf[0].properties.sourceId)')
        self.regenerate()
        self.git("add", "api")
        self.commit("Another baseline")
        newer = self.git("rev-parse", "HEAD").stdout.strip()
        result, report, _ = self.check(refs=[newer, self.baseline])
        assert result.returncode == 1, result.stderr
        assert report["baselines"][0]["compatible"]
        assert not report["baselines"][1]["compatible"]

    def test_missing_baseline_fails_in_report_only_mode(self):
        result, report, _ = self.check(refs=["missing-release"], enforce=False)
        assert result.returncode == 2
        assert "Cannot resolve baseline" in report["errors"][0]

    def test_missing_spec_in_baseline_fails(self):
        self.git("rm", runner.SPEC_PATH)
        self.commit("Baseline missing its contract")
        ref = self.git("rev-parse", "HEAD").stdout.strip()
        shutil.copyfile(ROOT / runner.SPEC_PATH, self.repo / runner.SPEC_PATH)
        result, report, _ = self.check(refs=[ref])
        assert result.returncode == 2
        assert report["status"] == "error"

    def test_wrong_api_version_baseline_fails(self):
        shutil.copyfile(ROOT / "api/openapi/catalog.yaml", self.repo / runner.SPEC_PATH)
        self.git("add", "api")
        self.commit("Alpha contract is not a v1 baseline")
        ref = self.git("rev-parse", "HEAD").stdout.strip()
        self.regenerate()
        result, report, _ = self.check(refs=[ref])
        assert result.returncode == 2
        assert "wrong API/version" in report["errors"][0]

    def test_malformed_source_fails_generation(self):
        (self.repo / "api/openapi/src/plugins/model-v1.yaml").write_text("paths: [\n")
        result, report, _ = self.check(enforce=False)
        assert result.returncode == 2
        assert report["status"] == "error"

    def test_unresolved_reference_fails_validation(self):
        self.mutate('.components.schemas.CatalogModel.allOf[1]."$ref" = "#/components/schemas/DoesNotExist"')
        self.regenerate()
        result, report, _ = self.check(enforce=False)
        assert result.returncode == 2
        assert report["status"] == "error"

    def test_other_plugin_operation_is_outside_model_catalog_scope(self):
        relative = "api/openapi/src/plugins/mcp-v1.yaml"
        self.mutate('del(.paths."/api/mcp_catalog/v1/mcp_servers".get)', relative)
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"
        assert report["baselines"][0]["sha256"] != report["candidate"]["sha256"]

    def test_local_oasdiff_configuration_cannot_suppress_findings(self):
        (self.repo / ".oasdiff.yaml").write_text('match-path: "^/nothing$"\n')
        self.assert_breaking('del(.components.schemas.CatalogModel.allOf[0].properties.sourceId)', "response-optional-property-removed")

    def test_missing_tool_is_not_a_skipped_check(self):
        result, report, _ = self.check(extra=["--oasdiff", str(self.work / "missing-oasdiff")])
        assert result.returncode == 2
        assert "Missing executable oasdiff" in report["errors"][0]

    def test_existing_report_directory_is_not_overwritten(self):
        _, _, output = self.check()
        before = (output / "report.json").read_bytes()
        result = subprocess.run([sys.executable, str(ROOT / "scripts/check_catalog_compatibility.py"),
                                 "--baseline-ref", self.baseline, "--output-dir", str(output)],
                                capture_output=True, text=True)
        assert result.returncode == 2
        assert (output / "report.json").read_bytes() == before

    @pytest.mark.parametrize("code,data", [(102, "[]"), (1, "[]"), (0, "not-json"),
                                          (0, "{}"), (0, '[{"id": "incomplete"}]')])
    def test_tool_failure_or_malformed_output_is_not_compatibility_success(self, code, data):
        result = subprocess.CompletedProcess([], code, data, "tool error")
        with pytest.raises(runner.CheckError):
            runner.findings(result, "breaking")

    def test_wrong_tool_version_is_rejected(self):
        with mock.patch.object(runner, "command", side_effect=[
            subprocess.CompletedProcess([], 0, "oasdiff version 1.0.0\n", ""),
            subprocess.CompletedProcess([], 0, "\tmod github.com/oasdiff/oasdiff v1.0.0 hash\n", ""),
        ]):
            with pytest.raises(runner.CheckError):
                runner.verify_oasdiff(self.oasdiff, self.work)
