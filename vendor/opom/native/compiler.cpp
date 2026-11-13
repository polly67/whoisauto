#include "Luau/Compiler.h"
#include <iostream>
#include <iterator>
#include <string>

static std::string quote(const std::string& value) {
    std::string out = "\"";
    for (unsigned char c : value) {
        if (c == '\\' || c == '"') { out += '\\'; out += c; }
        else if (c < 32) {
            const char* hex = "0123456789abcdef";
            out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15];
        } else out += c;
    }
    return out + '"';
}

int main() {
    const std::string source{std::istreambuf_iterator<char>(std::cin), {}};
    Luau::CompileOptions options;
    options.optimizationLevel = 0;
    const std::string bytecode = Luau::compile(source, options);
    if (bytecode.empty() || bytecode[0] == 0) {
        std::cout << "{\"ok\":false,\"error\":"
                  << quote(bytecode.size() > 1 ? bytecode.substr(1) : "Compiler returned no bytecode") << "}\n";
        return 1;
    }
    std::cout << "{\"ok\":true,\"bytecode_bytes\":" << bytecode.size() << "}\n";
}
