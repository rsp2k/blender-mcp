// How far each control on /b may move B. One place, so the measured
// no-clipping range can drop in without touching anything else.
//
// Units follow Blender's pose bones (see onboarding/build_clip_mascot.py):
// handles and lean in degrees, pupil travel in scene metres along the plate,
// blink as the eye's Y scale. The hero loop uses front -5..12, back -5..5,
// lean +-2.5, pupils 0..0.085 across and 0..0.06 up; these add a margin.
// TODO: replace the handle ranges with the measured ones once they land.
export const POSE_LIMITS = {
  frontHandle: { min: -10, max: 20 }, // + swings forward and down
  backHandle: { min: -12, max: 12 },
  lean: { min: -8, max: 8 }, // + leans to the viewer's right
  lookX: { min: -0.06, max: 0.1 }, // + toward the viewer's right
  lookY: { min: -0.06, max: 0.07 }, // + up the plate
  blinkClosed: 0.06, // eye Y scale with Blink at 100%
} as const;
