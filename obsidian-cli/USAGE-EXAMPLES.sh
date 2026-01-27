#!/bin/bash
# Obsidian CLI Usage Examples
# Practical examples demonstrating the Obsidian CLI integration

# ==============================================================================
# BASIC OPERATIONS
# ==============================================================================

# Open today's daily note
obscli obs daily
# or use the alias
today

# Open a specific file
obscli obs open "Projects/MyProject.md"

# Open file at specific heading
obscli obs open "Projects/MyProject.md" --heading "Tasks"

# Open file at specific line
obscli obs open "Notes/Code.md" --line 42

# ==============================================================================
# WORKSPACE MANAGEMENT
# ==============================================================================

# Load workspace by name
obscli obs workspace "1-Obsidian"
obscli obs workspace "2-Civic"

# Quick workspace shortcuts (0-6)
obscli obs quick ws 1  # 1-Obsidian
obscli obs quick ws 2  # 2-Civic
obscli obs quick ws 3  # 3-Algorithms

# Use convenient aliases
ws1  # Loads 1-Obsidian
ws2  # Loads 2-Civic
ws3  # Loads 3-Algorithms
ws4  # Loads 4-Computing

# ==============================================================================
# WRITING CONTENT
# ==============================================================================

# Append content to file
obscli obs write "Notes/Tasks.md" "- [ ] New task from CLI"

# Prepend content
obscli obs write "Notes/Log.md" "$(date): CLI test" --mode prepend

# Overwrite file
obscli obs write "Temp/Test.md" "# Fresh Start\n\nNew content" --mode overwrite

# Write under specific heading
obscli obs write "Projects/Project.md" "- Task item" --heading "Next Actions"

# Write at specific line
obscli obs write "Notes/Code.md" "// New comment" --line 10

# ==============================================================================
# COMMAND EXECUTION
# ==============================================================================

# Execute Obsidian command by ID
obscli obs command "app:reload"
obscli obs command "app:delete-file"
obscli obs command "markdown:toggle-preview"

# Quick command shortcuts
obscli obs quick delete      # Delete current file
obscli obs quick reload      # Reload Obsidian
obscli obs quick toggle-mode # Toggle preview mode

# ==============================================================================
# SEARCH AND REPLACE
# ==============================================================================

# Search for text
obscli obs search "TODO"

# Search in specific file
obscli obs search "TODO" --file "Projects/MyProject.md"

# Search and replace
obscli obs search "TODO" --replace "DONE"

# Search and replace in specific file
obscli obs search "old text" --replace "new text" --file "Notes/Document.md"

# ==============================================================================
# FRONTMATTER MANAGEMENT
# ==============================================================================

# Set frontmatter value
obscli obs frontmatter "Projects/Project.md" "status" "active"
obscli obs frontmatter "Notes/Meeting.md" "date" "$(date +%Y-%m-%d)"
obscli obs frontmatter "Tasks/Task.md" "priority" "high"

# ==============================================================================
# URI BUILDING (for scripts)
# ==============================================================================

# Generate URI without executing
URI=$(obscli obs uri "Notes/File.md" --heading "Section")
echo "Generated URI: $URI"

# Build complex URI
obscli obs uri "Projects/Project.md" --heading "Tasks" --line 10 --mode source

# ==============================================================================
# PRACTICAL WORKFLOWS
# ==============================================================================

# Morning Routine
morning_routine() {
    echo "Starting morning routine..."
    today
    sleep 1
    DATE=$(date +%Y-%m-%d)
    obscli obs write "Journal/Daily/$DATE.md" \
        "## Morning Tasks\n- [x] Opened daily note\n- [ ] Review calendar\n- [ ] Check email" \
        --heading "Log"
    echo "Morning routine initialized!"
}

# Quick Capture
capture() {
    if [ -z "$1" ]; then
        echo "Usage: capture \"your note\""
        return 1
    fi
    obscli obs write "Inbox/Quick-Capture.md" \
        "- $1 [$(date '+%Y-%m-%d %H:%M')]" \
        --heading "Unsorted"
    echo "✓ Captured: $1"
}

# Project Status Update
update_project_status() {
    PROJECT=$1
    STATUS=$2
    if [ -z "$PROJECT" ] || [ -z "$STATUS" ]; then
        echo "Usage: update_project_status \"Project Name\" \"status\""
        return 1
    fi
    obscli obs frontmatter "Projects/$PROJECT.md" "status" "$STATUS"
    obscli obs frontmatter "Projects/$PROJECT.md" "updated" "$(date +%Y-%m-%d)"
    echo "✓ Updated $PROJECT status to: $STATUS"
}

# End of Day Review
end_of_day() {
    DATE=$(date +%Y-%m-%d)
    REVIEW="## End of Day Review\nDate: $(date)\n\n### Completed\n- \n\n### Notes\n- "
    obscli obs write "Journal/Daily/$DATE.md" "$REVIEW" --mode append
    obscli obs open "Journal/Daily/$DATE.md" --heading "End of Day Review"
    echo "Opened daily note for review"
}

# Create Meeting Note
new_meeting() {
    TITLE="${1:-Meeting Notes}"
    DATE=$(date +%Y-%m-%d)
    FILENAME="Meetings/$DATE-$TITLE.md"

    CONTENT="# $TITLE\nDate: $DATE\nTime: $(date +%H:%M)\n\n## Agenda\n- \n\n## Notes\n- \n\n## Action Items\n- [ ] "

    obscli obs write "$FILENAME" "$CONTENT" --mode overwrite
    sleep 1
    obscli obs open "$FILENAME"
    echo "✓ Created meeting note: $FILENAME"
}

# Workspace Context Switching
work_mode() {
    echo "Switching to work mode..."
    ws2  # Civic workspace
    obscli obs open "Projects/Current-Project.md"
    echo "✓ Work mode activated"
}

study_mode() {
    echo "Switching to study mode..."
    ws3  # Algorithms workspace
    obscli obs open "3-Algorithms/Current-Topic.md"
    echo "✓ Study mode activated"
}

dev_mode() {
    echo "Switching to development mode..."
    ws1  # Obsidian workspace
    obscli obs open "1-Obsidian/Development-Log.md"
    echo "✓ Development mode activated"
}

# ==============================================================================
# AUTOMATION EXAMPLES
# ==============================================================================

# Git commit hook example
git_log_to_obsidian() {
    if [ -n "$1" ]; then
        COMMIT_MSG=$1
        obscli obs write "Work/Git-Log.md" \
            "- $COMMIT_MSG [$(date '+%Y-%m-%d %H:%M')]" \
            --heading "Recent Commits"
        echo "✓ Logged commit to Obsidian"
    fi
}

# Batch update frontmatter
update_all_project_dates() {
    echo "Updating all project review dates..."
    for file in /home/token/Obsidian/Token-ENV/Projects/*.md; do
        basename=$(basename "$file")
        obscli obs frontmatter "Projects/$basename" "last_reviewed" "$(date +%Y-%m-%d)"
    done
    echo "✓ Updated all project dates"
}

# Create daily note if not exists
ensure_daily_note() {
    DATE=$(date +%Y-%m-%d)
    FILEPATH="Journal/Daily/$DATE.md"

    # Check if file exists
    if [ ! -f "/home/token/Obsidian/Token-ENV/$FILEPATH" ]; then
        echo "Creating daily note for $DATE..."
        CONTENT="# $DATE\n\n## Focus Areas\n- \n\n## Tasks\n- [ ] \n\n## Log\n- "
        obscli obs write "$FILEPATH" "$CONTENT" --mode overwrite
        echo "✓ Created daily note"
    else
        echo "Daily note already exists"
    fi

    obscli obs open "$FILEPATH"
}

# ==============================================================================
# ADVANCED PATTERNS
# ==============================================================================

# Conditional execution
smart_open() {
    FILE=$1
    VAULT_PATH="/home/token/Obsidian/Token-ENV"

    if [ -f "$VAULT_PATH/$FILE" ]; then
        obscli obs open "$FILE"
    else
        echo "File doesn't exist. Creating..."
        TITLE=$(basename "$FILE" .md)
        obscli obs write "$FILE" "# $TITLE\n\nCreated: $(date)" --mode overwrite
        sleep 1
        obscli obs open "$FILE"
    fi
}

# Chain multiple operations
create_and_setup_project() {
    PROJECT_NAME=$1
    if [ -z "$PROJECT_NAME" ]; then
        echo "Usage: create_and_setup_project \"Project Name\""
        return 1
    fi

    echo "Creating project: $PROJECT_NAME"

    # Create file
    obscli obs write "Projects/$PROJECT_NAME.md" "# $PROJECT_NAME\n\n" --mode overwrite
    sleep 1

    # Set frontmatter
    obscli obs frontmatter "Projects/$PROJECT_NAME.md" "status" "active"
    obscli obs frontmatter "Projects/$PROJECT_NAME.md" "created" "$(date +%Y-%m-%d)"
    obscli obs frontmatter "Projects/$PROJECT_NAME.md" "workspace" "2-Civic"

    # Add initial structure
    obscli obs write "Projects/$PROJECT_NAME.md" \
        "## Overview\n\n## Objectives\n- \n\n## Tasks\n- [ ] \n\n## Notes\n- " \
        --mode append

    # Open file
    obscli obs open "Projects/$PROJECT_NAME.md"

    echo "✓ Project setup complete!"
}

# Pipe content from clipboard
clipboard_to_note() {
    FILENAME=${1:-"Inbox/Clipboard.md"}
    CLIP_CONTENT=$(xclip -o 2>/dev/null || pbpaste 2>/dev/null)

    if [ -n "$CLIP_CONTENT" ]; then
        obscli obs write "$FILENAME" "\n---\n$CLIP_CONTENT\n" --mode append
        echo "✓ Clipboard content added to $FILENAME"
    else
        echo "No clipboard content found"
    fi
}

# ==============================================================================
# HELP
# ==============================================================================

# Show available functions
show_examples() {
    echo "Available workflow functions:"
    echo "  morning_routine              - Initialize daily note with tasks"
    echo "  capture \"text\"               - Quick capture to inbox"
    echo "  update_project_status        - Update project frontmatter"
    echo "  end_of_day                   - End of day review template"
    echo "  new_meeting \"title\"          - Create meeting note"
    echo "  work_mode                    - Switch to work context"
    echo "  study_mode                   - Switch to study context"
    echo "  dev_mode                     - Switch to dev context"
    echo "  ensure_daily_note            - Create/open daily note"
    echo "  smart_open \"file\"            - Open or create file"
    echo "  create_and_setup_project     - Full project setup"
    echo ""
    echo "To use these functions, source this file:"
    echo "  source /home/token/Obsidian/Token-ENV/Scripts/cli/USAGE-EXAMPLES.sh"
}

# ==============================================================================
# USAGE
# ==============================================================================

# If sourced, make functions available
# If executed, show help
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    echo "Obsidian CLI Usage Examples"
    echo "============================"
    echo ""
    echo "To use these functions, source this file:"
    echo "  source $0"
    echo ""
    show_examples
else
    echo "✓ Obsidian CLI workflow functions loaded"
    echo "Run 'show_examples' to see available functions"
fi
