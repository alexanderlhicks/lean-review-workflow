import os
import argparse
import subprocess
import requests
import json
import time
import sys
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
import logging
import re
from typing import Tuple, List, Dict, Union, Literal
from concurrent.futures import ThreadPoolExecutor
from pydantic import BaseModel, Field

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

ACTION_PATH = os.path.dirname(os.path.realpath(__file__))

# --- Pydantic Schemas for Multi-Agent Orchestration ---
class ChecklistItem(BaseModel):
    concept: str = Field(description="The mathematical concept or theorem name.")
    verification_steps: list[str] = Field(description="List of specific things to check for to avoid misformalization.")
    severity: Literal["Critical", "Major", "Minor"] = Field(description="Severity of this item: 'Critical', 'Major', or 'Minor'")

class SpecChecklist(BaseModel):
    items: list[ChecklistItem] = Field(description="List of checklist items derived from the specification.")

# --- Helper Functions ---
def generate_with_retry(client: genai.Client, model: str, contents, config=None, max_retries=3):
    """Wraps Gemini API calls with exponential backoff."""
    for attempt in range(max_retries):
        try:
            if config:
                return client.models.generate_content(model=model, contents=contents, config=config)
            else:
                return client.models.generate_content(model=model, contents=contents)
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            logging.warning(f"API call failed, retrying ({attempt+1}/{max_retries}): {e}")
            time.sleep(2 ** attempt)

def get_document_content(urls_str: str) -> Tuple[List[Union[str, types.Part]], List[str]]:
    """Fetches and extracts content from a comma-separated string of URLs. Returns Gemini Parts."""
    if not urls_str:
        logging.info("No external references provided.")
        return [], []
    
    parts, errors = [], []
    urls = [url.strip() for url in urls_str.split(',') if url.strip()]
    logging.info(f"Fetching content from {len(urls)} external references...")
    
    for url in urls:
        try:
            logging.info(f"Processing URL: {url}")
            # Handle GitHub URLs: convert to raw content if possible
            processed_url = url
            if "github.com" in url and "/blob/" in url:
                processed_url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
                logging.info(f"Converted GitHub URL to raw: {processed_url}")

            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(processed_url, timeout=30, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            
            if "application/pdf" in content_type or url.lower().endswith('.pdf'):
                # Use native PDF support
                pdf_part = types.Part.from_bytes(
                    data=response.content,
                    mime_type="application/pdf"
                )
                parts.append(pdf_part)
                logging.info(f"Added PDF part from: {url}")
            elif "text/html" in content_type or url.lower().endswith(('.html', '.htm')):
                # Parse HTML to extract readable text
                soup = BeautifulSoup(response.content, "html.parser")
                for element in soup(["script", "style", "nav", "footer", "header"]):
                    element.decompose()
                text = soup.get_text()
                lines = (line.strip() for line in text.splitlines())
                content = "\n".join(chunk for line in lines for chunk in line.split("  ") if chunk)
                parts.append(f"--- Content from {url} ---\n{content}\n")
                logging.info(f"Added parsed HTML part from: {url}")
            else:
                # Treat as plain text (markdown, lean, txt, raw github files, etc.)
                # This preserves crucial whitespace and formatting
                content = response.text
                parts.append(f"--- Content from {url} ---\n{content}\n")
                logging.info(f"Added plain text part from: {url}")
        except Exception as e:
            error_message = f"Error processing document '{url}': {e}"
            logging.error(error_message)
            errors.append(error_message)
    return parts, errors

def analyze_specification(client: genai.Client, external_parts: List[Union[str, types.Part]], cached_content_name: str, model_name: str, all_diffs: str) -> str:
    """Agent A: Analyzes the external specification and generates a checklist."""
    if not external_parts and not cached_content_name:
        return ""
    
    prompt_template_path = os.path.join(ACTION_PATH, "prompts", "analyze_spec.md")
    
    try:
        with open(prompt_template_path, "r") as f:
            prompt_template = f.read()
    except FileNotFoundError:
        logging.error(f"Error: Prompt template not found at {prompt_template_path}")
        return ""

    # Prepare multimodal contents
    prompt_text = prompt_template.replace("{{EXTERNAL_CONTEXT}}", "Please refer to the following external reference documents.") \
                                 .replace("{{FILE_DIFFS}}", all_diffs)
    
    config = {
        'response_mime_type': 'application/json',
        'response_schema': SpecChecklist,
    }
    
    if cached_content_name:
        contents = [prompt_text]
        config['cached_content'] = cached_content_name
    else:
        contents = [prompt_text] + external_parts
    
    try:
        logging.info("Agent A (Spec Analyst) is generating the formalization checklist...")
        response = generate_with_retry(
            client,
            model_name,
            contents,
            config
        )
        
        checklist: SpecChecklist = response.parsed
        checklist_str = ""
        if checklist:
            for item in checklist.items:
                checklist_str += f"- **{item.concept}** [{item.severity}]\n"
                for step in item.verification_steps:
                    checklist_str += f"  - [ ] {step}\n"
        
        logging.info("Spec checklist generated successfully.")
        return checklist_str
    except Exception as e:
        logging.error(f"Error during Spec Analysis: {e}")
        return ""

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


def get_repo_files_content(paths_str: str) -> Tuple[str, List[str]]:
    """Reads content from a comma-separated string of file and directory paths."""
    if not paths_str:
        logging.info("No repository context files were provided.")
        return "No repository context files were provided.", []
    all_files_content, errors = "", []
    paths = [path.strip() for path in paths_str.split(',') if path.strip()]
    logging.info(f"Fetching content from {len(paths)} repository paths...")
    expanded_files = []
    for path in paths:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                expanded_files.extend([os.path.join(root, name) for name in files if name.endswith(('.lean', '.md'))])
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
    file_diffs = re.split(r'(?=^diff --git a/.+ b/.+$)', diff_content, flags=re.MULTILINE)
    for file_diff in file_diffs:
        if not file_diff.strip():
            continue
        match = re.search(r'^diff --git a/(.+) b/(.+)$', file_diff, re.MULTILINE)
        if match:
            file_path = match.group(2)
            files[file_path] = file_diff
    return files

def analyze_file_changes_with_context(client: genai.Client, review_context: dict, file_path: str, file_diff: str, full_content: str, spec_checklist: str, external_parts: list, cached_content_name: str, lean4_checklist: str, verdict_rules: str) -> str:
    """Agent B (Code Reviewer): Generates a detailed code review for a single file using the specified Gemini model and the Checklist."""
    
    # Select the appropriate prompt depending on if we have a checklist from Agent A
    prompt_file = "review_code_with_spec.md" if spec_checklist else "review_file.md"
    prompt_template_path = os.path.join(ACTION_PATH, "prompts", prompt_file)

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

    prompt_text = prompt_template.replace("{{SPEC_CHECKLIST}}", spec_checklist) \
                            .replace("{{REPO_CONTEXT}}", review_context.get("repo_context", "")) \
                            .replace("{{FILE_PATH}}", file_path) \
                            .replace("{{FILE_DIFF}}", file_diff) \
                            .replace("{{FULL_CONTENT}}", full_content) \
                            .replace("{{ADDITIONAL_COMMENTS}}", additional_comments_section) \
                            .replace("{{LEAN4_CHECKLIST}}", lean4_checklist) \
                            .replace("{{VERDICT_RULES}}", verdict_rules)
    
    config = {}
    # Handle the fallback template which still has EXTERNAL_CONTEXT
    if "{{EXTERNAL_CONTEXT}}" in prompt_text:
        prompt_text = prompt_text.replace("{{EXTERNAL_CONTEXT}}", "Please refer to the following external reference documents.")
        if cached_content_name:
            contents = [prompt_text]
            config['cached_content'] = cached_content_name
        else:
            # Fallback: strip raw PDF types.Part to avoid rate limit/redundant API costs
            filtered_parts = [p for p in external_parts if isinstance(p, str)]
            contents = [prompt_text] + filtered_parts
    else:
        contents = [prompt_text]

    try:
        logging.info(f"Agent B is reviewing file: {file_path}...")
        gemini_model = review_context.get("gemini_model")
        
        if config:
            response = generate_with_retry(client, gemini_model, contents, config)
        else:
            response = generate_with_retry(client, gemini_model, contents)
            
        return response.text
    except Exception as e:
        logging.error(f"Error during Gemini API call for {file_path}: {e}")
        return f"An error occurred while analyzing `{file_path}`: {e}"

def synthesize_overall_summary(client: genai.Client, per_file_reviews: Dict[str, str], spec_checklist: str, verdict_rules: str, model_name: str) -> str:
    """Generates a high-level summary from all per-file reviews."""
    if not per_file_reviews:
        return "No files were reviewed."
    
    formatted_reviews = "\n\n".join(f"### Review for `{file_path}`:\n{review_text}" for file_path, review_text in per_file_reviews.items())
    
    prompt_template_path = os.path.join(ACTION_PATH, "prompts", "synthesize_summary.md")

    try:
        with open(prompt_template_path, "r") as f:
            prompt_template = f.read()
    except FileNotFoundError:
        return f"Error: Prompt template not found at {prompt_template_path}"

    prompt = prompt_template.replace("{{PER_FILE_REVIEWS}}", formatted_reviews).replace("{{SPEC_CHECKLIST}}", spec_checklist or "No explicit checklist provided.").replace("{{VERDICT_RULES}}", verdict_rules)

    try:
        logging.info("Synthesizing overall summary...")
        response = generate_with_retry(client, model_name, prompt)
        return response.text
    except Exception as e:
        logging.error(f"Error during Gemini API call for summary synthesis: {e}")
        return f"An error occurred while synthesizing the summary: {e}"

def main():
    parser = argparse.ArgumentParser(description="AI Code Reviewer")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--external-refs", default="")
    parser.add_argument("--repo-context-refs", default="")
    parser.add_argument("--additional-comments", default="")
    parser.add_argument("--gemini-model", default="gemini-2.5-pro")
    args = parser.parse_args()

    diff, diff_errors = get_pr_diff(args.pr_number)
    if diff_errors and not diff:
        logging.error("Aborting review: Could not fetch PR diff. Errors:\n" + "\n".join(diff_errors))
        sys.exit(1)

    diff_by_file = split_diff_into_files(diff)
    if not diff_by_file:
        print("### 🤖 AI Review\n\nNo Lean files were changed in this PR.")
        return

    external_parts, external_errors = get_document_content(args.external_refs)
    repo_context, repo_errors = get_repo_files_content(args.repo_context_refs)

    all_errors = external_errors + repo_errors
    if all_errors:
        logging.warning("Encountered non-critical errors. Review will proceed with partial context.")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logging.error("Error: GEMINI_API_KEY not set.")
        sys.exit(1)
    client = genai.Client(api_key=api_key)

    cached_content_name = None
    if external_parts:
        try:
            logging.info("Attempting to create context cache for external references...")
            cache = client.caches.create(
                model=args.gemini_model,
                config=types.CreateCachedContentConfig(
                    contents=external_parts,
                    ttl="3600s",
                    display_name="External Context Cache"
                )
            )
            cached_content_name = cache.name
            logging.info(f"Successfully created context cache: {cached_content_name}")
        except Exception as e:
            logging.warning(f"Could not create context cache (likely too small for caching, falling back to inline): {e}")

    try:
        review_context = {
            "external_context": "[Multimodal Content Provided]",
            "repo_context": repo_context,
            "additional_comments": args.additional_comments,
            "gemini_model": args.gemini_model,
        }
        
        lean4_checklist_path = os.path.join(ACTION_PATH, "prompts", "lean4_checklist.md")
        try:
            with open(lean4_checklist_path, "r") as f:
                lean4_checklist = f.read()
        except FileNotFoundError:
            logging.error(f"Error: lean4_checklist.md not found at {lean4_checklist_path}")
            sys.exit(1)

        verdict_rules_path = os.path.join(ACTION_PATH, "prompts", "verdict_rules.md")
        try:
            with open(verdict_rules_path, "r") as f:
                verdict_rules = f.read()
        except FileNotFoundError:
            logging.error(f"Error: verdict_rules.md not found at {verdict_rules_path}")
            sys.exit(1)

        all_diffs = "\n".join([f"--- {f} ---\n{d}" for f, d in diff_by_file.items()])
        
        # --- Multi-Agent Orchestration Step 1: Spec Analysis ---
        spec_checklist = analyze_specification(client, external_parts, cached_content_name, args.gemini_model, all_diffs)
        if spec_checklist:
            logging.info("Spec Analysis complete. Handing off checklist to Code Reviewers.")
        else:
            logging.info("No external specification provided or analysis failed. Proceeding with standard review.")

        def process_file(file_path, file_diff, review_ctx):
            if not file_path.endswith(".lean"):
                logging.info(f"Skipping non-Lean file: {file_path}")
                return None, None
            
            full_content = ""
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        full_content = f.read()
                except Exception as e:
                    logging.error(f"Error reading {file_path}: {e}")
            
            # --- Multi-Agent Orchestration Step 2: Code Review against Checklist ---
            review_text = analyze_file_changes_with_context(client, review_ctx, file_path, file_diff, full_content, spec_checklist, external_parts, cached_content_name, lean4_checklist, verdict_rules)
            return file_path, review_text

        per_file_reviews = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = executor.map(lambda p: process_file(p[0], p[1], review_context), diff_by_file.items())
            for file_path, review_text in results:
                if file_path:
                    per_file_reviews[file_path] = review_text
        
        overall_summary = synthesize_overall_summary(client, per_file_reviews, spec_checklist, verdict_rules, args.gemini_model)

        # Format the final comment for printing to stdout
        final_comment = f"### 🤖 AI Review\n\n**Overall Summary:**\n{overall_summary}\n\n---\n"
        for file_path, review_text in per_file_reviews.items():
            final_comment += f"\n<details><summary>📄 **Review for `{file_path}`**</summary>\n\n{review_text}\n</details>\n"
        
        print(final_comment)
    finally:
        if cached_content_name:
            try:
                client.caches.delete(name=cached_content_name)
                logging.info("Cleaned up context cache.")
            except Exception as e:
                logging.warning(f"Failed to delete context cache: {e}")

if __name__ == "__main__":
    main()
