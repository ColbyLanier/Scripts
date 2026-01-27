#!/usr/bin/env node

/**
 * Wait for Health Check Utility
 *
 * Polls the health endpoint until the server is ready, then optionally
 * sends a configured request.
 */

const path = require('path');
const DEPLOY_DIR = __dirname;
const { getState, markReady, markRequestSent, getRequestUrl, waitForNgrokUrl, updateNgrokUrl } = require(path.join(DEPLOY_DIR, 'local-server-state'));
const { sendLocalRequest } = require(path.join(DEPLOY_DIR, 'send-local-request'));
const http = require('http');
const https = require('https');

const HEALTH_CHECK_INTERVAL = 2000; // 2 seconds
const MAX_WAIT_TIME = 120000; // 2 minutes
const HEALTH_ENDPOINT = '/health';

/**
 * Check if server is healthy
 */
async function checkHealth(url) {
  return new Promise((resolve) => {
    const isHttps = url.startsWith('https://');
    const client = isHttps ? https : http;

    const req = client.get(`${url}${HEALTH_ENDPOINT}`, { timeout: 5000 }, (res) => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => {
        try {
          const parsed = JSON.parse(data);
          // Server is healthy if status code is 200 and status is "healthy"
          const isHealthy = res.statusCode === 200 && parsed.status === 'healthy';
          resolve({ healthy: isHealthy, response: parsed });
        } catch (error) {
          resolve({ healthy: false, error: 'Invalid JSON response' });
        }
      });
    });

    req.on('error', () => resolve({ healthy: false, error: 'Connection error' }));
    req.on('timeout', () => {
      req.destroy();
      resolve({ healthy: false, error: 'Timeout' });
    });
  });
}

/**
 * Wait for server to become healthy
 */
async function waitForHealth(baseUrl, options = {}) {
  const {
    maxWaitTime = MAX_WAIT_TIME,
    interval = HEALTH_CHECK_INTERVAL,
    verbose = true
  } = options;

  const startTime = Date.now();
  let attempt = 0;

  if (verbose) {
    console.log(`Waiting for server health check at ${baseUrl}${HEALTH_ENDPOINT}...`);
  }

  while (Date.now() - startTime < maxWaitTime) {
    attempt++;

    if (verbose && attempt % 5 === 0) {
      const elapsed = Math.round((Date.now() - startTime) / 1000);
      const remaining = Math.round((maxWaitTime - (Date.now() - startTime)) / 1000);
      process.stdout.write(`\rHealth check attempt ${attempt} (${elapsed}s elapsed, ${remaining}s remaining)...`);
    }

    const result = await checkHealth(baseUrl);

    if (result.healthy) {
      if (verbose) {
        console.log(`\nServer is healthy!`);
        console.log(`   Status: ${result.response.status}`);
        if (result.response.services) {
          console.log(`   Services:`, JSON.stringify(result.response.services, null, 2));
        }
      }
      return { success: true, response: result.response };
    }

    // Wait before next check
    await new Promise(resolve => setTimeout(resolve, interval));
  }

  if (verbose) {
    console.log(`\nHealth check timeout after ${Math.round(maxWaitTime / 1000)} seconds`);
  }

  return { success: false, error: 'Timeout waiting for health check' };
}

/**
 * Wait for health and send pending request
 * @param {boolean} preferNgrok - If true, prefer ngrok URL for requests (default: true)
 */
async function waitForHealthAndSendRequest(preferNgrok = true) {
  const state = getState();

  if (!state) {
    console.error('No server state found');
    return { success: false, error: 'No state' };
  }

  // Wait for ngrok URL if preferred (give it up to 15 seconds)
  if (preferNgrok) {
    console.log('Waiting for ngrok URL...');
    const ngrokUrl = await waitForNgrokUrl(15000, 2000);
    if (ngrokUrl) {
      console.log(`ngrok URL detected: ${ngrokUrl}`);
    } else {
      console.log('ngrok URL not available, using localhost');
    }
  }

  // Get the best URL to use (prefers ngrok, falls back to localhost)
  const baseUrl = getRequestUrl(preferNgrok);
  const urlType = baseUrl.includes('ngrok') ? 'ngrok' : 'localhost';

  console.log(`Health check will use: ${urlType} (${baseUrl})`);

  // Wait for health check
  const healthResult = await waitForHealth(baseUrl);

  if (!healthResult.success) {
    // If ngrok failed and we prefer ngrok, try localhost as fallback
    if (preferNgrok && urlType === 'ngrok') {
      console.log('Health check via ngrok failed, trying localhost fallback...');
      const localhostUrl = getRequestUrl(false);
      const fallbackResult = await waitForHealth(localhostUrl);
      if (fallbackResult.success) {
        console.log('Health check via localhost succeeded');
        markReady();
        return { success: true, health: fallbackResult.response, url: localhostUrl, urlType: 'localhost' };
      }
    }
    return healthResult;
  }

  // Mark server as ready
  markReady();

  // Check if there's a pending request
  if (state.pendingRequest && !state.requestSent) {
    console.log('\nSending pending request...');
    console.log(`   Endpoint: ${state.pendingRequest.endpoint}`);
    console.log(`   Method: ${state.pendingRequest.method || 'GET'}`);
    console.log(`   URL: ${urlType} (${baseUrl})`);

    try {
      const response = await sendLocalRequest(
        state.pendingRequest.endpoint,
        {
          method: state.pendingRequest.method || 'GET',
          body: state.pendingRequest.body,
          headers: state.pendingRequest.headers
        },
        preferNgrok
      );

      markRequestSent();

      console.log('\nRequest sent successfully');
      return {
        success: true,
        health: healthResult.response,
        url: baseUrl,
        urlType: urlType,
        request: {
          endpoint: state.pendingRequest.endpoint,
          response: response
        }
      };
    } catch (error) {
      console.error(`\nFailed to send request: ${error.message}`);
      return {
        success: false,
        health: healthResult.response,
        url: baseUrl,
        urlType: urlType,
        error: `Request failed: ${error.message}`
      };
    }
  }

  return { success: true, health: healthResult.response, url: baseUrl, urlType: urlType };
}

// CLI interface
if (require.main === module) {
  const args = process.argv.slice(2);

  if (args.includes('--send-request')) {
    waitForHealthAndSendRequest().then(result => {
      if (result.success) {
        process.exit(0);
      } else {
        process.exit(1);
      }
    }).catch(error => {
      console.error('Error:', error);
      process.exit(1);
    });
  } else {
    const url = args[0] || 'http://localhost:8080';
    waitForHealth(url).then(result => {
      process.exit(result.success ? 0 : 1);
    }).catch(error => {
      console.error('Error:', error);
      process.exit(1);
    });
  }
}

module.exports = { waitForHealth, waitForHealthAndSendRequest, checkHealth };
