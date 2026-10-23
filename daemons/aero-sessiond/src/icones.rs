//! Módulo vigia de ícones (`icones.rs`), equivalente em Rust ao `icones.py --watch` (`vigiar()`).
//!
//! O gerador visual e gerenciador de packs (`icones.py`) continua em Python e só roda sob demanda
//! (`--auto`, `--retint`, `--tint`, `--app`, etc.), saindo da memória logo ao terminar.
//! A única parte que ficava residente na memória (~13 MB PSS + ~14 MB SwapPss) era o `--watch`:
//! 1. Vigia mudanças em `~/.config/hypr/painel.conf`, `~/.config/aero-shell/widgets.json` e
//!    no esquema claro/escuro (`org.gnome.desktop.interface color-scheme`), com debounce de 1,5 s
//!    (e uma conferência inicial 3 s após o login).
//! 2. Se a fonte de cores mudou e o iconizador ASCII está ligado (`ICONES_ASCII=1`), dispara
//!    `python3 ~/.local/share/aero-shell/icones.py --retint`.
//! 3. Ao trocar de área de trabalho (`workspacev2` no compositor), alterna o tema GTK de ícones
//!    entre `Aero-ws<N>` (se aquela área tiver variante própria) e `Aero`.

use crate::compositor::Compositor;
use serde_json::Value;
use std::collections::{BTreeMap, HashMap};
use std::env;
use std::fs;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const TEMA: &str = "Aero";
const TINT_MODES: [&str; 4] = ["classic", "system", "general", "workspaces"];

fn pasta_home() -> PathBuf {
    env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/home/sapista"))
}

fn caminho_painel() -> PathBuf {
    pasta_home().join(".config/hypr/painel.conf")
}

fn caminho_widgets() -> PathBuf {
    pasta_home().join(".config/aero-shell/widgets.json")
}

fn pasta_icones() -> PathBuf {
    pasta_home().join(".local/share/icons")
}

fn script_icones_py() -> PathBuf {
    pasta_home().join(".local/share/aero-shell/icones.py")
}

/// Lê os pares `CHAVE=valor` de `~/.config/hypr/painel.conf`.
fn ler_painel() -> HashMap<String, String> {
    let mut mapa = HashMap::new();
    let Ok(txt) = fs::read_to_string(caminho_painel()) else {
        return mapa;
    };
    for linha in txt.lines() {
        if !linha.starts_with('#')
            && let Some((k, v)) = linha.split_once('=')
        {
            mapa.insert(
                k.trim().to_string(),
                v.trim().trim_matches('"').to_string(),
            );
        }
    }
    mapa
}

fn ligado(painel: &HashMap<String, String>) -> bool {
    painel.get("ICONES_ASCII").map(String::as_str) == Some("1")
}

fn tint_mode(painel: &HashMap<String, String>) -> String {
    let v = painel
        .get("ICONS_TINT")
        .map(String::as_str)
        .unwrap_or("classic");
    if TINT_MODES.contains(&v) {
        v.to_string()
    } else {
        "classic".to_string()
    }
}

fn sistema_escuro() -> bool {
    Command::new("gsettings")
        .args(["get", "org.gnome.desktop.interface", "color-scheme"])
        .output()
        .ok()
        .map(|o| !String::from_utf8_lossy(&o.stdout).contains("light"))
        .unwrap_or(true)
}

fn hex_normalizado(hex: Option<&String>, padrao: &str) -> String {
    let s = hex
        .map(|x| x.trim().trim_start_matches('#'))
        .filter(|x| x.len() >= 6 && x[..6].chars().all(|c| c.is_ascii_hexdigit()))
        .unwrap_or(padrao);
    s[..6].to_ascii_lowercase()
}

/// Calcula a chave hexadecimal de 12 caracteres (`plate` + `ink`) da paleta do sistema,
/// reproduzindo `chave(paleta_sistema())` de `icones.py`.
fn chave_paleta_sistema(painel: &HashMap<String, String>) -> String {
    let escura = hex_normalizado(painel.get("COR_ESCURA"), "1e1e1e");
    let clara = hex_normalizado(painel.get("COR_CLARA"), "f6f6f6");
    if sistema_escuro() {
        format!("{escura}{clara}")
    } else {
        format!("{clara}{escura}")
    }
}

/// Lê o dicionário `por_area` de `~/.config/aero-shell/widgets.json` ordenado por chave.
fn ler_por_area() -> BTreeMap<String, Value> {
    let Ok(txt) = fs::read_to_string(caminho_widgets()) else {
        return BTreeMap::new();
    };
    let Ok(val) = serde_json::from_str::<Value>(&txt) else {
        return BTreeMap::new();
    };
    let Some(obj) = val.get("por_area").and_then(Value::as_object) else {
        return BTreeMap::new();
    };
    obj.iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// Assinatura da fonte de cores atual (`fonte()` em `icones.py`).
fn calcular_fonte(painel: &HashMap<String, String>) -> String {
    let m = tint_mode(painel);
    if m == "system" {
        return format!("system|{}", chave_paleta_sistema(painel));
    }
    let por_area = ler_por_area();
    if m == "general" || m == "workspaces" {
        let wall = painel.get("WALL_ORIGINAL").cloned().unwrap_or_default();
        let area_json = serde_json::to_string(&por_area).unwrap_or_default();
        return format!("{m}|{wall}|{area_json}");
    }
    // classic (com possíveis overrides por área)
    let mut icones_area: BTreeMap<String, Value> = BTreeMap::new();
    for (a, reg) in por_area {
        let ic = reg
            .as_object()
            .and_then(|o| o.get("icons"))
            .cloned()
            .unwrap_or(Value::Null);
        icones_area.insert(a, ic);
    }
    let area_json = serde_json::to_string(&icones_area).unwrap_or_default();
    format!("{m}|{area_json}")
}

/// Nome do tema de ícones que deve estar ativo para a área de trabalho `area`:
/// `Aero-ws<area>` se existir `index.theme` nela, ou `Aero`.
pub fn tema_da_area(area: i32) -> String {
    let variante = format!("{TEMA}-ws{area}");
    if pasta_icones().join(&variante).join("index.theme").is_file() {
        variante
    } else {
        TEMA.to_string()
    }
}

fn tema_atual_gsettings() -> String {
    Command::new("gsettings")
        .args(["get", "org.gnome.desktop.interface", "icon-theme"])
        .output()
        .ok()
        .map(|o| {
            String::from_utf8_lossy(&o.stdout)
                .trim()
                .trim_matches('\'')
                .to_string()
        })
        .unwrap_or_default()
}

/// Estado residente do vigia de ícones dentro do `aero-sessiond`.
pub struct EstadoIcones {
    fonte_anterior: Option<String>,
    conferir_em: Option<Instant>,
    tema_cache: Option<String>,
}

impl EstadoIcones {
    pub fn novo() -> Self {
        Self {
            fonte_anterior: None,
            // Igual ao icones.py: 3 s após iniciar, confere se as cores mudaram enquanto estava desligado
            conferir_em: Some(Instant::now() + Duration::from_secs(3)),
            tema_cache: None,
        }
    }

    pub fn invalidar_cache_tema(&mut self) {
        self.tema_cache = None;
    }

    pub fn usar_tema(&mut self, nome: &str) {
        if self.tema_cache.as_deref() == Some(nome) {
            return;
        }
        let atual = tema_atual_gsettings();
        if atual != nome {
            let _ = Command::new("gsettings")
                .args(["set", "org.gnome.desktop.interface", "icon-theme", nome])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        self.tema_cache = Some(nome.to_string());
    }

    /// Agenda uma conferência com debounce de 1,5 s (`depois()` no `icones.py`).
    pub fn agendar_conferencia(&mut self) {
        self.conferir_em = Some(Instant::now() + Duration::from_millis(1500));
    }

    /// Executa `conferir()` quando o timer vence: se a fonte de cores mudou e o iconizador ASCII
    /// está ligado, chama `python3 ~/.local/share/aero-shell/icones.py --retint`.
    pub fn verificar_prazo(&mut self, compositor: &dyn Compositor) {
        let Some(quando) = self.conferir_em else {
            return;
        };
        if Instant::now() < quando {
            return;
        }
        self.conferir_em = None;
        let painel = ler_painel();
        let f = calcular_fonte(&painel);
        if self.fonte_anterior.as_ref() != Some(&f) && ligado(&painel) {
            self.fonte_anterior = Some(f);
            let script = script_icones_py();
            if script.is_file() {
                let _ = Command::new("python3")
                    .arg("-I")
                    .arg(&script)
                    .arg("--retint")
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
                self.tema_cache = None;
            }
        }
        let area = compositor.area_ativa();
        let alvo = tema_da_area(area);
        self.usar_tema(&alvo);
    }

    /// Ao mudar de área de trabalho (`workspacev2`), aplica o tema de ícones daquela área.
    pub fn ao_mudar_workspace(&mut self, area: i32) {
        let alvo = tema_da_area(area);
        self.usar_tema(&alvo);
    }

    pub fn proximo_prazo(&self) -> Option<Duration> {
        self.conferir_em
            .map(|quando| quando.saturating_duration_since(Instant::now()))
    }
}
