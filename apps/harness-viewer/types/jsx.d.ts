// ponytail: local JSX shims so `tsc --noEmit` passes with zero installed deps.
// Upgrade path: add real react/next/convex deps and delete this file when the viewer goes live.
declare global {
  namespace JSX {
    interface IntrinsicElements {
      [tag: string]: Record<string, unknown>;
    }
    interface Element {
      readonly _jsxBrand: unique symbol;
    }
    interface ElementChildrenAttribute {
      children: unknown;
    }
  }
}

export { };
