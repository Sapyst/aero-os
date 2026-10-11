# painel.sh: lê ~/.config/hypr/painel.conf SEM executá-lo (biblioteca bash: ". painel.sh; ler_painel").
# Antes os scripts faziam ". painel.conf", ou seja, rodavam o arquivo como código. Como valores vêm de
# fora (o caminho do wallpaper, por exemplo), um nome de arquivo com $(...) virava comando.
# Aqui cada linha CHAVE=valor só vira uma variável; nada é avaliado.
ler_painel() {
    local arq="${1:-$HOME/.config/hypr/painel.conf}" k v
    [ -f "$arq" ] || return 0
    while IFS='=' read -r k v || [ -n "$k" ]; do
        [[ $k =~ ^[A-Z][A-Z0-9_]*$ ]] || continue                       # só CHAVES_ASSIM (ignora comentários)
        case "$k" in PATH|HOME|IFS|SHELL|USER|ENV|BASH*|LD_*|PS[0-9]|PROMPT_COMMAND|CDPATH|GLOBIGNORE) continue ;; esac
        v="${v%$'\r'}"
        if [[ $v == \"*\" && ${#v} -ge 2 ]]; then v="${v:1:${#v}-2}"
        elif [[ $v == \'*\' && ${#v} -ge 2 ]]; then v="${v:1:${#v}-2}"; fi
        printf -v "$k" '%s' "$v"
    done < "$arq"
}
