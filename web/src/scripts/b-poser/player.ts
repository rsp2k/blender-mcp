// Plays B's loops from b-clip.json: every channel sampled each half frame
// in Blender (onboarding/export_b_assets.py), so the page moves exactly as
// the homepage and the add-on do. Starting a loop eases out of the current
// pose; channels the loop doesn't drive ease back to rest.
import { restOf, type Pose } from "./rig";

export interface Preset {
  action: string;
  frames: number;
  step: number;
  channels: Record<string, number[]>;
}

const BLEND_IN = 0.35; // seconds to ease from the current pose into a loop
const REST_TIME = 0.45; // seconds the Rest preset takes

const smooth = (t: number) => t * t * (3 - 2 * t);

function sample(series: number[], frame: number, step: number): number {
  const n = series.length;
  const x = (frame - 1) / step;
  const i = Math.floor(x);
  const f = x - i;
  const a = series[((i % n) + n) % n];
  const b = series[(((i + 1) % n) + n) % n]; // the last sample flows into the first
  return a + (b - a) * f;
}

type Mode =
  | { kind: "idle" }
  | { kind: "loop"; name: string; preset: Preset; t0: number; cycles: number }
  | { kind: "rest"; t0: number };

export class Player {
  private mode: Mode = { kind: "idle" };
  private from: Pose = new Map();

  constructor(private presets: Record<string, Preset>, private fps: number) {}

  get playing(): string | null {
    if (this.mode.kind === "loop") return this.mode.name;
    if (this.mode.kind === "rest") return "rest";
    return null;
  }

  /** Start a loop; cycles = Infinity repeats until stopped. */
  play(name: string, pose: Pose, now: number, cycles = Infinity): void {
    const preset = this.presets[name];
    if (!preset) return;
    this.from = new Map(pose);
    this.mode = { kind: "loop", name, preset, t0: now, cycles };
  }

  rest(pose: Pose, now: number): void {
    this.from = new Map(pose);
    this.mode = { kind: "rest", t0: now };
  }

  /** Stop easing these channels; a slider has taken them over. */
  release(keys: Iterable<string>): void {
    for (const k of keys) this.from.delete(k);
  }

  stop(): void {
    this.mode = { kind: "idle" };
  }

  /** Write this moment's values into pose. Returns false once idle. */
  tick(pose: Pose, now: number): boolean {
    const m = this.mode;
    if (m.kind === "idle") return false;
    const elapsed = (now - m.t0) / 1000;

    if (m.kind === "rest") {
      const k = smooth(Math.min(1, elapsed / REST_TIME));
      for (const [key, start] of this.from) pose.set(key, start + (restOf(key) - start) * k);
      if (k >= 1) this.stop();
      return true;
    }

    const { preset } = m;
    const frame = 1 + elapsed * this.fps;
    const done = (frame - 1) / preset.frames >= m.cycles;
    const k = smooth(Math.min(1, elapsed / BLEND_IN));
    const keys = new Set([...this.from.keys(), ...Object.keys(preset.channels)]);
    for (const key of keys) {
      const series = preset.channels[key];
      const target = done ? restOf(key) : series ? sample(series, frame, preset.step) : restOf(key);
      const start = this.from.get(key) ?? restOf(key);
      pose.set(key, start + (target - start) * k);
    }
    if (done) this.stop();
    return true;
  }
}
