// B's joints in three.js, posed with Blender's own pose-bone values.
//
// The glTF exporter keeps each bone's axes (only armature space turns
// Y-up) and writes joints at rest, so a Blender pose applies on top of the
// rest transform exactly as Blender composes it:
//   position = rest + restRotation * location
//   rotation = restRotation * euler (Blender "XYZ" = three "ZYX")
//   scale    = restScale * scale
// onboarding/export_b_assets.py checks the hierarchy this relies on.
import { Euler, Quaternion, Vector3, type Object3D } from "three";

export type Pose = Map<string, number>;

export const BONES = [
  "root", "body", "handle.front", "handle.back", "face",
  "eye.L", "eye.R", "pupil.L", "pupil.R",
] as const;

const PROPS = ["location", "rotation_euler", "scale"] as const;
const DEG = Math.PI / 180;

/** Rest value of a channel key like "eye.L/scale/1". */
export function restOf(key: string): number {
  return key.includes("/scale/") ? 1 : 0;
}

interface Joint {
  node: Object3D;
  pos: Vector3;
  quat: Quaternion;
  scale: Vector3;
}

export class Rig {
  private joints = new Map<string, Joint>();
  private euler = new Euler(0, 0, 0, "ZYX");
  private q = new Quaternion();
  private v = new Vector3();

  constructor(root: Object3D) {
    // GLTFLoader strips "." from node names but keeps the original in userData.
    root.traverse((node) => {
      const name = (node.userData?.name as string | undefined) ?? node.name;
      if ((BONES as readonly string[]).includes(name) && !this.joints.has(name)) {
        this.joints.set(name, {
          node,
          pos: node.position.clone(),
          quat: node.quaternion.clone(),
          scale: node.scale.clone(),
        });
      }
    });
    const missing = BONES.filter((b) => !this.joints.has(b));
    if (missing.length) throw new Error(`b-clip.glb lacks joints: ${missing.join(", ")}`);
  }

  apply(pose: Pose, overlay?: Pose): void {
    const get = (bone: string, prop: string, axis: number) => {
      const key = `${bone}/${prop}/${axis}`;
      let value = pose.get(key) ?? restOf(key);
      const extra = overlay?.get(key);
      if (extra !== undefined) value = prop === "scale" ? value * extra : value + extra;
      return value;
    };
    for (const [bone, j] of this.joints) {
      const [loc, rot, scl] = PROPS.map((p) => [0, 1, 2].map((a) => get(bone, p, a)));
      this.v.set(loc[0], loc[1], loc[2]).applyQuaternion(j.quat);
      j.node.position.copy(j.pos).add(this.v);
      this.euler.set(rot[0] * DEG, rot[1] * DEG, rot[2] * DEG, "ZYX");
      j.node.quaternion.copy(j.quat).multiply(this.q.setFromEuler(this.euler));
      j.node.scale.set(j.scale.x * scl[0], j.scale.y * scl[1], j.scale.z * scl[2]);
    }
  }
}
