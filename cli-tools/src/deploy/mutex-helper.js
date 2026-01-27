#!/usr/bin/env node

/**
 * Shared Mutex Helper for Multi-Repo Deployment Coordination
 *
 * This utility provides mutex management for coordinating deployments across
 * multiple repositories (ProcurementAgentAI and google-chat-sandbox).
 *
 * Features:
 * - Shared mutex file location in project root
 * - Timeout/polling system (20 min timeout, 30 sec poll interval)
 * - Detailed metadata tracking (repo, environment, timestamp, PID)
 * - Graceful waiting and error handling
 *
 * Usage:
 *   const { acquireMutex, waitForMutex, releaseMutex } = require('./mutex-helper');
 *
 *   // Wait for mutex to be available, then acquire
 *   await waitForMutex('ProcurementAgentAI', 'development');
 *
 *   // Or try to acquire immediately
 *   const acquired = acquireMutex('ProcurementAgentAI', 'development');
 */

const fs = require('fs');
const path = require('path');

// Shared mutex location - in the current project directory
const SHARED_MUTEX_FILE = path.join(process.cwd(), '.claude-deploy-signal');

// Timeout and polling configuration
const MAX_WAIT_TIME_MS = 20 * 60 * 1000; // 20 minutes
const POLL_INTERVAL_MS = 30 * 1000;      // 30 seconds

/**
 * Check if mutex exists and return its metadata
 * @returns {Object|null} Mutex metadata or null if not locked
 */
function checkMutex() {
  if (!fs.existsSync(SHARED_MUTEX_FILE)) {
    return null;
  }

  try {
    const content = fs.readFileSync(SHARED_MUTEX_FILE, 'utf8');
    return JSON.parse(content);
  } catch (error) {
    // Corrupted mutex file - treat as unlocked
    console.warn('Warning: Corrupted mutex file, treating as unlocked');
    return null;
  }
}

/**
 * Acquire the shared mutex
 * @param {string} repoName - Name of the repository acquiring the mutex
 * @param {string} environment - Deployment environment (development/production/sandbox)
 * @param {string} flag - Optional deployment flag
 * @returns {boolean} True if mutex was acquired, false if already locked
 */
function acquireMutex(repoName, environment, flag = '') {
  const existing = checkMutex();

  if (existing) {
    return false;
  }

  const metadata = {
    repo: repoName,
    environment,
    flag,
    timestamp: new Date().toISOString(),
    pid: process.pid,
    mutexLocation: SHARED_MUTEX_FILE
  };

  fs.writeFileSync(SHARED_MUTEX_FILE, JSON.stringify(metadata, null, 2));
  return true;
}

/**
 * Release the shared mutex
 * @param {boolean} force - Force release even if owned by another process
 */
function releaseMutex(force = false) {
  if (!fs.existsSync(SHARED_MUTEX_FILE)) {
    return;
  }

  if (!force) {
    const metadata = checkMutex();
    if (metadata && metadata.pid !== process.pid) {
      console.warn(`Warning: Mutex owned by different process (PID ${metadata.pid})`);
      return;
    }
  }

  fs.unlinkSync(SHARED_MUTEX_FILE);
}

/**
 * Wait for mutex to become available, then acquire it
 * @param {string} repoName - Name of the repository acquiring the mutex
 * @param {string} environment - Deployment environment
 * @param {string} flag - Optional deployment flag
 * @param {boolean} verbose - Show detailed waiting messages
 * @returns {Promise<boolean>} True if acquired, false if timeout
 */
function waitForMutex(repoName, environment, flag = '', verbose = true) {
  return new Promise((resolve) => {
    // Try immediate acquisition
    if (acquireMutex(repoName, environment, flag)) {
      if (verbose) {
        console.log(`Mutex acquired by ${repoName}`);
      }
      resolve(true);
      return;
    }

    const startTime = Date.now();
    const existingMutex = checkMutex();

    if (verbose && existingMutex) {
      console.log(`Deployment already in progress:`);
      console.log(`   Repo: ${existingMutex.repo}`);
      console.log(`   Environment: ${existingMutex.environment}`);
      console.log(`   Started: ${existingMutex.timestamp}`);
      console.log(`   PID: ${existingMutex.pid}`);
      console.log(`\nWaiting for deployment to complete (timeout: 20 minutes)...`);
    }

    // Poll for mutex availability
    const pollInterval = setInterval(() => {
      const elapsed = Date.now() - startTime;

      // Check for timeout
      if (elapsed >= MAX_WAIT_TIME_MS) {
        clearInterval(pollInterval);
        if (verbose) {
          console.error(`\nTimeout waiting for mutex (20 minutes elapsed)`);
          const currentMutex = checkMutex();
          if (currentMutex) {
            console.error(`   Still locked by: ${currentMutex.repo} (${currentMutex.environment})`);
            console.error(`   Consider manually removing: ${SHARED_MUTEX_FILE}`);
          }
        }
        resolve(false);
        return;
      }

      // Try to acquire mutex
      if (acquireMutex(repoName, environment, flag)) {
        clearInterval(pollInterval);
        if (verbose) {
          const waitSeconds = Math.round(elapsed / 1000);
          console.log(`\nMutex acquired after ${waitSeconds} seconds`);
        }
        resolve(true);
        return;
      }

      // Show progress
      if (verbose) {
        const remainingMs = MAX_WAIT_TIME_MS - elapsed;
        const remainingMinutes = Math.ceil(remainingMs / 60000);
        process.stdout.write(`\rStill waiting... (${remainingMinutes} minutes remaining)`);
      }
    }, POLL_INTERVAL_MS);
  });
}

/**
 * Get formatted mutex status for display
 * @returns {string} Human-readable mutex status
 */
function getMutexStatus() {
  const mutex = checkMutex();

  if (!mutex) {
    return 'No deployment in progress (mutex available)';
  }

  const elapsed = Date.now() - new Date(mutex.timestamp).getTime();
  const elapsedMinutes = Math.round(elapsed / 60000);

  return `Deployment in progress:
   Repository: ${mutex.repo}
   Environment: ${mutex.environment}
   Started: ${mutex.timestamp}
   Duration: ${elapsedMinutes} minutes
   PID: ${mutex.pid}`;
}

module.exports = {
  checkMutex,
  acquireMutex,
  releaseMutex,
  waitForMutex,
  getMutexStatus,
  SHARED_MUTEX_FILE,
  MAX_WAIT_TIME_MS,
  POLL_INTERVAL_MS
};
