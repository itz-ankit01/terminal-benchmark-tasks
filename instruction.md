`/app` contains a small transactional page store whose crash recovery has not been written yet. Implement `pager::recover` in `/app/recovery.cpp`.

`/app/contract.txt` is the specification. It covers the page and log formats, the records recovery is allowed to write, the state the store must be in when recovery returns, and what happens when the machine crashes again in the middle of recovery. The types are in `/app/api.hpp`, and `/app/driver.cpp` is the program that reads a store image, calls your function and prints the actions it returns.

Only `/app/recovery.cpp` is graded. It is compiled with fresh copies of `api.hpp` and `driver.cpp`, so changes to other files have no effect. It must build as C++17 using only the standard library. `make -C /app` builds the program and `python3 /app/smoke.py` runs a quick sanity check; passing that check says nothing about correctness.

Grading runs your build on store images you have not seen and checks every action it returns against the contract. Resource limits are in section 8 of the contract.
