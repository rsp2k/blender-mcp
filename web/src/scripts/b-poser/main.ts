// Wires the /b page to B: sliders and presets write one shared pose, the
// render loop applies it to the rig and draws only when something moved.
import data from "../../data/b-clip.json";
import { SLIDERS, type SliderDef } from "./controls";
import { Player, type Preset } from "./player";
import { Rig, restOf, type Pose } from "./rig";
import { createStage, type Stage } from "./stage";

const GLB_URL = "/b/b-clip.glb";
const PNG_NAME = "b-clip-pose.png";
const SLIDER_KEYS = new Set(SLIDERS.flatMap((s) => s.channels));
const DEG = Math.PI / 180;

// Idle life between presets: a slow breath and a blink every few seconds.
// Laid over the pose at draw time, so it never moves a slider.
const BREATH = { amp: 0.012, period: 3.8 };
const BLINK = { every: [2.8, 6.0], length: 0.16, shut: 0.1 };

function idleOverlay(now: number, nextBlink: number): Pose {
  const t = now / 1000;
  const overlay: Pose = new Map();
  overlay.set("body/location/1", BREATH.amp * (0.5 - 0.5 * Math.cos((2 * Math.PI * t) / BREATH.period)));
  const into = (now - nextBlink) / 1000;
  if (into > 0 && into < BLINK.length) {
    const k = 1 - Math.sin((Math.PI * into) / BLINK.length) * (1 - BLINK.shut);
    overlay.set("eye.L/scale/1", k);
    overlay.set("eye.R/scale/1", k);
  }
  return overlay;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export async function startPoser(root: HTMLElement): Promise<void> {
  const $ = <T extends Element>(sel: string) => root.querySelector<T>(sel);
  const canvas = $<HTMLCanvasElement>("[data-b-canvas]");
  const still = $<HTMLElement>("[data-b-still]");
  const loading = $<HTMLElement>("[data-b-loading]");
  const loadingText = $<HTMLElement>("[data-b-loading-text]");
  const progress = $<HTMLElement>("[data-b-progress]");
  const status = $<HTMLElement>("[data-b-status]");
  const hint = $<HTMLElement>("[data-b-hint]");
  if (!canvas || !still || !loading) return;

  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const say = (text: string) => {
    if (status) status.textContent = text;
  };

  let stage: Stage;
  try {
    stage = await createStage(canvas, GLB_URL, {
      calm,
      onProgress: (f) => {
        progress?.style.setProperty("--b-progress", String(f));
        if (loadingText) loadingText.textContent = `Loading B in 3D, ${Math.round(f * 100)}%`;
      },
    });
  } catch (err) {
    console.warn("[b] 3D view unavailable:", err);
    loading.classList.add("is-failed");
    if (loadingText) {
      loadingText.textContent = "B couldn't load in 3D here, so this is his still. The .blend download works either way.";
    }
    return;
  }

  const rig = new Rig(stage.model);
  const player = new Player(data.presets as Record<string, Preset>, data.fps);
  const pose: Pose = new Map();
  let dirty = true;
  let visible = true;
  let nextBlink = performance.now() + 2500;

  // ------------------------------------------------------------ sliders
  const sliders = SLIDERS.map((def) => ({
    def,
    input: $<HTMLInputElement>(`[data-b-slider="${def.id}"]`)!,
    readout: $<HTMLOutputElement>(`[data-b-readout="${def.id}"]`)!,
  })).filter((s) => s.input);

  const showValue = (def: SliderDef, input: HTMLInputElement, readout: HTMLElement | null, v: number) => {
    input.value = String(v);
    const pct = (x: number) => ((x - def.min) / (def.max - def.min)) * 100;
    const zero = pct(clamp(0, def.min, def.max));
    input.style.setProperty("--b-from", `${Math.min(zero, pct(v))}%`);
    input.style.setProperty("--b-to", `${Math.max(zero, pct(v))}%`);
    input.setAttribute("aria-valuetext", def.format(v));
    if (readout) readout.textContent = def.format(v);
  };

  const syncSliders = () => {
    for (const { def, input, readout } of sliders) {
      const c = pose.get(def.channels[0]) ?? restOf(def.channels[0]);
      showValue(def, input, readout, clamp(def.fromChannel(c), def.min, def.max));
    }
  };

  // ------------------------------------------------------------ presets
  const presetButtons = [...root.querySelectorAll<HTMLButtonElement>("[data-b-preset]")];
  let shownPlaying: string | null = null;
  const syncPresets = () => {
    const playing = player.playing;
    if (playing === shownPlaying) return;
    shownPlaying = playing;
    for (const b of presetButtons) {
      const name = b.dataset.bPreset!;
      if (name !== "rest") b.setAttribute("aria-pressed", String(playing === name));
    }
  };

  const stopForSlider = (now: number) => {
    if (!player.playing) return;
    if (player.playing === "rest") {
      player.release(SLIDER_KEYS);
      return;
    }
    // Keep where the sliders are; ease the loop-only channels (bob, squash) home.
    const others: Pose = new Map([...pose].filter(([k]) => !SLIDER_KEYS.has(k)));
    player.rest(others, now);
    syncPresets();
  };

  for (const { def, input, readout } of sliders) {
    input.addEventListener("pointerdown", () => stopForSlider(performance.now()));
    input.addEventListener("input", () => {
      stopForSlider(performance.now());
      const v = Number(input.value);
      for (const ch of def.channels) pose.set(ch, def.toChannel(v));
      showValue(def, input, readout, v);
      dirty = true;
    });
  }

  for (const b of presetButtons) {
    b.addEventListener("click", () => {
      const name = b.dataset.bPreset!;
      const now = performance.now();
      if (name === "rest") {
        if (calm) {
          player.stop();
          pose.clear();
          syncSliders();
          dirty = true;
        } else {
          player.rest(pose, now);
        }
        say("B is back at rest.");
      } else if (player.playing === name) {
        player.stop();
        say(`${b.textContent?.trim()} paused. The sliders hold his pose.`);
      } else {
        player.play(name, pose, now);
        say(`Playing ${b.textContent?.trim()}. Touch any slider to stop.`);
      }
      syncPresets();
    });
  }

  // ------------------------------------------------------------ actions
  $<HTMLButtonElement>("[data-b-reset-view]")?.addEventListener("click", () => {
    stage.resetView();
    dirty = true;
  });

  $<HTMLButtonElement>("[data-b-save]")?.addEventListener("click", async () => {
    rig.apply(pose); // no idle blink caught mid-way
    try {
      const blob = await stage.savePng();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = PNG_NAME;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      say(`Saved ${PNG_NAME}, with a clear background.`);
    } catch (err) {
      console.warn("[b] PNG save failed:", err);
      say("Saving the picture didn't work in this browser.");
    }
    dirty = true;
  });

  canvas.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 25 : 10;
    const moves: Record<string, [number, number, number]> = {
      ArrowLeft: [-step * DEG, 0, 1],
      ArrowRight: [step * DEG, 0, 1],
      ArrowUp: [0, -step * 0.6 * DEG, 1],
      ArrowDown: [0, step * 0.6 * DEG, 1],
      "+": [0, 0, 0.9],
      "=": [0, 0, 0.9],
      "-": [0, 0, 1.1],
    };
    if (e.key === "Home" || e.key === "0") {
      stage.resetView();
    } else if (moves[e.key]) {
      stage.nudge(...moves[e.key]);
    } else {
      return;
    }
    e.preventDefault();
    dirty = true;
  });

  stage.onChange(() => {
    dirty = true;
  });

  // ------------------------------------------------------------ loop
  new IntersectionObserver((entries) => {
    visible = entries.some((e) => e.isIntersecting);
    if (visible) dirty = true;
  }).observe(canvas);

  const frame = (now: number) => {
    requestAnimationFrame(frame);
    if (!visible || document.hidden) return;
    let moving = false;
    if (player.tick(pose, now)) {
      syncSliders();
      moving = true;
    }
    syncPresets();
    let overlay: Pose | undefined;
    if (!calm && !player.playing) {
      overlay = idleOverlay(now, nextBlink);
      if (now - nextBlink > BLINK.length * 1000) {
        const [lo, hi] = BLINK.every;
        nextBlink = now + (lo + Math.random() * (hi - lo)) * 1000;
      }
      moving = true;
    }
    if (stage.updateControls()) moving = true;
    if (moving || dirty) {
      rig.apply(pose, overlay);
      stage.render();
      dirty = false;
    }
  };

  // First frame, then swap the still for the live canvas.
  rig.apply(pose);
  syncSliders();
  stage.render();
  canvas.hidden = false;
  root.classList.add("is-live");
  loading.hidden = true;
  if (hint) hint.hidden = false;
  for (const el of root.querySelectorAll<HTMLButtonElement | HTMLInputElement>("[data-b-needs-3d]")) {
    el.disabled = false;
  }
  if (!calm) {
    // One wave hello, then idle. Never with reduced motion.
    player.play("hello", pose, performance.now(), 1);
  }
  requestAnimationFrame(frame);
}
