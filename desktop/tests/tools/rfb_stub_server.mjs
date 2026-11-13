// A local RFB server for exercising the Computer tab without a VM.
//
// Speaks just enough RFB 3.8 to walk noVNC through VNC authentication and one
// raw framebuffer update, over a hand-rolled WebSocket (this tree has no `ws`
// dependency). The stub cannot do the VNC DES check, so "accept" mode takes
// any password; the wrong-password path is exercised with --reject.
//
//   node tests/tools/rfb_stub_server.mjs              # accept any password
//   node tests/tools/rfb_stub_server.mjs --reject     # fail VNC auth, with reason
//   node tests/tools/rfb_stub_server.mjs --no-auth    # advertise security type "None"
//   node tests/tools/rfb_stub_server.mjs --port 6099
//
// GET /stats reports `{ connections, inputEvents }`. `inputEvents` counts
// KeyEvent/PointerEvent messages reaching the server — while the viewer is
// view-only that must stay at zero, which is how input forwarding is checked.

import crypto from "node:crypto";
import http from "node:http";
import { pathToFileURL } from "node:url";

const WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const WIDTH = 1280;
const HEIGHT = 800;
const DESKTOP_NAME = "kelp-stub";

/** One WebSocket binary frame (server→client, so unmasked). */
function sendFrame(socket, payload, opcode = 0x82) {
  const len = payload.length;
  let header;
  if (len < 126) {
    header = Buffer.from([opcode, len]);
  } else if (len <= 0xffff) {
    header = Buffer.alloc(4);
    header[0] = opcode;
    header[1] = 126;
    header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = opcode;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(len), 2);
  }
  socket.write(Buffer.concat([header, payload]));
}

/** What the server advertised in ServerInit; a client may override it with
 *  SetPixelFormat, and when it does the frame must be packed its way. Getting
 *  this wrong does not fail loudly — it just swaps the channels. */
const DEFAULT_FORMAT = { redShift: 16, greenShift: 8, blueShift: 0, bigEndian: 0 };

/** A test-card framebuffer update covering the whole screen.
 *
 * A single flat colour (what this used to send) proves only that bytes moved:
 * on screen it is indistinguishable from a black pane, a stalled canvas, or a
 * bad pixel format. Colour bars plus a grid make a working viewer obvious and
 * a wrong channel order visible at a glance.
 */
function framebufferUpdate(format = DEFAULT_FORMAT) {
  const header = Buffer.alloc(16);
  header[0] = 0; // FramebufferUpdate
  header.writeUInt16BE(1, 2); // one rect
  header.writeUInt16BE(0, 4);
  header.writeUInt16BE(0, 6);
  header.writeUInt16BE(WIDTH, 8);
  header.writeUInt16BE(HEIGHT, 10);
  header.writeInt32BE(0, 12); // raw encoding
  const pixels = Buffer.alloc(WIDTH * HEIGHT * 4);
  const put = (index, red, green, blue) => {
    const value =
      ((red << format.redShift) |
        (green << format.greenShift) |
        (blue << format.blueShift)) >>>
      0;
    if (format.bigEndian) pixels.writeUInt32BE(value, index * 4);
    else pixels.writeUInt32LE(value, index * 4);
  };
  const BARS = [
    [0xe0, 0x4f, 0x4f],
    [0xe0, 0xb0, 0x4f],
    [0x7f, 0xc4, 0x4f],
    [0x4f, 0xc4, 0xa8],
    [0x4f, 0x8f, 0xe0],
    [0x9b, 0x5f, 0xe0],
  ];
  for (let y = 0; y < HEIGHT; y++) {
    for (let x = 0; x < WIDTH; x++) {
      const index = y * WIDTH + x;
      if (y < 160) {
        const band = Math.min(BARS.length - 1, Math.floor((x / WIDTH) * BARS.length));
        put(index, ...BARS[band]);
        continue;
      }
      if (x % 64 === 0 || y % 64 === 0) {
        put(index, 0x2c, 0x34, 0x3c); // grid
        continue;
      }
      put(index, 0x14, 0x17, 0x19);
    }
  }
  return Buffer.concat([header, pixels]);
}

/** Read one masked client frame; returns null until a full frame is buffered. */
function readFrame(state) {
  const buffer = state.wsBuffer;
  if (buffer.length < 2) return null;
  const opcode = buffer[0] & 0x0f;
  const masked = (buffer[1] & 0x80) !== 0;
  let len = buffer[1] & 0x7f;
  let offset = 2;
  if (len === 126) {
    if (buffer.length < 4) return null;
    len = buffer.readUInt16BE(2);
    offset = 4;
  } else if (len === 127) {
    if (buffer.length < 10) return null;
    len = Number(buffer.readBigUInt64BE(2));
    offset = 10;
  }
  const maskOffset = masked ? offset : -1;
  if (masked) offset += 4;
  if (buffer.length < offset + len) return null;
  let payload = buffer.subarray(offset, offset + len);
  if (masked) {
    const mask = buffer.subarray(maskOffset, maskOffset + 4);
    const unmasked = Buffer.allocUnsafe(len);
    for (let i = 0; i < len; i++) unmasked[i] = payload[i] ^ mask[i % 4];
    payload = unmasked;
  }
  state.wsBuffer = buffer.subarray(offset + len);
  return { opcode, payload };
}

/** Drive the RFB handshake for one connection. */
function runRfbSession(socket, stats, rejectPasswords, noAuth) {
  let rfbBuffer = Buffer.alloc(0);
  let stage = "version";
  let sentFramebuffer = false;
  let format = { ...DEFAULT_FORMAT };
  const state = { wsBuffer: Buffer.alloc(0) };

  const send = (bytes) => sendFrame(socket, bytes);
  send(Buffer.from("RFB 003.008\n", "ascii"));

  const step = () => {
    for (;;) {
      if (stage === "version") {
        if (rfbBuffer.length < 12) return;
        rfbBuffer = rfbBuffer.subarray(12);
        stage = "security-types";
        // VNC authentication asks for a password; type 1 (None) does not.
        send(Buffer.from(noAuth ? [0x01, 0x01] : [0x01, 0x02]));
      } else if (stage === "security-types") {
        if (rfbBuffer.length < 1) return;
        rfbBuffer = rfbBuffer.subarray(1); // the client's echoed choice
        if (noAuth) {
          stage = "result";
          send(Buffer.from([0, 0, 0, 0]));
          continue;
        }
        stage = "challenge";
        send(crypto.randomBytes(16));
      } else if (stage === "challenge") {
        if (rfbBuffer.length < 16) return;
        rfbBuffer = rfbBuffer.subarray(16); // the DES response, taken as-is
        if (rejectPasswords) {
          stage = "dead";
          const reason = Buffer.from("stub: password rejected", "ascii");
          const failure = Buffer.alloc(8 + reason.length);
          failure.writeUInt32BE(1, 0);
          failure.writeUInt32BE(reason.length, 4);
          reason.copy(failure, 8);
          send(failure);
          socket.end();
          return;
        }
        stage = "result";
        send(Buffer.from([0, 0, 0, 0]));
      } else if (stage === "result") {
        if (rfbBuffer.length < 1) return; // ClientInit
        rfbBuffer = rfbBuffer.subarray(1);
        const name = Buffer.from(DESKTOP_NAME, "ascii");
        const init = Buffer.alloc(24 + 4 + name.length);
        let o = 0;
        init.writeUInt16BE(WIDTH, o);
        o += 2;
        init.writeUInt16BE(HEIGHT, o);
        o += 2;
        init[o++] = 32; // bits-per-pixel
        init[o++] = 24; // depth
        init[o++] = 0; // big-endian
        init[o++] = 1; // true-colour
        init.writeUInt16BE(255, o);
        o += 2; // red-max
        init.writeUInt16BE(255, o);
        o += 2; // green-max
        init.writeUInt16BE(255, o);
        o += 2; // blue-max
        init[o++] = 16; // red-shift
        init[o++] = 8; // green-shift
        init[o++] = 0; // blue-shift
        o += 3; // padding
        init.writeUInt32BE(name.length, o);
        o += 4;
        name.copy(init, o);
        stage = "live";
        send(init);
      } else if (stage === "live") {
        // Client messages: 0 SetPixelFormat(20), 2 SetEncodings(4+4n),
        // 3 FBUpdateRequest(10), 4 KeyEvent(8), 5 PointerEvent(6),
        // 6 ClientCutText(8+len). Only the update request and the input
        // events matter here; everything else is skipped.
        if (rfbBuffer.length < 1) return;
        const type = rfbBuffer[0];
        let size;
        if (type === 2) {
          if (rfbBuffer.length < 4) return;
          const count = rfbBuffer.readUInt16BE(2);
          size = 4 + 4 * count;
        } else if (type === 6) {
          if (rfbBuffer.length < 8) return;
          size = 8 + rfbBuffer.readUInt32BE(4);
        } else if (type === 0) {
          size = 20;
        } else if (type === 3) {
          size = 10;
        } else if (type === 4) {
          size = 8;
        } else if (type === 5) {
          size = 6;
        } else {
          socket.destroy();
          return;
        }
        if (rfbBuffer.length < size) return;
        const message = rfbBuffer.subarray(0, size);
        rfbBuffer = rfbBuffer.subarray(size);
        if (type === 0) {
          // SetPixelFormat decides how the frame must be packed. Ignoring it
          // (as this did) leaves the viewer decoding the bytes with its own
          // shifts, which reads as a plausible-but-wrong colour.
          format = {
            redShift: message[14],
            greenShift: message[15],
            blueShift: message[16],
            bigEndian: message[6],
          };
        }
        // Key and pointer events must never arrive from a view-only viewer.
        if (type === 4 || type === 5) stats.inputEvents += 1;
        if (type === 3) {
          // Every request gets a live frame, not just the first: a viewer
          // that paints once and stalls is indistinguishable from a dead
          // socket, so the stub must keep answering to prove the mirror.
          sentFramebuffer = true;
          send(framebufferUpdate(format));
        }
      } else {
        return;
      }
    }
  };

  socket.on("data", (chunk) => {
    state.wsBuffer = Buffer.concat([state.wsBuffer, chunk]);
    for (;;) {
      const frame = readFrame(state);
      if (!frame) return;
      if (frame.opcode === 8) {
        // The client is going away. The stub may already have ended this
        // socket (rejected password), so echo only while it is still open —
        // writing to an ended socket throws and wedges the test server.
        if (!socket.destroyed && socket.writable && !socket.writableEnded) {
          try {
            sendFrame(socket, Buffer.alloc(0), 0x88); // echo close
          } catch {
            /* already gone */
          }
        }
        socket.destroy();
        return;
      }
      if (frame.opcode === 9) {
        sendFrame(socket, frame.payload, 0x8a); // ping → pong
        continue;
      }
      if (frame.opcode === 1 || frame.opcode === 2) {
        rfbBuffer = Buffer.concat([rfbBuffer, frame.payload]);
        step();
      }
    }
  });
}

/** Start the stub. Resolves once it is listening on an ephemeral or given port. */
export async function startServer({ port = 0, rejectPasswords = false, noAuth = false } = {}) {
  const stats = { connections: 0, inputEvents: 0 };
  const sockets = new Set();
  const server = http.createServer((request, response) => {
    if (request.url === "/stats") {
      response.writeHead(200, {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
      });
      response.end(JSON.stringify(stats));
      return;
    }
    response.writeHead(404).end();
  });

  server.on("upgrade", (request, socket) => {
    const key = request.headers["sec-websocket-key"];
    if (!key) {
      socket.destroy();
      return;
    }
    const accept = crypto.createHash("sha1").update(key + WS_MAGIC).digest("base64");
    const headers = [
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${accept}`,
    ];
    const protocols = request.headers["sec-websocket-protocol"];
    if (protocols) headers.push(`Sec-WebSocket-Protocol: ${String(protocols).split(",")[0].trim()}`);
    socket.write(headers.join("\r\n") + "\r\n\r\n");
    stats.connections += 1;
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    runRfbSession(socket, stats, rejectPasswords, noAuth);
  });

  return new Promise((resolve) => {
    server.listen(port, "127.0.0.1", () => {
      const address = server.address();
      resolve({
        port: address.port,
        url: `ws://127.0.0.1:${address.port}/websockify`,
        stats,
        close: () =>
          new Promise((done) => {
            // A test must never hang on teardown: drop any sockets the
            // client left open, then close the listener.
            for (const socket of sockets) socket.destroy();
            server.close(() => done());
          }),
      });
    });
  });
}

const isMain =
  process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;

if (isMain) {
  const args = process.argv.slice(2);
  const portFlag = args.indexOf("--port");
  const port = portFlag !== -1 ? Number(args[portFlag + 1]) || 6080 : 6080;
  const rejectPasswords = args.includes("--reject");
  const noAuth = args.includes("--no-auth");
  const stub = await startServer({ port, rejectPasswords, noAuth });
  const mode = noAuth ? "no authentication" : rejectPasswords ? "rejects every password" : "accepts any password";
  console.log(
    `RFB stub on ${stub.url} — ${mode} (stats: http://127.0.0.1:${stub.port}/stats)`,
  );
}
