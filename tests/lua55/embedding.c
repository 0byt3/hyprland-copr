#include <stdio.h>
#include <string.h>

#include <lua.h>
#include <lauxlib.h>
#include <lualib.h>

#if LUA_VERSION_NUM != 505
#error "pkgconfig(lua55) must select Lua 5.5 headers"
#endif

int main(void)
{
    lua_State *state = luaL_newstate();

    if (state == NULL) {
        return 1;
    }
    luaL_openlibs(state);
    lua_getglobal(state, "package");
    lua_getfield(state, -1, "path");
    if (strcmp(lua_tostring(state, -1), LUA_PATH_DEFAULT) != 0) {
        fprintf(stderr, "installed header and runtime Lua paths differ\n");
        lua_close(state);
        return 1;
    }
    lua_pop(state, 1);
    lua_getfield(state, -1, "cpath");
    if (strcmp(lua_tostring(state, -1), LUA_CPATH_DEFAULT) != 0) {
        fprintf(stderr, "installed header and runtime C paths differ\n");
        lua_close(state);
        return 1;
    }
    lua_pop(state, 2);
    if (luaL_dostring(state,
            "assert(_VERSION == 'Lua 5.5'); "
            "assert(type(table.create) == 'function'); "
            "return 6 * 7") != LUA_OK) {
        fprintf(stderr, "%s\n", lua_tostring(state, -1));
        lua_close(state);
        return 1;
    }
    if (lua_tointeger(state, -1) != 42) {
        lua_close(state);
        return 1;
    }
    lua_close(state);
    return 0;
}
