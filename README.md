# AI Code Review Workflow for Lean Projects

This GitHub Action provides an advanced, AI-powered code review for Pull Requests in Lean projects, with a strong focus on detecting potential misformalization issues. It leverages the Gemini API to analyze code changes in the context of formal specifications and project dependencies.

## Features

*   **Multi-Agent AI Architecture:** Transforms the review process into a specialized pipeline:
    1.  **Specification Analyst (Agent A):** Reads external PDFs and math papers to extract a rigorous "Formalization Checklist" of concepts and edge cases that must be handled.
    2.  **Code Reviewer (Agent B):** Evaluates the Lean PR diffs and full file contents strictly against the formal checklist provided by Agent A, checking for universe errors, off-by-one errors, and misformalizations.
    3.  **Lead Synthesizer (Agent C):** Aggregates the findings into a clear, prioritized executive summary for the Pull Request.
*   **Multimodal Specification Analysis:** Agent A uses Gemini's native PDF support to "read" mathematical formulas and diagrams directly from arXiv papers or textbook PDFs, ensuring high-fidelity extraction of mathematical intent.
*   **Enhanced Lean Dependency Discovery:** Automatically identifies relevant Lean files impacted by a PR by leveraging `lake exe graph --json`. 
*   **Parallel File Processing:** Reviews multiple files concurrently, drastically speeding up the review process for large pull requests.
*   **Structured, File-by-File Review Output:** Provides an overall summary of the PR, followed by detailed and collapsible review sections for each individual Lean file changed.
*   **Customizable AI Prompts:** The core instructions given to the AI are stored in external Markdown files (`prompts/`), allowing for easy customization of the agents' personas.
*   **GitHub Raw Support:** Automatically handles GitHub URLs for external references, fetching raw source code for the AI to analyze.
*   **PR Hygiene:** Automatically updates existing AI review comments to keep the PR discussion thread clean and focused.

## How it Works

1.  **Checkout Repository:** Fetches the full Git history of the repository.
2.  **Set up Python & Lean:** Configures the environment with Python for the review script and Lean/Lake for building the project and generating dependency graphs.
3.  **Install Python Dependencies:** Installs required Python libraries (e.g., `google-generativeai`, `requests`, `beautifulsoup4`, `pydantic`).
4.  **Discover Related Files:**
    *   Identifies all `.lean` files changed in the pull request.
    *   Attempts to build the Lean project using `lake build`.
    *   If successful, it runs `lake exe graph --json` to get the precise dependency graph (capped to top 15 dependencies for context efficiency).
5.  **Run AI Review Script:** Executes `review.py` with the gathered context (Multimodal PR diff, external references, dependency content).
6.  **Post Review Comment:** Publishes or updates the AI-generated review as a comment on the Pull Request.

## Usage

This is a composite action. To unlock its full power (specifically the `external_refs` and `additional_comments` inputs), it is highly recommended to trigger this action via a **PR Comment (ChatOps)**.

### Recommended: ChatOps Workflow (Dynamic PR Comments)

Create a workflow file in your repository at `.github/workflows/ai-chatops.yml`:

```yaml
name: AI PR ChatOps
on:
  issue_comment:
    types: [created]

jobs:
  ai_review_chatops:
    if: ${{ github.event.issue.pull_request && startsWith(github.event.comment.body, '/review') }}
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - name: Parse Command
        id: parse_command
        uses: actions/github-script@v7
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

---

### Alternative: Standard Push Workflow (Static)

```yaml
name: AI Code Review for Lean PRs

on:
  pull_request:
    types: [opened, synchronize]

jobs:
  ai_review_lean:
    runs-on: ubuntu-latest
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
          gemini_model: "gemini-3.1-pro-preview"
```

### Inputs

*   `github_token` (Required): GitHub Token for API calls.
*   `gemini_api_key` (Required): Gemini API Key for AI review generation.
*   `pr_number` (Required): The Pull Request number.
*   `external_refs` (Optional): Comma-separated list of URLs to external documents (PDFs, HTML, Raw Source).
*   `repo_context_refs` (Optional): Comma-separated list of paths to additional internal context.
*   `additional_comments` (Optional): Extra focus instructions for the AI reviewer.
*   `gemini_model` (Optional): Default: `gemini-3.1-pro-preview`.
*   `lint` (Optional): Whether to run the linter (default: `false`).

## Development

Contributions are welcome! Please ensure that any changes adhere to the existing code style and conventions.

## Customizing AI Prompts

The intelligence and behavior of the AI reviewer are primarily governed by Markdown prompt templates stored in the `prompts/` directory within this action.

*   `review_file.md`: Contains the detailed instructions and checklist for the AI when reviewing individual Lean files.
*   `synthesize_summary.md`: Guides the AI in generating the overall high-level summary from the per-file reviews.

You can modify these `.md` files directly within your forked repository to fine-tune the AI's persona, review criteria, desired output format, or focus areas. Placeholders like `` `{{EXTERNAL_CONTEXT}}` ``, `` `{{REPO_CONTEXT}}` ``, `` `{{FILE_PATH}}` ``, `` `{{FILE_DIFF}}` ``, and `` `{{PER_FILE_REVIEWS}}` `` are used to inject dynamic information into the prompts during runtime. Ensure these placeholders are kept intact if you wish the AI to receive the corresponding context.
