"""Shared Mnerys chat-context composition for trusted server adapters.

Only explicitly injected ports perform reads or writes. HTTP authentication and
Gateway admission must happen before this internal function is invoked.
"""
import logging
from typing import Callable

from src.gateway_chat_context_rules import (
    _is_casual_low_signal,
    _truthy_request_flag,
    _session_is_research_spinoff,
    _PERSONAL_TOOL_CONTEXT_RE,
)
from src.gateway_chat_context_types import ChatContext
from src.gateway_context_ports import ChatContextPorts
from src.prompt_security import untrusted_context_message

logger = logging.getLogger(__name__)

async def assemble_chat_context(
    sess,
    user: str | None,
    chat_handler,
    chat_processor,
    message: str,
    session_id: str,
    selected_notes_context: str | None = None,
    preset_id=None,
    att_ids: list = None,
    use_web=None,
    use_rag=None,
    use_research=None,
    time_filter=None,
    incognito: bool = False,
    no_memory: bool = False,
    search_context: str = None,
    compare_mode: bool = False,
    use_enhanced_message: bool = False,
    agent_mode: bool = False,
    allow_tool_preprocessing: bool = True,
    defer_context_shaping: bool = False,
    continuation_context_message: str | None = None,
    persist_user_message: bool = True,
    interaction_mode: str | None = None,
    auto_escalated: bool = False,
    context_resolution=None,
    on_user_message_persisted: Callable[[], None] | None = None,
    ports: ChatContextPorts | None = None,
    require_text_user_content: bool = False,
) -> ChatContext:
    """Build the full context (preface + messages) for an LLM call.

    This is the shared logic between /chat and /chat_stream — preset extraction,
    message preprocessing, memory/RAG/web injection, compaction, normalization.

    ``context_resolution`` is the turn's already resolved context window. When
    supplied, history shaping sizes against it instead of probing the endpoint.
    """
    # External reads/writes are supplied by a trusted service adapter.
    # No alternative storage, context or agent engine is introduced here.
    if ports is None:
        raise ValueError("Trusted ChatContextPorts must be provided by the caller")

    # Preset
    preset = ports.extract_preset(chat_handler, preset_id)

    # Preprocess message (CoT, YouTube, VL images, build content). The
    # auto_opened_docs collector captures any docs created server-side
    # (e.g. fillable PDF → markdown editor doc) so the chat route can
    # announce them to the frontend before streaming.
    auto_opened_docs: list = []
    preprocessed = await ports.preprocess_message(
        chat_handler, message, att_ids or [], sess,
        auto_opened_docs=auto_opened_docs,
        allow_tool_preprocessing=allow_tool_preprocessing,
    )

    # The direct persisted text profile must not append a multimodal user
    # record before discovering that its format is unsupported.
    if require_text_user_content and not isinstance(preprocessed.user_content, str):
        raise ValueError("Persisted direct turns require text-only preprocessing")

    # Add user message to history. Nobody/incognito uses a request-local
    # transcript store instead of session history so stale saved chats cannot
    # bleed into context and the turn is not persisted.
    if persist_user_message and incognito:
        user_meta = {}
        if preprocessed.attachment_meta:
            user_meta["attachments"] = preprocessed.attachment_meta
        if interaction_mode in {"chat", "agent", "research"}:
            user_meta["interaction_mode"] = interaction_mode
        if auto_escalated:
            user_meta["auto_escalated"] = True
        ports.append_private_message(session_id, "user", preprocessed.user_content, user_meta)
    elif persist_user_message:
        ports.append_user_message(
            sess,
            chat_handler,
            preprocessed,
            incognito=False,
            interaction_mode=interaction_mode,
            auto_escalated=auto_escalated,
        )

    # Emit events through a transport callback only after message persistence.
    if persist_user_message and not incognito and on_user_message_persisted is not None:
        on_user_message_persisted()

    # Owner comes from the adapter's trusted, admitted principal.
    # Core never reads HTTP credentials or client-selected owner fields.
    uprefs = ports.load_preferences(user)
    uploaded_files = ports.upload_manifest(
        att_ids or [],
        getattr(chat_handler, "upload_handler", None),
        getattr(sess, "owner", None),
    )
    context_message = (
        str(continuation_context_message).strip()
        if continuation_context_message
        else message
    )
    casual_low_signal = _is_casual_low_signal(context_message)

    # Memory enabled?
    mem_enabled = (
        not incognito
        and not no_memory
        and uprefs.get("memory_enabled", True)
        and getattr(sess, "memory_injection_enabled", True) is not False
    )
    # Skills injection respects its own enable toggle (mirrors memory_enabled).
    # When off, the "Available skills" index is not added to the prompt.
    skills_enabled = (
        not incognito
        and uprefs.get("skills_enabled", True)
        and getattr(sess, "skill_injection_enabled", True) is not False
    )
    if not allow_tool_preprocessing:
        mem_enabled = False
        skills_enabled = False
    if casual_low_signal:
        mem_enabled = False
        skills_enabled = False
    logger.debug(
        "Memory enabled=%s for user=%s (incognito=%s, no_memory=%s, pref=%s)",
        mem_enabled, user, incognito, no_memory, uprefs.get("memory_enabled", "NOT_SET"),
    )

    # Research-spinoff ("Discuss") sessions are grounded on the seeded report:
    # the primer system message IS the knowledge base. Injecting global memory
    # or personal-doc RAG on every turn pulls in keyword-matched but off-topic
    # facts ("wrong data") and competes with the report, so suppress both here.
    is_research_spinoff = _session_is_research_spinoff(sess)
    if is_research_spinoff:
        mem_enabled = False

    # Use RAG?
    use_rag_val = (str(use_rag).lower() != "false") if use_rag is not None else True
    if incognito or not allow_tool_preprocessing or is_research_spinoff or casual_low_signal:
        use_rag_val = False

    use_web_val = _truthy_request_flag(use_web)
    # If pre-fetched search context was provided (compare mode), skip live web
    # search. Personal app requests should be served by their tools; pre-search
    # here caused calendar/email turns with use_web="false" to run irrelevant
    # web searches before the agent even saw the tool surface.
    skip_web = (
        bool(search_context)
        or not allow_tool_preprocessing
        or casual_low_signal
        or bool(agent_mode and _PERSONAL_TOOL_CONTEXT_RE.search(context_message or ""))
    )

    # Build context preface
    # The stream path uses enhanced_message (with CoT/preprocessing applied),
    # the sync path uses text_for_context.
    _ctx_msg = (
        context_message
        if continuation_context_message
        else (
            preprocessed.enhanced_message
            if use_enhanced_message
            else preprocessed.text_for_context
        )
    )
    _preface_kwargs = dict(
        message=_ctx_msg,
        session=sess,
        use_web=use_web_val and not skip_web,
        use_memory=mem_enabled,
        time_filter=time_filter,
        preset_system_prompt=preset.system_prompt,
        owner=user,
        character_name=preset.character_name,
        persona_memory=preset.persona_memory,
        agent_mode=agent_mode,
        incognito=incognito,
        use_skills=skills_enabled,
    )
    if use_rag is not None or is_research_spinoff or casual_low_signal:
        _preface_kwargs["use_rag"] = use_rag_val
    preface, rag_sources, web_sources = ports.build_preface(chat_processor, **_preface_kwargs)

    # Capture used memories immediately
    used_memories = ports.used_memories(chat_processor)

    # Inject pre-fetched search context (compare mode)
    if search_context and allow_tool_preprocessing and not casual_low_signal:
        preface.append(untrusted_context_message("prefetched search context", search_context))

    # Inject selected notes as context
    if selected_notes_context:
        selected_notes_context = (
            "The following selected notes and todos are reference context explicitly chosen "
            "by the user. Use them directly when relevant to the user's request. Do not "
            "ask for permission to use this context, and do not call note-management "
            "tools just to retrieve the same notes; use those tools only for a requested "
            "change or when this context is insufficient.\n\n"
            + selected_notes_context
        )
        preface.append(
            untrusted_context_message(
                "Selected notes and todos",
                selected_notes_context,
            )
        )

    # YouTube transcripts
    for transcript in preprocessed.youtube_transcripts:
        preface.append(untrusted_context_message("youtube transcript", transcript))
    for source in ports.youtube_sources(
        preprocessed.text_for_context, preprocessed.youtube_transcripts
    ):
        if not any(existing.get("url") == source["url"] for existing in web_sources):
            web_sources.append(source)

    # Normalize model ID. Prefer cached endpoint models so group chat does not
    # re-hit slow local /models endpoints on every participant turn.
    norm = ports.normalize_model(sess)
    if norm:
        sess.model = norm

    # Build messages. In Nobody/incognito mode, never read saved session
    # history: the session id may be a temporary wrapper or, in buggy clients, a
    # stale normal session id. Only the ephemeral incognito transcript is safe.
    messages = preface + (ports.private_messages(session_id) if incognito else ports.session_history(sess))

    # Current date/time — injected as a standalone *user*-role context message
    # placed immediately before the latest user turn, NOT folded into the
    # system prompt. Its text changes every minute, and local OpenAI-compatible
    # backends (llama.cpp / LM Studio) key their KV-cache prefix off the
    # system message byte-for-byte; mixing ever-changing timestamp text into
    # it would invalidate the cached prefix on every request (issue #2927).
    # Placing it at the tail also keeps it out of the stable
    # preface+history prefix, so that prefix stays byte-identical turn over
    # turn (modulo the genuinely new history entries) and the cache survives.
    if not agent_mode:
        try:
            _dt_msg = ports.current_datetime_message()
            if messages and messages[-1].get("role") == "user":
                messages.insert(len(messages) - 1, _dt_msg)
            else:
                messages.append(_dt_msg)
        except Exception:
            logger.debug("Failed to add current date/time context", exc_info=True)

    route_messages = list(messages)
    # Explicit fallback routing must shape from the same route-neutral prompt
    # for every candidate. Running selected-model compaction here would mutate
    # session history before we know which route can answer and would make a
    # later larger-context candidate unable to recover discarded history.
    if context_resolution is not None:
        prepared_window = {"context_length": context_resolution.shaping_window}
    else:
        prepared_window = {}
    if defer_context_shaping:
        context_length = (
            prepared_window.get("context_length")
            or ports.context_length(sess.endpoint_url, sess.model)
        )
        was_compacted = False
    else:
        messages, context_length, was_compacted = await ports.compact(
            sess, sess.endpoint_url, sess.model, messages, sess.headers, owner=user,
            **prepared_window,
        )
    _before_trim_messages = len(messages)
    _before_trim_tokens = ports.estimate_tokens(messages)
    if not defer_context_shaping:
        messages = ports.trim(messages, context_length)
    _after_trim_messages = len(messages)
    _after_trim_tokens = ports.estimate_tokens(messages)
    _context_trimmed = _after_trim_messages < _before_trim_messages or _after_trim_tokens < _before_trim_tokens

    return ChatContext(
        preface=preface,
        rag_sources=rag_sources,
        web_sources=web_sources,
        used_memories=used_memories,
        messages=messages,
        context_length=context_length,
        was_compacted=was_compacted,
        user=user,
        uprefs=uprefs,
        preset=preset,
        preprocessed=preprocessed,
        context_trimmed=_context_trimmed,
        context_messages_before_trim=_before_trim_messages,
        context_messages_after_trim=_after_trim_messages,
        context_tokens_before_trim=_before_trim_tokens,
        context_tokens_after_trim=_after_trim_tokens,
        auto_opened_docs=auto_opened_docs,
        uploaded_files=uploaded_files,
        route_messages=route_messages,
    )
