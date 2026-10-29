CXX ?= c++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic
all: recovery
recovery: driver.cpp recovery.cpp api.hpp
	$(CXX) $(CXXFLAGS) driver.cpp recovery.cpp -o recovery
clean:
	rm -f recovery
.PHONY: all clean
