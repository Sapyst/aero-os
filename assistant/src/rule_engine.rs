//! Rule engine with slots, conversation context, phonetic matching, confidence scoring,
//! language policy enforcement, and decision explanation.

use std::collections::HashSet;
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Language {
    English,
    Portuguese,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Slots {
    pub action: Option<String>,
    pub object: Option<String>,
    pub value: Option<String>,
    pub place: Option<String>,
    pub time: Option<String>,
}

#[derive(Debug, Clone)]
pub struct RuleCandidate {
    pub rule_name: String,
    pub score: i32,
    pub matched_slots: Vec<String>,
    pub reason: String,
}

#[derive(Debug, Clone)]
pub struct Decision {
    pub language: Language,
    pub slots: Slots,
    pub confidence: i32,
    pub matched_rule: Option<String>,
    pub resolution: Resolution,
    pub candidates: Vec<RuleCandidate>,
    pub spoken_text: String,
    pub display_text: String,
    pub needs_confirmation: bool,
    pub confirmation_question: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Resolution {
    DirectRule(String),
    ContextRule(String),
    Clarification(String),
    Model,
    CannotAnswer(String),
}

#[derive(Debug, Clone)]
pub struct ContextTurn {
    pub object: Option<String>,
    pub action: Option<String>,
    pub command: Option<String>,
    pub topic: Option<String>,
    pub timestamp: Instant,
}

static CONTEXT: Mutex<Option<ContextTurn>> = Mutex::new(None);

/// Context expires after 60 seconds.
const CONTEXT_LIFETIME: Duration = Duration::from_secs(60);

pub fn update_context(topic: Option<&str>, action: Option<&str>, command: Option<&str>) {
    let mut lock = CONTEXT.lock().unwrap();
    *lock = Some(ContextTurn {
        object: topic.map(String::from),
        action: action.map(String::from),
        command: command.map(String::from),
        topic: topic.map(String::from),
        timestamp: Instant::now(),
    });
}

pub fn get_context() -> Option<ContextTurn> {
    let mut lock = CONTEXT.lock().unwrap();
    if let Some(ctx) = lock.as_ref() {
        if ctx.timestamp.elapsed() > CONTEXT_LIFETIME {
            *lock = None;
            return None;
        }
        return Some(ctx.clone());
    }
    None
}

pub fn clear_context() {
    let mut lock = CONTEXT.lock().unwrap();
    *lock = None;
}

/// Detects language with documented criteria.
/// Policy: If uncertain or mixed, returns Language::Portuguese so the request NEVER reaches the LLM.
pub fn detect_language(text: &str) -> (Language, f32) {
    let lower = text.to_lowercase();

    // 1. Accented characters unique to Portuguese
    let pt_accents = ['á', 'à', 'ã', 'â', 'é', 'ê', 'í', 'ó', 'õ', 'ô', 'ú', 'ç'];
    let has_pt_accent = lower.chars().any(|c| pt_accents.contains(&c));
    if has_pt_accent {
        return (Language::Portuguese, 1.0);
    }

    let tokens: Vec<&str> = lower
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .collect();

    // Portuguese exclusive tokens (words that NEVER appear as valid English words)
    let pt_exclusive: HashSet<&str> = [
        "o", "os", "um", "uma", "uns", "umas", "de", "do", "da", "dos", "das",
        "em", "na", "nos", "nas", "por", "pelo", "pela", "pelos", "pelas", "para", "pra", "pro", "pras", "pros",
        "meu", "minha", "meus", "minhas", "seu", "sua", "seus", "suas", "nosso", "nossa", "nossos", "nossas",
        "este", "esta", "estes", "estas", "esse", "essa", "esses", "essas", "aquele", "aquela", "aqueles", "aquelas",
        "isto", "isso", "aquilo", "aqui", "ali", "que", "com", "sem", "sob", "sobre", "entre", "ate",
        "mas", "porem", "contudo", "todavia", "portanto", "entao", "tambem", "nem", "nao", "sim", "ja", "ainda", "nunca",
        "sempre", "talvez", "muito", "muita", "muitos", "muitas", "pouco", "pouca", "poucos", "poucas", "mais", "menos",
        "como", "onde", "aonde", "quando", "quanto", "quanta", "quantos", "quantas", "qual", "quais", "quem", "porque", "por que", "oque",
        "voce", "voces", "eu", "tu", "ele", "ela", "eles", "elas",
        "estou", "estamos", "estao", "ta", "sou", "somos", "sao", "era", "eram", "foi", "foram",
        "tem", "ter", "tenho", "temos", "fazer", "faca", "faz", "fazem",
        "ver", "veja", "olha", "olhe", "mostra", "mostrar", "mostre", "baixa", "abaixar", "abaixe", "aumenta", "aumentar", "aumente",
        "muda", "mudar", "mude", "altera", "alterar", "altere", "verifica", "verificar", "verifique", "checar", "cheque",
        "explique", "explica", "explicar", "conte", "diga", "fale", "ajude", "ajudar", "ajuda",
        "bateria", "brilho", "memoria", "disco", "rede", "tela", "processos", "erros", "servicos",
        "pacotes", "pacote", "atualizacoes", "atualizacao", "desligar", "desliga", "reiniciar", "reinicia", "ligar", "liga",
        "obrigado", "obrigada", "valeu", "bom", "dia", "tarde", "noite", "ola", "oi",
        "janela", "arquivo", "arquivos", "pasta", "pastas", "diretorio", "diretorios", "sistema", "comando",
    ].into_iter().collect();

    // English exclusive tokens (words that NEVER appear as valid Portuguese words)
    let en_exclusive: HashSet<&str> = [
        "the", "an", "this", "that", "these", "those", "my", "your", "our", "its", "their",
        "in", "on", "at", "by", "for", "with", "about", "into", "to", "from", "up", "down", "of", "off", "over", "under", "out",
        "and", "but", "or", "so", "as", "if", "then", "not",
        "where", "when", "why", "how", "what", "which", "who", "whom", "whose",
        "is", "are", "am", "was", "were", "be", "been", "being",
        "have", "has", "had", "having",
        "do", "does", "did", "doing",
        "can", "could", "should", "would", "will", "shall", "may", "might", "must",
        "hello", "hi", "hey", "good", "morning", "afternoon", "evening", "thanks", "thank", "you", "please",
        "explain", "describe", "define", "show", "tell", "check", "list", "display", "help", "find", "search",
        "create", "delete", "remove", "install", "update", "run", "start", "stop", "restart", "enable", "disable", "configure", "fix", "clean", "set",
        "timer", "timers", "subvolume", "subvolumes", "hook", "hooks", "systemd", "pacman", "btrfs", "kernel",
        "file", "files", "folder", "folders", "directory", "directories", "package", "packages", "service", "services", "command", "process", "processes",
        "disk", "memory", "battery", "network", "brightness", "terminal", "script", "config", "configuration", "uptime", "system",
        "again", "much", "many", "free", "used", "available", "left", "total", "details"
    ].into_iter().collect();

    let mut pt_count = 0;
    let mut en_count = 0;

    for t in &tokens {
        if pt_exclusive.contains(t) {
            pt_count += 1;
        }
        if en_exclusive.contains(t) {
            en_count += 1;
        }
    }

    // Disambiguate common shared tokens like "a" and "no"
    for t in &tokens {
        if *t == "a" {
            if en_count > 0 && pt_count == 0 {
                en_count += 1;
            } else if pt_count > 0 && en_count == 0 {
                pt_count += 1;
            }
        } else if *t == "no" {
            // "no" in English: "no" / negation; in PT: "em + o"
            if en_count > 0 && pt_count == 0 {
                en_count += 1;
            } else if pt_count > 0 && en_count == 0 {
                pt_count += 1;
            }
        }
    }

    let total = pt_count + en_count;
    if total == 0 {
        // Truly uncertain: return low confidence (0.40) so composer asks in both languages
        return (Language::Portuguese, 0.40);
    }

    // Mixed language: policy requires treating as Portuguese so it never reaches the model
    if pt_count > 0 && en_count > 0 {
        return (Language::Portuguese, 0.60);
    }

    if pt_count > 0 && en_count == 0 {
        let conf = (0.75 + (pt_count as f32 * 0.05)).min(1.0);
        return (Language::Portuguese, conf);
    }

    if en_count > 0 && pt_count == 0 {
        let conf = (0.75 + (en_count as f32 * 0.05)).min(1.0);
        return (Language::English, conf);
    }

    (Language::Portuguese, 0.40)
}

fn strip_accents(s: &str) -> String {
    s.chars()
        .map(|c| match c {
            'á' | 'à' | 'ã' | 'â' => 'a',
            'é' | 'ê' => 'e',
            'í' => 'i',
            'ó' | 'õ' | 'ô' => 'o',
            'ú' | 'ü' => 'u',
            'ç' => 'c',
            other => other.to_ascii_lowercase(),
        })
        .collect()
}

pub fn is_stopword(token: &str) -> bool {
    let clean = token.trim_matches(|c: char| !c.is_alphanumeric()).to_lowercase();
    let unaccented = strip_accents(&clean);
    let stopwords = [
        "the", "a", "an", "this", "that", "these", "those", "my", "your", "is", "are", "of", "in", "on", "at", "for", "to", "now", "please", "me", "it",
        "has", "been", "have", "had", "how", "much", "many", "what", "which", "left", "free", "used", "active", "running", "details", "machine",
        "o", "os", "a", "as", "um", "uma", "uns", "umas", "meu", "minha", "meus", "minhas", "seu", "sua", "de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "por", "para", "pra", "esta", "está", "estou", "agora", "por favor", "com", "e", "ou", "se", "que", "eu", "ele", "ela", "isso", "esse", "essa", "este", "sistema", "system", "pc",
        "quanto", "quanta", "quantos", "quantas", "qual", "quais", "livre", "usando", "usado", "ativo", "rodando", "atual", "algum", "alguma", "disponivel", "disponível"
    ];
    stopwords.contains(&clean.as_str()) || stopwords.contains(&unaccented.as_str())
}

/// Hard safety gate: returns true ONLY if language is unambiguously English.
/// Must be confident English and contain recognized English question/greeting/system markers.
pub fn allow_to_model(text: &str) -> bool {
    let (lang, conf) = detect_language(text);
    if lang != Language::English || conf < 0.70 {
        return false;
    }

    let tokens: Vec<String> = text.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect();

    if tokens.is_empty() {
        return false;
    }

    let en_markers = [
        "what", "how", "why", "when", "where", "which", "who", "whom", "whose",
        "can", "could", "would", "should", "will", "is", "are", "do", "does", "did",
        "explain", "describe", "define", "tell", "help", "hello", "hi", "hey",
        "systemd", "timer", "timers", "pacman", "hook", "hooks", "btrfs", "subvolume", "subvolumes", "kernel",
        "linux", "arch", "pipewire", "hyprland", "wayland", "desktop", "command", "check", "show", "list"
    ];

    tokens.iter().any(|t| en_markers.contains(&t.as_str()))
}

/// Phonetic normalization for known system terms to recover speech recognition errors.
pub fn phonetic_canonical(token: &str) -> &'static str {
    let clean = token.trim_matches(|c: char| !c.is_alphanumeric()).to_lowercase();
    let unacc = strip_accents(&clean);
    match unacc.as_str() {
        // Battery
        "batery" | "betery" | "bataria" | "batary" | "bateri" => "battery",
        // Brightness
        "brightnes" | "briteness" | "bright" | "brigh" | "brigtness" | "brilo" | "briro" => "brightness",
        // Memory
        "memori" | "menoria" | "memmory" | "memry" => "memory",
        // Kernel
        "kernal" | "cernel" | "kurnel" => "kernel",
        // Volume
        "volum" | "volumi" => "volume",
        // Updates
        "updats" | "updaits" => "updates",
        // Network
        "netwrok" | "netwrk" => "network",
        // Processes
        "prosses" | "proceces" | "prossecos" => "processes",
        _ => "",
    }
}

/// Extracts slots from user utterance.
pub fn extract_slots(text: &str) -> Slots {
    let lower = text.to_lowercase();
    let lower_unacc = strip_accents(&lower);
    let raw_tokens: Vec<&str> = text.split_whitespace().collect();
    let tokens: Vec<String> = raw_tokens
        .iter()
        .map(|w| strip_accents(w.trim_matches(|c: char| !c.is_alphanumeric())))
        .filter(|s| !s.is_empty())
        .collect();

    let mut action = None;
    let mut object = None;
    let mut value = None;
    let place = None;
    let mut time = None;

    // 1. Actions
    let action_map: &[(&[&str], &str)] = &[
        (&[
            "show", "display", "check", "view", "what is", "what are", "what", "which", "how is", "how much", "how long", "how bright", "how",
            "is the", "are there", "list", "level", "percentage", "status", "info", "details",
            "mostra", "mostrar", "mostre", "ver", "veja", "qual", "quais", "como esta", "como está", "quanto", "quanta",
            "quantos", "quantas", "tem algum", "tem alguma", "listar", "status", "nivel", "nível", "porcentagem",
            "verificar", "checar",
        ], "read"),
        (&["set", "change", "adjust", "muda", "mudar", "altera", "alterar", "definir", "coloca", "colocar", "ajusta", "ajustar"], "set"),
        (&["lower", "decrease", "dim", "baixa", "baixar", "abaixa", "abaixar", "diminui", "diminuir"], "lower"),
        (&["raise", "increase", "brighten", "aumenta", "aumentar"], "raise"),
        (&["install", "instalar", "instale", "upgrade", "atualizar", "atualiza", "atualize"], "install"),
        (&["remove", "uninstall", "remover", "remova", "desinstalar"], "remove"),
        (&["repeat", "again", "de novo", "novamente"], "repeat"),
    ];

    for (syns, act) in action_map {
        for s in *syns {
            let s_unacc = strip_accents(s);
            if s.contains(' ') {
                if lower.contains(s) || lower_unacc.contains(&s_unacc) {
                    action = Some(act.to_string());
                    break;
                }
            } else {
                if tokens.iter().any(|t| t == &s_unacc) {
                    action = Some(act.to_string());
                    break;
                }
            }
        }
        if action.is_some() {
            break;
        }
    }

    // 2. Objects
    let object_map: &[(&[&str], &str)] = &[
        (&["battery", "bateria"], "battery"),
        (&["brightness", "screen brightness", "brilho"], "brightness"),
        (&["memory", "ram", "memoria", "memória", "swap"], "memory"),
        (&["cpu", "processador"], "cpu"),
        (&["gpu", "placa de video", "placa de vídeo"], "gpu"),
        (&["disk", "storage", "disco", "armazenamento", "root", "espaco", "espaço", "espaco livre", "espaço livre"], "disk"),
        (&["uptime", "tempo de atividade", "tempo ligado", "ligado", "pc ligado", "machine up", "been up", "up"], "uptime"),
        (&["network", "ip", "wifi", "internet", "rede", "conexao", "conexão", "ip address", "endereco ip", "endereço ip", "connection"], "network"),
        (&["bluetooth"], "bluetooth"),
        (&["audio", "sound", "volume", "som"], "audio"),
        (&["power", "profile", "energia", "modo de energia", "perfil de energia"], "power"),
        (&["updates", "atualizacoes", "atualizações", "atualizacao", "atualização", "packages to update", "pacotes para atualizar"], "updates"),
        (&["packages", "pacotes", "installed packages"], "packages"),
        (&["processes", "process", "processos", "processo", "top processes", "processos principais", "active processes"], "processes"),
        (&["errors", "error", "erros", "erro", "failed services", "falhas", "servicos falharam", "serviços falharam"], "errors"),
        (&["kernel", "versao do kernel", "versão do kernel", "kernel version", "linux version", "versao do linux", "versão do linux", "linux"], "kernel"),
    ];

    for (syns, obj) in object_map {
        for s in *syns {
            let s_unacc = strip_accents(s);
            if s.contains(' ') {
                if lower.contains(s) || lower_unacc.contains(&s_unacc) {
                    object = Some(obj.to_string());
                    break;
                }
            } else {
                if tokens.iter().any(|t| t == &s_unacc) {
                    object = Some(obj.to_string());
                    break;
                }
            }
        }
        if object.is_some() {
            break;
        }
    }

    // Check phonetic match if object not found yet
    if object.is_none() {
        for w in &raw_tokens {
            let canon = phonetic_canonical(w);
            if !canon.is_empty() {
                for (syns, obj) in object_map {
                    if syns.contains(&canon) {
                        object = Some(obj.to_string());
                        break;
                    }
                }
            }
            if object.is_some() {
                break;
            }
        }
    }

    // 3. Numeric values
    for w in &raw_tokens {
        let clean = w.trim_matches(|c: char| !c.is_ascii_digit());
        if !clean.is_empty() && clean.chars().all(|c| c.is_ascii_digit()) {
            value = Some(clean.to_string());
            break;
        }
    }

    // 4. Place / Time
    if lower.contains("now") || lower.contains("agora") || lower.contains("today") || lower.contains("hoje") {
        time = Some("now".into());
    }

    Slots { action, object, value, place, time }
}

/// Evaluates utterance against rule catalog, assigns confidence score, and determines resolution.
pub fn evaluate_decision(text: &str) -> Decision {
    let (lang, lang_conf) = detect_language(text);
    let slots = extract_slots(text);
    let words: Vec<&str> = text.split_whitespace().collect();

    // Check for truly uncertain language (< 0.50)
    if lang_conf < 0.50 {
        let question = "Could you please repeat in English or Portuguese? / Por favor, repita em inglês ou português.";
        return Decision {
            language: Language::Portuguese, // Default to Portuguese for safety so it NEVER reaches the model
            slots,
            confidence: 30,
            matched_rule: None,
            resolution: Resolution::Clarification(question.into()),
            candidates: vec![],
            spoken_text: "Please repeat in English or Portuguese. Por favor repita em inglês ou português.".into(),
            display_text: question.into(),
            needs_confirmation: false,
            confirmation_question: None,
        };
    }

    let mut candidates = Vec::new();
    let mut best_rule: Option<String> = None;
    let mut best_score = 0;
    let mut resolution = Resolution::Model;
    let mut needs_confirmation = false;
    let mut confirmation_question = None;
    let mut spoken_text = String::new();
    let mut display_text = String::new();

    // Context resolution: check if slot object can be inherited
    let mut effective_object = slots.object.clone();
    let mut context_used = false;

    if effective_object.is_none() {
        let lower = text.to_lowercase();
        let is_followup = lower.contains(" it") || lower.contains(" that") || lower.contains(" this")
            || lower.contains("isso") || lower.contains("ele") || lower.contains("ela")
            || slots.action.as_deref() == Some("repeat")
            || words.len() <= 2;
        if is_followup {
            if let Some(ctx) = get_context() {
                if slots.action.as_deref() == Some("repeat") {
                    effective_object = ctx.object.clone();
                    context_used = true;
                } else if matches!(slots.action.as_deref(), Some("set") | Some("lower") | Some("raise") | Some("read")) {
                    if let Some(ctx_obj) = ctx.object {
                        effective_object = Some(ctx_obj);
                        context_used = true;
                    }
                }
            }
        }
    }

    // Evaluate known rules
    let topics = [
        ("battery", "Battery status and charge level", "read:battery"),
        ("brightness", "Screen brightness level", "read:brightness"),
        ("memory", "RAM and swap memory usage", "read:memory"),
        ("cpu", "CPU load and temperature", "read:cpu"),
        ("gpu", "GPU utilization and VRAM", "read:gpu"),
        ("disk", "Filesystem disk space", "read:disk"),
        ("uptime", "System uptime", "read:uptime"),
        ("network", "Network IP and interface status", "read:network"),
        ("power", "Power profile", "read:power"),
        ("updates", "Pending repository updates", "read:updates"),
        ("processes", "Top active processes", "read:processes"),
        ("errors", "System error logs and failed units", "read:errors"),
        ("kernel", "Linux kernel release", "read:kernel"),
    ];

    for (topic, _desc, rule_id) in &topics {
        let mut score = 0;
        let mut matched_slots = Vec::new();
        let mut reason_parts = Vec::new();

        if effective_object.as_deref() == Some(*topic) {
            score += 45;
            matched_slots.push(format!("object={topic}"));
            reason_parts.push("object matched".into());

            let has_read = slots.action.as_deref() == Some("read");
            let content_words: Vec<&str> = words.iter().filter(|w| !is_stopword(w)).cloned().collect();
            let is_implied_read = slots.action.is_none() && !content_words.is_empty() && content_words.iter().all(|w| {
                let unacc = strip_accents(w);
                let syns: &[&str] = match *topic {
                    "battery" => &["battery", "bateria"],
                    "brightness" => &["brightness", "screen", "brilho", "tela"],
                    "memory" => &["memory", "ram", "memoria", "memória", "swap"],
                    "cpu" => &["cpu", "processador"],
                    "gpu" => &["gpu", "placa", "video", "vídeo"],
                    "disk" => &["disk", "storage", "disco", "armazenamento", "root", "espaco", "espaço"],
                    "uptime" => &["uptime", "tempo", "atividade", "ligado"],
                    "network" => &["network", "ip", "wifi", "internet", "rede", "conexao", "conexão", "connection"],
                    "power" => &["power", "profile", "energia", "perfil"],
                    "updates" => &["updates", "atualizacoes", "atualizações", "atualizacao", "atualização"],
                    "processes" => &["processes", "process", "processos", "processo", "top"],
                    "errors" => &["errors", "error", "erros", "erro"],
                    "kernel" => &["kernel", "linux", "versao", "versão", "version"],
                    _ => &[],
                };
                syns.iter().any(|s| strip_accents(s) == unacc)
            });

            if has_read {
                score += 30;
                matched_slots.push("action=read".into());
                reason_parts.push("read action matched".into());
            } else if is_implied_read {
                score += 25;
                matched_slots.push("action=implied_read".into());
                reason_parts.push("implied read for topic query".into());
            }

            if context_used {
                score += 15;
                reason_parts.push("context inherited".into());
            }

            // Word penalty: count content words not in matched slots
            let extra_content = content_words.len().saturating_sub(matched_slots.len() + 1);
            if extra_content > 0 {
                let mult = if has_read { 5 } else { 15 };
                let penalty = (extra_content as i32) * mult;
                score -= penalty;
                reason_parts.push(format!("penalty for {extra_content} extra content words (-{penalty})"));
            }

            let mut final_rule_id = rule_id.to_string();
            if *topic == "updates" && slots.action.as_deref() == Some("install") {
                final_rule_id = "run:system-update".to_string();
            } else if *topic == "brightness" {
                if slots.action.as_deref() == Some("lower") {
                    final_rule_id = "run:lower-brightness".to_string();
                    score += 35;
                } else if slots.action.as_deref() == Some("raise") {
                    final_rule_id = "run:raise-brightness".to_string();
                    score += 35;
                }
            } else if *topic == "audio" {
                if slots.action.as_deref() == Some("lower") {
                    final_rule_id = "run:lower-volume".to_string();
                    score += 35;
                } else if slots.action.as_deref() == Some("raise") {
                    final_rule_id = "run:raise-volume".to_string();
                    score += 35;
                }
            }

            candidates.push(RuleCandidate {
                rule_name: final_rule_id.clone(),
                score,
                matched_slots,
                reason: reason_parts.join("; "),
            });

            if score > best_score {
                best_score = score;
                best_rule = Some(final_rule_id);
            }
        }
    }

    // Clarification for action=set with missing value or missing object
    if slots.action.as_deref() == Some("set") {
        if best_score < 55 {
            best_score = 55;
        }
    }

    // Check brightness control action
    if effective_object.as_deref() == Some("brightness") {
        if let Some(val_str) = &slots.value {
            if let Ok(n) = val_str.parse::<u32>() {
                if n <= 100 {
                    let score = 85 + if context_used { 10 } else { 0 };
                    let rule_name = format!("run:brightness-set-{n}");
                    candidates.push(RuleCandidate {
                        rule_name: rule_name.clone(),
                        score,
                        matched_slots: vec!["object=brightness".into(), format!("value={n}")],
                        reason: "Brightness value in valid range 0..100".into(),
                    });
                    if score > best_score {
                        best_score = score;
                        best_rule = Some(rule_name);
                    }
                }
            }
        }
    }

    // Sort candidates descending by score
    candidates.sort_by(|a, b| b.score.cmp(&a.score));

    // Decision Logic
    let confidence = best_score;

    if confidence >= 70 {
        if let Some(ref r) = best_rule {
            if context_used {
                resolution = Resolution::ContextRule(r.clone());
                needs_confirmation = true;
                if lang == Language::Portuguese {
                    confirmation_question = Some(format!("Você quer que eu verifique {}, correto?", effective_object.as_deref().unwrap_or("isso")));
                } else {
                    confirmation_question = Some(format!("You want to check {}, right?", effective_object.as_deref().unwrap_or("that")));
                }
            } else {
                resolution = Resolution::DirectRule(r.clone());
            }
        }
    } else if confidence >= 40 {
        // Needs clarification
        let question = if lang == Language::Portuguese {
            if effective_object.is_some() && slots.value.is_none() && slots.action.as_deref() == Some("set") {
                format!("Entendi que você quer ajustar {}, mas para qual valor? Você pode dizer um número de 0 a 100.", effective_object.as_deref().unwrap_or("o item"))
            } else if effective_object.is_none() && slots.value.is_some() && slots.action.as_deref() == Some("set") {
                let v = slots.value.as_deref().unwrap_or("");
                format!("Entendi que você quer ajustar para {v}, mas qual item você deseja alterar? Você pode ajustar o brilho ou o volume.")
            } else {
                "Poderia esclarecer o que você deseja verificar?".into()
            }
        } else {
            if effective_object.is_some() && slots.value.is_none() && slots.action.as_deref() == Some("set") {
                format!("I understood you want to change {}, but to what value? You can say a number from 0 to 100.", effective_object.as_deref().unwrap_or("it"))
            } else if effective_object.is_none() && slots.value.is_some() && slots.action.as_deref() == Some("set") {
                let v = slots.value.as_deref().unwrap_or("");
                format!("I understood you want to set {v}, but which item do you want to change? You can adjust brightness or volume.")
            } else {
                "Could you please clarify what you would like to check?".into()
            }
        };
        resolution = Resolution::Clarification(question.clone());
        spoken_text = question.clone();
        display_text = question;
    } else {
        // Low confidence (< 40)
        if lang == Language::Portuguese || !allow_to_model(text) {
            // HARD POLICY: Portuguese requests NEVER go to the model!
            let cannot_msg = "Não compreendi com certeza o seu pedido pelas regras locais. Você pode perguntar sobre bateria, memória, brilho ou rede.".to_string();
            resolution = Resolution::CannotAnswer(cannot_msg.clone());
            spoken_text = cannot_msg.clone();
            display_text = cannot_msg;
        } else {
            resolution = Resolution::Model;
            spoken_text = String::new();
            display_text = format!("Sent to English model: {text}");
        }
    }

    Decision {
        language: lang,
        slots,
        confidence,
        matched_rule: best_rule,
        resolution,
        candidates,
        spoken_text,
        display_text,
        needs_confirmation,
        confirmation_question,
    }
}

/// CLI command that explains a decision for testing: which rule matched, the score and why others lost.
pub fn explain_decision(text: &str) -> String {
    let d = evaluate_decision(text);
    let mut out = String::new();
    out.push_str(&format!("Request: \"{text}\"\n"));
    out.push_str(&format!("Detected Language: {:?}\n", d.language));
    out.push_str(&format!("Extracted Slots: action={:?}, object={:?}, value={:?}, time={:?}\n",
        d.slots.action, d.slots.object, d.slots.value, d.slots.time));
    out.push_str(&format!("Confidence Score: {}\n", d.confidence));
    out.push_str(&format!("Resolution: {:?}\n", d.resolution));

    if d.needs_confirmation {
        out.push_str(&format!("Requires Confirmation: Yes ({:?})\n", d.confirmation_question));
    }

    out.push_str("\nRule Candidates Evaluated:\n");
    if d.candidates.is_empty() {
        out.push_str("  (none)\n");
    } else {
        for (i, c) in d.candidates.iter().enumerate() {
            out.push_str(&format!("  {}. Rule '{}': score={} [matched: {}] -> {}\n",
                i + 1, c.rule_name, c.score, c.matched_slots.join(", "), c.reason));
        }
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static TEST_CONTEXT_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn test_language_detection() {
        assert_eq!(detect_language("show the battery level").0, Language::English);
        assert_eq!(detect_language("how much RAM is used").0, Language::English);
        assert_eq!(detect_language("qual a bateria atual").0, Language::Portuguese);
        assert_eq!(detect_language("quanto de memória está livre").0, Language::Portuguese);
        assert_eq!(detect_language("muda o brilho para cinquenta").0, Language::Portuguese);

        // Mixed or ambiguous defaults to Portuguese
        assert_eq!(detect_language("show a bateria agora").0, Language::Portuguese);
    }

    #[test]
    fn test_english_open_questions() {
        let _guard = TEST_CONTEXT_LOCK.lock().unwrap();
        clear_context();
        // Specifically requested in Defect 2:
        let q1 = "What is a systemd timer?";
        let q2 = "hello";
        let q3 = "explain btrfs subvolumes";
        let q4 = "what is a pacman hook";

        assert_eq!(detect_language(q1).0, Language::English);
        assert_eq!(detect_language(q2).0, Language::English);
        assert_eq!(detect_language(q3).0, Language::English);
        assert_eq!(detect_language(q4).0, Language::English);

        assert!(allow_to_model(q1));
        assert!(allow_to_model(q2));
        assert!(allow_to_model(q3));
        assert!(allow_to_model(q4));

        let d1 = evaluate_decision(q1);
        assert_eq!(d1.resolution, Resolution::Model);

        let d2 = evaluate_decision(q2);
        assert_eq!(d2.resolution, Resolution::Model);

        let d3 = evaluate_decision(q3);
        assert_eq!(d3.resolution, Resolution::Model);

        let d4 = evaluate_decision(q4);
        assert_eq!(d4.resolution, Resolution::Model);
    }

    #[test]
    fn test_language_detection_200_phrases() {
        let en_phrases = [
            "What is a systemd timer?", "hello", "explain btrfs subvolumes", "what is a pacman hook",
            "how do I configure pipewire", "explain how swap works", "why is the disk full", "what is a cgroup",
            "how to list systemd units", "explain memory management", "what is the kernel version", "show the battery",
            "check the memory", "lower the brightness", "raise the volume", "good morning assistant",
            "tell me about arch linux", "how does pacman work", "what are system packages", "check network connection",
            "display current brightness", "is there any system update", "show the uptime of this computer", "how much storage is left on the disk",
            "list all running processes", "check for system errors", "show memory usage now", "what is my ip address",
            "how to restart bluetooth service", "can you explain systemd journals", "hello assistant can you help me", "what is wayland",
            "how to check gpu usage", "display cpu load average", "set brightness to 50", "increase the sound volume",
            "turn off wifi", "enable bluetooth connection", "show disk space", "what is zram",
            "how to clear the package cache", "explain aur packages", "check failed services", "describe the boot process",
            "what is a systemd service", "how does pipewire routing work", "show power profile", "tell me the details",
            "what is hyprland", "how to reload the desktop theme", "where are configuration files stored", "how to mount a btrfs filesystem",
            "explain arch linux rolling release", "how to inspect hardware errors", "show available updates", "why is the machine slow",
            "check wireless connection status", "explain how linux permissions work", "what is sudo", "how to check open ports",
            "show battery health percentage", "explain systemd socket activation", "what is wireplumber", "how to use pacman to search packages",
            "display screen resolution", "what is linux kernel release", "explain difference between x11 and wayland", "show disk partitions",
            "how to monitor system temperature", "explain tmpfiles configuration", "what is a desktop portal", "check audio volume level",
            "how to rebuild initramfs", "show nvidia gpu temperature", "explain linux virtual memory", "how to kill a frozen process",
            "what is pipewire pulse", "check for broken packages", "how to read kernel dmesg", "explain user systemd units",
            "show system swap usage", "how to configure network manager", "what is systemd resolved", "explain btrfs snapshots",
            "how to set screen brightness", "show top cpu consumers", "explain arch linux pacman hooks", "how to check battery discharging rate",
            "what is polkit in linux", "explain systemd target units", "show memory free right now", "how to view failed systemd services",
            "what is dbus in linux", "explain the proc filesystem", "how to check filesystem usage", "show bluetooth device status",
            "explain linux page cache", "how to disable network manager", "what is a linux daemon", "show system uptime details",
        ];

        let pt_phrases = [
            "o que é um timer do systemd", "olá", "explique os subvolumes do btrfs", "o que é um hook do pacman",
            "como configuro o pipewire", "mostra a bateria", "verificar a memória", "abaixar o brilho",
            "aumentar o volume", "desligar o computador", "bom dia assistente", "como funciona o aero os",
            "o que são pacotes do sistema", "verificar a conexão de rede", "mostrar o brilho da tela", "tem alguma atualização do sistema",
            "mostre o tempo de atividade", "quanto espaço livre tem no disco", "listar os processos principais", "checar erros no sistema",
            "mostrar o uso de memória agora", "qual é o meu endereço de ip", "como reiniciar o serviço de bluetooth", "pode me explicar o systemd",
            "olá assistente pode me ajudar", "o que é o wayland", "como verificar o uso da gpu", "mostrar a carga da cpu",
            "definir o brilho para 50", "aumentar o volume do som", "desativar o wifi", "ativar a conexão bluetooth",
            "mostrar espaço em disco", "o que é zram", "como limpar o cache de pacotes", "explique o que é o aur",
            "verificar serviços que falharam", "descrever o processo de inicialização", "o que é um serviço do systemd", "como funciona o roteamento do pipewire",
            "mostrar o modo de energia", "mostrar os detalhes do sistema", "o que é o hyprland", "como recarregar o tema do sistema",
            "onde ficam os arquivos de configuração", "como montar um sistema de arquivos btrfs", "explique o modelo rolling release do arch linux", "como inspecionar erros de hardware",
            "mostrar atualizações disponíveis", "por que a máquina está lenta", "verificar o status da conexão sem fio", "explique como funcionam as permissões no linux",
            "o que é o sudo", "como verificar portas abertas", "mostrar a saúde da bateria", "explique a ativação por socket do systemd",
            "o que é o wireplumber", "como pesquisar pacotes no pacman", "mostrar a resolução da tela", "qual a versão do kernel linux",
            "explique a diferença entre x11 e wayland", "mostrar as partições do disco", "como monitorar a temperatura do sistema", "explique os arquivos tmpfiles",
            "o que é um desktop portal", "verificar o nível do volume de áudio", "como reconstruir o initramfs", "mostrar a temperatura da gpu nvidia",
            "explique a memória virtual no linux", "como encerrar um processo travado", "o que é o pipewire pulse", "verificar se há pacotes quebrados",
            "como ler os logs do dmesg", "explique as unidades do systemd de usuário", "mostrar o uso de swap do sistema", "como configurar o network manager",
            "o que é o systemd resolved", "explique os snapshots do btrfs", "como ajustar o brilho da tela", "mostrar processos que mais consomem cpu",
            "explique os hooks do pacman", "como verificar a taxa de descarga da bateria", "o que é o polkit no linux", "explique os targets do systemd",
            "mostrar memória livre agora", "como ver serviços do systemd com falha", "o que é o dbus no linux", "explique o sistema de arquivos proc",
            "como verificar o uso do disco", "mostrar o status do bluetooth", "explique o page cache do linux", "como desativar o network manager",
            "o que é um daemon no linux", "mostrar detalhes do tempo de atividade",
            "como verificar o kernel linux", "mostrar a placa de vídeo", "como listar arquivos do sistema",
            "qual o uso de memória ram", "como abrir o terminal", "mostrar processos ativos",
        ];

        assert_eq!(en_phrases.len(), 100);
        assert_eq!(pt_phrases.len(), 100);

        let mut en_correct = 0;
        let mut pt_correct = 0;
        let mut pt_to_model = 0;

        for p in &en_phrases {
            let (lang, _conf) = detect_language(p);
            if lang == Language::English {
                en_correct += 1;
            } else {
                eprintln!("EN phrase classified as {:?}: '{}'", lang, p);
            }
        }

        for p in &pt_phrases {
            let (lang, _conf) = detect_language(p);
            if lang == Language::Portuguese {
                pt_correct += 1;
            } else {
                eprintln!("PT phrase classified as {:?}: '{}'", lang, p);
            }
            if allow_to_model(p) {
                pt_to_model += 1;
            }
        }

        let total = en_phrases.len() + pt_phrases.len();
        let total_correct = en_correct + pt_correct;
        let accuracy = (total_correct as f32 / total as f32) * 100.0;

        eprintln!("Language Detection Labeled Benchmark (200 phrases):");
        eprintln!("  Total Phrases:       {total}");
        eprintln!("  English Accuracy:    {en_correct}/100");
        eprintln!("  Portuguese Accuracy: {pt_correct}/100");
        eprintln!("  Overall Accuracy:    {accuracy:.1}%");
        eprintln!("  Portuguese to Model: {pt_to_model} (must be 0)");

        assert_eq!(pt_to_model, 0, "Portuguese request must NEVER be allowed to the model!");
        assert!(accuracy >= 99.0, "Language detection accuracy must be >= 99%");
    }

    #[test]
    fn test_slots_and_phonetic() {
        let s1 = extract_slots("show the battery");
        assert_eq!(s1.action, Some("read".into()));
        assert_eq!(s1.object, Some("battery".into()));

        // Phonetic variation: "batery"
        let s2 = extract_slots("check batery");
        assert_eq!(s2.object, Some("battery".into()));
    }

    #[test]
    fn test_context_resolution() {
        let _guard = TEST_CONTEXT_LOCK.lock().unwrap();
        clear_context();
        update_context(Some("brightness"), Some("read"), None);

        let d = evaluate_decision("lower it to 40");
        assert!(d.confidence >= 70);
        assert!(d.needs_confirmation);
    }

    #[test]
    fn test_explain_decision() {
        let exp = explain_decision("show the battery");
        assert!(exp.contains("read:battery"));
        assert!(exp.contains("Confidence Score"));
    }
}
