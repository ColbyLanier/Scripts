#!/usr/bin/env node

/**
 * Stop Local Server Utility
 *
 * Gracefully stops local deployment by:
 * 1. Reading state file to get PIDs
 * 2. Sending SIGTERM to process tree
 * 3. Waiting for graceful shutdown
 * 4. Force killing if necessary
 * 5. Cleaning up state file
 */

const path = require('path');
const DEPLOY_DIR = __dirname;
const { getState, clearState, updateState } = require(path.join(DEPLOY_DIR, 'local-server-state'));
const { releaseMutex } = require(path.join(DEPLOY_DIR, 'mutex-helper'));
const { exec } = require('child_process');
const util = require('util');
const execPromise = util.promisify(exec);

/**
 * Kill process tree (parent and children)
 */
async function killProcessTree(pid, signal = 'SIGTERM') {
  if (!pid || pid === 0) return;

  try {
    // Check if process exists
    process.kill(pid, 0);
  } catch (error) {
    // Process doesn't exist
    return;
  }

  // On Unix-like systems, get child processes
  if (process.platform !== 'win32') {
    try {
      // Get all child PIDs
      const { stdout } = await execPromise(`pgrep -P ${pid}`, { encoding: 'utf8' });
      const childPids = stdout.trim().split('\n').filter(Boolean).map(Number);

      // Kill children first
      for (const childPid of childPids) {
        await killProcessTree(childPid, signal);
      }
    } catch (error) {
      // No children or pgrep failed, continue
    }
  }

  // Kill the process itself
  try {
    process.kill(pid, signal);
    console.log(`Sent ${signal} to PID ${pid}`);
  } catch (error) {
    console.warn(`Warning: Could not send ${signal} to PID ${pid}: ${error.message}`);
  }
}

/**
 * Wait for process to exit
 */
async function waitForProcessExit(pid, timeout = 5000) {
  const startTime = Date.now();

  while (Date.now() - startTime < timeout) {
    try {
      process.kill(pid, 0);
      await new Promise(resolve => setTimeout(resolve, 100));
    } catch (error) {
      // Process exited
      return true;
    }
  }

  return false;
}

function isProcessRunning(pid) {
  if (!pid || pid === 0) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return false;
  }
}

function addProcessTarget(map, pid, name) {
  const numericPid = Number(pid);
  if (!numericPid || Number.isNaN(numericPid) || numericPid <= 0) {
    return;
  }

  if (map.has(numericPid)) {
    const existing = map.get(numericPid);
    if (!existing.name.includes(name)) {
      existing.name += `, ${name}`;
    }
    return;
  }

  map.set(numericPid, { pid: numericPid, name });
}

/**
 * Stop local server
 */
async function stopLocalServer(force = false) {
  const state = getState();

  if (!state) {
    console.log('No local server state found - nothing to stop');
    return;
  }

  console.log(`Stopping local server (${state.environment})...`);
  console.log(`   Started: ${state.startedAt}`);

  const pids = state.pids || {};
  const processMap = new Map();
  addProcessTarget(processMap, pids.app, 'app');
  if (pids.make) {
    addProcessTarget(processMap, pids.make, 'make');
  } else if (pids.wrapper && pids.wrapper !== process.ppid) {
    // Fallback: kill wrapper if make PID is unavailable and this script is not wrapper-owned
    addProcessTarget(processMap, pids.wrapper, 'wrapper');
  }
  addProcessTarget(processMap, pids.ngrok, 'ngrok');

  const processesToKill = Array.from(processMap.values());

  if (processesToKill.length === 0) {
    console.log('No process IDs found in state file');
    clearState();
    releaseMutex(true);
    return;
  }

  console.log('\nTargeting processes:');
  processesToKill.forEach(({ pid, name }) => {
    console.log(`   - ${name} (PID ${pid})`);
  });

  if (force) {
    console.log('\nForce killing all processes...');
    for (const { pid, name } of processesToKill) {
      console.log(`   Force killing ${name} (PID ${pid})`);
      await killProcessTree(pid, 'SIGKILL');
    }
  } else {
    console.log('\nSending SIGTERM to processes...');
    for (const { pid, name } of processesToKill) {
      await killProcessTree(pid, 'SIGTERM');
    }

    console.log('\nWaiting for graceful shutdown (up to 5 seconds per process)...');
    const stillRunning = [];
    for (const { pid, name } of processesToKill) {
      const exited = await waitForProcessExit(pid, 5000);
      if (!exited) {
        stillRunning.push({ pid, name });
      }
    }

    if (stillRunning.length > 0) {
      console.log('\nSome processes did not exit, sending SIGKILL...');
      for (const { pid, name } of stillRunning) {
        console.log(`   Force killing ${name} (PID ${pid})`);
        await killProcessTree(pid, 'SIGKILL');
      }
    }
  }

  const stubbornProcesses = [];
  for (const target of processesToKill) {
    if (isProcessRunning(target.pid)) {
      stubbornProcesses.push(target);
    }
  }

  if (stubbornProcesses.length > 0) {
    updateState({
      status: 'stop_failed',
      lastStopError: `Unable to stop: ${stubbornProcesses.map(p => `${p.name} (${p.pid})`).join(', ')}`,
      lastStopAttemptAt: new Date().toISOString()
    });
    console.error('\nFailed to stop all processes. Leaving mutex and state file intact for manual cleanup.');
    stubbornProcesses.forEach(({ pid, name }) => {
      console.error(`   Still running: ${name} (PID ${pid})`);
    });
    throw new Error('Stop operation incomplete');
  }

  // Clean up ngrok if still running (by port)
  try {
    if (process.platform !== 'win32') {
      await execPromise('pkill -f "ngrok http 8080" || true');
    }
  } catch (error) {
    // Ignore errors
  }

  clearState();
  releaseMutex(true);

  console.log('\nLocal server stopped');
  console.log('State file cleared');
  console.log('Mutex released');
}

// CLI interface
if (require.main === module) {
  const force = process.argv.includes('--force') || process.argv.includes('-f');
  stopLocalServer(force).catch(error => {
    console.error('Error stopping local server:', error);
    process.exit(1);
  });
}

module.exports = { stopLocalServer, killProcessTree };
