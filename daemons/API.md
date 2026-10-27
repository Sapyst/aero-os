# API D-Bus dos daemons do Aero OS (para o shell nativo do Daimon)

Todos os serviços ficam no **barramento de sessão**. Regra geral: **todo setter devolve o estado que
realmente ficou**, relido na fonte da verdade (PipeWire, NetworkManager, BlueZ, sysfs, arquivo de
tema...) — a UI só muda quando a resposta (ou o sinal) confirma. Nenhum setter segura o chamador mais
de ~1,8 s: se a ação demora mais (conectar Bluetooth/Wi-Fi, `ddcutil detect`), a resposta vem com
`"pending": true` e o sinal correspondente traz o estado final.

Os JSONs são devolvidos como **string** (`s`) para manter a interface estável; `"error"` aparece dentro
do JSON quando algo falhou (o estado atual vem junto mesmo assim).

| Serviço | Nome | Caminho | Interface | Binário |
|---|---|---|---|---|
| Hermes (notificações + Hestia) | `com.aero.Hermes` | `/org/freedesktop/Notifications` | `com.aero.Hermes` | `daemons/hermesd` |
| Sessão (tema, brilho, energia, ocioso) | `com.aero.Sessiond` | `/com/aero/Sessiond` | `com.aero.Sessiond` | `daemons/aero-sessiond` |

Instâncias de teste (rodam ao lado das reais, sem tocar na sessão): `hermesd --teste` →
`com.aero.Hermes.Teste` (não assume `org.freedesktop.Notifications` nem mexe no `som.py`);
`aero-sessiond --dbus-teste` → `com.aero.Sessiond.Teste` (só o serviço D-Bus).

---

## 1. `com.aero.Hermes`

### 1.1 Áudio (PipeWire)

Fonte: `pactl -f json list sinks|sources` (já usado pelo Hermes) + `wpctl` para gravar. O **`id`** de cada
aparelho é o `object.id` do PipeWire — o mesmo número que o `wpctl status` mostra — e é ele que os
setters recebem. `index` é o índice do pactl (só informativo).

Aparelho (`Aparelho`):
```json
{"id": 80, "index": 95, "name": "alsa_output.usb-...analog-stereo", "pretty_name": "AC511 Sound Bar",
 "is_default": true, "volume": 100, "muted": false}
```
Estado completo (`EstadoAudio`): `{"sinks": [Aparelho...], "sources": [Aparelho...]}` (os `.monitor` não entram).

| Método | Assinatura | Devolve |
|---|---|---|
| `ListSinks` | `() → s` | JSON `[Aparelho...]` (saídas) |
| `ListSources` | `() → s` | JSON `[Aparelho...]` (entradas/microfones) |
| `SetDefaultSink` | `(u id) → s` | `EstadoAudio` relido (+`error`) |
| `SetDefaultSource` | `(u id) → s` | `EstadoAudio` relido (+`error`) |
| `SetVolume` | `(u id, u pct) → s` | `EstadoAudio` relido; `pct` 0..150 (teto do `wpctl -l 1.5`) |
| `SetMute` | `(u id, b muted) → s` | `EstadoAudio` relido |
| `GetAudio` | `() → s` | (antigo) JSON cru do pactl com `id`, `is_default`, `pretty_name`, `volume_pct` acrescentados |

Sinal: **`AudioChanged(s state_json)`** — `EstadoAudio` completo. Emitido depois de cada setter e a cada
evento do `pactl subscribe` (só quando o estado mudou de verdade).

### 1.2 Wi-Fi (NetworkManager, barramento do sistema)

`EstadoWifi`: `{"enabled": true, "connected": true, "ssid": "Casa", "strength": 68, "state": 100}`
(`state` = `NMDeviceState`; 100 = ativado, 30 = desconectado, 50–90 = conectando).

Rede (`Rede`): `{"ssid": "Casa", "strength": 68, "secured": true, "known": true, "in_use": true}` —
uma por SSID (a mais forte), ordenadas: em uso primeiro, depois por força.

| Método | Assinatura | Devolve |
|---|---|---|
| `GetWifi` | `() → s` | `EstadoWifi` |
| `SetWifiEnabled` | `(b enabled) → s` | `EstadoWifi` relido (espera o rádio refletir, até 1 s) |
| `ListNetworks` | `(b rescan) → s` | JSON `[Rede...]`; `rescan=true` pede varredura e espera ~1,2 s |
| `WifiConnect` | `(s ssid, s password) → s` | `EstadoWifi`; senha vazia = usar a conexão salva; com senha = cria (`wpa-psk`) e ativa. Normalmente volta `pending` (o NM negocia por segundos) e o `WifiChanged` confirma |
| `WifiDisconnect` | `() → s` | `EstadoWifi` relido |
| `GetWifiNetworks` | `(b rescan) → s` | (antigo, via `nmcli`) `{"redes": [...], "salvas": [...]}` |

Sinal: **`WifiChanged(s state_json)`** — `EstadoWifi`, a partir dos `PropertiesChanged` do NM (raiz e
`/Devices/*`), só quando o JSON mudou.

### 1.3 Bluetooth (BlueZ, barramento do sistema)

`EstadoBluetooth`:
```json
{"powered": true, "adapter": "/org/bluez/hci0",
 "devices": [{"addr": "00:88:24:48:14:87", "name": "H848", "connected": false, "paired": true,
              "trusted": true, "battery": null, "path": "/org/bluez/hci0/dev_00_88_24_48_14_87"}]}
```
`battery` = porcentagem (`org.bluez.Battery1`) ou `null`. Ordem: conectados, pareados, depois por nome.

| Método | Assinatura | Devolve |
|---|---|---|
| `GetBluetooth` | `() → s` | `EstadoBluetooth` |
| `SetBluetoothPowered` | `(b powered) → s` | `EstadoBluetooth` relido |
| `BluetoothConnect` | `(s addr) → s` | `EstadoBluetooth`; `Device1.Connect` bloqueia até o BlueZ terminar → costuma voltar `pending`; o `BluetoothChanged` traz o resultado |
| `BluetoothDisconnect` | `(s addr) → s` | `EstadoBluetooth` relido |
| `GetBluetoothDevices` | `() → s` | (antigo) `{"powered", "connected": [nomes], "paired": [...], "nearby": [...]}` |

Sinal: **`BluetoothChanged(s state_json)`** — `EstadoBluetooth`, a partir dos sinais do `org.bluez`
(`InterfacesAdded/Removed`, `PropertiesChanged`), só quando o JSON mudou.

### 1.4 Não perturbe

| Método | Assinatura | Devolve |
|---|---|---|
| `GetDnd` | `() → b` | modo `dnd` ligado? |
| `SetDnd` | `(b enabled) → b` | o estado que ficou |
| `SetMode` / `SetModes` / `Modes` | (antigos) | continuam valendo; mexer em `dnd` por eles também emite `DndChanged` |

Sinal: **`DndChanged(b enabled)`**. (O `NotificationEvent("modes", "[...]")` antigo continua sendo emitido.)

### 1.5 Exemplos

```sh
busctl --user call com.aero.Hermes /org/freedesktop/Notifications com.aero.Hermes ListSinks
busctl --user call com.aero.Hermes /org/freedesktop/Notifications com.aero.Hermes SetVolume uu 80 90
busctl --user call com.aero.Hermes /org/freedesktop/Notifications com.aero.Hermes WifiConnect ss "Casa" ""
gdbus monitor --session --dest com.aero.Hermes --object-path /org/freedesktop/Notifications
```

---

## 2. `com.aero.Sessiond`

### 2.1 Tema

Fonte da verdade: `~/.cache/hypr-tema/tema` (`escuro`/`claro`). `SetTheme` faz em Rust o que o
`hypr-tema` fazia: lê `~/.config/hypr/painel.conf` (e, no visual Solid, a paleta tonal do `solido.py`),
grava `~/.cache/hypr-tema/{tema,cores,destaque,cascata,solido-gtk.css}`, `~/.config/gtk-{3.0,4.0}/
{cores-tema.css,settings.ini}`, `~/.gtkrc-2.0`, `~/.config/waybar/cores-<tema>.css` (se a pasta existir),
aplica `gsettings color-scheme` + `gtk-theme` (com o "pisca" de nome do Solid) e avisa o Hermes
(`SetMode("claro", ...)`). Nenhum script `hypr-*` é chamado.

| Método | Assinatura | Devolve |
|---|---|---|
| `GetTheme` | `() → s` | `"dark"` ou `"light"` |
| `SetTheme` | `(s theme) → s` | o tema relido do arquivo (`"dark"`/`"light"`), `"error: ..."` ou `"pending"`. Aceita `dark|light|escuro|claro` |

Sinal: **`ThemeChanged(s theme)`** (`"dark"`/`"light"`), também quando a programação automática troca.

Programação claro de dia / escuro de noite (o `hypr-tema --loop`): `"tema_auto": true` em `sessao.json`;
os horários vêm de `TEMA_MODO`/`TEMA_CLARO`/`TEMA_ESCURO` do `painel.conf` (uma conferência por minuto).

### 2.2 Brilho

```json
{"internal": {"device": "amdgpu_bl1", "pct": 20, "raw": 13107, "max": 65535, "writable": false},
 "external": [{"display": 1, "name": "DELL G2725D", "pct": 40}]}
```
`internal.writable` diz se o sysfs aceita escrita direta; quando não aceita, o daemon grava pelo
`logind` (`org.freedesktop.login1.Session.SetBrightness`, permitido ao dono da sessão) e relê o sysfs.
Externos: `ddcutil` (`detect --brief` uma vez e guardado; `getvcp/setvcp 10` por monitor; `pct` = `null`
quando o monitor não respondeu).

| Método | Assinatura | Devolve |
|---|---|---|
| `GetBrightness` | `(b rescan) → s` | o JSON acima; `rescan=true` refaz o `ddcutil detect` (lento → pode voltar `pending`) |
| `SetBrightness` | `(s target, u pct) → s` | `target` = `"internal"`, `"all"` ou o número do monitor externo (`"1"`); devolve só o alvo relido: `{"internal": {...}}` / `{"external": [...]}` (+`error`) |

Sinal: **`BrightnessChanged(s state_json)`** — mesmo JSON da resposta do `SetBrightness` (o daemon não
vigia mudanças feitas por fora: o sysfs não tem inotify).

### 2.3 Modo de energia (`net.hadess.PowerProfiles`)

| Método | Assinatura | Devolve |
|---|---|---|
| `GetPowerProfile` | `() → s` | `power-saver` / `balanced` / `performance` (ou `error: ...`) |
| `ListPowerProfiles` | `() → as` | perfis que o serviço oferece |
| `SetPowerProfile` | `(s profile) → s` | o perfil relido do serviço (ou `error: ...`) |

Sinal: **`PowerProfileChanged(s profile)`** — do `PropertiesChanged` do serviço (vale também para trocas
feitas por `powerprofilesctl` ou pelo `bateria` do próprio daemon).

### 2.4 Ociosidade

Config em `~/.config/aero-shell/sessao.json` (vigiado por inotify; recarrega sozinho):
```json
{"tema_auto": false, "idle_nativo": false, "lock_s": 300, "dpms_s": 600, "suspend_s": 1800, "locker": ""}
```
`idle_nativo` e `tema_auto` aceitam `true/false` e `"sim"/"nao"`; os segundos aceitam número ou texto;
`0` desliga a ação. **Padrão: tudo inerte** (na sessão Hyprland o hypridle continua cuidando).

Dois caminhos para as mesmas ações:
- **`IdleEvent(kind)`**: o compositor chama quando o usuário parou/voltou.
- **`idle_nativo = sim`** + `WAYLAND_DISPLAY`: o daemon vira cliente `ext_idle_notifier_v1` do compositor
  (uma notificação por timer configurado) e executa sozinho.

Ações: `lock` → roda `locker` (`sh -c`); `dpms` → `{"cmd": "output-power", "on": "off"}` no socket do
Daimon (`DAIMON_SOCKET` / `$XDG_RUNTIME_DIR/daimon/$WAYLAND_DISPLAY.sock`), `resume` manda `"on"`;
`suspend` → `systemctl suspend`.

| Método | Assinatura | Devolve |
|---|---|---|
| `IdleEvent` | `(s kind) → s` | `kind` = `lock` / `dpms` / `suspend` / `resume`; texto do que foi feito (`"lock: started '...'"`, `"dpms off: {...}"`, `"error: ..."`) |
| `GetIdleConfig` | `() → s` | o `sessao.json` em uso |

### 2.5 Exemplos

```sh
busctl --user call com.aero.Sessiond /com/aero/Sessiond com.aero.Sessiond SetTheme s light
busctl --user call com.aero.Sessiond /com/aero/Sessiond com.aero.Sessiond SetBrightness su internal 40
busctl --user call com.aero.Sessiond /com/aero/Sessiond com.aero.Sessiond SetPowerProfile s power-saver
busctl --user call com.aero.Sessiond /com/aero/Sessiond com.aero.Sessiond IdleEvent s dpms
dbus-monitor --session "type='signal',interface='com.aero.Sessiond'"
```

---

## 3. Como testar sem mexer na sessão

```sh
# instância de teste do Hermes num barramento privado (system bus continua o real)
dbus-run-session -- sh -c 'hermesd --teste & sleep 1; busctl --user call com.aero.Hermes.Teste \
  /org/freedesktop/Notifications com.aero.Hermes ListSinks; kill $!'
# instância de teste do Sessiond com HOME falso (o SetTheme grava só lá)
HOME=/tmp/casa-teste dbus-run-session -- sh -c 'aero-sessiond --dbus-teste & sleep 1; busctl --user call \
  com.aero.Sessiond.Teste /com/aero/Sessiond com.aero.Sessiond SetTheme s light; kill $!'
```
