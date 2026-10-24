#!/usr/bin/env bash
# Run inside a disposable Fedora container after installing
# gcc, binutils, pkgconf-pkg-config, lua, lua-libs, lua-devel, and Lua55 RPMs.
set -euo pipefail

if [[ ! -e /run/.containerenv && ! -e /.dockerenv ]]; then
    printf '%s\n' "Refusing to run installed module probes outside a disposable container." >&2
    exit 1
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
unset LUA_PATH LUA_CPATH LUA_PATH_5_5 LUA_CPATH_5_5 LD_LIBRARY_PATH
reject_match()
{
    if grep -E "$1" "$2"; then
        printf 'Unexpected metadata or ELF entry: %s\n' "$1" >&2
        exit 1
    fi
}

rpm -q lua lua-libs lua-devel lua55 lua55-libs lua55-devel
rpm -V lua lua-libs lua-devel lua55 lua55-libs lua55-devel
lua -E -e 'assert(_VERSION == "Lua 5.4")'
lua5.5 -E -e 'assert(_VERSION == "Lua 5.5")'
luac5.5 -v
version="$(rpm -q --qf '%{VERSION}' lua55)"
pkg-config --exact-version="$version" lua55
pkg-config --cflags --libs lua55
pkg-config --exists lua

libdir="$(pkg-config --variable=libdir lua55)"
lmod="$(pkg-config --variable=INSTALL_LMOD lua55)"
cmod="$(pkg-config --variable=INSTALL_CMOD lua55)"
[[ "$(pkg-config --variable=includedir lua55)" == /usr/include/lua5.5 ]]
[[ "$libdir" == /usr/lib64 ]]
[[ "$lmod" == /usr/share/lua/5.5 ]]
[[ "$cmod" == /usr/lib64/lua/5.5 ]]

rpm -q --requires lua55 > "$work/requires"
rpm -q --provides lua55-libs > "$work/provides"
rpm -q --provides lua55-devel > "$work/devel-provides"
grep -F 'liblua5.5.so.0()(64bit)' "$work/requires"
grep -F 'liblua5.5.so.0()(64bit)' "$work/provides"
grep -Fx "pkgconfig(lua55) = $version" "$work/devel-provides"
reject_match '^pkgconfig\(lua\)( |$)' "$work/devel-provides"
reject_match '^liblua-5[.]4[.]so|^liblua[.]so' "$work/provides"

readelf -d "$libdir/liblua5.5.so.0" > "$work/library-dynamic"
readelf -d /usr/bin/lua5.5 > "$work/interpreter-dynamic"
grep -E 'SONAME.*\[liblua5[.]5[.]so[.]0\]' "$work/library-dynamic"
grep -E 'NEEDED.*\[liblua5[.]5[.]so[.]0\]' "$work/interpreter-dynamic"
reject_match 'TEXTREL|RPATH|RUNPATH' "$work/library-dynamic"
reject_match 'RPATH|RUNPATH' "$work/interpreter-dynamic"
ldd /usr/bin/lua5.5 > "$work/ldd"
cat "$work/ldd"
reject_match 'not found' "$work/ldd"
[[ "$(readlink "$libdir/liblua5.5.so")" == liblua5.5.so.0 ]]

rpm -ql lua55 lua55-libs lua55-devel > "$work/files"
reject_match '[.]a$|/usr/bin/(lua|luac)$|/usr/include/(lua[.]h|luaconf[.]h|lualib[.]h|lauxlib[.]h|lua[.]hpp)$|/pkgconfig/lua[.]pc$|/liblua[.]so' "$work/files"

# Fail on every common owned path except shared directories such as
# /usr/lib/.build-id; no Fedora-owned regular file may be reused.
rpm -ql lua lua-libs lua-devel > "$work/fedora-files"
while IFS= read -r path; do
    if [[ ! -d "$path" ]] && grep -Fxq "$path" "$work/fedora-files"; then
        printf 'Conflicting file: %s\n' "$path" >&2
        exit 1
    fi
done < "$work/files"

# Compile and execute against the installed public pkg-config interface.
cflags="$(pkg-config --cflags lua55)"
libs="$(pkg-config --libs lua55)"
read -r -a cflag_args <<< "$cflags"
read -r -a lib_args <<< "$libs"
gcc "${cflag_args[@]}" "$root/tests/lua55/embedding.c" \
    -o "$work/embedding" "${lib_args[@]}"
"$work/embedding"
readelf -d "$work/embedding" > "$work/embedding-dynamic"
grep -E 'NEEDED.*\[liblua5[.]5[.]so[.]0\]' "$work/embedding-dynamic"

# A normal Lua extension leaves Lua API symbols for the host to resolve;
# loading it verifies the shared interpreter exports the correct API.
gcc -shared -fPIC "${cflag_args[@]}" \
    "$root/tests/lua55/module.c" -o "$work/packaging_probe.so"
printf '%s\n' 'return {value = 55}' > "$work/packaging_lua_probe.lua"
lua5.5 -E - "$work" "$lmod" "$cmod" <<'LUA'
    assert(package.path:find(arg[2] .. "/?.lua", 1, true))
    assert(package.cpath:find(arg[3] .. "/?.so", 1, true))
    print(package.path)
    print(package.cpath)
    package.path = arg[1] .. "/?.lua;" .. package.path
    package.cpath = arg[1] .. "/?.so;" .. package.cpath
    assert(require("packaging_probe") == 55)
    assert(require("packaging_lua_probe").value == 55)
LUA
lua -E -e '
    assert(not package.path:find("/lua/5.5/", 1, true))
    assert(not package.cpath:find("/lua/5.5/", 1, true))
    assert(not pcall(require, "packaging_probe"))
    assert(not pcall(require, "packaging_lua_probe"))
'

printf '%s\n' 'assert(_VERSION == "Lua 5.5"); assert(table.create)' > "$work/probe.lua"
luac5.5 -p "$work/probe.lua"
luac5.5 -o "$work/probe.luac" "$work/probe.lua"
lua5.5 -E "$work/probe.luac"
rpm -V lua lua-libs lua-devel lua55 lua55-libs lua55-devel
printf '%s\n' "Lua55 installed RPM, embedding, modules, and Lua 5.4 coexistence checks passed."
