//! Ociosidade (idle): travar, apagar as telas e suspender depois de `lock_s` / `dpms_s` / `suspend_s`
//! segundos parado (`sessao.json`).
//!
//! Dois caminhos, os dois com as mesmas ações:
//! 1. **`IdleEvent(kind)`** no D-Bus: o compositor (que já sabe quando o usuário parou) chama
//!    `lock` / `dpms` / `suspend` / `resume` e o daemon executa.
//! 2. **`ext_idle_notifier_v1`** direto no compositor: só com `idle_nativo = sim` E `WAYLAND_DISPLAY`
//!    definido E o global existindo. Um `wayland-client` mínimo numa thread que dorme em `poll` no fd
//!    do Wayland e num `eventfd` (acordado quando a config muda, para refazer os timers).
//!
//! Na sessão Hyprland este módulo fica INERTE (o padrão é `idle_nativo = false`; o hypridle cuida).

use std::os::fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd, OwnedFd};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;

use wayland_client::globals::{registry_queue_init, GlobalListContents};
use wayland_client::protocol::{wl_registry, wl_seat};
use wayland_client::{delegate_noop, Connection, Dispatch, QueueHandle};
use wayland_protocols::ext::idle_notify::v1::client::ext_idle_notification_v1::{self, ExtIdleNotificationV1};
use wayland_protocols::ext::idle_notify::v1::client::ext_idle_notifier_v1::ExtIdleNotifierV1;

use crate::compositor::DaimonCompositor;
use crate::sessao::ConfigSessao;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Acao {
    Lock,
    Dpms,
    Suspend,
}

impl Acao {
    pub fn de_texto(s: &str) -> Option<(Acao, bool)> {
        match s.trim().to_ascii_lowercase().as_str() {
            "lock" => Some((Acao::Lock, true)),
            "dpms" | "dpms-off" | "screen-off" => Some((Acao::Dpms, true)),
            "suspend" => Some((Acao::Suspend, true)),
            "resume" | "dpms-on" | "screen-on" => Some((Acao::Dpms, false)),
            _ => None,
        }
    }

    pub fn nome(self) -> &'static str {
        match self {
            Acao::Lock => "lock",
            Acao::Dpms => "dpms",
            Acao::Suspend => "suspend",
        }
    }

    /// Segundos configurados para esta ação (0 = desligada).
    pub fn prazo(self, cfg: &ConfigSessao) -> u32 {
        match self {
            Acao::Lock => cfg.lock_s,
            Acao::Dpms => cfg.dpms_s,
            Acao::Suspend => cfg.suspend_s,
        }
    }
}

fn rodar_solto(cmd: &str) -> bool {
    Command::new("sh")
        .arg("-c")
        .arg(cmd)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .is_ok()
}

/// Executa a ação (`parou = true`: o usuário ficou parado; `false`: voltou). Devolve o que foi feito.
pub fn executar(acao: Acao, parou: bool, cfg: &ConfigSessao) -> String {
    match (acao, parou) {
        (Acao::Lock, true) => {
            if cfg.locker.trim().is_empty() {
                "lock: no locker configured".to_string()
            } else if rodar_solto(&cfg.locker) {
                format!("lock: started '{}'", cfg.locker)
            } else {
                format!("lock: failed to start '{}'", cfg.locker)
            }
        }
        (Acao::Dpms, parou) => {
            let on = if parou { "off" } else { "on" };
            match DaimonCompositor::pedir(&serde_json::json!({"cmd": "output-power", "on": on})) {
                Some(resp) => format!("dpms {on}: {resp}"),
                None => format!("dpms {on}: compositor socket unavailable"),
            }
        }
        (Acao::Suspend, true) => {
            if rodar_solto("systemctl suspend") { "suspend: requested".to_string() } else { "suspend: failed".to_string() }
        }
        (Acao::Lock | Acao::Suspend, false) => format!("{}: resume (nothing to do)", acao.nome()),
    }
}

// ---------------- Cliente Wayland (ext_idle_notifier_v1) ----------------

struct Escuta {
    eventos: Vec<(Acao, bool)>,
}

impl Dispatch<wl_registry::WlRegistry, GlobalListContents> for Escuta {
    fn event(
        _: &mut Self,
        _: &wl_registry::WlRegistry,
        _: wl_registry::Event,
        _: &GlobalListContents,
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

delegate_noop!(Escuta: ignore wl_seat::WlSeat);
delegate_noop!(Escuta: ignore ExtIdleNotifierV1);

impl Dispatch<ExtIdleNotificationV1, Acao> for Escuta {
    fn event(
        st: &mut Self,
        _: &ExtIdleNotificationV1,
        ev: ext_idle_notification_v1::Event,
        acao: &Acao,
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        match ev {
            ext_idle_notification_v1::Event::Idled => st.eventos.push((*acao, true)),
            ext_idle_notification_v1::Event::Resumed => st.eventos.push((*acao, false)),
            _ => {}
        }
    }
}

/// Controle da thread de escuta: `recarregar()` acorda a thread para reler a config.
pub struct Controle {
    cfg: Arc<Mutex<ConfigSessao>>,
    acordar: OwnedFd,
}

impl Controle {
    pub fn recarregar(&self) {
        let um: u64 = 1;
        unsafe {
            libc::write(self.acordar.as_raw_fd(), &um as *const u64 as *const libc::c_void, 8);
        }
    }
}

/// Uma rodada: conecta, cria as notificações e escuta até a config mudar (ou o compositor sumir).
/// Devolve `Err` com o motivo quando não dá para escutar (sem WAYLAND_DISPLAY, sem o global...).
fn escutar(cfg: &Arc<Mutex<ConfigSessao>>, acordar: BorrowedFd<'_>) -> Result<(), String> {
    let conn = Connection::connect_to_env().map_err(|e| format!("wayland: {e}"))?;
    let (globals, mut fila) = registry_queue_init::<Escuta>(&conn).map_err(|e| format!("registry: {e}"))?;
    let qh = fila.handle();
    let seat: wl_seat::WlSeat = globals.bind(&qh, 1..=7, ()).map_err(|e| format!("wl_seat: {e}"))?;
    let notifier: ExtIdleNotifierV1 =
        globals.bind(&qh, 1..=1, ()).map_err(|_| "compositor has no ext_idle_notifier_v1".to_string())?;

    let atual = cfg.lock().unwrap().clone();
    let mut notificacoes = Vec::new();
    for acao in [Acao::Lock, Acao::Dpms, Acao::Suspend] {
        let s = acao.prazo(&atual);
        if s > 0 {
            notificacoes.push(notifier.get_idle_notification(s.saturating_mul(1000), &seat, &qh, acao));
        }
    }
    eprintln!(
        "aero-sessiond: idle nativo escutando ext_idle_notifier_v1 (lock={}s dpms={}s suspend={}s)",
        atual.lock_s, atual.dpms_s, atual.suspend_s
    );
    let mut st = Escuta { eventos: Vec::new() };
    loop {
        fila.flush().map_err(|e| format!("flush: {e}"))?;
        fila.dispatch_pending(&mut st).map_err(|e| format!("dispatch: {e}"))?;
        for (acao, parou) in st.eventos.drain(..) {
            let c = cfg.lock().unwrap().clone();
            eprintln!("aero-sessiond: idle {} ({}) → {}", acao.nome(), if parou { "idled" } else { "resumed" }, executar(acao, parou, &c));
        }
        let Some(guarda) = fila.prepare_read() else { continue };
        let mut fds = [
            libc::pollfd { fd: guarda.connection_fd().as_raw_fd(), events: libc::POLLIN, revents: 0 },
            libc::pollfd { fd: acordar.as_raw_fd(), events: libc::POLLIN, revents: 0 },
        ];
        let n = unsafe { libc::poll(fds.as_mut_ptr(), 2, -1) };
        if n < 0 {
            return Err("poll failed".into());
        }
        if fds[1].revents & libc::POLLIN != 0 {
            let mut lixo = 0u64;
            unsafe {
                libc::read(acordar.as_raw_fd(), &mut lixo as *mut u64 as *mut libc::c_void, 8);
            }
            for nt in notificacoes.drain(..) {
                nt.destroy();
            }
            let _ = fila.flush();
            return Ok(()); // a config mudou: quem chamou refaz a rodada
        }
        if fds[0].revents & libc::POLLIN != 0 {
            guarda.read().map_err(|e| format!("read: {e}"))?;
        } else if fds[0].revents & (libc::POLLHUP | libc::POLLERR) != 0 {
            return Err("compositor went away".into());
        }
    }
}

/// Sobe a thread de escuta (inerte enquanto `idle_nativo` for falso ou não houver Wayland).
pub fn iniciar(cfg: Arc<Mutex<ConfigSessao>>) -> Arc<Controle> {
    let fd = unsafe { libc::eventfd(0, libc::EFD_CLOEXEC | libc::EFD_NONBLOCK) };
    let acordar = unsafe { OwnedFd::from_raw_fd(fd.max(0)) };
    let controle = Arc::new(Controle { cfg: Arc::clone(&cfg), acordar });
    if fd < 0 {
        return controle;
    }
    let ctl = Arc::clone(&controle);
    thread::Builder::new()
        .name("sessao-ocioso".to_string())
        .spawn(move || {
            loop {
                let ligado = ctl.cfg.lock().unwrap().idle_nativo && std::env::var_os("WAYLAND_DISPLAY").is_some();
                if ligado {
                    match escutar(&ctl.cfg, ctl.acordar.as_fd()) {
                        Ok(()) => continue, // config mudou: refaz já
                        Err(e) => eprintln!("aero-sessiond: idle nativo desligado ({e})"),
                    }
                }
                // inerte: dorme até alguém chamar recarregar() (ou, se deu erro, tenta de novo em 30 s)
                let mut fds = [libc::pollfd { fd: ctl.acordar.as_raw_fd(), events: libc::POLLIN, revents: 0 }];
                let prazo = if ligado { 30_000 } else { -1 };
                unsafe {
                    libc::poll(fds.as_mut_ptr(), 1, prazo);
                }
                let mut lixo = 0u64;
                unsafe {
                    libc::read(ctl.acordar.as_raw_fd(), &mut lixo as *mut u64 as *mut libc::c_void, 8);
                }
            }
        })
        .ok();
    controle
}

#[cfg(test)]
mod testes {
    use super::*;

    #[test]
    fn nomes_de_evento() {
        assert_eq!(Acao::de_texto("lock"), Some((Acao::Lock, true)));
        assert_eq!(Acao::de_texto("DPMS"), Some((Acao::Dpms, true)));
        assert_eq!(Acao::de_texto("resume"), Some((Acao::Dpms, false)));
        assert_eq!(Acao::de_texto("suspend"), Some((Acao::Suspend, true)));
        assert_eq!(Acao::de_texto("x"), None);
    }

    #[test]
    fn lock_sem_locker_nao_roda_nada() {
        let cfg = ConfigSessao::default();
        assert_eq!(executar(Acao::Lock, true, &cfg), "lock: no locker configured");
        assert_eq!(Acao::Lock.prazo(&cfg), 300);
        assert!(executar(Acao::Suspend, false, &cfg).contains("resume"));
    }
}
