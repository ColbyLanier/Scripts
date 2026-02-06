#!/usr/bin/env node

/**
 * Autonomous deployment utility for Claude Code
 *
 * This script handles deployment in a separate terminal window with proper logging
 * and error handling. It uses a mutex signal to prevent concurrent deployments.
 *
 * Usage:
 *   deploy [target] [options]
 *
 * Targets:
 *   dev, development   Cloud development deployment (default)
 *   prod, production   Cloud production deployment
 *   local              Local continuous server with ngrok
 *   debug              Local server with debugpy on port 5678
 *
 * Options:
 *   -b, --blocking     Wait for completion, monitor errors
 *   -p, --skip-build   Use last built image (skip docker build)
 *   -y, --skip-push    Use last pushed image (skip build + push)
 *   -h, --help         Show help
 *
 * Examples:
 *   # Cloud deployments
 *   deploy                           # Dev async deployment
 *   deploy -b                        # Dev blocking deployment
 *   deploy prod -b                   # Prod blocking deployment
 *
 *   # Local deployments
 *   deploy local                     # Local server (continuous)
 *   deploy local -b                  # Local with health monitoring
 *   deploy debug                     # Local with debugger
 *
 * Legacy Syntax (deprecated but supported):
 *   deploy development -l            # Use: deploy local
 *   deploy development -d            # Use: deploy debug
 *
 * Request Options (for local deployments):
 *   --request-endpoint <path>     : Endpoint to call after health checks (e.g., /health)
 *   --request-method <method>     : HTTP method (GET, POST, etc.) - default: GET
 *   --request-body <json-string>  : Request body as JSON string
 *   --request-headers <json>      : Custom headers as JSON object
 *   --force-localhost             : Force use of localhost instead of ngrok (for testing)
 *   --localhost                   : Alias for --force-localhost
 *   --one-shot                    : One-shot mode: start -> health check -> request -> stop
 *   --test-and-stop               : Alias for --one-shot
 *   --auto-stop                   : Alias for --one-shot
 *
 * One-Shot Mode (--one-shot):
 *   Runs a complete end-to-end test cycle:
 *   1. Starts local server
 *   2. Waits for health checks to pass
 *   3. Sends the configured request
 *   4. Automatically stops the server
 *   5. Cleans up and exits
 *
 *   Requirements:
 *   - Must be used with local deployment (local or debug target)
 *   - Requires --blocking mode (enabled automatically)
 *   - Requires --request-endpoint to be specified
 *
 *   Example:
 *   deploy local -b --one-shot \
 *     --request-endpoint /health \
 *     --request-method GET
 *
 * Google Chat Testing:
 *   --google-chat-message <text> : Send a Google Chat test message after health check
 *                                  Automatically sets endpoint, method, body, and headers
 *
 *   Example:
 *   deploy local -b --one-shot \
 *     --google-chat-message "hello world"
 *
 * URL Selection (for local deployments):
 *   By default, the system prefers ngrok URLs to simulate production environment.
 *   This ensures end-to-end testing through the full pipeline (ngrok routing, TLS, headers).
 *   Use --force-localhost to test via localhost directly (faster, but doesn't test ngrok routing).
 */

const { exec, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

// Import from same directory (~/cli-tools/src/deploy/)
const DEPLOY_DIR = __dirname;
const { waitForMutex, releaseMutex, SHARED_MUTEX_FILE } = require(path.join(DEPLOY_DIR, 'mutex-helper'));
const { initializeState, setPendingRequest, getState, waitForNgrokUrl } = require(path.join(DEPLOY_DIR, 'local-server-state'));
const { waitForHealthAndSendRequest } = require(path.join(DEPLOY_DIR, 'wait-for-health'));
const { stopLocalServer } = require(path.join(DEPLOY_DIR, 'stop-local-server'));
const { buildRequestConfig } = require(path.join(DEPLOY_DIR, 'google-chat-message'));

// Log file is always in project root
const LOG_FILE = path.join(process.cwd(), '.claude-deploy.log');

// Parse arguments - support both target-based (new) and flag-based (legacy)
let environment = 'development';
let target = null; // 'local', 'debug', or null for cloud
let flag = '';
let mode = 'async';
let requestEndpoint = null;
let requestMethod = 'GET';
let requestBody = null;
let googleChatMessage = null;
let requestHeaders = null;
let firstPositionalArgProcessed = false;

for (let i = 2; i < process.argv.length; i++) {
  const arg = process.argv[i];

  // Handle long-form options
  if (arg === '--blocking' || arg === '-b') {
    mode = 'blocking';
  } else if (arg === '--request-endpoint' && i + 1 < process.argv.length) {
    requestEndpoint = process.argv[++i];
  } else if (arg === '--request-method' && i + 1 < process.argv.length) {
    requestMethod = process.argv[++i].toUpperCase();
  } else if (arg === '--request-body' && i + 1 < process.argv.length) {
    try {
      requestBody = JSON.parse(process.argv[++i]);
    } catch (error) {
      requestBody = process.argv[i];
    }
  } else if (arg === '--request-headers' && i + 1 < process.argv.length) {
    try {
      requestHeaders = JSON.parse(process.argv[++i]);
    } catch (error) {
      console.warn('Warning: Invalid JSON for request headers, ignoring');
    }
  } else if (arg === '--google-chat-message' && i + 1 < process.argv.length) {
    googleChatMessage = process.argv[++i];
  } else if (arg === '--one-shot' || arg === '--test-and-stop' || arg === '--auto-stop' ||
             arg === '--force-localhost' || arg === '--localhost') {
    // Skip these flags - they are handled separately via process.argv.includes()
    continue;
  } else if (arg === '-l') {
    // Legacy: Local deployment flag
    console.warn('⚠️  Warning: -l is deprecated, use "deploy local" instead');
    target = 'local';
    flag = arg; // Keep for Makefile compatibility
  } else if (arg === '-d') {
    // Legacy: Debug deployment flag
    console.warn('⚠️  Warning: -d is deprecated, use "deploy debug" instead');
    target = 'debug';
    flag = arg; // Keep for Makefile compatibility
  } else if ((arg === '-p' || arg === '--skip-build') ||
             (arg === '-y' || arg === '--skip-push')) {
    // Build control flags
    flag = arg.startsWith('--') ? arg.substring(2, 3) : arg.substring(1);
    if (flag === 's') flag = 'p'; // --skip-build -> -p
  } else if (!firstPositionalArgProcessed && !arg.startsWith('-')) {
    // First positional argument: target or environment
    firstPositionalArgProcessed = true;

    if (arg === 'local') {
      target = 'local';
      flag = '-l'; // Set flag for Makefile compatibility
    } else if (arg === 'debug') {
      target = 'debug';
      flag = '-d'; // Set flag for Makefile compatibility
    } else if (arg === 'dev' || arg === 'development') {
      environment = 'development';
    } else if (arg === 'prod' || arg === 'production') {
      environment = 'production';
    } else {
      // Assume it's an environment
      environment = arg;
    }
  }
}

// If no flag but target is set, derive flag from target
if (!flag && target) {
  flag = target === 'local' ? '-l' : '-d';
}

// Check if this is a local deployment (supports both new and legacy syntax)
const isLocalDeployment = target === 'local' || target === 'debug' || flag === '-l' || flag === '-d';

// Check if user wants to force localhost (for testing)
const forceLocalhost = process.argv.includes('--force-localhost') || process.argv.includes('--localhost');

// Check if user wants one-shot mode (start -> health check -> request -> stop)
const oneShot = process.argv.includes('--one-shot') ||
                process.argv.includes('--test-and-stop') ||
                process.argv.includes('--auto-stop');

// If --google-chat-message is provided, build the request config automatically
if (googleChatMessage) {
  const chatConfig = buildRequestConfig(googleChatMessage);
  requestEndpoint = chatConfig.endpoint;
  requestMethod = chatConfig.method;
  requestBody = chatConfig.body;
  requestHeaders = chatConfig.headers;
  console.log(`Google Chat message configured: "${googleChatMessage}"`);
}

// Main execution wrapped in async function to support mutex waiting
(async () => {
  // Derive repo name from current directory (supports worktrees)
  const repoName = path.basename(process.cwd());

  // Wait for mutex to become available (with 20 min timeout, 30 sec polling)
  const acquired = await waitForMutex(repoName, environment, flag);

  if (!acquired) {
    console.error('Could not acquire deployment mutex (timeout or conflict)');
    process.exit(1);
  }

  console.log(`Deployment triggered for ${environment} environment`);

  // Validate one-shot mode (only works with local deployments)
  if (oneShot && !isLocalDeployment) {
    console.error('--one-shot flag only works with local deployments (-l or -d)');
    releaseMutex(true);
    process.exit(1);
  }

  // One-shot mode requires blocking mode
  if (oneShot && mode !== 'blocking') {
    console.log('--one-shot requires --blocking mode, enabling blocking mode...');
    mode = 'blocking';
  }

  // One-shot mode requires a request endpoint
  if (oneShot && !requestEndpoint) {
    console.error('--one-shot requires --request-endpoint to be specified');
    releaseMutex(true);
    process.exit(1);
  }

  // Initialize local server state if this is a local deployment
  if (isLocalDeployment) {
    initializeState(environment, flag, oneShot);

    // Set pending request if provided
    if (requestEndpoint) {
      setPendingRequest({
        endpoint: requestEndpoint,
        method: requestMethod,
        body: requestBody,
        headers: requestHeaders
      });
      console.log(`Request queued: ${requestMethod} ${requestEndpoint}`);
      if (requestBody) {
        console.log(`   Body: ${JSON.stringify(requestBody).substring(0, 100)}...`);
      }
    }

    if (oneShot) {
      console.log(`One-shot mode enabled: will auto-stop after request`);
    }
  }

  // Clear previous log
  if (fs.existsSync(LOG_FILE)) {
    fs.unlinkSync(LOG_FILE);
  }

  // Build the make command
  const makeCmd = `make deploy ENVIRONMENT=${environment}${flag ? ` FLAG=${flag}` : ''}`;

  // Detect if running in WSL
  const isWSL = fs.existsSync('/proc/version') &&
                fs.readFileSync('/proc/version', 'utf8').toLowerCase().includes('microsoft');

  // Get paths
  const projectDir = process.cwd();
  const wrapperScript = path.join(DEPLOY_DIR, 'deploy-wrapper.sh');

  // Verify wrapper script exists
  if (!fs.existsSync(wrapperScript)) {
    console.error(`Wrapper script not found: ${wrapperScript}`);
    releaseMutex(true); // Force release on error
    process.exit(1);
  }

  // Build terminal command based on platform
  let terminalCmd;

  if (isWSL || process.platform === 'win32') {
    // For WSL on Windows: Use PowerShell script to launch with foreground focus
    // This workaround is needed because wt.exe doesn't have a native foreground flag
    const psScript = path.join(DEPLOY_DIR, 'launch-terminal.ps1');
    const title = `Claude Deploy - ${environment}`;

    terminalCmd = `powershell.exe -ExecutionPolicy Bypass -File "${psScript}" -Title "${title}" -WrapperScript "${wrapperScript}" -Environment "${environment}" -Flag "${flag}" -ProjectDir "${projectDir}"`;
  } else if (process.platform === 'darwin') {
    // For macOS: Launch Terminal.app with our wrapper script and activate
    terminalCmd = `osascript -e 'tell app "Terminal" to do script "bash \\"${wrapperScript}\\" \\"${environment}\\" \\"${flag}\\" \\"${projectDir}\\""' -e 'tell app "Terminal" to activate'`;
  } else {
    // Native Linux: Launch gnome-terminal with our wrapper script
    terminalCmd = `gnome-terminal -- bash "${wrapperScript}" "${environment}" "${flag}" "${projectDir}"`;
  }

  // Launch deployment in separate terminal
  exec(terminalCmd, (error) => {
    if (error) {
      console.error('Failed to launch deployment terminal:', error);
      fs.writeFileSync(LOG_FILE, `Failed to launch terminal: ${error.message}\n`);
      // Clean up mutex on failure
      releaseMutex(true);
      process.exit(1);
    }
  });

  if (mode === 'blocking') {
    console.log(`
Deployment running in BLOCKING mode
${oneShot ? 'One-shot mode enabled - will auto-stop after request' : ''}

Environment: ${environment}
Command: ${makeCmd}
Log file: ${LOG_FILE}
${isLocalDeployment ? 'Local deployment detected - will monitor health checks' : ''}
${requestEndpoint ? `Request queued: ${requestMethod} ${requestEndpoint}` : ''}

Monitoring deployment progress...
`);

    // For local deployments, wait a bit then monitor health checks
    if (isLocalDeployment) {
      // Give server time to start (wait 10 seconds)
      setTimeout(async () => {
        try {
          // Use ngrok by default (unless forced to localhost)
          const preferNgrok = !forceLocalhost;

          if (preferNgrok) {
            console.log('\nWaiting for ngrok to be ready...');
          }

          const result = await waitForHealthAndSendRequest(preferNgrok);
          if (result.success) {
            console.log(`\nLocal server is ready!`);
            console.log(`   Using: ${result.urlType || 'unknown'} (${result.url || 'unknown'})`);
            if (result.request) {
              console.log(`Request sent successfully via ${result.urlType || 'unknown'}`);

              // One-shot mode: stop server after successful request
              if (oneShot) {
                console.log('\nOne-shot mode: stopping server...');
                try {
                  // Suppress stop-local-server output by redirecting to log
                  await stopLocalServer(false); // Graceful shutdown
                  console.log('\nOne-shot test completed successfully!');
                  console.log('   Server stopped, cleanup complete');

                  // Output log file contents
                  if (fs.existsSync(LOG_FILE)) {
                    console.log('\nDeployment log:\n');
                    console.log(fs.readFileSync(LOG_FILE, 'utf8'));
                  }

                  process.exit(0);
                } catch (stopError) {
                  console.error('\nError stopping server:', stopError.message);
                  await stopLocalServer(true); // Force kill

                  // Output log file even on error
                  if (fs.existsSync(LOG_FILE)) {
                    console.log('\nDeployment log:\n');
                    console.log(fs.readFileSync(LOG_FILE, 'utf8'));
                  }

                  process.exit(1);
                }
              }
            } else if (oneShot) {
              // One-shot mode but no request was sent (shouldn't happen, but handle it)
              console.log('\nOne-shot mode: No request was sent, stopping server anyway...');
              await stopLocalServer(false);

              // Output log file
              if (fs.existsSync(LOG_FILE)) {
                console.log('\nDeployment log:\n');
                console.log(fs.readFileSync(LOG_FILE, 'utf8'));
              }

              process.exit(1);
            }
          } else {
            console.log('\nHealth check failed or timeout');
            if (oneShot) {
              console.log('\nOne-shot mode: Stopping server due to health check failure...');
              await stopLocalServer(false);

              // Output log file
              if (fs.existsSync(LOG_FILE)) {
                console.log('\nDeployment log:\n');
                console.log(fs.readFileSync(LOG_FILE, 'utf8'));
              }

              process.exit(1);
            }
          }
        } catch (error) {
          console.error('\nError during health check:', error.message);
          if (oneShot) {
            console.log('\nOne-shot mode: Stopping server due to error...');
            try {
              await stopLocalServer(false);
            } catch (stopError) {
              // Ignore stop errors if we're already exiting
            }

            // Output log file
            if (fs.existsSync(LOG_FILE)) {
              console.log('\nDeployment log:\n');
              console.log(fs.readFileSync(LOG_FILE, 'utf8'));
            }

            process.exit(1);
          }
        }
      }, 10000);
    }

    // Monitor log file for completion or compile-time errors
    const monitorInterval = setInterval(() => {
      if (!fs.existsSync(SHARED_MUTEX_FILE)) {
        clearInterval(monitorInterval);

        // Deployment complete - check for errors
        if (fs.existsSync(LOG_FILE)) {
          const logContent = fs.readFileSync(LOG_FILE, 'utf8');

          // For local deployments, check for different success indicators
          if (isLocalDeployment) {
            // Check for successful local server startup
            const hasLocalSuccess = logContent.includes('Server starting at') ||
                                   logContent.includes('Starting development server') ||
                                   logContent.includes('Local development environment is ready');

            // Check for critical errors
            const hasCriticalError = logContent.includes('Failed') &&
                                    (logContent.includes('ERROR') ||
                                     logContent.includes('failed to start'));

            if (hasCriticalError) {
              console.error('\nLocal deployment failed - check log for details\n');
              if (oneShot) {
                console.log('\nDeployment log:\n');
                console.log(logContent);
              }
              process.exit(1);
            } else if (hasLocalSuccess || logContent.includes('successfully')) {
              // Local deployment successful (one-shot handles its own exit)
              if (!oneShot) {
                console.log('\nLocal server started successfully!\n');
              }
              process.exit(0);
            } else {
              // Status unclear - for one-shot, we already handled success/failure above
              if (!oneShot) {
                console.log('\nDeployment finished - status unclear, check log\n');
              }
              process.exit(0);
            }
          } else {
            // Cloud deployment - check for standard success indicators
            const hasBuildError = logContent.includes('failed') && !logContent.includes('Image built:');
            const hasDeploymentError = logContent.includes('failed') || logContent.includes('ERROR');

            if (hasBuildError || hasDeploymentError) {
              console.error('\nDeployment failed - check log for details\n');
              process.exit(1);
            } else if (logContent.includes('deployed successfully')) {
              console.log('\nDeployment completed successfully!\n');
              process.exit(0);
            } else {
              console.log('\nDeployment finished - status unclear, check log\n');
              process.exit(0);
            }
          }
        } else {
          console.error('\nLog file not found\n');
          process.exit(1);
        }
      }
    }, 2000); // Check every 2 seconds

    // Timeout after 20 minutes
    setTimeout(() => {
      clearInterval(monitorInterval);
      console.error('\nDeployment timeout (20 minutes)\n');
      process.exit(1);
    }, 1200000);
  } else {
    console.log(`
Deployment launched in separate terminal window

Environment: ${environment}
Command: ${makeCmd}
Log file: ${LOG_FILE}

The deployment is running asynchronously. You can:
- Monitor progress in the terminal window
- Read ${LOG_FILE} to check status
- Wait for the mutex file ${SHARED_MUTEX_FILE} to be removed (indicates completion)
`);
  }
})(); // Close async function
