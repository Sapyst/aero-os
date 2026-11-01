//! The conversation screen: a scrolling chat above fixed status, cost, hardware and input lines.

use std::collections::VecDeque;
use std::fs;
use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Mutex, OnceLock};
use std::thread::{sleep, spawn};
use std::time::{Duration, Instant};

const COMPACT_LINES: u16 = 4;
const PANEL_LINES: u16 = 11;
const PANEL_MIN_ROWS: u16 = 22;
const TIOCGWINSZ: u64 = 0x5413;

fn fixed_lines(rows: u16) -> u16 {
    if rows >= PANEL_MIN_ROWS { PANEL_LINES } else { COMPACT_LINES }
}

#[repr(C)]
struct WinSize {
    rows: u16,
    cols: u16,
    x: u16,
    y: u16,
}

#[repr(C)]
struct PollFd {
    fd: i32,
    events: i16,
    revents: i16,
}

unsafe extern "C" {
    fn ioctl(fd: i32, req: u64, arg: *mut WinSize) -> i32;
    fn poll(fds: *mut PollFd, n: u64, timeout: i32) -> i32;
    fn read(fd: i32, buf: *mut std::ffi::c_void, count: usize) -> isize;
}

pub enum Event {
    Line(String),
    Eof,
}

#[derive(Default)]
struct State {
    status: String,
    since: Option<Instant>,
    tokens: usize,
    context: Option<u64>,
    context_max: u64,
    queue: VecDeque<String>,
    input: String,
    busy: bool,
    send_first: usize,
    send_all: usize,
    costs: String,
    hardware: String,
    process_rows: Vec<String>,
    total_row: String,
    hardware_row: String,
    rows: u16,
    cols: u16,
}

static ACTIVE: AtomicBool = AtomicBool::new(false);
static PAUSED: AtomicBool = AtomicBool::new(false);
static FINISHED: AtomicBool = AtomicBool::new(false);

fn state() -> &'static Mutex<State> {
    static STATE: OnceLock<Mutex<State>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(State::default()))
}

fn saved_tty() -> &'static Mutex<String> {
    static SAVED: OnceLock<Mutex<String>> = OnceLock::new();
    SAVED.get_or_init(|| Mutex::new(String::new()))
}

pub fn active() -> bool {
    ACTIVE.load(Ordering::SeqCst)
}

fn size() -> (u16, u16) {
    let mut w = WinSize { rows: 24, cols: 80, x: 0, y: 0 };
    unsafe {
        ioctl(1, TIOCGWINSZ, &mut w);
    }
    (w.rows.max(COMPACT_LINES + 3), w.cols.max(20))
}

fn stty(args: &[&str]) -> String {
    Command::new("stty").args(args).stdin(Stdio::inherit()).stderr(Stdio::null()).output().map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default()
}

fn emit(text: &str) {
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(text.as_bytes());
    let _ = out.flush();
}

fn clip(text: &str, cols: usize) -> String {
    text.chars().take(cols).collect()
}

fn panel_row(text: &str, inner: usize) -> String {
    let clipped = clip(text, inner);
    let pad = inner - clipped.chars().count();
    format!("\x1b[36m│\x1b[0m {clipped}{} \x1b[36m│\x1b[0m", " ".repeat(pad))
}

fn panel_rule(left: char, title: &str, right: char, cols: usize) -> String {
    let label = if title.is_empty() { String::new() } else { format!(" {title} ") };
    let fill = cols.saturating_sub(3 + label.chars().count());
    format!("\x1b[36m{left}─{label}{}{right}\x1b[0m", "─".repeat(fill))
}

fn frame(s: &State) -> String {
    let cols = s.cols as usize;
    let lines = fixed_lines(s.rows);
    let top = s.rows - lines + 1;
    let label = if s.status.is_empty() { "Ready" } else { &s.status };
    let dot = if s.busy { "\x1b[33m●\x1b[0m" } else { "\x1b[32m●\x1b[0m" };
    let mut status = label.to_string();
    if let (true, Some(t)) = (s.busy, s.since) {
        status.push_str(&format!(" {}s", t.elapsed().as_secs()));
    }
    if s.tokens > 0 {
        status.push_str(&format!(" · {} tokens", s.tokens));
    }
    match s.context {
        Some(c) => status.push_str(&format!(" · context {c}/{}", s.context_max)),
        None => status.push_str(&format!(" · context -/{}", s.context_max)),
    }
    status.push_str(&format!(" · queue {}", s.queue.len()));
    if let Some(next) = s.queue.front() {
        status.push_str(&format!(" (next: {next})"));
    }
    let waiting = if s.busy { " [queued until -send] " } else { "" };
    let input_room = cols.saturating_sub(4 + waiting.len());
    let shown: String = {
        let chars: Vec<char> = s.input.chars().collect();
        chars[chars.len().saturating_sub(input_room)..].iter().collect()
    };
    let input = format!("\x1b[36m>{waiting}\x1b[0m {shown}█");
    let inner = cols.saturating_sub(4);
    let rows: Vec<String> = if lines == PANEL_LINES && inner >= 30 {
        let status_row = panel_row(&format!("  {status}"), inner).replacen("  ", &format!(" {dot}"), 1);
        let mut out = vec![panel_rule('╭', "Aero Assistant", '╮', cols), status_row, panel_rule('├', "Processes", '┤', cols)];
        let empty = String::new();
        for i in 0..3 {
            out.push(panel_row(s.process_rows.get(i).unwrap_or(&empty), inner));
        }
        out.push(panel_row(&s.total_row, inner));
        out.push(panel_rule('├', "Hardware", '┤', cols));
        out.push(panel_row(&s.hardware_row, inner));
        out.push(panel_rule('╰', "", '╯', cols));
        out.push(input);
        out
    } else {
        vec![format!("{dot} \x1b[1m{}\x1b[0m", clip(&status, cols.saturating_sub(2))), format!("\x1b[2m{}\x1b[0m", clip(&s.costs, cols)), format!("\x1b[2m{}\x1b[0m", clip(&s.hardware, cols)), input]
    };
    let mut out = String::from("\x1b7");
    for (i, row) in rows.iter().enumerate() {
        out.push_str(&format!("\x1b[{};1H\x1b[2K{row}", top + i as u16));
    }
    out.push_str("\x1b8");
    out
}

pub fn redraw() {
    if !active() || PAUSED.load(Ordering::SeqCst) {
        return;
    }
    if let Ok(s) = state().lock() {
        if s.rows > 0 {
            emit(&frame(&s));
        }
    }
}

fn apply_region(rows: u16) {
    let lines = fixed_lines(rows);
    let room = "\n".repeat(lines as usize);
    emit(&format!("{room}\x1b[{lines}A\x1b7\x1b[1;{}r\x1b8\x1b[?25l", rows - lines));
}

pub fn status(text: &str) {
    if !active() {
        return;
    }
    if let Ok(mut s) = state().lock() {
        s.status = text.to_string();
    }
    redraw();
}

pub fn set_busy(busy: bool) {
    if !active() {
        return;
    }
    if let Ok(mut s) = state().lock() {
        s.busy = busy;
        s.since = busy.then(Instant::now);
        s.tokens = 0;
        if !busy {
            s.status = "Ready".into();
        }
    }
    redraw();
}

pub fn add_token() {
    if !active() {
        return;
    }
    if let Ok(mut s) = state().lock() {
        s.tokens += 1;
    }
}

pub fn set_context(used: u64) {
    if let Ok(mut s) = state().lock() {
        s.context = Some(used);
    }
}

/// Gives the terminal back for a question or a command that needs it.
pub fn suspend() {
    if !active() {
        return;
    }
    PAUSED.store(true, Ordering::SeqCst);
    sleep(Duration::from_millis(120));
    let rows = state().lock().map(|s| s.rows).unwrap_or(24);
    let mut clear = String::from("\x1b7");
    for i in 0..fixed_lines(rows) {
        clear.push_str(&format!("\x1b[{};1H\x1b[2K", rows - i));
    }
    emit(&format!("{clear}\x1b8\x1b7\x1b[r\x1b8\x1b[?25h"));
    let saved = saved_tty().lock().map(|s| s.clone()).unwrap_or_default();
    stty(&[&saved]);
}

pub fn resume() {
    if !active() {
        return;
    }
    stty(&["-icanon", "-echo", "min", "1"]);
    let rows = state().lock().map(|s| s.rows).unwrap_or(24);
    apply_region(rows);
    PAUSED.store(false, Ordering::SeqCst);
    redraw();
}

pub fn finish() {
    if !ACTIVE.swap(false, Ordering::SeqCst) {
        return;
    }
    FINISHED.store(true, Ordering::SeqCst);
    let rows = state().lock().map(|s| s.rows).unwrap_or(24);
    let mut clear = String::from("\x1b7");
    for i in 0..fixed_lines(rows) {
        clear.push_str(&format!("\x1b[{};1H\x1b[2K", rows - i));
    }
    emit(&format!("{clear}\x1b8\x1b7\x1b[r\x1b8\x1b[?25h\n"));
    let saved = saved_tty().lock().map(|s| s.clone()).unwrap_or_default();
    stty(&[&saved]);
}

fn on_enter(line: String, tx: &Sender<Event>) {
    let Ok(mut s) = state().lock() else { return };
    if !s.busy {
        drop(s);
        let _ = tx.send(Event::Line(line));
        return;
    }
    match line.trim() {
        "" => {}
        "-send" => s.send_first += 1,
        "--send" => s.send_all = s.queue.len(),
        _ => s.queue.push_back(line),
    }
}

fn reader(tx: Sender<Event>) {
    let mut pending: Vec<u8> = vec![];
    let mut escape = false;
    loop {
        if FINISHED.load(Ordering::SeqCst) {
            return;
        }
        if PAUSED.load(Ordering::SeqCst) {
            sleep(Duration::from_millis(40));
            continue;
        }
        let mut fds = PollFd { fd: 0, events: 1, revents: 0 };
        if unsafe { poll(&mut fds, 1, 80) } <= 0 || PAUSED.load(Ordering::SeqCst) {
            continue;
        }
        let mut buf = [0u8; 256];
        let n = unsafe { read(0, buf.as_mut_ptr() as *mut std::ffi::c_void, buf.len()) };
        if n <= 0 {
            let _ = tx.send(Event::Eof);
            return;
        }
        for &b in &buf[..n as usize] {
            if escape {
                if b.is_ascii_alphabetic() || b == b'~' {
                    escape = false;
                }
                continue;
            }
            match b {
                0x1b => escape = true,
                b'\r' | b'\n' => {
                    let line = state().lock().map(|mut s| std::mem::take(&mut s.input)).unwrap_or_default();
                    on_enter(line, &tx);
                }
                0x7f | 0x08 => {
                    if let Ok(mut s) = state().lock() {
                        s.input.pop();
                    }
                }
                0x15 => {
                    if let Ok(mut s) = state().lock() {
                        s.input.clear();
                    }
                }
                0x04 => {
                    let empty = state().lock().map(|s| s.input.is_empty()).unwrap_or(true);
                    if empty {
                        let _ = tx.send(Event::Eof);
                    }
                }
                b if b < 0x20 => {}
                b => {
                    pending.push(b);
                    if let Ok(text) = std::str::from_utf8(&pending) {
                        if let Ok(mut s) = state().lock() {
                            s.input.push_str(text);
                        }
                        pending.clear();
                    } else if pending.len() > 4 {
                        pending.clear();
                    }
                }
            }
        }
        redraw();
    }
}

fn human(kb: u64) -> String {
    if kb >= 1024 * 1024 {
        format!("{:.1} GB", kb as f64 / 1048576.0)
    } else {
        format!("{} MB", kb / 1024)
    }
}

struct Proc {
    pid: u32,
    ppid: u32,
    comm: String,
    ticks: u64,
    rss_kb: u64,
}

fn processes() -> Vec<Proc> {
    let mut out = vec![];
    for e in fs::read_dir("/proc").into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        let Ok(pid) = name.parse::<u32>() else { continue };
        let Ok(stat) = fs::read_to_string(format!("/proc/{pid}/stat")) else { continue };
        let (Some(a), Some(b)) = (stat.find('('), stat.rfind(')')) else { continue };
        let f: Vec<&str> = stat[b + 2..].split_whitespace().collect();
        if f.len() < 22 {
            continue;
        }
        out.push(Proc {
            pid,
            ppid: f[1].parse().unwrap_or(0),
            comm: stat[a + 1..b].to_string(),
            ticks: f[11].parse::<u64>().unwrap_or(0) + f[12].parse::<u64>().unwrap_or(0),
            rss_kb: f[21].parse::<u64>().unwrap_or(0) * 4,
        });
    }
    out
}

fn meminfo(key: &str, text: &str) -> u64 {
    text.lines().find(|l| l.starts_with(key)).and_then(|l| l.split_whitespace().nth(1)).and_then(|v| v.parse().ok()).unwrap_or(0)
}

fn cpu_total() -> (u64, u64) {
    let text = fs::read_to_string("/proc/stat").unwrap_or_default();
    let v: Vec<u64> = text.lines().next().unwrap_or("").split_whitespace().skip(1).filter_map(|x| x.parse().ok()).collect();
    (v.iter().sum(), v.get(3).copied().unwrap_or(0) + v.get(4).copied().unwrap_or(0))
}

fn cpu_temp() -> Option<f64> {
    for e in fs::read_dir("/sys/class/hwmon").ok()?.flatten() {
        if fs::read_to_string(e.path().join("name")).ok()?.trim() == "k10temp" {
            return fs::read_to_string(e.path().join("temp1_input")).ok()?.trim().parse::<f64>().ok().map(|t| t / 1000.0);
        }
    }
    None
}

fn sampler() {
    let me = std::process::id();
    let mut last = Instant::now();
    let mut before: std::collections::HashMap<u32, u64> = Default::default();
    let (mut cpu_all, mut cpu_idle) = cpu_total();
    let mut gpu = String::new();
    let mut gpu_at: Option<Instant> = None;
    let mut model_prev = false;
    loop {
        for _ in 0..10 {
            if FINISHED.load(Ordering::SeqCst) {
                return;
            }
            sleep(Duration::from_millis(100));
        }
        let (rows, cols) = size();
        let resized = state().lock().map(|mut s| {
            let changed = s.rows != rows;
            s.rows = rows;
            s.cols = cols;
            changed
        });
        if resized.unwrap_or(false) && !PAUSED.load(Ordering::SeqCst) {
            apply_region(rows);
        }
        let dt = last.elapsed().as_secs_f64().max(0.1);
        last = Instant::now();
        let list = processes();
        let pct = |p: &Proc, prev: &std::collections::HashMap<u32, u64>| prev.get(&p.pid).map_or(0.0, |t| (p.ticks.saturating_sub(*t)) as f64 / 100.0 / dt * 100.0);
        let (mut own_kb, mut own_cpu, mut tool_kb, mut tool_cpu, mut model_kb, mut model_cpu) = (0, 0.0, 0, 0.0, 0, 0.0);
        let mut model = false;
        let mut model_pid = 0;
        let mut tool_names: Vec<String> = vec![];
        for p in &list {
            if p.pid == me {
                own_kb = p.rss_kb;
                own_cpu = pct(p, &before);
            } else if p.ppid == me {
                tool_kb += p.rss_kb;
                tool_cpu += pct(p, &before);
                tool_names.push(format!("{} ({})", p.comm, p.pid));
            } else if p.comm == "janas-server" {
                model = true;
                model_pid = p.pid;
                model_kb = p.rss_kb;
                model_cpu = pct(p, &before);
            }
        }
        before = list.iter().map(|p| (p.pid, p.ticks)).collect();
        model_prev |= model;
        let model_text = if model { format!("{} {model_cpu:.0}%", human(model_kb)) } else if model_prev { "closed".into() } else { "not loaded".into() };
        let costs = format!(
            "Cost: aero {} {own_cpu:.0}% | model {model_text} | tools {} {tool_cpu:.0}% | total {} {:.0}%",
            human(own_kb),
            human(tool_kb),
            human(own_kb + tool_kb + model_kb),
            own_cpu + tool_cpu + model_cpu
        );
        let line = |name: &str, pid: String, kb: u64, cpu: f64| format!("{name:<22} {pid:>8} {:>9} {:>6}", human(kb), format!("{cpu:.0}%"));
        let process_rows = vec![
            line("aero-assistant", me.to_string(), own_kb, own_cpu),
            if model { line("janas-server (model)", model_pid.to_string(), model_kb, model_cpu) } else { format!("{:<22} {model_text}", "janas-server (model)") },
            if tool_names.is_empty() { format!("{:<22} none running", "tools") } else { line(&clip(&tool_names.join(", "), 22), String::new(), tool_kb, tool_cpu) },
        ];
        let total_row = line("Total", String::new(), own_kb + tool_kb + model_kb, own_cpu + tool_cpu + model_cpu);
        let mem = fs::read_to_string("/proc/meminfo").unwrap_or_default();
        let (total, avail) = (meminfo("MemTotal:", &mem), meminfo("MemAvailable:", &mem));
        let (swap_total, swap_free) = (meminfo("SwapTotal:", &mem), meminfo("SwapFree:", &mem));
        let (all, idle) = cpu_total();
        let busy = 100.0 * (1.0 - (idle.saturating_sub(cpu_idle)) as f64 / (all.saturating_sub(cpu_all)).max(1) as f64);
        (cpu_all, cpu_idle) = (all, idle);
        if gpu_at.is_none_or(|t| t.elapsed() > Duration::from_secs(5)) {
            gpu_at = Some(Instant::now());
            let out = Command::new("nvidia-smi").args(["--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]).stderr(Stdio::null()).output();
            gpu = out
                .ok()
                .map(|o| String::from_utf8_lossy(&o.stdout).to_string())
                .and_then(|t| {
                    let f: Vec<u64> = t.trim().split(", ").filter_map(|x| x.parse().ok()).collect();
                    (f.len() == 3).then(|| format!(" | GPU {}% VRAM {:.1}/{:.1} GB", f[0], f[1] as f64 / 1024.0, f[2] as f64 / 1024.0))
                })
                .unwrap_or_default();
        }
        let temp = cpu_temp().map(|t| format!(" {t:.0}°C")).unwrap_or_default();
        let hardware_row = format!("RAM {}/{} | swap {}/{} | CPU {busy:.0}%{temp}{gpu}", human(total - avail), human(total), human(swap_total - swap_free), human(swap_total));
        let hardware = format!("Hardware: RAM {}/{} | swap {}/{} | CPU {busy:.0}%{temp}{gpu}", human(total - avail), human(total), human(swap_total - swap_free), human(swap_total));
        if let Ok(mut s) = state().lock() {
            s.costs = costs;
            s.hardware = hardware;
            s.process_rows = process_rows;
            s.total_row = total_row;
            s.hardware_row = hardware_row;
        }
        redraw();
    }
}

/// Starts the screen. Returns the channel of lines typed while the model is idle.
pub fn start(context_max: u64) -> Receiver<Event> {
    let (tx, rx) = channel();
    if let Ok(mut saved) = saved_tty().lock() {
        *saved = stty(&["-g"]);
    }
    stty(&["-icanon", "-echo", "min", "1"]);
    let (rows, cols) = size();
    if let Ok(mut s) = state().lock() {
        s.rows = rows;
        s.cols = cols;
        s.status = "Ready".into();
        s.context_max = context_max;
        s.costs = "Cost: measuring...".into();
        s.hardware = "Hardware: measuring...".into();
        s.hardware_row = "measuring...".into();
    }
    ACTIVE.store(true, Ordering::SeqCst);
    apply_region(rows);
    redraw();
    spawn(move || reader(tx));
    spawn(sampler);
    rx
}

/// The next queued request that was released with -send or --send.
pub fn next_released() -> Option<String> {
    let mut s = state().lock().ok()?;
    if s.send_all > 0 {
        s.send_all -= 1;
        return s.queue.pop_front();
    }
    if s.send_first > 0 {
        s.send_first -= 1;
        return s.queue.pop_front();
    }
    None
}

pub fn release_first() -> bool {
    state().lock().map(|mut s| {
        if s.queue.is_empty() {
            false
        } else {
            s.send_first += 1;
            true
        }
    }).unwrap_or(false)
}

pub fn release_all() -> usize {
    state().lock().map(|mut s| {
        s.send_all = s.queue.len();
        s.send_all
    }).unwrap_or(0)
}

pub fn queue_list() -> Vec<String> {
    state().lock().map(|s| s.queue.iter().cloned().collect()).unwrap_or_default()
}

pub fn clear_queue() -> usize {
    state().lock().map(|mut s| {
        let n = s.queue.len();
        s.queue.clear();
        s.send_all = 0;
        s.send_first = 0;
        n
    }).unwrap_or(0)
}
