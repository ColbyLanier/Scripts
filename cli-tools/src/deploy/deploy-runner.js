#!/usr/bin/env node

/**
 * Deploy Runner with Listr2
 *
 * Wraps `make deploy` with a clean Listr2 task display.
 * Parses make deploy output to show progress updates.
 *
 * Usage:
 *   node deploy-runner.js <environment> [flag] [projectDir]
 */

const { Listr } = require('listr2');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const chalk = require('chalk');

// Parse arguments
const args = process.argv.slice(2);
const environment = args[0] || 'development';
const flag = args[1] || '';
const projectDir = args[2] || process.cwd();

// Configuration
const LOG_FILE = path.join(projectDir, '.claude-deploy.log');
const SIGNAL_FILE = path.join(projectDir, '.claude-deploy-signal');
const DEPLOY_DIR = __dirname;

// State tracking
let logStream = null;
let currentPhase = 'initializing';
let buildStep = { current: 0, total: 0 };
let pushProgress = '';

/**
 * Write to log file
 */
function log(message) {
  if (logStream && message) {
    logStream.write(message);
  }
}

/**
 * Parse output line to detect current phase
 */
function parseOutputLine(line) {
  // Docker build progress
  const stepMatch = line.match(/Step (\d+)\/(\d+)/i);
  if (stepMatch) {
    buildStep.current = parseInt(stepMatch[1]);
    buildStep.total = parseInt(stepMatch[2]);
    return { phase: 'building', detail: `Step ${buildStep.current}/${buildStep.total}` };
  }

  // Docker push progress
  if (line.includes('Pushing') || line.includes('Layer already exists')) {
    return { phase: 'pushing', detail: 'Pushing layers...' };
  }
  if (line.includes('digest:') || line.includes('sha256:')) {
    return { phase: 'pushing', detail: 'Push complete' };
  }

  // Cloud Run deployment
  if (line.includes('Deploying') || line.includes('gcloud run services replace')) {
    return { phase: 'deploying', detail: 'Updating Cloud Run...' };
  }
  if (line.includes('Revision') || line.includes('Service URL')) {
    return { phase: 'deploying', detail: 'Deployment complete' };
  }

  // Validation
  if (line.includes('Validating') || line.includes('prerequisites')) {
    return { phase: 'validating', detail: 'Checking prerequisites...' };
  }

  // Build started
  if (line.includes('Building') && line.includes('image')) {
    return { phase: 'building', detail: 'Starting build...' };
  }

  // Getting digest
  if (line.includes('digest') || line.includes('Getting image')) {
    return { phase: 'pushing', detail: 'Getting image digest...' };
  }

  return null;
}

/**
 * Run make deploy and track progress
 */
async function runMakeDeploy(ctx) {
  return new Promise((resolve, reject) => {
    const makeArgs = ['deploy'];
    const env = { ...process.env };

    if (environment) {
      env.ENVIRONMENT = environment;
    }
    if (flag) {
      env.FLAG = flag;
    }

    const proc = spawn('make', makeArgs, {
      cwd: projectDir,
      env: env,
      stdio: ['ignore', 'pipe', 'pipe']
    });

    let stdout = '';
    let stderr = '';

    proc.stdout.on('data', (data) => {
      const text = data.toString();
      stdout += text;
      log(text);

      // Parse each line for progress
      text.split('\n').forEach(line => {
        const parsed = parseOutputLine(line);
        if (parsed) {
          currentPhase = parsed.phase;
          ctx.phaseDetail = parsed.detail;
        }
      });
    });

    proc.stderr.on('data', (data) => {
      const text = data.toString();
      stderr += text;
      log(text);
    });

    proc.on('close', (code) => {
      ctx.exitCode = code;
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        const error = new Error(`make deploy failed with code ${code}`);
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
      }
    });

    proc.on('error', reject);
  });
}

/**
 * Create the task list
 */
function createTasks() {
  const skipBuild = flag.includes('p') || flag.includes('y');
  const skipPush = flag.includes('y');

  return new Listr([
    {
      title: 'Validate prerequisites',
      task: async (ctx, task) => {
        // Wait for validation phase
        await new Promise(resolve => setTimeout(resolve, 500));
        task.title = 'Prerequisites validated';
      },
      options: { persistentOutput: false }
    },
    {
      title: 'Build Docker image',
      skip: () => skipBuild ? 'Using existing image' : false,
      task: async (ctx, task) => {
        // Wait until we detect build progress
        while (currentPhase !== 'building' && currentPhase !== 'pushing' && currentPhase !== 'deploying') {
          task.title = ctx.phaseDetail || 'Preparing build...';
          await new Promise(resolve => setTimeout(resolve, 500));
          if (ctx.exitCode !== undefined) break;
        }

        // Show build progress
        while (currentPhase === 'building') {
          if (buildStep.total > 0) {
            task.title = `Building image (${buildStep.current}/${buildStep.total})...`;
          } else {
            task.title = ctx.phaseDetail || 'Building...';
          }
          await new Promise(resolve => setTimeout(resolve, 300));
          if (ctx.exitCode !== undefined) break;
        }

        task.title = 'Docker image built';
      },
      options: { persistentOutput: false }
    },
    {
      title: 'Push to registry',
      skip: () => skipPush ? 'Using already pushed image' : false,
      task: async (ctx, task) => {
        // Wait for push phase
        while (currentPhase !== 'pushing' && currentPhase !== 'deploying') {
          await new Promise(resolve => setTimeout(resolve, 300));
          if (ctx.exitCode !== undefined) break;
        }

        // Show push progress
        while (currentPhase === 'pushing') {
          task.title = ctx.phaseDetail || 'Pushing...';
          await new Promise(resolve => setTimeout(resolve, 300));
          if (ctx.exitCode !== undefined) break;
        }

        task.title = 'Image pushed to registry';
      },
      options: { persistentOutput: false }
    },
    {
      title: 'Deploy to Cloud Run',
      task: async (ctx, task) => {
        // Wait for deploy phase
        while (currentPhase !== 'deploying' && ctx.exitCode === undefined) {
          await new Promise(resolve => setTimeout(resolve, 300));
        }

        // Show deploy progress
        while (currentPhase === 'deploying' && ctx.exitCode === undefined) {
          task.title = ctx.phaseDetail || 'Deploying...';
          await new Promise(resolve => setTimeout(resolve, 300));
        }

        if (ctx.exitCode === 0) {
          task.title = 'Deployed to Cloud Run';
        }
      },
      options: { persistentOutput: false }
    }
  ], {
    concurrent: false,
    exitOnError: false,
    rendererOptions: {
      collapseSubtasks: false,
      showTimer: true,
      formatOutput: 'wrap'
    }
  });
}

/**
 * Main function
 */
async function main() {
  console.log(chalk.bold('\n  Claude Code Deployment\n'));
  console.log(`  ${chalk.gray('Environment:')} ${chalk.cyan(environment)}`);
  console.log(`  ${chalk.gray('Project:')} ${chalk.cyan(path.basename(projectDir))}`);
  if (flag) console.log(`  ${chalk.gray('Flags:')} ${chalk.cyan(flag)}`);
  console.log('');

  // Initialize log file
  logStream = fs.createWriteStream(LOG_FILE, { flags: 'w' });
  log(`[${new Date().toISOString()}] Deployment started: environment=${environment}, flag=${flag}\n`);

  const tasks = createTasks();
  const ctx = { phaseDetail: '', exitCode: undefined };

  try {
    // Start make deploy in background
    const deployPromise = runMakeDeploy(ctx);

    // Run task display (this updates based on ctx changes)
    await tasks.run(ctx);

    // Wait for make deploy to complete
    await deployPromise;

    console.log('');
    console.log(chalk.green.bold('  Deployment completed successfully'));
    console.log('');

  } catch (error) {
    // Wait a moment for tasks to finish updating
    await new Promise(resolve => setTimeout(resolve, 500));

    console.log('');
    console.log(chalk.red.bold('  Deployment failed'));
    if (error.message) {
      console.log(chalk.red(`  ${error.message}`));
    }
    console.log(chalk.gray(`  See log for details: ${LOG_FILE}`));
    console.log('');

    log(`\n[${new Date().toISOString()}] ERROR: ${error.message}\n`);

    if (logStream) logStream.end();

    // Run log deduplication
    try {
      require(path.join(DEPLOY_DIR, 'deduplicate-log.js'));
    } catch (e) {
      // Ignore
    }

    process.exit(1);
  }

  if (logStream) logStream.end();

  // Run log deduplication
  try {
    require(path.join(DEPLOY_DIR, 'deduplicate-log.js'));
  } catch (e) {
    // Ignore
  }
}

/**
 * Cleanup function - always release mutex
 */
function cleanup() {
  // Close log stream
  if (logStream) {
    try { logStream.end(); } catch (e) {}
  }

  // Release mutex
  try {
    if (fs.existsSync(SIGNAL_FILE)) {
      fs.unlinkSync(SIGNAL_FILE);
      console.log(chalk.gray('  Mutex released'));
    }
  } catch (e) {
    // Ignore cleanup errors
  }
}

// Handle signals - always cleanup
process.on('SIGINT', () => {
  console.log('\n  Deployment interrupted');
  cleanup();
  process.exit(130);
});

process.on('SIGTERM', () => {
  cleanup();
  process.exit(143);
});

process.on('exit', () => {
  cleanup();
});

// Run
main().catch(error => {
  console.error('Unexpected error:', error);
  cleanup();
  process.exit(1);
});
