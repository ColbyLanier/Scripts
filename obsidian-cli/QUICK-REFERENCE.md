# Obsidian CLI Quick Reference

## Most Common Commands

```bash
# Daily Operations
today                    # Open today's daily note
obscli obs daily         # Same as above

# Workspace Switching
ws1                      # Load workspace 1-Obsidian
ws2                      # Load workspace 2-Civic
ws3                      # Load workspace 3-Algorithms
ws4                      # Load workspace 4-Computing

# File Operations
obscli obs open "file.md"                      # Open file
obscli obs open "file.md" --heading "Tasks"    # Open at heading
obscli obs write "file.md" "Content"           # Append content
obscli obs quick create "Note Name"            # Create new note

# Quick Actions
obscli obs command "app:delete-file"           # Delete current file
obscli obs command "app:reload"                # Reload Obsidian
obscli obs quick delete                        # Delete (shortcut)
obscli obs quick reload                        # Reload (shortcut)
```

## Command Structure

```bash
obscli obs <subcommand> [arguments] [options]
```

### Subcommands

- `open` - Open files
- `write` - Write to files
- `workspace` - Load workspace
- `daily` - Open daily note
- `command` - Execute Obsidian command
- `search` - Search (and replace)
- `frontmatter` - Update frontmatter
- `uri` - Build URI without executing
- `quick` - Quick shortcuts

## Parameters

### File Opening
```bash
--heading "Name"    # Navigate to heading
--block "id"        # Navigate to block
--line 42           # Navigate to line
--mode source       # source/preview/live
```

### File Writing
```bash
--mode append       # append/prepend/overwrite
--heading "Name"    # Write under heading
--line 10           # Write at line
```

### Search
```bash
--replace "text"    # Replace matches
--file "path"       # Limit to file
```

## Vault Selection

```bash
--vault "Token-ENV"      # Specify vault
--vault "Personal-ENV"   # Different vault
```

## Integration Patterns

### Shell Script
```bash
#!/bin/bash
obscli obs daily
sleep 1
obscli obs write "Journal/Daily/$(date +%Y-%m-%d).md" "- Task" --heading "Tasks"
```

### AutoHotkey
```ahk
^d::Run("today", , "Hide")
Numpad1::Run("ws1", , "Hide")
```

### Cron Job
```bash
0 9 * * * obscli obs daily
0 18 * * * obscli obs write "Log.md" "Daily backup: $(date)"
```

## Examples by Use Case

### Morning Routine
```bash
today
obscli obs write "$(date +%Y-%m-%d).md" "## Morning Tasks" --mode append
```

### Project Management
```bash
obscli obs open "Projects/Current.md"
obscli obs frontmatter "Projects/Current.md" "status" "active"
```

### Quick Capture
```bash
obscli obs write "Inbox/Quick.md" "- Idea: $1" --mode append
```

### Workspace Organization
```bash
ws1  # Development work
ws2  # Civic projects
ws3  # Algorithm studies
ws4  # Computing/automation
```

## Command IDs Reference

```bash
# File operations
app:delete-file
app:reload

# View operations
markdown:toggle-preview
editor:toggle-fold

# Workspace operations
workspace:split-vertical
workspace:split-horizontal
workspace:close

# Navigation
file-explorer:reveal-active-file
command-palette:open
```

## Troubleshooting

**Command not found?**
```bash
source ~/.bash_aliases
```

**URI not executing?**
- Check vault name: `--vault "Token-ENV"`
- Verify Advanced URI plugin installed
- Test with: `obscli obs daily`

**File not found?**
- Use relative path from vault root
- Include `.md` extension
- Use quotes for spaces

## Full Documentation

- Complete guide: `Scripts/cli/OBSIDIAN-COMMANDS.md`
- Framework docs: `Scripts/cli/README.md`
- Quick start: `Scripts/cli/QUICKSTART.md`

## Help Commands

```bash
obscli --help                # Main help
obscli obs --help            # Obsidian commands help
obscli obs open --help       # Specific command help
obscli obs quick --help      # Quick shortcuts help
```
