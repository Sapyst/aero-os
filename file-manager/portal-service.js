#!/usr/bin/env node
const dbus = require('dbus-next');
const { spawn } = require('child_process');
const path = require('path');
const { pathToFileURL } = require('url');

const APP_PATH = process.env.APP_PATH || path.dirname(require.main.filename);
const ELECTRON_BIN = process.env.ELECTRON_BIN || 'electron';
const SERVICE_NAME = 'org.freedesktop.impl.portal.myfm';
const OBJECT_PATH = '/org/freedesktop/portal/desktop';

// [Aero/doc 43] current_folder chega como bytes terminados em \0 (ay)
function folderOf(options) {
  const v = options.current_folder?.value;
  if (!v) return null;
  const s = Buffer.from(v).toString('utf8').replace(/\0+$/, '');
  return s.startsWith('/') ? s : null;
}

function launchPicker(mode, multiple, currentName, folder) {
  return new Promise((resolve) => {
    const args = ['--picker', `--mode=${mode}`];
    if (multiple) args.push('--multiple');
    // the suggested name comes from the requesting app: keep only a plain file name (no ../, no /)
    const safeName = currentName ? path.basename(String(currentName)).replace(/[\r\n]/g, '') : '';
    if (safeName && safeName !== '.' && safeName !== '..' && mode === 'save') args.push(`--filename=${safeName}`);
    if (folder) args.push(folder);

    console.log(`[Portal] Launching: ${ELECTRON_BIN} ${APP_PATH} ${args.join(' ')}`);

    const child = spawn(
      ELECTRON_BIN,
      [
        APP_PATH,
        '--ozone-platform=wayland',
        '--enable-features=UseOzonePlatform',
        ...args,
      ],
      {
        cwd: APP_PATH,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
          ...process.env,
          DISPLAY: process.env.DISPLAY || ':0',
          WAYLAND_DISPLAY: process.env.WAYLAND_DISPLAY || 'wayland-1',
          XDG_RUNTIME_DIR: process.env.XDG_RUNTIME_DIR || '/run/user/1000',
          ELECTRON_OZONE_PLATFORM_HINT: 'wayland'
        }
      }
    );

    let output = '';
    child.stdout.on('data', (data) => { output += data.toString(); });
    child.stderr.on('data', (data) => { console.error('[Electron Error]', data.toString()); });

    child.on('close', (code) => {
      console.log(`[Portal] Electron exited with code ${code}`);
      if (code === 0) {
        const paths = output.trim().split('\n').filter(Boolean);
        if (paths.length === 0) {
          resolve([1, {}]);
          return;
        }
        const uris = paths.map(p => pathToFileURL(p.startsWith('/') ? p : '/' + p).href);
        console.log(`[Portal] Success: ${uris.length} item(s)`);
        resolve([0, { uris: new dbus.Variant('as', uris) }]);
      } else {
        resolve([1, {}]);
      }
    });

    child.on('error', () => resolve([2, {}]));
  });
}

const { Interface } = dbus.interface;

class FileChooserInterface extends Interface {
  constructor() {
    super('org.freedesktop.impl.portal.FileChooser');
  }

  OpenFile(handle, app_id, parent_window, title, options) {
    const isDirectory = options.directory?.value || false;
    const mode = isDirectory ? 'directory' : 'open';
    console.log(`[Portal] OpenFile called by ${app_id || 'unknown'} (mode: ${mode})`);
    return launchPicker(mode, options.multiple?.value || false, null, folderOf(options));
  }

  SaveFile(handle, app_id, parent_window, title, options) {
    console.log(`[Portal] SaveFile called`);
    return launchPicker('save', false, options.current_name?.value || '', folderOf(options));
  }

  SaveFiles(handle, app_id, parent_window, title, options) {
    console.log(`[Portal] SaveFiles called`);
    // pasta escolhida + o nome de cada arquivo pedido (files = aay)
    const names = (options.files?.value || []).map(b => path.basename(Buffer.from(b).toString('utf8').replace(/\0+$/, '')));
    // names come from the requesting app: only plain file names, or nothing at all
    if (!names.length || names.some(n => !n || n === '.' || n === '..' || /[\/\0\r\n]/.test(n))) {
      return Promise.resolve([2, {}]);
    }
    return launchPicker('directory', false, null, folderOf(options)).then(([code, res]) => {
      if (code !== 0 || !res.uris) return [code, res];
      const dir = new URL(res.uris.value[0]).pathname;
      const uris = names.map(n => pathToFileURL(path.join(decodeURIComponent(dir), n)).href);
      return [0, { uris: new dbus.Variant('as', uris) }];
    });
  }
}

FileChooserInterface.configureMembers({
  methods: {
    OpenFile: {
      inSignature: 'osssa{sv}',
      outSignature: 'ua{sv}'
    },
    SaveFile: {
      inSignature: 'osssa{sv}',
      outSignature: 'ua{sv}'
    },
    SaveFiles: {
      inSignature: 'osssa{sv}',
      outSignature: 'ua{sv}'
    }
  }
});

async function main() {
  const bus = dbus.sessionBus();

  console.log('[Portal] Starting...');
  const nameRequest = await bus.requestName(SERVICE_NAME, 7);

  if (nameRequest !== 1 && nameRequest !== 4) {
    console.error('[Portal] Failed to acquire name');
    process.exit(1);
  }

  console.log(`[Portal] Acquired: ${SERVICE_NAME}`);

  const iface = new FileChooserInterface();
  bus.export(OBJECT_PATH, iface);

  console.log('[Portal] Ready and listening!');
}

process.on('SIGINT', () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));
main().catch(err => { console.error('[Portal] Error:', err.stack); process.exit(1); });
