# AI Code Review Workflow for Lean Projects

This GitHub Action provides an advanced, AI-powered code review for Pull Requests in Lean projects, with a strong focus on detecting potential misformalization issues. It leverages the Gemini API to analyze code changes in the context of formal specifications and project dependencies.

## Features

*   **AI-Powered Rigorous Review:** Utilizes a specified Gemini model (`gemini-3-pro-preview` by default) to act as a "meticulous senior engineer specializing in formal verification." The AI follows a detailed "chain-of-thought" process to identify misformalization.
*   **Enhanced Lean Dependency Discovery:** Automatically identifies relevant Lean files impacted by a PR by leveraging `lake exe graph --json`. This ensures the AI receives a comprehensive understanding of the code's context, including both modules that the changed code *depends on* and modules that *depend on* the changed code.
*   **Structured, File-by-File Review Output:** Provides an overall summary of the PR, followed by detailed and collapsible review sections for each individual Lean file changed.
*   **Customizable AI Prompts:** The core instructions given to the AI are stored in external Markdown files, allowing for easy customization and fine-tuning of the review's focus and style.
*   **External Reference Integration:** Fetches and extracts content from external URLs (PDFs, HTML pages) to provide the AI with formal specifications or documentation for comparison against the PR's implementation.
*   **Additional Repository Context:** Allows developers to explicitly provide paths to additional relevant files or directories within the repository (via `repo_context_refs`). This input can be dynamically generated (e.g., from a PR comment) to augment the AI's understanding with expert-selected or non-discoverable context.
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
    *   Parses the graph to find:
        *   All Lean modules that directly or transitively *depend on* the changed files (downstream dependencies).
        *   All Lean modules that the changed files *directly depend on* (upstream dependencies).
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
          # Optional: Provide URLs to external formal specifications or documentation (can be dynamic, e.g., parsed from a PR comment)
          external_refs: "https://example.com/spec.pdf, https://another.com/design.html"
          # Optional: Provide paths to additional repository files or directories for context (can be dynamic, e.g., parsed from a PR comment)
          repo_context_refs: "src/MyProject/FormalSpec.lean, src/MyProject/Types.lean, docs/architecture.md"
          # Optional: Add specific instructions or focus areas for the AI reviewer (can be dynamic, e.g., parsed from a PR comment)
          additional_comments: "Pay special attention to the proof completeness and adherence to the type class inference rules."
          # Optional: Specify a different Gemini model (default is 'gemini-3-pro-preview')
          gemini_model: "gemini-3-pro-preview"
```

### Inputs

*   `github_token` (Required): GitHub Token for API calls. Use `${{ secrets.GITHUB_TOKEN }}`.
*   `gemini_api_key` (Required): Gemini API Key for AI review generation. Store this as a repository secret.
*   `pr_number` (Required): The Pull Request number. Use `${{ github.event.pull_request.number }}`.
*   `external_refs` (Optional): Comma-separated list of URLs to external documents (PDFs, HTML) for contextual information.
*   `repo_context_refs` (Optional): Comma-separated list of paths to relevant files or directories within the repository for additional context. Can be provided dynamically (e.g., from a PR comment).
*   `additional_comments` (Optional): Extra comments or instructions for the AI reviewer.
*   `gemini_model` (Optional): The specific Gemini model to use (default: `gemini-3-pro-preview`).

## Development

Contributions are welcome! Please ensure that any changes adhere to the existing code style and conventions.

## Customizing AI Prompts

The intelligence and behavior of the AI reviewer are primarily governed by Markdown prompt templates stored in the `prompts/` directory within this action.

*   `review_file.md`: Contains the detailed instructions and checklist for the AI when reviewing individual Lean files.
*   `synthesize_summary.md`: Guides the AI in generating the overall high-level summary from the per-file reviews.

You can modify these `.md` files directly within your forked repository to fine-tune the AI's persona, review criteria, desired output format, or focus areas. Placeholders like `` `{{EXTERNAL_CONTEXT}}` ``, `` `{{REPO_CONTEXT}}` ``, `` `{{FILE_PATH}}` ``, `` `{{FILE_DIFF}}` ``, and `` `{{PER_FILE_REVIEWS}}` `` are used to inject dynamic information into the prompts during runtime. Ensure these placeholders are kept intact if you wish the AI to receive the corresponding context.
