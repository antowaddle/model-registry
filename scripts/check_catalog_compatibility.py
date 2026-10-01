#!/usr/bin/env python3
"""Check Model Catalog v1 compatibility with oasdiff and explicit Git baselines."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


OASDIFF_VERSION = "1.33.0"
SPEC_PATH = "api/openapi/catalog-v1.yaml"
PATH_PATTERN = r"^/api/model_catalog/v1(/|$)"
SEVERITY_OVERRIDES = {"response-optional-property-removed": "warn"}


class CheckError(Exception):
    """The comparison could not be completed reliably."""


def command(args, *, cwd, env=None, check=True):
    try:
        result = subprocess.run(
            [str(arg) for arg in args], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"Cannot run {args[0]}: {error}") from error
    if check and result.returncode:
        raise CheckError(f"{args[0]} failed: {result.stderr or result.stdout}")
    return result


def tool_path(name, explicit, repo):
    if explicit:
        candidate = shutil.which(explicit) or explicit
    elif (repo / "bin" / name).is_file():
        candidate = repo / "bin" / name
    else:
        candidate = shutil.which(name)
    if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
        raise CheckError(f"Missing executable {name}; see docs/model-catalog-compatibility.md")
    return Path(candidate).resolve()


def verify_oasdiff(binary, cwd):
    version = command([binary, "--version"], cwd=cwd).stdout.strip()
    if version == f"oasdiff version {OASDIFF_VERSION}" or version == f"oasdiff version v{OASDIFF_VERSION}":
        return version
    # go install leaves build.Version as 'main'. Check its embedded module
    # version instead, rather than accepting an unversioned development build.
    build_info = command(["go", "version", "-m", binary], cwd=cwd).stdout
    for line in build_info.splitlines():
        fields = line.split()
        if fields[:3] == ["mod", "github.com/oasdiff/oasdiff", f"v{OASDIFF_VERSION}"]:
            return f"{version}; module v{OASDIFF_VERSION}"
    raise CheckError(f"Expected oasdiff v{OASDIFF_VERSION}, got {version!r}")


def resolve_commit(repo, ref):
    result = command(
        ["git", "--no-replace-objects", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=repo, check=False,
    )
    sha = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise CheckError(f"Cannot resolve baseline revision {ref!r}; fetch the trusted revision first")
    return sha


def generate_candidate(repo, yq, destination):
    # Use the existing bundler, but never rewrite the developer's committed
    # bundle. Copy only its declared source inputs into a private workspace.
    with tempfile.TemporaryDirectory(prefix="catalog-v1-generation-") as temporary:
        workspace = Path(temporary)
        (workspace / "scripts").mkdir()
        shutil.copyfile(repo / "scripts/merge_catalog_specs.sh", workspace / "scripts/merge_catalog_specs.sh")
        source = repo / "api/openapi/src"
        target = workspace / "api/openapi/src"
        (target / "lib").mkdir(parents=True)
        (target / "plugins").mkdir()
        inputs = [source / "catalog-v1.yaml", *sorted((source / "lib").glob("*.yaml")),
                  *sorted((source / "plugins").glob("*-v1.yaml"))]
        for path in inputs:
            shutil.copyfile(path, target / path.relative_to(source))
        environment = dict(os.environ, YQ=str(yq))
        command(["bash", "scripts/merge_catalog_specs.sh", "catalog-v1.yaml"], cwd=workspace, env=environment)
        shutil.copyfile(workspace / SPEC_PATH, destination)


def spec_has_catalog_paths(yq, path, cwd):
    parsed = json.loads(command([yq, "eval", "-o=json", ".", path], cwd=cwd).stdout)
    paths = parsed.get("paths") if isinstance(parsed, dict) else None
    return isinstance(paths, dict) and any(
        isinstance(name, str) and re.match(PATH_PATTERN, name) for name in paths
    )


def findings(result, operation):
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CheckError(f"oasdiff {operation} returned invalid JSON: {result.stderr or result.stdout}") from error
    if parsed is None:
        parsed = []
    if not isinstance(parsed, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str)
        or not isinstance(item.get("text"), str) or not isinstance(item.get("level"), int)
        for item in parsed
    ):
        raise CheckError(f"oasdiff {operation} returned malformed findings")
    if result.returncode not in (0, 1) or (result.returncode == 1 and not parsed):
        raise CheckError(f"oasdiff {operation} failed: {result.stderr or result.stdout}")
    return parsed


def validate_spec(binary, policy, path, cwd):
    result = command(
        [binary, "validate", "--config", policy, "--allow-external-refs=false",
         "--fail-on", "ERR", "--format", "json", path], cwd=cwd, check=False,
    )
    records = findings(result, "validate")
    if result.returncode:
        raise CheckError(f"Invalid OpenAPI contract {path.name}: {json.dumps(records)}")
    return records


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_compatibility(args, output, report):
    repo = args.repo.resolve()
    oasdiff = tool_path("oasdiff", args.oasdiff, repo)
    yq = tool_path("yq", args.yq, repo)
    report["tool_build"] = verify_oasdiff(oasdiff, output)
    # Explicit empty policy prevents PR-local .oasdiff files or environment
    # config from silently suppressing findings. No uploads/external refs.
    policy = output / "oasdiff-policy.json"
    policy.write_text("{}\n", encoding="utf-8")
    levels = output / "oasdiff-levels.txt"
    levels.write_text("".join(f"{rule} {level}\n" for rule, level in SEVERITY_OVERRIDES.items()), encoding="utf-8")
    report["policy"] = {
        "config_file": policy.name, "config_sha256": digest(policy),
        "severity_file": levels.name, "severity_sha256": digest(levels),
        "severity_overrides": SEVERITY_OVERRIDES, "fail_on": "WARN",
    }
    for ref in args.baseline_ref:
        sha = resolve_commit(repo, ref)
        baseline = output / f"baseline-{sha}.yaml"
        raw = command(["git", "--no-replace-objects", "show", f"{sha}:{SPEC_PATH}"], cwd=repo).stdout
        baseline.write_text(raw, encoding="utf-8")
        if not spec_has_catalog_paths(yq, baseline, output):
            raise CheckError(f"Baseline {ref!r} has no Model Catalog v1 paths; wrong API/version")
        validation = validate_spec(oasdiff, policy, baseline, output)
        report["baselines"].append({
            "ref": ref, "sha": sha, "file": baseline.name,
            "sha256": digest(baseline), "validation_findings": validation,
        })

    candidate = output / "candidate.yaml"
    generate_candidate(repo, yq, candidate)
    report["candidate"] = {
        "head": resolve_commit(repo, "HEAD"), "file": candidate.name, "sha256": digest(candidate),
    }
    if candidate.read_bytes() != (repo / SPEC_PATH).read_bytes():
        raise CheckError("Stale committed api/openapi/catalog-v1.yaml; run make api/openapi/catalog-v1.yaml")
    report["candidate"]["validation_findings"] = validate_spec(oasdiff, policy, candidate, output)

    breaking = False
    for baseline in report["baselines"]:
        result = command(
            [oasdiff, "breaking", "--config", policy, "--allow-external-refs=false",
             "--severity-levels", levels,
             "--flatten-allof", "--match-path", PATH_PATTERN, "--fail-on", "WARN",
             "--format", "json", output / baseline["file"], candidate], cwd=output, check=False,
        )
        baseline["findings"] = findings(result, "breaking")
        baseline["compatible"] = result.returncode == 0
        breaking = breaking or not baseline["compatible"]
    report["status"] = "breaking" if breaking else "compatible"
    return 1 if breaking and args.enforce else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-ref", action="append", required=True,
                        help="Trusted Git revision; repeat for every supported baseline. Never defaults to HEAD.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New report directory; existing paths are never overwritten")
    parser.add_argument("--enforce", action="store_true", help="Exit 1 for breaking changes; default is report-only")
    parser.add_argument("--oasdiff", default=os.environ.get("OASDIFF"), help="Pinned oasdiff executable")
    parser.add_argument("--yq", default=os.environ.get("YQ"), help="Repository bundler's yq executable")
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(f"Cannot create new output directory: {error}", file=sys.stderr)
        return 2
    report = {
        "status": "error", "mode": "enforce" if args.enforce else "report-only",
        "oasdiff_version": OASDIFF_VERSION, "path_pattern": PATH_PATTERN,
        "baselines": [], "errors": [],
    }
    try:
        code = check_compatibility(args, output, report)
    except (CheckError, OSError, ValueError) as error:
        report["errors"].append(str(error))
        print(str(error), file=sys.stderr)
        code = 2
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Model Catalog v1: {report['status']} ({report['mode']}); report: {output / 'report.json'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
