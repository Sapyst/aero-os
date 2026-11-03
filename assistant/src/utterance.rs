//! Utterance cleanup and splitting: turning raw speech transcripts into clean, structured requests.

use std::collections::HashMap;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CleanResult {
    pub original: String,
    pub cleaned: String,
    pub requests: Vec<String>,
    pub self_corrected: Option<(String, String)>,
    pub is_uncertain: bool,
    pub clarification: Option<String>,
}

const FILLERS_PT: &[&str] = &[
    "tipo", "ne", "né", "uh", "hmm", "hum", "eh", "er", "ah", "entao", "então", "assim",
    "sabe", "olha", "veja", "bom", "ou seja",
];

const FILLERS_EN: &[&str] = &[
    "uh", "um", "hmm", "er", "ah", "like", "you know", "well", "actually", "basically",
    "i mean", "sort of", "kind of",
];

/// Number words mapping for Portuguese.
fn pt_number_words() -> HashMap<&'static str, u32> {
    let mut m = HashMap::new();
    m.insert("zero", 0);
    m.insert("um", 1);
    m.insert("uma", 1);
    m.insert("dois", 2);
    m.insert("duas", 2);
    m.insert("tres", 3);
    m.insert("três", 3);
    m.insert("quatro", 4);
    m.insert("cinco", 5);
    m.insert("seis", 6);
    m.insert("sete", 7);
    m.insert("oito", 8);
    m.insert("nove", 9);
    m.insert("dez", 10);
    m.insert("onze", 11);
    m.insert("doze", 12);
    m.insert("treze", 13);
    m.insert("quatorze", 14);
    m.insert("catorze", 14);
    m.insert("quinze", 15);
    m.insert("dezesseis", 16);
    m.insert("dezessete", 17);
    m.insert("dezoito", 18);
    m.insert("dezenove", 19);
    m.insert("vinte", 20);
    m.insert("trinta", 30);
    m.insert("quarenta", 40);
    m.insert("cinquenta", 50);
    m.insert("sessenta", 60);
    m.insert("setenta", 70);
    m.insert("oitenta", 80);
    m.insert("noventa", 90);
    m.insert("cem", 100);
    m.insert("cento", 100);
    m
}

/// Number words mapping for English.
fn en_number_words() -> HashMap<&'static str, u32> {
    let mut m = HashMap::new();
    m.insert("zero", 0);
    m.insert("one", 1);
    m.insert("two", 2);
    m.insert("three", 3);
    m.insert("four", 4);
    m.insert("five", 5);
    m.insert("six", 6);
    m.insert("seven", 7);
    m.insert("eight", 8);
    m.insert("nine", 9);
    m.insert("ten", 10);
    m.insert("eleven", 11);
    m.insert("twelve", 12);
    m.insert("thirteen", 13);
    m.insert("fourteen", 14);
    m.insert("fifteen", 15);
    m.insert("sixteen", 16);
    m.insert("seventeen", 17);
    m.insert("eighteen", 18);
    m.insert("nineteen", 19);
    m.insert("twenty", 20);
    m.insert("thirty", 30);
    m.insert("forty", 40);
    m.insert("fifty", 50);
    m.insert("sixty", 60);
    m.insert("seventy", 70);
    m.insert("eighty", 80);
    m.insert("ninety", 90);
    m.insert("hundred", 100);
    m
}

/// Normalize text to lowercase without accents for comparison, but preserving basic chars.
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

/// Removes filler words and stuttered repeated words.
pub fn remove_fillers(text: &str) -> String {
    let mut tokens: Vec<String> = text.split_whitespace().map(String::from).collect();
    let mut i = 0;
    while i < tokens.len() {
        let clean = strip_accents(&tokens[i])
            .trim_matches(|c: char| !c.is_alphanumeric())
            .to_string();

        // Check single-word fillers
        if FILLERS_PT.contains(&clean.as_str()) || FILLERS_EN.contains(&clean.as_str()) {
            tokens.remove(i);
            continue;
        }

        // Check two-word fillers ("you know", "i mean", "ou seja")
        if i + 1 < tokens.len() {
            let next_clean = strip_accents(&tokens[i + 1])
                .trim_matches(|c: char| !c.is_alphanumeric())
                .to_string();
            let pair = format!("{clean} {next_clean}");
            if pair == "you know" || pair == "i mean" || pair == "ou seja" || pair == "sort of" || pair == "kind of" {
                tokens.remove(i);
                tokens.remove(i);
                continue;
            }
        }

        // Remove immediate word stutters: "o o" -> "o", "the the" -> "the"
        if i + 1 < tokens.len() {
            let next_clean = strip_accents(&tokens[i + 1])
                .trim_matches(|c: char| !c.is_alphanumeric())
                .to_string();
            if !clean.is_empty() && clean == next_clean {
                tokens.remove(i);
                continue;
            }
        }

        i += 1;
    }
    tokens.join(" ")
}

/// Normalizes spoken numbers and percentage phrases in Portuguese and English.
pub fn normalize_numbers(text: &str) -> String {
    let mut s = text.to_string();

    // Normalize percent phrases first: "quarenta por cento" -> "40%"
    // Case-insensitive replacements
    let pt_nums = pt_number_words();
    let en_nums = en_number_words();

    // Check words sequence
    let words: Vec<&str> = s.split_whitespace().collect();
    let mut out: Vec<String> = Vec::new();
    let mut i = 0;

    while i < words.len() {
        let w_raw = words[i];
        let w = strip_accents(w_raw).trim_matches(|c: char| !c.is_alphanumeric()).to_string();

        // Try Portuguese compound numbers: "quarenta e cinco" -> "45"
        if (w == "vinte" || w == "trinta" || w == "quarenta" || w == "cinquenta"
            || w == "sessenta" || w == "setenta" || w == "oitenta" || w == "noventa")
            && i + 2 < words.len()
            && strip_accents(words[i + 1]) == "e"
        {
            let tens = pt_nums.get(w.as_str()).copied().unwrap_or(0);
            let unit_w = strip_accents(words[i + 2]).trim_matches(|c: char| !c.is_alphanumeric()).to_string();
            if let Some(&unit) = pt_nums.get(unit_w.as_str()) {
                if unit < 10 {
                    let total = tens + unit;
                    // Check if followed by "por cento"
                    if i + 4 < words.len()
                        && strip_accents(words[i + 3]) == "por"
                        && strip_accents(words[i + 4]).trim_matches(|c: char| !c.is_alphanumeric()) == "cento"
                    {
                        out.push(total.to_string());
                        i += 5;
                        continue;
                    }
                    out.push(total.to_string());
                    i += 3;
                    continue;
                }
            }
        }

        // Try English compound numbers: "forty five" -> "45"
        if (w == "twenty" || w == "thirty" || w == "forty" || w == "fifty"
            || w == "sixty" || w == "seventy" || w == "eighty" || w == "ninety")
            && i + 1 < words.len()
        {
            let tens = en_nums.get(w.as_str()).copied().unwrap_or(0);
            let unit_w = strip_accents(words[i + 1]).trim_matches(|c: char| !c.is_alphanumeric()).to_string();
            if let Some(&unit) = en_nums.get(unit_w.as_str()) {
                if unit < 10 {
                    let total = tens + unit;
                    if i + 2 < words.len() && strip_accents(words[i + 2]).trim_matches(|c: char| !c.is_alphanumeric()) == "percent" {
                        out.push(total.to_string());
                        i += 3;
                        continue;
                    }
                    out.push(total.to_string());
                    i += 2;
                    continue;
                }
            }
        }

        // Single number word: check if followed by "por cento" or "percent"
        if let Some(&val) = pt_nums.get(w.as_str()).or_else(|| en_nums.get(w.as_str())) {
            // Check "por cento"
            if i + 2 < words.len()
                && strip_accents(words[i + 1]) == "por"
                && strip_accents(words[i + 2]).trim_matches(|c: char| !c.is_alphanumeric()) == "cento"
            {
                out.push(val.to_string());
                i += 3;
                continue;
            }
            // Check "percent"
            if i + 1 < words.len() && strip_accents(words[i + 1]).trim_matches(|c: char| !c.is_alphanumeric()) == "percent" {
                out.push(val.to_string());
                i += 2;
                continue;
            }
            // Standalone digit normalization if in context of numbers
            out.push(val.to_string());
            i += 1;
            continue;
        }

        // Check if already a number followed by percent/por cento: "40 por cento" -> "40", "40 percent" -> "40"
        if w.chars().all(|c| c.is_ascii_digit()) {
            if i + 2 < words.len()
                && strip_accents(words[i + 1]) == "por"
                && strip_accents(words[i + 2]).trim_matches(|c: char| !c.is_alphanumeric()) == "cento"
            {
                out.push(w);
                i += 3;
                continue;
            }
            if i + 1 < words.len() && strip_accents(words[i + 1]).trim_matches(|c: char| !c.is_alphanumeric()) == "percent" {
                out.push(w);
                i += 2;
                continue;
            }
            out.push(w);
            i += 1;
            continue;
        }

        out.push(w_raw.to_string());
        i += 1;
    }

    s = out.join(" ");
    s = s.replace(" %", "%");
    s
}

/// Handles self-corrections such as:
/// "set it to 40, no, 50" -> 50
/// "muda pra 40, não, 50" -> 50
/// "bateria, digo, memoria" -> "memoria"
/// "brightness to 20, wait, 30" -> "brightness to 30"
pub fn handle_self_corrections(text: &str) -> (String, Option<(String, String)>) {
    let lower = text.to_lowercase();
    let markers = [
        ", no, ", " no, ", ", nao, ", " nao, ", ", não, ", " não, ",
        ", digo, ", " digo, ", ", espera, ", " espera, ", ", wait, ", " wait, ",
        ", ou melhor, ", " ou melhor, ",
    ];

    for marker in &markers {
        if let Some(pos) = lower.find(marker) {
            let before = text[..pos].trim();
            let after = text[pos + marker.len()..].trim();

            let before_words: Vec<&str> = before.split_whitespace().collect();
            let after_words: Vec<&str> = after.split_whitespace().collect();

            if let (Some(&old_val), Some(&new_val)) = (before_words.last(), after_words.first()) {
                // Construct the corrected sentence replacing the old value phrase with the new
                let mut prefix_words = before_words.clone();
                prefix_words.pop();
                let corrected = if prefix_words.is_empty() {
                    after.to_string()
                } else {
                    format!("{} {}", prefix_words.join(" "), after)
                };
                return (corrected, Some((old_val.to_string(), new_val.to_string())));
            }
        }
    }

    (text.to_string(), None)
}

/// Helper to clean trailing and leading punctuation and whitespace from a request piece.
pub fn clean_request_part(p: &str) -> String {
    p.trim()
        .trim_matches(|c: char| c.is_whitespace() || c == ',' || c == '.' || c == ';' || c == '!' || c == '?' || c == ':')
        .trim()
        .to_string()
}

/// Splits compound speech into ordered requests:
/// "Lower the brightness, and then show the battery." -> ["Lower the brightness", "show the battery"]
/// "Abaixar o brilho, e depois mostrar a bateria." -> ["Abaixar o brilho", "mostrar a bateria"]
pub fn split_compound_speech(text: &str) -> Vec<String> {
    let lower = text.to_lowercase();

    // Priority connectors (ordered from longest/most specific to shorter)
    let explicit_splitters = [
        ", and then ", ". and then ", "; and then ", " and then ",
        ", and also ", ". and also ", "; and also ", " and also ",
        ", and next ", ". and next ", "; and next ", " and next ",
        ", e depois ", ". e depois ", "; e depois ", " e depois ",
        ", e também ", ". e também ", "; e também ", " e também ",
        ", e tambem ", ". e tambem ", "; e tambem ", " e tambem ",
        ", e em seguida ", ". e em seguida ", "; e em seguida ", " e em seguida ",
        ", e então ", ". e então ", "; e então ", " e então ",
        ", e entao ", ". e entao ", "; e entao ", " e entao ",
        ", then ", ". then ", "; then ", " then ",
        ", also ", ". also ", "; also ",
        ", depois ", ". depois ", "; depois ", " depois ",
        ", também ", ". também ", "; também ",
        ", tambem ", ". tambem ", "; tambem ",
    ];

    for splitter in &explicit_splitters {
        if lower.contains(splitter) {
            let mut result = Vec::new();
            for part in text.split(splitter) {
                let cleaned = clean_request_part(part);
                if !cleaned.is_empty() {
                    result.push(cleaned);
                }
            }
            if result.len() > 1 {
                return result;
            }
        }
    }

    // Secondary check for " also ", " também ", " tambem " between words
    for mid_splitter in &[" also ", " também ", " tambem "] {
        if let Some(pos) = lower.find(mid_splitter) {
            let before = clean_request_part(&text[..pos]);
            let after = clean_request_part(&text[pos + mid_splitter.len()..]);
            if !before.is_empty() && !after.is_empty() {
                return vec![before, after];
            }
        }
    }

    // Tertiary check for " and " / " e " or period/semicolon before distinct action verbs
    let action_verbs = [
        "show", "display", "set", "lower", "raise", "increase", "decrease", "check", "run", "explain", "tell", "list",
        "mostra", "mostrar", "mostre", "muda", "mudar", "alterar", "baixa", "abaixar", "aumenta",
        "aumentar", "verifica", "verificar", "checar", "explique", "explica", "diga", "liste", "listar",
    ];

    for conj in &[", and ", ". and ", " and ", ", e ", ". e ", " e ", "; ", ". "] {
        if let Some(pos) = lower.find(conj) {
            let before = clean_request_part(&text[..pos]);
            let after = clean_request_part(&text[pos + conj.len()..]);

            let after_lower = after.to_lowercase();
            let after_first_word = after_lower
                .split(|c: char| !c.is_alphanumeric())
                .filter(|s| !s.is_empty())
                .next()
                .unwrap_or("");

            if action_verbs.contains(&after_first_word) && !before.is_empty() && !after.is_empty() {
                return vec![before, after];
            }
        }
    }

    let single = clean_request_part(text);
    if single.is_empty() {
        vec![]
    } else {
        vec![single]
    }
}

/// Full utterance pipeline turning raw speech into clean requests.
pub fn clean_utterance(raw: &str) -> CleanResult {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return CleanResult {
            original: raw.to_string(),
            cleaned: String::new(),
            requests: Vec::new(),
            self_corrected: None,
            is_uncertain: true,
            clarification: Some("I did not hear anything clearly.".into()),
        };
    }

    // 1. Fillers removal
    let without_fillers = remove_fillers(trimmed);

    // 2. Number normalization
    let normalized = normalize_numbers(&without_fillers);

    // 3. Self corrections
    let (corrected, correction) = handle_self_corrections(&normalized);

    // 4. Split compound requests
    let raw_requests = split_compound_speech(&corrected);
    let requests: Vec<String> = raw_requests
        .into_iter()
        .map(|r| clean_request_part(&r))
        .filter(|r| !r.is_empty())
        .collect();

    let cleaned_single = clean_request_part(&corrected);

    // Check for uncertain or nonsensical input
    let is_uncertain = cleaned_single.len() < 2 || requests.is_empty();
    let clarification = if is_uncertain {
        Some("Could you please repeat that more clearly?".into())
    } else {
        None
    };

    CleanResult {
        original: raw.to_string(),
        cleaned: cleaned_single,
        requests,
        self_corrected: correction,
        is_uncertain,
        clarification,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_filler_removal() {
        assert_eq!(remove_fillers("uh show the battery please"), "show the battery please");
        assert_eq!(remove_fillers("tipo assim né mostra a bateria"), "mostra a bateria");
        assert_eq!(remove_fillers("well actually you know check memory"), "check memory");
        assert_eq!(remove_fillers("show show the the memory"), "show the memory");
    }

    #[test]
    fn test_number_normalization() {
        assert_eq!(normalize_numbers("quarenta por cento"), "40");
        assert_eq!(normalize_numbers("forty percent"), "40");
        assert_eq!(normalize_numbers("oitenta e cinco por cento"), "85");
        assert_eq!(normalize_numbers("eighty five percent"), "85");
        assert_eq!(normalize_numbers("mudar brilho para cinquenta"), "mudar brilho para 50");
        assert_eq!(normalize_numbers("set brightness to fifty"), "set brightness to 50");
    }

    #[test]
    fn test_self_corrections() {
        let (res, corr) = handle_self_corrections("set it to 40, no, 50");
        assert_eq!(res, "set it to 50");
        assert_eq!(corr, Some(("40".into(), "50".into())));

        let (res, corr) = handle_self_corrections("muda para 30, não, 70");
        assert_eq!(res, "muda para 70");
        assert_eq!(corr, Some(("30".into(), "70".into())));
    }

    #[test]
    fn test_compound_splitting_with_punctuation() {
        // Whisper-style punctuation
        let reqs = split_compound_speech("Lower the brightness, and then show the battery.");
        assert_eq!(reqs, vec!["Lower the brightness", "show the battery"]);

        let reqs = split_compound_speech("Abaixar o brilho, e depois mostrar a bateria.");
        assert_eq!(reqs, vec!["Abaixar o brilho", "mostrar a bateria"]);

        let reqs = split_compound_speech("Lower the brightness, then show the battery.");
        assert_eq!(reqs, vec!["Lower the brightness", "show the battery"]);

        let reqs = split_compound_speech("Lower the brightness, also show the battery.");
        assert_eq!(reqs, vec!["Lower the brightness", "show the battery"]);

        let reqs = split_compound_speech("Abaixar o brilho, e também mostrar a bateria.");
        assert_eq!(reqs, vec!["Abaixar o brilho", "mostrar a bateria"]);

        let reqs = split_compound_speech("Abaixar o brilho, também mostrar a bateria.");
        assert_eq!(reqs, vec!["Abaixar o brilho", "mostrar a bateria"]);

        let single = split_compound_speech("Show the battery.");
        assert_eq!(single, vec!["Show the battery"]);
    }

    #[test]
    fn test_full_clean_utterance() {
        let res = clean_utterance("uh tipo set brightness to forty, no, fifty percent, and then show the battery.");
        assert_eq!(res.self_corrected, Some(("40".into(), "50".into())));
        assert_eq!(res.requests, vec!["set brightness to 50", "show the battery"]);
    }
}
