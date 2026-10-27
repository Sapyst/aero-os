//! Serviço D-Bus `com.aero.Sessiond` em `/com/aero/Sessiond` (documentado em `daemons/API.md`):
//! tema (`GetTheme`/`SetTheme`), brilho (`GetBrightness`/`SetBrightness`), modo de energia
//! (`GetPowerProfile`/`SetPowerProfile`), ociosidade (`IdleEvent`, `GetIdleConfig`) e os sinais
//! `ThemeChanged`, `BrightnessChanged`, `PowerProfileChanged`.
//!
//! Todo setter devolve o estado RELIDO da fonte da verdade (arquivo de tema, sysfs/ddcutil, serviço
//! de energia) e nunca segura o chamador mais de [`PRAZO_SETTER`]: o trabalho roda numa thread e, se
//! passar do prazo, a resposta vem com `pending` e o sinal traz o estado final.
//!
//! `--dbus-teste` sobe só este serviço, como `com.aero.Sessiond.Teste`, para testes ao lado do real.

use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{json, Value as JsonValue};
use zbus::blocking::Connection as DbusConn;
use zbus::fdo::RequestNameFlags;
use zbus::interface;
use zbus::names::WellKnownName;
use zbus::object_server::SignalEmitter;

use crate::ocioso::{self, Acao};
use crate::sessao::{self, ConfigSessao};
use crate::{brilho, perfil, tema};

pub const NOME: &str = "com.aero.Sessiond";
pub const CAMINHO: &str = "/com/aero/Sessiond";
pub const PRAZO_SETTER: Duration = Duration::from_millis(1800);

/// Roda `f` numa thread e espera no máximo [`PRAZO_SETTER`]; `None` = ainda rodando.
pub fn com_prazo<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Option<T> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let _ = tx.send(f());
    });
    rx.recv_timeout(PRAZO_SETTER).ok()
}

fn emitir(conn: &DbusConn, sinal: &str, payload: &str) {
    let _ = conn.emit_signal(None::<&str>, CAMINHO, NOME, sinal, &(payload,));
}

pub struct Servico {
    cfg: Arc<Mutex<ConfigSessao>>,
    sessao: DbusConn,
    sistema: Option<DbusConn>,
}

impl Servico {
    fn sistema(&self) -> Result<DbusConn, String> {
        self.sistema.clone().ok_or_else(|| "no system bus".to_string())
    }
}

#[interface(name = "com.aero.Sessiond")]
impl Servico {
    // ---------- Tema ----------

    /// `"dark"` ou `"light"`, lido de `~/.cache/hypr-tema/tema`.
    #[zbus(name = "GetTheme")]
    fn get_theme(&self) -> String {
        tema::nome_api(&tema::atual()).to_string()
    }

    /// Aplica `"dark"`/`"light"` e devolve o que ficou gravado (ou `"error: ..."`).
    #[zbus(name = "SetTheme")]
    fn set_theme(&self, theme: String) -> String {
        let Some(interno) = tema::normalizar(&theme) else {
            return format!("error: invalid theme '{theme}' (dark|light)");
        };
        let sessao = self.sessao.clone();
        match com_prazo(move || {
            let r = tema::aplicar(interno);
            if let Ok(t) = &r {
                emitir(&sessao, "ThemeChanged", tema::nome_api(t));
            }
            r
        }) {
            Some(Ok(t)) => tema::nome_api(&t).to_string(),
            Some(Err(e)) => format!("error: {e}"),
            None => "pending".to_string(),
        }
    }

    // ---------- Brilho ----------

    /// `{"internal": {"device","pct","raw","max","writable"}|null, "external": [{"display","name","pct"}]}`.
    /// `rescan` refaz o `ddcutil detect` (lento) em vez de usar a lista guardada.
    #[zbus(name = "GetBrightness")]
    fn get_brightness(&self, rescan: bool) -> String {
        com_prazo(move || brilho::estado(rescan).to_string())
            .unwrap_or_else(|| json!({"internal": brilho::interna(), "external": [], "pending": true}).to_string())
    }

    /// `target`: `"internal"`, `"all"` ou o número do monitor externo (`"1"`, `"2"`...). Devolve o
    /// estado relido do alvo: `{"internal": ...}` / `{"external": [...]}` (com `"error"` se falhou).
    #[zbus(name = "SetBrightness")]
    fn set_brightness(&self, target: String, pct: u32) -> String {
        let sessao = self.sessao.clone();
        let alvo = target.trim().to_ascii_lowercase();
        match com_prazo(move || {
            let mut resp = serde_json::Map::new();
            let mut erros = Vec::new();
            if alvo == "internal" || alvo == "all" {
                match brilho::definir_interna(pct) {
                    Ok(v) => {
                        resp.insert("internal".into(), v);
                    }
                    Err(e) => {
                        resp.insert("internal".into(), brilho::interna());
                        erros.push(format!("internal: {e}"));
                    }
                }
            }
            if alvo != "internal" {
                let alvos: Vec<u32> = if alvo == "all" {
                    brilho::externos_detectar(false).into_iter().map(|(n, _)| n).collect()
                } else {
                    match alvo.parse::<u32>() {
                        Ok(n) => vec![n],
                        Err(_) => {
                            erros.push(format!("unknown target '{alvo}' (internal|all|<display number>)"));
                            Vec::new()
                        }
                    }
                };
                let mut ext = Vec::new();
                for n in alvos {
                    match brilho::definir_externo(n, pct) {
                        Ok(v) => ext.push(v),
                        Err(e) => erros.push(format!("display {n}: {e}")),
                    }
                }
                resp.insert("external".into(), JsonValue::Array(ext));
            }
            if !erros.is_empty() {
                resp.insert("error".into(), JsonValue::String(erros.join("; ")));
            }
            let js = JsonValue::Object(resp);
            emitir(&sessao, "BrightnessChanged", &js.to_string());
            js.to_string()
        }) {
            Some(r) => r,
            None => json!({"pending": true}).to_string(),
        }
    }

    // ---------- Modo de energia ----------

    /// `"power-saver"` | `"balanced"` | `"performance"` (ou `"error: ..."`).
    #[zbus(name = "GetPowerProfile")]
    fn get_power_profile(&self) -> String {
        match self.sistema().and_then(|c| perfil::atual(&c)) {
            Ok(p) => p,
            Err(e) => format!("error: {e}"),
        }
    }

    #[zbus(name = "ListPowerProfiles")]
    fn list_power_profiles(&self) -> Vec<String> {
        self.sistema().map(|c| perfil::disponiveis(&c)).unwrap_or_default()
    }

    /// Grava e devolve o perfil relido (ou `"error: ..."`). O `PowerProfileChanged` vem do vigia.
    #[zbus(name = "SetPowerProfile")]
    fn set_power_profile(&self, profile: String) -> String {
        let Ok(c) = self.sistema() else { return "error: no system bus".into() };
        match com_prazo(move || perfil::definir(&c, &profile)) {
            Some(Ok(p)) => p,
            Some(Err(e)) => format!("error: {e}"),
            None => "pending".into(),
        }
    }

    // ---------- Ociosidade ----------

    /// O compositor avisa: `"lock"` / `"dpms"` / `"suspend"` (parou) ou `"resume"` (voltou).
    #[zbus(name = "IdleEvent")]
    fn idle_event(&self, kind: String) -> String {
        let Some((acao, parou)) = Acao::de_texto(&kind) else {
            return format!("error: unknown kind '{kind}' (lock|dpms|suspend|resume)");
        };
        let cfg = self.cfg.lock().unwrap().clone();
        ocioso::executar(acao, parou, &cfg)
    }

    /// A config de ociosidade em uso (`sessao.json`).
    #[zbus(name = "GetIdleConfig")]
    fn get_idle_config(&self) -> String {
        serde_json::to_string(&*self.cfg.lock().unwrap()).unwrap_or_else(|_| "{}".into())
    }

    #[zbus(signal, name = "ThemeChanged")]
    async fn theme_changed(emitter: &SignalEmitter<'_>, theme: &str) -> zbus::Result<()>;

    /// Mesmo JSON da resposta do `SetBrightness`.
    #[zbus(signal, name = "BrightnessChanged")]
    async fn brightness_changed(emitter: &SignalEmitter<'_>, state_json: &str) -> zbus::Result<()>;

    #[zbus(signal, name = "PowerProfileChanged")]
    async fn power_profile_changed(emitter: &SignalEmitter<'_>, profile: &str) -> zbus::Result<()>;
}

/// O serviço no ar: a conexão (mantida viva) e o controle da escuta de ociosidade.
pub struct ServicoAtivo {
    pub cfg: Arc<Mutex<ConfigSessao>>,
    _conn: DbusConn,
    ocioso: Arc<ocioso::Controle>,
}

impl ServicoAtivo {
    /// `sessao.json` mudou: relê e acorda a escuta de ociosidade.
    pub fn recarregar(&self) {
        let nova = sessao::ler_config();
        let mudou = {
            let mut c = self.cfg.lock().unwrap();
            let mudou = *c != nova;
            *c = nova;
            mudou
        };
        if mudou {
            self.ocioso.recarregar();
        }
    }
}

/// Sobe o serviço (nome `com.aero.Sessiond`, ou `.Teste`). `None` se não há barramento de sessão.
pub fn iniciar(teste: bool) -> Option<ServicoAtivo> {
    let conn = DbusConn::session().ok()?;
    let cfg = Arc::new(Mutex::new(sessao::ler_config()));
    let srv = Servico {
        cfg: Arc::clone(&cfg),
        sessao: conn.clone(),
        sistema: DbusConn::system().ok(),
    };
    conn.object_server().at(CAMINHO, srv).ok()?;
    let nome = if teste { format!("{NOME}.Teste") } else { NOME.to_string() };
    conn.request_name_with_flags(
        WellKnownName::try_from(nome.as_str()).ok()?,
        RequestNameFlags::AllowReplacement | RequestNameFlags::ReplaceExisting,
    )
    .ok()?;
    if teste {
        eprintln!("aero-sessiond: serviço de teste em {nome}");
    }

    // Vigia do modo de energia → PowerProfileChanged
    let conn_ppd = conn.clone();
    thread::Builder::new()
        .name("sessao-perfil".to_string())
        .spawn(move || perfil::vigiar(|p| emitir(&conn_ppd, "PowerProfileChanged", &p)))
        .ok();

    // Programação claro/escuro (o `hypr-tema --loop`): uma conferência por minuto, só com `tema_auto`
    let conn_tema = conn.clone();
    let cfg_tema = Arc::clone(&cfg);
    thread::Builder::new()
        .name("sessao-tema".to_string())
        .spawn(move || {
            loop {
                thread::sleep(Duration::from_secs(60));
                if !cfg_tema.lock().unwrap().tema_auto {
                    continue;
                }
                let alvo = tema::programado(&tema::ler_painel(), tema::minuto_local_agora());
                if alvo != tema::atual()
                    && let Ok(t) = tema::aplicar(alvo)
                {
                    emitir(&conn_tema, "ThemeChanged", tema::nome_api(&t));
                }
            }
        })
        .ok();

    let ocioso = ocioso::iniciar(Arc::clone(&cfg));
    Some(ServicoAtivo { cfg, _conn: conn, ocioso })
}
