//! Full-text index over the offline Arch Wiki from arch-wiki-docs.

use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::fs;
use std::path::Path;

const WIKI_DIR: &str = "/usr/share/doc/arch-wiki/html/en";

fn strip_block(html: &str, tag: &str) -> String {
    let (open, close) = (format!("<{tag}"), format!("</{tag}>"));
    let mut out = String::with_capacity(html.len());
    let mut rest = html;
    while let Some(i) = rest.find(&open) {
        out.push_str(&rest[..i]);
        match rest[i..].find(&close) {
            Some(j) => rest = &rest[i + j + close.len()..],
            None => return out,
        }
    }
    out.push_str(rest);
    out
}

fn to_text(html: &str) -> String {
    let mut out = String::with_capacity(html.len() / 2);
    let mut in_tag = false;
    let mut tag = String::new();
    for c in html.chars() {
        match (in_tag, c) {
            (false, '<') => {
                in_tag = true;
                tag.clear();
            }
            (true, '>') => {
                in_tag = false;
                let t = tag.trim_start_matches('/').split_whitespace().next().unwrap_or("").to_lowercase();
                if ["p", "br", "li", "tr", "div", "pre", "dd", "dt"].contains(&t.as_str()) {
                    out.push('\n');
                }
            }
            (true, _) => tag.push(c),
            (false, _) => out.push(c),
        }
    }
    let decoded = out
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
        .replace("&amp;", "&");
    decoded
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn page_title(html: &str) -> Option<String> {
    let start = html.find("<title>")? + 7;
    let end = html[start..].find("</title>")? + start;
    Some(to_text(&html[start..end]).trim_end_matches(" - ArchWiki").to_string())
}

fn sections(body: &str) -> Vec<(String, String)> {
    let mut result = vec![];
    let mut heading = "Introduction".to_string();
    let mut rest = body;
    loop {
        let next = ["<h2", "<h3"].iter().filter_map(|t| rest.find(t)).min();
        let Some(i) = next else {
            result.push((heading, to_text(rest)));
            return result;
        };
        result.push((heading.clone(), to_text(&rest[..i])));
        let after = &rest[i..];
        let Some(end) = after.find("</h").and_then(|j| after[j..].find('>').map(|k| j + k + 1)) else {
            return result;
        };
        heading = to_text(&after[..end]).replace("[edit]", "").trim().to_string();
        rest = &after[end..];
    }
}

pub fn build(db_path: &Path) -> Result<usize, String> {
    let e = |x: rusqlite::Error| x.to_string();
    let _ = fs::remove_file(db_path);
    let mut db = Connection::open(db_path).map_err(e)?;
    db.execute_batch("create virtual table wiki using fts5(title, section, body, tokenize='porter unicode61');").map_err(e)?;
    let tx = db.transaction().map_err(e)?;
    let mut count = 0;
    for entry in fs::read_dir(WIKI_DIR).map_err(|x| x.to_string())?.flatten() {
        let Ok(html) = fs::read_to_string(entry.path()) else {
            continue;
        };
        let Some(title) = page_title(&html) else {
            continue;
        };
        let start = html.find("id=\"mw-content-text\"").or_else(|| html.find("id=\"bodyContent\"")).unwrap_or(0);
        let end = html.find("id=\"catlinks\"").filter(|e| *e > start).unwrap_or(html.len());
        let body = strip_block(&strip_block(&strip_block(&html[start..end], "script"), "style"), "table");
        for (section, text) in sections(&body) {
            if text.len() > 40 {
                tx.execute("insert into wiki values (?1, ?2, ?3)", params![title, section, text]).map_err(e)?;
            }
        }
        count += 1;
    }
    let knowledge = Path::new(&std::env::var("HOME").unwrap_or_default()).join(".config/aero-assistant/knowledge");
    for entry in fs::read_dir(knowledge).into_iter().flatten().flatten() {
        let path = entry.path();
        if path.extension().is_some_and(|x| x == "md") {
            if let (Some(stem), Ok(text)) = (path.file_stem(), fs::read_to_string(&path)) {
                let title = format!("AeroOS: {}", stem.to_string_lossy());
                for part in text.split("\n## ") {
                    let (section, body) = part.split_once('\n').unwrap_or(("Introduction", part));
                    if !body.trim().is_empty() {
                        tx.execute("insert into wiki values (?1, ?2, ?3)", params![title, section.trim_start_matches("# "), body]).map_err(e)?;
                    }
                }
                count += 1;
            }
        }
    }
    tx.commit().map_err(e)?;
    db.execute_batch("insert into wiki(wiki) values('optimize');").map_err(e)?;
    Ok(count)
}

pub fn search(db_path: &Path, query: &str) -> Result<Value, String> {
    let db = Connection::open(db_path).map_err(|e| e.to_string())?;
    let terms: Vec<String> = query
        .split(|c: char| !c.is_alphanumeric() && c != '-' && c != '_')
        .filter(|t| t.len() > 1)
        .map(|t| format!("\"{t}\""))
        .collect();
    if terms.is_empty() {
        return Err("empty query".into());
    }
    let mut st = db
        .prepare("select title, section, snippet(wiki, 2, '', '', ' ... ', 30) from wiki where wiki match ?1 order by case when title like 'AeroOS:%' then bm25(wiki, 12.0, 4.0, 1.0) * 10.0 else bm25(wiki, 12.0, 4.0, 1.0) end limit 6")
        .map_err(|e| e.to_string())?;
    let rows = st
        .query_map([terms.join(" OR ")], |r| {
            Ok(json!({"title": r.get::<_, String>(0)?, "section": r.get::<_, String>(1)?, "snippet": r.get::<_, String>(2)?}))
        })
        .map_err(|e| e.to_string())?;
    Ok(Value::Array(rows.flatten().collect()))
}

pub fn read(db_path: &Path, title: &str, section: Option<&str>) -> Result<String, String> {
    let db = Connection::open(db_path).map_err(|e| e.to_string())?;
    let mut st = db.prepare("select section, body from wiki where title = ?1 collate nocase").map_err(|e| e.to_string())?;
    let rows: Vec<(String, String)> = st
        .query_map([title], |r| Ok((r.get(0)?, r.get(1)?)))
        .map_err(|e| e.to_string())?
        .flatten()
        .collect();
    if rows.is_empty() {
        let hits = search(db_path, title)?;
        let best = hits[0]["title"].as_str().map(String::from);
        return match best {
            Some(b) if !b.eq_ignore_ascii_case(title) => read(db_path, &b, section).map(|t| format!("Closest page: {b}\n{t}")),
            _ => Err(format!("no wiki page matches {title}")),
        };
    }
    let mut out = String::new();
    if let Some(s) = section {
        let wanted = s.to_lowercase();
        let words: Vec<&str> = wanted.split(|c: char| !c.is_alphanumeric()).filter(|w| w.len() > 2).collect();
        for (name, body) in &rows {
            let nl = name.to_lowercase();
            let bl = body.to_lowercase();
            if nl.contains(&wanted) || (!words.is_empty() && words.iter().any(|w| nl.contains(w) || bl.contains(w))) {
                out.push_str(&format!("## {name}\n{body}\n"));
            }
        }
    }
    if out.is_empty() {
        let total: usize = rows.iter().map(|(n, b)| n.len() + b.len() + 5).sum();
        if total <= 12000 {
            for (name, body) in &rows {
                out.push_str(&format!("## {name}\n{body}\n"));
            }
        } else {
            out.push_str("Sections: ");
            out.push_str(&rows.iter().map(|r| r.0.as_str()).collect::<Vec<_>>().join(" | "));
            out.push('\n');
            out.push_str(&rows[0].1);
        }
    }
    Ok(out)
}
