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
    } finally {
      await stub.close();
    }
  });
});
