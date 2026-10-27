//! Configuração do serviço `com.aero.Sessiond` em `~/.config/aero-shell/sessao.json`
//! (ao lado de `energia.json` e `taxa.json`, vigiado pelo mesmo `inotify`).
//!
//! ```json
//! {
//!   "tema_auto": false,        // claro de dia / escuro de noite (TEMA_CLARO/TEMA_ESCURO do painel.conf)
//!   "idle_nativo": false,      // o daemon escuta o ext_idle_notifier do compositor (padrão: não)
//!   "lock_s": 300,             // segundos parado até travar (0 = nunca)
//!   "dpms_s": 600,             // segundos parado até apagar as telas (0 = nunca)
//!   "suspend_s": 1800,         // segundos parado até suspender (0 = nunca)
//!   "locker": ""               // comando da tela de bloqueio (vazio = nenhum)
//! }
//! ```
//!
//! `idle_nativo` e `tema_auto` aceitam `true`/`false` e também `"sim"`/`"nao"` (como o `daimon.conf`).

use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Deserializer, Serialize};

fn pasta_home() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/home/sapista"))
}

pub fn caminho_config_sessao() -> PathBuf {
    pasta_home().join(".config/aero-shell/sessao.json")
}

/// `true`/`false`, `1`/`0`, `"sim"`/`"nao"`, `"yes"`/`"no"`, `"on"`/`"off"`.
fn bool_flexivel<'de, D: Deserializer<'de>>(d: D) -> Result<bool, D::Error> {
    let v = serde_json::Value::deserialize(d)?;
    Ok(match v {
        serde_json::Value::Bool(b) => b,
        serde_json::Value::Number(n) => n.as_f64().unwrap_or(0.0) != 0.0,
        serde_json::Value::String(s) => matches!(
            s.trim().to_ascii_lowercase().as_str(),
            "sim" | "s" | "yes" | "y" | "true" | "on" | "1" | "ligado"
        ),
        _ => false,
    })
}

/// Segundos: aceita número ou texto (`"300"`); negativo vira 0.
fn segundos_flexivel<'de, D: Deserializer<'de>>(d: D) -> Result<u32, D::Error> {
    let v = serde_json::Value::deserialize(d)?;
    Ok(match v {
        serde_json::Value::Number(n) => n.as_f64().unwrap_or(0.0).max(0.0) as u32,
        serde_json::Value::String(s) => s.trim().parse::<f64>().unwrap_or(0.0).max(0.0) as u32,
        _ => 0,
    })
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct ConfigSessao {
    #[serde(default, deserialize_with = "bool_flexivel")]
    pub tema_auto: bool,
    #[serde(default, deserialize_with = "bool_flexivel")]
    pub idle_nativo: bool,
    #[serde(default = "padrao_lock", deserialize_with = "segundos_flexivel")]
    pub lock_s: u32,
    #[serde(default = "padrao_dpms", deserialize_with = "segundos_flexivel")]
    pub dpms_s: u32,
    #[serde(default = "padrao_suspend", deserialize_with = "segundos_flexivel")]
    pub suspend_s: u32,
    #[serde(default)]
    pub locker: String,
}

fn padrao_lock() -> u32 {
    300
}
fn padrao_dpms() -> u32 {
    600
}
fn padrao_suspend() -> u32 {
    1800
}

impl Default for ConfigSessao {
    fn default() -> Self {
        Self {
            tema_auto: false,
            idle_nativo: false,
            lock_s: padrao_lock(),
            dpms_s: padrao_dpms(),
            suspend_s: padrao_suspend(),
            locker: String::new(),
        }
    }
}

impl ConfigSessao {
    pub fn de_texto(txt: &str) -> Self {
        serde_json::from_str::<ConfigSessao>(txt).unwrap_or_default()
    }
}

pub fn ler_config() -> ConfigSessao {
    fs::read_to_string(caminho_config_sessao())
        .map(|t| ConfigSessao::de_texto(&t))
        .unwrap_or_default()
}

#[cfg(test)]
mod testes {
    use super::*;

    #[test]
    fn padrao_inerte() {
        let c = ConfigSessao::de_texto("{}");
        assert!(!c.idle_nativo);
        assert!(!c.tema_auto);
        assert_eq!((c.lock_s, c.dpms_s, c.suspend_s), (300, 600, 1800));
    }

    #[test]
    fn aceita_sim_nao_e_numeros_em_texto() {
        let c = ConfigSessao::de_texto(r#"{"idle_nativo": "sim", "tema_auto": "nao", "lock_s": "120", "dpms_s": 0, "locker": "daimonctl lock"}"#);
        assert!(c.idle_nativo);
        assert!(!c.tema_auto);
        assert_eq!(c.lock_s, 120);
        assert_eq!(c.dpms_s, 0);
        assert_eq!(c.locker, "daimonctl lock");
    }

    #[test]
    fn json_invalido_vira_padrao() {
        assert_eq!(ConfigSessao::de_texto("{ isso nao e json"), ConfigSessao::default());
    }
}
