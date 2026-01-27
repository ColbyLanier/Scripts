#!/usr/bin/env node

/**
 * Log deduplication utility for .claude-deploy.log
 *
 * This script intelligently reduces log verbosity by removing redundant information:
 * - After successful Docker build: Remove pip requirements logs
 * - After successful Docker push: Remove "Waiting" lines
 * - Keep Docker logs with INFO or WARNING
 * - Truncate excessive dots in deployment progress lines
 *
 * Usage:
 *   node deduplicate-log.js [logfile]
 *   node deduplicate-log.js  # defaults to .claude-deploy.log in cwd
 */

const fs = require('fs');
const path = require('path');

const LOG_FILE = process.argv[2] || path.join(process.cwd(), '.claude-deploy.log');

if (!fs.existsSync(LOG_FILE)) {
  console.error(`Log file not found: ${LOG_FILE}`);
  process.exit(1);
}

// Read the log file
const logContent = fs.readFileSync(LOG_FILE, 'utf8');
const lines = logContent.split('\n');

// Check for success markers
const dockerBuildSuccess = logContent.includes('Image built:');
const dockerPushSuccess = logContent.includes('Push successful');

let processedLines = [];
let inAptGetSection = false;
let inPipInstallSection = false;

for (let i = 0; i < lines.length; i++) {
  let line = lines[i];
  let keepLine = true;

  // Rule 1: After successful Docker build, remove verbose dependency installation logs
  if (dockerBuildSuccess) {
    // Check if we're in apt-get section
    if (inAptGetSection) {
      // Keep the DONE line and INFO/WARNING, skip verbose package lists
      if (line.match(/^#9 DONE/) || line.includes('INFO') || line.includes('WARNING')) {
        keepLine = true;
        if (line.match(/^#9 DONE/)) {
          inAptGetSection = false;
        }
      } else if (line.startsWith('#9 ')) {
        keepLine = false;
      } else {
        // Different section started, exit apt-get section
        inAptGetSection = false;
        keepLine = true;
      }
    }
    // Detect apt-get section start (step #9)
    else if (line.match(/^#9 \[base-stage 4\/4\] RUN apt-get/)) {
      inAptGetSection = true;
      keepLine = true; // Keep the header line
    }

    // Check if we're in pip install section
    if (inPipInstallSection) {
      // Keep INFO/WARNING lines and completion markers, skip verbose pip output
      if (line.includes('INFO') || line.includes('WARNING') ||
          line.includes('Successfully built') || line.includes('Successfully installed') ||
          line.includes('ERROR')) {
        keepLine = true;
      } else if (line.startsWith('#11 ') && !line.match(/^#11 \[deps-stage 2\/2\] RUN pip install/)) {
        // Still in #11 section but not header, skip unless it's DONE or important
        if (line.match(/^#11 DONE/)) {
          inPipInstallSection = false;
          keepLine = true;
        } else {
          keepLine = false;
        }
      } else if (!line.startsWith('#11 ')) {
        // Different section started, exit pip section
        inPipInstallSection = false;
        keepLine = true;
      }
    }
    // Detect pip install section start (step #11)
    else if (line.match(/^#11 \[deps-stage 2\/2\] RUN pip install/)) {
      inPipInstallSection = true;
      keepLine = true; // Keep the header line
    }
  }

  // Rule 2: After successful push, remove "Waiting" lines
  if (dockerPushSuccess && line.trim().endsWith('Waiting')) {
    keepLine = false;
  }

  // Rule 3: Keep Docker logs with INFO or WARNING (override previous rules)
  if (line.includes('INFO') || line.includes('WARNING')) {
    keepLine = true;
  }

  // Rule 4: Truncate excessive dots in deployment progress lines
  // Match patterns like "Creating Revision...........done" or "Deploying.....................done"
  if (line.match(/(Deploying|Creating Revision|Routing traffic)\.{4,}/)) {
    line = line.replace(/(Deploying|Creating Revision|Routing traffic)\.{4,}(done|failed)?/, '$1...$2');
  }

  if (keepLine) {
    processedLines.push(line);
  }
}

// Write deduplicated log back
const deduplicatedContent = processedLines.join('\n');
fs.writeFileSync(LOG_FILE, deduplicatedContent, 'utf8');

// Calculate reduction
const originalSize = logContent.length;
const newSize = deduplicatedContent.length;
const reduction = ((originalSize - newSize) / originalSize * 100).toFixed(1);

console.log(`Log deduplicated: ${LOG_FILE}`);
console.log(`   Original: ${originalSize} bytes`);
console.log(`   New: ${newSize} bytes`);
console.log(`   Reduction: ${reduction}%`);
