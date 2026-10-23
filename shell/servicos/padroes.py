"""
servicos/padroes.py — Service dos apps padrão: que programa abre cada tipo de arquivo.

API
  CATEGORIAS                      [(id, nome, ícone)] na ordem da tela
  tipos(cat)                      os tipos (MIME) da categoria que algum app instalado abre
  descricao(tipo)                 "MPEG-4 video" (nome legível do tipo)
  apps_do_tipo(tipo)              [app] que abrem o tipo (Gio.AppInfo; só os que aparecem no menu)
  padrao(tipo)                    o app padrão do tipo, ou None
  definir(tipo, app_id)           muda o padrão de um tipo (grava ~/.config/mimeapps.list)
  apps_da_categoria(cat)          [(app, quantos tipos da categoria ele abre)], quem abre mais primeiro
  estado(cat)                     ("app", app_id) | ("custom", None) | ("none", None)
  definir_categoria(cat, app_id)  o app vira padrão de TODOS os tipos da categoria que ele abre
                                  (os que ele não abre ficam como estão)
  tipo_do_arquivo(caminho)        o tipo de um arquivo (pelo conteúdo e pelo nome)
  abrir(caminho, app_id)          abre o arquivo com aquele app (sem mudar o padrão)

Tudo pelo Gio (o mesmo banco do xdg-open e de todo app GTK/Qt): a escolha vale pro sistema inteiro.
Teste: python3 -m servicos.padroes [categoria]
"""
import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

CATEGORIAS = [
    ("web", "Web browser", "web-browser-symbolic"),
    ("email", "Email", "mail-unread-symbolic"),
    ("video", "Video", "video-x-generic-symbolic"),
    ("audio", "Music and audio", "audio-x-generic-symbolic"),
    ("imagem", "Images", "image-x-generic-symbolic"),
    ("raw", "Camera RAW photos", "camera-photo-symbolic"),
    ("camadas", "Layered images and design", "applications-graphics-symbolic"),
    ("pdf", "PDF and print documents", "x-office-document-symbolic"),
    ("office", "Office documents", "x-office-spreadsheet-symbolic"),
    ("ebook", "E-books", "accessories-dictionary-symbolic"),
    ("texto", "Text and notes", "text-x-generic-symbolic"),
    ("codigo", "Code and data", "text-x-script-symbolic"),
    ("compactado", "Archives", "package-x-generic-symbolic"),
    ("pasta", "Folders", "folder-symbolic"),
    ("suite", "Arch Quilly documents", "document-edit-symbolic"),
    ("outros", "Other", "application-x-executable-symbolic"),
]

_RAW = ("x-adobe-dng", "x-canon-", "x-nikon-", "x-sony-", "x-fuji-", "x-olympus-", "x-panasonic-", "x-pentax-",
        "x-samsung-srw", "x-dcraw", "x-sigma-", "x-kodak-", "x-minolta-", "x-epson-", "x-hasselblad", "x-leaf-",
        "x-mamiya-", "x-phaseone-", "x-raw", "x-arw", "x-cr2", "x-cr3", "x-nef", "x-orf", "x-rw2", "x-pef", "x-raf")
_CAMADAS = {"image/vnd.adobe.photoshop", "image/x-psd", "image/x-xcf", "image/openraster", "application/x-krita",
            "application/x-photocraft", "image/x-psb", "application/x-paintnet", "image/x-paintnet"}
_WEB = {"x-scheme-handler/http", "x-scheme-handler/https", "text/html", "application/xhtml+xml",
        "x-scheme-handler/about", "x-scheme-handler/unknown"}
_PDF = {"application/pdf", "application/postscript", "image/vnd.djvu", "application/x-xpsdocument", "application/oxps"}
_EBOOK = {"application/epub+zip", "application/x-mobipocket-ebook", "application/vnd.amazon.mobi8-ebook",
          "application/x-fictionbook+xml", "application/vnd.comicbook+zip", "application/vnd.comicbook-rar",
          "application/x-cbz", "application/x-cbr"}
_TEXTO = {"text/plain", "text/markdown", "text/x-markdown", "text/x-log", "text/x-readme", "text/x-nfo"}
_SUITE = {"application/x-hyp", "application/x-artry", "application/x-filmcraft"}
_COMPACTADO = ("zip", "x-tar", "compressed-tar", "x-7z", "rar", "zstd", "x-xz", "gzip", "x-bzip", "x-lzma", "x-lz4",
               "x-lzip", "x-cpio", "x-archive", "x-ar", "vnd.ms-cab", "x-iso9660", "x-xar", "x-arj", "x-ace",
               "x-lha", "x-zoo", "x-stuffit", "x-compress", "x-lrzip", "x-rzip", "x-source-rpm", "x-rpm",
               "vnd.debian.binary-package", "x-deb", "x-squashfs", "x-apple-diskimage")
_OFFICE = ("vnd.oasis.opendocument", "vnd.openxmlformats", "msword", "vnd.ms-excel", "vnd.ms-powerpoint",
           "rtf", "vnd.ms-works", "vnd.wordperfect", "x-abiword", "vnd.sun.xml", "x-gnumeric", "csv",
           "vnd.lotus", "x-iwork", "vnd.apple.")


def categoria_de(tipo):
    """A categoria de um tipo MIME."""
    t = tipo.lower()
    base, _, sub = t.partition("/")
    if t in _WEB:
        return "web"
    if t == "x-scheme-handler/mailto" or t == "message/rfc822":
        return "email"
    if t in _SUITE:
        return "suite"
    if t in _CAMADAS:
        return "camadas"
    if t in _PDF:
        return "pdf"
    if t in _EBOOK:
        return "ebook"
    if t == "inode/directory":
        return "pasta"
    if base == "video":
        return "video"
    if base == "audio":
        return "audio"
    if base == "image":
        return "raw" if any(sub.startswith(r) for r in _RAW) else "imagem"
    if t in _TEXTO:
        return "texto"
    if base == "text" and sub in ("csv", "tab-separated-values", "x-csv"):
        return "office"
    if base == "text" or sub in ("json", "xml", "x-shellscript", "javascript", "x-yaml", "toml", "x-python",
                                 "x-perl", "x-php", "x-ruby", "sql", "x-sql", "x-desktop", "x-sh"):
        return "codigo"
    if base == "application" and any(k in sub for k in _COMPACTADO):
        return "compactado"
    if base == "application" and any(k in sub for k in _OFFICE):
        return "office"
    return "outros"


def _visivel(app):
    return app is not None and app.should_show()


def apps_do_tipo(tipo):
    vistos, out = set(), []
    for a in Gio.AppInfo.get_all_for_type(tipo):
        if _visivel(a) and a.get_id() not in vistos:
            vistos.add(a.get_id())
            out.append(a)
    return out


_cache_tipos = {}


def _todos_os_tipos():
    """Cada tipo registrado que algum app (visível) abre, com a categoria dele."""
    if not _cache_tipos:
        for t in Gio.content_types_get_registered():
            if apps_do_tipo(t):
                _cache_tipos.setdefault(categoria_de(t), []).append(t)
        # os de protocolo (http, mailto…) não estão no banco de tipos: entram pelos apps que os declaram
        for a in Gio.AppInfo.get_all():
            if not _visivel(a):
                continue
            for t in a.get_supported_types() or []:
                if t.startswith("x-scheme-handler/") and t in _WEB | {"x-scheme-handler/mailto"}:
                    lista = _cache_tipos.setdefault(categoria_de(t), [])
                    if t not in lista:
                        lista.append(t)
        for lista in _cache_tipos.values():
            lista.sort()
    return _cache_tipos


def recarregar():
    _cache_tipos.clear()


def tipos(cat):
    return list(_todos_os_tipos().get(cat, []))


def descricao(tipo):
    if tipo.startswith("x-scheme-handler/"):
        return tipo.split("/", 1)[1] + " links"
    return Gio.content_type_get_description(tipo) or tipo


def padrao(tipo):
    a = Gio.AppInfo.get_default_for_type(tipo, False)
    return a if _visivel(a) else None


def definir(tipo, app_id):
    app = _app(app_id)
    if not app:
        raise ValueError(f"app {app_id} not found")
    app.set_as_default_for_type(tipo)


def apps_da_categoria(cat):
    cont = {}
    apps = {}
    for t in tipos(cat):
        for a in apps_do_tipo(t):
            cont[a.get_id()] = cont.get(a.get_id(), 0) + 1
            apps[a.get_id()] = a
    return sorted(((apps[i], n) for i, n in cont.items()), key=lambda x: (-x[1], x[0].get_display_name().casefold()))


def estado(cat):
    ids = set()
    for t in tipos(cat):
        p = padrao(t)
        ids.add(p.get_id() if p else None)
    ids.discard(None) if len(ids) > 1 else None
    if not ids or ids == {None}:
        return ("none", None)
    if len(ids) == 1:
        return ("app", next(iter(ids)))
    return ("custom", None)


def definir_categoria(cat, app_id):
    app = _app(app_id)
    if not app:
        raise ValueError(f"app {app_id} not found")
    feitos = 0
    for t in tipos(cat):
        if any(a.get_id() == app_id for a in apps_do_tipo(t)):
            app.set_as_default_for_type(t)
            feitos += 1
    return feitos


def tipo_do_arquivo(caminho):
    f = Gio.File.new_for_path(caminho)
    try:
        info = f.query_info("standard::content-type", Gio.FileQueryInfoFlags.NONE, None)
        return info.get_content_type()
    except Exception:  # noqa: BLE001
        tipo, _ = Gio.content_type_guess(caminho, None)
        return tipo


def abrir(caminho, app_id):
    app = _app(app_id)
    if not app:
        raise ValueError(f"app {app_id} not found")
    return app.launch([Gio.File.new_for_path(caminho)], None)


def _app(app_id):
    for a in Gio.AppInfo.get_all():
        if a.get_id() == app_id:
            return a
    return None


if __name__ == "__main__":
    import sys
    cats = [c for c, *_ in CATEGORIAS] if len(sys.argv) < 2 else sys.argv[1:]
    for c in cats:
        e = estado(c)
        print(f"{c:12} {len(tipos(c)):4} tipos  estado={e[0]} {e[1] or ''}  apps={[a.get_display_name() for a, _ in apps_da_categoria(c)][:5]}")
