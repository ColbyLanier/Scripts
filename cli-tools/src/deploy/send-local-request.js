#!/usr/bin/env node

/**
 * Send Request to Local Server
 *
 * Utility to send HTTP requests to the locally running server.
 * Automatically detects server URL from state file or uses defaults.
 */

const path = require('path');
const DEPLOY_DIR = __dirname;
const { getState, getRequestUrl, updateNgrokUrl } = require(path.join(DEPLOY_DIR, 'local-server-state'));
const http = require('http');
const https = require('https');
const { URL } = require('url');

/**
 * Send HTTP request
 */
function sendRequest(options) {
  return new Promise((resolve, reject) => {
    const url = new URL(options.url);
    const isHttps = url.protocol === 'https:';
    const client = isHttps ? https : http;

    const requestOptions = {
      hostname: url.hostname,
      port: url.port || (isHttps ? 443 : 80),
      path: url.pathname + url.search,
      method: options.method || 'GET',
      headers: {
        'Content-Type': 'application/json',
        ...options.headers
      },
      timeout: options.timeout || 30000
    };

    if (options.body) {
      const bodyString = typeof options.body === 'string'
        ? options.body
        : JSON.stringify(options.body);
      requestOptions.headers['Content-Length'] = Buffer.byteLength(bodyString);

      const req = client.request(requestOptions, (res) => {
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          try {
            const parsed = JSON.parse(data);
            resolve({ status: res.statusCode, headers: res.headers, body: parsed });
          } catch (error) {
            resolve({ status: res.statusCode, headers: res.headers, body: data });
          }
        });
      });

      req.on('error', reject);
      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Request timeout'));
      });

      req.write(bodyString);
      req.end();
    } else {
      const req = client.request(requestOptions, (res) => {
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          try {
            const parsed = JSON.parse(data);
            resolve({ status: res.statusCode, headers: res.headers, body: parsed });
          } catch (error) {
            resolve({ status: res.statusCode, headers: res.headers, body: data });
          }
        });
      });

      req.on('error', reject);
      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Request timeout'));
      });

      req.end();
    }
  });
}

/**
 * Send request to local server
 * @param {string} endpoint - Endpoint path (e.g., '/health') or full URL
 * @param {object} options - Request options (method, body, headers)
 * @param {boolean} preferNgrok - If true, prefer ngrok URL (default: true)
 */
async function sendLocalRequest(endpoint, options = {}, preferNgrok = true) {
  // Update ngrok URL if not in state and we prefer ngrok
  if (preferNgrok) {
    updateNgrokUrl();
  }

  // Get the best URL to use (prefers ngrok, falls back to localhost)
  const baseUrl = getRequestUrl(preferNgrok);
  const urlType = baseUrl.includes('ngrok') ? 'ngrok' : 'localhost';

  const url = endpoint.startsWith('http') ? endpoint : `${baseUrl}${endpoint}`;

  console.log(`Sending ${options.method || 'GET'} request to ${url}`);
  console.log(`   Using: ${urlType} (${baseUrl})`);

  try {
    const response = await sendRequest({ ...options, url });
    console.log(`Response status: ${response.status}`);
    console.log(`Response body:`, JSON.stringify(response.body, null, 2));
    return response;
  } catch (error) {
    // If ngrok failed and we prefer ngrok, try localhost as fallback
    if (preferNgrok && urlType === 'ngrok') {
      console.log(`Request via ngrok failed, trying localhost fallback...`);
      const localhostUrl = getRequestUrl(false);
      const fallbackUrl = endpoint.startsWith('http') ? endpoint : `${localhostUrl}${endpoint}`;
      try {
        const response = await sendRequest({ ...options, url: fallbackUrl });
        console.log(`Request via localhost succeeded`);
        console.log(`Response status: ${response.status}`);
        console.log(`Response body:`, JSON.stringify(response.body, null, 2));
        return response;
      } catch (fallbackError) {
        console.error(`Request failed on both ngrok and localhost: ${fallbackError.message}`);
        throw fallbackError;
      }
    }
    console.error(`Request failed: ${error.message}`);
    throw error;
  }
}

// CLI interface
if (require.main === module) {
  const args = process.argv.slice(2);

  if (args.length === 0) {
    console.log('Usage: node send-local-request.js <endpoint> [method] [body]');
    console.log('');
    console.log('Examples:');
    console.log('  node send-local-request.js /health');
    console.log('  node send-local-request.js /webhook POST \'{"text":"hello"}\'');
    process.exit(1);
  }

  const endpoint = args[0];
  const method = args[1] || 'GET';
  const body = args[2] ? JSON.parse(args[2]) : undefined;

  sendLocalRequest(endpoint, { method, body }).catch(error => {
    console.error('Error:', error);
    process.exit(1);
  });
}

module.exports = { sendLocalRequest, sendRequest };
