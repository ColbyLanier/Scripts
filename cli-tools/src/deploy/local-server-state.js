#!/usr/bin/env node

/**
 * Local Server State Tracker
 *
 * Manages state file for local deployments including:
 * - Process IDs (app, ngrok)
 * - Server URLs (local, ngrok)
 * - Port information
 * - Start timestamps
 * - Log file paths
 */

const fs = require('fs');
const path = require('path');

// State file is in project root
const STATE_FILE = path.join(process.cwd(), '.claude-local-server-state.json');

/**
 * Get current state
 */
function getState() {
  if (!fs.existsSync(STATE_FILE)) {
    return null;
  }

  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  } catch (error) {
    console.warn('Warning: Corrupted state file, treating as empty');
    return null;
  }
}

/**
 * Write state to file
 */
function setState(state) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

/**
 * Update state with new information
 */
function updateState(updates) {
  const current = getState() || {};
  const newState = { ...current, ...updates, updatedAt: new Date().toISOString() };
  setState(newState);
  return newState;
}

/**
 * Clear state
 */
function clearState() {
  if (fs.existsSync(STATE_FILE)) {
    fs.unlinkSync(STATE_FILE);
  }
}

/**
 * Get ngrok URL from ngrok API
 */
function getNgrokUrl() {
  try {
    const response = require('child_process').execSync(
      'curl -s http://127.0.0.1:4040/api/tunnels',
      { encoding: 'utf8', timeout: 5000 }
    );
    const data = JSON.parse(response);
    const tunnel = data.tunnels?.find(t => t.proto === 'https');
    return tunnel?.public_url || null;
  } catch (error) {
    return null;
  }
}

/**
 * Initialize state for local deployment
 */
function initializeState(environment, flag, oneShot = false) {
  const projectDir = process.cwd();
  const state = {
    environment,
    flag,
    oneShot,
    startedAt: new Date().toISOString(),
    localUrl: 'http://localhost:8080',
    port: 8080,
    pids: {
      app: null,
      ngrok: null,
      make: null,
      wrapper: process.pid
    },
    logFile: path.join(projectDir, '.claude-deploy.log'),
    status: 'starting'
  };

  setState(state);
  return state;
}

/**
 * Update process IDs
 */
function updatePids(pids) {
  const state = getState();
  if (!state) return;

  state.pids = { ...state.pids, ...pids };
  state.updatedAt = new Date().toISOString();
  setState(state);
}

/**
 * Update ngrok URL and return it
 */
function updateNgrokUrl() {
  const url = getNgrokUrl();
  if (url) {
    updateState({ ngrokUrl: url, webhookUrl: `${url}/webhook` });
  }
  return url;
}

/**
 * Get the best URL to use for requests (prefers ngrok, falls back to localhost)
 * @param {boolean} preferNgrok - If true, prefer ngrok URL; if false, prefer localhost
 * @returns {string} URL to use for requests
 */
function getRequestUrl(preferNgrok = true) {
  const state = getState();
  if (!state) {
    return 'http://localhost:8080';
  }

  if (preferNgrok && state.ngrokUrl) {
    return state.ngrokUrl;
  }

  // Try to get ngrok URL if not in state
  if (preferNgrok) {
    const ngrokUrl = updateNgrokUrl();
    if (ngrokUrl) {
      return ngrokUrl;
    }
  }

  return state.localUrl || 'http://localhost:8080';
}

/**
 * Wait for ngrok URL to become available
 * @param {number} maxWaitTime - Maximum time to wait in milliseconds
 * @param {number} interval - Polling interval in milliseconds
 * @returns {Promise<string|null>} ngrok URL or null if timeout
 */
function waitForNgrokUrl(maxWaitTime = 30000, interval = 2000) {
  return new Promise((resolve) => {
    const startTime = Date.now();

    const checkNgrok = () => {
      const url = getNgrokUrl();
      if (url) {
        updateNgrokUrl();
        resolve(url);
        return;
      }

      if (Date.now() - startTime >= maxWaitTime) {
        resolve(null);
        return;
      }

      setTimeout(checkNgrok, interval);
    };

    checkNgrok();
  });
}

/**
 * Mark server as running
 */
function markRunning() {
  updateState({ status: 'running' });
}

/**
 * Mark server as ready (health checks passed)
 */
function markReady() {
  updateState({
    status: 'ready',
    readyAt: new Date().toISOString()
  });
}

/**
 * Set pending request to send after health checks
 */
function setPendingRequest(requestConfig) {
  updateState({
    pendingRequest: requestConfig,
    requestSent: false
  });
}

/**
 * Mark request as sent
 */
function markRequestSent() {
  updateState({
    requestSent: true,
    requestSentAt: new Date().toISOString()
  });
}

/**
 * Mark server as stopped
 */
function markStopped() {
  const state = getState();
  if (state) {
    updateState({
      status: 'stopped',
      stoppedAt: new Date().toISOString()
    });
  }
}

module.exports = {
  getState,
  setState,
  updateState,
  clearState,
  initializeState,
  updatePids,
  updateNgrokUrl,
  getNgrokUrl,
  getRequestUrl,
  waitForNgrokUrl,
  markRunning,
  markReady,
  markStopped,
  setPendingRequest,
  markRequestSent,
  STATE_FILE
};
