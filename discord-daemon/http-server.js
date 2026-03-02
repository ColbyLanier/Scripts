// http-server.js — Local HTTP API on localhost:7779
// Pure Node.js HTTP server (no Express/Fastify dependency needed)

import { createServer } from 'http';

export function createHttpServer(discordClient, messageStore, config, logger) {
  // Resolve channel name to ID
  function resolveChannel(name) {
    if (!name) return null;
    // Direct ID
    if (/^\d+$/.test(name)) return name;
    // Alias lookup
    return config.channels[name] || null;
  }

  // Reverse lookup: ID to name
  function channelName(id) {
    for (const [name, cid] of Object.entries(config.channels)) {
      if (cid === id) return name;
    }
    return id;
  }

  // Parse JSON body
  function parseBody(req) {
    return new Promise((resolve, reject) => {
      let body = '';
      req.on('data', chunk => body += chunk);
      req.on('end', () => {
        try {
          resolve(body ? JSON.parse(body) : {});
        } catch (e) {
          reject(new Error('Invalid JSON body'));
        }
      });
      req.on('error', reject);
    });
  }

  // Parse query string
  function parseQuery(url) {
    const idx = url.indexOf('?');
    if (idx === -1) return {};
    const params = {};
    new URLSearchParams(url.slice(idx + 1)).forEach((v, k) => params[k] = v);
    return params;
  }

  // JSON response helper
  function json(res, data, status = 200) {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
  }

  // Active /wait and /subscribe listeners
  const waitListeners = new Map();  // messageId -> { resolve, timeout, channelId }
  const subscribeListeners = new Set(); // Set of { res, channels, since }

  // Register message handler for /wait and /subscribe
  discordClient.onMessage((msg) => {
    // Check /wait listeners — match by reply_to, operator-only
    for (const [waitMsgId, waiter] of waitListeners.entries()) {
      if (msg.reply_to_message_id === waitMsgId) {
        // Only resolve if from the operator (ignore other guild members replying)
        if (config.operator_user_id && msg.author?.id !== config.operator_user_id) continue;
        waiter.resolve(msg);
        clearTimeout(waiter.timeout);
        waitListeners.delete(waitMsgId);
      }
    }

    // Check /subscribe listeners — send to all matching subscriptions
    for (const sub of subscribeListeners) {
      if (sub.channels === null || sub.channels.has(msg.channel_id) || sub.channels.has(msg.channel_name)) {
        try {
          sub.res.write(`data: ${JSON.stringify(msg)}\n\n`);
        } catch {
          subscribeListeners.delete(sub);
        }
      }
    }
  });

  // Also check reactions for /wait
  discordClient.onReaction((reaction) => {
    // Skip bot's own pre-populated reactions
    if (reaction.user_id === discordClient.botUserId) return;
    // Only resolve for the operator (ignore other guild members reacting)
    if (config.operator_user_id && reaction.user_id !== config.operator_user_id) return;

    const waiter = waitListeners.get(reaction.message_id);
    if (waiter) {
      waiter.resolve({
        type: 'reaction',
        message_id: reaction.message_id,
        emoji: reaction.emoji,
        user_id: reaction.user_id,
        username: reaction.username,
      });
      clearTimeout(waiter.timeout);
      waitListeners.delete(reaction.message_id);
    }
  });

  const server = createServer(async (req, res) => {
    const path = req.url.split('?')[0];
    const method = req.method;

    try {
      // POST /send — Send message to a channel
      if (method === 'POST' && path === '/send') {
        const body = await parseBody(req);
        const channelId = resolveChannel(body.channel);
        if (!channelId) return json(res, { error: `Unknown channel: ${body.channel}` }, 400);
        if (!body.content && !body.embeds) return json(res, { error: 'content or embeds required' }, 400);

        // Persist before sending (crash recovery)
        const pendingId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        messageStore.persist(pendingId, { channel: body.channel, channelId, content: body.content });

        const result = await discordClient.sendMessage(channelId, body.content, {
          embeds: body.embeds,
          reply_to: body.reply_to,
        });

        messageStore.remove(pendingId);
        logger.info(`Sent to ${channelName(channelId)}: ${(body.content || '').slice(0, 60)}`);
        return json(res, result);
      }

      // GET /read — Read recent messages
      if (method === 'GET' && path === '/read') {
        const query = parseQuery(req.url);
        const channelId = resolveChannel(query.channel);
        if (!channelId) return json(res, { error: `Unknown channel: ${query.channel}` }, 400);

        const limit = parseInt(query.limit) || 25;
        const messages = await discordClient.readMessages(channelId, Math.min(limit, 100));

        // Apply --since filter if provided
        let filtered = messages;
        if (query.since) {
          const sinceDate = parseSince(query.since);
          if (sinceDate) {
            filtered = messages.filter(m => new Date(m.timestamp) >= sinceDate);
          }
        }

        return json(res, { channel: channelName(channelId), channel_id: channelId, messages: filtered });
      }

      // POST /react — Add reaction
      if (method === 'POST' && path === '/react') {
        const body = await parseBody(req);
        const channelId = resolveChannel(body.channel);
        if (!channelId) return json(res, { error: `Unknown channel: ${body.channel}` }, 400);
        if (!body.message_id || !body.emoji) return json(res, { error: 'message_id and emoji required' }, 400);

        const result = await discordClient.addReaction(channelId, body.message_id, body.emoji);
        return json(res, result);
      }

      // POST /wait — Block until reply to a specific message
      if (method === 'POST' && path === '/wait') {
        const body = await parseBody(req);
        if (!body.message_id) return json(res, { error: 'message_id required' }, 400);

        const timeoutMs = (body.timeout_seconds || 86400) * 1000; // Default 24h
        const channelId = resolveChannel(body.channel);

        const result = await new Promise((resolve, reject) => {
          const timeout = setTimeout(() => {
            waitListeners.delete(body.message_id);
            resolve(null); // null = timeout
          }, timeoutMs);

          waitListeners.set(body.message_id, { resolve, timeout, channelId });
        });

        if (result === null) {
          return json(res, { timeout: true }, 408);
        }
        return json(res, { timeout: false, reply: result });
      }

      // GET /subscribe — SSE stream of messages from channels
      // Subscribe to ALL messages (not just pings) — this is the key feature
      if (method === 'GET' && path === '/subscribe') {
        const query = parseQuery(req.url);

        // Set up SSE headers
        res.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
        });

        // Parse channel filter (comma-separated names/IDs, or null for all)
        let channels = null;
        if (query.channels) {
          channels = new Set();
          for (const ch of query.channels.split(',')) {
            const id = resolveChannel(ch.trim());
            if (id) channels.add(id);
            channels.add(ch.trim()); // Also keep the name for matching
          }
        }

        const sub = { res, channels };
        subscribeListeners.add(sub);

        // Send initial keepalive
        res.write(`data: ${JSON.stringify({ type: 'connected', channels: query.channels || 'all' })}\n\n`);

        // Clean up on disconnect
        req.on('close', () => {
          subscribeListeners.delete(sub);
          logger.debug('Subscribe client disconnected');
        });
        return; // Keep connection open
      }

      // POST /dm — Send DM to operator
      if (method === 'POST' && path === '/dm') {
        const body = await parseBody(req);
        if (!body.content) return json(res, { error: 'content required' }, 400);
        const result = await discordClient.sendDM(body.content);
        logger.info(`DM sent: ${body.content.slice(0, 60)}`);
        return json(res, result);
      }

      // GET /status — Health check
      if (method === 'GET' && path === '/status') {
        return json(res, discordClient.getStatus());
      }

      // GET /channels — List configured channels
      if (method === 'GET' && path === '/channels') {
        const channels = Object.entries(config.channels).map(([name, id]) => ({ name, id }));
        return json(res, { channels });
      }

      // GET /poll — Check if a message has received a human reaction or text reply
      if (method === 'GET' && path === '/poll') {
        const query = parseQuery(req.url);
        if (!query.message_id) return json(res, { error: 'message_id required' }, 400);
        if (!query.channel) return json(res, { error: 'channel required' }, 400);

        const channelId = resolveChannel(query.channel);
        if (!channelId) return json(res, { error: `Unknown channel: ${query.channel}` }, 400);

        const reactionResult = await discordClient.getMessageReactions(channelId, query.message_id);
        if (reactionResult) return json(res, reactionResult);

        const replyResult = await discordClient.getMessageReplies(channelId, query.message_id);
        if (replyResult) return json(res, replyResult);

        return json(res, { answered: false });
      }

      // 404
      json(res, { error: 'Not found' }, 404);

    } catch (err) {
      logger.error(`HTTP error on ${method} ${path}: ${err.message}`);
      json(res, { error: err.message }, 500);
    }
  });

  return {
    start() {
      return new Promise((resolve) => {
        server.listen(config.daemon_port, '127.0.0.1', () => {
          logger.info(`HTTP API listening on http://127.0.0.1:${config.daemon_port}`);
          resolve();
        });
      });
    },
    stop() {
      return new Promise((resolve) => {
        // Close all SSE connections
        for (const sub of subscribeListeners) {
          try { sub.res.end(); } catch {}
        }
        subscribeListeners.clear();
        server.close(resolve);
      });
    },
  };
}

// Parse relative time strings like "1h ago", "30m ago", "2d ago"
function parseSince(since) {
  const match = since.match(/^(\d+)\s*(s|m|h|d)\s*(ago)?$/i);
  if (!match) {
    // Try as ISO date
    const d = new Date(since);
    return isNaN(d.getTime()) ? null : d;
  }
  const num = parseInt(match[1]);
  const unit = match[2].toLowerCase();
  const multipliers = { s: 1000, m: 60000, h: 3600000, d: 86400000 };
  return new Date(Date.now() - num * multipliers[unit]);
}
