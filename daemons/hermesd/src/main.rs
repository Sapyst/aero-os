//! `hermesd` — Daemon nativo em Rust do Hermes (`org.freedesktop.Notifications` + `com.aero.Hermes`)
//! e dos dados da Hestia (áudio, Bluetooth, rede) para o Aero OS.
//!
//! Arquitetura (fiel a `shell/hermes.py` e `docs/daimon/daimon-shell-nativo.md`):
//! 1. **Núcleo do Hermes (`/org/freedesktop/Notifications`)**:
//!    - Implementa a especificação Desktop Notifications 1.2 completa (`GetCapabilities`,
//!      `GetServerInformation`, `Notify`, `CloseNotification`, sinais `NotificationClosed`,
//!      `ActionInvoked`, `ActivationToken`).
//!    - Implementa a interface `com.aero.Hermes` consumida por `hermesctl`, `aerobar.py` e `som.py`
//!      (`History`, `Modes`, `SetModes`, `SetMode`, `DismissAll`, `Reload`, `LatestId`,
//!      `RemoveHistory`, `ClearHistory`, `Dismiss`, `InvokeAction`, `Hold`, `Arm`, `GetConfig`,
//!      `GetActive`, `GetAudio`, `GetBluetoothDevices`, `GetWifiNetworks`, `GetHestiaState`,
//!      e sinais `NotificationEvent` e `AudioChanged`).
//!    - Backends REAIS com leitura de volta (`backends.rs`, documentados em `daemons/API.md`):
//!      áudio (`ListSinks`, `ListSources`, `SetDefaultSink`, `SetDefaultSource`, `SetVolume`, `SetMute`),
//!      Wi-Fi (`GetWifi`, `SetWifiEnabled`, `ListNetworks`, `WifiConnect`, `WifiDisconnect`),
//!      Bluetooth (`GetBluetooth`, `SetBluetoothPowered`, `BluetoothConnect`, `BluetoothDisconnect`)
//!      e Não perturbe (`GetDnd`, `SetDnd`), com os sinais `AudioChanged(s)`, `WifiChanged(s)`,
//!      `BluetoothChanged(s)` e `DndChanged(b)`. Todo setter devolve o estado que REALMENTE ficou.
//!    - `--teste`: sobe como `com.aero.Hermes.Teste` sem tocar em `org.freedesktop.Notifications`.
//!    - Valida `~/.config/aero/hermes.json` com os mesmos padrões e limites de `hermes.py`.
//!    - Sanitiza markup Pango (`<b>`, `<i>`, `<u>`, `<br>`, entidades XML), aplica limite de
//!      20 notificações/s por remetente, substituição por tag (`x-canonical-private-synchronous` /
//!      `x-dunst-stack-tag`), modo Não Perturbe (`dnd`), regras de usuário (`rules`) e gravação
//!      atômica de `~/.local/state/aero/hermes-history.json` (`0600`, `O_NOFOLLOW`).
//!    - Converte imagens cruas (`image-data` / `icon_data`) em arquivos PNG temporários em
//!      `$XDG_RUNTIME_DIR/hermes-icons/` (redimensionados para no máximo 128×128) para não reter
//!      buffers de até 16 MB na RAM nem trafegá-los pelo D-Bus.
//! 2. **Dados da Hestia (Áudio, Bluetooth, Rede) orientados a eventos (0% CPU parado)**:
//!    - **Áudio**: escuta `pactl subscribe` (com `PR_SET_PDEATHSIG(SIGTERM)`) numa thread bloqueante
//!      e mantém a lista de `sinks` e `sources` pronta em memória, emitindo `AudioChanged`.
//!    - **Bluetooth**: lê `org.bluez` (`GetManagedObjects`) direto no barramento D-Bus do sistema
//!      e escuta sinais `InterfacesAdded` / `InterfacesRemoved` / `PropertiesChanged` sem `bluetoothctl`.
//!    - **Rede (Wi-Fi)**: lê `org.freedesktop.NetworkManager` (`WirelessEnabled`, `ActiveConnections`)
//!      direto no barramento D-Bus do sistema e escuta sinais `PropertiesChanged` sem `nmcli`.

use std::collections::{BTreeSet, HashMap, VecDeque};
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value as JsonValue};
use zbus::blocking::Connection as DbusConn;
use zbus::fdo::RequestNameFlags;
use zbus::message::Header;
use zbus::names::WellKnownName;
use zbus::object_server::SignalEmitter;
use zbus::zvariant::{OwnedObjectPath, OwnedValue, Value};
use zbus::interface;

mod backends;

const CAMINHO_NOTIF: &str = "/org/freedesktop/Notifications";
const NOME_NOTIF: &str = "org.freedesktop.Notifications";
const NOME_HERMES: &str = "com.aero.Hermes";

/// Nome de barramento que este processo assume: `com.aero.Hermes` normalmente, `com.aero.Hermes.Teste`
/// com `--teste` (instância de teste ao lado da real, sem tocar em `org.freedesktop.Notifications`).
static NOME_BUS: OnceLock<String> = OnceLock::new();

fn nome_bus() -> &'static str {
    NOME_BUS.get().map(String::as_str).unwrap_or(NOME_HERMES)
}

fn modo_teste() -> bool {
    nome_bus() != NOME_HERMES
}

/// Motivos de fechamento da especificação freedesktop Notifications.
const EXPIROU: u32 = 1;
const DISPENSADA: u32 = 2;
const PEDIDO: u32 = 3;

/// Limite máximo de notificações por segundo vindas de um mesmo remetente D-Bus.
const LIMITE_POR_SEG: u32 = 20;

/// Limite máximo de bytes de uma imagem crua (`image-data`).
const MAX_IMAGEM_BYTES: usize = 16 * 1024 * 1024;

const POSICOES_VALIDAS: &[&str] = &[
    "follow-bell",
    "follow-central",
    "top-left",
    "top-center",
    "top-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
];

// ---------------- Configuração (`~/.config/aero/hermes.json`) ----------------

#[derive(Clone, Serialize, Deserialize)]
struct TimeoutConfig {
    low: i32,
    normal: i32,
    critical: i32,
}

#[derive(Clone, Serialize, Deserialize)]
struct HistoryConfig {
    size: usize,
}

#[derive(Clone, Serialize, Deserialize)]
struct DndConfig {
    enabled: bool,
    allow_critical: bool,
}

#[derive(Clone, Serialize, Deserialize)]
struct WaveConfig {
    enabled: bool,
    origin: String,
    color: String,
    urgent_color: String,
    duration_ms: i32,
    dot_spacing: i32,
    dot_size: f64,
    reach: f64,
    intensity: f64,
    width: f64,
    reduced_motion: String,
    fullscreen: String,
    fullscreen_intensity: f64,
    under_bar: String,
}

#[derive(Clone, Serialize, Deserialize)]
struct ColorsConfig {
    background: String,
    text: String,
    dim: String,
    accent: String,
}

#[derive(Clone, Serialize, Deserialize)]
struct FontConfig {
    title: String,
    body: String,
    mono: String,
}

#[derive(Clone)]
struct RegraCompilada {
    match_app: Option<Regex>,
    match_summary: Option<Regex>,
    match_body: Option<Regex>,
    match_category: Option<Regex>,
    match_desktop_entry: Option<Regex>,
    match_urgency: Option<String>,
    set_hide: Option<bool>,
    set_wave: Option<bool>,
    set_timeout: Option<i32>,
    set_history: Option<bool>,
    set_urgency: Option<u8>,
    set_sound: Option<bool>,
    raw_json: JsonValue,
}

#[derive(Clone)]
struct HermesConfig {
    style: String,
    position: String,
    margin: i32,
    width: i32,
    gap: i32,
    max_visible: usize,
    timeout: TimeoutConfig,
    history: HistoryConfig,
    dnd: DndConfig,
    fullscreen: String,
    wave: WaveConfig,
    colors: ColorsConfig,
    colors_light: ColorsConfig,
    font: FontConfig,
    radius: i32,
    icons: bool,
    steam: String,
    rules: Vec<RegraCompilada>,
}

impl Default for HermesConfig {
    fn default() -> Self {
        Self {
            style: "dotstyle".to_string(),
            position: "follow-bell".to_string(),
            margin: 12,
            width: 340,
            gap: 8,
            max_visible: 4,
            timeout: TimeoutConfig {
                low: 3000,
                normal: 5000,
                critical: 0,
            },
            history: HistoryConfig { size: 100 },
            dnd: DndConfig {
                enabled: false,
                allow_critical: true,
            },
            fullscreen: "hold".to_string(),
            wave: WaveConfig {
                enabled: true,
                origin: "notification".to_string(),
                color: "auto".to_string(),
                urgent_color: "#e8312a".to_string(),
                duration_ms: 1400,
                dot_spacing: 22,
                dot_size: 0.3,
                reach: 0.55,
                intensity: 0.55,
                width: 0.11,
                reduced_motion: "auto".to_string(),
                fullscreen: "faded".to_string(),
                fullscreen_intensity: 0.22,
                under_bar: "soft".to_string(),
            },
            colors: ColorsConfig {
                background: "#0b0b0c".to_string(),
                text: "#ecebe8".to_string(),
                dim: "#8c8b88".to_string(),
                accent: "#e8312a".to_string(),
            },
            colors_light: ColorsConfig {
                background: "#f4f4f1".to_string(),
                text: "#151516".to_string(),
                dim: "#6b6a67".to_string(),
                accent: "#d42a22".to_string(),
            },
            font: FontConfig {
                title: "Doto".to_string(),
                body: "Geist".to_string(),
                mono: "Geist Mono".to_string(),
            },
            radius: 22,
            icons: true,
            steam: "wave".to_string(),
            rules: Vec::new(),
        }
    }
}

impl HermesConfig {
    fn para_json(&self) -> JsonValue {
        json!({
            "style": self.style,
            "position": self.position,
            "margin": self.margin,
            "width": self.width,
            "gap": self.gap,
            "max_visible": self.max_visible,
            "timeout": self.timeout,
            "history": self.history,
            "dnd": self.dnd,
            "fullscreen": self.fullscreen,
            "wave": self.wave,
            "colors": self.colors,
            "colors_light": self.colors_light,
            "font": self.font,
            "radius": self.radius,
            "icons": self.icons,
            "steam": self.steam,
            "rules": self.rules.iter().map(|r| r.raw_json.clone()).collect::<Vec<_>>(),
        })
    }
}

fn eh_cor_hex(s: &str) -> bool {
    s.len() == 7 && s.starts_with('#') && s[1..].chars().all(|c| c.is_ascii_hexdigit())
}

fn eh_nome_fonte(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.chars()
            .all(|c| c.is_alphanumeric() || c == '_' || c == ' ' || c == '.' || c == '-')
}

fn num_i32(v: Option<&JsonValue>, padrao: i32, min: i32, max: i32) -> i32 {
    match v {
        Some(JsonValue::Number(n)) => {
            if let Some(i) = n.as_i64() {
                (i as i32).clamp(min, max)
            } else if let Some(f) = n.as_f64() {
                (f as i32).clamp(min, max)
            } else {
                padrao
            }
        }
        _ => padrao,
    }
}

fn num_f64(v: Option<&JsonValue>, padrao: f64, min: f64, max: f64) -> f64 {
    match v {
        Some(JsonValue::Number(n)) => n.as_f64().map(|f| f.clamp(min, max)).unwrap_or(padrao),
        _ => padrao,
    }
}

fn validar_regra(v: &JsonValue) -> Option<RegraCompilada> {
    let obj = v.as_object()?;
    let m_obj = obj.get("match")?.as_object()?;
    let s_obj = obj.get("set")?.as_object()?;

    let mut match_app = None;
    let mut match_summary = None;
    let mut match_body = None;
    let mut match_category = None;
    let mut match_desktop_entry = None;
    let mut match_urgency = None;

    for (k, val) in m_obj {
        let texto = val.as_str()?;
        if texto.len() > 200 {
            return None;
        }
        if k == "urgency" {
            if !matches!(texto, "low" | "normal" | "critical") {
                return None;
            }
            match_urgency = Some(texto.to_string());
        } else {
            let pat = format!("(?i){texto}");
            let re = Regex::new(&pat).ok()?;
            match k.as_str() {
                "app" => match_app = Some(re),
                "summary" => match_summary = Some(re),
                "body" => match_body = Some(re),
                "category" => match_category = Some(re),
                "desktop_entry" => match_desktop_entry = Some(re),
                _ => return None,
            }
        }
    }

    let mut set_hide = None;
    let mut set_wave = None;
    let mut set_timeout = None;
    let mut set_history = None;
    let mut set_urgency = None;
    let mut set_sound = None;

    for (k, val) in s_obj {
        match k.as_str() {
            "hide" => set_hide = Some(val.as_bool()?),
            "wave" => set_wave = Some(val.as_bool()?),
            "history" => set_history = Some(val.as_bool()?),
            "sound" => set_sound = Some(val.as_bool()?),
            "timeout" => {
                let t = val.as_i64()?;
                if !(0..=3_600_000).contains(&t) {
                    return None;
                }
                set_timeout = Some(t as i32);
            }
            "urgency" => {
                let u = match val.as_str()? {
                    "low" => 0,
                    "normal" => 1,
                    "critical" => 2,
                    _ => return None,
                };
                set_urgency = Some(u);
            }
            _ => return None,
        }
    }

    if m_obj.is_empty() && s_obj.is_empty() {
        return None;
    }

    Some(RegraCompilada {
        match_app,
        match_summary,
        match_body,
        match_category,
        match_desktop_entry,
        match_urgency,
        set_hide,
        set_wave,
        set_timeout,
        set_history,
        set_urgency,
        set_sound,
        raw_json: v.clone(),
    })
}

fn validar_config(bruto: &JsonValue) -> HermesConfig {
    let mut c = HermesConfig::default();
    let Some(obj) = bruto.as_object() else {
        return c;
    };

    if let Some(s) = obj.get("style").and_then(|v| v.as_str())
        && matches!(s, "dotstyle" | "aeroterm" | "aerocloud") {
            c.style = s.to_string();
        }
    if let Some(p) = obj.get("position").and_then(|v| v.as_str())
        && POSICOES_VALIDAS.contains(&p) {
            c.position = p.to_string();
        }
    c.margin = num_i32(obj.get("margin"), c.margin, 0, 400);
    c.width = num_i32(obj.get("width"), c.width, 200, 900);
    c.gap = num_i32(obj.get("gap"), c.gap, 0, 60);
    c.max_visible = num_i32(obj.get("max_visible"), c.max_visible as i32, 1, 12) as usize;
    c.radius = num_i32(obj.get("radius"), c.radius, 0, 60);
    if let Some(ic) = obj.get("icons").and_then(|v| v.as_bool()) {
        c.icons = ic;
    }
    if let Some(t) = obj.get("timeout").and_then(|v| v.as_object()) {
        c.timeout.low = num_i32(t.get("low"), c.timeout.low, 0, 3_600_000);
        c.timeout.normal = num_i32(t.get("normal"), c.timeout.normal, 0, 3_600_000);
        c.timeout.critical = num_i32(t.get("critical"), c.timeout.critical, 0, 3_600_000);
    }
    if let Some(h) = obj.get("history").and_then(|v| v.as_object()) {
        c.history.size = num_i32(h.get("size"), c.history.size as i32, 0, 1000) as usize;
    }
    if let Some(d) = obj.get("dnd").and_then(|v| v.as_object()) {
        if let Some(en) = d.get("enabled").and_then(|v| v.as_bool()) {
            c.dnd.enabled = en;
        }
        if let Some(ac) = d.get("allow_critical").and_then(|v| v.as_bool()) {
            c.dnd.allow_critical = ac;
        }
    }
    if let Some(fs) = obj.get("fullscreen").and_then(|v| v.as_str())
        && matches!(fs, "hold" | "show") {
            c.fullscreen = fs.to_string();
        }
    if let Some(w) = obj.get("wave").and_then(|v| v.as_object()) {
        if let Some(en) = w.get("enabled").and_then(|v| v.as_bool()) {
            c.wave.enabled = en;
        }
        if let Some(or) = w.get("origin").and_then(|v| v.as_str())
            && matches!(or, "notification" | "screen-center" | "off-card") {
                c.wave.origin = or.to_string();
            }
        if let Some(col) = w.get("color").and_then(|v| v.as_str())
            && (col == "auto" || eh_cor_hex(col)) {
                c.wave.color = col.to_string();
            }
        if let Some(uc) = w.get("urgent_color").and_then(|v| v.as_str())
            && eh_cor_hex(uc) {
                c.wave.urgent_color = uc.to_string();
            }
        c.wave.duration_ms = num_i32(w.get("duration_ms"), c.wave.duration_ms, 200, 6000);
        c.wave.dot_spacing = num_i32(w.get("dot_spacing"), c.wave.dot_spacing, 8, 80);
        c.wave.dot_size = num_f64(w.get("dot_size"), c.wave.dot_size, 0.05, 0.5);
        c.wave.reach = num_f64(w.get("reach"), c.wave.reach, 0.05, 1.5);
        c.wave.intensity = num_f64(w.get("intensity"), c.wave.intensity, 0.05, 1.0);
        c.wave.width = num_f64(w.get("width"), c.wave.width, 0.02, 0.5);
        if let Some(rm) = w.get("reduced_motion").and_then(|v| v.as_str())
            && matches!(rm, "auto" | "on" | "off") {
                c.wave.reduced_motion = rm.to_string();
            }
        if let Some(ub) = w.get("under_bar").and_then(|v| v.as_str())
            && matches!(ub, "soft" | "normal" | "hide") {
                c.wave.under_bar = ub.to_string();
            }
        if let Some(fs) = w.get("fullscreen").and_then(|v| v.as_str())
            && matches!(fs, "faded" | "normal" | "off") {
                c.wave.fullscreen = fs.to_string();
            }
        c.wave.fullscreen_intensity = num_f64(
            w.get("fullscreen_intensity"),
            c.wave.fullscreen_intensity,
            0.02,
            1.0,
        );
    }

    for (chave, destino) in [("colors", &mut c.colors), ("colors_light", &mut c.colors_light)] {
        if let Some(cores) = obj.get(chave).and_then(|v| v.as_object()) {
            if let Some(s) = cores.get("background").and_then(|v| v.as_str())
                && eh_cor_hex(s) {
                    destino.background = s.to_string();
                }
            if let Some(s) = cores.get("text").and_then(|v| v.as_str())
                && eh_cor_hex(s) {
                    destino.text = s.to_string();
                }
            if let Some(s) = cores.get("dim").and_then(|v| v.as_str())
                && eh_cor_hex(s) {
                    destino.dim = s.to_string();
                }
            if let Some(s) = cores.get("accent").and_then(|v| v.as_str())
                && eh_cor_hex(s) {
                    destino.accent = s.to_string();
                }
        }
    }

    if let Some(f) = obj.get("font").and_then(|v| v.as_object()) {
        if let Some(s) = f.get("title").and_then(|v| v.as_str())
            && eh_nome_fonte(s) {
                c.font.title = s.to_string();
            }
        if let Some(s) = f.get("body").and_then(|v| v.as_str())
            && eh_nome_fonte(s) {
                c.font.body = s.to_string();
            }
        if let Some(s) = f.get("mono").and_then(|v| v.as_str())
            && eh_nome_fonte(s) {
                c.font.mono = s.to_string();
            }
    }

    if let Some(st) = obj.get("steam").and_then(|v| v.as_str())
        && matches!(st, "wave" | "move" | "off") {
            c.steam = st.to_string();
        }

    if let Some(regras) = obj.get("rules").and_then(|v| v.as_array()) {
        c.rules = regras
            .iter()
            .take(100)
            .filter_map(validar_regra)
            .collect();
    }

    c
}

fn caminho_config_hermes() -> PathBuf {
    let home = env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    PathBuf::from(home).join(".config/aero/hermes.json")
}

fn caminho_historico_hermes() -> PathBuf {
    let home = env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    PathBuf::from(home).join(".local/state/aero/hermes-history.json")
}

fn ler_config() -> HermesConfig {
    if let Ok(texto) = fs::read_to_string(caminho_config_hermes())
        && let Ok(val) = serde_json::from_str::<JsonValue>(&texto) {
            return validar_config(&val);
        }
    HermesConfig::default()
}

// ---------------- Sanitização de Texto e Markup ----------------

fn escapar_markup(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            _ => out.push(ch),
        }
    }
    out
}

fn truncar_chars(s: &str, limite: usize) -> &str {
    match s.char_indices().nth(limite) {
        Some((idx, _)) => &s[..idx],
        None => s,
    }
}

/// Remove caracteres de controle (`[\x00-\x08\x0b-\x1f\x7f]`) e limita o comprimento.
fn texto_puro(texto: &str, limite: usize) -> String {
    texto
        .chars()
        .filter(|&c| {
            let u = c as u32;
            !((u <= 0x08) || (0x0b..=0x1f).contains(&u) || u == 0x7f)
        })
        .take(limite)
        .collect()
}

fn entidade_valida(ent: &str) -> bool {
    if matches!(ent, "amp" | "lt" | "gt" | "quot" | "apos") {
        return true;
    }
    if let Some(hex) = ent.strip_prefix("#x") {
        return (1..=6).contains(&hex.len()) && hex.chars().all(|c| c.is_ascii_hexdigit());
    }
    if let Some(dec) = ent.strip_prefix('#') {
        return (1..=7).contains(&dec.len()) && dec.chars().all(|c| c.is_ascii_digit());
    }
    false
}

/// Sanitiza o corpo da notificação para Pango markup seguro (`<b>`, `<i>`, `<u>`, `<br>`),
/// exatamente como `hermes.limpar_markup`.
fn limpar_markup(texto: &str, limite: usize) -> String {
    let cortado = truncar_chars(texto, limite);
    let re = Regex::new(r"<\s*(/?)\s*([a-zA-Z]+)[^>]*>|&(#?\w+);").unwrap();
    let mut saida = String::with_capacity(cortado.len());
    let mut abertas: Vec<&str> = Vec::new();
    let mut pos = 0;

    for caps in re.captures_iter(cortado) {
        let m = caps.get(0).unwrap();
        saida.push_str(&escapar_markup(&cortado[pos..m.start()]));
        pos = m.end();

        if let Some(ent_match) = caps.get(3) {
            let ent = ent_match.as_str();
            if entidade_valida(ent) {
                saida.push('&');
                saida.push_str(ent);
                saida.push(';');
            } else {
                saida.push_str(&escapar_markup(m.as_str()));
            }
            continue;
        }

        let fecha = caps.get(1).map(|g| g.as_str() == "/").unwrap_or(false);
        let tag_raw = caps.get(2).map(|g| g.as_str()).unwrap_or("");
        if tag_raw.eq_ignore_ascii_case("br") {
            saida.push('\n');
        } else {
            let tag_norm = if tag_raw.eq_ignore_ascii_case("b") {
                Some("b")
            } else if tag_raw.eq_ignore_ascii_case("i") {
                Some("i")
            } else if tag_raw.eq_ignore_ascii_case("u") {
                Some("u")
            } else {
                None
            };
            if let Some(tag) = tag_norm {
                if !fecha {
                    saida.push('<');
                    saida.push_str(tag);
                    saida.push('>');
                    abertas.push(tag);
                } else if abertas.contains(&tag) {
                    while let Some(t) = abertas.pop() {
                        saida.push_str("</");
                        saida.push_str(t);
                        saida.push('>');
                        if t == tag {
                            break;
                        }
                    }
                }
            }
        }
    }

    saida.push_str(&escapar_markup(&cortado[pos..]));
    for t in abertas.into_iter().rev() {
        saida.push_str("</");
        saida.push_str(t);
        saida.push('>');
    }
    saida
}

/// Converte markup seguro em texto puro para gravar no histórico (`markup_para_texto`).
fn markup_para_texto(markup: &str) -> String {
    let mut sem_tags = String::with_capacity(markup.len());
    let mut dentro_tag = false;
    for ch in markup.chars() {
        match ch {
            '<' => dentro_tag = true,
            '>' => dentro_tag = false,
            _ if !dentro_tag => sem_tags.push(ch),
            _ => {}
        }
    }
    sem_tags
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

// ---------------- Notificação e Imagens Temporárias ----------------

fn urgencia_str(u: u8) -> &'static str {
    match u {
        0 => "low",
        2 => "critical",
        _ => "normal",
    }
}

fn pasta_icones_temp() -> PathBuf {
    let base = env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| "/tmp".to_string());
    PathBuf::from(base).join("hermes-icons")
}

/// Salva um hint `image-data` `(iiibiiay)` como um PNG compacto (máx 128×128) em
/// `$XDG_RUNTIME_DIR/hermes-icons/notif-<id>.png`, evitando manter buffers de até 16 MB na RAM.
fn salvar_imagem_hint(id: u32, v: &Value<'_>) -> Option<String> {
    let mut val = v;
    while let Value::Value(inner) = val {
        val = inner;
    }
    let Value::Structure(s) = val else {
        return None;
    };
    let c = s.fields();
    if c.len() < 7 {
        return None;
    }
    let w = i32::try_from(&c[0]).ok()?;
    let h = i32::try_from(&c[1]).ok()?;
    let stride = i32::try_from(&c[2]).ok()?;
    let alfa = bool::try_from(&c[3]).ok()?;
    let bits = i32::try_from(&c[4]).ok()?;
    let canais = i32::try_from(&c[5]).ok()?;

    if !(0 < w && w <= 4096 && 0 < h && h <= 4096 && bits == 8 && (canais == 3 || canais == 4) && stride >= w * canais)
    {
        return None;
    }
    if alfa != (canais == 4) {
        return None;
    }
    let precisa = (stride as usize) * ((h as usize) - 1) + (w as usize) * (canais as usize);

    let Value::Array(arr) = &c[6] else {
        return None;
    };
    if arr.len() < precisa || arr.len() > MAX_IMAGEM_BYTES {
        return None;
    }
    let mut dados = Vec::with_capacity(arr.len());
    for b in arr.iter() {
        dados.push(u8::try_from(b).ok()?);
    }
    if dados.len() < precisa {
        return None;
    }

    let (w_us, h_us, stride_us, ch_us) = (w as usize, h as usize, stride as usize, canais as usize);
    let max_lado = 96usize;
    let (out_w, out_h) = if w_us > max_lado || h_us > max_lado {
        if w_us >= h_us {
            (max_lado, (h_us * max_lado / w_us).max(1))
        } else {
            ((w_us * max_lado / h_us).max(1), max_lado)
        }
    } else {
        (w_us, h_us)
    };

    let mut rgba = vec![0u8; out_w * out_h * 4];
    for oy in 0..out_h {
        let sy = (oy * h_us / out_h).min(h_us - 1);
        for ox in 0..out_w {
            let sx = (ox * w_us / out_w).min(w_us - 1);
            let idx = sy * stride_us + sx * ch_us;
            let dst = (oy * out_w + ox) * 4;
            rgba[dst] = dados[idx];
            rgba[dst + 1] = dados[idx + 1];
            rgba[dst + 2] = dados[idx + 2];
            rgba[dst + 3] = if ch_us == 4 { dados[idx + 3] } else { 255 };
        }
    }

    let pasta = pasta_icones_temp();
    let _ = fs::DirBuilder::new().recursive(true).mode(0o700).create(&pasta);
    let arq = pasta.join(format!("notif-{id}.png"));
    let file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&arq)
        .ok()?;
    let mut encoder = png::Encoder::new(file, out_w as u32, out_h as u32);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder.write_header().ok()?;
    writer.write_image_data(&rgba).ok()?;
    Some(arq.to_string_lossy().into_owned())
}

#[derive(Clone, Serialize, Deserialize)]
struct ResumoHistorico {
    id: u32,
    app: String,
    summary: String,
    body: String,
    urgency: String,
    category: String,
    icon: String,
    desktop_entry: String,
    time: f64,
    actions: Vec<[String; 2]>,
}

#[derive(Clone)]
struct Notificacao {
    id: u32,
    app: String,
    icone: String,
    imagem_temp: Option<String>,
    titulo: String,
    corpo: String,
    acoes: Vec<(String, String)>,
    urgencia: u8,
    categoria: String,
    entrada: String,
    valor: Option<i32>,
    etiqueta: Option<String>,
    transitoria: bool,
    residente: bool,
    som: Option<String>,
    timeout: i32,
    remetente: String,
    criada: f64,
    escondida: bool,
    onda: bool,
    guardar: bool,
    pendente: bool,
}

impl Notificacao {
    fn icone_efetivo(&self) -> &str {
        self.imagem_temp.as_deref().unwrap_or(&self.icone)
    }

    fn resumo(&self) -> ResumoHistorico {
        ResumoHistorico {
            id: self.id,
            app: self.app.clone(),
            summary: self.titulo.clone(),
            body: markup_para_texto(&self.corpo),
            urgency: urgencia_str(self.urgencia).to_string(),
            category: self.categoria.clone(),
            icon: self.icone.clone(),
            desktop_entry: self.entrada.clone(),
            time: (self.criada * 1000.0).round() / 1000.0,
            actions: self
                .acoes
                .iter()
                .map(|(k, t)| [k.clone(), t.clone()])
                .collect(),
        }
    }

    fn para_ui_json(&self) -> JsonValue {
        json!({
            "id": self.id,
            "app": self.app,
            "icone": self.icone_efetivo(),
            "titulo": self.titulo,
            "corpo": self.corpo,
            "acoes": self.acoes,
            "urgencia": self.urgencia,
            "categoria": self.categoria,
            "entrada": self.entrada,
            "valor": self.valor,
            "onda": self.onda,
            "criada": self.criada,
            "residente": self.residente,
            "som": self.som,
        })
    }

    fn limpar_temp(&self) {
        if let Some(ref p) = self.imagem_temp {
            let _ = fs::remove_file(p);
        }
    }
}

fn aplicar_regras(n: &mut Notificacao, regras: &[RegraCompilada]) {
    for r in regras {
        if let Some(ref re) = r.match_app
            && !re.is_match(&n.app) {
                continue;
            }
        if let Some(ref re) = r.match_summary
            && !re.is_match(&n.titulo) {
                continue;
            }
        if let Some(ref re) = r.match_body
            && !re.is_match(&n.corpo) {
                continue;
            }
        if let Some(ref re) = r.match_category
            && !re.is_match(&n.categoria) {
                continue;
            }
        if let Some(ref re) = r.match_desktop_entry
            && !re.is_match(&n.entrada) {
                continue;
            }
        if let Some(ref u_str) = r.match_urgency
            && urgencia_str(n.urgencia) != u_str {
                continue;
            }

        if let Some(h) = r.set_hide {
            n.escondida = h;
        }
        if let Some(w) = r.set_wave {
            n.onda = w;
        }
        if let Some(t) = r.set_timeout {
            n.timeout = t;
        }
        if let Some(his) = r.set_history {
            n.guardar = his;
        }
        if let Some(u) = r.set_urgency {
            n.urgencia = u;
        }
        let _ = r.set_sound;
    }
}

fn extrair_int_hint(v: &Value<'_>) -> Option<i32> {
    let mut val = v;
    while let Value::Value(inner) = val {
        val = inner;
    }
    match val {
        Value::U8(x) => Some(*x as i32),
        Value::I16(x) => Some(*x as i32),
        Value::U16(x) => Some(*x as i32),
        Value::I32(x) => Some(*x),
        Value::U32(x) => Some(*x as i32),
        Value::I64(x) => Some(*x as i32),
        Value::U64(x) => Some(*x as i32),
        _ => None,
    }
}

fn extrair_bool_hint(v: &Value<'_>) -> Option<bool> {
    let mut val = v;
    while let Value::Value(inner) = val {
        val = inner;
    }
    match val {
        Value::Bool(b) => Some(*b),
        Value::U8(x) => Some(*x != 0),
        Value::I32(x) => Some(*x != 0),
        Value::U32(x) => Some(*x != 0),
        _ => None,
    }
}

fn extrair_str_hint<'a>(v: &'a Value<'a>) -> Option<&'a str> {
    let mut val = v;
    while let Value::Value(inner) = val {
        val = inner;
    }
    if let Value::Str(s) = val {
        Some(s.as_str())
    } else {
        None
    }
}

fn agora_segundos() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Envia `SIGRTMIN+10` para o Waybar quando os modos mudam (ex: DND).
fn avisar_waybar_dnd() {
    let sinal = libc::SIGRTMIN() + 10;
    let Ok(entries) = fs::read_dir("/proc") else {
        return;
    };
    let uid_atual = unsafe { libc::getuid() };
    for entry in entries.flatten() {
        let nome = entry.file_name();
        let Some(s) = nome.to_str() else {
            continue;
        };
        let Ok(pid) = s.parse::<i32>() else {
            continue;
        };
        let comm_path = entry.path().join("comm");
        if let Ok(comm) = fs::read_to_string(comm_path)
            && comm.trim() == "waybar"
            && let Ok(meta) = entry.metadata()
            && std::os::unix::fs::MetadataExt::uid(&meta) == uid_atual
        {
            unsafe {
                libc::kill(pid, sinal);
            }
        }
    }
}


// ---------------- Dados da Hestia (Áudio, Bluetooth, Rede) ----------------

fn nome_bonito_audio(desc: &str, nome: &str, entrada: bool) -> String {
    if let Some(ini) = desc.find('[')
        && let Some(fim) = desc[ini + 1..].find(']') {
            let monitor = &desc[ini + 1..ini + 1 + fim];
            if !monitor.is_empty() {
                return format!("{monitor} (HDMI)");
            }
        }
    if nome.contains("pci-0000_66") {
        return if entrada {
            "Laptop microphone".to_string()
        } else {
            "Laptop speakers".to_string()
        };
    }
    let mut limpo = desc.to_string();
    for lixo in [
        " Analog Stereo",
        " Digital Stereo",
        " (Stereo)",
        " Mono",
        " Multichannel",
    ] {
        limpo = limpo.replace(lixo, "");
    }
    limpo.trim().to_string()
}

fn consultar_lista_pactl(tipo: &str) -> Vec<JsonValue> {
    let cmd_padrao = if tipo == "sinks" {
        "get-default-sink"
    } else {
        "get-default-source"
    };
    let padrao = Command::new("pactl")
        .arg(cmd_padrao)
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();

    let Ok(out) = Command::new("pactl")
        .args(["-f", "json", "list", tipo])
        .output()
    else {
        return Vec::new();
    };
    if !out.status.success() {
        return Vec::new();
    }
    let Ok(lista) = serde_json::from_slice::<Vec<JsonValue>>(&out.stdout) else {
        return Vec::new();
    };

    let eh_entrada = tipo == "sources";
    let mut itens = Vec::new();
    for mut d in lista {
        let Some(obj) = d.as_object_mut() else {
            continue;
        };
        let nome = obj
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if nome.is_empty() || nome.ends_with(".monitor") {
            continue;
        }
        let porta_ativa = obj
            .get("active_port")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if !porta_ativa.is_empty() && nome != padrao
            && let Some(ports) = obj.get("ports").and_then(|v| v.as_array()) {
                let indisponivel = ports.iter().any(|p| {
                    p.get("name").and_then(|v| v.as_str()) == Some(porta_ativa.as_str())
                        && p.get("availability").and_then(|v| v.as_str()) == Some("not available")
                });
                if indisponivel {
                    continue;
                }
            }
        let desc = obj
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or(&nome)
            .to_string();
        let bonito = nome_bonito_audio(&desc, &nome, eh_entrada);
        let id_pw = backends::id_pipewire(&JsonValue::Object(obj.clone()));
        let vol = backends::volume_pct_pactl(&JsonValue::Object(obj.clone()));
        obj.insert("padrao".to_string(), JsonValue::Bool(nome == padrao));
        obj.insert("is_default".to_string(), JsonValue::Bool(nome == padrao));
        obj.insert("pretty_name".to_string(), JsonValue::String(bonito));
        obj.insert("id".to_string(), JsonValue::from(id_pw));
        obj.insert("volume_pct".to_string(), JsonValue::from(vol));
        itens.push(d);
    }
    itens
}

#[derive(Clone, Default, Serialize)]
struct EstadoAudio {
    sinks: Vec<JsonValue>,
    sources: Vec<JsonValue>,
}

fn coletar_audio_atual() -> EstadoAudio {
    EstadoAudio {
        sinks: consultar_lista_pactl("sinks"),
        sources: consultar_lista_pactl("sources"),
    }
}

impl EstadoAudio {
    /// Estado compacto para a API (`ListSinks`/`ListSources`/`AudioChanged`):
    /// `{"sinks": [{id,index,name,pretty_name,is_default,volume,muted}], "sources": [...]}`.
    fn compacto(&self) -> JsonValue {
        json!({
            "sinks": self.sinks.iter().map(backends::aparelho_compacto).collect::<Vec<_>>(),
            "sources": self.sources.iter().map(backends::aparelho_compacto).collect::<Vec<_>>(),
        })
    }
}

#[derive(Clone, Default, Serialize)]
struct AparelhoBt {
    mac: String,
    nome: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    uso: Option<bool>,
}

#[derive(Clone, Default, Serialize)]
struct EstadoBluetooth {
    powered: bool,
    connected: Vec<String>,
    paired: Vec<AparelhoBt>,
    nearby: Vec<AparelhoBt>,
}

/// Consulta `org.bluez` direto pelo barramento do sistema (`GetManagedObjects`), sem `bluetoothctl`.
fn consultar_bluetooth_dbus(sys_conn: &DbusConn) -> EstadoBluetooth {
    let mut est = EstadoBluetooth::default();
    let Ok(msg) = sys_conn.call_method(
        Some("org.bluez"),
        "/",
        Some("org.freedesktop.DBus.ObjectManager"),
        "GetManagedObjects",
        &(),
    ) else {
        return est;
    };

    type MapaObjetos = HashMap<OwnedObjectPath, HashMap<String, HashMap<String, OwnedValue>>>;
    let Ok(objetos) = msg.body().deserialize::<MapaObjetos>() else {
        return est;
    };

    for ifaces in objetos.values() {
        if let Some(adapter) = ifaces.get("org.bluez.Adapter1")
            && let Some(p) = adapter.get("Powered").and_then(|v| bool::try_from(v).ok())
                && p {
                    est.powered = true;
                }
        if let Some(dev) = ifaces.get("org.bluez.Device1") {
            let mac = dev
                .get("Address")
                .and_then(|v| <&str>::try_from(v).ok())
                .unwrap_or("")
                .to_string();
            let nome = dev
                .get("Alias")
                .and_then(|v| <&str>::try_from(v).ok())
                .or_else(|| dev.get("Name").and_then(|v| <&str>::try_from(v).ok()))
                .unwrap_or("")
                .to_string();
            if mac.is_empty() || nome.is_empty() || nome.replace('-', ":") == mac {
                continue;
            }
            let pareado = dev
                .get("Paired")
                .and_then(|v| bool::try_from(v).ok())
                .unwrap_or(false);
            let conectado = dev
                .get("Connected")
                .and_then(|v| bool::try_from(v).ok())
                .unwrap_or(false);
            if conectado {
                est.connected.push(nome.clone());
            }
            if pareado {
                est.paired.push(AparelhoBt {
                    mac,
                    nome,
                    uso: Some(conectado),
                });
            } else {
                est.nearby.push(AparelhoBt {
                    mac,
                    nome,
                    uso: None,
                });
            }
        }
    }
    est.paired.sort_by_key(|a| !a.uso.unwrap_or(false));
    est
}

#[derive(Clone, Default, Serialize)]
struct EstadoWifi {
    enabled: bool,
    ssid: String,
}

/// Consulta `org.freedesktop.NetworkManager` direto pelo barramento do sistema, sem `nmcli`.
fn consultar_wifi_dbus(sys_conn: &DbusConn) -> EstadoWifi {
    let mut est = EstadoWifi::default();
    if let Ok(msg) = sys_conn.call_method(
        Some("org.freedesktop.NetworkManager"),
        "/org/freedesktop/NetworkManager",
        Some("org.freedesktop.DBus.Properties"),
        "Get",
        &("org.freedesktop.NetworkManager", "WirelessEnabled"),
    )
        && let Ok(val) = msg.body().deserialize::<OwnedValue>() {
            est.enabled = bool::try_from(&val).unwrap_or(false);
        }
    if !est.enabled {
        return est;
    }

    if let Ok(msg) = sys_conn.call_method(
        Some("org.freedesktop.NetworkManager"),
        "/org/freedesktop/NetworkManager",
        Some("org.freedesktop.DBus.Properties"),
        "Get",
        &("org.freedesktop.NetworkManager", "ActiveConnections"),
    )
        && let Ok(val) = msg.body().deserialize::<OwnedValue>() {
            let v_ref: &Value<'_> = &val;
            if let Value::Array(arr) = v_ref {
                for item in arr.iter() {
                    let caminho = match item {
                        Value::ObjectPath(p) => p.as_str(),
                        _ => continue,
                    };
                    if let Ok(m2) = sys_conn.call_method(
                        Some("org.freedesktop.NetworkManager"),
                        caminho,
                        Some("org.freedesktop.DBus.Properties"),
                        "GetAll",
                        &("org.freedesktop.NetworkManager.Connection.Active",),
                    )
                        && let Ok(props) = m2.body().deserialize::<HashMap<String, OwnedValue>>() {
                            let tipo = props
                                .get("Type")
                                .and_then(|v| <&str>::try_from(v).ok())
                                .unwrap_or("");
                            if tipo == "802-11-wireless" {
                                est.ssid = props
                                    .get("Id")
                                    .and_then(|v| <&str>::try_from(v).ok())
                                    .unwrap_or("")
                                    .to_string();
                                break;
                            }
                        }
                }
            }
        }
    est
}

fn listar_redes_wifi_nmcli(rescan: bool) -> JsonValue {
    let modo_rescan = if rescan { "auto" } else { "no" };
    let saida = Command::new("nmcli")
        .args([
            "-t",
            "-f",
            "IN-USE,SSID,SIGNAL,SECURITY",
            "dev",
            "wifi",
            "list",
            "--rescan",
            modo_rescan,
        ])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default();

    let mut mapa: HashMap<String, (bool, i32, bool, String)> = HashMap::new();
    for linha in saida.lines() {
        // nmcli -t escapa ':' como '\:'
        let mut campos = Vec::new();
        let mut atual = String::new();
        let mut chars = linha.chars().peekable();
        while let Some(c) = chars.next() {
            if c == '\\' && chars.peek() == Some(&':') {
                atual.push(chars.next().unwrap());
            } else if c == ':' {
                campos.push(std::mem::take(&mut atual));
            } else {
                atual.push(c);
            }
        }
        campos.push(atual);
        if campos.len() < 4 || campos[1].is_empty() {
            continue;
        }
        let uso = campos[0] == "*";
        let ssid = campos[1].clone();
        let sinal = campos[2].parse::<i32>().unwrap_or(0);
        let seg = campos[3].clone();
        let tem_seg = !seg.is_empty() && seg != "--";

        match mapa.get(&ssid) {
            None => {
                mapa.insert(ssid, (uso, sinal, tem_seg, seg));
            }
            Some(&(uso_ant, sinal_ant, _, _)) => {
                if uso || sinal > sinal_ant {
                    mapa.insert(ssid, (uso || uso_ant, sinal, tem_seg, seg));
                }
            }
        }
    }

    let mut lista: Vec<(String, bool, i32, bool, String)> = mapa
        .into_iter()
        .map(|(ssid, (uso, sinal, seguranca, tipo))| (ssid, uso, sinal, seguranca, tipo))
        .collect();
    lista.sort_by_key(|a| (!a.1, -a.2));

    let redes_json: Vec<JsonValue> = lista
        .into_iter()
        .map(|(ssid, uso, sinal, seguranca, tipo)| {
            json!({
                "ssid": ssid,
                "uso": uso,
                "sinal": sinal,
                "seguranca": seguranca,
                "tipo": tipo,
            })
        })
        .collect();

    let salvas_out = Command::new("nmcli")
        .args(["-t", "-f", "NAME", "con", "show"])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default();
    let salvas: Vec<String> = salvas_out
        .lines()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();

    json!({
        "redes": redes_json,
        "salvas": salvas,
    })
}

fn ler_filtro_e_tema() -> (bool, String) {
    let home = env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    let pasta = PathBuf::from(home).join(".cache/hypr-tema");
    let filtro = fs::read_to_string(pasta.join("filtro"))
        .map(|s| {
            let t = s.trim();
            !t.is_empty() && t != "0"
        })
        .unwrap_or(false);
    let tema = fs::read_to_string(pasta.join("tema"))
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|_| "escuro".to_string());
    (filtro, tema)
}

// ---------------- Estado Compartilhado e Timers ----------------

enum ComandoTimer {
    AgendarExpiracao { id: u32, quando: Instant },
    CancelarExpiracao { id: u32 },
    AgendarGravacaoHistorico { quando: Instant },
    EmitirSinalFechada { id: u32, motivo: u32 },
    EmitirEventoHermes { evento: &'static str, payload: JsonValue },
    EmitirSinalAcao {
        dest: Option<String>,
        id: u32,
        chave: String,
        token: String,
    },
}

struct EstadoHermes {
    config: HermesConfig,
    ativas: HashMap<u32, Notificacao>,
    historico: VecDeque<ResumoHistorico>,
    proximo: u32,
    taxa: HashMap<String, (u64, u32)>,
    modos: BTreeSet<String>,
    audio: EstadoAudio,
    /// Último JSON compacto de áudio emitido em `AudioChanged` (evita repetir o mesmo estado).
    audio_json_emitido: String,
    bluetooth: EstadoBluetooth,
    wifi: EstadoWifi,
    tx_timer: Sender<ComandoTimer>,
}

impl EstadoHermes {
    fn carregar_historico(&mut self, caminho: &Path) {
        let Ok(texto) = fs::read_to_string(caminho) else {
            return;
        };
        let Ok(dados) = serde_json::from_str::<JsonValue>(&texto) else {
            return;
        };
        let Some(itens) = dados.get("items").and_then(|v| v.as_array()) else {
            return;
        };
        let maxlen = self.config.history.size.max(1);
        let inicio = itens.len().saturating_sub(maxlen);
        let mut bons = VecDeque::with_capacity(maxlen);
        let mut maior_id = 0u32;

        for x in &itens[inicio..] {
            let Some(obj) = x.as_object() else {
                continue;
            };
            let Some(id_u64) = obj.get("id").and_then(|v| v.as_u64()) else {
                continue;
            };
            let Some(summary) = obj.get("summary").and_then(|v| v.as_str()) else {
                continue;
            };
            let id = id_u64 as u32;
            maior_id = maior_id.max(id);
            let app = texto_puro(obj.get("app").and_then(|v| v.as_str()).unwrap_or(""), 120);
            let body = texto_puro(obj.get("body").and_then(|v| v.as_str()).unwrap_or(""), 2000);
            let urg_raw = obj
                .get("urgency")
                .and_then(|v| v.as_str())
                .unwrap_or("normal");
            let urgency = if matches!(urg_raw, "low" | "normal" | "critical") {
                urg_raw.to_string()
            } else {
                "normal".to_string()
            };
            let category = texto_puro(
                obj.get("category").and_then(|v| v.as_str()).unwrap_or(""),
                64,
            );
            let icon = texto_puro(obj.get("icon").and_then(|v| v.as_str()).unwrap_or(""), 500);
            let desktop_entry = texto_puro(
                obj.get("desktop_entry")
                    .and_then(|v| v.as_str())
                    .unwrap_or(""),
                128,
            );
            let time = obj.get("time").and_then(|v| v.as_f64()).unwrap_or(0.0);
            bons.push_back(ResumoHistorico {
                id,
                app,
                summary: texto_puro(summary, 500),
                body,
                urgency,
                category,
                icon,
                desktop_entry,
                time,
                actions: Vec::new(),
            });
        }
        self.historico = bons;
        let prox = dados
            .get("next")
            .and_then(|v| v.as_u64())
            .map(|n| n as u32)
            .unwrap_or(1);
        self.proximo = self.proximo.max(prox).max(maior_id.saturating_add(1));
    }

    fn gravar_historico_disco(&self) {
        let caminho = caminho_historico_hermes();
        if let Some(pasta) = caminho.parent() {
            let _ = fs::DirBuilder::new().recursive(true).mode(0o700).create(pasta);
        }
        let tmp = caminho.with_extension("json.tmp");
        let Ok(mut f) = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&tmp)
        else {
            return;
        };
        let payload = json!({
            "next": self.proximo,
            "items": self.historico,
        });
        if let Ok(bytes) = serde_json::to_vec(&payload)
            && f.write_all(&bytes).is_ok() && f.flush().is_ok() {
                let _ = fs::rename(&tmp, &caminho);
            }
    }

    fn guardar_no_historico(&mut self, n: &Notificacao) {
        let maxlen = self.config.history.size.max(1);
        self.historico.retain(|x| x.id != n.id);
        if self.config.history.size > 0 {
            while self.historico.len() >= maxlen {
                self.historico.pop_front();
            }
            self.historico.push_back(n.resumo());
        }
        let _ = self.tx_timer.send(ComandoTimer::AgendarGravacaoHistorico {
            quando: Instant::now() + Duration::from_millis(1500),
        });
    }

    fn historico_mako(&self) -> Vec<JsonValue> {
        self.historico
            .iter()
            .rev()
            .map(|x| {
                let mut acoes_map = serde_json::Map::new();
                for [k, t] in &x.actions {
                    acoes_map.insert(k.clone(), JsonValue::String(t.clone()));
                }
                json!({
                    "id": x.id,
                    "app_name": x.app,
                    "app_icon": if x.icon.is_empty() { JsonValue::Null } else { JsonValue::String(x.icon.clone()) },
                    "category": if x.category.is_empty() { JsonValue::Null } else { JsonValue::String(x.category.clone()) },
                    "desktop_entry": if x.desktop_entry.is_empty() { JsonValue::Null } else { JsonValue::String(x.desktop_entry.clone()) },
                    "summary": x.summary,
                    "body": x.body,
                    "urgency": x.urgency,
                    "time": x.time,
                    "actions": acoes_map,
                })
            })
            .collect()
    }
}

fn emitir_sinal_fechada(conn: &DbusConn, id: u32, motivo: u32) {
    let _ = conn.emit_signal(
        None::<&str>,
        CAMINHO_NOTIF,
        NOME_NOTIF,
        "NotificationClosed",
        &(id, motivo),
    );
}

fn emitir_evento_hermes(conn: &DbusConn, evento: &str, payload: &JsonValue) {
    let texto = serde_json::to_string(payload).unwrap_or_else(|_| "{}".to_string());
    let _ = conn.emit_signal(
        None::<&str>,
        CAMINHO_NOTIF,
        NOME_HERMES,
        "NotificationEvent",
        &(evento, texto.as_str()),
    );
}

// ---------------- Interfaces D-Bus (`org.freedesktop.Notifications` e `com.aero.Hermes`) ----------------

struct ServicoNotificacoes {
    estado: Arc<Mutex<EstadoHermes>>,
}

#[interface(name = "org.freedesktop.Notifications")]
impl ServicoNotificacoes {
    #[zbus(name = "GetCapabilities")]
    fn get_capabilities(&self) -> Vec<String> {
        vec![
            "body".to_string(),
            "body-markup".to_string(),
            "actions".to_string(),
            "icon-static".to_string(),
            "persistence".to_string(),
            "x-canonical-private-synchronous".to_string(),
            "x-dunst-stack-tag".to_string(),
            "x-aero-wave".to_string(),
        ]
    }

    #[zbus(name = "GetServerInformation")]
    fn get_server_information(&self) -> (String, String, String, String) {
        (
            "Hermes".to_string(),
            "Aero OS".to_string(),
            "1.0".to_string(),
            "1.2".to_string(),
        )
    }

    #[allow(clippy::too_many_arguments)]
    #[zbus(name = "Notify")]
    fn notify(
        &self,
        #[zbus(header)] hdr: Header<'_>,
        app_name: String,
        replaces_id: u32,
        app_icon: String,
        summary: String,
        body: String,
        actions: Vec<String>,
        hints: HashMap<String, OwnedValue>,
        expire_timeout: i32,
    ) -> u32 {
        let remetente = hdr
            .sender()
            .map(|s| s.as_str().to_string())
            .unwrap_or_default();

        {
            let mut est = self.estado.lock().unwrap();
            let agora_s = agora_segundos() as u64;
            let (seg, conta_ant) = est
                .taxa
                .get(&remetente)
                .copied()
                .unwrap_or((agora_s, 0));
            let conta = if seg == agora_s { conta_ant + 1 } else { 1 };
            if est.taxa.len() > 500 {
                est.taxa.clear();
            }
            est.taxa.insert(remetente.clone(), (agora_s, conta));
            if conta > LIMITE_POR_SEG {
                return 0;
            }

            let existente = if replaces_id != 0 {
                est.ativas.get(&replaces_id).cloned()
            } else {
                None
            };
            let mut nova = match existente {
                Some(ref velha) => {
                    !velha.remetente.is_empty()
                        && !remetente.is_empty()
                        && velha.remetente != remetente
                }
                None => true,
            };

            let id_prov = if nova {
                let i = est.proximo;
                est.proximo = if est.proximo < u32::MAX - 1 {
                    est.proximo + 1
                } else {
                    1
                };
                i
            } else {
                replaces_id
            };

            let app_limpo = {
                let s = texto_puro(&app_name, 120);
                if s.is_empty() {
                    "Unknown".to_string()
                } else {
                    s
                }
            };
            let mut icone_limpo = texto_puro(&app_icon, 500);
            let titulo_limpo = texto_puro(&summary, 500);
            let corpo_limpo = limpar_markup(&body, 2000);

            let mut pares_acoes = Vec::new();
            let max_acoes = actions.len().min(20);
            let mut idx = 0;
            while idx + 1 < max_acoes {
                pares_acoes.push((
                    texto_puro(&actions[idx], 64),
                    texto_puro(&actions[idx + 1], 64),
                ));
                idx += 2;
            }

            let mut urgencia = 1u8;
            let mut categoria = String::new();
            let mut entrada = String::new();
            let mut valor = None;
            let mut imagem_temp = None;
            let mut etiqueta = None;
            let mut transitoria = false;
            let mut residente = false;
            let mut som = None;

            for (chave, val_owned) in &hints {
                let v: &Value<'_> = val_owned;
                match chave.as_str() {
                    "urgency" => {
                        if let Some(u) = extrair_int_hint(v) {
                            urgencia = if (0..=2).contains(&u) { u as u8 } else { 1 };
                        }
                    }
                    "category" => {
                        if let Some(s) = extrair_str_hint(v) {
                            categoria = texto_puro(s, 64);
                        }
                    }
                    "desktop-entry" => {
                        if let Some(s) = extrair_str_hint(v) {
                            entrada = texto_puro(s, 128);
                        }
                    }
                    "value" => {
                        if let Some(val_i) = extrair_int_hint(v) {
                            valor = Some(val_i.clamp(0, 100));
                        }
                    }
                    "image-path" | "image_path" => {
                        if let Some(s) = extrair_str_hint(v) {
                            icone_limpo = texto_puro(s, 500);
                        }
                    }
                    "image-data" | "image_data" | "icon_data" => {
                        if let Some(arq) = salvar_imagem_hint(id_prov, v) {
                            imagem_temp = Some(arq);
                        }
                    }
                    "x-canonical-private-synchronous" | "x-dunst-stack-tag" => {
                        if let Some(s) = extrair_str_hint(v) {
                            let t = texto_puro(s, 128);
                            if !t.is_empty() {
                                etiqueta = Some(t);
                            }
                        }
                    }
                    "transient" => {
                        if let Some(b) = extrair_bool_hint(v) {
                            transitoria = b;
                        }
                    }
                    "resident" => {
                        if let Some(b) = extrair_bool_hint(v) {
                            residente = b;
                        }
                    }
                    "sound-file" => {
                        if let Some(s) = extrair_str_hint(v) {
                            som = Some(texto_puro(s, 500));
                        }
                    }
                    _ => {}
                }
            }

            let tempo_padrao = match urgencia {
                0 => est.config.timeout.low,
                2 => est.config.timeout.critical,
                _ => est.config.timeout.normal,
            };
            let timeout = if expire_timeout < 0 {
                tempo_padrao
            } else {
                expire_timeout
            };

            let mut n = Notificacao {
                id: id_prov,
                app: app_limpo,
                icone: icone_limpo,
                imagem_temp,
                titulo: titulo_limpo,
                corpo: corpo_limpo,
                acoes: pares_acoes,
                urgencia,
                categoria,
                entrada,
                valor,
                etiqueta,
                transitoria,
                residente,
                som,
                timeout,
                remetente,
                criada: agora_segundos(),
                escondida: false,
                onda: true,
                guardar: !transitoria,
                pendente: false,
            };

            aplicar_regras(&mut n, &est.config.rules);

            // Substituição por tag (x-canonical-private-synchronous / x-dunst-stack-tag) do mesmo app
            if nova {
                if let Some(ref tag) = n.etiqueta {
                    let achada = est
                        .ativas
                        .values()
                        .find(|v| v.etiqueta.as_ref() == Some(tag) && v.app == n.app)
                        .map(|v| v.id);
                    if let Some(velha_id) = achada {
                        if let Some(velha) = est.ativas.remove(&velha_id) {
                            velha.limpar_temp();
                        }
                        let _ = est
                            .tx_timer
                            .send(ComandoTimer::CancelarExpiracao { id: velha_id });
                        n.id = velha_id;
                        nova = false;
                    }
                }
            } else if let Some(velha) = est.ativas.get(&n.id)
                && velha.imagem_temp != n.imagem_temp {
                    velha.limpar_temp();
                }

            if est.modos.contains("dnd")
                && !(n.urgencia == 2 && est.config.dnd.allow_critical)
            {
                n.escondida = true;
                n.onda = false;
            }

            if n.guardar && !n.transitoria {
                est.guardar_no_historico(&n);
            }

            let _ = est
                .tx_timer
                .send(ComandoTimer::CancelarExpiracao { id: n.id });

            if n.escondida {
                n.limpar_temp();
                est.ativas.remove(&n.id);
                let _ = est.tx_timer.send(ComandoTimer::EmitirSinalFechada {
                    id: n.id,
                    motivo: DISPENSADA,
                });
                n.id
            } else {
                if n.timeout > 0 {
                    let quando = Instant::now() + Duration::from_millis(n.timeout as u64);
                    let _ = est
                        .tx_timer
                        .send(ComandoTimer::AgendarExpiracao { id: n.id, quando });
                }
                let ui_json = n.para_ui_json();
                let id_ret = n.id;
                est.ativas.insert(id_ret, n);
                let _ = est.tx_timer.send(ComandoTimer::EmitirEventoHermes {
                    evento: if nova { "show" } else { "update" },
                    payload: ui_json,
                });
                id_ret
            }
        }
    }

    #[zbus(name = "CloseNotification")]
    fn close_notification(&self, #[zbus(header)] hdr: Header<'_>, id: u32) {
        let remetente = hdr
            .sender()
            .map(|s| s.as_str().to_string())
            .unwrap_or_default();
        let mut est = self.estado.lock().unwrap();
        let pode = est
            .ativas
            .get(&id)
            .map(|n| {
                n.remetente.is_empty() || remetente.is_empty() || n.remetente == remetente
            })
            .unwrap_or(false);
        if !pode {
            return;
        }
        let _ = est
            .tx_timer
            .send(ComandoTimer::CancelarExpiracao { id });
        if let Some(n) = est.ativas.remove(&id) {
            let ui_json = n.para_ui_json();
            n.limpar_temp();
            let _ = est.tx_timer.send(ComandoTimer::EmitirEventoHermes {
                evento: "close",
                payload: ui_json,
            });
            let _ = est.tx_timer.send(ComandoTimer::EmitirSinalFechada {
                id,
                motivo: PEDIDO,
            });
        }
    }

    #[zbus(signal, name = "NotificationClosed")]
    async fn notification_closed(
        emitter: &SignalEmitter<'_>,
        id: u32,
        reason: u32,
    ) -> zbus::Result<()>;

    #[zbus(signal, name = "ActionInvoked")]
    async fn action_invoked(
        emitter: &SignalEmitter<'_>,
        id: u32,
        action_key: &str,
    ) -> zbus::Result<()>;

    #[zbus(signal, name = "ActivationToken")]
    async fn activation_token(
        emitter: &SignalEmitter<'_>,
        id: u32,
        token: &str,
    ) -> zbus::Result<()>;
}

struct ServicoControleHermes {
    estado: Arc<Mutex<EstadoHermes>>,
    /// Conexão da sessão (para emitir os sinais de estado a partir das threads dos setters).
    sessao: DbusConn,
    /// Conexão do sistema (NetworkManager, BlueZ); `None` sem barramento do sistema.
    sistema: Option<DbusConn>,
}

/// Emite `AudioChanged(estado_json)` em `com.aero.Hermes`.
fn emitir_audio_changed(conn: &DbusConn, estado: &JsonValue) {
    let _ = conn.emit_signal(
        None::<&str>,
        CAMINHO_NOTIF,
        NOME_HERMES,
        "AudioChanged",
        &(estado.to_string(),),
    );
}

/// Emite `WifiChanged` / `BluetoothChanged` / `DndChanged` em `com.aero.Hermes`.
fn emitir_estado(conn: &DbusConn, sinal: &str, payload: &JsonValue) {
    let _ = conn.emit_signal(
        None::<&str>,
        CAMINHO_NOTIF,
        NOME_HERMES,
        sinal,
        &(payload.to_string(),),
    );
}

fn emitir_dnd_changed(conn: &DbusConn, ligado: bool) {
    let _ = conn.emit_signal(None::<&str>, CAMINHO_NOTIF, NOME_HERMES, "DndChanged", &(ligado,));
}

/// Resposta padrão de erro dos setters: `{"error": "...", ...estado atual}`.
fn com_erro(mut estado: JsonValue, erro: Option<String>) -> String {
    if let (Some(e), Some(obj)) = (erro, estado.as_object_mut()) {
        obj.insert("error".to_string(), JsonValue::String(e));
    }
    estado.to_string()
}

fn pendente(mut estado: JsonValue) -> String {
    if let Some(obj) = estado.as_object_mut() {
        obj.insert("pending".to_string(), JsonValue::Bool(true));
    }
    estado.to_string()
}

impl ServicoControleHermes {
    /// Relê o áudio na fonte (pactl), guarda no estado e emite `AudioChanged` com o estado novo.
    fn releitura_audio(estado: &Arc<Mutex<EstadoHermes>>, sessao: &DbusConn) -> JsonValue {
        // o wpctl volta antes de o pipewire-pulse refletir a mudança: um respiro curto antes de reler
        thread::sleep(Duration::from_millis(60));
        let novo = coletar_audio_atual();
        let js = novo.compacto();
        {
            let mut est = estado.lock().unwrap();
            est.audio = novo;
            est.audio_json_emitido = js.to_string();
        }
        emitir_audio_changed(sessao, &js);
        js
    }

    /// Executa um setter de áudio e devolve o estado real que ficou (JSON).
    fn acao_audio(&self, acao: impl FnOnce() -> Result<(), String> + Send + 'static) -> String {
        let estado = Arc::clone(&self.estado);
        let sessao = self.sessao.clone();
        match backends::com_prazo(move || {
            let r = acao();
            let js = Self::releitura_audio(&estado, &sessao);
            com_erro(js, r.err())
        }) {
            Some(resp) => resp,
            None => pendente(self.estado.lock().unwrap().audio.compacto()),
        }
    }

    fn wifi_json(&self) -> JsonValue {
        match &self.sistema {
            Some(c) => backends::wifi_estado(c),
            None => json!({"enabled": false, "connected": false, "ssid": "", "strength": 0, "error": "no system bus"}),
        }
    }

    fn bluetooth_json(&self) -> JsonValue {
        match &self.sistema {
            Some(c) => backends::bluetooth_estado(c),
            None => json!({"powered": false, "devices": [], "error": "no system bus"}),
        }
    }

    /// Executa um setter de Wi-Fi ou Bluetooth numa thread, relê o estado, emite o sinal e responde
    /// (ou responde `pending` se passou do prazo; a thread emite o sinal quando terminar).
    fn acao_sistema(
        &self,
        sinal: &'static str,
        ler: fn(&DbusConn) -> JsonValue,
        acao: impl FnOnce(&DbusConn) -> Result<(), String> + Send + 'static,
    ) -> String {
        let Some(sistema) = self.sistema.clone() else {
            return com_erro(json!({}), Some("no system bus".into()));
        };
        let sessao = self.sessao.clone();
        let sistema2 = sistema.clone();
        match backends::com_prazo(move || {
            let r = acao(&sistema2);
            let js = ler(&sistema2);
            emitir_estado(&sessao, sinal, &js);
            com_erro(js, r.err())
        }) {
            Some(resp) => resp,
            None => pendente(ler(&sistema)),
        }
    }

    fn definir_dnd(&self, ligado: bool) -> bool {
        let mudou;
        {
            let mut est = self.estado.lock().unwrap();
            mudou = if ligado {
                est.modos.insert("dnd".to_string())
            } else {
                est.modos.remove("dnd")
            };
            if mudou {
                let lista_modos: Vec<String> = est.modos.iter().cloned().collect();
                let _ = est.tx_timer.send(ComandoTimer::EmitirEventoHermes {
                    evento: "modes",
                    payload: json!(lista_modos),
                });
            }
        }
        if mudou {
            emitir_dnd_changed(&self.sessao, ligado);
            avisar_waybar_dnd();
        }
        ligado
    }
}

#[interface(name = "com.aero.Hermes")]
impl ServicoControleHermes {
    #[zbus(name = "History")]
    fn history(&self) -> String {
        let est = self.estado.lock().unwrap();
        serde_json::to_string(&est.historico_mako()).unwrap_or_else(|_| "[]".to_string())
    }

    #[zbus(name = "Modes")]
    fn modes(&self) -> Vec<String> {
        let est = self.estado.lock().unwrap();
        est.modos.iter().cloned().collect()
    }

    #[zbus(name = "SetModes")]
    fn set_modes(&self, modes: Vec<String>) {
        let re = Regex::new(r"^[\w-]{1,32}$").unwrap();
        let dnd_antes = self.estado.lock().unwrap().modos.contains("dnd");
        {
            let mut est = self.estado.lock().unwrap();
            est.modos = modes
                .into_iter()
                .take(20)
                .filter(|m| re.is_match(m))
                .collect();
            let lista_modos: Vec<String> = est.modos.iter().cloned().collect();
            let _ = est.tx_timer.send(ComandoTimer::EmitirEventoHermes {
                evento: "modes",
                payload: json!(lista_modos),
            });
        }
        let dnd_depois = self.estado.lock().unwrap().modos.contains("dnd");
        if dnd_antes != dnd_depois {
            emitir_dnd_changed(&self.sessao, dnd_depois);
        }
        avisar_waybar_dnd();
    }

    #[zbus(name = "SetMode")]
    fn set_mode(&self, nome: String, ligado: bool) {
        let re = Regex::new(r"^[\w-]{1,32}$").unwrap();
        if !re.is_match(&nome) {
            return;
        }
        if nome == "dnd" {
            self.definir_dnd(ligado);
            return;
        }
        {
            let mut est = self.estado.lock().unwrap();
            if ligado {
                est.modos.insert(nome);
            } else {
                est.modos.remove(&nome);
            }
            let lista_modos: Vec<String> = est.modos.iter().cloned().collect();
            let _ = est.tx_timer.send(ComandoTimer::EmitirEventoHermes {
                evento: "modes",
                payload: json!(lista_modos),
            });
        }
        avisar_waybar_dnd();
    }

    #[zbus(name = "DismissAll")]
    fn dismiss_all(&self) {
        let mut est = self.estado.lock().unwrap();
        let ids: Vec<u32> = est.ativas.keys().copied().collect();
        for id in ids {
            let _ = est
                .tx_timer
                .send(ComandoTimer::CancelarExpiracao { id });
            if let Some(n) = est.ativas.remove(&id) {
                let ui_json = n.para_ui_json();
                n.limpar_temp();
                let _ = est.tx_timer.send(ComandoTimer::EmitirEventoHermes {
                    evento: "close",
                    payload: ui_json,
                });
                let _ = est.tx_timer.send(ComandoTimer::EmitirSinalFechada {
                    id,
                    motivo: DISPENSADA,
                });
            }
        }
    }

    #[zbus(name = "Dismiss")]
    fn dismiss(&self, id: u32) {
        let mut est = self.estado.lock().unwrap();
        let _ = est
            .tx_timer
            .send(ComandoTimer::CancelarExpiracao { id });
        if let Some(n) = est.ativas.remove(&id) {
            let ui_json = n.para_ui_json();
            n.limpar_temp();
            let _ = est.tx_timer.send(ComandoTimer::EmitirEventoHermes {
                evento: "close",
                payload: ui_json,
            });
            let _ = est.tx_timer.send(ComandoTimer::EmitirSinalFechada {
                id,
                motivo: DISPENSADA,
            });
        }
    }

    #[zbus(name = "InvokeAction")]
    fn invoke_action(&self, id: u32, chave: String, token: String) {
        let fechar_agora = {
            let est = self.estado.lock().unwrap();
            let Some(n) = est.ativas.get(&id) else {
                return;
            };
            let valida = chave == "default" || n.acoes.iter().any(|(k, _)| k == &chave);
            if !valida {
                return;
            }
            let dest = if n.remetente.is_empty() {
                None
            } else {
                Some(n.remetente.clone())
            };
            let _ = est.tx_timer.send(ComandoTimer::EmitirSinalAcao {
                dest,
                id,
                chave,
                token,
            });
            !n.residente
        };

        if fechar_agora {
            self.dismiss(id);
        }
    }

    #[zbus(name = "Hold")]
    fn hold(&self, id: u32) {
        let mut est = self.estado.lock().unwrap();
        if let Some(n) = est.ativas.get_mut(&id) {
            n.pendente = true;
            let _ = est
                .tx_timer
                .send(ComandoTimer::CancelarExpiracao { id });
        }
    }

    #[zbus(name = "Arm")]
    fn arm(&self, id: u32) {
        let mut est = self.estado.lock().unwrap();
        if let Some(n) = est.ativas.get_mut(&id)
            && n.pendente {
                n.pendente = false;
                if n.timeout > 0 {
                    let quando = Instant::now() + Duration::from_millis(n.timeout as u64);
                    let _ = est
                        .tx_timer
                        .send(ComandoTimer::AgendarExpiracao { id, quando });
                }
            }
    }

    #[zbus(name = "RemoveHistory")]
    fn remove_history(&self, id: u32) {
        let mut est = self.estado.lock().unwrap();
        est.historico.retain(|x| x.id != id);
        let _ = est.tx_timer.send(ComandoTimer::AgendarGravacaoHistorico {
            quando: Instant::now() + Duration::from_millis(1500),
        });
    }

    #[zbus(name = "ClearHistory")]
    fn clear_history(&self) {
        let mut est = self.estado.lock().unwrap();
        est.historico.clear();
        let _ = est.tx_timer.send(ComandoTimer::AgendarGravacaoHistorico {
            quando: Instant::now() + Duration::from_millis(1500),
        });
    }

    #[zbus(name = "Reload")]
    fn reload(&self) {
        let nova_cfg = ler_config();
        let mut est = self.estado.lock().unwrap();
        let maxlen = nova_cfg.history.size.max(1);
        while est.historico.len() > maxlen {
            est.historico.pop_front();
        }
        let cfg_json = nova_cfg.para_json();
        est.config = nova_cfg;
        let _ = est.tx_timer.send(ComandoTimer::EmitirEventoHermes {
            evento: "config",
            payload: cfg_json,
        });
    }

    #[zbus(name = "LatestId")]
    fn latest_id(&self) -> u32 {
        let est = self.estado.lock().unwrap();
        est.historico.iter().map(|x| x.id).max().unwrap_or(0)
    }

    #[zbus(name = "GetConfig")]
    fn get_config(&self) -> String {
        let est = self.estado.lock().unwrap();
        serde_json::to_string(&est.config.para_json()).unwrap_or_else(|_| "{}".to_string())
    }

    #[zbus(name = "GetActive")]
    fn get_active(&self) -> String {
        let est = self.estado.lock().unwrap();
        let mut lista: Vec<&Notificacao> = est.ativas.values().collect();
        lista.sort_by_key(|n| n.id);
        let jsons: Vec<JsonValue> = lista.into_iter().map(|n| n.para_ui_json()).collect();
        serde_json::to_string(&jsons).unwrap_or_else(|_| "[]".to_string())
    }

    #[zbus(name = "GetAudio")]
    fn get_audio(&self) -> String {
        let est = self.estado.lock().unwrap();
        serde_json::to_string(&est.audio).unwrap_or_else(|_| "{\"sinks\":[],\"sources\":[]}".to_string())
    }

    #[zbus(name = "GetBluetoothDevices")]
    fn get_bluetooth_devices(&self) -> String {
        let est = self.estado.lock().unwrap();
        serde_json::to_string(&est.bluetooth).unwrap_or_else(|_| "{}".to_string())
    }

    #[zbus(name = "GetWifiNetworks")]
    fn get_wifi_networks(&self, rescan: bool) -> String {
        serde_json::to_string(&listar_redes_wifi_nmcli(rescan))
            .unwrap_or_else(|_| "{\"redes\":[],\"salvas\":[]}".to_string())
    }

    #[zbus(name = "GetHestiaState")]
    fn get_hestia_state(&self) -> String {
        let (filtro, tema) = ler_filtro_e_tema();
        let est = self.estado.lock().unwrap();
        let modos: Vec<String> = est.modos.iter().cloned().collect();
        let payload = json!({
            "sinks": est.audio.sinks,
            "sources": est.audio.sources,
            "wifi": [est.wifi.enabled, est.wifi.ssid],
            "bluetooth": [est.bluetooth.powered, est.bluetooth.connected],
            "bt_paired": est.bluetooth.paired,
            "bt_nearby": est.bluetooth.nearby,
            "modes": modos,
            "filter": filtro,
            "theme": tema,
        });
        serde_json::to_string(&payload).unwrap_or_else(|_| "{}".to_string())
    }

    // ---------- Áudio (PipeWire) com leitura de volta ----------

    /// Saídas de som: `[{id,index,name,pretty_name,is_default,volume,muted}]` (`id` = objeto do PipeWire).
    #[zbus(name = "ListSinks")]
    fn list_sinks(&self) -> String {
        let est = self.estado.lock().unwrap();
        est.audio.compacto()["sinks"].to_string()
    }

    /// Entradas de som (microfones), mesmo formato de `ListSinks`.
    #[zbus(name = "ListSources")]
    fn list_sources(&self) -> String {
        let est = self.estado.lock().unwrap();
        est.audio.compacto()["sources"].to_string()
    }

    #[zbus(name = "SetDefaultSink")]
    fn set_default_sink(&self, id: u32) -> String {
        self.acao_audio(move || backends::audio_definir_padrao(id))
    }

    #[zbus(name = "SetDefaultSource")]
    fn set_default_source(&self, id: u32) -> String {
        self.acao_audio(move || backends::audio_definir_padrao(id))
    }

    /// Volume em % (0..=150) de uma saída ou entrada pelo `id` do PipeWire.
    #[zbus(name = "SetVolume")]
    fn set_volume(&self, id: u32, pct: u32) -> String {
        self.acao_audio(move || backends::audio_definir_volume(id, pct))
    }

    #[zbus(name = "SetMute")]
    fn set_mute(&self, id: u32, muted: bool) -> String {
        self.acao_audio(move || backends::audio_definir_mudo(id, muted))
    }

    // ---------- Wi-Fi (NetworkManager) ----------

    /// `{"enabled","connected","ssid","strength","state"}` lido agora do NetworkManager.
    #[zbus(name = "GetWifi")]
    fn get_wifi(&self) -> String {
        self.wifi_json().to_string()
    }

    #[zbus(name = "SetWifiEnabled")]
    fn set_wifi_enabled(&self, enabled: bool) -> String {
        self.acao_sistema("WifiChanged", backends::wifi_estado, move |c| {
            backends::wifi_definir_ligado(c, enabled)?;
            // o NM leva um instante para desligar/ligar o rádio; esperamos refletir antes de reler
            for _ in 0..10 {
                thread::sleep(Duration::from_millis(100));
                if backends::wifi_estado(c)["enabled"].as_bool() == Some(enabled) {
                    break;
                }
            }
            Ok(())
        })
    }

    /// Redes visíveis: `[{"ssid","strength","secured","known","in_use"}]`; `rescan` pede uma varredura nova.
    #[zbus(name = "ListNetworks")]
    fn list_networks(&self, rescan: bool) -> String {
        let Some(c) = &self.sistema else {
            return "[]".to_string();
        };
        match backends::wifi_listar(c, rescan) {
            Ok(lista) => JsonValue::Array(lista).to_string(),
            Err(e) => json!({"error": e}).to_string(),
        }
    }

    /// Conecta ao `ssid` (senha vazia = usar a conexão salva). A resposta traz `pending` enquanto o NM
    /// negocia; o `WifiChanged` final confirma `connected`.
    #[zbus(name = "WifiConnect")]
    fn wifi_connect(&self, ssid: String, password: String) -> String {
        self.acao_sistema("WifiChanged", backends::wifi_estado, move |c| {
            backends::wifi_conectar(c, &ssid, &password)
        })
    }

    #[zbus(name = "WifiDisconnect")]
    fn wifi_disconnect(&self) -> String {
        self.acao_sistema("WifiChanged", backends::wifi_estado, backends::wifi_desconectar)
    }

    // ---------- Bluetooth (BlueZ) ----------

    /// `{"powered","adapter","devices":[{"addr","name","connected","paired","trusted","battery"}]}`.
    #[zbus(name = "GetBluetooth")]
    fn get_bluetooth(&self) -> String {
        self.bluetooth_json().to_string()
    }

    #[zbus(name = "SetBluetoothPowered")]
    fn set_bluetooth_powered(&self, powered: bool) -> String {
        self.acao_sistema("BluetoothChanged", backends::bluetooth_estado, move |c| {
            backends::bluetooth_definir_ligado(c, powered)
        })
    }

    #[zbus(name = "BluetoothConnect")]
    fn bluetooth_connect(&self, addr: String) -> String {
        self.acao_sistema("BluetoothChanged", backends::bluetooth_estado, move |c| {
            backends::bluetooth_conectar(c, &addr)
        })
    }

    #[zbus(name = "BluetoothDisconnect")]
    fn bluetooth_disconnect(&self, addr: String) -> String {
        self.acao_sistema("BluetoothChanged", backends::bluetooth_estado, move |c| {
            backends::bluetooth_desconectar(c, &addr)
        })
    }

    // ---------- Não perturbe ----------

    #[zbus(name = "GetDnd")]
    fn get_dnd(&self) -> bool {
        self.estado.lock().unwrap().modos.contains("dnd")
    }

    /// Liga/desliga o modo `dnd` e devolve o estado que ficou (emite `DndChanged` e `NotificationEvent("modes")`).
    #[zbus(name = "SetDnd")]
    fn set_dnd(&self, enabled: bool) -> bool {
        self.definir_dnd(enabled);
        self.estado.lock().unwrap().modos.contains("dnd")
    }

    #[zbus(signal, name = "NotificationEvent")]
    async fn notification_event(
        emitter: &SignalEmitter<'_>,
        event: &str,
        payload_json: &str,
    ) -> zbus::Result<()>;

    /// Estado completo do áudio (JSON de `ListSinks`/`ListSources` juntos: `{"sinks":[..],"sources":[..]}`).
    #[zbus(signal, name = "AudioChanged")]
    async fn audio_changed(emitter: &SignalEmitter<'_>, state_json: &str) -> zbus::Result<()>;

    /// Mesmo JSON de `GetWifi`.
    #[zbus(signal, name = "WifiChanged")]
    async fn wifi_changed(emitter: &SignalEmitter<'_>, state_json: &str) -> zbus::Result<()>;

    /// Mesmo JSON de `GetBluetooth`.
    #[zbus(signal, name = "BluetoothChanged")]
    async fn bluetooth_changed(emitter: &SignalEmitter<'_>, state_json: &str) -> zbus::Result<()>;

    #[zbus(signal, name = "DndChanged")]
    async fn dnd_changed(emitter: &SignalEmitter<'_>, enabled: bool) -> zbus::Result<()>;
}

// ---------------- Threads de Timers e Monitoramento por Eventos (0% CPU parado) ----------------

fn laco_timers(
    rx: Receiver<ComandoTimer>,
    estado: Arc<Mutex<EstadoHermes>>,
    conn_sessao: DbusConn,
) {
    let mut expiracoes: HashMap<u32, Instant> = HashMap::new();
    let mut gravar_em: Option<Instant> = None;

    loop {
        let agora = Instant::now();

        // 1. Dispara expirações vencidas
        let vencidos: Vec<u32> = expiracoes
            .iter()
            .filter_map(|(&id, &quando)| if quando <= agora { Some(id) } else { None })
            .collect();
        for id in vencidos {
            expiracoes.remove(&id);
            let removida = {
                let mut est = estado.lock().unwrap();
                est.ativas.remove(&id)
            };
            if let Some(n) = removida {
                let ui_json = n.para_ui_json();
                n.limpar_temp();
                emitir_evento_hermes(&conn_sessao, "close", &ui_json);
                emitir_sinal_fechada(&conn_sessao, id, EXPIROU);
            }
        }

        // 2. Dispara gravação de histórico se vencida
        if let Some(quando) = gravar_em
            && quando <= agora {
                gravar_em = None;
                let est = estado.lock().unwrap();
                est.gravar_historico_disco();
            }

        // 3. Calcula o próximo instante de despertar (ou dorme indefinidamente se não houver nada)
        let proximo_instante = expiracoes
            .values()
            .copied()
            .chain(gravar_em)
            .min();

        let cmd_opt = match proximo_instante {
            None => rx.recv().ok(),
            Some(alvo) => {
                let espera = alvo.saturating_duration_since(Instant::now());
                rx.recv_timeout(espera).ok()
            }
        };

        if let Some(cmd) = cmd_opt {
            match cmd {
                ComandoTimer::AgendarExpiracao { id, quando } => {
                    expiracoes.insert(id, quando);
                }
                ComandoTimer::CancelarExpiracao { id } => {
                    expiracoes.remove(&id);
                }
                ComandoTimer::AgendarGravacaoHistorico { quando } => {
                    if gravar_em.is_none() {
                        gravar_em = Some(quando);
                    }
                }
                ComandoTimer::EmitirSinalFechada { id, motivo } => {
                    emitir_sinal_fechada(&conn_sessao, id, motivo);
                }
                ComandoTimer::EmitirEventoHermes { evento, payload } => {
                    emitir_evento_hermes(&conn_sessao, evento, &payload);
                }
                ComandoTimer::EmitirSinalAcao {
                    dest,
                    id,
                    chave,
                    token,
                } => {
                    let alvo = dest.as_deref().filter(|s| !s.is_empty());
                    if !token.is_empty() {
                        let _ = conn_sessao.emit_signal(
                            alvo,
                            CAMINHO_NOTIF,
                            NOME_NOTIF,
                            "ActivationToken",
                            &(id, token.as_str()),
                        );
                    }
                    let _ = conn_sessao.emit_signal(
                        alvo,
                        CAMINHO_NOTIF,
                        NOME_NOTIF,
                        "ActionInvoked",
                        &(id, chave.as_str()),
                    );
                }
            }
        }
    }
}

/// Monitora eventos do PipeWire/PulseAudio via `pactl subscribe` (bloqueante, zero CPU parado).
fn laco_audio_eventos(estado: Arc<Mutex<EstadoHermes>>, conn_sessao: DbusConn) {
    loop {
        let mut cmd = Command::new("pactl");
        cmd.arg("subscribe")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
                Ok(())
            });
        }
        if let Ok(mut child) = cmd.spawn() {
            if let Some(stdout) = child.stdout.take() {
                let reader = BufReader::new(stdout);
                let mut ultimo_disparo = Instant::now() - Duration::from_secs(1);
                for linha in reader.lines().map_while(Result::ok) {
                    if (linha.contains("sink")
                        || linha.contains("source")
                        || linha.contains("server"))
                        && !linha.contains("monitor")
                        && ultimo_disparo.elapsed() >= Duration::from_millis(40) {
                            ultimo_disparo = Instant::now();
                            let novo = coletar_audio_atual();
                            let js = novo.compacto();
                            let txt = js.to_string();
                            let mudou = {
                                let mut est = estado.lock().unwrap();
                                est.audio = novo;
                                let mudou = est.audio_json_emitido != txt;
                                est.audio_json_emitido = txt;
                                mudou
                            };
                            if mudou {
                                emitir_audio_changed(&conn_sessao, &js);
                            }
                        }
                }
            }
            let _ = child.wait();
        }
        thread::sleep(Duration::from_secs(3));
    }
}

/// Monitora sinais D-Bus do `org.bluez` e `org.freedesktop.NetworkManager` no barramento do sistema.
fn laco_sistema_dbus(estado: Arc<Mutex<EstadoHermes>>, conn_sessao: DbusConn) {
    let Ok(sys_conn) = DbusConn::system() else {
        return;
    };
    for regra in [
        "type='signal',path='/org/freedesktop/NetworkManager',interface='org.freedesktop.DBus.Properties'",
        "type='signal',path_namespace='/org/freedesktop/NetworkManager/Devices',interface='org.freedesktop.DBus.Properties'",
        "type='signal',path_namespace='/org/bluez'",
    ] {
        let _ = sys_conn.call_method(
            Some("org.freedesktop.DBus"),
            "/org/freedesktop/DBus",
            Some("org.freedesktop.DBus"),
            "AddMatch",
            &(regra,),
        );
    }
    let iter = zbus::blocking::MessageIterator::from(&sys_conn);

    // Último JSON emitido de cada um: só emite de novo quando o estado REALMENTE mudou (o BlueZ e o NM
    // disparam PropertiesChanged para RSSI, varreduras etc. que não mudam nada do que a UI mostra).
    let mut ultimo_bt = String::new();
    let mut ultimo_wifi = String::new();

    for msg_res in iter {
        let Ok(msg) = msg_res else {
            continue;
        };
        let hdr = msg.header();
        if hdr.message_type() != zbus::message::Type::Signal {
            continue;
        }
        let path = hdr.path().map(|p| p.as_str()).unwrap_or("");

        if path.starts_with("/org/bluez") {
            let bt = consultar_bluetooth_dbus(&sys_conn);
            {
                let mut est = estado.lock().unwrap();
                est.bluetooth = bt;
            }
            let js = backends::bluetooth_estado(&sys_conn);
            let txt = js.to_string();
            if txt != ultimo_bt {
                ultimo_bt = txt;
                emitir_estado(&conn_sessao, "BluetoothChanged", &js);
            }
        } else if path.starts_with("/org/freedesktop/NetworkManager") {
            let wf = consultar_wifi_dbus(&sys_conn);
            {
                let mut est = estado.lock().unwrap();
                est.wifi = wf;
            }
            let js = backends::wifi_estado(&sys_conn);
            let txt = js.to_string();
            if txt != ultimo_wifi {
                ultimo_wifi = txt;
                emitir_estado(&conn_sessao, "WifiChanged", &js);
            }
        }
    }
}

fn main() {
    if env::args().any(|a| a == "--teste") {
        // Instância de teste: outro nome de barramento, sem assumir org.freedesktop.Notifications nem
        // mexer no som.py — roda ao lado do hermesd real (ou num `dbus-run-session`).
        let _ = NOME_BUS.set(format!("{NOME_HERMES}.Teste"));
    }
    let config = ler_config();
    let (tx_timer, rx_timer) = mpsc::channel();

    let mut modos = BTreeSet::new();
    if config.dnd.enabled {
        modos.insert("dnd".to_string());
    }

    let conn_sistema = DbusConn::system().ok();
    let audio_ini = coletar_audio_atual();
    let bt_ini = conn_sistema
        .as_ref()
        .map(consultar_bluetooth_dbus)
        .unwrap_or_default();
    let wifi_ini = conn_sistema
        .as_ref()
        .map(consultar_wifi_dbus)
        .unwrap_or_default();

    let mut estado_ini = EstadoHermes {
        config,
        ativas: HashMap::new(),
        historico: VecDeque::new(),
        proximo: 1,
        taxa: HashMap::new(),
        modos,
        audio: audio_ini,
        audio_json_emitido: String::new(),
        bluetooth: bt_ini,
        wifi: wifi_ini,
        tx_timer,
    };
    estado_ini.carregar_historico(&caminho_historico_hermes());

    let estado = Arc::new(Mutex::new(estado_ini));
    let conn_sessao =
        DbusConn::session().expect("hermesd: falha ao conectar ao D-Bus da sessão");

    let srv_notif = ServicoNotificacoes {
        estado: Arc::clone(&estado),
    };
    let srv_ctl = ServicoControleHermes {
        estado: Arc::clone(&estado),
        sessao: conn_sessao.clone(),
        sistema: conn_sistema,
    };

    if !modo_teste() {
        conn_sessao
            .object_server()
            .at(CAMINHO_NOTIF, srv_notif)
            .expect("hermesd: falha ao registrar org.freedesktop.Notifications");
    }
    conn_sessao
        .object_server()
        .at(CAMINHO_NOTIF, srv_ctl)
        .expect("hermesd: falha ao registrar com.aero.Hermes");

    if modo_teste() {
        conn_sessao
            .request_name_with_flags(
                WellKnownName::try_from(nome_bus()).expect("hermesd: nome de teste inválido"),
                RequestNameFlags::AllowReplacement | RequestNameFlags::ReplaceExisting,
            )
            .expect("hermesd: falha ao assumir o nome de teste");
        eprintln!("hermesd: modo de teste em {}", nome_bus());
    }

    let tinha_dono_py = !modo_teste() && conn_sessao
        .call_method(
            Some("org.freedesktop.DBus"),
            "/org/freedesktop/DBus",
            Some("org.freedesktop.DBus"),
            "NameHasOwner",
            &(NOME_HERMES,),
        )
        .ok()
        .and_then(|m| m.body().deserialize::<bool>().ok())
        .unwrap_or(false);

    if !modo_teste() {
        conn_sessao
            .request_name_with_flags(
                WellKnownName::from_static_str_unchecked(NOME_NOTIF),
                RequestNameFlags::AllowReplacement | RequestNameFlags::ReplaceExisting,
            )
            .expect("hermesd: falha ao assumir org.freedesktop.Notifications");

        conn_sessao
            .request_name_with_flags(
                WellKnownName::from_static_str_unchecked(NOME_HERMES),
                RequestNameFlags::AllowReplacement | RequestNameFlags::ReplaceExisting,
            )
            .expect("hermesd: falha ao assumir com.aero.Hermes");
    }

    let conn_som = conn_sessao.clone();
    if !modo_teste() {
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(300));
        if tinha_dono_py {
            // Se o som.py estava rodando como dono da reserva em Python, encerramos pelo PID exato
            // para que ele suba de novo como cliente do hermesd.
            if let Ok(entradas) = fs::read_dir("/proc") {
                for e in entradas.flatten() {
                    if let Some(s) = e.file_name().to_str()
                        && let Ok(pid) = s.parse::<libc::pid_t>()
                        && pid > 1
                        && let Ok(cmd) = fs::read(e.path().join("cmdline"))
                        && cmd.windows(20).any(|w| w == b"/aero-shell/som.py\0-")
                    {
                        unsafe {
                            libc::kill(pid, libc::SIGTERM);
                        }
                    }
                }
            }
            thread::sleep(Duration::from_millis(300));
        }
        let som_ativo = conn_som
            .call_method(
                Some("org.freedesktop.DBus"),
                "/org/freedesktop/DBus",
                Some("org.freedesktop.DBus"),
                "NameHasOwner",
                &("com.aero.Sound",),
            )
            .ok()
            .and_then(|m| m.body().deserialize::<bool>().ok())
            .unwrap_or(false);
        if !som_ativo {
            let home = env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
            let som_bin = format!("{home}/.local/bin/som");
            let _ = Command::new(som_bin)
                .arg("--daemon")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn();
        }
    });
    }

    let est_timer = Arc::clone(&estado);
    let conn_timer = conn_sessao.clone();
    thread::Builder::new()
        .name("hermes-timers".to_string())
        .spawn(move || laco_timers(rx_timer, est_timer, conn_timer))
        .expect("hermesd: falha ao criar thread de timers");

    let est_audio = Arc::clone(&estado);
    let conn_audio = conn_sessao.clone();
    thread::Builder::new()
        .name("hestia-audio".to_string())
        .spawn(move || laco_audio_eventos(est_audio, conn_audio))
        .expect("hermesd: falha ao criar thread de áudio");

    let est_sys = Arc::clone(&estado);
    let conn_sys_sinais = conn_sessao.clone();
    thread::Builder::new()
        .name("hestia-sysbus".to_string())
        .spawn(move || laco_sistema_dbus(est_sys, conn_sys_sinais))
        .expect("hermesd: falha ao criar thread do system bus");

    // A thread principal dorme indefinidamente (o zbus processa mensagens nas suas threads internas
    // bloqueadas em poll/epoll, consumindo 0% de CPU parado).
    loop {
        thread::park();
    }
}
