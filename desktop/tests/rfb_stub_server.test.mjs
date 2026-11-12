// Pins the RFB stub's handshake bytes — the browser-side password flow tests
// depend on them. Node's built-in WebSocket client plays the role of noVNC.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { startServer } from "./tools/rfb_stub_server.mjs";

async function openRfb(url) {
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error("could not open the stub's WebSocket"));
  });
  const queue = [];
  const waiting = [];
  ws.onmessage = (event) => {
    const data = Buffer.from(new Uint8Array(event.data));
    const waiter = waiting.shift();
    if (waiter) waiter(data);
    else queue.push(data);
  };
  return {
    ws,
    next: () => (queue.length ? Promise.resolve(queue.shift()) : new Promise((r) => waiting.push(r))),
    send: (buffer) => ws.send(buffer),
  };
}

describe("rfb stub server", () => {
  it("walks a client through VNC auth and ServerInit", async () => {
    const stub = await startServer({});
    try {
      const client = await openRfb(stub.url);

      const version = await client.next();
      assert.equal(version.toString("ascii"), "RFB 003.008\n");
      client.send(Buffer.from("RFB 003.008\n", "ascii"));

      const types = await client.next();
      assert.deepEqual([...types], [1, 2]); // one type: VNC authentication
      client.send(Buffer.from([2]));

      const challenge = await client.next();
      assert.equal(challenge.length, 16);
      client.send(Buffer.alloc(16)); // the DES response is accepted as-is

      const result = await client.next();
      assert.equal(result.readUInt32BE(0), 0);

      client.send(Buffer.from([1])); // ClientInit, shared
      const init = await client.next();
      assert.equal(init.readUInt16BE(0), 1280);
      assert.equal(init.readUInt16BE(2), 800);
      const nameLength = init.readUInt32BE(20);
      assert.equal(init.subarray(24, 24 + nameLength).toString("ascii"), "kelp-stub");

      // Sending a key event is what a non-view-only client would do; the stub
      // counts it so the view-only check can assert on /stats.
      client.send(Buffer.from([4, 0, 0, 0, 0, 0, 0, 1]));
      await new Promise((resolve) => setTimeout(resolve, 50));
      const stats = await (await fetch(`http://127.0.0.1:${stub.port}/stats`)).json();
      assert.equal(stats.connections, 1);
      assert.equal(stats.inputEvents, 1);

      client.ws.close();
    } finally {
      await stub.close();
    }
  });
  it("skips the challenge entirely in no-auth mode", async () => {
    const stub = await startServer({ noAuth: true });
    try {
      const client = await openRfb(stub.url);

      assert.equal((await client.next()).toString("ascii"), "RFB 003.008\n");
      client.send(Buffer.from("RFB 003.008\n", "ascii"));

      const types = await client.next();
      assert.deepEqual([...types], [1, 1]); // security type "None"
      client.send(Buffer.from([1]));

      // Straight to SecurityResult 0 — no 16-byte challenge in between.
      const result = await client.next();
      assert.equal(result.length, 4);
      assert.equal(result.readUInt32BE(0), 0);

      client.send(Buffer.from([1])); // ClientInit
      const init = await client.next();
      assert.equal(init.readUInt16BE(0), 1280);
      assert.equal(init.readUInt16BE(2), 800);

      client.ws.close();
    } finally {
      await stub.close();
    }
  });

  it("rejects the password with a reason when asked to", async () => {
    const stub = await startServer({ rejectPasswords: true });
    try {
      const client = await openRfb(stub.url);
      await client.next(); // version
      client.send(Buffer.from("RFB 003.008\n", "ascii"));
      await client.next(); // security types
      client.send(Buffer.from([2]));
      await client.next(); // challenge
      client.send(Buffer.alloc(16));

      const failure = await client.next();
      assert.equal(failure.readUInt32BE(0), 1);
      const reasonLength = failure.readUInt32BE(4);
      assert.equal(failure.subarray(8, 8 + reasonLength).toString("ascii"), "stub: password rejected");

      client.ws.close();
    } finally {
      await stub.close();
    }
  });

  it("answers every FramebufferUpdateRequest, packing pixels in the negotiated format", async () => {
    const stub = await startServer({});
    try {
      const client = await openRfb(stub.url);
      await client.next(); // version
      client.send(Buffer.from("RFB 003.008\n", "ascii"));
      await client.next(); // security types
      client.send(Buffer.from([2]));
      await client.next(); // challenge
      client.send(Buffer.alloc(16));
      await client.next(); // SecurityResult
      client.send(Buffer.from([1])); // ClientInit, shared
      await client.next(); // ServerInit

      // Ask for BGR order (blueShift 16): the frame must be packed that way,
      // or the channels arrive swapped and the card shows the wrong teal.
      const setFormat = Buffer.alloc(20);
      setFormat[0] = 0; // SetPixelFormat
      setFormat[4] = 32; // bits-per-pixel
      setFormat[5] = 24; // depth
      setFormat[6] = 0; // little-endian
      setFormat[7] = 1; // true-colour
      setFormat.writeUInt16BE(255, 8); // red-max
      setFormat.writeUInt16BE(255, 10); // green-max
      setFormat.writeUInt16BE(255, 12); // blue-max
      setFormat[14] = 0; // redShift
      setFormat[15] = 8; // greenShift
      setFormat[16] = 16; // blueShift
      client.send(setFormat);

      const frameRequest = Buffer.alloc(10);
      frameRequest[0] = 3; // FramebufferUpdateRequest
      frameRequest[1] = 0; // incremental = 0 → full
      frameRequest.writeUInt16BE(0, 2); // x
      frameRequest.writeUInt16BE(0, 4); // y
      frameRequest.writeUInt16BE(1280, 6); // w
      frameRequest.writeUInt16BE(800, 8); // h

      for (let round = 0; round < 2; round++) {
        client.send(frameRequest);
        const frame = await client.next();
        assert.equal(frame[0], 0); // FramebufferUpdate
        assert.equal(frame.readUInt16BE(2), 1); // one rect
        const pixels = frame.subarray(16);
        assert.equal(pixels.length, 1280 * 800 * 4);
        // Row 0 sits inside the first colour bar (teal [0x4f, 0xc4, 0xa8]),
        // pixel (x=700) lands in the fourth band. With the negotiated BGR
        // order (redShift 0, greenShift 8, blueShift 16) the little-endian
        // bytes are [0x4f, 0xc4, 0xa8, 0x00] — an RGB packing would swap the
        // first and third bytes and read as the wrong teal.
        const sample = pixels.subarray(700 * 4, 700 * 4 + 4);
        assert.deepEqual([...sample], [0x4f, 0xc4, 0xa8, 0x00]);
      }

      client.ws.close();
    } finally {
      await stub.close();
    }
  });
});
