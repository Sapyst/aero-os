//! Answer composer without the model: deterministic natural spoken sentences from exact system facts,
//! paired with the "Cannot Answer" composer for both Portuguese and English.

use crate::rule_engine::Language;

#[derive(Debug, Clone)]
pub struct ComposedAnswer {
    pub spoken: String,
    pub display: String,
    pub details: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CannotAnswerReason {
    NotHeardClearly,
    NotUnderstood,
    NoRuleExists,
    NotAllowed,
    MissingInformation,
}

/// Parses exact numbers and facts from system_info output without inventing any data.
pub fn compose_fact_answer(topic: &str, raw_output: &str, lang: Language) -> ComposedAnswer {
    let lower = raw_output.to_lowercase();
    let display = raw_output.trim().to_string();

    match topic {
        "battery" => {
            // e.g. "85%, discharging" or "100%, charging"
            let pct = extract_percentage(raw_output);
            let state = if lower.contains("discharging") || lower.contains("descarregando") {
                if lang == Language::Portuguese { "descarregando" } else { "discharging" }
            } else if lower.contains("charging") || lower.contains("carregando") {
                if lang == Language::Portuguese { "e carregando" } else { "and charging" }
            } else {
                ""
            };

            let spoken = if let Some(n) = pct {
                if lang == Language::Portuguese {
                    if state.is_empty() {
                        format!("A bateria está em {n} por cento.")
                    } else {
                        format!("A bateria está em {n} por cento {state}.")
                    }
                } else {
                    if state.is_empty() {
                        format!("Battery is at {n} percent.")
                    } else {
                        format!("Battery is at {n} percent {state}.")
                    }
                }
            } else {
                if lang == Language::Portuguese {
                    "Não foi possível obter o nível exato da bateria.".into()
                } else {
                    "Could not determine the exact battery level.".into()
                }
            };

            ComposedAnswer { spoken, display, details: None }
        }

        "brightness" => {
            let pct = extract_percentage(raw_output).or_else(|| extract_first_number(raw_output));
            let spoken = if let Some(n) = pct {
                if lang == Language::Portuguese {
                    format!("O brilho da tela está em {n} por cento.")
                } else {
                    format!("Screen brightness is at {n} percent.")
                }
            } else {
                if lang == Language::Portuguese {
                    "Não foi possível ler o brilho atual da tela.".into()
                } else {
                    "Could not read the current screen brightness.".into()
                }
            };
            ComposedAnswer { spoken, display, details: None }
        }

        "memory" => {
            let spoken = if let Some((total, used, free, avail)) = parse_memory_stats(raw_output) {
                let s_total = format_unit_spoken(&total, lang);
                let s_used = format_unit_spoken(&used, lang);
                let s_free = format_unit_spoken(&free, lang);
                let s_avail = format_unit_spoken(&avail, lang);
                if lang == Language::Portuguese {
                    format!("Você tem {s_free} de memória livre, com {s_avail} disponíveis e {s_used} em uso de {s_total}.")
                } else {
                    format!("There is {s_free} of free memory, with {s_avail} available and {s_used} used out of {s_total}.")
                }
            } else {
                if lang == Language::Portuguese {
                    "Não foi possível obter o valor exato da memória.".into()
                } else {
                    "Could not determine the exact memory statistics.".into()
                }
            };
            ComposedAnswer { spoken, details: Some(display.clone()), display }
        }

        "disk" => {
            let spoken = if let Some((size, used, avail, pct)) = parse_disk_stats(raw_output) {
                let s_size = format_unit_spoken(&size, lang);
                let s_used = format_unit_spoken(&used, lang);
                let s_avail = format_unit_spoken(&avail, lang);
                if lang == Language::Portuguese {
                    format!("O disco tem {s_avail} de espaço livre e {s_used} em uso de {s_size}, com {pct} de capacidade ocupada.")
                } else {
                    format!("Disk has {s_avail} available and {s_used} used out of {s_size}, at {pct} capacity.")
                }
            } else {
                if lang == Language::Portuguese {
                    "Não foi possível ler as estatísticas exatas do disco.".into()
                } else {
                    "Could not read the exact disk statistics.".into()
                }
            };
            ComposedAnswer { spoken, details: Some(display.clone()), display }
        }

        "uptime" => {
            let up_text = raw_output.lines().next().unwrap_or("").trim().trim_start_matches("up ").trim();
            let spoken = if lang == Language::Portuguese {
                let pt_up = up_text
                    .replace("weeks", "semanas")
                    .replace("week", "semana")
                    .replace("days", "dias")
                    .replace("day", "dia")
                    .replace("hours", "horas")
                    .replace("hour", "hora")
                    .replace("minutes", "minutos")
                    .replace("minute", "minuto");
                format!("O sistema está ativo há {pt_up}.")
            } else {
                format!("The system has been up for {up_text}.")
            };
            ComposedAnswer { spoken, display, details: None }
        }

        "kernel" => {
            let ver = raw_output.trim();
            let spoken = if lang == Language::Portuguese {
                format!("A versão do kernel Linux é {ver}.")
            } else {
                format!("The Linux kernel version is {ver}.")
            };
            ComposedAnswer { spoken, display, details: None }
        }

        "updates" => {
            let count = raw_output.lines().filter(|l| !l.trim().is_empty()).count();
            let spoken = if count == 0 {
                if lang == Language::Portuguese {
                    "O sistema está totalmente atualizado.".into()
                } else {
                    "The system is completely up to date.".into()
                }
            } else {
                if lang == Language::Portuguese {
                    format!("Existem {count} atualizações pendentes nos repositórios.")
                } else {
                    format!("There are {count} updates pending in the repositories.")
                }
            };
            ComposedAnswer { spoken, details: Some(display.clone()), display }
        }

        _ => {
            let first_line = raw_output.lines().next().unwrap_or("").trim();
            let spoken = if first_line.len() > 120 {
                if lang == Language::Portuguese {
                    "Informações obtidas do sistema. Os detalhes estão na tela.".into()
                } else {
                    "System information retrieved. Full details are on the screen.".into()
                }
            } else {
                first_line.to_string()
            };
            ComposedAnswer { spoken, display, details: Some(raw_output.to_string()) }
        }
    }
}

/// Builds natural, specific "Cannot Answer" sentences from what was understood without the model.
pub fn compose_cannot_answer(
    reason: CannotAnswerReason,
    understood_part: Option<&str>,
    missing_part: Option<&str>,
    closest_matches: &[&str],
    lang: Language,
) -> ComposedAnswer {
    let mut spoken = match reason {
        CannotAnswerReason::NotHeardClearly => {
            if lang == Language::Portuguese {
                "Não ouvi com clareza. Você pode repetir?".to_string()
            } else {
                "I did not hear clearly. Could you please repeat?".to_string()
            }
        }

        CannotAnswerReason::NotUnderstood => {
            if lang == Language::Portuguese {
                "Ouvi suas palavras, mas não entendi o comando.".to_string()
            } else {
                "I heard the words, but did not understand the request.".to_string()
            }
        }

        CannotAnswerReason::NoRuleExists => {
            if let Some(und) = understood_part {
                if lang == Language::Portuguese {
                    format!("Entendi que você perguntou sobre {und}, mas não há uma regra configurada para isso.")
                } else {
                    format!("I understood you asked about {und}, but there is no rule configured for that.")
                }
            } else {
                if lang == Language::Portuguese {
                    "Não existe uma regra do sistema para essa ação.".to_string()
                } else {
                    "No system rule exists for that action.".to_string()
                }
            }
        }

        CannotAnswerReason::NotAllowed => {
            if lang == Language::Portuguese {
                "Essa ação não é permitida sem confirmação explícita de segurança.".to_string()
            } else {
                "That action is not allowed without explicit security confirmation.".to_string()
            }
        }

        CannotAnswerReason::MissingInformation => {
            if let (Some(und), Some(miss)) = (understood_part, missing_part) {
                if lang == Language::Portuguese {
                    format!("Entendi que você quer {und}, mas não {miss}.")
                } else {
                    format!("I understood you want {und}, but not {miss}.")
                }
            } else {
                if lang == Language::Portuguese {
                    "Faltam informações para concluir o pedido.".to_string()
                } else {
                    "Information is missing to complete the request.".to_string()
                }
            }
        }
    };

    if !closest_matches.is_empty() {
        let suggestions = closest_matches.iter().take(2).copied().collect::<Vec<_>>().join(" ou ");
        if lang == Language::Portuguese {
            spoken.push_str(&format!(" Você pode tentar: {suggestions}."));
        } else {
            let suggestions_en = closest_matches.iter().take(2).copied().collect::<Vec<_>>().join(" or ");
            spoken.push_str(&format!(" You can try: {suggestions_en}."));
        }
    }

    let display = spoken.clone();
    ComposedAnswer { spoken, display, details: None }
}

fn extract_percentage(text: &str) -> Option<u32> {
    // Look specifically for "NN%" or token followed by "%"
    for part in text.split_whitespace() {
        if part.contains('%') {
            let num = part.trim_matches(|c: char| !c.is_ascii_digit());
            if let Ok(n) = num.parse::<u32>() {
                if n <= 100 {
                    return Some(n);
                }
            }
        }
    }
    let words: Vec<&str> = text.split_whitespace().collect();
    for (i, &w) in words.iter().enumerate() {
        if w.starts_with('%') && i > 0 {
            let num = words[i - 1].trim_matches(|c: char| !c.is_ascii_digit());
            if let Ok(n) = num.parse::<u32>() {
                if n <= 100 {
                    return Some(n);
                }
            }
        }
    }
    None
}

fn extract_first_number(text: &str) -> Option<u32> {
    for word in text.split_whitespace() {
        let clean = word.trim_matches(|c: char| !c.is_ascii_digit());
        if !clean.is_empty() && clean.chars().all(|c| c.is_ascii_digit()) {
            if let Ok(n) = clean.parse::<u32>() {
                return Some(n);
            }
        }
    }
    None
}

fn extract_regex(text: &str, _pattern: &str) -> Option<String> {
    // Simple fast scan for memory values like "7.2 GiB" or "16 GiB"
    let tokens: Vec<&str> = text.split_whitespace().collect();
    for (i, t) in tokens.iter().enumerate() {
        if (t.eq_ignore_ascii_case("gib") || t.eq_ignore_ascii_case("gb")) && i > 0 {
            let num = tokens[i - 1].trim_matches(|c: char| !c.is_ascii_digit() && c != '.');
            if !num.is_empty() {
                return Some(num.to_string());
            }
        }
    }
    None
}

fn parse_memory_stats(raw: &str) -> Option<(String, String, String, String)> {
    for line in raw.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("Mem:") {
            let parts: Vec<&str> = trimmed.split_whitespace().collect();
            // parts: ["Mem:", total, used, free, shared, buff_cache, available]
            if parts.len() >= 7 {
                return Some((parts[1].to_string(), parts[2].to_string(), parts[3].to_string(), parts[6].to_string()));
            } else if parts.len() >= 4 {
                return Some((parts[1].to_string(), parts[2].to_string(), parts[3].to_string(), parts[3].to_string()));
            }
        }
    }
    None
}

fn parse_disk_stats(raw: &str) -> Option<(String, String, String, String)> {
    for line in raw.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('/') {
            let parts: Vec<&str> = trimmed.split_whitespace().collect();
            // parts: [Filesystem, Size, Used, Avail, Use%, Mounted on]
            if parts.len() >= 5 {
                return Some((parts[1].to_string(), parts[2].to_string(), parts[3].to_string(), parts[4].to_string()));
            }
        }
    }
    None
}

fn format_unit_spoken(s: &str, _lang: Language) -> String {
    let clean = s.trim();
    if clean.ends_with("Gi") || clean.ends_with("G") || clean.ends_with("GB") || clean.ends_with("GiB") {
        let num = clean.trim_end_matches(|c: char| c.is_alphabetic());
        format!("{num} gigabytes")
    } else if clean.ends_with("Mi") || clean.ends_with("M") || clean.ends_with("MB") || clean.ends_with("MiB") {
        let num = clean.trim_end_matches(|c: char| c.is_alphanumeric() && !c.is_ascii_digit() && c != '.');
        format!("{num} megabytes")
    } else if clean.ends_with("Ti") || clean.ends_with("T") || clean.ends_with("TB") || clean.ends_with("TiB") {
        let num = clean.trim_end_matches(|c: char| c.is_alphanumeric() && !c.is_ascii_digit() && c != '.');
        format!("{num} terabytes")
    } else {
        clean.to_string()
    }
}

fn summarize_uptime(raw: &str, lang: Language) -> String {
    let lower = raw.to_lowercase();
    if lower.contains("hour") || lower.contains("hora") {
        if lang == Language::Portuguese { "algumas horas".into() } else { "several hours".into() }
    } else if lower.contains("day") || lower.contains("dia") {
        if lang == Language::Portuguese { "mais de um dia".into() } else { "more than a day".into() }
    } else {
        if lang == Language::Portuguese { "poucos minutos".into() } else { "a few minutes".into() }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_compose_battery() {
        let ans_en = compose_fact_answer("battery", "85%, discharging", Language::English);
        assert_eq!(ans_en.spoken, "Battery is at 85 percent discharging.");

        let ans_pt = compose_fact_answer("battery", "85%, carregando", Language::Portuguese);
        assert_eq!(ans_pt.spoken, "A bateria está em 85 por cento e carregando.");
    }

    #[test]
    fn test_compose_memory_and_disk() {
        let mem_raw = "               total        used        free      shared  buff/cache   available\nMem:            30Gi       9.4Gi       2.5Gi       149Mi        20Gi        21Gi\nSwap:           31Gi       1.1Gi        30Gi";
        let ans_mem_en = compose_fact_answer("memory", mem_raw, Language::English);
        assert!(ans_mem_en.spoken.contains("2.5 gigabytes of free memory"));
        assert!(ans_mem_en.spoken.contains("9.4 gigabytes used"));

        let ans_mem_pt = compose_fact_answer("memory", mem_raw, Language::Portuguese);
        assert!(ans_mem_pt.spoken.contains("2.5 gigabytes de memória livre"));
        assert!(ans_mem_pt.spoken.contains("9.4 gigabytes em uso"));

        let disk_raw = "Filesystem      Size  Used Avail Use% Mounted on\n/dev/nvme0n1p2  1.9T  790G  1.1T  43% /";
        let ans_disk_en = compose_fact_answer("disk", disk_raw, Language::English);
        assert!(ans_disk_en.spoken.contains("1.1 terabytes available"));
        assert!(ans_disk_en.spoken.contains("790 gigabytes used"));
    }

    #[test]
    fn test_compose_cannot_answer() {
        let ans = compose_cannot_answer(
            CannotAnswerReason::MissingInformation,
            Some("ajustar o brilho"),
            Some("para qual valor"),
            &["verificar o brilho", "bateria"],
            Language::Portuguese,
        );
        assert!(ans.spoken.contains("Entendi que você quer ajustar o brilho"));
        assert!(ans.spoken.contains("para qual valor"));
        assert!(ans.spoken.contains("verificar o brilho"));
    }
}
