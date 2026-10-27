//! Tema claro/escuro em Rust puro (o que o `hypr-tema` fazia em bash, sem chamar nenhum script `hypr-*`).
//!
//! Fontes: `~/.config/hypr/painel.conf` (TEMA_MODO/TEMA_CLARO/TEMA_ESCURO, COR_*, VISUAL) e, no visual
//! Solid, a paleta tonal que o `solido.py` calcula a partir do wallpaper (`python3 -I solido.py hypr
//! <tema> <solido-gtk.css>`). Com isso, [`aplicar`] escreve os MESMOS arquivos que o `hypr-tema`:
//!
//! - `~/.cache/hypr-tema/{tema,cores,destaque,cascata,solido-gtk.css}`
//! - `~/.config/gtk-4.0/cores-tema.css`, `~/.config/gtk-3.0/cores-tema.css`
//! - `~/.config/gtk-{3.0,4.0}/settings.ini`, `~/.gtkrc-2.0`
//! - `~/.config/waybar/cores-<tema>.css` (+ o atalho `cores.css`, se a pasta existir)
//!
//! e avisa quem precisa: `gsettings` (`color-scheme` e `gtk-theme`, com o "pisca" de nome que força o
//! GTK a reler o CSS no Solid) e o Hermes (`SetMode("claro", ...)` direto no D-Bus).
//!
//! Só grava o que MUDOU (quem vigia os arquivos não acorda à toa), como o script original.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

fn pasta_home() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/home/sapista"))
}

fn pasta_estado() -> PathBuf {
    pasta_home().join(".cache/hypr-tema")
}

/// Nome da API ("dark"/"light") → nome interno ("escuro"/"claro"). Aceita os dois.
pub fn normalizar(nome: &str) -> Option<&'static str> {
    match nome.trim().to_ascii_lowercase().as_str() {
        "dark" | "escuro" => Some("escuro"),
        "light" | "claro" => Some("claro"),
        _ => None,
    }
}

/// Nome interno → nome da API.
pub fn nome_api(interno: &str) -> &'static str {
    if interno == "claro" { "light" } else { "dark" }
}

/// O tema aplicado por último (`~/.cache/hypr-tema/tema`); sem o arquivo, "escuro".
pub fn atual() -> String {
    fs::read_to_string(pasta_estado().join("tema"))
        .map(|s| s.trim().to_string())
        .ok()
        .filter(|s| s == "claro" || s == "escuro")
        .unwrap_or_else(|| "escuro".to_string())
}

/// Lê `painel.conf` do jeito do `painel.sh`: só `CHAVES_ASSIM=valor`, tirando aspas simples/duplas.
pub fn ler_painel_texto(txt: &str) -> HashMap<String, String> {
    let mut m = HashMap::new();
    for linha in txt.lines() {
        let linha = linha.trim_end_matches('\r');
        let Some((k, v)) = linha.split_once('=') else { continue };
        let chave_ok = !k.is_empty()
            && k.chars().next().is_some_and(|c| c.is_ascii_uppercase())
            && k.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_');
        if !chave_ok {
            continue;
        }
        let mut v = v.to_string();
        if v.len() >= 2 && ((v.starts_with('"') && v.ends_with('"')) || (v.starts_with('\'') && v.ends_with('\''))) {
            v = v[1..v.len() - 1].to_string();
        }
        m.insert(k.to_string(), v);
    }
    m
}

pub fn ler_painel() -> HashMap<String, String> {
    fs::read_to_string(pasta_home().join(".config/hypr/painel.conf"))
        .map(|t| ler_painel_texto(&t))
        .unwrap_or_default()
}

/// "07:30" → 450 minutos; qualquer outra coisa → 0.
pub fn minutos(hhmm: &str) -> u32 {
    let Some((h, m)) = hhmm.trim().split_once(':') else { return 0 };
    match (h.parse::<u32>(), m.parse::<u32>()) {
        (Ok(h), Ok(m)) if h < 24 && m < 60 && m.to_string().len() <= 2 => h * 60 + m,
        _ => 0,
    }
}

/// `agora` está no intervalo `[ini, fim)`? (funciona passando da meia-noite)
fn dentro(agora: u32, ini: u32, fim: u32) -> bool {
    if ini <= fim { agora >= ini && agora < fim } else { agora >= ini || agora < fim }
}

/// O tema que o `painel.conf` pede neste minuto do dia: `TEMA_MODO=claro|escuro` fixo, ou `auto`
/// (claro entre `TEMA_CLARO` e `TEMA_ESCURO`, padrão 07:00–18:00).
pub fn programado(painel: &HashMap<String, String>, agora_min: u32) -> &'static str {
    match painel.get("TEMA_MODO").map(String::as_str) {
        Some("claro") => "claro",
        Some("escuro") => "escuro",
        _ => {
            let ini = minutos(painel.get("TEMA_CLARO").map(String::as_str).unwrap_or("07:00"));
            let fim = minutos(painel.get("TEMA_ESCURO").map(String::as_str).unwrap_or("18:00"));
            if dentro(agora_min, ini, fim) { "claro" } else { "escuro" }
        }
    }
}

/// Minuto do dia local agora (via `libc::localtime_r`, sem dependência de fuso).
pub fn minuto_local_agora() -> u32 {
    let t = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as libc::time_t).unwrap_or(0);
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe {
        libc::localtime_r(&t, &mut tm);
    }
    (tm.tm_hour.max(0) as u32) * 60 + tm.tm_min.max(0) as u32
}

/// Grava só se o conteúdo mudou. Devolve `true` quando gravou.
fn gravar(caminho: &Path, conteudo: &str) -> bool {
    if fs::read_to_string(caminho).map(|a| a == conteudo).unwrap_or(false) {
        return false;
    }
    if let Some(p) = caminho.parent() {
        let _ = fs::create_dir_all(p);
    }
    fs::write(caminho, conteudo).is_ok()
}

fn eh_hex6(s: &str) -> bool {
    s.len() == 6 && s.chars().all(|c| c.is_ascii_hexdigit())
}

/// "f6f6f6" → "246,246,246"
fn rgb(hex: &str) -> String {
    let c = |i: usize| u8::from_str_radix(&hex[i..i + 2], 16).unwrap_or(0);
    format!("{},{},{}", c(0), c(2), c(4))
}

/// Luminância aproximada (fórmula do script): texto escuro por cima de cores claras.
fn destaque_claro(hex: &str) -> bool {
    let c = |i: usize| u32::from_str_radix(&hex[i..i + 2], 16).unwrap_or(0);
    299 * c(0) + 587 * c(2) + 114 * c(4) > 165_000
}

/// Paleta efetiva de um tema (o que o `gerar_cores` do script calcula antes de escrever).
#[derive(Clone, Debug, PartialEq)]
pub struct Paleta {
    pub tema: &'static str,
    pub hex: String,
    pub texto: &'static str,
    pub hover: &'static str,
    pub rgbs: String,
    pub cor_alfa: String,
    pub bar_alpha: String,
    pub cor_sinc: bool,
    pub dest: String,
    pub dest_fg: String,
    pub halo: String,
    pub destaque_cfg: String,
    pub cor_clara: String,
    pub cor_escura: String,
    pub solid: bool,
}

/// Monta a paleta a partir do painel.conf (já com a sobreposição do Solid aplicada em `painel`).
pub fn paleta(painel: &HashMap<String, String>, tema: &'static str) -> Paleta {
    let g = |k: &str, padrao: &str| painel.get(k).cloned().unwrap_or_else(|| padrao.to_string());
    let mut cor_clara = g("COR_CLARA", "f6f6f6");
    let mut cor_escura = g("COR_ESCURA", "1e1e1e");
    if !eh_hex6(&cor_clara) {
        cor_clara = "f6f6f6".into();
    }
    if !eh_hex6(&cor_escura) {
        cor_escura = "1e1e1e".into();
    }
    let cor_alfa = g("COR_ALFA", "0.80");
    let bar_alpha = g("BAR_ALPHA", "0.45");
    let cor_sinc = g("COR_SINC", "1") == "1";
    let mut destaque_cfg = g("COR_DESTAQUE", "3584e4");
    if g("WALL_CORES", "0") == "1" {
        if let Some(w) = painel.get("COR_DESTAQUE_WALL").filter(|w| eh_hex6(w)) {
            destaque_cfg = w.clone();
        }
    }
    if destaque_cfg != "mono" && !eh_hex6(&destaque_cfg) {
        destaque_cfg = "3584e4".into();
    }
    let (hex, texto, hover) = if tema == "escuro" {
        (cor_escura.clone(), "f0f0f0", "rgba(255, 255, 255, 0.10)")
    } else {
        (cor_clara.clone(), "1e1e1e", "rgba(0, 0, 0, 0.07)")
    };
    let (dest, dest_fg, halo) = if destaque_cfg == "mono" {
        if tema == "escuro" {
            ("f0f0f0".to_string(), "1e1e1e".to_string(), "rgba(0,0,0,0.55)".to_string())
        } else {
            ("1e1e1e".to_string(), "f6f6f6".to_string(), "rgba(255,255,255,0.80)".to_string())
        }
    } else {
        let fg = if destaque_claro(&destaque_cfg) { "1e1e1e" } else { "ffffff" };
        (destaque_cfg.clone(), fg.to_string(), "rgba(0,0,0,0)".to_string())
    };
    Paleta {
        tema,
        rgbs: rgb(&hex),
        hex,
        texto,
        hover,
        cor_alfa,
        bar_alpha,
        cor_sinc,
        dest,
        dest_fg,
        halo,
        destaque_cfg,
        cor_clara,
        cor_escura,
        solid: g("VISUAL", "") == "Solid",
    }
}

/// CSS da barra de cima (waybar) — `cores-<tema>.css`.
pub fn css_waybar(p: &Paleta) -> String {
    let fonte = if p.tema == "escuro" { "ESCURA" } else { "CLARA" };
    format!(
        "/* Gerado pelo hypr-tema a partir de COR_{fonte} e BAR_ALPHA (painel.conf). */\n\
@define-color fundo rgba({rgbs}, {bar});\n\
@define-color fundo-solido #{hex};\n\
@define-color texto #{texto};\n\
@define-color destaque #{dest};\n\
@define-color destaque-texto #{fg};\n\
@define-color hover {hover};\n",
        rgbs = p.rgbs,
        bar = p.bar_alpha,
        hex = p.hex,
        texto = p.texto,
        dest = p.dest,
        fg = p.dest_fg,
        hover = p.hover
    )
}

/// CSS dos apps GTK4/libadwaita — `gtk-4.0/cores-tema.css` (+ o CSS do Solid por cima).
pub fn css_gtk4(p: &Paleta, extra_solid: &str) -> String {
    let mut txt = if p.cor_sinc {
        format!(
            "/* Gerado pelo hypr-tema: apps GTK4/libadwaita com a MESMA cor da barra de cima (COR_SINC=1), em VIDRO\n   \
FOSCO: fundo semitransparente + desfoque do Hyprland (\"carbono\"); texto sempre nítido (doc 44).\n   \
As --variáveis são as cores com nome do libadwaita: trocando aqui, muda em todos os apps. */\n\
:root {{\n    \
--window-bg-color: rgba({rgbs}, {a});\n    \
--headerbar-bg-color: rgba({rgbs}, {a});\n    \
--headerbar-backdrop-color: rgba({rgbs}, {a});\n    \
--sidebar-bg-color: rgba({rgbs}, {a});\n    \
--sidebar-backdrop-color: rgba({rgbs}, {a});\n    \
--dialog-bg-color: rgb({rgbs});   /* SÓLIDO: janelinhas DENTRO de um app não têm desfoque atrás (doc 37) */\n    \
--popover-bg-color: rgb({rgbs});   /* SÓLIDO: popups sem desfoque + transparência = texto \"fantasma\" de trás (doc 35) */\n\
}}\n\
/* Janelinhas DENTRO de um app (Adw.Dialog): o \"sheet\" delas usa a cor de JANELA (com transparência),\n   \
e dentro do app não existe desfoque: a página de trás aparecia por baixo. Sólidas (doc 37). */\n\
floating-sheet > sheet, bottom-sheet > sheet {{ background-color: var(--dialog-bg-color); }}\n\
/* Cor de destaque (Settings → Appearance → Accent color) */\n\
:root {{ --accent-bg-color: #{dest}; --accent-color: #{dest}; --accent-fg-color: #{fg}; }}\n\
/* Interruptor ligado: a bolinha na cor do TEXTO do destaque (no \"mono\" o fundo é branco/preto e a\n   \
bolinha branca sumiria). Campos em foco: contorno do destaque + halo de contraste (doc 42). */\n\
switch:checked > slider {{ background-color: var(--accent-fg-color); }}\n\
entry:focus-within, spinbutton:focus-within {{ box-shadow: 0 0 0 1px var(--accent-color), 0 0 0 3px {halo}; }}\n",
            rgbs = p.rgbs,
            a = p.cor_alfa,
            dest = p.dest,
            fg = p.dest_fg,
            halo = p.halo
        )
    } else {
        format!(
            "/* COR_SINC=0: apps GTK4 com as cores padrão do tema (só a cor de destaque é nossa). */\n\
:root {{ --accent-bg-color: #{dest}; --accent-color: #{dest}; --accent-fg-color: #{fg}; }}\n",
            dest = p.dest,
            fg = p.dest_fg
        )
    };
    if p.solid {
        txt.push_str(extra_solid);
    }
    txt
}

/// CSS dos apps GTK3 — `gtk-3.0/cores-tema.css` (`@define-color`, jeito antigo).
pub fn css_gtk3(p: &Paleta) -> String {
    let mut s = format!(
        "/* Gerado pelo hypr-tema (tema {tema}). Importado no gtk-3.0/gtk.css. Doc 25. */\n\
@define-color theme_selected_bg_color #{dest};\n\
@define-color theme_unfocused_selected_bg_color #{dest};\n\
@define-color theme_selected_fg_color #{fg};\n\
*:selected, *:selected:focus, row:selected, .view:selected, .view:selected:focus, iconview:selected,\n\
treeview.view:selected, placessidebar row:selected {{ background-color: #{dest}; color: #{fg}; }}\n\
row:selected label, row:selected image, .view:selected label {{ color: #{fg}; }}\n\
selection, entry selection, textview text selection, .view selection {{ background-color: #{dest}; color: #{fg}; }}\n\
entry:focus {{ border-color: #{dest}; box-shadow: inset 0 0 0 1px #{dest}, 0 0 0 2px {halo}; }}\n\
rubberband, .rubberband {{ border: 1px solid #{dest}; background-color: alpha(#{dest}, 0.15); }}\n",
        tema = p.tema,
        dest = p.dest,
        fg = p.dest_fg,
        halo = p.halo
    );
    if p.cor_sinc {
        s.push_str(&format!(
            "@define-color theme_bg_color rgba({rgbs}, {a});\n\
@define-color theme_unfocused_bg_color rgba({rgbs}, {a});\n\
@define-color theme_base_color rgba({rgbs}, 0.55);\n\
@define-color theme_unfocused_base_color rgba({rgbs}, 0.55);\n\
window.background, .background {{ background-color: @theme_bg_color; }}\n\
headerbar, .titlebar {{ background-color: @theme_bg_color; }}\n\
.sidebar, placessidebar, placessidebar list {{ background: transparent; }}\n",
            rgbs = p.rgbs,
            a = p.cor_alfa
        ));
    }
    s
}

/// A linha `cores` do estado (o script compara com ela para saber se a cor mudou).
pub fn linha_cores(p: &Paleta) -> String {
    format!(
        "{} {} {} {} {} {}",
        if p.cor_sinc { 1 } else { 0 },
        p.cor_clara,
        p.cor_escura,
        p.cor_alfa,
        p.destaque_cfg,
        p.bar_alpha
    )
}

/// Visual Solid: a paleta tonal do `solido.py` (superfícies e primária dos dois modos + o CSS GTK
/// gravado em `solido-gtk.css`). Sobrepõe COR_CLARA/COR_ESCURA/COR_DESTAQUE/alfas em `painel`.
fn sobrepor_solid(painel: &mut HashMap<String, String>, tema: &str) {
    let solido = pasta_home().join(".local/share/aero-shell/solido.py");
    if !solido.exists() {
        return;
    }
    let css = pasta_estado().join("solido-gtk.css");
    let _ = fs::create_dir_all(pasta_estado());
    let Ok(saida) = Command::new("python3")
        .arg("-I")
        .arg(&solido)
        .arg("hypr")
        .arg(tema)
        .arg(&css)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
    else {
        return;
    };
    let mut dest_escuro = None;
    let mut dest_claro = None;
    for linha in String::from_utf8_lossy(&saida.stdout).lines() {
        let Some((k, v)) = linha.split_once('=') else { continue };
        if !eh_hex6(v) {
            continue;
        }
        match k {
            "ESCURO_SURFACE" => {
                painel.insert("COR_ESCURA".into(), v.into());
            }
            "CLARO_SURFACE" => {
                painel.insert("COR_CLARA".into(), v.into());
            }
            "ESCURO_PRIMARY" => dest_escuro = Some(v.to_string()),
            "CLARO_PRIMARY" => dest_claro = Some(v.to_string()),
            _ => {}
        }
    }
    painel.insert("COR_ALFA".into(), "1.0".into());
    painel.insert("BAR_ALPHA".into(), "1.0".into());
    painel.insert("COR_SINC".into(), "1".into());
    let dest = if tema == "escuro" { dest_escuro } else { dest_claro };
    if let Some(d) = dest {
        painel.insert("COR_DESTAQUE".into(), d);
    }
}

/// Avisa as peças do shell Solid (cascata): `~/.cache/hypr-tema/cascata = {"tema","t0","origem"}`,
/// a não ser que alguém já tenha avisado esse mesmo tema há menos de 5 s.
fn cascata_avisar(tema: &str) {
    let arq = pasta_estado().join("cascata");
    let agora = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    if let Ok(atual) = fs::read_to_string(&arq)
        && let Ok(v) = serde_json::from_str::<serde_json::Value>(&atual)
        && v["tema"].as_str() == Some(tema)
        && agora - v["t0"].as_f64().unwrap_or(0.0) < 5.0
    {
        return;
    }
    let _ = fs::write(&arq, format!("{{\"tema\": \"{tema}\", \"t0\": {agora:.6}, \"origem\": \"\"}}\n"));
}

fn gsettings(args: &[&str]) {
    let _ = Command::new("gsettings")
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn gsettings_get(chave: &str) -> String {
    Command::new("gsettings")
        .args(["get", "org.gnome.desktop.interface", chave])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default()
}

/// Hermes: modo `claro` ligado/desligado (o `hermesctl mode -a/-r claro` do script, direto no D-Bus).
fn hermes_modo_claro(ligado: bool) {
    if let Ok(conn) = zbus::blocking::Connection::session() {
        let _ = conn.call_method(
            Some("com.aero.Hermes"),
            "/org/freedesktop/Notifications",
            Some("com.aero.Hermes"),
            "SetMode",
            &("claro", ligado),
        );
    }
}

/// Aplica o tema (`"escuro"`/`"claro"`): grava os arquivos, avisa gsettings/Hermes/shell e registra em
/// `~/.cache/hypr-tema/tema`. Devolve o tema que ficou gravado (lido de volta do arquivo).
pub fn aplicar(tema: &'static str) -> Result<String, String> {
    let home = pasta_home();
    let estado = pasta_estado();
    let _ = fs::create_dir_all(&estado);

    let mut painel = ler_painel();
    let solid = painel.get("VISUAL").map(String::as_str) == Some("Solid");
    if solid {
        sobrepor_solid(&mut painel, tema);
        cascata_avisar(tema);
    }
    let p = paleta(&painel, tema);

    // 1) estado: destaque (o print-area lê a cor da linha de seleção daqui)
    gravar(&estado.join("destaque"), &format!("{}\n", p.dest));
    // 2) waybar (+ atalho cores.css, só se a pasta já existe: não criamos config do waybar)
    let waybar = home.join(".config/waybar");
    if waybar.is_dir() {
        gravar(&waybar.join(format!("cores-{tema}.css")), &css_waybar(&p));
        let alvo = format!("cores-{tema}.css");
        let atalho = waybar.join("cores.css");
        if fs::read_link(&atalho).ok().and_then(|l| l.to_str().map(|s| s.to_string())) != Some(alvo.clone()) {
            let _ = fs::remove_file(&atalho);
            let _ = std::os::unix::fs::symlink(&alvo, &atalho);
        }
    }
    // 3) GTK4 (+ Solid por cima) e GTK3
    let extra = if solid {
        fs::read_to_string(estado.join("solido-gtk.css")).unwrap_or_default()
    } else {
        String::new()
    };
    gravar(&home.join(".config/gtk-4.0/cores-tema.css"), &css_gtk4(&p, &extra));
    gravar(&home.join(".config/gtk-3.0/cores-tema.css"), &css_gtk3(&p));
    // 4) nome do tema nos arquivos que alguns programas leem em vez do gsettings
    let (nome, escuro_ini) = if tema == "escuro" { ("ArchAeroX16-Dark", "true") } else { ("ArchAeroX16", "false") };
    for v in ["3.0", "4.0"] {
        gravar(
            &home.join(format!(".config/gtk-{v}/settings.ini")),
            &format!("[Settings]\ngtk-theme-name={nome}\ngtk-application-prefer-dark-theme={escuro_ini}\n"),
        );
    }
    gravar(&home.join(".gtkrc-2.0"), &format!("# Gerado pelo hypr-tema (doc 46)\ngtk-theme-name=\"{nome}\"\n"));

    // 5) avisar: Hermes (modo claro), gsettings (color-scheme e gtk-theme com o pisca do Solid)
    hermes_modo_claro(tema == "claro");
    gsettings(&[
        "set",
        "org.gnome.desktop.interface",
        "color-scheme",
        if tema == "escuro" { "prefer-dark" } else { "prefer-light" },
    ]);
    if solid && gsettings_get("gtk-theme") == format!("'{nome}'") {
        gsettings(&["set", "org.gnome.desktop.interface", "gtk-theme", "Adwaita"]);
    }
    gsettings(&["set", "org.gnome.desktop.interface", "gtk-theme", nome]);

    // 6) estado final (é daqui que `GetTheme` lê de volta)
    gravar(&estado.join("tema"), &format!("{tema}\n"));
    gravar(&estado.join("cores"), &format!("{}\n", linha_cores(&p)));

    let lido = atual();
    if lido == tema { Ok(lido) } else { Err(format!("theme file reads '{lido}' after writing '{tema}'")) }
}

#[cfg(test)]
mod testes {
    use super::*;

    fn painel(txt: &str) -> HashMap<String, String> {
        ler_painel_texto(txt)
    }

    #[test]
    fn normaliza_nomes() {
        assert_eq!(normalizar("dark"), Some("escuro"));
        assert_eq!(normalizar(" Light "), Some("claro"));
        assert_eq!(normalizar("escuro"), Some("escuro"));
        assert_eq!(normalizar("azul"), None);
        assert_eq!(nome_api("claro"), "light");
    }

    #[test]
    fn le_painel_como_o_painel_sh() {
        let m = painel("# comentário\nTEMA_MODO=auto\nWALL_ORIGINAL=\"/x/y z.jpg\"\nminusculo=1\nCOR_ALFA='1.0'\n");
        assert_eq!(m["TEMA_MODO"], "auto");
        assert_eq!(m["WALL_ORIGINAL"], "/x/y z.jpg");
        assert_eq!(m["COR_ALFA"], "1.0");
        assert!(!m.contains_key("minusculo"));
    }

    #[test]
    fn horario_programado() {
        let auto = painel("TEMA_MODO=auto\nTEMA_CLARO=07:00\nTEMA_ESCURO=18:00\n");
        assert_eq!(programado(&auto, 8 * 60), "claro");
        assert_eq!(programado(&auto, 19 * 60), "escuro");
        assert_eq!(programado(&auto, 6 * 60 + 59), "escuro");
        let fixo = painel("TEMA_MODO=claro\n");
        assert_eq!(programado(&fixo, 23 * 60), "claro");
        // passando da meia-noite: claro das 22:00 às 02:00
        let noite = painel("TEMA_MODO=auto\nTEMA_CLARO=22:00\nTEMA_ESCURO=02:00\n");
        assert_eq!(programado(&noite, 23 * 60), "claro");
        assert_eq!(programado(&noite, 3 * 60), "escuro");
        assert_eq!(minutos("lixo"), 0);
    }

    #[test]
    fn paleta_escura_padrao_e_mono() {
        let p = paleta(&painel("COR_DESTAQUE=3584e4\n"), "escuro");
        assert_eq!(p.hex, "1e1e1e");
        assert_eq!(p.rgbs, "30,30,30");
        assert_eq!(p.dest, "3584e4");
        assert_eq!(p.dest_fg, "ffffff");
        assert!(p.cor_sinc);
        let m = paleta(&painel("COR_DESTAQUE=mono\n"), "claro");
        assert_eq!((m.dest.as_str(), m.dest_fg.as_str()), ("1e1e1e", "f6f6f6"));
        // destaque claro (amarelo) → texto escuro por cima
        let a = paleta(&painel("COR_DESTAQUE=ffee00\n"), "claro");
        assert_eq!(a.dest_fg, "1e1e1e");
        assert_eq!(linha_cores(&a), "1 f6f6f6 1e1e1e 0.80 ffee00 0.45");
    }

    #[test]
    fn css_tem_as_cores() {
        let p = paleta(&painel("VISUAL=Solid\nCOR_ESCURA=101418\nCOR_ALFA=1.0\n"), "escuro");
        let g4 = css_gtk4(&p, "/* solid */\n");
        assert!(g4.contains("--window-bg-color: rgba(16,20,24, 1.0);"));
        assert!(g4.ends_with("/* solid */\n"));
        let g3 = css_gtk3(&p);
        assert!(g3.contains("@define-color theme_bg_color rgba(16,20,24, 1.0);"));
        let wb = css_waybar(&p);
        assert!(wb.contains("@define-color fundo-solido #101418;"));
    }
}
