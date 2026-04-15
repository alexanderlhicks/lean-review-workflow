# AI Code Review Workflow for Lean Projects

This GitHub Action provides an AI-powered code review for Pull Requests in Lean 4 projects, with a strong focus on detecting misformalization issues. It leverages the Gemini API to analyze code changes against formal specifications and project dependencies through a multi-agent pipeline.

## Features

*   **5-Agent Review Pipeline:**
    1.  **Mechanical Pre-Checks:** Deterministic scanning for escape hatches (`sorry`, `axiom`, `native_decide`, `opaque`, `implemented_by`, `sorryAx`) in both newly introduced and pre-existing code, with comment- and string-awareness (including Lean 4 nested block comments).
    2.  **Specification Analyst (Agent A):** Reads external PDFs and math papers using Gemini's native multimodal support to extract a "Formalization Checklist" — a mapping from paper results to mathematical content that any correct formalization must preserve.
    3.  **Triage Agent:** Groups changed files into review clusters based on the dependency graph and type signatures, prioritizing tightly-coupled files for joint review.
    4.  **Code Reviewer (Agent B):** Evaluates each Lean file's diff and full content against the spec checklist, repository context, and Lean 4 best practices. Runs in parallel across files (up to 5 concurrent workers) with cluster-level context for cross-file type awareness.
    5.  **Cross-File Analysis Agent:** Analyzes composition chains, type-flow across files, axiom/escape-hatch impact propagation, and external dependency correctness.
    6.  **Lead Synthesizer (Agent C):** Aggregates per-file reviews (both formatted and structured data) into a prioritized executive summary with deduplication.
*   **Transitive Dependency Discovery:** Uses `lake exe graph --json` with BFS traversal (configurable depth, default 2) to find both direct and transitive dependencies. Asymmetric depth: dependencies (what we import) go to depth 2; dependents (what imports us) stay at depth 1.
*   **Lean Toolchain Extraction:** Post-build extraction of axiom dependencies (`#print axioms`), `sorry`/`admit` locations, and compiler diagnostics for all changed files, plus lightweight sorry/admit scanning for overflow files.
*   **Tiered Context Management:** Full file content for up to 50 files (configurable via `CONTEXT_LIMIT`), with type-signature-only summaries for overflow files. Depth-1 dependencies are prioritized over depth-2 in the full-context tier.
*   **Context Completeness Guarantees:** External reference fetch errors, Lean toolchain extraction failures, and context cache failures are surfaced as warnings in the review output. External references fall back to inline delivery if Gemini caching fails.
*   **Structured Output:** All agents produce Pydantic-validated JSON responses. Line-level annotations are posted via the GitHub Review API using the modern `line`/`side` parameters.
*   **Per-Agent Model Selection:** Each pipeline stage can use a different Gemini model via CLI flags (`--spec-model`, `--review-model`, `--cross-file-model`, `--synthesis-model`).
*   **Adaptive Pipeline:** Single-file PRs skip triage, cross-file analysis, and synthesis (the per-file review is the output, with a deterministic downstream impact note from the dependency graph). Two-file PRs skip triage but get cross-file analysis.

## How it Works

1.  **Checkout & Environment Setup:** Fetches full Git history, sets up Python 3.13 and Lean/Lake via `lean-action`.
2.  **Build:** Builds the Lean project with `lake build` (with optional linting).
3.  **Discover Related Files:** Identifies changed `.lean` files, then uses the Lake dependency graph for BFS-based transitive dependency and dependent discovery. Splits results into full-context and summary-context tiers.
4.  **Extract Lean Toolchain Info:** Runs `#print axioms` per declaration, scans for `sorry`/`admit`, and captures compiler diagnostics for changed files. Performs lightweight sorry/admit scanning on summary-context overflow files. Operates within a configurable time budget (default 300s).
5.  **Run Multi-Agent Review Pipeline:**
    *   **Pre-checks** (deterministic): Scans diffs for escape hatches with nested block comment and string literal awareness.
    *   **Agent A** (spec analysis): Reads external PDFs/papers with repository structure context, produces a formalization checklist.
    *   **Triage**: Groups files into review clusters using dependency graph and type signatures.
    *   **Agent B** (per-file review, parallel): Reviews each file against the spec checklist, repo context, cluster file signatures, and Lean 4 best practices.
    *   **Cross-File Agent**: Analyzes composition chains, type-flow, and axiom impact across all changed files.
    *   **Synthesis**: Aggregates structured review data and formatted reviews into an executive summary.
6.  **Post Review:** Publishes or updates an AI review comment on the PR, with collapsible per-file details grouped by cluster. Line-level annotations are posted as GitHub Review comments where findings map to diff lines.

## Usage

This is a composite action. To unlock its full power (the `external_refs` and `additional_comments` inputs), it is recommended to trigger via a **PR Comment (ChatOps)**.

### Recommended: ChatOps Workflow (Dynamic PR Comments)

Create a workflow file at `.github/workflows/ai-chatops.yml`:

```yaml
name: AI PR ChatOps
on:
  issue_comment:
    types: [created]

jobs:
  ai_review_chatops:
    if: ${{ github.event.issue.pull_request && startsWith(github.event.comment.body, '/review') }}
    runs-on: ubuntu-latest
    timeout-minutes: 45
    permissions:
      contents: read
      pull-requests: write
    steps:
      - name: Parse Command
        id: parse_command
        uses: actions/github-script@v8
        with:
          script: |
            const body = context.payload.comment.body;
            let external_refs = "";
            let additional_comments = "";
            
            const lines = body.split('\n');
            for (const line of lines) {
              if (line.startsWith('refs: ')) external_refs = line.replace('refs: ', '').trim();
              if (line.startsWith('focus: ')) additional_comments = line.replace('focus: ', '').trim();
            }
            core.setOutput("external_refs", external_refs);
            core.setOutput("additional_comments", additional_comments);

      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Checkout PR
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: gh pr checkout ${{ github.event.issue.number }}

      - name: Run AI Code Review Action
        uses: ./ # Or your-username/lean-review-workflow@main
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
          pr_number: ${{ github.event.issue.number }}
          external_refs: ${{ steps.parse_command.outputs.external_refs }}
          additional_comments: ${{ steps.parse_command.outputs.additional_comments }}
```

**How developers use it:**
Leave a comment on any PR:
```text
/review
refs: https://arxiv.org/pdf/2301.12345.pdf
focus: Check if my definition of a Perfectoid Space matches Section 4.
```

### Alternative: Standard Push Workflow (Static)

```yaml
name: AI Code Review for Lean PRs

on:
  pull_request:
    types: [opened, synchronize]

jobs:
  ai_review_lean:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    permissions:
      contents: read
      pull-requests: write

    steps:
      - name: Run AI Code Review Action
        uses: ./ 
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
          pr_number: ${{ github.event.pull_request.number }}
          gemini_model: "gemini-2.5-pro"
```

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `github_token` | Yes | — | GitHub Token for API calls |
| `gemini_api_key` | Yes | — | Gemini API Key for AI review generation |
| `pr_number` | Yes | — | The Pull Request number |
| `external_refs` | No | `""` | Comma-separated URLs to external documents (PDFs, HTML, raw source) |
| `repo_context_refs` | No | `""` | Comma-separated paths to additional internal context files/directories |
| `additional_comments` | No | `""` | Extra focus instructions for the AI reviewer |
| `gemini_model` | No | `gemini-2.5-pro` | Default Gemini model for all agents |
| `lint` | No | `false` | Whether to run the Lean linter |
| `dependency_depth` | No | `2` | Depth of transitive dependency traversal (1=direct only, 2=imports of imports) |

The review script also accepts per-agent model overrides via CLI flags: `--spec-model`, `--review-model`, `--cross-file-model`, `--synthesis-model`.

## Project Structure

```
lean-review-workflow/
  action.yml                  # GitHub Actions composite action definition
  review.py                   # Main review orchestration (multi-agent pipeline)
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

See `requirements.txt`: `requests`, `beautifulsoup4`, `google-genai`, `pydantic`.

Contributions are welcome. Please ensure changes pass the existing test suite.
