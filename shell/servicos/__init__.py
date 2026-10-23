"""
servicos/ — a camada SERVICE do Aero: a lógica sem tela (hardware, Hyprland, arquivos).

Convenção (a mesma do Zenith Shell, adaptada): cada função do sistema tem três partes.

  Service  servicos/<x>.py        lê e muda o sistema. Sem GTK, sem janela: dá pra testar no terminal
                                  (python3 -m servicos.<x>) e qualquer parte do shell pode importar.
  State    servicos/estado.py     as escolhas do usuário, um JSON por função em ~/.config/aero-shell/<x>.json.
                                  (configs antigas continuam no painel.conf, lido pelo painel.sh / comum.py)
  Page     aero-settings/sec_<x>.py  a tela no Settings. Só mostra e grava o State; quem age é o Service.

Cada módulo começa com um bloco "API" listando o que ele oferece. Mapa completo: ARQUITETURA.md.
"""
