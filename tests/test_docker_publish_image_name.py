"""Execute the publication workflow's image-name setup without contacting GHCR."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "docker-publish.yml"
)


@pytest.mark.skipif(shutil.which("bash") is None, reason="Docker publication requires Bash")
@pytest.mark.parametrize("job", ["build", "merge"])
@pytest.mark.parametrize(
    ("repository", "expected"),
    [
        ("RaresKeY/odysseus-dev", "rareskey/odysseus-dev"),
        ("ExampleOwner/Example-Repo", "exampleowner/example-repo"),
        ("odysseus-dev/odysseus", "odysseus-dev/odysseus"),
    ],
)
def test_image_name_is_lowercase_before_docker_steps(tmp_path, job, repository, expected):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    image_name = workflow["env"].get("IMAGE_NAME", "").replace(
        "${{ github.repository }}", repository
    )
    github_env = tmp_path / "github-env"
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_REPOSITORY": repository,
        "GITHUB_ENV": str(github_env),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "GITHUB_SHA": "a" * 40,
    }

    # Exercise the exact name-normalization step in isolation. Versioning steps
    # have Actions-injected env (RELEASE_TAG) and their own release tests;
    # replaying those without their step env does not test image naming.
    steps = workflow["jobs"][job]["steps"]
    normalize_indexes = [
        index for index, step in enumerate(steps)
        if step.get("name") == "Normalize image name"
    ]
    assert len(normalize_indexes) == 1
    index = normalize_indexes[0]
    docker_indexes = [
        i for i, step in enumerate(steps)
        if step.get("uses", "").startswith("docker/")
    ]
    assert docker_indexes and index < min(docker_indexes)
    normalize = steps[index]
    assert "run" in normalize
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", normalize["run"]],
        cwd=WORKFLOW.parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert github_env.exists()
    env.update(line.split("=", 1) for line in github_env.read_text().splitlines())
    image_name = env.get("IMAGE_NAME", image_name)
    assert image_name == expected
