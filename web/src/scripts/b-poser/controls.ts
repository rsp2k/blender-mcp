// The sliders on /b: what each one is called, its range, and which rig
// channels it drives. The page renders its markup from this list and the
// client maps values through it, so the two never drift apart.
//
// A channel is "bone/property/axis", the same keys b-clip.json uses.
import { POSE_LIMITS as L } from "./limits";

export interface SliderDef {
  id: string;
  label: string;
  group: "Handles" | "Body" | "Eyes";
  min: number;
  max: number;
  step: number;
  channels: string[];
  /** Slider value to channel value. */
  toChannel: (v: number) => number;
  /** Channel value back to slider value (for playback). */
  fromChannel: (c: number) => number;
  /** Readout text for a slider value. */
  format: (v: number) => string;
}

const deg = (v: number) => `${Math.round(v)}°`;
const side = (v: number, neg: string, pos: string, unit = "%") => {
  const r = Math.round(Math.abs(v));
  return r === 0 ? "centre" : `${v < 0 ? neg : pos} ${r}${unit}`;
};
// Percent of each side's own reach, so 100% is the limit either way.
const toReach = (min: number, max: number) => (v: number) =>
  v >= 0 ? (v / 100) * max : (v / 100) * -min;
const fromReach = (min: number, max: number) => (c: number) =>
  c >= 0 ? (c / max) * 100 : (c / -min) * 100;
const identity = (v: number) => v;

export const SLIDERS: SliderDef[] = [
  {
    id: "front", label: "Front handle", group: "Handles",
    min: L.frontHandle.min, max: L.frontHandle.max, step: 1,
    channels: ["handle.front/rotation_euler/1"],
    toChannel: identity, fromChannel: identity, format: deg,
  },
  {
    id: "back", label: "Back handle", group: "Handles",
    min: L.backHandle.min, max: L.backHandle.max, step: 1,
    channels: ["handle.back/rotation_euler/1"],
    toChannel: identity, fromChannel: identity, format: deg,
  },
  {
    // Body Z faces the viewer, so a positive Z turn tips him to their left.
    id: "lean", label: "Lean", group: "Body",
    min: L.lean.min, max: L.lean.max, step: 0.5,
    channels: ["body/rotation_euler/2"],
    toChannel: (v) => -v, fromChannel: (c) => -c,
    format: (v) => side(v, "left", "right", "°"),
  },
  {
    id: "lookx", label: "Look left/right", group: "Eyes",
    min: -100, max: 100, step: 1,
    channels: ["pupil.L/location/0", "pupil.R/location/0"],
    toChannel: toReach(L.lookX.min, L.lookX.max),
    fromChannel: fromReach(L.lookX.min, L.lookX.max),
    format: (v) => side(v, "left", "right"),
  },
  {
    id: "looky", label: "Look up/down", group: "Eyes",
    min: -100, max: 100, step: 1,
    channels: ["pupil.L/location/1", "pupil.R/location/1"],
    toChannel: toReach(L.lookY.min, L.lookY.max),
    fromChannel: fromReach(L.lookY.min, L.lookY.max),
    format: (v) => side(v, "down", "up"),
  },
  {
    id: "blink", label: "Blink", group: "Eyes",
    min: 0, max: 100, step: 1,
    channels: ["eye.L/scale/1", "eye.R/scale/1"],
    toChannel: (v) => 1 - (v / 100) * (1 - L.blinkClosed),
    fromChannel: (c) => ((1 - c) / (1 - L.blinkClosed)) * 100,
    format: (v) => (Math.round(v) === 0 ? "open" : Math.round(v) >= 100 ? "shut" : `${Math.round(v)}%`),
  },
];
