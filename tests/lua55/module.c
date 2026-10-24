#include <lua.h>
#include <lauxlib.h>

#if LUA_VERSION_NUM != 505
#error "Lua 5.5 module requires Lua 5.5 headers"
#endif

int luaopen_packaging_probe(lua_State *state);

int luaopen_packaging_probe(lua_State *state)
{
    lua_pushinteger(state, 55);
    return 1;
}
