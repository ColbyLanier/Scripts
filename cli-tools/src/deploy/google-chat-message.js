/**
 * Google Chat Message Payload Builder
 *
 * Library module for generating Google Chat webhook payloads.
 * Used by trigger-deploy.js via --google-chat-message parameter.
 *
 * Usage (via deploy CLI):
 *   deploy development -l --blocking --one-shot \
 *     --google-chat-message "hello world"
 *
 * Or via cli-tools:
 *   google-chat-message "hello" --one-shot
 */

const fs = require('fs');
const path = require('path');

const TEMPLATE_PATH = path.join(__dirname, 'payloads', 'google-chat-message.json');

// Local testing uses the isolated local-only endpoint that bypasses auth
// Production endpoint (/webhooks/webhook) requires valid JWT tokens
const LOCAL_WEBHOOK_ENDPOINT = '/api/local/webhook';
const PRODUCTION_WEBHOOK_ENDPOINT = '/webhooks/webhook';

/**
 * Generate a random message ID (simulates Google's message IDs)
 */
function generateMessageId() {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let result = '';
  for (let i = 0; i < 11; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

/**
 * Generate a mock JWT token for local testing
 * NOTE: This is NOT a cryptographically valid JWT - it's for local development testing only.
 * The signature is properly base64url encoded but not cryptographically signed.
 */
function generateMockToken(audience) {
  const header = Buffer.from(JSON.stringify({ alg: 'RS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({
    aud: audience,
    azp: '113421852997393319348',
    email: 'service-227975563@gcp-sa-gsuiteaddons.iam.gserviceaccount.com',
    email_verified: true,
    exp: Math.floor(Date.now() / 1000) + 3600,
    iat: Math.floor(Date.now() / 1000),
    iss: 'https://accounts.google.com',
    sub: '113421852997393319348'
  })).toString('base64url');
  // Mock signature - base64url encoded placeholder (256 bytes = RS256 signature size)
  // This is NOT cryptographically valid but passes base64 validation
  const mockSignatureBytes = Buffer.alloc(256, 0xAB);
  const signature = mockSignatureBytes.toString('base64url');
  return `${header}.${payload}.${signature}`;
}

/**
 * Build a Google Chat webhook payload
 * @param {string} messageText - The message text to send
 * @param {object} options - Optional configuration
 * @param {string} options.userEmail - Sender email address
 * @param {string} options.userName - Sender display name
 * @param {string} options.spaceName - Chat space name
 * @param {string} options.targetUrl - Target URL for token audience (default: localhost)
 * @returns {object} The complete webhook payload with headers
 */
function buildPayload(messageText, options = {}) {
  const template = JSON.parse(fs.readFileSync(TEMPLATE_PATH, 'utf8'));

  const eventTime = new Date().toISOString();
  const messageId = generateMessageId();
  const targetUrl = options.targetUrl || 'http://localhost:8080';

  // Replace all placeholders
  let payloadStr = JSON.stringify(template);
  payloadStr = payloadStr.replace(/\{\{MESSAGE_TEXT\}\}/g, messageText);
  payloadStr = payloadStr.replace(/\{\{EVENT_TIME\}\}/g, eventTime);
  payloadStr = payloadStr.replace(/\{\{MESSAGE_ID\}\}/g, messageId);
  payloadStr = payloadStr.replace(/\{\{SYSTEM_ID_TOKEN\}\}/g, generateMockToken(`${targetUrl}${LOCAL_WEBHOOK_ENDPOINT}`));

  const payload = JSON.parse(payloadStr);

  // Apply custom options
  if (options.userEmail) {
    payload.chat.user.email = options.userEmail;
    payload.chat.messagePayload.message.sender.email = options.userEmail;
  }
  if (options.userName) {
    payload.chat.user.displayName = options.userName;
    payload.chat.messagePayload.message.sender.displayName = options.userName;
  }
  if (options.spaceName) {
    payload.chat.messagePayload.space.displayName = options.spaceName;
    payload.chat.messagePayload.message.space.displayName = options.spaceName;
  }

  return payload;
}

/**
 * Build Google Chat request configuration for use with trigger-deploy
 * @param {string} messageText - The message text to send
 * @param {object} options - Optional configuration
 * @param {boolean} options.useLocalEndpoint - Use local testing endpoint (default: true)
 * @returns {object} Request configuration { endpoint, method, body, headers }
 */
function buildRequestConfig(messageText, options = {}) {
  const payload = buildPayload(messageText, options);

  // Use local endpoint for development testing (bypasses JWT auth completely)
  // Production endpoint would require valid JWT tokens from Google Chat
  const useLocal = options.useLocalEndpoint !== false;
  const endpoint = useLocal ? LOCAL_WEBHOOK_ENDPOINT : PRODUCTION_WEBHOOK_ENDPOINT;

  return {
    endpoint: endpoint,
    method: 'POST',
    body: payload,
    headers: {
      'Content-Type': 'application/json'
    }
  };
}

module.exports = {
  buildPayload,
  buildRequestConfig,
  generateMessageId,
  generateMockToken,
  LOCAL_WEBHOOK_ENDPOINT,
  PRODUCTION_WEBHOOK_ENDPOINT,
  // Backwards compatibility alias
  WEBHOOK_ENDPOINT: LOCAL_WEBHOOK_ENDPOINT
};
