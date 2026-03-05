import subprocess
import json
import os
import sys

def get_changed_lean_files(pr_number):
    try:
        command = f"gh pr diff {pr_number} --name-only"
        result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        changed_files = [f.strip() for f in result.stdout.splitlines() if f.strip().endswith('.lean')]
        return changed_files
    except subprocess.CalledProcessError as e:
        print(f"::error::Failed to get changed files: {e.stderr}")
        return []

def get_lean_module_name(file_path):
    # Assumes a standard Lean project structure, adjust if necessary
    # e.g., src/My/Module.lean -> My.Module
    if file_path.startswith("src/"):
        file_path = file_path[4:]
    elif file_path.startswith("Mathlib/"): # Common in mathlib projects
        file_path = file_path[8:]
    return file_path.replace('/', '.').replace('.lean', '')

def get_dependent_lean_files(changed_modules, lake_graph_json):
    dependent_modules = set()
    for module_info in lake_graph_json:
        module_name = module_info['name']
        if any(imp in changed_modules for imp in module_info.get('imports', [])) and module_name not in changed_modules:
            dependent_modules.add(module_name)
    return list(dependent_modules)

def get_dependency_lean_files(changed_modules, lake_graph_json):
    dependency_modules = set()
    for module_info in lake_graph_json:
        if module_info['name'] in changed_modules:
            dependency_modules.update(module_info.get('imports', []))
    # We don't want to include dependencies that were also changed in the PR
    return list(dependency_modules - changed_modules)

def build_lean_file_index():
    index = []
    for root, dirs, files in os.walk('.'):
        if '.git' in root or '__pycache__' in root:
            continue
        for f in files:
            if f.endswith('.lean'):
                path = os.path.normpath(os.path.join(root, f))
                if path.startswith(f".{os.sep}"):
                    path = path[2:]
                index.append(path)
    return index

def convert_module_to_file_path(module_name, index):
    expected_suffix = module_name.replace('.', os.sep) + '.lean'
    for path in index:
        if path.endswith(expected_suffix) or path == expected_suffix:
            return path
    return module_name.replace('.', os.sep) + ".lean"

def main():
    pr_number = os.environ.get('PR_NUMBER')
    if not pr_number:
         print("::error::PR_NUMBER environment variable is required.")
         sys.exit(1)

    changed_files = get_changed_lean_files(pr_number)
    changed_modules = {get_lean_module_name(f) for f in changed_files}

    all_relevant_files = set(changed_files)

    try:
        print("Attempting to generate Lake dependency graph...")
        lake_graph_output = subprocess.run(
            ['lake', 'exe', 'graph', '--json'],
            check=True,
            capture_output=True,
            text=True,
            timeout=300
        ).stdout
        lake_graph_json = json.loads(lake_graph_output)
        print("Successfully generated Lake dependency graph.")

        lean_files_index = build_lean_file_index()

        # Find files that depend ON our changed files
        dependent_modules = get_dependent_lean_files(changed_modules, lake_graph_json)
        dependent_files = {convert_module_to_file_path(m, lean_files_index) for m in dependent_modules}
        all_relevant_files.update(dependent_files)

        # Find files that our changed files depend ON
        dependency_modules = get_dependency_lean_files(changed_modules, lake_graph_json)
        dependency_files = {convert_module_to_file_path(m, lean_files_index) for m in dependency_modules}
        all_relevant_files.update(dependency_files)

    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"::warning::Could not generate or parse Lake graph for full dependency analysis: {e}")
        print("::warning::Falling back to only changed files for context.")

    final_file_list = sorted([f for f in all_relevant_files if os.path.exists(f)])
    
    # Limit the number of context files to 15 to avoid token bloat
    CONTEXT_LIMIT = 15
    if len(final_file_list) > CONTEXT_LIMIT:
        print(f"::warning::Discovered {len(final_file_list)} files, capping context to {CONTEXT_LIMIT} most relevant.")
        # Prioritize changed files, then dependencies
        changed_first = [f for f in final_file_list if f in changed_files]
        others = [f for f in final_file_list if f not in changed_files]
        final_file_list = (changed_first + others)[:CONTEXT_LIMIT]

    output_string = ','.join(final_file_list)

    print(f"::notice::Discovered files for review: {output_string}")
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(f"discovered_files={output_string}\n")

if __name__ == "__main__":
    main()
