/** Presentation-only sizing policy for the inline Workbench composer. */
export const WORKBENCH_COMPOSER_MIN_HEIGHT_PX = 52;
export const WORKBENCH_COMPOSER_MAX_HEIGHT_PX = 220;

export interface WorkbenchComposerSizeV1 {
  heightPx: number;
  overflowY: "hidden" | "auto";
}

export function workbenchComposerSize(scrollHeight: number): WorkbenchComposerSizeV1 {
  const measuredHeight = Number.isFinite(scrollHeight) ? Math.max(0, scrollHeight) : 0;
  return {
    heightPx: Math.min(Math.max(measuredHeight, WORKBENCH_COMPOSER_MIN_HEIGHT_PX), WORKBENCH_COMPOSER_MAX_HEIGHT_PX),
    overflowY: measuredHeight > WORKBENCH_COMPOSER_MAX_HEIGHT_PX ? "auto" : "hidden",
  };
}
