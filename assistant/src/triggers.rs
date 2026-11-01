//! Paired-word triggers: common requests answered by exact rules, without the model.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

pub enum Rule {
    Read(Vec<String>),
    Report(Vec<String>),
    Run { name: String, command: String, description: String },
}

struct Entry {
    rule: Rule,
    verbs: Vec<String>,
    objects: Vec<String>,
}

pub enum Match {
    Rule(Rule),
    Extract { file: PathBuf, folder: Option<PathBuf> },
    Say(String),
    Install(Vec<String>),
    Remove(Vec<String>),
    Move { source: PathBuf, target: PathBuf },
    None(String),
}

const STOP: &[&str] = &[
    "a", "an", "the", "my", "me", "i", "is", "are", "of", "in", "on", "at", "by", "please", "now", "right", "for", "this", "there", "do", "have", "to", "much", "many",
    "o", "os", "as", "e", "um", "uma", "meu", "minha", "meus", "minhas", "de", "do", "da", "dos", "das", "no", "na", "em", "por", "favor", "agora", "pra", "para", "aqui", "estou", "usando", "using", "used", "use", "in", "how", "current", "atual", "esta", "ta", "tem", "que",
];
const NEEDS_THINKING: &[&str] = &[
    "why", "should", "could", "would", "can", "if", "when", "explain", "dont", "not", "never", "without", "except", "after", "before", "porque", "pq", "deveria", "posso", "se", "quando", "explique", "explica", "nao", "nunca", "sem", "depois", "antes", "exceto",
];

pub fn plain(text: &str) -> String {
    text.to_lowercase()
        .chars()
        .map(|c| match c {
            'á' | 'à' | 'ã' | 'â' => 'a',
            'é' | 'ê' => 'e',
            'í' => 'i',
            'ó' | 'õ' | 'ô' => 'o',
            'ú' => 'u',
            'ç' => 'c',
            c if c.is_alphanumeric() || c == '-' || c == '.' || c == '_' || c == '/' || c == '~' => c,
            '\'' => '\0',
            _ => ' ',
        })
        .filter(|c| *c != '\0')
        .collect()
}

fn list(field: &str, groups: &HashMap<String, Vec<String>>) -> Vec<String> {
    let mut out = vec![];
    for word in field.split(',').map(str::trim) {
        match groups.get(word) {
            Some(members) => out.extend(members.iter().cloned()),
            None => out.push(plain(word).trim().to_string()),
        }
    }
    out.retain(|w| !w.is_empty());
    out
}

/// Edit distance with swaps, stopping early: only 0 or 1 matter here.
fn near(a: &str, b: &str) -> bool {
    let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
    if a.len().abs_diff(b.len()) > 1 {
        return false;
    }
    let mut prev2 = vec![0; b.len() + 1];
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    for i in 1..=a.len() {
        let mut row = vec![i; b.len() + 1];
        for j in 1..=b.len() {
            let cost = usize::from(a[i - 1] != b[j - 1]);
            row[j] = (prev[j] + 1).min(row[j - 1] + 1).min(prev[j - 1] + cost);
            if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                row[j] = row[j].min(prev2[j - 2] + 1);
            }
        }
        prev2 = std::mem::replace(&mut prev, row);
    }
    prev[b.len()] <= 1
}

/// Corrects a typo only when exactly one known word is that close and shares its first letter.
fn fix_typos(tokens: &mut [String], entries: &[Entry]) {
    let mut known: HashSet<&str> = STOP.iter().chain(POLITE).chain(QUESTION).chain(NEEDS_THINKING).copied().collect();
    for e in entries {
        for phrase in e.verbs.iter().chain(&e.objects) {
            known.extend(phrase.split_whitespace());
        }
    }
    for token in tokens.iter_mut() {
        if token.len() < 5 || known.contains(token.as_str()) || !token.chars().all(char::is_alphabetic) {
            continue;
        }
        let close: Vec<&&str> = known.iter().filter(|k| k.len() >= 6 && k.chars().next() == token.chars().next() && near(token, k)).collect();
        if let [only] = close.as_slice() {
            *token = only.to_string();
        }
    }
}

fn parse(config: &str) -> Vec<Entry> {
    let mut entries = vec![];
    let mut groups: HashMap<String, Vec<String>> = HashMap::new();
    for line in config.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#')) {
        let f: Vec<&str> = line.split('|').collect();
        if f[0] == "group" && f.len() == 3 {
            let members = list(f[2], &groups);
            groups.insert(f[1].to_string(), members);
            continue;
        }
        if f.len() < 4 {
            continue;
        }
        let topics = || f[1].split(',').map(|t| t.trim().to_string()).collect::<Vec<_>>();
        let rule = match (f[0], f.len()) {
            ("read", 4) => Rule::Read(topics()),
            ("report", 4) => Rule::Report(topics()),
            ("run", 6) => Rule::Run { name: f[1].into(), command: f[4].into(), description: f[5].into() },
            _ => continue,
        };
        entries.push(Entry { rule, verbs: list(f[2], &groups), objects: list(f[3], &groups) });
    }
    entries
}

fn phrase_at(tokens: &[String], phrase: &str) -> Option<usize> {
    let words: Vec<&str> = phrase.split_whitespace().collect();
    tokens.windows(words.len()).position(|w| w.iter().zip(&words).all(|(a, b)| a == b))
}

const QUESTION: &[&str] = &["how", "what", "which", "is", "are", "does", "quanto", "quanta", "quantos", "qual", "quais", "esta", "tem"];
const POLITE: &[&str] = &["a", "o", "the", "my", "meu", "minha", "make", "deixe", "deixa", "torne", "faca", "please", "pode", "poderia", "por", "favor", "agora", "now", "just", "so", "you", "voce"];

/// A rule that acts fires only on a command: its verb comes first and nothing asks a question.
fn is_command(entry: &Entry, tokens: &[String], has_num_arg: bool) -> bool {
    if !matches!(entry.rule, Rule::Run { .. }) {
        return true;
    }
    if tokens.iter().any(|t| QUESTION.contains(&t.as_str())) {
        return false;
    }
    let Some(first_idx) = tokens.iter().position(|t| !POLITE.contains(&t.as_str())) else {
        return false;
    };
    let rest = &tokens[first_idx..];
    let _ = has_num_arg;
    entry.verbs.iter().any(|v| phrase_at(rest, v) == Some(0)) || entry.objects.iter().any(|o| phrase_at(rest, o) == Some(0))
}

fn matched(entry: &mut Entry, tokens: &[String]) -> Option<Vec<usize>> {
    let has_num_arg = matches!(&entry.rule, Rule::Run { command, .. } if command.contains("{n}"));
    if !is_command(entry, tokens, has_num_arg) {
        return None;
    }
    let mut used = vec![];
    let object = entry.objects.iter().filter(|o| phrase_at(tokens, o).is_some()).max_by_key(|o| o.len())?;
    let oi = phrase_at(tokens, object)?;
    let on = object.split_whitespace().count();
    used.extend(oi..oi + on);
    let verb = entry
        .verbs
        .iter()
        .filter(|v| phrase_at(tokens, v).is_some_and(|i| i + v.split_whitespace().count() <= oi || i >= oi + on))
        .max_by_key(|v| v.len());
    if let Some(verb) = verb {
        let vi = tokens.iter().enumerate().position(|(i, _)| phrase_at(&tokens[i..], verb) == Some(0) && (i + verb.split_whitespace().count() <= oi || i >= oi + on))?;
        used.extend(vi..vi + verb.split_whitespace().count());
    } else if !has_num_arg {
        return None;
    } else {
        let first_idx = tokens.iter().position(|t| !POLITE.contains(&t.as_str()))?;
        if oi != first_idx {
            return None;
        }
    }
    if has_num_arg {
        let nums: Vec<(usize, &String)> = tokens
            .iter()
            .enumerate()
            .filter(|(i, t)| !used.contains(i) && t.chars().any(|c| c.is_ascii_digit()))
            .collect();
        let [(ni, tok)] = nums.as_slice() else {
            return None;
        };
        if !tok.chars().all(|c| c.is_ascii_digit()) {
            return None;
        }
        let n: u32 = tok.parse().ok()?;
        if n > 100 {
            return None;
        }
        used.push(*ni);
        if tokens.iter().enumerate().any(|(i, t)| !used.contains(&i) && !STOP.contains(&t.as_str()) && !POLITE.contains(&t.as_str())) {
            return None;
        }
        if let Rule::Run { command, description, .. } = &mut entry.rule {
            let ns = n.to_string();
            *command = command.replace("{n}", &ns);
            *description = description.replace("{n}", &ns);
        }
    } else if tokens.iter().enumerate().any(|(i, t)| !used.contains(&i) && t.chars().any(|c| c.is_ascii_digit())) {
        return None;
    }
    Some(used)
}

fn quoted_or_words(text: &str) -> Vec<String> {
    let mut out = vec![];
    let mut cur = String::new();
    let mut quote = None;
    for c in text.chars() {
        match quote {
            Some(q) if c == q => {
                out.push(std::mem::take(&mut cur));
                quote = None;
            }
            Some(_) => cur.push(c),
            None if c == '"' || c == '\'' => quote = Some(c),
            None if c.is_whitespace() => {
                if !cur.is_empty() {
                    out.push(std::mem::take(&mut cur));
                }
            }
            None => cur.push(c),
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

fn home() -> String {
    std::env::var("HOME").unwrap_or_default()
}

fn resolve(word: &str) -> PathBuf {
    match word.strip_prefix("~/") {
        Some(rest) => Path::new(&home()).join(rest),
        None => PathBuf::from(word),
    }
}

fn find_file(word: &str) -> Option<PathBuf> {
    let direct = resolve(word);
    if direct.exists() {
        return Some(direct);
    }
    if word.contains('/') {
        return None;
    }
    ["Downloads", "Documents", "Desktop"].iter().map(|d| Path::new(&home()).join(d).join(word)).find(|p| p.exists())
}

const ARCHIVES: &[&str] = &[
    ".tar.gz", ".tar.xz", ".tar.zst", ".tar.bz2", ".tar.bz", ".tar.lz", ".tar.lz4", ".tar.lzma", ".tar.lzo", ".tar.z", ".tar.br",
    ".tgz", ".taz", ".tarz", ".txz", ".tzst", ".tbz2", ".tbz", ".tlz", ".tar",
    ".zip", ".zipx", ".7z", ".rar", ".cab", ".iso", ".arj", ".lha", ".lzh", ".ace", ".wim", ".dmg", ".xar", ".cpio", ".ar", ".a", ".deb", ".rpm", ".msi", ".apk", ".jar", ".war", ".ear", ".pk3", ".pk4", ".crx", ".xpi", ".whl", ".nupkg", ".vsix", ".epub", ".pkg", ".zipx",
    ".gz", ".bz2", ".bz", ".xz", ".lzma", ".zst", ".lz4", ".lz", ".z", ".br", ".lzo", ".sz", ".zpaq",
];

/// The folder name for an archive: its name without the archive extension.
pub fn archive_stem(path: &Path) -> Option<String> {
    let name = path.file_name()?.to_string_lossy().to_string();
    let lower = name.to_lowercase();
    ARCHIVES.iter().filter(|e| lower.ends_with(**e) && lower.len() > e.len()).max_by_key(|e| e.len()).map(|e| name[..name.len() - e.len()].to_string())
}

const INTO: &[&str] = &["em", "no", "na", "para", "pra", "to", "in", "into", "dentro"];

fn folder_named(word: &str) -> Option<PathBuf> {
    let home = PathBuf::from(home());
    let known = match plain(word).trim() {
        "downloads" | "download" | "baixados" => Some("Downloads"),
        "documents" | "documentos" => Some("Documents"),
        "desktop" | "area" => Some("Desktop"),
        "pictures" | "imagens" | "fotos" => Some("Pictures"),
        "videos" => Some("Videos"),
        "music" | "musicas" => Some("Music"),
        "home" => Some(""),
        _ => None,
    };
    if let Some(k) = known {
        return Some(home.join(k));
    }
    let direct = resolve(word);
    direct.is_dir().then_some(direct)
}

/// Splits "NAME in FOLDER" into the file word and the folder, when a folder is named.
fn extract_action(words: &[String]) -> Match {
    let filler = ["o", "a", "the", "arquivo", "file", "this", "esse", "este", "pasta", "folder", "de", "trabalho", "by", "por"];
    let at = words.iter().skip(1).position(|w| INTO.contains(&w.to_lowercase().as_str())).map(|i| i + 1);
    let (names, after) = match at {
        Some(i) => (&words[1..i], &words[i + 1..]),
        None => (&words[1..], &words[0..0]),
    };
    let names: Vec<&String> = names.iter().filter(|w| !filler.contains(&w.to_lowercase().as_str())).collect();
    let after: Vec<&String> = after.iter().filter(|w| !filler.contains(&w.to_lowercase().as_str())).collect();
    let [name] = names.as_slice() else {
        return Match::None(if names.is_empty() { "no archive named" } else { "more than one name" }.into());
    };
    let folder = match after.as_slice() {
        [] => None,
        [one] => match folder_named(one) {
            Some(f) if f.is_dir() => Some(f),
            _ => return Match::Say(format!("The folder {one} does not exist, so nothing was extracted.")),
        },
        _ => return Match::None("more than one folder named".into()),
    };
    let found = find_file(name).or_else(|| folder.as_ref().map(|f| f.join(name.as_str())).filter(|p| p.exists()));
    match found {
        Some(file) if file.is_file() => Match::Extract { file, folder },
        Some(_) => Match::Say(format!("{name} is a folder, not an archive.")),
        None if name.contains('.') || name.contains('/') => Match::Say(format!("{name} was not found, so nothing was extracted.")),
        None => Match::None("no archive named".into()),
    }
}

fn package_action(tokens: &[String]) -> Option<Match> {
    let first = tokens.first()?.as_str();
    let install = ["install", "instalar", "instale", "instala", "add"].contains(&first);
    let remove = ["remove", "uninstall", "remover", "remova", "desinstalar", "desinstale", "desinstala"].contains(&first);
    if !install && !remove {
        return None;
    }
    if tokens.iter().any(|t| NEEDS_THINKING.contains(&t.as_str())) {
        return Some(Match::None("the request needs thought".into()));
    }
    let filler = ["o", "a", "os", "as", "the", "pacote", "pacotes", "package", "packages", "programa", "app", "e", "and", "me", "for", "pra", "para", "mim", "please", "por", "favor"];
    let names: Vec<String> = tokens[1..].iter().filter(|t| !filler.contains(&t.as_str())).cloned().collect();
    let valid = |n: &String| n.chars().all(|c| c.is_ascii_alphanumeric() || "@._+-".contains(c)) && !n.starts_with('-');
    if names.is_empty() || names.len() > 8 || !names.iter().all(valid) {
        return Some(Match::None("no clear package name".into()));
    }
    Some(if install { Match::Install(names) } else { Match::Remove(names) })
}

/// Files and moves take their arguments from the text, unchanged.
fn file_action(text: &str, tokens: &[String]) -> Option<Match> {
    let extract = ["extract", "unzip", "unrar", "untar", "un7z", "unpack", "decompress", "uncompress", "descompactar", "descompacta", "descompacte", "descompacto", "extrair", "extraia", "extrai", "extraia", "dezipar", "deszipar", "dezipa", "deszipa"];
    let moving = ["move", "mover", "mova", "move-"];
    let first = tokens.first()?.as_str();
    let words = quoted_or_words(text);
    if extract.contains(&first) {
        return Some(extract_action(&words));
    }
    if moving.contains(&first) {
        let sep = words.iter().rposition(|w| ["to", "para", "pra", "into"].contains(&w.to_lowercase().as_str()))?;
        let source_words: Vec<&String> = words[1..sep].iter().filter(|w| !["o", "a", "the", "arquivo", "file", "pasta", "folder"].contains(&w.to_lowercase().as_str())).collect();
        let target_words: Vec<&String> = words[sep + 1..].iter().filter(|w| !["o", "a", "the", "pasta", "folder"].contains(&w.to_lowercase().as_str())).collect();
        if source_words.len() != 1 || target_words.len() != 1 {
            return Some(Match::None("move needs one source and one target; quote names with spaces".into()));
        }
        let Some(source) = find_file(source_words[0]) else {
            return Some(Match::None(format!("{} was not found", source_words[0])));
        };
        return Some(Match::Move { source, target: resolve(target_words[0]) });
    }
    None
}

pub fn classify(config: &str, text: &str) -> Match {
    let normal = plain(text);
    let tokens: Vec<String> = normal.split_whitespace().map(String::from).collect();
    if let Some(m) = file_action(text, &tokens) {
        return m;
    }
    if let Some(m) = package_action(&tokens) {
        return m;
    }
    if tokens.iter().any(|t| NEEDS_THINKING.contains(&t.as_str())) || phrase_at(&tokens, "por que").is_some() || phrase_at(&tokens, "how do").is_some() || phrase_at(&tokens, "how to").is_some() || phrase_at(&tokens, "como") .is_some() {
        return Match::None("the request needs thought".into());
    }
    let mut tokens = tokens;
    let entries = parse(config);
    fix_typos(&mut tokens, &entries);
    let too_big = tokens.iter().any(|t| t.chars().all(|c| c.is_ascii_digit()) && t.parse::<u32>().map_or(true, |n| n > 100));
    if too_big && entries.iter().any(|e| matches!(&e.rule, Rule::Run { command, .. } if command.contains("{n}")) && e.objects.iter().any(|o| phrase_at(&tokens, o).is_some())) {
        return Match::Say("That value must be from 0 to 100, so nothing was changed.".into());
    }
    let mut hits: Vec<(Entry, usize)> = vec![];
    for mut e in entries {
        if let Some(used) = matched(&mut e, &tokens) {
            let extra = tokens.iter().enumerate().filter(|(i, t)| !used.contains(i) && !STOP.contains(&t.as_str())).count();
            hits.push((e, extra));
        }
    }
    hits.sort_by_key(|h| h.1);
    let Some(best) = hits.first().map(|h| h.1) else {
        return Match::None("no trigger matched".into());
    };
    if best > 1 {
        return Match::None("too many other words for a safe match".into());
    }
    let mut top: Vec<(Entry, usize)> = hits.into_iter().filter(|h| h.1 == best).collect();
    let actionable = top.iter().position(|h| matches!(h.0.rule, Rule::Run { .. } | Rule::Report(_)));
    if top.len() > 1 {
        match actionable {
            Some(i) if top.iter().filter(|h| matches!(h.0.rule, Rule::Run { .. } | Rule::Report(_))).count() == 1 => return Match::Rule(top.remove(i).0.rule),
            _ => return Match::None("more than one trigger matched".into()),
        }
    }
    Match::Rule(top.remove(0).0.rule)
}

#[cfg(test)]
mod tests {
    use super::*;
    const CONFIG: &str = include_str!("../triggers.conf");
    fn name(text: &str) -> String {
        match classify(CONFIG, text) {
            Match::Rule(Rule::Read(t)) => format!("read:{}", t.join(",")),
            Match::Rule(Rule::Report(t)) => format!("report:{}", t[0]),
            Match::Rule(Rule::Run { name, .. }) => format!("run:{name}"),
            Match::Extract { .. } => "extract".into(),
            Match::Move { .. } => "move".into(),
            Match::Say(_) => "say".into(),
            Match::Install(n) => format!("install:{}", n.join(",")),
            Match::Remove(n) => format!("remove:{}", n.join(",")),
            Match::None(_) => "model".into(),
        }
    }
    fn cmd(text: &str) -> Option<String> {
        match classify(CONFIG, text) {
            Match::Rule(Rule::Run { command, .. }) => Some(command),
            _ => None,
        }
    }
    #[test]
    fn paired_words_fire() {
        for (t, want) in [
            ("How much RAM is free right now?", "read:memory"),
            ("quanto de ram estou usando", "read:memory"),
            ("show my battery", "read:battery"),
            ("qual a bateria", "read:battery"),
            ("what is the power profile", "read:power"),
            ("qual o modo de energia", "read:power"),
            ("show top processes", "read:processes"),
            ("listar processos", "read:processes"),
            ("what is the kernel version", "read:kernel"),
            ("qual a versao do kernel", "read:kernel"),
            ("are there any errors", "read:errors,services"),
            ("verificar erros", "read:errors,services"),
            ("what is the brightness", "read:brightness"),
            ("read brightness", "read:brightness"),
            ("qual o brilho", "read:brightness"),
            ("ler brilho", "read:brightness"),
            ("mostrar o brilho da tela", "read:brightness"),
            ("raise brightness", "run:raise-brightness"),
            ("increase the brightness", "run:raise-brightness"),
            ("turn up brightness", "run:raise-brightness"),
            ("brightness up", "run:raise-brightness"),
            ("aumentar o brilho", "run:raise-brightness"),
            ("subir brilho", "run:raise-brightness"),
            ("mais brilho", "run:raise-brightness"),
            ("lower brightness", "run:lower-brightness"),
            ("decrease the brightness", "run:lower-brightness"),
            ("turn down brightness", "run:lower-brightness"),
            ("brightness down", "run:lower-brightness"),
            ("diminuir o brilho", "run:lower-brightness"),
            ("abaixar o brilho", "run:lower-brightness"),
            ("baixar brilho", "run:lower-brightness"),
            ("menos brilho", "run:lower-brightness"),
            ("brightness 40", "run:set-brightness"),
            ("brilho 40", "run:set-brightness"),
            ("set brightness to 40", "run:set-brightness"),
            ("set brightness 75%", "run:set-brightness"),
            ("mudar o brilho para 60", "run:set-brightness"),
            ("colocar brilho em 30%", "run:set-brightness"),
            ("raise volume", "run:raise-volume"),
            ("aumentar o volume", "run:raise-volume"),
            ("lower volume", "run:lower-volume"),
            ("abaixar o volume", "run:lower-volume"),
            ("set volume to 50", "run:set-volume"),
            ("mudar volume para 50", "run:set-volume"),
            ("mute audio", "run:mute-audio"),
            ("silenciar o som", "run:mute-audio"),
            ("take a fullscreen screenshot", "run:screenshot-screen"),
            ("tirar print da tela inteira", "run:screenshot-screen"),
            ("take a window screenshot", "run:screenshot-window"),
            ("tirar print da janela", "run:screenshot-window"),
            ("take a screenshot", "run:screenshot-area"),
            ("tirar um print", "run:screenshot-area"),
            ("start screen recording", "run:start-recording"),
            ("iniciar gravacao de tela", "run:start-recording"),
            ("stop screen recording", "run:stop-recording"),
            ("parar gravacao de tela", "run:stop-recording"),
            ("set clipboard wallpaper", "run:wallpaper-clipboard"),
            ("colocar papel de parede da area de transferencia", "run:wallpaper-clipboard"),
            ("reload theme", "run:reload-theme"),
            ("recarregar o tema", "run:reload-theme"),
            ("open clipboard history", "run:open-clipboard"),
            ("abrir historico da area de transferencia", "run:open-clipboard"),
            ("turn on bluetooth", "run:enable-bluetooth"),
            ("ligar o bluetooth", "run:enable-bluetooth"),
            ("turn off bluetooth", "run:disable-bluetooth"),
            ("desligar o bluetooth", "run:disable-bluetooth"),
            ("turn on wifi", "run:enable-wifi"),
            ("ligar o wifi", "run:enable-wifi"),
            ("turn off wifi", "run:disable-wifi"),
            ("desligar o wi-fi", "run:disable-wifi"),
            ("set performance mode", "run:power-performance"),
            ("ativar modo desempenho", "run:power-performance"),
            ("set balanced mode", "run:power-balanced"),
            ("ativar modo equilibrado", "run:power-balanced"),
            ("set power saver", "run:power-saver"),
            ("ativar economia de energia", "run:power-saver"),
            ("limpar ram", "run:clean-ram"),
            ("clear the memory", "run:clean-ram"),
            ("reiniciar a barra de tarefas", "run:restart-taskbar"),
            ("restart taskbar", "run:restart-taskbar"),
            ("reinicia o dock", "run:restart-dock"),
            ("restart bluetooth", "run:restart-bluetooth"),
            ("gere um relatorio de memoria", "report:memory"),
            ("make a system report", "report:memory"),
            ("gere um relatório de erros", "report:errors"),
            ("what is my memory", "read:memory"),
        ] {
            assert_eq!(name(t), want, "{t}");
        }
    }
    #[test]
    fn numeric_arguments_are_bounded_and_safe() {
        assert_eq!(cmd("brightness 40").as_deref(), Some("brilho 40"));
        assert_eq!(cmd("brilho 40").as_deref(), Some("brilho 40"));
        assert_eq!(cmd("brightness 0").as_deref(), Some("brilho 0"));
        assert_eq!(cmd("brightness 100").as_deref(), Some("brilho 100"));
        assert_eq!(cmd("set brightness to 65%").as_deref(), Some("brilho 65"));
        assert_eq!(cmd("colocar o brilho em 25%").as_deref(), Some("brilho 25"));
        assert_eq!(cmd("set volume to 50").as_deref(), Some("wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 50% && som osd volume"));
        for bad in [
            "brightness",
            "brilho",
            "brightness -10",
            "brilho -5",
            "brightness 40 50",
            "brightness 40 apples",
            "is brightness 40",
            "why is brightness 40",
            "do not set brightness to 40",
            "how to set brightness to 40",
            "como mudar o brilho para 40",
            "volume -10",
        ] {
            assert_eq!(name(bad), "model", "{bad}");
        }
    }
    #[test]
    fn single_words_and_other_contexts_go_to_the_model() {
        for t in [
            "ram", "memory", "bar", "restart", "battery", "brightness", "brilho", "volume", "screenshot", "clipboard", "theme", "wifi", "kernel", "processes",
            "why is my ram so full",
            "how do I restart the taskbar",
            "como limpar a ram",
            "do not restart bluetooth",
            "nao limpar a ram",
            "should I clean the ram",
            "write a poem about the memory of the sea and show it",
            "clean my room",
            "restart my favorite song on the sound bar with a new playlist from yesterday",
            "if bluetooth is off restart audio",
            "the bar should restart",
            "is the taskbar restart broken",
            "ram clean status check how",
            "why is the brightness so low",
            "how do I lower brightness",
            "como diminuir o brilho",
            "do not lower the brightness",
            "nao aumentar o brilho",
            "how do I take a screenshot",
            "do not stop screen recording",
            "why did my clipboard wallpaper fail",
            "how to reload the theme",
            "should I open clipboard history",
            "if wifi is slow turn off bluetooth",
            "do not turn off wifi",
            "how do I set performance mode",
            "is balanced mode better than power saver",
            "why is the volume muted",
        ] {
            assert_eq!(name(t), "model", "{t}");
        }
    }
    #[test]
    fn archive_names() {
        for (file, stem) in [("a.tar.gz", "a"), ("a.tarz", "a"), ("a.tgz", "a"), ("a.7z", "a"), ("A.RAR", "A"), ("a.b.zip", "a.b"), ("a.tar.zst", "a"), ("a.gz", "a"), ("a.Z", "a"), ("a.iso", "a"), ("a.deb", "a")] {
            assert_eq!(archive_stem(Path::new(file)).as_deref(), Some(stem), "{file}");
        }
        assert_eq!(archive_stem(Path::new("notes.txt")), None);
    }
    #[test]
    fn missing_files_do_not_reach_the_model() {
        assert_eq!(name("descompacte nada-aqui-xyz.tarz em downloads"), "say");
        assert_eq!(name("extract nada-aqui-xyz.zip in pasta-que-nao-existe-xyz"), "say");
        assert_eq!(name("extract the meaning"), "model");
    }
    #[test]
    fn packages() {
        assert_eq!(name("install htop"), "install:htop");
        assert_eq!(name("instale o pacote htop e vim"), "install:htop,vim");
        assert_eq!(name("remova o htop"), "remove:htop");
        assert_eq!(name("how do I install htop"), "model");
        assert_eq!(name("install htop if it is missing"), "model");
        assert_eq!(name("do not remove htop"), "model");
        assert_eq!(name("atualizar o sistema"), "run:system-update");
        assert_eq!(name("update my system"), "run:system-update");
        assert_eq!(name("how do I update my system"), "model");
        assert_eq!(name("is my system update broken"), "model");
    }
    #[test]
    fn mixed_words_form_the_intent() {
        for (t, want) in [
            ("bright down", "run:lower-brightness"),
            ("brightnes lower", "run:lower-brightness"),
            ("brigthness up", "run:raise-brightness"),
            ("brilo menos", "run:lower-brightness"),
            ("brilho abaixar", "run:lower-brightness"),
            ("dim the screen", "run:lower-brightness"),
            ("deixe a tela mais escura", "run:lower-brightness"),
            ("escurecer tela", "run:lower-brightness"),
            ("too bright", "run:lower-brightness"),
            ("muito escuro", "run:raise-brightness"),
            ("clarear a tela", "run:raise-brightness"),
            ("luminosidade 40", "run:set-brightness"),
            ("bright 40", "run:set-brightness"),
            ("bateriaa", "model"),
            ("show my batery", "read:battery"),
        ] {
            assert_eq!(name(t), want, "{t}");
        }
        assert_eq!(name("brightness 400"), "say");
        assert_eq!(name("brilho 101"), "say");
        for t in ["bright idea", "the sun is too bright", "why is my screen so dark", "bright", "screen", "lower the price of the screen", "dim sum"] {
            assert_eq!(name(t), "model", "{t}");
        }
    }
}
