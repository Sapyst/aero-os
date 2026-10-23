//! Módulo de taxa de atualização adaptativa (`taxa.rs`), equivalente em Rust ao `taxa.py` e `servicos/telas.py`.
//!
//! Regras de funcionamento:
//! - **Na tomada** (ou máquina de mesa sem bateria): maior taxa disponível em cada monitor (`max`).
//! - **Na bateria com jogo aberto** (quando `jogos = true`): maior taxa (`max`).
//! - **Na bateria sem jogo**: `60` Hz (economiza bastante bateria na tela) ou `max` se o usuário escolheu.
//!
//! Importante: toda conversa com o compositor (listar monitores ativos e trocar a taxa de atualização)
//! passa exclusivamente pela trait [`Compositor`](crate::compositor::Compositor). Quando o compositor
//! próprio (**Daimon**) substituir o Hyprland, este arquivo não precisa de nenhuma alteração.

use crate::bateria::{self, caminho_config_taxa, padrao_classes_jogo};
use crate::compositor::Compositor;
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs;
use std::time::{Duration, Instant};

/// Configuração lida de `~/.config/aero-shell/taxa.json`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigTaxa {
    #[serde(default = "padrao_true")]
    pub ligado: bool,
    #[serde(default = "padrao_bateria")]
    pub bateria: String,
    #[serde(default = "padrao_true")]
    pub jogos: bool,
    #[serde(default = "padrao_classes_jogo")]
    pub classes_jogo: Vec<String>,
}

fn padrao_true() -> bool {
    true
}

fn padrao_bateria() -> String {
    "60".to_string()
}

impl Default for ConfigTaxa {
    fn default() -> Self {
        Self {
            ligado: true,
            bateria: padrao_bateria(),
            jogos: true,
            classes_jogo: padrao_classes_jogo(),
        }
    }
}

pub fn ler_config() -> ConfigTaxa {
    let p = caminho_config_taxa();
    let Ok(txt) = fs::read_to_string(p) else {
        return ConfigTaxa::default();
    };
    let mut cfg = serde_json::from_str::<ConfigTaxa>(&txt).unwrap_or_default();
    if cfg.classes_jogo.is_empty() {
        cfg.classes_jogo = padrao_classes_jogo();
    }
    cfg
}

/// Estado residente do controlador de taxa de atualização dentro do `aero-sessiond`.
pub struct EstadoTaxa {
    pub cfg: ConfigTaxa,
    padroes: Vec<Regex>,
    pub jogos: HashSet<String>,
    ultimo_estado: Option<(Option<bool>, bool)>,
    reaplicar_em: Option<Instant>,
}

impl EstadoTaxa {
    pub fn novo(janelas_iniciais: &[(String, String)]) -> Self {
        let cfg = ler_config();
        let padroes = Self::compilar_padroes(&cfg.classes_jogo);
        let mut jogos = HashSet::new();
        for (end, classe) in janelas_iniciais {
            if padroes.iter().any(|re| re.is_match(classe)) {
                jogos.insert(end.clone());
            }
        }
        Self {
            cfg,
            padroes,
            jogos,
            ultimo_estado: None,
            reaplicar_em: None,
        }
    }

    fn compilar_padroes(lista: &[String]) -> Vec<Regex> {
        lista.iter().filter_map(|s| Regex::new(s).ok()).collect()
    }

    pub fn eh_jogo(&self, classe: &str) -> bool {
        self.padroes.iter().any(|re| re.is_match(classe))
    }

    /// Determina o modo alvo (`"max"` ou `"60"`) e o motivo (`"charger"`, `"game"` ou `"battery"`).
    pub fn modo(&self) -> (&'static str, &'static str) {
        let tomada = bateria::na_tomada();
        if tomada.is_none() || tomada == Some(true) {
            return ("max", "charger");
        }
        if self.cfg.jogos && !self.jogos.is_empty() {
            return ("max", "game");
        }
        let m = if self.cfg.bateria == "max" {
            "max"
        } else {
            "60"
        };
        (m, "battery")
    }

    /// Aplica a taxa-alvo em cada monitor ativo se a diferença para a taxa atual for > 0.5 Hz.
    pub fn aplicar(&mut self, compositor: &dyn Compositor) {
        self.reaplicar_em = None;
        self.ultimo_estado = Some((bateria::na_tomada(), !self.jogos.is_empty()));
        if !self.cfg.ligado {
            return;
        }
        let (modo, _) = self.modo();
        for m in compositor.monitores() {
            let hz = m.taxa_alvo(modo);
            if (hz - m.taxa_atual).abs() > 0.5 {
                compositor.definir_taxa(&m, hz);
            }
        }
    }

    /// Recarrega `~/.config/aero-shell/taxa.json` e reavalia as janelas abertas.
    pub fn recarregar(&mut self, compositor: &dyn Compositor, janelas_atuais: &[(String, String)]) {
        self.cfg = ler_config();
        self.padroes = Self::compilar_padroes(&self.cfg.classes_jogo);
        self.jogos.clear();
        for (end, classe) in janelas_atuais {
            if self.eh_jogo(classe) {
                self.jogos.insert(end.clone());
            }
        }
        self.aplicar(compositor);
    }

    /// Chamado quando a tomada muda (uevent). Só reaplica se o par `(na_tomada, tem_jogo)` mudou.
    pub fn ao_mudar_energia(&mut self, compositor: &dyn Compositor) {
        let agora = (bateria::na_tomada(), !self.jogos.is_empty());
        if self.ultimo_estado != Some(agora) {
            self.aplicar(compositor);
        }
    }

    pub fn ao_abrir_janela(&mut self, compositor: &dyn Compositor, endereco: &str, classe: &str) {
        if self.eh_jogo(classe) && self.jogos.insert(endereco.to_string()) {
            self.aplicar(compositor);
        }
    }

    pub fn ao_fechar_janela(&mut self, compositor: &dyn Compositor, endereco: &str) {
        if self.jogos.remove(endereco) {
            self.aplicar(compositor);
        }
    }

    /// Quando o Hyprland recarrega a configuração ou um monitor é conectado, esperamos 1 s
    /// para deixar `hypr-lid` e `monitores.conf` terminarem antes de reaplicar a taxa.
    pub fn agendar_reaplicacao_pos_reload(&mut self) {
        self.reaplicar_em = Some(Instant::now() + Duration::from_secs(1));
    }

    pub fn verificar_prazo(&mut self, compositor: &dyn Compositor) {
        if let Some(quando) = self.reaplicar_em
            && Instant::now() >= quando
        {
            self.aplicar(compositor);
        }
    }

    pub fn proximo_prazo(&self) -> Option<Duration> {
        self.reaplicar_em
            .map(|quando| quando.saturating_duration_since(Instant::now()))
    }
}

/// Exibe o status atual da taxa adaptativa (`aero-hz --status` / `aero-sessiond --hz-status`),
/// idêntico ao `taxa.py --status`.
pub fn cmd_status(compositor: &dyn Compositor) {
    let janelas = compositor.janelas_abertas();
    let t = EstadoTaxa::novo(&janelas);
    let (modo, motivo) = t.modo();
    let ligado_str = if t.cfg.ligado { "on" } else { "off" };
    println!(
        "{} · mode {} ({}) · games open: {}",
        ligado_str,
        modo,
        motivo,
        t.jogos.len()
    );
    for m in compositor.monitores() {
        println!(
            "  {}: {:.2} Hz -> {:.2} Hz",
            m.nome,
            m.taxa_atual,
            m.taxa_alvo(modo)
        );
    }
}
