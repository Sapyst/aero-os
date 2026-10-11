// [Aero/doc 43] Botões ✖ ▲ ▼ ● da barra de cima (mesmos comandos das barras do sistema)
document.querySelectorAll(".controles-janela button").forEach((b) =>
  b.addEventListener("click", () => window.fileManager.janelaAcao(b.dataset.acao)));

// [Aero/doc 50] Date/time segments in the status bar
function aeroRelogio() {
  const agora = new Date();
  const hm = agora.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const left = document.querySelector(".status-left");
  if (left && !document.getElementById("aero-data")) {
    const d = document.createElement("span");
    d.id = "aero-data";
    left.prepend(d);
  }
  const right = document.querySelector(".status-right");
  if (right && !document.getElementById("aero-hora")) {
    const h = document.createElement("span");
    h.id = "aero-hora";
    right.append(h);
  }
  const data = document.getElementById("aero-data");
  if (data) data.textContent = agora.toLocaleDateString("en-US", { weekday: "short", day: "2-digit" });
  const hora = document.getElementById("aero-hora");
  if (hora) hora.textContent = hm;
}
aeroRelogio();
setInterval(aeroRelogio, 15000);
