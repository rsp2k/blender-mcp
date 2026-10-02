// The 3D stage for /b: B on a soft studio floor, lit like his Blender
// renders (key, fill, two blue rims), with orbit controls that stay in
// front of him. Transparent canvas, so the page's own backdrop and theme
// show through and a saved PNG keeps a clear background.
import {
  AgXToneMapping,
  Box3,
  CanvasTexture,
  Color,
  DirectionalLight,
  Group,
  HemisphereLight,
  Mesh,
  MeshBasicMaterial,
  PerspectiveCamera,
  PlaneGeometry,
  PMREMGenerator,
  Scene,
  ShadowMaterial,
  Sphere,
  SRGBColorSpace,
  Vector3,
  WebGLRenderer,
  type Object3D,
  PCFShadowMap,
} from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";

const FOV = 30; // close to the 70 mm lens of B's hero render
// The hero render's camera: azimuth -38 deg, elevation 16 deg, in Blender
// terms; converted to three's Y-up below.
const HERO_AZIMUTH = (-38 * Math.PI) / 180;
const HERO_ELEVATION = (16 * Math.PI) / 180;
const RIM_BLUE = 0x3b82f6;
const PNG_LONG_EDGE = 1600;

/** Lights placed where build_clip_mascot.py puts them (Blender Z-up to Y-up). */
const blenderToThree = (x: number, y: number, z: number) => new Vector3(x, z, -y);

function contactShadowTexture(): CanvasTexture {
  const size = 128;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d")!;
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, "rgba(0,0,0,0.55)");
  grad.addColorStop(0.45, "rgba(0,0,0,0.25)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  const tex = new CanvasTexture(c);
  tex.colorSpace = SRGBColorSpace;
  return tex;
}

export interface Stage {
  model: Object3D;
  render: () => void;
  /** True while the camera is still easing after a drag. */
  updateControls: () => boolean;
  resetView: () => void;
  nudge: (dAzimuth: number, dPolar: number, zoom: number) => void;
  savePng: () => Promise<Blob>;
  onChange: (fn: () => void) => void;
  dispose: () => void;
}

export async function createStage(
  canvas: HTMLCanvasElement,
  glbUrl: string,
  opts: { calm: boolean; onProgress?: (fraction: number) => void },
): Promise<Stage> {
  const renderer = new WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = SRGBColorSpace;
  renderer.toneMapping = AgXToneMapping;
  renderer.toneMappingExposure = 1.15;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = PCFShadowMap;
  renderer.setClearColor(0x000000, 0);

  const scene = new Scene();
  const pmrem = new PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  scene.environment = pmrem.fromScene(room, 0.04).texture;
  scene.environmentIntensity = 0.55;
  room.dispose();

  // Soft studio: a wide sky/floor bounce, a key that casts the shadow, a
  // gentle fill, and the two blue rims from behind that outline the steel.
  scene.add(new HemisphereLight(0xdfe8f5, 0x20242c, 0.6));
  const key = new DirectionalLight(0xffffff, 2.4);
  key.position.copy(blenderToThree(-3.5, -4.5, 5.0));
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.radius = 6;
  key.shadow.bias = -0.0005;
  const cam = key.shadow.camera;
  cam.left = cam.bottom = -3;
  cam.right = cam.top = 3;
  cam.near = 0.5;
  cam.far = 20;
  scene.add(key);
  const fill = new DirectionalLight(0xf2f6ff, 0.8);
  fill.position.copy(blenderToThree(4.5, -3.5, 1.8));
  scene.add(fill);
  for (const x of [-1.3, 1.3]) {
    const rim = new DirectionalLight(new Color(RIM_BLUE), 2.2);
    rim.position.copy(blenderToThree(x, 5.0, 3.0));
    scene.add(rim);
  }

  const gltf = await new GLTFLoader().loadAsync(glbUrl, (e) => {
    if (e.lengthComputable && opts.onProgress) opts.onProgress(e.loaded / e.total);
  });
  const model = gltf.scene;
  model.traverse((o) => {
    if ((o as Mesh).isMesh) {
      o.castShadow = true;
      o.receiveShadow = false;
    }
  });
  scene.add(model);

  // Floor: a shadow catcher for the key light plus a soft contact blob.
  const box = new Box3().setFromObject(model);
  const sphere = box.getBoundingSphere(new Sphere());
  const floor = new Group();
  const catcher = new Mesh(new PlaneGeometry(12, 12), new ShadowMaterial({ opacity: 0.22 }));
  catcher.rotation.x = -Math.PI / 2;
  catcher.receiveShadow = true;
  const blobSize = Math.max(box.max.x - box.min.x, box.max.z - box.min.z) * 1.5;
  const blob = new Mesh(
    new PlaneGeometry(blobSize, blobSize * 0.75),
    new MeshBasicMaterial({ map: contactShadowTexture(), transparent: true, depthWrite: false }),
  );
  blob.rotation.x = -Math.PI / 2;
  blob.position.y = 0.002;
  floor.add(catcher, blob);
  floor.position.y = box.min.y;
  scene.add(floor);

  const camera = new PerspectiveCamera(FOV, 1, 0.1, 100);
  const target = sphere.center.clone();
  const heroDir = blenderToThree(
    Math.sin(HERO_AZIMUTH) * Math.cos(HERO_ELEVATION),
    -Math.cos(HERO_AZIMUTH) * Math.cos(HERO_ELEVATION),
    Math.sin(HERO_ELEVATION),
  ).normalize();
  // The bounding sphere is generous (the handles reach wide), so stand a
  // little inside it; B still clears the frame at every loop pose.
  const fitDistance = (sphere.radius / Math.sin((FOV * Math.PI) / 360)) * 0.8;

  const controls = new OrbitControls(camera, canvas);
  controls.target.copy(target);
  controls.enablePan = false;
  controls.enableDamping = !opts.calm;
  controls.dampingFactor = 0.08;
  controls.minDistance = fitDistance * 0.6;
  controls.maxDistance = fitDistance * 1.6;
  controls.minPolarAngle = 0.25; // not straight down on his jaws
  controls.maxPolarAngle = Math.PI / 2 - 0.02; // and never under the floor
  controls.rotateSpeed = 0.8;

  const resetView = () => {
    camera.position.copy(target).addScaledVector(heroDir, fitDistance);
    camera.lookAt(target);
    controls.update();
  };
  resetView();

  const resize = () => {
    const w = canvas.clientWidth || 1;
    const h = canvas.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // Keep B whole on narrow, tall stages: widen the vertical view instead.
    camera.fov = w < h ? (Math.atan(Math.tan((FOV * Math.PI) / 360) * (h / w)) * 360) / Math.PI : FOV;
    camera.updateProjectionMatrix();
  };
  resize();
  const changeFns: (() => void)[] = [];
  const ro = new ResizeObserver(() => {
    resize();
    changeFns.forEach((f) => f());
  });
  ro.observe(canvas);
  controls.addEventListener("change", () => changeFns.forEach((f) => f()));

  const render = () => renderer.render(scene, camera);

  const nudge = (dAzimuth: number, dPolar: number, zoom: number) => {
    const offset = camera.position.clone().sub(controls.target);
    const r = offset.length();
    const theta = Math.atan2(offset.x, offset.z) + dAzimuth;
    let phi = Math.acos(Math.min(1, Math.max(-1, offset.y / r))) + dPolar;
    phi = Math.min(controls.maxPolarAngle, Math.max(controls.minPolarAngle, phi));
    const dist = Math.min(controls.maxDistance, Math.max(controls.minDistance, r * zoom));
    offset.set(Math.sin(phi) * Math.sin(theta), Math.cos(phi), Math.sin(phi) * Math.cos(theta));
    camera.position.copy(controls.target).addScaledVector(offset, dist);
    camera.lookAt(controls.target);
    controls.update();
  };

  const savePng = () =>
    new Promise<Blob>((resolve, reject) => {
      // Render once at a larger size, grab it in the same task (before the
      // browser presents and clears the buffer), then put the canvas back.
      const w = canvas.clientWidth || 1;
      const h = canvas.clientHeight || 1;
      const k = PNG_LONG_EDGE / Math.max(w, h);
      const ratio = renderer.getPixelRatio();
      renderer.setPixelRatio(1);
      renderer.setSize(Math.round(w * k), Math.round(h * k), false);
      render();
      canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("toBlob gave nothing"))), "image/png");
      renderer.setPixelRatio(ratio);
      resize();
      render();
    });

  return {
    model,
    render,
    updateControls: () => controls.update(),
    resetView,
    nudge,
    savePng,
    onChange: (fn) => changeFns.push(fn),
    dispose: () => {
      ro.disconnect();
      controls.dispose();
      pmrem.dispose();
      renderer.dispose();
    },
  };
}
