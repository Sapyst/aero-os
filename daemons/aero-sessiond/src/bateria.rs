//! Módulo de energia inteligente (`bateria.rs`), equivalente em Rust ao `bateria.py` e `servicos/energia.py`.
//!
//! Funções que este módulo cumpre:
//! 1. **Limitador de carga**:
//!    - `smart`: para em 80%; depois de 24 h seguidas na tomada, desce para 60% ("desk mode").
//!    - `fixo`: para sempre em `limite`% (60..=100).
//!    - `bypass`: segura a bateria no nível atual (mín. 60%) para o notebook rodar direto da tomada.
//!    - `desligado`: carrega até 100%.
//!    - `cheia_uma_vez`: carrega até 100% uma única vez (para viagem) e volta ao limitador ao tirar da tomada.
//! 2. **Modo de energia automático (`powerprofilesctl`)**:
//!    - Jogo aberto -> `performance`
//!    - Na tomada -> `perfil_tomada` (padrão `balanced`)
//!    - Na bateria -> `power-saver`
//! 3. **Salvar o momento (bateria crítica)**:
//!    - Na bateria com nível <= `critico`%: avisa com urgência crítica, espera 15 s (cancela se ligar na tomada)
//!      e hiberna (se suportado pelo `systemd-logind`) ou suspende.
//! 4. **Comandos rápidos de linha de comando**:
//!    - `--waybar`, `--alternar`, `--cheia`, `--status`.

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::HashSet;
use std::env;
use std::ffi::CString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const FONTES: &str = "/sys/class/power_supply";
const AORUS: &str = "/sys/devices/platform/aorus_laptop";
const DIA_SEGUNDOS: f64 = 24.0 * 3600.0;
const SINAL_WAYBAR: i32 = 12;

/// Dados lidos do kernel (`/sys/class/power_supply/BAT*`).
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct InfoBateria {
    pub nivel: i32,
    pub estado: String,
    pub saude: Option<i32>,
    pub ciclos: Option<i32>,
}

/// Configuração persistida em `~/.config/aero-shell/energia.json`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigEnergia {
    #[serde(default = "padrao_modo")]
    pub modo: String,
    #[serde(default = "padrao_limite")]
    pub limite: i32,
    #[serde(default = "padrao_perfil_auto")]
    pub perfil_auto: bool,
    #[serde(default = "padrao_perfil_tomada")]
    pub perfil_tomada: String,
    #[serde(default = "padrao_critico")]
    pub critico: i32,
    #[serde(default)]
    pub cheia_uma_vez: bool,
    #[serde(default)]
    pub segurar: Option<i32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub modo_antes: Option<String>,
    #[serde(flatten)]
    pub extras: Map<String, Value>,
}

fn padrao_modo() -> String {
    "smart".to_string()
}
fn padrao_limite() -> i32 {
    80
}
fn padrao_perfil_auto() -> bool {
    true
}
fn padrao_perfil_tomada() -> String {
    "balanced".to_string()
}
fn padrao_critico() -> i32 {
    6
}

impl Default for ConfigEnergia {
    fn default() -> Self {
        Self {
            modo: padrao_modo(),
            limite: padrao_limite(),
            perfil_auto: padrao_perfil_auto(),
            perfil_tomada: padrao_perfil_tomada(),
            critico: padrao_critico(),
            cheia_uma_vez: false,
            segurar: None,
            modo_antes: None,
            extras: Map::new(),
        }
    }
}

/// Memória interna do daemon em `~/.cache/aero-shell/energia.json`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct MemoriaEnergia {
    #[serde(default)]
    pub tomada_desde: Option<f64>,
}

/// Parâmetros de detecção de jogos vindos de `~/.config/aero-shell/taxa.json`.
#[derive(Deserialize)]
struct ConfigTaxaJogos {
    #[serde(default = "padrao_classes_jogo")]
    classes_jogo: Vec<String>,
}

pub fn padrao_classes_jogo() -> Vec<String> {
    vec![
        "^steam_app_".to_string(),
        "^gamescope$".to_string(),
        "^Minecraft".to_string(),
        "\\.exe$".to_string(),
    ]
}

fn pasta_home() -> PathBuf {
    env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/home/sapista"))
}

pub fn caminho_config_energia() -> PathBuf {
    pasta_home().join(".config/aero-shell/energia.json")
}

pub fn caminho_config_taxa() -> PathBuf {
    pasta_home().join(".config/aero-shell/taxa.json")
}

fn caminho_memoria() -> PathBuf {
    pasta_home().join(".cache/aero-shell/energia.json")
}

pub fn ler_config() -> ConfigEnergia {
    let p = caminho_config_energia();
    let Ok(txt) = fs::read_to_string(p) else {
        return ConfigEnergia::default();
    };
    serde_json::from_str::<ConfigEnergia>(&txt).unwrap_or_default()
}

pub fn gravar_config(cfg: &ConfigEnergia) {
    let p = caminho_config_energia();
    if let Some(dir) = p.parent() {
        let _ = fs::create_dir_all(dir);
    }
    let tmp = p.with_extension("json.tmp");
    if let Ok(txt) = serde_json::to_string_pretty(cfg)
        && fs::write(&tmp, txt).is_ok()
    {
        let _ = fs::rename(&tmp, &p);
    }
}

pub fn ler_memoria() -> MemoriaEnergia {
    let p = caminho_memoria();
    let Ok(txt) = fs::read_to_string(p) else {
        return MemoriaEnergia::default();
    };
    serde_json::from_str::<MemoriaEnergia>(&txt).unwrap_or_default()
}

pub fn gravar_memoria(mem: &MemoriaEnergia) {
    let p = caminho_memoria();
    if let Some(dir) = p.parent() {
        let _ = fs::create_dir_all(dir);
    }
    let tmp = p.with_extension("json.tmp");
    if let Ok(txt) = serde_json::to_string(mem)
        && fs::write(&tmp, txt).is_ok()
    {
        let _ = fs::rename(&tmp, &p);
    }
}

pub fn ler_padroes_jogo() -> Vec<Regex> {
    let p = caminho_config_taxa();
    let padroes = fs::read_to_string(p)
        .ok()
        .and_then(|t| serde_json::from_str::<ConfigTaxaJogos>(&t).ok())
        .map(|c| c.classes_jogo)
        .filter(|v| !v.is_empty())
        .unwrap_or_else(padrao_classes_jogo);
    padroes
        .iter()
        .filter_map(|s| Regex::new(s).ok())
        .collect()
}

fn ler_sysfs(caminho: impl AsRef<Path>) -> Option<String> {
    fs::read_to_string(caminho).ok().map(|s| s.trim().to_string())
}

fn escrever_sysfs(caminho: impl AsRef<Path>, valor: impl ToString) -> bool {
    fs::write(caminho, valor.to_string()).is_ok()
}

fn fontes_do_tipo(tipo: &str) -> Vec<PathBuf> {
    let Ok(entradas) = fs::read_dir(FONTES) else {
        return Vec::new();
    };
    let mut caminhos: Vec<PathBuf> = entradas
        .flatten()
        .map(|e| e.path())
        .filter(|p| ler_sysfs(p.join("type")).as_deref() == Some(tipo))
        .collect();
    caminhos.sort();
    caminhos
}

/// Retorna `Some(true)` se o carregador está conectado, `Some(false)` se está na bateria,
/// ou `None` se a máquina não tem fonte de tomada/bateria.
pub fn na_tomada() -> Option<bool> {
    let adaptadores: Vec<PathBuf> = fontes_do_tipo("Mains")
        .into_iter()
        .filter(|p| ler_sysfs(p.join("online")).is_some())
        .collect();
    if adaptadores.is_empty() {
        return None;
    }
    Some(
        adaptadores
            .iter()
            .any(|p| ler_sysfs(p.join("online")).as_deref() == Some("1")),
    )
}

fn pasta_bateria() -> Option<PathBuf> {
    fontes_do_tipo("Battery").into_iter().next()
}

/// Lê os dados atuais da bateria no sysfs.
pub fn bateria() -> Option<InfoBateria> {
    let b = pasta_bateria()?;
    let ler_int = |nome: &str| -> i64 {
        ler_sysfs(b.join(nome))
            .and_then(|s| s.parse::<i64>().ok())
            .unwrap_or(0)
    };
    let cheio = {
        let c = ler_int("charge_full");
        if c != 0 { c } else { ler_int("energy_full") }
    };
    let projeto = {
        let p = ler_int("charge_full_design");
        if p != 0 {
            p
        } else {
            ler_int("energy_full_design")
        }
    };
    let nivel = ler_int("capacity") as i32;
    let estado = ler_sysfs(b.join("status")).unwrap_or_else(|| "?".to_string());
    let saude = if cheio > 0 && projeto > 0 {
        Some(((cheio as f64 * 100.0) / (projeto as f64)).round() as i32)
    } else {
        None
    };
    let ciclos_raw = ler_int("cycle_count") as i32;
    let ciclos = if ciclos_raw > 0 { Some(ciclos_raw) } else { None };
    Some(InfoBateria {
        nivel,
        estado,
        saude,
        ciclos,
    })
}

/// Identifica o driver de limite de carga disponível: `"padrao"`, `"gigabyte"` ou `""`.
pub fn driver() -> &'static str {
    if let Some(b) = pasta_bateria()
        && b.join("charge_control_end_threshold").exists()
    {
        return "padrao";
    }
    if Path::new(AORUS).join("charge_limit").exists() {
        return "gigabyte";
    }
    ""
}

pub fn limite_arquivo() -> Option<PathBuf> {
    match driver() {
        "padrao" => Some(pasta_bateria()?.join("charge_control_end_threshold")),
        "gigabyte" => Some(Path::new(AORUS).join("charge_limit")),
        _ => None,
    }
}

pub fn limite() -> Option<i32> {
    let d = driver();
    if d.is_empty() {
        return None;
    }
    if d == "gigabyte" && ler_sysfs(Path::new(AORUS).join("charge_mode")).as_deref() != Some("1") {
        return Some(100);
    }
    let arq = limite_arquivo()?;
    let v = ler_sysfs(arq)?;
    if !v.is_empty() && v.chars().all(|c| c.is_ascii_digit()) {
        v.parse::<i32>().ok()
    } else {
        None
    }
}

fn arquivo_gravavel(p: &Path) -> bool {
    let Ok(cstr) = CString::new(p.as_os_str().as_encoded_bytes()) else {
        return false;
    };
    unsafe { libc::access(cstr.as_ptr(), libc::W_OK) == 0 }
}

pub fn pode_escrever() -> bool {
    let Some(arq) = limite_arquivo() else {
        return false;
    };
    arquivo_gravavel(&arq)
        && (driver() != "gigabyte" || arquivo_gravavel(&Path::new(AORUS).join("charge_mode")))
}

pub fn definir_limite(pct: i32) -> bool {
    let pct = pct.clamp(60, 100);
    if limite() == Some(pct) {
        return true;
    }
    match driver() {
        "gigabyte" => {
            let modo = if pct == 100 { "0" } else { "1" };
            let caminho_modo = Path::new(AORUS).join("charge_mode");
            let caminho_lim = Path::new(AORUS).join("charge_limit");
            let mut ok = true;
            if ler_sysfs(&caminho_modo).as_deref() != Some(modo) {
                ok = escrever_sysfs(&caminho_modo, modo);
            }
            if ok && pct < 100 && ler_sysfs(&caminho_lim).as_deref() != Some(&pct.to_string()) {
                ok = escrever_sysfs(&caminho_lim, pct);
            }
            ok
        }
        "padrao" => {
            if let Some(arq) = limite_arquivo() {
                escrever_sysfs(arq, pct)
            } else {
                false
            }
        }
        _ => false,
    }
}

pub fn perfil() -> String {
    Command::new("powerprofilesctl")
        .arg("get")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default()
}

pub fn definir_perfil(p: &str) {
    if matches!(p, "performance" | "balanced" | "power-saver") && perfil() != p {
        let _ = Command::new("powerprofilesctl")
            .args(["set", p])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

pub fn pode_hibernar() -> bool {
    Command::new("busctl")
        .args([
            "call",
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "CanHibernate",
        ])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).contains("\"yes\""))
        .unwrap_or(false)
}

fn agora_unix() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn nome_modo(modo: &str) -> &'static str {
    match modo {
        "smart" => "Smart limiter",
        "fixo" => "Charge limit",
        "bypass" => "Direct power",
        "desligado" => "No limit",
        _ => "Smart limiter",
    }
}

fn icone_modo(modo: &str) -> &'static str {
    match modo {
        "smart" | "fixo" => "\u{f06c}",
        "bypass" => "\u{f1e6}",
        _ => "",
    }
}

/// Calcula o limite de carga que deve vigorar agora (`100` = sem limite).
pub fn alvo(cfg: &ConfigEnergia, mem: &MemoriaEnergia, tomada: Option<bool>, nivel: i32) -> i32 {
    if cfg.cheia_uma_vez || cfg.modo == "desligado" {
        return 100;
    }
    if cfg.modo == "fixo" {
        return cfg.limite.clamp(60, 100);
    }
    if cfg.modo == "bypass" {
        let base = cfg.segurar.unwrap_or(if nivel > 0 { nivel } else { 80 });
        return base.clamp(60, 100);
    }
    // smart
    if tomada == Some(true)
        && let Some(desde) = mem.tomada_desde
        && desde > 0.0
        && agora_unix() - desde >= DIA_SEGUNDOS
    {
        return 60;
    }
    80
}

pub fn descrever(
    cfg: &ConfigEnergia,
    mem: &MemoriaEnergia,
    b: Option<&InfoBateria>,
    tomada: Option<bool>,
) -> String {
    let mut linhas = Vec::new();
    if let Some(bat) = b {
        linhas.push(format!(
            "{}%  ·  {}",
            bat.nivel,
            if tomada == Some(true) {
                "on the charger"
            } else {
                "on battery"
            }
        ));
    }
    if cfg.cheia_uma_vez {
        linhas.push("Charging to 100% once (back to the limiter when unplugged)".to_string());
    } else if cfg.modo != "desligado" {
        let lim = alvo(cfg, mem, tomada, b.map(|x| x.nivel).unwrap_or(80));
        let extra = match cfg.modo.as_str() {
            "smart" if lim == 60 => " (desk mode: a day on the charger)",
            "bypass" => ": the laptop runs from the charger, the battery rests",
            _ => "",
        };
        linhas.push(format!("{}: stops at {}%{}", nome_modo(&cfg.modo), lim, extra));
    }
    if cfg.modo != "desligado" && driver().is_empty() {
        linhas.push("Needs the battery driver (Settings → Power)".to_string());
    }
    if let Some(bat) = b
        && let Some(saude) = bat.saude
    {
        linhas.push(format!(
            "Health {}%  ·  {} cycles",
            saude,
            bat.ciclos.unwrap_or(0)
        ));
    }
    linhas.push("Click: limiter on/off  ·  Right click: Power settings".to_string());
    linhas.join("\n")
}

pub fn avisar(titulo: &str, texto: &str, urgente: bool, etiqueta: Option<&str>) {
    let mut cmd = Command::new("notify-send");
    cmd.args(["-a", "Power", "-i", "battery-symbolic"]);
    if urgente {
        cmd.args(["-u", "critical"]);
    }
    if let Some(tag) = etiqueta {
        cmd.args([
            "-h",
            &format!("string:x-canonical-private-synchronous:{tag}"),
            "-h",
            &format!("string:x-dunst-stack-tag:{tag}"),
        ]);
    }
    cmd.args([titulo, texto]);
    cmd.stdout(Stdio::null()).stderr(Stdio::null());
    let _ = cmd.spawn();
}

pub fn atualizar_barra() {
    let _ = Command::new("pkill")
        .args([&format!("-RTMIN+{SINAL_WAYBAR}"), "-x", "waybar"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

/// Saída JSON em uma linha para o módulo da barra (`--waybar`).
pub fn cmd_waybar() {
    let cfg = ler_config();
    let mem = ler_memoria();
    let b = bateria();
    let tomada = na_tomada();
    let modo = if b.is_none() {
        "desligado"
    } else if cfg.cheia_uma_vez {
        "cheia"
    } else {
        cfg.modo.as_str()
    };
    let texto = if modo != "desligado" && modo != "cheia" {
        icone_modo(&cfg.modo)
    } else if modo == "cheia" {
        "\u{f0e7}"
    } else {
        ""
    };
    let classe = if !driver().is_empty() || modo == "desligado" {
        modo.to_string()
    } else {
        format!("{modo} sem-driver")
    };
    let tooltip = if b.is_some() {
        descrever(&cfg, &mem, b.as_ref(), tomada)
    } else {
        String::new()
    };
    let saida = serde_json::json!({
        "text": texto,
        "tooltip": tooltip,
        "class": classe,
    });
    println!("{}", saida);
}

/// Liga/desliga o limitador ao clicar na bateria (`--alternar`).
pub fn cmd_alternar() {
    let mut cfg = ler_config();
    if cfg.modo == "desligado" {
        cfg.modo = cfg.modo_antes.clone().unwrap_or_else(|| "smart".to_string());
    } else {
        cfg.modo_antes = Some(cfg.modo.clone());
        cfg.modo = "desligado".to_string();
    }
    cfg.cheia_uma_vez = false;
    if cfg.modo == "bypass" {
        cfg.segurar = bateria().map(|b| b.nivel);
    }
    gravar_config(&cfg);
    if driver().is_empty() && cfg.modo != "desligado" {
        avisar(
            &format!("{} on", nome_modo(&cfg.modo)),
            "It starts working once the battery driver is installed (Settings → Power).",
            false,
            Some("aero-power-mode"),
        );
    } else {
        let sufixo = if cfg.modo != "desligado" { " on" } else { "" };
        let corpo = if cfg.modo == "desligado" {
            "Charging to 100% again"
        } else {
            "Stops before full to keep the battery healthy"
        };
        avisar(
            &format!("{}{}", nome_modo(&cfg.modo), sufixo),
            corpo,
            false,
            Some("aero-power-mode"),
        );
    }
    atualizar_barra();
}

/// Ativa "carregar 100% uma vez" (`--cheia`).
pub fn cmd_cheia() {
    let mut cfg = ler_config();
    cfg.cheia_uma_vez = true;
    gravar_config(&cfg);
    avisar(
        "Charging to 100% once",
        "Back to the limiter when you unplug.",
        false,
        None,
    );
    atualizar_barra();
}

/// Exibe o diagnóstico completo no terminal (`--status`), idêntico ao `bateria.py --status`.
pub fn cmd_status() {
    let cfg = ler_config();
    let mem = ler_memoria();
    let b = bateria();
    let t = na_tomada();
    if b.is_some() {
        println!("{}", descrever(&cfg, &mem, b.as_ref(), t));
    } else {
        println!("no battery");
    }
    let drv = driver();
    let drv_str = if drv.is_empty() { "none" } else { drv };
    let write_str = if pode_escrever() { "True" } else { "False" };
    let hib_str = if pode_hibernar() { "True" } else { "False" };
    let lim_alvo = alvo(&cfg, &mem, t, b.as_ref().map(|x| x.nivel).unwrap_or(80));
    println!(
        "driver: {} | write access: {} | target: {} | profile: {} | hibernate: {}",
        drv_str,
        write_str,
        lim_alvo,
        perfil(),
        hib_str
    );
}

/// Estado residente do controlador de bateria dentro do `aero-sessiond`.
pub struct EstadoBateria {
    mem: MemoriaEnergia,
    tomada: Option<bool>,
    salvando: Option<Instant>,
    janelas_jogo: HashSet<String>,
    padroes_jogo: Vec<Regex>,
}

impl EstadoBateria {
    pub fn novo(janelas_iniciais: &[(String, String)]) -> Self {
        let padroes_jogo = ler_padroes_jogo();
        let mut janelas_jogo = HashSet::new();
        for (end, classe) in janelas_iniciais {
            if padroes_jogo.iter().any(|re| re.is_match(classe)) {
                janelas_jogo.insert(end.clone());
            }
        }
        Self {
            mem: ler_memoria(),
            tomada: None,
            salvando: None,
            janelas_jogo,
            padroes_jogo,
        }
    }

    pub fn recarregar_padroes_jogo(&mut self, janelas_atuais: &[(String, String)]) {
        self.padroes_jogo = ler_padroes_jogo();
        self.janelas_jogo.clear();
        for (end, classe) in janelas_atuais {
            if self.eh_jogo(classe) {
                self.janelas_jogo.insert(end.clone());
            }
        }
    }

    pub fn eh_jogo(&self, classe: &str) -> bool {
        self.padroes_jogo.iter().any(|re| re.is_match(classe))
    }

    pub fn ao_abrir_janela(&mut self, endereco: &str, classe: &str) -> bool {
        if self.eh_jogo(classe) {
            return self.janelas_jogo.insert(endereco.to_string());
        }
        false
    }

    pub fn ao_fechar_janela(&mut self, endereco: &str) -> bool {
        self.janelas_jogo.remove(endereco)
    }

    pub fn tem_jogo_aberto(&self) -> bool {
        !self.janelas_jogo.is_empty()
    }

    /// Executa um passo de verificação/aplicação (chamado somente quando há evento real:
    /// uevent de energia, mudança em `energia.json`/`taxa.json`, abertura/fechamento de jogo
    /// ou vencimento do timer de 15 s / 24 h).
    pub fn passo(&mut self) {
        let mut cfg = ler_config();
        let Some(b) = bateria() else {
            return;
        };
        let tomada = na_tomada();

        if tomada != self.tomada {
            if tomada == Some(true) {
                // O relógio das 24 h só zera numa ligação de verdade (não quando o daemon reinicia)
                if self.tomada == Some(false) || self.mem.tomada_desde.is_none() {
                    self.mem.tomada_desde = Some(agora_unix());
                }
                self.salvando = None;
            } else {
                self.mem.tomada_desde = None;
                if self.tomada.is_some() {
                    // Tirou da tomada (não é a primeira leitura ao iniciar)
                    unsafe {
                        libc::sync();
                    }
                    if cfg.cheia_uma_vez {
                        cfg.cheia_uma_vez = false;
                        gravar_config(&cfg);
                    }
                }
            }
            self.tomada = tomada;
            gravar_memoria(&self.mem);
            atualizar_barra();
        }

        // Modo de energia automático
        if cfg.perfil_auto {
            let alvo_perfil = if self.tem_jogo_aberto() {
                "performance"
            } else if tomada == Some(true) {
                cfg.perfil_tomada.as_str()
            } else {
                "power-saver"
            };
            definir_perfil(alvo_perfil);
        }

        // Limitador de carga
        if !driver().is_empty() && pode_escrever() {
            definir_limite(alvo(&cfg, &self.mem, tomada, b.nivel));
        }

        // Salvar o momento (bateria crítica)
        let critico = cfg.critico.max(0);
        if tomada != Some(true) && critico > 0 && b.nivel <= critico {
            match self.salvando {
                None => {
                    self.salvando = Some(Instant::now());
                    let acao = if pode_hibernar() {
                        "Hibernating"
                    } else {
                        "Suspending"
                    };
                    avisar(
                        "Battery critical: saving the moment",
                        &format!("{acao} in 15 s to keep your session. Plug in the charger to cancel."),
                        true,
                        None,
                    );
                }
                Some(inicio) if inicio.elapsed() >= Duration::from_secs(15) => {
                    unsafe {
                        libc::sync();
                    }
                    let subcmd = if pode_hibernar() {
                        "hibernate"
                    } else {
                        "suspend"
                    };
                    let _ = Command::new("systemctl").arg(subcmd).status();
                    self.salvando = None;
                }
                _ => {}
            }
        } else if self.salvando.is_some() && (tomada == Some(true) || b.nivel > critico) {
            self.salvando = None;
        }
    }

    /// Calcula quanto tempo falta para o próximo evento agendado (15 s do bateria crítica
    /// ou completar 24 h na tomada no modo `smart`). Se não houver timer pendente, retorna `None`
    /// para que o loop principal durma sem acordar à toa (0% CPU parado).
    pub fn proximo_prazo(&self) -> Option<Duration> {
        if let Some(inicio) = self.salvando {
            let decorrido = inicio.elapsed();
            return Some(Duration::from_secs(15).saturating_sub(decorrido));
        }
        if self.tomada == Some(true)
            && let Some(desde) = self.mem.tomada_desde
            && desde > 0.0
        {
            let decorrido = agora_unix() - desde;
            if decorrido < DIA_SEGUNDOS {
                let falta = (DIA_SEGUNDOS - decorrido).ceil() as u64;
                return Some(Duration::from_secs(falta.max(1)));
            }
        }
        None
    }
}
