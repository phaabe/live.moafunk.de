// Type definitions for flv.js
declare module 'flv.js' {
  export interface MediaDataSource {
    type: string;
    url: string;
  }

  export interface Player {
    attachMediaElement(element: HTMLMediaElement): void;
    load(): void;
    unload(): void;
    play(): void;
    pause(): void;
    destroy(): void;
    on(event: string, listener: (...args: unknown[]) => void): void;
    off(event: string, listener: (...args: unknown[]) => void): void;
  }

  export function createPlayer(config: MediaDataSource): Player;
  export function isSupported(): boolean;
}
