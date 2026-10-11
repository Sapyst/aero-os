#!/usr/bin/env bash
# Gera os quadros do efeito: {esq,dir}-{0..5}.frag. Força cai de 1.0 a 0.3 ao longo dos quadros.
cd "$(dirname "$0")"
forcas=(1.0 0.95 0.8 0.6 0.45 0.3)
for lado in esq:-1.0 dir:1.0; do
    for i in 0 1 2 3 4 5; do
        sed -e "s/@DIR@/${lado#*:}/" -e "s/@FORCA@/${forcas[$i]}/" -e "s/@SEMENTE@/$((i * 37 + 11)).0/" \
            glitch.frag.in > "${lado%%:*}-$i.frag"
    done
done
