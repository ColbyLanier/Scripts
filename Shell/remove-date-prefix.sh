#!/bin/bash

# Script to remove YYYY-MM-DD and YYYY-MM-DD-HHMM date/timestamp prefixes from filenames
# Usage: ./remove-date-prefix.sh [directory]
# If no directory is provided, uses current directory
# Processes files recursively through all subdirectories

# Set the target directory (default to current directory)
TARGET_DIR="${1:-.}"

# Check if directory exists
if [ ! -d "$TARGET_DIR" ]; then
    echo "Error: Directory '$TARGET_DIR' does not exist."
    exit 1
fi

# Get absolute path for proper handling
TARGET_DIR=$(cd "$TARGET_DIR" && pwd)

# Counter for renamed files
renamed_count=0

# Function to process a single file
process_file() {
    local file_path="$1"
    local file_dir=$(dirname "$file_path")
    local file=$(basename "$file_path")
    
    # Skip if it's a directory
    [ -d "$file_path" ] && return
    
    # Extract filename and extension
    filename="${file%.*}"
    extension="${file##*.}"
    
    # Check if filename starts with YYYY-MM-DD pattern (basic date)
    # or YYYY-MM-DD-HHMM pattern (extended timestamp)
    # Pattern 1: YYYY-MM-DD-... (basic date with optional hyphen after)
    # Pattern 2: YYYY-MM-DD-HHMM-... or YYYY-MM-DD-HHMM ... (extended timestamp)
    if [[ "$filename" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})(-[0-9]{4})?(.*)$ ]]; then
        date_prefix="${BASH_REMATCH[1]}"
        timestamp_part="${BASH_REMATCH[2]}"
        remaining="${BASH_REMATCH[3]}"
        
        # Remove leading whitespace, hyphens, or spaces from remaining part
        remaining=$(echo "$remaining" | sed 's/^[[:space:]-]*//')
        
        # Only rename if there's something left after removing the date/timestamp
        if [ -n "$remaining" ]; then
            new_filename="${remaining}.${extension}"
            new_file_path="${file_dir}/${new_filename}"
            
            # Check if target file already exists
            if [ -e "$new_file_path" ]; then
                echo "Warning: Skipping '$file_path' - target '$new_file_path' already exists"
                return
            fi
            
            # Rename the file
            mv "$file_path" "$new_file_path"
            echo "Renamed: '$file_path' -> '$new_file_path'"
            ((renamed_count++))
        else
            echo "Warning: Skipping '$file_path' - no descriptive title after date/timestamp prefix"
        fi
    fi
}

# Use find to recursively process all files
while IFS= read -r -d '' file_path; do
    process_file "$file_path"
done < <(find "$TARGET_DIR" -type f -print0)

echo ""
echo "Process complete. Renamed $renamed_count file(s)."

