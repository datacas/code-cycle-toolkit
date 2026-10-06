import http from 'node:http';
const pages = {
  '/login': '<h1>Login</h1><input aria-label="Email"><button id="go">Sign in</button><p id="count">3</p>',
  '/dashboard': '<h1>Dashboard</h1><p id="count">3</p>',
};
http.createServer((req, res) => {
  const body = pages[req.url.split('?')[0]];
  if (!body) { res.writeHead(404); res.end('nf'); return; }
  res.writeHead(200, { 'content-type': 'text/html' });
  res.end(`<!doctype html><html><body>${body}<script>console.error('spike console error')</script></body></html>`);
}).listen(4173, '127.0.0.1');
