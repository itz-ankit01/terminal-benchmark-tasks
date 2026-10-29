#include "api.hpp"
#include <iostream>
#include <stdexcept>

int main() {
    using namespace pager;
    try {
        Image in;
        std::size_t np,nr,nd;
        if (!(std::cin >> in.base_lsn >> np >> nr >> nd)) return 2;
        if (np>64 || nr>32768 || nd>128) return 2;
        in.pages.resize(np);
        for (auto& p:in.pages) {
            std::cin >> p.lsn;
            for (auto& v:p.values) std::cin >> v;
        }
        in.log.resize(nr);
        for (auto& r:in.log)
            std::cin >> r.lsn >> r.kind >> r.tx >> r.prev >> r.undo
                     >> r.page >> r.slot >> r.before >> r.after >> r.target
                     >> r.page2 >> r.slot2;
        for (std::size_t i=0;i<nd;++i) {
            std::uint32_t tx; int d; std::cin >> tx >> d;
            in.decisions[tx]=d;
        }
        if (!std::cin) return 2;
        auto actions=recover(in);
        if (actions.size()>12*nr+8*128+2*np) return 3;
        for (const auto& a:actions) {
            if (a.op=='A') {
                const auto& r=a.record;
                std::cout << "A " << r.lsn << ' ' << r.kind << ' ' << r.tx
                          << ' ' << r.prev << ' ' << r.undo << ' ' << r.page
                          << ' ' << r.slot << ' ' << r.before << ' ' << r.after
                          << ' ' << r.target << ' ' << r.page2 << ' ' << r.slot2 << '\n';
            } else if (a.op=='F') std::cout << "F " << a.upto << '\n';
            else if (a.op=='P') {
                std::cout << "P " << a.page_id << ' ' << a.page.lsn;
                for (auto v:a.page.values) std::cout << ' ' << v;
                std::cout << '\n';
            } else return 3;
        }
    } catch (...) { return 4; }
    return 0;
}
