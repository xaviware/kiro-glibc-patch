# Kiro IDE — Parche e Instalador de Compatibilidad GLIBC 2.27 (Ubuntu 18.04 / Peppermint 10)

Script en Python (sin dependencias externas) para instalar y ejecutar **Kiro IDE** (`kiro.dev`) en distribuciones Linux con **GLIBC 2.27** (como Ubuntu 18.04 LTS, Peppermint OS 10, Linux Mint 19.x o Debian Buster/Stretch).

---

## El Problema

En sistemas con `glibc 2.27`, la instalación y ejecución de Kiro IDE falla por dos motivos:

1. **Bloqueo del paquete `.deb` en `dpkg`/`apt`**: El archivo `DEBIAN/control` declara `Depends: libc6 (>= 2.28)`, impidiendo la instalación estándar aunque el ejecutable principal de Electron (`kiro`) sí sea compatible con `glibc 2.27`.
2. **Módulos nativos enlazados contra `GLIBC_2.28`**: Tres módulos nativos incluidos en Kiro IDE fallan al cargar con:
   ```text
   version `GLIBC_2.28' not found (required by .../node-pty/build/Release/pty.node)
   version `GLIBC_2.28' not found (required by .../@vscode/sqlite3/build/Release/vscode-sqlite3.node)
   version `GLIBC_2.28' not found (required by .../@vscode/os-proxy-resolver-linux-x64-gnu/os_proxy_resolver.node)
   ```

   Específicamente, la dependencia de `GLIBC_2.28` proviene únicamente de dos símbolos:
   - **`fcntl64@GLIBC_2.28`** (en `pty.node` y `vscode-sqlite3.node`): afecta la terminal integrada y la base de datos SQLite de estado del IDE.
   - **`statx@GLIBC_2.28`** (símbolo débil `STB_WEAK` en el módulo Rust `os_proxy_resolver.node`): aunque la librería estándar de Rust (`std::fs`) define `statx` como símbolo débil con fallback automático cuando no existe, la entrada de versión `GLIBC_2.28` en `.gnu.version_r` hace que el enlazador dinámico (`ld.so`) aborte antes de iniciar.

---

## ¿Qué hace este script?

`fix_glibc_kiro.py` resuelve tanto la instalación como el parcheo binario:

1. **Instalación en espacio de usuario (`~/.local/share/kiro`)**: Si Kiro aún no está instalado (o si se pasa `--deb /ruta/a/kiro-ide-*-linux-x64.deb`), extrae automáticamente el paquete `.deb` oficial en `~/.local/share/kiro`, crea el enlace simbólico `~/.local/bin/kiro` y registra los lanzadores de escritorio (`kiro.desktop` y el manejador de autenticación `kiro-url-handler.desktop` para enlaces `kiro://`).
2. **Reemplazo de `fcntl64@GLIBC_2.28` → `fcntl@GLIBC_2.2.5`**: En arquitecturas de 64 bits (`x86_64`), `fcntl64` y `fcntl` son equivalentes a nivel de ABI. El script renombra `fcntl64\0` a `fcntl\0\0\0` en `.dynstr` y reasigna su índice en `.gnu.version` a `GLIBC_2.2.5`.
3. **Desvinculación de versión en el símbolo débil `statx`**: En `os_proxy_resolver.node`, que declara `w statx@GLIBC_2.28`, cambia su índice en `.gnu.version` a `1` (`VER_NDX_GLOBAL`, símbolo débil sin versión) para que `ld.so` lo resuelva a `NULL` sin error y el runtime de Rust utilice su fallback nativo.
4. **Limpieza de `.gnu.version_r` (`verneed`)**: Desvincula el nodo `GLIBC_2.28` de la lista enlazada `Vernaux` de `libc.so.6` y decrementa `vn_cnt`.
5. **Verificación con `ldd -r`**: Comprueba cada binario parcheado para confirmar que no queden referencias a `GLIBC_2.28` ni bibliotecas faltantes.

> **Nota:** El script es **idempotente** (puedes ejecutarlo múltiples veces de forma segura; detecta si los archivos ya fueron parcheados).

---

## Requisitos

- **SO / Arquitectura:** Linux `x86_64` con `glibc 2.27`
- **Python:** Python 3.6+ (solo utiliza librería estándar)
- **Ruta por defecto del IDE:** `~/.local/share/kiro` (también soporta `/usr/share/kiro`)

---

## Uso

### 1. Clonar el repositorio

```bash
git clone https://github.com/xaviware/kiro-glibc-patch.git
cd kiro-glibc-patch
```

### 2. Instalar desde un paquete `.deb` y aplicar el parche

Si descargaste `kiro-ide-*-linux-x64.deb` en `~/Descargas` (o `~/Downloads`), ejecuta simplemente:

```bash
python3 fix_glibc_kiro.py
```

O especificando la ruta al archivo `.deb`:

```bash
python3 fix_glibc_kiro.py --deb ~/Descargas/kiro-ide-1.4.178-stable-linux-x64.deb
```

### 3. Re-aplicar el parche tras una actualización

Si Kiro IDE se actualiza y sobrescribe sus binarios:

```bash
python3 fix_glibc_kiro.py
```

---

## Licencia

Distribuido bajo la licencia [MIT](LICENSE).

