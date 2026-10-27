//! `aero-sessiond` — daemon unificado de sessão sem tela do Aero OS.
//!
//! Absorve três ajudantes residentes que antes rodavam como processos Python separados:
//! 1. **`bateria`** (`bateria.py` / `aero-energia`): limitador inteligente de carga (`smart`, `fixo`,
//!    `bypass`, `desligado`, `cheia_uma_vez`), modo de energia automático (`powerprofilesctl`) e
//!    "salvar o momento" (hibernar/suspender em bateria crítica).
//! 2. **`taxa`** (`taxa.py` / `aero-hz`): taxa de atualização adaptativa (máxima até o teto do dono
//!    em `monitores.conf` na tomada e em jogos, 60 Hz na bateria, gravando `taxa-atual.conf`),
//!    falando com o compositor exclusivamente através da trait [`Compositor`].
//! 3. **`icones`** (`icones.py --watch` / `aero-icones --watch`): troca o tema de ícones por área de
//!    trabalho (`Aero-ws<N>` vs `Aero`) e dispara `icones.py --retint` quando as cores mudam.
//!
//! 100% orientado a eventos (0% de CPU quando parado):
//! - **Netlink (`NETLINK_KOBJECT_UEVENT`)**: eventos `SUBSYSTEM=power_supply` direto do kernel.
//! - **`inotify`**: mudanças em `~/.config/aero-shell` (`energia.json`, `taxa.json`, `widgets.json`),
//!   `~/.config/hypr/painel.conf`, `~/.config/dconf/user` e `~/.cache/hypr-tema/tema`.
//! - **Socket do compositor (`trait Compositor`)**: janelas abertas/fechadas, monitores/reload e
//!   troca de área de trabalho (`workspacev2`).
//!
//! 4. **Serviço D-Bus `com.aero.Sessiond`** (`servico.rs`, documentado em `daemons/API.md`): tema
//!    claro/escuro em Rust puro (`tema.rs`, sem scripts `hypr-*`), brilho interno/externo (`brilho.rs`),
//!    modo de energia (`perfil.rs`) e ociosidade (`ocioso.rs`, inerte sem `idle_nativo` em `sessao.json`).

mod bateria;
mod brilho;
mod compositor;
mod icones;
mod ocioso;
mod perfil;
mod servico;
mod sessao;
mod taxa;
mod tema;

use compositor::{EventoCompositor, obter_compositor};
use std::env;
use std::ffi::CString;
use std::fs::{self, OpenOptions};
use std::os::unix::io::AsRawFd;
use std::path::Path;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread;
use std::time::Duration;

#[derive(Debug)]
enum EventoSessao {
    /// Mudança na fonte de alimentação ou nível da bateria vinda do kernel (uevent).
    EnergiaMudou,
    /// Arquivo `energia.json` foi modificado.
    ConfigEnergiaMudou,
    /// Arquivo `taxa.json` foi modificado.
    ConfigTaxaMudou,
    /// Arquivo `sessao.json` (serviço D-Bus: tema automático, ociosidade) foi modificado.
    ConfigSessaoMudou,
    /// Arquivos de paleta/ícones (`painel.conf`, `widgets.json`, esquema claro/escuro) mudaram.
    FontesIconesMudaram,
    /// Evento vindo do compositor (janela abriu/fechou, monitor/config, workspace).
    Compositor(EventoCompositor),
}

/// Garante que apenas uma instância residente de `taxa.py`, `bateria.py` e `icones.py --watch`
/// seja encerrada pelo PID exato lido de `/proc/<pid>/cmdline` (sem `pkill -f`).
pub fn encerrar_ajudantes_python_antigos() {
    let meu_pid = unsafe { libc::getpid() };
    let Ok(entradas) = fs::read_dir("/proc") else {
        return;
    };
    for e in entradas.flatten() {
        let Some(nome) = e.file_name().to_str().map(str::to_string) else {
            continue;
        };
        let Ok(pid) = nome.parse::<libc::pid_t>() else {
            continue;
        };
        if pid <= 1 || pid == meu_pid {
            continue;
        }
        let Ok(cmdline) = fs::read(e.path().join("cmdline")) else {
            continue;
        };
        let args: Vec<&str> = cmdline
            .split(|&b| b == 0)
            .filter(|s| !s.is_empty())
            .filter_map(|s| std::str::from_utf8(s).ok())
            .collect();
        let Some(&prog) = args.first() else {
            continue;
        };
        if !prog.contains("python") {
            continue;
        }
        // Ignora chamadas rápidas de linha de comando (--status, --waybar, --alternar, --cheia)
        if args
            .iter()
            .any(|a| matches!(*a, "--status" | "--waybar" | "--alternar" | "--cheia"))
        {
            continue;
        }
        let eh_ajudante_residente = args
            .iter()
            .any(|a| a.ends_with("/aero-shell/taxa.py") || a.ends_with("/aero-shell/bateria.py"))
            || (args.iter().any(|a| a.ends_with("/aero-shell/icones.py"))
                && args.contains(&"--watch"));
        if eh_ajudante_residente {
            unsafe {
                libc::kill(pid, libc::SIGTERM);
            }
        }
    }
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

/// Usa `inotify` do kernel para vigiar arquivos de configuração e tema sem polling.
fn iniciar_escuta_inotify(tx: Sender<EventoSessao>) {
    thread::spawn(move || unsafe {
        let fd = libc::inotify_init1(libc::IN_CLOEXEC);
        if fd < 0 {
            return;
        }
        let home = env::var("HOME").unwrap_or_else(|_| "/home/sapista".to_string());
        let pasta_aero = format!("{home}/.config/aero-shell");
        let pasta_hypr = format!("{home}/.config/hypr");
        let pasta_dconf = format!("{home}/.config/dconf");
        let pasta_tema = format!("{home}/.cache/hypr-tema");

        let _ = fs::create_dir_all(&pasta_aero);
        let _ = fs::create_dir_all(&pasta_hypr);
        let _ = fs::create_dir_all(&pasta_dconf);
        let _ = fs::create_dir_all(&pasta_tema);

        let adicionar_watch = |caminho: &str| -> libc::c_int {
            let Ok(cstr) = CString::new(caminho) else {
                return -1;
            };
            libc::inotify_add_watch(
                fd,
                cstr.as_ptr(),
                libc::IN_CLOSE_WRITE
                    | libc::IN_MOVED_TO
                    | libc::IN_CREATE
                    | libc::IN_MODIFY
                    | libc::IN_DELETE,
            )
        };

        let wd_aero = adicionar_watch(&pasta_aero);
        let wd_hypr = adicionar_watch(&pasta_hypr);
        let wd_dconf = adicionar_watch(&pasta_dconf);
        let wd_tema = adicionar_watch(&pasta_tema);

        if wd_aero < 0 && wd_hypr < 0 {
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
            let mut viu_tomada_sim = false;
            let mut viu_taxa = false;
            let mut viu_sessao = false;
            let mut viu_icones = false;

            while pos + std::mem::size_of::<libc::inotify_event>() <= total {
                let ev = &*(buf.as_ptr().add(pos) as *const libc::inotify_event);
                let len = ev.len as usize;
                let nome_inicio = pos + std::mem::size_of::<libc::inotify_event>();
                if nome_inicio + len <= total && len > 0 {
                    let bytes = &buf[nome_inicio..nome_inicio + len];
                    let fim = bytes.iter().position(|&b| b == 0).unwrap_or(bytes.len());
                    if let Ok(nome) = std::str::from_utf8(&bytes[..fim]) {
                        if ev.wd == wd_aero {
                            match nome {
                                "energia.json" => viu_energia = true,
                                ".simulacao_tomada" => viu_tomada_sim = true,
                                "taxa.json" => viu_taxa = true,
                                "sessao.json" => viu_sessao = true,
                                "widgets.json" => viu_icones = true,
                                _ => {}
                            }
                        } else if (ev.wd == wd_hypr && nome == "painel.conf")
                            || (ev.wd == wd_dconf && nome == "user")
                            || (ev.wd == wd_tema && nome == "tema")
                        {
                            viu_icones = true;
                        }
                    }
                }
                pos = nome_inicio + len;
            }

            if viu_tomada_sim {
                let _ = tx.send(EventoSessao::EnergiaMudou);
            }
            if viu_energia {
                let _ = tx.send(EventoSessao::ConfigEnergiaMudou);
            }
            if viu_taxa {
                let _ = tx.send(EventoSessao::ConfigTaxaMudou);
            }
            if viu_sessao {
                let _ = tx.send(EventoSessao::ConfigSessaoMudou);
            }
            if viu_icones {
                let _ = tx.send(EventoSessao::FontesIconesMudaram);
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
    // Trava exclusiva para garantir que apenas UMA instância do aero-sessiond rode por vez
    let xdg = env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| "/tmp".to_string());
    let lock_path = format!("{xdg}/aero-sessiond.lock");
    let Ok(lock_file) = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(&lock_path)
    else {
        return;
    };
    let rc = unsafe { libc::flock(lock_file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc != 0 {
        return;
    }

    // No modo Rust, garante que nenhum taxa.py, bateria.py ou icones.py --watch fique rodando em paralelo
    encerrar_ajudantes_python_antigos();

    let comp = obter_compositor();
    let (tx, rx): (Sender<EventoSessao>, Receiver<EventoSessao>) = mpsc::channel();

    let janelas_iniciais = comp.janelas_abertas();
    let mut bat = bateria::EstadoBateria::novo(&janelas_iniciais);
    let mut hz = taxa::EstadoTaxa::novo(&janelas_iniciais);
    let mut icn = icones::EstadoIcones::novo();

    // Primeira aplicação logo ao iniciar
    bat.passo();
    bateria::atualizar_barra();
    hz.aplicar(comp);

    iniciar_escuta_uevent(tx.clone());
    iniciar_escuta_inotify(tx.clone());
    compositor::iniciar_escuta(comp, tx, EventoSessao::Compositor);

    // Serviço D-Bus com.aero.Sessiond (tema, brilho, modo de energia, ociosidade)
    let servico_dbus = servico::iniciar(false);

    loop {
        let prazo = menor_prazo(
            menor_prazo(bat.proximo_prazo(), hz.proximo_prazo()),
            icn.proximo_prazo(),
        );
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
                hz.verificar_prazo(comp);
                icn.verificar_prazo(comp);
            }
            Some(EventoSessao::EnergiaMudou) => {
                bat.passo();
                bateria::atualizar_barra();
                hz.ao_mudar_energia(comp);
            }
            Some(EventoSessao::ConfigEnergiaMudou) => {
                bat.passo();
                bateria::atualizar_barra();
            }
            Some(EventoSessao::ConfigTaxaMudou) => {
                let janelas = comp.janelas_abertas();
                bat.recarregar_padroes_jogo(&janelas);
                bat.passo();
                hz.recarregar(comp, &janelas);
            }
            Some(EventoSessao::ConfigSessaoMudou) => {
                if let Some(s) = &servico_dbus {
                    s.recarregar();
                }
            }
            Some(EventoSessao::FontesIconesMudaram) => {
                icn.invalidar_cache_tema();
                icn.agendar_conferencia();
            }
            Some(EventoSessao::Compositor(ev_comp)) => match ev_comp {
                EventoCompositor::JanelaAberta { endereco, classe } => {
                    if bat.ao_abrir_janela(&endereco, &classe) {
                        bat.passo();
                    }
                    hz.ao_abrir_janela(comp, &endereco, &classe);
                }
                EventoCompositor::JanelaFechada { endereco } => {
                    if bat.ao_fechar_janela(&endereco) {
                        bat.passo();
                    }
                    hz.ao_fechar_janela(comp, &endereco);
                }
                EventoCompositor::MonitoresOuConfigMudaram => {
                    hz.agendar_reaplicacao_pos_reload();
                }
                EventoCompositor::WorkspaceMudou(area) => {
                    icn.ao_mudar_workspace(area);
                }
            },
        }
    }
}

fn cmd_somente_teste() {
    let comp = obter_compositor();
    let nome_comp = if compositor::DaimonCompositor::socket().is_some() {
        "Daimon"
    } else {
        "Hyprland"
    };
    println!("=== aero-sessiond (modo teste) ===");
    println!("Compositor detectado: {nome_comp}");
    let area = comp.area_ativa();
    println!("Área de trabalho ativa: {area}");

    let janelas = comp.janelas_abertas();
    println!("Janelas abertas ({}):", janelas.len());
    for (id, classe) in &janelas {
        println!("  - id={id} classe={classe}");
    }

    let monitores = comp.monitores();
    println!("Monitores detectados ({}):", monitores.len());
    for m in &monitores {
        println!(
            "  - {} {}x{} @ {:.2} Hz (pos: {}x{}, escala: {:.2})",
            m.nome, m.largura, m.altura, m.taxa_atual, m.x, m.y, m.escala
        );
        println!("    Modos disponíveis: {:?}", m.modos_disponiveis);
    }
}

/// Só o serviço D-Bus, como `com.aero.Sessiond.Teste` (para testar ao lado do daemon real).
fn cmd_dbus_teste() {
    let Some(_srv) = servico::iniciar(true) else {
        eprintln!("aero-sessiond: sem barramento de sessão");
        std::process::exit(1);
    };
    loop {
        thread::park();
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

    let comp = obter_compositor();
    match arg1 {
        "--somente-teste" => cmd_somente_teste(),
        "--dbus-teste" => cmd_dbus_teste(),
        "--waybar" => bateria::cmd_waybar(),
        "--alternar" => bateria::cmd_alternar(),
        "--cheia" => bateria::cmd_cheia(),
        "--hz-status" => taxa::cmd_status(comp),
        "--encerrar-python" => encerrar_ajudantes_python_antigos(),
        "--status" if prog == "aero-hz" => taxa::cmd_status(comp),
        "--status" | "--battery-status" => bateria::cmd_status(),
        _ => rodar_daemon(),
    }
}
