import os
import sys
from pathlib import Path
from io import StringIO

import click
import pathspec
import pyperclip

LARGE_CONTENT_THRESHOLD_PERCENT = 35

# --- Output Formatting & Tree Generation ---

def generate_tree_output(file_paths, project_root):
    """Generates a string representing the directory tree for the given files."""
    tree = {}
    for path in file_paths:
        relative_path = path.relative_to(project_root)
        parts = list(relative_path.parts)
        current_level = tree
        for part in parts:
            current_level = current_level.setdefault(part, {})

    def build_tree_string(d, prefix=""):
        s = ""
        items = sorted(d.keys())
        for i, key in enumerate(items):
            connector = "└── " if i == len(items) - 1 else "├── "
            s += f"{prefix}{connector}{key}\n"
            if d[key]:
                extension = "    " if i == len(items) - 1 else "│   "
                s += build_tree_string(d[key], prefix + extension)
        return s

    return f"{project_root.name}/\n{build_tree_string(tree)}"


def add_line_numbers(content):
    """Prepends line numbers to a string of content."""
    lines = content.splitlines()
    max_digits = len(str(len(lines)))
    return "\n".join(f"{str(i+1).rjust(max_digits)} | {line}" for i, line in enumerate(lines))

def print_default(writer, path, content):
    content_with_lines = add_line_numbers(content)
    writer(f"{path}\n---\n{content_with_lines}\n---\n")

def print_as_xml(writer, path, content, index):
    content_with_lines = add_line_numbers(content)
    writer(f'<document index="{index}">\n<source>{path}</source>\n<document_content>')
    content_with_lines = content_with_lines.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    writer(content_with_lines)
    writer("</document_content>\n</document>")

def print_as_markdown(writer, path, content):
    content_with_lines = add_line_numbers(content)
    lang = path.suffix.lstrip('.')
    backticks = "```"
    while backticks in content:
        backticks += "`"
    writer(f"{path}\n{backticks}{lang}\n{content_with_lines}\n{backticks}\n")


# --- File Collection & Analysis ---

def get_gitignore_spec(path):
    specs = []
    current_path = path.resolve()
    while True:
        gitignore_path = current_path / ".gitignore"
        if gitignore_path.is_file():
            with open(gitignore_path, "r") as f:
                spec = pathspec.PathSpec.from_lines('gitwildmatch', f)
                specs.append((spec, current_path))
        parent = current_path.parent
        if parent == current_path: break
        current_path = parent
    return specs

def is_ignored(path, gitignore_specs):
    path_abs = path.resolve()
    for spec, root in gitignore_specs:
        try:
            if spec.match_file(path_abs.relative_to(root)): return True
        except ValueError: continue
    return False

def collect_files(paths, include, exclude, no_gitignore):
    all_files = set()
    gitignore_specs = []
    if not no_gitignore:
        unique_paths = {Path(p).resolve() for p in paths}
        search_roots = {p if p.is_dir() else p.parent for p in unique_paths}
        for root in search_roots:
            gitignore_specs.extend(get_gitignore_spec(root))

    for start_path_str in paths:
        start_path = Path(start_path_str).resolve()
        if start_path.is_file():
            if not is_ignored(start_path, gitignore_specs): all_files.add(start_path)
            continue
        for root, dirs, files in os.walk(start_path, topdown=True):
            root_path = Path(root)
            if not no_gitignore:
                dirs[:] = [d for d in dirs if not is_ignored(root_path / d, gitignore_specs)]
                files = [f for f in files if not is_ignored(root_path / f, gitignore_specs)]
            for file_name in files: all_files.add(root_path / file_name)

    exclude_spec = pathspec.PathSpec.from_lines('gitwildmatch', exclude)
    filtered_files = [p for p in all_files if not exclude_spec.match_file(str(p.relative_to(Path.cwd())))]

    if include:
        include_spec = pathspec.PathSpec.from_lines('gitwildmatch', include)
        filtered_files = [p for p in filtered_files if include_spec.match_file(str(p.relative_to(Path.cwd())))]

    return sorted(filtered_files)

def analyze_content_sizes(file_contents, initial_paths):
    """Identifies files or directories contributing a large percentage of total content."""
    total_size = sum(len(c) for c in file_contents.values())
    if total_size == 0:
        return []

    large_items = []
    # Check individual files
    for path, content in file_contents.items():
        size = len(content)
        if (size / total_size * 100) > LARGE_CONTENT_THRESHOLD_PERCENT:
            large_items.append((path.relative_to(Path.cwd()), size / total_size * 100))

    # Check top-level directories provided by the user
    initial_paths_resolved = [Path(p).resolve() for p in initial_paths if Path(p).is_dir()]
    for dir_path in initial_paths_resolved:
        dir_size = sum(len(c) for p, c in file_contents.items() if dir_path in p.parents)
        if (dir_size / total_size * 100) > LARGE_CONTENT_THRESHOLD_PERCENT:
            large_items.append((dir_path.relative_to(Path.cwd()), dir_size / total_size * 100))

    # Return unique items
    return sorted(list(set(item[0] for item in large_items)))

# --- Main CLI ---

@click.command()
@click.argument("paths", nargs=-1, type=click.Path())
@click.option("-i", "--include", multiple=True, help="Glob pattern for files to include.")
@click.option("-x", "--exclude", multiple=True, help="Glob pattern for files/directories to exclude.")
@click.option("--no-gitignore", is_flag=True, help="Disable parsing of .gitignore files.")
@click.option("-o", "--output", type=click.File('w', encoding='utf-8'), help="Output to a file instead of stdout.")
@click.option("-c", "--cxml", is_flag=True, help="Output in XML-ish format suitable for Claude.")
@click.option("-m", "--markdown", is_flag=True, help="Output Markdown with fenced code blocks.")
@click.option("-C", "--copy", is_flag=True, help="Copy the final output to the clipboard.")
@click.version_option()
def cli(paths, include, exclude, no_gitignore, output, cxml, markdown, copy):
    """
    Outputs the contents of files from specified paths, with filtering and a project tree.
    """
    if not paths and sys.stdin.isatty():
        raise click.UsageError("No paths provided. Provide paths as arguments or pipe from stdin.")
    if not paths:
        paths = [line.strip() for line in sys.stdin if line.strip()]

    current_exclude = list(exclude)
    
    while True:
        files_to_process = collect_files(paths, include, current_exclude, no_gitignore)
        if not files_to_process:
            click.echo("No files found matching the criteria.", err=True)
            return

        file_contents = {}
        for file_path in files_to_process:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    file_contents[file_path] = f.read()
            except IOError as e:
                click.echo(f"Warning: Skipping file {file_path} due to error: {e}", err=True)
        
        large_items = analyze_content_sizes(file_contents, paths)
        
        if large_items:
            click.echo("Warning: The following items contribute a large portion of the total output:", err=True)
            for item in large_items:
                click.echo(f"  - {item}", err=True)
            
            if click.confirm("Do you want to automatically exclude them and regenerate the output?", err=True):
                current_exclude.extend(f"{str(item)}/**" if Path(item).is_dir() else str(item) for item in large_items)
                click.echo("Regenerating output with new exclusions...", err=True)
                continue # Restart the loop
            else:
                click.echo("Proceeding with the original file selection.", err=True)
                break # Exit loop and proceed with large files
        else:
            break # No large items, exit loop and proceed

    string_buffer = StringIO()
    writer = string_buffer.write

    # 1. Add the project tree
    project_root = Path.cwd() # Or determine a more specific root if desired
    tree_str = generate_tree_output(files_to_process, project_root)
    writer(f"Project Structure:\n```\n{tree_str}\n```\n\n")

    # 2. Add file contents
    if cxml: writer("<documents>\n")
    for idx, (file_path, content) in enumerate(file_contents.items()):
        relative_path = file_path.relative_to(project_root)
        if cxml: print_as_xml(writer, relative_path, content, idx + 1)
        elif markdown: print_as_markdown(writer, relative_path, content)
        else: print_default(writer, relative_path, content)
        writer("\n")
    if cxml: writer("</documents>")
        
    final_output = string_buffer.getvalue()

    if copy:
        pyperclip.copy(final_output)
        click.echo("Output copied to clipboard.", err=True)
    if output:
        output.write(final_output)
    if not output and not copy:
        click.echo(final_output)

if __name__ == "__main__":
    cli()