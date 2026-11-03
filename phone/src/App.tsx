import { useCallback, useEffect, useState } from "react";
import { onUnauthorized } from "./api";
import Chat from "./screens/Chat";
import Pair from "./screens/Pair";
import Settings from "./screens/Settings";
import Threads from "./screens/Threads";
import { getSession, setSession, type SessionInfo } from "./state";

export type Route =
  | { screen: "threads" }
  | { screen: "chat"; threadId: string }
  | { screen: "settings" };

function parseHash(): Route {
  const hash = window.location.hash.replace(/^#\/?/, "");
  if (hash === "settings") return { screen: "settings" };
  const chatMatch = /^t\/([A-Za-z0-9_-]+)$/.exec(hash);
  if (chatMatch) return { screen: "chat", threadId: chatMatch[1] };
  return { screen: "threads" };
}

export function routeToHash(route: Route): string {
  switch (route.screen) {
    case "settings":
      return "#/settings";
    case "chat":
      return `#/t/${route.threadId}`;
    default:
      return "#/";
  }
}

export default function App() {
  const [session, setSessionState] = useState<SessionInfo | null>(getSession());
  const [route, setRoute] = useState<Route>(parseHash);

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  // Any 401 from the API clears the session and returns to pairing.
  useEffect(() => {
    onUnauthorized(() => {
      setSession(null);
      setSessionState(null);
      window.location.hash = "#/";
    });
    return () => onUnauthorized(null);
  }, []);

  const navigate = useCallback((next: Route) => {
    window.location.hash = routeToHash(next);
  }, []);

  const onPaired = useCallback(
    (next: SessionInfo) => {
      setSession(next);
      setSessionState(next);
      navigate({ screen: "threads" });
    },
    [navigate],
  );

  if (!session) {
    return <Pair onPaired={onPaired} />;
  }

  switch (route.screen) {
    case "chat":
      return (
        <Chat
          threadId={route.threadId}
          onBack={() => navigate({ screen: "threads" })}
          onOpenThread={(id) => navigate({ screen: "chat", threadId: id })}
        />
      );
    case "settings":
      return <Settings onBack={() => navigate({ screen: "threads" })} onSignedOut={() => setSessionState(null)} />;
    default:
      return (
        <Threads
          onOpenThread={(threadId) => navigate({ screen: "chat", threadId })}
          onOpenSettings={() => navigate({ screen: "settings" })}
        />
      );
  }
}
