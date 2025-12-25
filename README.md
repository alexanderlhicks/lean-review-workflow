# AI Code Review Workflow for Lean Projects

This GitHub Action provides an advanced, AI-powered code review for Pull Requests in Lean projects, with a strong focus on detecting potential misformalization issues. It leverages the Gemini API to analyze code changes in the context of formal specifications and project dependencies.

## Features

*   **AI-Powered Rigorous Review:** Utilizes a specified Gemini model (`gemini-3-pro-preview` by default) to act as a "meticulous senior engineer specializing in formal verification." The AI follows a detailed "chain-of-thought" process to identify misformalization.
*   **Automated Lean Dependency Discovery:** Automatically identifies relevant Lean files impacted by a PR by leveraging `lake exe graph --json`. This ensures the AI receives a comprehensive understanding of the code's context and dependencies.
*   **External Reference Integration:** Fetches and extracts content from external URLs (PDFs, HTML pages) to provide the AI with formal specifications or documentation for comparison against the PR's implementation.
*   **Internal ArkLib Context:** Allows developers to explicitly provide paths to additional relevant files or directories within the repository (e.g., specific library files, internal documentation) to augment the AI's understanding.
*   **Flexible Review Comments:** Supports additional, human-provided comments to guide the AI's focus during the review process.
*   **Robust Error Handling:** Features enhanced error handling, logging, and graceful fallbacks for scenarios like failed dependency graph generation or inaccessible external references.
*   **Configurable Gemini Model:** The specific Gemini model used for the review can be easily configured via action inputs.

## How it Works

1.  **Checkout Repository:** Fetches the full Git history of the repository.
2.  **Set up Python & Lean:** Configures the environment with Python for the review script and Lean/Lake for building the project and generating dependency graphs.
3.  **Install Python Dependencies:** Installs required Python libraries (e.g., `google-generativeai`, `requests`, `beautifulsoup4`, `PyMuPDF`).
4.  **Discover Related Files:**
    *   Identifies all `.lean` files changed in the pull request.
    *   Attempts to build the Lean project using `lake build`.
    *   If successful, it runs `lake exe graph --json` to get the precise dependency graph.
    *   Parses the graph to find all Lean modules that directly or transitively depend on the changed files.
    *   If `lake build` or `lake graph` fails, it falls back to providing only the directly changed files as context.
5.  **Run AI Review Script:** Executes `review.py` with all the gathered context (PR diff, external references, internal files, automatically discovered dependencies).
6.  **Post Review Comment:** Publishes the AI-generated review as a comment on the Pull Request using `actions/github-script`, with retry logic for network resilience.

## Usage

This is a composite action intended to be used within your main repository workflow (e.g., in `.github/workflows/pr-review.yml`).

```yaml
name: AI Code Review for Lean PRs

on:
  pull_request:
    types: [opened, synchronize] # Trigger on PR open and new commits

jobs:
  ai_review_lean:
    runs-on: ubuntu-latest
    permissions:
      contents: read       # Required for actions/checkout
      pull-requests: write # Required for actions/github-script to post comments

    steps:
      - name: Run AI Code Review Action
        uses: ./       # Refers to the current directory where action.yml resides
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
          pr_number: ${{ github.event.pull_request.number }}
          # Optional: Provide URLs to external formal specifications or documentation
          external_refs: "https://example.com/spec.pdf, https://another.com/design.html"
          # Optional: Provide paths to internal repository files or directories for additional context
          arklib_refs: "src/MyProject/FormalSpec.lean, src/MyProject/Types.lean, docs/architecture.md"
          # Optional: Add specific instructions or focus areas for the AI reviewer
          additional_comments: "Pay special attention to the proof completeness and adherence to the type class inference rules."
          # Optional: Specify a different Gemini model (default is 'gemini-3-pro-preview')
          gemini_model: "gemini-1.5-pro-preview"
```

### Inputs

*   `github_token` (Required): GitHub Token for API calls. Use `${{ secrets.GITHUB_TOKEN }}`.
*   `gemini_api_key` (Required): Gemini API Key for AI review generation. Store this as a repository secret.
*   `pr_number` (Required): The Pull Request number. Use `${{ github.event.pull_request.number }}`.
*   `external_refs` (Optional): Comma-separated list of URLs to external documents (PDFs, HTML) for contextual information.
*   `arklib_refs` (Optional): Comma-separated list of paths to relevant files or directories within the repository for additional context.
*   `additional_comments` (Optional): Extra comments or instructions for the AI reviewer.
*   `gemini_model` (Optional): The specific Gemini model to use (default: `gemini-3-pro-preview`).

## Development

Contributions are welcome! Please ensure that any changes adhere to the existing code style and conventions.
