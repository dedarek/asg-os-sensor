// 最简 OTLP 接收器: 记录所有 POST 的 path + body 摘要
const http = require('http');
const fs = require('fs');
const host = process.env.ASG_OTEL_HOST || '127.0.0.1';
const parsedPort = Number.parseInt(process.env.ASG_OTEL_PORT || '4318', 10);
const port = Number.isFinite(parsedPort) ? parsedPort : 4318;
const output = process.env.ASG_OTEL_LOG || 'otel_hits.log';
const srv = http.createServer((req, res) => {
  let chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', () => {
    const body = Buffer.concat(chunks);
    fs.appendFileSync(output,
      JSON.stringify({ts: new Date().toISOString(), path: req.url, bytes: body.length,
                      head: body.slice(0, 400).toString('utf8',).replace(/\n/g,' ')}) + '\n');
    res.writeHead(200, {'content-type': 'application/json'}); res.end('{}');
  });
});
srv.listen(port, host, () => console.log(`OTEL sink on http://${host}:${port}`));
