# Obsidian Advanced URI Commands

Execute Obsidian commands from the command line using the Advanced URI plugin.

## Quick Start

```bash
# Open today's daily note
obscli obs daily

# Load a workspace
obscli obs workspace "1-Obsidian"

# Open a specific file
obscli obs open "Journal/Daily/2025-01-15.md"

# Open file at specific heading
obscli obs open "Projects/MyProject.md" --heading "Tasks"

# Quick workspace shortcuts
obscli obs quick ws 1  # Load workspace 1-Obsidian
```

## Command Reference

### File Operations

#### Open File
```bash
# Basic file open
obscli obs open "path/to/file.md"

# Open at specific heading
obscli obs open "file.md" --heading "Section Name"

# Open at specific block
obscli obs open "file.md" --block "block-id"

# Open at specific line
obscli obs open "file.md" --line 42

# Open in specific mode
obscli obs open "file.md" --mode source    # source, preview, or live
```

#### Write to File
```bash
# Append content
obscli obs write "file.md" "New content here"

# Prepend content
obscli obs write "file.md" "Top content" --mode prepend

# Overwrite file
obscli obs write "file.md" "Replace all" --mode overwrite

# Append under specific heading
obscli obs write "file.md" "Task item" --heading "Tasks"

# Insert at specific line
obscli obs write "file.md" "New line" --line 10
```

### Workspace Management

```bash
# Load workspace by name
obscli obs workspace "1-Obsidian"

# Quick workspace shortcuts (0-6)
obscli obs quick ws 0  # 0-Admin
obscli obs quick ws 1  # 1-Obsidian
obscli obs quick ws 2  # 2-Civic
obscli obs quick ws 3  # 3-Algorithms
obscli obs quick ws 4  # 4-Computing
obscli obs quick ws 6  # 6-Scheming
```

### Daily Notes

```bash
# Open today's daily note
obscli obs daily
```

### Command Execution

```bash
# Execute any Obsidian command by ID
obscli obs command "app:delete-file"

# Execute command after opening file
obscli obs command "markdown:toggle-preview" --file "note.md"

# Common command IDs:
# - app:delete-file
# - app:reload
# - markdown:toggle-preview
# - editor:toggle-fold
# - workspace:split-vertical
```

### Search and Replace

```bash
# Search for text
obscli obs search "TODO"

# Search in specific file
obscli obs search "TODO" --file "Projects/MyProject.md"

# Search and replace
obscli obs search "old text" --replace "new text"

# Search and replace in specific file
obscli obs search "TODO" --replace "DONE" --file "Tasks.md"
```

### Frontmatter

```bash
# Set frontmatter value
obscli obs frontmatter "file.md" "status" "completed"

# Set tags
obscli obs frontmatter "file.md" "tags" "work, important"

# Update metadata
obscli obs frontmatter "Projects/Project.md" "priority" "high"
```

### JavaScript Evaluation

```bash
# Execute JavaScript in Obsidian context
obscli obs eval "console.log('Hello from CLI')"

# More complex operations
obscli obs eval "app.workspace.getActiveFile().basename"
```

### Settings

```bash
# Open specific settings panel
obscli obs settings "plugin-browser"
obscli obs settings "appearance"
obscli obs settings "hotkeys"
```

### Quick Commands

Convenient shortcuts for common operations:

```bash
# Delete current file
obscli obs quick delete

# Reload Obsidian
obscli obs quick reload

# Toggle reading/editing mode
obscli obs quick toggle-mode

# Create new note
obscli obs quick create "My New Note"
```

### URI Builder

Generate URIs without executing them (useful for scripting):

```bash
# Build URI for file
obscli obs uri "path/to/file.md"

# Build URI with parameters
obscli obs uri "file.md" --heading "Tasks" --line 10

# Copy to clipboard or use in scripts
URI=$(obscli obs uri "file.md" --heading "Section")
echo $URI
```

## Vault Selection

All commands support the `--vault` option to target different vaults:

```bash
# Use different vault
obscli obs --vault "Personal-ENV" daily

# Default is Token-ENV
obscli obs daily  # Uses Token-ENV
```

## Integration Examples

### Shell Scripts

```bash
#!/bin/bash
# Open daily note and add a task

obscli obs daily

# Wait a moment for Obsidian to open
sleep 1

# Add task under Tasks heading
obscli obs write "Journal/Daily/$(date +%Y-%m-%d).md" \
    "- [ ] New task from script" \
    --heading "Tasks"
```

### Bash Aliases

Add to `~/.bash_aliases`:

```bash
# Quick daily note
alias today="obscli obs daily"

# Quick workspace switching
alias ws1="obscli obs quick ws 1"
alias ws2="obscli obs quick ws 2"

# Quick note creation
alias note="obscli obs quick create"

# Open specific project
alias proj="obscli obs open 'Projects/Current.md'"
```

### AutoHotkey Integration

```ahk
; Open daily note with Ctrl+D
^d::Run("obscli obs daily", , "Hide")

; Quick workspace switching
Numpad1::Run("obscli obs quick ws 1", , "Hide")
Numpad2::Run("obscli obs quick ws 2", , "Hide")

; Create note from clipboard
^!n::{
    clip := A_Clipboard
    Run("obscli obs quick create '" clip "'", , "Hide")
}
```

### Python Integration

```python
import subprocess

def obs_open(filepath, heading=None):
    """Open file in Obsidian."""
    cmd = ["obscli", "obs", "open", filepath]
    if heading:
        cmd.extend(["--heading", heading])
    subprocess.run(cmd)

def obs_write(filepath, content, mode="append"):
    """Write to Obsidian file."""
    cmd = ["obscli", "obs", "write", filepath, content, "--mode", mode]
    subprocess.run(cmd)

# Usage
obs_open("Projects/MyProject.md", heading="Tasks")
obs_write("Tasks.md", "- [ ] New task", mode="append")
```

### Templater Integration

```javascript
// In Templater template
const { execSync } = require('child_process');

// Open related project
const projectFile = "Projects/CurrentProject.md";
execSync(`obscli obs open "${projectFile}"`);

// Add to daily note
const today = tp.date.now("YYYY-MM-DD");
const dailyPath = `Journal/Daily/${today}.md`;
execSync(`obscli obs write "${dailyPath}" "- Note created at ${tp.date.now("HH:mm")}" --heading "Log"`);
```

## Advanced Patterns

### Batch Operations

```bash
# Open multiple files
for file in Projects/*.md; do
    obscli obs open "$file"
    sleep 2
done

# Update status in multiple files
for file in Projects/*.md; do
    obscli obs frontmatter "$file" "reviewed" "$(date +%Y-%m-%d)"
done
```

### Conditional Execution

```bash
# Open file only if it exists
if [ -f "/home/token/Obsidian/Token-ENV/$FILE" ]; then
    obscli obs open "$FILE"
else
    obscli obs quick create "$FILE"
fi
```

### Piping Content

```bash
# Write clipboard to file
xclip -o | xargs -I {} obscli obs write "Inbox/Clip.md" "{}"

# Append command output to note
date | xargs -I {} obscli obs write "Log.md" "Timestamp: {}"
```

### Chaining Commands

```bash
# Create note, add content, then open
obscli obs quick create "Meeting Notes" && \
obscli obs write "Meeting Notes.md" "# Meeting Notes\n\nDate: $(date)" && \
obscli obs open "Meeting Notes.md"
```

## Common Use Cases

### Project Management

```bash
# Create new project
obscli obs quick create "Project: New Feature"
obscli obs frontmatter "Project: New Feature.md" "status" "active"
obscli obs frontmatter "Project: New Feature.md" "workspace" "2-Civic"

# Update project status
obscli obs frontmatter "Projects/Feature.md" "status" "completed"
```

### Daily Workflow

```bash
# Morning routine
obscli obs daily
obscli obs write "Journal/Daily/$(date +%Y-%m-%d).md" \
    "## Morning Routine\n- [x] Opened daily note" \
    --heading "Log"

# End of day review
obscli obs write "Journal/Daily/$(date +%Y-%m-%d).md" \
    "## End of Day\nCompleted tasks: 5\nEnergy: 7/10" \
    --mode append
```

### Quick Capture

```bash
# Capture idea quickly
IDEA="New automation idea"
obscli obs write "Inbox/Ideas.md" "- $IDEA [$(date +%H:%M)]" --heading "Unsorted"
```

### Automation Integration

```bash
# Git commit hook - update work log
obscli obs write "Work/Log.md" \
    "- Committed: $GIT_COMMIT_MSG at $(date)" \
    --heading "$(date +%Y-%m-%d)"

# Cron job - daily backup note
0 18 * * * obscli obs write "Admin/Backups.md" \
    "- Backup completed: $(date)" --mode append
```

## Troubleshooting

### URI Not Executing

If URIs don't execute:
1. Ensure Advanced URI plugin is installed in Obsidian
2. Check vault name matches exactly: `--vault "Token-ENV"`
3. Verify PowerShell is available: `powershell.exe -Command "echo test"`

### File Not Found

If files aren't found:
- Use relative paths from vault root: `Journal/Daily/file.md`
- Ensure file extension is included: `.md`
- Check file exists in vault: `ls /home/token/Obsidian/Token-ENV/path/to/file.md`

### Special Characters

For files with special characters:
- Use quotes: `obscli obs open "File with spaces.md"`
- Escape as needed: `obscli obs open "File \"with\" quotes.md"`

## Command ID Reference

Common Obsidian command IDs:
- `app:delete-file` - Delete current file
- `app:reload` - Reload Obsidian
- `app:open-vault` - Open vault picker
- `markdown:toggle-preview` - Toggle preview mode
- `editor:toggle-fold` - Toggle fold
- `editor:focus-top` - Focus top of editor
- `workspace:split-vertical` - Split vertically
- `workspace:split-horizontal` - Split horizontally
- `workspace:close` - Close current pane
- `file-explorer:reveal-active-file` - Reveal in file explorer
- `command-palette:open` - Open command palette

Find more command IDs:
1. Open Obsidian Settings → Hotkeys
2. Search for desired command
3. Use the command ID shown in URL when hovering

## See Also

- [Advanced URI Plugin Docs](https://publish.obsidian.md/advanced-uri-doc/)
- [CLI Framework README](README.md)
- [Quick Start Guide](QUICKSTART.md)
