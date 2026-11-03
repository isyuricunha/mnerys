"""Release workflow contract: safety, initial version, tags and CI gating."""
from pathlib import Path
import json
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str):
    data = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    return data, data.get("on", data.get(True))


def test_release_is_post_ci_and_trusted_main_only():
    doc, events = _workflow("release.yml")
    assert list(events) == ["workflow_run"]
    assert events["workflow_run"]["workflows"] == ["CI"]
    assert events["workflow_run"]["types"] == ["completed"]
    assert events["workflow_run"]["branches"] == ["main"]
    job = doc["jobs"]["release"]
    gate = job["if"]
    for required in ("conclusion == 'success'", "event == 'push'", "head_branch == 'main'",
                     "head_repository.full_name == github.repository"):
        assert required in gate
    assert job["permissions"] == {"contents": "write"}
    assert doc["permissions"] == {}
    assert doc["concurrency"]["cancel-in-progress"] is False
    steps = job["steps"]
    checkout = next(s for s in steps if s["name"] == "Check out the exact validated commit")
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False
    assert "head_sha" in checkout["with"]["ref"]
    script = next(s["run"] for s in steps if "Bootstrap" in s["name"])
    assert "git fetch origin --tags" in script
    assert "gh release create v0.1.0" in script
    assert "npx --no-install semantic-release" in script
    assert "docs/releases/0.1.0.md" in script
    assert "git tag --points-at" in script
    assert "gh release view" in script
    assert "git ls-remote origin refs/heads/main" in steps[1]["run"]
    images = doc["jobs"]["images"]
    assert images["needs"] == "release"
    assert images["permissions"] == {"contents": "read", "packages": "write"}


def test_docker_release_tags_are_derived_from_verified_semver():
    doc, events = _workflow("docker-publish.yml")
    assert list(events) == ["workflow_call", "push"]
    assert events["push"]["branches"] == ["dev"]
    assert events["workflow_call"]["inputs"]["release_tag"]["required"] is True
    for job in ("build", "merge"):
        checkout = next(s for s in doc["jobs"][job]["steps"] if "Check out" in s["name"])
        assert "inputs.release_tag" in checkout["with"]["ref"]
        assert checkout["with"]["persist-credentials"] is False
    merge = doc["jobs"]["merge"]
    metadata = next(s for s in merge["steps"] if s.get("id") == "metadata")
    tags = metadata["with"]["tags"]
    for token in ("value=latest", "value=dev", "inputs.release_tag",
                  "steps.version.outputs.series", "steps.version.outputs.short"):
        assert token in tags
    assert "APP_VERSION" not in (ROOT / ".github/workflows/docker-publish.yml").read_text()
    builder = next(s for s in doc["jobs"]["build"]["steps"] if s.get("id") == "build")
    assert "MNERYS_RELEASE_VERSION" in builder["with"]["build-args"]
    assert "MNERYS_RELEASE_VERSION" in (ROOT / "Dockerfile").read_text()


def test_version_is_release_defined_and_initial_manifest_is_clear():
    source = (ROOT / "src/constants.py").read_text()
    assert 'os.getenv("MNERYS_RELEASE_VERSION", "0.1.0")' in source
    doc = (ROOT / "docs/releases/0.1.0.md").read_text()
    assert "Mnerys v0.1.0" in doc
    assert "SQLite" in doc and "PostgreSQL 17" in doc
    assert "planned" in doc.lower()
    assert "CLI" in doc and "TUI" in doc
    policy = json.loads((ROOT / ".releaserc.json").read_text())
    assert policy["tagFormat"] == "v${version}"
    package = json.loads((ROOT / "package.json").read_text())
    assert package["private"] is True
    assert package["devDependencies"]["semantic-release"] == "25.0.9"
    assert package["devDependencies"]["conventional-changelog-conventionalcommits"] == "10.4.1"
    assert "@semantic-release/npm" not in json.dumps(policy)


def test_release_tooling_is_pinned_and_all_actions_pinned_to_sha():
    for name in ("release.yml", "docker-publish.yml"):
        doc, _ = _workflow(name)
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses", "")
                if action:
                    assert re.search(r"@[a-f0-9]{40}$", action), (name, action)
