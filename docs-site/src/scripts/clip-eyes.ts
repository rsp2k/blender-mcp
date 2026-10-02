// Copied from web/src/scripts/clip-eyes.ts; keep in sync. Used by the docs
// header logo (components/SiteTitle.astro), a one-frame "sheet".
// B. Clip on a canvas, with pupils that look at the pointer.
//
// The body loop comes from a sprite sheet rendered in Blender without pupils
// (onboarding/build_clip_mascot.py --eyes-web). For every frame, eyes.json
// holds each eye as an ellipse in sheet pixels: centre c and semi-axis
// vectors u (across the plate) and v (up the plate; it collapses in the
// blink). A pupil is a circle in that eye's own space, so one affine
// transform per eye puts it on the tilted, squashed plate correctly, and
// clipping to the unit circle keeps it inside the eye white.

type Vec = [number, number];
interface Eye { c: Vec; u: Vec; v: Vec; look: Vec }
interface EyeData {
  size: number;
  frames: number;
  fps: number;
  pupil: Vec;
  glint: { offset: Vec; r: number };
  pupil_colour: string;
  eyes: Record<"L" | "R", Eye>[];
}

const COLS = 12;
const IDLE_MS = 2500; // after this long without pointer movement, B looks on his own
const REACH_PX = 220; // pointer distance (CSS px) at which the pupils reach the edge
const EASE = 0.18; // per-frame easing toward the target look

export function startClipEyes(root: HTMLElement, sheetUrl: string, data: EyeData): void {
  const canvas = root.querySelector<HTMLCanvasElement>("canvas");
  const still = root.querySelector<HTMLElement>("picture");
  if (!canvas || !still) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const sheet = new Image();
  sheet.decoding = "async";
  sheet.src = sheetUrl;

  const maxTravel: Vec = [1 - data.pupil[0], 1 - data.pupil[1]].map((m) => m * 0.92) as Vec;
  const current: Record<"L" | "R", Vec> = { L: [0, 0], R: [0, 0] };
  let pointer: Vec | null = null;
  let lastMove = -Infinity;
  let visible = true;
  let started = 0;

  window.addEventListener(
    "pointermove",
    (e) => {
      pointer = [e.clientX, e.clientY];
      lastMove = performance.now();
    },
    { passive: true },
  );
  document.addEventListener("pointerleave", () => {
    pointer = null;
  });
  new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    if (visible) requestAnimationFrame(draw);
  }).observe(root);

  function resize(): number {
    const css = canvas!.getBoundingClientRect().width || 240;
    const px = Math.round(css * Math.min(window.devicePixelRatio || 1, 2));
    if (canvas!.width !== px) canvas!.width = canvas!.height = px;
    return css;
  }

  // Where the pupil should sit for this eye: toward the pointer when it's
  // active, otherwise the loop's own glance.
  function target(eye: Eye, rect: DOMRect, cssScale: number, now: number): Vec {
    if (!pointer || now - lastMove > IDLE_MS) return eye.look;
    const cx = rect.left + eye.c[0] * cssScale;
    const cy = rect.top + eye.c[1] * cssScale;
    const dx = pointer[0] - cx;
    const dy = pointer[1] - cy;
    // Screen direction in the eye's own (u, v) basis: solve [u v] [a b]^T = d.
    const [ux, uy] = eye.u;
    const [vx, vy] = eye.v;
    const det = ux * vy - vx * uy;
    if (Math.abs(det) < 1e-3) return eye.look;
    let a = (dx * vy - vx * dy) / det;
    let b = (ux * dy - dx * uy) / det;
    const len = Math.hypot(a, b) || 1;
    const reach = Math.min(1, Math.hypot(dx, dy) / REACH_PX);
    a = (a / len) * reach * maxTravel[0];
    b = (b / len) * reach * maxTravel[1];
    return [a, b];
  }

  function drawPupil(eye: Eye, look: Vec, k: number): void {
    const [ux, uy] = eye.u;
    const [vx, vy] = eye.v;
    if (Math.abs(ux * vy - vx * uy) < 0.5) return; // eyelid shut (the blink)
    ctx!.save();
    ctx!.setTransform(ux * k, uy * k, vx * k, vy * k, eye.c[0] * k, eye.c[1] * k);
    ctx!.beginPath();
    ctx!.arc(0, 0, 1, 0, Math.PI * 2);
    ctx!.clip();
    ctx!.fillStyle = data.pupil_colour;
    ctx!.beginPath();
    ctx!.ellipse(look[0], look[1], data.pupil[0], data.pupil[1], 0, 0, Math.PI * 2);
    ctx!.fill();
    ctx!.fillStyle = "rgba(255, 255, 255, 0.95)";
    ctx!.beginPath();
    ctx!.arc(look[0] + data.glint.offset[0], look[1] + data.glint.offset[1], data.glint.r, 0, Math.PI * 2);
    ctx!.fill();
    ctx!.restore();
  }

  function draw(now: number): void {
    if (!visible) return;
    const css = resize();
    const k = canvas!.width / data.size;
    // rAF's timestamp is the frame's start, which can be a hair earlier than
    // the performance.now() taken at load: keep the index non-negative.
    const tick = Math.floor(((now - started) / 1000) * data.fps);
    const frame = ((tick % data.frames) + data.frames) % data.frames;
    const col = frame % COLS;
    const row = Math.floor(frame / COLS);
    ctx!.setTransform(1, 0, 0, 1, 0, 0);
    ctx!.clearRect(0, 0, canvas!.width, canvas!.height);
    ctx!.drawImage(sheet, col * data.size, row * data.size, data.size, data.size,
      0, 0, canvas!.width, canvas!.height);
    const rect = canvas!.getBoundingClientRect();
    const eyes = data.eyes[frame];
    for (const side of ["L", "R"] as const) {
      const want = target(eyes[side], rect, css / data.size, now);
      const cur = current[side];
      cur[0] += (want[0] - cur[0]) * EASE;
      cur[1] += (want[1] - cur[1]) * EASE;
      drawPupil(eyes[side], cur, k);
    }
    requestAnimationFrame(draw);
  }

  sheet.addEventListener("load", () => {
    current.L = [...data.eyes[0].L.look] as Vec;
    current.R = [...data.eyes[0].R.look] as Vec;
    started = performance.now();
    requestAnimationFrame((t) => {
      draw(t);
      // Swap only once the first frame is on the canvas, so nothing flashes.
      canvas.hidden = false;
      still.hidden = true;
    });
  });
}
