#!/usr/bin/env bash
# install.sh — instala a memória esperta do Aero OS: a parte do kernel (sysctl, MGLRU, zram,
# swap no SSD) e o vigia de memória aero-memory (serviço do sistema).
#
# Uso (de dentro do repositório):
#   bash system/memory/install.sh                        mostra os passos (dry-run; não precisa de root)
#   sudo bash system/memory/install.sh --apply           instala
#   sudo bash system/memory/install.sh --apply --swap-size 32    (4..64 GB; sem isso, mantém o
#                                        tamanho do swapfile que já existe, ou 16 GB)
#   sudo bash system/memory/install.sh --apply --no-swap         sem swapfile no SSD
#   sudo bash system/memory/install.sh --apply --no-zram         sem zram (sem o zram-generator):
#                                        não instala a config do zram, e vm.swappiness e
#                                        vm.page-cluster ficam no padrão do kernel
#   sudo bash system/memory/install.sh --uninstall               mostra o que desfaria
#   sudo bash system/memory/install.sh --uninstall --apply       desinstala
#   bash system/memory/install.sh --apply --destdir DIR  só copia os arquivos para DIR (pacotes/testes)
#
# Antes de trocar um arquivo que já existe e é diferente, guarda uma cópia ao lado:
# ARQUIVO.bak-aero-AAAAmmdd-HHMMSS (cópias de programas perdem o +x, para não rodarem por engano).
# Rodar de novo é seguro: o que já está igual fica como está.
# Códigos de saída: 0 ok (avisos podem aparecer), 1 um passo falhou, 2 uso errado, 3 precisa de root.
#
# Roda como root: PATH fixo e caminhos absolutos. Os arquivos do repositório só são copiados;
# o único executado é o aero-swap-setup (o instalado no --apply; o do repositório, só em --dry-run).
set -euo pipefail
export LC_ALL=C
PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 022

AQUI=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
LOCK=/run/aero-memory-install.lock
SERVICE=aero-memory.service
SWAP_SETUP=/usr/local/bin/aero-swap-setup
ZRAM_GENERATOR=/usr/lib/systemd/system-generators/zram-generator
ZRAM_CONF=/etc/systemd/zram-generator.conf
SYSCTL_CONF=/etc/sysctl.d/90-aero-memory.conf
TMPFILES_CONF=/etc/tmpfiles.d/90-aero-memory.conf
ZRAM_MAX_MIB=16384      # igual ao zram-size = min(ram / 2, 16384) do zram-generator.conf
AERO_MARK="Aero OS"     # texto que identifica arquivos nossos (para achar o original no desinstalar)

# origem (em system/memory/) | destino | modo
FILES=(
    "90-aero-memory.conf|$SYSCTL_CONF|644"
    "90-aero-memory.tmpfiles|$TMPFILES_CONF|644"
    "zram-generator.conf|$ZRAM_CONF|644"
    "aero-swap-setup|$SWAP_SETUP|755"
    "aero-memory|/usr/local/bin/aero-memory|755"
    "aero-memory-config|/usr/local/bin/aero-memory-config|755"
    "aero-memory.service|/etc/systemd/system/$SERVICE|644"
)
# arquivos do vigia (componente B): se mudarem, o serviço reinicia
DAEMON_FILES=(aero-memory aero-memory-config aero-memory.service)

say()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; WARNINGS=$(( WARNINGS + 1 )); }
err()  { printf 'error: %s\n' "$*" >&2; }
WARNINGS=0
FAILED=0

# ---------- funções puras (testadas em tests/memory/test_swap_setup_install.py) ----------

valid_size() {
    [[ ${1:-} =~ ^[1-9][0-9]?$ ]] && (( $1 >= 4 && $1 <= 64 ))
}

# nome da cópia de segurança: DESTINO.bak-aero-CARIMBO (com -N se já existir)
backup_name() {
    local dst=$1 stamp=$2 name n=1
    name="$dst.bak-aero-$stamp"
    while [[ -e $name || -L $name ]]; do
        name="$dst.bak-aero-$stamp-$n"; n=$(( n + 1 ))
    done
    printf '%s\n' "$name"
}

# estado de um destino: new | same | changed   (changed = conteúdo, modo ou dono diferentes)
file_state() {
    local src=$1 dst=$2 mode=$3 want_owner=${4:-0}
    if [[ ! -e $dst && ! -L $dst ]]; then echo new; return; fi
    if [[ -L $dst || ! -f $dst ]]; then echo changed; return; fi
    if cmp -s -- "$src" "$dst" && [[ $(stat -c '%a %u' -- "$dst") == "$mode $want_owner" ]]; then
        echo same
    else
        echo changed
    fi
}

# "chave valor" de cada linha de configuração de um arquivo sysctl.d
sysctl_settings() {
    awk -F= '
        /^[[:space:]]*[#;]/ || NF < 2 { next }
        { k = $1; v = substr($0, index($0, "=") + 1)
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", k); gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
          if (k != "") print k, v }' "$1"
}

# tamanho do zram que o zram-generator.conf pede, em bytes, para um MemTotal (kB)
# ram (MB) = kB/1024; min(ram/2, 16384) MB → bytes = min(kB*512, 16384 MiB)
zram_wanted_bytes() {
    local b=$(( $1 * 512 )) max=$(( ZRAM_MAX_MIB * 1048576 ))
    (( b > max )) && b=$max
    echo "$b"
}

# o que fazer com o zram: none | start | restart | reboot
# uso: zram_action ATIVO(0/1) USADO_KIB CONFERE(0/1)
zram_action() {
    local active=$1 used=$2 ok=$3
    if (( ! active )); then echo start
    elif (( ok )); then echo none
    elif (( used == 0 )); then echo restart
    else echo reboot; fi
}

# algoritmo escolhido em /sys/block/zramN/comp_algorithm ("lzo [zstd] lz4" → zstd)
zram_selected_algo() {
    [[ -r $1 ]] || return 0
    sed -n 's/.*\[\([^]]*\)\].*/\1/p' -- "$1" 2>/dev/null | head -n 1 || true
}

# o 90-aero-memory.conf sem as chaves pensadas para o zram (swappiness 150 e page-cluster 0 só
# fazem sentido com a swap em RAM; com só o SSD de swap, o kernel mandaria memória para o disco em
# vez de largar cache). Sai junto o bloco de comentário logo acima de cada uma. (--no-zram)
sysctl_without_zram_keys() {
    awk '
        /^[[:space:]]*#/ { buf = buf $0 "\n"; next }
        {
            k = $0; sub(/=.*/, "", k); gsub(/[[:space:]]/, "", k)
            if (k == "vm.swappiness" || k == "vm.page-cluster") { buf = ""; skip_blank = 1; next }
            if (NF == 0 && skip_blank) { skip_blank = 0; next }
            skip_blank = 0
            printf "%s", buf; buf = ""; print
        }
        END { printf "%s", buf }' "$1"
    printf '%s\n' "" "# install.sh --no-zram: sem zram, vm.swappiness e vm.page-cluster ficam no padrão do kernel."
}

# a cópia mais antiga de DESTINO que não é nossa (o arquivo de antes do Aero), ou nada
original_backup() {
    local dst=$1 f
    for f in "$dst".bak-aero-*; do
        [[ -f $f && ! -L $f ]] || continue
        grep -qF -- "$AERO_MARK" "$f" && continue
        printf '%s\n' "$f"; return 0
    done
    return 0
}

# ---------- passos ----------

APPLY=0 DESTDIR="" STAMP="" NO_ZRAM=0
CHANGED=()       # destinos que mudaram nesta execução
declare -A BACKUP_OF=()   # destino -> cópia de segurança feita agora ("new" = não existia)
INSTALL_TMP=""   # cópia temporária em andamento (apagada se o script parar no meio)
VARIANT_TMP=""   # o sysctl sem as chaves do zram (--no-zram), gerado na hora
KEEP_SWAP_SETUP=0

cleanup() {
    [[ -n $INSTALL_TMP && -e $INSTALL_TMP ]] && rm -f -- "$INSTALL_TMP"
    [[ -n $VARIANT_TMP && -e $VARIANT_TMP ]] && rm -f -- "$VARIANT_TMP"
    return 0
}

# imprime e executa (ou só imprime no dry-run). Uma falha é contada e a instalação continua:
# cada passo do sistema é independente, e o resumo no fim diz o que falhou.
LAST_RC=0
run() {
    LAST_RC=0
    if (( ! APPLY )); then
        printf '  would run: %s\n' "$*"
        return 0
    fi
    printf '  + %s\n' "$*"
    "$@" || LAST_RC=$?
    if (( LAST_RC != 0 )); then
        err "this step failed (exit $LAST_RC): $*"
        FAILED=$(( FAILED + 1 ))
    fi
    return 0
}

# roda o zram-generator no modo de teste (ZRAM_GENERATOR_ROOT: só escreve units numa pasta
# temporária, não cria dispositivos) para ter certeza de que a config gera o zram0
zram_config_ok() {
    local conf=$1 root rc=1
    root=$(mktemp -d) || return 1
    mkdir -p -- "$root/etc/systemd" "$root/proc" "$root/out" "$root/early" "$root/late"
    cp -- "$conf" "$root/etc/systemd/zram-generator.conf"
    cat /proc/meminfo > "$root/proc/meminfo"
    : > "$root/proc/cmdline"
    if ZRAM_GENERATOR_ROOT=$root timeout 10 "$ZRAM_GENERATOR" "$root/out" "$root/early" "$root/late" >/dev/null 2>&1 \
        && [[ -f $root/out/dev-zram0.swap ]]; then
        rc=0
    fi
    rm -rf -- "$root"
    return "$rc"
}

# confere os arquivos do repositório antes de mexer em qualquer coisa
preflight() {
    local entry src dst mode bad=0 first
    for entry in "${FILES[@]}"; do
        IFS='|' read -r src dst mode <<< "$entry"
        src="$AQUI/$src"
        if [[ -L $src || ! -f $src || ! -s $src ]]; then
            err "missing or not a regular file in the repo: system/memory/${src##*/}"
            bad=1; continue
        fi
        first=$(head -n 1 -- "$src")
        case $first in
            '#!'*bash*)
                bash -n -- "$src" || { err "shell syntax error in ${src##*/}"; bad=1; } ;;
            '#!'*python3*)
                if [[ -x /usr/bin/python3 ]]; then
                    /usr/bin/python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1], "rb").read(), sys.argv[1])' "$src" \
                        || { err "python syntax error in ${src##*/}"; bad=1; }
                fi ;;
        esac
    done
    if [[ ! -x /usr/bin/python3 ]]; then
        err "python3 is not installed (aero-memory needs it): pacman -S python"; bad=1
    fi
    if (( NO_ZRAM )); then
        :
    elif [[ -x $ZRAM_GENERATOR ]]; then
        # testa a config do repositório ANTES de instalar: uma que o zram-generator recusa deixaria
        # o próximo boot sem zram nenhum
        if [[ -f $AQUI/zram-generator.conf && ! -L $AQUI/zram-generator.conf ]] && ! zram_config_ok "$AQUI/zram-generator.conf"; then
            err "zram-generator can't use system/memory/zram-generator.conf (its test run failed)"; bad=1
        fi
    elif [[ -z $DESTDIR ]]; then
        # sem zram, swappiness 150 + page-cluster 0 mandariam memória para o SSD em vez de largar cache
        err "zram-generator is not installed (pacman -S zram-generator), so there would be no compressed RAM swap and these settings would push memory to the SSD. Install it, or run with --no-zram."
        bad=1
    fi
    return "$bad"
}

# 0 se DESTINO é igual ao que o Aero instala (o arquivo do repositório ou, no sysctl, a variante
# --no-zram)
same_as_ours() {
    local src=$1 dst=$2 plain=$3
    cmp -s -- "$src" "$dst" && return 0
    [[ $plain == "$SYSCTL_CONF" ]] && cmp -s -- "$dst" <(sysctl_without_zram_keys "$src")
}

# desfaz a troca de um arquivo feita nesta execução: volta a cópia de segurança, ou apaga o que
# não existia. 1 se não havia nada a desfazer (o arquivo já estava assim antes)
undo_install() {
    local dst=$1 b=${BACKUP_OF[$1]:-}
    if [[ $b == new ]]; then
        rm -f -- "$dst"
        say "  removed the new $dst again"
    elif [[ -n $b && -f $b && ! -L $b ]]; then
        mv -fT -- "$b" "$dst"
        say "  put the old $dst back (from ${b##*/})"
    else
        return 1
    fi
}

install_one() {
    local src=$1 dst=$2 mode=$3 dir tmp backup state owner=0
    (( EUID == 0 )) || owner=$(id -u)
    state=$(file_state "$src" "$dst" "$mode" "$owner")
    case $state in
        same) say "  unchanged  $dst"; return 0 ;;
        new) say "  new        $dst" ;;
        changed) say "  update     $dst (old one kept as .bak-aero-$STAMP)" ;;
    esac
    CHANGED+=("$dst")
    (( APPLY )) || return 0
    [[ $state == new ]] && BACKUP_OF[$dst]=new
    dir=$(dirname -- "$dst")
    [[ -d $dir ]] || install -d -m 755 -- "$dir"
    # cópia temporária na mesma pasta + mv = troca atômica (quem lê nunca vê um arquivo pela metade;
    # um programa em execução continua com o antigo)
    tmp=$(mktemp -- "$dir/.aero-install.XXXXXX")
    INSTALL_TMP=$tmp
    install -m "$mode" -- "$src" "$tmp"
    (( EUID == 0 )) && chown root:root -- "$tmp"
    if [[ $state == changed ]]; then
        backup=$(backup_name "$dst" "$STAMP")
        cp -a -- "$dst" "$backup"
        [[ $mode == 755 ]] && chmod a-x -- "$backup"
        BACKUP_OF[$dst]=$backup
    fi
    mv -fT -- "$tmp" "$dst"
    INSTALL_TMP=""
}

install_files() {
    local entry src dst mode from
    say "==> Files"
    if (( NO_ZRAM )) && [[ -f $AQUI/90-aero-memory.conf && ! -L $AQUI/90-aero-memory.conf ]]; then
        VARIANT_TMP=$(mktemp)
        sysctl_without_zram_keys "$AQUI/90-aero-memory.conf" > "$VARIANT_TMP"
    fi
    for entry in "${FILES[@]}"; do
        IFS='|' read -r src dst mode <<< "$entry"
        if [[ -L $AQUI/$src || ! -f $AQUI/$src ]]; then
            say "  missing    $DESTDIR$dst (system/memory/$src is not in the repo)"
            continue
        fi
        from="$AQUI/$src"
        if (( NO_ZRAM )); then
            if [[ $dst == "$ZRAM_CONF" ]]; then
                say "  skipped    $DESTDIR$dst (--no-zram)"
                continue
            fi
            [[ $dst == "$SYSCTL_CONF" && -n $VARIANT_TMP ]] && from=$VARIANT_TMP
        fi
        install_one "$from" "$DESTDIR$dst" "$mode"
    done
    if [[ -n $VARIANT_TMP ]]; then
        rm -f -- "$VARIANT_TMP"
        VARIANT_TMP=""
    fi
    if [[ ! -d $DESTDIR/etc/aero ]]; then
        say "  new        $DESTDIR/etc/aero/ (settings of the memory guard)"
        (( APPLY )) && install -d -m 755 -- "$DESTDIR/etc/aero"
    fi
    return 0
}

daemon_changed() {
    local f d
    for f in "${DAEMON_FILES[@]}"; do
        for d in "${CHANGED[@]}"; do
            [[ ${d##*/} == "$f" ]] && return 0
        done
    done
    return 1
}

step_sysctl() {
    say "==> Kernel memory settings (sysctl)"
    run sysctl -q --system
    (( APPLY )) || return 0
    local key want have f later
    while read -r key want; do
        have=$(sysctl -n -- "$key" 2>/dev/null || true)
        if [[ $have != "$want" ]]; then
            later=""
            for f in /etc/sysctl.d/*.conf /run/sysctl.d/*.conf /usr/local/lib/sysctl.d/*.conf /usr/lib/sysctl.d/*.conf /etc/sysctl.conf; do
                [[ -f $f && $f != "$SYSCTL_CONF" ]] || continue
                # só quem é lido depois do nosso pode vencer (o /etc/sysctl.conf é sempre o último)
                [[ $f == /etc/sysctl.conf || ${f##*/} > ${SYSCTL_CONF##*/} ]] || continue
                if sysctl_settings "$f" | awk -v k="$key" '$1 == k { found = 1 } END { exit !found }'; then
                    later+=" $f"
                fi
            done
            warn "$key is $have, not $want.${later:+ Also set in:$later (a file sorted after 90-aero-memory.conf wins)}"
        fi
    done < <(sysctl_settings "$SYSCTL_CONF")
}

step_tmpfiles() {
    say "==> MGLRU thrashing protection (min_ttl_ms)"
    run systemd-tmpfiles --create -- "$TMPFILES_CONF"
    if (( APPLY )) && [[ -r /sys/kernel/mm/lru_gen/min_ttl_ms ]]; then
        say "  min_ttl_ms is now $(cat /sys/kernel/mm/lru_gen/min_ttl_ms)"
    elif [[ ! -e /sys/kernel/mm/lru_gen ]]; then
        say "  this kernel has no MGLRU; the setting is skipped"
    fi
}

step_zram() {
    say "==> Compressed RAM swap (zram)"
    if (( NO_ZRAM )); then
        say "  skipped (--no-zram)"; return 0
    fi
    if [[ ! -x $ZRAM_GENERATOR ]]; then
        say "  skipped: zram-generator is not installed"; return 0
    fi
    if tr ' ' '\n' < /proc/cmdline | grep -qE '^systemd\.zram=(0|no|false|off)$'; then
        warn "the kernel command line has systemd.zram=0, so no zram device is made."
    fi
    local f
    for f in /etc/systemd/zram-generator.conf.d/*.conf /run/systemd/zram-generator.conf.d/*.conf \
             /usr/local/lib/systemd/zram-generator.conf.d/*.conf /usr/lib/systemd/zram-generator.conf.d/*.conf; do
        [[ -f $f ]] && warn "$f overrides $ZRAM_CONF (files in zram-generator.conf.d win)."
    done
    local memtotal want have=0 algo="" active=0 used=0 prio="" ok=0
    memtotal=$(awk '$1 == "MemTotal:" { print $2; exit }' /proc/meminfo)
    want=$(zram_wanted_bytes "${memtotal:-0}")
    if read -r _ _ _ used prio < <(awk '$1 == "/dev/zram0" { print; exit }' /proc/swaps); then
        active=1
        have=$(cat /sys/block/zram0/disksize 2>/dev/null || echo 0)
        algo=$(zram_selected_algo /sys/block/zram0/comp_algorithm)
    fi
    local diff=$(( have - want ))
    (( diff < 0 )) && diff=$(( -diff ))
    (( active && diff <= 1048576 )) && [[ $algo == zstd && $prio == 100 ]] && ok=1
    (( active )) && say "  now: $(( have / 1048576 )) MB, ${algo:-?}, priority ${prio:-?}, $(( used / 1024 )) MB used; wanted: $(( want / 1048576 )) MB, zstd, priority 100"
    # testa a config que vai valer (no dry-run, a do repositório; o preflight já testou essa)
    local conf="$ZRAM_CONF"
    (( APPLY )) || conf="$AQUI/zram-generator.conf"
    if zram_config_ok "$conf"; then
        say "  zram-generator test run with this config: ok"
    elif (( ! APPLY )); then
        warn "zram-generator could not use $conf in a test run."
        return 0
    elif undo_install "$ZRAM_CONF"; then
        # a config nova não pode ficar para o próximo boot (seria um boot sem zram)
        warn "zram-generator could not use the new $ZRAM_CONF in a test run, so the change was undone (see above); the compressed RAM swap stays as it was."
        return 0
    else
        warn "zram-generator could not use $ZRAM_CONF in a test run (it was already installed before this run, so it was left in place). Check it before the next reboot, or run install.sh --uninstall."
        return 0
    fi
    run systemctl daemon-reload
    case $(zram_action "$active" "$used" "$ok") in
        none) say "  already right" ;;
        start|restart)
            # restart recria o zram0 com o tamanho novo (só com ele vazio: nada a mover de volta)
            run systemctl restart systemd-zram-setup@zram0.service
            run systemctl start dev-zram0.swap
            if (( APPLY )); then
                if awk '$1 == "/dev/zram0" { found = 1 } END { exit !found }' /proc/swaps; then
                    say "  on: $(( $(cat /sys/block/zram0/disksize 2>/dev/null || echo 0) / 1048576 )) MB"
                else
                    warn "the compressed RAM swap is not on. See: journalctl -u systemd-zram-setup@zram0 -n 30"
                fi
            fi ;;
        reboot)
            say "  the compressed RAM swap is in use now ($(( used / 1024 )) MB); the new size applies after a reboot" ;;
    esac
}

# sem --swap-size, o aero-swap-setup mantém o tamanho de um swapfile que já existe (padrão 16 GB)
step_swap() {
    local size=$1 size_set=$2 rc=0 size_args=()
    (( size_set )) && size_args=(--size "$size")
    say "==> SSD swap tier"
    if (( APPLY )); then
        printf '  + %s\n' "$SWAP_SETUP --apply ${size_args[*]}"
        "$SWAP_SETUP" --apply "${size_args[@]}" || rc=$?
    else
        # no dry-run mostra o plano do próprio aero-swap-setup (do repositório; não precisa de root)
        bash "$AQUI/aero-swap-setup" --dry-run "${size_args[@]}" 2>&1 | sed -e '/^To do it:/d' -e 's/^/    /' || rc=$?
    fi
    case $rc in
        0) ;;
        4|5|6) warn "the SSD swap was not set up (aero-swap-setup exit $rc, see above). Everything else is installed; zram still works." ;;
        *) err "aero-swap-setup failed (exit $rc). Everything else is installed."; FAILED=$(( FAILED + 1 )) ;;
    esac
}

step_service() {
    say "==> Memory guard service ($SERVICE)"
    run systemctl daemon-reload
    run systemctl enable "$SERVICE"
    if (( APPLY )) && systemctl is-active -q "$SERVICE"; then
        if daemon_changed; then run systemctl restart "$SERVICE"; else say "  already running"; fi
    else
        run systemctl start "$SERVICE"
    fi
    (( APPLY )) || return 0
    local i
    for i in 1 2 3 4 5; do
        systemctl is-active -q "$SERVICE" && { say "  running"; return 0; }
        sleep 1
    done
    warn "$SERVICE is not running. See: journalctl -u $SERVICE -n 30"
}

print_next_steps() {
    say ""
    say "Next, as your normal user (not root), the ComfyUI helper and the Settings page:"
    say "  install -Dm755 bin/aero-memory-agent ~/.local/bin/aero-memory-agent"
    say "  install -Dm644 config/systemd/user/aero-memory-agent.service ~/.config/systemd/user/aero-memory-agent.service"
    say "  systemctl --user daemon-reload && systemctl --user enable --now aero-memory-agent.service"
    say "Settings → System → Memory comes with the Settings app (settings/sec_memoria.py)."
}

zram_is_on() {
    awk '$1 ~ /^\/dev\/zram[0-9]+$/ { found = 1 } END { exit !found }' /proc/swaps
}

do_install() {
    local size=$1 no_swap=$2 size_set=$3
    install_files
    if [[ -n $DESTDIR ]]; then
        say "==> System steps skipped (--destdir)"
        return 0
    fi
    say ""
    step_sysctl
    step_tmpfiles
    step_zram
    if (( no_swap )); then
        say "==> SSD swap tier"; say "  skipped (--no-swap)"
    elif (( APPLY && ! NO_ZRAM )) && ! zram_is_on; then
        # com swappiness 150 e sem zram, o SSD seria a única swap e receberia tudo
        say "==> SSD swap tier"
        warn "the SSD swap was not set up: the compressed RAM swap (zram) is not on, so the SSD would be the only swap. Fix zram first (journalctl -u systemd-zram-setup@zram0), then run this again."
    else
        step_swap "$size" "$size_set"
    fi
    step_service
    print_next_steps
}

do_uninstall() {
    local entry src dst mode orig
    say "==> Memory guard service"
    if [[ -n $DESTDIR ]]; then
        say "  skipped (--destdir)"
    elif [[ -e /etc/systemd/system/$SERVICE ]]; then
        # parar o serviço também solta (SIGCONT) qualquer processo que ele tenha pausado
        run systemctl disable --now "$SERVICE"
    else
        say "  not installed"
    fi
    say "==> SSD swap tier"
    if [[ -z $DESTDIR && -x $SWAP_SETUP ]]; then
        local rc=0
        if (( APPLY )); then
            printf '  + %s\n' "$SWAP_SETUP --remove --apply"
            "$SWAP_SETUP" --remove --apply || rc=$?
        else
            bash "$AQUI/aero-swap-setup" --remove --dry-run 2>&1 | sed -e '/^To do it:/d' -e 's/^/    /' || rc=$?
        fi
        # sem conseguir tirar a swap do SSD, o aero-swap-setup fica instalado para tentar depois
        (( rc == 0 )) || KEEP_SWAP_SETUP=1
        case $rc in
            0) ;;
            4|6) warn "the SSD swap was not removed (exit $rc, see above). Later: sudo $SWAP_SETUP --remove --apply" ;;
            *) err "removing the SSD swap failed (exit $rc). Later: sudo $SWAP_SETUP --remove --apply"; FAILED=$(( FAILED + 1 )) ;;
        esac
    else
        say "  skipped"
    fi
    say "==> Files"
    for entry in "${FILES[@]}"; do
        IFS='|' read -r src dst mode <<< "$entry"
        dst="$DESTDIR$dst"
        [[ -e $dst || -L $dst ]] || continue
        if (( KEEP_SWAP_SETUP )) && [[ $dst == "$DESTDIR$SWAP_SETUP" ]]; then
            say "  kept       $dst (the SSD swap is still set up)"
            continue
        fi
        orig=""
        if [[ $dst == "$DESTDIR$ZRAM_CONF" ]]; then
            # o único destino com nome que não é só nosso: só mexe se for o arquivo do Aero
            if [[ -L $dst ]] || ! grep -qF -- "$AERO_MARK" "$dst"; then
                say "  kept       $dst (not Aero's)"
                continue
            fi
            orig=$(original_backup "$dst")
        fi
        if [[ -n $orig ]]; then
            # o do Aero foi editado depois de instalado: a edição vira cópia antes do original voltar
            if ! cmp -s -- "$AQUI/$src" "$dst"; then
                local edited
                edited=$(backup_name "$dst" "$STAMP")
                say "  move aside $dst → ${edited##*/} (it was edited)"
                (( APPLY )) && mv -fT -- "$dst" "$edited"
            fi
            say "  restore    $dst (from ${orig##*/})"
            (( APPLY )) && mv -fT -- "$orig" "$dst"
        elif [[ -f $dst && ! -L $dst ]] && same_as_ours "$AQUI/$src" "$dst" "${dst#"$DESTDIR"}"; then
            say "  remove     $dst"
            (( APPLY )) && rm -f -- "$dst"
        else
            # diferente do repositório (alguém editou): vira cópia de segurança em vez de sumir
            local backup
            backup=$(backup_name "$dst" "$STAMP")
            say "  move aside $dst → ${backup##*/} (it was edited)"
            if (( APPLY )); then
                mv -fT -- "$dst" "$backup"
                [[ $mode == 755 ]] && chmod a-x -- "$backup"
            fi
        fi
    done
    say "  kept       $DESTDIR/etc/aero/ (your settings) and the .bak-aero-* copies"
    if [[ -z $DESTDIR ]]; then
        run systemctl daemon-reload
        say ""
        say "Reboot to put the kernel memory settings and the zram size back to their defaults."
    fi
}

usage() {
    cat <<'EOF'
Usage: install.sh [--dry-run | --apply] [--swap-size N | --no-swap] [--no-zram] [--destdir DIR]
       install.sh --uninstall [--dry-run | --apply] [--destdir DIR]

Installs Aero's smart memory: kernel memory settings, MGLRU thrashing protection,
compressed RAM swap (zram), a low-priority SSD swap file and the aero-memory guard service.
Without --apply it only shows the steps.

  --apply        do it (needs root, unless --destdir is given)
  --dry-run      only show the steps (the default)
  --swap-size N  SSD swap file size in GB, 4 to 64 (default: keep the current one, or 16)
  --no-swap      don't make the SSD swap file
  --no-zram      set things up without compressed RAM swap (no zram-generator): its
                 config isn't installed, and vm.swappiness/vm.page-cluster stay at the
                 kernel defaults (they are tuned for zram)
  --uninstall    undo the install (keeps /etc/aero and the .bak-aero-* copies)
  --destdir DIR  only copy the files under DIR (for packaging and tests; no system changes)

Exit codes: 0 done (warnings may be printed), 1 a step failed, 2 bad usage, 3 needs root.
EOF
}

main() {
    local mode=install dry=0 size=16 no_swap=0 size_set=0
    while (( $# )); do
        case $1 in
            --apply) APPLY=1 ;;
            --dry-run) dry=1 ;;
            --uninstall) mode=uninstall ;;
            --no-swap) no_swap=1 ;;
            --no-zram) NO_ZRAM=1 ;;
            --swap-size)
                (( $# >= 2 )) || { err "--swap-size needs a number"; exit 2; }
                size=$2; size_set=1; shift ;;
            --swap-size=*) size=${1#--swap-size=}; size_set=1 ;;
            --destdir)
                (( $# >= 2 )) || { err "--destdir needs a folder"; exit 2; }
                DESTDIR=$2; shift ;;
            -h|--help) usage; exit 0 ;;
            *) err "unknown option: $(printf '%q' "$1") (see --help)"; exit 2 ;;
        esac
        shift
    done
    (( dry )) && APPLY=0
    valid_size "$size" || { err "--swap-size must be a whole number of GB from 4 to 64"; exit 2; }
    if (( size_set && no_swap )); then err "--swap-size and --no-swap don't go together"; exit 2; fi
    if [[ -n $DESTDIR ]]; then
        [[ $DESTDIR == /* && -d $DESTDIR && ! -L $DESTDIR ]] || { err "--destdir must be an existing folder, given as an absolute path"; exit 2; }
        DESTDIR=${DESTDIR%/}
        [[ -n $DESTDIR ]] || { err "--destdir can't be /"; exit 2; }
    fi
    if (( APPLY && EUID != 0 )) && [[ -z $DESTDIR ]]; then
        err "--apply changes the system and needs root. Run: sudo bash system/memory/install.sh $( [[ $mode == uninstall ]] && echo '--uninstall ' )--apply"
        exit 3
    fi
    if (( APPLY )) && [[ -z $DESTDIR ]]; then
        [[ -d /run/systemd/system ]] || { err "systemd is not running here; use --destdir to only copy the files"; exit 1; }
        exec 9>>"$LOCK"
        flock -n 9 || { err "another install.sh is running"; exit 1; }
    fi
    trap cleanup EXIT
    STAMP=$(date +%Y%m%d-%H%M%S)

    if (( APPLY )); then say "Aero smart memory: $mode"; else say "Aero smart memory: $mode (dry run: nothing will change)"; fi
    local ready=1
    if [[ $mode == install ]]; then
        if ! preflight; then
            ready=0
            if (( APPLY )); then
                err "nothing was changed; fix the problems above first"
                exit 1
            fi
        fi
        do_install "$size" "$no_swap" "$size_set"
    else
        do_uninstall
    fi
    say ""
    if (( ! ready )); then
        err "this is not ready to install (see the errors above)"
        exit 1
    fi
    if (( ! APPLY )); then
        say "To do it: sudo bash system/memory/install.sh $( [[ $mode == uninstall ]] && echo '--uninstall ' )--apply$( (( NO_ZRAM )) && [[ $mode == install ]] && echo ' --no-zram' )"
    elif (( FAILED )); then
        err "finished, but $FAILED step(s) failed (see above)"
        exit 1
    elif (( WARNINGS )); then
        say "Done, with $WARNINGS warning(s) above."
    else
        say "Done."
    fi
}

# "source install.sh" (os testes) só carrega as funções
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
