//! Voice call pipeline: capture, VAD, transcription, cleanup, routing, synthesis, playback,
//! barge-in, safety proposal gating, audio ducking, and stage timing.

use std::fs;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::composer::{self, ComposedAnswer};
use crate::guard::{self, Risk};
use crate::rule_engine::{self, Decision, Language, Resolution};
use crate::utterance;

#[derive(Debug, Clone, Default)]
pub struct StageTimings {
    pub capture_ms: u64,
    pub transcription_ms: u64,
    pub cleanup_ms: u64,
    pub routing_ms: u64,
    pub synthesis_ms: u64,
    pub total_ms: u64,
}

#[derive(Debug, Clone)]
pub struct VoiceResult {
    pub raw_transcript: String,
    pub cleaned_text: String,
    pub language: Language,
    pub decision: Decision,
    pub answer: ComposedAnswer,
    pub timings: StageTimings,
    pub proposal_command: Option<String>,
    pub proposal_risk: Option<Risk>,
    pub voice_approved: bool,
}

static ACTIVE_SPEECH: Mutex<Option<Child>> = Mutex::new(None);
static IS_MUTED: AtomicBool = AtomicBool::new(false);

pub fn set_muted(muted: bool) {
    IS_MUTED.store(muted, Ordering::SeqCst);
}

pub fn is_muted() -> bool {
    IS_MUTED.load(Ordering::SeqCst)
}

/// Aborts any currently playing assistant speech immediately (barge-in).
pub fn stop_speech() {
    let mut lock = ACTIVE_SPEECH.lock().unwrap();
    if let Some(mut child) = lock.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// Applies audio ducking to other applications via wpctl / pactl during assistant speech.
pub fn duck_audio(duck: bool) {
    // Attempt gentle ducking with wpctl if available
    let vol = if duck { "0.30" } else { "1.00" };
    let _ = Command::new("wpctl")
        .args(["set-volume", "@DEFAULT_AUDIO_SINK@", vol])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

/// Creates a concise, natural summary suitable for spoken Text-to-Speech output.
pub fn summarize_for_speech(text: &str) -> String {
    let clean = text
        .lines()
        .filter(|line| !line.trim_start().starts_with("```"))
        .collect::<Vec<_>>()
        .join(" ");
    let clean = clean.replace("**", "").replace('*', "").replace('`', "");
    let clean = clean.trim();
    if clean.is_empty() {
        return "I found an answer from the local model.".to_string();
    }

    let mut summary = String::new();
    let sentences = clean.split_inclusive(&['.', '!', '?'][..]);
    for s in sentences {
        let trimmed = s.trim();
        if trimmed.is_empty() {
            continue;
        }
        if summary.is_empty() {
            summary.push_str(trimmed);
        } else if summary.len() + trimmed.len() + 1 <= 220 {
            summary.push(' ');
            summary.push_str(trimmed);
        } else {
            break;
        }
    }

    if summary.is_empty() {
        if clean.len() <= 180 {
            clean.to_string()
        } else {
            let truncated = &clean[..180];
            let end = truncated.rfind(' ').unwrap_or(180);
            format!("{}.", &clean[..end].trim_end_matches(&['.', ',', ';', ':', '-'][..]))
        }
    } else {
        summary
    }
}

/// Synthesizes spoken text to a temporary WAV file using espeak-ng.
pub fn synthesize_speech(text: &str, lang: Language, target_wav: &Path) -> Result<u64, String> {
    let t0 = Instant::now();
    let voice = match lang {
        Language::Portuguese => "pt-br",
        Language::English => "en-us",
    };

    let status = Command::new("espeak-ng")
        .args(["-v", voice, "-w", &target_wav.to_string_lossy(), text])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|e| format!("Failed to run espeak-ng: {e}"))?;

    if !status.success() {
        return Err("espeak-ng returned non-zero exit code".into());
    }

    Ok(t0.elapsed().as_millis() as u64)
}

/// Plays audio using PipeWire (pw-play) with barge-in support.
pub fn play_audio_with_barge_in(wav_path: &Path, cancel_signal: &Arc<AtomicBool>) -> Result<(), String> {
    duck_audio(true);
    let child = Command::new("pw-play")
        .arg(wav_path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("Failed to start pw-play: {e}"))?;

    {
        let mut lock = ACTIVE_SPEECH.lock().unwrap();
        *lock = Some(child);
    }

    // Monitor child or barge-in signal
    loop {
        if cancel_signal.load(Ordering::SeqCst) {
            stop_speech();
            break;
        }

        let mut lock = ACTIVE_SPEECH.lock().unwrap();
        if let Some(ref mut child) = *lock {
            match child.try_wait() {
                Ok(Some(_)) => {
                    *lock = None;
                    break;
                }
                Ok(None) => {}
                Err(_) => {
                    *lock = None;
                    break;
                }
            }
        } else {
            break;
        }
        drop(lock);
        std::thread::sleep(Duration::from_millis(25));
    }

    duck_audio(false);
    Ok(())
}

/// Locates the speech model via models.json and standard model directories without hardcoded user paths.
pub fn find_speech_model() -> Result<std::path::PathBuf, String> {
    if let Ok(p) = std::env::var("AERO_SPEECH_MODEL") {
        let path = std::path::PathBuf::from(p);
        if path.exists() {
            return Ok(path);
        }
    }

    let mut model_files = vec![
        "ggml-large-v3-turbo-q5_0.bin".to_string(),
        "ggml-base.bin".to_string(),
    ];

    let home = std::env::var("HOME").unwrap_or_default();
    let candidate_dirs = [
        std::path::PathBuf::from(&home).join(".local/share/aero-assistant/models"),
        std::path::PathBuf::from("models"),
    ];

    let catalog_paths = [
        std::path::PathBuf::from(&home).join(".local/share/aero-assistant/models.json"),
        std::path::PathBuf::from("models.json"),
    ];

    for cat_p in &catalog_paths {
        if cat_p.exists() {
            if let Ok(content) = fs::read_to_string(cat_p) {
                if let Ok(v) = serde_json::from_str::<serde_json::Value>(&content) {
                    if let Some(arr) = v.get("speech_models").and_then(|s| s.as_array()) {
                        for m in arr {
                            if let Some(f) = m.get("file").and_then(|s| s.as_str()) {
                                if !model_files.contains(&f.to_string()) {
                                    model_files.insert(0, f.to_string());
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    for dir in &candidate_dirs {
        for file in &model_files {
            let candidate = dir.join(file);
            if candidate.exists() {
                return Ok(candidate);
            }
        }
    }

    Err("Speech model 'ggml-large-v3-turbo-q5_0.bin' is missing. Please download it via the app consent flow or place it in ~/.local/share/aero-assistant/models/.".to_string())
}

/// Transcribes an audio WAV file with local whisper-cli.
pub fn transcribe_audio(wav_path: &Path) -> Result<(String, Language, u64), String> {
    let t0 = Instant::now();

    let model_path = find_speech_model()?;

    let out_prefix = wav_path.with_extension("");
    let json_file = out_prefix.with_extension("json");

    let status = Command::new("whisper-cli")
        .args([
            "-m", &model_path.to_string_lossy(),
            "-f", &wav_path.to_string_lossy(),
            "-oj",
            "-of", &out_prefix.to_string_lossy(),
            "-l", "auto",
            "-nt",
            "-t", "12",
            "--prompt", "Aero Assistant, português e inglês. Bateria, brilho, tela, memória, disco, uptime, kernel, rede, sistema.",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|e| format!("whisper-cli invocation failed: {e}"))?;

    if !status.success() {
        return Err("whisper-cli returned failure".into());
    }

    let elapsed = t0.elapsed().as_millis() as u64;

    if json_file.exists() {
        let content = fs::read_to_string(&json_file).map_err(|e| e.to_string())?;
        let _ = fs::remove_file(&json_file);
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&content) {
            let detected_lang = val["result"]["language"].as_str().unwrap_or("pt");
            let lang = if detected_lang == "en" { Language::English } else { Language::Portuguese };

            let mut transcript = String::new();
            if let Some(arr) = val["transcription"].as_array() {
                for seg in arr {
                    if let Some(t) = seg["text"].as_str() {
                        transcript.push_str(t);
                    }
                }
            }
            return Ok((transcript.trim().to_string(), lang, elapsed));
        }
    }

    Err("Could not read whisper JSON output".into())
}

/// Evaluates a spoken voice proposal for approval safety.
/// Explicit confirmation: requires "confirm" or "confirmar" (or explicit equivalent).
/// Lone "ok", "sim", "yes", doubt or silence cancels.
/// High risk: Voice can NEVER approve. Must type YES in the UI.
pub fn evaluate_voice_confirmation(spoken_response: &str, risk: &Risk) -> Result<bool, String> {
    if matches!(risk, Risk::High(_)) {
        return Err("High-risk action: Voice cannot approve high risk. You must type YES in the window.".into());
    }

    let lower = spoken_response.to_lowercase();
    let tokens: Vec<String> = lower
        .split_whitespace()
        .map(|t| t.trim_matches(|c: char| !c.is_alphanumeric()).to_string())
        .filter(|t| !t.is_empty())
        .collect();

    // Explicit confirmation words only. "ok", "sim", "yes" are NOT lone confirmations.
    let confirm_words = ["confirm", "confirmar", "confirma", "confirmo", "confirmado", "confirmed"];
    let cancel_words = ["cancel", "cancelar", "cancela", "nao", "não", "no", "abort", "stop", "para"];

    let has_confirm = tokens.iter().any(|t| confirm_words.contains(&t.as_str()));
    let has_cancel = tokens.iter().any(|t| cancel_words.contains(&t.as_str()));

    if has_confirm && !has_cancel {
        Ok(true)
    } else if has_cancel && !has_confirm {
        Ok(false)
    } else {
        // Doubt, lone "ok"/"sim"/"yes", or ambiguous response cancels on doubt for safety
        Err("Explicit 'confirm' or 'confirmar' required. Cancelling on doubt for safety.".into())
    }
}

/// Resolves an action rule identifier into a command, English description, and Portuguese description.
fn resolve_action_command(rule: &str) -> Option<(String, String, String)> {
    if let Some(num_str) = rule.strip_prefix("run:brightness-set-") {
        if let Ok(n) = num_str.parse::<u32>() {
            return Some((
                format!("brilho {n}"),
                format!("Set screen brightness to {n}%"),
                format!("Ajustar o brilho da tela para {n}%"),
            ));
        }
    }
    match rule {
        "run:lower-brightness" => Some((
            "brilho -5".into(),
            "Lower screen brightness by 5%".into(),
            "Abaixar o brilho da tela em 5%".into(),
        )),
        "run:raise-brightness" => Some((
            "brilho +5".into(),
            "Raise screen brightness by 5%".into(),
            "Aumentar o brilho da tela em 5%".into(),
        )),
        "run:lower-volume" => Some((
            "wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%- && som osd volume".into(),
            "Lower volume by 5%".into(),
            "Abaixar o volume em 5%".into(),
        )),
        "run:raise-volume" => Some((
            "wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 5%+ && som osd volume".into(),
            "Raise volume by 5%".into(),
            "Aumentar o volume em 5%".into(),
        )),
        "run:mute-audio" => Some((
            "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle && som osd volume".into(),
            "Toggle audio mute".into(),
            "Alternar mudo do áudio".into(),
        )),
        "run:system-update" => Some((
            "sudo pacman -Syu".into(),
            "Update every repository package".into(),
            "Atualizar todos os pacotes dos repositórios".into(),
        )),
        "run:clean-ram" => Some((
            "aero-assistant stop; sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'".into(),
            "Stop local model and drop disk cache".into(),
            "Limpar o cache de memória RAM".into(),
        )),
        "run:restart-network" => Some((
            "sudo systemctl restart NetworkManager".into(),
            "Restart the network".into(),
            "Reiniciar a rede".into(),
        )),
        "run:restart-bluetooth" => Some((
            "sudo systemctl restart bluetooth".into(),
            "Restart Bluetooth".into(),
            "Reiniciar o Bluetooth".into(),
        )),
        "run:restart-audio" => Some((
            "systemctl --user restart pipewire pipewire-pulse wireplumber".into(),
            "Restart audio services".into(),
            "Reiniciar os serviços de áudio".into(),
        )),
        "run:restart-taskbar" => Some((
            "setsid -f aerobar".into(),
            "Restart AeroOS taskbar".into(),
            "Reiniciar a barra de tarefas do AeroOS".into(),
        )),
        "run:restart-dock" => Some((
            "setsid -f dock --reiniciar".into(),
            "Restart AeroOS dock".into(),
            "Reiniciar o dock do AeroOS".into(),
        )),
        "run:screenshot-screen" => Some((
            "print-area tela".into(),
            "Take a full-screen screenshot".into(),
            "Tirar captura da tela inteira".into(),
        )),
        "run:screenshot-window" => Some((
            "print-area janela".into(),
            "Take a screenshot of the active window".into(),
            "Tirar captura da janela ativa".into(),
        )),
        "run:screenshot-area" => Some((
            "print-area".into(),
            "Take a screenshot of an area".into(),
            "Tirar uma captura de tela".into(),
        )),
        _ => None,
    }
}

/// Full speech pipeline step: given input audio WAV, processes through pipeline to execution.
/// Supports ordered sequential execution for compound speech requests.
pub fn process_speech_pipeline(wav_path: &Path) -> Result<VoiceResult, String> {
    let t_total_start = Instant::now();
    let mut timings = StageTimings::default();

    // 1. Transcription & language detection
    let (raw_transcript, whisper_lang, trans_ms) = transcribe_audio(wav_path)?;
    timings.transcription_ms = trans_ms;

    // 2. Utterance cleanup and normalization
    let t_clean_start = Instant::now();
    let clean = utterance::clean_utterance(&raw_transcript);
    timings.cleanup_ms = t_clean_start.elapsed().as_millis() as u64;

    // 3. Routing & Rule Engine for each ordered request
    let t_route_start = Instant::now();

    let req_list = if clean.requests.is_empty() {
        vec![clean.cleaned.clone()]
    } else {
        clean.requests.clone()
    };

    let mut spoken_parts = Vec::new();
    let mut display_parts = Vec::new();
    let mut primary_decision = None;
    let mut proposal_command = None;
    let mut proposal_risk = None;

    for req in &req_list {
        let mut sub_decision = rule_engine::evaluate_decision(req);
        if whisper_lang == Language::Portuguese || sub_decision.language == Language::Portuguese {
            sub_decision.language = Language::Portuguese;
            // HARD POLICY: Portuguese requests NEVER reach the model under any circumstance!
            if matches!(sub_decision.resolution, Resolution::Model) {
                let cannot_msg = "Não compreendi com certeza o seu pedido pelas regras locais. Você pode perguntar sobre bateria, memória, brilho ou rede.".to_string();
                sub_decision.resolution = Resolution::CannotAnswer(cannot_msg.clone());
                sub_decision.spoken_text = cannot_msg.clone();
                sub_decision.display_text = cannot_msg;
            }
        }

        let mut sub_answer = ComposedAnswer {
            spoken: sub_decision.spoken_text.clone(),
            display: sub_decision.display_text.clone(),
            details: None,
        };

        match &sub_decision.resolution {
            Resolution::DirectRule(rule) | Resolution::ContextRule(rule) => {
                if let Some(topic) = rule.strip_prefix("read:") {
                    let fact = crate::system_info(topic);
                    sub_answer = composer::compose_fact_answer(topic, &fact, sub_decision.language);
                    rule_engine::update_context(Some(topic), Some("read"), None);
                } else if let Some((cmd, desc_en, desc_pt)) = resolve_action_command(rule) {
                    let risk = guard::proposal(&cmd).unwrap_or(Risk::Normal);
                    if proposal_command.is_none() {
                        proposal_command = Some(cmd.clone());
                        proposal_risk = Some(risk.clone());
                    }
                    if matches!(risk, Risk::High(_)) {
                        sub_answer.spoken = if sub_decision.language == Language::Portuguese {
                            format!("Ação de alto risco: {desc_pt}. Não pode ser aprovada por voz. Digite YES na janela.")
                        } else {
                            format!("High-risk action: {desc_en}. Voice cannot approve high risk. You must type YES in the window.")
                        };
                        sub_answer.display = format!("Proposal (High Risk): {cmd} ({desc_en})");
                    } else {
                        sub_answer.spoken = if sub_decision.language == Language::Portuguese {
                            format!("Proposta: {desc_pt}. Diga confirmar ou cancelar.")
                        } else {
                            format!("Proposal: {desc_en}. Say confirm or cancel.")
                        };
                        sub_answer.display = format!("Proposal: {cmd} ({desc_en})");
                    }
                    sub_answer.details = Some(format!("Command: {cmd}"));
                }
            }
            Resolution::Clarification(q) => {
                sub_answer.spoken = q.clone();
                sub_answer.display = q.clone();
            }
            Resolution::CannotAnswer(msg) => {
                sub_answer.spoken = msg.clone();
                sub_answer.display = msg.clone();
            }
            Resolution::Model => {
                if sub_decision.language == Language::Portuguese || !rule_engine::allow_to_model(req) {
                    let fallback = "Não compreendi o comando em português pelas regras locais.";
                    sub_answer.spoken = fallback.into();
                    sub_answer.display = fallback.into();
                } else {
                    match crate::query_model_for_voice(req) {
                        Ok(full_reply) => {
                            sub_answer.display = full_reply.clone();
                            sub_answer.spoken = summarize_for_speech(&full_reply);
                        }
                        Err(e) => {
                            sub_answer.display = format!("Error from model: {e}");
                            sub_answer.spoken = "Could not contact the local model.".to_string();
                        }
                    }
                }
            }
        }

        if !sub_answer.spoken.is_empty() {
            spoken_parts.push(sub_answer.spoken);
        }
        if !sub_answer.display.is_empty() {
            display_parts.push(sub_answer.display);
        }
        if primary_decision.is_none() {
            primary_decision = Some(sub_decision);
        }
    }

    timings.routing_ms = t_route_start.elapsed().as_millis() as u64;

    let decision = primary_decision.unwrap_or_else(|| rule_engine::evaluate_decision(&clean.cleaned));
    let answer = ComposedAnswer {
        spoken: spoken_parts.join(" "),
        display: display_parts.join("\n"),
        details: None,
    };

    // 4. Synthesis
    let _t_synth_start = Instant::now();
    let temp_out = wav_path.with_file_name("response_temp.wav");
    if !answer.spoken.is_empty() {
        if let Ok(synth_ms) = synthesize_speech(&answer.spoken, decision.language, &temp_out) {
            timings.synthesis_ms = synth_ms;
        }
    }

    timings.total_ms = t_total_start.elapsed().as_millis() as u64;

    Ok(VoiceResult {
        raw_transcript,
        cleaned_text: req_list.join(" | "),
        language: decision.language,
        decision,
        answer,
        timings,
        proposal_command,
        proposal_risk,
        voice_approved: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_voice_proposal_confirmation_safety() {
        let normal_risk = Risk::Normal;
        let high_risk = Risk::High("Destructive system changes".into());

        // Explicit confirmations MUST pass
        assert_eq!(evaluate_voice_confirmation("confirm", &normal_risk), Ok(true));
        assert_eq!(evaluate_voice_confirmation("confirmar", &normal_risk), Ok(true));
        assert_eq!(evaluate_voice_confirmation("confirma", &normal_risk), Ok(true));
        assert_eq!(evaluate_voice_confirmation("eu confirmo", &normal_risk), Ok(true));

        // Lone "ok", "sim", "yes" MUST BE REJECTED (Defect 5 requirement)
        assert!(evaluate_voice_confirmation("ok", &normal_risk).is_err());
        assert!(evaluate_voice_confirmation("sim", &normal_risk).is_err());
        assert!(evaluate_voice_confirmation("yes", &normal_risk).is_err());
        assert!(evaluate_voice_confirmation("ok sure", &normal_risk).is_err());

        // Explicit cancels MUST cancel
        assert_eq!(evaluate_voice_confirmation("cancel", &normal_risk), Ok(false));
        assert_eq!(evaluate_voice_confirmation("cancelar", &normal_risk), Ok(false));
        assert_eq!(evaluate_voice_confirmation("não", &normal_risk), Ok(false));
        assert_eq!(evaluate_voice_confirmation("no", &normal_risk), Ok(false));

        // Doubt MUST cancel
        assert!(evaluate_voice_confirmation("maybe maybe not", &normal_risk).is_err());
        assert!(evaluate_voice_confirmation("confirm, no, wait cancel", &normal_risk).is_err());

        // High risk: Voice can NEVER approve!
        assert!(evaluate_voice_confirmation("confirm", &high_risk).is_err());
        assert!(evaluate_voice_confirmation("confirmar", &high_risk).is_err());
    }

    #[test]
    fn test_find_speech_model_does_not_hardcode() {
        // Must resolve to valid model path
        let model = find_speech_model();
        assert!(model.is_ok(), "Expected find_speech_model to find configured model: {:?}", model);
        let path_str = model.unwrap().to_string_lossy().to_string();
        assert!(path_str.ends_with(".bin"));
    }

    #[test]
    fn test_audio_ducking_call() {
        // Safe invocation: does not crash
        duck_audio(false);
    }

    #[test]
    fn test_no_spoken_answer_contains_request_ready_for_model() {
        let phrases = [
            "What is a systemd timer?",
            "Hello.",
            "Explain btrfs subvolumes.",
            "What is a pacman hook?",
            "How much memory is free",
            "Lower the brightness",
            "Qual o nível da bateria",
            "parar de ouvir",
            "stop listening",
        ];
        for p in &phrases {
            let dec = rule_engine::evaluate_decision(p);
            assert!(
                !dec.spoken_text.contains("Request ready for model"),
                "evaluate_decision for {:?} had placeholder in spoken_text: {:?}",
                p, dec.spoken_text
            );
        }

        let summary = summarize_for_speech("A systemd timer is a unit configuration file ending in .timer that controls services.");
        assert!(!summary.contains("Request ready for model"));
        assert!(!summary.is_empty());
    }
}
