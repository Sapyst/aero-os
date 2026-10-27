//! Brilho: a tela do notebook (sysfs `/sys/class/backlight/*`) e os monitores externos (`ddcutil`).
//!
//! - **Interna**: lê `brightness`/`max_brightness`. Para gravar, tenta o sysfs direto (se a regra de
//!   udev deu permissão ao grupo) e, se não der, pede ao `logind`
//!   (`org.freedesktop.login1.Session.SetBrightness`, permitido ao dono da sessão sem root). Depois
//!   relê o sysfs: o valor devolvido é o que o kernel diz, não o que pedimos.
//! - **Externos**: `ddcutil detect --brief` (lento, ~2 s: feito uma vez e guardado) e `getvcp 10` /
//!   `setvcp 10` por monitor, com `getvcp` de novo depois do `setvcp` como leitura de volta.

use std::fs;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{Mutex, OnceLock};

use serde_json::{json, Value as JsonValue};

/// Monitores externos vistos pelo `ddcutil detect` (número do display, nome do modelo).
static EXTERNOS: OnceLock<Mutex<Option<Vec<(u32, String)>>>> = OnceLock::new();

fn externos_cache() -> &'static Mutex<Option<Vec<(u32, String)>>> {
    EXTERNOS.get_or_init(|| Mutex::new(None))
}

/// Primeira pasta em `/sys/class/backlight` (a tela interna).
pub fn pasta_backlight() -> Option<PathBuf> {
    let mut entradas: Vec<PathBuf> = fs::read_dir("/sys/class/backlight").ok()?.flatten().map(|e| e.path()).collect();
    entradas.sort();
    entradas.into_iter().next()
}

fn ler_num(p: &std::path::Path) -> Option<u32> {
    fs::read_to_string(p).ok()?.trim().parse::<u32>().ok()
}

/// Porcentagem a partir de (valor, máximo), arredondada.
pub fn pct_de(valor: u32, maximo: u32) -> u32 {
    if maximo == 0 { 0 } else { ((valor as f64) * 100.0 / (maximo as f64)).round() as u32 }
}

/// Valor bruto a partir da porcentagem (1..=max nunca vira 0 por arredondamento se pct > 0).
pub fn bruto_de(pct: u32, maximo: u32) -> u32 {
    let pct = pct.min(100);
    let v = ((pct as f64) * (maximo as f64) / 100.0).round() as u32;
    if pct > 0 { v.max(1) } else { 0 }.min(maximo)
}

/// `{"device","pct","raw","max","writable"}` da tela interna, ou `null` sem backlight.
pub fn interna() -> JsonValue {
    let Some(pasta) = pasta_backlight() else { return JsonValue::Null };
    let nome = pasta.file_name().and_then(|s| s.to_str()).unwrap_or("").to_string();
    let maximo = ler_num(&pasta.join("max_brightness")).unwrap_or(0);
    let valor = ler_num(&pasta.join("brightness")).unwrap_or(0);
    let gravavel = fs::OpenOptions::new().write(true).open(pasta.join("brightness")).is_ok();
    json!({
        "device": nome,
        "pct": pct_de(valor, maximo),
        "raw": valor,
        "max": maximo,
        "writable": gravavel,
    })
}

/// Grava o brilho interno: sysfs direto ou, sem permissão, pelo logind. Devolve o estado relido.
pub fn definir_interna(pct: u32) -> Result<JsonValue, String> {
    let pasta = pasta_backlight().ok_or("no backlight device")?;
    let nome = pasta.file_name().and_then(|s| s.to_str()).unwrap_or("").to_string();
    let maximo = ler_num(&pasta.join("max_brightness")).ok_or("cannot read max_brightness")?;
    let bruto = bruto_de(pct, maximo);
    let direto = fs::write(pasta.join("brightness"), bruto.to_string());
    if direto.is_err() {
        // logind: o dono da sessão pode ajustar o backlight sem root (sessão "auto" = a do chamador)
        let conn = zbus::blocking::Connection::system().map_err(|e| e.to_string())?;
        conn.call_method(
            Some("org.freedesktop.login1"),
            "/org/freedesktop/login1/session/auto",
            Some("org.freedesktop.login1.Session"),
            "SetBrightness",
            &("backlight", nome.as_str(), bruto),
        )
        .map_err(|e| format!("logind SetBrightness: {e}"))?;
    }
    // leitura de volta (o kernel pode arredondar)
    std::thread::sleep(std::time::Duration::from_millis(30));
    let lido = ler_num(&pasta.join("brightness")).ok_or("cannot read brightness back")?;
    let est = interna();
    if pct_de(lido, maximo).abs_diff(pct.min(100)) > 1 {
        return Err(format!("backlight reads {}% after asking {}%", pct_de(lido, maximo), pct.min(100)));
    }
    Ok(est)
}

/// Parse de `ddcutil detect --brief`: blocos `Display N` com a linha `Monitor: FAB:MODELO:SERIE`.
pub fn parse_detect(txt: &str) -> Vec<(u32, String)> {
    let mut lista = Vec::new();
    let mut atual: Option<u32> = None;
    for linha in txt.lines() {
        let l = linha.trim();
        if let Some(n) = l.strip_prefix("Display ") {
            atual = n.trim().parse::<u32>().ok();
            if let Some(n) = atual {
                lista.push((n, String::new()));
            }
        } else if l.starts_with("Invalid display") || l.starts_with("Phantom display") {
            atual = None;
        } else if let (Some(n), Some(m)) = (atual, l.strip_prefix("Monitor:")) {
            let partes: Vec<&str> = m.trim().split(':').collect();
            let nome = partes.get(1).map(|s| s.trim()).filter(|s| !s.is_empty()).unwrap_or("Display");
            if let Some(e) = lista.iter_mut().find(|(k, _)| *k == n) {
                e.1 = nome.to_string();
            }
        }
    }
    lista
}

/// Parse de `ddcutil getvcp 10 --brief`: `VCP 10 C <atual> <max>` → (atual, max).
pub fn parse_getvcp(txt: &str) -> Option<(u32, u32)> {
    for linha in txt.lines() {
        let campos: Vec<&str> = linha.split_whitespace().collect();
        if campos.len() >= 5 && campos[0] == "VCP" && campos[1] == "10" {
            return Some((campos[3].parse().ok()?, campos[4].parse().ok()?));
        }
    }
    None
}

fn ddcutil(args: &[&str]) -> Option<String> {
    let saida = Command::new("ddcutil").args(args).stdin(Stdio::null()).stderr(Stdio::null()).output().ok()?;
    saida.status.success().then(|| String::from_utf8_lossy(&saida.stdout).into_owned())
}

/// Lista de monitores externos (detectada uma vez; `forcar` refaz o `detect`).
pub fn externos_detectar(forcar: bool) -> Vec<(u32, String)> {
    let mut cache = externos_cache().lock().unwrap();
    if forcar || cache.is_none() {
        *cache = Some(ddcutil(&["detect", "--brief"]).map(|t| parse_detect(&t)).unwrap_or_default());
    }
    cache.clone().unwrap_or_default()
}

/// `[{"display","name","pct"}]` dos monitores externos (um `getvcp` por monitor).
pub fn externos(forcar_detect: bool) -> Vec<JsonValue> {
    externos_detectar(forcar_detect)
        .into_iter()
        .map(|(n, nome)| {
            let pct = ddcutil(&["getvcp", "10", "--brief", "--display", &n.to_string()])
                .and_then(|t| parse_getvcp(&t))
                .map(|(v, max)| pct_de(v, max));
            json!({"display": n, "name": nome, "pct": pct})
        })
        .collect()
}

/// `setvcp 10` + `getvcp` de volta. Devolve `{"display","name","pct"}` com o valor relido.
pub fn definir_externo(display: u32, pct: u32) -> Result<JsonValue, String> {
    let lista = externos_detectar(false);
    let (n, nome) = lista.into_iter().find(|(k, _)| *k == display).ok_or("unknown display")?;
    let d = n.to_string();
    ddcutil(&["setvcp", "10", &pct.min(100).to_string(), "--display", &d]).ok_or("ddcutil setvcp failed")?;
    let (v, max) = ddcutil(&["getvcp", "10", "--brief", "--display", &d])
        .and_then(|t| parse_getvcp(&t))
        .ok_or("ddcutil getvcp failed after set")?;
    Ok(json!({"display": n, "name": nome, "pct": pct_de(v, max)}))
}

/// Estado completo: `{"internal": {...}|null, "external": [...]}`.
pub fn estado(forcar_detect: bool) -> JsonValue {
    json!({"internal": interna(), "external": externos(forcar_detect)})
}

#[cfg(test)]
mod testes {
    use super::*;

    #[test]
    fn porcentagens() {
        assert_eq!(pct_de(65535, 65535), 100);
        assert_eq!(pct_de(0, 65535), 0);
        assert_eq!(pct_de(32768, 65535), 50);
        assert_eq!(pct_de(5, 0), 0);
        assert_eq!(bruto_de(100, 65535), 65535);
        assert_eq!(bruto_de(150, 65535), 65535);
        assert_eq!(bruto_de(0, 65535), 0);
        assert_eq!(bruto_de(1, 10), 1);
    }

    #[test]
    fn detect_ignora_invalidos() {
        let txt = "Invalid display\n   I2C bus: /dev/i2c-4\n   Monitor: BOE:NE160QDM-NYJ:\n\nDisplay 1\n   I2C bus: /dev/i2c-15\n   Monitor: DEL:DELL G2725D:F4M5S34\n";
        assert_eq!(parse_detect(txt), vec![(1, "DELL G2725D".to_string())]);
        assert!(parse_detect("").is_empty());
    }

    #[test]
    fn getvcp_brief() {
        assert_eq!(parse_getvcp("VCP 10 C 40 100\n"), Some((40, 100)));
        assert_eq!(parse_getvcp("garbage"), None);
    }
}
