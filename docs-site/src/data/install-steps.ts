// Copied from web/src/data/install-steps.ts; keep in sync. The web copy is the source of truth for step text.
// First-time install walkthrough shown on the homepage.
//
// Frames are captured from a fresh blender-docker instance (see
// onboarding/README.md) and cropped into public/img/onboarding/web/.
// `spot` is the control to point at, in percent of the cropped frame:
// [left, top, width, height].

export interface InstallStep {
  img: string;
  w: number;
  h: number;
  title: string;
  detail: string;
  alt: string;
  spot?: [number, number, number, number];
}

export interface InstallChapter {
  name: string;
  steps: InstallStep[];
}

export const INSTALL_URL = 'https://mcp.blender.bet/install';

export const chapters: InstallChapter[] = [
  {
    name: 'Add the repository',
    steps: [
      {
        img: '01', w: 1440, h: 810,
        title: 'Open Blender',
        detail: 'Blender 4.2 or newer, with any scene open.',
        alt: 'Blender 5.2 with the Supported Systems mark open in the viewport.',
      },
      {
        img: '02', w: 960, h: 540,
        title: 'Edit › Preferences',
        detail: 'Open the Edit menu and choose Preferences.',
        alt: 'The Edit menu open with Preferences highlighted.',
        spot: [6.5, 48.5, 20.8, 3.4],
      },
      {
        img: '03', w: 900, h: 556,
        title: 'Repositories',
        detail: 'In Get Extensions, open the Repositories menu at the top right.',
        alt: 'The Get Extensions page with the Repositories menu open.',
        spot: [76.2, 2.2, 13.6, 3.2],
      },
      {
        img: '04', w: 852, h: 560,
        title: 'Add Remote Repository',
        detail: 'Click + beside the repository list and choose Add Remote Repository.',
        alt: 'The plus menu open, showing Add Remote Repository and Add Local Repository.',
        spot: [76.1, 13.6, 20.4, 2.9],
      },
      {
        img: '05', w: 852, h: 560,
        title: 'Paste the address',
        detail: 'Paste the address above, leave Check for Updates on Startup ticked, and click Create.',
        alt: 'The Add New Extension Repository dialog with https://mcp.blender.bet/install in the URL field.',
        spot: [69.0, 15.0, 20.5, 3.6],
      },
      {
        img: '06', w: 852, h: 560,
        title: 'Blender MCP appears',
        detail: 'The repository is added, and Blender MCP shows up under Available.',
        alt: 'The repository list now includes mcp.blender.bet, and Blender MCP is listed under Available.',
        spot: [20.7, 13.8, 37.6, 3.8],
      },
    ],
  },
  {
    name: 'Install',
    steps: [
      {
        img: '07', w: 852, h: 560,
        title: 'Install',
        detail: 'Expand the entry to see what it asks for (network and file access), then click Install. It is about 24 MB with its dependencies bundled.',
        alt: 'The Blender MCP entry expanded, showing version, size, permissions and an Install button.',
        spot: [87.3, 14.1, 7.3, 3.2],
      },
      {
        img: '08', w: 852, h: 560,
        title: 'Installed',
        detail: 'Blender downloads and enables the add-on. A BlenderMCP tab appears in the 3D viewport sidebar; press N to show it.',
        alt: 'Blender MCP listed under Installed, with its install path.',
        spot: [21.8, 8.0, 8.2, 2.9],
      },
    ],
  },
  {
    name: 'Log in',
    steps: [
      {
        img: '09', w: 1010, h: 660,
        title: 'Log in',
        detail: 'Open the BlenderMCP tab and click Login. Your web browser opens.',
        alt: 'The BlenderMCP sidebar panel reading Not logged in, with a Login button.',
        spot: [75.0, 13.3, 21.4, 3.0],
      },
      {
        img: '10', w: 1440, h: 810,
        title: 'Approve in the browser',
        detail: 'Sign in and approve access. Blender waits for you in the meantime.',
        alt: 'Blender on the left waiting for sign-in; the browser on the right asking to approve access for BlenderMCP.',
        spot: [63.0, 84.0, 24.0, 4.0],
      },
      {
        img: '11', w: 1440, h: 810,
        title: 'Connected',
        detail: 'Back in Blender the panel reads Connected. You can close the browser tab.',
        alt: 'The panel reads Connected, Personal; the browser shows You are signed in.',
        spot: [27.1, 10.4, 12.5, 4.4],
      },
    ],
  },
  {
    name: 'Afterwards',
    steps: [
      {
        img: '12', w: 760, h: 460,
        title: 'Updates',
        detail: 'When a new version is published, an Update button appears at the top of the panel. One click installs it, with no restart.',
        alt: 'The panel showing an Update to 2026.928.5 button above the connection status.',
        spot: [75.8, 10.0, 19.7, 3.0],
      },
    ],
  },
];
