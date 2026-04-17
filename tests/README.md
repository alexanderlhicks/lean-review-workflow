# Tests

Unit tests for the modules that power the GitHub Action. The suite is pure
Python — no network, no Lean toolchain, no real API keys — so it runs in
under 5 seconds locally and in CI.

## Running

```bash
# From the repository root:
python3 -m pytest tests/ -q            # full suite
python3 -m pytest tests/test_llm_provider.py -q     # one file
python3 -m pytest tests/ -k "thinking" -q           # filter by name
python3 -m pytest tests/ -x --tb=short              # stop on first failure
```

All tests are expected to pass on a clean checkout with
`pip install -r requirements.txt`. Tests that touch a third-party SDK are
wrapped in `try / except ImportError: pytest.skip(...)` so the suite still
runs if one of `anthropic`, `openai`, `google-genai`, or `pymupdf` is
unavailable.

## Layout

| File | Module under test | Focus |
|------|-------------------|-------|
| [`test_llm_provider.py`](./test_llm_provider.py) | `llm_provider.py` | Provider abstraction: Gemini, Anthropic, OpenAI. API-shape regression coverage. |
| [`test_review.py`](./test_review.py) | `review.py` | Diff parsing, mechanical pre-checks, SSRF protection, Pydantic schemas, the orchestration entrypoint. |
| [`test_lean_utils.py`](./test_lean_utils.py) | `lean_utils.py` | Module-name resolution, comment detection (including nested block comments), the file-content cache, src-dir detection via `lakefile.{toml,lean}`. |
| [`test_lean_info_extractor.py`](./test_lean_info_extractor.py) | `lean_info_extractor.py` | Lean declaration extraction, `sorry`/axiom reporting, diagnostics formatting, GitHub Actions output formatting. |
| [`test_discover_files.py`](./test_discover_files.py) | `discover_files.py` | Dependency graph traversal (forward and reverse, transitive by depth), file-index construction. |

## Coverage details

### `test_llm_provider.py` — the largest file

The provider layer is where we've hit the most API-shape issues in production
(forced `tool_choice` + thinking, `beta.chat.completions` deprecation,
`thinking_budget` unsupported on Opus 4.7). Tests are organised so each layer
of behaviour has its own class:

**Shared infrastructure**

- `TestContentPart`, `TestTokenUsage` — provider-agnostic dataclasses.
- `TestRetryLogic` — the generic retry classifier (`_is_retryable_generic`):
  429/500/502/503/504/overloaded/capacity are retryable; 400/401/403 are not.
- `TestCreateProvider` — factory (name normalisation, unknown providers).
- `TestExtractPdfText` — pymupdf fallback for providers without native PDF
  support (now only used as a fallback — every provider sends PDFs natively).
- `TestGenerateStructured` — the retry/backoff wrapper that wraps each
  provider's `_generate_once`: exponential wait, rate-limit jitter, cap at
  `max_retries`.
- `TestAnthropicRetryable`, `TestOpenAIRetryable` — provider-specific
  `_is_retryable` overrides (Anthropic: 529 overloaded; OpenAI:
  `insufficient_quota` is terminal).

**Per-provider content conversion** (`TestGeminiContentConversion`,
`TestAnthropicContentConversion`, `TestOpenAIContentConversion`)

Each class exercises the same matrix:
- text block
- text with prompt-cache control (Anthropic)
- PDF block (native shape per provider)
- image block (native shape per provider)
- unknown `ContentPart.type` is silently skipped — and a warning is logged
  (caplog assertion)

**Per-provider thinking/reasoning mapping**

- `TestGeminiThinking` — `_is_thinking_model` prefix check (gemini-3 only),
  `_level_for_budget` token→effort mapping, `response.parsed → response.text`
  fallback, and the `ValueError` raised when both are empty (so retry kicks
  in instead of the caller crashing).
- `TestAnthropicThinking` — adaptive-model prefix check (Opus 4.7/4.6,
  Sonnet 4.6, Mythos), `_effort_for_budget` mapping, end-to-end kwargs
  capture for the adaptive path vs. the non-adaptive (warn-and-skip) path.
- `TestOpenAIReasoning` — reasoning-model prefix check (o1/o3/o4/gpt-5),
  effort mapping.

**End-to-end request-shape tests**

These drive each provider's `_generate_once` against a `SimpleNamespace`
fake client and assert the exact kwargs we send. They are the primary guard
against API drift — if Google, Anthropic, or OpenAI rename a field, these
tests fail loudly.

- `TestGeminiRequestKwargs` — `GenerateContentConfig` carries
  `response_mime_type`, `response_schema=PydanticClass`, `cached_content`
  when supplied, and `thinking_config.thinking_level` **only** on
  gemini-3 models with a non-zero budget.
- `TestAnthropicRequestShape` — `tools` entry has exactly
  `{name, description, input_schema}` with
  `input_schema == schema.model_json_schema()`, `max_tokens=16384`, the
  adaptive-thinking instruction nudge is appended when thinking is on, and
  *not* appended when it's off.
- `TestOpenAIRequestKwargs` — `responses.parse` receives `text_format=schema`,
  `input` is a user-role block list, and `reasoning={"effort":...}` is sent
  **only** for reasoning models with a non-zero budget.

**Token-usage extraction**

- `TestGeminiTokenUsage` — `usage_metadata.{prompt,candidates,thoughts}_token_count`.
- `TestAnthropicTokenUsage` — `usage.{input,output}_tokens`; Anthropic does
  not expose thinking-token counts so `thinking_tokens` stays at 0. Also:
  a response missing the `tool_use` block raises `ValueError`.
- `TestOpenAITokenUsage` — `usage.{input,output}_tokens` plus
  `usage.output_tokens_details.reasoning_tokens`; gracefully handles a
  missing `output_tokens_details`; a missing `output_parsed` raises
  `ValueError`.

**Cache defaults** — `TestCacheDefaults` — the base class's
no-op cache methods, and Anthropic's "per-request ephemeral caching" sentinel.

### `test_review.py`

- **Diff parsing** (`TestSplitDiffIntoFiles`, `TestExtractAddedLines`,
  `TestGetDiffLines`) — unified-diff split into per-file chunks, extraction
  of added lines, rename handling, non-Lean files preserved.
- **Prompt-size budget** (`TestFitPromptToBudget`) — the helper that trims
  `REPO_CONTEXT` / `DEPENDENCY_CONTEXT` when the assembled prompt would
  exceed the per-call character budget (`MAX_PROMPT_CHARS`, default
  2.5M ≈ 830K tokens). Guards against the Anthropic 400
  `prompt is too long` error; keeps the file under review, diff, and spec
  checklist intact.
- **`REPO_CONTEXT` rendering and filtering** (`TestFormatRepoFiles`) — the
  helper that renders the discovered-files dict into the prompt block format
  and drops sibling changed files from per-file reviews (each changed file
  already has its own review pass, so including it in `REPO_CONTEXT` is
  duplicate token spend).
- **Lean-aware text handling** (`TestIsInComment`, `TestIsInString`) —
  single-line and block comments (including nested), string-literal
  recognition.
- **Mechanical pre-checks** (`TestMechanicalPrechecks`) — `sorry`/axiom
  scanning on diffs, comment-only matches ignored, non-Lean files skipped,
  large-file warnings.
- **SSRF protection** (`TestValidateUrl`, `TestCheckIpSafe`,
  `TestResolveAndValidate`, `TestExternalFetch`) — the external-reference
  fetcher rejects localhost, RFC1918 ranges, cloud metadata endpoints
  (AWS 169.254.169.254, Azure, Alibaba), non-`http(s)` schemes, and DNS
  names that resolve to private IPs. Redirects are re-validated before
  being followed.
- **Retry classification for HTTP** (`TestRetryLogic`) — rate-limit and
  server-error retry; auth and invalid-request errors terminate.
- **Schema validation** (`TestPydanticSchemas`, `TestStructuredSynthesisInput`)
  — the Pydantic models that shape each agent's structured output
  (`FileReview`, `TriageResult`, `SpecChecklist`, `CrossFileAnalysis`,
  `SynthesisSummary`) accept expected fields and reject missing required
  ones; optional fields stay optional.
- **Main-flow smoke test** (`TestMainFlow`) — early exit when no Lean
  files changed.

### `test_lean_utils.py`

- `TestFilePathToModuleName` — path → module mapping with `src/`, `lib/`,
  `Mathlib/` prefixes and explicit `src_dir` overrides.
- `TestIsInComment` — comprehensive nested-block-comment state machine.
- `TestFileCache` — the read-once/read-lines cache.
- `TestDetectSrcDir` — reading `lakefile.toml` / `lakefile.lean` to pick
  the source root; `toml` takes precedence.

### `test_lean_info_extractor.py`

- `TestGetLeanDeclarations` — parses `def`, `theorem`, `lemma`,
  `structure`, `noncomputable def` from a real on-disk Lean file.
- `TestExtractSorryWarnings` — finds `sorry` tokens but ignores comments
  and tolerates nested block comments.
- `TestExtractDiagnostics`, `TestExtractInfoForFiles`,
  `TestExtractLightInfo` — the wrapper that assembles per-file info for
  the reviewer; gracefully degrades when `lake` isn't on PATH.
- `TestFormatForReview` — formatted output with/without diagnostics,
  sorries, and axioms.
- `TestGitHubOutputFormatting` — multi-line values use heredoc syntax so
  the `$GITHUB_OUTPUT` writer doesn't break on embedded newlines.

### `test_discover_files.py`

- `TestGetLeanModuleName` — same as `file_path_to_module_name` but on the
  `discover_files.py` convenience wrapper (kept for backwards compat).
- `TestGetDependentLeanFiles`, `TestGetDependencyLeanFiles` — forward
  and reverse edges off a parsed `lake exe deps` graph.
- `TestTransitiveDependencies` — BFS by depth; cycle-safe; depth tags are
  correct; excludes seed modules from results.
- `TestBuildLeanFileIndex` — on-disk traversal; skips `.git/` and `.lake/`.
- `TestPartitionContextTiers` — the full-context / summary-context split.
  Changed files are preferred; depth-1 beats depth-2; the total tier size is
  hard-capped at `CONTEXT_LIMIT` so very-large PRs demote overflow
  (including excess changed files) to the summary tier.

## Patterns and conventions

**`SimpleNamespace` fake clients.** Provider tests that need to exercise
`_generate_once` construct the Anthropic/OpenAI/Gemini client out of
`types.SimpleNamespace`. The fake `messages.create` / `responses.parse` /
`models.generate_content` captures kwargs into a dict and returns a fake
response. No SDK-specific mock framework is required.

**Kwargs capture for API-shape regression.** End-to-end tests in the
`*RequestKwargs` / `*RequestShape` classes inspect the exact dict we pass
to the SDK. These were added after three separate API-shape breaks and are
the highest-value tests in the suite. If you're changing how requests are
built, expect these to fail first.

**`try / except ImportError: pytest.skip(...)`.** The SDKs are heavy
optional imports. Each test that touches a provider SDK opens with this
pattern so `pip install -r requirements.txt` minus one SDK doesn't break
the whole suite.

**`caplog` for warning-log assertions.** `_to_content_blocks` /
`_to_native_contents` / `_to_input` silently skip unknown
`ContentPart.type`s. The tests use `caplog` at `WARNING` level to assert
the skip is accompanied by a log line — so a future refactor can't turn
the skip into a silent drop.

**Schema fixture.** `MockReview(BaseModel)` in `test_llm_provider.py` is a
minimal Pydantic model used across provider tests so the structured-output
pathway can be exercised without dragging in the real `FileReview` schema
(which would couple provider tests to `review.py`).

## Adding a test

1. Pick the file whose module you're testing. If it's a new module, create
   `tests/test_<module>.py` using the existing files as templates.
2. Group related assertions into a `TestXyz` class — pytest auto-discovers
   `test_*` methods on any class whose name starts with `Test`.
3. For provider-level changes, prefer extending an existing
   `*ContentConversion`, `*Thinking`, `*RequestKwargs`, or `*TokenUsage`
   class rather than creating a new one. The current structure is
   deliberately exhaustive; new tests should slot into the existing grid.
4. If your change touches the request payload sent to a provider, add a
   case to the corresponding `*RequestKwargs` / `*RequestShape` class.
   These are the tests most likely to catch future API drift.
