import os
import argparse
import subprocess
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import fitz
import io
import logging
import re
from typing import Tuple, List, Dict

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Helper Functions ---
def get_pr_diff(pr_number: str) -> Tuple[str, List[str]]:
    """Fetches the diff of the specified pull request."""
    logging.info(f"Fetching PR diff for PR #{pr_number}...")
    errors = []
    try:
        diff = subprocess.check_output(["gh", "pr", "diff", pr_number], text=True, stderr=subprocess.PIPE).strip()
        if not diff:
            logging.warning("PR diff is empty.")
            errors.append("Could not retrieve PR diff or diff is empty.")
        logging.info("Successfully fetched PR diff.")
        return diff, errors
    except subprocess.CalledProcessError as e:
        error_message = f"Failed to fetch PR diff for PR #{pr_number}: {e.stderr}"
        logging.error(error_message)
        errors.append(error_message)
        return "", errors

def get_document_content(urls_str: str) -> Tuple[str, List[str]]:
    """Fetches and extracts text content from a comma-separated string of URLs."""
    if not urls_str:
        logging.info("No external references provided.")
        return "No external references were provided.", []
    all_docs_content, errors = "", []
    urls = [url.strip() for url in urls_str.split(',') if url.strip()]
    logging.info(f"Fetching content from {len(urls)} external references...")
    for url in urls:
        try:
            logging.info(f"Processing URL: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, timeout=30, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            content = ""
            if "application/pdf" in content_type or url.lower().endswith('.pdf'):
                with fitz.open(stream=io.BytesIO(response.content), filetype="pdf") as doc:
                    content = "".join(page.get_text() for page in doc)
            else:
                soup = BeautifulSoup(response.content, "html.parser")
                for element in soup(["script", "style", "nav", "footer", "header"]):
                    element.decompose()
                text = soup.get_text()
                lines = (line.strip() for line in text.splitlines())
                content = "\n".join(chunk for line in lines for chunk in line.split("  ") if chunk)
            all_docs_content += f"--- Start of content from {url} ---\n{content}\n--- End of content from {url} ---\n\n"
            logging.info(f"Successfully processed URL: {url}")
        except Exception as e:
            error_message = f"Error processing document '{url}': {e}"
            logging.error(error_message)
            errors.append(error_message)
    return all_docs_content, errors

def get_repo_files_content(paths_str: str) -> Tuple[str, List[str]]:
    """Reads content from a comma-separated string of file and directory paths."""
    if not paths_str:
        logging.info("No ArkLib references were provided.")
        return "No ArkLib references were provided.", []
    all_files_content, errors = "", []
    paths = [path.strip() for path in paths_str.split(',') if path.strip()]
    logging.info(f"Fetching content from {len(paths)} repository paths...")
    expanded_files = []
    for path in paths:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                expanded_files.extend([os.path.join(root, name) for name in files])
        elif os.path.isfile(path):
            expanded_files.append(path)
        else:
            errors.append(f"Could not find file or directory: {path}")
    for file_path in sorted(list(set(expanded_files))):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                all_files_content += f"--- Start of content from {file_path} ---\n{content}\n--- End of content from {file_path} ---\n\n"
        except Exception as e:
            errors.append(f"Error reading file {file_path}: {e}")
    return all_files_content, errors

def split_diff_into_files(diff_content: str) -> Dict[str, str]:
    """Splits a full git diff into a dictionary of per-file diffs."""
    files = {}
    file_diffs = re.split(r'(?=diff --git a/.+ b/.+)', diff_content)
    for file_diff in file_diffs:
        if not file_diff.strip():
            continue
        match = re.search(r'diff --git a/(.+) b/(.+)', file_diff)
        if match:
            file_path = match.group(2)
            files[file_path] = file_diff
    return files

def analyze_file_changes_with_context(review_context: dict, file_path: str, file_diff: str) -> str:
    """Generates a detailed code review for a single file using the specified Gemini model."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "Error: GEMINI_API_KEY not set."

    gemini_model = review_context.get("gemini_model", "gemini-3-pro-preview")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(gemini_model)

    action_path = os.path.dirname(os.path.realpath(__file__))
    prompt_template_path = os.path.join(action_path, "prompts", "review_file.md")

    try:
        with open(prompt_template_path, "r") as f:
            prompt_template = f.read()
    except FileNotFoundError:
        return f"Error: Prompt template not found at {prompt_template_path}"

    additional_comments = review_context.get("additional_comments", "")
    additional_comments_section = ""
    if additional_comments and additional_comments.strip():
        additional_comments_section = f"""**Additional Reviewer Comments:**
---
{additional_comments}
---
"""

    prompt = prompt_template.replace("{{EXTERNAL_CONTEXT}}", review_context.get("external_context", "")) \
                            .replace("{{REPO_CONTEXT}}", review_context.get("repo_context", "")) \
                            .replace("{{FILE_PATH}}", file_path) \
                            .replace("{{FILE_DIFF}}", file_diff) \
                            .replace("{{ADDITIONAL_COMMENTS}}", additional_comments_section)
    
    try:
        logging.info(f"Generating review for file: {file_path}...")
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logging.error(f"Error during Gemini API call for {file_path}: {e}")
        return f"An error occurred while analyzing `{file_path}`: {e}"

def synthesize_overall_summary(per_file_reviews: Dict[str, str], model_name: str) -> str:
    """Generates a high-level summary from all per-file reviews."""
    if not per_file_reviews:
        return "No files were reviewed."
    
    formatted_reviews = "\n\n".join(f"### Review for `{file_path}`:\n{review_text}" for file_path, review_text in per_file_reviews.items())
    
    action_path = os.path.dirname(os.path.realpath(__file__))
    prompt_template_path = os.path.join(action_path, "prompts", "synthesize_summary.md")

    try:
        with open(prompt_template_path, "r") as f:
            prompt_template = f.read()
    except FileNotFoundError:
        return f"Error: Prompt template not found at {prompt_template_path}"

    prompt = prompt_template.replace("{{PER_FILE_REVIEWS}}", formatted_reviews)

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: return "Error: GEMINI_API_KEY not set."
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    try:
        logging.info("Synthesizing overall summary...")
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logging.error(f"Error during Gemini API call for summary synthesis: {e}")
        return f"An error occurred while synthesizing the summary: {e}"

def main():
    parser = argparse.ArgumentParser(description="AI Code Reviewer")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--external-refs", default="")
    parser.add_argument("--arklib-refs", default="")
    parser.add_argument("--additional-comments", default="")
    parser.add_argument("--gemini-model", default="gemini-3-pro-preview")
    args = parser.parse_args()

    diff, diff_errors = get_pr_diff(args.pr_number)
    if diff_errors and not diff:
        print("Aborting review: Could not fetch PR diff. Errors:\n" + "\n".join(diff_errors))
        return

    external_context, external_errors = get_document_content(args.external_refs)
    repo_context, repo_errors = get_repo_files_content(args.arklib_refs)

    all_errors = diff_errors + external_errors + repo_errors
    if all_errors:
        error_section = "\n--- Errors Encountered During Context Fetching ---\n" + "\n".join(all_errors)
        repo_context += error_section
        logging.warning("Encountered non-critical errors. Review will proceed with partial context.")

    review_context = {
        "external_context": external_context,
        "repo_context": repo_context,
        "additional_comments": args.additional_comments,
        "gemini_model": args.gemini_model,
    }

    diff_by_file = split_diff_into_files(diff)
    per_file_reviews = {}
    for file_path, file_diff in diff_by_file.items():
        if not file_path.endswith(".lean"):
            logging.info(f"Skipping non-Lean file: {file_path}")
            continue
        review_text = analyze_file_changes_with_context(review_context, file_path, file_diff)
        per_file_reviews[file_path] = review_text
    
    overall_summary = synthesize_overall_summary(per_file_reviews, args.gemini_model)

    # Format the final comment for printing to stdout
    final_comment = f"### 🤖 AI Review\n\n**Overall Summary:**\n{overall_summary}\n\n---\n"
    for file_path, review_text in per_file_reviews.items():
        final_comment += f"\n<details><summary>📄 **Review for `{file_path}`**</summary>\n\n{review_text}\n</details>\n"
    
    print(final_comment)

if __name__ == "__main__":
    main()
