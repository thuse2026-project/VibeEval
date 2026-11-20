"""Inspectable model windows, material projection and bounded compaction planning.

No model calls, history writes or implicit adoption. The next coordinator owns
attempt persistence, CAS adoption and the automatic loop around these primitives.
"""

import json
import math
from itertools import islice
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from app.agent import apply_compaction, prepare_compaction, prepare_model_request
from app.agent.model import MAX_AGENT_REQUEST_CHARS, NativeRequest, project_model_input
from app.agent.session_types import FrozenModel, SessionBusy, SessionError

from .materials import digest, encoded

READ_TOOLS = frozenset(
    (
        "list_directory",
        "glob_files",
        "search_content",
        "read_file",
        "list_conversations",
        "read_conversation",
        "read_material",
        "read_reference",
        "read_source",
        "read_execution_output",
    )
)


class WindowPolicy(FrozenModel):
    context_window_tokens: int = Field(default=32768, strict=True, ge=256)
    safety_reserve_tokens: int = Field(default=1024, strict=True, ge=0)
    soft_input_tokens: int = Field(default=9000, strict=True, ge=1)
    hard_input_tokens: int = Field(default=16000, strict=True, ge=1)
    soft_request_chars: int = Field(default=45000, strict=True, ge=1)
    hard_request_chars: int = Field(
        default=60000, strict=True, ge=1, le=MAX_AGENT_REQUEST_CHARS
    )
    max_messages: int = Field(default=240, strict=True, ge=4, le=256)
    max_tool_body_chars: int = Field(default=12000, strict=True, ge=100)
    preview_chars: int = Field(default=2000, strict=True, ge=1)
    keep_recent_exchanges: int = Field(default=1, strict=True, ge=1)
    keep_recent_turns: int = Field(default=1, strict=True, ge=0)
    compaction_input_chars: int = Field(
        default=60000, strict=True, ge=1000, le=MAX_AGENT_REQUEST_CHARS
    )
    max_candidate_checks: int = Field(default=64, strict=True, ge=1, le=256)

    @model_validator(mode="after")
    def ordered_limits(self):
        if (
            self.soft_input_tokens > self.hard_input_tokens
            or self.soft_request_chars > self.hard_request_chars
            or self.preview_chars >= self.max_tool_body_chars
            or self.safety_reserve_tokens >= self.context_window_tokens
        ):
            raise ValueError("context limits must be ordered")
        return self


STAGE_AGENT_WINDOW = WindowPolicy(
    context_window_tokens=65_536,
    soft_input_tokens=24_000,
    hard_input_tokens=32_000,
    soft_request_chars=72_000,
    hard_request_chars=120_000,
    compaction_input_chars=120_000,
)


def read_body(content):
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        return None
    if (
        not isinstance(body, dict)
        or body.get("status") != "ok"
        or not isinstance(body.get("material_id"), str)
        or not isinstance(body.get("text"), str)
        or body.get("returned_sha256") != digest(body["text"])
    ):
        return None
    return body


def _signature(body):
    return encoded(
        [
            body["material_id"],
            body.get("source_version"),
            body.get("source_sha256"),
            body["returned_sha256"],
            body.get("start_offset"),
            body.get("end_offset"),
            body.get("selection"),
        ]
    )


def _file_key(body):
    if body.get("source_kind") != "workspace_file":
        return None
    start, end = body.get("start_offset"), body.get("end_offset")
    if (
        type(start) is not int
        or type(end) is not int
        or end - start != len(body["text"])
    ):
        return None
    key = tuple(
        body.get(name) for name in ("source_id", "source_version", "source_sha256")
    )
    return key if all(isinstance(part, str) and part for part in key) else None


def _file_overlap(body, visible):
    start, end = body["start_offset"], body["end_offset"]
    missing = []
    cursor = start
    for left, right in sorted(visible):
        if right <= cursor:
            continue
        if left >= end:
            break
        if left > cursor:
            missing.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        missing.append((cursor, end))
    if missing == [(start, end)] or start == end:
        return None

    new_fragments = []
    for left, right in missing:
        text = body["text"][left - start : right - start]
        new_fragments.append(
            {
                "start_offset": left,
                "end_offset": right,
                "text": text,
                "returned_sha256": digest(text),
            }
        )
    return {
        **{
            name: body[name]
            for name in (
                "status",
                "material_id",
                "source_kind",
                "source_id",
                "source_version",
                "returned_sha256",
                "start_offset",
                "end_offset",
            )
        },
        "window_projection": "file_overlap",
        "new_fragments": new_fragments,
        "note": (
            "The other characters in this range already appear in earlier results "
            "for this file version. Offsets are Unicode codepoints. Use read_material "
            "to retrieve the original material."
        ),
        "has_more": body.get("has_more", False),
        "next_offset": body.get("next_offset"),
    }


def _visible_fragments(body):
    return [
        {
            name: fragment[name]
            for name in ("start_offset", "end_offset", "returned_sha256")
        }
        for fragment in body["new_fragments"]
    ]


def _shorter(candidate, original):
    # Both limits matter: the window also estimates tokens from UTF-8 bytes.
    return len(candidate) < len(original) and len(candidate.encode("utf-8")) <= len(
        original.encode("utf-8")
    )


def project_materials(messages, policy=None, *, summary_input=False):
    """Change only recognized read results; preserve every native call/result pair.

    Repeated file ranges reuse text still visible in this window. Older large
    read bodies become explicit material previews.
    Recent tool exchanges, errors and unknown command output remain intact.
    """
    policy = policy or WindowPolicy()
    blocks = [
        index
        for index, item in enumerate(messages)
        if item.get("role") == "assistant" and item.get("tool_calls")
    ]
    protected = set() if summary_input else set(blocks[-policy.keep_recent_exchanges :])
    names, block, seen, file_spans, result = {}, None, {}, {}, []
    for index, item in enumerate(messages):
        view = dict(item)
        if item.get("role") == "assistant" and item.get("tool_calls"):
            block = index
            names = {
                call["id"]: call["function"]["name"] for call in item["tool_calls"]
            }
        body = (
            read_body(item.get("content"))
            if item.get("role") == "tool"
            and names.get(item.get("tool_call_id")) in READ_TOOLS
            else None
        )
        if body is not None:
            key = _signature(body)
            file_key = _file_key(body)
            replacement = None
            if key in seen:
                replacement = {
                    key: body[key]
                    for key in (
                        "status",
                        "material_id",
                        "source_kind",
                        "source_id",
                        "source_version",
                        "returned_sha256",
                        "start_offset",
                        "end_offset",
                    )
                    if key in body
                }
                replacement.update(
                    window_projection="duplicate_locator",
                    first_context_message_index=seen[key],
                    note="The exact fragment remains earlier in this model window. Use read_material to fetch it again if needed.",
                )
            elif file_key in file_spans:
                overlap = _file_overlap(body, file_spans[file_key])
                if overlap is not None and _shorter(encoded(overlap), item["content"]):
                    replacement = overlap
            if (
                block not in protected
                and len(item["content"]) > policy.max_tool_body_chars
            ):
                preview = body["text"][: policy.preview_chars]
                if len(preview) < len(body["text"]):
                    preview_body = {
                        **body,
                        "text": preview,
                        "returned_sha256": digest(preview),
                        "window_projection": "material_preview",
                        "has_more": True,
                        "truncated": True,
                        "original_fragment": {
                            "returned_sha256": body["returned_sha256"],
                            "start_offset": body.get("start_offset"),
                            "end_offset": body.get("end_offset"),
                        },
                        "note": "The original result is retained externally. Continue reading this immutable material_id; this preview is not the full result.",
                    }
                    preview_body["end_offset"] = body["start_offset"] + len(preview)
                    preview_body["byte_end"] = body["byte_start"] + len(
                        preview.encode("utf-8")
                    )
                    preview_body["next_offset"] = (
                        body["next_offset"] - len(body["text"]) + len(preview)
                    )
                    if replacement is None or not _shorter(
                        encoded(replacement), encoded(preview_body)
                    ):
                        replacement = preview_body
            if replacement is not None and _shorter(
                encoded(replacement), item["content"]
            ):
                view["content"] = encoded(replacement)
            else:
                replacement = None
                seen.setdefault(key, index)
            if file_key is not None:
                shown = body if replacement is None else replacement
                spans = file_spans.setdefault(file_key, [])
                if shown.get("window_projection") == "file_overlap":
                    spans.extend(
                        (f["start_offset"], f["end_offset"])
                        for f in shown["new_fragments"]
                    )
                elif "text" in shown:
                    spans.append((shown["start_offset"], shown["end_offset"]))
        result.append(view)
    return result


def _retained_tools(state):
    latest = state.compactions[-1] if state.compactions else None
    covered = latest.through_turn_count if latest else 0
    covered_steps = latest.through_step_count if latest else 0
    for turn_index, turn in enumerate(state.turns):
        if turn_index < covered:
            continue
        for step_index, step in enumerate(turn.steps):
            if turn_index == covered and step_index < covered_steps:
                continue
            if not step.response:
                continue
            results = {result.tool_call_id for result in step.tool_results}
            for call in step.response.message.tool_calls:
                if call.id in results or step.status in ("interrupted", "failed"):
                    yield (turn.id, step.id, call.id)


def execution_materials(body):
    """Describe the bounded fragments in a platform execution receipt."""
    if (
        isinstance(body, dict)
        and body.get("status") == "error"
        and isinstance(body.get("content"), str)
    ):
        try:
            body = json.loads(body["content"])
        except ValueError:
            return
    if not isinstance(body, dict) or not isinstance(body.get("execution_id"), str):
        return
    for stream in ("stdout", "stderr"):
        identity, text = body.get(stream + "_material_id"), body.get(stream)
        if not isinstance(identity, str) or not isinstance(text, str):
            continue
        yield {
            "material_id": identity,
            "source_kind": "execution_" + stream,
            "source_id": body["execution_id"] + ":" + stream,
            "source_version": str(body.get("output_revision", "")),
            "returned_sha256": digest(text),
            "preview": body.get(stream + "_preview_has_more", False),
            "end_offset": len(text),
        }
    change = body.get("change")
    if not isinstance(change, dict):
        return
    for part in ("before", "after", "diff", "observed"):
        identity = change.get(part + "_material_id")
        if not isinstance(identity, str):
            continue
        text = change.get("diff_preview") if part == "diff" else None
        locator = not isinstance(text, str)
        is_file = part in ("before", "observed")
        yield {
            "material_id": identity,
            "source_kind": (
                "workspace_file"
                if is_file
                else "file_diff" if part == "diff" else "proposed_file"
            ),
            "source_id": (
                change["path"] if is_file else body["execution_id"] + ":" + part
            ),
            "source_version": str(
                change.get("base_version")
                if part == "before"
                else change.get("current_version") if part == "observed" else 1
            ),
            "returned_sha256": None if locator else digest(text),
            "preview": (
                change.get("diff_preview_has_more", False) if part == "diff" else False
            ),
            "locator": locator,
            "end_offset": None if locator else len(text),
        }


def material_manifest(state, payload, *, answer_evidence=None):
    """Describe actual projected bodies, not presumed model memory/understanding."""
    tool_messages = (
        [
            (len(payload["messages"]) - 1, item)
            for item in answer_evidence
            if item.get("role") == "tool"
        ]
        if answer_evidence is not None
        else [
            (index, item)
            for index, item in enumerate(payload["messages"])
            if item.get("role") == "tool"
        ]
    )
    visible = dict(zip(_retained_tools(state), tool_messages, strict=True))
    latest = state.compactions[-1] if state.compactions else None
    boundary = (
        (latest.through_turn_count, latest.through_step_count) if latest else (0, 0)
    )
    entries = []
    for turn_index, turn in enumerate(state.turns):
        for step_index, step in enumerate(turn.steps):
            calls = (
                {call.id: call for call in step.response.message.tool_calls}
                if step.response
                else {}
            )
            for result in step.tool_results:
                call = calls[result.tool_call_id]
                if call.name in ("run_command", "write_file", "apply_patch"):
                    try:
                        body = json.loads(result.content)
                    except ValueError:
                        continue
                    item = visible.get((turn.id, step.id, result.tool_call_id))
                    compacted = turn_index < boundary[0] or (
                        turn_index == boundary[0] and step_index < boundary[1]
                    )
                    for part in execution_materials(body):
                        entries.append(
                            {
                                "turn_id": turn.id,
                                "step_id": step.id,
                                "tool_call_id": result.tool_call_id,
                                "material_id": part["material_id"],
                                "source_kind": part["source_kind"],
                                "source_id": part["source_id"],
                                "source_version": part["source_version"],
                                "original_returned_sha256": part["returned_sha256"],
                                "window_mode": (
                                    (
                                        "locator"
                                        if part.get("locator")
                                        else (
                                            "preview"
                                            if part["preview"]
                                            else "full_material"
                                        )
                                    )
                                    if item
                                    else "compacted" if compacted else "not_included"
                                ),
                                "message_index": item[0] if item else None,
                                "visible_sha256": (
                                    part["returned_sha256"] if item else None
                                ),
                                "start_offset": (
                                    0 if item and not part.get("locator") else None
                                ),
                                "end_offset": part["end_offset"] if item else None,
                            }
                        )
                    continue
                if result.is_error or calls[result.tool_call_id].name not in READ_TOOLS:
                    continue
                original = read_body(result.content)
                if original is None:
                    continue
                item = visible.get((turn.id, step.id, result.tool_call_id))
                compacted = turn_index < boundary[0] or (
                    turn_index == boundary[0] and step_index < boundary[1]
                )
                mode, shown, message_index = (
                    ("compacted" if compacted else "not_included"),
                    None,
                    None,
                )
                if item:
                    message_index, message = item
                    shown = json.loads(message["content"])
                    mode = (
                        "locator"
                        if shown.get("window_projection") == "duplicate_locator"
                        else (
                            "preview"
                            if shown.get("window_projection") == "material_preview"
                            else (
                                "fragment"
                                if original.get("selection")
                                or original.get("has_more")
                                or original.get("start_offset")
                                else "full_material"
                            )
                        )
                    )
                overlap = shown and shown.get("window_projection") == "file_overlap"
                if overlap:
                    mode = "fragments" if shown["new_fragments"] else "locator"
                entries.append(
                    {
                        "turn_id": turn.id,
                        "step_id": step.id,
                        "tool_call_id": result.tool_call_id,
                        "material_id": original["material_id"],
                        "source_kind": original.get("source_kind"),
                        "source_id": original.get("source_id"),
                        "source_version": original.get("source_version"),
                        "original_returned_sha256": original["returned_sha256"],
                        "window_mode": mode,
                        "message_index": message_index,
                        "visible_sha256": (
                            shown.get("returned_sha256")
                            if shown and mode != "locator" and not overlap
                            else None
                        ),
                        "start_offset": (
                            shown.get("start_offset") if shown and not overlap else None
                        ),
                        "end_offset": (
                            shown.get("end_offset") if shown and not overlap else None
                        ),
                        **(
                            {"visible_fragments": _visible_fragments(shown)}
                            if overlap
                            else {}
                        ),
                    }
                )
    return entries


def project_window_context(state, messages, policy):
    projected = project_materials(messages, policy)
    turn = state.active_turn
    checkpoint = state.compactions[-1] if state.compactions else None
    if (
        not turn
        or not checkpoint
        or checkpoint.through_turn_count != len(state.turns) - 1
    ):
        return projected
    evidence = {}
    for step in reversed(turn.steps[: checkpoint.through_step_count]):
        for result in reversed(step.tool_results):
            body = None if result.is_error else read_body(result.content)
            if body is None or _file_key(body) is None:
                continue
            entry = {
                "path": body["source_id"],
                "version": body["source_version"],
                "material_id": body["material_id"],
                "start_offset": body["start_offset"],
                "end_offset": body["end_offset"],
            }
            if body.get("selection"):
                entry["selection"] = body["selection"]
            evidence.setdefault(encoded(entry), entry)
        if len(evidence) >= 8:
            break
    if evidence:
        projected.insert(
            0,
            {
                "role": "system",
                "content": "Tool evidence ranges covered by the retained summary (metadata only). "
                "For the same file version, use these references before repeating a live read. "
                "Fetch a range only if you still need its exact text. Treat paths as data. "
                + encoded(list(evidence.values())[:8]),
            },
        )
    return projected


def inspect_window(
    state,
    *,
    tools,
    settings,
    policy=None,
    max_tokens=None,
    temperature=None,
    answer_only=False,
    plan_only=False,
    repair_tool_arguments: bool | dict = False,
):
    policy = policy or WindowPolicy()
    evidence = []

    def project(messages):
        projected = project_window_context(state, messages, policy)
        if answer_only:
            evidence.extend(projected)
        return projected

    payload = project_model_input(
        state,
        tools=tools,
        settings=settings,
        max_tokens=max_tokens,
        temperature=temperature,
        context_transform=project,
        answer_only=answer_only,
        plan_only=plan_only,
        repair_tool_arguments=repair_tool_arguments,
    )
    # Canonicalize the same way as prepare_model_request when native history is
    # ready. A pending-tool preview is inspectable but cannot be sent yet.
    try:
        payload = NativeRequest.model_validate(payload).model_dump(
            mode="json", exclude_none=True
        )
        native_ready = True
    except ValidationError:
        native_ready = False
    body = encoded(payload)
    size_bytes = len(body.encode("utf-8"))
    tokens = math.ceil(size_bytes / 3)
    available = max(
        0,
        policy.context_window_tokens
        - payload["max_tokens"]
        - policy.safety_reserve_tokens,
    )
    hard_tokens = min(policy.hard_input_tokens, available)
    soft_tokens = min(policy.soft_input_tokens, hard_tokens)
    soft = tokens >= soft_tokens or len(body) >= policy.soft_request_chars
    hard = (
        tokens > hard_tokens
        or len(body) > policy.hard_request_chars
        or len(payload["messages"]) > policy.max_messages
    )
    return {
        "stage": "preview",
        "conversation_id": state.id,
        "revision": state.revision,
        "context_generation": state.context_generation,
        "through_turn_count": (
            state.compactions[-1].through_turn_count if state.compactions else 0
        ),
        "through_step_count": (
            state.compactions[-1].through_step_count if state.compactions else 0
        ),
        "payload_sha256": digest(body),
        "native_history_valid": native_ready,
        "active_turn_id": state.active_turn.id if state.active_turn else None,
        "turn_status": state.active_turn.status if state.active_turn else "idle",
        "checkpoint_id": state.compactions[-1].id if state.compactions else None,
        "through_step_id": (
            state.compactions[-1].through_step_id if state.compactions else None
        ),
        "budget": {
            "serialized_characters": len(body),
            "serialized_utf8_bytes": size_bytes,
            "estimated_input_tokens": tokens,
            "estimate_method": "utf8_bytes_div_3_heuristic_v1",
            "reserved_output_tokens": payload["max_tokens"],
            "configured_context_window_tokens": policy.context_window_tokens,
            "safety_reserve_tokens": policy.safety_reserve_tokens,
            "available_input_tokens": available,
            "effective_hard_input_tokens": hard_tokens,
            "soft_input_tokens": policy.soft_input_tokens,
            "hard_input_tokens": policy.hard_input_tokens,
            "soft_request_characters": policy.soft_request_chars,
            "hard_request_characters": policy.hard_request_chars,
            "max_messages": policy.max_messages,
            "message_count": len(payload["messages"]),
            "over_soft_limit": soft,
            "over_hard_limit": hard,
        },
        "materials": material_manifest(
            state, payload, answer_evidence=evidence if answer_only else None
        ),
        "input_references": input_reference_manifest(state),
    }


def input_reference_manifest(state):
    covered = state.compactions[-1].through_turn_count if state.compactions else 0
    result = []
    for index, turn in enumerate(state.turns):
        try:
            context = json.loads(turn.input_context) if turn.input_context else {}
        except ValueError:
            continue
        if not isinstance(context, dict) or not isinstance(
            context.get("references"), list
        ):
            continue
        for item in context["references"]:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "turn_id": turn.id,
                    "reference_id": item.get("reference_id"),
                    "material_id": item.get("material_id"),
                    "kind": item.get("reference", {}).get("kind"),
                    "source_version": item.get("source_version"),
                    "window_mode": (
                        "compacted"
                        if index < covered
                        else (
                            "inline_fragment"
                            if "text" in item and item.get("has_more")
                            else "inline_material" if "text" in item else "locator"
                        )
                    ),
                    "visible_sha256": (
                        item.get("returned_sha256") if index >= covered else None
                    ),
                }
            )
    return result


def compaction_manifest(request):
    material = json.loads(request.payload["messages"][-1]["content"])
    bodies = []
    for turn in material["new_turns"]:
        for message in turn["messages"]:
            if message.get("role") != "tool":
                continue
            try:
                body = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            for part in execution_materials(body):
                bodies.append(
                    {
                        "turn_id": turn["turn_id"],
                        "tool_call_id": message["tool_call_id"],
                        "material_id": part["material_id"],
                        "source_version": part["source_version"],
                        "window_projection": (
                            "locator"
                            if part.get("locator")
                            else (
                                "execution_preview"
                                if part["preview"]
                                else "original_fragment"
                            )
                        ),
                        "returned_sha256": part["returned_sha256"],
                    }
                )
            if isinstance(body, dict) and isinstance(body.get("material_id"), str):
                bodies.append(
                    {
                        "turn_id": turn["turn_id"],
                        "tool_call_id": message["tool_call_id"],
                        "material_id": body["material_id"],
                        "source_version": body.get("source_version"),
                        "window_projection": body.get(
                            "window_projection", "original_fragment"
                        ),
                        "returned_sha256": (
                            None
                            if body.get("window_projection") == "file_overlap"
                            else body.get("returned_sha256")
                        ),
                        **(
                            {"visible_fragments": _visible_fragments(body)}
                            if body.get("window_projection") == "file_overlap"
                            else {}
                        ),
                    }
                )
    return {
        "stage": "prepared",
        "kind": "compaction",
        "payload_sha256": request.payload_sha256,
        "source_revision": request.source_revision,
        "through_turn_count": request.through_turn_count,
        "through_step_count": request.through_step_count,
        "source_digest": request.source_digest,
        "materials": bodies,
        "budget": {
            "serialized_characters": len(request.payload_json),
            "estimated_input_tokens": math.ceil(
                len(request.payload_json.encode("utf-8")) / 3
            ),
            "estimate_method": "utf8_bytes_div_3_heuristic_v1",
            "reserved_output_tokens": request.payload["max_tokens"],
        },
    }


class ContextCapacityError(ValueError):
    def __init__(self, report):
        super().__init__("CONTEXT_CAPACITY_EXCEEDED")
        self.report = report


def prepare_window_request(
    state,
    *,
    tools,
    settings,
    policy=None,
    max_tokens=None,
    temperature=None,
    answer_only=False,
    plan_only=False,
    repair_tool_arguments: bool | dict = False,
):
    """Final hard gate; a coordinator should plan soft maintenance while ready."""
    policy = policy or WindowPolicy()
    report = inspect_window(
        state,
        tools=tools,
        settings=settings,
        policy=policy,
        max_tokens=max_tokens,
        temperature=temperature,
        answer_only=answer_only,
        plan_only=plan_only,
        repair_tool_arguments=repair_tool_arguments,
    )
    if report["budget"]["over_hard_limit"]:
        raise ContextCapacityError(report)
    prepared = prepare_model_request(
        state,
        tools=tools,
        settings=settings,
        max_tokens=max_tokens,
        temperature=temperature,
        context_transform=lambda messages: project_window_context(
            state, messages, policy
        ),
        answer_only=answer_only,
        plan_only=plan_only,
        repair_tool_arguments=repair_tool_arguments,
    )
    return prepared, {
        **report,
        "stage": "prepared",
        "payload_sha256": prepared.payload_sha256,
    }


def adopt_window_compaction(state, result, *, tools, settings, policy=None):
    """Require reduction of the actual projected request, not only raw history."""
    policy = policy or WindowPolicy()
    updated = apply_compaction(
        state,
        result,
        context_transform=lambda messages: project_materials(messages, policy),
    )
    if updated is state:
        return state
    before = inspect_window(state, tools=tools, settings=settings, policy=policy)
    after = inspect_window(updated, tools=tools, settings=settings, policy=policy)
    if (
        after["budget"]["serialized_characters"]
        >= before["budget"]["serialized_characters"]
    ):
        raise SessionError("summary does not reduce the projected model request")
    return updated


def plan_compaction(
    state,
    *,
    tools,
    settings,
    policy=None,
    trigger: Literal["manual", "threshold"] = "threshold",
):
    """Select a bounded safe request, without calling the provider or adopting it.

    A long history may require several chunks. The coordinator must cap attempts,
    persist every attempt and recheck the resulting budget before model dispatch.
    """
    if trigger not in ("manual", "threshold"):
        raise ValueError("unsupported compaction trigger")
    policy = policy or WindowPolicy()
    if state.active_turn and state.active_turn.status != "ready":
        raise SessionBusy("context maintenance waits for a closed exchange boundary")
    report = inspect_window(state, tools=tools, settings=settings, policy=policy)
    if trigger == "threshold" and not report["budget"]["over_soft_limit"]:
        return None
    previous = state.compactions[-1] if state.compactions else None
    covered = (
        (previous.through_turn_count, previous.through_step_count)
        if previous
        else (0, 0)
    )
    closed = len(state.turns) - bool(state.active_turn)

    def candidates():
        if covered != (0, 0) and (not covered[1] or covered[0] == closed):
            yield covered
        last_closed = closed - policy.keep_recent_turns
        if covered[1] and covered[0] < closed and report["budget"]["over_hard_limit"]:
            # Finish the previously summarized turn if keeping its unsummarized
            # tail would leave the new request above the hard limit.
            last_closed = max(last_closed, covered[0] + 1)
        for count in range(covered[0] + 1, last_closed + 1):
            yield count, 0
        active = state.active_turn
        if active:
            first = covered[1] + 1 if covered[0] == closed else 1
            for count in range(
                first, len(active.steps) - policy.keep_recent_exchanges + 1
            ):
                yield closed, count

    best = None
    for turns, steps in islice(candidates(), policy.max_candidate_checks):
        try:
            request = prepare_compaction(
                state,
                through_turn=turns,
                through_step_count=steps,
                settings=settings,
                history_transform=lambda messages: project_materials(
                    messages, policy, summary_input=True
                ),
            )
        except ValidationError:
            # The prepared-request hard cap is not permission to trim user text
            # or split tool pairs. Return the last feasible prefix, if any.
            break
        if len(request.payload_json) > policy.compaction_input_chars:
            break
        best = request
    return best
