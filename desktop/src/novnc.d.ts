// Minimal types for noVNC's RFB client, which ships JavaScript only.
// `@novnc/novnc` exports exactly one module: `core/rfb.js`.
declare module "@novnc/novnc" {
  export interface RFBCredentials {
    username?: string;
    password?: string;
    target?: string;
  }

  export interface RFBOptions {
    shared?: boolean;
    viewOnly?: boolean;
    scaleViewport?: boolean;
    resizeSession?: boolean;
    clipViewport?: boolean;
    wsProtocols?: string[];
    credentials?: RFBCredentials;
  }

  /** The events this pane listens to, with the details noVNC 1.7 puts on them. */
  export interface RFBEventMap {
    connect: Event;
    disconnect: CustomEvent<{ clean: boolean }>;
    securityfailure: CustomEvent<{ status: number; reason?: string }>;
    credentialsrequired: CustomEvent<{ types: string[] }>;
  }

  export default class RFB {
    constructor(target: HTMLElement, url: string, options?: RFBOptions);
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    clipViewport: boolean;
    addEventListener<K extends keyof RFBEventMap & string>(
      type: K,
      listener: (event: RFBEventMap[K]) => void,
    ): void;
    addEventListener(type: string, listener: (event: Event) => void): void;
    removeEventListener(type: string, listener: (event: Event) => void): void;
    disconnect(): void;
    focus(): void;
    sendCredentials(credentials: RFBCredentials): void;
  }
}
