#include <glaze/glaze.hpp>
#include <string>

struct Payload {
    int answer = 0;
};

int main() {
    Payload value;
    const std::string input = R"({"answer":42})";
    if (glz::read_json(value, input) || value.answer != 42) {
        return 1;
    }
    auto encoded = glz::write_json(value);
    if (!encoded) {
        return 2;
    }
    Payload round_trip;
    return glz::read_json(round_trip, encoded.value()) || round_trip.answer != 42;
}
