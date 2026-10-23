//! aero-clipd — daemon residente de histórico da área de transferência do Aero OS.
//!
//! Substitui a combinação `wl-paste --watch clipboard-guardar` + `clipboard --daemon` (que gastava
//! ~77 MB de PSS ficando com o GTK4 aberto o dia todo).
//!
//! Como funciona:
//! 1. Conecta direto ao compositor Wayland (Hyprland hoje, Daimon amanhã) usando o protocolo
//!    padrão `ext-data-control-v1` (ou o fallback `zwlr-data-control-v1`).
//! 2. Fica dormindo no `poll` do socket Wayland (zero CPU parado). Quando o usuário copia algo,
//!    o compositor avisa quais formatos MIME o aplicativo de origem ofereceu.
//! 3. Ignora senhas (`x-kde-passwordManagerHint`), itens vazios e itens maiores que 30 MB.
//! 4. Pede os bytes pelo pipe do Wayland, calcula o hash SHA-1 e grava em
//!    `~/.local/share/aero-clipboard/` (`indice.json` + `<sha1>.txt` ou `<sha1>.bin`),
//!    exatamente no formato que o `clipboard.py` (janela visual), o `papel-de-parede` e o
//!    Settings esperam.

use std::collections::HashMap;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Sender};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use sha1_smol::Sha1;
use wayland_client::backend::ObjectId;
use wayland_client::protocol::wl_registry::{self, WlRegistry};
use wayland_client::protocol::wl_seat::{self, WlSeat};
use wayland_client::{Connection, Dispatch, Proxy, QueueHandle};
use wayland_protocols::ext::data_control::v1::client::{
    ext_data_control_device_v1::{self, ExtDataControlDeviceV1},
    ext_data_control_manager_v1::{self, ExtDataControlManagerV1},
    ext_data_control_offer_v1::{self, ExtDataControlOfferV1},
};
use wayland_protocols_wlr::data_control::v1::client::{
    zwlr_data_control_device_v1::{self, ZwlrDataControlDeviceV1},
    zwlr_data_control_manager_v1::{self, ZwlrDataControlManagerV1},
    zwlr_data_control_offer_v1::{self, ZwlrDataControlOfferV1},
};

/// Limite máximo por item copiado (30 MB), idêntico ao `clipboard-guardar`.
const LIMITE_BYTES: usize = 30 * 1024 * 1024;

/// Tempo máximo (em ms) esperando o app de origem escrever no pipe antes de desistir.
const TIMEOUT_LEITURA_MS: i32 = 5_000;

/// Ordem de preferência dos formatos MIME: o primeiro que o app oferecer ganha.
const PREFERIDOS: &[&str] = &[
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/svg+xml",
    "text/uri-list",
    "text/plain;charset=utf-8",
    "text/plain",
    "UTF8_STRING",
    "STRING",
    "TEXT",
];

/// Tipo MIME especial usado por gerenciadores de senha (KeePassXC, Bitwarden...): nunca guardar.
const SEGREDO: &str = "x-kde-passwordManagerHint";

/// Estrutura de cada entrada em `~/.local/share/aero-clipboard/indice.json`.
#[derive(Serialize, Deserialize, Clone, Debug)]
struct ItemHistorico {
    hash: String,
    tipo: String,
    arquivo: String,
    quando: f64,
    tamanho: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    previa: Option<String>,
}

/// Qual gerenciador de data-control o compositor ofereceu (preferimos `ext`, com fallback `wlr`).
enum Gerenciador {
    Ext(ExtDataControlManagerV1),
    Wlr(ZwlrDataControlManagerV1),
}

/// Estado mantido pelo laço de eventos Wayland.
struct Estado {
    seats: Vec<(u32, WlSeat)>,
    global_ext: Option<(u32, u32)>,
    global_wlr: Option<(u32, u32)>,
    gerenciador: Option<Gerenciador>,
    /// Mapa de ofertas em construção: ObjectId da oferta -> lista de tipos MIME anunciados.
    ofertas: HashMap<ObjectId, Vec<String>>,
    /// Pipes prontos para ler assim que o `conn.flush()` enviar o pedido `receive` ao compositor.
    leituras_pendentes: Vec<(OwnedFd, String, bool)>,
    /// Na primeira seleção logo ao conectar, se o item já for o primeiro do índice,
    /// preservamos o carimbo de tempo original para não fingir que acabou de ser copiado.
    primeira_selecao: bool,
}

impl Estado {
    fn novo() -> Self {
        Self {
            seats: Vec::new(),
            global_ext: None,
            global_wlr: None,
            gerenciador: None,
            ofertas: HashMap::new(),
            leituras_pendentes: Vec::new(),
            primeira_selecao: true,
        }
    }
}

/// Escolhe o formato MIME mais útil dentre os oferecidos pelo aplicativo que copiou.
fn escolher_tipo(tipos: &[String]) -> Option<String> {
    if tipos.is_empty() || tipos.iter().any(|t| t == SEGREDO) {
        return None;
    }
    for &pref in PREFERIDOS {
        if tipos.iter().any(|t| t == pref) {
            return Some(pref.to_string());
        }
    }
    // Qualquer outro formato com nome MIME (contendo "/")
    tipos.iter().find(|t| t.contains('/')).cloned()
}

/// Cria um pipe Unix (`O_CLOEXEC`) para receber os bytes da oferta do Wayland.
fn criar_pipe() -> Option<(OwnedFd, OwnedFd)> {
    let mut fds = [0i32; 2];
    // SAFETY: `fds` tem espaço para 2 descritores e `O_CLOEXEC` evita vazar FDs para filhos.
    let rc = unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) };
    if rc < 0 {
        return None;
    }
    // SAFETY: `pipe2` retornou 0, então ambos os descritores são válidos e exclusivos.
    unsafe { Some((OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1]))) }
}

/// Lê os bytes do pipe com limite de 30 MB e timeout para não travar se um app congelar.
fn ler_pipe_com_limite(fd: OwnedFd) -> Option<Vec<u8>> {
    let raw = fd.as_raw_fd();
    // Coloca a ponta de leitura em modo não-bloqueante para usar `poll` com timeout.
    unsafe {
        let flags = libc::fcntl(raw, libc::F_GETFL);
        if flags >= 0 {
            libc::fcntl(raw, libc::F_SETFL, flags | libc::O_NONBLOCK);
        }
    }

    let mut arquivo = File::from(fd);
    let mut dados = Vec::new();
    let mut buf = [0u8; 65536];

    loop {
        let mut pfd = libc::pollfd {
            fd: arquivo.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let rc = unsafe { libc::poll(&mut pfd, 1, TIMEOUT_LEITURA_MS) };
        if rc <= 0 {
            // Timeout (0) ou erro (< 0): aborta a leitura
            return None;
        }
        match arquivo.read(&mut buf) {
            Ok(0) => break, // EOF: o app terminou de enviar
            Ok(n) => {
                dados.extend_from_slice(&buf[..n]);
                if dados.len() > LIMITE_BYTES {
                    // Passou de 30 MB: descarta imediatamente sem gastar mais RAM
                    return None;
                }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(_) => return None,
        }
    }

    Some(dados)
}

/// Caminho da pasta `~/.local/share/aero-clipboard`.
fn pasta_clipboard() -> PathBuf {
    let home = env::var("HOME").unwrap_or_else(|_| "/home/sapista".to_string());
    PathBuf::from(home).join(".local/share/aero-clipboard")
}

/// Lê `CLIP_MAX` do `~/.config/hypr/painel.conf` (ou `~/.config/aero/painel.conf`), padrão 50, mínimo 5.
fn ler_max() -> usize {
    let home = env::var("HOME").unwrap_or_else(|_| "/home/sapista".to_string());
    let candidatos = [
        PathBuf::from(&home).join(".config/hypr/painel.conf"),
        PathBuf::from(&home).join(".config/aero/painel.conf"),
    ];
    for caminho in &candidatos {
        if let Ok(conteudo) = fs::read_to_string(caminho) {
            for linha in conteudo.lines() {
                if let Some(resto) = linha.strip_prefix("CLIP_MAX=") {
                    let limpo = resto.trim().trim_matches('"');
                    if let Ok(n) = limpo.parse::<usize>() {
                        return n.max(5);
                    }
                }
            }
        }
    }
    50
}

/// Verifica se uma fatia de bytes contém apenas espaços em branco ASCII.
fn apenas_espacos(dados: &[u8]) -> bool {
    dados.iter().all(|b| b.is_ascii_whitespace())
}

/// Salva o item copiado no histórico em disco (`indice.json` + arquivo `.txt`/`.bin`).
fn guardar_item(dados: Vec<u8>, tipo: String, primeira_selecao: bool) {
    let eh_texto = tipo.starts_with("text");
    if dados.is_empty() || dados.len() > LIMITE_BYTES || (eh_texto && apenas_espacos(&dados)) {
        return;
    }

    let pasta = pasta_clipboard();
    let _ = fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&pasta);
    let _ = fs::set_permissions(&pasta, fs::Permissions::from_mode(0o700));

    let indice_caminho = pasta.join("indice.json");
    let trava_caminho = pasta.join("indice.json.trava");

    // Trava exclusiva para evitar condição de corrida entre cópias rápidas ou limpeza
    let Ok(trava) = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .mode(0o600)
        .open(&trava_caminho)
    else {
        return;
    };
    unsafe {
        libc::flock(trava.as_raw_fd(), libc::LOCK_EX);
    }

    let mut sha = Sha1::new();
    sha.update(&dados);
    let resumo = sha.digest().to_string();

    let mut itens: Vec<ItemHistorico> = fs::read_to_string(&indice_caminho)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();

    let agora = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);

    // Se acabamos de iniciar o daemon e o item atual do clipboard já é o 1º do histórico,
    // não mexemos no horário dele.
    if primeira_selecao {
        if let Some(primeiro) = itens.first() {
            if primeiro.hash == resumo && pasta.join(&primeiro.arquivo).exists() {
                return;
            }
        }
    }

    if let Some(pos) = itens.iter().position(|i| i.hash == resumo) {
        let mut antigo = itens.remove(pos);
        antigo.quando = agora;
        let caminho_arq = pasta.join(&antigo.arquivo);
        if !caminho_arq.exists() {
            let _ = escrever_arquivo_privado(&caminho_arq, &dados);
        }
        itens.insert(0, antigo);
    } else {
        let sufixo = if eh_texto { ".txt" } else { ".bin" };
        let arquivo = format!("{resumo}{sufixo}");
        let caminho_arq = pasta.join(&arquivo);
        if escrever_arquivo_privado(&caminho_arq, &dados).is_err() {
            return;
        }
        let tem_previa =
            eh_texto || matches!(tipo.as_str(), "UTF8_STRING" | "STRING" | "TEXT");
        let previa = if tem_previa {
            let limite = dados.len().min(400);
            Some(String::from_utf8_lossy(&dados[..limite]).into_owned())
        } else {
            None
        };
        itens.insert(
            0,
            ItemHistorico {
                hash: resumo,
                tipo,
                arquivo,
                quando: agora,
                tamanho: dados.len(),
                previa,
            },
        );
    }

    // Corta os itens mais antigos além de CLIP_MAX e apaga seus arquivos
    let max_itens = ler_max();
    if itens.len() > max_itens {
        for velho in itens.drain(max_itens..) {
            let _ = fs::remove_file(pasta.join(&velho.arquivo));
        }
    }

    // Gravação atômica via `.tmp` + `rename`
    let tmp_caminho = pasta.join("indice.json.tmp");
    if let Ok(json_bytes) = serde_json::to_vec(&itens) {
        if escrever_arquivo_privado(&tmp_caminho, &json_bytes).is_ok() {
            let _ = fs::rename(&tmp_caminho, &indice_caminho);
        }
    }
}

/// Escreve um arquivo com permissão `0600` (apenas o dono lê/escreve).
fn escrever_arquivo_privado(caminho: &Path, dados: &[u8]) -> std::io::Result<()> {
    let mut arq = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .mode(0o600)
        .open(caminho)?;
    arq.write_all(dados)?;
    Ok(())
}

/// Garante que só uma instância do `aero-clipd` rode por sessão.
fn adquirir_trava_instancia() -> Option<File> {
    let runtime = env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| "/tmp".to_string());
    let caminho = PathBuf::from(runtime).join("aero-clipd.lock");
    let arq = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .mode(0o600)
        .open(caminho)
        .ok()?;
    let rc = unsafe { libc::flock(arq.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc < 0 {
        None
    } else {
        Some(arq)
    }
}

// ---------------- Implementações do Dispatch Wayland ----------------

impl Dispatch<WlRegistry, ()> for Estado {
    fn event(
        estado: &mut Self,
        registry: &WlRegistry,
        event: wl_registry::Event,
        _: &(),
        _: &Connection,
        qh: &QueueHandle<Self>,
    ) {
        match event {
            wl_registry::Event::Global {
                name,
                interface,
                version,
            } => {
                if interface == WlSeat::interface().name {
                    let seat = registry.bind::<WlSeat, _, _>(name, version.min(4), qh, ());
                    if let Some(ref gerenciador) = estado.gerenciador {
                        match gerenciador {
                            Gerenciador::Ext(m) => {
                                m.get_data_device(&seat, qh, ());
                            }
                            Gerenciador::Wlr(m) => {
                                m.get_data_device(&seat, qh, ());
                            }
                        }
                    }
                    estado.seats.push((name, seat));
                } else if interface == ExtDataControlManagerV1::interface().name {
                    estado.global_ext = Some((name, version.min(1)));
                } else if interface == ZwlrDataControlManagerV1::interface().name {
                    estado.global_wlr = Some((name, version.min(2)));
                }
            }
            wl_registry::Event::GlobalRemove { name } => {
                estado.seats.retain(|(n, _)| *n != name);
            }
            _ => {}
        }
    }
}

impl Dispatch<WlSeat, ()> for Estado {
    fn event(
        _: &mut Self,
        _: &WlSeat,
        _: wl_seat::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

// ---- Protocolo padrão: ext-data-control-v1 ----

impl Dispatch<ExtDataControlManagerV1, ()> for Estado {
    fn event(
        _: &mut Self,
        _: &ExtDataControlManagerV1,
        _: ext_data_control_manager_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<ExtDataControlDeviceV1, ()> for Estado {
    fn event(
        estado: &mut Self,
        device: &ExtDataControlDeviceV1,
        event: ext_data_control_device_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        match event {
            ext_data_control_device_v1::Event::DataOffer { id } => {
                estado.ofertas.insert(id.id(), Vec::new());
            }
            ext_data_control_device_v1::Event::PrimarySelection { id: Some(oferta) } => {
                // Ignoramos a seleção primária (botão do meio) e destruímos a oferta para não vazar
                estado.ofertas.remove(&oferta.id());
                oferta.destroy();
            }
            ext_data_control_device_v1::Event::Selection { id } => {
                let primeira = std::mem::replace(&mut estado.primeira_selecao, false);
                if let Some(oferta) = id {
                    let tipos = estado.ofertas.remove(&oferta.id()).unwrap_or_default();
                    if let Some(tipo) = escolher_tipo(&tipos) {
                        if let Some((leitura, escrita)) = criar_pipe() {
                            oferta.receive(tipo.clone(), escrita.as_fd());
                            drop(escrita);
                            estado.leituras_pendentes.push((leitura, tipo, primeira));
                        }
                    }
                    oferta.destroy();
                }
            }
            ext_data_control_device_v1::Event::Finished => {
                device.destroy();
            }
            _ => {}
        }
    }

    wayland_client::event_created_child!(Estado, ExtDataControlDeviceV1, [
        ext_data_control_device_v1::EVT_DATA_OFFER_OPCODE => (ExtDataControlOfferV1, ()),
    ]);
}

impl Dispatch<ExtDataControlOfferV1, ()> for Estado {
    fn event(
        estado: &mut Self,
        oferta: &ExtDataControlOfferV1,
        event: ext_data_control_offer_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        if let ext_data_control_offer_v1::Event::Offer { mime_type } = event {
            if let Some(lista) = estado.ofertas.get_mut(&oferta.id()) {
                lista.push(mime_type);
            }
        }
    }
}

// ---- Protocolo fallback wlroots: zwlr-data-control-v1 ----

impl Dispatch<ZwlrDataControlManagerV1, ()> for Estado {
    fn event(
        _: &mut Self,
        _: &ZwlrDataControlManagerV1,
        _: zwlr_data_control_manager_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<ZwlrDataControlDeviceV1, ()> for Estado {
    fn event(
        estado: &mut Self,
        device: &ZwlrDataControlDeviceV1,
        event: zwlr_data_control_device_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        match event {
            zwlr_data_control_device_v1::Event::DataOffer { id } => {
                estado.ofertas.insert(id.id(), Vec::new());
            }
            zwlr_data_control_device_v1::Event::PrimarySelection { id: Some(oferta) } => {
                estado.ofertas.remove(&oferta.id());
                oferta.destroy();
            }
            zwlr_data_control_device_v1::Event::Selection { id } => {
                let primeira = std::mem::replace(&mut estado.primeira_selecao, false);
                if let Some(oferta) = id {
                    let tipos = estado.ofertas.remove(&oferta.id()).unwrap_or_default();
                    if let Some(tipo) = escolher_tipo(&tipos) {
                        if let Some((leitura, escrita)) = criar_pipe() {
                            oferta.receive(tipo.clone(), escrita.as_fd());
                            drop(escrita);
                            estado.leituras_pendentes.push((leitura, tipo, primeira));
                        }
                    }
                    oferta.destroy();
                }
            }
            zwlr_data_control_device_v1::Event::Finished => {
                device.destroy();
            }
            _ => {}
        }
    }

    wayland_client::event_created_child!(Estado, ZwlrDataControlDeviceV1, [
        zwlr_data_control_device_v1::EVT_DATA_OFFER_OPCODE => (ZwlrDataControlOfferV1, ()),
    ]);
}

impl Dispatch<ZwlrDataControlOfferV1, ()> for Estado {
    fn event(
        estado: &mut Self,
        oferta: &ZwlrDataControlOfferV1,
        event: zwlr_data_control_offer_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        if let zwlr_data_control_offer_v1::Event::Offer { mime_type } = event {
            if let Some(lista) = estado.ofertas.get_mut(&oferta.id()) {
                lista.push(mime_type);
            }
        }
    }
}

/// Inicia a thread trabalhadora que lê os pipes e grava no disco sem bloquear o socket Wayland.
fn iniciar_trabalhador() -> Sender<(OwnedFd, String, bool)> {
    let (tx, rx) = mpsc::channel::<(OwnedFd, String, bool)>();
    thread::Builder::new()
        .name("clipd-io".to_string())
        .spawn(move || {
            while let Ok((fd, tipo, primeira)) = rx.recv() {
                if let Some(dados) = ler_pipe_com_limite(fd) {
                    guardar_item(dados, tipo, primeira);
                }
            }
        })
        .expect("Falha ao criar thread de I/O do aero-clipd");
    tx
}

fn main() {
    // Define umask 0077 para que qualquer arquivo/pasta criado seja privado do usuário
    unsafe {
        libc::umask(0o077);
    }

    let Some(_trava_instancia) = adquirir_trava_instancia() else {
        // Outra instância do aero-clipd já está rodando
        return;
    };

    let conn = Connection::connect_to_env().expect("Não foi possível conectar ao Wayland");
    let mut event_queue = conn.new_event_queue();
    let qh = event_queue.handle();

    let display = conn.display();
    let registry = display.get_registry(&qh, ());

    let mut estado = Estado::novo();
    event_queue
        .roundtrip(&mut estado)
        .expect("Falha no roundtrip inicial do Wayland");

    // Escolhe `ext_data_control_manager_v1` se disponível; caso contrário, `zwlr_data_control_manager_v1`
    if let Some((name, version)) = estado.global_ext {
        let mgr = registry.bind::<ExtDataControlManagerV1, _, _>(name, version, &qh, ());
        for (_, seat) in &estado.seats {
            mgr.get_data_device(seat, &qh, ());
        }
        estado.gerenciador = Some(Gerenciador::Ext(mgr));
    } else if let Some((name, version)) = estado.global_wlr {
        let mgr = registry.bind::<ZwlrDataControlManagerV1, _, _>(name, version, &qh, ());
        for (_, seat) in &estado.seats {
            mgr.get_data_device(seat, &qh, ());
        }
        estado.gerenciador = Some(Gerenciador::Wlr(mgr));
    } else {
        eprintln!("aero-clipd: compositor não suporta ext-data-control-v1 nem zwlr-data-control-v1");
        std::process::exit(1);
    }

    let trabalhador = iniciar_trabalhador();

    // Faz mais um roundtrip para processar a seleção inicial ao ligar
    let _ = event_queue.roundtrip(&mut estado);
    let _ = conn.flush();
    for item in estado.leituras_pendentes.drain(..) {
        let _ = trabalhador.send(item);
    }

    // Laço principal: dorme no descritor do Wayland (0% CPU parado)
    loop {
        if event_queue.blocking_dispatch(&mut estado).is_err() {
            break;
        }
        if !estado.leituras_pendentes.is_empty() {
            let _ = conn.flush();
            for item in estado.leituras_pendentes.drain(..) {
                let _ = trabalhador.send(item);
            }
        }
    }
}
