//! Modo de energia (`net.hadess.PowerProfiles`, o mesmo serviço que o `powerprofilesctl` fala) direto
//! pelo barramento do sistema: ler `ActiveProfile`, gravar e vigiar `PropertiesChanged`.

use std::collections::HashMap;

use zbus::blocking::Connection as DbusConn;
use zbus::zvariant::{OwnedValue, Value};

const PPD: &str = "net.hadess.PowerProfiles";
const PPD_CAMINHO: &str = "/net/hadess/PowerProfiles";
const PROPS: &str = "org.freedesktop.DBus.Properties";

pub const VALIDOS: &[&str] = &["power-saver", "balanced", "performance"];

pub fn atual(conn: &DbusConn) -> Result<String, String> {
    let v = conn
        .call_method(Some(PPD), PPD_CAMINHO, Some(PROPS), "Get", &(PPD, "ActiveProfile"))
        .map_err(|e| e.to_string())?
        .body()
        .deserialize::<OwnedValue>()
        .map_err(|e| e.to_string())?;
    <&str>::try_from(&v).map(str::to_string).map_err(|e| e.to_string())
}

/// Perfis que o serviço oferece (`Profiles` → cada um tem a chave `Profile`).
pub fn disponiveis(conn: &DbusConn) -> Vec<String> {
    let Ok(msg) = conn.call_method(Some(PPD), PPD_CAMINHO, Some(PROPS), "Get", &(PPD, "Profiles")) else {
        return Vec::new();
    };
    let Ok(v) = msg.body().deserialize::<OwnedValue>() else {
        return Vec::new();
    };
    let Ok(lista) = Vec::<HashMap<String, OwnedValue>>::try_from(v) else {
        return Vec::new();
    };
    lista
        .iter()
        .filter_map(|m| m.get("Profile").and_then(|p| <&str>::try_from(p).ok()).map(str::to_string))
        .collect()
}

/// Grava o perfil e relê `ActiveProfile` (é o relido que volta).
pub fn definir(conn: &DbusConn, perfil: &str) -> Result<String, String> {
    if !VALIDOS.contains(&perfil) {
        return Err(format!("invalid profile '{perfil}' (power-saver|balanced|performance)"));
    }
    conn.call_method(Some(PPD), PPD_CAMINHO, Some(PROPS), "Set", &(PPD, "ActiveProfile", Value::from(perfil)))
        .map_err(|e| match e {
            zbus::Error::MethodError(_, Some(m), _) => m,
            outro => outro.to_string(),
        })?;
    let lido = atual(conn)?;
    if lido == perfil { Ok(lido) } else { Err(format!("profile reads '{lido}' after setting '{perfil}'")) }
}

/// Fica escutando `PropertiesChanged` do serviço e chama `avisar(perfil_novo)` a cada mudança real.
/// Bloqueia a thread (0% de CPU parado: o zbus dorme em poll).
pub fn vigiar(avisar: impl Fn(String)) {
    let Ok(conn) = DbusConn::system() else { return };
    let _ = conn.call_method(
        Some("org.freedesktop.DBus"),
        "/org/freedesktop/DBus",
        Some("org.freedesktop.DBus"),
        "AddMatch",
        &(format!("type='signal',path='{PPD_CAMINHO}',interface='{PROPS}',member='PropertiesChanged'"),),
    );
    let mut ultimo = atual(&conn).unwrap_or_default();
    for msg in zbus::blocking::MessageIterator::from(&conn).flatten() {
        let hdr = msg.header();
        if hdr.message_type() != zbus::message::Type::Signal || hdr.path().map(|p| p.as_str()) != Some(PPD_CAMINHO) {
            continue;
        }
        if let Ok(novo) = atual(&conn)
            && novo != ultimo
        {
            ultimo = novo.clone();
            avisar(novo);
        }
    }
}

#[cfg(test)]
mod testes {
    use super::*;

    #[test]
    fn so_perfis_validos() {
        assert!(VALIDOS.contains(&"balanced"));
        assert!(!VALIDOS.contains(&"turbo"));
    }
}
