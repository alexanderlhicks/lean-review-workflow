import os
import argparse
import subprocess
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import fitz
import io
import logging

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

from typing import Tuple, List

# --- Helper Functions ---
# --- PR diff ---
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

# --- Reference document ---
def get_document_content(urls_str: str) -> Tuple[str, List[str]]:
    """Fetches and extracts text content from a comma-separated string of URLs."""
    if not urls_str:
        logging.info("No external references provided.")
        return "No external references were provided.", []

    all_docs_content = ""
    errors = []
    urls = [url.strip() for url in urls_str.split(',')]
    logging.info(f"Fetching content from {len(urls)} external references...")

    for url in urls:
        if not url: continue
        try:
            logging.info(f"Processing URL: {url}")
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            response = requests.get(url, timeout=30, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            content = ""
            if "application/pdf" in content_type or url.lower().endswith('.pdf'):
                with fitz.open(stream=io.BytesIO(response.content), filetype="pdf") as doc:
                    content = "".join(page.get_text() for page in doc)
            else:
                soup = BeautifulSoup(response.content, "html.parser")
                for element in soup(["script", "style", "nav", "footer", "header"]): element.decompose()
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

# --- Relevant files from the repository ---
def get_repo_files_content(paths_str: str) -> Tuple[str, List[str]]:
    """Reads content from a comma-separated string of file and directory paths."""
    if not paths_str:
        logging.info("No ArkLib references were provided.")
        return "No ArkLib references were provided.", []

    all_files_content = ""
    errors = []
    paths = [path.strip() for path in paths_str.split(',')]
    logging.info(f"Fetching content from {len(paths)} repository paths...")

    expanded_files = []
    for path in paths:
        if not path: continue
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for name in files:
                    expanded_files.append(os.path.join(root, name))
        elif os.path.isfile(path):
            expanded_files.append(path)
        else:
            error_message = f"Could not find file or directory: {path}"
            logging.warning(error_message)
            errors.append(error_message)

    unique_files = sorted(list(set(expanded_files)))
    logging.info(f"Found {len(unique_files)} unique files to read.")

    for file_path in unique_files:
        try:
            logging.info(f"Reading file: {file_path}")
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                all_files_content += f"--- Start of content from {file_path} ---\n{content}\n--- End of content from {file_path} ---\n\n"
        except Exception as e:
            error_message = f"Error reading file {file_path}: {e}"
            logging.error(error_message)
            errors.append(error_message)

    return all_files_content, errors

def analyze_code_with_context(review_context: dict) -> str:
    """Generates a code review using the specified Gemini model."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logging.error("GEMINI_API_KEY environment variable not set.")
        return "Error: GEMINI_API_KEY environment variable not set."

    gemini_model = review_context.get("gemini_model", "gemini-3-pro-preview")
    logging.info(f"Configuring Gemini API with model: {gemini_model}")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(gemini_model)

    additional_comments = review_context.get("additional_comments", "")
    additional_comments_section = ""
    if additional_comments and additional_comments.strip():
        additional_comments_section = f"""
    **4. Additional Reviewer Comments:**
    ---
    {additional_comments}
    ---
    """

    prompt = f"""
    You are a meticulous senior engineer specializing in formal verification. Your task is to rigorously review a pull request for misformalization issues. You have been given the following information:
    1.  The content of external reference documents, which contains the formal specification.
    2.  The full content of other relevant files from the repository.
    3.  The code changes ("diff") from the pull request that intends to implement the specification.
    {additional_comments_section}
    **1. External Reference Documents (Specification):**
    ---
    {review_context.get("external_context", "")}
    ---

    **2. Additional Repository Context Files:**
    ---
    {review_context.get("repo_context", "")}
    ---

    **3. Pull Request Diff:**
    ---
    {review_context.get("diff", "")}
    ---

    **Your Instructions:**
    Follow these steps precisely to conduct your review:
    1.  **Summarize Goal:** In a single sentence, state the primary goal of this pull request based on the provided context.
    2.  **Identify Specification:** Quote the specific section(s) from the "External Reference Documents" that the PR is attempting to formalize.
    3.  **Analyze Implementation:** Go through the "Pull Request Diff" hunk by hunk. For each change, analyze its logic and correctness. Explicitly map the code changes back to the specification you identified.
    4.  **Check for Misformalization:** Critically assess whether the code is a correct and complete formalization of the specification. Pay close attention to edge cases, logical inconsistencies, incorrect assumptions, or deviations from the formal model.
    5.  **Provide Verdict:** State clearly whether the formalization is correct or incorrect.
    6.  **Actionable Feedback:** If the formalization is incorrect, provide a detailed explanation of the misformalization. Explain *why* it is wrong and illustrate your point with corrected code snippets. If the formalization is correct, state that and suggest any minor improvements if applicable.

    Structure your review clearly using markdown for formatting.
    """
    try:
        logging.info("Generating code review with Gemini API...")
        response = model.generate_content(prompt)
        logging.info("Successfully generated review.")
        return response.text
    except Exception as e:
        logging.error(f"An error occurred while calling the Gemini API: {e}")
        return f"An error occurred while calling the Gemini API: {e}"

def main():
    parser = argparse.ArgumentParser(description="AI Code Reviewer")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--external-refs", required=False, default="")
    parser.add_argument("--arklib-refs", required=False, default="")
    parser.add_argument("--additional-comments", required=False, default="")
    parser.add_argument("--gemini-model", required=False, default="gemini-3-pro-preview")
    args = parser.parse_args()

    diff, diff_errors = get_pr_diff(args.pr_number)
    external_context, external_errors = get_document_content(args.external_refs)
    repo_context, repo_errors = get_repo_files_content(args.arklib_refs)

    # Abort if the PR diff could not be fetched, as it's essential for the review.
    if diff_errors and not diff:
        error_message = "Aborting review: Could not fetch PR diff. Errors:\n" + "\n".join(diff_errors)
        logging.error(error_message)
        print(error_message) # Print to output for visibility in the PR comment.
        return

    # For non-critical errors (e.g., a single missing file), log them and append to the context
    # This informs the AI that some context might be missing.
    all_errors = diff_errors + external_errors + repo_errors
    if all_errors:
        error_section = "\n--- Errors Encountered During Context Fetching ---\n" + "\n".join(all_errors)
        repo_context += error_section
        logging.warning("Encountered non-critical errors during context fetching. The review will proceed with partial context.")

    logging.info("Generating AI review...")
    review_context = {
        "diff": diff,
        "external_context": external_context,
        "repo_context": repo_context,
        "additional_comments": args.additional_comments,
        "gemini_model": args.gemini_model,
    }
    review = analyze_code_with_context(review_context)
    print(review)

if __name__ == "__main__":
    main()
