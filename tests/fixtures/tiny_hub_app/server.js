// A dependency-free fixture for the hub protocol, not a production app or Vite.
const http = require('node:http');
const fs = require('node:fs');
const args = process.argv.slice(2);

if (args[0] === 'build') {
  fs.mkdirSync('dist', { recursive: true });
  fs.copyFileSync('index.html', 'dist/index.html');
} else if (args[0] === 'preview') {
  const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
  const defaults = { currentUser: {}, items: [], ui: {} };
  fs.mkdirSync('.mock-states', { recursive: true });

  // Objects and arrays recurse; leaf additions, removals and changes remain visible.
  function diff(before, after) {
    if (before === after) return {};
    if (before === undefined) return { added: after };
    if (after === undefined) return { removed: before };
    if (before && after && typeof before === 'object' && typeof after === 'object'
        && Array.isArray(before) === Array.isArray(after)) {
      const result = Object.create(null);
      for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
        const change = diff(before[key], after[key]);
        if (Object.keys(change).length) result[key] = change;
      }
      return result;
    }
    return { old: before, new: after };
  }

  const server = http.createServer(async (req, res) => {
    const reply = (status, value, type = 'application/json') => {
      res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store' });
      res.end(type === 'application/json' ? JSON.stringify(value) : value);
    };
    try {
      const url = new URL(req.url, 'http://localhost');
      const sid = url.searchParams.get('sid') || '_default';
      if (!/^[A-Za-z0-9_-]{1,64}$/.test(sid)) return reply(400, { error: 'Invalid sid' });
      const file = `.mock-states/${sid}.json`;
      const entry = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
      if (req.method === 'GET' && url.pathname === '/state') {
        return reply(200, { stored_state: entry?.current ?? null, has_custom_state: !!entry, sid });
      }
      if (req.method === 'GET' && url.pathname === '/go') {
        const initial = entry?.initial ?? defaults;
        const current = entry?.current ?? initial;
        return reply(200, { initial_state: initial, current_state: current, state_diff: diff(initial, current) });
      }
      if (req.method === 'POST' && url.pathname === '/post') {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const data = JSON.parse(Buffer.concat(chunks).toString());
        if (data.action === 'reset') {
          fs.rmSync(file, { force: true });
          return reply(200, { success: true, sid });
        }
        if (!['set', 'set_current'].includes(data.action)) return reply(400, { error: 'Unknown action' });
        if (!data.state || typeof data.state !== 'object' || Array.isArray(data.state)) {
          return reply(400, { error: 'Expected state object' });
        }
        const initial = data.action === 'set' ? data.state : (entry?.initial ?? defaults);
        fs.writeFileSync(file, JSON.stringify({ initial, current: data.state }));
        return reply(200, { success: true, sid });
      }
      // Minimal multipart support: one file per request, preserved as binary bytes.
      if (req.method === 'POST' && url.pathname === '/upload') {
        const boundary = (req.headers['content-type'] || '').match(/boundary="?([^";]+)"?/);
        if (!boundary) return reply(400, { error: 'Expected multipart boundary' });
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const body = Buffer.concat(chunks);
        const split = body.indexOf('\r\n\r\n');
        const header = body.subarray(0, split).toString();
        const filename = header.match(/filename="([^"]+)"/);
        const end = body.indexOf(`\r\n--${boundary[1]}`, split + 4);
        if (split < 0 || !filename || end < 0) return reply(400, { error: 'Expected one file' });
        const bytes = body.subarray(split + 4, end);
        const dir = `.mock-files/${sid}`;
        fs.mkdirSync(dir, { recursive: true });
        const stored = `upload_${filename[1].replace(/[^A-Za-z0-9._-]/g, '_')}`;
        fs.writeFileSync(`${dir}/${stored}`, bytes);
        return reply(200, { success: true, files: [{
          original_name: filename[1], stored_name: stored, size: bytes.length,
          content_type: header.match(/Content-Type:\s*([^\r\n]+)/i)?.[1] || 'application/octet-stream',
          url: `/files/${sid}/${stored}`,
        }] });
      }
      const download = url.pathname.match(/^\/files\/([A-Za-z0-9_-]{1,64})\/(upload_[A-Za-z0-9._-]+)$/);
      if (req.method === 'GET' && download) {
        const path = `.mock-files/${download[1]}/${download[2]}`;
        if (fs.existsSync(path)) return reply(200, fs.readFileSync(path), 'application/octet-stream');
      }
      if (req.method === 'GET' && ['/', '/index.html'].includes(url.pathname)) {
        return reply(200, fs.readFileSync('dist/index.html'), 'text/html; charset=utf-8');
      }
      return reply(404, { error: 'Not found' });
    } catch (error) {
      return reply(400, { error: error.message });
    }
  });
  // listen fails on an occupied port; it never falls back, matching --strictPort.
  server.listen(Number(option('--port', '4173')), option('--host', '127.0.0.1'));
} else {
  throw new Error('Expected build or preview');
}
