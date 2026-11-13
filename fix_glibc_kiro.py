#!/usr/bin/env python3
"""
Parche de compatibilidad e instalador GLIBC 2.27 (Ubuntu 18.04 / Peppermint 10) para Kiro IDE.

Resuelve las dependencias de GLIBC_2.28 en binarios ELF de Kiro IDE:
  1. Reemplaza fcntl64@GLIBC_2.28 por fcntl@GLIBC_2.2.5 en módulos nativos de Node:
     - pty.node (node-pty, terminal integrada)
     - vscode-sqlite3.node (@vscode/sqlite3, base de datos de estado de Kiro/VS Code)
  2. Desvincula la versión GLIBC_2.28 del símbolo débil (STB_WEAK) statx@GLIBC_2.28 en binarios Rust
     para permitir el fallback nativo de la librería estándar de Rust a syscalls compatibles:
     - os_proxy_resolver.node (@vscode/os-proxy-resolver-linux-x64-gnu)

Además, permite extraer e instalar el paquete oficial .deb de Kiro en ~/.local/share/kiro
(evitando el bloqueo de dpkg por 'Depends: libc6 (>= 2.28)').
"""
import argparse
import glob
import os
import shutil
import struct
import subprocess
import sys
import tempfile

DEFAULT_IDE_ROOT = os.path.expanduser("~/.local/share/kiro")
SYSTEM_IDE_ROOT = "/usr/share/kiro"

IDE_RELATIVE_TARGETS = [
    "resources/app/node_modules.asar.unpacked/node-pty/build/Release/pty.node",
    "resources/app/node_modules.asar.unpacked/@vscode/sqlite3/build/Release/vscode-sqlite3.node",
    "resources/app/node_modules.asar.unpacked/@vscode/os-proxy-resolver-linux-x64-gnu/os_proxy_resolver.node",
]


def _get_str(data: bytearray, base: int, idx: int) -> str:
    end = data.index(0, base + idx)
    return data[base + idx : end].decode("utf-8", "replace")


def patch_elf(path: str) -> bool:
    """
    Parchea un binario ELF x86_64 para eliminar referencias a GLIBC_2.28
    (fcntl64 -> fcntl@GLIBC_2.2.5 y statx débil -> global sin versión).
    Devuelve True si el binario quedó libre de errores de GLIBC_2.28.
    """
    if not os.path.exists(path) or os.path.islink(path):
        print(f"[SKIP] No existe: {path}")
        return True

    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"\x7fELF":
            return True
        f.seek(0)
        data = bytearray(f.read())

    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    if e_shoff == 0 or e_shnum == 0:
        return True

    shstr_off = struct.unpack_from("<Q", data, e_shoff + e_shstrndx * e_shentsize + 0x18)[0]

    sections = {}
    for i in range(e_shnum):
        sh = e_shoff + i * e_shentsize
        name_idx = struct.unpack_from("<I", data, sh)[0]
        name = _get_str(data, shstr_off, name_idx)
        sh_offset, sh_size = struct.unpack_from("<QQ", data, sh + 0x18)
        sections[name] = (sh_offset, sh_size)

    required = (".dynstr", ".dynsym", ".gnu.version", ".gnu.version_r")
    if not all(k in sections for k in required):
        return True

    dynstr_off, _ = sections[".dynstr"]
    dynsym_off, dynsym_sz = sections[".dynsym"]
    gnuver_off, _ = sections[".gnu.version"]
    verr_off, _ = sections[".gnu.version_r"]

    # 1. Buscar versiones GLIBC_2.2.5 y GLIBC_2.28 en la entrada Verneed de libc.so.6
    off = verr_off
    ver_225 = None
    ver_228 = None
    libc_verneed_off = None

    while True:
        vn_version, vn_cnt, vn_file, vn_aux, vn_next = struct.unpack_from("<HHIII", data, off)
        fname = _get_str(data, dynstr_off, vn_file)
        if fname == "libc.so.6":
            libc_verneed_off = off
            aoff = off + vn_aux
            for _ in range(vn_cnt):
                vna_hash, vna_flags, vna_other, vna_name, vna_next = struct.unpack_from("<IHHII", data, aoff)
                vname = _get_str(data, dynstr_off, vna_name)
                if vname == "GLIBC_2.2.5":
                    ver_225 = vna_other
                elif vname == "GLIBC_2.28":
                    ver_228 = vna_other
                if vna_next == 0:
                    break
                aoff += vna_next
        if vn_next == 0:
            break
        off += vn_next

    patched = False

    # 2. Actualizar tabla de símbolos dinámicos (.dynsym y .gnu.version)
    nsyms = dynsym_sz // 24
    for sym_idx in range(nsyms):
        sym_off = dynsym_off + sym_idx * 24
        st_name, st_info = struct.unpack_from("<IB", data, sym_off)
        sname = _get_str(data, dynstr_off, st_name)
        ver = struct.unpack_from("<H", data, gnuver_off + sym_idx * 2)[0]
        ver_idx = ver & 0x7FFF

        if sname == "fcntl64" or (sname == "fcntl" and ver_228 is not None and ver_idx == ver_228):
            if data[dynstr_off + st_name : dynstr_off + st_name + 8] == b"fcntl64\x00":
                data[dynstr_off + st_name : dynstr_off + st_name + 8] = b"fcntl\x00\x00\x00"
                patched = True
            if ver_225 is not None and ver != ver_225:
                struct.pack_into("<H", data, gnuver_off + sym_idx * 2, ver_225)
                patched = True
        elif sname == "statx" and (ver_228 is not None and ver_idx == ver_228):
            # Símbolo débil (STB_WEAK) en Rust std::fs: cambiar a versión global (1)
            struct.pack_into("<H", data, gnuver_off + sym_idx * 2, 1)
            patched = True

    # 3. Desvincular el requisito GLIBC_2.28 de la lista enlazada Vernaux de libc.so.6
    if libc_verneed_off is not None and ver_228 is not None:
        vn_version, vn_cnt, vn_file, vn_aux, vn_next = struct.unpack_from("<HHIII", data, libc_verneed_off)
        aoff = libc_verneed_off + vn_aux
        prev_aoff = None
        for _ in range(vn_cnt):
            vna_hash, vna_flags, vna_other, vna_name, vna_next = struct.unpack_from("<IHHII", data, aoff)
            vname = _get_str(data, dynstr_off, vna_name)
            if vname == "GLIBC_2.28":
                if prev_aoff is None:
                    vn_aux = (aoff + vna_next - libc_verneed_off) if vna_next != 0 else 0
                else:
                    p_hash, p_flags, p_other, p_name, p_next = struct.unpack_from("<IHHII", data, prev_aoff)
                    new_p_next = (p_next + vna_next) if vna_next != 0 else 0
                    struct.pack_into("<IHHII", data, prev_aoff, p_hash, p_flags, p_other, p_name, new_p_next)
                vn_cnt -= 1
                struct.pack_into("<HHIII", data, libc_verneed_off, vn_version, vn_cnt, vn_file, vn_aux, vn_next)
                patched = True
                break
            if vna_next == 0:
                break
            prev_aoff = aoff
            aoff += vna_next

    if patched:
        with open(path, "wb") as f:
            f.write(data)
        print(f"[OK] Parcheado: {path}")
    else:
        print(f"[OK] Ya estaba parcheado / sin cambios: {path}")

    out = subprocess.run(
        ["ldd", "-r", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    ).stdout.decode("utf-8", "replace")
    bad = [line.strip() for line in out.splitlines() if "not found" in line or "GLIBC_2.28" in line]
    if bad:
        print(f"[WARN] Problemas restantes en {path}:\n  " + "\n  ".join(bad))
        return False
    return True


def find_latest_deb() -> str:
    """Busca el archivo kiro*.deb más reciente en ~/Descargas o ~/Downloads."""
    candidates = []
    for folder in ("~/Descargas", "~/Downloads", "."):
        pattern = os.path.join(os.path.expanduser(folder), "kiro*.deb")
        candidates.extend(glob.glob(pattern))
    if not candidates:
        return ""
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def ensure_user_integration(dest_dir: str, icon_src: str = "") -> None:
    """Configura el icono, el enlace en ~/.local/bin/kiro y los lanzadores .desktop."""
    icon_dest_dir = os.path.expanduser("~/.local/share/icons")
    icon_dest = os.path.join(icon_dest_dir, "kiro.png")
    fallback_icon = os.path.join(dest_dir, "resources/app/resources/linux/code.png")
    src = icon_src if (icon_src and os.path.isfile(icon_src)) else fallback_icon
    if os.path.isfile(src):
        os.makedirs(icon_dest_dir, exist_ok=True)
        shutil.copy2(src, icon_dest)
        shutil.copy2(src, os.path.join(icon_dest_dir, "code-oss.png"))

    bin_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(bin_dir, exist_ok=True)
    bin_link = os.path.join(bin_dir, "kiro")
    target_bin = os.path.join(dest_dir, "bin/kiro")
    if os.path.lexists(bin_link):
        os.remove(bin_link)
    os.symlink(target_bin, bin_link)
    print(f"[OK] Enlace configurado: {bin_link} -> {target_bin}")

    apps_dir = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps_dir, exist_ok=True)
    desktop_path = os.path.join(apps_dir, "kiro.desktop")
    url_desktop_path = os.path.join(apps_dir, "kiro-url-handler.desktop")
    icon_ref = icon_dest if os.path.isfile(icon_dest) else "code-oss"

    desktop_content = f"""[Desktop Entry]
Name=Kiro
Comment=Alongside you from concept to production.
GenericName=Text Editor
Exec={dest_dir}/kiro %F
Icon={icon_ref}
Type=Application
StartupNotify=false
StartupWMClass=Kiro
Categories=TextEditor;Development;IDE;AI;
MimeType=text/plain;inode/directory;application/x-kiro-workspace;
Actions=new-empty-window;
Keywords=kiro;vscode;code;ide;

[Desktop Action new-empty-window]
Name=New Empty Window
Name[es]=Nueva ventana vacía
Exec={dest_dir}/kiro --new-window %F
Icon={icon_ref}
"""
    with open(desktop_path, "w", encoding="utf-8") as f:
        f.write(desktop_content)
    os.chmod(desktop_path, 0o755)
    print(f"[OK] Lanzador configurado: {desktop_path}")

    url_desktop_content = f"""[Desktop Entry]
Name=Kiro - URL Handler
Comment=Alongside you from concept to production.
GenericName=Kiro
Exec={dest_dir}/kiro --open-url %U
Icon={icon_ref}
Type=Application
NoDisplay=true
StartupNotify=true
Categories=Utility;TextEditor;Development;IDE;
MimeType=x-scheme-handler/kiro;
Keywords=kiro;
"""
    with open(url_desktop_path, "w", encoding="utf-8") as f:
        f.write(url_desktop_content)
    os.chmod(url_desktop_path, 0o755)
    print(f"[OK] Manejador URL configurado: {url_desktop_path}")

    subprocess.run(
        ["xdg-mime", "default", "kiro-url-handler.desktop", "x-scheme-handler/kiro"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def install_from_deb(deb_path: str, dest_dir: str) -> None:
    """Extrae el paquete .deb de Kiro en dest_dir y configura el enlace en ~/.local/bin y los .desktop."""
    deb_path = os.path.abspath(os.path.expanduser(deb_path))
    if not os.path.isfile(deb_path):
        print(f"[ERROR] No existe el paquete .deb: {deb_path}")
        sys.exit(1)

    print(f"[INFO] Extrayendo {deb_path} ...")
    tmp_dir = tempfile.mkdtemp(prefix="kiro-deb-")
    try:
        subprocess.check_call(["dpkg-deb", "-x", deb_path, tmp_dir])
        extracted_kiro = os.path.join(tmp_dir, "usr/share/kiro")
        if not os.path.isdir(extracted_kiro):
            print(f"[ERROR] El paquete {deb_path} no contiene usr/share/kiro")
            sys.exit(1)

        os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
        if os.path.exists(dest_dir):
            print(f"[INFO] Eliminando instalación previa en {dest_dir} ...")
            shutil.rmtree(dest_dir)

        print(f"[INFO] Moviendo archivos a {dest_dir} ...")
        shutil.move(extracted_kiro, dest_dir)

        icon_src = os.path.join(tmp_dir, "usr/share/pixmaps/code-oss.png")
        ensure_user_integration(dest_dir, icon_src)
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Instalador y parche de compatibilidad GLIBC 2.27 para Kiro IDE."
    )
    parser.add_argument(
        "--deb",
        help="Ruta al paquete kiro-ide-*-linux-x64.deb para extraer e instalar en ~/.local/share/kiro",
        default=None,
    )
    parser.add_argument(
        "--ide-root",
        help=f"Ruta de instalación de Kiro IDE (por defecto: {DEFAULT_IDE_ROOT})",
        default=None,
    )
    args = parser.parse_args()

    ide_root = os.path.expanduser(args.ide_root) if args.ide_root else DEFAULT_IDE_ROOT

    if args.deb:
        install_from_deb(args.deb, ide_root)
    elif not os.path.isdir(ide_root) and os.path.isdir(SYSTEM_IDE_ROOT):
        ide_root = SYSTEM_IDE_ROOT
    elif not os.path.isdir(ide_root):
        latest_deb = find_latest_deb()
        if latest_deb:
            print(f"[INFO] Kiro no está instalado en {ide_root}. Se encontró: {latest_deb}")
            install_from_deb(latest_deb, ide_root)
        else:
            print(
                f"[ERROR] No se encontró Kiro en {ide_root} ni paquetes kiro*.deb en ~/Descargas."
            )
            sys.exit(1)
    elif ide_root == DEFAULT_IDE_ROOT and not os.path.lexists(os.path.expanduser("~/.local/bin/kiro")):
        ensure_user_integration(ide_root)

    targets = [os.path.join(ide_root, rel) for rel in IDE_RELATIVE_TARGETS]

    all_ok = True
    for target in targets:
        if not patch_elf(target):
            all_ok = False

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()

