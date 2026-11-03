"""Repository layout and safe historical-document relocation contracts.

Keep user-facing launchers, packaging attribution and install manifests stable.
"""
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]

ROOT_CONTRACTS=(
    "README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
    "ACKNOWLEDGMENTS.md", "THIRD_PARTY_PROVENANCE.json",
    "Dockerfile", "docker-compose.yml", "app.py", "setup.py",
    "pyproject.toml", "requirements.txt", "start-macos.sh",
    "launch-windows.ps1", "update_windows.bat", "Odysseus.spec",
)
RELOCATED={
    "ROADMAP.md":"docs/archive/odysseus/ROADMAP.md",
    "PUBLICATION_ASSET_DECISIONS.md":
      "docs/archive/odysseus/publication/PUBLICATION_ASSET_DECISIONS.md",
    "THREAT_MODEL.md":"docs/security/THREAT_MODEL.md",
    "build-macos-app.sh":"scripts/build/build-macos-app.sh",
    "build-windows-portable.ps1":"scripts/build/build-windows-portable.ps1",
    "install-service.sh":"scripts/legacy/odysseus/install-service.sh",
    "odysseus-ui.service":"scripts/legacy/odysseus/odysseus-ui.service",
    "docs/AGENT_TURN_CONTRACT.md":"docs/architecture/agents/AGENT_TURN_CONTRACT.md",
    "docs/BACKGROUND_TOOL_JOBS.md":"docs/architecture/agents/BACKGROUND_TOOL_JOBS.md",
    "docs/chromadb-volume-migration.md":
      "docs/deployment/migrations/chromadb-volume-migration.md",
    "docs/tls-trust.md":"docs/security/tls-trust.md",
    "docs/searxng-trust-image.md":"docs/security/searxng-trust-image.md",
}
MARKDOWN_LINK=re.compile(r"!?\[[^\]]*\]\(([^)\n]+)\)")

def test_required_root_contracts_remain_available():
    assert all((ROOT/path).is_file() for path in ROOT_CONTRACTS)

def test_reorganized_documents_and_scripts_exist_at_documented_paths():
    for previous,current in RELOCATED.items():
        assert not (ROOT/previous).exists(), previous
        assert (ROOT/current).is_file(), current

def test_current_docs_index_and_historical_boundary():
    index=(ROOT/"docs/README.md").read_text()
    readme=(ROOT/"README.md").read_text()
    legacy=(ROOT/"docs/archive/odysseus/ROADMAP.md").read_text()
    current=(ROOT/"docs/ROADMAP.md").read_text()
    assert "docs/README.md" in readme
    assert "docs/archive/odysseus/ROADMAP.md" in readme
    assert "Odysseus is on a voyage" in legacy
    assert "CLI" in current and "Gateway" in current
    assert "historical" in index.lower()

def test_relocated_markdown_links_point_to_real_files():
    paths=[
        ROOT/"README.md",
        ROOT/"docs/README.md",
        ROOT/"docs/architecture/README.md",
        ROOT/"docs/security/README.md",
        ROOT/"docs/archive/odysseus/README.md",
        ROOT/"docs/deployment/migrations/README.md",
        ROOT/"scripts/build/README.md",
        ROOT/"scripts/legacy/odysseus/README.md",
    ]
    paths += list((ROOT/"docs/archive/odysseus").rglob("*.md"))
    paths += list((ROOT/"docs/architecture").rglob("*.md"))
    paths += list((ROOT/"docs/security").rglob("*.md"))
    paths += list((ROOT/"docs/deployment/migrations").rglob("*.md"))
    for path in paths:
        for link in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
            if link.startswith(("https:","http:","mailto:","#","//","data:","/")):
                continue
            relative=link.split("#",1)[0].split("?",1)[0]
            if not relative or " " in relative:
                continue
            assert (path.parent/relative).resolve().exists(), (path.relative_to(ROOT),relative)

def test_moved_distribution_scripts_still_locate_repository_root():
    mac=(ROOT/"scripts/build/build-macos-app.sh").read_text()
    windows=(ROOT/"scripts/build/build-windows-portable.ps1").read_text()
    assert ')/../.." && pwd)' in mac
    assert 'Join-Path $PSScriptRoot "../.."' in windows
    assert "Set-Location -Path $RepoRoot" in windows
    assert '$RepoRoot\\dist\\Odysseus' in windows
    assert 'THIRD_PARTY_PROVENANCE.json' in mac and 'ACKNOWLEDGMENTS.md' in mac
    assert 'THIRD_PARTY_PROVENANCE.json' in windows and 'ACKNOWLEDGMENTS.md' in windows

def test_legacy_systemd_install_not_used_by_current_preview():
    installer=(ROOT/"scripts/legacy/odysseus/install-service.sh").read_text()
    assert 'SERVICE_FILE="$SCRIPT_DIR/odysseus-ui.service"' in installer
    assert (ROOT/"scripts/legacy/odysseus/odysseus-ui.service").exists()
    assert "Do not execute" in (ROOT/"scripts/legacy/odysseus/README.md").read_text()

def test_github_feature_requests_link_to_mnerys_roadmap():
    template=(ROOT/".github/ISSUE_TEMPLATE/feature_request.yml").read_text()
    assert "https://github.com/isyuricunha/mnerys/blob/main/docs/ROADMAP.md" in template
