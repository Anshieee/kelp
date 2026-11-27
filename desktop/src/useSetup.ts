// Setup status tracking, kept out of the .tsx so node --test can load it.

import { useCallback, useEffect, useState } from "react";

import { UNKNOWN_FAILURE, parseSetupStatus, type SetupAction, type SetupStatus } from "./setup";
import type { SetupDriver } from "./setupDriver";

/**
 * Track Rust's status. `null` until the first answer. A pushed event always
 * wins over the initial query, which may have been answered before it. A
 * rejected command shows the generic failure rather than leaving the screen
 * stuck on a disabled button.
 */
export function useSetup(driver: SetupDriver) {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let disposed = false;
    let pushed = false;
    const unsubscribe = driver.subscribe((value) => {
      if (disposed) return;
      pushed = true;
      setBusy(false);
      setStatus(parseSetupStatus(value));
    });
    driver.status().then(
      (value) => {
        if (!disposed && !pushed) setStatus(parseSetupStatus(value));
      },
      () => {
        if (!disposed && !pushed) setStatus(UNKNOWN_FAILURE);
      },
    );
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, [driver]);

  const act = useCallback(
    (action: SetupAction) => {
      setBusy(true);
      const sent =
        action === "local" || action === "hosted"
          ? driver.choose(action)
          : action === "retry"
            ? driver.retry()
            : driver.quit();
      sent.catch(() => {
        setBusy(false);
        setStatus(UNKNOWN_FAILURE);
      });
    },
    [driver],
  );

  return { status, busy, act };
}
