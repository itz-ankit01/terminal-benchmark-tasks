#pragma once
#include <array>
#include <cstdint>
#include <map>
#include <vector>

namespace pager {
using LSN = std::uint64_t;
using Value = std::int64_t;
enum Kind { UPDATE=1, CLR=2, COMMIT=3, ABORT=4, END=5, PREPARE=6, NESTED=7, SWAP=8 };
struct Record {
    LSN lsn=0;
    int kind=0;
    std::uint32_t tx=0;
    LSN prev=0, undo=0;
    std::uint32_t page=0, slot=0;
    Value before=0, after=0;
    LSN target=0;
    std::uint32_t page2=0, slot2=0;
};
struct Page { LSN lsn=0; std::array<Value,8> values{}; };
struct Image {
    LSN base_lsn=0;
    std::vector<Page> pages;
    std::vector<Record> log;
    // Outside decisions for prepared transactions: 1 = commit, 2 = abort.
    std::map<std::uint32_t,int> decisions;
};
struct Action {
    // 'A': append record to the log buffer; 'F': make the log durable
    // through upto; 'P': write page to stable storage as page page_id.
    char op=0;
    Record record;
    LSN upto=0;
    std::uint32_t page_id=0;
    Page page;
};
std::vector<Action> recover(const Image&);
}
