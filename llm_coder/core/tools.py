"""
Tool schema definitions (OpenAI function-calling format) and execution logic.
Read/search tools execute immediately (hybrid mode). Write/delete tools are
staged as pending patches and require UI approval based on the active patch
handling mode. Failed tool calls return structured, actionable error messages.
"""
import time
from typing import Dict, Any, List, Optional, Callable, TYPE_CHECKING
from .workspace import Workspace, WorkspaceError
from .virtual_fs import VirtualFS
from . import computer_tools

if TYPE_CHECKING:
    from .draft_store import DraftStore

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in the project workspace (relative paths).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List immediate contents (files and subdirectories) of a directory in the project.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative directory path, '.' for root"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a text file in the project. SVG files are read as their XML source; use view_image to see one rendered instead. Optionally read only a specific line range with start_line/end_line (1-based, inclusive). Omit both to read the entire file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "start_line": {"type": "integer", "description": "First line to read (1-based). Defaults to 1.", "default": 1},
                    "end_line": {"type": "integer", "description": "Last line to read (1-based, inclusive). Use -1 to read to the end of the file (default).", "default": -1},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_multiple_files",
            "description": "Read the full contents of several text files at once, to save round-trips.",
            "parameters": {
                "type": "object",
                "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": "Load an image or vector graphic (SVG) and show it to you visually. Accepts paths inside the project (relative) or anywhere on disk (absolute paths outside the project root are allowed for read-only viewing). Images/GIFs/WebP are shown as a single frame; animated images show their first frame only. For videos use view_video instead. Only works if the loaded model supports vision (mmproj).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to an image/svg file: relative to the project root, or an absolute path anywhere on disk"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_video",
            "description": "Load a video file and show it to you as a sequence of evenly-spaced sampled frames. Accepts paths inside the project (relative) or anywhere on disk (absolute paths outside the project root are allowed for read-only viewing). Optionally restrict sampling to a time window with start_time/end_time (seconds; either may be given alone, values are clamped to the clip). Use media_metadata first to learn the video's duration before picking a window. Only works if the loaded model supports vision (mmproj).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to a video file: relative to the project root, or an absolute path anywhere on disk"},
                    "max_frames": {"type": "integer", "description": "Number of frames to sample from the (possibly windowed) video. Default 8, max 60."},
                    "start_time": {"type": "number", "description": "Optional start of the sampling window in seconds from the beginning of the video. Omit to start at the beginning."},
                    "end_time": {"type": "number", "description": "Optional end of the sampling window in seconds from the beginning of the video. Omit to run to the end."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_metadata",
            "description": "Report technical metadata for an image or video file: format/mode/resolution/size for images, and encoding (fourcc codec), duration, framerate, frame count, resolution, and bitrate estimate for videos. Read-only and text-only — it never loads pixels into the conversation, so it is cheap and safe to call before view_image/view_video. Accepts paths inside the project (relative) or absolute paths anywhere on disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to an image/video file: relative to the project root, or an absolute path anywhere on disk"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": "Search for a substring/keyword across all text files in the project, returns matching lines with file+line number.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_regex",
            "description": "Search project text files using a regular expression pattern, returns matching file+line+text.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string", "description": "Python regex pattern"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "**WARNING: This tool FULLY REPLACES the entire file content.** Any existing content is discarded. Prefer using append_to_file (add to end), prepend_to_file (add to beginning), or replace_in_file (replace a specific section) for in-place edits. Only use write_file for new files or when you intentionally want to replace the entire file. Subject to the active patch handling mode (may require user approval).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "Full new file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Propose replacing an exact text segment within an existing file with new text. Use this for incremental changes. Do NOT use this for full body replacements, and do NOT use it to delete a section — to remove text from a file, use delete_section instead (when available). Subject to the active patch handling mode. The old_text must match exactly (including whitespace). By default (unique=true, the normal case) the old_text must match exactly ONE location in the file; if it matches more than one location the call fails — either make the snippet more specific by including surrounding context, or explicitly set unique=false. WARNING: unique=false is disruptive — it replaces EVERY occurrence of old_text in the file at once, so the same change is applied at each match site with no per-location review. Only use unique=false when you genuinely want the change applied to all matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "unique": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether the old_text is expected to match a unique (single) segment of the file. Leave at the default true for the normal case: the call fails if old_text matches 0 or 2+ locations. Set to false only when you intentionally want to replace EVERY occurrence of old_text at once — this is disruptive because the replacement is applied to all matches in the file simultaneously, and each match site should be reviewed carefully.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_section",
            "description": "Propose deleting an exact text segment from an existing file. Use this whenever you want to remove a section of a file — do NOT use replace_in_file with an empty new_text for that. The section must match the file exactly, character-for-character including whitespace. By default (unique=true, the normal case) the section must match exactly ONE location in the file; if it matches more than one location the call fails — either make the snippet more specific by including surrounding context, or explicitly set unique=false. WARNING: unique=false is disruptive — it deletes EVERY occurrence of the section in the file at once. Only set it to false when you genuinely want all matches removed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "section": {"type": "string", "description": "The verbatim text segment to delete (exact match, including whitespace)"},
                    "unique": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether the section is expected to match a unique (single) segment of the file. Leave at the default true for the normal case: the call fails if the section matches 0 or 2+ locations. Set to false only when you intentionally want to delete EVERY occurrence of the section at once — this is disruptive because all matching locations are removed from the file simultaneously.",
                    },
                },
                "required": ["path", "section"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Propose deleting a file from the project. Subject to the active patch handling mode.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": "Propose renaming/moving a file within the project. Subject to the active patch handling mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_path": {"type": "string"},
                    "new_path": {"type": "string"},
                },
                "required": ["old_path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_to_file",
            "description": "Propose appending text content to the END of an existing file. The file must already exist (check with list_files or read_file first). The content is appended exactly as-is after the file's current content. Use this for adding new sections, logs, or content to the bottom of a file without rewriting the whole thing. Subject to the active patch handling mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to an existing file"},
                    "content": {"type": "string", "description": "Text content to append to the end of the file"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prepend_to_file",
            "description": "Propose prepending text content to the BEGINNING of an existing file. The file must already exist (check with list_files or read_file first). The content is inserted before the file's current content, pushing existing content down. Useful for adding headers, license blocks, imports, or configuration at the top. Subject to the active patch handling mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to an existing file"},
                    "content": {"type": "string", "description": "Text content to prepend to the beginning of the file"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "think_out_loud",
            "description": "Think out loud before making a tool call or giving a final answer. Use this to articulate your reasoning, consider alternatives, note uncertainties, or plan your next steps. This tool does NOT change any files or produce side-effects — it simply returns your thought text to context so you can continue with more informed tool calls. You are encouraged to use this before each tool call when the task is complex.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought": {"type": "string", "description": "Your reasoning, plan, or observations before proceeding"},
                },
                "required": ["thought"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_context_budget",
            "description": "Report the current context-window budget of this conversation: the absolute context size (token limit), how many tokens are used, how many remain free, and the percentage used. Use it to judge how much room is left before compaction or overflow becomes likely. Read-only — it changes nothing.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uptime",
            "description": "Report how long the current response / tool-call chain has been running: the elapsed time since this turn began (the point it last went idle). Use it to track how much wall-clock time the work so far has taken. Read-only — it changes nothing.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_turn",
            "description": "Signal that you are finished with this task and have nothing further to do right now. Optionally include a final summary message.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_subagent",
            "description": (
                "Deploy a fresh subagent: spin up a separate, clean-context worker on a "
                "dedicated model and run it to completion on ONE self-contained task, "
                "then hand you back its final answer. The subagent does NOT see this "
                "conversation and cannot talk to the user, so the `task` text must carry "
                "everything it needs (file paths, exact requirements, what to return). "
                "It works autonomously with read-only tools — it can list, read and search "
                "the project and think out loud, but CANNOT edit files, run shell commands, "
                "search the web, or ask you anything; it reports findings rather than "
                "making changes. Use it to offload a bounded investigation (read/summarise/"
                "verify across files, digest a long document, sanity-check an approach) so "
                "you keep your own context clean. The tool result is the subagent's final "
                "message verbatim — anything it did not write into that message is lost, so "
                "say in `expected_return` exactly what shape you want. This call blocks "
                "until the subagent finishes. Only available when the user has configured "
                "and enabled a subagent model under Settings -> Backend."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "The complete, self-contained instructions for the subagent. "
                            "It has no memory of this conversation, so include every path, "
                            "constraint and detail it needs to work independently."
                        ),
                    },
                    "expected_return": {
                        "type": "string",
                        "description": (
                            "Optional: exactly what the subagent's final answer should "
                            "contain and how it should be structured (e.g. 'a list of the "
                            "functions that touch self.executor, each as path:line — "
                            "signature'). Omit for a free-form report."
                        ),
                    },
                    "max_rounds": {
                        "type": "integer",
                        "description": "Optional cap on the subagent's tool-call rounds (default 8). Use a higher value for a deeper investigation.",
                        "default": 8,
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a direct question to resolve an ambiguity before proceeding. Use this when you "
                "genuinely need clarification rather than guessing. Provide either a list of choices for the user "
                "to pick from, or omit choices to request free-form text. Set multi_select=true to let the user "
                "tick SEVERAL choices at once — use it when the answers are not mutually exclusive (which features "
                "to implement, which files to touch, which checks to run). A free-text 'Other' answer is always "
                "available to the user, even when choices are given, so never add a placeholder choice like "
                "'Other' or 'Something else' yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to show the user"},
                    "choices": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of choices. If omitted, a free-text answer is expected.",
                    },
                    "allow_custom_answer": {
                        "type": "boolean",
                        "description": "If true and choices are given, still allow the user to type a custom answer instead of picking one.",
                    },
                    "multi_select": {
                        "type": "boolean",
                        "description": (
                            "If true, the user may select MULTIPLE choices (checkboxes) instead of exactly one. "
                            "The answer then comes back as the chosen options joined by ', '. Only meaningful when "
                            "choices are provided. Leave false (the default) for mutually exclusive options."
                        ),
                        "default": False,
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command directly in the system terminal. The command is run from the project root directory. Requires user approval unless 'always allow' is configured for the specific command. Always provide a clear description of what the command does — this helps the user make an informed decision.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute (e.g., 'ls -la', 'git status', 'tree -I __pycache__')"},
                    "description": {"type": "string", "description": "A clear description of what this command does and why it's being run"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)", "default": 30},
                },
                "required": ["command", "description"],
            },
        },
    },
]

WEB_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for a query and return structured results (title, URL, and snippet). "
                "Keep the query SHORT: 2-6 keywords, not a natural sentence or a question — long descriptive "
                "queries return little or nothing. Iterate by shortening or changing a term, not by "
                "adding more terms. "
                "The backend is chosen automatically by the current web-access policy: "
                "lightweight DuckDuckGo HTML in 'search_and_fetch_safe' mode, "
                "DuckDuckGo HTML routed through your local Tor SOCKS5 proxy in 'tor' mode, "
                "headless Chromium in 'open_web' mode, or the user's real Chrome with cookies in "
                "'open_browser' mode. Supports 'duckduckgo' (default) and 'google' engines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The search query: a short keyword string of 2-6 words, NOT a natural sentence. "
                            "Search engines match keywords and degrade sharply as queries get longer — "
                            "each extra descriptive word narrows the result set and usually returns "
                            "nothing useful. Do NOT write this like a natural-language question or like "
                            "a grep pattern (unlike search_in_files, which does want an exact phrase). "
                            "Strip stopwords ('the', 'how', 'what', 'for'), and keep only the distinctive "
                            "terms: 'PySide6 QTableView delegate checkboxes' — not 'how do I add "
                            "checkboxes to a QTableView using a delegate in PySide6'. When a query "
                            "returns nothing, SHORTEN it or swap one term for a synonym; do not add more "
                            "words to it."
                        ),
                    },
                    "engine": {"type": "string", "description": "Search engine: 'duckduckgo' or 'google'. Defaults to duckduckgo.", "enum": ["duckduckgo", "google"], "default": "duckduckgo"},
                    "max_results": {"type": "integer", "description": "Maximum number of results to return (default: 6)", "default": 6},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_text_url",
            "description": (
                "Fetch a URL and return extracted plain text and links. "
                "Scripts, styles, and visual clutter are stripped. "
                "The backend is chosen automatically by the current web-access policy: "
                "plain HTTP in 'search_and_fetch_safe' mode (no JS rendering), "
                "plain HTTP routed through your local Tor SOCKS5 proxy in 'tor' mode (no JS), "
                "headless Chromium in 'open_web' mode (full JS), "
                "or the user's real Chrome with cookies in 'open_browser' mode "
                "(best for SPAs and login-gated pages). Subject to whitelist/blocklist rules."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL to fetch"},
                    "timeout": {"type": "integer", "description": "Seconds to wait for page load (default: 30)", "default": 30},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_screen_capture",
            "description": (
                "Take a screenshot (or scrolling screenshots) of a webpage URL and return rendered images. "
                "The backend is chosen automatically by the current web-access policy: "
                "headless Chromium in 'open_web' mode or the user's real Chrome in 'open_browser' mode. "
                "Not available in 'search_and_fetch_safe' mode (no JS rendering) or 'off'. "
                "mode='full' (default): captures the entire page as multiple viewport-height screenshots. "
                "mode='viewport': captures only the visible viewport area as a single screenshot. "
                "Screenshots are embedded directly in the response for vision models to see."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL to capture"},
                    "mode": {"type": "string", "description": "Screenshot mode: 'viewport' (single visible area) or 'full' (scrolling, multi-chunk for long pages)", "enum": ["viewport", "full"], "default": "full"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_images",
            "description": (
                "Extract images from a webpage URL. Scans the page for <img> tags, Open Graph images, "
                "and Twitter card images. Returns a description of found images (URLs, alt text) "
                "and optionally base64-encoded image data for vision models to see. "
                "The backend is chosen automatically by the current web-access policy: "
                "plain HTTP in 'search_and_fetch_safe' mode (may miss lazy-loaded images), "
                "plain HTTP routed through your local Tor SOCKS5 proxy in 'tor' mode (no JS), "
                "headless Chromium in 'open_web' mode (full JS rendering), "
                "or the user's real Chrome in 'open_browser' mode (full JS, logged-in cookies). "
                "Not available in 'off' mode. Subject to whitelist/blocklist rules."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL to extract images from"},
                    "max_images": {"type": "integer", "description": "Maximum number of images to extract (default: 5)", "default": 5},
                    "max_embed_images": {"type": "integer", "description": "Maximum number of images to base64-encode for embedding (default: 3)", "default": 3},
                },
                "required": ["url"],
            },
        },
    },
]

WRITE_TOOL_NAMES = {"write_file", "replace_in_file", "delete_section", "delete_file", "rename_file", "append_to_file", "prepend_to_file"}
CONTROL_TOOL_NAMES = {"end_turn"}
ASK_USER_TOOL_NAME = "ask_user"
DEPLOY_SUBAGENT_TOOL_NAME = "deploy_subagent"
SCRIPT_TOOL_NAMES = {"run_python_script"}
COMMAND_TOOL_NAMES = {"run_command"}

# Agent-introspection tools: they report the agent's own live state (context
# window budget, elapsed turn time) rather than touching the workspace, the
# web, or the user. Like think_out_loud they are side-effect-free and read-only,
# so they stay available to subagents (they are NOT in SUBAGENT_TOOL_BLOCKLIST).
# Both are handled in ToolExecutor.execute_read_tool, which reaches the live
# Agent through the executor.agent back-reference (set once in Agent.__init__).
META_TOOL_NAMES = {"get_context_budget", "uptime"}

# Desktop-control tools (imported from computer_tools). view_monitor was
# removed: view_desktop already captures every monitor (the full virtual
# desktop), and view_region covers any closer look at a single display.
COMPUTER_TOOL_NAMES = {
    "fetch_monitor_metadata",
    "view_desktop",
    "view_region",
}

ALL_TOOL_NAMES = (
    [t["function"]["name"] for t in TOOL_SCHEMAS]
    + [t["function"]["name"] for t in WEB_TOOL_SCHEMAS]
    + [t["function"]["name"] for t in computer_tools.COMPUTER_TOOL_SCHEMAS]
)

# Tools a deployed subagent is never allowed, regardless of the user's per-tool
# subagent toggles. This is a hard floor the Settings Tools-tab column cannot
# raise, enforced again at execution time by SubagentRunner (which re-checks
# every call against this same set). Rationale, matching the product rule:
#   * every file-editing tool  — a subagent must not touch the workspace; its
#     output is a text report the parent acts on (see core/subagent.py).
#   * run_command              — arbitrary shell, same reason the subagent gets
#     no edits: no unsupervised side effects.
#   * web_search               — the subagent is a cheap worker; broad web
#     discovery is the parent's job. (fetch_text_url etc. stay available.)
#   * ask_user                 — there is no user present inside a subturn.
# deploy_subagent is included so nested deployment can never be offered even
# once that tool exists (a subagent may not spawn its own subagent).
SUBAGENT_TOOL_BLOCKLIST = (
    set(WRITE_TOOL_NAMES)
    | set(COMMAND_TOOL_NAMES)          # run_command
    | {"web_search", ASK_USER_TOOL_NAME, DEPLOY_SUBAGENT_TOOL_NAME}
)

# Default subagent tool state: everything is offered except the hard-blocked
# set. The user then narrows this per tool in the Tools tab ("Subagent" column),
# which is stored under its own settings key and kept separate from the main
# "On" toggles.
def default_subagent_tool_toggles() -> dict:
    return {name: (name not in SUBAGENT_TOOL_BLOCKLIST) for name in ALL_TOOL_NAMES}

# Tool categorization for the Tools tab UI.
# Order of categories determines display order. Tools within each category
# preserve their order from TOOL_SCHEMAS / WEB_TOOL_SCHEMAS.
TOOLS_CATEGORIES = {
    "File System": [
        "list_files",
        "list_directory",
        "read_file",
        "read_multiple_files",
        "search_in_files",
        "grep_regex",
    ],
    "File Editing": [
        "write_file",
        "replace_in_file",
        "delete_section",
        "append_to_file",
        "prepend_to_file",
        "delete_file",
        "rename_file",
    ],
    "Media": [
        "view_image",
        "view_video",
        "media_metadata",
    ],
    "Code": [
        "run_python_script",
        "run_command",
    ],
    "Web": [
        "web_search",
        "fetch_text_url",
        "web_screen_capture",
        "extract_images",
    ],
    "Desktop Tools": [
        "fetch_monitor_metadata",
        "view_desktop",
        "view_region",
    ],
    "Conversation": [
        "think_out_loud",
        "deploy_subagent",
        "end_turn",
        "ask_user",
    ],
    "Meta": [
        "get_context_budget",
        "uptime",
    ],
}

# Reverse lookup: tool_name -> category_name
_TOOL_CATEGORY_MAP = {}
for _cat, _tools in TOOLS_CATEGORIES.items():
    for _t in _tools:
        _TOOL_CATEGORY_MAP[_t] = _cat
del _cat, _tools, _t


def get_tool_category(tool_name: str) -> Optional[str]:
    """Return the category name for a tool, or None if uncategorized."""
    return _TOOL_CATEGORY_MAP.get(tool_name)


ERROR_HINTS = {
    "file_not_found": "Call list_files or list_directory first to confirm the correct relative path and spelling.",
    "multi_match": "Provide a longer, more unique old_text snippet that includes surrounding context lines.",
    "no_match": "Re-read the file with read_file to get its exact current content before proposing a replacement. If changes are already pending, wait for user to accept changes",
    "too_large": "This file is too large to read fully; consider reading it in a targeted way or asking the user for guidance.",
    "escape_root": "Only relative paths inside the project root are allowed. Do not use '..' or absolute paths.",
    "disabled_tool": "This tool is currently disabled by the user in the Tools tab. Ask the user to enable it if you need it.",
    "empty_result": "This search/read returned nothing. Try a broader query, check spelling, or use list_files to explore.",
}


class ToolStats:
    """Lightweight stats: only tracks total calls for the Tools tab."""
    def __init__(self):
        self.counts: Dict[str, int] = {}
        self._listeners: List[Callable[[], None]] = []

    def _ensure(self, name):
        if name not in self.counts:
            self.counts[name] = 0

    def add_listener(self, fn: Callable[[], None]):
        self._listeners.append(fn)

    def _notify(self):
        for fn in self._listeners:
            try:
                fn()
            except Exception:
                pass

    def record_call(self, name: str, _succeeded=None, _result_label=None):
        self._ensure(name)
        self.counts[name] += 1
        self._notify()

    def record_decision(self, _name=None, _approved=None):
        # No-op now — decisions are handled separately via Change Queue
        self._notify()

    def as_rows(self):
        rows = []
        for name in ALL_TOOL_NAMES:
            self._ensure(name)
            rows.append((name, self.counts[name]))
        return rows


class PendingPatch:
    _next_id = 1

    def __init__(self, kind: str, path: str, old_content: str, new_content: str, description: str = "", new_path: str = ""):
        self.id = PendingPatch._next_id
        PendingPatch._next_id += 1
        self.kind = kind  # "create" | "modify" | "delete" | "rename"
        self.path = path
        self.new_path = new_path
        self.old_content = old_content
        self.new_content = new_content
        self.description = description
        self.status = "pending"


class EndTurnSignal(Exception):
    def __init__(self, summary: str = ""):
        self.summary = summary


class ToolExecutor:
    def __init__(self, workspace: Workspace, enabled_tools: Dict[str, bool] = None,
                 draft_store: "DraftStore" = None,
                 subagent_enabled_tools: Dict[str, bool] = None):
        self.workspace = workspace
        self.enabled_tools = enabled_tools if enabled_tools is not None else {n: True for n in ALL_TOOL_NAMES}
        # Separate per-tool enablement for deployed subagents — its own settings
        # key, its own Tools-tab column. Defaults to "everything except the hard
        # blocklist"; main_window overrides this from persisted settings.
        self.subagent_enabled_tools = (
            dict(subagent_enabled_tools) if subagent_enabled_tools is not None
            else default_subagent_tool_toggles()
        )
        # Whether deploy_subagent may be offered to the MAIN agent. MainWindow
        # sets this True only when a subagent model is both configured AND
        # enabled — i.e. there is a second llama-server to hand the worker to.
        # The executor itself never talks to that server (the Agent does, via a
        # client provider it is given); this flag exists purely so the tool is
        # hidden when there is nothing to deploy onto. Keeping the flag here,
        # next to enabled_tools, means active_schemas() and the Tools tab read
        # exactly one source of truth.
        self.subagent_enabled = False
        self.stats = ToolStats()
        self.web_policy = "off"  # "off" | "whitelist_only" | "search_and_fetch_safe" | "tor" | "open_web" | "open_browser"
        self.web_allowlist: List[str] = []
        self.web_blocklist: List[str] = []
        self.staged_paths: Dict[str, str] = {}  # path -> patch kind, for unapplied pending patches
        self.draft_store = draft_store
        # Unified read-side facade over workspace (disk) + draft_store (RAM
        # pending edits) — see core/virtual_fs.py. Tool branches will be
        # migrated to use self.vfs incrementally, replacing their individual
        # ad-hoc "if self.draft_store and self.draft_store.has_draft(...)"
        # checks one at a time.
        self.vfs = VirtualFS(self.workspace, self.draft_store)
        # Back-reference to the live Agent, set once in Agent.__init__
        # (agent.attach_executor_to_agent). None until an Agent owns this
        # executor. The Meta tools (get_context_budget, uptime) read the
        # agent's live context/turn-timer state through this; every use is
        # guarded with `is None` so the executor still works standalone (unit
        # tests, the web_chat_server, subagent runners) and the tools just
        # report that the figure is unavailable rather than crashing.
        self.agent = None

    def attach_agent(self, agent) -> None:
        """Point this executor at the Agent that owns it, so the Meta tools
        (get_context_budget, uptime) can read the agent's live context-window
        and turn-timer state from execute_read_tool. Called once from
        Agent.__init__."""
        self.agent = agent

    def mark_staged(self, path: str, kind: str):
        self.staged_paths[path] = kind

    def clear_staged(self, path: str):
        self.staged_paths.pop(path, None)

    def is_enabled(self, name: str) -> bool:
        return self.enabled_tools.get(name, True)

    def set_tor_proxy(self, host: str, port: int) -> None:
        """Point the 'tor' web policy mode at a non-default local SOCKS5 proxy
        (e.g. 127.0.0.1:9150 for Tor Browser instead of the daemon's 9050)."""
        from . import web_tools as _wt
        _wt.set_tor_proxy(host, port)

    def active_schemas(self) -> List[Dict[str, Any]]:
        schemas = [t for t in TOOL_SCHEMAS if self.is_enabled(t["function"]["name"])]
        if self.web_policy != "off":
            schemas += [t for t in WEB_TOOL_SCHEMAS if self.is_enabled(t["function"]["name"])]
        # Desktop-control tools are always available when enabled
        schemas += [t for t in computer_tools.COMPUTER_TOOL_SCHEMAS if self.is_enabled(t["function"]["name"])]
        # deploy_subagent is only offered when there is actually a subagent model
        # to deploy onto (configured + enabled). When it is off the tool simply
        # is not in the list, so the model never tries to call it — the same
        # "hidden, not offered" contract as disabled tools — rather than calling
        # it and getting a runtime error back.
        if not self.subagent_enabled:
            schemas = [t for t in schemas
                       if t["function"]["name"] != DEPLOY_SUBAGENT_TOOL_NAME]
        return schemas

    # ---- subagent tool access (separate from the main "On" toggles) ----

    def is_enabled_for_subagent(self, name: str) -> bool:
        """Whether a tool is offered to a deployed subagent.

        Independent of the main is_enabled(): the Tools tab keeps a separate
        "Subagent" column. The hard SUBAGENT_TOOL_BLOCKLIST is applied here too,
        so a stale or hand-edited setting can never re-offer a forbidden tool;
        SubagentRunner checks the same set again at execution time."""
        if name in SUBAGENT_TOOL_BLOCKLIST:
            return False
        return self.subagent_enabled_tools.get(name, name not in SUBAGENT_TOOL_BLOCKLIST)

    def subagent_schemas(self) -> List[Dict[str, Any]]:
        """The tool list a deployed subagent is offered.

        Mirrors active_schemas() but filters on the *subagent* toggles rather
        than the main ones, and always drops the hard blocklist (so no editing,
        run_command, web_search, or ask_user can appear no matter what the user
        ticked). Web tools follow the same web-policy gate as the main agent —
        except web_search, which is blocklisted outright for subagents. The
        control tools (end_turn) and read/observe tools stay available, which is
        exactly the set the product rule wants: read and think freely, but no
        side effects and no way to stall waiting on a user."""
        schemas = [t for t in TOOL_SCHEMAS
                   if self.is_enabled_for_subagent(t["function"]["name"])]
        if self.web_policy != "off":
            schemas += [t for t in WEB_TOOL_SCHEMAS
                        if self.is_enabled_for_subagent(t["function"]["name"])]
        schemas += [t for t in computer_tools.COMPUTER_TOOL_SCHEMAS
                    if self.is_enabled_for_subagent(t["function"]["name"])]
        return schemas


    def execute_read_tool(self, name: str, args: Dict[str, Any]) -> str:
        try:
            if name == "list_files":
                files = self.vfs.list_files()
                if not files:
                    self.stats.record_call(name, True, "empty")
                    return f"(empty project) {ERROR_HINTS['empty_result']}"
                lines = []
                for f in files:
                    info = self.vfs.read_info(f)
                    if info.content is not None:
                        # splitlines() counts lines the way a reader does: a
                        # trailing newline terminates the last line rather than
                        # starting an empty one, so "a\nb\n" is 2 lines and not
                        # 3 (content.count("\n") + 1 reported the phantom line).
                        loc = len(info.content.splitlines())
                        tag = " [draft — not yet applied to disk]" if info.is_draft else ""
                        lines.append(f"{f} ({info.size} bytes, {loc} lines){tag}")
                    else:
                        lines.append(f"{f} (binary/unreadable)")
                self.stats.record_call(name, True, "ok")
                return "\n".join(lines)
            elif name == "list_directory":
                # A missing directory must read as an error, not as an empty
                # one: "(empty directory)" tells the model the path is fine and
                # simply has nothing in it, so it never suspects a wrong or
                # misspelled path and keeps building on it.
                try:
                    entries = self.vfs.list_directory(args.get("path", "."),
                                                      strict=True)
                except WorkspaceError as e:
                    self.stats.record_call(name, False, "error")
                    return f"Error: {e}. {ERROR_HINTS['file_not_found']}"
                result = "\n".join(entries) if entries else "(empty directory)"
                self.stats.record_call(name, True, "ok" if entries else "empty")
                return result
            elif name == "read_file":
                fpath = args["path"]
                start_line = args.get("start_line", 1)
                end_line = args.get("end_line", -1)
                # Draft-vs-disk decision now lives in VirtualFS — every tool
                # that reads project state goes through the same logic
                # instead of each re-implementing its own has_draft() check.
                is_draft = self.vfs.is_draft(fpath)
                full_content = self.vfs.read(fpath)
                all_lines = full_content.splitlines(keepends=True)
                total_lines = len(all_lines)

                # Resolve -1 sentinels and validate range
                if start_line == -1:
                    start_line = 1
                if end_line == -1 or end_line > total_lines:
                    end_line = total_lines
                if start_line < 1:
                    start_line = 1
                if start_line > total_lines:
                    return (
                        f"Error: start_line {start_line} is beyond the end of the file "
                        f"({total_lines} lines). {ERROR_HINTS['empty_result']}"
                    )
                if start_line > end_line:
                    return f"Error: start_line ({start_line}) must be <= end_line ({end_line})."

                # Slice the line range (1-based inclusive)
                selected = all_lines[start_line - 1 : end_line]
                result = "".join(selected)

                # Add a range header when showing a subset
                if start_line != 1 or end_line != total_lines:
                    result = f"[Lines {start_line}-{end_line} of {total_lines} total]\n" + result

                if is_draft:
                    result = (
                        f"[DRAFT: This file has pending changes not yet applied on disk. "
                        "Showing the draft version.]\n\n" + result
                    )
                    self.stats.record_call(name, True, "draft")
                    if fpath in self.staged_paths:
                        result += (
                            f"\n\n[NOTE: This file also has an unapproved pending "
                            f"{self.staged_paths[fpath]} patch awaiting user review.]"
                        )
                    return result
                self.stats.record_call(name, True, "ok")
                if fpath in self.staged_paths:
                    result = (
                        f"[NOTE: This file has an unapproved pending {self.staged_paths[fpath]} patch "
                        "awaiting user review. The content below is the OLD, unmodified version — your proposed "
                        "changes are not yet applied on disk.]\n\n" + result
                    )
                return result
            elif name == "read_multiple_files":
                MAX_BYTES = 60000
                chunks = []
                for p in args.get("paths", []):
                    info = self.vfs.read_info(p)
                    if info.is_draft:
                        chunks.append(f"--- {p} ---\n[DRAFT]\n{info.content}")
                        continue
                    if not info.exists:
                        chunks.append(f"--- {p} --- (File not found: {p}. {ERROR_HINTS['file_not_found']})")
                        continue
                    if info.size is not None and info.size > MAX_BYTES:
                        chunks.append(
                            f"--- {p} --- (file is {info.size} bytes, over {MAX_BYTES}-byte batch limit. "
                            f"Call read_file on {p} individually instead.)"
                        )
                        continue
                    if info.content is None:
                        chunks.append(f"--- {p} --- (Could not read {p} as text.)")
                        continue
                    chunks.append(f"--- {p} ---\n{info.content}")
                self.stats.record_call(name, True, "ok")
                return "\n\n".join(chunks) if chunks else "(no files specified)"
            elif name == "search_in_files":
                matches = self.vfs.search(args["query"])
                if not matches:
                    self.stats.record_call(name, True, "empty")
                    return f"No matches found. {ERROR_HINTS['empty_result']}"
                lines = [f"{m['file']}:{m['line']}: {m['text']}" for m in matches]
                self.stats.record_call(name, True, "ok")
                return "\n".join(lines)
            elif name == "grep_regex":
                matches = self.vfs.search_regex(args["pattern"])
                if not matches:
                    self.stats.record_call(name, True, "empty")
                    return f"No matches found for pattern. {ERROR_HINTS['empty_result']}"
                lines = [f"{m['file']}:{m['line']}: {m['text']}" for m in matches]
                self.stats.record_call(name, True, "ok")
                return "\n".join(lines)
            elif name == "think_out_loud":
                thought = args.get("thought", "")
                self.stats.record_call(name, True, "ok")
                return thought
            elif name == "get_context_budget":
                # Context-window budget of the live conversation. Reaches the
                # Agent through the executor.agent back-reference; the token
                # figures and the limit are the SAME ones the Context tab meters
                # (see ui/context_tab.py), so the tool never disagrees with the
                # UI. Falls back gracefully when there is no agent attached or
                # no limit configured, rather than raising.
                agent = self.agent
                if agent is None:
                    self.stats.record_call(name, False, "no_agent")
                    return ("Context budget is unavailable: this tool executor is "
                            "not attached to a live agent, so there is no "
                            "conversation context to measure.")
                limit = int(getattr(agent, "context_limit", 0) or 0)
                # Prefer the server's last reported real token count; fall back
                # to the ~4-chars/token heuristic (mirrors _current_usage_percent).
                real = getattr(agent, "last_known_real_tokens", None)
                used = int(real) if real is not None else int(agent.current_context_tokens())
                source = "server-reported" if real is not None else "heuristic estimate (~4 chars/token)"
                if limit <= 0:
                    self.stats.record_call(name, True, "no_limit")
                    return (
                        "Context budget: no context limit is configured, so the "
                        f"absolute size is unknown. Used: {used} tokens ({source})."
                    )
                free = max(0, limit - used)
                pct = 100.0 * used / limit
                self.stats.record_call(name, True, "ok")
                return (
                    f"Context budget ({source}):\n"
                    f"- Absolute context size (limit): {limit} tokens\n"
                    f"- Used: {used} tokens\n"
                    f"- Free: {free} tokens\n"
                    f"- Used: {pct:.1f}%"
                )
            elif name == "uptime":
                # Elapsed wall-clock time since the current turn started — i.e.
                # since the agent last went idle (the turn timer is (re)started
                # in Agent._start_turn_timer on add_user_message/prepare_continue,
                # exactly the moment activity flips from 'idle' to working).
                agent = self.agent
                if agent is None:
                    self.stats.record_call(name, False, "no_agent")
                    return ("Uptime is unavailable: this tool executor is not "
                            "attached to a live agent, so there is no turn timer "
                            "to read.")
                start = getattr(agent, "_turn_start_time", None)
                if start is None:
                    self.stats.record_call(name, True, "idle")
                    return ("Uptime: no response/tool chain is currently running "
                            "(the agent has not started a turn since it last went "
                            "idle).")
                elapsed = time.monotonic() - start
                if elapsed < 0:
                    elapsed = 0.0
                h, rem = divmod(int(elapsed), 3600)
                m, s = divmod(rem, 60)
                if h:
                    human = f"{h}h {m}m {s}s"
                elif m:
                    human = f"{m}m {s}s"
                else:
                    human = f"{s}s"
                self.stats.record_call(name, True, "ok")
                return (
                    f"Uptime: {elapsed:.1f} seconds ({human}) since this turn "
                    "started — the time since the agent last went idle, i.e. the "
                    "duration of the current response/tool chain up to now."
                )
            elif name == "media_metadata":
                mpath = args.get("path", "")
                if not mpath:
                    self.stats.record_call(name, False, "error")
                    return "Error: media_metadata needs a 'path' to an image or video file."
                try:
                    info = self.workspace.media_info(mpath)
                except WorkspaceError as e:
                    self.stats.record_call(name, False, "error")
                    return (f"Error: {e} "
                            "Call list_files first to confirm the path, or use "
                            "media_metadata on a supported image/video file.")
                except ImportError as e:
                    self.stats.record_call(name, False, "error")
                    return (f"Error: reading media metadata needs a dependency that is "
                            f"not installed ({e}). Install pillow and/or opencv-python.")
                except Exception as e:
                    self.stats.record_call(name, False, "error")
                    return f"Error: could not read metadata for '{mpath}': {e}"
                skip = {"path"}
                lines = [f"Media metadata for '{mpath}':"]
                for key, value in info.items():
                    if key in skip:
                        continue
                    lines.append(f"- {key}: {value}")
                self.stats.record_call(name, True, "ok")
                return "\n".join(lines)
            elif name == "append_to_file":
                fpath = args["path"]
                content = args.get("content", "")
                try:
                    existing = self.workspace.read_file(fpath)
                    self.workspace.append_file(fpath, content)
                    self.stats.record_call(name, True, "ok")
                    return f"Appended {len(content)} characters to '{fpath}'."
                except WorkspaceError as e:
                    self.stats.record_call(name, False, "error")
                    return f"Error: {e}. {ERROR_HINTS['file_not_found']}"
            elif name == "prepend_to_file":
                fpath = args["path"]
                content = args.get("content", "")
                try:
                    existing = self.workspace.read_file(fpath)
                    self.workspace.prepend_file(fpath, content)
                    self.stats.record_call(name, True, "ok")
                    return f"Prepended {len(content)} characters to '{fpath}'."
                except WorkspaceError as e:
                    self.stats.record_call(name, False, "error")
                    return f"Error: {e}. {ERROR_HINTS['file_not_found']}"
            else:
                self.stats.record_call(name, False, "unknown_tool")
                return f"Unknown read tool: {name}"
        except WorkspaceError as e:
            self.stats.record_call(name, False, "error")
            hint = ERROR_HINTS.get("file_not_found", "")
            return f"Error: {e}. {hint}"

    def build_pending_patch(self, name: str, args: Dict[str, Any]) -> PendingPatch:
        path = args.get("path", "")
        try:
            if name == "write_file":
                existed = self.workspace.exists(path)
                if existed:
                    try:
                        old = self.workspace.read_file(path)
                    except WorkspaceError:
                        old = "(binary/non-text file — previous content not shown in diff)"
                else:
                    old = ""
                # Draft: compound or create
                if self.draft_store:
                    if self.draft_store.has_draft(path):
                        draft = self.draft_store.get_draft(path)
                        if draft:
                            self.draft_store.set_draft(path, draft.resolved_content, args["content"], "write")
                        # patch built later from final draft state
                    else:
                        self.draft_store.set_draft(path, old, args["content"], "write")
                patch = PendingPatch(
                    kind="modify" if existed else "create",
                    path=path, old_content=old, new_content=args["content"],
                )
            elif name in ("replace_in_file", "delete_section"):
                # delete_section is replace_in_file with an empty new_text.
                if name == "delete_section":
                    old_text = args["section"]
                    new_text = ""
                else:
                    old_text = args["old_text"]
                    new_text = args["new_text"]
                # unique defaults to True. When False, every occurrence of
                # old_text is modified at once (the explicitly disruptive mode).
                unique = args.get("unique", True)
                label = "section" if name == "delete_section" else "old_text"
                if self.draft_store and self.draft_store.has_draft(path):
                    # Draft: compound on resolved content
                    old_full_draft = self.draft_store.get_draft(path)
                    if old_full_draft is None or old_text not in old_full_draft.resolved_content:
                        # Fall back: read from disk
                        try:
                            old_full = self.workspace.read_file(path)
                        except WorkspaceError:
                            # File doesn't exist on disk — use draft content if available
                            if old_full_draft:
                                old_full = old_full_draft.resolved_content
                            else:
                                old_full = ""
                    else:
                        old_full = old_full_draft.resolved_content
                    if old_text not in old_full:
                        raise WorkspaceError(f"{label} not found verbatim in {path}. {ERROR_HINTS['no_match']}")
                    if unique and old_full.count(old_text) > 1:
                        raise WorkspaceError(f"{label} matches multiple locations in {path}. {ERROR_HINTS['multi_match']} Or set unique=false to apply to all matches.")
                    # Compound on draft (replace every occurrence in all-matches mode)
                    self.draft_store.apply_replace_on_draft(path, old_text, new_text, replace_all=not unique)
                    # In unique mode there is exactly one occurrence (2+ was rejected above),
                    # so an unbounded replace is equivalent to replace-first-occurrence.
                    new_full = old_full.replace(old_text, new_text)
                else:
                    # No draft: check if file exists on disk first
                    try:
                        old_full = self.workspace.read_file(path)
                    except WorkspaceError:
                        old_full = ""
                    if old_text not in old_full:
                        raise WorkspaceError(f"{label} not found verbatim in {path}. {ERROR_HINTS['no_match']}")
                    if unique and old_full.count(old_text) > 1:
                        raise WorkspaceError(f"{label} matches multiple locations in {path}. {ERROR_HINTS['multi_match']} Or set unique=false to apply to all matches.")
                    # Same reasoning as the draft branch: unique mode already
                    # guarantees exactly one occurrence.
                    new_full = old_full.replace(old_text, new_text)
                    # Create draft if one doesn't exist
                    if self.draft_store:
                        self.draft_store.set_draft(path, old_full, new_full, "replace")
                patch = PendingPatch(kind="modify", path=path, old_content=old_full, new_content=new_full)
            elif name == "delete_file":
                if self.workspace.exists(path):
                    try:
                        old = self.workspace.read_file(path)
                    except WorkspaceError:
                        old = "(binary/non-text file)"
                else:
                    old = ""
                if self.draft_store:
                    self.draft_store.delete_draft(path)
                patch = PendingPatch(kind="delete", path=path, old_content=old, new_content="")
            elif name == "rename_file":
                old_path = args["old_path"]
                new_path = args["new_path"]
                try:
                    old_content = self.workspace.read_file(old_path)
                except WorkspaceError:
                    old_content = "(binary/non-text file)"
                if self.draft_store:
                    self.draft_store.rename_draft(old_path, new_path)
                patch = PendingPatch(kind="rename", path=old_path, new_path=new_path, old_content=old_content, new_content=old_content)
            elif name == "append_to_file":
                path = args.get("path", "")
                content = args.get("content", "")
                try:
                    old_full = self.workspace.read_file(path)
                except WorkspaceError:
                    old_full = ""
                if self.draft_store:
                    if self.draft_store.has_draft(path):
                        # Compound onto the draft. new_full must describe the
                        # SAME content the draft now holds, or the staged patch
                        # the user reviews (and apply_patch later writes) would
                        # silently drop every earlier edit in this draft.
                        self.draft_store.append_to_draft(path, content)
                        draft = self.draft_store.get_draft(path)
                        base_full = draft.base_content if draft else old_full
                        new_full = draft.resolved_content if draft else old_full + content
                    else:
                        new_full = old_full + content
                        self.draft_store.set_draft(path, old_full, new_full, "append")
                        base_full = old_full
                else:
                    new_full = old_full + content
                    base_full = old_full
                patch = PendingPatch(kind="modify", path=path, old_content=base_full, new_content=new_full)
            elif name == "prepend_to_file":
                path = args.get("path", "")
                content = args.get("content", "")
                try:
                    old_full = self.workspace.read_file(path)
                except WorkspaceError:
                    old_full = ""
                if self.draft_store:
                    if self.draft_store.has_draft(path):
                        # See append: diff against the draft, not the disk copy.
                        self.draft_store.prepend_to_draft(path, content)
                        draft = self.draft_store.get_draft(path)
                        base_full = draft.base_content if draft else old_full
                        new_full = draft.resolved_content if draft else content + old_full
                    else:
                        new_full = content + old_full
                        self.draft_store.set_draft(path, old_full, new_full, "prepend")
                        base_full = old_full
                else:
                    new_full = content + old_full
                    base_full = old_full
                patch = PendingPatch(kind="modify", path=path, old_content=base_full, new_content=new_full)
            else:
                raise WorkspaceError(f"Unknown write tool: {name}")
            self.stats.record_call(name, True, "staged")
            return patch
        except WorkspaceError as e:
            self.stats.record_call(name, False, "error")
            raise

    def apply_patch(self, patch: PendingPatch) -> None:
        if patch.kind in ("create", "modify"):
            self.workspace.write_file(patch.path, patch.new_content)
        elif patch.kind == "delete":
            self.workspace.delete_file(patch.path)
        elif patch.kind == "rename":
            self.workspace.write_file(patch.new_path, patch.new_content)
            self.workspace.delete_file(patch.path)

    def run_python_script(self, rel_path: str, args: Optional[List[str]] = None, timeout: int = 10) -> str:
        import subprocess, sys
        full_path = self.workspace._resolve(rel_path)
        if not full_path.exists():
            return f"Error: script not found: {rel_path}"
        cmd = [sys.executable, str(full_path)] + [str(a) for a in (args or [])]
        try:
            proc = subprocess.run(
                cmd, cwd=str(self.workspace.root), timeout=timeout,
                capture_output=True, text=True,
            )
            out = (proc.stdout or "")[-4000:]
            err = (proc.stderr or "")[-2000:]
            return f"Exit code {proc.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
        except subprocess.TimeoutExpired:
            return f"Script timed out after {timeout}s and was aborted."
        except Exception as e:
            return f"Error running script: {e}"

    @staticmethod
    def extract_command_name(command: str) -> str:
        """Extract the base command name from a shell command string.
        
        E.g. 'tree -I __pycache__' -> 'tree'
             'git diff HEAD' -> 'git'
             'cd src && ls' -> 'cd'
        """
        import shlex
        try:
            parts = shlex.split(command)
        except ValueError:
            # If shlex fails (e.g. unbalanced quotes), split on whitespace
            parts = command.split()
        if parts:
            return parts[0].strip()
        return ""

    @staticmethod
    def parse_shell_commands(command: str) -> List[str]:
        """Parse a shell command string and return a list of base command names.
        
        Splits on shell operators: &&, ||, ;, | and newlines.
        Treats # as a comment (everything from # to end of line is ignored).
        Skips leading redirection tokens like <, >, 2>, 2>&1, etc.
        
        Examples:
            'cd /tmp && rm -rf *'          -> ['cd', 'rm']
            'grep foo bar | sort | uniq'   -> ['grep', 'sort', 'uniq']
            'ls && find . -name *.py | wc -l' -> ['ls', 'find', 'wc']
            'git status'                   -> ['git']
            'echo "hello" && cat file'     -> ['echo', 'cat']
            'python script.py arg1 arg2'   -> ['python']
            'echo hello\\ngrep world'      -> ['echo', 'grep']
            '< input.txt cat'              -> ['cat']
            '2>/dev/null command'          -> ['command']
        
        Handles quoted strings properly (e.g. 'echo "foo && bar"' -> ['echo']).
        """
        import shlex
        import re
        commands: List[str] = []

        def _is_redirect(tok: str) -> bool:
            """Check if a token is a shell redirection."""
            if tok in ('<', '>'):
                return True
            if re.match(r'^[0-9]+[>&]', tok):
                return True
            if tok.startswith('<(') or tok.startswith('>('):
                return True
            return False

        def _strip_comments(text: str) -> str:
            """Remove # comments from text, respecting quotes."""
            cleaned = []
            in_sq = False
            in_dq = False
            i = 0
            while i < len(text):
                ch = text[i]
                if ch == "'" and not in_dq:
                    in_sq = not in_sq
                    cleaned.append(ch)
                elif ch == '"' and not in_sq:
                    in_dq = not in_dq
                    cleaned.append(ch)
                elif ch == '#' and not in_sq and not in_dq:
                    # Replace with a space and skip rest of line
                    cleaned.append(' ')
                    i += 1
                    while i < len(text):
                        nc = text[i]
                        if nc == '\\' and i + 1 < len(text):
                            i += 2
                        elif nc in ('\n', ';', '&', '|'):
                            break
                        else:
                            i += 1
                    continue
                else:
                    cleaned.append(ch)
                i += 1
            return ''.join(cleaned)

        def _extract_command(tokens: List[str]) -> Optional[str]:
            """Extract base command from token list, skipping leading redirections.
            
            Bare < and > consume the next token as their argument (e.g. '< file.txt').
            <(...> and >(...) process substitutions also consume the next token.
            N> or N&> redirections (e.g. 2>/dev/null, 2>&1) are self-contained.
            """
            idx = 0
            while idx < len(tokens):
                tok = tokens[idx]
                if tok in ('<', '>') or tok.startswith('<(') or tok.startswith('>('):
                    idx += 2  # skip the redirection token and its argument
                elif re.match(r'^[0-9]+[>&]', tok):
                    idx += 1  # self-contained, e.g. 2>/dev/null or 2>&1
                else:
                    break
            if idx < len(tokens):
                base = tokens[idx].strip()
                return base if base else None
            return None

        # Normalize: replace newlines with ';' and CR with space
        cmd = command.replace('\r', ' ').replace('\n', ';')

        # Split on top-level ';' (not inside quotes) into separate command groups
        parts: List[str] = []
        current: List[str] = []
        in_sq = False
        in_dq = False
        for ch in cmd:
            if ch == "'" and not in_dq:
                in_sq = not in_sq
                current.append(ch)
            elif ch == '"' and not in_sq:
                in_dq = not in_dq
                current.append(ch)
            elif ch == ';' and not in_sq and not in_dq:
                parts.append(''.join(current))
                current = []
            else:
                current.append(ch)
        if current:
            parts.append(''.join(current))

        for part in parts:
            if not part.strip():
                continue
            # Strip comments from this part
            part = _strip_comments(part)
            if not part.strip():
                continue
            # Tokenize respecting quotes
            try:
                tokens = shlex.split(part)
            except ValueError:
                tokens = part.split()

            # Split on &&, ||, | operators
            operators = {'&&', '||'}
            segments: List[List[str]] = []
            cur_seg: List[str] = []
            for tok in tokens:
                if tok in operators:
                    if cur_seg:
                        segments.append(cur_seg)
                        cur_seg = []
                elif tok == '|':
                    if cur_seg:
                        segments.append(cur_seg)
                        cur_seg = []
                else:
                    cur_seg.append(tok)
            if cur_seg:
                segments.append(cur_seg)

            for seg in segments:
                base = _extract_command(seg)
                if base:
                    commands.append(base)

        return commands

    @staticmethod
    def check_commands_allowed(command: str, allowlist: List[Dict[str, str]]) -> bool:
        """Check if ALL commands in the shell command string are in the allowlist.
        
        Args:
            command: The full shell command string.
            allowlist: List of dicts with 'command' key (e.g. [{'command': 'git'}, ...])
        
        Returns:
            True if every base command is in the allowlist, False otherwise.
        """
        base_commands = ToolExecutor.parse_shell_commands(command)
        allowed_names = [a.get("command", "").lower() for a in allowlist]
        return all(cmd.lower() in allowed_names for cmd in base_commands)

    def run_command(self, command: str, description: str = "", timeout: int = 30) -> str:
        import subprocess
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(self.workspace.root), timeout=timeout,
                capture_output=True, text=True,
            )
            out = (proc.stdout or "")[-4000:]
            err = (proc.stderr or "")[-2000:]
            parts = []
            parts.append(f"Exit code: {proc.returncode}")
            if out.strip():
                parts.append(f"STDOUT:\n{out}")
            if err.strip():
                parts.append(f"STDERR:\n{err}")
            return "\n".join(parts)
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s and was aborted."
        except Exception as e:
            return f"Error running command: {e}"




# ── Quick self-test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Quick import test — just verify no import errors
    import sys
    sys.path.insert(0, '.')  # Add current dir to path
    try:
        from core.computer_tools import (
            COMPUTER_TOOL_SCHEMAS,
            run_computer_tool,
        )
        from core.tools import COMPUTER_TOOL_NAMES
        print("✓ computer_tools imported successfully")
        print(f"  Tool schemas: {len(COMPUTER_TOOL_SCHEMAS)}")
        for s in COMPUTER_TOOL_SCHEMAS:
            print(f"    - {s['function']['name']}")
        print(f"  Computer tool names: {COMPUTER_TOOL_NAMES}")
    except Exception as e:
        print(f"✗ Import failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    # Run the shell command parser self-test
    test_cases = [
        # Basic operators
        ("cd /tmp && rm -rf *", ["cd", "rm"]),
        ("grep foo bar | sort | uniq", ["grep", "sort", "uniq"]),
        ("git status", ["git"]),
        ("ls && find . -name '*.py' | wc -l", ["ls", "find", "wc"]),
        ("echo 'hello && world'", ["echo"]),  # && inside quotes should NOT split
        ("python script.py arg1 arg2", ["python"]),
        ("ls ; rm file.txt ; touch new.txt", ["ls", "rm", "touch"]),
        ("cat file.txt || echo 'file not found'", ["cat", "echo"]),
        ("sudo apt-get update && sudo apt-get upgrade", ["sudo", "sudo"]),
        # # comment handling
        ("echo hello # this is a comment && echo world", ["echo", "echo"]),
        ("echo '# not a comment'", ["echo"]),
        ("echo '# not a comment' && echo ok", ["echo", "echo"]),
        ("echo hello # comment", ["echo"]),
        # Newline handling (newlines act as ; separators)
        ("echo hello\ngrep world", ["echo", "grep"]),
        ("echo hello\n# comment\ngrep world", ["echo", "grep"]),
        # < and > redirection handling
        ("ls < input.txt", ["ls"]),
        ("ls > output.txt && grep foo", ["ls", "grep"]),
        ("< input.txt cat", ["cat"]),
        ("2>/dev/null command", ["command"]),
        ("2>&1 command arg", ["command"]),
        ("0>/dev/null cmd", ["cmd"]),
    ]
    
    all_pass = True
    for cmd, expected in test_cases:
        result = ToolExecutor.parse_shell_commands(cmd)
        status = "✓" if result == expected else "✗"
        if result != expected:
            all_pass = False
        print(f"{status} parse_shell_commands('{cmd}')")
        if result != expected:
            print(f"    Expected: {expected}")
            print(f"    Got:      {result}")
    
    if all_pass:
        print("\nAll tests passed!")
    else:
        print("\nSome tests FAILED!")
        sys.exit(1)
    
    print("✓ Shell command parser test passed!")
    
    # Test the computer tool dispatcher with graceful degradation (no mss/pynput)
    try:
        result = run_computer_tool("fetch_monitor_metadata", {})
        print(f"\n✓ fetch_monitor_metadata works (graceful degradation):")
        print(f"  Status: {'failed' if 'mss' in result['text'].lower() else 'ok'}")
        assert "Result:" in result["text"], "Missing Result field"
        assert "Context:" in result["text"], "Missing Context field"
        assert "Confidence:" in result["text"], "Missing Confidence field"
        assert "Next step:" in result["text"], "Missing Next step field"
        assert result["image"] is None, "fetch_monitor_metadata should not return image"
        print("  ✓ All required fields present")
        
        # Test take_screenshot
        result = run_computer_tool("take_screenshot", {"monitor_id": "monitor_1"})
        print(f"\n✓ take_screenshot works (graceful degradation):")
        print(f"  Has image: {result['image'] is not None}")
        assert "Result:" in result["text"], "Missing Result field"
        print("  ✓ All required fields present")
        
        # Test take_screenshot_with_marker
        result = run_computer_tool("take_screenshot_with_marker", {"target_x": 0.5, "target_y": 0.5})
        print(f"\n✓ take_screenshot_with_marker works (graceful degradation):")
        print(f"  Has image: {result['image'] is not None}")
        assert "Result:" in result["text"], "Missing Result field"
        assert "Action status:" in result["text"], "Missing Action status field"
        print("  ✓ All required fields present")
        
        # Test move_mouse
        result = run_computer_tool("move_mouse", {"x": 0.5, "y": 0.5})
        print(f"\n✓ move_mouse works (graceful degradation):")
        assert "Result:" in result["text"]
        assert result["image"] is None
        print("  ✓ All required fields present")
        
        # Test click
        result = run_computer_tool("click", {"button": "left"})
        print(f"\n✓ click works (graceful degradation):")
        assert "Result:" in result["text"]
        assert result["image"] is None
        print("  ✓ All required fields present")
        
        # Test scroll
        result = run_computer_tool("scroll", {"dy": -5})
        print(f"\n✓ scroll works (graceful degradation):")
        assert "Result:" in result["text"]
        assert result["image"] is None
        print("  ✓ All required fields present")
        
        # Test take_screenshot_all_monitors
        result = run_computer_tool("take_screenshot_all_monitors", {})
        print(f"\n✓ take_screenshot_all_monitors works (graceful degradation):")
        print(f"  Has image: {result['image'] is not None}")
        assert "Result:" in result["text"]
        print("  ✓ All required fields present")
        
        print("\n" + "="*50)
        print("ALL COMPUTER TOOL TESTS PASSED!")
        print("="*50)
    except Exception as e:
        print(f"\n✗ Computer tool test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("✓ Import test passed!")
