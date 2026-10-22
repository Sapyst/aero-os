

const {
  app,
  BrowserWindow,
  ipcMain,
  dialog,
  shell,
  Menu,
  clipboard,
  globalShortcut,
  nativeTheme,
} = require("electron");
const path = require("path");
const fs = require("fs").promises;
const fsSync = require("fs");
const sizeOf = require("image-size");

// [Aero/doc 43] Aspas SIMPLES pro shell: dentro delas nada é interpretado ($(...), `...`, ;).
// Uma aspa simples no próprio nome vira '\'' (fecha, aspa escapada, abre de novo).
const shq = (t) => "'" + String(t).replace(/'/g, "'\\''") + "'";
const { fileURLToPath } = require("url");

// [Aero] Smoother drawing and scrolling (GPU rasterization; the app is all scrolling lists of thumbnails)
if (!process.env.AERO_FM_SEM_GPU) app.commandLine.appendSwitch("enable-gpu-rasterization");
if (!process.env.AERO_FM_SEM_GPU) app.commandLine.appendSwitch("enable-zero-copy");
app.commandLine.appendSwitch("enable-smooth-scrolling");

let isPicker = false;
let isDev = false;

app.name = "ez-fm";

let mainWindow;
const cancelOperations = new Set();

function logCommandFailure(command, error) {
  if (!command) return;
  const message = error?.message || String(error || "");
  const stderr = error?.stderr || "";
  const stdout = error?.stdout || "";
  const detail = [message, stderr, stdout].filter(Boolean).join("\n");
  console.error(`[command failed] ${command}\n${detail}`);
}


function toFileUrl(p) {
  const withSlashes = p.replace(/\\/g, "/");

  return (
    "file://" +
    encodeURI(withSlashes.startsWith("/") ? withSlashes : "/" + withSlashes)
  );
}

// [Aero] A warm window can be reused by a later launch: its arguments replace the original ones.
let argvAtual = process.argv;

function getUserArgs() {
  const appPath = app.getAppPath();
  const exePath = app.getPath("exe");
  return argvAtual.filter((arg, idx) => {
    if (idx === 0) return false;
    if (arg === appPath || arg === exePath) return false;
  return true;
  });
}

async function collectWalThemes() {
  const walDir = path.join(app.getPath("home"), ".config", "wal", "colorschemes");
  try {
    const stat = await fs.stat(walDir);
    if (!stat.isDirectory()) return [];
  } catch {
    return [];
  }

  const results = [];
  const walk = async (dir, depth) => {
    if (depth > 4) return;
    let entries;
    try {
      entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }

    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        await walk(fullPath, depth + 1);
        continue;
      }
      if (!entry.isFile() || !entry.name.endsWith(".json")) continue;

      try {
        const raw = await fs.readFile(fullPath, "utf8");
        const data = JSON.parse(raw);
        if (!data || !data.colors || !data.special) continue;
        const rel = path
          .relative(walDir, fullPath)
          .replace(/\\/g, "/")
          .replace(/\.json$/i, "");
        const name = rel.split("/").join(" / ");
        results.push({
          name,
          path: fullPath,
          colors: data.colors,
          special: data.special,
          alpha: data.alpha,
          wallpaper: data.wallpaper,
        });
      } catch {}
    }
  };

  await walk(walDir, 0);
  results.sort((a, b) => a.name.localeCompare(b.name));
  return results;
}

function resolveStartPath(args) {
  let startPath = null;

  for (let i = args.length - 1; i >= 0; i--) {
    const arg = args[i];
    if (!arg.startsWith("-")) {
      startPath = arg;
      break;
    }
  }

  if (!startPath) {
    const npmArgv = process.env.npm_config_argv;
    if (npmArgv) {
      try {
        const parsed = JSON.parse(npmArgv);
        const candidates = [];
        if (Array.isArray(parsed.remain)) candidates.push(...parsed.remain);
        if (Array.isArray(parsed.original)) {
          candidates.push(
            ...parsed.original.filter((val) => val !== "run" && val !== "start"),
          );
        }
        for (let i = candidates.length - 1; i >= 0; i--) {
          const arg = String(candidates[i] || "");
          if (arg && !arg.startsWith("-")) {
            startPath = arg;
            break;
          }
        }
      } catch {}
    }
  }

  if (!startPath) return null;
  if (startPath.startsWith("file://")) {
    try {
      startPath = fileURLToPath(startPath);
    } catch {
      return null;
    }
  }
  if (!path.isAbsolute(startPath)) {
    return path.resolve(process.cwd(), startPath);
  }
  return startPath;
}

function getPickerOptions(args) {
  let pickerMode = "open";
  if (args.includes("--mode=save")) pickerMode = "save";
  if (args.includes("--mode=directory")) pickerMode = "directory";

  const allowMultiple = args.includes("--multiple");
  const filenameArg = args.find((arg) => arg.startsWith("--filename="));
  const defaultFilename = filenameArg ? filenameArg.substring(11) : "";

  return { pickerMode, allowMultiple, defaultFilename };
}

function buildWindowQuery(startPath, pickerOptions) {
  const query = {
    picker: isPicker ? "true" : "false",
    pickerMode: pickerOptions.pickerMode,
    allowMultiple: pickerOptions.allowMultiple ? "true" : "false",
    defaultFilename: pickerOptions.defaultFilename,
  };
  if (startPath) query.startPath = startPath;
  return query;
}

function registerDevtoolsShortcuts() {
  const register = (accelerator) => {
    const registered = globalShortcut.register(accelerator, () => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.toggleDevTools();
      }
    });
    if (!registered) {
      console.warn(`Failed to register devtools shortcut: ${accelerator}`);
    }
  };

  try {
    register("F12");
    register("CommandOrControl+Shift+I");
  } catch (error) {
    console.warn("Failed to register devtools shortcuts:", error);
  }
}


// [Aero] DotStyle: when the system visual is DotStyle, its CSS goes on top of aero.css
function visualDoSistema() {
  try {
    const m = fsSync.readFileSync(path.join(require("os").homedir(), ".config", "hypr", "painel.conf"), "utf8")
      .match(/^VISUAL=(.*)$/m);
    return m ? m[1].replace(/"/g, "").trim() : "";
  } catch { return ""; }
}
function aplicarVisual(win) {
  if (!win || win.isDestroyed()) return;
  const css = path.join(__dirname, "aero-dotstyle.css");
  if (visualDoSistema() === "DotStyle" && fsSync.existsSync(css)) {
    win.webContents.insertCSS(fsSync.readFileSync(css, "utf8")).catch(() => {});
    win.webContents.executeJavaScript("document.documentElement.classList.add('dotstyle')").catch(() => {});
  }
}

// [Aero] Warm window: closing it only HIDES it for 10 minutes, so the next Super+E shows it at once
// (no new Electron to start). After 10 minutes without use the app quits and gives the memory back.
// The file picker (portal) never stays warm. A launch while warm comes through ez-fm.sock (see below).
const MORNO_MS = 10 * 60 * 1000;
let saindoDeVez = false;
let timerMorno = null;
function esconderMorno(win) {
  win.hide();
  clearTimeout(timerMorno);
  timerMorno = setTimeout(() => { saindoDeVez = true; app.quit(); }, MORNO_MS);
}
function reabrir(argv) {
  if (!mainWindow || mainWindow.isDestroyed()) { argvAtual = argv; createWindow(); return; }
  clearTimeout(timerMorno);
  argvAtual = argv;
  const query = buildWindowQuery(resolveStartPath(getUserArgs()), getPickerOptions(getUserArgs()));
  mainWindow.loadFile("index.html", { query });
  mainWindow.webContents.once("did-finish-load", () => { aplicarVisual(mainWindow); mainWindow.show(); mainWindow.focus(); });
}
function ouvirSocket() {
  const sock = path.join(process.env.XDG_RUNTIME_DIR || "/tmp", "ez-fm.sock");
  try { fsSync.unlinkSync(sock); } catch {}
  const srv = require("net").createServer((c) => {
    let dados = "";
    c.on("data", (d) => { dados += d; if (dados.length > 8192) c.destroy(); });
    c.on("end", () => {
      const args = dados.split("\0").filter((a) => a.length > 0 && a.length < 4096);
      reabrir([process.argv[0], app.getAppPath(), ...args]);
    });
  });
  srv.listen(sock, () => { try { fsSync.chmodSync(sock, 0o600); } catch {} });
  app.on("will-quit", () => { try { fsSync.unlinkSync(sock); } catch {} });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    show: false,
    width: 1200,
    height: 800,
    minWidth: 360,
    minHeight: 300,
    transparent: true,
    vibrancy: "under-window",
    visualEffectState: "active",
    backgroundColor: "#00000000",
    titleBarStyle: "hidden",
    trafficLightPosition: { x: 10, y: 10 },
    icon: path.join(__dirname, "icon.png"),
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  const userArgs = getUserArgs();
  isPicker = userArgs.includes("--picker");
  const pickerOptions = getPickerOptions(userArgs);
  let startPath = resolveStartPath(userArgs);
  const appPath = app.getAppPath();
  const exePath = app.getPath("exe");
  if (
    startPath &&
    (startPath === appPath ||
      startPath === exePath ||
      path.resolve(startPath) === path.resolve(appPath))
  ) {
    startPath = null;
  }
  const query = buildWindowQuery(startPath, pickerOptions);

  mainWindow.loadFile("index.html", { query });
  // shown only once the page is ready: no empty frame, no flash
  mainWindow.once("ready-to-show", () => { aplicarVisual(mainWindow); mainWindow.show(); });
  if (!isPicker) {
    mainWindow.on("close", (e) => {
      if (saindoDeVez) return;
      e.preventDefault();
      esconderMorno(mainWindow);
    });
  }

  mainWindow.webContents.on("did-finish-load", () => {
    if (isPicker) {
      const titles = {
        open: "Open File",
        save: "Save File",
        directory: "Select Folder",
      };
      mainWindow.setTitle(titles[pickerOptions.pickerMode] || "File Picker");
    } else {
      mainWindow.setTitle("EZ File Manager");
    }
  });

  Menu.setApplicationMenu(null);

  mainWindow.webContents.on("before-input-event", (event, input) => {
    const isF12 = input.type === "keyDown" && input.key === "F12";
    const isCtrlShiftI =
      input.type === "keyDown" &&
      input.key.toLowerCase() === "i" &&
      (input.control || input.meta) &&
      input.shift;

    if ((isF12 || isCtrlShiftI) && isDev) {
      event.preventDefault();
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.toggleDevTools();
      }
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// [Aero/doc 43] Claro/escuro AO VIVO: o Electron não percebe sozinho a troca do sistema.
// Lê o "color-scheme" do GNOME (o mesmo que o hypr-tema troca) e fica ouvindo mudanças.
function temaDoSistema() {
  require("child_process").execFile("gsettings",
    ["get", "org.gnome.desktop.interface", "color-scheme"], (err, out) => {
      if (!err) nativeTheme.themeSource = String(out).includes("dark") ? "dark" : "light";
    });
}
function ouvirTemaDoSistema() {
  temaDoSistema();
  const vigia = require("child_process").spawn("gsettings",
    ["monitor", "org.gnome.desktop.interface", "color-scheme"]);
  vigia.stdout.on("data", temaDoSistema);
  app.on("will-quit", () => vigia.kill());
}

// [Aero/doc 43] Configurações num ARQUIVO comum (ajustes.json), não só no localStorage do Chromium: o banco
// do localStorage fica trancado pela janela aberta, e o seletor de arquivos (outra instância) via tudo zerado.
// O arquivo é a fonte da verdade; cada janela manda só as chaves que mudou (uma não apaga a mudança da outra).
const AJUSTES = path.join(app.getPath("userData"), "ajustes.json");
function lerAjustes() {
  try { return JSON.parse(fsSync.readFileSync(AJUSTES, "utf8")); } catch { return null; }
}
ipcMain.on("ajustes-ler", (event) => { event.returnValue = lerAjustes(); });
ipcMain.on("ajustes-mudar", (event, delta) => {
  try {
    const a = lerAjustes() || {};
    for (const [k, v] of Object.entries(delta || {})) {
      if (v === null) delete a[k]; else a[k] = String(v);
    }
    const tmp = AJUSTES + ".tmp";
    fsSync.writeFileSync(tmp, JSON.stringify(a));
    fsSync.renameSync(tmp, AJUSTES);
    event.returnValue = true;
  } catch (e) {
    event.returnValue = false;
  }
});

// [Aero] Clique direito › Properties: tudo o que dá pra saber de um arquivo ou pasta
ipcMain.handle("propriedades", async (event, alvo) => {
  const { execFile } = require("child_process");
  const run = (cmd, args, ms = 4000) => new Promise((ok) => {
    execFile(cmd, args, { timeout: ms }, (err, out) => ok(err ? "" : String(out).trim()));
  });
  try {
    const st = await fs.lstat(alvo);
    const real = st.isSymbolicLink() ? await fs.realpath(alvo).catch(() => "") : "";
    const info = {
      nome: path.basename(alvo) || alvo, pasta: path.dirname(alvo), caminho: alvo,
      pastaQ: st.isDirectory(), link: st.isSymbolicLink() ? real : "",
      tamanho: st.size, criado: st.birthtimeMs, modificado: st.mtimeMs, acessado: st.atimeMs,
      modo: st.mode & 0o7777, uid: st.uid, gid: st.gid,
    };
    const passwd = fsSync.existsSync("/etc/passwd") ? fsSync.readFileSync("/etc/passwd", "utf8") : "";
    const grupo = fsSync.existsSync("/etc/group") ? fsSync.readFileSync("/etc/group", "utf8") : "";
    const nomeDe = (txt, id) => (txt.split("\n").find((l) => l.split(":")[2] === String(id)) || "").split(":")[0] || String(id);
    info.dono = nomeDe(passwd, st.uid);
    info.grupo = nomeDe(grupo, st.gid);
    info.mime = await run("xdg-mime", ["query", "filetype", alvo]);
    const desc = await run("gio", ["info", "-a", "standard::description", alvo]);
    info.descricao = (desc.match(/standard::description:\s*(.+)/) || [])[1] || "";
    if (info.mime) {
      const desk = await run("xdg-mime", ["query", "default", info.mime]);
      info.app = "";
      if (desk) {
        for (const base of [path.join(require("os").homedir(), ".local/share/applications"), "/usr/share/applications"]) {
          const f = path.join(base, desk);
          if (fsSync.existsSync(f)) {
            const m = fsSync.readFileSync(f, "utf8").match(/^Name=(.+)$/m);
            info.app = m ? m[1] : desk;
            break;
          }
        }
      }
    }
    if (st.isDirectory()) {
      try { info.itens = (await fs.readdir(alvo)).length; } catch { info.itens = null; }
    } else if (/^image\//.test(info.mime || "")) {
      try { const d = sizeOf(alvo); info.dimensoes = `${d.width} × ${d.height}`; } catch {}
    }
    return { ok: true, info };
  } catch (e) {
    return { ok: false, erro: e.message };
  }
});
// tamanho total de uma pasta (pode demorar: separado, com limite de tempo)
ipcMain.handle("tamanho-pasta", async (event, alvo) => new Promise((ok) => {
  require("child_process").execFile("du", ["-sb", "--", alvo], { timeout: 30000 }, (err, out) => {
    const n = parseInt(String(out || "").split(/\s/)[0], 10);
    ok(Number.isFinite(n) ? n : null);
  });
}));

// [Aero] Clique direito › Open With…: o diálogo do sistema (abrir-com), com "Just once" e "Always"
ipcMain.handle("abrir-com", (event, caminhos) => {
  const lista = (Array.isArray(caminhos) ? caminhos : []).filter((c) => typeof c === "string" && c.startsWith("/"));
  if (!lista.length) return false;
  const bin = path.join(require("os").homedir(), ".local", "bin", "abrir-com");
  const p = require("child_process").spawn(fsSync.existsSync(bin) ? bin : "abrir-com", lista, { detached: true, stdio: "ignore" });
  p.on("error", () => {});
  p.unref();
  return true;
});

// [Aero/doc 43] Botões ✖ ▲ ▼ ● da barra de cima: os mesmos comandos do sistema (agem na janela ativa)
ipcMain.handle("janela-acao", (event, acao) => {
  const bin = path.join(require("os").homedir(), ".local", "bin");
  const comandos = { minimizar: "hypr-minimizar", maximizar: "hypr-maximizar", visivel: "hypr-sempre-visivel" };
  if (acao === "fechar") {
    BrowserWindow.fromWebContents(event.sender)?.close();
  } else if (comandos[acao]) {
    require("child_process").execFile(path.join(bin, comandos[acao]), () => {});
  }
});

app.whenReady().then(() => {
  ouvirTemaDoSistema();
  isDev = process.env.EZFM_DEVTOOLS === "1";
  createWindow();
  if (!isPicker) ouvirSocket();

  if (isDev) {
    registerDevtoolsShortcuts();
  }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
app.on("before-quit", () => { saindoDeVez = true; });

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});

app.on("will-quit", () => {
  try {
    globalShortcut.unregisterAll();
  } catch (error) {
    console.warn("Failed to unregister global shortcuts:", error);
  }
});


ipcMain.on("window-minimize", () => {
  mainWindow?.minimize();
});

ipcMain.on("window-maximize", () => {
  if (mainWindow?.isMaximized()) {
    mainWindow.unmaximize();
  } else {
    mainWindow?.maximize();
  }
});

ipcMain.on("window-close", () => {
  mainWindow?.close();
});

ipcMain.on("picker-confirm", (event, paths) => {
  if (Array.isArray(paths)) {
    // one path per line: refuse names with line breaks (they would forge an extra path)
    paths.filter((p) => typeof p === "string" && !/[\r\n]/.test(p))
      .forEach((p) => process.stdout.write(p + "\n"));
  }
  app.exit(0);
});

ipcMain.on("picker-cancel", () => {
  app.exit(1);
});


ipcMain.handle("get-directory-contents", async (event, dirPath) => {
  try {
    const items = await fs.readdir(dirPath, { withFileTypes: true });
    const contents = await Promise.all(
      items.map(async (item) => {
        const fullPath = path.join(dirPath, item.name);
        let stats = null;
        let linkTarget = null;

        try {
          stats = await fs.stat(fullPath);
          if (item.isSymbolicLink()) {
            linkTarget = await fs.readlink(fullPath);
          }
        } catch (err) {}

        return {
          name: item.name,
          path: fullPath,
          isDirectory: item.isDirectory(),
          isFile: item.isFile(),
          isSymlink: item.isSymbolicLink(),
          linkTarget,
          size: stats?.size || 0,
          modified: stats?.mtime || null,
          created: stats?.birthtime || null,
          extension: item.isFile()
            ? path.extname(item.name).toLowerCase()
            : null,
        };
      }),
    );

    contents.sort((a, b) => {
      if (a.isDirectory && !b.isDirectory) return -1;
      if (!a.isDirectory && b.isDirectory) return 1;
      return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    });

    return { success: true, contents, path: dirPath };
  } catch (error) {
    if (error.code === "ENOTDIR" || error.code === "ENOENT") {
      return await handleArchiveBrowsing(dirPath);
    }
    return { success: false, error: error.message };
  }
});

ipcMain.handle("get-home-directory", () => {
  return app.getPath("home");
});

ipcMain.handle("get-wal-themes", async () => {
  try {
    return await collectWalThemes();
  } catch {
    return [];
  }
});

ipcMain.handle("get-common-directories", () => {
  const homePath = app.getPath("home");

  const existingOrNull = (p) => {
    if (!p) return null;
    try {
      if (fsSync.existsSync(p) && fsSync.statSync(p).isDirectory()) return p;
    } catch {}
    return null;
  };

  const getElectronPathOrNull = (name) => {
    try {
      return existingOrNull(app.getPath(name));
    } catch {
      return null;
    }
  };

  const pickHomeSubdir = (candidates) => {
    for (const name of candidates) {
      const p = existingOrNull(path.join(homePath, name));
      if (p) return p;
    }
    return null;
  };

  const desktop =
    getElectronPathOrNull("desktop") ??
    pickHomeSubdir(["Desktop", "desktop", "Schreibtisch", "Bureau"]) ??
    homePath;

  const documents =
    getElectronPathOrNull("documents") ??
    pickHomeSubdir(["Documents", "documents", "Dokumente", "Documenti"]) ??
    homePath;

  const downloads =
    getElectronPathOrNull("downloads") ??
    pickHomeSubdir([
      "Downloads",
      "downloads",
      "Téléchargements",
      "Scaricati",
    ]) ??
    homePath;

  const pictures =
    getElectronPathOrNull("pictures") ??
    pickHomeSubdir(["Pictures", "pictures", "Images", "Bilder", "Immagini"]) ??
    homePath;

  const music =
    getElectronPathOrNull("music") ??
    pickHomeSubdir(["Music", "music", "Musik", "Musica"]) ??
    homePath;

  const videos =
    getElectronPathOrNull("videos") ??
    pickHomeSubdir(["Videos", "videos", "Vidéo", "Video"]) ??
    homePath;

  const trash = (() => {
    try {
      if (process.platform === "darwin") {
        const p = path.join(homePath, ".Trash");
        return fsSync.existsSync(p) ? p : null;
      }
      if (process.platform === "linux") {
        const candidates = [
          path.join(homePath, ".local", "share", "Trash", "files"),
          path.join(homePath, ".Trash"),
        ];
        for (const p of candidates) {
          if (fsSync.existsSync(p)) return p;
        }
        return null;
      }
      return null;
    } catch {
      return null;
    }
  })();

  return {
    root: "/",
    ...(trash ? { trash } : {}),
    home: homePath,
    desktop,
    documents,
    downloads,
    pictures,
    music,
    videos,
    config: path.join(homePath, ".config"),
  };
});

ipcMain.handle("path-exists", async (event, checkPath) => {
  try {
    await fs.access(checkPath);
    return true;
  } catch {
    return false;
  }
});

ipcMain.handle("get-parent-directory", (event, currentPath) => {
  return path.dirname(currentPath);
});

ipcMain.handle("join-paths", (event, ...paths) => {
  return path.join(...paths);
});

ipcMain.handle("parse-path", (event, pathString) => {
  return path.parse(pathString);
});


ipcMain.handle("open-file", async (event, filePath) => {
  try {
    await shell.openPath(filePath);
    return { success: true };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle("show-in-folder", async (event, filePath) => {
  shell.showItemInFolder(filePath);
  return { success: true };
});

ipcMain.handle("open-terminal", async (event, dirPath) => {
  const { spawn } = require("child_process");
  try {
    if (process.platform === "win32") {
      spawn("cmd.exe", ["/c", "start", "cmd.exe"], {
        cwd: dirPath,
        shell: true,
      });
    } else if (process.platform === "darwin") {
      spawn("open", ["-a", "Terminal", dirPath]);
    } else {
      const child = spawn("kitty", [], {
        cwd: dirPath,
        detached: true,
        stdio: "ignore",
      });
      child.on("error", (e) => {
        if (e.code === "ENOENT") {
          const child2 = spawn("x-terminal-emulator", [], {
            cwd: dirPath,
            detached: true,
            stdio: "ignore",
          });
          child2.on("error", (e2) => {
            if (e2.code === "ENOENT") {
              spawn("gnome-terminal", [`--working-directory=${dirPath}`], {
                detached: true,
                stdio: "ignore",
              });
            }
          });
          child2.unref();
        }
      });
      child.unref();
    }
    return { success: true };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle("delete-item", async (event, itemPath) => {
  try {
    const stats = await fs.stat(itemPath);

    if (stats.isDirectory()) {
      await fs.rm(itemPath, { recursive: true, force: true });
    } else {
      try {
        await fs.unlink(itemPath);
      } catch (err) {
        if (err && (err.code === "EACCES" || err.code === "EPERM")) {
          await fs.chmod(itemPath, 0o600);
          await fs.unlink(itemPath);
        } else {
          throw err;
        }
      }
    }

    return { success: true };
  } catch (error) {
    try {
      await fs.rm(itemPath, { recursive: true, force: true });
      return { success: true };
    } catch (finalErr) {
      return { success: false, error: finalErr.message, code: finalErr.code };
    }
  }
});

// [Aero/doc 43] Desligado: este recurso pedia a SENHA DE ADMINISTRADOR dentro do app pra apagar
// arquivos do sistema. Regra da casa: comandos de administrador só no terminal, por você.
ipcMain.handle("delete-item-sudo", async () => ({
  success: false,
  error: "Deleting system files from here is disabled. Use a terminal (sudo rm ...).",
}));

ipcMain.handle("trash-item", async (event, itemPath) => {
  try {
    await shell.trashItem(itemPath);
    return { success: true };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle("restore-trash-items", async (event, originalPaths) => {
  const list = Array.isArray(originalPaths)
    ? originalPaths.filter(Boolean)
    : [];
  if (list.length === 0) {
    return { success: false, error: "No paths provided" };
  }

  if (process.platform !== "linux") {
    return { success: false, error: "Restore from Trash is not supported" };
  }

  const homePath = app.getPath("home");
  const trashBase = path.join(homePath, ".local", "share", "Trash");
  const trashFilesDir = path.join(trashBase, "files");
  const trashInfoDir = path.join(trashBase, "info");

  const normalizePath = (p) =>
    String(p || "")
      .replace(/\\/g, "/")
      .replace(/\/+$/, "");

  const decodePath = (value) => {
    try {
      return decodeURIComponent(value);
    } catch {
      return value;
    }
  };

  let infoFiles = [];
  try {
    infoFiles = await fs.readdir(trashInfoDir);
  } catch (error) {
    return { success: false, error: "Trash info directory not found" };
  }

  const entries = [];
  for (const fileName of infoFiles) {
    if (!fileName.endsWith(".trashinfo")) continue;
    const infoPath = path.join(trashInfoDir, fileName);
    let content = "";
    try {
      content = await fs.readFile(infoPath, "utf8");
    } catch {
      continue;
    }

    let original = "";
    let deletedAt = "";
    for (const line of content.split(/\r?\n/)) {
      if (line.startsWith("Path=")) {
        original = decodePath(line.slice(5).trim());
      } else if (line.startsWith("DeletionDate=")) {
        deletedAt = line.slice(13).trim();
      }
    }
    if (!original) continue;

    const trashName = fileName.replace(/\.trashinfo$/i, "");
    entries.push({
      original: normalizePath(original),
      deletedAt: deletedAt ? Date.parse(deletedAt) : 0,
      trashName,
      infoPath,
    });
  }

  const entryMap = new Map();
  for (const entry of entries) {
    const existing = entryMap.get(entry.original);
    if (!existing || entry.deletedAt > existing.deletedAt) {
      entryMap.set(entry.original, entry);
    }
  }

  const restored = [];
  const failed = [];

  for (const rawPath of list) {
    const original = normalizePath(rawPath);
    const entry = entryMap.get(original);
    if (!entry) {
      failed.push({ path: rawPath, error: "Item not found in Trash" });
      continue;
    }

    const trashedPath = path.join(trashFilesDir, entry.trashName);
    let stats;
    try {
      stats = await fs.stat(trashedPath);
    } catch (error) {
      failed.push({ path: rawPath, error: "Trashed item missing" });
      continue;
    }

    let targetPath = original || rawPath;
    try {
      await fs.access(targetPath);
      targetPath = await findUniquePath(
        targetPath,
        stats.isDirectory() ? "folder" : "file",
      );
    } catch {}

    try {
      await fs.mkdir(path.dirname(targetPath), { recursive: true });
      await fs.rename(trashedPath, targetPath);
      try {
        await fs.unlink(entry.infoPath);
      } catch {}
      restored.push({ from: trashedPath, to: targetPath });
    } catch (error) {
      try {
        if (stats.isDirectory()) {
          await copyDirectory(trashedPath, targetPath);
          await fs.rm(trashedPath, { recursive: true, force: true });
        } else {
          await fs.copyFile(trashedPath, targetPath);
          await fs.unlink(trashedPath);
        }
        try {
          await fs.unlink(entry.infoPath);
        } catch {}
        restored.push({ from: trashedPath, to: targetPath });
      } catch (fallbackError) {
        failed.push({
          path: rawPath,
          error: fallbackError.message || "Restore failed",
        });
      }
    }
  }

  return { success: failed.length === 0, restored, failed };
});

ipcMain.handle("rename-item", async (event, oldPath, newName) => {
  try {
    const dirName = path.dirname(oldPath);
    const newPath = path.join(dirName, newName);
    await fs.rename(oldPath, newPath);
    return { success: true, newPath };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle("create-folder", async (event, parentPath, folderName) => {
  try {
    const basePath = path.resolve(parentPath);
    const desiredPath = path.join(basePath, folderName);

    try {
      await fs.mkdir(desiredPath, { recursive: false });
      return { success: true, path: desiredPath };
    } catch (err) {
      if (err && (err.code === "EEXIST" || err.code === "ENOTEMPTY")) {
        const uniquePath = await findUniquePath(desiredPath, "folder");
        await fs.mkdir(uniquePath, { recursive: false });
        return { success: true, path: uniquePath };
      }

      if (err && err.code === "ENOENT") {
        await fs.mkdir(basePath, { recursive: true });
        try {
          await fs.mkdir(desiredPath, { recursive: false });
          return { success: true, path: desiredPath };
        } catch (err2) {
          if (err2 && (err2.code === "EEXIST" || err2.code === "ENOTEMPTY")) {
            const uniquePath = await findUniquePath(desiredPath, "folder");
            await fs.mkdir(uniquePath, { recursive: false });
            return { success: true, path: uniquePath };
          }
          throw err2;
        }
      }
      throw err;
    }
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle("create-file", async (event, parentPath, fileName) => {
  try {
    const basePath = path.resolve(parentPath);
    const desiredPath = path.join(basePath, fileName);

    await fs.mkdir(basePath, { recursive: true });

    try {
      const fh = await fs.open(desiredPath, "wx");
      await fh.close();
      return { success: true, path: desiredPath };
    } catch (err) {
      if (err && err.code === "EEXIST") {
        const uniquePath = await findUniquePath(desiredPath, "file");
        const fh = await fs.open(uniquePath, "wx");
        await fh.close();
        return { success: true, path: uniquePath };
      }
      throw err;
    }
  } catch (error) {
    return { success: false, error: error.message };
  }
});

async function findUniquePath(desiredPath, kind) {
  const dir = path.dirname(desiredPath);
  const parsed = path.parse(desiredPath);

  const baseName = kind === "file" ? parsed.name : parsed.base;
  const ext = kind === "file" ? parsed.ext : "";

  for (let i = 1; i < 10000; i++) {
    const candidateName = `${baseName} (${i})${ext}`;
    const candidatePath = path.join(dir, candidateName);
    try {
      await fs.access(candidatePath);
    } catch {
      return candidatePath;
    }
  }

  throw new Error("Could not find a unique name");
}

ipcMain.handle("copy-item", async (event, sourcePath, destPath) => {
  try {
    const stats = await fs.stat(sourcePath);
    if (stats.isDirectory()) {
      await copyDirectory(sourcePath, destPath);
    } else {
      await fs.copyFile(sourcePath, destPath);
    }
    return { success: true };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

async function copyDirectory(source, destination) {
  await fs.mkdir(destination, { recursive: true });
  const items = await fs.readdir(source, { withFileTypes: true });

  for (const item of items) {
    const srcPath = path.join(source, item.name);
    const destPath = path.join(destination, item.name);

    if (item.isDirectory()) {
      await copyDirectory(srcPath, destPath);
    } else {
      await fs.copyFile(srcPath, destPath);
    }
  }
}

ipcMain.handle("move-item", async (event, sourcePath, destPath) => {
  try {
    await fs.rename(sourcePath, destPath);
    return { success: true };
  } catch (error) {
    try {
      const stats = await fs.stat(sourcePath);
      if (stats.isDirectory()) {
        await copyDirectory(sourcePath, destPath);
        await fs.rm(sourcePath, { recursive: true });
      } else {
        await fs.copyFile(sourcePath, destPath);
        await fs.unlink(sourcePath);
      }
      return { success: true };
    } catch (copyError) {
      return { success: false, error: copyError.message };
    }
  }
});

ipcMain.handle("cancel-operation", async (event, operationId) => {
  if (!operationId) return { success: false, error: "Missing operation id" };
  cancelOperations.add(String(operationId));
  return { success: true };
});

ipcMain.handle("batch-file-operation", async (event, items, operation, operationId) => {
  const cancelKey = operationId ? String(operationId) : null;
  const shouldCancel = () =>
    Boolean(cancelKey && cancelOperations.has(cancelKey));
  const checkCancelled = () => {
    if (shouldCancel()) {
      const err = new Error("Operation cancelled");
      err.code = "CANCELLED";
      throw err;
    }
  };

  let totalBytes = 0;
  let processedBytes = 0;
  let totalFiles = 0;
  let processedFiles = 0;

  const itemSizes = new Map();

  const scan = async (p, rootItemPath) => {
    checkCancelled();
    try {
      const stats = await fs.stat(p);
      if (stats.isDirectory()) {
        const children = await fs.readdir(p);
        for (const c of children) await scan(path.join(p, c), rootItemPath);
      } else {
        totalBytes += stats.size;
        totalFiles++;
        itemSizes.set(
          rootItemPath,
          (itemSizes.get(rootItemPath) || 0) + stats.size,
        );
      }
    } catch {}
  };

  for (const item of items) {
    await scan(item.source, item.source);
  }

  if (totalBytes === 0) totalBytes = 1;

  const reportProgress = () => {
    const percent = Math.min(100, (processedBytes / totalBytes) * 100);
    event.sender.send("file-operation-progress", percent);
  };

  const copyRecursive = async (src, dest) => {
    checkCancelled();
    const stats = await fs.stat(src);
    if (stats.isDirectory()) {
      await fs.mkdir(dest, { recursive: true });
      const children = await fs.readdir(src);
      for (const child of children) {
        await copyRecursive(path.join(src, child), path.join(dest, child));
      }
    } else {
      checkCancelled();
      await fs.copyFile(src, dest);
      processedBytes += stats.size;
      processedFiles++;
      reportProgress();
    }
  };

  try {
    for (const item of items) {
      checkCancelled();
      if (operation === "copy") {
        await copyRecursive(item.source, item.dest);
      } else {
        try {
          checkCancelled();
          await fs.rename(item.source, item.dest);

          const size = itemSizes.get(item.source) || 0;
          processedBytes += size;
          reportProgress();
        } catch (err) {
          await copyRecursive(item.source, item.dest);
          await fs.rm(item.source, { recursive: true, force: true });
        }
      }
    }
    return { success: true };
  } catch (error) {
    if (error?.code === "CANCELLED") {
      return { success: false, cancelled: true, error: "Cancelled" };
    }
    return { success: false, error: error.message };
  } finally {
    if (cancelKey) {
      cancelOperations.delete(cancelKey);
    }
  }
});

ipcMain.handle("get-item-info", async (event, itemPath) => {
  try {
    const stats = await fs.stat(itemPath);
    let size = stats.size;

    if (stats.isDirectory()) {
      size = await getDirectorySize(itemPath);
    }

    return {
      success: true,
      info: {
        name: path.basename(itemPath),
        path: itemPath,
        size,
        created: stats.birthtime,
        modified: stats.mtime,
        accessed: stats.atime,
        isDirectory: stats.isDirectory(),
        isFile: stats.isFile(),
        permissions: stats.mode,
      },
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

async function getDirectorySize(dirPath) {
  let size = 0;
  try {
    const items = await fs.readdir(dirPath, { withFileTypes: true });
    for (const item of items) {
      const fullPath = path.join(dirPath, item.name);
      try {
        if (item.isDirectory()) {
          size += await getDirectorySize(fullPath);
        } else {
          const stats = await fs.stat(fullPath);
          size += stats.size;
        }
      } catch (err) {}
    }
  } catch (err) {}
  return size;
}

ipcMain.handle("clipboard-copy-paths", async (event, paths) => {
  try {
    const list = Array.isArray(paths) ? paths.filter(Boolean) : [];
    if (list.length === 0)
      return { success: false, error: "No paths provided" };

    clipboard.writeText(list.join("\n"));

    try {
      clipboard.write({
        text: list.join("\n"),

        "text/uri-list": list.map(toFileUrl).join("\n"),
      });
    } catch {}

    return { success: true };
  } catch (error) {
    return { success: false, error: error.message };
  }
});


async function handleArchiveBrowsing(fullPath) {
  let archivePath = fullPath;
  let internalPath = "";
  let found = false;

  let depth = 0;
  while (depth < 20) {
    try {
      const stats = await fs.stat(archivePath);
      if (stats.isFile()) {
        found = true;
        break;
      } else if (stats.isDirectory()) {
        return { success: false, error: "Not a directory" };
      }
    } catch (e) {
      const parent = path.dirname(archivePath);
      if (parent === archivePath) break;
      const base = path.basename(archivePath);
      internalPath = internalPath ? path.join(base, internalPath) : base;
      archivePath = parent;
    }
    depth++;
  }

  if (!found) return { success: false, error: "Path not found" };

  const { exec } = require("child_process");
  const util = require("util");
  const execPromise = util.promisify(exec);

  let cmd = "";
  try {
    const safeArchivePath = archivePath.replace(/"/g, '\\"');

    const isCompressedTar = /\.(tar\.(gz|xz|bz2)|tgz|txz|tbz2)$/i.test(
      archivePath,
    );

    cmd = `7z l -slt -ba -sccUTF-8 ${shq(archivePath)}`;
    if (isCompressedTar) {
      cmd = `7z x -so ${shq(archivePath)} | 7z l -slt -ba -sccUTF-8 -si -ttar`;
    }

    const { stdout } = await execPromise(cmd, { maxBuffer: 10 * 1024 * 1024 });

    const contents = [];
    const seen = new Set();

    const normalizedInternal = internalPath.replace(/\\/g, "/");

    const blocks = stdout.split(/\r?\n\r?\n/);

    for (const block of blocks) {
      const entry = {};
      block.split(/\r?\n/).forEach((line) => {
        const match = line.match(/^(\w+)\s=\s(.*)$/);
        if (match) entry[match[1]] = match[2];
      });

      if (!entry.Path) continue;

      let entryPath = entry.Path.replace(/\\/g, "/");

      if (normalizedInternal && !entryPath.startsWith(normalizedInternal + "/"))
        continue;

      let relative = normalizedInternal
        ? entryPath.slice(normalizedInternal.length + 1)
        : entryPath;
      if (!relative) continue;

      const parts = relative.split("/");
      const name = parts[0];

      if (seen.has(name)) continue;
      seen.add(name);

      const isDirectChild = parts.length === 1;
      const isDir =
        !isDirectChild || (entry.Attributes && entry.Attributes.includes("D"));

      contents.push({
        name: name,
        path: path.join(fullPath, name),
        isDirectory: isDir,
        isFile: !isDir,
        isSymlink: false,
        size: isDir ? 0 : parseInt(entry.Size || "0", 10),
        modified: entry.Modified ? new Date(entry.Modified) : null,
        created: null,
        extension: isDir ? null : path.extname(name).toLowerCase(),
      });
    }

    contents.sort((a, b) => {
      if (a.isDirectory && !b.isDirectory) return -1;
      if (!a.isDirectory && b.isDirectory) return 1;
      return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    });

    return { success: true, contents, path: fullPath, isArchive: true };
  } catch (err) {
    logCommandFailure(cmd, err);
    return {
      success: false,
      error: "Failed to read archive (7z required): " + err.message,
    };
  }
}

ipcMain.handle("extract-archive", async (event, archivePath, destPath) => {
  try {
    const { execSync } = require("child_process");
    const baseName = path.basename(archivePath);

    let outputDir = path.join(
      destPath,
      baseName
        .replace(/\.(zip|tar|gz|bz2|xz|7z|rar|tgz)$/gi, "")
        .replace(/\.tar$/i, ""),
    );

    const lower = archivePath.toLowerCase();

    try {
      await fs.mkdir(outputDir, { recursive: true });

      // [Aero/doc 43] execFileSync = programa + lista de argumentos, SEM shell: um arquivo baixado
      // com nome malicioso (ex: contendo $(...)) não consegue virar comando.
      const { execFileSync, spawnSync } = require("child_process");
      const run = (cmd, args) => execFileSync(cmd, args, { stdio: "pipe" });
      if (lower.endsWith(".zip")) {
        try {
          run("unzip", ["-o", archivePath, "-d", outputDir]);
        } catch {
          run("7z", ["x", archivePath, `-o${outputDir}`, "-y"]);
        }
      } else if (lower.endsWith(".tar.gz") || lower.endsWith(".tgz")) {
        run("tar", ["-xzf", archivePath, "-C", outputDir]);
      } else if (lower.endsWith(".tar.bz2")) {
        run("tar", ["-xjf", archivePath, "-C", outputDir]);
      } else if (lower.endsWith(".tar.xz")) {
        run("tar", ["-xJf", archivePath, "-C", outputDir]);
      } else if (lower.endsWith(".tar")) {
        run("tar", ["-xf", archivePath, "-C", outputDir]);
      } else if (lower.endsWith(".gz")) {
        const fsSync = require("fs");
        const fd = fsSync.openSync(path.join(outputDir, baseName.replace(/\.gz$/i, "")), "w");
        try {
          const r = spawnSync("gunzip", ["-c", archivePath], { stdio: ["ignore", fd, "pipe"] });
          if (r.status !== 0) throw new Error(String(r.stderr || "gunzip failed"));
        } finally {
          fsSync.closeSync(fd);
        }
      } else {
        run("7z", ["x", archivePath, `-o${outputDir}`, "-y"]);
      }

      return { success: true, outputDir };
    } catch (cmdError) {
      return { success: false, error: cmdError.message || "Extraction failed" };
    }
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle("compress-items", async (event, paths, outputPath) => {
  try {
    const { execSync } = require("child_process");
    const lower = outputPath.toLowerCase();

    // [Aero/doc 43] sem shell: cada arquivo é um argumento separado ("./nome": nunca vira opção)
    const { execFileSync } = require("child_process");
    const dir = path.dirname(paths[0]);
    const nomes = paths.map((p) => "./" + path.basename(p));
    const run = (cmd, args, cwd) => execFileSync(cmd, args, { stdio: "pipe", cwd });

    try {
      if (lower.endsWith(".tar.gz") || lower.endsWith(".tgz")) {
        run("tar", ["-czf", outputPath, "-C", dir, "--", ...nomes]);
      } else if (lower.endsWith(".tar.bz2")) {
        run("tar", ["-cjf", outputPath, "-C", dir, "--", ...nomes]);
      } else if (lower.endsWith(".tar.xz")) {
        run("tar", ["-cJf", outputPath, "-C", dir, "--", ...nomes]);
      } else if (lower.endsWith(".tar")) {
        run("tar", ["-cf", outputPath, "-C", dir, "--", ...nomes]);
      } else if (lower.endsWith(".7z")) {
        run("7z", ["a", outputPath, ...nomes], dir);
      } else {
        run("zip", ["-r", outputPath, ...nomes], dir);   // .zip e qualquer outro
      }

      return { success: true };
    } catch (cmdError) {
      return {
        success: false,
        error: cmdError.message || "Compression failed",
      };
    }
  } catch (error) {
    return { success: false, error: error.message };
  }
});


async function getDiskSpace(pathStr) {
  try {
    if (fs.statfs) {
      const stats = await fs.statfs(pathStr);
      return {
        total: stats.blocks * stats.bsize,
        free: stats.bavail * stats.bsize,
      };
    }
  } catch {}

  const { exec } = require("child_process");
  const util = require("util");
  const execPromise = util.promisify(exec);

  let cmd = "";
  try {
    if (process.platform === "win32") {
      const driveLetter = pathStr.substring(0, 2);
      cmd = `wmic logicaldisk where "DeviceID='${driveLetter}'" get FreeSpace,Size /format:value`;
      const { stdout } = await execPromise(cmd);
      const sizeMatch = stdout.match(/Size=(\d+)/);
      const freeMatch = stdout.match(/FreeSpace=(\d+)/);
      if (sizeMatch && freeMatch) {
        return {
          total: parseInt(sizeMatch[1], 10),
          free: parseInt(freeMatch[1], 10),
        };
      }
    } else {
      cmd = `df -kP ${shq(pathStr)}`;
      const { stdout } = await execPromise(cmd);
      const lines = stdout.trim().split("\n");
      if (lines.length >= 2) {
        const parts = lines[1].split(/\s+/);
        if (parts.length >= 4) {
          return {
            total: parseInt(parts[1], 10) * 1024,
            free: parseInt(parts[3], 10) * 1024,
          };
        }
      }
    }
  } catch (error) {
    logCommandFailure(cmd, error);
  }
  return null;
}

ipcMain.handle("get-drives", async () => {
  if (process.platform === "win32") {
    const drives = [];
    for (let i = 65; i <= 90; i++) {
      const drive = String.fromCharCode(i) + ":\\";
      try {
        await fs.access(drive);
        const space = await getDiskSpace(drive);
        drives.push({ name: drive, path: drive, mounted: true, space });
      } catch (err) {}
    }
    return drives;
  } else {
    const { exec } = require("child_process");
    const util = require("util");
    const execPromise = util.promisify(exec);

    const drives = [];

    let cmd = "lsblk -J -o NAME,SIZE,TYPE,MOUNTPOINT,MOUNTPOINTS,LABEL,FSTYPE,RO,UUID,HOTPLUG 2>/dev/null";
    try {
      const { stdout } = await execPromise(cmd);
      const data = JSON.parse(stdout);

      const processDevice = (device, parentName = null) => {
        const name = device.name;
        const fullPath = `/dev/${name}`;
        // [letras] btrfs com subvolumes tem VÁRIOS pontos de montagem (/, /home, /var/log...);
        // o MOUNTPOINT sozinho às vezes trazia /var/log e a raiz aparecia duplicada ("Root" + nvme...).
        const pontos = (device.mountpoints || []).filter(Boolean);
        const mountpoint = pontos.includes("/") ? "/" : (pontos[0] || device.mountpoint);
        const label = device.label;
        const size = device.size;
        const fstype = device.fstype;
        const type = device.type;
        const ro = device.ro === "1" || device.ro === true;

        if (type === "loop" || type === "ram" || type === "rom") return;

        const skipFsTypes = ["swap", "vfat"];
        const skipMountpoints = ["/boot", "/boot/efi", "/efi", "[SWAP]"];
        const skipLabels = ["EFI", "SYSTEM", "Recovery", "RECOVERY", "BIOS"];

        if (
          fstype &&
          skipFsTypes.includes(fstype) &&
          (mountpoint === "[SWAP]" ||
            !mountpoint ||
            skipMountpoints.some((m) => mountpoint?.startsWith(m)))
        )
          return;
        if (mountpoint && skipMountpoints.some((m) => mountpoint.startsWith(m)))
          return;
        if (label && skipLabels.includes(label)) return;

        if (fstype || mountpoint) {
          let displayName = label || name;
          if (size) displayName += ` (${size})`;

          drives.push({
            name: displayName,
            path: mountpoint || fullPath,
            mounted: Boolean(mountpoint),
            size: size,
            device: fullPath,
            fstype: fstype,
            readonly: ro,
            label: label || null,
            uuid: device.uuid || null,
            removable: device.hotplug === true || device.hotplug === "1",
          });
        }

        if (device.children) {
          for (const child of device.children) {
            processDevice(child, name);
          }
        }
      };

      if (data.blockdevices) {
        for (const device of data.blockdevices) {
          processDevice(device);
        }
      }
    } catch (err) {
      logCommandFailure(cmd, err);
    }

    if (!drives.some((d) => d.path === "/")) {
      drives.unshift({ name: "Root", path: "/", mounted: true });
    }

    const commonMounts = ["/mnt", "/media", "/Volumes", "/run/media"];
    for (const mount of commonMounts) {
      try {
        await fs.access(mount);
        const items = await fs.readdir(mount, { withFileTypes: true });
        for (const item of items) {
          if (item.isDirectory()) {
            const itemPath = path.join(mount, item.name);

            if (mount === "/run/media") {
              try {
                const subItems = await fs.readdir(itemPath, {
                  withFileTypes: true,
                });
                for (const subItem of subItems) {
                  if (subItem.isDirectory()) {
                    const subPath = path.join(itemPath, subItem.name);
                    if (!drives.some((d) => d.path === subPath)) {
                      drives.push({
                        name: subItem.name,
                        path: subPath,
                        mounted: true,
                      });
                    }
                  }
                }
              } catch (e) {}
            } else if (!drives.some((d) => d.path === itemPath)) {
              drives.push({
                name: item.name,
                path: itemPath,
                mounted: true,
              });
            }
          }
        }
      } catch (err) {}
    }

    for (const d of drives) {
      if (d.mounted && d.path) {
        d.space = await getDiskSpace(d.path);
      }
    }

    await darLetras(drives);
    return drives;
  }
});

// ---------- [letras] Letras de unidade estilo Windows + apelidos ----------
// Só aparência: o caminho real do disco não muda. O disco do sistema ("/") é sempre C:. Os outros
// discos internos ganham D:, E:...; pendrives e HDs externos, a próxima letra livre. Cada disco
// LEMBRA a letra que ganhou (como no Windows), guardada por UUID em ~/.config/ez-fm-letras.json.
const LETRAS_ARQ = path.join(require("os").homedir(), ".config", "ez-fm-letras.json");

function lerLetras() {
  try {
    const j = JSON.parse(fsSync.readFileSync(LETRAS_ARQ, "utf8"));
    return { letras: j.letras || {}, apelidos: j.apelidos || {} };
  } catch {
    return { letras: {}, apelidos: {} };
  }
}

function gravarLetras(dados) {
  fsSync.writeFileSync(LETRAS_ARQ, JSON.stringify(dados, null, 2));
}

async function darLetras(drives) {
  const dados = lerLetras();
  const vistas = new Set();
  for (const d of drives) {
    // chave estável: UUID do sistema de arquivos (dois pedaços de uma ISO podem repetir o UUID)
    let chave = d.uuid || d.path;
    if (vistas.has(chave)) chave += "#" + (d.device || d.path);
    vistas.add(chave);
    d.chave = chave;
  }
  const usadas = new Set(["C"]);
  for (const d of drives) {
    if (d.path === "/") d.letra = "C";
    else if (dados.letras[d.chave] && dados.letras[d.chave] !== "C" && !usadas.has(dados.letras[d.chave])) d.letra = dados.letras[d.chave];
    if (d.letra) usadas.add(d.letra);
  }
  // novos: primeiro os internos, depois os removíveis (como o Windows faz no boot)
  const novos = drives.filter((d) => !d.letra).sort((a, b) => (a.removable ? 1 : 0) - (b.removable ? 1 : 0));
  let mudou = false;
  for (const d of novos) {
    const livre = "DEFGHIJKLMNOPQRSTUVWXYZ".split("").find((l) => !usadas.has(l));
    if (!livre) break;
    d.letra = livre;
    usadas.add(livre);
    if (d.path !== "/") { dados.letras[d.chave] = livre; mudou = true; }
  }
  if (mudou) gravarLetras(dados);
  for (const d of drives) {
    const base = dados.apelidos[d.chave] || d.label ||
      (d.path === "/" ? "Local Disk" : d.removable ? "USB Drive" : d.device ? "Local Disk" : path.basename(d.path));
    d.nomeOriginal = d.name;
    d.apelido = dados.apelidos[d.chave] || null;
    d.name = d.letra ? `${base} (${d.letra}:)` : base;
    d.nomeBase = base;
  }
  drives.sort((a, b) => (a.letra || "~").localeCompare(b.letra || "~"));
}

ipcMain.handle("set-drive-alias", async (event, chave, apelido) => {
  const dados = lerLetras();
  const limpo = String(apelido || "").trim().slice(0, 40);
  if (limpo) dados.apelidos[chave] = limpo;
  else delete dados.apelidos[chave];
  gravarLetras(dados);
  return { success: true };
});

ipcMain.handle("set-drive-letter", async (event, chave, letra) => {
  const l = String(letra || "").trim().replace(":", "").toUpperCase();
  if (!/^[D-Z]$/.test(l)) return { success: false, error: "Use a letter from D to Z (C is the system disk)." };
  const dados = lerLetras();
  const dono = Object.keys(dados.letras).find((k) => dados.letras[k] === l && k !== chave);
  if (dono) dados.letras[dono] = dados.letras[chave] || null;   // troca: o outro disco fica com a letra antiga
  if (dono && !dados.letras[dono]) delete dados.letras[dono];
  dados.letras[chave] = l;
  gravarLetras(dados);
  return { success: true };
});

ipcMain.handle("unmount-device", async (event, devicePath, options = {}) => {
  const { exec } = require("child_process");
  const util = require("util");
  const execPromise = util.promisify(exec);

  const getErrorText = (err) =>
    [err?.message, err?.stderr, err?.stdout].filter(Boolean).join("\n");

  const needsAuth = (message) =>
    /not authorized|authentication is required|permission denied|access denied/i.test(
      String(message || ""),
    );

  // [Aero/doc 43] sem senha de administrador dentro do app: só o udisksctl normal (polkit)
  const runWithSudo = () => Promise.reject(new Error("Needs admin rights: mount it from a terminal."));
  const runWithSudoOriginal = (password) => {
    const { spawn } = require("child_process");
    return new Promise((resolve, reject) => {
      const child = spawn(
        "sudo",
        ["-S", "-p", "", "udisksctl", "unmount", "-b", devicePath],
        { stdio: ["pipe", "pipe", "pipe"] },
      );
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (data) => {
        stdout += data.toString();
      });
      child.stderr.on("data", (data) => {
        stderr += data.toString();
      });
      child.on("error", reject);
      child.on("close", (code) => {
        const output = (stdout + stderr).trim();
        if (code === 0) resolve(output);
        else reject(new Error(output || `Unmount failed (${code})`));
      });
      if (password) {
        child.stdin.write(`${password}\n`);
      }
      child.stdin.end();
    });
  };

  if (options && typeof options.password === "string") {
    try {
      await runWithSudo(options.password);
      return { success: true };
    } catch (error) {
      const detail = getErrorText(error);
      logCommandFailure(`sudo udisksctl unmount -b ${shq(devicePath)}`, error);
      return { success: false, error: detail || "Unmount failed" };
    }
  }

  try {
    const cmd = `udisksctl unmount -b ${shq(devicePath)} --no-user-interaction 2>&1`;
    await execPromise(cmd);
    return { success: true };
  } catch (error) {
    const detail = getErrorText(error);
    logCommandFailure(
      `udisksctl unmount -b ${shq(devicePath)} --no-user-interaction`,
      error,
    );
    if (needsAuth(detail)) {
      return { success: false, needsAuth: true, error: detail };
    }
    try {
      const cmd = `gio mount -u ${shq(devicePath)} 2>&1`;
      await execPromise(cmd);
      return { success: true };
    } catch (gioErr) {
      logCommandFailure(`gio mount -u ${shq(devicePath)}`, gioErr);
      try {
        const cmd = `umount ${shq(devicePath)} 2>&1`;
        await execPromise(cmd);
        return { success: true };
      } catch (umountErr) {
        logCommandFailure(`umount ${shq(devicePath)}`, umountErr);
        return { success: false, error: error.message || "Unmount failed" };
      }
    }
  }
});

ipcMain.handle("mount-device", async (event, devicePath, options = {}) => {
  const { exec } = require("child_process");
  const util = require("util");
  const execPromise = util.promisify(exec);

  const parseMountPoint = (output) => {
    const match = String(output || "").match(/at (.+?)\.?\s*$/);
    return match ? match[1].trim() : null;
  };

  const getErrorText = (err) =>
    [err?.message, err?.stderr, err?.stdout].filter(Boolean).join("\n");

  const needsAuth = (message) =>
    /not authorized|authentication is required|permission denied|access denied/i.test(
      String(message || ""),
    );

  // [Aero/doc 43] sem senha de administrador dentro do app: só o udisksctl normal (polkit)
  const runWithSudo = () => Promise.reject(new Error("Needs admin rights: mount it from a terminal."));
  const runWithSudoOriginal = (password) => {
    const { spawn } = require("child_process");
    return new Promise((resolve, reject) => {
      const child = spawn(
        "sudo",
        ["-S", "-p", "", "udisksctl", "mount", "-b", devicePath],
        { stdio: ["pipe", "pipe", "pipe"] },
      );
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (data) => {
        stdout += data.toString();
      });
      child.stderr.on("data", (data) => {
        stderr += data.toString();
      });
      child.on("error", reject);
      child.on("close", (code) => {
        const output = (stdout + stderr).trim();
        if (code === 0) resolve(output);
        else reject(new Error(output || `Mount failed (${code})`));
      });
      if (password) {
        child.stdin.write(`${password}\n`);
      }
      child.stdin.end();
    });
  };

  if (options && typeof options.password === "string") {
    try {
      const output = await runWithSudo(options.password);
      const mountpoint = parseMountPoint(output);
      return { success: true, mountpoint };
    } catch (error) {
      const detail = getErrorText(error);
      logCommandFailure(`sudo udisksctl mount -b ${shq(devicePath)}`, error);
      return { success: false, error: detail || "Mount failed" };
    }
  }

  try {
    const cmd = `udisksctl mount -b ${shq(devicePath)} --no-user-interaction 2>&1`;
    const { stdout } = await execPromise(cmd);

    const mountpoint = parseMountPoint(stdout);
    return { success: true, mountpoint };
  } catch (error) {
    const detail = getErrorText(error);
    logCommandFailure(
      `udisksctl mount -b ${shq(devicePath)} --no-user-interaction`,
      error,
    );
    const message = detail || error.message || "";
    if (needsAuth(message)) {
      return { success: false, needsAuth: true, error: message };
    }
    try {
      const cmd = `gio mount -d ${shq(devicePath)} 2>&1`;
      await execPromise(cmd);

      const cmdLsblk = `lsblk -n -o MOUNTPOINT ${shq(devicePath)} 2>/dev/null`;
      const { stdout: lsblkOut } = await execPromise(cmdLsblk);
      const mountpoint = lsblkOut.trim() || null;
      if (mountpoint) {
        return { success: true, mountpoint };
      }
    } catch (gioErr) {
      logCommandFailure(`gio mount -d ${shq(devicePath)}`, gioErr);
    }
    return { success: false, error: message || "Mount failed" };
  }
});


ipcMain.handle("show-open-dialog", async (event, options) => {
  const result = await dialog.showOpenDialog(mainWindow, options);
  return result;
});

ipcMain.handle("show-save-dialog", async (event, options) => {
  const result = await dialog.showSaveDialog(mainWindow, options);
  return result;
});


ipcMain.handle("read-file-preview", async (event, filePath) => {
  try {
    const stats = await fs.stat(filePath);

    const MAX_PREVIEW_SIZE = 1024 * 1024;
    if (stats.size > MAX_PREVIEW_SIZE) {
      return { success: false, error: "File too large for preview" };
    }

    const ext = path.extname(filePath).toLowerCase();
    const textExtensions = [
      ".txt",
      ".md",
      ".json",
      ".xml",
      ".html",
      ".css",
      ".js",
      ".ts",
      ".py",
      ".java",
      ".c",
      ".cpp",
      ".h",
      ".hpp",
      ".rs",
      ".go",
      ".rb",
      ".php",
      ".sh",
      ".bat",
      ".yml",
      ".yaml",
      ".ini",
      ".conf",
      ".log",
      ".csv",
      ".tsv",
      ".sql",
      ".rtf",
      ".tex",
      ".latex",
    ];

    if (!textExtensions.includes(ext)) {
      return {
        success: false,
        error: "File type not supported for text preview",
      };
    }

    const content = await fs.readFile(filePath, "utf8");
    return { success: true, content };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle("get-image-metadata", async (event, filePath) => {
  try {
    const stats = await fs.stat(filePath);
    let dimensions = null;

    try {
      dimensions = sizeOf(filePath);
    } catch (e) {}

    return {
      success: true,
      metadata: {
        width: dimensions?.width,
        height: dimensions?.height,
        type: dimensions?.type,
        orientation: dimensions?.orientation,
        hasAlpha: dimensions?.hasAlpha,
        fileSize: stats.size,
      },
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

// [Aero/doc 43] Miniaturas de VÍDEO (um quadro) e MÚSICA (capa embutida) com o ffmpeg.
// Cache em ~/.cache/ez-fm/thumbs (nome = hash do caminho + data + tamanho: arquivo mudou = miniatura nova).
// No máximo 3 ffmpeg ao mesmo tempo; argumentos separados (sem shell).
const thumbFila = [];
let thumbRodando = 0;
function thumbProximo() {
  while (thumbRodando < 3 && thumbFila.length) {
    const tarefa = thumbFila.shift();
    thumbRodando++;
    tarefa().finally(() => { thumbRodando--; thumbProximo(); });
  }
}
ipcMain.handle("get-media-thumbnail", async (event, filePath, kind) => {
  try {
    const crypto = require("crypto");
    const os = require("os");
    const { execFile } = require("child_process");
    const st = await fs.stat(filePath);
    const pasta = path.join(os.homedir(), ".cache", "ez-fm", "thumbs");
    await fs.mkdir(pasta, { recursive: true });
    const nome = crypto.createHash("sha1").update(`${filePath}|${st.mtimeMs}|${st.size}`).digest("hex");
    const saida = path.join(pasta, nome + ".jpg");
    try { await fs.access(saida); return saida; } catch {}
    const rodar = (args) => new Promise((ok) =>
      // -nostdin: o ffmpeg não mexe no terminal (senão o Linux PAUSA o app inteiro com SIGTTOU, doc 45)
      execFile("ffmpeg", ["-nostdin", ...args], { timeout: 20000 }, (err) => ok(!err)));
    return await new Promise((resolve) => {
      thumbFila.push(async () => {
        const escala = ["-vf", "scale=480:-2", "-frames:v", "1", "-y", saida];
        let deu;
        if (kind === "audio") {
          deu = await rodar(["-v", "error", "-i", filePath, "-an", "-map", "0:v:0", ...escala]);
        } else {
          deu = await rodar(["-v", "error", "-ss", "3", "-i", filePath, "-an", ...escala]) ||
                await rodar(["-v", "error", "-i", filePath, "-an", ...escala]);   // vídeo curto
        }
        resolve(deu ? saida : null);
      });
      thumbProximo();
    });
  } catch {
    return null;
  }
});

ipcMain.handle("get-video-metadata", async (event, filePath) => {
  try {
    const stats = await fs.stat(filePath);
    // [Aero/doc 43] execFile (sem shell): o nome do vídeo é só um argumento, nunca vira comando
    const { execFile } = require("child_process");
    const util = require("util");
    const execFilePromise = util.promisify(execFile);

    try {
      const { stdout } = await execFilePromise("ffprobe",
        ["-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", "--", filePath]);
      const data = JSON.parse(stdout);

      const videoStream = data.streams?.find((s) => s.codec_type === "video");
      const audioStream = data.streams?.find((s) => s.codec_type === "audio");
      const format = data.format;

      return {
        success: true,
        metadata: {
          duration: format?.duration ? parseFloat(format.duration) : null,
          fileSize: stats.size,
          bitrate: format?.bit_rate ? parseInt(format.bit_rate) : null,
          videoCodec: videoStream?.codec_name || null,
          videoWidth: videoStream?.width || null,
          videoHeight: videoStream?.height || null,
          videoFps: videoStream?.r_frame_rate
            ? (() => {
                const parts = videoStream.r_frame_rate.split("/");
                if (parts.length === 2) {
                  return parseFloat(parts[0]) / parseFloat(parts[1]);
                }
                return parseFloat(videoStream.r_frame_rate);
              })()
            : null,
          audioCodec: audioStream?.codec_name || null,
          audioChannels: audioStream?.channels || null,
          audioSampleRate: audioStream?.sample_rate || null,
        },
      };
    } catch (ffprobeError) {
      logCommandFailure(
        `ffprobe -v quiet -print_format json -show_format -show_streams "${filePath}"`,
        ffprobeError,
      );
      return {
        success: true,
        metadata: {
          fileSize: stats.size,
          note: "Install ffprobe for detailed video metadata",
        },
      };
    }
  } catch (error) {
    return { success: false, error: error.message };
  }
});
