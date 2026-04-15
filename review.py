import argparse
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Literal, Tuple, Union
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from lean_utils import is_in_comment, FileCache, file_path_to_module_name
from llm_provider import LLMProvider, ContentPart, TokenUsage, create_provider

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

ACTION_PATH = os.path.dirname(os.path.realpath(__file__))

# --- Pydantic Schemas for Multi-Agent Orchestration ---
class ReferenceMappingEntry(BaseModel):
    paper_result: str = Field(description="The theorem/definition as stated in the paper (section number, statement).")
    mathematical_content: str = Field(description="The precise mathematical content (hypotheses, conclusion, objects) that any correct formalization must preserve.")
    status: Literal["Present", "Missing", "Partial"] = Field(description="Whether the diff contains a corresponding formalization.")

class ChecklistItem(BaseModel):
    concept: str = Field(description="The mathematical concept or theorem name.")
    verification_steps: list[str] = Field(description="List of specific things to check for to avoid misformalization.")
    severity: Literal["Critical", "Major", "Minor"] = Field(description="Severity of this item: 'Critical', 'Major', or 'Minor'")

class SpecChecklist(BaseModel):
    reference_mapping: list[ReferenceMappingEntry] = Field(default_factory=list, description="Paper Result → Expected Lean Statement mapping table.")
    items: list[ChecklistItem] = Field(description="List of checklist items derived from the specification.")

# --- Agent B: Per-File Review Schema ---
class ChecklistResult(BaseModel):
    item: str = Field(description="The checklist item being verified.")
    status: Literal["satisfied", "violated", "unclear"] = Field(description="Whether the code satisfies, violates, or is unclear on this item.")
    explanation: str = Field(description="Brief explanation of the status.")

class Finding(BaseModel):
    description: str = Field(description="Description of the finding.")
    location: str = Field(default="", description="File path and line/range if applicable, e.g. 'MyFile.lean:42' or 'MyFile.lean:42-55'.")
    suggested_fix: str = Field(default="", description="Suggested fix or corrected code snippet, if applicable.")

class FileReview(BaseModel):
    analysis: str = Field(default="", description="Step-by-step analysis BEFORE findings: (1) What does the changed code do mathematically? (2) How do changes relate to the spec checklist? (3) What are the riskiest aspects? (4) Any ambiguities in mathematical intent?")
    verdict: Literal["Approved", "Needs Minor Revisions", "Changes Requested"] = Field(description="The verdict for this file.")
    checklist_results: list[ChecklistResult] = Field(default_factory=list, description="Checklist verification results (only when spec checklist provided).")
    critical_misformalizations: list[Finding] = Field(default_factory=list, description="Mathematical errors, broken assumptions, missing hypotheses.")
    lean_issues: list[Finding] = Field(default_factory=list, description="Lean 4 / Mathlib idiom violations, typeclass issues, escape hatches.")
    nitpicks: list[Finding] = Field(default_factory=list, description="Naming, style, minor cleanups.")

# --- Cross-File Analysis Schema ---
class CrossFileAnalysis(BaseModel):
    analysis: str = Field(default="", description="Trace the main composition chains across files BEFORE reporting issues. Identify type-flow paths, axiom propagation chains, and external dependency interfaces.")
    composition_issues: list[Finding] = Field(default_factory=list, description="Issues with how files connect: type mismatches, broken composition chains.")
    escape_hatch_impact: list[Finding] = Field(default_factory=list, description="Axioms/sorries and their downstream impact through the dependency chain.")
    external_dependency_issues: list[Finding] = Field(default_factory=list, description="Incorrect usage of external library APIs.")
    missing_cross_file_verification: list[Finding] = Field(default_factory=list, description="Spec items requiring multi-file coordination that lack it.")

# --- Synthesis Schema ---
class SynthesisSummary(BaseModel):
    tldr: str = Field(description="1-2 sentence executive summary of the PR state.")
    precheck_summary: str = Field(description="Summary of mechanical pre-check results.")
    checklist_coverage: str = Field(default="", description="How well the PR covers the specification checklist.")
    cross_file_summary: str = Field(default="", description="Summary of cross-file analysis findings.")
    critical_misformalizations: list[Finding] = Field(default_factory=list, description="Aggregated critical misformalizations.")
    key_lean_issues: list[Finding] = Field(default_factory=list, description="Grouped/deduplicated Lean issues across files.")
    overall_verdict: Literal["Approved", "Needs Minor Revisions", "Changes Requested"] = Field(description="The overall PR verdict.")

# --- Triage Schema ---
class ReviewCluster(BaseModel):
    name: str = Field(description="Short descriptive name for the cluster.")
    files: list[str] = Field(description="File paths in this cluster.")
    review_question: str = Field(description="The key cross-file question to answer for this cluster.")
    priority: Literal["critical", "high", "medium", "low"] = Field(description="Priority of this cluster.")
    review_strategy: str = Field(default="", description="Detailed review strategy: what mathematical properties to verify, what cross-file interactions to check, specific concerns about potential issues.")
    key_hypotheses: list[str] = Field(default_factory=list, description="Specific testable hypotheses for the per-file reviewer to verify or falsify.")

class TriageResult(BaseModel):
    clusters: list[ReviewCluster] = Field(description="Review clusters ordered by priority.")


# --- Token Usage Tracking ---
class TokenTracker:
    """Tracks cumulative token usage across all API calls (thread-safe)."""
    def __init__(self):
        self._lock = threading.Lock()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_thinking_tokens = 0
        self.call_count = 0

    def record(self, usage: TokenUsage):
        """Records token usage from a provider response."""
        with self._lock:
            self.call_count += 1
            self.total_input_tokens += usage.input_tokens
            self.total_output_tokens += usage.output_tokens
            self.total_thinking_tokens += usage.thinking_tokens

    def summary(self) -> str:
        with self._lock:
            total = self.total_input_tokens + self.total_output_tokens + self.total_thinking_tokens
            parts = [f"Token usage: {self.total_input_tokens:,} input + {self.total_output_tokens:,} output"]
            if self.total_thinking_tokens > 0:
                parts.append(f" + {self.total_thinking_tokens:,} thinking")
            parts.append(f" = {total:,} total across {self.call_count} API calls")
            return "".join(parts)

token_tracker = TokenTracker()
file_cache = FileCache()

# Thinking budgets (set by main() from CLI args)
THINKING_BUDGET_HIGH = 10240   # deep analysis agents (Agent A, B, Cross-File)
THINKING_BUDGET_LOW = 2048     # structural agents (Triage, Synthesis)

# --- Helper Functions ---
def _load_prompt(template_name: str, replacements: Dict[str, str]) -> str:
    """Loads a prompt template and applies replacements with validation."""
    path = os.path.join(ACTION_PATH, "prompts", template_name)
    with open(path, "r") as f:
        template = f.read()

    # Validate: warn about unreplaced placeholders after substitution
    result = template
    for key, value in replacements.items():
        placeholder = "{{" + key + "}}"
        result = result.replace(placeholder, value)

    # Check for any remaining unreplaced placeholders
    remaining = re.findall(r'\{\{([A-Z_]+)\}\}', result)
    if remaining:
        logging.warning(f"Unreplaced placeholders in {template_name}: {remaining}")

    return result


def _validate_url(url: str) -> Tuple[bool, str]:
    """Validates a URL is safe to fetch (SSRF protection).
    Blocks private IPs, link-local, loopback, and non-HTTP(S) schemes."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False, f"Blocked non-HTTP scheme: {parsed.scheme}"
        hostname = parsed.hostname
        if not hostname:
            return False, "No hostname in URL"
        hostname = hostname.lower()
        # Check for obvious private/dangerous hostnames
        if hostname in ('localhost', '127.0.0.1', '::1', '0.0.0.0'):
            return False, f"Blocked localhost URL: {hostname}"
        # Try to resolve and check IP ranges
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False, f"Blocked private/reserved IP: {ip}"
        except ValueError:
            # hostname is a domain name, not an IP — check for metadata endpoints
            if hostname.endswith('.internal') or hostname == 'metadata.google.internal':
                return False, f"Blocked internal hostname: {hostname}"
            try:
                resolved = {
                    addr_info[4][0]
                    for addr_info in socket.getaddrinfo(hostname, None)
                    if addr_info[4]
                }
            except socket.gaierror as e:
                return False, f"Hostname resolution failed for {hostname}: {e}"
            if not resolved:
                return False, f"No IP addresses resolved for hostname: {hostname}"
            for resolved_ip in resolved:
                ip = ipaddress.ip_address(resolved_ip)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return False, f"Blocked private/reserved IP via DNS resolution: {resolved_ip}"
        return True, ""
    except Exception as e:
        return False, f"URL validation error: {e}"


def _normalize_external_url(url: str) -> str:
    """Normalizes supported external reference URLs before fetching."""
    processed_url = url
    if "github.com" in url and "/blob/" in url:
        processed_url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        logging.info(f"Converted GitHub URL to raw: {processed_url}")
    return processed_url


def _fetch_url_content(url: str, timeout: int = 30, max_redirects: int = 5) -> Tuple[requests.Response, str]:
    """Fetches a URL while validating every hop to prevent SSRF via redirects."""
    headers = {'User-Agent': 'Mozilla/5.0'}
    current_url = url
    visited = set()
    session = requests.Session()

    for _ in range(max_redirects + 1):
        is_safe, reason = _validate_url(current_url)
        if not is_safe:
            raise ValueError(f"Blocked unsafe URL '{current_url}': {reason}")
        if current_url in visited:
            raise ValueError(f"Redirect loop detected while fetching '{url}'")
        visited.add(current_url)

        response = session.get(current_url, timeout=timeout, headers=headers, allow_redirects=False)
        if 300 <= response.status_code < 400 and response.headers.get("Location"):
            current_url = urljoin(current_url, response.headers["Location"])
            continue

        response.raise_for_status()
        return response, current_url

    raise requests.TooManyRedirects(f"Too many redirects while fetching '{url}'")


def get_document_content(urls_str: str) -> Tuple[List[ContentPart], List[str]]:
    """Fetches content from URLs and returns provider-agnostic ContentParts."""
    if not urls_str:
        logging.info("No external references provided.")
        return [], []

    parts, errors = [], []
    urls = [url.strip() for url in urls_str.split(',') if url.strip()]
    logging.info(f"Fetching content from {len(urls)} external references...")

    for url in urls:
        try:
            logging.info(f"Processing URL: {url}")
            processed_url = _normalize_external_url(url)
            response, final_url = _fetch_url_content(processed_url, timeout=30)
            content_type = response.headers.get("Content-Type", "")

            if "application/pdf" in content_type or final_url.lower().endswith('.pdf'):
                parts.append(ContentPart(type="pdf", data=response.content, mime_type="application/pdf"))
                logging.info(f"Added PDF part from: {url}")
            elif "text/html" in content_type or final_url.lower().endswith(('.html', '.htm')):
                soup = BeautifulSoup(response.content, "html.parser")
                for element in soup(["script", "style", "nav", "footer", "header"]):
                    element.decompose()
                text = soup.get_text()
                lines = (line.strip() for line in text.splitlines())
                content = "\n".join(chunk for line in lines for chunk in line.split("  ") if chunk)
                parts.append(ContentPart(type="text", data=f"--- Content from {url} ---\n{content}\n"))
                logging.info(f"Added parsed HTML part from: {url}")
            else:
                content = response.text
                parts.append(ContentPart(type="text", data=f"--- Content from {url} ---\n{content}\n"))
                logging.info(f"Added plain text part from: {url}")
        except Exception as e:
            error_message = f"Error processing document '{url}': {e}"
            logging.error(error_message)
            errors.append(error_message)
    return parts, errors

def _extract_added_lines(diff_text: str) -> List[str]:
    """Extracts only added lines (starting with +) from a unified diff, excluding diff headers."""
    added = []
    for line in diff_text.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            added.append(line[1:])  # Strip the leading '+'
    return added



def _is_in_string(keyword: str, line: str) -> bool:
    """Basic check: if the keyword appears only inside a string literal."""
    # Find all string regions and check if keyword is exclusively within them
    in_string = False
    string_char = None
    string_ranges = []
    start = 0
    for i, ch in enumerate(line):
        if not in_string and ch == '"':
            in_string = True
            start = i
        elif in_string and ch == '"' and (i == 0 or line[i-1] != '\\'):
            in_string = False
            string_ranges.append((start, i))

    if not string_ranges:
        return False

    # Check each occurrence of keyword
    for m in re.finditer(rf'\b{re.escape(keyword)}\b', line):
        in_any_string = any(s <= m.start() <= e for s, e in string_ranges)
        if not in_any_string:
            return False  # At least one occurrence is outside strings
    return True  # All occurrences are inside strings


def run_mechanical_prechecks(diff_by_file: Dict[str, str]) -> str:
    """Runs deterministic pre-checks on added lines in the diff only.
    Separates findings into 'introduced' (new in this PR) and 'pre-existing' (context)."""
    ESCAPE_HATCHES = ['sorry', 'admit', 'axiom', 'native_decide', 'implemented_by', 'opaque', 'sorryAx']
    introduced_findings = []
    preexisting_findings = []

    for file_path, diff in diff_by_file.items():
        if not file_path.endswith('.lean'):
            continue

        # Scan added lines from the diff (new in this PR)
        added_lines = _extract_added_lines(diff)
        comment_depth = 0
        for line in added_lines:
            in_comment, comment_depth = is_in_comment(line, comment_depth)
            if in_comment:
                continue
            for keyword in ESCAPE_HATCHES:
                if re.search(rf'\b{keyword}\b', line) and not _is_in_string(keyword, line):
                    stripped = line.strip()[:120]
                    introduced_findings.append(f"- **`{keyword}`** introduced in `{file_path}`: `{stripped}`")

        # Also note pre-existing escape hatches in the full file (for context, not verdict)
        full_content = file_cache.read(file_path)
        if full_content is not None:
            full_lines = full_content.splitlines(keepends=True)
            comment_depth = 0
            for i, line in enumerate(full_lines, 1):
                in_comment, comment_depth = is_in_comment(line, comment_depth)
                if in_comment:
                    continue
                for keyword in ESCAPE_HATCHES:
                    if re.search(rf'\b{keyword}\b', line) and not _is_in_string(keyword, line):
                        stripped = line.strip()[:120]
                        entry = f"- `{keyword}` in `{file_path}` line {i}: `{stripped}`"
                        if entry not in [f.replace('**', '') for f in introduced_findings]:
                            preexisting_findings.append(entry)

            # File size check
            if len(full_lines) > 1500:
                introduced_findings.append(f"- **Large file**: `{file_path}` is {len(full_lines)} lines (exceeds 1500-line lint threshold)")

    parts = []
    if introduced_findings:
        parts.append("**Escape hatches introduced in this PR (triggers hard verdict rule):**\n" + "\n".join(introduced_findings))
    if preexisting_findings:
        parts.append("**Pre-existing escape hatches in touched files (context only, does not affect verdict):**\n" + "\n".join(preexisting_findings))

    if not parts:
        return "No escape hatches or file size issues detected."

    return "\n\n".join(parts)


def get_summary_context(paths_str: str) -> str:
    """Reads type signatures and key declarations from files for summary-level context.
    Handles attributes on preceding lines, where-clauses, and inductive constructors."""
    if not paths_str:
        return ""
    summary_parts = []
    paths = [p.strip() for p in paths_str.split(',') if p.strip()]
    SIG_START = re.compile(
        r'^\s*(?:private |protected |noncomputable |partial |unsafe )*'
        r'(?:def |theorem |lemma |structure |class |instance |axiom |opaque |abbrev |inductive |variable |notation |macro |syntax )'
    )
    ATTR_LINE = re.compile(r'^\s*@\[')

    for file_path in paths:
        if not file_path.endswith('.lean'):
            continue
        content = file_cache.read(file_path)
        if content is None:
            continue
        all_lines = content.splitlines(keepends=True)
        sig_lines = []
        capturing = False
        pending_attr = None  # attribute line waiting for a declaration

        for line in all_lines:
            stripped = line.strip()

            # Standalone attribute line (e.g., @[simp])
            if ATTR_LINE.match(line) and not capturing:
                pending_attr = line.rstrip()
                continue

            if SIG_START.match(line):
                if pending_attr:
                    sig_lines.append(pending_attr)
                    pending_attr = None
                capturing = True
                sig_lines.append(line.rstrip())
            elif capturing:
                if not stripped:
                    capturing = False
                elif stripped.startswith(':= by') or stripped.startswith(':= fun') or stripped == ':= {':
                    capturing = False
                elif stripped.startswith(':=') and 'where' not in stripped:
                    capturing = False
                elif stripped == 'where':
                    sig_lines.append(line.rstrip())
                    # continue capturing structure fields
                elif stripped.startswith('|'):
                    sig_lines.append(line.rstrip())
                elif SIG_START.match(line):
                    if pending_attr:
                        sig_lines.append(pending_attr)
                        pending_attr = None
                    sig_lines.append(line.rstrip())
                elif line[0] in (' ', '\t'):
                    sig_lines.append(line.rstrip())
                else:
                    capturing = False
            else:
                pending_attr = None

        if sig_lines:
            summary_parts.append(f"--- Signatures from {file_path} ---\n" + "\n".join(sig_lines) + "\n--- End ---\n")

    return "\n".join(summary_parts)


def _call_provider(provider: LLMProvider, model: str, contents: List[ContentPart],
                   schema, thinking_budget=None, cache_name=None):
    """Wrapper: calls provider, records token usage, returns parsed object."""
    parsed, usage = provider.generate_structured(
        model=model, contents=contents, schema=schema,
        thinking_budget=thinking_budget, cache_name=cache_name,
    )
    token_tracker.record(usage)
    return parsed


def run_triage(provider: LLMProvider, diff_by_file: Dict[str, str], spec_checklist: str, additional_comments: str, model_name: str) -> List[ReviewCluster]:
    """Triage Agent: Groups changed files into review clusters based on dependencies and coupling."""
    all_diffs = "\n".join([f"--- {f} ---\n{d}" for f, d in diff_by_file.items()])

    # Use lake graph from discover step (avoids redundant subprocess call)
    dep_graph = os.environ.get('LAKE_GRAPH', '') or "Dependency graph not available."

    # Generate type signatures of changed files for semantic clustering
    changed_files_str = ','.join(f for f in diff_by_file.keys() if f.endswith('.lean'))
    changed_signatures = get_summary_context(changed_files_str)

    additional_section = ""
    if additional_comments and additional_comments.strip():
        additional_section = f"**Additional Reviewer Comments:**\n---\n{additional_comments}\n---\n"

    try:
        prompt_text = _load_prompt("triage.md", {
            "DEPENDENCY_GRAPH": dep_graph,
            "ALL_DIFFS": all_diffs,
            "SPEC_CHECKLIST": spec_checklist or "No specification checklist provided.",
            "ADDITIONAL_COMMENTS": additional_section,
            "CHANGED_FILE_SIGNATURES": changed_signatures or "No signatures extracted.",
        })
    except FileNotFoundError:
        logging.warning("triage.md not found, falling back to per-file review.")
        return [ReviewCluster(name=f, files=[f], review_question="Review this file independently.", priority="medium")
                for f in diff_by_file if f.endswith('.lean')]

    try:
        logging.info("Triage Agent is grouping files into review clusters...")
        contents = [ContentPart(type="text", data=prompt_text)]
        triage = _call_provider(provider, model_name, contents, TriageResult, thinking_budget=THINKING_BUDGET_LOW)
        logging.info(f"Triage complete: {len(triage.clusters)} clusters identified.")
        return triage.clusters
    except Exception as e:
        logging.error(f"Triage failed, falling back to per-file: {e}")
        return [ReviewCluster(name=f, files=[f], review_question="Review this file independently.", priority="medium")
                for f in diff_by_file if f.endswith('.lean')]


def analyze_specification(provider: LLMProvider, external_parts: List[ContentPart], cached_content_name: str, model_name: str, all_diffs: str, summary_context: str = "", lake_graph: str = "") -> str:
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
                                 .replace("{{FILE_DIFFS}}", all_diffs) \
                                 .replace("{{REPO_STRUCTURE}}", summary_context or "No repository structure available.") \
                                 .replace("{{DEPENDENCY_GRAPH}}", lake_graph or "Dependency graph not available.")
    
    contents = [ContentPart(type="text", data=prompt_text)]
    if not cached_content_name:
        contents.extend(external_parts)

    try:
        logging.info("Agent A (Spec Analyst) is generating the formalization checklist...")
        checklist = _call_provider(
            provider, model_name, contents, SpecChecklist,
            thinking_budget=THINKING_BUDGET_HIGH, cache_name=cached_content_name,
        )
        checklist_str = ""
        if checklist:
            # Reference mapping table
            if checklist.reference_mapping:
                checklist_str += "**Reference Mapping (Paper → Lean):**\n"
                for entry in checklist.reference_mapping:
                    status_icon = {"Present": "✅", "Missing": "❌", "Partial": "⚠️"}.get(entry.status, "?")
                    checklist_str += f"- {status_icon} **{entry.paper_result}**\n"
                    checklist_str += f"  - Mathematical content: {entry.mathematical_content}\n"
                    checklist_str += f"  - Status: {entry.status}\n"
                checklist_str += "\n"

            # Checklist items
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
        content = file_cache.read(file_path)
        if content is not None:
            all_files_content += f"--- Start of content from {file_path} ---\n{content}\n--- End of content from {file_path} ---\n\n"
        else:
            errors.append(f"Error reading file {file_path}")
    return all_files_content, errors

def split_diff_into_files(diff_content: str) -> Dict[str, str]:
    """Splits a full git diff into a dictionary of per-file diffs.
    Handles renames by using the new (b/) path as the key."""
    files = {}
    file_diffs = re.split(r'(?=^diff --git a/.+ b/.+$)', diff_content, flags=re.MULTILINE)
    for file_diff in file_diffs:
        if not file_diff.strip():
            continue
        match = re.search(r'^diff --git a/(.+) b/(.+)$', file_diff, re.MULTILINE)
        if match:
            old_path = match.group(1)
            new_path = match.group(2)
            # For renames, check the rename header
            rename_match = re.search(r'^rename to (.+)$', file_diff, re.MULTILINE)
            if rename_match:
                new_path = rename_match.group(1)
            files[new_path] = file_diff
    return files

def _format_file_review(review: FileReview, file_path: str) -> str:
    """Formats a structured FileReview into markdown."""
    parts = []

    if review.analysis:
        parts.append(f"**Analysis:**\n{review.analysis}\n")

    parts.append(f"**Verdict:** {review.verdict}\n")

    if review.checklist_results:
        parts.append("**Checklist Verification:**")
        for cr in review.checklist_results:
            icon = {"satisfied": "✅", "violated": "❌", "unclear": "⚠️"}.get(cr.status, "?")
            parts.append(f"- {icon} **{cr.item}**: {cr.explanation}")
        parts.append("")

    if review.critical_misformalizations:
        parts.append("**Critical Misformalizations:**")
        for f in review.critical_misformalizations:
            loc = f" (`{f.location}`)" if f.location else ""
            parts.append(f"- {f.description}{loc}")
            if f.suggested_fix:
                parts.append(f"  - Suggested fix: {f.suggested_fix}")
        parts.append("")
    else:
        parts.append("**Critical Misformalizations:** None\n")

    if review.lean_issues:
        parts.append("**Lean 4 / Mathlib Issues:**")
        for f in review.lean_issues:
            loc = f" (`{f.location}`)" if f.location else ""
            parts.append(f"- {f.description}{loc}")
            if f.suggested_fix:
                parts.append(f"  - Suggested fix: {f.suggested_fix}")
        parts.append("")
    else:
        parts.append("**Lean 4 / Mathlib Issues:** None\n")

    if review.nitpicks:
        parts.append("**Nitpicks:**")
        for f in review.nitpicks:
            loc = f" (`{f.location}`)" if f.location else ""
            parts.append(f"- {f.description}{loc}")
        parts.append("")
    else:
        parts.append("**Nitpicks:** None\n")

    return "\n".join(parts)


def analyze_file_changes_with_context(provider: LLMProvider, review_context: dict, file_path: str, file_diff: str, full_content: str, spec_checklist: str, external_parts: list, cached_content_name: str, lean4_checklist: str, verdict_rules: str) -> Tuple[FileReview, str]:
    """Agent B (Code Reviewer): Returns (structured FileReview, formatted markdown).
    On error, returns (None, error_message)."""

    # Select the appropriate prompt depending on if we have a checklist from Agent A
    prompt_file = "review_code_with_spec.md" if spec_checklist else "review_file.md"
    prompt_template_path = os.path.join(ACTION_PATH, "prompts", prompt_file)

    try:
        with open(prompt_template_path, "r") as f:
            prompt_template = f.read()
    except FileNotFoundError:
        return None, f"Error: Prompt template not found at {prompt_template_path}"

    additional_comments = review_context.get("additional_comments", "")
    additional_comments_section = ""
    if additional_comments and additional_comments.strip():
        additional_comments_section = f"""**Additional Reviewer Comments:**
---
{additional_comments}
---
"""

    cluster_context = review_context.get("cluster_context", "")
    cluster_section = ""
    if cluster_context:
        cluster_section = f"**Review Cluster Context (signatures of related files in this cluster):**\n---\n{cluster_context}\n---\n"

    prompt_text = prompt_template.replace("{{SPEC_CHECKLIST}}", spec_checklist) \
                            .replace("{{REPO_CONTEXT}}", review_context.get("repo_context", "")) \
                            .replace("{{FILE_PATH}}", file_path) \
                            .replace("{{FILE_DIFF}}", file_diff) \
                            .replace("{{FULL_CONTENT}}", full_content) \
                            .replace("{{ADDITIONAL_COMMENTS}}", additional_comments_section) \
                            .replace("{{CLUSTER_CONTEXT}}", cluster_section) \
                            .replace("{{LEAN4_CHECKLIST}}", lean4_checklist) \
                            .replace("{{VERDICT_RULES}}", verdict_rules)

    contents = [ContentPart(type="text", data=prompt_text)]
    if not cached_content_name and external_parts:
        contents.extend(external_parts)

    try:
        logging.info(f"Agent B is reviewing file: {file_path}...")
        review_model = review_context.get("review_model")
        review = _call_provider(
            provider, review_model, contents, FileReview,
            thinking_budget=THINKING_BUDGET_HIGH, cache_name=cached_content_name,
        )
        formatted = _format_file_review(review, file_path)
        return review, formatted
    except Exception as e:
        logging.error(f"Error during API call for {file_path}: {e}")
        return None, f"An error occurred while analyzing `{file_path}`: {e}"

def _format_cross_file(analysis: CrossFileAnalysis) -> str:
    """Formats a structured CrossFileAnalysis into markdown."""
    sections = []

    if analysis.analysis:
        sections.append(f"**Cross-File Analysis:**\n{analysis.analysis}\n")

    def _fmt_findings(title: str, findings: list[Finding]) -> str:
        if not findings:
            return f"**{title}:** None"
        lines = [f"**{title}:**"]
        for f in findings:
            loc = f" (`{f.location}`)" if f.location else ""
            lines.append(f"- {f.description}{loc}")
            if f.suggested_fix:
                lines.append(f"  - Suggested fix: {f.suggested_fix}")
        return "\n".join(lines)

    sections.append(_fmt_findings("Cross-File Composition Issues", analysis.composition_issues))
    sections.append(_fmt_findings("Axiom/Escape Hatch Impact", analysis.escape_hatch_impact))
    sections.append(_fmt_findings("External Dependency Issues", analysis.external_dependency_issues))
    sections.append(_fmt_findings("Missing Cross-File Verification", analysis.missing_cross_file_verification))
    return "\n\n".join(sections)


def analyze_cross_file(provider: LLMProvider, diff_by_file: Dict[str, str], spec_checklist: str, pre_check_findings: str, repo_context: str, additional_comments: str, external_parts: list, cached_content_name: str, model_name: str) -> Tuple[CrossFileAnalysis, str]:
    """Cross-File Analysis Agent. Returns (structured CrossFileAnalysis, formatted markdown).
    On error returns (None, error_message)."""
    prompt_template_path = os.path.join(ACTION_PATH, "prompts", "cross_file_analysis.md")

    try:
        with open(prompt_template_path, "r") as f:
            prompt_template = f.read()
    except FileNotFoundError:
        logging.warning("cross_file_analysis.md not found, skipping cross-file analysis.")
        return None, ""

    # Build full content of all changed Lean files using cache
    all_changed_contents = ""
    for file_path in diff_by_file:
        if not file_path.endswith('.lean'):
            continue
        content = file_cache.read(file_path)
        if content is not None:
            all_changed_contents += f"--- Start of {file_path} ---\n{content}\n--- End of {file_path} ---\n\n"

    all_diffs = "\n".join([f"--- {f} ---\n{d}" for f, d in diff_by_file.items()])

    additional_comments_section = ""
    if additional_comments and additional_comments.strip():
        additional_comments_section = f"**Additional Reviewer Comments:**\n---\n{additional_comments}\n---\n"

    prompt_text = prompt_template.replace("{{SPEC_CHECKLIST}}", spec_checklist or "No specification checklist provided.") \
                                 .replace("{{PRE_CHECK_FINDINGS}}", pre_check_findings) \
                                 .replace("{{ALL_DIFFS}}", all_diffs) \
                                 .replace("{{ALL_CHANGED_CONTENTS}}", all_changed_contents) \
                                 .replace("{{DEPENDENCY_CONTEXT}}", repo_context) \
                                 .replace("{{ADDITIONAL_COMMENTS}}", additional_comments_section)

    contents = [ContentPart(type="text", data=prompt_text)]
    if not cached_content_name and external_parts:
        contents.extend(external_parts)

    try:
        logging.info("Cross-File Analysis Agent is analyzing composition and dependencies...")
        analysis = _call_provider(
            provider, model_name, contents, CrossFileAnalysis,
            thinking_budget=THINKING_BUDGET_HIGH, cache_name=cached_content_name,
        )
        formatted = _format_cross_file(analysis)
        return analysis, formatted
    except Exception as e:
        logging.error(f"Error during cross-file analysis: {e}")
        return None, f"Cross-file analysis failed: {e}"


def _format_synthesis(summary: SynthesisSummary) -> str:
    """Formats a structured SynthesisSummary into markdown."""
    parts = [f"**TL;DR:** {summary.tldr}\n"]
    parts.append(f"**Mechanical Pre-Check Results:** {summary.precheck_summary}\n")

    if summary.checklist_coverage:
        parts.append(f"**Checklist Coverage:** {summary.checklist_coverage}\n")

    if summary.cross_file_summary:
        parts.append(f"**Cross-File Issues:** {summary.cross_file_summary}\n")

    if summary.critical_misformalizations:
        parts.append("**Critical Misformalizations:**")
        for f in summary.critical_misformalizations:
            loc = f" (`{f.location}`)" if f.location else ""
            parts.append(f"- {f.description}{loc}")
        parts.append("")

    if summary.key_lean_issues:
        parts.append("**Key Lean 4 / Mathlib Issues:**")
        for f in summary.key_lean_issues:
            loc = f" (`{f.location}`)" if f.location else ""
            parts.append(f"- {f.description}{loc}")
        parts.append("")

    parts.append(f"**Overall Verdict:** {summary.overall_verdict}")
    return "\n".join(parts)


def synthesize_overall_summary(provider: LLMProvider, per_file_reviews: Dict[str, str], per_file_structured: Dict[str, 'FileReview'], spec_checklist: str, pre_check_findings: str, cross_file_analysis: str, verdict_rules: str, model_name: str) -> Tuple[SynthesisSummary, str]:
    """Generates a structured high-level summary. Returns (SynthesisSummary, formatted markdown).
    On error returns (None, error_message)."""
    if not per_file_reviews:
        return None, "No files were reviewed."

    formatted_reviews = "\n\n".join(f"### Review for `{file_path}`:\n{review_text}" for file_path, review_text in per_file_reviews.items())

    # Build compact structured summary for accurate counting/deduplication
    structured_data = {}
    for file_path, review in per_file_structured.items():
        if review is None:
            continue
        structured_data[file_path] = {
            "verdict": review.verdict,
            "critical_count": len(review.critical_misformalizations),
            "issue_count": len(review.lean_issues),
            "nitpick_count": len(review.nitpicks),
            "violated_checklist": [cr.item for cr in review.checklist_results if cr.status == "violated"],
            "unclear_checklist": [cr.item for cr in review.checklist_results if cr.status == "unclear"],
        }
    structured_json = json.dumps(structured_data, indent=2)

    prompt_template_path = os.path.join(ACTION_PATH, "prompts", "synthesize_summary.md")

    try:
        with open(prompt_template_path, "r") as f:
            prompt_template = f.read()
    except FileNotFoundError:
        return None, f"Error: Prompt template not found at {prompt_template_path}"

    prompt = prompt_template.replace("{{PER_FILE_REVIEWS}}", formatted_reviews) \
                           .replace("{{STRUCTURED_REVIEWS}}", structured_json) \
                           .replace("{{SPEC_CHECKLIST}}", spec_checklist or "No explicit checklist provided.") \
                           .replace("{{PRE_CHECK_FINDINGS}}", pre_check_findings or "No issues detected.") \
                           .replace("{{CROSS_FILE_ANALYSIS}}", cross_file_analysis or "No cross-file analysis performed.") \
                           .replace("{{VERDICT_RULES}}", verdict_rules)

    try:
        logging.info("Synthesizing overall summary...")
        contents = [ContentPart(type="text", data=prompt)]
        summary = _call_provider(provider, model_name, contents, SynthesisSummary, thinking_budget=THINKING_BUDGET_LOW)
        formatted = _format_synthesis(summary)
        return summary, formatted
    except Exception as e:
        logging.error(f"Error during summary synthesis: {e}")
        return None, f"An error occurred while synthesizing the summary: {e}"

def _get_diff_lines(diff_text: str) -> set:
    """Returns the set of line numbers (in the new file) that appear in the diff.
    Used for mapping findings to GitHub Review API line annotations."""
    diff_lines = set()
    current_line = 0
    in_hunk = False

    for line in diff_text.splitlines():
        if line.startswith('@@'):
            m = re.search(r'\+(\d+)', line)
            if m:
                current_line = int(m.group(1)) - 1
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if line.startswith('+'):
            current_line += 1
            diff_lines.add(current_line)
        elif line.startswith('-'):
            pass  # deleted line, don't advance new-file counter
        else:
            current_line += 1
            diff_lines.add(current_line)  # context line

    return diff_lines


def _build_line_annotations(per_file_structured: Dict[str, FileReview], diff_by_file: Dict[str, str]) -> List[Dict]:
    """Builds GitHub Review API comment annotations from structured reviews.
    Returns a list of {path, line, side, body} dicts using the modern API."""
    annotations = []

    for file_path, review in per_file_structured.items():
        if review is None:
            continue

        diff = diff_by_file.get(file_path, "")
        diff_lines = _get_diff_lines(diff)

        all_findings = []
        for f in review.critical_misformalizations:
            all_findings.append(("🔴 Critical", f))
        for f in review.lean_issues:
            all_findings.append(("🟡 Issue", f))
        for f in review.nitpicks:
            all_findings.append(("💡 Nitpick", f))

        for severity, finding in all_findings:
            if not finding.location:
                continue

            m = re.search(r':(\d+)', finding.location)
            if not m:
                continue

            line_num = int(m.group(1))

            # Check if line is in diff, or try nearby lines
            target_line = None
            if line_num in diff_lines:
                target_line = line_num
            else:
                for offset in range(1, 6):
                    if line_num + offset in diff_lines:
                        target_line = line_num + offset
                        break
                    if line_num - offset in diff_lines:
                        target_line = line_num - offset
                        break

            if target_line is None:
                continue  # line not in diff, can't annotate

            body = f"**{severity}:** {finding.description}"
            if finding.suggested_fix:
                body += f"\n\n**Suggested fix:** {finding.suggested_fix}"

            annotations.append({
                "path": file_path,
                "line": target_line,
                "side": "RIGHT",
                "body": body
            })

    return annotations


def main():
    parser = argparse.ArgumentParser(description="AI Code Reviewer")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--external-refs", default="")
    parser.add_argument("--repo-context-refs", default="")
    parser.add_argument("--additional-comments", default="")
    parser.add_argument("--provider", default="gemini", help="LLM provider: gemini, anthropic, or openai")
    parser.add_argument("--model", default="", help="Default model for all agents")
    parser.add_argument("--spec-model", default="", help="Model for Agent A (spec analysis). Falls back to --model")
    parser.add_argument("--triage-model", default="", help="Model for Triage agent. Falls back to --model")
    parser.add_argument("--review-model", default="", help="Model for Agent B (per-file review). Falls back to --model")
    parser.add_argument("--cross-file-model", default="", help="Model for cross-file analysis. Falls back to --model")
    parser.add_argument("--synthesis-model", default="", help="Model for synthesis. Falls back to --model")
    parser.add_argument("--thinking-budget", type=int, default=10240, help="Thinking token budget for deep-analysis agents. Triage and Synthesis use 1/5 of this.")
    args = parser.parse_args()

    # Resolve per-agent models (fall back to default)
    if not args.model:
        args.model = os.environ.get("MODEL", "")
    args.spec_model = args.spec_model or args.model
    args.triage_model = args.triage_model or args.model
    args.review_model = args.review_model or args.model
    args.cross_file_model = args.cross_file_model or args.model
    args.synthesis_model = args.synthesis_model or args.model

    # Configure thinking budgets
    global THINKING_BUDGET_HIGH, THINKING_BUDGET_LOW
    THINKING_BUDGET_HIGH = args.thinking_budget
    THINKING_BUDGET_LOW = max(1024, args.thinking_budget // 5)

    diff, diff_errors = get_pr_diff(args.pr_number)
    if diff_errors and not diff:
        logging.error("Aborting review: Could not fetch PR diff. Errors:\n" + "\n".join(diff_errors))
        sys.exit(1)

    diff_by_file = split_diff_into_files(diff)
    lean_files = {f: d for f, d in diff_by_file.items() if f.endswith('.lean')}
    if not lean_files:
        print("### 🤖 AI Review\n\nNo Lean files were changed in this PR.")
        return

    context_warnings = []

    external_parts, external_errors = get_document_content(args.external_refs)
    repo_context, repo_errors = get_repo_files_content(args.repo_context_refs)
    summary_context = get_summary_context(os.environ.get("SUMMARY_FILES", ""))

    # Append summary-level context (type signatures only) for overflow files
    if summary_context:
        repo_context += f"\n\n--- Summary Context (type signatures only, from overflow files) ---\n{summary_context}\n"

    # Append Lean toolchain info (axiom dependencies, sorry locations, diagnostics, etc.)
    lean_info = os.environ.get("LEAN_INFO", "")
    if lean_info:
        repo_context += f"\n\n{lean_info}\n"
    elif os.environ.get("DISCOVERED_FILES", ""):
        context_warnings.append(
            "Lean toolchain analysis produced no output despite changed Lean files being present. "
            "Axiom dependencies and compiler diagnostics are unavailable for this review."
        )

    # Append build output (warnings/errors captured from lake build)
    build_output = os.environ.get("BUILD_OUTPUT", "")
    if build_output and build_output.strip() and "no warnings" not in build_output.lower():
        repo_context += f"\n\n**Lake Build Diagnostics (compiler output):**\n{build_output}\n"

    all_errors = external_errors + repo_errors
    if all_errors:
        logging.warning("Encountered non-critical errors. Review will proceed with partial context.")

    # Try generic API_KEY first, then provider-specific fallbacks
    api_key = os.getenv("API_KEY")
    if not api_key:
        provider_env_keys = {
            "gemini": "GEMINI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        fallback_key = provider_env_keys.get(args.provider.lower(), "")
        if fallback_key:
            api_key = os.getenv(fallback_key)
    if not api_key:
        logging.error(f"Error: API_KEY not set. Set API_KEY or the provider-specific key for '{args.provider}'.")
        sys.exit(1)

    provider = create_provider(args.provider, api_key)
    logging.info(f"Using LLM provider: {provider.name}")

    cached_content_name = None
    if external_parts:
        try:
            logging.info("Attempting to create context cache for external references...")
            cached_content_name = provider.create_cache(args.model, external_parts)
            if cached_content_name:
                logging.info(f"Successfully created context cache: {cached_content_name}")
            else:
                logging.info("Provider does not support content caching. Using inline references.")
        except Exception as e:
            logging.warning(f"Could not create context cache (falling back to inline): {e}")
            context_warnings.append(
                "External reference caching failed. References will be inlined per API call (higher cost)."
            )

    try:
        review_context = {
            "external_context": "[Multimodal Content Provided]",
            "repo_context": repo_context,
            "additional_comments": args.additional_comments,
            "review_model": args.review_model,
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

        # --- Multi-Agent Orchestration Step 0: Mechanical Pre-Checks ---
        pre_check_findings = run_mechanical_prechecks(diff_by_file)
        has_findings = "No escape hatches" not in pre_check_findings
        logging.info(f"Pre-check complete: {'findings detected' if has_findings else 'clean'}.")

        # --- Multi-Agent Orchestration Step 1: Spec Analysis ---
        spec_checklist = analyze_specification(
            provider, external_parts, cached_content_name, args.spec_model, all_diffs,
            summary_context=summary_context, lake_graph=os.environ.get('LAKE_GRAPH', '')
        )
        if spec_checklist:
            logging.info("Spec Analysis complete. Handing off checklist to Code Reviewers.")
        else:
            logging.info("No external specification provided or analysis failed. Proceeding with standard review.")

        # --- Multi-Agent Orchestration Step 1.5: Triage ---
        if len(lean_files) > 2:
            clusters = run_triage(provider, lean_files, spec_checklist, args.additional_comments, args.triage_model)
        elif len(lean_files) == 2:
            # Two files: single cluster without the overhead of triage
            files = list(lean_files.keys())
            clusters = [ReviewCluster(name="Changed files", files=files,
                                      review_question="Check type/interface consistency between these files.",
                                      priority="high")]
        else:
            clusters = [ReviewCluster(name=f, files=[f], review_question="", priority="medium")
                        for f in lean_files]

        # Ensure all Lean files are covered (triage might miss some)
        clustered_files = set()
        for c in clusters:
            clustered_files.update(c.files)
        unclustered = [f for f in lean_files if f not in clustered_files]
        if unclustered:
            clusters.append(ReviewCluster(name="Unclustered files", files=unclustered,
                                          review_question="Review these files independently.", priority="low"))

        # --- Multi-Agent Orchestration Step 2: Review per cluster ---
        def process_file(file_path, file_diff, review_ctx, cluster_context=""):
            """Reviews a single file, optionally with cluster context."""
            if not file_path.endswith(".lean"):
                logging.info(f"Skipping non-Lean file: {file_path}")
                return None, None, None

            full_content = file_cache.read(file_path) or ""

            augmented_ctx = dict(review_ctx)
            if cluster_context:
                augmented_ctx["cluster_context"] = cluster_context

            structured_review, formatted_text = analyze_file_changes_with_context(
                provider, augmented_ctx, file_path, file_diff, full_content,
                spec_checklist, external_parts, cached_content_name, lean4_checklist, verdict_rules
            )
            return file_path, structured_review, formatted_text

        per_file_reviews = {}      # file_path -> formatted markdown
        per_file_structured = {}   # file_path -> FileReview (or None on error)
        review_errors = []
        cluster_info = {}          # file_path -> cluster name

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = []
            for cluster in clusters:
                # Build cluster context for multi-file clusters
                cluster_context = ""
                if len(cluster.files) > 1 and cluster.review_question:
                    # Build signatures of other cluster files for type-level awareness
                    cluster_file_paths = ','.join(
                        cf for cf in cluster.files
                        if cf in diff_by_file and cf.endswith('.lean')
                    )
                    cluster_sigs = get_summary_context(cluster_file_paths)
                    cluster_parts = [
                        f"**Cluster: {cluster.name}** (Priority: {cluster.priority})",
                        f"**Cross-file question:** {cluster.review_question}",
                    ]
                    if cluster.review_strategy:
                        cluster_parts.append(f"**Review strategy:** {cluster.review_strategy}")
                    if cluster.key_hypotheses:
                        cluster_parts.append("**Key hypotheses to verify:**")
                        for hyp in cluster.key_hypotheses:
                            cluster_parts.append(f"- {hyp}")
                    cluster_parts.append(f"**Type signatures of other files in this cluster:**\n{cluster_sigs}")
                    cluster_context = "\n".join(cluster_parts)

                for file_path in cluster.files:
                    if file_path in diff_by_file:
                        cluster_info[file_path] = cluster.name
                        futures.append(executor.submit(
                            process_file, file_path, diff_by_file[file_path],
                            review_context, cluster_context
                        ))

            for future in futures:
                file_path, structured, formatted = future.result()
                if file_path:
                    per_file_reviews[file_path] = formatted
                    per_file_structured[file_path] = structured
                    if structured is None:
                        review_errors.append(f"Agent B failed for `{file_path}`")

        # --- Multi-Agent Orchestration Step 3: Cross-File Analysis ---
        cross_file_text = ""
        cross_file_structured = None
        if len(lean_files) > 1:
            cross_file_structured, cross_file_text = analyze_cross_file(
                provider, diff_by_file, spec_checklist, pre_check_findings,
                repo_context, args.additional_comments, external_parts,
                cached_content_name, args.cross_file_model
            )
            logging.info("Cross-file analysis complete.")
        else:
            logging.info("Single file PR — skipping cross-file analysis.")
            # Deterministic downstream impact note from lake graph
            lake_graph_str = os.environ.get('LAKE_GRAPH', '')
            if lake_graph_str:
                try:
                    lake_graph_data = json.loads(lake_graph_str)
                    single_file = list(lean_files.keys())[0]
                    single_module = file_path_to_module_name(single_file)
                    dependents = [m['name'] for m in lake_graph_data
                                  if single_module in m.get('imports', []) and m['name'] != single_module]
                    if dependents:
                        dep_list = ', '.join(f'`{d}`' for d in dependents[:10])
                        suffix = f' and {len(dependents) - 10} more' if len(dependents) > 10 else ''
                        cross_file_text = (
                            f"**Downstream Impact Note:** This file is imported by "
                            f"{len(dependents)} module(s): {dep_list}{suffix}. "
                            f"Changes to public API may affect these downstream consumers."
                        )
                except (json.JSONDecodeError, Exception) as e:
                    logging.warning(f"Could not generate downstream impact note: {e}")

        # --- Multi-Agent Orchestration Step 4: Synthesis ---
        if len(lean_files) == 1 and not cross_file_text:
            # Single-file PR: the per-file review IS the summary — skip synthesis agent
            logging.info("Single-file PR — skipping synthesis (per-file review is the summary).")
            summary_text = ""
            only_file = list(per_file_reviews.keys())[0] if per_file_reviews else None
            if only_file and per_file_structured.get(only_file):
                review = per_file_structured[only_file]
                summary_text = f"**Verdict:** {review.verdict}\n"
                if pre_check_findings and "No escape hatches" not in pre_check_findings:
                    summary_text += f"\n**Pre-Check:** Escape hatches detected (see details below).\n"
        else:
            summary_structured, summary_text = synthesize_overall_summary(
                provider, per_file_reviews, per_file_structured, spec_checklist,
                pre_check_findings, cross_file_text, verdict_rules, args.synthesis_model
            )

        # Format the final comment for printing to stdout
        final_comment = f"### 🤖 AI Review\n\n**Overall Summary:**\n{summary_text}\n\n---\n"

        if review_errors:
            final_comment += "\n**Errors during review:**\n"
            for err in review_errors:
                final_comment += f"- {err}\n"
            final_comment += "\n---\n"

        all_warnings = all_errors + context_warnings
        if all_warnings:
            final_comment += "\n<details><summary>**Context Warnings**</summary>\n\n"
            final_comment += "The following issues occurred while gathering context. The review proceeded with partial information:\n\n"
            for w in all_warnings:
                final_comment += f"- {w}\n"
            final_comment += "\n</details>\n"

        if pre_check_findings and "No escape hatches" not in pre_check_findings:
            final_comment += f"\n<details><summary>🔍 **Mechanical Pre-Check Results**</summary>\n\n{pre_check_findings}\n</details>\n"

        if cross_file_text:
            final_comment += f"\n<details><summary>🔗 **Cross-File Analysis**</summary>\n\n{cross_file_text}\n</details>\n"

        # Group per-file reviews by cluster
        shown_files = set()
        for cluster in clusters:
            cluster_files = [f for f in cluster.files if f in per_file_reviews]
            if not cluster_files:
                continue
            if len(cluster.files) > 1:
                final_comment += f"\n#### Cluster: {cluster.name} ({cluster.priority})\n"
                if cluster.review_question:
                    final_comment += f"*{cluster.review_question}*\n"
            for file_path in cluster_files:
                final_comment += f"\n<details><summary>📄 **Review for `{file_path}`**</summary>\n\n{per_file_reviews[file_path]}\n</details>\n"
                shown_files.add(file_path)

        # Show any files not in clusters (non-Lean files that were reviewed)
        for file_path, review_text in per_file_reviews.items():
            if file_path not in shown_files:
                final_comment += f"\n<details><summary>📄 **Review for `{file_path}`**</summary>\n\n{review_text}\n</details>\n"

        # GitHub PR comments have a ~65536 char limit
        MAX_COMMENT_SIZE = 65000
        if len(final_comment) > MAX_COMMENT_SIZE:
            truncation_msg = "\n\n---\n**Note:** This review was truncated due to GitHub's comment size limit. See the full review in the Actions log.\n"
            final_comment = final_comment[:MAX_COMMENT_SIZE - len(truncation_msg)] + truncation_msg

        print(final_comment)

        # Generate line-level annotations for GitHub Review API
        annotations = _build_line_annotations(per_file_structured, diff_by_file)
        if annotations:
            try:
                with open('review_annotations.json', 'w') as f:
                    json.dump(annotations, f, indent=2)
                logging.info(f"Wrote {len(annotations)} line annotations to review_annotations.json")
            except Exception as e:
                logging.warning(f"Failed to write annotations: {e}")
    finally:
        logging.info(token_tracker.summary())
        if cached_content_name:
            try:
                provider.delete_cache(cached_content_name)
            except Exception as e:
                logging.warning(f"Failed to delete context cache: {e}")

if __name__ == "__main__":
    main()
