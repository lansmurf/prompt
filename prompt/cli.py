import os
import sys
from pathlib import Path
from io import StringIO
from collections import defaultdict

import click
import pathspec
import pyperclip

LARGE_CONTENT_THRESHOLD_PERCENT = 35
DEFAULT_OUTPUT_FILENAME = "out.txt"

# Expanded list of binary/non-parseable extensions as fallback
DEFAULT_BINARY_EXTENSIONS = {
    # Images
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.tif', '.tiff', '.webp', 
    '.svg', '.psd', '.ai', '.eps', '.raw', '.cr2', '.nef', '.heic', '.avif',
    # Audio/Video
    '.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.wma', '.mp4', '.mov', 
    '.avi', '.mkv', '.flv', '.wmv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp',
    # Archives
    '.zip', '.tar', '.gz', '.rar', '.7z', '.bz2', '.xz', '.tgz', '.jar', '.war',
    '.ear', '.dmg', '.iso', '.apk', '.deb', '.rpm',
    # Documents (binary formats)
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods',
    '.odp', '.pages', '.numbers', '.key',
    # Compiled code / binaries
    '.pyc', '.pyo', '.so', '.dll', '.exe', '.o', '.a', '.lib', '.dylib', 
    '.class', '.bin', '.dat', '.out',
    # Databases
    '.db', '.sqlite', '.sqlite3', '.mdb', '.accdb',
    # Fonts
    '.ttf', '.otf', '.woff', '.woff2', '.eot',
    # 3D/CAD
    '.blend', '.fbx', '.obj', '.stl', '.3ds', '.dae', '.dwg', '.dxf',
    # Other
    '.DS_Store', '.lock', '.pack', '.idx', '.sample',
}


# --- Binary Detection ---

def is_binary_file(file_path, sample_size=8192):
    """
    Detect if a file is binary by checking for null bytes and non-text characters.
    More reliable than extension-based detection.
    """
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(sample_size)
            if not chunk:
                return False
            
            # Check for null bytes (strong indicator of binary)
            if b'\x00' in chunk:
                return True
            
            # Count non-text bytes
            # Text files should have mostly printable ASCII, newlines, tabs
            non_text_chars = 0
            for byte in chunk:
                # Allow printable ASCII (32-126), plus newline (10), carriage return (13), tab (9)
                if not (9 <= byte <= 13 or 32 <= byte <= 126 or byte >= 128):
                    non_text_chars += 1
            
            # If more than 30% of bytes are non-text, consider it binary
            if len(chunk) > 0 and (non_text_chars / len(chunk)) > 0.3:
                return True
            
            return False
    except Exception:
        # If we can't read it, assume it's binary to be safe
        return True


# --- Output Formatting & Tree Generation ---

def generate_tree_output(file_paths, project_root):
    tree = {}
    for path in file_paths:
        try:
            relative_path = path.relative_to(project_root)
            parts = list(relative_path.parts)
            current_level = tree
            for part in parts:
                current_level = current_level.setdefault(part, {})
        except ValueError:
            tree[str(path)] = {}

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
    
    try:
        root_display_name = project_root.relative_to(Path.cwd())
        if str(root_display_name) == '.':
             root_display_name = './'
        else:
            root_display_name = f'./{root_display_name}/'
    except ValueError:
        root_display_name = f'{project_root.name}/'

    return f"{root_display_name}\n{build_tree_string(tree)}"

def add_line_numbers(content):
    lines = content.splitlines()
    max_digits = len(str(len(lines)))
    return "\n".join(f"{str(i+1).rjust(max_digits)} | {line}" for i, line in enumerate(lines))

def print_default(writer, path, content):
    content_with_lines = add_line_numbers(content)
    writer(f"\n{path}\n---\n{content_with_lines}\n---\n")

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
    writer(f"\n{path}\n{backticks}{lang}\n{content_with_lines}\n{backticks}\n")

# --- File Collection & Analysis ---

def load_gitignore_spec(gitignore_path):
    """Load a single .gitignore file and return a PathSpec."""
    try:
        with open(gitignore_path, "r", encoding='utf-8', errors='ignore') as f:
            return pathspec.PathSpec.from_lines('gitwildmatch', f)
    except Exception:
        return None

def is_ignored(path, gitignore_cache, search_root, exclude_spec, include_spec):
    """
    Check if a path should be ignored based on:
    1. Explicit exclude patterns
    2. .gitignore files (searched downward from search_root)
    3. Include patterns (if specified)
    """
    path_abs = path.resolve()
    
    # First check explicit exclude patterns
    try:
        relative_to_root = path_abs.relative_to(search_root)
        if exclude_spec.match_file(str(relative_to_root)):
            return True
    except ValueError:
        # Path is outside search_root, check against absolute path
        if exclude_spec.match_file(str(path_abs)):
            return True
    
    # Check gitignore files from root down to this path
    # We need to check each directory level from search_root to the file
    try:
        relative_path = path_abs.relative_to(search_root)
        
        # Check gitignore at the search root level
        if search_root in gitignore_cache:
            spec = gitignore_cache[search_root]
            if spec and spec.match_file(str(relative_path)):
                return True
        
        # Check gitignore in each parent directory down to the file
        for parent in relative_path.parents:
            if parent == Path('.'):
                continue
            parent_abs = search_root / parent
            if parent_abs in gitignore_cache:
                spec = gitignore_cache[parent_abs]
                if spec:
                    # Make path relative to this gitignore's location
                    rel_to_parent = relative_path.relative_to(parent)
                    if spec.match_file(str(rel_to_parent)):
                        return True
    except ValueError:
        pass
    
    # Finally check include patterns if specified
    if include_spec:
        try:
            relative_to_root = path_abs.relative_to(search_root)
            if not include_spec.match_file(str(relative_to_root)):
                return True
        except ValueError:
            if not include_spec.match_file(str(path_abs)):
                return True
        
    return False

def should_include_file(file_path, no_binary_filter, use_extension_filter):
    """
    Determine if a file should be included based on binary detection and extension filtering.
    """
    if no_binary_filter:
        return True
    
    # Always check extension list first (catches SVGs, lockfiles, etc.)
    if file_path.suffix.lower() in DEFAULT_BINARY_EXTENSIONS:
        return False
    
    # Also check common lockfile patterns by name
    file_name_lower = file_path.name.lower()
    lockfile_patterns = [
        'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock',
        'pipfile.lock', 'gemfile.lock', 'composer.lock', 'cargo.lock',
        'go.sum', 'pom.xml.asc'
    ]
    if file_name_lower in lockfile_patterns:
        return False
    
    # If using extension filter only, we're done
    if use_extension_filter:
        return True
    
    # Otherwise, also do content-based binary detection
    return not is_binary_file(file_path)


def collect_files(paths, include, exclude, no_gitignore, no_binary_filter, use_extension_filter):
    """
    Collect all files from the specified paths, respecting gitignore and exclude/include patterns.
    """
    all_files = set()
    
    # Prepare exclude patterns
    exclude_spec = pathspec.PathSpec.from_lines('gitwildmatch', exclude)
    include_spec = pathspec.PathSpec.from_lines('gitwildmatch', include) if include else None

    for start_path_str in paths:
        start_path = Path(start_path_str).resolve()
        
        # Cache for gitignore specs: maps directory path -> PathSpec
        gitignore_cache = {}
        
        # If not ignoring gitignore files, load the root one
        if not no_gitignore:
            root_gitignore = start_path / ".gitignore" if start_path.is_dir() else start_path.parent / ".gitignore"
            if root_gitignore.is_file():
                spec = load_gitignore_spec(root_gitignore)
                if spec:
                    gitignore_cache[root_gitignore.parent] = spec
        
        # Determine the search root for relative path calculations
        search_root = start_path if start_path.is_dir() else start_path.parent
        
        # Handle single file case
        if start_path.is_file():
            if not is_ignored(start_path, gitignore_cache, search_root, exclude_spec, include_spec):
                if should_include_file(start_path, no_binary_filter, use_extension_filter):
                    all_files.add(start_path)
            continue
        
        # Walk the directory tree
        for root, dirs, files in os.walk(start_path, topdown=True):
            root_path = Path(root)
            
            # Load .gitignore in this directory if it exists and we haven't seen it yet
            if not no_gitignore and root_path not in gitignore_cache:
                gitignore_file = root_path / ".gitignore"
                if gitignore_file.is_file():
                    spec = load_gitignore_spec(gitignore_file)
                    if spec:
                        gitignore_cache[root_path] = spec
            
            # Filter directories in-place (modifies dirs list to prune the walk)
            dirs_to_remove = []
            for d in dirs:
                dir_path = root_path / d
                if is_ignored(dir_path, gitignore_cache, search_root, exclude_spec, include_spec):
                    dirs_to_remove.append(d)
            
            for d in dirs_to_remove:
                dirs.remove(d)
            
            # Check files
            for file_name in files:
                file_path = root_path / file_name
                if not is_ignored(file_path, gitignore_cache, search_root, exclude_spec, include_spec):
                    if should_include_file(file_path, no_binary_filter, use_extension_filter):
                        all_files.add(file_path)

    return sorted(list(all_files))


def analyze_content_sizes(file_contents, common_root):
    total_size = sum(len(c) for c in file_contents.values())
    if total_size == 0: return []

    item_sizes = defaultdict(int)

    for path, content in file_contents.items():
        size = len(content)
        try:
            relative_path = path.relative_to(common_root)
            item_sizes[relative_path] += size
            for parent in relative_path.parents:
                if str(parent) != '.':
                    item_sizes[parent] += size
        except ValueError:
            item_sizes[path] += size
            
    potential_culprits = set()
    for path, size in item_sizes.items():
        if (size / total_size * 100) > LARGE_CONTENT_THRESHOLD_PERCENT:
            potential_culprits.add(path)

    final_culprits = set()
    for path in potential_culprits:
        is_nearest = all(parent not in potential_culprits for parent in path.parents)
        if is_nearest:
            final_culprits.add(path)

    return sorted(list(final_culprits))

# --- Main CLI ---

@click.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
@click.option("-i", "--include", multiple=True, help="Glob pattern for files to include.")
@click.option("-x", "--exclude", multiple=True, help="Glob pattern for files/directories to exclude.")
@click.option("--no-gitignore", is_flag=True, help="Disable parsing of .gitignore files.")
@click.option("--no-binary-filter", is_flag=True, help="Disable the default filter for binary files.")
@click.option("--use-extension-filter", is_flag=True, help="Use extension-based filtering instead of content detection (faster but less accurate).")
@click.option("-o", "--output", type=click.File('w', encoding='utf-8'), help="Output to a file instead of stdout.")
@click.option("-c", "--cxml", is_flag=True, help="Output in XML-ish format suitable for Claude.")
@click.option("-m", "--markdown", is_flag=True, help="Output Markdown with fenced code blocks.")
@click.option("-C", "--copy", is_flag=True, help="Copy the final output to the clipboard.")
@click.version_option()
def cli(paths, include, exclude, no_gitignore, no_binary_filter, use_extension_filter, output, cxml, markdown, copy):
    if not paths and sys.stdin.isatty():
        paths = ['.']
    elif not paths:
        paths = [line.strip() for line in sys.stdin if line.strip()]

    current_exclude = list(exclude)
    current_exclude.append('.git/')
    current_exclude.append('.git/**')

    while True:
        files_to_process = collect_files(paths, include, current_exclude, no_gitignore, no_binary_filter, use_extension_filter)
        if not files_to_process:
            click.echo("No files found matching the criteria.", err=True)
            return

        common_root = Path(os.path.commonpath(files_to_process)) if files_to_process else Path.cwd()
        file_contents = {fp: fp.read_text(encoding='utf-8', errors='ignore') for fp in files_to_process}
        
        large_items = analyze_content_sizes(file_contents, common_root)
        
        if large_items:
            click.echo("Warning: The following items contribute a large portion of the total output:", err=True)
            for item in large_items:
                click.echo(f"  - {item}", err=True)
            
            if click.confirm("Do you want to automatically exclude them and regenerate the output?", err=True):
                current_exclude.extend(str(item) for item in large_items)
                click.echo("Regenerating output with new exclusions...", err=True)
                continue
            else:
                click.echo("Proceeding with the original file selection.", err=True)
                break
        else:
            break

    string_buffer = StringIO()
    writer = string_buffer.write

    project_root = Path(os.path.commonpath(files_to_process)) if files_to_process else Path.cwd()
    tree_str = generate_tree_output(files_to_process, project_root)
    writer(f"Project Structure:\n```\n{tree_str}\n```\n")

    if cxml: writer("<documents>\n")
    for idx, (file_path, content) in enumerate(file_contents.items()):
        try:
            relative_path = file_path.relative_to(project_root)
        except ValueError:
            relative_path = file_path
            
        if cxml: print_as_xml(writer, relative_path, content, idx + 1)
        elif markdown: print_as_markdown(writer, relative_path, content)
        else: print_default(writer, relative_path, content)
    if cxml: writer("</documents>")
        
    final_output = string_buffer.getvalue().strip()

    if copy:
        pyperclip.copy(final_output)
        click.echo("Output copied to clipboard.", err=True)

    if output:
        output.write(final_output)
    elif not copy:
        with open(DEFAULT_OUTPUT_FILENAME, "w", encoding="utf-8") as f:
            f.write(final_output)
        click.echo(f"Output written to {DEFAULT_OUTPUT_FILENAME}", err=True)

if __name__ == "__main__":
    cli()