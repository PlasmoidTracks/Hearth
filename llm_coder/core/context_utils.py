"""
Reasoning parsing and context estimation utilities.

Reasoning detection: many local reasoning-tuned models wrap internal thought
in tags like <think>...</think> or similarly named blocks. We parse those out
so the UI can show them separately and apply retention policy. If no such tags
are found, the text is treated as plain answer content with no reasoning.
"""
import json
import re
from typing import Tuple, List, Dict, Any

THINK_PATTERN = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def split_reasoning(text: str) -> Tuple[str, List[str]]:
    """Returns (visible_answer_text, [reasoning_blocks])."""
    if not text:
        return text, []
    reasoning_blocks = THINK_PATTERN.findall(text)
    visible = THINK_PATTERN.sub("", text).strip()
    return visible, [r.strip() for r in reasoning_blocks]


def estimate_tokens(text) -> int:
    """Rough token estimate: ~4 chars per token (English-ish heuristic)."""
    if text is None or text == "":
        return 0
    if isinstance(text, int):
        return max(1, text // 4) if text > 0 else 0
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = m.get("content") or ""
        total += estimate_tokens(content)
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            total += estimate_tokens(fn.get("name", "")) + estimate_tokens(fn.get("arguments", ""))
    return total


def apply_reasoning_retention(messages: List[Dict[str, Any]], policy: str) -> List[Dict[str, Any]]:
    """
    policy: "keep_all" | "keep_last" | "drop" | "hide"
    Operates on a copy; strips <think> blocks from all but the last assistant
    message when policy == "keep_last", strips from all when "drop", leaves
    untouched for "keep_all"/"hide" (hide only affects UI display, not storage).
    """
    if policy in ("keep_all", "hide"):
        return messages

    result = list(messages)
    assistant_indices = [i for i, m in enumerate(result) if m.get("role") == "assistant" and m.get("content")]
    if not assistant_indices:
        return result

    last_idx = assistant_indices[-1] if policy == "keep_last" else None

    for i in assistant_indices:
        if policy == "keep_last" and i == last_idx:
            continue
        visible, _ = split_reasoning(result[i]["content"])
        result[i] = dict(result[i])
        result[i]["content"] = visible
    return result


def _tool_result_key(tool_name: str, args: dict) -> str:
    """Extract a 'key' from a tool call's arguments for grouping by identity.

    The key represents *what* was operated on, not *which* invocation:
    - read_file: file path
    - read_multiple_files: each file path individually (multiple keys per call)
    - fetch_text_url: URL
    - web_search: query string
    - extract_images: URL
    - view_image: file path
    - view_video: file path plus its optional time window
    - run_command: the command string
    - write_file / replace_in_file / delete_section / append_to_file / prepend_to_file: file path
    - delete_file: file path
    - rename_file: old path
    - Other tools: a short string "tool_name" (no dedup grouping)
    """
    if tool_name in ("read_file",):
        return f"read_file:{args.get('path', '')}"
    elif tool_name == "read_multiple_files":
        paths = args.get("paths", [])
        # Return a special compound key; the caller will expand this.
        return f"read_multiple_files:{'|'.join(paths)}"
    elif tool_name == "fetch_text_url":
        return f"fetch_text_url:{args.get('url', '')}"
    elif tool_name == "web_search":
        return f"web_search:{args.get('query', '')}"
    elif tool_name == "extract_images":
        return f"extract_images:{args.get('url', '')}"
    elif tool_name == "view_image":
        return f"view_image:{args.get('path', '')}"
    elif tool_name == "view_video":
        # Windowed views of the same file are distinct observations, so the
        # time bounds are part of the identity for compaction grouping.
        return (f"view_video:{args.get('path', '')}"
                f":{args.get('start_time', '')}-{args.get('end_time', '')}")
    elif tool_name == "run_command":
        return f"run_command:{args.get('command', '')}"
    elif tool_name in ("write_file", "replace_in_file", "delete_section",
                       "append_to_file", "prepend_to_file", "delete_file"):
        return f"{tool_name}:{args.get('path', '')}"
    elif tool_name == "rename_file":
        return f"rename_file:{args.get('old_path', '')}"
    else:
        return f"{tool_name}:_generic_"


def compact_delete_tool_returns(messages: List[Dict[str, Any]], keep_n: int = 1) -> List[Dict[str, Any]]:
    """Remove tool call result messages from context, keeping the last N per key.

    When keep_n >= 1: for each unique tool-result key, keeps only the last N
    tool-result messages (along with their corresponding assistant tool-call
    messages). Older ones are removed and replaced with brief placeholder
    user messages.

    When keep_n == 0: all tool result messages are removed (old behavior).

    Args:
        messages: The message list to compact (modified in-place).
        keep_n: Number of recent tool results to keep per unique key.

    Returns:
        The modified messages list.
    """
    if not messages:
        return messages

    # Step 1: Walk messages and record every tool call + tool result pair.
    # Each pair: (assistant_msg_idx, tool_result_idx, tool_name, key, file)
    # A compound call (e.g. read_multiple_files) shares ONE tool-result message
    # across all its per-file keys — resolve that shared tool_idx once, up front,
    # so it is never mistaken for "removable" just because one of its keys is stale.
    tool_pairs: List[tuple] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            tool_call_id = tc.get("id", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}

            raw_key = _tool_result_key(tool_name, args)

            tool_idx = None
            for j in range(i + 1, len(messages)):
                if (messages[j].get("role") == "tool"
                        and messages[j].get("tool_call_id") == tool_call_id):
                    tool_idx = j
                    break

            if tool_name == "read_multiple_files" and raw_key.startswith("read_multiple_files:"):
                file_paths = args.get("paths", [])
                if not file_paths:
                    continue
                for fp in file_paths:
                    if fp:
                        pair_key = f"read_multiple_files:{fp}"
                        tool_pairs.append((i, tool_idx, tool_name, pair_key, fp))
            else:
                tool_pairs.append((i, tool_idx, tool_name, raw_key, None))

    if not tool_pairs:
        return messages

    # Step 2: Group by key.
    from collections import defaultdict
    key_to_pairs: Dict[str, List[tuple]] = defaultdict(list)
    for pair in tool_pairs:
        key_to_pairs[pair[3]].append(pair)

    # Step 3: Protect any assistant message that has at least one key still
    # within the "keep last N" window. Since a compound call's tool-result is
    # shared across all its keys, we must never remove it while a sibling key
    # on the same call is still kept — doing so both destroys content the
    # model still needs AND leaves a dangling tool_call_id, which is exactly
    # the kind of malformed history that makes models hallucinate fake tool
    # call/result text instead of calling tools properly.
    protected_asst_idx: set = set()
    if keep_n > 0:
        for key, pairs in key_to_pairs.items():
            for p in pairs[-keep_n:]:
                protected_asst_idx.add(p[0])

    to_remove_set: set = set()
    placeholder_by_asst: Dict[int, Dict[str, Any]] = {}

    for key, pairs in key_to_pairs.items():
        to_prune = pairs if keep_n <= 0 else pairs[:-keep_n]
        for p in to_prune:
            asst_idx, tool_idx, tool_name, _key, file_path = p
            if asst_idx in protected_asst_idx:
                continue  # a sibling key on this same call is still kept
            to_remove_set.add(asst_idx)
            if tool_idx is not None:
                to_remove_set.add(tool_idx)
            if asst_idx not in placeholder_by_asst:
                text = (
                    f"[Pruned: earlier {tool_name} result for '{file_path}' was "
                    "removed during compaction; the most recent result is still available.]"
                    if file_path else
                    f"[Pruned: {tool_name} result removed during compaction.]"
                )
                placeholder_by_asst[asst_idx] = {"role": "user", "content": text}

    if not to_remove_set:
        return messages

    # Step 4: Rebuild in a single pass, replacing each removed assistant message
    # IN PLACE with its placeholder — never clustering removals elsewhere. This
    # guarantees a placeholder can never land between a surviving assistant
    # tool_calls message and its own matching tool-result message, which is the
    # adjacency the chat template/API relies on.
    new_messages: List[Dict[str, Any]] = []
    for i, m in enumerate(messages):
        if i in placeholder_by_asst:
            new_messages.append(placeholder_by_asst[i])
        elif i in to_remove_set:
            continue
        else:
            new_messages.append(m)

    messages.clear()
    messages.extend(new_messages)
    return messages


def prune_stale_file_reads(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove stale read_file / read_multiple_files tool-call pairs from history.

    When the model reads a file, changes it, and then re-reads it, the old read
    + its large file-content result sits in context alongside the newer, accurate
    read.  This function scans the message list and for every file that has been
    read more than once, removes all but the *last* (read_file call, tool result)
    pair, replacing the older pair with a brief placeholder so the model is not
    confused by orphaned tool calls.

    Operates in-place and returns the modified list.
    """
    READ_TOOL_NAMES = frozenset({"read_file", "read_multiple_files"})
    if not messages:
        return messages

    # Step 1: walk forward and record every read_file / read_multiple_files call
    # alongside its index in the messages list.
    # Structure:
    #   assistant[i]  -> tool_calls  -> {function: {name: "read_file", arguments: '{"path":"..."}'}}
    #   tool[j]       -> {tool_call_id: "...", name: "read_file", content: "file contents"}
    read_occurrences: List[dict] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            if tool_name not in READ_TOOL_NAMES:
                continue
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if tool_name == "read_file":
                file_paths = [args.get("path", "")]
            else:  # read_multiple_files
                file_paths = args.get("paths", [])
            for fp in file_paths:
                if fp:
                    read_occurrences.append({
                        "msg_idx": i,
                        "tool_call_id": tc.get("id", ""),
                        "tool_name": tool_name,
                        "file": fp,
                    })

    # Step 2: find files read more than once.
    from collections import defaultdict
    file_to_occurrences: Dict[str, List[dict]] = defaultdict(list)
    for occ in read_occurrences:
        file_to_occurrences[occ["file"]].append(occ)

    files_to_prune = {fp for fp, occs in file_to_occurrences.items() if len(occs) > 1}

    if not files_to_prune:
        return messages

    # Step 3: protect any assistant message that still has at least one file
    # that is the LAST (freshest) read for that file. A call that reads
    # multiple files at once shares one tool-result message across all of
    # them, so it can only be removed if EVERY file it covers is stale —
    # never partially, or the surviving file's content would vanish along
    # with a dangling tool_call_id left in the assistant's history.
    protected_asst_idx: set = set()
    for fp, occs in file_to_occurrences.items():
        protected_asst_idx.add(occs[-1]["msg_idx"])

    to_remove: set = set()
    placeholder_by_asst: Dict[int, Dict[str, Any]] = {}

    for fp in sorted(files_to_prune):
        occs = file_to_occurrences[fp]
        for occ in occs[:-1]:
            assistant_idx = occ["msg_idx"]
            if assistant_idx in protected_asst_idx:
                continue  # this call also covers a file that's still fresh
            tool_call_id = occ["tool_call_id"]
            to_remove.add(assistant_idx)
            for j in range(assistant_idx + 1, len(messages)):
                if (messages[j].get("role") == "tool"
                        and messages[j].get("tool_call_id") == tool_call_id):
                    to_remove.add(j)
                    break
            if assistant_idx not in placeholder_by_asst:
                placeholder_by_asst[assistant_idx] = {
                    "role": "user",
                    "content": (
                        f"[Pruned: earlier read_file call for '{fp}' was removed from context "
                        "because the file was re-read later in the conversation. "
                        "The latest read is still available.]"
                    ),
                }

    if not to_remove:
        return messages

    # Rebuild in a single pass, replacing each removed assistant message IN
    # PLACE with its placeholder — this guarantees a placeholder can never
    # land between a surviving assistant tool_calls message and its own
    # matching tool-result message.
    new_messages: List[Dict[str, Any]] = []
    for i, m in enumerate(messages):
        if i in placeholder_by_asst:
            new_messages.append(placeholder_by_asst[i])
        elif i in to_remove:
            continue
        else:
            new_messages.append(m)

    messages.clear()
    messages.extend(new_messages)
    return messages
