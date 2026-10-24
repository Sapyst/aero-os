//! Camada de abstração do compositor (`trait Compositor`).
//!
//! Por que este módulo existe separado?
//! Hoje o Aero OS roda sobre o Hyprland, mas no futuro próximo vai rodar sobre o compositor
//! próprio (**Daimon**). Toda conversa com o compositor (listar janelas abertas para detectar
//! jogos, listar monitores e trocar a taxa de atualização em Hz, descobrir o workspace ativo
//! e escutar eventos de janela/monitor/workspace) fica isolada atrás da trait [`Compositor`].
//!
//! Quando o Daimon assumir, basta criar `DaimonCompositor` implementando [`Compositor`]
//! (falando com o socket do `daimonctl`) sem tocar em uma linha sequer de `bateria.rs`,
//! `taxa.rs` ou `icones.rs`.

use serde::Deserialize;
use std::env;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::mpsc::Sender;
use std::thread;
use std::time::Duration;

/// Eventos emitidos pelo compositor que interessam ao `aero-sessiond`.
#[derive(Debug, Clone)]
pub enum EventoCompositor {
    /// Uma janela foi aberta: `(endereco_sem_0x, classe)`.
    JanelaAberta { endereco: String, classe: String },
    /// Uma janela foi fechada: `endereco_sem_0x`.
    JanelaFechada { endereco: String },
    /// Configuração recarregada (`configreloaded`) ou monitor conectado (`monitoradded`/`monitoraddedv2`).
    MonitoresOuConfigMudaram,
    /// Usuário trocou para a área de trabalho `id` (`workspacev2`).
    WorkspaceMudou(i32),
}

/// A taxa que o DONO escolheu para esse monitor em `~/.config/hypr/monitores.conf` (Settings → Display).
/// É o TETO: o monitor pode anunciar modos que ele não aguenta direito (o Dell DP-2 anuncia 180 Hz,
/// mas o dono usa 144 Hz e passar disso pode deixar a tela sem imagem).
/// Retorna `None` se o monitor não tiver regra própria com `@` no `monitores.conf`.
pub fn teto_do_dono(nome: &str) -> Option<f64> {
    let home = env::var("HOME").unwrap_or_else(|_| "/home/sapista".to_string());
    let caminho = PathBuf::from(home).join(".config/hypr/monitores.conf");
    let txt = fs::read_to_string(caminho).ok()?;
    let prefixo = format!("monitor={nome},");
    for l in txt.lines() {
        let l = l.trim();
        if l.starts_with(&prefixo)
            && l.contains('@')
            && let Some(modo) = l.split(',').nth(1)
            && let Some((_, hz_str)) = modo.split_once('@')
        {
            let limpo = hz_str.strip_suffix("Hz").unwrap_or(hz_str).trim();
            if let Ok(hz) = limpo.parse::<f64>() {
                return Some(hz);
            }
        }
    }
    None
}

/// Grava a regra do monitor em `~/.config/hypr/taxa-atual.conf` (atômico: `.tmp` + `rename`;
/// não regrava se o conteúdo for igual), reproduzindo `servicos/telas.py::lembrar_taxa`.
/// O `hyprland.conf` lê esse arquivo logo depois do `monitores.conf`, impedindo que um `hyprctl reload`
/// volte o modo do monitor e faça a tela piscar/apagar.
pub fn lembrar_taxa(nome: &str, regra: &str) {
    let home = env::var("HOME").unwrap_or_else(|_| "/home/sapista".to_string());
    let taxa_conf = PathBuf::from(home).join(".config/hypr/taxa-atual.conf");
    let prefixo_este = format!("monitor={nome},");
    let mut linhas: Vec<String> = Vec::new();
    if let Ok(atual) = fs::read_to_string(&taxa_conf) {
        for l in atual.lines() {
            if l.starts_with("monitor=") && !l.starts_with(&prefixo_este) {
                linhas.push(l.to_string());
            }
        }
    }
    linhas.push(format!("monitor={regra}"));
    let novo = format!(
        "# Gerado pelo taxa.py (taxa de atualização em uso). Não editar.\n{}\n",
        linhas.join("\n")
    );
    if let Ok(atual) = fs::read_to_string(&taxa_conf)
        && atual == novo
    {
        return;
    }
    if let Some(dir) = taxa_conf.parent() {
        let _ = fs::create_dir_all(dir);
    }
    let tmp = taxa_conf.with_extension("conf.tmp");
    if fs::write(&tmp, &novo).is_ok() {
        let _ = fs::rename(&tmp, &taxa_conf);
    }
}

/// Dados de um monitor conectado e ativo, independentes do compositor.
#[derive(Debug, Clone)]
pub struct MonitorInfo {
    pub nome: String,
    pub largura: i32,
    pub altura: i32,
    pub x: i32,
    pub y: i32,
    pub escala: f64,
    pub taxa_atual: f64,
    pub modos_disponiveis: Vec<String>,
    pub espelho_de: Option<String>,
    pub transformacao: i32,
}

impl MonitorInfo {
    /// Extrai todas as taxas de atualização (Hz) disponíveis na resolução atual do monitor,
    /// ordenadas da menor para a maior (mesma lógica de `servicos/telas.py::taxas`).
    pub fn taxas_na_resolucao(&self) -> Vec<f64> {
        let prefixo = format!("{}x{}@", self.largura, self.altura);
        let mut taxas: Vec<f64> = Vec::new();
        for modo in &self.modos_disponiveis {
            if let Some(resto) = modo.strip_prefix(&prefixo) {
                let limpo = resto.strip_suffix("Hz").unwrap_or(resto);
                if let Ok(hz) = limpo.parse::<f64>()
                    && !taxas.iter().any(|&x| (x - hz).abs() < 0.005)
                {
                    taxas.push(hz);
                }
            }
        }
        taxas.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        taxas
    }

    /// Calcula a taxa-alvo para o modo pedido (`"max"` = maior até o teto do dono em `monitores.conf`;
    /// `"60"` = mais próxima de 60 Hz), reproduzindo exatamente `servicos/telas.py::alvo`.
    pub fn taxa_alvo(&self, modo: &str) -> f64 {
        let mut taxas = self.taxas_na_resolucao();
        if taxas.is_empty() {
            taxas.push(self.taxa_atual);
        }
        if let Some(teto) = teto_do_dono(&self.nome)
            && teto > 0.0
        {
            let ate_o_teto: Vec<f64> = taxas
                .iter()
                .copied()
                .filter(|&hz| hz <= teto + 0.5)
                .collect();
            if !ate_o_teto.is_empty() {
                taxas = ate_o_teto;
            } else if let Some(&mais_perto) = taxas.iter().min_by(|&&a, &&b| {
                (a - teto)
                    .abs()
                    .partial_cmp(&(b - teto).abs())
                    .unwrap_or(std::cmp::Ordering::Equal)
            }) {
                taxas = vec![mais_perto];
            }
        }
        if modo == "max" {
            taxas.into_iter().fold(f64::MIN, f64::max)
        } else {
            taxas
                .into_iter()
                .min_by(|a, b| {
                    (a - 60.0)
                        .abs()
                        .partial_cmp(&(b - 60.0).abs())
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
                .unwrap_or(self.taxa_atual)
        }
    }
}

/// Contrato abstrato de comunicação com o compositor da sessão.
pub trait Compositor: Send + Sync {
    /// Lista todas as janelas abertas no momento como pares `(endereco_sem_0x, classe)`.
    fn janelas_abertas(&self) -> Vec<(String, String)>;

    /// Lista todos os monitores ativos (não desativados).
    fn monitores(&self) -> Vec<MonitorInfo>;

    /// Aplica uma nova taxa de atualização (`hz`) a um monitor preservando resolução, posição,
    /// escala, espelhamento e rotação, respeitando o teto do dono e gravando `taxa-atual.conf`.
    fn definir_taxa(&self, monitor: &MonitorInfo, hz: f64) -> bool;

    /// Retorna o ID numérico da área de trabalho ativa no momento (0 se indisponível).
    fn area_ativa(&self) -> i32;

    /// Escuta continuamente o fluxo de eventos do compositor (bloqueante; rodar em thread própria)
    /// e envia cada [`EventoCompositor`] relevante através da função `enviar`.
    fn escutar_eventos(&self, enviar: Box<dyn Fn(EventoCompositor) + Send>);
}

/// Implementação de [`Compositor`] para o Hyprland usando os sockets Unix nativos
/// (`.socket.sock` para comandos/JSON e `.socket2.sock` para eventos em tempo real,
/// sem criar processos `hyprctl`).
#[derive(Default, Clone)]
pub struct HyprlandCompositor;

impl HyprlandCompositor {
    /// Descobre a pasta da instância atual do Hyprland em `$XDG_RUNTIME_DIR/hypr/<assinatura>`.
    fn pasta_socket() -> Option<PathBuf> {
        let xdg = env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| {
            let uid = unsafe { libc::getuid() };
            format!("/run/user/{uid}")
        });
        let base = PathBuf::from(xdg).join("hypr");
        if let Ok(sig) = env::var("HYPRLAND_INSTANCE_SIGNATURE")
            && !sig.is_empty()
        {
            let candidata = base.join(&sig);
            if candidata.join(".socket.sock").exists() {
                return Some(candidata);
            }
        }
        if let Ok(entradas) = fs::read_dir(&base) {
            for e in entradas.flatten() {
                let p = e.path();
                if p.join(".socket.sock").exists() {
                    return Some(p);
                }
            }
        }
        None
    }

    /// Envia um comando ao `.socket.sock` do Hyprland e devolve a resposta em texto.
    fn pedir(cmd: &str) -> Option<String> {
        let sock = Self::pasta_socket()?.join(".socket.sock");
        let mut stream = UnixStream::connect(sock).ok()?;
        let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
        stream.write_all(cmd.as_bytes()).ok()?;
        let mut resp = String::new();
        stream.read_to_string(&mut resp).ok()?;
        Some(resp)
    }
}

#[derive(Deserialize)]
struct HyprClient {
    #[serde(default)]
    address: String,
    #[serde(default)]
    class: String,
}

#[derive(Deserialize)]
struct HyprMonitor {
    #[serde(default)]
    name: String,
    #[serde(default)]
    width: i32,
    #[serde(default)]
    height: i32,
    #[serde(default)]
    x: i32,
    #[serde(default)]
    y: i32,
    #[serde(default = "escala_padrao")]
    scale: f64,
    #[serde(rename = "refreshRate", default)]
    refresh_rate: f64,
    #[serde(rename = "availableModes", default)]
    available_modes: Vec<String>,
    #[serde(rename = "mirrorOf", default)]
    mirror_of: Option<String>,
    #[serde(default)]
    transform: i32,
    #[serde(default)]
    disabled: bool,
}

fn escala_padrao() -> f64 {
    1.0
}

#[derive(Deserialize)]
struct HyprWorkspace {
    #[serde(default)]
    id: i32,
}

/// Formata a escala como o `:g` do Python (ex.: `1.0` -> `"1"`, `1.25` -> `"1.25"`).
fn formatar_escala(escala: f64) -> String {
    let s = format!("{escala:.4}");
    let s = s.trim_end_matches('0').trim_end_matches('.');
    if s.is_empty() {
        "1".to_string()
    } else {
        s.to_string()
    }
}

impl Compositor for HyprlandCompositor {
    fn janelas_abertas(&self) -> Vec<(String, String)> {
        let Some(txt) = Self::pedir("j/clients") else {
            return Vec::new();
        };
        let Ok(clientes) = serde_json::from_str::<Vec<HyprClient>>(&txt) else {
            return Vec::new();
        };
        clientes
            .into_iter()
            .map(|c| {
                let end = c.address.strip_prefix("0x").unwrap_or(&c.address).to_string();
                (end, c.class)
            })
            .collect()
    }

    fn monitores(&self) -> Vec<MonitorInfo> {
        let Some(txt) = Self::pedir("j/monitors all") else {
            return Vec::new();
        };
        let Ok(lista) = serde_json::from_str::<Vec<HyprMonitor>>(&txt) else {
            return Vec::new();
        };
        lista
            .into_iter()
            .filter(|m| !m.disabled)
            .map(|m| MonitorInfo {
                nome: m.name,
                largura: m.width,
                altura: m.height,
                x: m.x,
                y: m.y,
                escala: m.scale,
                taxa_atual: m.refresh_rate,
                modos_disponiveis: m.available_modes,
                espelho_de: m.mirror_of.filter(|s| !s.is_empty() && s != "none"),
                transformacao: m.transform,
            })
            .collect()
    }

    fn definir_taxa(&self, m: &MonitorInfo, hz: f64) -> bool {
        // Trava de segurança: nunca aplicar uma taxa acima do teto configurado pelo dono no monitores.conf
        let hz = match teto_do_dono(&m.nome) {
            Some(teto) if teto > 0.0 && hz > teto + 0.5 => teto,
            _ => hz,
        };
        let mut regra = format!(
            "{},{}x{}@{:.2},{}x{},{}",
            m.nome,
            m.largura,
            m.altura,
            hz,
            m.x,
            m.y,
            formatar_escala(m.escala)
        );
        if let Some(ref espelho) = m.espelho_de {
            regra.push_str(&format!(",mirror,{espelho}"));
        }
        if m.transformacao != 0 {
            regra.push_str(&format!(",transform,{}", m.transformacao));
        }
        lembrar_taxa(&m.nome, &regra);
        Self::pedir(&format!("keyword monitor {regra}")).is_some()
    }

    fn area_ativa(&self) -> i32 {
        let Some(txt) = Self::pedir("j/activeworkspace") else {
            return 0;
        };
        serde_json::from_str::<HyprWorkspace>(&txt)
            .map(|w| w.id)
            .unwrap_or(0)
    }

    fn escutar_eventos(&self, enviar: Box<dyn Fn(EventoCompositor) + Send>) {
        loop {
            if let Some(pasta) = Self::pasta_socket()
                && let Ok(stream) = UnixStream::connect(pasta.join(".socket2.sock"))
            {
                let leitor = BufReader::new(stream);
                for linha in leitor.lines().map_while(Result::ok) {
                    let Some((nome, dados)) = linha.split_once(">>") else {
                        continue;
                    };
                    match nome {
                        "openwindow" => {
                            let mut partes = dados.splitn(4, ',');
                            let end = partes.next().unwrap_or("").trim_start_matches("0x").to_string();
                            let _ws = partes.next();
                            let classe = partes.next().unwrap_or("").to_string();
                            if !end.is_empty() {
                                enviar(EventoCompositor::JanelaAberta {
                                    endereco: end,
                                    classe,
                                });
                            }
                        }
                        "closewindow" => {
                            let end = dados.trim().trim_start_matches("0x").to_string();
                            if !end.is_empty() {
                                enviar(EventoCompositor::JanelaFechada { endereco: end });
                            }
                        }
                        "configreloaded" | "monitoradded" | "monitoraddedv2" => {
                            enviar(EventoCompositor::MonitoresOuConfigMudaram);
                        }
                        "workspacev2" => {
                            if let Some(id_str) = dados.split(',').next()
                                && let Ok(id) = id_str.trim().parse::<i32>()
                            {
                                enviar(EventoCompositor::WorkspaceMudou(id));
                            }
                        }
                        _ => {}
                    }
                }
            }
            thread::sleep(Duration::from_secs(2));
        }
    }
}

/// Inicia uma thread dedicada para escutar eventos do compositor e enviá-los ao canal principal.
pub fn iniciar_escuta<M, F>(compositor: &'static dyn Compositor, tx: Sender<M>, mapear: F)
where
    M: Send + 'static,
    F: Fn(EventoCompositor) -> M + Send + 'static,
{
    thread::spawn(move || {
        compositor.escutar_eventos(Box::new(move |ev| {
            let _ = tx.send(mapear(ev));
        }));
    });
}

/// Implementação de [`Compositor`] para o Daimon usando o socket IPC nativo
/// (`$DAIMON_SOCKET` ou `$XDG_RUNTIME_DIR/daimon/$WAYLAND_DISPLAY.sock`).
#[derive(Default, Clone)]
pub struct DaimonCompositor;

impl DaimonCompositor {
    /// Descobre o caminho do socket do Daimon:
    /// 1. $DAIMON_SOCKET se definido e existente;
    /// 2. $XDG_RUNTIME_DIR/daimon/$WAYLAND_DISPLAY.sock se existente.
    pub fn socket() -> Option<PathBuf> {
        if let Some(s) = env::var_os("DAIMON_SOCKET").filter(|s| !s.is_empty()) {
            let p = PathBuf::from(s);
            if p.exists() {
                return Some(p);
            }
        }
        let xdg = env::var_os("XDG_RUNTIME_DIR")?;
        let display = env::var_os("WAYLAND_DISPLAY")?;
        let p = PathBuf::from(xdg).join("daimon").join(format!("{}.sock", display.to_string_lossy()));
        if p.exists() {
            Some(p)
        } else {
            None
        }
    }

    /// Envia um comando JSON ao socket do Daimon e devolve a resposta JSON.
    fn pedir(cmd_json: &serde_json::Value) -> Option<serde_json::Value> {
        let sock = Self::socket()?;
        let mut stream = UnixStream::connect(sock).ok()?;
        let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
        let mut linha = cmd_json.to_string();
        linha.push('\n');
        stream.write_all(linha.as_bytes()).ok()?;
        let mut leitor = BufReader::new(stream);
        let mut resp = String::new();
        leitor.read_line(&mut resp).ok()?;
        serde_json::from_str(&resp).ok()
    }
}

impl Compositor for DaimonCompositor {
    fn janelas_abertas(&self) -> Vec<(String, String)> {
        let Some(resp) = Self::pedir(&serde_json::json!({"cmd": "windows"})) else {
            return Vec::new();
        };
        let Some(data) = resp.get("data").and_then(|v| v.as_array()) else {
            return Vec::new();
        };
        data.iter()
            .filter_map(|w| {
                let id = w.get("id")?;
                let app_id = w.get("app_id").and_then(|v| v.as_str()).unwrap_or_default();
                let endereco = if let Some(n) = id.as_u64() {
                    format!("{n}")
                } else {
                    id.as_str()?.to_string()
                };
                Some((endereco, app_id.to_string()))
            })
            .collect()
    }

    fn monitores(&self) -> Vec<MonitorInfo> {
        let Some(resp) = Self::pedir(&serde_json::json!({"cmd": "outputs"})) else {
            return Vec::new();
        };
        let Some(data) = resp.get("data").and_then(|v| v.as_array()) else {
            return Vec::new();
        };
        data.iter()
            .filter_map(|m| {
                let nome = m.get("name").and_then(|v| v.as_str())?.to_string();
                let largura = m.get("w").and_then(|v| v.as_i64()).unwrap_or(1920) as i32;
                let altura = m.get("h").and_then(|v| v.as_i64()).unwrap_or(1080) as i32;
                let x = m.get("x").and_then(|v| v.as_i64()).unwrap_or(0) as i32;
                let y = m.get("y").and_then(|v| v.as_i64()).unwrap_or(0) as i32;
                let escala = m.get("scale").and_then(|v| v.as_f64()).unwrap_or(1.0);
                let refresh_mhz = m.get("refresh_mhz").and_then(|v| v.as_i64()).unwrap_or(60000);
                let taxa_atual = (refresh_mhz as f64) / 1000.0;
                let mut modos_disponiveis = Vec::new();
                if let Some(arr) = m.get("available_modes").and_then(|v| v.as_array()) {
                    for modo in arr {
                        if let Some(s) = modo.as_str() {
                            modos_disponiveis.push(s.to_string());
                        }
                    }
                }
                if modos_disponiveis.is_empty() {
                    modos_disponiveis.push(format!("{}x{}@{:.2}Hz", largura, altura, taxa_atual));
                }
                Some(MonitorInfo {
                    nome,
                    largura,
                    altura,
                    x,
                    y,
                    escala,
                    taxa_atual,
                    modos_disponiveis,
                    espelho_de: None,
                    transformacao: 0,
                })
            })
            .collect()
    }

    fn definir_taxa(&self, m: &MonitorInfo, hz: f64) -> bool {
        let hz = match teto_do_dono(&m.nome) {
            Some(teto) if teto > 0.0 && hz > teto + 0.5 => teto,
            _ => hz,
        };
        let regra = format!(
            "{},{}x{}@{:.2},{}x{},{}",
            m.nome,
            m.largura,
            m.altura,
            hz,
            m.x,
            m.y,
            formatar_escala(m.escala)
        );
        lembrar_taxa(&m.nome, &regra);
        true
    }

    fn area_ativa(&self) -> i32 {
        let Some(resp) = Self::pedir(&serde_json::json!({"cmd": "focused-workspace"})) else {
            return 1;
        };
        resp.get("data")
            .and_then(|d| d.get("id"))
            .and_then(|id| id.as_i64())
            .unwrap_or(1) as i32
    }

    fn escutar_eventos(&self, enviar: Box<dyn Fn(EventoCompositor) + Send>) {
        loop {
            if let Some(caminho) = Self::socket()
                && let Ok(mut stream) = UnixStream::connect(caminho)
            {
                let cmd = serde_json::json!({"cmd": "subscribe"}).to_string() + "\n";
                if stream.write_all(cmd.as_bytes()).is_ok() {
                    let leitor = BufReader::new(stream);
                    for linha in leitor.lines().map_while(Result::ok) {
                        if let Ok(ev) = serde_json::from_str::<serde_json::Value>(&linha) {
                            let tipo = ev.get("event").and_then(|v| v.as_str()).unwrap_or_default();
                            match tipo {
                                "window_opened" => {
                                    let id = ev.get("id").and_then(|v| v.as_u64()).map(|n| format!("{n}")).unwrap_or_default();
                                    let classe = ev.get("app_id").and_then(|v| v.as_str()).unwrap_or_default().to_string();
                                    if !id.is_empty() {
                                        enviar(EventoCompositor::JanelaAberta { endereco: id, classe });
                                    }
                                }
                                "window_closed" => {
                                    let id = ev.get("id").and_then(|v| v.as_u64()).map(|n| format!("{n}")).unwrap_or_default();
                                    if !id.is_empty() {
                                        enviar(EventoCompositor::JanelaFechada { endereco: id });
                                    }
                                }
                                "output_added" | "output_removed" | "output_mode" | "config_changed" => {
                                    enviar(EventoCompositor::MonitoresOuConfigMudaram);
                                }
                                "workspace_changed" => {
                                    if let Some(to) = ev.get("to").and_then(|v| v.as_i64()) {
                                        enviar(EventoCompositor::WorkspaceMudou(to as i32));
                                    }
                                }
                                _ => {}
                            }
                        }
                    }
                }
            }
            thread::sleep(Duration::from_secs(2));
        }
    }
}

/// Escolhe o compositor ativo na sessão:
/// Se o socket do Daimon estiver disponível (DAIMON_SOCKET ou socket por WAYLAND_DISPLAY), usa `DaimonCompositor`.
/// Caso contrário, usa `HyprlandCompositor`.
pub fn obter_compositor() -> &'static dyn Compositor {
    static DAIMON: DaimonCompositor = DaimonCompositor;
    static HYPRLAND: HyprlandCompositor = HyprlandCompositor;

    if DaimonCompositor::socket().is_some() {
        &DAIMON
    } else {
        &HYPRLAND
    }
}
