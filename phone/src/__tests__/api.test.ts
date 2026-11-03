import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  decideApproval,
  listThreads,
  onUnauthorized,
  pair,
  sendTurn,
} from "../api";
import { getCsrfToken, setSession } from "../state";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  setSession(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
  onUnauthorized(null);
});

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

describe("api client", () => {
  it("sends credentials and no CSRF header on GET", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true, threads: [] }));
    await listThreads();
    const init = fetchMock.mock.calls[0][1];
    expect(init.credentials).toBe("include");
    expect(init.headers["X-CSRF-Token"]).toBeUndefined();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/threads?limit=50");
  });

  it("injects X-CSRF-Token from the session on mutations", async () => {
    setSession({ csrfToken: "csrf-123", device: { id: "d1", label: "Phone" }, expiresAt: null });
    expect(getCsrfToken()).toBe("csrf-123");
    fetchMock.mockResolvedValue(jsonResponse(202, { ok: true, turn_id: "turn_1" }));
    await sendTurn("ses_abc", "hello", "ctid-1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/threads/ses_abc/turns");
    expect(init.method).toBe("POST");
    expect(init.headers["X-CSRF-Token"]).toBe("csrf-123");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body)).toEqual({ text: "hello", client_turn_id: "ctid-1" });
  });

  it("sends the bearer token only on the pairing call and never stores it", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(201, {
        ok: true,
        session: {
          csrf_token: "csrf-9",
          device: { id: "dev1", label: "Phone" },
          expires_at: "2026-12-01T00:00:00Z",
        },
      }),
    );
    const session = await pair("root-token-value", "Phone");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/session");
    expect(init.headers["Authorization"]).toBe("Bearer root-token-value");
    expect(init.headers["X-CSRF-Token"]).toBeUndefined();
    expect(session.csrfToken).toBe("csrf-9");
    expect(session.device).toEqual({ id: "dev1", label: "Phone" });
  });

  it("parses the error envelope into ApiError with code and message", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(409, {
        ok: false,
        error: { code: "turn_in_progress", message: "Still answering.", retryable: false },
      }),
    );
    const error = await sendTurn("ses_abc", "hi", "ctid").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(409);
    expect(apiError.code).toBe("turn_in_progress");
    expect(apiError.message).toBe("Still answering.");
  });

  it("falls back to generic copy for a non-JSON error body", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      json: async () => {
        throw new Error("not json");
      },
    });
    const error = await listThreads().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("unknown_error");
    expect((error as ApiError).message).toBe("Something went wrong.");
  });

  it("clears the session and fires the 401 listener", async () => {
    setSession({ csrfToken: "csrf-1", device: { id: "d", label: "Phone" }, expiresAt: null });
    const listener = vi.fn();
    onUnauthorized(listener);
    fetchMock.mockResolvedValue(jsonResponse(401, { error: { code: "unauthorized" } }));
    await expect(listThreads()).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(getCsrfToken()).toBeNull();
  });

  it("maps a network failure to a retryable ApiError", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    const error = await decideApproval("apr_1", "approve").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("network");
    expect((error as ApiError).retryable).toBe(true);
  });
});
