# AI Code Review Workflow for Lean Projects

This GitHub Action provides an AI-powered code review for Pull Requests in Lean 4 projects, with a strong focus on detecting misformalization issues. It supports multiple LLM providers (Gemini, Anthropic Claude, OpenAI GPT) and analyzes code changes against formal specifications and project dependencies through a multi-agent pipeline.

## Features

*   **5-Agent Review Pipeline:**
    1.  **Mechanical Pre-Checks:** Deterministic scanning for escape hatches (`sorry`, `axiom`, `native_decide`, `opaque`, `implemented_by`, `sorryAx`) in both newly introduced and pre-existing code, with comment- and string-awareness (including Lean 4 nested block comments).
    2.  **Specification Analyst (Agent A):** Reads external PDFs and math papers (native multimodal for Gemini/Anthropic; text extraction via pymupdf for OpenAI) to extract a "Formalization Checklist" — a mapping from paper results to mathematical content that any correct formalization must preserve. Receives repository structure and dependency graph for awareness of existing formalizations.
    3.  **Triage Agent:** Groups changed files into review clusters based on the dependency graph and type signatures, prioritizing tightly-coupled files for joint review. Produces a **review strategy** and **key hypotheses** per cluster that guide the per-file reviewers.
    4.  **Code Reviewer (Agent B):** Evaluates each Lean file's diff and full content against the spec checklist, repository context, and Lean 4 best practices. Writes a structured **analysis** before producing findings (what the code does mathematically, risk assessment, spec mapping). Runs in parallel across files (up to 5 concurrent workers) with cluster-level type signatures, review strategy, and key hypotheses.
    5.  **Cross-File Analysis Agent:** Analyzes composition chains, type-flow across files, axiom/escape-hatch impact propagation, and external dependency correctness.
    6.  **Lead Synthesizer (Agent C):** Aggregates per-file reviews (both formatted and structured data) into a prioritized executive summary with deduplication.
*   **Transitive Dependency Discovery:** Uses `lake exe graph --json` with BFS traversal (configurable depth, default 2) to find both direct and transitive dependencies. Asymmetric depth: dependencies (what we import) go to depth 2; dependents (what imports us) stay at depth 1.
*   **Lean Toolchain Extraction:** Post-build extraction of axiom dependencies (`#print axioms`), `sorry`/`admit` locations, and compiler diagnostics for all changed files, plus lightweight sorry/admit scanning for overflow files.
*   **Tiered Context Management:** Full file content for up to 50 files (configurable via `CONTEXT_LIMIT`), with type-signature-only summaries for overflow files. Depth-1 dependencies are prioritized over depth-2 in the full-context tier.
*   **Context Completeness Guarantees:** External reference fetch errors, Lean toolchain extraction failures, and content cache failures are surfaced as warnings in the review output. External references fall back to inline delivery if caching fails.
*   **Agent Deliberation:** Agents think before reviewing. Extended thinking (configurable token budget) gives the model internal reasoning time. FileReview and CrossFileAnalysis schemas include an `analysis` field where the model writes its step-by-step reasoning before committing to findings. Triage produces per-cluster review strategies and testable hypotheses that flow to the per-file reviewers.
*   **Multi-Provider Support:** Supports Gemini, Anthropic (Claude), and OpenAI (GPT) as interchangeable backends via a provider abstraction layer. Each provider handles structured output, PDF content, caching, and extended thinking according to its native API.
*   **Structured Output:** All agents produce Pydantic-validated JSON responses. Line-level annotations are posted via the GitHub Review API using the modern `line`/`side` parameters.
*   **Per-Agent Model Selection:** Each pipeline stage can use a different model via CLI flags (`--spec-model`, `--review-model`, `--cross-file-model`, `--synthesis-model`).
*   **Adaptive Pipeline:** Single-file PRs skip triage, cross-file analysis, and synthesis (the per-file review is the output, with a deterministic downstream impact note from the dependency graph). Two-file PRs skip triage but get cross-file analysis.

## How it Works

1.  **Checkout & Environment Setup:** Fetches full Git history, sets up Python 3.13 and Lean/Lake via `lean-action`.
2.  **Build:** Builds the Lean project with `lake build` (with optional linting).
3.  **Discover Related Files:** Identifies changed `.lean` files, then uses the Lake dependency graph for BFS-based transitive dependency and dependent discovery. Splits results into full-context and summary-context tiers.
4.  **Extract Lean Toolchain Info:** Runs `#print axioms` per declaration, scans for `sorry`/`admit`, and captures compiler diagnostics for changed files. Performs lightweight sorry/admit scanning on summary-context overflow files. Operates within a configurable time budget (default 300s).
5.  **Run Multi-Agent Review Pipeline:**
    *   **Pre-checks** (deterministic): Scans diffs for escape hatches with nested block comment and string literal awareness.
    *   **Agent A** (spec analysis): Reads external PDFs/papers with repository structure context, produces a formalization checklist.
    *   **Triage**: Groups files into review clusters using dependency graph and type signatures. Produces review strategies and key hypotheses per cluster.
    *   **Agent B** (per-file review, parallel): Writes a step-by-step analysis of each file, then derives findings from the analysis. Receives cluster review strategy, key hypotheses, and type signatures of related files.
    *   **Cross-File Agent**: Traces composition chains, type-flow, and axiom propagation, then reports issues grounded in that analysis.
    *   **Synthesis**: Aggregates structured review data (finding counts, verdicts, violated checklist items) and formatted reviews into an executive summary.
6.  **Post Review:** Publishes or updates an AI review comment on the PR, with collapsible per-file details grouped by cluster. Line-level annotations are posted as GitHub Review comments where findings map to diff lines.

## Usage

This is a composite action. It supports both automatic review on PR open and on-demand review via `/review` comments, ideally combined in a single workflow.

### Recommended: Combined Workflow (Auto + ChatOps)

Create a workflow file at `.github/workflows/ai-review.yml`:

```yaml
name: PR Review

on:
  pull_request:
    types: [opened]
  issue_comment:
    types: [created]

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: true

jobs:
  review:
    if: >-
      (
        github.event_name == 'pull_request' &&
        (
          github.event.pull_request.author_association == 'OWNER' ||
          github.event.pull_request.author_association == 'MEMBER' ||
          github.event.pull_request.author_association == 'COLLABORATOR'
        )
      ) ||
      (
        github.event_name == 'issue_comment' &&
        github.event.issue.pull_request &&
        startsWith(github.event.comment.body, '/review') &&
        (
          github.event.comment.author_association == 'OWNER' ||
          github.event.comment.author_association == 'MEMBER' ||
          github.event.comment.author_association == 'COLLABORATOR'
        )
      )
    runs-on: ubuntu-latest
    timeout-minutes: 90
    permissions:
      contents: read
      pull-requests: write
    steps:
      - name: Extract arguments from comment
        id: get_args
        if: github.event_name == 'issue_comment'
        env:
          COMMENT_BODY: ${{ github.event.comment.body }}
        run: |
          # Generate a random EOF marker for the GITHUB_OUTPUT heredoc.
          # This variable is passed to awk which handles all output writing.
          EOF=$(openssl rand -hex 8)

          awk -v eof="$EOF" -v gh_out="$GITHUB_OUTPUT" '
            BEGIN {
              ext = ""
              repo = ""
              com = ""
              section = ""
            }
            /^External:/ { section="ext"; next }
            /^Internal:/ { section="repo"; next }
            /^Comments:/ { section="com"; next }
            {
              gsub(/\r/, "", $0);
              gsub(/^[ \t]+|[ \t]+$/, "", $0);
              if ($0 == "" || $0 == "/review") { next }

              if (section == "ext") {
                sub(/^- +/, "");
                if (ext != "") { ext = ext "," $0 } else { ext = $0 }
              }
              else if (section == "repo") {
                sub(/^- +/, "");
                if (repo != "") { repo = repo "," $0 } else { repo = $0 }
              }
              else if (section == "com") {
                if (com != "") { com = com "\n" $0 } else { com = $0 }
              }
            }
            END {
              printf "external_refs=%s\n", ext >> gh_out
              printf "repo_context_refs=%s\n", repo >> gh_out
              printf "additional_comments<<%s\n", eof >> gh_out
              printf "%s\n", com >> gh_out
              printf "%s\n", eof >> gh_out
            }
          ' <<< "$COMMENT_BODY"
        shell: bash

      - uses: alexanderlhicks/lean-review-workflow@main
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.LLM_API_KEY }}
          provider: gemini  # or: anthropic, openai
          model: gemini-2.5-pro-preview-06-05
          pr_number: ${{ github.event.issue.number || github.event.pull_request.number }}
          external_refs: "${{ steps.get_args.outputs.external_refs }}"
          repo_context_refs: "${{ steps.get_args.outputs.repo_context_refs }}"
          additional_comments: "${{ steps.get_args.outputs.additional_comments }}"
```

**Key features of this workflow:**
- **Concurrency control** prevents duplicate reviews on the same PR.
- **Access control** restricts triggers to owners, members, and collaborators only (prevents abuse on public repos).
- **Combined triggers** handle both auto-review on PR open and on-demand `/review` comments.
- **Multiline comment parsing** supports structured sections with bullet points.

**How developers use it:**

Automatic review runs on PR open. For re-review with context, leave a comment:
```text
/review
External:
- https://arxiv.org/pdf/2301.12345.pdf
- https://eprint.iacr.org/2024/001.pdf
Internal:
- docs/spec.md
Comments:
Check that the definition of the ring homomorphism in Section 4
matches the formalization, especially the commutativity hypothesis.
```

### Alternative: Minimal Push Workflow (No ChatOps)

```yaml
name: AI Code Review for Lean PRs

on:
  pull_request:
    types: [opened, synchronize]

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  ai_review_lean:
    if: >-
      github.event.pull_request.author_association == 'OWNER' ||
      github.event.pull_request.author_association == 'MEMBER' ||
      github.event.pull_request.author_association == 'COLLABORATOR'
    runs-on: ubuntu-latest
    timeout-minutes: 90
    permissions:
      contents: read
      pull-requests: write

    steps:
      - uses: alexanderlhicks/lean-review-workflow@main
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          api_key: ${{ secrets.LLM_API_KEY }}
          provider: anthropic  # or: gemini, openai
          model: claude-opus-4-7 # or gemini-3.1-pro-preview, gpt-5.4
          pr_number: ${{ github.event.pull_request.number }}
```

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `github_token` | Yes | — | GitHub Token for API calls |
| `api_key` | Yes | — | API key for the LLM provider |
| `provider` | No | `gemini` | LLM provider: `gemini`, `anthropic`, or `openai` |
| `model` | Yes | — | Model name (e.g., `gemini-3.1-pro-preview`, `claude-opus-4-7`, `gpt-5.4`) |
| `pr_number` | Yes | — | The Pull Request number |
| `external_refs` | No | `""` | Comma-separated URLs to external documents (PDFs, HTML, raw source) |
| `repo_context_refs` | No | `""` | Comma-separated paths to additional internal context files/directories |
| `additional_comments` | No | `""` | Extra focus instructions for the AI reviewer |
| `lint` | No | `false` | Whether to run the Lean linter |
| `dependency_depth` | No | `2` | Depth of transitive dependency traversal (1=direct only, 2=imports of imports) |
| `thinking_budget` | No | `10240` | Thinking token budget for deep-analysis agents (Gemini/Anthropic; ignored by OpenAI) |
| `spec_model` | No | `model` | Model override for the Specification Analyst agent |
| `triage_model` | No | `model` | Model override for the Triage agent |
| `review_model` | No | `model` | Model override for the per-file Code Reviewer agent |
| `cross_file_model` | No | `model` | Model override for the Cross-File Analysis agent |
| `synthesis_model` | No | `model` | Model override for the Synthesis agent |

### Provider Feature Notes

| Feature | Gemini | Anthropic (Claude) | OpenAI (GPT) |
|---------|--------|-------------------|--------------|
| Structured output | Native schema | Tool use pattern | Native schema |
| Native PDF | Yes | Yes (document blocks) | No (text extracted client-side via pymupdf) |
| Extended thinking | ThinkingConfig | Thinking blocks | Not supported (ignored with warning) |
| Content caching | Server-side (TTL) | Per-request (ephemeral) | Automatic |

## Project Structure

```
lean-review-workflow/
  action.yml                  # GitHub Actions composite action definition
  review.py                   # Main review orchestration (multi-agent pipeline)
  llm_provider.py             # LLM provider abstraction (Gemini, Anthropic, OpenAI)
  discover_files.py           # Dependency discovery via lake graph (BFS)
  lean_info_extractor.py      # Lean toolchain data extraction (axioms, sorry, diagnostics)
  lean_utils.py               # Shared utilities (module names, comment parsing, file cache)
  requirements.txt            # Python dependencies
  prompts/
    analyze_spec.md           # Agent A: specification analysis prompt
    triage.md                 # Triage agent: file clustering prompt
    review_file.md            # Agent B: per-file review (no spec)
    review_code_with_spec.md  # Agent B: per-file review (with spec checklist)
    cross_file_analysis.md    # Cross-file analysis prompt
    synthesize_summary.md     # Synthesis agent: executive summary prompt
    lean4_checklist.md        # Lean 4 best practices checklist (injected into Agent B)
    verdict_rules.md          # Hard verdict rules (injected into Agent B and Synthesis)
  tests/
    test_review.py
    test_llm_provider.py
    test_discover_files.py
    test_lean_info_extractor.py
    test_lean_utils.py
```

## Customizing AI Prompts

The intelligence and behavior of the AI reviewer are governed by Markdown prompt templates in the `prompts/` directory. Each template uses `{{PLACEHOLDER}}` syntax for dynamic content injection at runtime.

### Key prompt files and their placeholders:

**`analyze_spec.md`** (Agent A — Specification Analyst):
`{{EXTERNAL_CONTEXT}}`, `{{FILE_DIFFS}}`, `{{REPO_STRUCTURE}}`, `{{DEPENDENCY_GRAPH}}`

**`triage.md`** (Triage Agent):
`{{DEPENDENCY_GRAPH}}`, `{{ALL_DIFFS}}`, `{{CHANGED_FILE_SIGNATURES}}`, `{{SPEC_CHECKLIST}}`, `{{ADDITIONAL_COMMENTS}}`

**`review_file.md`** / **`review_code_with_spec.md`** (Agent B — Code Reviewer):
`{{REPO_CONTEXT}}`, `{{FILE_PATH}}`, `{{FULL_CONTENT}}`, `{{FILE_DIFF}}`, `{{SPEC_CHECKLIST}}`, `{{ADDITIONAL_COMMENTS}}`, `{{CLUSTER_CONTEXT}}`, `{{LEAN4_CHECKLIST}}`, `{{VERDICT_RULES}}`

**`cross_file_analysis.md`** (Cross-File Agent):
`{{SPEC_CHECKLIST}}`, `{{PRE_CHECK_FINDINGS}}`, `{{ALL_DIFFS}}`, `{{ALL_CHANGED_CONTENTS}}`, `{{DEPENDENCY_CONTEXT}}`, `{{ADDITIONAL_COMMENTS}}`

**`synthesize_summary.md`** (Synthesis Agent):
`{{SPEC_CHECKLIST}}`, `{{PRE_CHECK_FINDINGS}}`, `{{CROSS_FILE_ANALYSIS}}`, `{{PER_FILE_REVIEWS}}`, `{{STRUCTURED_REVIEWS}}`, `{{VERDICT_RULES}}`

**`lean4_checklist.md`** and **`verdict_rules.md`** are static content injected into Agent B and Synthesis prompts. They contain the Lean 4 best practices checklist and hard verdict rules respectively.

## Development

### Running Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

### Dependencies

See `requirements.txt`: `requests`, `beautifulsoup4`, `pydantic`, `google-genai`, `anthropic`, `openai`, `pymupdf`.

Contributions are welcome. Please ensure changes pass the existing test suite.
