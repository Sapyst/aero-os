//! `aero-sessiond` — daemon unificado de sessão sem tela do Aero OS.
//!
//! Em vez de manter processos Python separados acordando a cada 2–3 segundos em `sleep`,
//! o `aero-sessiond` concentra os ajudantes de sessão num único binário nativo em Rust,
//! 100% orientado a eventos (0% de CPU quando parado):
//! - **Netlink (`NETLINK_KOBJECT_UEVENT`)**: o próprio kernel Linux avisa no instante em que
//!   o carregador é ligado/desligado ou o nível da bateria muda (`SUBSYSTEM=power_supply`).
//! - **`inotify`**: o kernel avisa quando `~/.config/aero-shell/energia.json` ou `taxa.json`
//!   são salvos pelo Settings ou pelos atalhos da barra.
//! - **Socket do compositor (`trait Compositor`)**: recebe aberturas e fechamentos de janelas
//!   (para detectar jogos em tempo real) e eventos de monitor/configuração.

mod bateria;
mod compositor;
mod taxa;

use compositor::{Compositor, EventoCompositor, HyprlandCompositor};
use std::env;
use std::ffi::CString;
use std::fs;
use std::path::Path;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread;
use std::time::Duration;

static COMPOSITOR: HyprlandCompositor = HyprlandCompositor;

#[derive(Debug)]
enum EventoSessao {
    /// Mudança na fonte de alimentação ou nível da bateria vinda do kernel (uevent).
    EnergiaMudou,
    /// Arquivo `energia.json` foi modificado.
    ConfigEnergiaMudou,
    /// Arquivo `taxa.json` foi modificado.
    ConfigTaxaMudou,
    /// Evento vindo do compositor (janela abriu/fechou, monitor/config, workspace).
    Compositor(EventoCompositor),
}

/// Abre um socket `AF_NETLINK` (`NETLINK_KOBJECT_UEVENT`) para receber eventos `power_supply`
/// direto do kernel sem precisar de root e sem gastar CPU com polling.
fn iniciar_escuta_uevent(tx: Sender<EventoSessao>) {
    thread::spawn(move || unsafe {
        let fd = libc::socket(
            libc::AF_NETLINK,
            libc::SOCK_DGRAM | libc::SOCK_CLOEXEC,
            libc::NETLINK_KOBJECT_UEVENT,
        );
        if fd < 0 {
            return;
        }
        let mut addr: libc::sockaddr_nl = std::mem::zeroed();
        addr.nl_family = libc::AF_NETLINK as libc::sa_family_t;
        addr.nl_pid = 0;
        addr.nl_groups = 1; // Grupo multicast 1 = eventos uevent do kernel
        if libc::bind(
            fd,
            &addr as *const libc::sockaddr_nl as *const libc::sockaddr,
            std::mem::size_of::<libc::sockaddr_nl>() as libc::socklen_t,
        ) < 0
        {
            libc::close(fd);
            return;
        }
        let mut buf = [0u8; 4096];
        loop {
            let n = libc::recv(fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len(), 0);
            if n <= 0 {
                thread::sleep(Duration::from_secs(2));
                continue;
            }
            let fatia = &buf[..n as usize];
            if fatia
                .split(|&b| b == 0)
                .any(|campo| campo == b"SUBSYSTEM=power_supply")
            {
                let _ = tx.send(EventoSessao::EnergiaMudou);
            }
        }
    });
}

/// Usa `inotify` do kernel para vigiar `~/.config/aero-shell` (`energia.json` e `taxa.json`).
fn iniciar_escuta_inotify(tx: Sender<EventoSessao>) {
    thread::spawn(move || unsafe {
        let fd = libc::inotify_init1(libc::IN_CLOEXEC);
        if fd < 0 {
            return;
        }
        let home = env::var("HOME").unwrap_or_else(|_| "/home/sapista".to_string());
        let pasta_aero = format!("{home}/.config/aero-shell");
        let _ = fs::create_dir_all(&pasta_aero);

        let adicionar_watch = |caminho: &str| -> libc::c_int {
            let Ok(cstr) = CString::new(caminho) else {
                return -1;
            };
            libc::inotify_add_watch(
                fd,
                cstr.as_ptr(),
                libc::IN_CLOSE_WRITE | libc::IN_MOVED_TO | libc::IN_CREATE | libc::IN_MODIFY,
            )
        };

        let wd_aero = adicionar_watch(&pasta_aero);
        if wd_aero < 0 {
            libc::close(fd);
            return;
        }

        let mut buf = [0u8; 4096];
        loop {
            let n = libc::read(fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len());
            if n <= 0 {
                break;
            }
            let mut pos = 0usize;
            let total = n as usize;
            let mut viu_energia = false;
            let mut viu_taxa = false;

            while pos + std::mem::size_of::<libc::inotify_event>() <= total {
                let ev = &*(buf.as_ptr().add(pos) as *const libc::inotify_event);
                let len = ev.len as usize;
                let nome_inicio = pos + std::mem::size_of::<libc::inotify_event>();
                if nome_inicio + len <= total && len > 0 {
                    let bytes = &buf[nome_inicio..nome_inicio + len];
                    let fim = bytes.iter().position(|&b| b == 0).unwrap_or(bytes.len());
                    if let Ok(nome) = std::str::from_utf8(&bytes[..fim]) {
                        match nome {
                            "energia.json" => viu_energia = true,
                            "taxa.json" => viu_taxa = true,
                            _ => {}
                        }
                    }
                }
                pos = nome_inicio + len;
            }

            if viu_energia {
                let _ = tx.send(EventoSessao::ConfigEnergiaMudou);
            }
            if viu_taxa {
                let _ = tx.send(EventoSessao::ConfigTaxaMudou);
            }
        }
        libc::close(fd);
    });
}

fn menor_prazo(a: Option<Duration>, b: Option<Duration>) -> Option<Duration> {
    match (a, b) {
        (Some(x), Some(y)) => Some(x.min(y)),
        (Some(x), None) => Some(x),
        (None, Some(y)) => Some(y),
        (None, None) => None,
    }
}

fn rodar_daemon() {
    let (tx, rx): (Sender<EventoSessao>, Receiver<EventoSessao>) = mpsc::channel();

    let janelas_iniciais = COMPOSITOR.janelas_abertas();
    let mut bat = bateria::EstadoBateria::novo(&janelas_iniciais);
    let mut hz = taxa::EstadoTaxa::novo(&janelas_iniciais);

    // Primeira aplicação logo ao iniciar
    bat.passo();
    bateria::atualizar_barra();
    hz.aplicar(&COMPOSITOR);

    iniciar_escuta_uevent(tx.clone());
    iniciar_escuta_inotify(tx.clone());
    compositor::iniciar_escuta(&COMPOSITOR, tx, EventoSessao::Compositor);

    loop {
        let prazo = menor_prazo(bat.proximo_prazo(), hz.proximo_prazo());
        let ev = match prazo {
            Some(p) => match rx.recv_timeout(p) {
                Ok(e) => Some(e),
                Err(RecvTimeoutError::Timeout) => None,
                Err(RecvTimeoutError::Disconnected) => break,
            },
            None => match rx.recv() {
                Ok(e) => Some(e),
                Err(_) => break,
            },
        };

        match ev {
            None => {
                bat.passo();
                bateria::atualizar_barra();
                hz.verificar_prazo(&COMPOSITOR);
            }
            Some(EventoSessao::EnergiaMudou) => {
                bat.passo();
                bateria::atualizar_barra();
                hz.ao_mudar_energia(&COMPOSITOR);
            }
            Some(EventoSessao::ConfigEnergiaMudou) => {
                bat.passo();
                bateria::atualizar_barra();
            }
            Some(EventoSessao::ConfigTaxaMudou) => {
                let janelas = COMPOSITOR.janelas_abertas();
                bat.recarregar_padroes_jogo(&janelas);
                bat.passo();
                hz.recarregar(&COMPOSITOR, &janelas);
            }
            Some(EventoSessao::Compositor(ev_comp)) => match ev_comp {
                EventoCompositor::JanelaAberta { endereco, classe } => {
                    if bat.ao_abrir_janela(&endereco, &classe) {
                        bat.passo();
                    }
                    hz.ao_abrir_janela(&COMPOSITOR, &endereco, &classe);
                }
                EventoCompositor::JanelaFechada { endereco } => {
                    if bat.ao_fechar_janela(&endereco) {
                        bat.passo();
                    }
                    hz.ao_fechar_janela(&COMPOSITOR, &endereco);
                }
                EventoCompositor::MonitoresOuConfigMudaram => {
                    hz.agendar_reaplicacao_pos_reload();
                }
                EventoCompositor::WorkspaceMudou(_) => {
                    // Será utilizado no passo 3 (ícones)
                }
            },
        }
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let arg1 = args.get(1).map(String::as_str).unwrap_or("");
    let prog = args
        .first()
        .and_then(|s| Path::new(s).file_name())
        .and_then(|s| s.to_str())
        .unwrap_or("aero-sessiond");

    match arg1 {
        "--waybar" => bateria::cmd_waybar(),
        "--alternar" => bateria::cmd_alternar(),
        "--cheia" => bateria::cmd_cheia(),
        "--hz-status" => taxa::cmd_status(&COMPOSITOR),
        "--status" if prog == "aero-hz" => taxa::cmd_status(&COMPOSITOR),
        "--status" | "--battery-status" => bateria::cmd_status(),
        _ => rodar_daemon(),
    }
}
