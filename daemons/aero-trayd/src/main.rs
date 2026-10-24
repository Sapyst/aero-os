//! aero-trayd — daemon da bandeja do sistema (StatusNotifierWatcher + camada Wayland + dbusmenu).
//!
//! Substitui o `bandeja.py` (que gastava ~60–72 MB de PSS + ~30 MB de GPU por carregar GTK4,
//! Libadwaita e um contexto OpenGL só para desenhar 1 ou 2 ícones de 18×18 px).
//!
//! Como funciona:
//! 1. No D-Bus da sessão (`zbus`), publica o cartório `org.kde.StatusNotifierWatcher` em
//!    `/StatusNotifierWatcher` (e adota os itens que já estavam registrados antes da troca).
//! 2. Vigia os sinais `NameOwnerChanged` (quando um app fecha) e `org.kde.StatusNotifierItem`
//!    (`NewIcon`, `NewStatus`, `NewTitle`, `NewToolTip`).
//! 3. Reduz os ícones (`IconPixmap`, PNG ou SVG) imediatamente para miniaturas `18×18` em RAM
//!    (1,3 KB por ícone) e descarta os buffers grandes originais (que chegam a 1 MB por app).
//! 4. Grava `~/.cache/aero-shell/bandeja.json` (`{"text": " ", "class": "nX"}`) no formato exato
//!    que a `aerobar.py` (via inotify) e a `waybar` (`SIGRTMIN+9`) consomem para reservar espaço.
//! 5. Desenha os ícones numa superfície `zwlr_layer_shell_v1` (`namespace = "bandeja"`, `TOP`)
//!    usando `wl_shm` (CPU pura: zero OpenGL/Vulkan, nunca acorda a NVIDIA) e responde aos cliques:
//!    - Clique simples esquerdo: nada
//!    - Clique duplo esquerdo: `Activate(0, 0)` (ou fallback `.desktop` na ordem exata, ou menu)
//!    - Clique do meio: `SecondaryActivate(0, 0)`
//!    - Clique direito: menu `com.canonical.dbusmenu` (`AboutToShow` + `GetLayout`) desenhado
//!      sob demanda numa camada Wayland com fonte carregada apenas durante a pintura.

use std::collections::HashMap;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use ab_glyph::{Font, FontRef, PxScale, ScaleFont};
use serde::Serialize;
use wayland_client::protocol::{
    wl_buffer::{self, WlBuffer},
    wl_compositor::{self, WlCompositor},
    wl_keyboard::{self, WlKeyboard},
    wl_output::{self, WlOutput},
    wl_pointer::{self, WlPointer},
    wl_registry::{self, WlRegistry},
    wl_seat::{self, WlSeat},
    wl_shm::{self, WlShm},
    wl_shm_pool::{self, WlShmPool},
    wl_surface::{self, WlSurface},
};
use wayland_client::{Connection, Dispatch, Proxy, QueueHandle, WEnum};
use wayland_protocols::wp::viewporter::client::{
    wp_viewport::{self, WpViewport},
    wp_viewporter::{self, WpViewporter},
};
use wayland_protocols_wlr::layer_shell::v1::client::{
    zwlr_layer_shell_v1::{self, ZwlrLayerShellV1},
    zwlr_layer_surface_v1::{self, Anchor, KeyboardInteractivity, ZwlrLayerSurfaceV1},
};
use zbus::blocking::Connection as DbusConn;
use zbus::fdo::RequestNameFlags;
use zbus::message::{Header, Type as MessageType};
use zbus::names::WellKnownName;
use zbus::object_server::SignalEmitter;
use zbus::zvariant::{OwnedValue, Value};
use zbus::{interface, MatchRule};

const LARGURA_SLOT: u32 = 30;
const ALTURA_BARRA: u32 = 34;
const LADO_ICONE: usize = 18;
const SINAL_WAYBAR_OFFSET: i32 = 9;

/// Botões do mouse no Linux (`linux/input-event-codes.h`).
const BTN_LEFT: u32 = 0x110;
const BTN_RIGHT: u32 = 0x111;
const BTN_MIDDLE: u32 = 0x112;

/// Estrutura gravada em `~/.cache/aero-shell/bandeja.json` (contrato com AeroBar e Waybar).
#[derive(Serialize)]
struct EstadoBandejaJson<'a> {
    text: &'a str,
    class: String,
}

/// Dados visíveis já processados de um ícone da bandeja (apenas ~2,6 KB por item!).
#[derive(Clone)]
struct ItemVisivel {
    chave: String,
    id: String,
    caminho_menu: String,
    visivel: bool,
    icone_escuro: [u32; LADO_ICONE * LADO_ICONE],
    icone_claro: [u32; LADO_ICONE * LADO_ICONE],
}

/// Linha de um menu `com.canonical.dbusmenu`.
#[derive(Clone, Debug)]
enum LinhaMenu {
    Separador,
    Botao {
        id: i32,
        rotulo: String,
        ativo: bool,
        submenu: Vec<LinhaMenu>,
    },
}

/// Eventos enviados das threads auxiliares (D-Bus / socket2) para o laço Wayland principal.
enum EventoWayland {
    AtualizarItens(Vec<ItemVisivel>),
    MostrarMenu {
        servico: String,
        caminho_menu: String,
        indice_icone: usize,
        linhas: Vec<LinhaMenu>,
    },
    ReerguerBarra,
}

/// Comandos enviados do laço Wayland para a thread trabalhadora do D-Bus.
enum ComandoDbus {
    Registrar {
        servico: String,
        caminho: String,
    },
    RemoverPorDono {
        nome: String,
    },
    AtualizarPorRemetente {
        remetente: String,
        caminho: String,
    },
    Ativar {
        chave: String,
        indice_icone: usize,
    },
    AtivarSecundario {
        chave: String,
    },
    AbrirMenu {
        chave: String,
        indice_icone: usize,
    },
    ClicarMenu {
        servico: String,
        caminho_menu: String,
        id: i32,
    },
}

/// Qual camada uma `ZwlrLayerSurfaceV1` representa.
#[derive(Clone, Copy, PartialEq, Eq)]
enum PapelCamada {
    Bandeja,
    Pegador,
    Menu,
}

/// Acorda o `poll()` principal escrevendo `1u64` no `eventfd`.
fn acordar_wayland(fd_wakeup: i32) {
    let val: u64 = 1;
    unsafe {
        libc::write(
            fd_wakeup,
            &val as *const u64 as *const libc::c_void,
            std::mem::size_of::<u64>(),
        );
    }
}

/// Objeto D-Bus `/StatusNotifierWatcher` (`org.kde.StatusNotifierWatcher`).
struct CartorioWatcher {
    itens: Arc<Mutex<Vec<String>>>,
    tx_cmd: Sender<ComandoDbus>,
}

#[interface(name = "org.kde.StatusNotifierWatcher")]
impl CartorioWatcher {
    #[zbus(name = "RegisterStatusNotifierItem")]
    async fn register_status_notifier_item(
        &mut self,
        service: &str,
        #[zbus(header)] header: Header<'_>,
        #[zbus(signal_emitter)] emitter: SignalEmitter<'_>,
    ) {
        let remetente = header
            .sender()
            .map(|s| s.as_str().to_string())
            .unwrap_or_default();

        let (servico, caminho) = if service.starts_with('/') {
            (remetente, service.to_string())
        } else if let Some((s, p)) = service.split_once('/') {
            (s.to_string(), format!("/{p}"))
        } else {
            (service.to_string(), "/StatusNotifierItem".to_string())
        };

        if servico.is_empty() {
            return;
        }
        let chave = format!("{servico}{caminho}");
        {
            let mut guard = self.itens.lock().unwrap();
            if !guard.contains(&chave) {
                guard.push(chave.clone());
            }
        }
        let _ = Self::status_notifier_item_registered(&emitter, &chave).await;
        let _ = self.tx_cmd.send(ComandoDbus::Registrar { servico, caminho });
    }

    #[zbus(name = "RegisterStatusNotifierHost")]
    async fn register_status_notifier_host(
        &mut self,
        _service: &str,
        #[zbus(signal_emitter)] emitter: SignalEmitter<'_>,
    ) {
        let _ = Self::status_notifier_host_registered(&emitter).await;
    }

    #[zbus(property, name = "RegisteredStatusNotifierItems")]
    fn registered_status_notifier_items(&self) -> Vec<String> {
        self.itens.lock().unwrap().clone()
    }

    #[zbus(property, name = "IsStatusNotifierHostRegistered")]
    fn is_status_notifier_host_registered(&self) -> bool {
        true
    }

    #[zbus(property, name = "ProtocolVersion")]
    fn protocol_version(&self) -> i32 {
        0
    }

    #[zbus(signal, name = "StatusNotifierItemRegistered")]
    async fn status_notifier_item_registered(
        emitter: &SignalEmitter<'_>,
        service: &str,
    ) -> zbus::Result<()>;

    #[zbus(signal, name = "StatusNotifierItemUnregistered")]
    async fn status_notifier_item_unregistered(
        emitter: &SignalEmitter<'_>,
        service: &str,
    ) -> zbus::Result<()>;

    #[zbus(signal, name = "StatusNotifierHostRegistered")]
    async fn status_notifier_host_registered(emitter: &SignalEmitter<'_>) -> zbus::Result<()>;
}

// ---------------- Processamento de Ícones (18x18 ARGB8888) ----------------

/// Verifica se um desenho ARGB (`[A, R, G, B]`) é quase todo branco/cinza claro (monocromático escuro).
fn desenho_branco(dados: &[u8]) -> bool {
    let mut visiveis = 0usize;
    let mut brancos = 0usize;
    for &[a, r, g, b] in dados.as_chunks::<4>().0 {
        if a > 40 {
            visiveis += 1;
            let min = r.min(g).min(b);
            let max = r.max(g).max(b);
            if min > 190 && max - min < 30 {
                brancos += 1;
            }
        }
    }
    visiveis > 0 && (brancos as f64) / (visiveis as f64) > 0.9
}

/// Verifica se um desenho ARGB (`[A, R, G, B]`) é quase todo preto/cinza escuro (ex: ícone do Claude).
fn desenho_preto(dados: &[u8]) -> bool {
    let mut visiveis = 0usize;
    let mut pretos = 0usize;
    for &[a, r, g, b] in dados.as_chunks::<4>().0 {
        if a > 40 {
            visiveis += 1;
            let min = r.min(g).min(b);
            let max = r.max(g).max(b);
            if max < 80 && max - min < 30 {
                pretos += 1;
            }
        }
    }
    visiveis > 0 && (pretos as f64) / (visiveis as f64) > 0.9
}

/// Redimensiona uma imagem em bytes `[A, R, G, B]` de `(w, h)` para `18×18` por média de área,
/// gerando as versões para tema escuro e tema claro (com inversão de contraste para ícones mono).
fn gerar_miniaturas_18x18(
    w: usize,
    h: usize,
    argb_be: &[u8],
    forcar_mono_branco: bool,
) -> ([u32; LADO_ICONE * LADO_ICONE], [u32; LADO_ICONE * LADO_ICONE]) {
    let mut escuro = [0u32; LADO_ICONE * LADO_ICONE];
    let mut claro = [0u32; LADO_ICONE * LADO_ICONE];
    if w == 0 || h == 0 || argb_be.len() < w * h * 4 {
        return (escuro, claro);
    }

    let eh_branco = forcar_mono_branco || desenho_branco(argb_be);
    let eh_preto = !eh_branco && desenho_preto(argb_be);

    for dy in 0..LADO_ICONE {
        let sy0 = dy * h / LADO_ICONE;
        let sy1 = ((dy + 1) * h / LADO_ICONE).max(sy0 + 1).min(h);
        for dx in 0..LADO_ICONE {
            let sx0 = dx * w / LADO_ICONE;
            let sx1 = ((dx + 1) * w / LADO_ICONE).max(sx0 + 1).min(w);

            let mut soma_a = 0.0f64;
            let mut soma_r = 0.0f64;
            let mut soma_g = 0.0f64;
            let mut soma_b = 0.0f64;
            let mut cont = 0.0f64;

            for sy in sy0..sy1 {
                for sx in sx0..sx1 {
                    let idx = (sy * w + sx) * 4;
                    let a = argb_be[idx] as f64;
                    let r = argb_be[idx + 1] as f64;
                    let g = argb_be[idx + 2] as f64;
                    let b = argb_be[idx + 3] as f64;
                    soma_a += a;
                    // Pondera a cor pela opacidade para não borrar bordas transparentes com preto
                    soma_r += r * a;
                    soma_g += g * a;
                    soma_b += b * a;
                    cont += 1.0;
                }
            }

            let a_med = (soma_a / cont).round().clamp(0.0, 255.0);
            if a_med <= 1.0 {
                continue;
            }
            let r_med = (soma_r / soma_a).clamp(0.0, 255.0);
            let g_med = (soma_g / soma_a).clamp(0.0, 255.0);
            let b_med = (soma_b / soma_a).clamp(0.0, 255.0);

            // Tema escuro: inverte ícones pretos (`invert(0.92)`)
            let (re, ge, be) = if eh_preto {
                (
                    inverter_canal(r_med, 0.92),
                    inverter_canal(g_med, 0.92),
                    inverter_canal(b_med, 0.92),
                )
            } else {
                (r_med as u8, g_med as u8, b_med as u8)
            };

            // Tema claro: inverte ícones brancos/mono (`invert(0.85)`)
            let (rc, gc, bc) = if eh_branco {
                (
                    inverter_canal(r_med, 0.85),
                    inverter_canal(g_med, 0.85),
                    inverter_canal(b_med, 0.85),
                )
            } else {
                (r_med as u8, g_med as u8, b_med as u8)
            };

            let alpha = a_med as u8;
            escuro[dy * LADO_ICONE + dx] = empacotar_premult(alpha, re, ge, be);
            claro[dy * LADO_ICONE + dx] = empacotar_premult(alpha, rc, gc, bc);
        }
    }

    (escuro, claro)
}

fn inverter_canal(v: f64, fator: f64) -> u8 {
    let norm = v / 255.0;
    let inv = norm * (1.0 - fator) + (1.0 - norm) * fator;
    (inv * 255.0).round().clamp(0.0, 255.0) as u8
}

/// Empacota um pixel em ARGB8888 pré-multiplicado (`0xAARRGGBB` little-endian para `wl_shm`).
fn empacotar_premult(a: u8, r: u8, g: u8, b: u8) -> u32 {
    let af = a as u32;
    let rp = (r as u32 * af + 127) / 255;
    let gp = (g as u32 * af + 127) / 255;
    let bp = (b as u32 * af + 127) / 255;
    (af << 24) | (rp << 16) | (gp << 8) | bp
}

/// Procura um arquivo de ícone (`.png` ou `.svg`) pelo nome em `IconThemePath` e nas pastas padrão.
fn localizar_arquivo_icone(nome: &str, pasta_tema: &str) -> Option<PathBuf> {
    if nome.is_empty() {
        return None;
    }
    let p = Path::new(nome);
    if p.is_absolute() && p.is_file() {
        return Some(p.to_path_buf());
    }

    let mut raizes = Vec::new();
    if !pasta_tema.is_empty() {
        raizes.push(PathBuf::from(pasta_tema));
    }
    if let Ok(home) = env::var("HOME") {
        raizes.push(PathBuf::from(home).join(".local/share/icons"));
    }
    raizes.push(PathBuf::from("/usr/share/icons/hicolor"));
    raizes.push(PathBuf::from("/usr/share/icons/Adwaita"));
    raizes.push(PathBuf::from("/usr/share/pixmaps"));

    let subpastas = [
        "",
        "24x24/apps",
        "22x22/apps",
        "16x16/apps",
        "32x32/apps",
        "48x48/apps",
        "64x64/apps",
        "128x128/apps",
        "scalable/apps",
        "symbolic/apps",
        "24x24/status",
        "16x16/status",
        "scalable/status",
        "symbolic/status",
    ];

    for raiz in &raizes {
        for sub in &subpastas {
            let base = if sub.is_empty() {
                raiz.clone()
            } else {
                raiz.join(sub)
            };
            for ext in ["png", "svg"] {
                let tent = base.join(format!("{nome}.{ext}"));
                if tent.is_file() {
                    return Some(tent);
                }
            }
        }
    }
    None
}

/// Carrega um arquivo `.png` ou `.svg` do disco e converte para `[A, R, G, B]`.
fn carregar_icone_disco(caminho: &Path) -> Option<(usize, usize, Vec<u8>)> {
    let ext = caminho.extension()?.to_str()?.to_ascii_lowercase();
    if ext == "png" {
        let arq = File::open(caminho).ok()?;
        let decoder = png::Decoder::new(BufReader::new(arq));
        let mut reader = decoder.read_info().ok()?;
        let mut buf = vec![0u8; reader.output_buffer_size()?];
        let info = reader.next_frame(&mut buf).ok()?;
        let w = info.width as usize;
        let h = info.height as usize;
        let bytes = &buf[..info.buffer_size()];
        let mut argb = Vec::with_capacity(w * h * 4);
        match info.color_type {
            png::ColorType::Rgba => {
                for &[r, g, b, a] in bytes.as_chunks::<4>().0 {
                    argb.extend_from_slice(&[a, r, g, b]);
                }
            }
            png::ColorType::Rgb => {
                for &[r, g, b] in bytes.as_chunks::<3>().0 {
                    argb.extend_from_slice(&[255, r, g, b]);
                }
            }
            png::ColorType::GrayscaleAlpha => {
                for &[g, a] in bytes.as_chunks::<2>().0 {
                    argb.extend_from_slice(&[a, g, g, g]);
                }
            }
            png::ColorType::Grayscale => {
                for &g in bytes {
                    argb.extend_from_slice(&[255, g, g, g]);
                }
            }
            _ => return None,
        }
        Some((w, h, argb))
    } else if ext == "svg" {
        let dados = fs::read(caminho).ok()?;
        let opt = resvg::usvg::Options::default();
        let tree = resvg::usvg::Tree::from_data(&dados, &opt).ok()?;
        let mut pixmap = resvg::tiny_skia::Pixmap::new(36, 36)?;
        let sz = tree.size();
        let sx = 36.0 / sz.width().max(1.0);
        let sy = 36.0 / sz.height().max(1.0);
        let escala = sx.min(sy);
        resvg::render(
            &tree,
            resvg::tiny_skia::Transform::from_scale(escala, escala),
            &mut pixmap.as_mut(),
        );
        let mut argb = Vec::with_capacity(36 * 36 * 4);
        for px in pixmap.pixels() {
            let a = px.alpha();
            if a == 0 {
                argb.extend_from_slice(&[0, 0, 0, 0]);
            } else {
                // tiny-skia usa RGBA pré-multiplicado: desfazemos para passar ao gerador padrão
                let r = ((px.red() as u32 * 255) / (a as u32)).min(255) as u8;
                let g = ((px.green() as u32 * 255) / (a as u32)).min(255) as u8;
                let b = ((px.blue() as u32 * 255) / (a as u32)).min(255) as u8;
                argb.extend_from_slice(&[a, r, g, b]);
            }
        }
        Some((36, 36, argb))
    } else {
        None
    }
}

/// Ícone genérico de fallback (um pequeno círculo/quadrado suave) caso o app não forneça nada válido.
fn icone_padrao_18x18() -> ([u32; LADO_ICONE * LADO_ICONE], [u32; LADO_ICONE * LADO_ICONE]) {
    let mut escuro = [0u32; LADO_ICONE * LADO_ICONE];
    let mut claro = [0u32; LADO_ICONE * LADO_ICONE];
    for y in 0..LADO_ICONE {
        for x in 0..LADO_ICONE {
            let dx = x as f32 - 8.5;
            let dy = y as f32 - 8.5;
            let d = (dx * dx + dy * dy).sqrt();
            if (4.5..=7.0).contains(&d) {
                escuro[y * LADO_ICONE + x] = empacotar_premult(220, 235, 235, 235);
                claro[y * LADO_ICONE + x] = empacotar_premult(220, 40, 40, 40);
            }
        }
    }
    (escuro, claro)
}

// ---------------- Leitura de Propriedades D-Bus e Menus ----------------

#[derive(Clone)]
struct RegistroInterno {
    chave: String,
    servico: String,
    dono_unico: String,
    caminho: String,
    visivel: ItemVisivel,
}

fn extrair_string(mapa: &HashMap<String, OwnedValue>, chave: &str) -> String {
    mapa.get(chave)
        .and_then(|v| <&str>::try_from(v).ok())
        .unwrap_or("")
        .to_string()
}

fn extrair_objeto(mapa: &HashMap<String, OwnedValue>, chave: &str) -> String {
    if let Some(v) = mapa.get(chave) {
        let val_ref: &Value<'_> = v;
        if let Value::ObjectPath(p) = val_ref {
            return p.as_str().to_string();
        }
        if let Ok(s) = <&str>::try_from(v) {
            return s.to_string();
        }
    }
    String::new()
}

/// Extrai o maior pixmap `(largura, altura, bytes_argb)` de `IconPixmap` (`a(iiay)`).
fn extrair_maior_pixmap(mapa: &HashMap<String, OwnedValue>) -> Option<(usize, usize, Vec<u8>)> {
    let val = mapa.get("IconPixmap")?;
    let val_ref: &Value<'_> = val;
    let Value::Array(arr) = val_ref else {
        return None;
    };
    let mut melhor: Option<(usize, usize, Vec<u8>)> = None;
    for elem in arr.iter() {
        if let Value::Structure(s) = elem {
            let campos = s.fields();
            if campos.len() >= 3 {
                let w = i32::try_from(&campos[0]).ok().unwrap_or(0).max(0) as usize;
                let h = i32::try_from(&campos[1]).ok().unwrap_or(0).max(0) as usize;
                if w > 0 && h > 0 {
                    if let Value::Array(bytes_arr) = &campos[2] {
                        let mut bytes = Vec::with_capacity(bytes_arr.len());
                        for b in bytes_arr.iter() {
                            if let Ok(byte) = u8::try_from(b) {
                                bytes.push(byte);
                            }
                        }
                        if bytes.len() >= w * h * 4
                            && melhor.as_ref().is_none_or(|(mw, mh, _)| w * h > mw * mh)
                        {
                            melhor = Some((w, h, bytes));
                        }
                    }
                }
            }
        }
    }
    melhor
}

/// Consulta `GetAll("org.kde.StatusNotifierItem")` no serviço/caminho e monta o `ItemVisivel`.
fn consultar_item_dbus(conn: &DbusConn, servico: &str, caminho: &str) -> Option<ItemVisivel> {
    let msg = conn
        .call_method(
            Some(servico),
            caminho,
            Some("org.freedesktop.DBus.Properties"),
            "GetAll",
            &("org.kde.StatusNotifierItem",),
        )
        .ok()?;
    let mapa: HashMap<String, OwnedValue> = msg.body().deserialize().ok()?;

    let id = extrair_string(&mapa, "Id");
    let status = extrair_string(&mapa, "Status");
    let nome_icone = extrair_string(&mapa, "IconName");
    let pasta_tema = extrair_string(&mapa, "IconThemePath");
    let caminho_menu = extrair_objeto(&mapa, "Menu");
    let visivel = status != "Passive";

    let eh_mono_nome = nome_icone.contains("mono") || nome_icone.ends_with("-symbolic");

    let (icone_escuro, icone_claro) =
        if let Some(arq) = localizar_arquivo_icone(&nome_icone, &pasta_tema) {
            if let Some((w, h, dados)) = carregar_icone_disco(&arq) {
                gerar_miniaturas_18x18(w, h, &dados, eh_mono_nome)
            } else {
                icone_padrao_18x18()
            }
        } else if let Some((w, h, dados)) = extrair_maior_pixmap(&mapa) {
            gerar_miniaturas_18x18(w, h, &dados, eh_mono_nome)
        } else {
            icone_padrao_18x18()
        };

    Some(ItemVisivel {
        chave: format!("{servico}{caminho}"),
        id,
        caminho_menu,
        visivel,
        icone_escuro,
        icone_claro,
    })
}

/// Remove sublinhados de atalho mnemônico (`"_Library"` -> `"Library"`, `"__"` -> `"_"`).
fn sem_mnemonico(texto: &str) -> String {
    texto
        .replace("__", "\0")
        .replace('_', "")
        .replace('\0', "_")
}

/// Converte um nó `(i32, a{sv}, av)` retornado pelo `com.canonical.dbusmenu.GetLayout`.
fn converter_filhos_dbusmenu<'a, I>(filhos: I) -> Vec<LinhaMenu>
where
    I: IntoIterator<Item = &'a Value<'a>>,
{
    let mut saida = Vec::new();
    for item in filhos {
        let mut v = item;
        while let Value::Value(inner) = v {
            v = inner;
        }
        let Value::Structure(s) = v else {
            continue;
        };
        let campos = s.fields();
        if campos.len() < 3 {
            continue;
        }
        let id = i32::try_from(&campos[0]).unwrap_or(0);
        let Value::Dict(ref dict) = campos[1] else {
            continue;
        };

        let visivel = dict
            .get::<_, bool>(&"visible")
            .ok()
            .flatten()
            .unwrap_or(true);
        if !visivel {
            continue;
        }

        let tipo = dict.get::<_, &str>(&"type").ok().flatten().unwrap_or("");
        if tipo == "separator" {
            saida.push(LinhaMenu::Separador);
            continue;
        }

        let rotulo_cru = dict.get::<_, &str>(&"label").ok().flatten().unwrap_or("");
        let mut rotulo = sem_mnemonico(rotulo_cru);
        let toggle_type = dict
            .get::<_, &str>(&"toggle-type")
            .ok()
            .flatten()
            .unwrap_or("");
        let toggle_state = dict
            .get::<_, i32>(&"toggle-state")
            .ok()
            .flatten()
            .unwrap_or(0);
        if !toggle_type.is_empty() && toggle_state == 1 {
            rotulo = format!("✓  {rotulo}");
        }

        let ativo = dict
            .get::<_, bool>(&"enabled")
            .ok()
            .flatten()
            .unwrap_or(true);
        let children_display = dict
            .get::<_, &str>(&"children-display")
            .ok()
            .flatten()
            .unwrap_or("");

        let netos = if let Value::Array(ref arr) = campos[2] {
            converter_filhos_dbusmenu(arr.iter())
        } else {
            Vec::new()
        };

        let eh_submenu = children_display == "submenu" && !netos.is_empty();
        if eh_submenu {
            rotulo.push_str("   ›");
        }

        saida.push(LinhaMenu::Botao {
            id,
            rotulo,
            ativo,
            submenu: if eh_submenu { netos } else { Vec::new() },
        });
    }
    saida
}

/// Pede `AboutToShow(0)` + `GetLayout(0, -1, [])` ao menu do aplicativo.
fn buscar_layout_menu(conn: &DbusConn, servico: &str, caminho_menu: &str) -> Vec<LinhaMenu> {
    if caminho_menu.is_empty() {
        return Vec::new();
    }
    let _ = conn.call_method(
        Some(servico),
        caminho_menu,
        Some("com.canonical.dbusmenu"),
        "AboutToShow",
        &(0i32,),
    );
    let Ok(msg) = conn.call_method(
        Some(servico),
        caminho_menu,
        Some("com.canonical.dbusmenu"),
        "GetLayout",
        &(0i32, -1i32, Vec::<String>::new()),
    ) else {
        return Vec::new();
    };
    let Ok(corpo) = msg.body().deserialize::<(u32, OwnedValue)>() else {
        return Vec::new();
    };
    let raiz: &Value<'_> = &corpo.1;
    if let Value::Structure(s) = raiz {
        let campos = s.fields();
        if campos.len() >= 3 {
            if let Value::Array(ref arr) = campos[2] {
                return converter_filhos_dbusmenu(arr.iter());
            }
        }
    }
    Vec::new()
}

// ---------------- Fallback de abrir aplicativo (.desktop) ----------------

/// Procura o `.desktop` pelo `Id` do ícone na ordem de confiança do `bandeja.py`:
/// 1) nome igual ao Id (`steam` -> `steam.desktop`)
/// 2) sufixo `.Id` (`org.exemplo.steam.desktop`)
/// 3) nome do executável igual ao Id (por último, para nunca abrir um jogo da Steam no lugar da Steam!)
fn abrir_pelo_atalho(ident: &str) -> bool {
    let ident = ident.trim().to_ascii_lowercase();
    if ident.is_empty() {
        return false;
    }

    let mut pastas = Vec::new();
    if let Ok(home) = env::var("HOME") {
        pastas.push(PathBuf::from(home).join(".local/share/applications"));
    }
    pastas.push(PathBuf::from("/usr/local/share/applications"));
    pastas.push(PathBuf::from("/usr/share/applications"));
    pastas.push(PathBuf::from("/var/lib/flatpak/exports/share/applications"));

    struct EntradaDesktop {
        id_base: String,
        executavel: String,
        caminho: PathBuf,
    }

    let mut entradas = Vec::new();
    for pasta in &pastas {
        let Ok(dir) = fs::read_dir(pasta) else {
            continue;
        };
        for entry in dir.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) != Some("desktop") {
                continue;
            }
            let Some(stem) = p.file_stem().and_then(|s| s.to_str()) else {
                continue;
            };
            let Ok(conteudo) = fs::read_to_string(&p) else {
                continue;
            };
            if conteudo.lines().any(|l| l.trim() == "NoDisplay=true") {
                continue;
            }
            let mut exec_bin = String::new();
            for linha in conteudo.lines() {
                if let Some(resto) = linha.strip_prefix("Exec=") {
                    let primeiro = resto
                        .split_whitespace()
                        .next()
                        .unwrap_or("")
                        .trim_matches('"');
                    exec_bin = Path::new(primeiro)
                        .file_name()
                        .and_then(|n| n.to_str())
                        .unwrap_or("")
                        .to_ascii_lowercase();
                    break;
                }
            }
            entradas.push(EntradaDesktop {
                id_base: stem.to_ascii_lowercase(),
                executavel: exec_bin,
                caminho: p,
            });
        }
    }

    let sufixo = format!(".{ident}");
    let escolhido = entradas
        .iter()
        .find(|e| e.id_base == ident)
        .or_else(|| entradas.iter().find(|e| e.id_base.ends_with(&sufixo)))
        .or_else(|| entradas.iter().find(|e| e.executavel == ident));

    if let Some(app) = escolhido {
        let _ = Command::new("gio")
            .arg("launch")
            .arg(&app.caminho)
            .spawn();
        true
    } else {
        false
    }
}

// ---------------- Contrato com AeroBar e Waybar (bandeja.json) ----------------

/// Grava `~/.cache/aero-shell/bandeja.json` e avisa a Waybar (`SIGRTMIN+9`) se ela estiver rodando.
fn gravar_estado_barra(n_visiveis: usize) {
    let home = env::var("HOME").unwrap_or_else(|_| "/home/sapista".to_string());
    let pasta = PathBuf::from(home).join(".cache/aero-shell");
    let _ = fs::create_dir_all(&pasta);
    let caminho = pasta.join("bandeja.json");
    let tmp = pasta.join("bandeja.json.tmp");

    let payload = EstadoBandejaJson {
        text: if n_visiveis > 0 { " " } else { "" },
        class: format!("n{n_visiveis}"),
    };
    if let Ok(bytes) = serde_json::to_vec(&payload) {
        if fs::write(&tmp, &bytes).is_ok() {
            let _ = fs::rename(&tmp, &caminho);
        }
    }

    sinalizar_waybar();
}

/// Envia `SIGRTMIN + 9` aos processos `waybar` do usuário atual (sem precisar de `pkill`).
fn sinalizar_waybar() {
    let Ok(dir) = fs::read_dir("/proc") else {
        return;
    };
    let sig = libc::SIGRTMIN() + SINAL_WAYBAR_OFFSET;
    for entry in dir.flatten() {
        let nome = entry.file_name();
        let Some(pid_str) = nome.to_str() else {
            continue;
        };
        let Ok(pid) = pid_str.parse::<i32>() else {
            continue;
        };
        if let Ok(comm) = fs::read_to_string(entry.path().join("comm")) {
            if comm.trim() == "waybar" {
                unsafe {
                    libc::kill(pid, sig);
                }
            }
        }
    }
}

/// Verifica se a `aerobar.py` está rodando conferindo o PID em `$XDG_RUNTIME_DIR/aerobar.pid`.
fn aerobar_ativa() -> bool {
    let runtime = env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| "/tmp".to_string());
    let caminho = PathBuf::from(runtime).join("aerobar.pid");
    let Ok(texto) = fs::read_to_string(caminho) else {
        return false;
    };
    let Ok(pid) = texto.trim().parse::<i32>() else {
        return false;
    };
    unsafe { libc::kill(pid, 0) == 0 }
}

/// Calcula `(margem_topo, margem_direita)` da bandeja a partir da barra em uso (AeroBar ou Waybar).
fn geometria_da_barra() -> (i32, i32) {
    let home = env::var("HOME").unwrap_or_else(|_| "/home/sapista".to_string());
    if aerobar_ativa() {
        let mut height = 34i32;
        let mut margin_top = 0i32;
        let mut margin_sides = 0i32;
        let mut icon_size = 15i32;
        let cfg_path = PathBuf::from(&home).join(".config/aero/aerobar.json");
        if let Ok(txt) = fs::read_to_string(cfg_path) {
            if let Ok(val) = serde_json::from_str::<serde_json::Value>(&txt) {
                if let Some(v) = val.get("height").and_then(|v| v.as_i64()) {
                    height = v as i32;
                }
                if let Some(v) = val.get("margin_top").and_then(|v| v.as_i64()) {
                    margin_top = v as i32;
                }
                if let Some(v) = val.get("margin_sides").and_then(|v| v.as_i64()) {
                    margin_sides = v as i32;
                }
                if let Some(v) = val.get("icon_size").and_then(|v| v.as_i64()) {
                    icon_size = v as i32;
                }
            }
        }
        let mut borda = 6i32;
        if solido_ativo(&home) {
            if let Ok(out) = Command::new("hyprctl")
                .args(["getoption", "general:gaps_out", "-j"])
                .output()
            {
                if out.status.success() {
                    if let Ok(d) = serde_json::from_slice::<serde_json::Value>(&out.stdout) {
                        let texto = d
                            .get("custom")
                            .and_then(|v| v.as_str().map(String::from))
                            .or_else(|| {
                                d.get("int").and_then(|v| {
                                    v.as_i64()
                                        .map(|n| n.to_string())
                                        .or_else(|| v.as_f64().map(|n| n.to_string()))
                                        .or_else(|| v.as_str().map(String::from))
                                })
                            })
                            .unwrap_or_default();
                        let v: Vec<i32> = texto
                            .split_whitespace()
                            .filter_map(|x| x.parse::<f64>().ok().map(|f| f as i32))
                            .collect();
                        if !v.is_empty() {
                            let gd = if v.len() > 1 { v[1] } else { v[0] };
                            borda = (gd - margin_sides).max(2) + 2;
                        }
                    }
                }
            }
        }
        return (
            margin_top + (height - 34) / 2,
            margin_sides + borda + 8 + icon_size + 8,
        );
    }

    let waybar_path = PathBuf::from(&home).join(".config/waybar/config.jsonc");
    let txt = fs::read_to_string(waybar_path).unwrap_or_default();
    let extrair_num = |chave: &str, padrao: i32| -> i32 {
        let padrao_busca = format!("\"{chave}\"");
        for linha in txt.lines() {
            if let Some(pos) = linha.find(&padrao_busca) {
                let resto = &linha[pos + padrao_busca.len()..];
                if let Some(dois_pontos) = resto.find(':') {
                    let num_str: String = resto[dois_pontos + 1..]
                        .chars()
                        .skip_while(|c| c.is_whitespace())
                        .take_while(|c| c.is_ascii_digit())
                        .collect();
                    if let Ok(n) = num_str.parse::<i32>() {
                        return n;
                    }
                }
            }
        }
        padrao
    };
    let mt = extrair_num("margin-top", 8);
    let h = extrair_num("height", 34);
    let mr = extrair_num("margin-right", 12);
    (mt + (h - 34) / 2, mr + 40)
}

/// Verifica se o visual atual é o Solid (`VISUAL=Solid` no `painel.conf`).
fn solido_ativo(home: &str) -> bool {
    for sub in [".config/hypr/painel.conf", ".config/aero/painel.conf"] {
        if let Ok(txt) = fs::read_to_string(PathBuf::from(home).join(sub)) {
            for linha in txt.lines() {
                let l = linha.trim();
                if l.starts_with('#') {
                    continue;
                }
                if let Some((k, v)) = l.split_once('=') {
                    if k.trim() == "VISUAL" {
                        return v.trim().trim_matches('"') == "Solid";
                    }
                }
            }
        }
    }
    false
}

/// Verifica se o tema atual é claro (`TEMA_MODO=claro` no `painel.conf`).
fn tema_eh_claro() -> bool {
    let home = env::var("HOME").unwrap_or_else(|_| "/home/sapista".to_string());
    for sub in [".config/hypr/painel.conf", ".config/aero/painel.conf"] {
        if let Ok(txt) = fs::read_to_string(PathBuf::from(&home).join(sub)) {
            for linha in txt.lines() {
                if let Some(resto) = linha.strip_prefix("TEMA_MODO=") {
                    return resto.trim().trim_matches('"') == "claro";
                }
            }
        }
    }
    false
}

/// Verifica se a janela ativa no Hyprland está em tela cheia (jogo/vídeo).
/// Se o Hyprland não estiver rodando (ex: sessão Daimon), retorna `false`.
fn jogo_em_tela_cheia() -> bool {
    let Ok(out) = Command::new("hyprctl")
        .args(["activewindow", "-j"])
        .output()
    else {
        return false;
    };
    if !out.status.success() {
        return false;
    }
    let Ok(val) = serde_json::from_slice::<serde_json::Value>(&out.stdout) else {
        return false;
    };
    val.get("fullscreen")
        .and_then(|v| v.as_i64().or_else(|| v.as_bool().map(i64::from)))
        .unwrap_or(0)
        > 0
}

// ---------------- Threads D-Bus e Vigia da Barra ----------------

fn obter_dono_unico(conn: &DbusConn, servico: &str) -> String {
    if servico.starts_with(':') {
        return servico.to_string();
    }
    conn.call_method(
        Some("org.freedesktop.DBus"),
        "/org/freedesktop/DBus",
        Some("org.freedesktop.DBus"),
        "GetNameOwner",
        &(servico,),
    )
    .ok()
    .and_then(|m| m.body().deserialize::<String>().ok())
    .unwrap_or_default()
}

fn iniciar_threads_dbus(
    tx_wayland: Sender<EventoWayland>,
    fd_wakeup: i32,
) -> Sender<ComandoDbus> {
    let (tx_cmd, rx_cmd) = mpsc::channel::<ComandoDbus>();
    let tx_cmd_watcher = tx_cmd.clone();
    let tx_cmd_sinais = tx_cmd.clone();
    let tx_cmd_iniciais = tx_cmd.clone();

    thread::Builder::new()
        .name("trayd-dbus".to_string())
        .spawn(move || {
            let conn = DbusConn::session().expect("aero-trayd: falha ao conectar ao D-Bus da sessão");

            // Antes de substituir o watcher antigo, lê os itens já registrados nele para não perder nenhum
            let iniciais: Vec<String> = conn
                .call_method(
                    Some("org.kde.StatusNotifierWatcher"),
                    "/StatusNotifierWatcher",
                    Some("org.freedesktop.DBus.Properties"),
                    "Get",
                    &(
                        "org.kde.StatusNotifierWatcher",
                        "RegisteredStatusNotifierItems",
                    ),
                )
                .ok()
                .and_then(|m| m.body().deserialize::<OwnedValue>().ok())
                .and_then(|v| Vec::<String>::try_from(v).ok())
                .unwrap_or_default();

            let itens_watcher = Arc::new(Mutex::new(Vec::<String>::new()));
            let cartorio = CartorioWatcher {
                itens: Arc::clone(&itens_watcher),
                tx_cmd: tx_cmd_watcher,
            };

            conn.object_server()
                .at("/StatusNotifierWatcher", cartorio)
                .expect("aero-trayd: falha ao registrar /StatusNotifierWatcher");

            let nome_watcher = WellKnownName::try_from("org.kde.StatusNotifierWatcher").unwrap();
            let _ = conn.request_name_with_flags(
                nome_watcher,
                RequestNameFlags::AllowReplacement | RequestNameFlags::ReplaceExisting,
            );

            // Emite StatusNotifierHostRegistered para que apps atentos se registrem
            if let Ok(iface_ref) = conn
                .object_server()
                .interface::<_, CartorioWatcher>("/StatusNotifierWatcher")
            {
                let _ = zbus::block_on(CartorioWatcher::status_notifier_host_registered(
                    iface_ref.signal_emitter(),
                ));
            }

            // Enfileira os itens herdados do watcher anterior
            for entrada in iniciais {
                let (servico, caminho) = if let Some((s, p)) = entrada.split_once('/') {
                    (s.to_string(), format!("/{p}"))
                } else {
                    (entrada, "/StatusNotifierItem".to_string())
                };
                if !servico.is_empty() {
                    let chave = format!("{servico}{caminho}");
                    {
                        let mut g = itens_watcher.lock().unwrap();
                        if !g.contains(&chave) {
                            g.push(chave);
                        }
                    }
                    let _ = tx_cmd_iniciais.send(ComandoDbus::Registrar { servico, caminho });
                }
            }

            // Thread ouvinte de sinais D-Bus (NameOwnerChanged + sinais StatusNotifierItem)
            let conn_sinais = conn.clone();
            thread::Builder::new()
                .name("trayd-sig".to_string())
                .spawn(move || {
                    escutar_sinais_dbus(conn_sinais, tx_cmd_sinais);
                })
                .expect("Falha ao criar thread de sinais D-Bus");

            // Laço de processamento dos comandos D-Bus
            let mut registros: Vec<RegistroInterno> = Vec::new();
            let mut n_visiveis_anterior: Option<usize> = None;

            // Garante que bandeja.json exista desde o início
            gravar_estado_barra(0);

            while let Ok(cmd) = rx_cmd.recv() {
                let mut mudou = false;
                match cmd {
                    ComandoDbus::Registrar { servico, caminho } => {
                        let chave = format!("{servico}{caminho}");
                        let dono_unico = obter_dono_unico(&conn, &servico);
                        if let Some(visivel) = consultar_item_dbus(&conn, &servico, &caminho) {
                            if let Some(reg) = registros.iter_mut().find(|r| r.chave == chave) {
                                reg.dono_unico = dono_unico;
                                reg.visivel = visivel;
                            } else {
                                registros.push(RegistroInterno {
                                    chave,
                                    servico,
                                    dono_unico,
                                    caminho,
                                    visivel,
                                });
                            }
                            mudou = true;
                        }
                    }
                    ComandoDbus::RemoverPorDono { nome } => {
                        let mut removidos = Vec::new();
                        registros.retain(|r| {
                            if r.servico == nome || (!r.dono_unico.is_empty() && r.dono_unico == nome)
                            {
                                removidos.push(r.chave.clone());
                                false
                            } else {
                                true
                            }
                        });
                        if !removidos.is_empty() {
                            {
                                let mut g = itens_watcher.lock().unwrap();
                                g.retain(|c| !removidos.contains(c));
                            }
                            if let Ok(iface_ref) = conn
                                .object_server()
                                .interface::<_, CartorioWatcher>("/StatusNotifierWatcher")
                            {
                                for ch in &removidos {
                                    let _ = zbus::block_on(
                                        CartorioWatcher::status_notifier_item_unregistered(
                                            iface_ref.signal_emitter(),
                                            ch,
                                        ),
                                    );
                                }
                            }
                            mudou = true;
                        }
                    }
                    ComandoDbus::AtualizarPorRemetente { remetente, caminho } => {
                        for reg in &mut registros {
                            if (reg.servico == remetente || reg.dono_unico == remetente)
                                && reg.caminho == caminho
                            {
                                if let Some(vis) =
                                    consultar_item_dbus(&conn, &reg.servico, &reg.caminho)
                                {
                                    reg.visivel = vis;
                                    mudou = true;
                                }
                            }
                        }
                    }
                    ComandoDbus::Ativar {
                        chave,
                        indice_icone,
                    } => {
                        if let Some(reg) = registros.iter().find(|r| r.chave == chave) {
                            let res = conn.call_method(
                                Some(reg.servico.as_str()),
                                reg.caminho.as_str(),
                                Some("org.kde.StatusNotifierItem"),
                                "Activate",
                                &(0i32, 0i32),
                            );
                            if res.is_err() && !abrir_pelo_atalho(&reg.visivel.id) {
                                let linhas = buscar_layout_menu(
                                    &conn,
                                    &reg.servico,
                                    &reg.visivel.caminho_menu,
                                );
                                if !linhas.is_empty() {
                                    let _ = tx_wayland.send(EventoWayland::MostrarMenu {
                                        servico: reg.servico.clone(),
                                        caminho_menu: reg.visivel.caminho_menu.clone(),
                                        indice_icone,
                                        linhas,
                                    });
                                    acordar_wayland(fd_wakeup);
                                }
                            }
                        }
                    }
                    ComandoDbus::AtivarSecundario { chave } => {
                        if let Some(reg) = registros.iter().find(|r| r.chave == chave) {
                            let _ = conn.call_method(
                                Some(reg.servico.as_str()),
                                reg.caminho.as_str(),
                                Some("org.kde.StatusNotifierItem"),
                                "SecondaryActivate",
                                &(0i32, 0i32),
                            );
                        }
                    }
                    ComandoDbus::AbrirMenu {
                        chave,
                        indice_icone,
                    } => {
                        if let Some(reg) = registros.iter().find(|r| r.chave == chave) {
                            let linhas =
                                buscar_layout_menu(&conn, &reg.servico, &reg.visivel.caminho_menu);
                            if !linhas.is_empty() {
                                let _ = tx_wayland.send(EventoWayland::MostrarMenu {
                                    servico: reg.servico.clone(),
                                    caminho_menu: reg.visivel.caminho_menu.clone(),
                                    indice_icone,
                                    linhas,
                                });
                                acordar_wayland(fd_wakeup);
                            }
                        }
                    }
                    ComandoDbus::ClicarMenu {
                        servico,
                        caminho_menu,
                        id,
                    } => {
                        let _ = conn.call_method(
                            Some(servico.as_str()),
                            caminho_menu.as_str(),
                            Some("com.canonical.dbusmenu"),
                            "Event",
                            &(id, "clicked", Value::from(0i32), 0u32),
                        );
                    }
                }

                if mudou {
                    let lista_vis: Vec<ItemVisivel> = registros
                        .iter()
                        .filter(|r| r.visivel.visivel)
                        .map(|r| r.visivel.clone())
                        .collect();
                    let n = lista_vis.len();
                    if n_visiveis_anterior != Some(n) {
                        n_visiveis_anterior = Some(n);
                        gravar_estado_barra(n);
                    }
                    let _ = tx_wayland.send(EventoWayland::AtualizarItens(lista_vis));
                    acordar_wayland(fd_wakeup);
                }
            }
        })
        .expect("Falha ao criar thread D-Bus do aero-trayd");

    tx_cmd
}

fn escutar_sinais_dbus(conn: DbusConn, tx_cmd: Sender<ComandoDbus>) {
    let regra_sni = MatchRule::builder()
        .msg_type(MessageType::Signal)
        .interface("org.kde.StatusNotifierItem")
        .unwrap()
        .build();
    let regra_noc = MatchRule::builder()
        .msg_type(MessageType::Signal)
        .sender("org.freedesktop.DBus")
        .unwrap()
        .interface("org.freedesktop.DBus")
        .unwrap()
        .member("NameOwnerChanged")
        .unwrap()
        .build();

    if let Ok(dbus_proxy) = zbus::blocking::fdo::DBusProxy::new(&conn) {
        let _ = dbus_proxy.add_match_rule(regra_sni);
        let _ = dbus_proxy.add_match_rule(regra_noc);
    }

    let iter = zbus::blocking::MessageIterator::from(&conn);
    for msg in iter.flatten() {
        let hdr = msg.header();
        let Some(iface) = hdr.interface().map(|i| i.as_str()) else {
            continue;
        };
        let Some(membro) = hdr.member().map(|m| m.as_str()) else {
            continue;
        };

        if iface == "org.freedesktop.DBus" && membro == "NameOwnerChanged" {
            if let Ok((nome, _antigo, novo)) = msg.body().deserialize::<(String, String, String)>()
            {
                if novo.is_empty() && !nome.is_empty() {
                    let _ = tx_cmd.send(ComandoDbus::RemoverPorDono { nome });
                }
            }
        } else if iface == "org.kde.StatusNotifierItem" {
            let remetente = hdr
                .sender()
                .map(|s| s.as_str().to_string())
                .unwrap_or_default();
            let caminho = hdr
                .path()
                .map(|p| p.as_str().to_string())
                .unwrap_or_default();
            if !remetente.is_empty() && !caminho.is_empty() {
                let _ = tx_cmd.send(ComandoDbus::AtualizarPorRemetente { remetente, caminho });
            }
        }
    }
}

/// Ouve o socket2 do Hyprland (se existir) para saber quando a `waybar` ou `aerobar` reabre,
/// mantendo a bandeja sempre na frente e alinhada com a barra.
fn iniciar_vigia_barra(tx_wayland: Sender<EventoWayland>, fd_wakeup: i32) {
    let Ok(runtime) = env::var("XDG_RUNTIME_DIR") else {
        return;
    };
    let Ok(sig) = env::var("HYPRLAND_INSTANCE_SIGNATURE") else {
        return;
    };
    let caminho = PathBuf::from(runtime)
        .join("hypr")
        .join(sig)
        .join(".socket2.sock");
    if !caminho.exists() {
        return;
    }

    thread::Builder::new()
        .name("trayd-bar".to_string())
        .spawn(move || {
            let Ok(stream) = UnixStream::connect(&caminho) else {
                return;
            };
            let reader = BufReader::new(stream);
            for linha in reader.lines().map_while(Result::ok) {
                let l = linha.trim();
                if l == "openlayer>>waybar" || l == "openlayer>>aerobar" || l == "configreloaded>>" {
                    thread::sleep(Duration::from_millis(300));
                    let _ = tx_wayland.send(EventoWayland::ReerguerBarra);
                    acordar_wayland(fd_wakeup);
                }
            }
        })
        .ok();
}

// ---------------- Desenho em Memória Compartilhada (wl_shm) ----------------

/// Cria um arquivo anônimo em RAM (`memfd_create`) com os pixels ARGB8888 (`u32` little-endian)
/// e devolve um `WlBuffer` pronto para anexar à superfície Wayland.
fn criar_buffer_shm(
    shm: &WlShm,
    largura: u32,
    altura: u32,
    pixels: &[u32],
    qh: &QueueHandle<EstadoWayland>,
) -> Option<WlBuffer> {
    let bytes_len = (largura as usize) * (altura as usize) * 4;
    if pixels.len() * 4 < bytes_len || largura == 0 || altura == 0 {
        return None;
    }
    let nome = c"aero-trayd-shm";
    let fd_raw = unsafe { libc::memfd_create(nome.as_ptr(), libc::MFD_CLOEXEC) };
    if fd_raw < 0 {
        return None;
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd_raw) };
    let mut arq = File::from(fd);
    let slice_bytes =
        unsafe { std::slice::from_raw_parts(pixels.as_ptr() as *const u8, bytes_len) };
    if arq.write_all(slice_bytes).is_err() {
        return None;
    }

    let pool = shm.create_pool(arq.as_fd(), bytes_len as i32, qh, ());
    let buffer = pool.create_buffer(
        0,
        largura as i32,
        altura as i32,
        (largura * 4) as i32,
        wl_shm::Format::Argb8888,
        qh,
        (),
    );
    pool.destroy();
    Some(buffer)
}

/// Mistura um pixel pré-multiplicado `src` sobre `dst`.
fn misturar_pixel(dst: &mut u32, src: u32) {
    let sa = (src >> 24) & 0xFF;
    if sa == 0 {
        return;
    }
    if sa == 255 {
        *dst = src;
        return;
    }
    let sr = (src >> 16) & 0xFF;
    let sg = (src >> 8) & 0xFF;
    let sb = src & 0xFF;

    let d = *dst;
    let da = (d >> 24) & 0xFF;
    let dr = (d >> 16) & 0xFF;
    let dg = (d >> 8) & 0xFF;
    let db = d & 0xFF;

    let inv = 255 - sa;
    let oa = sa + (da * inv + 127) / 255;
    let or = sr + (dr * inv + 127) / 255;
    let og = sg + (dg * inv + 127) / 255;
    let ob = sb + (db * inv + 127) / 255;
    *dst = (oa << 24) | (or << 16) | (og << 8) | ob;
}

/// Desenha um retângulo de cantos arredondados com anti-aliasing suave.
#[allow(clippy::too_many_arguments)]
fn desenhar_retangulo_arredondado(
    pixels: &mut [u32],
    stride: u32,
    x0: i32,
    y0: i32,
    w: i32,
    h: i32,
    raio: f32,
    cor: (u8, u8, u8, u8),
) {
    let altura_buf = (pixels.len() as u32).checked_div(stride).unwrap_or(0) as i32;
    let largura_buf = stride as i32;
    let r = raio.min(w as f32 / 2.0).min(h as f32 / 2.0);

    for py in y0.max(0)..(y0 + h).min(altura_buf) {
        for px in x0.max(0)..(x0 + w).min(largura_buf) {
            let lx = (px - x0) as f32 + 0.5;
            let ly = (py - y0) as f32 + 0.5;
            let dx = (lx - r).min(0.0).abs().max((lx - (w as f32 - r)).max(0.0));
            let dy = (ly - r).min(0.0).abs().max((ly - (h as f32 - r)).max(0.0));
            let dist = (dx * dx + dy * dy).sqrt() - r;
            let cob = (0.5 - dist).clamp(0.0, 1.0);
            if cob > 0.0 {
                let a = (cor.0 as f32 * cob).round() as u8;
                let pix = empacotar_premult(a, cor.1, cor.2, cor.3);
                misturar_pixel(&mut pixels[(py as u32 * stride + px as u32) as usize], pix);
            }
        }
    }
}

/// Pinta a faixa da bandeja (`n * 30` × `34` px).
fn renderizar_faixa_bandeja(
    itens: &[ItemVisivel],
    hover: Option<usize>,
    claro: bool,
) -> (u32, u32, Vec<u32>) {
    let n = itens.len() as u32;
    let w = (n * LARGURA_SLOT).max(1);
    let h = ALTURA_BARRA;
    let mut pixels = vec![0u32; (w * h) as usize];

    for (i, item) in itens.iter().enumerate() {
        let x_slot = (i as u32 * LARGURA_SLOT) as i32;
        if hover == Some(i) {
            let cor_hover = if claro {
                (18, 0, 0, 0)
            } else {
                (18, 255, 255, 255)
            };
            desenhar_retangulo_arredondado(&mut pixels, w, x_slot, 4, 30, 26, 10.0, cor_hover);
        }

        let icone = if claro {
            &item.icone_claro
        } else {
            &item.icone_escuro
        };
        let ox = x_slot as u32 + (LARGURA_SLOT - LADO_ICONE as u32) / 2;
        let oy = (ALTURA_BARRA - LADO_ICONE as u32) / 2;
        for iy in 0..LADO_ICONE {
            for ix in 0..LADO_ICONE {
                let src = icone[iy * LADO_ICONE + ix];
                if src != 0 {
                    let idx = ((oy + iy as u32) * w + (ox + ix as u32)) as usize;
                    misturar_pixel(&mut pixels[idx], src);
                }
            }
        }
    }

    (w, h, pixels)
}

/// Carrega a fonte TTF do sistema apenas durante a pintura do menu, descartando em seguida.
fn carregar_bytes_fonte() -> Option<Vec<u8>> {
    let candidatos = [
        "/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf",
        "/usr/share/fonts/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ];
    for c in &candidatos {
        if let Ok(b) = fs::read(c) {
            return Some(b);
        }
    }
    None
}

fn medir_texto(font: &FontRef<'_>, escala: PxScale, texto: &str) -> f32 {
    let scaled = font.as_scaled(escala);
    let mut x = 0.0f32;
    let mut ant = None;
    for ch in texto.chars() {
        let gid = scaled.glyph_id(ch);
        if let Some(p) = ant {
            x += scaled.kern(p, gid);
        }
        x += scaled.h_advance(gid);
        ant = Some(gid);
    }
    x
}

#[allow(clippy::too_many_arguments)]
fn desenhar_texto(
    pixels: &mut [u32],
    stride: u32,
    font: &FontRef<'_>,
    escala: PxScale,
    x_ini: f32,
    y_base: f32,
    texto: &str,
    cor: (u8, u8, u8, u8),
) {
    let altura_buf = (pixels.len() as u32).checked_div(stride).unwrap_or(0) as i32;
    let largura_buf = stride as i32;
    let scaled = font.as_scaled(escala);
    let mut cursor_x = x_ini;
    let mut ant = None;

    for ch in texto.chars() {
        let gid = scaled.glyph_id(ch);
        if let Some(p) = ant {
            cursor_x += scaled.kern(p, gid);
        }
        let glyph = gid.with_scale_and_position(escala, ab_glyph::point(cursor_x, y_base));
        if let Some(outlined) = font.outline_glyph(glyph) {
            let bounds = outlined.px_bounds();
            outlined.draw(|gx, gy, cov| {
                let px = bounds.min.x as i32 + gx as i32;
                let py = bounds.min.y as i32 + gy as i32;
                if px >= 0 && px < largura_buf && py >= 0 && py < altura_buf {
                    let a = ((cor.0 as f32) * cov).round() as u8;
                    if a > 0 {
                        let pix = empacotar_premult(a, cor.1, cor.2, cor.3);
                        misturar_pixel(&mut pixels[(py as u32 * stride + px as u32) as usize], pix);
                    }
                }
            });
        }
        cursor_x += scaled.h_advance(gid);
        ant = Some(gid);
    }
}

/// Geometria e renderização do menu de contexto (`com.canonical.dbusmenu`).
struct LayoutMenuRenderizado {
    largura: u32,
    altura: u32,
    faixas_y: Vec<(i32, i32, usize)>,
    pixels: Vec<u32>,
}

fn renderizar_menu(linhas: &[LinhaMenu], hover_linha: Option<usize>, claro: bool) -> LayoutMenuRenderizado {
    let bytes_fonte = carregar_bytes_fonte();
    let fonte_opt = bytes_fonte
        .as_deref()
        .and_then(|b| FontRef::try_from_slice(b).ok());
    let escala = PxScale::from(14.5);

    let mut larg_texto = 160.0f32;
    if let Some(ref f) = fonte_opt {
        for l in linhas {
            if let LinhaMenu::Botao { rotulo, .. } = l {
                larg_texto = larg_texto.max(medir_texto(f, escala, rotulo));
            }
        }
    }

    let largura = (larg_texto.ceil() as u32 + 40).clamp(196, 360);
    let mut y_cursor = 6i32;
    let mut faixas_y = Vec::new();
    for (idx, l) in linhas.iter().enumerate() {
        match l {
            LinhaMenu::Separador => {
                y_cursor += 9;
            }
            LinhaMenu::Botao { .. } => {
                faixas_y.push((y_cursor, y_cursor + 30, idx));
                y_cursor += 30;
            }
        }
    }
    let altura = (y_cursor + 6).max(24) as u32;
    let mut pixels = vec![0u32; (largura * altura) as usize];

    let (fundo, borda, txt_normal, txt_inativo, cor_hover, cor_sep) = if claro {
        (
            (246, 246, 245, 244),
            (28, 0, 0, 0),
            (255, 28, 28, 30),
            (115, 28, 28, 30),
            (20, 0, 0, 0),
            (26, 0, 0, 0),
        )
    } else {
        (
            (244, 30, 30, 34),
            (32, 255, 255, 255),
            (255, 236, 235, 232),
            (110, 236, 235, 232),
            (22, 255, 255, 255),
            (28, 255, 255, 255),
        )
    };

    // Borda externa + fundo arredondado do menu
    desenhar_retangulo_arredondado(
        &mut pixels,
        largura,
        0,
        0,
        largura as i32,
        altura as i32,
        12.0,
        borda,
    );
    desenhar_retangulo_arredondado(
        &mut pixels,
        largura,
        1,
        1,
        largura as i32 - 2,
        altura as i32 - 2,
        11.0,
        fundo,
    );

    let mut y = 6i32;
    for (idx, l) in linhas.iter().enumerate() {
        match l {
            LinhaMenu::Separador => {
                let sy = y + 4;
                for px in 10..(largura as i32 - 10) {
                    let pix = empacotar_premult(cor_sep.0, cor_sep.1, cor_sep.2, cor_sep.3);
                    misturar_pixel(&mut pixels[(sy as u32 * largura + px as u32) as usize], pix);
                }
                y += 9;
            }
            LinhaMenu::Botao { rotulo, ativo, .. } => {
                if *ativo && hover_linha == Some(idx) {
                    desenhar_retangulo_arredondado(
                        &mut pixels,
                        largura,
                        6,
                        y,
                        largura as i32 - 12,
                        30,
                        8.0,
                        cor_hover,
                    );
                }
                if let Some(ref f) = fonte_opt {
                    let cor = if *ativo { txt_normal } else { txt_inativo };
                    desenhar_texto(
                        &mut pixels,
                        largura,
                        f,
                        escala,
                        18.0,
                        (y + 20) as f32,
                        rotulo,
                        cor,
                    );
                }
                y += 30;
            }
        }
    }

    LayoutMenuRenderizado {
        largura,
        altura,
        faixas_y,
        pixels,
    }
}

// ---------------- Estado e Superfícies Wayland ----------------

struct CamadaWayland {
    surface: WlSurface,
    layer: ZwlrLayerSurfaceV1,
    viewport: Option<WpViewport>,
    buffer: Option<WlBuffer>,
    configurada: bool,
    largura_cfg: u32,
    altura_cfg: u32,
}

impl CamadaWayland {
    fn destruir(self) {
        if let Some(vp) = self.viewport {
            vp.destroy();
        }
        self.layer.destroy();
        self.surface.destroy();
        if let Some(b) = self.buffer {
            b.destroy();
        }
    }
}

struct MenuAtivo {
    servico: String,
    caminho_menu: String,
    linhas: Vec<LinhaMenu>,
    faixas_y: Vec<(i32, i32, usize)>,
    hover_linha: Option<usize>,
    camada_menu: CamadaWayland,
    camada_pegador: CamadaWayland,
}

impl MenuAtivo {
    fn destruir(self) {
        self.camada_menu.destruir();
        self.camada_pegador.destruir();
    }
}

struct EstadoWayland {
    compositor: Option<WlCompositor>,
    shm: Option<WlShm>,
    layer_shell: Option<ZwlrLayerShellV1>,
    viewporter: Option<WpViewporter>,
    seat: Option<WlSeat>,
    pointer: Option<WlPointer>,
    keyboard: Option<WlKeyboard>,

    tx_cmd: Sender<ComandoDbus>,
    itens: Vec<ItemVisivel>,
    camada_bandeja: Option<CamadaWayland>,
    menu_ativo: Option<MenuAtivo>,

    foco_ponteiro: Option<PapelCamada>,
    ponteiro_x: f64,
    ponteiro_y: f64,
    hover_icone: Option<usize>,
    ultimo_clique_esq: Option<(Instant, usize)>,
}

impl EstadoWayland {
    fn novo(tx_cmd: Sender<ComandoDbus>) -> Self {
        Self {
            compositor: None,
            shm: None,
            layer_shell: None,
            viewporter: None,
            seat: None,
            pointer: None,
            keyboard: None,
            tx_cmd,
            itens: Vec::new(),
            camada_bandeja: None,
            menu_ativo: None,
            foco_ponteiro: None,
            ponteiro_x: 0.0,
            ponteiro_y: 0.0,
            hover_icone: None,
            ultimo_clique_esq: None,
        }
    }

    fn fechar_menu(&mut self) {
        if let Some(m) = self.menu_ativo.take() {
            m.destruir();
        }
        if self.foco_ponteiro == Some(PapelCamada::Menu)
            || self.foco_ponteiro == Some(PapelCamada::Pegador)
        {
            self.foco_ponteiro = None;
        }
    }

    fn sincronizar_camada_bandeja(&mut self, qh: &QueueHandle<Self>) {
        if self.itens.is_empty() {
            if let Some(c) = self.camada_bandeja.take() {
                c.destruir();
            }
            self.hover_icone = None;
            return;
        }

        let Some(ref comp) = self.compositor else {
            return;
        };
        let Some(ref ls) = self.layer_shell else {
            return;
        };

        let largura = (self.itens.len() as u32) * LARGURA_SLOT;
        let (topo, direita) = geometria_da_barra();

        if let Some(ref mut cam) = self.camada_bandeja {
            cam.layer.set_margin(topo, direita, 0, 0);
            cam.layer.set_size(largura, ALTURA_BARRA);
            if cam.configurada {
                self.pintar_bandeja(qh);
            } else {
                cam.surface.commit();
            }
        } else {
            let surface = comp.create_surface(qh, ());
            let layer = ls.get_layer_surface(
                &surface,
                None,
                zwlr_layer_shell_v1::Layer::Top,
                "bandeja".to_string(),
                qh,
                PapelCamada::Bandeja,
            );
            layer.set_anchor(Anchor::Top | Anchor::Right);
            layer.set_exclusive_zone(-1);
            layer.set_keyboard_interactivity(KeyboardInteractivity::None);
            layer.set_margin(topo, direita, 0, 0);
            layer.set_size(largura, ALTURA_BARRA);
            surface.commit();

            self.camada_bandeja = Some(CamadaWayland {
                surface,
                layer,
                viewport: None,
                buffer: None,
                configurada: false,
                largura_cfg: largura,
                altura_cfg: ALTURA_BARRA,
            });
        }
    }

    fn pintar_bandeja(&mut self, qh: &QueueHandle<Self>) {
        let Some(ref shm) = self.shm else {
            return;
        };
        let Some(ref mut cam) = self.camada_bandeja else {
            return;
        };
        if !cam.configurada || self.itens.is_empty() {
            return;
        }
        let claro = tema_eh_claro();
        let (w, h, pixels) = renderizar_faixa_bandeja(&self.itens, self.hover_icone, claro);
        if let Some(novo_buf) = criar_buffer_shm(shm, w, h, &pixels, qh) {
            cam.surface.attach(Some(&novo_buf), 0, 0);
            cam.surface.damage_buffer(0, 0, w as i32, h as i32);
            cam.surface.commit();
            if let Some(antigo) = cam.buffer.replace(novo_buf) {
                antigo.destroy();
            }
        }
    }

    fn abrir_menu_wayland(
        &mut self,
        servico: String,
        caminho_menu: String,
        indice_icone: usize,
        linhas: Vec<LinhaMenu>,
        qh: &QueueHandle<Self>,
    ) {
        if jogo_em_tela_cheia() {
            return;
        }
        self.fechar_menu();

        let Some(ref comp) = self.compositor else {
            return;
        };
        let Some(ref ls) = self.layer_shell else {
            return;
        };

        let claro = tema_eh_claro();
        let render = renderizar_menu(&linhas, None, claro);
        let (topo_barra, dir_barra) = geometria_da_barra();
        let n_total = self.itens.len();
        let desloc_dir = if n_total > indice_icone {
            ((n_total - 1 - indice_icone) as i32) * (LARGURA_SLOT as i32)
        } else {
            0
        };

        // 1. Pegador de clique fora (camada OVERLAY transparente atrás do menu)
        let sup_peg = comp.create_surface(qh, ());
        let lay_peg = ls.get_layer_surface(
            &sup_peg,
            None,
            zwlr_layer_shell_v1::Layer::Overlay,
            "aerobar-pegador".to_string(),
            qh,
            PapelCamada::Pegador,
        );
        lay_peg.set_anchor(Anchor::Top | Anchor::Bottom | Anchor::Left | Anchor::Right);
        lay_peg.set_exclusive_zone(-1);
        lay_peg.set_keyboard_interactivity(KeyboardInteractivity::None);
        let vp_peg = self
            .viewporter
            .as_ref()
            .map(|vp| vp.get_viewport(&sup_peg, qh, ()));
        sup_peg.commit();

        // 2. Superfície do Menu (camada OVERLAY aberta depois do Pegador -> fica acima dele)
        let sup_menu = comp.create_surface(qh, ());
        let lay_menu = ls.get_layer_surface(
            &sup_menu,
            None,
            zwlr_layer_shell_v1::Layer::Overlay,
            "bandeja-menu".to_string(),
            qh,
            PapelCamada::Menu,
        );
        lay_menu.set_anchor(Anchor::Top | Anchor::Right);
        lay_menu.set_exclusive_zone(-1);
        lay_menu.set_keyboard_interactivity(KeyboardInteractivity::OnDemand);
        lay_menu.set_margin(topo_barra + ALTURA_BARRA as i32 + 6, dir_barra + desloc_dir, 0, 0);
        lay_menu.set_size(render.largura, render.altura);
        sup_menu.commit();

        self.menu_ativo = Some(MenuAtivo {
            servico,
            caminho_menu,
            linhas,
            faixas_y: render.faixas_y,
            hover_linha: None,
            camada_menu: CamadaWayland {
                surface: sup_menu,
                layer: lay_menu,
                viewport: None,
                buffer: None,
                configurada: false,
                largura_cfg: render.largura,
                altura_cfg: render.altura,
            },
            camada_pegador: CamadaWayland {
                surface: sup_peg,
                layer: lay_peg,
                viewport: vp_peg,
                buffer: None,
                configurada: false,
                largura_cfg: 1,
                altura_cfg: 1,
            },
        });
    }

    fn pintar_menu(&mut self, qh: &QueueHandle<Self>) {
        let Some(ref shm) = self.shm else {
            return;
        };
        let Some(ref mut menu) = self.menu_ativo else {
            return;
        };
        if !menu.camada_menu.configurada {
            return;
        }
        let claro = tema_eh_claro();
        let render = renderizar_menu(&menu.linhas, menu.hover_linha, claro);
        menu.faixas_y = render.faixas_y;
        if let Some(novo_buf) =
            criar_buffer_shm(shm, render.largura, render.altura, &render.pixels, qh)
        {
            menu.camada_menu.surface.attach(Some(&novo_buf), 0, 0);
            menu.camada_menu
                .surface
                .damage_buffer(0, 0, render.largura as i32, render.altura as i32);
            menu.camada_menu.surface.commit();
            if let Some(antigo) = menu.camada_menu.buffer.replace(novo_buf) {
                antigo.destroy();
            }
        }
    }

    fn pintar_pegador(&mut self, qh: &QueueHandle<Self>) {
        let Some(ref shm) = self.shm else {
            return;
        };
        let Some(ref mut menu) = self.menu_ativo else {
            return;
        };
        if !menu.camada_pegador.configurada {
            return;
        }
        // Com wp_viewporter, esticamos um buffer de 1×1 pixel (4 bytes!) com alfa 1/255 pela tela
        let pix = [0x01000000u32];
        if let Some(ref vp) = menu.camada_pegador.viewport {
            let w = menu.camada_pegador.largura_cfg.max(1) as i32;
            let h = menu.camada_pegador.altura_cfg.max(1) as i32;
            vp.set_destination(w, h);
            if let Some(buf) = criar_buffer_shm(shm, 1, 1, &pix, qh) {
                menu.camada_pegador.surface.attach(Some(&buf), 0, 0);
                menu.camada_pegador.surface.damage_buffer(0, 0, 1, 1);
                menu.camada_pegador.surface.commit();
                if let Some(antigo) = menu.camada_pegador.buffer.replace(buf) {
                    antigo.destroy();
                }
            }
        }
    }
}

// ---------------- Implementações de Dispatch Wayland ----------------

impl Dispatch<WlRegistry, ()> for EstadoWayland {
    fn event(
        estado: &mut Self,
        registry: &WlRegistry,
        event: wl_registry::Event,
        _: &(),
        _: &Connection,
        qh: &QueueHandle<Self>,
    ) {
        if let wl_registry::Event::Global {
            name,
            interface,
            version,
        } = event
        {
            if interface == WlCompositor::interface().name {
                estado.compositor =
                    Some(registry.bind::<WlCompositor, _, _>(name, version.min(6), qh, ()));
            } else if interface == WlShm::interface().name {
                estado.shm = Some(registry.bind::<WlShm, _, _>(name, version.min(1), qh, ()));
            } else if interface == ZwlrLayerShellV1::interface().name {
                estado.layer_shell =
                    Some(registry.bind::<ZwlrLayerShellV1, _, _>(name, version.min(4), qh, ()));
            } else if interface == WpViewporter::interface().name {
                estado.viewporter =
                    Some(registry.bind::<WpViewporter, _, _>(name, version.min(1), qh, ()));
            } else if interface == WlSeat::interface().name && estado.seat.is_none() {
                estado.seat = Some(registry.bind::<WlSeat, _, _>(name, version.min(5), qh, ()));
            }
        }
    }
}

impl Dispatch<WlSeat, ()> for EstadoWayland {
    fn event(
        estado: &mut Self,
        seat: &WlSeat,
        event: wl_seat::Event,
        _: &(),
        _: &Connection,
        qh: &QueueHandle<Self>,
    ) {
        if let wl_seat::Event::Capabilities {
            capabilities: WEnum::Value(caps),
        } = event
        {
            if caps.contains(wl_seat::Capability::Pointer) && estado.pointer.is_none() {
                estado.pointer = Some(seat.get_pointer(qh, ()));
            }
            if caps.contains(wl_seat::Capability::Keyboard) && estado.keyboard.is_none() {
                estado.keyboard = Some(seat.get_keyboard(qh, ()));
            }
        }
    }
}

impl Dispatch<ZwlrLayerSurfaceV1, PapelCamada> for EstadoWayland {
    fn event(
        estado: &mut Self,
        layer: &ZwlrLayerSurfaceV1,
        event: zwlr_layer_surface_v1::Event,
        papel: &PapelCamada,
        _: &Connection,
        qh: &QueueHandle<Self>,
    ) {
        match event {
            zwlr_layer_surface_v1::Event::Configure {
                serial,
                width,
                height,
            } => {
                layer.ack_configure(serial);
                match papel {
                    PapelCamada::Bandeja => {
                        if let Some(ref mut cam) = estado.camada_bandeja {
                            cam.configurada = true;
                            if width > 0 {
                                cam.largura_cfg = width;
                            }
                            if height > 0 {
                                cam.altura_cfg = height;
                            }
                        }
                        estado.pintar_bandeja(qh);
                    }
                    PapelCamada::Pegador => {
                        if let Some(ref mut menu) = estado.menu_ativo {
                            menu.camada_pegador.configurada = true;
                            menu.camada_pegador.largura_cfg = width.max(1);
                            menu.camada_pegador.altura_cfg = height.max(1);
                        }
                        estado.pintar_pegador(qh);
                    }
                    PapelCamada::Menu => {
                        if let Some(ref mut menu) = estado.menu_ativo {
                            menu.camada_menu.configurada = true;
                        }
                        estado.pintar_menu(qh);
                    }
                }
            }
            zwlr_layer_surface_v1::Event::Closed => match papel {
                PapelCamada::Bandeja => {
                    if let Some(c) = estado.camada_bandeja.take() {
                        c.destruir();
                    }
                }
                PapelCamada::Pegador | PapelCamada::Menu => {
                    estado.fechar_menu();
                }
            },
            _ => {}
        }
    }
}

impl Dispatch<WlPointer, ()> for EstadoWayland {
    fn event(
        estado: &mut Self,
        _: &WlPointer,
        event: wl_pointer::Event,
        _: &(),
        _: &Connection,
        qh: &QueueHandle<Self>,
    ) {
        match event {
            wl_pointer::Event::Enter {
                surface,
                surface_x,
                surface_y,
                ..
            } => {
                estado.ponteiro_x = surface_x;
                estado.ponteiro_y = surface_y;
                if estado
                    .camada_bandeja
                    .as_ref()
                    .is_some_and(|c| c.surface.id() == surface.id())
                {
                    estado.foco_ponteiro = Some(PapelCamada::Bandeja);
                    let idx = (surface_x.max(0.0) as u32 / LARGURA_SLOT) as usize;
                    let novo = (idx < estado.itens.len()).then_some(idx);
                    if novo != estado.hover_icone {
                        estado.hover_icone = novo;
                        estado.pintar_bandeja(qh);
                    }
                } else if estado
                    .menu_ativo
                    .as_ref()
                    .is_some_and(|m| m.camada_menu.surface.id() == surface.id())
                {
                    estado.foco_ponteiro = Some(PapelCamada::Menu);
                    atualizar_hover_menu(estado, surface_y, qh);
                } else if estado
                    .menu_ativo
                    .as_ref()
                    .is_some_and(|m| m.camada_pegador.surface.id() == surface.id())
                {
                    estado.foco_ponteiro = Some(PapelCamada::Pegador);
                }
            }
            wl_pointer::Event::Motion {
                surface_x,
                surface_y,
                ..
            } => {
                estado.ponteiro_x = surface_x;
                estado.ponteiro_y = surface_y;
                match estado.foco_ponteiro {
                    Some(PapelCamada::Bandeja) => {
                        let idx = (surface_x.max(0.0) as u32 / LARGURA_SLOT) as usize;
                        let novo = (idx < estado.itens.len()).then_some(idx);
                        if novo != estado.hover_icone {
                            estado.hover_icone = novo;
                            estado.pintar_bandeja(qh);
                        }
                    }
                    Some(PapelCamada::Menu) => {
                        atualizar_hover_menu(estado, surface_y, qh);
                    }
                    _ => {}
                }
            }
            wl_pointer::Event::Leave { .. } => {
                match estado.foco_ponteiro.take() {
                    Some(PapelCamada::Bandeja) => {
                        if estado.hover_icone.take().is_some() {
                            estado.pintar_bandeja(qh);
                        }
                    }
                    Some(PapelCamada::Menu) => {
                        if let Some(ref mut m) = estado.menu_ativo {
                            if m.hover_linha.take().is_some() {
                                estado.pintar_menu(qh);
                            }
                        }
                    }
                    _ => {}
                }
            }
            wl_pointer::Event::Button {
                button,
                state: WEnum::Value(wl_pointer::ButtonState::Pressed),
                ..
            } => match estado.foco_ponteiro {
                Some(PapelCamada::Pegador) => {
                    estado.fechar_menu();
                }
                Some(PapelCamada::Menu) => {
                    if button == BTN_LEFT {
                        clicar_item_menu(estado, qh);
                    } else {
                        estado.fechar_menu();
                    }
                }
                Some(PapelCamada::Bandeja) => {
                    estado.fechar_menu();
                    let idx = (estado.ponteiro_x.max(0.0) as u32 / LARGURA_SLOT) as usize;
                    let Some(item) = estado.itens.get(idx) else {
                        return;
                    };
                    let chave = item.chave.clone();
                    if button == BTN_LEFT {
                        let agora = Instant::now();
                        let eh_duplo = estado.ultimo_clique_esq.is_some_and(|(t0, i0)| {
                            i0 == idx && agora.duration_since(t0) <= Duration::from_millis(400)
                        });
                        if eh_duplo {
                            estado.ultimo_clique_esq = None;
                            let _ = estado.tx_cmd.send(ComandoDbus::Ativar {
                                chave,
                                indice_icone: idx,
                            });
                        } else {
                            estado.ultimo_clique_esq = Some((agora, idx));
                        }
                    } else if button == BTN_RIGHT {
                        let _ = estado.tx_cmd.send(ComandoDbus::AbrirMenu {
                            chave,
                            indice_icone: idx,
                        });
                    } else if button == BTN_MIDDLE {
                        let _ = estado.tx_cmd.send(ComandoDbus::AtivarSecundario { chave });
                    }
                }
                None => {}
            },
            _ => {}
        }
    }
}

fn atualizar_hover_menu(estado: &mut EstadoWayland, y: f64, qh: &QueueHandle<EstadoWayland>) {
    let Some(ref mut menu) = estado.menu_ativo else {
        return;
    };
    let yi = y as i32;
    let novo = menu
        .faixas_y
        .iter()
        .find(|&&(y0, y1, _)| yi >= y0 && yi < y1)
        .map(|&(_, _, idx)| idx);
    if novo != menu.hover_linha {
        menu.hover_linha = novo;
        estado.pintar_menu(qh);
    }
}

fn clicar_item_menu(estado: &mut EstadoWayland, qh: &QueueHandle<EstadoWayland>) {
    let Some(ref mut menu) = estado.menu_ativo else {
        return;
    };
    let Some(idx) = menu.hover_linha else {
        return;
    };
    let Some(linha) = menu.linhas.get(idx).cloned() else {
        return;
    };
    if let LinhaMenu::Botao {
        id,
        ativo,
        submenu,
        ..
    } = linha
    {
        if !ativo {
            return;
        }
        if !submenu.is_empty() {
            menu.linhas = submenu;
            menu.hover_linha = None;
            let claro = tema_eh_claro();
            let render = renderizar_menu(&menu.linhas, None, claro);
            menu.faixas_y = render.faixas_y;
            menu.camada_menu
                .layer
                .set_size(render.largura, render.altura);
            menu.camada_menu.surface.commit();
            estado.pintar_menu(qh);
        } else {
            let servico = menu.servico.clone();
            let caminho_menu = menu.caminho_menu.clone();
            let _ = estado.tx_cmd.send(ComandoDbus::ClicarMenu {
                servico,
                caminho_menu,
                id,
            });
            estado.fechar_menu();
        }
    }
}

impl Dispatch<WlKeyboard, ()> for EstadoWayland {
    fn event(
        estado: &mut Self,
        _: &WlKeyboard,
        event: wl_keyboard::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        // Tecla Escape (código evdev 1) fecha o menu aberto
        if let wl_keyboard::Event::Key {
            key: 1,
            state: WEnum::Value(wl_keyboard::KeyState::Pressed),
            ..
        } = event
        {
            estado.fechar_menu();
        }
    }
}

impl Dispatch<WlCompositor, ()> for EstadoWayland {
    fn event(
        _: &mut Self,
        _: &WlCompositor,
        _: wl_compositor::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<WlSurface, ()> for EstadoWayland {
    fn event(
        _: &mut Self,
        _: &WlSurface,
        _: wl_surface::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<WlShm, ()> for EstadoWayland {
    fn event(
        _: &mut Self,
        _: &WlShm,
        _: wl_shm::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<WlShmPool, ()> for EstadoWayland {
    fn event(
        _: &mut Self,
        _: &WlShmPool,
        _: wl_shm_pool::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<WlBuffer, ()> for EstadoWayland {
    fn event(
        _: &mut Self,
        _: &WlBuffer,
        _: wl_buffer::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<WlOutput, ()> for EstadoWayland {
    fn event(
        _: &mut Self,
        _: &WlOutput,
        _: wl_output::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<ZwlrLayerShellV1, ()> for EstadoWayland {
    fn event(
        _: &mut Self,
        _: &ZwlrLayerShellV1,
        _: zwlr_layer_shell_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<WpViewporter, ()> for EstadoWayland {
    fn event(
        _: &mut Self,
        _: &WpViewporter,
        _: wp_viewporter::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<WpViewport, ()> for EstadoWayland {
    fn event(
        _: &mut Self,
        _: &WpViewport,
        _: wp_viewport::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

// ---------------- Inicialização e Laço de Eventos ----------------

fn adquirir_trava_instancia() -> Option<File> {
    let runtime = env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| "/tmp".to_string());
    let caminho = PathBuf::from(runtime).join("aero-trayd.lock");
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

fn processar_eventos_internos(
    rx_wayland: &Receiver<EventoWayland>,
    estado: &mut EstadoWayland,
    qh: &QueueHandle<EstadoWayland>,
) {
    while let Ok(ev) = rx_wayland.try_recv() {
        match ev {
            EventoWayland::AtualizarItens(novos) => {
                estado.itens = novos;
                estado.sincronizar_camada_bandeja(qh);
            }
            EventoWayland::MostrarMenu {
                servico,
                caminho_menu,
                indice_icone,
                linhas,
            } => {
                estado.abrir_menu_wayland(servico, caminho_menu, indice_icone, linhas, qh);
            }
            EventoWayland::ReerguerBarra => {
                if let Some(c) = estado.camada_bandeja.take() {
                    c.destruir();
                }
                estado.sincronizar_camada_bandeja(qh);
            }
        }
    }
}

fn main() {
    let Some(_trava) = adquirir_trava_instancia() else {
        return;
    };

    let fd_wakeup = unsafe { libc::eventfd(0, libc::EFD_CLOEXEC | libc::EFD_NONBLOCK) };
    assert!(fd_wakeup >= 0, "Falha ao criar eventfd");

    let (tx_wayland, rx_wayland) = mpsc::channel::<EventoWayland>();
    let tx_cmd = iniciar_threads_dbus(tx_wayland.clone(), fd_wakeup);
    iniciar_vigia_barra(tx_wayland, fd_wakeup);

    let conn = Connection::connect_to_env().expect("Não foi possível conectar ao Wayland");
    let mut event_queue = conn.new_event_queue();
    let qh = event_queue.handle();

    let display = conn.display();
    let _registry = display.get_registry(&qh, ());

    let mut estado = EstadoWayland::novo(tx_cmd);
    event_queue
        .roundtrip(&mut estado)
        .expect("Falha no roundtrip inicial do Wayland");
    let _ = event_queue.roundtrip(&mut estado);

    processar_eventos_internos(&rx_wayland, &mut estado, &qh);
    let _ = conn.flush();

    // Laço principal: dorme em `poll` esperando eventos do Wayland ou do D-Bus (0% CPU parado)
    loop {
        let _ = event_queue.dispatch_pending(&mut estado);
        processar_eventos_internos(&rx_wayland, &mut estado, &qh);
        let _ = conn.flush();

        let Some(guard) = event_queue.prepare_read() else {
            continue;
        };
        let wl_fd = guard.connection_fd().as_raw_fd();

        let mut pfds = [
            libc::pollfd {
                fd: wl_fd,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: fd_wakeup,
                events: libc::POLLIN,
                revents: 0,
            },
        ];

        let rc = unsafe { libc::poll(pfds.as_mut_ptr(), 2, -1) };
        if rc < 0 {
            drop(guard);
            continue;
        }

        if (pfds[0].revents & (libc::POLLIN | libc::POLLERR | libc::POLLHUP)) != 0 {
            if guard.read().is_err() {
                break;
            }
        } else {
            drop(guard);
        }

        if (pfds[1].revents & libc::POLLIN) != 0 {
            let mut buf = 0u64;
            unsafe {
                libc::read(
                    fd_wakeup,
                    &mut buf as *mut u64 as *mut libc::c_void,
                    std::mem::size_of::<u64>(),
                );
            }
        }
    }
}
