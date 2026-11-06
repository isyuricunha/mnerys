"""Static website truthfulness: M2 complete, future clients planned.

The landing page must never claim the Gateway or Agent Core are planned,
must present the private preview as private, and must keep CLI, TUI and
Telegram clearly planned.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INDEX = (REPO / "website" / "index.html").read_text(encoding="utf-8")
_ONE_LINE = " ".join(INDEX.split())


def test_website_presents_m2_as_complete():
    assert "M2, M3 and M4 complete" in _ONE_LINE
    assert 'milestone-state done">Complete' in INDEX
    # M1 through M4 are the completed milestone cards.
    assert INDEX.count('milestone-state done">Complete') == 4
    assert INDEX.count("In progress") == 0
    assert "First-party TUI" in _ONE_LINE


def test_website_marks_cli_and_telegram_accurately():
    # The CLI milestone is complete, not planned; TUI runs on M4.
    assert "Global CLI" in _ONE_LINE
    assert "(remote mode)" in _ONE_LINE or "remote mode" in _ONE_LINE
    assert "CLI + TUI shipped" in _ONE_LINE
    assert "M4 acceptance evidence" in _ONE_LINE
    assert "Telegram" in _ONE_LINE


def test_website_no_longer_claims_the_gateway_is_planned():
    assert "CENTRAL AUTHORITY / IMPLEMENTED" in _ONE_LINE
    assert "SHARED RUNTIME / IMPLEMENTED" in _ONE_LINE
    assert "not shipped yet" not in _ONE_LINE
    assert "Gateway will own" not in _ONE_LINE
    assert "independent runtime and clients are still being built" not in _ONE_LINE


def test_website_keeps_the_private_preview_private():
    assert "private preview" in _ONE_LINE
    assert "publicly accessible" not in _ONE_LINE
    assert "public hosted" not in _ONE_LINE
    assert "sign up" not in _ONE_LINE
    assert "self-hosted" in _ONE_LINE


def test_website_roadmap_links_still_resolve():
    for fragment in (
        "docs/architecture/M2_3_DURABLE_TURNS.md",
        "docs/architecture/M2_5_ACCEPTANCE.md",
        "docs/ROADMAP.md",
        "docs/mascot/README.md",
    ):
        assert fragment in INDEX, fragment
        target = REPO / fragment
        assert target.is_file(), fragment