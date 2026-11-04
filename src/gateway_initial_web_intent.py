"""Pure early public-Web and browser intent classification for the Gateway.

This classifier runs before session admission, on untrusted request text. Its
output is only a routing hint and must never be treated as an executable tool
permission. Existing admitted account/owner policy, workspace validation and
the hardened dispatcher remain authoritative. Reuses the inherited lexical
rules without broadening URL, browser or personal-data matching.
"""

from dataclasses import dataclass
import re
from typing import Any, Callable

from src.turn_contract import requires_external_web_verification


def _is_personal_data_search_without_web_target(text: str) -> bool:
    """Prevent generic ``search`` wording from disabling personal tools."""
    text = str(text or "")
    if not re.search(
        r"\b(?:memory|memories|remembered|recall|brain|prior\s+chats?|"
        r"previous\s+chats?|past\s+conversations?|previous\s+conversations?|"
        r"chat\s+history|sessions?|notes?|todos?|tasks?|skills?|documents?|docs?|"
        r"calendar|events?|meetings?|appointments?|schedule|emails?|inbox|contacts?)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    return not re.search(
        r"\b(?:web|internet|online|google|news|weather|website|url|"
        r"browse|browser)\b",
        text,
        re.IGNORECASE,
    )


def _explicitly_denies_web_lookup(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:no\s+web|do\s+not\s+search|don'?t\s+search|without\s+looking\s+it\s+up|"
            r"without\s+searching|answer\s+from\s+memory\s+only|from\s+memory|"
            r"no\s+tools?|do\s+not\s+use\s+(?:any\s+)?tools?|don'?t\s+use\s+(?:any\s+)?tools?)\b",
            str(text or "").lower(),
        )
    )


_EXPLICIT_URL_TARGET = re.compile(
    r"\bhttps?://\S+|(?<![/\\@])\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?",
    re.IGNORECASE,
)


def _contains_explicit_url_target(text: str) -> bool:
    """Recognize public URLs/domains without treating local paths as domains."""
    return bool(_EXPLICIT_URL_TARGET.search(str(text or "")))


def _authorizes_exact_url_fetch(text: str) -> bool:
    """Treat a pasted public URL as authority to read that URL, not search.

    The Web toggle controls open-ended discovery.  A concrete URL is already
    the user's chosen network target, so reading it does not need the broader
    search grant. Interactive navigation remains owned by ``private_browser``;
    YouTube links remain owned by ``youtube_tool``.
    """
    value = str(text or "")
    if _explicitly_denies_web_lookup(value) or _is_explicit_browser_automation_request(value):
        return False
    urls = re.findall(r"\bhttps?://[^\s<>\"']+", value, re.IGNORECASE)
    return any(
        not re.match(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)(?:/|$)", url, re.IGNORECASE)
        for url in urls
    )


def _is_explicit_browser_automation_request(text: str) -> bool:
    """Distinguish interactive navigation from ordinary URL/PDF retrieval."""
    return bool(re.search(
        r"\b(brow(?:ser|esr|sr)|browse|visit|go\s+to|navigate\s+to|"
        r"open\s+(?:the\s+)?(?:site|page|url|link)|click|fill(?:\s+out)?|"
        r"submit|send\s+(?:the\s+)?form|contact\s+form|form\s+submission)\b",
        str(text or ""),
        re.IGNORECASE,
    ))


def _is_external_discovery_request(text: str) -> bool:
    """Recognize requests to locate an authoritative public web source."""
    return bool(re.search(
        r"\b(?:find|locate|get)\s+(?:me\s+)?(?:the\s+|an?\s+)?"
        r"(?:official\s+)?(?:announcement|press\s+release|article|source|web\s*page|website|site)\b",
        str(text or ""),
        re.IGNORECASE,
    ))


@dataclass(frozen=True)
class InitialWebIntent:
    """Unprivileged lexical routing hints; no grants or session access."""

    explicit_web_intent: bool
    personal_store_intent: bool
    explicit_web_target: bool
    exact_url_fetch_intent: bool
    explicit_browser_intent: bool
    external_discovery_intent: bool
    explicit_private_browser_intent: bool
    local_browser_render_intent: bool
    allow_browser_for_web_turn: bool


def classify_initial_web_intent(
    *,
    message: Any,
    search_enabled: bool,
    workspace_bound: bool,
    local_browser_render_lookup: Callable[[], bool],
) -> InitialWebIntent:
    """Preserve the Web route's original early intent and rendering order.

    The callback is a *server-owned* pure browser-render necessity check
    supplied by Web. It is only consulted for string messages with a
    validated nonempty workspace, and is not an execution grant.
    """
    if type(search_enabled) is not bool or type(workspace_bound) is not bool:
        raise TypeError("Invalid preliminary Web routing flag")
    if not callable(local_browser_render_lookup):
        raise TypeError("Missing preliminary browser-render resolver")

    explicit_web = False
    personal = False
    web_target = False
    exact_url = False
    browser = False
    external = False
    private = False
    local_render = False

    if isinstance(message, str):
        msg_l = message.lower()
        url_target = _contains_explicit_url_target(msg_l)
        personal = _is_personal_data_search_without_web_target(msg_l)
        web_target = bool(re.search(
            r"\b(?:web|internet|online|google|news|weather|website|url|browse|browser)\b",
            msg_l,
        )) or url_target
        explicit_web = (
            url_target
            or bool(re.search(
                r"\b(search|look\s+(?:(?:this|that|it|them|these|those)\s*)?up|lookup|find\s*out|google|browse|web|online|latest|current|today|news|weather|forecast|rate|exchange\s+rate)\b",
                msg_l,
            ))
            or requires_external_web_verification(message)
        ) and (not personal or web_target)
        browser = _is_explicit_browser_automation_request(msg_l)
        external = _is_external_discovery_request(msg_l)
        if external:
            explicit_web = True
        exact_url = _authorizes_exact_url_fetch(msg_l)
        private = bool(re.search(
            r"\bprivate[_ -]?brow(?:ser|esr|sr)\b", msg_l,
        )) or bool(re.search(
            r"\bagent\s+unsubscribe\b.*\bhttps?://",
            msg_l,
            re.DOTALL,
        ))
        if private:
            browser = True
            # An exact browser flow must not be downgraded to open search.
            explicit_web = False
        if workspace_bound:
            local_render = local_browser_render_lookup()
            if type(local_render) is not bool:
                raise TypeError("Invalid server-owned browser rendering evidence")

    return InitialWebIntent(
        explicit_web_intent=explicit_web,
        personal_store_intent=personal,
        explicit_web_target=web_target,
        exact_url_fetch_intent=exact_url,
        explicit_browser_intent=browser,
        external_discovery_intent=external,
        explicit_private_browser_intent=private,
        local_browser_render_intent=local_render,
        allow_browser_for_web_turn=bool(
            browser or local_render
            or (explicit_web and not personal)
            or search_enabled
        ),
    )
