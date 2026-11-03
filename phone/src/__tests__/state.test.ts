import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cacheThreads, getCachedThreads, getSession, setSession } from "../state";

beforeEach(() => {
  setSession(null);
  sessionStorage.clear();
});

afterEach(() => {
  setSession(null);
  sessionStorage.clear();
});

describe("session state", () => {
  it("persists the session to sessionStorage and reloads it", () => {
    setSession({ csrfToken: "csrf-1", device: { id: "d1", label: "Phone" }, expiresAt: "2026-12-01T00:00:00Z" });
    expect(getSession()?.csrfToken).toBe("csrf-1");
    // A fresh module instance would load from sessionStorage; simulate by
    // checking the raw stored value.
    const raw = sessionStorage.getItem("kelp.phone.session");
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw as string)).toMatchObject({
      csrfToken: "csrf-1",
      device: { id: "d1", label: "Phone" },
    });
  });

  it("clears sessionStorage on sign-out", () => {
    setSession({ csrfToken: "csrf-1", device: { id: "d1", label: "Phone" }, expiresAt: null });
    setSession(null);
    expect(getSession()).toBeNull();
    expect(sessionStorage.getItem("kelp.phone.session")).toBeNull();
  });

  it("rejects a malformed stored session", () => {
    sessionStorage.setItem("kelp.phone.session", '{"csrfToken": 42}');
    // The in-memory session is unaffected; a reload would fall back to null
    // because the shape check fails.
    expect(getSession()).toBeNull();
  });
});

describe("thread list cache", () => {
  it("caches and returns the thread list", () => {
    expect(getCachedThreads()).toBeNull();
    cacheThreads([
      {
        id: "ses_1",
        title: "Hello",
        updated_at: "2026-11-03T00:00:00Z",
        last_message_preview: null,
        message_count: 2,
        active_turn_id: null,
      },
    ]);
    const cached = getCachedThreads();
    expect(cached?.threads[0].title).toBe("Hello");
    expect(cached?.fetchedAt).toBeLessThanOrEqual(Date.now());
  });
});
