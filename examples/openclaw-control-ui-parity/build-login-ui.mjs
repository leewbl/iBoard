import { mkdir, writeFile } from 'node:fs/promises';
import { build } from '/app/node_modules/vite/dist/node/index.js';
import config from '/app/ui/vite.config.ts';

// The stock login page has no plugin settings. Skip rebuilding packaged settings
// in this experiment, keeping the OpenClaw application and Vite plugins intact.
process.env.OPENCLAW_CONTROL_UI_PREPARED_SETTINGS_ROOT = '/app';
process.env.OPENCLAW_CONTROL_UI_REQUIRE_PLUGIN_DISTRIBUTIONS = '0';
const outDir = '/app/dist/control-ui';
await mkdir(outDir, { recursive: true });
await build({ ...config({ outDir, command: 'build' }), root: '/app/ui', configFile: false, logLevel: 'warn' });
await writeFile(outDir + '/control-ui-config.json', JSON.stringify({
  basePath: '/', assistantName: '', assistantAvatar: '', communityInvite: true,
}));
