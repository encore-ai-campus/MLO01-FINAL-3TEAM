import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const preview = new URL('../../pages/design-preview/', import.meta.url);
const output = new URL('../.followup-assets/followup/', import.meta.url);
async function writeStyle(source, target) {
  await writeFile(new URL(target, output), await readFile(new URL(source, preview)));
}

await mkdir(output, { recursive: true });
await Promise.all([
  build({
    entryPoints: [fileURLToPath(new URL('followup.js', import.meta.url))],
    outfile: fileURLToPath(new URL('ui.js', output)),
    nodePaths: [fileURLToPath(new URL('node_modules/', import.meta.url))],
    bundle: true,
    define: { 'process.env.NODE_ENV': '"production"' },
    minify: true,
    legalComments: 'inline',
  }),
  writeStyle('00-Shared/00-DeskLayout.css', 'desk.css'),
  writeStyle('08-FollowUpPage/08-FollowUpPage.css', 'preview.css'),
]);
