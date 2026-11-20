# Hearth — progress against AUDIT_SILENT_DECISIONS.md

**Companion to `AUDIT_SILENT_DECISIONS.md`.** That file is the audit (read-only, findings).
This one is the remediation log: what was changed, what was verified, what was deliberately
left alone. Audit citations (`#1`, `B2`, …) refer to its finding IDs.

Suite status at time of writing: **`Ran 219 tests … OK (skipped=12)`**, and green with
`HEARTH_RUN_HTTP=1` too (`OK (skipped=2)`). Baseline before this work was
`Ran 204 tests / FAILED (failures=1, skipped=12)`.

---

## Scorecard

| Finding | Sev | Status | What changed | Test |
|---|---|---|---|---|
| #1 / B3 workspace listing capped at 80, prompt claimed "every file" | Critical | **Fixed** | Cap is `agent.workspace_listing_entries`; listing appends `…and more (the listing stops here)`; preamble generated from the real cap; `list_tree` now reports `.truncated` | `test_workspace_listing_cap_is_settings_driven_and_announced` |
| #2 upload silently overwrote different bytes | Critical | **Fixed (reported, not blocked)** | `replaced` / `replaced_different_content` on every artifact, `upload_replaced` audit event, amber chat warning | `test_upload_reports_when_it_replaced_different_bytes` |
| #3 `fs_list` silent 500 | High | **Fixed** | `agent.fs_list_entries`; result says `truncated at N entries — more exist here` (reads `list_tree().truncated`) | `AuditRegressionTests.test_fs_list_announces_its_cap` |
| #4 approved gated call executed without freshness check | High | **Fixed** | `agent.pending_max_age_s` (default 3600, 0 = off); stale approvals refused + audit-logged | `test_stale_approval_is_not_executed` |
| #5 prompt sections swallowed to `""` | High | **Fixed** | Explicit "listing FAILED" / "a project EXISTS but could not be read" text + audit events | `test_workspace_listing_failure_says_failed_not_empty` |
| #6 `pdf_read` clamps 40 pages → 6 silently | High | **Fixed (announced; knob declined)** | Result states the clamp and where to continue; description quotes its own limit | `test_pdf_read_announces_its_page_clamp` |
| #7 provider if-chain + silent stub fallback | High | **Fixed** | `manager.PROVIDERS` registry, `Provider.capabilities()`, `last_fallback_note` → `provider_fallback` chat notice | 2 tests (registry, capabilities) |
| #8 hardcoded 25 MiB upload cap | High | **Fixed** | `uploads.max_bytes`; `/api/upload` returns `max_bytes`; client caches it | `test_upload_limit_is_an_admin_setting` |
| #9 client showed 6 links vs `web.max_links=30` | Medium | **Fixed** | Search results unsliced; page links capped at 12 with "showing 12 of N"; setting help rewritten | static-reviewed (see *Not verified*) |
| #10 replay resolved downloads by filename | Medium | **Fixed** | `meta.download` persisted on tool rows; replay resolves by artifact id, name-scan as fallback | covered via #2's artifact-id assertions |
| #11 single-process dicts; README recommended `--workers` | Medium | **Fixed (warned)** | Boot warning on `WEB_CONCURRENCY>1`; README rewritten to forbid multi-worker | `test_single_worker_assumption_is_warned_about` |
| #12 caption cache evicted by insertion order | Medium | **Fixed** | LRU: touched on read, oldest `_used_at` evicted; fallback path retained | `test_caption_cache_evicts_least_recently_used` |
| #13 UI chip truncation unlabelled | Medium | **Fixed** | `ui_preview` appends "(truncated for display — N of M …; the model may have seen more)" | `test_ui_preview_is_generous_but_bounded` (updated) |
| #14 filesystem policy spread over 4 places / 3 constants | Medium | **Partly fixed** | 80 and 500 now settings read by prompt + tool; preamble derives from the cap. No single `workspace_visibility` object | — |
| #15 / B2 43 dead lines after `_merge_progress` return | Low | **Fixed** | Deleted; AST test asserts no unreachable statements follow the final return | `test_no_dead_duplicate_of_progress_event` |
| #16 orchestrator god-module (1 976 lines) | Low | **Not attempted** | See *Remaining work* | — |
| #17 two hand-maintained tool registries | Low | **Fixed** | `Tool.label` + derived `tools.TOOL_LABELS_BY_NAME`; `_TOOL_LABELS` demoted to fallback | `test_tool_labels_are_derived_from_the_classes` |
| #18 `zip_pack` dropped files 201+ silently | Low | **Fixed** | `tools.zip_pack.max_files`; result names the shortfall; `skipped` in meta | `test_zip_pack_reports_omitted_paths` |
| B1 suite red on a literal panel-list assertion | — | **Fixed** | Assertion rewritten to subset + per-row shape; brittleness noted in the test | `test_models_listing_shows_both_models` |
| Repo hygiene: stale self-copy misleads every grep | — | **Mitigated** | `.gitignore` entries for ripgrep; `data/README-do-not-search.md`; nothing deleted | — |

---

## New settings (all Admin-editable, with help text)

| Key | Default | Governs |
|---|---|---|
| `agent.workspace_listing_entries` | 80 | Entries listed in every prompt (was a literal) |
| `agent.fs_list_entries` | 500 | Entries per `fs_list` call (was an invisible default) |
| `tools.zip_pack.max_files` | 200 | Files per zip (was a literal slice) |
| `uploads.max_bytes` | 25 MiB | Per-file upload cap (was a literal, duplicated in JS) |
| `agent.pending_max_age_s` | 3600 | How long an approval card stays executable (0 = never expires) |

## Files touched

`hearth/prompt.py` · `hearth/orchestrator.py` · `hearth/routes_chat.py` ·
`hearth/manager.py` · `hearth/main.py` · `hearth/runtime_settings.py` ·
`hearth/tools/base.py` · `hearth/tools/fs_tools.py` · `hearth/tools/media_tools.py` ·
`hearth/tools/__init__.py` + one `label` line per tool class (11 files) ·
`hearth/llm/base.py` · `hearth/llm/stub.py` · `hearth/llm/llama_server.py` ·
`hearth/i18n.py` (EN+DE) · `hearth/static/chat.js` · `hearth/static/admin.js` ·
`hearth/workspaces.py` (`EntryList` + `.truncated`) ·
`README.md` · `.gitignore` · `data/README-do-not-search.md` (new) ·
`docs/DECISIONS.md` (DEC-067…073) · `scripts/selftest.py` (+15 tests, 204 → 219)

## Notes on three judgement calls

**#1: the marker says "and more", not "and N more" — because the count is unknowable.** An
intermediate version of this fix read `cap+1` entries and subtracted, which produced a
confidently wrong number in the system prompt: for a 300-file workspace at cap 80 it printed
"…and 1 more". A capped read yields no total. `workspaces.list_tree` now returns an
`EntryList` (a `list` subclass, so iteration/JSON/equality are unchanged) carrying a truthful
`.truncated` boolean, and both the prompt and `fs_list` report truncation without inventing a
figure. Verified against the audit's own scenario: 300 files, cap 80 → "…and more", never a
number.

**#2 was made honest, not prevented.** The audit's stronger fix (content-addressed uploads)
changes artifact identity and every download URL. DEC-040 §2's reasoning survives intact when
the bytes match, and hashing the *existing* file is cheap because uploads are streamed anyway.
So replacement still happens — it is now named in the response, the audit log, and the chat.

**#1/B3 changed a prompt string, so the test that pinned the old string changed too.**
`test_workspace_context_shows_sizes_and_uploads` asserted `"attachments"` in
`prompt.WORKSPACE_PREAMBLE`; that word still lives there for the uncapped case, which is why
the constant keeps its original wording when no cap is known. `build_workspace_preamble()`
only departs from it when a caller supplies the settings that produced the listing.

**B1 was fixed at the test *and* at the cause.** The failing assertion compared a literal list
of panel rows, so adding the image generator "broke" the suite while saying nothing about
behaviour. Rewritten as a subset check plus per-row shape assertions — restoring green without
re-introducing a magic list.

## Not verified (stated plainly)

* **No browser session.** Everything in `chat.js` / `admin.js` — the "showing 12 of N" links,
  the amber upload-replacement warning, the `provider_fallback` bubble, artifact-id replay, the
  Uploads section in Admin → Agent — is read-reviewed and CSP-compatible (plain text nodes, no
  new inline scripts), but **not exercised in a browser**.
* **No real `llama-server`.** The provider registry, capabilities and fallback notice are tested
  with `ensure_server` stubbed to return nothing. Live llama.cpp behaviour, streaming, ETA and
  vision recovery remain unexercised — same limitation the audit recorded.
* **#6 is asserted from source text**, not from rendering a 40-page PDF (no renderer fixture).
* **12 skipped tests** in the default run are environment-gated (selenium installed, HTTP pass
  off); they were not individually triaged.

## Remaining work, ranked

1. **#16 god-module split** — extract `captions.py` and `prompt_context.py`. Deferred because it
   moves ~500 lines of the most carefully-commented streaming code with no behavioural test
   coverage behind it; doing it blind would trade a readability finding for a regression risk.
2. **#14 one `workspace_visibility` policy object** — the numbers are settings now, so the
   remaining gap is structural (one object read by prompt assembly and both fs tools).
3. **Shared store for the four per-process dicts** (#11's real fix) — needs a deployment
   decision; currently warned about rather than fixed.
4. **`TurnState` extraction** (audit Part B, agent loop) — makes the `round_offset=1000` resume
   hack unnecessary and the loop testable without a provider.
5. **Browser pass** over the four UI changes listed above; `scratch/` already has Playwright
   harnesses for it.

## Method note worth keeping

Three of my own edits collapsed a `def …:` line onto its first statement, producing
`IndentationError`/broken code in `main.py`, `orchestrator.py` and `workspaces.py`. All three
were caught only by running the suite — which is the argument for keeping it green *first*. If
you make in-place edits in this tree, run `python3 scripts/selftest.py < /dev/null` immediately
after; note the stdin redirect, or a backgrounded run stalls on SIGTTIN (the audit hit this too).

A fourth lesson is recorded under DEC-067 and above: **do not infer a total from a truncated
read.** That produced a wrong number in a system prompt, i.e. exactly class of bug this whole
audit was about, introduced while fixing it.
