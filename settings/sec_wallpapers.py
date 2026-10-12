"""
sec_wallpapers.py — seção Wallpapers: navegar em repositórios GitHub de imagens e usar como papel de parede.

Como funciona:
  - Cada "fonte" é um repositório GitHub (padrão: dharmx/walls). Dá pra colar outros links:
      https://github.com/dono/repo
      https://github.com/dono/repo/tree/branch/pasta/opcional
      dono/repo
  - A lista de arquivos vem de UMA chamada da API (git/trees?recursive=1) e fica em cache em
    ~/.cache/aero-shell/wallpapers/ (a API sem login tem limite de 60 chamadas por hora).
  - Esquerda: árvore de pastas (a 1ª camada são as "categorias"). Direita: miniaturas da pasta
    escolhida (inclui subpastas) + busca por nome.
  - Miniaturas: baixa a imagem, reduz com GdkPixbuf e guarda só a miniatura no cache.
  - Clicar numa miniatura chama `papel-de-parede --url URL`, o mesmo comando do resto do sistema
    (guarda em ~/Pictures/Wallpapers, troca o wallpaper, refaz as cores se estiver ligado). Doc 21.
  - "Apply to" (doc 57): a área em que você está (padrão), todas, ou uma área escolhida. Aberto pelo
    Workspaces → Choose…, já vem na área que você estava editando. "From file…" usa uma imagem do disco.
"""
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: E402

from comum import Secao, botao, confirmar, em_segundo_plano, pagina, rodar  # noqa: E402

CONFIG = os.path.expanduser("~/.config/aero-settings/wallpaper-repos.json")
CACHE = os.path.expanduser("~/.cache/aero-shell/wallpapers")
MINIATURAS = os.path.join(CACHE, "thumbs-hd")
PADRAO = {"owner": "dharmx", "repo": "walls", "branch": "main", "sub": ""}
EXTENSOES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
POR_PAGINA = 48
# Tamanho mínimo do arquivo (bytes): imagem pequena em disco quase sempre é de baixa resolução
TAMANHOS_MIN = [("Any size", 0), ("> 300 KB", 300_000), ("> 1 MB", 1_000_000), ("> 2 MB", 2_000_000)]
LARGURA_BOA = 1920      # abaixo disso a imagem aparece com aviso (a tela é 2560x1440)
LIMITE_BYTES = 40 * 1024 * 1024      # não baixa nada maior que isso
CABECALHO = {"User-Agent": "aero-settings-wallpapers"}

piscina = ThreadPoolExecutor(max_workers=4)   # baixa miniaturas em paralelo, sem travar a janela


# ---------- fontes (repositórios) ----------

def ler_fontes():
    try:
        with open(CONFIG) as f:
            fontes = json.load(f)
        if fontes:
            return fontes
    except (FileNotFoundError, ValueError):
        pass
    return [dict(PADRAO)]


def gravar_fontes(fontes):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(fontes, f, indent=2)


def nome_fonte(f):
    return f"{f['owner']}/{f['repo']}" + (f"/{f['sub']}" if f.get("sub") else "")


def chave_fonte(f):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{nome_fonte(f)}@{f['branch']}")


def interpretar(texto):
    """Link do GitHub (ou 'dono/repo') -> dict da fonte. Levanta ValueError se não entender."""
    t = texto.strip().removesuffix(".git").rstrip("/")
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)(?:/(?:tree|blob)/([^/]+)(?:/(.*))?)?$", t)
    if not m:
        m = re.match(r"^([\w.-]+)/([\w.-]+)()()$", t)
    if not m:
        raise ValueError("Paste a GitHub link like https://github.com/owner/repo")
    owner, repo, branch, sub = m.group(1), m.group(2), m.group(3) or "", m.group(4) or ""
    if not branch:
        dados = api(f"https://api.github.com/repos/{owner}/{repo}")
        branch = dados["default_branch"]
    return {"owner": owner, "repo": repo, "branch": branch, "sub": sub.strip("/")}


def api(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=CABECALHO), timeout=30) as r:
        return json.load(r)


def carregar_arquivos(f, forcar=False):
    """Lista de caminhos de imagens do repositório (com cache). Devolve (caminhos, truncado)."""
    os.makedirs(CACHE, exist_ok=True)
    arq = os.path.join(CACHE, chave_fonte(f) + ".json")
    if forcar or not os.path.exists(arq):
        dados = api(f"https://api.github.com/repos/{f['owner']}/{f['repo']}/git/trees/"
                    f"{urllib.parse.quote(f['branch'])}?recursive=1")
        tamanhos = {t["path"]: t.get("size", 0) for t in dados["tree"]
                    if t["type"] == "blob" and t["path"].lower().endswith(EXTENSOES)}
        with open(arq, "w") as out:
            json.dump({"sizes": tamanhos, "truncated": dados.get("truncated", False)}, out)
    with open(arq) as inp:
        dados = json.load(inp)
    if "sizes" not in dados:                       # cache antigo, sem tamanhos: baixa de novo
        return carregar_arquivos(f, forcar=True)
    tamanhos = dados["sizes"]
    if f.get("sub"):
        pref = f["sub"] + "/"
        tamanhos = {c[len(pref):]: n for c, n in tamanhos.items() if c.startswith(pref)}
    return list(tamanhos), dados.get("truncated", False), tamanhos


def url_raw(f, caminho):
    completo = (f["sub"] + "/" if f.get("sub") else "") + caminho
    return (f"https://raw.githubusercontent.com/{f['owner']}/{f['repo']}/"
            f"{urllib.parse.quote(f['branch'])}/{urllib.parse.quote(completo)}")


def miniatura(url):
    """Caminho da miniatura em cache (baixa e reduz se ainda não existir). Roda em thread."""
    os.makedirs(MINIATURAS, exist_ok=True)
    destino = os.path.join(MINIATURAS, hashlib.sha1(url.encode()).hexdigest() + ".jpg")
    if os.path.exists(destino) and os.path.exists(destino + ".dim"):
        with open(destino + ".dim") as d:
            return (destino, *json.load(d))
    tmp = destino + f".{os.getpid()}.{id(url)}.tmp"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=CABECALHO), timeout=30) as r, \
                open(tmp, "wb") as out:
            total = 0
            while bloco := r.read(65536):
                total += len(bloco)
                if total > LIMITE_BYTES:
                    raise ValueError("file too large")
                out.write(bloco)
        _info, largura, altura = GdkPixbuf.Pixbuf.get_file_info(tmp)   # tamanho REAL da imagem original
        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(tmp, 800, 500, True)
        pix.savev(destino, "jpeg", ["quality"], ["92"])
        with open(destino + ".dim", "w") as d:
            json.dump([largura, altura], d)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return destino, largura, altura


# ---------- árvore de pastas ----------

class Nodo(GObject.Object):
    def __init__(self, nome, caminho):
        super().__init__()
        self.nome = nome
        self.caminho = caminho        # "" = raiz
        self.total = 0                # imagens aqui dentro (contando subpastas)
        self.filhos = Gio.ListStore.new(Nodo)


def montar_arvore(caminhos, nome_raiz):
    raiz = Nodo(nome_raiz, "")
    pastas = {"": raiz}
    for c in sorted(caminhos, key=str.lower):
        partes = c.split("/")[:-1]
        atual = ""
        raiz.total += 1
        for p in partes:
            pai = pastas[atual]
            atual = f"{atual}/{p}" if atual else p
            if atual not in pastas:
                pastas[atual] = Nodo(p, atual)
                pai.filhos.append(pastas[atual])
            pastas[atual].total += 1
    return raiz


# ---------- a seção ----------

class SecaoWallpapers(Secao):
    id = "wallpapers"
    titulo = "Wallpapers"
    icone = "preferences-desktop-wallpaper-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.fontes = ler_fontes()
        self.fonte = self.fontes[0]
        self.arquivos = []
        self.tamanhos = {}
        self.pasta = ""
        self.busca = ""
        self.mostrando = 0
        self.geracao = 0              # muda a cada redesenho: miniaturas atrasadas se descartam
        self.carregou = False

        # --- barra de cima: fonte + busca ---
        self.lista_fontes = Gtk.StringList()
        self.seletor = Gtk.DropDown(model=self.lista_fontes, hexpand=True)
        self.seletor.connect("notify::selected", self.ao_trocar_fonte)
        self.busca_entrada = Gtk.SearchEntry(placeholder_text="Search by name", hexpand=True)
        self.busca_entrada.connect("search-changed", self.ao_buscar)
        topo = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        topo.append(Gtk.Image.new_from_icon_name("folder-remote-symbolic"))
        topo.append(self.seletor)
        topo.append(self.busca_entrada)
        self.minimo = Gtk.DropDown(model=Gtk.StringList.new([t for t, _ in TAMANHOS_MIN]),
                                   tooltip_text="Hide small files (they are usually low resolution)")
        self.minimo.connect("notify::selected", lambda *_: self.desenhar())
        topo.append(self.minimo)
        # para onde vai o wallpaper escolhido (papel-de-parede --area N / --all)
        self.alvo = Gtk.DropDown(model=Gtk.StringList.new(["This workspace", "All workspaces"] +
                                                          [f"Workspace {n}" for n in range(1, 6)]),
                                 tooltip_text="Where the wallpaper you pick goes")
        self.alvo_fixo = False
        topo.append(Gtk.Label(label="Apply to"))
        topo.append(self.alvo)
        do_disco = Gtk.Button(label="From file…", tooltip_text="Use an image from your computer")
        do_disco.connect("clicked", lambda *_: self.escolher_arquivo())
        topo.append(do_disco)

        # --- esquerda: árvore ---
        self.selecao = Gtk.SingleSelection()
        fabrica = Gtk.SignalListItemFactory()
        fabrica.connect("setup", self.criar_linha)
        fabrica.connect("bind", self.preencher_linha)
        self.arvore = Gtk.ListView(model=self.selecao, factory=fabrica)
        self.arvore.add_css_class("navigation-sidebar")
        self.selecao.connect("selection-changed", self.ao_escolher_pasta)
        esquerda = Gtk.ScrolledWindow(child=self.arvore, width_request=190, hscrollbar_policy=Gtk.PolicyType.NEVER)

        # --- direita: grade ---
        self.grade = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                 min_children_per_line=2, max_children_per_line=6,
                                 row_spacing=10, column_spacing=10, valign=Gtk.Align.START,
                                 margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)
        self.grade.connect("child-activated", self.ao_clicar_imagem)
        self.mais = botao("Show more", self.mostrar_mais, "pill", "view-more-symbolic")
        self.mais.set_halign(Gtk.Align.CENTER)
        self.mais.set_margin_bottom(12)
        self.estado = Adw.StatusPage(icon_name="image-x-generic-symbolic", title="Loading…", vexpand=True)
        self.contagem = Gtk.Label(xalign=0, margin_start=12, margin_top=8, margin_bottom=2)
        self.contagem.add_css_class("dim-label")
        corpo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        corpo.append(self.contagem)
        corpo.append(self.grade)
        corpo.append(self.mais)
        self.pilha = Gtk.Stack()
        self.pilha.add_named(self.estado, "estado")
        self.pilha.add_named(Gtk.ScrolledWindow(child=corpo, vexpand=True, hexpand=True), "grade")
        self.rolagem = self.pilha.get_child_by_name("grade")

        divisao = Gtk.Paned(start_child=esquerda, end_child=self.pilha, position=210,
                            shrink_start_child=False, resize_start_child=False, vexpand=True)
        raiz = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        raiz.append(topo)
        raiz.append(Gtk.Separator())
        raiz.append(divisao)

        def so_icone(dica, acao, icone):
            """Botão só com ícone (o nome aparece ao passar o mouse): o título da página não fica cortado."""
            b = Gtk.Button(icon_name=icone, tooltip_text=dica, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: acao())
            return b
        # pack_end empilha da direita pra esquerda: o primeiro fica na ponta direita
        self.pagina = pagina(self.titulo, raiz,
                             botao("Lock screen & menus", lambda: __import__("login_visual").abrir(self.janela),
                                   icone="preferences-desktop-wallpaper-symbolic"),
                             so_icone("Adjust wallpaper (brightness, contrast, saturation)",
                                      lambda: __import__("wall_ajustes").abrir(self.janela), "display-brightness-symbolic"),
                             so_icone("Refresh list from GitHub", lambda: self.carregar(forcar=True),
                                      "view-refresh-symbolic"),
                             so_icone("Remove this source", self.remover_fonte, "user-trash-symbolic"),
                             so_icone("Add source", self.pedir_fonte, "list-add-symbolic"))
        self.preencher_fontes()

    # --- fontes ---

    def preencher_fontes(self, escolher=None):
        self.lista_fontes.splice(0, self.lista_fontes.get_n_items(), [nome_fonte(f) for f in self.fontes])
        i = self.fontes.index(escolher) if escolher in self.fontes else 0
        self.seletor.set_selected(i)
        self.fonte = self.fontes[i]

    def ao_trocar_fonte(self, *_):
        i = self.seletor.get_selected()
        if i < len(self.fontes) and (self.fontes[i] != self.fonte or not self.carregou):
            self.fonte = self.fontes[i]
            self.carregar()

    def pedir_fonte(self):
        entrada = Gtk.Entry(placeholder_text="https://github.com/owner/repo", activates_default=True)
        d = Adw.AlertDialog(heading="Add wallpaper source",
                            body="Paste a GitHub repository link. A link to a folder "
                                 "(…/tree/main/folder) uses only that folder as the root.")
        d.set_extra_child(entrada)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Add")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.connect("response", lambda _d, r: self.adicionar_fonte(entrada.get_text()) if r == "ok" else None)
        d.present(self.janela)

    def adicionar_fonte(self, texto):
        def trabalho():
            nova = interpretar(texto)
            carregar_arquivos(nova, forcar=True)      # já valida que o repositório existe e tem imagens
            return nova

        def depois(r):
            if isinstance(r, Exception):
                self.janela.avisar(f"Couldn't add: {r}")
                return
            if r in self.fontes:
                self.janela.avisar("That source is already in the list")
            else:
                self.fontes.append(r)
                gravar_fontes(self.fontes)
            self.carregou = False
            self.preencher_fontes(r)
            self.carregar()
        self.janela.avisar("Checking repository…")
        em_segundo_plano(trabalho, depois)

    def remover_fonte(self):
        if len(self.fontes) <= 1:
            self.janela.avisar("Keep at least one source")
            return

        def remover():
            self.fontes.remove(self.fonte)
            gravar_fontes(self.fontes)
            self.carregou = False
            self.preencher_fontes()
            self.carregar()
        confirmar(self.janela, "Remove source?", f"{nome_fonte(self.fonte)} will be removed from the list "
                  "(wallpapers you already set stay saved).", "Remove", remover, destrutivo=True)

    # --- carregar lista de arquivos ---

    def ao_mostrar(self):
        if not self.alvo_fixo:
            from comum import ler_painel
            self.alvo.set_selected(1 if ler_painel().get("WALL_TARGET") == "all" else 0)
            self.pagina.set_title(self.titulo)
        self.alvo_fixo = False
        if not self.carregou:
            self.carregar()

    def para_area(self, area):
        """Aberto por Workspaces → Choose…: o próximo wallpaper vai pra essa área."""
        self.alvo.set_selected(area + 1)
        self.alvo_fixo = True
        self.pagina.set_title(f"Wallpaper for workspace {area}")

    def args_alvo(self):
        i = self.alvo.get_selected()
        if i == 1:
            return ["--all"], "all workspaces"
        if i >= 2:
            return ["--area", str(i - 1)], f"workspace {i - 1}"
        import json
        import subprocess
        try:
            a = json.loads(subprocess.run(["hyprctl", "activeworkspace", "-j"], capture_output=True, text=True).stdout)["id"]
        except (ValueError, KeyError):
            a = 0
        return (["--area", str(a)], f"workspace {a}") if a > 0 else (["--all"], "all workspaces")

    def aplicar(self, pedido, nome):
        args, onde = self.args_alvo()

        def trabalho():
            r = rodar([os.path.expanduser("~/.local/bin/papel-de-parede"), *args, *pedido])
            if r.returncode != 0:
                raise RuntimeError(f"papel-de-parede exited with code {r.returncode} (see its log)")
            return nome

        def depois(r):
            self.janela.avisar(f"Couldn't set wallpaper: {r}" if isinstance(r, Exception)
                               else f"Wallpaper set for {onde}: {r}")
            if not isinstance(r, Exception) and hasattr(self.janela, "atualizar_secoes"):
                self.janela.atualizar_secoes()
        em_segundo_plano(trabalho, depois)

    def escolher_arquivo(self):
        dialogo = Gtk.FileDialog(title="Choose a wallpaper")
        filtro = Gtk.FileFilter(name="Images")
        filtro.add_pixbuf_formats()
        filtros = Gio.ListStore.new(Gtk.FileFilter)
        filtros.append(filtro)
        dialogo.set_filters(filtros)
        pasta = os.path.expanduser("~/Pictures/Wallpapers")
        if os.path.isdir(pasta):
            dialogo.set_initial_folder(Gio.File.new_for_path(pasta))

        def pronto(dlg, res):
            try:
                arquivo = dlg.open_finish(res)
            except GLib.Error:
                return
            caminho = arquivo.get_path() if arquivo else None
            if caminho and "," in caminho:
                self.janela.avisar("That file name has a comma; rename it first")
            elif caminho:
                self.janela.avisar("Setting wallpaper…")
                self.aplicar([caminho], os.path.basename(caminho))
        dialogo.open(self.janela, None, pronto)

    def carregar(self, forcar=False):
        fonte = self.fonte
        self.carregou = True
        self.estado.set_title("Loading…")
        self.estado.set_description("")
        self.pilha.set_visible_child_name("estado")

        def depois(r):
            if fonte != self.fonte:
                return
            if isinstance(r, Exception):
                self.estado.set_title("Couldn't load this source")
                self.estado.set_description(f"{r}\nGitHub allows 60 requests per hour without login.")
                self.carregou = False
                return
            self.arquivos, truncado, self.tamanhos = r
            if truncado:
                self.janela.avisar("Repository is huge: the list from GitHub is partial")
            raiz = montar_arvore(self.arquivos, nome_fonte(fonte))
            modelo = Gtk.TreeListModel.new(
                self._store_raiz(raiz),
                False, False, lambda n: n.filhos if n.filhos.get_n_items() else None)
            self.selecao.set_model(modelo)
            modelo.get_row(0).set_expanded(True)        # abre a raiz: já mostra as categorias
            self.selecao.set_selected(0)
            self.pasta, self.busca = "", ""
            self.busca_entrada.set_text("")
            self.desenhar()
        em_segundo_plano(lambda: carregar_arquivos(fonte, forcar), depois)

    @staticmethod
    def _store_raiz(raiz):
        s = Gio.ListStore.new(Nodo)
        s.append(raiz)
        return s

    # --- árvore ---

    def criar_linha(self, _f, item):
        expansor = Gtk.TreeExpander()
        caixa = Gtk.Box(spacing=6)
        caixa.append(Gtk.Image.new_from_icon_name("folder-symbolic"))
        nome = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        total = Gtk.Label()
        total.add_css_class("dim-label")
        caixa.append(nome)
        caixa.append(total)
        expansor.set_child(caixa)
        item.set_child(expansor)

    def preencher_linha(self, _f, item):
        linha = item.get_item()                 # TreeListRow
        expansor = item.get_child()
        expansor.set_list_row(linha)
        nodo = linha.get_item()
        caixa = expansor.get_child()
        nome = caixa.get_first_child().get_next_sibling()
        nome.set_text(nodo.nome)
        nome.get_next_sibling().set_text(str(nodo.total))

    def ao_escolher_pasta(self, *_):
        linha = self.selecao.get_selected_item()
        if linha is not None:
            self.pasta = linha.get_item().caminho
            self.desenhar()

    def ao_buscar(self, entrada):
        self.busca = entrada.get_text().strip().lower()
        self.desenhar()

    # --- grade ---

    def filtrados(self):
        pref = self.pasta + "/" if self.pasta else ""
        minimo = TAMANHOS_MIN[self.minimo.get_selected()][1]
        return [c for c in self.arquivos
                if self.tamanhos.get(c, 0) >= minimo and c.startswith(pref) and (not self.busca or self.busca in c[len(pref):].lower())]

    def desenhar(self):
        self.geracao += 1
        while (c := self.grade.get_first_child()) is not None:
            self.grade.remove(c)
        self.lista = self.filtrados()
        self.mostrando = 0
        if not self.lista:
            self.estado.set_title("No images here")
            self.estado.set_description("Try another folder or clear the search.")
            self.pilha.set_visible_child_name("estado")
            return
        self.pilha.set_visible_child_name("grade")
        self.contagem.set_text(f"{len(self.lista)} images" + (f" in {self.pasta}" if self.pasta else ""))
        self.rolagem.get_vadjustment().set_value(0)
        self.mostrar_mais()

    def mostrar_mais(self):
        for caminho in self.lista[self.mostrando:self.mostrando + POR_PAGINA]:
            self.grade.append(self.celula(caminho))
        self.mostrando = min(self.mostrando + POR_PAGINA, len(self.lista))
        self.mais.set_visible(self.mostrando < len(self.lista))

    def celula(self, caminho):
        url = url_raw(self.fonte, caminho)
        imagem = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, width_request=130, height_request=84,
                             can_shrink=True)
        imagem.add_css_class("card")
        legenda = Gtk.Label(label=os.path.basename(caminho), ellipsize=3, xalign=0, max_width_chars=18)
        legenda.add_css_class("caption")
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, tooltip_text=f"{caminho}\nClick to set as wallpaper")
        caixa.append(imagem)
        caixa.append(legenda)
        caixa.caminho = caminho
        geracao = self.geracao

        def depois(r):
            if geracao != self.geracao or isinstance(r, Exception):
                return
            textura, largura, altura = r
            imagem.set_paintable(textura)
            legenda.set_text(f"{largura}×{altura}  {os.path.basename(caminho)}")
            if largura < LARGURA_BOA:
                legenda.add_css_class("warning")
                caixa.set_tooltip_text(f"{caminho}\n{largura}×{altura}: low resolution, will look soft "
                                       "on a big screen\nClick to set as wallpaper")
        # Baixar E decodificar a imagem na thread: decodificar na linha principal (a que desenha a tela)
        # travava o Settings ~6 s ao carregar mais miniaturas ("Not Responding", doc 45).
        def trabalho():
            r = self._tentar(miniatura, url)
            if not isinstance(r, Exception):
                try:
                    arq, largura, altura = r
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(arq, 480, 300, True)   # do tamanho do cartão
                    r = (Gdk.Texture.new_for_pixbuf(pb), largura, altura)
                except Exception as erro:  # noqa: BLE001
                    r = erro
            GLib.idle_add(depois, r)
        piscina.submit(trabalho)
        return caixa

    @staticmethod
    def _tentar(funcao, *args):
        try:
            return funcao(*args)
        except Exception as erro:  # noqa: BLE001 — miniatura que falha só fica vazia
            return erro

    # --- usar como papel de parede ---

    def ao_clicar_imagem(self, _grade, filho):
        caminho = filho.get_child().caminho
        url = url_raw(self.fonte, caminho)
        pagina_web = f"https://github.com/{self.fonte['owner']}/{self.fonte['repo']}"
        self.janela.avisar("Downloading wallpaper…")
        self.aplicar(["--url", url, pagina_web], os.path.basename(caminho))
