//! Backends REAIS da Hestia com leitura de volta (read-back) após cada ação.
//!
//! Regra de ouro: nenhum setter aqui "acredita" que deu certo. Depois de mandar o comando, a função
//! relê o estado na fonte da verdade (PipeWire via `pactl`/`wpctl`, NetworkManager e BlueZ pelo
//! barramento D-Bus do sistema) e devolve o que realmente ficou. É esse estado que o `main.rs`
//! devolve ao chamador e carrega nos sinais `AudioChanged` / `WifiChanged` / `BluetoothChanged`.
//!
//! - **Áudio**: a lista vem do `pactl -f json` (já usada pelo Hermes); o `id` de cada aparelho é o
//!   `object.id` do PipeWire (o mesmo número que o `wpctl status` mostra), e os setters usam o
//!   `wpctl` (`set-default`, `set-volume`, `set-mute`) com esse `id`.
//! - **Wi-Fi**: `org.freedesktop.NetworkManager` (dispositivo sem fio, pontos de acesso, conexões
//!   salvas, `ActivateConnection` / `AddAndActivateConnection` / `Disconnect`).
//! - **Bluetooth**: `org.bluez` (`Adapter1.Powered`, `Device1.Connect` / `Disconnect`, `Battery1`).

use std::collections::HashMap;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use serde_json::{json, Value as JsonValue};
use zbus::blocking::Connection as DbusConn;
use zbus::zvariant::{OwnedObjectPath, OwnedValue, Value};

const NM: &str = "org.freedesktop.NetworkManager";
const NM_CAMINHO: &str = "/org/freedesktop/NetworkManager";
const NM_DEVICE: &str = "org.freedesktop.NetworkManager.Device";
const NM_WIRELESS: &str = "org.freedesktop.NetworkManager.Device.Wireless";
const NM_AP: &str = "org.freedesktop.NetworkManager.AccessPoint";
const NM_SETTINGS: &str = "org.freedesktop.NetworkManager.Settings";
const NM_CONN: &str = "org.freedesktop.NetworkManager.Settings.Connection";
const PROPS: &str = "org.freedesktop.DBus.Properties";
const BLUEZ: &str = "org.bluez";

/// Tempo máximo que um setter segura o chamador D-Bus esperando o resultado. Passou disso, devolve o
/// estado atual com `"pending": true` e o sinal correspondente traz o estado final.
pub const PRAZO_SETTER: Duration = Duration::from_millis(1800);

/// Roda `f` numa thread própria e espera o resultado no máximo [`PRAZO_SETTER`]. `None` = ainda
/// rodando (a thread continua até o fim e cabe a ela emitir o sinal com o estado final).
pub fn com_prazo<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Option<T> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let _ = tx.send(f());
    });
    rx.recv_timeout(PRAZO_SETTER).ok()
}

// ---------------- Áudio ----------------

/// Porcentagem de volume de um objeto do `pactl -f json` (maior canal, ex: `"value_percent": "95%"`).
pub fn volume_pct_pactl(obj: &JsonValue) -> u32 {
    obj.get("volume")
        .and_then(|v| v.as_object())
        .map(|canais| {
            canais
                .values()
                .filter_map(|c| c.get("value_percent").and_then(|p| p.as_str()))
                .filter_map(|p| p.trim_end_matches('%').trim().parse::<u32>().ok())
                .max()
                .unwrap_or(0)
        })
        .unwrap_or(0)
}

/// `object.id` do PipeWire (o número do `wpctl`); sem ele, cai no `index` do pactl.
pub fn id_pipewire(obj: &JsonValue) -> u32 {
    obj.get("properties")
        .and_then(|p| p.get("object.id"))
        .and_then(|v| v.as_str())
        .and_then(|s| s.parse::<u32>().ok())
        .or_else(|| obj.get("index").and_then(|v| v.as_u64()).map(|i| i as u32))
        .unwrap_or(0)
}

/// Projeção compacta de um aparelho de áudio para a API (`ListSinks` / `ListSources`).
pub fn aparelho_compacto(obj: &JsonValue) -> JsonValue {
    let s = |k: &str| obj.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    json!({
        "id": id_pipewire(obj),
        "index": obj.get("index").and_then(|v| v.as_u64()).unwrap_or(0),
        "name": s("name"),
        "pretty_name": s("pretty_name"),
        "is_default": obj.get("padrao").and_then(|v| v.as_bool()).unwrap_or(false),
        "volume": volume_pct_pactl(obj),
        "muted": obj.get("mute").and_then(|v| v.as_bool()).unwrap_or(false),
    })
}

fn wpctl(args: &[&str]) -> Result<(), String> {
    let saida = Command::new("wpctl")
        .args(args)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("wpctl: {e}"))?;
    if saida.status.success() {
        Ok(())
    } else {
        let err = String::from_utf8_lossy(&saida.stderr).trim().to_string();
        Err(if err.is_empty() {
            format!("wpctl {} failed", args.join(" "))
        } else {
            err
        })
    }
}

pub fn audio_definir_padrao(id: u32) -> Result<(), String> {
    wpctl(&["set-default", &id.to_string()])
}

/// Volume em porcentagem (0..=150, o teto do próprio `wpctl -l 1.5`).
pub fn audio_definir_volume(id: u32, pct: u32) -> Result<(), String> {
    let pct = pct.min(150);
    wpctl(&["set-volume", "-l", "1.5", &id.to_string(), &format!("{pct}%")])
}

pub fn audio_definir_mudo(id: u32, mudo: bool) -> Result<(), String> {
    wpctl(&["set-mute", &id.to_string(), if mudo { "1" } else { "0" }])
}

// ---------------- Utilidades D-Bus ----------------

fn prop(conn: &DbusConn, dest: &str, caminho: &str, iface: &str, nome: &str) -> Option<OwnedValue> {
    conn.call_method(Some(dest), caminho, Some(PROPS), "Get", &(iface, nome))
        .ok()?
        .body()
        .deserialize::<OwnedValue>()
        .ok()
}

fn prop_todas(conn: &DbusConn, dest: &str, caminho: &str, iface: &str) -> HashMap<String, OwnedValue> {
    conn.call_method(Some(dest), caminho, Some(PROPS), "GetAll", &(iface,))
        .ok()
        .and_then(|m| m.body().deserialize::<HashMap<String, OwnedValue>>().ok())
        .unwrap_or_default()
}

fn prop_definir(conn: &DbusConn, dest: &str, caminho: &str, iface: &str, nome: &str, v: Value<'_>) -> Result<(), String> {
    conn.call_method(Some(dest), caminho, Some(PROPS), "Set", &(iface, nome, v))
        .map(|_| ())
        .map_err(|e| erro_dbus(&e))
}

fn erro_dbus(e: &zbus::Error) -> String {
    match e {
        zbus::Error::MethodError(nome, msg, _) => {
            let nome = nome.as_str().rsplit('.').next().unwrap_or("Error").to_string();
            match msg {
                Some(m) if !m.is_empty() => format!("{nome}: {m}"),
                _ => nome,
            }
        }
        outro => outro.to_string(),
    }
}

fn como_u32(v: Option<&OwnedValue>) -> u32 {
    let Some(v) = v else { return 0 };
    u32::try_from(v)
        .ok()
        .or_else(|| u8::try_from(v).ok().map(u32::from))
        .or_else(|| i32::try_from(v).ok().map(|n| n.max(0) as u32))
        .unwrap_or(0)
}

fn como_bool(v: Option<&OwnedValue>) -> bool {
    v.and_then(|v| bool::try_from(v).ok()).unwrap_or(false)
}

fn como_str(v: Option<&OwnedValue>) -> String {
    v.and_then(|v| <&str>::try_from(v).ok()).unwrap_or("").to_string()
}

fn como_caminho(v: Option<&OwnedValue>) -> String {
    v.and_then(|v| OwnedObjectPath::try_from(v.clone()).ok())
        .map(|p| p.as_str().to_string())
        .unwrap_or_default()
}

fn como_bytes(v: Option<&OwnedValue>) -> Vec<u8> {
    v.and_then(|v| Vec::<u8>::try_from(v.clone()).ok()).unwrap_or_default()
}

// ---------------- Wi-Fi (NetworkManager) ----------------

/// Caminho do primeiro dispositivo Wi-Fi (`DeviceType == 2`).
fn nm_dispositivo_wifi(conn: &DbusConn) -> Option<String> {
    let devs = conn
        .call_method(Some(NM), NM_CAMINHO, Some(NM), "GetDevices", &())
        .ok()?
        .body()
        .deserialize::<Vec<OwnedObjectPath>>()
        .ok()?;
    devs.into_iter().find_map(|d| {
        let tipo = como_u32(prop(conn, NM, d.as_str(), NM_DEVICE, "DeviceType").as_ref());
        (tipo == 2).then(|| d.as_str().to_string())
    })
}

/// SSID de um ponto de acesso (bytes → texto, com `�` nos bytes inválidos).
fn ssid_texto(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

/// Estado do Wi-Fi: `{"enabled", "connected", "ssid", "strength", "state"}`.
pub fn wifi_estado(conn: &DbusConn) -> JsonValue {
    let ligado = como_bool(prop(conn, NM, NM_CAMINHO, NM, "WirelessEnabled").as_ref());
    let mut ssid = String::new();
    let mut forca = 0u32;
    let mut estado_dev = 0u32;
    if let Some(dev) = nm_dispositivo_wifi(conn) {
        estado_dev = como_u32(prop(conn, NM, &dev, NM_DEVICE, "State").as_ref());
        let ap = como_caminho(prop(conn, NM, &dev, NM_WIRELESS, "ActiveAccessPoint").as_ref());
        if ap.len() > 1 {
            let p = prop_todas(conn, NM, &ap, NM_AP);
            ssid = ssid_texto(&como_bytes(p.get("Ssid")));
            forca = como_u32(p.get("Strength"));
        }
    }
    // 100 = NM_DEVICE_STATE_ACTIVATED
    let conectado = ligado && estado_dev == 100;
    json!({
        "enabled": ligado,
        "connected": conectado,
        "ssid": if conectado { ssid } else { String::new() },
        "strength": if conectado { forca } else { 0 },
        "state": estado_dev,
    })
}

/// SSIDs das conexões Wi-Fi salvas → caminho da conexão.
fn nm_conexoes_salvas(conn: &DbusConn) -> HashMap<String, String> {
    let mut mapa = HashMap::new();
    let Ok(msg) = conn.call_method(
        Some(NM),
        "/org/freedesktop/NetworkManager/Settings",
        Some(NM_SETTINGS),
        "ListConnections",
        &(),
    ) else {
        return mapa;
    };
    let Ok(lista) = msg.body().deserialize::<Vec<OwnedObjectPath>>() else {
        return mapa;
    };
    type Ajustes = HashMap<String, HashMap<String, OwnedValue>>;
    for c in lista {
        let Ok(m) = conn.call_method(Some(NM), c.as_str(), Some(NM_CONN), "GetSettings", &()) else {
            continue;
        };
        let Ok(aj) = m.body().deserialize::<Ajustes>() else {
            continue;
        };
        if let Some(w) = aj.get("802-11-wireless") {
            let ssid = ssid_texto(&como_bytes(w.get("ssid")));
            if !ssid.is_empty() {
                mapa.insert(ssid, c.as_str().to_string());
            }
        }
    }
    mapa
}

/// Lista de redes visíveis: `[{"ssid","strength","secured","known","in_use"}]`, uma por SSID (a mais forte).
pub fn wifi_listar(conn: &DbusConn, rescan: bool) -> Result<Vec<JsonValue>, String> {
    let dev = nm_dispositivo_wifi(conn).ok_or("no Wi-Fi device")?;
    if rescan {
        let opcoes: HashMap<&str, Value<'_>> = HashMap::new();
        let _ = conn.call_method(Some(NM), dev.as_str(), Some(NM_WIRELESS), "RequestScan", &(opcoes,));
        // O NM leva ~1 s para popular a lista nova; esperamos um pouco sem passar do prazo do setter.
        thread::sleep(Duration::from_millis(1200));
    }
    let ativo = como_caminho(prop(conn, NM, &dev, NM_WIRELESS, "ActiveAccessPoint").as_ref());
    let aps = conn
        .call_method(Some(NM), dev.as_str(), Some(NM_WIRELESS), "GetAllAccessPoints", &())
        .map_err(|e| erro_dbus(&e))?
        .body()
        .deserialize::<Vec<OwnedObjectPath>>()
        .map_err(|e| e.to_string())?;
    let salvas = nm_conexoes_salvas(conn);

    let mut mapa: HashMap<String, (u32, bool, bool)> = HashMap::new();
    for ap in aps {
        let p = prop_todas(conn, NM, ap.as_str(), NM_AP);
        let ssid = ssid_texto(&como_bytes(p.get("Ssid")));
        if ssid.is_empty() {
            continue;
        }
        let forca = como_u32(p.get("Strength"));
        // Flags bit 0 = privacidade (WEP); WpaFlags/RsnFlags != 0 = WPA/WPA2/WPA3
        let seguro = como_u32(p.get("Flags")) & 1 != 0
            || como_u32(p.get("WpaFlags")) != 0
            || como_u32(p.get("RsnFlags")) != 0;
        let em_uso = ap.as_str() == ativo;
        let e = mapa.entry(ssid).or_insert((0, seguro, false));
        if forca > e.0 {
            e.0 = forca;
        }
        e.2 |= em_uso;
    }
    let mut lista: Vec<(String, u32, bool, bool)> =
        mapa.into_iter().map(|(s, (f, seg, uso))| (s, f, seg, uso)).collect();
    lista.sort_by(|a, b| b.3.cmp(&a.3).then(b.1.cmp(&a.1)));
    Ok(lista
        .into_iter()
        .map(|(ssid, forca, seguro, em_uso)| {
            let conhecida = salvas.contains_key(&ssid);
            json!({
                "ssid": ssid,
                "strength": forca,
                "secured": seguro,
                "known": conhecida,
                "in_use": em_uso,
            })
        })
        .collect())
}

pub fn wifi_definir_ligado(conn: &DbusConn, ligado: bool) -> Result<(), String> {
    prop_definir(conn, NM, NM_CAMINHO, NM, "WirelessEnabled", Value::Bool(ligado))
}

/// Conecta a `ssid`: conexão salva → `ActivateConnection`; nova → `AddAndActivateConnection` (com
/// `psk` se veio senha). Devolve assim que o NM aceitou o pedido (a conexão em si leva segundos; o
/// `WifiChanged` avisa quando terminar).
pub fn wifi_conectar(conn: &DbusConn, ssid: &str, senha: &str) -> Result<(), String> {
    if ssid.is_empty() || ssid.len() > 32 {
        return Err("invalid ssid".into());
    }
    let dev = nm_dispositivo_wifi(conn).ok_or("no Wi-Fi device")?;
    let dev_path = OwnedObjectPath::try_from(dev.as_str()).map_err(|e| e.to_string())?;
    let raiz = OwnedObjectPath::try_from("/").map_err(|e| e.to_string())?;
    let salvas = nm_conexoes_salvas(conn);
    if let Some(c) = salvas.get(ssid).filter(|_| senha.is_empty()) {
        let c_path = OwnedObjectPath::try_from(c.as_str()).map_err(|e| e.to_string())?;
        conn.call_method(Some(NM), NM_CAMINHO, Some(NM), "ActivateConnection", &(c_path, dev_path, raiz))
            .map(|_| ())
            .map_err(|e| erro_dbus(&e))
    } else {
        let mut ajustes: HashMap<&str, HashMap<&str, Value<'_>>> = HashMap::new();
        let mut c: HashMap<&str, Value<'_>> = HashMap::new();
        c.insert("type", Value::from("802-11-wireless"));
        c.insert("id", Value::from(ssid));
        ajustes.insert("connection", c);
        let mut w: HashMap<&str, Value<'_>> = HashMap::new();
        w.insert("ssid", Value::from(ssid.as_bytes().to_vec()));
        w.insert("mode", Value::from("infrastructure"));
        ajustes.insert("802-11-wireless", w);
        if !senha.is_empty() {
            let mut s: HashMap<&str, Value<'_>> = HashMap::new();
            s.insert("key-mgmt", Value::from("wpa-psk"));
            s.insert("psk", Value::from(senha));
            ajustes.insert("802-11-wireless-security", s);
        }
        conn.call_method(Some(NM), NM_CAMINHO, Some(NM), "AddAndActivateConnection", &(ajustes, dev_path, raiz))
            .map(|_| ())
            .map_err(|e| erro_dbus(&e))
    }
}

pub fn wifi_desconectar(conn: &DbusConn) -> Result<(), String> {
    let dev = nm_dispositivo_wifi(conn).ok_or("no Wi-Fi device")?;
    conn.call_method(Some(NM), dev.as_str(), Some(NM_DEVICE), "Disconnect", &())
        .map(|_| ())
        .map_err(|e| erro_dbus(&e))
}

// ---------------- Bluetooth (BlueZ) ----------------

type MapaObjetos = HashMap<OwnedObjectPath, HashMap<String, HashMap<String, OwnedValue>>>;

fn bluez_objetos(conn: &DbusConn) -> MapaObjetos {
    conn.call_method(Some(BLUEZ), "/", Some("org.freedesktop.DBus.ObjectManager"), "GetManagedObjects", &())
        .ok()
        .and_then(|m| m.body().deserialize::<MapaObjetos>().ok())
        .unwrap_or_default()
}

fn bluez_adaptador(objetos: &MapaObjetos) -> Option<String> {
    let mut caminhos: Vec<&str> = objetos
        .iter()
        .filter(|(_, ifaces)| ifaces.contains_key("org.bluez.Adapter1"))
        .map(|(p, _)| p.as_str())
        .collect();
    caminhos.sort_unstable();
    caminhos.first().map(|p| p.to_string())
}

/// Estado do Bluetooth: `{"powered", "adapter", "devices": [{"addr","name","connected","paired","battery"}]}`.
pub fn bluetooth_estado(conn: &DbusConn) -> JsonValue {
    let objetos = bluez_objetos(conn);
    let adaptador = bluez_adaptador(&objetos);
    let ligado = adaptador
        .as_ref()
        .and_then(|a| objetos.get(&OwnedObjectPath::try_from(a.as_str()).ok()?)?.get("org.bluez.Adapter1").map(|p| como_bool(p.get("Powered"))))
        .unwrap_or(false);
    let mut aparelhos = Vec::new();
    for (caminho, ifaces) in &objetos {
        let Some(dev) = ifaces.get("org.bluez.Device1") else { continue };
        let addr = como_str(dev.get("Address"));
        let nome = {
            let alias = como_str(dev.get("Alias"));
            if alias.is_empty() { como_str(dev.get("Name")) } else { alias }
        };
        if addr.is_empty() || nome.is_empty() || nome.replace('-', ":") == addr {
            continue;
        }
        let bateria = ifaces
            .get("org.bluez.Battery1")
            .map(|b| JsonValue::from(como_u32(b.get("Percentage"))))
            .unwrap_or(JsonValue::Null);
        aparelhos.push(json!({
            "addr": addr,
            "name": nome,
            "connected": como_bool(dev.get("Connected")),
            "paired": como_bool(dev.get("Paired")),
            "trusted": como_bool(dev.get("Trusted")),
            "battery": bateria,
            "path": caminho.as_str(),
        }));
    }
    aparelhos.sort_by(|a, b| {
        let chave = |v: &JsonValue| {
            (
                !v["connected"].as_bool().unwrap_or(false),
                !v["paired"].as_bool().unwrap_or(false),
                v["name"].as_str().unwrap_or("").to_lowercase(),
            )
        };
        chave(a).cmp(&chave(b))
    });
    json!({
        "powered": ligado,
        "adapter": adaptador.unwrap_or_default(),
        "devices": aparelhos,
    })
}

pub fn bluetooth_definir_ligado(conn: &DbusConn, ligado: bool) -> Result<(), String> {
    let adaptador = bluez_adaptador(&bluez_objetos(conn)).ok_or("no Bluetooth adapter")?;
    prop_definir(conn, BLUEZ, &adaptador, "org.bluez.Adapter1", "Powered", Value::Bool(ligado))
}

fn bluez_caminho_aparelho(conn: &DbusConn, addr: &str) -> Result<String, String> {
    let addr_ok = addr.len() == 17
        && addr.split(':').count() == 6
        && addr.chars().all(|c| c.is_ascii_hexdigit() || c == ':');
    if !addr_ok {
        return Err("invalid address".into());
    }
    let objetos = bluez_objetos(conn);
    let adaptador = bluez_adaptador(&objetos).ok_or("no Bluetooth adapter")?;
    let caminho = format!("{adaptador}/dev_{}", addr.to_uppercase().replace(':', "_"));
    let existe = objetos.keys().any(|p| p.as_str() == caminho);
    if existe { Ok(caminho) } else { Err("unknown device".into()) }
}

/// `Device1.Connect` (bloqueia até o BlueZ terminar: pode levar vários segundos).
pub fn bluetooth_conectar(conn: &DbusConn, addr: &str) -> Result<(), String> {
    let caminho = bluez_caminho_aparelho(conn, addr)?;
    conn.call_method(Some(BLUEZ), caminho.as_str(), Some("org.bluez.Device1"), "Connect", &())
        .map(|_| ())
        .map_err(|e| erro_dbus(&e))
}

pub fn bluetooth_desconectar(conn: &DbusConn, addr: &str) -> Result<(), String> {
    let caminho = bluez_caminho_aparelho(conn, addr)?;
    conn.call_method(Some(BLUEZ), caminho.as_str(), Some("org.bluez.Device1"), "Disconnect", &())
        .map(|_| ())
        .map_err(|e| erro_dbus(&e))
}

#[cfg(test)]
mod testes {
    use super::*;

    #[test]
    fn volume_pct_pega_o_maior_canal() {
        let obj = json!({
            "volume": {
                "front-left": {"value": 65536, "value_percent": "100%"},
                "front-right": {"value": 62259, "value_percent": "95%"}
            }
        });
        assert_eq!(volume_pct_pactl(&obj), 100);
        assert_eq!(volume_pct_pactl(&json!({})), 0);
    }

    #[test]
    fn id_prefere_object_id_do_pipewire() {
        let obj = json!({"index": 95, "properties": {"object.id": "80"}});
        assert_eq!(id_pipewire(&obj), 80);
        assert_eq!(id_pipewire(&json!({"index": 95})), 95);
        assert_eq!(id_pipewire(&json!({})), 0);
    }

    #[test]
    fn projecao_compacta() {
        let obj = json!({
            "index": 95, "name": "alsa_output.x", "pretty_name": "Sound Bar", "padrao": true,
            "mute": false, "properties": {"object.id": "80"},
            "volume": {"mono": {"value_percent": "40%"}}
        });
        let c = aparelho_compacto(&obj);
        assert_eq!(c["id"], 80);
        assert_eq!(c["is_default"], true);
        assert_eq!(c["volume"], 40);
        assert_eq!(c["muted"], false);
        assert_eq!(c["pretty_name"], "Sound Bar");
    }

    #[test]
    fn ssid_de_bytes() {
        assert_eq!(ssid_texto(b"Casa 5G"), "Casa 5G");
        assert_eq!(ssid_texto(&[]), "");
    }

    #[test]
    fn com_prazo_devolve_rapido_e_none_no_lento() {
        assert_eq!(com_prazo(|| 7), Some(7));
        // uma tarefa mais lenta que o prazo não segura o chamador
        let t0 = std::time::Instant::now();
        let r: Option<u8> = com_prazo(|| {
            thread::sleep(PRAZO_SETTER + Duration::from_millis(800));
            1
        });
        assert_eq!(r, None);
        assert!(t0.elapsed() < PRAZO_SETTER + Duration::from_millis(500));
    }
}
